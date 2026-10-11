"""Contrato assinado na tela (tablet/celular), página da cliente e verificação pelo código."""
import base64
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from .. import avisos, codigos, contrato, cripto, db, documentos, pdf
from ..auth import auditar, ip, requer
from ..pagamento import formulario
from ..web import checar_csrf, tela
from .pedidos import STATUS

router = APIRouter()


def _pagina(request, usuario, pid, erro=None, status=200, ok=""):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = contrato.dados_do_pedido(cur, str(pid))
        if not p:
            return tela(request, "erro.html", usuario, status=404, titulo="Pedido não encontrado")
        assinado = contrato.assinado(cur, str(pid))
        texto = assinado["texto"] if assinado else contrato.texto(p)
    return tela(request, "contrato.html", usuario, status=status, p=p, texto=texto, assinado=assinado, erro=erro,
                ok=ok, png=base64.b64encode(assinado["assinatura"]).decode() if assinado else None)


@router.get("/pedidos/{pid}/contrato")
def ver(request: Request, pid: UUID, ok: str = "", usuario: dict = Depends(requer("pedidos.ver"))):
    return _pagina(request, usuario, pid, ok=ok)


@router.post("/pedidos/{pid}/contrato")
def assinar(request: Request, pid: UUID, usuario: dict = Depends(requer("pedidos.criar")),
            form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    if form.get("aceite") != "1":
        return _pagina(request, usuario, pid, erro="A cliente precisa marcar que leu e concorda.", status=400)
    try:
        png = contrato.ler_assinatura(form.get("assinatura") or "")
    except contrato.ContratoInvalido as e:
        return _pagina(request, usuario, pid, erro=str(e), status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = contrato.dados_do_pedido(cur, str(pid))
        if not p or not p["cliente"]:
            return _pagina(request, usuario, pid, erro="Este pedido não tem cliente para assinar.", status=400)
        if p["status"] == "CANCELADO":
            return _pagina(request, usuario, pid, erro="Pedido cancelado não recebe contrato.", status=400)
        if contrato.assinado(cur, str(pid)):
            return RedirectResponse(f"/pedidos/{pid}/contrato", status_code=303)
        texto = contrato.texto(p)
        quando = datetime.now(timezone.utc)
        codigo = contrato.novo_codigo()
        cur.execute("""INSERT INTO contratos (loja_id, pedido_id, texto_c, assinatura_c, assinante, codigo, hash,
                                              assinado_em, ip, navegador, atendente_id)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s::inet, %s, %s)""",
                    (str(pid), cripto.cifrar(texto, "contratos.texto"), cripto.cifrar(png, "contratos.assinatura"),
                     p["cliente"], codigo, contrato.calcular_hash(str(pid), texto, png, quando), quando, ip(request),
                     (request.headers.get("user-agent") or "")[:200], usuario["id"]))
        auditar(cur, request, "contrato_assinado", usuario["id"], "pedido", str(pid), {"codigo": codigo})
        # Cliente recebe o contrato assinado no WhatsApp e no e-mail
        doc = documentos.pedido(cur, str(pid), STATUS, base=codigos.base(request))
        arquivo = pdf.gerar(doc, "a4")
        url = avisos.link(f"/site/meu-pedido/{p['token_publico']}", str(request.base_url))
        texto_msg = (f"Olá, {avisos.primeiro_nome(p['cliente'])}! Seu contrato na {p['loja']} foi assinado ✍\n"
                     f"Código de verificação: {codigo}\nVeja quando quiser: {url}")
        avisos.para_cliente(cur, p["cliente_id"], texto_msg, "contrato", f"contrato:{pid}",
                            anexo=arquivo, anexo_nome=f"contrato-{codigo}.pdf")
        avisos.email_cliente(cur, p["cliente_id"], f"Seu contrato - {p['loja']}", texto_msg, "contrato",
                             f"contrato-email:{pid}", anexo=arquivo, anexo_nome=f"contrato-{codigo}.pdf")
    return RedirectResponse(f"/pedidos/{pid}/contrato?ok={quote('Contrato assinado! A cliente recebe uma cópia.')}",
                            status_code=303)


@router.post("/pedidos/{pid}/enviar-comprovante")
def enviar_comprovante(request: Request, pid: UUID, usuario: dict = Depends(requer("pedidos.ver")),
                       form: dict = Depends(formulario)):
    """Manda de novo o comprovante (com o contrato) por WhatsApp e e-mail."""
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = contrato.dados_do_pedido(cur, str(pid))
        if not p or not p["cliente_id"]:
            return RedirectResponse(f"/pedidos/{pid}", status_code=303)
        arquivo = pdf.gerar(documentos.pedido(cur, str(pid), STATUS, base=codigos.base(request)), "a4")
        url = avisos.link(f"/site/meu-pedido/{p['token_publico']}", str(request.base_url))
        msg = f"Olá, {avisos.primeiro_nome(p['cliente'])}! Segue o comprovante do seu pedido na {p['loja']}: {url}"
        agora = datetime.now().strftime("%Y%m%d%H%M")
        zap = avisos.para_cliente(cur, str(p["cliente_id"]), msg, "comprovante", f"comprovante:{pid}:{agora}",
                                  anexo=arquivo, anexo_nome="comprovante.pdf")
        mail = avisos.email_cliente(cur, str(p["cliente_id"]), f"Comprovante - {p['loja']}", msg, "comprovante",
                                    f"comprovante-email:{pid}:{agora}", anexo=arquivo, anexo_nome="comprovante.pdf")
        auditar(cur, request, "comprovante_enviado", usuario["id"], "pedido", str(pid), {"whatsapp": zap, "email": mail})
    recado = ("Enviado " + " e ".join(x for x, ok in (("no WhatsApp", zap), ("por e-mail", mail)) if ok) + ".") \
        if zap or mail else "Não deu para enviar: falta WhatsApp/e-mail da cliente ou da loja."
    return RedirectResponse(f"/pedidos/{pid}?recado_link={quote(recado)}", status_code=303)


# --------------------------------------------------------------------------- #
# Público: página da cliente (link secreto) e verificação pelo código
# --------------------------------------------------------------------------- #
def _por_token(cur, token: str):
    if not token.isalnum() or len(token) != 36:
        return None
    cur.execute("SELECT id::text FROM pedidos WHERE token_publico = %s", (token,))
    linha = cur.fetchone()
    return linha["id"] if linha else None


@router.get("/site/meu-pedido/{token}")
def meu_pedido(request: Request, token: str):
    from .site import _pagina as pagina_site
    with db.transacao() as cur:
        pid = _por_token(cur, token)
        if not pid:
            return pagina_site(request, "site/nao_encontrada.html", cur)
        doc = documentos.pedido(cur, pid, STATUS, lambda t: (("•" * max(len(t) - 4, 3)) + t[-4:]) if t else t, base=codigos.base(request),
                                publico=True)
        assinado = contrato.assinado(cur, pid)
        return pagina_site(request, "site/meu_pedido.html", cur, doc=doc, token=token, assinado=assinado,
                           texto=contrato.mascarar_cpf(assinado["texto"]) if assinado else None,
                           png=base64.b64encode(assinado["assinatura"]).decode() if assinado else None)


@router.get("/site/meu-pedido/{token}/pdf")
def meu_pedido_pdf(request: Request, token: str):
    with db.transacao() as cur:
        pid = _por_token(cur, token)
        if not pid:
            return Response(status_code=404)
        doc = documentos.pedido(cur, pid, STATUS, lambda t: (("•" * max(len(t) - 4, 3)) + t[-4:]) if t else t, base=codigos.base(request),
                                publico=True)
    return Response(pdf.gerar(doc, "a4"), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="pedido-{doc["numero"]}.pdf"'})


@router.get("/site/contrato")
def verificar(request: Request, codigo: str = ""):
    """Verificar pedido e contrato: código do contrato, número do pedido (até dos antigos) ou o QR da via da cliente."""
    import json as _json
    from .. import codigos
    from .site import _pagina as pagina_site
    bruto = codigo.strip()[:20]
    numero, conferido = codigos.separar(bruto)
    with db.transacao() as cur:
        resultado = None
        if numero:
            cur.execute("SELECT verificar_codigo(%s) AS r", (numero,))
            r = cur.fetchone()["r"]
            resultado = (_json.loads(r) if isinstance(r, str) else r) or {}
        return pagina_site(request, "site/verificar_contrato.html", cur, codigo=bruto.upper(), resultado=resultado,
                           conferido=conferido, STATUS=STATUS)


@router.get("/v/{codigo}")
def qr(request: Request, codigo: str):
    """Destino do QR da via da cliente. Funcionário logado vai direto para o pedido no sistema;
    qualquer outra pessoa vê a página pública de verificação (situação e histórico, sem dados pessoais)."""
    from .. import codigos
    numero, _ = codigos.separar(codigo)
    uid = request.session.get("usuario_id")
    if uid and numero:
        try:
            from ..auth import usuario_atual
            usuario = usuario_atual(request)
            if "pedidos.ver" in usuario["permissoes"]:
                with db.transacao(usuario_id=usuario["id"]) as cur:
                    cur.execute("SELECT verificar_codigo(%s) AS r", (numero,))
                    r = cur.fetchone()["r"]
                if r:
                    import json as _json
                    pid = (_json.loads(r) if isinstance(r, str) else r)["pedido_id"]
                    return RedirectResponse(f"/pedidos/{pid}", status_code=303)
            return RedirectResponse("/", status_code=303)
        except Exception:                       # sessão vencida etc.: segue para a página pública
            pass
    return RedirectResponse(f"/site/contrato?codigo={quote(codigo[:20])}", status_code=303)


# --------------------------------------------------------------------------- #
# Configurações: texto do contrato (com histórico; o contrato assinado guarda o texto daquele dia)
# --------------------------------------------------------------------------- #
def _tela_modelo(request, usuario, texto=None, erro=None, ok="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT m.texto, to_char(m.definido_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando,
                              u.nome FROM contrato_modelos m JOIN usuarios u ON u.id = m.definido_por
                        ORDER BY m.definido_em DESC LIMIT 6""")
        versoes = cur.fetchall()
        cur.execute("SELECT contrato_texto FROM lojas WHERE id = ctx_loja()")
        antigo = cur.fetchone()["contrato_texto"]
    atual = texto if texto is not None else (versoes[0]["texto"] if versoes else (antigo or contrato.PADRAO))
    try:
        previa = contrato.texto({**contrato.exemplo(), "contrato_texto": atual}) if not erro else None
    except (ValueError, IndexError, KeyError):
        previa = None
    return tela(request, "config_contrato.html", usuario, status=status, texto=atual, previa=previa, versoes=versoes,
                campos=contrato.DESCRICAO_CAMPOS, erro=erro, ok=ok or request.query_params.get("ok", ""))


@router.get("/config/contrato")
def modelo(request: Request, usuario: dict = Depends(requer("contrato.editar"))):
    return _tela_modelo(request, usuario)


@router.post("/config/contrato")
def salvar_modelo(request: Request, usuario: dict = Depends(requer("contrato.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    texto = contrato.PADRAO if form.get("padrao") == "1" else (form.get("texto") or "").replace("\r\n", "\n").strip()[:30000]
    if len(texto) < 200:
        return _tela_modelo(request, usuario, texto, "O contrato está curto demais.", status=400)
    erro = contrato.conferir_modelo(texto)
    if erro:
        return _tela_modelo(request, usuario, texto, erro, status=400)
    if form.get("acao") == "previa":
        return _tela_modelo(request, usuario, texto, ok="Prévia atualizada (ainda não salvo).")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("INSERT INTO contrato_modelos (loja_id, texto, definido_por) VALUES (ctx_loja(), %s, %s)",
                    (texto, usuario["id"]))
        auditar(cur, request, "contrato_modelo_alterado", usuario["id"], "loja")
    return RedirectResponse(f"/config/contrato?ok={quote('Contrato salvo. Vale para os próximos contratos assinados.')}",
                            status_code=303)
