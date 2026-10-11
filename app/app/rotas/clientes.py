"""Clientes: procurar, cadastrar, editar, histórico de pedidos e medidas (costura)."""
import re
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import cripto, db, medidas, pessoas
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import checar_csrf, mensagem_do_banco, tela

router = APIRouter()


def f_nome(texto: str | None) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (texto or "").lower()) if unicodedata.category(ch) != "Mn")


def so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def cpf_valido(cpf: str) -> bool:
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(cpf[n]):
            return False
    return True


def mascarar(usuario, texto: str | None, visiveis: int = 4) -> str | None:
    """Quem só vê (ex.: Desenvolvedor) enxerga dados pessoais mascarados (LGPD)."""
    if not texto or pode(usuario, "clientes.editar", "medidas.editar"):
        return texto
    return "•" * max(len(texto) - visiveis, 3) + texto[-visiveis:]


def _ler(form: dict) -> tuple[dict, str | None]:
    d = {"nome": (form.get("nome") or "").strip()[:120],
         "telefone": so_digitos(form.get("telefone"))[:13] or None,
         "cpf": so_digitos(form.get("cpf")) or None,
         "email": (form.get("email") or "").strip()[:120] or None,
         "observacoes": (form.get("observacoes") or "").strip()[:500] or None,
         "endereco": (form.get("endereco") or "").strip()[:200] or None,
         "lgpd": form.get("lgpd") == "1"}
    if len(d["nome"]) < 2:
        return d, "Escreva o nome da cliente."
    if d["telefone"] and len(d["telefone"]) not in (10, 11):
        return d, "Telefone com DDD, por exemplo (19) 99999-9999."
    if d["cpf"] and not cpf_valido(d["cpf"]):
        return d, "Este CPF não é válido. Confira os números ou deixe em branco."
    if d["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", d["email"]):
        return d, "Este e-mail não parece certo. Confira ou deixe em branco."
    if not d["lgpd"]:
        return d, "Marque que a cliente autorizou guardar os dados dela."
    return d, None


@router.get("/clientes")
def lista(request: Request, q: str = "", usuario: dict = Depends(requer("clientes.ver"))):
    q = q.strip()[:60]
    digitos = so_digitos(q)
    # Telefone é cifrado: acha pelo número inteiro (índice cego) ou pelos 4 últimos dígitos
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""
            SELECT c.id::text, c.nome, c.telefone_c,
                   count(p.id) FILTER (WHERE p.status <> 'CANCELADO') AS pedidos,
                   COALESCE(sum(p.valor_total_centavos - valor_pago(p.id))
                            FILTER (WHERE p.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')), 0) AS em_aberto
              FROM clientes c LEFT JOIN pedidos p ON p.cliente_id = c.id
             WHERE %(q)s = '' OR f_unaccent(lower(c.nome)) LIKE f_unaccent(lower(%(like)s))
                OR c.telefone_idx = %(idx)s OR c.telefone_fim = %(fim)s OR c.cpf_idx = %(cpf)s
             GROUP BY c.id ORDER BY c.nome LIMIT 100""",
                    {"q": q, "like": f"%{q}%", "idx": cripto.indice_telefone(digitos) or "#",
                     "fim": digitos[-4:] if 4 <= len(digitos) < 10 else "#", "cpf": cripto.indice(digitos, "cpf") or "#"})
        clientes = cripto.abrir_todas(cur.fetchall(), "clientes", "telefone")
    if 4 < len(digitos) < 10:          # parte do número: os 4 finais acharam; confere o resto já aberto
        clientes = [c for c in clientes if digitos in (c["telefone"] or "")
                    or f_nome(q) in f_nome(c["nome"])]
    for c in clientes:
        c["telefone"] = mascarar(usuario, c["telefone"])
    return tela(request, "clientes_lista.html", usuario, clientes=clientes, q=q)


@router.get("/clientes/nova")
def nova_form(request: Request, usuario: dict = Depends(requer("clientes.editar"))):
    return tela(request, "cliente_form.html", usuario, v={}, nova=True)


@router.post("/clientes/nova")
def nova(request: Request, usuario: dict = Depends(requer("clientes.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler(form)
    cid = cid_existente = None
    if not erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            if d["telefone"]:
                ja = pessoas.achar_por_telefone(cur, d["telefone"])
                if ja:
                    erro = f"Já existe cliente com esse telefone: {ja['nome']}."
                    cid_existente = ja["id"]
            if not erro:
                cid = pessoas.gravar_cliente(cur, d)
                auditar(cur, request, "cliente_cadastrado", usuario["id"], "cliente", cid)
    if erro:
        return tela(request, "cliente_form.html", usuario, v=form, erro=erro, nova=True, status=400,
                    existente=cid_existente)
    return RedirectResponse(f"/clientes/{cid}?nova=1", status_code=303)


def _cliente(cur, cid):
    cur.execute("SELECT *, id::text AS id FROM clientes WHERE id = %s", (cid,))
    return pessoas.abrir_cliente(cur.fetchone())


@router.get("/clientes/{cid}")
def detalhe(request: Request, cid: UUID, nova: int = 0, usuario: dict = Depends(requer("clientes.ver"))):
    from .pedidos import STATUS, TIPO_NOME
    with db.transacao(usuario_id=usuario["id"]) as cur:
        c = _cliente(cur, cid)
        if not c:
            return tela(request, "erro.html", usuario, status=404, titulo="Cliente não encontrada")
        cur.execute("""SELECT p.id::text, p.tipo, p.com_ajuste, p.status, p.data_retirada, p.valor_total_centavos,
                              p.valor_total_centavos - valor_pago(p.id) AS saldo, pr.sku, pr.nome AS peca,
                              (p.criado_em AT TIME ZONE 'America/Sao_Paulo')::date AS criado
                         FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
                        WHERE p.cliente_id = %s ORDER BY p.criado_em DESC""", (cid,))
        pedidos = cur.fetchall()
        ultimas = None
        if pode(usuario, "medidas.ver"):
            historico = _historico(cur, cid, 30)
            ultimas = [m for m in (next((h for h in historico if h["ficha"] == f), None) for f in medidas.FICHAS) if m]
    for campo in ("telefone", "cpf", "email", "endereco"):
        c[campo] = mascarar(usuario, c[campo])
    return tela(request, "cliente_detalhe.html", usuario, c=c, pedidos=pedidos, ultimas=ultimas, nova=nova,
                FICHAS=medidas.FICHAS, numero=medidas.numero, STATUS=STATUS,
                tipo_nome=lambda p: TIPO_NOME["ALUGUEL" if p["tipo"] == "ALUGUEL" else
                                              ("VENDA_AJUSTE" if p["com_ajuste"] else "VENDA_SIMPLES")])


@router.get("/clientes/{cid}/editar")
def editar_form(request: Request, cid: UUID, usuario: dict = Depends(requer("clientes.editar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        c = _cliente(cur, cid)
    if not c:
        return RedirectResponse("/clientes", status_code=303)
    v = {k: c[k] or "" for k in ("nome", "telefone", "cpf", "email", "observacoes", "endereco")}
    v["lgpd"] = "1" if c["consentimento_lgpd_em"] else ""
    return tela(request, "cliente_form.html", usuario, v=v, c=c, nova=False)


@router.post("/clientes/{cid}/editar")
def editar(request: Request, cid: UUID, usuario: dict = Depends(requer("clientes.editar")),
           form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler(form)
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                pessoas.gravar_cliente(cur, d, cid=str(cid))
                auditar(cur, request, "cliente_editado", usuario["id"], "cliente", str(cid))
        except psycopg.Error as e:
            erro = mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            c = _cliente(cur, cid)
        return tela(request, "cliente_form.html", usuario, v=form, c=c, erro=erro, nova=False, status=400)
    return RedirectResponse(f"/clientes/{cid}", status_code=303)


# --------------------------------------------------------------------------- #
# Medidas (costureira): ficha com desenho numerado; cada medição é uma linha nova
# --------------------------------------------------------------------------- #
def _historico(cur, cid, limite=20):
    cur.execute("""SELECT m.id::text, m.ficha, m.valores, m.valores_c, m.salto_cm, m.observacoes_c, u.nome AS quem,
                          to_char(m.tomada_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando
                     FROM medidas m JOIN usuarios u ON u.id = m.tomada_por
                    WHERE m.cliente_id = %s ORDER BY m.tomada_em DESC LIMIT %s""", (cid, limite))
    historico = cur.fetchall()
    for m in historico:
        m["valores"] = cripto.decifrar_json(m.pop("valores_c"), "medidas.valores", {}) if m["valores_c"] is not None \
            else (m["valores"] or {})
        cripto.abrir(m, "medidas", "observacoes")
    # Diferença para a medição anterior da mesma ficha (a costureira vê se a cliente mudou)
    for i, m in enumerate(historico):
        anterior = next((h for h in historico[i + 1:] if h["ficha"] == m["ficha"]), None)
        m["diferenca"] = {}
        if anterior:
            for k, v in m["valores"].items():
                if k in anterior["valores"] and abs(v - anterior["valores"][k]) >= 0.5:
                    m["diferenca"][k] = v - anterior["valores"][k]
    return historico


def _tela_medidas(request, usuario, cid, ficha=None, v=None, erro=None, status=200, ok=False):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT id::text, nome FROM clientes WHERE id = %s", (cid,))
        c = cur.fetchone()
        if not c:
            return RedirectResponse("/", status_code=303)
        historico = _historico(cur, cid)
    ultima = historico[0] if historico else None
    ficha = ficha if ficha in medidas.FICHAS else (ultima["ficha"] if ultima else "FEMININO")
    if v is None:
        v = {"ficha": ficha, "salto": medidas.numero(ultima["salto_cm"]) if ultima and ultima["salto_cm"] else ""}
        for nome_ficha in medidas.FICHAS:
            base = next((h for h in historico if h["ficha"] == nome_ficha), None)
            if base:
                v.update({f"m_{nome_ficha}_{k}": medidas.numero(val) for k, val in base["valores"].items()})
    ultimas = {f: next((h for h in historico if h["ficha"] == f), None) for f in medidas.FICHAS}
    sugestao = medidas.sugestao_masculina(ultimas["MASCULINO"]["valores"]) if ultimas["MASCULINO"] else {}
    return tela(request, "medidas.html", usuario, status=status, c=c, historico=historico, v=v, erro=erro, ok=ok,
                ficha=v.get("ficha", ficha), FICHAS=medidas.FICHAS, ultimas=ultimas, sugestao=sugestao,
                numero=medidas.numero, editar=pode(usuario, "medidas.editar"))


@router.get("/clientes/{cid}/medidas")
def medidas_form(request: Request, cid: UUID, ficha: str = "", ok: int = 0,
                 usuario: dict = Depends(requer("medidas.ver"))):
    return _tela_medidas(request, usuario, cid, ficha=ficha or None, ok=bool(ok))


@router.post("/clientes/{cid}/medidas")
def medidas_salvar(request: Request, cid: UUID, usuario: dict = Depends(requer("medidas.editar")),
                   form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    ficha = form.get("ficha") if form.get("ficha") in medidas.FICHAS else "FEMININO"
    valores, salto, erro = medidas.ler(ficha, form)
    if erro:
        return _tela_medidas(request, usuario, cid, v={**form, "ficha": ficha}, erro=erro, status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""INSERT INTO medidas (loja_id, cliente_id, ficha, valores_c, salto_cm, observacoes_c, tomada_por)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s)""",
                    (cid, ficha, cripto.cifrar_json(valores, "medidas.valores"), salto,
                     cripto.cifrar((form.get("observacoes") or "").strip()[:300] or None, "medidas.observacoes"),
                     usuario["id"]))
        auditar(cur, request, "medidas_tomadas", usuario["id"], "cliente", str(cid), {"ficha": ficha, "pontos": len(valores)})
    return RedirectResponse(f"/clientes/{cid}/medidas?ok=1&ficha={ficha}", status_code=303)


@router.get("/clientes/{cid}/medidas/imprimir")
def medidas_imprimir(request: Request, cid: UUID, ficha: str = "FEMININO", em_branco: int = 0,
                     usuario: dict = Depends(requer("medidas.ver"))):
    ficha = ficha if ficha in medidas.FICHAS else "FEMININO"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT id::text, nome, telefone_c FROM clientes WHERE id = %s", (cid,))
        c = pessoas.abrir_cliente(cur.fetchone())
        if not c:
            return RedirectResponse("/", status_code=303)
        m = None if em_branco else next((h for h in _historico(cur, cid) if h["ficha"] == ficha), None)
        cur.execute("""SELECT pr.sku, pr.nome, p.data_retirada FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
                        WHERE p.cliente_id = %s AND p.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
                        ORDER BY p.data_retirada NULLS LAST LIMIT 1""", (cid,))
        pedido = cur.fetchone()
        cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()["nome"]
    c["telefone"] = mascarar(usuario, c["telefone"])
    return tela(request, "medidas_imprimir.html", usuario, c=c, m=m, ficha=ficha, FICHAS=medidas.FICHAS,
                pedido=pedido, loja=loja, numero=medidas.numero)
