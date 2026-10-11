"""Sessão em cookie CIFRADO (AES-256-GCM) + registro das sessões de quem entrou no sistema.

Por que não o SessionMiddleware padrão: ele só ASSINA o cookie. O conteúdo (id do usuário, dados do login)
fica legível para quem abre o F12 → Aplicativo → Cookies e decodifica o base64. Aqui o cookie é um
bloco cifrado: sem a DADOS_CHAVE do servidor não dá para ler nem montar um cookie válido.

Mesma interface do Starlette (request.session é um dicionário), então o resto do sistema não muda.
"""
import base64
import hashlib
import json
import secrets
import time
from http.cookies import SimpleCookie

from . import cripto

ROTULO = "sessao.cookie"


def _abrir(valor: str | None) -> dict:
    if not valor or len(valor) > 8000:
        return {}
    try:
        bruto = cripto.decifrar_bytes(base64.urlsafe_b64decode(valor + "=" * (-len(valor) % 4)), ROTULO)
        dados = json.loads(bruto)
    except Exception:           # cookie adulterado, de outra chave ou velho: começa do zero
        return {}
    if not isinstance(dados, dict) or dados.get("_ate", 0) < time.time():
        return {}
    return dados


def _fechar(dados: dict) -> str:
    return base64.urlsafe_b64encode(cripto.cifrar(json.dumps(dados, separators=(",", ":")), ROTULO)).decode().rstrip("=")


class SessaoCifrada:
    def __init__(self, app, cookie: str, max_age: int, same_site: str = "strict", https_only: bool = True):
        self.app, self.cookie, self.max_age = app, cookie, max_age
        self.flags = f"Path=/; HttpOnly; SameSite={same_site}" + ("; Secure" if https_only else "")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        valor = None
        for nome, conteudo in scope.get("headers", []):
            if nome == b"cookie":
                c = SimpleCookie()
                try:
                    c.load(conteudo.decode("latin-1"))
                except Exception:
                    continue
                if self.cookie in c:
                    valor = c[self.cookie].value
        dados = _abrir(valor)
        limite = dados.pop("_ate", None)
        scope["session"] = dados
        antes = json.dumps(dados, sort_keys=True)

        async def enviar(msg):
            if msg["type"] == "http.response.start":
                sessao = scope["session"]
                agora = json.dumps(sessao, sort_keys=True)
                cabecalho = None
                if sessao and (agora != antes or not limite):
                    ate = int(time.time()) + self.max_age if agora != antes or not limite else limite
                    cabecalho = f"{self.cookie}={_fechar({**sessao, '_ate': ate})}; Max-Age={self.max_age}; {self.flags}"
                elif not sessao and valor:
                    cabecalho = f"{self.cookie}=; Max-Age=0; {self.flags}"
                if cabecalho:
                    msg.setdefault("headers", [])
                    msg["headers"] = list(msg["headers"]) + [(b"set-cookie", cabecalho.encode("latin-1"))]
            await send(msg)

        await self.app(scope, receive, enviar)


# --------------------------------------------------------------------------- #
# Registro das sessões no banco
# --------------------------------------------------------------------------- #
def _h(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def hash_navegador(ua: str | None) -> str:
    return _h("ua:" + (ua or "")[:400])


def abrir(cur, uid: str, aparelho: str | None, ua: str | None, descricao: str, ip: str | None, horas: int) -> str:
    """Cria a sessão no banco e devolve o identificador (vai só para dentro do cookie cifrado)."""
    sid = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO sessoes (id, loja_id, usuario_id, aparelho, navegador, descricao, ip_inicio, ip_ultimo, expira_em)
                   VALUES (%s, ctx_loja(), %s, %s, %s, %s, %s::inet, %s::inet, now() + make_interval(hours => %s))""",
                (_h(sid), uid, aparelho, hash_navegador(ua), descricao[:80], ip, ip, horas))
    return sid


def conferir(cur, sid: str | None, uid: str, aparelho: str | None, ua: str | None, ip: str | None) -> str | None:
    """None se a sessão vale; senão o motivo para derrubar (a sessão já fica encerrada no banco)."""
    if not sid or len(sid) > 100:
        return "SEM_SESSAO"
    cur.execute("""SELECT usuario_id::text AS uid, aparelho, navegador, encerrada_em IS NOT NULL AS encerrada,
                          expira_em < now() AS vencida, visto_em < now() - interval '1 minute' AS atualizar
                     FROM sessoes WHERE id = %s""", (_h(sid),))
    s = cur.fetchone()
    if not s or s["uid"] != uid or s["encerrada"]:
        return "ENCERRADA"
    motivo = ("EXPIRADA" if s["vencida"] else
              "NAVEGADOR_DIFERENTE" if s["navegador"] != hash_navegador(ua) else
              "APARELHO_DIFERENTE" if s["aparelho"] and s["aparelho"] != aparelho else None)
    if motivo:
        encerrar(cur, sid, motivo)
        return motivo
    if s["atualizar"]:
        cur.execute("UPDATE sessoes SET visto_em = now(), ip_ultimo = %s::inet WHERE id = %s", (ip, _h(sid)))
    return None


def encerrar(cur, sid: str | None, motivo: str) -> None:
    if sid and len(sid) <= 100:
        cur.execute("UPDATE sessoes SET encerrada_em = now(), motivo = %s WHERE id = %s AND encerrada_em IS NULL",
                    (motivo, _h(sid)))


def encerrar_todas(cur, uid: str, motivo: str, menos: str | None = None) -> int:
    cur.execute("""UPDATE sessoes SET encerrada_em = now(), motivo = %s
                    WHERE usuario_id = %s AND encerrada_em IS NULL AND id <> %s""",
                (motivo, uid, _h(menos) if menos else "-"))
    return cur.rowcount
