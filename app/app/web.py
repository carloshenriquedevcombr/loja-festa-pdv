"""Peças comuns das telas: templates, dinheiro, CSRF, mensagens do banco."""
import re
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import psycopg.errors as pgerr
from psycopg import sql
from fastapi import Request
from fastapi.templating import Jinja2Templates

AQUI = Path(__file__).parent
templates = Jinja2Templates(directory=AQUI / "templates")

FORMAS = {"DINHEIRO": "Dinheiro", "PIX": "PIX", "DEBITO": "Débito", "CREDITO": "Crédito",
          "BOLETO": "Boleto", "CARNE": "Carnê de financeira (fora da loja)"}
EM_FORMA = {"DINHEIRO": "em dinheiro", "PIX": "no PIX", "DEBITO": "no débito", "CREDITO": "no crédito",
            "BOLETO": "no boleto", "CARNE": "no carnê", "CREDIARIO": "no crediário da loja"}
TZ = "America/Sao_Paulo"
TZ_SQL = sql.Literal(TZ)   # fuso já pronto para compor consultas (psycopg.sql)


# --------------------------------------------------------------------------- #
# Dinheiro: sempre centavos inteiros
# --------------------------------------------------------------------------- #
def brl(centavos) -> str:
    if centavos is None:
        return "—"
    c = int(centavos)
    reais, cent = divmod(abs(c), 100)
    texto = f"R$ {reais:,}".replace(",", ".") + f",{cent:02d}"
    return f"- {texto}" if c < 0 else texto


def ler_json(bruto: bytes, limite: int = 256 * 1024):
    """JSON vindo de fora (avisos do WhatsApp/Mercado Pago): tamanho e profundidade limitados.
    Qualquer coisa estranha vira ValueError (nunca derruba o sistema)."""
    import json
    if len(bruto) > limite:
        raise ValueError("grande demais")
    profundidade = maior = 0
    dentro_texto = escapado = False
    for c in bruto.decode("utf-8", "strict"):
        if dentro_texto:
            escapado = (c == "\\" and not escapado)
            if c == '"' and not escapado:
                dentro_texto = False
            continue
        if c == '"':
            dentro_texto = True
        elif c in "[{":
            profundidade += 1
            maior = max(maior, profundidade)
            if maior > 32:
                raise ValueError("profundo demais")
        elif c in "]}":
            profundidade -= 1
    return json.loads(bruto)


def volta_segura(url: str | None, padrao: str) -> str:
    """Só aceita voltar para uma página DESTE sistema (evita redirecionar para site de golpe: //x.com, /\\x.com)."""
    if url and re.fullmatch(r"/(?![/\\])[A-Za-z0-9/_\-?=&.%#]{0,200}", url):
        return url
    return padrao



def ler_reais(texto: str | None) -> int | None:
    """'1.250,50' / '1250,5' / '1250.50' / 'R$ 150' -> centavos. None se inválido ou <= 0."""
    if not texto:
        return None
    t = texto.strip().replace("R$", "").replace(" ", "")
    if not re.fullmatch(r"[\d.,]+", t):
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) == 3:
        t = t.replace(".", "")                   # "1.250" = mil duzentos e cinquenta
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        valor = Decimal(t)
    except InvalidOperation:
        return None
    centavos = int((valor * 100).quantize(Decimal("1")))
    if centavos <= 0 or centavos > 100_000_000:   # teto de R$ 1 milhão por lançamento
        return None
    return centavos


def reais_para_campo(centavos) -> str:
    """Valor para voltar ao campo do formulário: 1250,50"""
    if not centavos:
        return ""
    reais, cent = divmod(int(centavos), 100)
    return f"{reais},{cent:02d}"


def telefone(t) -> str:
    d = re.sub(r"\D", "", t or "")
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return t or ""


def cpf(t) -> str:
    d = re.sub(r"\D", "", t or "")
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else (t or "")


def data_br(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


templates.env.filters["brl"] = brl
templates.env.filters["data"] = data_br
templates.env.filters["campo"] = reais_para_campo
templates.env.globals["nova_chave"] = lambda: nova_chave()
templates.env.filters["pct"] = lambda v: (f"{float(v):.2f}".rstrip("0").rstrip(".")).replace(".", ",")
templates.env.filters["telefone"] = telefone
templates.env.filters["cpf"] = cpf
templates.env.globals["FORMAS"] = FORMAS


def estatico(nome: str) -> str:
    """'/static/app.css?v=<hash do conteúdo>': o navegador pega a versão nova assim que o arquivo muda."""
    return f"/static/{nome}?v={_versao(nome)}"


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=64)
def _versao(nome: str) -> str:
    import hashlib
    arq = AQUI / "static" / nome
    try:
        return hashlib.sha256(arq.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


templates.env.globals["estatico"] = estatico


def _dica_botao(url: str) -> str:
    from .botoes import dica
    return dica(url)


templates.env.globals["dica_botao"] = _dica_botao


def _chave_simbolo(nome: str) -> str:
    from .rotas.simbolos_rotas import chave
    return chave(nome)


templates.env.globals["chave_simbolo"] = _chave_simbolo
templates.env.globals["EM_FORMA"] = EM_FORMA
templates.env.globals["timedelta"] = timedelta
_marca_cache: dict = {}


def marca_atual() -> dict:
    """Logo e ícone da loja para as telas (guardado 60 s; muda na hora quando a loja troca)."""
    import time as _t
    if _marca_cache.get("ate", 0) > _t.monotonic():
        return _marca_cache["dados"]
    dados = {"logo": None, "icone": None, "nome": ""}
    try:
        from . import db
        with db.transacao() as cur:
            cur.execute("SELECT nome, logo_arquivo, icone_arquivo FROM lojas WHERE id = ctx_loja()")
            l = cur.fetchone()
            if l:
                dados = {"logo": l["logo_arquivo"], "icone": l["icone_arquivo"], "nome": l["nome"]}
    except Exception:                      # banco fora do ar ou loja não configurada: telas sem logo
        pass
    _marca_cache.update(dados=dados, ate=_t.monotonic() + 60)
    return dados


def esquecer_marca() -> None:
    _marca_cache.clear()


templates.env.globals["marca"] = marca_atual


def _qr_svg(texto, rotulo="QR code"):
    from .codigos import qr_svg
    return qr_svg(texto, rotulo)


templates.env.globals["qr_svg"] = _qr_svg
templates.env.filters["b64"] = lambda b: __import__("base64").b64encode(b or b"").decode()


# --------------------------------------------------------------------------- #
# CSRF e idempotência
# --------------------------------------------------------------------------- #
class CsrfInvalido(Exception):
    pass


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def checar_csrf(request: Request, recebido: str | None) -> None:
    esperado = request.session.get("csrf", "")
    if not esperado or not recebido or not secrets.compare_digest(esperado, recebido):
        raise CsrfInvalido


def nova_chave() -> str:
    return str(uuid.uuid4())


def chave_valida(chave: str | None) -> str | None:
    try:
        return str(uuid.UUID(chave)) if chave else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Tela
# --------------------------------------------------------------------------- #
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def _menu(request: Request, usuario: dict) -> dict:
    """Menu lateral (mesmos botões da tela inicial, respeitando cargo e botões escondidos) e o total online."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import botoes, db, presenca
    caminho = request.url.path
    extra = {"menu_secoes": [], "online_total": None}
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            visiveis = botoes.visiveis(cur, usuario)
            if "presenca.ver" in usuario["permissoes"]:
                extra["online_total"] = presenca.online(cur)["total"]
    except Exception:                 # o menu nunca derruba a tela
        visiveis = []
    ativo, tamanho = None, -1
    for b in visiveis:
        u = botoes.base(b[3])
        if (caminho == u or (u != "/" and caminho.startswith(u + "/"))) and len(u) > tamanho:
            ativo, tamanho = b[3], len(u)
    extra["menu_secoes"] = botoes.por_secao(visiveis)
    extra["menu_ativo"] = ativo if caminho != "/" else "/"
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    extra["agora_texto"] = f"{DIAS_SEMANA[agora.weekday()]}, {agora:%d/%m/%Y}"
    return extra

def tela(request: Request, nome: str, usuario: dict | None = None, status: int = 200, **ctx):
    permissoes = usuario["permissoes"] if usuario else set()
    if usuario and usuario.get("id") and "menu_secoes" not in ctx:
        ctx.update(_menu(request, usuario))
    ctx.update(
        usuario=usuario,
        csrf=csrf(request),
        pode=lambda *p: any(x in permissoes for x in p),
        request_id=getattr(request.state, "request_id", ""),
    )
    return templates.TemplateResponse(request, nome, ctx, status_code=status)


# --------------------------------------------------------------------------- #
# Erros do banco em português simples
# --------------------------------------------------------------------------- #
RESTRICOES = {
    "sem_conflito_de_datas": ("Esta peça já está reservada nessas datas ou está com outra cliente até a devolução. "
                              "Escolha outras datas ou outra peça."),
    "parcelado_so_no_credito": "Parcelamento só no cartão de crédito.",
    "cartao_exige_maquininha": "Escolha em qual maquininha o cartão foi passado.",
    "modalidade_definida": "Marque se a peça é para alugar, vender ou os dois.",
    "preco_aluguel_coerente": "Faltou o preço do aluguel.",
    "preco_venda_coerente": "Faltou o preço de venda.",
    "aluguel_tem_datas": "Faltam as datas de retirada e devolução (a devolução não pode ser antes da retirada).",
    "venda_ajuste_tem_retirada": "Falta a data de retirada.",
    "ciclo_exige_cliente": "Falta o nome da cliente.",
    "ajuste_cobrado_na_venda": "Informe o valor do ajuste.",
    "uq_produtos_chave": "Esta peça já foi cadastrada.",
}


def mensagem_do_banco(erro: Exception) -> str:
    diag = getattr(erro, "diag", None)
    nome = getattr(diag, "constraint_name", None) if diag else None
    if nome in RESTRICOES:
        return RESTRICOES[nome]
    if isinstance(erro, pgerr.RaiseException) and diag and diag.message_primary:
        return diag.message_primary                      # mensagens das travas já são em português
    if isinstance(erro, pgerr.ExclusionViolation):
        return RESTRICOES["sem_conflito_de_datas"]
    if isinstance(erro, pgerr.InsufficientPrivilege):
        return "Esta ação não é permitida para o seu cargo."
    return "Não foi possível guardar. Confira os dados e tente de novo."


def e_chave_repetida(erro: Exception) -> bool:
    """Clique duplo: o banco recusou por chave de idempotência repetida."""
    nome = getattr(getattr(erro, "diag", None), "constraint_name", "") or ""
    return isinstance(erro, pgerr.UniqueViolation) and "chave" in nome
