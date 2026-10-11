"""App web da loja: montagem, segurança, login e tela inicial."""
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import acesso, config, db, logs, pessoas, protecao, seguranca, sessao
from .auth import NaoAutenticado, SQL_USUARIO, nova_sessao, veio_da_cloudflare, SemPermissao, TrocarSenha, auditar, ip, usuario_atual
from .web import AQUI, TZ, CsrfInvalido, checar_csrf, tela
from . import botoes as botoes_mod
from .rotas import (agenda, caixa, catalogo, clientes, config as rotas_config, equipe, impressao, operacao,
                    botoes_rotas, presenca_rotas, servicos_rotas, simbolos_rotas, reimprimir, conta as rotas_conta, entrega_rotas, sacola, vitrine_rotas, chat, contratos, crediario as rotas_crediario, fornecedores, impressos, marca_rotas, pagamentos, rodape, pedidos, site, site_config, solicitacoes, whatsapp_rotas)

log = logs.configurar()


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    db.pool.open(wait=False)
    if config.LOJA_ID:
        try:                                   # primeira subida depois da atualização: cifra o que estava aberto
            with db.transacao() as cur:
                n = pessoas.cifrar_pendentes(cur)
            if n:
                log.info("dados pessoais cifrados", extra={"evento": "cifra", "registros": n})
        except Exception:
            log.exception("não consegui cifrar os dados antigos agora")
    log.info("app iniciado", extra={"evento": "inicio"})
    yield
    db.pool.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=ciclo_de_vida)
app.mount("/static", StaticFiles(directory=AQUI / "static"), name="static")
for modulo in (impressos, caixa, catalogo, pedidos, operacao, rotas_config, clientes, equipe, agenda, impressao,
               site, site_config, pagamentos, solicitacoes, whatsapp_rotas, chat, contratos, marca_rotas, rodape,
               rotas_crediario, fornecedores, botoes_rotas, vitrine_rotas,
               rotas_conta, sacola, entrega_rotas, presenca_rotas, simbolos_rotas, servicos_rotas, reimprimir):
    app.include_router(modulo.router)


# --------------------------------------------------------------------------- #
# Middleware: request_id, cabeçalhos de segurança, log de cada requisição
# --------------------------------------------------------------------------- #
_ID_SEGURO = re.compile(r"[A-Za-z0-9-]{8,64}")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "frame-src https://www.google.com; "                        # só o mapa do Google, e só depois do toque
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


@app.middleware("http")
async def contexto_da_requisicao(request: Request, call_next):
    recebido = request.headers.get("x-request-id", "")
    # Aceita o id do proxy só se for curto e simples: nada de texto arbitrário nos logs.
    request.state.request_id = recebido if _ID_SEGURO.fullmatch(recebido) else uuid.uuid4().hex[:16]
    inicio = time.perf_counter()
    caminho = request.url.path
    pais = request.headers.get("cf-ipcountry", "") if config.CLOUDFLARE and veio_da_cloudflare(request) else ""
    try:
        if acesso.excedeu(request.method, caminho, ip(request)):
            resposta = tela(request, "erro.html", status=429, titulo="Muitos acessos seguidos",
                            mensagem="Espere um minuto e tente de novo.")
        elif config.CLOUDFLARE and not acesso.area_publica(caminho) and not veio_da_cloudflare(request):
            resposta = tela(request, "erro.html", status=403, titulo="Acesse pelo endereço da loja",
                            mensagem="A área da equipe só abre pelo endereço oficial.")
        elif config.CLOUDFLARE and config.SO_BRASIL and pais and pais not in ("BR", "XX", "T1") and not acesso.area_publica(caminho):
            resposta = tela(request, "erro.html", status=403, titulo="Acesso da equipe só do Brasil",
                            mensagem="A área da loja não abre fora do Brasil.")
        else:
            resposta = await call_next(request)
    except protecao.EnvioGrandeDemais:
        raise
    except Exception:
        log.exception("erro não tratado", extra={"request_id": request.state.request_id,
                                                 "caminho": protecao.caminho_para_log(request.url.path)})
        resposta = tela(request, "erro.html", status=500)
    resposta.headers["X-Request-ID"] = request.state.request_id
    resposta.headers["Content-Security-Policy"] = CSP
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["X-Frame-Options"] = "DENY"
    resposta.headers["Referrer-Policy"] = "no-referrer"
    resposta.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=(), payment=()"
    resposta.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resposta.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if not acesso.area_publica(caminho) or caminho == "/" or caminho.startswith(("/site/meu-pedido", "/pagar", "/site/conta", "/site/sacola", "/site/avaliar")):
        resposta.headers["X-Robots-Tag"] = "noindex, nofollow"      # sistema e links pessoais fora do Google
    if config.COOKIE_SECURE:
        resposta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/static"):
        # Com ?v= (nome com a versão): guarda 1 ano. Sem versão: sempre confere se mudou (nada de CSS velho).
        resposta.headers["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                                             else "no-cache")
    elif "cache-control" not in resposta.headers:
        resposta.headers["Cache-Control"] = "no-store"          # nada de dado de caixa em cache
    if request.url.path != "/health":
        log.info("requisição", extra={
            "request_id": request.state.request_id,
            "metodo": request.method,
            "caminho": protecao.caminho_para_log(request.url.path),
            "status": resposta.status_code,
            "duracao_ms": round((time.perf_counter() - inicio) * 1000, 1),
            "usuario_id": request.session.get("usuario_id") if "session" in request.scope else None,
        })
    return resposta


# Registrado depois: roda ANTES do middleware acima, que então já enxerga a sessão.
app.add_middleware(
    sessao.SessaoCifrada,
    cookie="sessao_loja",
    max_age=config.SESSAO_HORAS * 3600,
    same_site="strict",
    https_only=config.COOKIE_SECURE,
)
app.add_middleware(protecao.Protecao)             # o mais de fora: roda antes de tudo


@app.exception_handler(NaoAutenticado)
async def _nao_autenticado(_request: Request, _exc: NaoAutenticado):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(TrocarSenha)
async def _trocar_senha(_request: Request, _exc: TrocarSenha):
    return RedirectResponse("/minha-senha?obrigatoria=1", status_code=303)


@app.exception_handler(SemPermissao)
async def _sem_permissao(request: Request, _exc: SemPermissao):
    return tela(request, "erro.html", status=403, titulo="Esta área não é do seu cargo",
                mensagem="Se precisar dela, fale com a Gerente.")


@app.exception_handler(CsrfInvalido)
async def _csrf_invalido(request: Request, _exc: CsrfInvalido):
    return tela(request, "erro.html", status=400, titulo="A página ficou aberta muito tempo",
                mensagem="Por segurança, nada foi guardado. Volte e faça de novo.")


from fastapi.exceptions import RequestValidationError  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def _http(request: Request, exc: StarletteHTTPException):
    """Nada de JSON técnico ({"detail": ...}) para quem abre o F12: sempre a página de erro da loja."""
    if exc.status_code == 404:
        return tela(request, "erro.html", status=404, titulo="Página não encontrada")
    return tela(request, "erro.html", status=exc.status_code if exc.status_code >= 400 else 400,
                titulo="Não foi possível abrir esta página")


@app.exception_handler(RequestValidationError)
async def _validacao(request: Request, _exc: RequestValidationError):
    # O detalhe (nome dos campos, tipos, o que foi enviado) nunca volta para o navegador
    return tela(request, "erro.html", status=404, titulo="Página não encontrada")


# --------------------------------------------------------------------------- #
# Login e saída
# --------------------------------------------------------------------------- #
@app.get("/health", include_in_schema=False)
def health():
    if db.banco_ok():
        return {"status": "ok", "banco": "ok", "loja_configurada": bool(config.LOJA_ID)}
    return JSONResponse({"status": "erro", "banco": "indisponivel"}, status_code=503)


@app.get("/login")
def login_form(request: Request):
    return tela(request, "login.html", configurada=bool(config.LOJA_ID), aviso=request.session.pop("aviso_login", None))


def _aparelho(request: Request) -> tuple[str, bool]:
    """(token do cookie do aparelho, se é novo)."""
    token = request.cookies.get(acesso.COOKIE_APARELHO)
    if token and acesso.hash_aparelho(token):
        return token, False
    return acesso.novo_token_aparelho(), True


def _com_cookie_aparelho(resposta, token: str):
    resposta.set_cookie(acesso.COOKIE_APARELHO, token, max_age=400 * 86400, httponly=True,
                        secure=config.COOKIE_SECURE, samesite="strict")
    return resposta


def _entrar(request: Request, cur, uid: str, token: str, info: dict, senha_fraca: bool):
    """Senha (e código, se pedido) conferidos: abre a sessão."""
    cur.execute(SQL_USUARIO, (uid,))
    perfil = cur.fetchone()
    acesso.registrar_entrada(cur, uid, perfil["nome"], acesso.hash_aparelho(token), ip(request),
                             acesso.descrever_navegador(request.headers.get("user-agent")), info)
    auditar(cur, request, "login_ok", uid, dados={"aparelho_novo": info["novo_aparelho"]})
    request.session.pop("trocar_senha", None)
    nova_sessao(request, cur, uid, perfil["sessao_versao"], acesso.hash_aparelho(token))
    if senha_fraca:
        request.session["trocar_senha"] = True
    return _com_cookie_aparelho(RedirectResponse("/minha-senha?obrigatoria=1" if senha_fraca else "/", status_code=303),
                                token)


@app.post("/login")
def login(request: Request, login: str = Form(""), senha: str = Form(""), csrf: str = Form("")):
    def recusar(mensagem: str, status: int = 400):
        return tela(request, "login.html", status=status, configurada=bool(config.LOJA_ID),
                    erro=mensagem, login=login)

    if not config.LOJA_ID:
        return recusar("O sistema ainda não foi configurado.", 503)
    try:
        checar_csrf(request, csrf)
    except CsrfInvalido:
        return recusar("A página ficou aberta muito tempo. Tente de novo.")
    login = login.strip().lower()
    if not login or not senha:
        return recusar("Preencha o usuário e a senha.")
    if len(senha) > 256 or len(login) > 60:     # evita gastar CPU do Argon2 com entradas absurdas
        return recusar("Usuário ou senha não conferem.", 401)
    endereco = ip(request)
    token, _ = _aparelho(request)

    with db.transacao() as cur:
        if acesso.ip_bloqueado(cur, endereco):
            auditar(cur, request, "login_ip_bloqueado", None, dados={"login": login[:60]})
            return recusar("Muitas tentativas erradas deste lugar. Tente de novo mais tarde ou fale com a Dona.", 429)
        cur.execute(
            """SELECT id::text, senha_hash, ativo, tentativas_falhas,
                      (bloqueado_ate IS NOT NULL AND bloqueado_ate > now()) AS bloqueado
                 FROM usuarios WHERE login = %s AND NOT sistema FOR UPDATE""", (login,))
        u = cur.fetchone()

        senha_ok = seguranca.verificar(u["senha_hash"] if u else None, senha)

        if u and u["bloqueado"]:
            auditar(cur, request, "login_bloqueado", u["id"])
            acesso.talvez_bloquear(cur, endereco)
            return recusar("Muitas tentativas erradas. Espere alguns minutos ou chame a Gerente.", 429)

        if not u or not senha_ok or not u["ativo"]:
            if u:
                cur.execute(
                    """UPDATE usuarios
                          SET tentativas_falhas = tentativas_falhas + 1,
                              bloqueado_ate = CASE WHEN tentativas_falhas + 1 >= %(max)s
                                   THEN now() + make_interval(mins => 5 * (tentativas_falhas + 2 - %(max)s))
                                   ELSE bloqueado_ate END
                        WHERE id = %(id)s""",
                    {"max": config.LOGIN_MAX_TENTATIVAS, "id": u["id"]})
            auditar(cur, request, "login_falhou", u["id"] if u else None, dados={"login": login[:60]})
            acesso.talvez_bloquear(cur, endereco)
            return recusar("Usuário ou senha não conferem.", 401)

        cur.execute("UPDATE usuarios SET tentativas_falhas = 0, bloqueado_ate = NULL WHERE id = %s",
                    (u["id"],))
        if seguranca.precisa_rehash(u["senha_hash"]):
            cur.execute("UPDATE usuarios SET senha_hash = %s WHERE id = %s",
                        (seguranca.gerar_hash(senha), u["id"]))
        cur.execute(SQL_USUARIO, (u["id"],))
        perfil = cur.fetchone()
        if not perfil["permissoes"]:
            auditar(cur, request, "login_sem_papel", u["id"])
            return recusar("Seu acesso expirou ou ainda não foi liberado. Fale com a Gerente.", 403)
        info = acesso.analisar(cur, u["id"], acesso.hash_aparelho(token), endereco)
        senha_fraca = seguranca.validar_senha_nova(senha) is not None
        fone = acesso.precisa_codigo(cur, u["id"], info)
        if fone:
            codigo = acesso.enviar_codigo(cur, u["id"], fone)
            auditar(cur, request, "codigo_enviado", u["id"])
            request.session.clear()
            request.session["pendente"] = {"uid": u["id"], "hash": acesso.hash_codigo(codigo, u["id"]),
                                           "expira": int(time.time()) + acesso.CODIGO_VALIDADE_S, "erros": 0,
                                           "info": info, "fraca": senha_fraca, "final": fone[-4:]}
            return _com_cookie_aparelho(RedirectResponse("/login/codigo", status_code=303), token)
        return _entrar(request, cur, u["id"], token, info, senha_fraca)


@app.get("/login/codigo")
def codigo_form(request: Request):
    p = request.session.get("pendente")
    if not p:
        return RedirectResponse("/login", status_code=303)
    return tela(request, "login_codigo.html", final=p["final"])


@app.post("/login/codigo")
def codigo(request: Request, codigo: str = Form(""), csrf: str = Form("")):
    p = request.session.get("pendente")
    if not p:
        return RedirectResponse("/login", status_code=303)
    try:
        checar_csrf(request, csrf)
    except CsrfInvalido:
        return tela(request, "login_codigo.html", status=400, final=p["final"],
                    erro="A página ficou aberta muito tempo. Digite o código de novo.")
    codigo = "".join(c for c in codigo if c.isdigit())[:6]
    if int(time.time()) > p["expira"] or p["erros"] >= 5:
        request.session.clear()
        return tela(request, "login.html", status=400, configurada=True,
                    erro="O código venceu ou teve erros demais. Entre de novo para receber outro.")
    import hmac
    if not hmac.compare_digest(acesso.hash_codigo(codigo, p["uid"]), p["hash"]):
        p["erros"] += 1
        request.session["pendente"] = p
        with db.transacao() as cur:
            auditar(cur, request, "codigo_errado", p["uid"])
        return tela(request, "login_codigo.html", status=400, final=p["final"],
                    erro="Código errado. Confira a mensagem no WhatsApp.")
    token, _ = _aparelho(request)
    with db.transacao() as cur:
        return _entrar(request, cur, p["uid"], token, p["info"], p["fraca"])


@app.post("/sair")
def sair(request: Request, csrf: str = Form("")):
    if request.session.get("usuario_id"):
        try:
            checar_csrf(request, csrf)
        except CsrfInvalido:                       # outro site tentando derrubar a sessão: ignora
            return tela(request, "erro.html", status=400, titulo="A página ficou aberta muito tempo",
                        mensagem="Volte e toque em Sair de novo.")
        with db.transacao(usuario_id=request.session["usuario_id"]) as cur:
            sessao.encerrar(cur, request.session.get("sid"), "SAIU")
            auditar(cur, request, "logout", request.session["usuario_id"])
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --------------------------------------------------------------------------- #
# Tela inicial: cada pessoa vê só os botões do próprio cargo
# --------------------------------------------------------------------------- #
BOTOES = botoes_mod.BOTOES


@app.get("/")
def inicio(request: Request):
    # Quem não entrou no sistema cai na vitrine da loja; funcionária vai para o login pelo rodapé do site
    if not request.session.get("usuario_id"):
        return RedirectResponse("/site" if config.LOJA_ID else "/login", status_code=303)
    usuario = usuario_atual(request)
    from . import painel
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT liberar_pedidos_vencidos()")
        botoes = botoes_mod.visiveis(cur, usuario)
        resumo = painel.montar(cur, usuario)
    grandes = [b for u in ("/pedidos/novo", "/caixa/receber", "/caixa/pagar") for b in botoes if b[3] == u]
    resto = [b for b in botoes if b not in grandes]
    return tela(request, "inicio.html", usuario, botoes=botoes, grandes=grandes,
                secoes=botoes_mod.por_secao(resto), resumo=resumo)
