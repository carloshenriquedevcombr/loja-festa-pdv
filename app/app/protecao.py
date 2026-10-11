"""Proteções de protocolo que valem antes de qualquer tela (ASGI puro, bem na entrada).

* Host: só os endereços da loja (link envenenado por cabeçalho Host falso não sai em WhatsApp/e-mail).
* Tamanho do envio: corta quem manda corpo gigante (DDoS de memória), com ou sem Content-Length.
* Métodos: só GET, HEAD e POST (TRACE, PUT, DELETE… nem chegam nas rotas).
"""
from . import config

METODOS = {"GET", "HEAD", "POST"}
ROTAS_DE_FOTO = ("/pecas/",)


def _resposta(status: int, texto: str):
    corpo = texto.encode()
    return [
        {"type": "http.response.start", "status": status,
         "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(corpo)).encode()),
                     (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]},
        {"type": "http.response.body", "body": corpo},
    ]


def limite_para(caminho: str) -> int:
    if caminho.startswith(ROTAS_DE_FOTO) and caminho.endswith("/fotos"):
        return config.LIMITE_ENVIO_FOTOS
    if caminho.startswith(("/config/marca/", "/config/simbolos/")):
        return 10 * 1024 * 1024
    if caminho.startswith("/config/banners"):              # foto do banner
        return 16 * 1024 * 1024
    if caminho == "/caixa/pagar/conferir":                   # foto da nota
        return 16 * 1024 * 1024
    return config.LIMITE_ENVIO


class Protecao:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def recusar(status, texto):
            for msg in _resposta(status, texto):
                await send(msg)

        if scope["method"] not in METODOS:
            return await recusar(405, "Método não permitido.")
        cab = {}
        for k, v in scope.get("headers", []):
            cab.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
        if config.HOSTS_PERMITIDOS:
            hosts = cab.get("host", [])
            host = hosts[0].rsplit(":", 1)[0].strip("[]").lower() if len(hosts) == 1 else ""
            if host not in config.HOSTS_PERMITIDOS and host not in ("localhost", "127.0.0.1", "loja-app"):
                return await recusar(400, "Endereço inválido.")
        if len(cab.get("content-length", [])) > 1 or ("content-length" in cab and "transfer-encoding" in cab):
            return await recusar(400, "Pedido malformado.")         # base do "request smuggling"
        limite = limite_para(scope.get("path", ""))
        try:
            declarado = int(cab.get("content-length", ["0"])[0])
        except ValueError:
            return await recusar(400, "Pedido malformado.")
        if declarado > limite:
            return await recusar(413, "Envio grande demais.")

        recebido = 0

        async def receber():
            nonlocal recebido
            msg = await receive()
            if msg["type"] == "http.request":
                recebido += len(msg.get("body", b""))
                if recebido > limite:
                    raise EnvioGrandeDemais
            return msg

        try:
            await self.app(scope, receber, send)
        except EnvioGrandeDemais:
            await recusar(413, "Envio grande demais.")


class EnvioGrandeDemais(Exception):
    pass


# --------------------------------------------------------------------------- #
# Logs sem segredos: o endereço de algumas páginas É o segredo
# --------------------------------------------------------------------------- #
SEGREDOS_NA_URL = ("/site/meu-pedido/", "/whatsapp/aviso/", "/pagar/")


def caminho_para_log(caminho: str) -> str:
    for prefixo in SEGREDOS_NA_URL:
        if caminho.startswith(prefixo):
            resto = caminho[len(prefixo):]
            final = resto[resto.find("/"):] if "/" in resto else ""
            return f"{prefixo}***{final}"
    return caminho
