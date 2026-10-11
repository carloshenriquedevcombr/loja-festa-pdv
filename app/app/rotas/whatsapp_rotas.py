"""WhatsApp da loja (WPPConnect): conectar com código, e-mail Gmail, avisos recebidos (webhook).

Mensagens que CHEGAM no WhatsApp da loja:
  * de um funcionário cadastrado, começando com # : comando (#caixa, #hoje, #pedidos, #site, #ajuda)
    ou resposta ao chat do site (#K3F9 texto);
  * de cliente "SAIR": para de receber avisos.
O endereço do aviso tem um segredo aleatório; sem ele, nada é aceito.
"""
import hmac
import logging
import re
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from .. import avisos, cripto, db, whatsapp
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import checar_csrf, ler_json, tela

router = APIRouter()
log = logging.getLogger("loja")
AJUDA = ("Comandos da loja:\n#caixa · caixa de hoje\n#ontem · caixa de ontem\n#hoje · agenda de hoje\n"
         "#pedidos · pedidos em andamento\n#atrasos · peças com devolução atrasada\n#site · pedidos de prova do site\n"
         "#online · quem está online (equipe e clientes)\n#ajuda · esta lista")


def _tela(request, usuario, ok="", erro="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cfg = whatsapp.config_loja(cur)
        cur.execute("SELECT remetente, ativo FROM email_config")
        email = cur.fetchone()
        cur.execute("""SELECT status, count(*) AS n FROM mensagens
                        WHERE criado_em > now() - interval '7 days' GROUP BY status""")
        fila = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT abre_as, fecha_as, dias_abertos, relatorio_as FROM lojas WHERE id = ctx_loja()")
        horario = cur.fetchone()
    if cfg:
        cfg.pop("segredo", None)
        cfg.pop("token", None)
    return tela(request, "config_whatsapp.html", usuario, status=status, cfg=cfg, email=email, fila=fila,
                horario=horario, ok=ok or request.query_params.get("ok", ""), erro=erro,
                url_webhook=whatsapp.url_webhook(cfg) if cfg else "")


@router.get("/config/whatsapp")
def ver(request: Request, usuario: dict = Depends(requer("whatsapp.config"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cfg = whatsapp.config_loja(cur)
        if cfg and cfg["status"] in ("CONECTANDO", "CODIGO", "QRCODE") and cfg["token"]:
            try:
                whatsapp.atualizar_status(cur)
            except whatsapp.ErroWhatsApp:
                pass
    return _tela(request, usuario)


@router.post("/config/whatsapp")
def salvar(request: Request, usuario: dict = Depends(requer("whatsapp.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    url = (form.get("url") or "").strip().rstrip("/")
    sessao = (form.get("sessao") or "").strip()
    numero = whatsapp.numero_whatsapp(form.get("numero"))
    segredo = (form.get("segredo") or "").strip() or None
    endereco = (form.get("endereco_sistema") or "").strip().rstrip("/")
    if not re.fullmatch(r"https?://[\w.:-]+(/[\w./-]*)?", url):
        return _tela(request, usuario, erro="Endereço do WPPConnect inválido. Ex.: http://wppconnect:21465", status=400)
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", sessao):
        return _tela(request, usuario, erro="Nome da sessão: só letras, números, _ ou - (ex.: loja).", status=400)
    if not numero:
        return _tela(request, usuario, erro="Número do WhatsApp da loja com DDD, ex.: (19) 3333-0000.", status=400)
    if endereco and not re.fullmatch(r"https?://[\w.:-]+", endereco):
        return _tela(request, usuario, erro="Endereço do sistema inválido. Ex.: https://loja.seudominio.com.br", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT 1 FROM whatsapp_config")
        if not cur.fetchone() and not segredo:
            return _tela(request, usuario, erro="Cole a chave secreta (SECRET_KEY) do WPPConnect.", status=400)
        whatsapp.salvar_config(cur, url, sessao, segredo, numero, endereco)
        auditar(cur, request, "whatsapp_configurado", usuario["id"], "loja", None, {"url": url, "sessao": sessao})
    return RedirectResponse(f"/config/whatsapp?ok={quote('Dados salvos. Agora toque em Gerar código.')}", status_code=303)


@router.post("/config/whatsapp/conectar")
def conectar(request: Request, usuario: dict = Depends(requer("whatsapp.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            whatsapp.conectar(cur)
            auditar(cur, request, "whatsapp_conectando", usuario["id"])
    except whatsapp.ErroWhatsApp as e:
        return _tela(request, usuario, erro=str(e), status=400)
    return RedirectResponse("/config/whatsapp#conexao", status_code=303)


@router.post("/config/whatsapp/desconectar")
def desconectar(request: Request, usuario: dict = Depends(requer("whatsapp.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        whatsapp.desconectar(cur)
        auditar(cur, request, "whatsapp_desconectado", usuario["id"])
    return RedirectResponse(f"/config/whatsapp?ok={quote('WhatsApp desconectado.')}", status_code=303)


@router.post("/config/whatsapp/teste")
def teste(request: Request, usuario: dict = Depends(requer("whatsapp.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    from .. import pessoas
    with db.transacao(usuario_id=usuario["id"]) as cur:
        fone = pessoas.telefone_usuario(cur, usuario["id"])
        if not fone:
            return _tela(request, usuario, erro="Cadastre o seu WhatsApp em Equipe para receber o teste.", status=400)
        avisos.whatsapp(cur, fone, f"✅ Teste do sistema da loja. Se você recebeu, os avisos estão funcionando.",
                        "teste")
    return RedirectResponse(f"/config/whatsapp?ok={quote('Mensagem de teste na fila. Chega em alguns segundos.')}",
                            status_code=303)


@router.post("/config/email")
def salvar_email(request: Request, usuario: dict = Depends(requer("whatsapp.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    remetente = (form.get("remetente") or "").strip().lower()
    senha = re.sub(r"\s", "", form.get("senha") or "")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", remetente):
        return _tela(request, usuario, erro="Escreva o Gmail da loja (ex.: lojafesta@gmail.com).", status=400)
    if not re.fullmatch(r"[a-z]{16}", senha):
        return _tela(request, usuario, erro="A senha de app do Google tem 16 letras (ex.: abcd efgh ijkl mnop).",
                     status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE email_config SET remetente=%s, senha_c=%s, ativo=true, definido_por=%s, definido_em=now()""",
                    (remetente, cripto.cifrar(senha, "email.senha"), usuario["id"]))
        if cur.rowcount == 0:
            cur.execute("""INSERT INTO email_config (loja_id, remetente, senha_c, definido_por)
                           VALUES (ctx_loja(), %s, %s, %s)""", (remetente, cripto.cifrar(senha, "email.senha"), usuario["id"]))
        auditar(cur, request, "email_configurado", usuario["id"], "loja", None, {"remetente": remetente})
    return RedirectResponse(f"/config/whatsapp?ok={quote('E-mail salvo.')}#email", status_code=303)


@router.post("/config/horario")
def salvar_horario(request: Request, usuario: dict = Depends(requer("whatsapp.config", "config.pagamento")),
                   form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    from datetime import time
    try:
        abre, fecha = time.fromisoformat(form.get("abre") or ""), time.fromisoformat(form.get("fecha") or "")
        relatorio = time.fromisoformat(form.get("relatorio") or "19:30")
    except ValueError:
        return _tela(request, usuario, erro="Horário inválido (use 08:00).", status=400)
    dias = sorted(int(d) for d in range(1, 8) if form.get(f"dia_{d}") == "1")
    if not dias or fecha <= abre:
        return _tela(request, usuario, erro="Marque os dias e um horário de fechar depois do de abrir.", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("UPDATE lojas SET abre_as=%s, fecha_as=%s, dias_abertos=%s, relatorio_as=%s WHERE id = ctx_loja()",
                    (abre, fecha, dias, relatorio))
        auditar(cur, request, "horario_alterado", usuario["id"], "loja")
    return RedirectResponse(f"/config/whatsapp?ok={quote('Horário salvo.')}#horario", status_code=303)


# --------------------------------------------------------------------------- #
# Aviso do WPPConnect (mensagens recebidas, código de conexão, status)
# --------------------------------------------------------------------------- #
def _comando(cur, texto: str, uid: str, permissoes: set) -> str | None:
    from ..trabalhador import resumo_caixa
    cmd = texto.strip().split()[0].lower()
    if cmd == "#ajuda":
        return AJUDA
    if cmd in ("#caixa", "#ontem") and permissoes & {"relatorios.completo", "caixa.fechar", "relatorios.dia"}:
        dia = date.today() if cmd == "#caixa" else date.fromordinal(date.today().toordinal() - 1)
        return resumo_caixa(cur, dia)
    if cmd == "#hoje" and "agenda.ver" in permissoes:
        cur.execute("""SELECT to_char(a.inicio AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS hora, a.tipo, cl.nome, pr.sku
                         FROM agendamentos a JOIN pedidos p ON p.id = a.pedido_id JOIN produtos pr ON pr.id = p.produto_id
                         LEFT JOIN clientes cl ON cl.id = p.cliente_id
                        WHERE a.status IN ('AGENDADO','REMARCADO')
                          AND (a.inicio AT TIME ZONE 'America/Sao_Paulo')::date = (now() AT TIME ZONE 'America/Sao_Paulo')::date
                        ORDER BY a.inicio""")
        itens = cur.fetchall()
        return "📅 Hoje:\n" + ("\n".join(f"{i['hora']} · {i['tipo'].lower().replace('_', ' ')} · {i['nome'] or '—'} · {i['sku']}"
                                         for i in itens) or "nada marcado")
    if cmd == "#pedidos" and "pedidos.ver" in permissoes:
        cur.execute("""SELECT status, count(*) AS n FROM pedidos
                        WHERE status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO') GROUP BY status ORDER BY 2 DESC""")
        return "🛍 Pedidos em andamento:\n" + ("\n".join(f"{r['status'].lower().replace('_', ' ')}: {r['n']}"
                                                         for r in cur.fetchall()) or "nenhum")
    if cmd == "#atrasos" and "pedidos.ver" in permissoes:
        cur.execute("""SELECT pr.sku, cl.nome, p.data_devolucao FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
                         LEFT JOIN clientes cl ON cl.id = p.cliente_id
                        WHERE p.status = 'ALUGADO' AND p.data_devolucao < (now() AT TIME ZONE 'America/Sao_Paulo')::date
                        ORDER BY p.data_devolucao""")
        return "⏰ Em atraso:\n" + ("\n".join(f"{r['sku']} · {r['nome'] or '—'} · era {r['data_devolucao']:%d/%m}"
                                             for r in cur.fetchall()) or "nenhuma peça 🎉")
    if cmd == "#online" and "presenca.ver" in permissoes:
        from .. import presenca
        return presenca.texto_whatsapp(presenca.online(cur))
    if cmd == "#site" and "site.solicitacoes" in permissoes:
        cur.execute("SELECT count(*) AS n FROM solicitacoes_prova WHERE status = 'NOVA'")
        return f"✉ Pedidos de prova do site esperando: {cur.fetchone()['n']}"
    return "Não conheço esse comando ou ele não é do seu cargo. Mande #ajuda."


def processar_mensagem(cur, corpo: dict) -> str:
    """Devolve o que foi feito (para teste e log)."""
    from .chat import resposta_da_equipe
    if corpo.get("fromMe") or corpo.get("isGroupMsg"):
        return "ignorada"
    remetente = cripto.so_digitos(str(corpo.get("from") or "").split("@")[0])
    texto = str(corpo.get("body") or "").strip()[:2000]
    if not remetente or not texto:
        return "ignorada"
    idx = cripto.indice_telefone(remetente)
    cur.execute("SELECT id::text, nome FROM usuarios WHERE ativo AND telefone_idx = %s", (idx,))
    func = cur.fetchone()
    if func:
        cur.execute("SELECT set_config('app.usuario_id', %s, true)", (func["id"],))
        if resposta_da_equipe(cur, func, texto, corpo.get("quotedMsgId") or (corpo.get("quotedMsg") or {}).get("id")):
            return "chat"
        if texto.startswith("#"):
            from ..auth import SQL_USUARIO
            cur.execute(SQL_USUARIO, (func["id"],))
            perm = set(cur.fetchone()["permissoes"])
            avisos.whatsapp(cur, remetente, _comando(cur, texto, func["id"], perm), "comando")
            cur.execute("""INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, dados)
                           VALUES (ctx_loja(), %s, 'comando_whatsapp', 'whatsapp', jsonb_build_object('comando', %s::text))""",
                        (func["id"], texto.split()[0][:20]))
            return "comando"
        return "funcionario"
    if texto.strip().upper() in ("SAIR", "PARAR", "STOP"):
        cur.execute("UPDATE clientes SET whatsapp_sair = true WHERE telefone_idx = %s", (idx,))
        avisos.whatsapp(cur, remetente, "Pronto, você não vai mais receber avisos automáticos da loja. "
                                        "Para voltar, é só pedir na loja.", "sair")
        return "sair"
    return "cliente"


@router.post("/whatsapp/aviso/{segredo}")
async def aviso(request: Request, segredo: str):
    try:
        corpo = ler_json(await request.body())
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(corpo, dict):
        return JSONResponse({"ok": False}, status_code=400)

    def tratar():
        with db.transacao() as cur:
            cur.execute("SELECT webhook_segredo FROM whatsapp_config")
            cfg = cur.fetchone()
            if not cfg or not hmac.compare_digest(cfg["webhook_segredo"], segredo[:64]):
                return None
            evento = str(corpo.get("event") or "")
            if evento == "phoneCode":
                codigo = str(corpo.get("data") or corpo.get("phoneCode") or "")[:12]
                if re.fullmatch(r"[A-Z0-9-]{8,9}", codigo):
                    cur.execute("UPDATE whatsapp_config SET codigo = %s, status = 'CODIGO', atualizado_em = now()", (codigo,))
                return "codigo"
            if evento in ("status-find", "onstatechange", "session-logged", "status"):
                estado = str(corpo.get("status") or corpo.get("state") or "").lower()
                if estado in ("inchat", "islogged", "qrreadsuccess", "connected", "successchat", "session-logged") \
                        or evento == "session-logged":
                    cur.execute("UPDATE whatsapp_config SET status = 'CONECTADO', codigo = NULL, qrcode = NULL, "
                                "atualizado_em = now()")
                elif estado in ("disconnected", "browserclose", "notlogged", "desconnectedmobile", "closed"):
                    cur.execute("UPDATE whatsapp_config SET status = 'DESCONECTADO', atualizado_em = now()")
                return "status"
            if evento in ("onmessage", "onMessage", "message"):
                return processar_mensagem(cur, corpo)
            return "ignorado"

    resultado = await run_in_threadpool(tratar)
    if resultado is None:
        return JSONResponse({"ok": False}, status_code=404)
    return JSONResponse({"ok": True, "resultado": resultado})
