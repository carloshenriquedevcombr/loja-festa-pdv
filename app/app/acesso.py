"""Blindagem da entrada no sistema.

* Aparelho: cada navegador recebe um cookie aleatório; guardamos só o hash. Aparelho novo ou IP novo
  avisa a Dona e o Dev no WhatsApp; entrada fora do horário da loja também.
* Código no WhatsApp (2 fatores): Dona, Dev e Financeiro sempre; qualquer pessoa em aparelho novo.
* IP que erra muitas senhas seguidas fica bloqueado por 1 hora (e avisa).
* Limite de pedidos por IP (em memória, por processo) contra robôs.
"""
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from . import avisos, config, pessoas, whatsapp

FUSO = ZoneInfo("America/Sao_Paulo")
CARGOS_2FA = ("DONO", "DESENVOLVEDOR", "FINANCEIRO", "MASTER")
FALHAS_PARA_BLOQUEIO = 12          # por IP, em 15 minutos
MINUTOS_BLOQUEIO = 60
CODIGO_VALIDADE_S = 300
COOKIE_APARELHO = "aparelho"


def hash_aparelho(token: str | None) -> str | None:
    if not token or len(token) > 100:
        return None
    return hashlib.sha256(("aparelho:" + token).encode()).hexdigest()


def novo_token_aparelho() -> str:
    return secrets.token_urlsafe(32)


def descrever_navegador(ua: str | None) -> str:
    ua = ua or ""
    sistema = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                   ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "?")
    nav = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"),
                               ("Safari/", "Safari")) if k in ua), "navegador")
    return f"{nav} no {sistema}"


# --------------------------------------------------------------------------- #
# Bloqueio de IP por excesso de senha errada
# --------------------------------------------------------------------------- #
def ip_bloqueado(cur, ip: str | None) -> bool:
    if not ip:
        return False
    cur.execute("SELECT 1 FROM bloqueios_ip WHERE ip = %s::inet AND ate > now() LIMIT 1", (ip,))
    return cur.fetchone() is not None


def talvez_bloquear(cur, ip: str | None) -> bool:
    if not ip:
        return False
    cur.execute("""SELECT count(*) AS n FROM auditoria WHERE acao IN ('login_falhou', 'login_bloqueado') AND ip = %s::inet
                     AND criado_em > now() - interval '15 minutes'""", (ip,))
    if cur.fetchone()["n"] < FALHAS_PARA_BLOQUEIO or ip_bloqueado(cur, ip):
        return False
    cur.execute("""INSERT INTO bloqueios_ip (loja_id, ip, ate, motivo)
                   VALUES (ctx_loja(), %s::inet, now() + make_interval(mins => %s), 'senhas erradas seguidas')""",
                (ip, MINUTOS_BLOQUEIO))
    avisos.para_equipe(cur, "seguranca",
                       f"🚨 Alguém errou a senha {FALHAS_PARA_BLOQUEIO} vezes seguidas do IP {ip}. "
                       f"Esse IP ficou bloqueado por {MINUTOS_BLOQUEIO} minutos.", "seguranca",
                       f"bloqueio:{ip}:{int(time.time() // 3600)}")
    return True


# --------------------------------------------------------------------------- #
# Horário da loja, aparelho e IP novos
# --------------------------------------------------------------------------- #
def fora_do_horario(cur, agora: datetime | None = None) -> bool:
    agora = agora or datetime.now(FUSO)
    cur.execute("SELECT abre_as, fecha_as, dias_abertos FROM lojas WHERE id = ctx_loja()")
    l = cur.fetchone()
    return agora.isoweekday() not in l["dias_abertos"] or not (l["abre_as"] <= agora.time() <= l["fecha_as"])


def analisar(cur, uid: str, aparelho: str | None, ip: str | None) -> dict:
    cur.execute("""SELECT count(*) AS total, bool_or(aparelho = %s) AS conhecido,
                          bool_or(ip_ultimo = %s::inet) AS ip_conhecido
                     FROM usuario_aparelhos WHERE usuario_id = %s""", (aparelho or "-", ip, uid))
    r = cur.fetchone()
    return {"primeiro": r["total"] == 0, "novo_aparelho": not r["conhecido"],
            "novo_ip": bool(ip) and not r["ip_conhecido"]}


def cargos(cur, uid: str) -> set[str]:
    cur.execute("""SELECT p.codigo FROM usuario_papeis up JOIN papeis p ON p.id = up.papel_id
                    WHERE up.usuario_id = %s AND (up.valido_ate IS NULL OR up.valido_ate > now())""", (uid,))
    return {r["codigo"] for r in cur.fetchall()}


def precisa_codigo(cur, uid: str, info: dict) -> str | None:
    """Devolve o telefone para mandar o código, ou None se não precisa (ou não dá)."""
    if not whatsapp.ligado(cur):
        return None
    fone = pessoas.telefone_usuario(cur, uid)
    if not fone:
        return None
    if cargos(cur, uid) & set(CARGOS_2FA) or (info["novo_aparelho"] and not info["primeiro"]):
        return fone
    return None


def hash_codigo(codigo: str, uid: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), f"{uid}:{codigo}".encode(), hashlib.sha256).hexdigest()


def enviar_codigo(cur, uid: str, fone: str) -> str:
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
    loja = cur.fetchone()["nome"]
    texto = (f"🔐 {loja}: seu código para entrar no sistema é {codigo}. Vale por 5 minutos. "
             f"Não passe este código para ninguém — a loja nunca pede.")
    cfg = whatsapp.config_loja(cur)
    try:                                     # direto (a pessoa está esperando na tela)
        whatsapp.enviar_texto(cfg, whatsapp.numero_whatsapp(fone), texto)
    except whatsapp.ErroWhatsApp:
        avisos.whatsapp(cur, fone, texto, "codigo_acesso")
    return codigo


def registrar_entrada(cur, uid: str, nome: str, aparelho: str, ip: str | None, navegador: str, info: dict) -> None:
    cur.execute("""INSERT INTO usuario_aparelhos (loja_id, usuario_id, aparelho, navegador, ip_ultimo)
                   VALUES (ctx_loja(), %s, %s, %s, %s::inet)
                   ON CONFLICT (usuario_id, aparelho) DO UPDATE SET ultimo_em = now(), ip_ultimo = EXCLUDED.ip_ultimo,
                                                                     navegador = EXCLUDED.navegador""",
                (uid, aparelho, navegador, ip))
    agora = datetime.now(FUSO)
    motivos = []
    if not info["primeiro"] and info["novo_aparelho"]:
        motivos.append(f"de um aparelho novo ({navegador})")
    elif not info["primeiro"] and info["novo_ip"]:
        motivos.append(f"de um lugar diferente (IP {ip})")
    if fora_do_horario(cur, agora):
        motivos.append(f"fora do horário da loja ({agora:%d/%m às %H:%M})")
    if motivos:
        avisos.para_equipe(cur, "seguranca",
                           f"👀 {nome} entrou no sistema " + " e ".join(motivos) + f". IP {ip or '?'}. "
                           "Se não foi essa pessoa, desconecte os aparelhos dela em Equipe.",
                           "seguranca", f"entrada:{uid}:{aparelho[:12]}:{ip}:{agora:%Y%m%d%H}")


# --------------------------------------------------------------------------- #
# Limite de pedidos por IP (memória do processo)
# --------------------------------------------------------------------------- #
LIMITES = [  # (método, prefixo, quantidade, janela em segundos)
    ("POST", "/login", 10, 300),
    ("POST", "/site/prova", 10, 600),
    ("POST", "/site/chat", 30, 600),
    ("POST", "/site/conta", 20, 600),
    ("POST", "/site/sacola", 60, 600),
    ("POST", "/whatsapp/aviso", 300, 60),
    ("POST", "/pagamentos/mercadopago/aviso", 120, 60),
    ("*", "/", 600, 60),
]
_janelas: dict = defaultdict(deque)


def excedeu(metodo: str, caminho: str, ip: str | None) -> bool:
    if not config.LIMITES_ATIVOS:
        return False
    agora = time.monotonic()
    for m, prefixo, limite, janela in LIMITES:
        if (m == "*" or m == metodo) and caminho.startswith(prefixo):
            fila = _janelas[(prefixo, m, ip or "-")]
            while fila and agora - fila[0] > janela:
                fila.popleft()
            if len(fila) >= limite:
                return True
            fila.append(agora)
    return False


PUBLICOS = ("/site", "/static", "/fotos", "/pagar", "/v/", "/pagamentos/mercadopago/aviso", "/whatsapp/aviso", "/health",
            "/marca/", "/simbolo/", "/favicon.ico", "/icone-", "/apple-touch-icon.png")


def area_publica(caminho: str) -> bool:
    return caminho == "/" or caminho.startswith(PUBLICOS)
