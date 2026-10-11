"""Marca da loja: enviar logo e ícone; servir logo, ícones e favicon (públicos)."""
from urllib.parse import quote

from psycopg import sql
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

from .. import db, marca
from ..auth import auditar, requer
from ..web import checar_csrf, esquecer_marca, marca_atual, tela

router = APIRouter()
CACHE = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
SUFIXOS = {"logo.png", "mono.png", "32.png", "180.png", "192.png", "512.png"}


@router.get("/config/marca")
def ver(request: Request, ok: str = "", erro: str = "", usuario: dict = Depends(requer("site.editar"))):
    return tela(request, "config_marca.html", usuario, ok=ok, erro=erro, m=marca_atual())


async def _arquivo(request: Request) -> tuple[bytes, dict]:
    form = await request.form(max_files=1, max_fields=5)
    checar_csrf(request, str(form.get("csrf") or ""))
    f = form.get("arquivo")
    dados = await f.read(marca.MAX_BYTES + 1) if hasattr(f, "read") else b""
    return dados, form


def _gravar(usuario, request, dados: bytes, tipo: str) -> str | None:
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT nome, logo_arquivo FROM lojas WHERE id = ctx_loja()")
            l = cur.fetchone()
            if tipo == "logo":
                nome = marca.salvar_logo(dados)
                icone = marca.salvar_icone(None, nome, l["nome"])
                cur.execute("""UPDATE lojas SET logo_arquivo = %s,
                                      icone_arquivo = COALESCE(icone_arquivo, %s) WHERE id = ctx_loja()""", (nome, icone))
            else:
                icone = marca.salvar_icone(dados, None, l["nome"])
                cur.execute("UPDATE lojas SET icone_arquivo = %s WHERE id = ctx_loja()", (icone,))
            auditar(cur, request, f"marca_{tipo}_alterada", usuario["id"], "loja")
    except marca.MarcaInvalida as e:
        return str(e)
    esquecer_marca()
    return None


@router.post("/config/marca/logo")
async def enviar_logo(request: Request, usuario: dict = Depends(requer("site.editar"))):
    dados, _ = await _arquivo(request)
    erro = await run_in_threadpool(_gravar, usuario, request, dados, "logo")
    return RedirectResponse("/config/marca?" + (f"erro={quote(erro)}" if erro else f"ok={quote('Logo salvo. Já aparece no site, no sistema e nos impressos.')}"),
                            status_code=303)


@router.post("/config/marca/icone")
async def enviar_icone(request: Request, usuario: dict = Depends(requer("site.editar"))):
    dados, _ = await _arquivo(request)
    erro = await run_in_threadpool(_gravar, usuario, request, dados, "icone")
    return RedirectResponse("/config/marca?" + (f"erro={quote(erro)}" if erro else f"ok={quote('Ícone salvo (aba do navegador e celular).')}"),
                            status_code=303)


@router.post("/config/marca/remover")
async def remover(request: Request, usuario: dict = Depends(requer("site.editar"))):
    form = await request.form(max_fields=5)
    checar_csrf(request, str(form.get("csrf") or ""))
    campo = "logo_arquivo" if form.get("qual") == "logo" else "icone_arquivo"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(sql.SQL("UPDATE lojas SET {} = NULL WHERE id = ctx_loja()").format(sql.Identifier(campo)))  # campo vem da lista acima
        auditar(cur, request, "marca_removida", usuario["id"], "loja", None, {"campo": campo})
    esquecer_marca()
    return RedirectResponse(f"/config/marca?ok={quote('Removido. O arquivo antigo fica guardado.')}", status_code=303)


@router.get("/marca/{nome}-{sufixo}")
def arquivo(nome: str, sufixo: str):
    if not marca.arquivo_valido(nome) or sufixo not in SUFIXOS:
        return Response(status_code=404)
    m = marca_atual()
    if nome not in (m["logo"], m["icone"]):                 # só a marca em uso (as antigas não ficam públicas)
        return Response(status_code=404)
    p = marca.caminho(nome, sufixo)
    if not p.exists():
        return Response(status_code=404)
    return Response(p.read_bytes(), media_type="image/png", headers=CACHE)


def _icone(lado: int) -> Response:
    m = marca_atual()
    if m["icone"] and marca.caminho(m["icone"], f"{lado}.png").exists():
        return Response(marca.caminho(m["icone"], f"{lado}.png").read_bytes(), media_type="image/png", headers=CACHE)
    return Response(marca.icone_padrao(m["nome"] or "Loja", lado), media_type="image/png", headers=CACHE)


@router.get("/favicon.ico")
def favicon():
    m = marca_atual()
    if m["icone"] and marca.caminho(m["icone"], "ico").exists():
        return Response(marca.caminho(m["icone"], "ico").read_bytes(), media_type="image/x-icon", headers=CACHE)
    return Response(marca.icone_padrao(m["nome"] or "Loja", 48, ico=True), media_type="image/x-icon", headers=CACHE)


@router.get("/icone-{lado}.png")
def icone(lado: int):
    return _icone(lado) if lado in marca.ICONES else Response(status_code=404)


@router.get("/apple-touch-icon.png")
def apple():
    return _icone(180)
