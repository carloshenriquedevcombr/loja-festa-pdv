"""Painel do site: banners de promoção, os 4 destaques, avaliações (aprovar/responder) e nota do Google.
Do lado do site: a cliente avalia o pedido pelo link secreto que chega no WhatsApp."""
import re
from datetime import date
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import config, db, fotos, vitrine
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import checar_csrf, mensagem_do_banco, tela

router = APIRouter()


# --------------------------------------------------------------------------- #
# Banners e destaques
# --------------------------------------------------------------------------- #
def _tela_banners(request, usuario, erro=None, ok="", status=200, v=None):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT id::text, titulo, texto, botao, link, imagem, tema, inicio, fim, ordem, ativo
                         FROM banners ORDER BY ativo DESC, ordem, criado_em DESC""")
        banners = cur.fetchall()
        destaques = vitrine.destaques(cur)
    return tela(request, "config_banners.html", usuario, status=status, banners=banners, destaques=destaques,
                erro=erro, ok=ok or request.query_params.get("ok", ""), v=v or {}, TEMAS=vitrine.TEMAS,
                ICONES=vitrine.ICONES)


@router.get("/config/banners")
def banners(request: Request, usuario: dict = Depends(requer("site.editar"))):
    return _tela_banners(request, usuario)


def _data(texto: str | None):
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def _ler_banner(form: dict) -> tuple[dict, str | None]:
    d = {"titulo": (form.get("titulo") or "").strip()[:80], "texto": (form.get("texto") or "").strip()[:160] or None,
         "botao": (form.get("botao") or "").strip()[:30] or None, "link": vitrine.link_do_site(form.get("link")),
         "tema": form.get("tema") if form.get("tema") in vitrine.TEMAS else "escuro",
         "inicio": _data(form.get("inicio")), "fim": _data(form.get("fim"))}
    try:
        d["ordem"] = max(0, min(int(form.get("ordem") or 0), 99))
    except ValueError:
        d["ordem"] = 0
    if len(d["titulo"]) < 2:
        return d, "Escreva o título do banner (ex.: Semana das madrinhas: 15% no aluguel)."
    if (form.get("link") or "").strip() and not d["link"]:
        return d, "O link do banner precisa ser uma página do site (ex.: /site/colecao ou /site/c/madrinhas)."
    if d["inicio"] and d["fim"] and d["fim"] < d["inicio"]:
        return d, "A data de fim é antes do início."
    return d, None


async def _form_banner(request: Request) -> tuple[dict, bytes | None]:
    dados, imagem = {}, None
    async with request.form(max_files=1, max_fields=20) as f:
        for k, v in f.multi_items():
            if hasattr(v, "read"):
                if k == "imagem" and v.filename:
                    imagem = await v.read(fotos.MAX_BYTES + 1)
            elif k not in dados:
                dados[k] = str(v)
    return dados, imagem


@router.post("/config/banners")
async def novo_banner(request: Request, usuario: dict = Depends(requer("site.editar"))):
    form, imagem = await _form_banner(request)
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_banner(form)
    if not erro and imagem:
        try:
            d["imagem"], largura, altura = fotos.processar(imagem)
            if largura < 900:
                erro = "Imagem do banner pequena demais: use uma foto deitada com pelo menos 1200 px de largura."
        except fotos.FotoInvalida as e:
            erro = str(e)
    if erro:
        return _tela_banners(request, usuario, erro=erro, status=400, v=form)
    d.setdefault("imagem", None)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""INSERT INTO banners (loja_id, titulo, texto, botao, link, imagem, tema, inicio, fim, ordem)
                       VALUES (ctx_loja(), %(titulo)s, %(texto)s, %(botao)s, %(link)s, %(imagem)s, %(tema)s,
                               %(inicio)s, %(fim)s, %(ordem)s) RETURNING id::text""", d)
        auditar(cur, request, "banner_criado", usuario["id"], "banner", cur.fetchone()["id"], {"titulo": d["titulo"]})
    return RedirectResponse("/config/banners?ok=Banner+publicado", status_code=303)


@router.post("/config/banners/{bid}")
def editar_banner(request: Request, bid: UUID, usuario: dict = Depends(requer("site.editar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_banner(form)
    if erro:
        return _tela_banners(request, usuario, erro=erro, status=400)
    d.update(id=str(bid), ativo=form.get("ativo") == "1")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE banners SET titulo = %(titulo)s, texto = %(texto)s, botao = %(botao)s, link = %(link)s,
                              tema = %(tema)s, inicio = %(inicio)s, fim = %(fim)s, ordem = %(ordem)s, ativo = %(ativo)s
                        WHERE id = %(id)s""", d)
        auditar(cur, request, "banner_alterado", usuario["id"], "banner", str(bid), {"ativo": d["ativo"]})
    return RedirectResponse("/config/banners?ok=Banner+guardado", status_code=303)


@router.post("/config/destaques")
def salvar_destaques(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    linhas = []
    for n in range(1, 5):
        titulo = (form.get(f"titulo{n}") or "").strip()[:40]
        icone = form.get(f"icone{n}")
        if len(titulo) < 2 or icone not in vitrine.ICONES:
            return _tela_banners(request, usuario, erro=f"Destaque {n}: escolha o ícone e escreva o título.", status=400)
        link = vitrine.link_do_site(form.get(f"link{n}"))
        linhas.append((n, icone, titulo, (form.get(f"texto{n}") or "").strip()[:120] or None, link))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        for n, icone, titulo, texto, link in linhas:
            cur.execute("""INSERT INTO destaques (loja_id, posicao, icone, titulo, texto, link)
                           VALUES (ctx_loja(), %s, %s, %s, %s, %s)
                           ON CONFLICT (loja_id, posicao) DO UPDATE SET icone = EXCLUDED.icone, titulo = EXCLUDED.titulo,
                               texto = EXCLUDED.texto, link = EXCLUDED.link""", (n, icone, titulo, texto, link))
        auditar(cur, request, "destaques_alterados", usuario["id"], "loja", None)
    return RedirectResponse("/config/banners?ok=Destaques+guardados#destaques", status_code=303)


# --------------------------------------------------------------------------- #
# Avaliações: aprovar/recusar/responder + nota do Google
# --------------------------------------------------------------------------- #
@router.get("/config/avaliacoes")
def avaliacoes(request: Request, ver: str = "PENDENTE", ok: str = "",
               usuario: dict = Depends(requer("avaliacoes.moderar"))):
    ver = ver if ver in ("PENDENTE", "APROVADA", "RECUSADA") else "PENDENTE"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT a.id::text, a.nota, a.texto, a.nome_exibicao, a.autoriza_site, a.status, a.resposta,
                              to_char(a.criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY') AS quando,
                              a.pedido_id::text AS pedido_id, pr.sku, cl.nome AS cliente
                         FROM avaliacoes a JOIN pedidos pe ON pe.id = a.pedido_id JOIN produtos pr ON pr.id = pe.produto_id
                         LEFT JOIN clientes cl ON cl.id = a.cliente_id
                        WHERE a.status = %s ORDER BY a.criado_em DESC LIMIT 200""", (ver,))
        lista = cur.fetchall()
        cur.execute("SELECT status, count(*) AS n FROM avaliacoes GROUP BY status")
        contagem = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("""SELECT google_nota, google_total, google_link, google_place_id, avaliacoes_no_site,
                              to_char(google_atualizado AT TIME ZONE 'America/Sao_Paulo', 'DD/MM HH24:MI') AS atualizado
                         FROM lojas WHERE id = ctx_loja()""")
        loja = cur.fetchone()
    return tela(request, "config_avaliacoes.html", usuario, lista=lista, ver=ver, contagem=contagem, loja=loja, ok=ok,
                tem_chave=bool(config.GOOGLE_PLACES_KEY))


@router.post("/config/avaliacoes/{aid}")
def moderar(request: Request, aid: UUID, usuario: dict = Depends(requer("avaliacoes.moderar")),
            form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    acao = form.get("acao")
    status = {"aprovar": "APROVADA", "recusar": "RECUSADA", "responder": None}.get(acao, "?")
    if status == "?":
        return RedirectResponse("/config/avaliacoes", status_code=303)
    resposta = (form.get("resposta") or "").strip()[:400] or None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""UPDATE avaliacoes SET status = COALESCE(%s, status), resposta = %s WHERE id = %s
                           RETURNING status""", (status, resposta, str(aid)))
            linha = cur.fetchone()
            auditar(cur, request, f"avaliacao_{acao}", usuario["id"], "avaliacao", str(aid))
    except psycopg.Error as e:
        return RedirectResponse(f"/config/avaliacoes?ok={mensagem_do_banco(e)[:80]}", status_code=303)
    volta = linha["status"] if linha else "PENDENTE"
    return RedirectResponse(f"/config/avaliacoes?ver={'PENDENTE' if acao != 'responder' else volta}&ok=Guardado",
                            status_code=303)


@router.post("/config/google")
def salvar_google(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    try:
        nota = round(float((form.get("google_nota") or "").replace(",", ".")), 1) if form.get("google_nota") else None
        total = int(re.sub(r"\D", "", form.get("google_total") or "") or 0) if form.get("google_total") else None
    except ValueError:
        nota, total = None, None
    if nota is not None and not 1 <= nota <= 5:
        nota = None
    link = (form.get("google_link") or "").strip() or None
    place = (form.get("google_place_id") or "").strip() or None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""UPDATE lojas SET google_nota = %s, google_total = %s, google_link = %s, google_place_id = %s,
                                  avaliacoes_no_site = %s, google_atualizado = NULL WHERE id = ctx_loja()""",
                        (nota, total, link, place, form.get("avaliacoes_no_site") == "1"))
            auditar(cur, request, "google_config", usuario["id"], "loja", None, {"nota": str(nota), "place": bool(place)})
    except psycopg.Error:
        return RedirectResponse("/config/avaliacoes?ok=Link+do+Google+ou+Place+ID+inválido", status_code=303)
    return RedirectResponse("/config/avaliacoes?ok=Nota+do+Google+guardada", status_code=303)


# --------------------------------------------------------------------------- #
# Site: a cliente avalia (link do WhatsApp)
# --------------------------------------------------------------------------- #
def _pedido_para_avaliar(cur, token):
    pid = vitrine.ler_token(token)
    if not pid:
        return None
    cur.execute("""SELECT pe.id::text AS id, pe.status, pr.nome AS peca, cl.nome AS cliente,
                          (SELECT a.nota FROM avaliacoes a WHERE a.pedido_id = pe.id) AS ja_nota
                     FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                     LEFT JOIN clientes cl ON cl.id = pe.cliente_id WHERE pe.id = %s""", (pid,))
    return cur.fetchone()


@router.get("/site/avaliar/{token}")
def avaliar_form(request: Request, token: str):
    from .site import _pagina
    with db.transacao() as cur:
        p = _pedido_para_avaliar(cur, token)
        if not p:
            return _pagina(request, "site/nao_encontrada.html", cur, status=404)
        return _pagina(request, "site/avaliar.html", cur, p=p, token=token, v={"nome": vitrine.nome_publico(p["cliente"])},
                       destaques=[], avaliacoes=None)


@router.post("/site/avaliar/{token}")
def avaliar(request: Request, token: str, form: dict = Depends(formulario)):
    from .site import _pagina
    checar_csrf(request, form.get("csrf"))
    try:
        nota = int(form.get("nota") or 0)
    except ValueError:
        nota = 0
    nome = re.sub(r"\s+", " ", (form.get("nome") or "").strip())[:40]
    texto = (form.get("texto") or "").strip()[:600] or None
    with db.transacao() as cur:
        p = _pedido_para_avaliar(cur, token)
        if not p:
            return _pagina(request, "site/nao_encontrada.html", cur, status=404)
    erro = None
    if not 1 <= nota <= 5:
        erro = "Escolha de 1 a 5 estrelas."
    elif len(nome) < 2:
        erro = "Escreva como seu nome vai aparecer (ex.: Maria S.)."
    if not erro and not p["ja_nota"]:
        try:
            with db.transacao() as cur:
                cur.execute("""INSERT INTO avaliacoes (loja_id, pedido_id, nota, texto, nome_exibicao, autoriza_site)
                               VALUES (ctx_loja(), %s, %s, %s, %s, %s)""",
                            (p["id"], nota, texto, nome, form.get("autoriza") == "1"))
        except psycopg.Error as e:
            erro = mensagem_do_banco(e)
    with db.transacao() as cur:
        if erro:
            return _pagina(request, "site/avaliar.html", cur, p=p, token=token, v=form, erro=erro, status=400,
                           destaques=[], avaliacoes=None)
        return _pagina(request, "site/avaliar.html", cur, p={**p, "ja_nota": p["ja_nota"] or nota}, token=token,
                       obrigado=True, destaques=[], avaliacoes=None)
