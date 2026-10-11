"""Símbolos das redes sociais e das formas de pagamento no rodapé do site."""
import re
import unicodedata

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import db, marca
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import checar_csrf, tela

router = APIRouter()
REDES = {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "youtube": "YouTube",
         "pinterest": "Pinterest", "whatsapp": "WhatsApp"}
GENERICOS = {"dinheiro": "dinheiro", "cartao-de-debito": "cartao", "cartao-de-credito": "cartao",
             "link-de-pagamento": "link", "site-externo": "globo"}


def chave(nome: str) -> str:
    t = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40]


def mapa(cur) -> dict:
    cur.execute("SELECT chave, arquivo FROM simbolos")
    return {r["chave"]: r["arquivo"] for r in cur.fetchall()}


def _lista(cur):
    cur.execute("SELECT formas_site, bandeiras_site FROM lojas WHERE id = ctx_loja()")
    loja = cur.fetchone()
    itens = [(k, n, "Rede social") for k, n in REDES.items()]
    itens += [(chave(f), f, "Forma de pagamento") for f in loja["formas_site"]]
    itens += [(chave(b), b, "Bandeira de cartão") for b in loja["bandeiras_site"]]
    atuais = mapa(cur)
    return [{"chave": k, "nome": n, "tipo": t, "arquivo": atuais.get(k), "generico": GENERICOS.get(k)}
            for k, n, t in itens]


@router.get("/config/simbolos")
def ver(request: Request, ok: str = "", erro: str = "", usuario: dict = Depends(requer("site.editar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        itens = _lista(cur)
    return tela(request, "config_simbolos.html", usuario, itens=itens, ok=ok[:80], erro=erro[:120])


@router.post("/config/simbolos/{ch}")
async def enviar(request: Request, ch: str, usuario: dict = Depends(requer("site.editar"))):
    dados, arquivo = {}, None
    async with request.form(max_files=1, max_fields=5) as f:
        for k, v in f.multi_items():
            if hasattr(v, "read"):
                if k == "arquivo" and v.filename:
                    arquivo = await v.read(marca.MAX_BYTES + 1)
            else:
                dados[k] = str(v)
    checar_csrf(request, dados.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        validas = {i["chave"] for i in _lista(cur)}
        if ch not in validas:
            return RedirectResponse("/config/simbolos", status_code=303)
        if dados.get("acao") == "tirar":
            cur.execute("DELETE FROM simbolos WHERE chave = %s", (ch,))
            auditar(cur, request, "simbolo_tirado", usuario["id"], "loja", None, {"chave": ch})
            return RedirectResponse("/config/simbolos?ok=Voltou+a+aparecer+o+nome", status_code=303)
        try:
            nome = marca.salvar_simbolo(arquivo or b"")
        except marca.MarcaInvalida as e:
            return RedirectResponse(f"/config/simbolos?erro={str(e)}#{ch}", status_code=303)
        cur.execute("""INSERT INTO simbolos (loja_id, chave, arquivo, definido_por) VALUES (ctx_loja(), %s, %s, %s)
                       ON CONFLICT (loja_id, chave) DO UPDATE SET arquivo = EXCLUDED.arquivo,
                           definido_por = EXCLUDED.definido_por, definido_em = now()""", (ch, nome, usuario["id"]))
        auditar(cur, request, "simbolo_enviado", usuario["id"], "loja", None, {"chave": ch})
    return RedirectResponse(f"/config/simbolos?ok=Símbolo+guardado#{ch}", status_code=303)


@router.get("/simbolo/{arquivo}.png")
def mostrar(arquivo: str):
    if not marca.arquivo_valido(arquivo):
        return Response(status_code=404)
    with db.transacao() as cur:
        cur.execute("SELECT 1 FROM simbolos WHERE arquivo = %s", (arquivo,))
        existe = cur.fetchone()
    caminho = marca.pasta_simbolos() / f"{arquivo}.png"
    if not existe or not caminho.exists():
        return Response(status_code=404)
    return FileResponse(caminho, media_type="image/png", headers={"Cache-Control": "public, max-age=2592000, immutable"})
