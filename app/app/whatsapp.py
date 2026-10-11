"""Cliente do WPPConnect Server (o que já roda na VPS).

Conexão sem QR code: a loja informa o número, o WPPConnect devolve um código de 8 letras
(ex.: A1A4-FCB3) e a Dona digita no celular em WhatsApp > Dispositivos conectados >
Conectar com número de telefone. O código chega por aviso (webhook) ou pela consulta de status.

Rotas usadas (wppconnect-server):
  POST /api/{sessao}/{segredo}/generate-token      -> {"token": "..."}
  POST /api/{sessao}/start-session                 {"webhook": url, "waitQrCode": false, "phone": "55..."}
  GET  /api/{sessao}/status-session                -> {"status": "CONNECTED" | "QRCODE" | ..., "qrcode": ...}
  POST /api/{sessao}/send-message                  {"phone": "55...", "message": "...", "isGroup": false}
  POST /api/{sessao}/send-file-base64              {"phone": ..., "base64": "data:...;base64,...", "filename": ...}
  POST /api/{sessao}/logout-session
"""
import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from . import config, cripto


class ErroWhatsApp(Exception):
    pass


def _chamar(cfg: dict, metodo: str, caminho: str, corpo: dict | None = None, token: str | None = None,
            tempo: int = 20) -> dict:
    cab = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        cab["Authorization"] = f"Bearer {token}"
    url = cfg["url"].rstrip("/") + caminho
    pedido = urllib.request.Request(url, method=metodo, headers=cab,
                                    data=json.dumps(corpo).encode() if corpo is not None else None)
    try:
        with urllib.request.urlopen(pedido, timeout=tempo) as r:
            texto = r.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ErroWhatsApp("O WPPConnect recusou a chave (SECRET_KEY) ou o token. Confira a chave.") from e
        raise ErroWhatsApp(f"O WPPConnect respondeu com erro ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErroWhatsApp("Não consegui falar com o WPPConnect. Confira o endereço e se ele está ligado.") from e
    try:
        return json.loads(texto)
    except ValueError:
        return {"texto": texto}


def numero_whatsapp(fone: str | None) -> str | None:
    """'(19) 99999-1111' -> '5519999991111'"""
    d = cripto.so_digitos(fone)
    if len(d) in (10, 11):
        d = "55" + d
    return d if len(d) in (12, 13) else None


# --------------------------------------------------------------------------- #
# Configuração guardada (segredo e token cifrados)
# --------------------------------------------------------------------------- #
def config_loja(cur) -> dict | None:
    cur.execute("SELECT * FROM whatsapp_config")
    cfg = cur.fetchone()
    if not cfg:
        return None
    cfg["segredo"] = cripto.decifrar(cfg.pop("segredo_c"), "whatsapp.segredo")
    cfg["token"] = cripto.decifrar(cfg.pop("token_c"), "whatsapp.token")
    return cfg


def ligado(cur) -> bool:
    cur.execute("SELECT 1 FROM whatsapp_config WHERE ativo AND status = 'CONECTADO'")
    return cur.fetchone() is not None


def salvar_config(cur, url: str, sessao: str, segredo: str | None, numero: str, endereco_sistema: str) -> None:
    cur.execute("SELECT webhook_segredo, segredo_c FROM whatsapp_config")
    atual = cur.fetchone()
    if atual:
        cur.execute("""UPDATE whatsapp_config SET url=%s, sessao=%s, numero=%s, endereco_sistema=%s,
                              segredo_c = COALESCE(%s, segredo_c), ativo = true, atualizado_em = now()""",
                    (url, sessao, numero, endereco_sistema, cripto.cifrar(segredo, "whatsapp.segredo") if segredo else None))
    else:
        cur.execute("""INSERT INTO whatsapp_config (loja_id, url, sessao, segredo_c, numero, webhook_segredo, endereco_sistema)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s)""",
                    (url, sessao, cripto.cifrar(segredo, "whatsapp.segredo"), numero, secrets.token_hex(20),
                     endereco_sistema))


def url_webhook(cfg: dict) -> str:
    base = (cfg.get("endereco_sistema") or config.SITE_URL or "").rstrip("/")
    return f"{base}/whatsapp/aviso/{cfg['webhook_segredo']}"


def conectar(cur) -> dict:
    """Gera o token, inicia a sessão pedindo o código pelo número. Devolve {status, codigo, qrcode}."""
    cfg = config_loja(cur)
    if not cfg:
        raise ErroWhatsApp("Salve primeiro o endereço, a sessão, a chave e o número.")
    r = _chamar(cfg, "POST", f"/api/{urllib.parse.quote(cfg['sessao'])}/{urllib.parse.quote(cfg['segredo'])}/generate-token")
    token = r.get("token")
    if not token:
        raise ErroWhatsApp("O WPPConnect não devolveu o token. Confira a chave (SECRET_KEY).")
    cur.execute("UPDATE whatsapp_config SET token_c = %s, status = 'CONECTANDO', codigo = NULL, qrcode = NULL",
                (cripto.cifrar(token, "whatsapp.token"),))
    r = _chamar(cfg, "POST", f"/api/{cfg['sessao']}/start-session", token=token, tempo=60,
                corpo={"webhook": url_webhook(cfg), "waitQrCode": False, "phone": cfg["numero"]})
    return atualizar_status(cur, resposta=r)


def atualizar_status(cur, resposta: dict | None = None) -> dict:
    cfg = config_loja(cur)
    if not cfg or not cfg["token"]:
        return {"status": "DESCONECTADO"}
    r = resposta or _chamar(cfg, "GET", f"/api/{cfg['sessao']}/status-session", token=cfg["token"])
    bruto = str(r.get("status") or "").upper()
    codigo = r.get("phoneCode") or r.get("code")
    qr = r.get("qrcode") or r.get("urlcode")
    status = ("CONECTADO" if bruto in ("CONNECTED", "INCHAT", "ISLOGGED", "SUCCESS") and not codigo and not qr
              else "CODIGO" if codigo or bruto == "PHONECODE"
              else "QRCODE" if qr or bruto == "QRCODE"
              else "DESCONECTADO" if bruto in ("CLOSED", "DISCONNECTED", "NOTLOGGED", "FALSE")
              else "CONECTANDO")
    if qr and isinstance(qr, str) and not qr.startswith("data:image/"):
        qr = None                                              # só aceitamos imagem embutida
    cur.execute("""UPDATE whatsapp_config SET status = %s, codigo = COALESCE(%s, CASE WHEN %s = 'CONECTADO' THEN NULL ELSE codigo END),
                          qrcode = %s, atualizado_em = now()""", (status, codigo, status, qr))
    return {"status": status, "codigo": codigo, "qrcode": qr}


def enviar_texto(cfg: dict, numero: str, texto: str) -> str | None:
    r = _chamar(cfg, "POST", f"/api/{cfg['sessao']}/send-message", token=cfg["token"],
                corpo={"phone": numero, "message": texto, "isGroup": False})
    return _id_mensagem(r)


def enviar_arquivo(cfg: dict, numero: str, nome: str, dados: bytes, legenda: str = "") -> str | None:
    tipo = "application/pdf" if nome.lower().endswith(".pdf") else "image/png"
    r = _chamar(cfg, "POST", f"/api/{cfg['sessao']}/send-file-base64", token=cfg["token"], tempo=40,
                corpo={"phone": numero, "isGroup": False, "filename": nome, "caption": legenda,
                       "base64": f"data:{tipo};base64,{base64.b64encode(dados).decode()}"})
    return _id_mensagem(r)


def desconectar(cur) -> None:
    cfg = config_loja(cur)
    if cfg and cfg["token"]:
        try:
            _chamar(cfg, "POST", f"/api/{cfg['sessao']}/logout-session", token=cfg["token"])
        except ErroWhatsApp:
            pass
    cur.execute("UPDATE whatsapp_config SET status = 'DESCONECTADO', codigo = NULL, qrcode = NULL, ativo = false")


def _id_mensagem(r: dict) -> str | None:
    if r.get("status") not in (None, "success", "SUCCESS", True):
        raise ErroWhatsApp(f"O WhatsApp não aceitou a mensagem ({str(r.get('message') or r.get('status'))[:80]}).")
    resp = r.get("response")
    if isinstance(resp, list) and resp:
        resp = resp[0]
    if isinstance(resp, dict):
        return str(resp.get("id") or "") or None
    return None
