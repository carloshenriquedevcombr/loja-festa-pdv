"""Caixa: receber, pagar, caixa do dia e desfazer (estorno). Nada é apagado."""
import uuid
from datetime import date

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import codigos, cripto, db, fotos, pagamento
from ..auth import auditar, pode, requer
from ..pagamento import formulario, formulario_com_nota
from ..web import TZ, chave_valida, checar_csrf, e_chave_repetida, mensagem_do_banco, nova_chave, tela

router = APIRouter()

MODOS = {
    "receber": {"tipo": "ENTRADA", "perm": "caixa.entrada", "titulo": "Recebi dinheiro", "cor": "entrada"},
    "pagar":   {"tipo": "SAIDA",   "perm": "caixa.saida",   "titulo": "Paguei algo",     "cor": "saida"},
}
CATEGORIAS = ("Costura e aviamentos", "Lavanderia e limpeza", "Compra de peças", "Tecidos", "Aluguel do ponto",
              "Luz, água e internet", "Embalagens", "Propaganda e redes sociais", "Salários e comissões",
              "Impostos e taxas", "Manutenção", "Frete e entregas", "Outros")


def filtro_cliente(q: str) -> tuple[str, dict]:
    """Pedaço de SQL que acha a cliente (alias cl) pelo nome, telefone inteiro, 4 finais ou CPF."""
    digitos = "".join(ch for ch in q if ch.isdigit())
    return ("""(f_unaccent(lower(cl.nome)) LIKE f_unaccent(lower(%(like)s)) OR cl.telefone_idx = %(idx)s
                OR cl.telefone_fim = %(fim)s OR cl.cpf_idx = %(cpf)s)""",
            {"like": f"%{q}%", "idx": cripto.indice_telefone(digitos) or "#",
             "fim": digitos[-4:] if 4 <= len(digitos) < 10 else "#", "cpf": cripto.indice(digitos, "cpf") or "#"})


def buscar_para_receber(cur, q: str) -> dict:
    """Recebi dinheiro → procurar: pedidos com saldo e parcelas do crediário em aberto."""
    q = q.strip()[:60]
    if len(q) < 2:
        return {"pedidos": [], "parcelas": [], "clientes": []}
    numero, _ = codigos.separar(q)
    filtro, params = filtro_cliente(q)
    params.update({"sku": f"%{q}%", "pref": f"{numero.lower()}%" if codigos.parece_codigo_pedido(numero) else "#"})
    cur.execute(f"""SELECT pe.id::text AS id, pr.sku, pr.nome AS peca, cl.nome AS cliente, pe.status, pe.tipo,
                           pe.valor_total_centavos - valor_pago(pe.id) AS saldo, pe.data_retirada
                      FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                      LEFT JOIN clientes cl ON cl.id = pe.cliente_id
                     WHERE pe.status <> 'CANCELADO' AND pe.valor_total_centavos - valor_pago(pe.id) > 0
                       AND (pr.sku ILIKE %(sku)s OR replace(pe.id::text, '-', '') LIKE %(pref)s OR {filtro})
                     ORDER BY pe.criado_em DESC LIMIT 30""", params)
    pedidos = cur.fetchall()
    cur.execute(f"""SELECT c.id::text AS id, cl.nome AS cliente, p.numero, c.parcelas, p.valor_centavos, p.vencimento,
                           p.vencimento < (now() AT TIME ZONE '{TZ}')::date AS vencida
                      FROM crediario_parcelas p JOIN crediarios c ON c.id = p.crediario_id
                      JOIN clientes cl ON cl.id = c.cliente_id
                     WHERE p.paga_em IS NULL AND c.status = 'ATIVO' AND {filtro}
                       AND p.numero = (SELECT min(x.numero) FROM crediario_parcelas x
                                        WHERE x.crediario_id = c.id AND x.paga_em IS NULL)
                     ORDER BY p.vencimento LIMIT 30""", params)
    parcelas = cur.fetchall()
    cur.execute(f"SELECT cl.id::text AS id, cl.nome, cl.telefone_fim FROM clientes cl WHERE {filtro} ORDER BY cl.nome LIMIT 15",
                params)
    return {"pedidos": pedidos, "parcelas": parcelas, "clientes": cur.fetchall()}


def _cliente_escolhido(cur, cid: str | None) -> dict | None:
    try:
        cid = str(uuid.UUID(str(cid)))
    except (ValueError, TypeError):
        return None
    cur.execute("SELECT id::text, nome, telefone_fim FROM clientes WHERE id = %s", (cid,))
    return cur.fetchone()


def _form(request, usuario, modo, valores=None, erro=None, status=200, q="", cliente=""):
    m = MODOS[modo]
    with db.transacao(usuario_id=usuario["id"]) as cur:
        opc = pagamento.opcoes(cur, modo=modo)
        cfg = pagamento.config_vigente(cur)
        extra = {}
        if modo == "pagar":
            cur.execute("SELECT id::text, nome, categoria FROM fornecedores WHERE ativo ORDER BY nome")
            extra["fornecedores"] = cur.fetchall()
        else:
            if q:
                extra["achados"] = buscar_para_receber(cur, q)
            cur.execute("SELECT id::text, nome, preco_centavos, descricao FROM servicos WHERE ativo ORDER BY nome")
            extra["servicos"] = cur.fetchall()
            extra["cliente_esc"] = _cliente_escolhido(cur, cliente or (valores or {}).get("cliente"))
    valores = valores or {}
    valores.setdefault("chave", nova_chave())
    return tela(request, "caixa_form.html", usuario, status=status, modo=modo, m=m, v=valores, erro=erro, q=q,
                max_parcelas=12 if modo == "pagar" else cfg["max_parcelas_venda"],
                teto=cfg["teto_saida_atendente_centavos"], CATEGORIAS=CATEGORIAS,
                ilimitada=pode(usuario, "caixa.saida_ilimitada"), **opc, **extra)


def _validar(cur, usuario, modo, form):
    """Devolve (pagamento, descrição, detalhes da despesa, erro)."""
    cfg = pagamento.config_vigente(cur)
    pag, erro = pagamento.ler(form, cur, cfg["max_parcelas_venda"], cfg["id"], modo=modo)
    descricao = (form.get("descricao") or "").strip()[:200]
    det = {}
    if not erro and modo == "pagar":
        fid = (form.get("fornecedor") or "").strip()
        if fid:
            try:
                fid = str(uuid.UUID(fid))
            except ValueError:
                fid = ""
            cur.execute("SELECT id::text, nome FROM fornecedores WHERE id = %s AND ativo", (fid or None,))
            forn = cur.fetchone()
            if not forn:
                erro = "Fornecedor não encontrado."
            else:
                det["fornecedor_id"], det["fornecedor"] = forn["id"], forn["nome"]
        categoria = (form.get("categoria") or "").strip()
        det["categoria"] = categoria if categoria in CATEGORIAS else None
        det["documento"] = (form.get("documento") or "").strip()[:60] or None
        anexo = (form.get("anexo") or "").strip()
        caminho = fotos.caminho_nota(anexo) if anexo else None
        det["anexo"] = anexo if caminho and caminho.exists() else None
        if not descricao and det.get("fornecedor"):
            descricao = det["fornecedor"] + (f" · {det['categoria']}" if det["categoria"] else "")
    if not erro and modo == "receber":
        # Recebimento sem pedido: sempre um serviço cadastrado e a cliente (nada de dinheiro sem motivo)
        cli = _cliente_escolhido(cur, form.get("cliente"))
        try:
            sid = str(uuid.UUID(form.get("servico") or ""))
        except ValueError:
            sid = None
        cur.execute("SELECT id::text, nome FROM servicos WHERE id = %s AND ativo", (sid,))
        serv = cur.fetchone()
        if not cli:
            erro = "Procure a cliente (ou o pedido) no topo. Recebimento sem pedido precisa da cliente."
        elif not serv:
            erro = "Escolha o serviço (ex.: limpeza, conserto). Não dá para receber sem motivo."
        else:
            det.update(servico_id=serv["id"], cliente_id=cli["id"], servico=serv["nome"], cliente=cli["nome"])
            descricao = f"{serv['nome']} · {cli['nome']}" + (f" · {descricao}" if descricao else "")
    if not erro and modo == "pagar" and not descricao:
        erro = "Escreva o que foi pago (ex.: linha de costura, conta de luz)."
    if not erro and modo == "pagar" and not pode(usuario, "caixa.saida_ilimitada") \
            and pag.valor > cfg["teto_saida_atendente_centavos"]:
        erro = "Valor acima do seu limite de saída. Chame a Gerente ou a Dona para lançar."
    if not erro and not chave_valida(form.get("chave")):
        erro = "A página ficou aberta muito tempo. Comece de novo."
    return pag, descricao or "Recebimento avulso", det, erro


for _modo in MODOS:
    def _rotas(modo=_modo):
        perm = requer(MODOS[modo]["perm"])
        ler_form = formulario_com_nota if modo == "pagar" else formulario

        @router.get(f"/caixa/{modo}")
        def abrir(request: Request, q: str = "", cliente: str = "", usuario: dict = Depends(perm)):
            return _form(request, usuario, modo, q=q, cliente=cliente)

        @router.post(f"/caixa/{modo}")
        def corrigir(request: Request, usuario: dict = Depends(perm), form: dict = Depends(ler_form)):
            checar_csrf(request, form.get("csrf"))
            form.pop("_nota", None)
            return _form(request, usuario, modo, valores=form)

        @router.post(f"/caixa/{modo}/conferir")
        def conferir(request: Request, usuario: dict = Depends(perm), form: dict = Depends(ler_form)):
            checar_csrf(request, form.get("csrf"))
            nota = form.pop("_nota", None)
            if nota:
                try:
                    form["anexo"] = fotos.salvar_nota(nota)
                except fotos.FotoInvalida as e:
                    return _form(request, usuario, modo, valores=form, erro=str(e), status=400)
            with db.transacao(usuario_id=usuario["id"]) as cur:
                pag, descricao, det, erro = _validar(cur, usuario, modo, form)
            if erro:
                return _form(request, usuario, modo, valores=form, erro=erro, status=400)
            campos = {**pag.campos(), "descricao": descricao, "chave": form["chave"]}
            if modo == "pagar":
                campos.update({"fornecedor": det.get("fornecedor_id") or "", "categoria": det["categoria"] or "",
                               "documento": det["documento"] or "", "anexo": det["anexo"] or ""})
            else:
                campos.update({"cliente": det["cliente_id"], "servico": det["servico_id"],
                               "descricao": (form.get("descricao") or "").strip()[:200]})
            return tela(request, "conferir.html", usuario, m=MODOS[modo], modo=modo, pag=pag, det=det,
                        descricao=descricao, campos=campos,
                        url_gravar=f"/caixa/{modo}/gravar", url_corrigir=f"/caixa/{modo}")

        @router.post(f"/caixa/{modo}/gravar")
        def gravar(request: Request, usuario: dict = Depends(perm), form: dict = Depends(ler_form)):
            checar_csrf(request, form.get("csrf"))
            form.pop("_nota", None)
            try:
                with db.transacao(usuario_id=usuario["id"]) as cur:
                    pag, descricao, det, erro = _validar(cur, usuario, modo, form)
                    if erro:
                        return _form(request, usuario, modo, valores=form, erro=erro, status=400)
                    det.pop("fornecedor", None)
                    det.pop("servico", None)
                    det.pop("cliente", None)
                    tid = pagamento.gravar(cur, pag, tipo=MODOS[modo]["tipo"],
                                           natureza="SERVICO" if modo == "receber" else "DESPESA",
                                           descricao=descricao, chave=form["chave"], usuario_id=usuario["id"], **det)
            except psycopg.Error as e:
                if e_chave_repetida(e):                     # clique duplo: já estava guardado
                    with db.transacao(usuario_id=usuario["id"]) as cur:
                        cur.execute("SELECT id FROM transacoes WHERE chave_idempotencia = %s", (form["chave"],))
                        tid = cur.fetchone()["id"]
                else:
                    return _form(request, usuario, modo, valores=form, erro=mensagem_do_banco(e), status=400)
            return RedirectResponse(f"/caixa/feito/{tid}", status_code=303)

    _rotas()


@router.get("/caixa/nota/{nome}")
def ver_nota(nome: str, usuario: dict = Depends(requer("caixa.saida", "caixa.fechar", "relatorios.completo"))):
    """Foto da nota: só abre se for de um lançamento desta loja (RLS) e para quem mexe no caixa."""
    caminho = fotos.caminho_nota(nome)
    if not caminho:
        return Response(status_code=404)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT 1 FROM transacoes WHERE anexo = %s LIMIT 1", (nome,))
        existe = cur.fetchone()
    if not existe or not caminho.exists():
        return Response(status_code=404)
    return FileResponse(caminho, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.get("/caixa/feito/{tid}")
def feito(request: Request, tid: int, usuario: dict = Depends(requer("caixa.entrada", "caixa.saida"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT t.*, m.apelido AS maquininha, f.nome AS fornecedor FROM transacoes t
                         LEFT JOIN maquininhas m ON m.id = t.maquininha_id
                         LEFT JOIN fornecedores f ON f.id = t.fornecedor_id WHERE t.id = %s""", (tid,))
        t = cur.fetchone()
    if not t:
        return RedirectResponse("/", status_code=303)
    return tela(request, "feito.html", usuario, t=t)


# --------------------------------------------------------------------------- #
# Caixa do dia
# --------------------------------------------------------------------------- #
@router.get("/caixa")
def caixa_do_dia(request: Request, dia: str | None = None,
                 usuario: dict = Depends(requer("caixa.fechar", "relatorios.completo"))):
    try:
        escolhido = date.fromisoformat(dia) if dia else None
    except ValueError:
        escolhido = None
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS hoje")
        hoje = cur.fetchone()["hoje"]
        escolhido = escolhido or hoje
        cur.execute(f"""
            SELECT t.id, t.tipo, t.natureza, t.valor_centavos, t.acrescimo_juros_centavos, t.forma_pagamento,
                   t.parcelas, t.descricao, t.estorno_de, t.pedido_id, t.usuario_id::text AS usuario_id,
                   to_char(t.criado_em AT TIME ZONE '{TZ}', 'HH24:MI') AS hora,
                   u.nome AS quem, m.apelido AS maquininha, p.sku, f.nome AS fornecedor, t.documento, t.anexo,
                   t.categoria,
                   EXISTS (SELECT 1 FROM transacoes e WHERE e.estorno_de = t.id) AS desfeito
              FROM transacoes t
              JOIN usuarios u ON u.id = t.usuario_id
              LEFT JOIN maquininhas m ON m.id = t.maquininha_id
              LEFT JOIN fornecedores f ON f.id = t.fornecedor_id
              LEFT JOIN pedidos pe ON pe.id = t.pedido_id
              LEFT JOIN produtos p ON p.id = pe.produto_id
             WHERE (t.criado_em AT TIME ZONE '{TZ}')::date = %s
             ORDER BY t.id DESC""", (escolhido,))
        lancamentos = cur.fetchall()
    tot = {"entrou": 0, "saiu": 0, "gaveta": 0, "por_forma": {}}
    for t in lancamentos:
        sinal = 1 if t["tipo"] == "ENTRADA" else -1
        tot["entrou" if sinal > 0 else "saiu"] += t["valor_centavos"]
        tot["por_forma"][t["forma_pagamento"]] = tot["por_forma"].get(t["forma_pagamento"], 0) + sinal * t["valor_centavos"]
        if t["forma_pagamento"] == "DINHEIRO":
            tot["gaveta"] += sinal * t["valor_centavos"]
    return tela(request, "caixa_dia.html", usuario, lancamentos=lancamentos, tot=tot,
                dia=escolhido, hoje=hoje, anterior=escolhido.fromordinal(escolhido.toordinal() - 1),
                seguinte=escolhido.fromordinal(escolhido.toordinal() + 1))


# --------------------------------------------------------------------------- #
# Desfazer = lançamento inverso (estorno), aprovado por quem não fez o original
# --------------------------------------------------------------------------- #
def _lancamento(cur, tid):
    cur.execute("""SELECT t.*, u.nome AS quem, m.apelido AS maquininha,
                          EXISTS (SELECT 1 FROM transacoes e WHERE e.estorno_de = t.id) AS desfeito
                     FROM transacoes t JOIN usuarios u ON u.id = t.usuario_id
                     LEFT JOIN maquininhas m ON m.id = t.maquininha_id WHERE t.id = %s""", (tid,))
    return cur.fetchone()


@router.get("/caixa/{tid}/desfazer")
def desfazer_form(request: Request, tid: int, usuario: dict = Depends(requer("caixa.estornar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        t = _lancamento(cur, tid)
    if not t:
        return RedirectResponse("/caixa", status_code=303)
    return tela(request, "desfazer.html", usuario, t=t)


@router.post("/caixa/{tid}/desfazer")
def desfazer(request: Request, tid: int, usuario: dict = Depends(requer("caixa.estornar")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    motivo = (form.get("motivo") or "").strip()[:150]
    erro = None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            t = _lancamento(cur, tid)
            if not t:
                return RedirectResponse("/caixa", status_code=303)
            if not motivo:
                erro = "Escreva o motivo para desfazer."
            else:
                cur.execute(
                    """INSERT INTO transacoes
                         (loja_id, tipo, natureza, valor_centavos, forma_pagamento, parcelas,
                          modalidade_cartao, maquininha_id, estorno_de, descricao,
                          chave_idempotencia, usuario_id, aprovado_por, fornecedor_id, categoria)
                       VALUES (ctx_loja(), %s, 'ESTORNO', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    ("SAIDA" if t["tipo"] == "ENTRADA" else "ENTRADA", t["valor_centavos"],
                     t["forma_pagamento"], t["parcelas"], t["modalidade_cartao"], t["maquininha_id"],
                     tid, f"Desfeito: {motivo}", str(uuid.uuid5(uuid.NAMESPACE_OID, f"estorno-{tid}")),
                     usuario["id"], usuario["id"], t["fornecedor_id"], t["categoria"]))
                auditar(cur, request, "lancamento_desfeito", usuario["id"], "transacao", str(tid),
                        {"motivo": motivo})
    except psycopg.Error as e:
        erro = "Este lançamento já foi desfeito." if (e_chave_repetida(e) or "estorno_de" in str(e)) \
            else mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            t = _lancamento(cur, tid)
        return tela(request, "desfazer.html", usuario, t=t, erro=erro, motivo=motivo, status=400)
    return RedirectResponse("/caixa", status_code=303)
