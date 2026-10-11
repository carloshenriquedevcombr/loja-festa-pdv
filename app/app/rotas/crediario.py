"""Crediário da loja: lista, parcelas, receber parcela, carnê para imprimir (com Pix) e cancelar."""
from uuid import UUID

import psycopg
from psycopg import sql
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import codigos, crediario, db, pagamento, pix
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import TZ_SQL, chave_valida, checar_csrf, e_chave_repetida, mensagem_do_banco, nova_chave, tela
from .caixa import filtro_cliente

router = APIRouter()


@router.get("/crediario")
def lista(request: Request, q: str = "", ver: str = "abertos", ok: str = "",
          usuario: dict = Depends(requer("caixa.entrada", "crediario.liberar"))):
    q = q.strip()[:60]
    ver = ver if ver in ("abertos", "vencidos", "todos") else "abertos"
    filtro, params = filtro_cliente(q)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(sql.SQL("""
            SELECT c.id::text AS id, cl.nome AS cliente, c.valor_centavos, c.parcelas, c.status, pr.sku,
                   count(p.id) FILTER (WHERE p.paga_em IS NOT NULL) AS pagas,
                   COALESCE(sum(p.valor_centavos) FILTER (WHERE p.paga_em IS NULL), 0) AS falta,
                   min(p.vencimento) FILTER (WHERE p.paga_em IS NULL) AS proximo,
                   count(p.id) FILTER (WHERE p.paga_em IS NULL
                                        AND p.vencimento < (now() AT TIME ZONE {tz})::date) AS vencidas
              FROM crediarios c JOIN clientes cl ON cl.id = c.cliente_id
              JOIN crediario_parcelas p ON p.crediario_id = c.id
              LEFT JOIN pedidos pe ON pe.id = c.pedido_id LEFT JOIN produtos pr ON pr.id = pe.produto_id
             WHERE (%(q)s = '' OR {filtro})
               AND (%(ver)s = 'todos' OR c.status = 'ATIVO')
             GROUP BY c.id, cl.nome, pr.sku
            HAVING %(ver)s <> 'vencidos' OR count(p.id) FILTER (WHERE p.paga_em IS NULL
                                        AND p.vencimento < (now() AT TIME ZONE {tz})::date) > 0
             ORDER BY min(p.vencimento) FILTER (WHERE p.paga_em IS NULL) NULLS LAST, c.criado_em DESC
             LIMIT 200""").format(tz=TZ_SQL, filtro=sql.SQL(filtro)), {**params, "q": q, "ver": ver})
        itens = cur.fetchall()
        cur.execute("SELECT pix_chave, pix_nome, pix_cidade FROM lojas WHERE id = ctx_loja()")
        loja_pix = cur.fetchone()
    totais = {"falta": sum(i["falta"] for i in itens if i["status"] == "ATIVO"),
              "vencidas": sum(i["vencidas"] for i in itens if i["status"] == "ATIVO")}
    return tela(request, "crediario_lista.html", usuario, itens=itens, q=q, ver=ver, totais=totais,
                loja_pix=loja_pix, ok=ok)


@router.post("/crediario/pix")
def salvar_pix(request: Request, usuario: dict = Depends(requer("crediario.liberar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    chave = (form.get("pix_chave") or "").strip()
    if chave and not pix.chave_valida(chave):
        return RedirectResponse("/crediario?ok=pix_invalido", status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("UPDATE lojas SET pix_chave = %s, pix_nome = %s, pix_cidade = %s WHERE id = ctx_loja()",
                    (chave or None, (form.get("pix_nome") or "").strip()[:25] or None,
                     (form.get("pix_cidade") or "").strip()[:15] or None))
        auditar(cur, request, "pix_carne_alterado", usuario["id"], "loja", None, {"tem_chave": bool(chave)})
    return RedirectResponse("/crediario?ok=pix", status_code=303)


def _tela(request, usuario, cid, erro=None, status=200, novo=0):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        c = crediario.carregar(cur, str(cid))
        tem_pix = bool(crediario.pix_da_loja(cur))
    if not c:
        return tela(request, "erro.html", usuario, status=404, titulo="Crediário não encontrado")
    return tela(request, "crediario_detalhe.html", usuario, status=status, c=c, erro=erro, novo=novo, tem_pix=tem_pix,
                pode_cancelar=pode(usuario, "crediario.liberar") and c["status"] == "ATIVO")


@router.get("/crediario/{cid}")
def detalhe(request: Request, cid: UUID, novo: int = 0,
            usuario: dict = Depends(requer("caixa.entrada", "crediario.liberar"))):
    return _tela(request, usuario, cid, novo=novo)


# --------------------------------------------------------------------------- #
# Receber uma parcela
# --------------------------------------------------------------------------- #
def _parcela(cur, cid, numero):
    c = crediario.carregar(cur, str(cid))
    if not c:
        return None, None
    return c, next((p for p in c["lista"] if p["numero"] == numero), None)


def _tela_parcela(request, usuario, cid, numero, v=None, erro=None, status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        c, p = _parcela(cur, cid, numero)
        opc = pagamento.opcoes(cur)
        cfg = pagamento.config_vigente(cur)
    if not p:
        return RedirectResponse(f"/crediario/{cid}", status_code=303)
    v = v or {}
    v.setdefault("chave", nova_chave())
    return tela(request, "crediario_parcela.html", usuario, status=status, c=c, p=p, v=v, erro=erro,
                max_parcelas=cfg["max_parcelas_venda"], **opc)


@router.get("/crediario/{cid}/parcela/{numero}")
def parcela_form(request: Request, cid: UUID, numero: int, usuario: dict = Depends(requer("caixa.entrada"))):
    return _tela_parcela(request, usuario, cid, numero)


@router.post("/crediario/{cid}/parcela/{numero}")
def parcela_receber(request: Request, cid: UUID, numero: int, usuario: dict = Depends(requer("caixa.entrada")),
                    form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    erro = None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            c, p = _parcela(cur, cid, numero)
            if not p:
                return RedirectResponse(f"/crediario/{cid}", status_code=303)
            if p["paga_em"]:
                return RedirectResponse(f"/crediario/{cid}", status_code=303)
            valor = p["valor_centavos"]
            cfg = pagamento.config_vigente(cur)
            pag, erro = pagamento.ler({**form, "valor": f"{valor // 100},{valor % 100:02d}"}, cur,
                                      cfg["max_parcelas_venda"], cfg["id"])
            if not erro and not chave_valida(form.get("chave")):
                erro = "A página ficou aberta muito tempo. Comece de novo."
            if not erro:
                tid = pagamento.gravar(cur, pag, tipo="ENTRADA", natureza="PARCELA_CREDIARIO",
                                       descricao=f"Crediário {c['cliente']} · parcela {numero}/{c['parcelas']}",
                                       chave=form["chave"], usuario_id=usuario["id"], crediario_parcela_id=p["id"])
                auditar(cur, request, "parcela_recebida", usuario["id"], "crediario", str(cid),
                        {"parcela": numero, "transacao": tid})
    except psycopg.Error as e:
        if e_chave_repetida(e):
            return RedirectResponse(f"/crediario/{cid}", status_code=303)
        erro = mensagem_do_banco(e)
    if erro:
        return _tela_parcela(request, usuario, cid, numero, v=form, erro=erro, status=400)
    return RedirectResponse(f"/caixa/feito/{tid}", status_code=303)


# --------------------------------------------------------------------------- #
# Carnê para imprimir (uma folha por parcela em aberto; Pix copia e cola com o valor certo)
# --------------------------------------------------------------------------- #
@router.get("/crediario/{cid}/carne")
def carne(request: Request, cid: UUID, formato: str = "a4", codigo: str = "barras",
          usuario: dict = Depends(requer("caixa.entrada", "crediario.liberar"))):
    formato = formato if formato in ("a4", "80") else "a4"
    codigo = codigo if codigo in ("barras", "qr") else "barras"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        c = crediario.carregar(cur, str(cid))
        dados_pix = crediario.pix_da_loja(cur)
        cur.execute("SELECT nome, telefone FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()
        if c:
            auditar(cur, request, "carne_impresso", usuario["id"], "crediario", str(cid))
    if not c:
        return tela(request, "erro.html", usuario, status=404, titulo="Crediário não encontrado")
    folhas = []
    for p in c["lista"]:
        if p["paga_em"] or c["status"] != "ATIVO":
            continue
        copia_cola = crediario.codigo_pix(dados_pix, p, c["id"])
        folhas.append({**p, "pix": copia_cola,
                       "qr": codigos.qr_svg(copia_cola, f"Pix da parcela {p['numero']}") if copia_cola else None,
                       "codigo": crediario.codigo_legivel(cod := crediario.codigo_parcela(c["id"], p["numero"])),
                       "marca_codigo": codigos.barras_svg(cod) if codigo == "barras"
                       else codigos.qr_svg(cod, f"Código da parcela {p['numero']}")})
    return tela(request, "carne.html", usuario, c=c, folhas=folhas, loja=loja, tem_pix=bool(dados_pix),
                formato=formato, codigo=codigo)


@router.post("/crediario/{cid}/cancelar")
def cancelar(request: Request, cid: UUID, usuario: dict = Depends(requer("crediario.liberar")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    motivo = (form.get("motivo") or "").strip()[:150]
    if len(motivo) < 3:
        return _tela(request, usuario, cid, erro="Escreva o motivo do cancelamento.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""UPDATE crediarios SET status = 'CANCELADO', cancelado_por = %s, motivo_cancel = %s
                            WHERE id = %s AND status = 'ATIVO'""", (usuario["id"], motivo, str(cid)))
            auditar(cur, request, "crediario_cancelado", usuario["id"], "crediario", str(cid), {"motivo": motivo})
    except psycopg.Error as e:
        return _tela(request, usuario, cid, erro=mensagem_do_banco(e), status=400)
    return RedirectResponse(f"/crediario/{cid}", status_code=303)
