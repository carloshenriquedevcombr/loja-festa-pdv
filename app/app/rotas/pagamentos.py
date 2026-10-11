"""Link de pagamento: gerar no pedido, mandar por WhatsApp, conferir, aviso do Mercado Pago."""
import json
import logging
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import db, pagamento_online as po
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import TZ, brl, checar_csrf, ler_json, ler_reais, tela

router = APIRouter()
log = logging.getLogger("loja")


def texto_whatsapp(loja: str, cliente: str, valor: int, url_curta: str, sku: str, provedor: str = "MERCADO_PAGO") -> str:
    primeiro = (cliente or "").split()[0] if cliente else ""
    como = ("Abra o link e pague com o Pix (QR code ou copia e cola)." if provedor == "PIX_LOJA"
            else "Aceita Pix e cartão.")
    return (f"Olá{', ' + primeiro if primeiro else ''}! Aqui é da {loja}. Segue o link para o pagamento "
            f"de {brl(valor)} referente à peça {sku}: {url_curta}\n"
            f"{como} O link vale por {po.HORAS_VALIDADE} horas.")


def links_do_pedido(cur, pedido_id: str, base_url: str) -> list[dict]:
    cur.execute(f"""SELECT l.id::text, l.valor_centavos, l.status, l.forma_paga, l.observacao, l.url_externa,
                           l.provedor, l.pix_copia_cola,
                           to_char(l.criado_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS criado,
                           to_char(l.expira_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS expira,
                           to_char(l.pago_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS pago,
                           l.expira_em < now() AS vencido, u.nome AS quem
                      FROM links_pagamento l JOIN usuarios u ON u.id = l.criado_por
                     WHERE l.pedido_id = %s ORDER BY l.criado_em DESC""", (pedido_id,))
    links = cur.fetchall()
    for l in links:
        l["curto"] = f"{base_url}/pagar/{l['id']}"
    return links


def base_publica(request: Request) -> str:
    from .. import config
    return config.SITE_URL or str(request.base_url).rstrip("/")


@router.post("/pedidos/{pid}/link")
def gerar_link(request: Request, pid: UUID, usuario: dict = Depends(requer("pagamento.link")),
               form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    erro = None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""SELECT pe.*, pe.id::text AS id, pr.sku, pe.valor_total_centavos - valor_pago(pe.id) AS saldo
                             FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id WHERE pe.id = %s""", (pid,))
            pe = cur.fetchone()
            if not pe:
                return RedirectResponse("/pedidos", status_code=303)
            valor = ler_reais(form.get("valor")) or pe["saldo"]
            if pe["status"] in ("CANCELADO", "CONCLUIDO", "RETIRADO", "VENDIDO"):
                erro = "Este pedido está encerrado."
            elif pe["saldo"] <= 0:
                erro = "Este pedido já está quitado."
            elif valor > pe["saldo"]:
                erro = f"O link não pode passar do que falta pagar ({brl(pe['saldo'])})."
            else:
                if form.get("provedor") == "PIX_LOJA":
                    link = po.criar_link_pix(cur, pe, valor, usuario["id"])
                else:
                    link = po.criar_link(cur, pe, valor, usuario["id"], f"Pagamento {pe['sku']}")
                auditar(cur, request, "link_pagamento_criado", usuario["id"], "pedido", str(pid),
                        {"link": link["id"], "valor": valor})
    except po.ErroProvedor as e:
        erro = str(e)
    if erro:
        return RedirectResponse(f"/pedidos/{pid}?erro_link={quote(erro)}#pagamento-online", status_code=303)
    return RedirectResponse(f"/pedidos/{pid}?link=1#pagamento-online", status_code=303)


def _link(cur, lid):
    cur.execute("SELECT id::text, pedido_id::text, status FROM links_pagamento WHERE id = %s", (lid,))
    return cur.fetchone()


@router.post("/pagamentos/links/{lid}/conferir")
def conferir(request: Request, lid: UUID, usuario: dict = Depends(requer("pagamento.link", "caixa.entrada")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        link = _link(cur, lid)
    if not link:
        return RedirectResponse("/pedidos", status_code=303)
    try:
        status = po.conferir_link(link["id"])
        recado = {"PAGO": "Pagamento recebido e lançado no caixa!",
                  "PAGO_CONFERIR": "Pagamento recebido, mas precisa de conferência (veja o aviso).",
                  "ABERTO": "Ainda não foi pago."}[status]
    except po.ErroProvedor as e:
        recado = str(e)
    return RedirectResponse(f"/pedidos/{link['pedido_id']}?recado_link={quote(recado)}#pagamento-online",
                            status_code=303)


@router.post("/pagamentos/links/{lid}/pix-recebido")
def pix_recebido(request: Request, lid: UUID, usuario: dict = Depends(requer("caixa.entrada")),
                 form: dict = Depends(formulario)):
    """Link Pix da loja: quem viu o Pix no extrato do banco confirma, e o valor entra no caixa."""
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        link = _link(cur, lid)
        if not link:
            return RedirectResponse("/pedidos", status_code=303)
        ok = po.confirmar_pix(cur, str(lid), usuario["id"])
        if ok:
            auditar(cur, request, "link_pix_confirmado", usuario["id"], "pedido", link["pedido_id"], {"link": str(lid)})
    recado = "Pix confirmado e lançado no caixa!" if ok else "Este link não está mais aberto."
    return RedirectResponse(f"/pedidos/{link['pedido_id']}?recado_link={quote(recado)}#pagamento-online", status_code=303)


@router.post("/pagamentos/links/{lid}/cancelar")
def cancelar(request: Request, lid: UUID, usuario: dict = Depends(requer("pagamento.link")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        link = _link(cur, lid)
        if not link:
            return RedirectResponse("/pedidos", status_code=303)
        cur.execute("""UPDATE links_pagamento SET status = 'CANCELADO', observacao = 'Cancelado na loja'
                        WHERE id = %s AND status = 'ABERTO' AND pagamento_externo_id IS NULL""", (lid,))
        auditar(cur, request, "link_pagamento_cancelado", usuario["id"], "pedido", link["pedido_id"], {"link": str(lid)})
    return RedirectResponse(f"/pedidos/{link['pedido_id']}#pagamento-online", status_code=303)


# --------------------------------------------------------------------------- #
# Público: link curto para a cliente e aviso do Mercado Pago
# --------------------------------------------------------------------------- #
@router.get("/pagar/{lid}")
def pagar(request: Request, lid: UUID):
    from .site import _pagina
    with db.transacao() as cur:
        cur.execute("""SELECT l.status, l.url_externa, l.expira_em < now() AS vencido, l.provedor, l.pix_copia_cola,
                              l.valor_centavos, pr.sku, pr.nome AS peca
                         FROM links_pagamento l JOIN pedidos pe ON pe.id = l.pedido_id
                         JOIN produtos pr ON pr.id = pe.produto_id WHERE l.id = %s""", (lid,))
        l = cur.fetchone()
        if l and l["status"] == "ABERTO" and not l["vencido"] and l["url_externa"]:
            return RedirectResponse(l["url_externa"], status_code=303)
        if l and l["status"] == "ABERTO" and not l["vencido"] and l["provedor"] == "PIX_LOJA" and l["pix_copia_cola"]:
            from ..codigos import qr_svg
            return _pagina(request, "site/pagamento.html", cur, situacao="pix", link=l,
                           qr=qr_svg(l["pix_copia_cola"], "QR code do Pix"))
        situacao = "inexistente" if not l else "pago" if l["status"] in ("PAGO", "PAGO_CONFERIR") else "vencido"
        return _pagina(request, "site/pagamento.html", cur, situacao=situacao)


@router.get("/site/pagamento")
def voltou_do_pagamento(request: Request, status: str = "", external_reference: str = ""):
    """O Mercado Pago devolve a cliente para cá depois de pagar."""
    from .site import _pagina
    situacao = "aprovado" if status == "approved" else ("pendente" if status in ("pending", "in_process") else "falhou")
    if situacao == "aprovado":
        try:
            if external_reference.startswith("C-"):
                situacao = "compra_paga" if po.conferir_compra(external_reference[2:]) == "PAGA" else situacao
            else:
                UUID(external_reference)
                po.conferir_link(external_reference)
        except (ValueError, po.ErroProvedor):
            pass
    with db.transacao() as cur:
        return _pagina(request, "site/pagamento.html", cur, situacao=situacao)


@router.post("/pagamentos/mercadopago/aviso")
async def aviso_mercado_pago(request: Request):
    """Webhook. Responde 200 sempre que entendeu o aviso (senão o Mercado Pago fica repetindo)."""
    try:
        corpo = ler_json((await request.body()) or b"{}", limite=20000)
    except ValueError:
        corpo = {}
    if not isinstance(corpo, dict):
        corpo = {}
    tipo = corpo.get("type") or corpo.get("topic") or request.query_params.get("type") or request.query_params.get("topic")
    pag_id = str((corpo.get("data") or {}).get("id") or request.query_params.get("data.id")
                 or request.query_params.get("id") or "")
    if tipo != "payment" or not pag_id.isdigit():
        return JSONResponse({"ok": True})
    from fastapi.concurrency import run_in_threadpool

    def processar():
        with db.transacao() as cur:
            token = po.token_da_loja(cur)
        if not token:
            return
        p = po.pagamento(token, pag_id)
        ref = str(p.get("external_reference") or "")
        if ref.startswith("C-"):                  # sacola do site
            po.aplicar_compra(ref[2:], p)
            return
        UUID(ref)
        po.aplicar(ref, p)
    try:
        await run_in_threadpool(processar)
    except ValueError:
        return JSONResponse({"ok": True})                     # pagamento que não é de link nosso
    except po.ErroProvedor as e:
        log.warning("aviso do mercado pago não aplicado", extra={"evento": "webhook_mp", "erro": str(e)[:200]})
        return JSONResponse({"ok": False}, status_code=503)   # o Mercado Pago tenta de novo depois
    return JSONResponse({"ok": True})
