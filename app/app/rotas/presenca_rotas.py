"""Presença online (equipe e clientes), página "Online agora" e a busca rápida do topo."""
import hashlib
import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from .. import codigos, config, contas, db, presenca, sessao
from ..auth import ip, requer
from ..web import CsrfInvalido, checar_csrf, ler_json, tela

router = APIRouter()


def _mesma_origem(request: Request) -> bool:
    """O sinal só vale vindo de uma página da própria loja (outro site não finge presença), com o token CSRF."""
    try:
        checar_csrf(request, request.headers.get("x-csrf"))
    except CsrfInvalido:
        return False
    origem = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        return False
    return not origem or urlparse(origem).netloc == request.headers.get("host", "")


async def _pagina(request: Request) -> str:
    try:
        corpo = ler_json((await request.body())[:2000] or b"{}", limite=2000)
    except ValueError:
        corpo = {}
    return presenca.area(corpo.get("p") if isinstance(corpo, dict) else None)


@router.post("/presenca")
async def sinal_equipe(request: Request):
    """Não conta como "usar o sistema": a saída automática por tempo parado continua valendo."""
    uid, sid = request.session.get("usuario_id"), request.session.get("sid")
    if not _mesma_origem(request):
        return Response(status_code=403)
    if not uid or not sid or not config.LOJA_ID:
        return Response(status_code=401)
    if int(time.time()) - request.session.get("visto", 0) > config.SESSAO_OCIOSA_MIN * 60:
        return Response(status_code=401)
    pagina = await _pagina(request)
    from ..auth import aparelho_da_requisicao
    with db.transacao(usuario_id=uid) as cur:
        if sessao.conferir(cur, sid, uid, aparelho_da_requisicao(request), request.headers.get("user-agent"), ip(request)):
            return Response(status_code=401)
        cur.execute("UPDATE sessoes SET visto_em = now(), pagina = %s WHERE id = %s AND encerrada_em IS NULL",
                    (pagina, hashlib.sha256(sid.encode()).hexdigest()))
    return Response(status_code=204)


@router.post("/site/presenca")
async def sinal_site(request: Request):
    if not config.LOJA_ID or not _mesma_origem(request):
        return Response(status_code=403)
    pagina = await _pagina(request)
    if not pagina.startswith("/site"):
        pagina = "/site"
    s = request.session.get(contas.CHAVE_SESSAO)
    if isinstance(s, dict) and s.get("sid") and len(str(s["sid"])) <= 100:
        with db.transacao() as cur:
            cur.execute("""UPDATE sessoes_clientes SET visto_em = now(), pagina = %s
                            WHERE id = %s AND encerrada_em IS NULL AND expira_em > now()
                              AND navegador = %s RETURNING 1""",
                        (pagina, hashlib.sha256(s["sid"].encode()).hexdigest(),
                         hashlib.sha256(("ua:" + (request.headers.get("user-agent") or "")[:400]).encode()).hexdigest()))
            if cur.fetchone():
                return Response(status_code=204)
    presenca.visitante(ip(request), request.headers.get("user-agent"), pagina)
    return Response(status_code=204)


@router.get("/online")
def online(request: Request, usuario: dict = Depends(requer("presenca.ver"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        dados = presenca.online(cur)
        cur.execute("""SELECT u.nome, to_char(max(s.visto_em) AT TIME ZONE 'America/Sao_Paulo', 'DD/MM HH24:MI') AS quando
                         FROM usuarios u LEFT JOIN sessoes s ON s.usuario_id = u.id
                        WHERE u.ativo AND NOT u.sistema GROUP BY u.id ORDER BY max(s.visto_em) DESC NULLS LAST LIMIT 30""")
        ultimos = cur.fetchall()
    online_nomes = {f["nome"] for f in dados["equipe"]}
    return tela(request, "online.html", usuario, o=dados, ultimos=[u for u in ultimos if u["nome"] not in online_nomes])


@router.get("/buscar")
def buscar(request: Request, q: str = "", usuario: dict = Depends(requer("pedidos.ver", "clientes.ver", "catalogo.ver"))):
    """Busca do topo: código do comprovante/QR → pedido; código de peça → peça; resto → pedidos e clientes."""
    q = q.strip()[:60]
    if not q:
        return RedirectResponse("/", status_code=303)
    numero, _ = codigos.separar(q)
    perms = usuario["permissoes"]
    with db.transacao(usuario_id=usuario["id"]) as cur:
        if codigos.parece_codigo_pedido(numero) and "pedidos.ver" in perms:
            cur.execute("SELECT id::text FROM pedidos WHERE replace(id::text, '-', '') LIKE %s LIMIT 2", (numero.lower() + "%",))
            achados = cur.fetchall()
            if len(achados) == 1:
                return RedirectResponse(f"/pedidos/{achados[0]['id']}", status_code=303)
        if "caixa.entrada" in perms:
            from .. import crediario
            parc = crediario.achar_parcela(cur, q)
            if parc:
                return RedirectResponse(f"/crediario/{parc[0]}/parcela/{parc[1]}", status_code=303)
        if "catalogo.ver" in perms:
            cur.execute("SELECT id::text FROM produtos WHERE upper(sku) = upper(%s)", (q,))
            peca = cur.fetchone()
            if peca:
                return RedirectResponse(f"/pecas/{peca['id']}", status_code=303)
        from .caixa import filtro_cliente
        filtro, params = filtro_cliente(q)
        pedidos, clientes, pecas = [], [], []
        if "pedidos.ver" in perms:
            cur.execute(f"""SELECT pe.id::text AS id, pr.sku, pr.nome AS peca, cl.nome AS cliente, pe.status
                              FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id LEFT JOIN clientes cl ON cl.id = pe.cliente_id
                             WHERE pr.sku ILIKE %(sku)s OR {filtro} ORDER BY pe.criado_em DESC LIMIT 20""",
                        {**params, "sku": f"%{q}%"})
            pedidos = cur.fetchall()
        if "clientes.ver" in perms:
            cur.execute(f"SELECT cl.id::text AS id, cl.nome FROM clientes cl WHERE {filtro} ORDER BY cl.nome LIMIT 20", params)
            clientes = cur.fetchall()
        if "catalogo.ver" in perms:
            cur.execute("""SELECT id::text, sku, nome FROM produtos
                            WHERE status <> 'ARQUIVADO' AND (sku ILIKE %s OR f_unaccent(lower(nome)) LIKE f_unaccent(lower(%s)))
                            ORDER BY criado_em DESC LIMIT 20""", (f"%{q}%", f"%{q}%"))
            pecas = cur.fetchall()
    from .pedidos import STATUS
    return tela(request, "buscar.html", usuario, q=q, pedidos=pedidos, clientes=clientes, pecas=pecas, STATUS=STATUS)
