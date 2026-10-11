"""Catálogo: procurar, cadastrar (SKU automático), fotos, ocasiões, editar, arquivar e etiqueta."""
import re
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import db, fotos
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import (chave_valida, checar_csrf, e_chave_repetida, ler_reais, mensagem_do_banco,
                   nova_chave, reais_para_campo, tela)

router = APIRouter()
ATIVOS = "('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')"
SITUACAO = {"DISPONIVEL": "Disponível", "RESERVADO": "Reservada", "EM_AJUSTE": "Em ajuste",
            "ALUGADO": "Alugada", "EM_LIMPEZA": "Na limpeza", "MANUTENCAO": "Em conserto",
            "VENDIDO": "Vendida", "ARQUIVADO": "Arquivada"}


def normalizar_sku(q: str) -> str | None:
    """'ves12', 'VES-0012', 'ves 12' -> 'VES-0012'"""
    m = re.fullmatch(r"\s*([A-Za-z]{2,5})\s*-?\s*(\d{1,6})\s*", q or "")
    return f"{m.group(1).upper()}-{int(m.group(2)):04d}" if m else None


def buscar(cur, q: str = "", filtro: str = "todas", limite: int = 60) -> list[dict]:
    q = (q or "").strip()[:60]
    condicoes, params = ["ps.status <> 'ARQUIVADO'"], {"sku": normalizar_sku(q) or "", "q": q}
    if filtro == "alugar":
        condicoes.append("ps.pode_alugar")
    elif filtro == "vender":
        condicoes.append("ps.pode_vender")
    ordem = "ps.sku"
    if q:
        # Cada palavra precisa aparecer (sem acento) em nome/cor/tamanho/categoria; OU parecido (erro de digitação)
        texto = "f_unaccent(lower(ps.nome || ' ' || coalesce(ps.cor,'') || ' ' || coalesce(ps.tamanho,'') || ' ' || c.nome || ' ' || ps.sku))"
        palavras = []
        for i, p in enumerate(q.split()[:6]):
            params[f"p{i}"] = f"%{p}%"
            palavras.append(f"{texto} LIKE f_unaccent(lower(%(p{i})s))")
        condicoes.append(f"(ps.sku = %(sku)s OR ({' AND '.join(palavras)}) "
                         f"OR f_unaccent(lower(ps.nome)) %% f_unaccent(lower(%(q)s)))")
        ordem = "(ps.sku = %(sku)s) DESC, similarity(f_unaccent(lower(ps.nome)), f_unaccent(lower(%(q)s))) DESC, ps.sku"
    cur.execute(f"""
        SELECT ps.id::text, ps.sku, ps.nome, ps.tamanho, ps.cor, ps.pode_alugar, ps.pode_vender,
               ps.preco_aluguel_centavos, ps.preco_venda_centavos, ps.situacao_hoje, c.nome AS categoria,
               (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = ps.id AND f.ativa
                 ORDER BY f.ordem, f.criado_em LIMIT 1) AS capa
          FROM produtos_situacao ps JOIN categorias c ON c.id = ps.categoria_id
         WHERE {' AND '.join(condicoes)}
         ORDER BY {ordem} LIMIT {int(limite)}""", params)
    return cur.fetchall()


@router.get("/pecas")
def procurar(request: Request, q: str = "", filtro: str = "todas", escolher: int = 0,
             cliente: UUID | None = None, usuario: dict = Depends(requer("catalogo.ver"))):
    filtro = filtro if filtro in ("alugar", "vender", "todas") else "todas"
    from .. import codigos
    if codigos.parece_codigo_pedido(q) and "pedidos.ver" in usuario["permissoes"]:   # leitor no checklist do pedido
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT id::text FROM pedidos WHERE replace(id::text, '-', '') LIKE %s LIMIT 2", (q.strip().lower() + "%",))
            achados = cur.fetchall()
        if len(achados) == 1:
            return RedirectResponse(f"/pedidos/{achados[0]['id']}", status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pecas = buscar(cur, q, filtro)
        sku = normalizar_sku(q)
    if sku and len(pecas) == 1 and pecas[0]["sku"] == sku and not escolher:
        return RedirectResponse(f"/pecas/{pecas[0]['id']}", status_code=303)   # leitor de código de barras
    return tela(request, "pecas_lista.html", usuario, pecas=pecas, q=q, filtro=filtro,
                escolher=escolher, cliente=cliente, SITUACAO=SITUACAO)


# --------------------------------------------------------------------------- #
# Cadastrar e editar
# --------------------------------------------------------------------------- #
def _categorias(cur):
    cur.execute("SELECT id, nome, prefixo_sku FROM categorias ORDER BY id")
    return cur.fetchall()


PUBLICO = {"FEMININO": "Feminino", "MASCULINO": "Masculino", "UNISSEX": "Unissex"}
FAIXA = {"ADULTO": "Adulto", "INFANTIL": "Infantil"}
COMPRIMENTO = {"CURTO": "Curto", "MIDI": "Midi", "LONGO": "Longo"}


def ocasioes_arvore(cur, so_ativas: bool = True) -> list[dict]:
    """[{id, nome, slug, filhos: [...]}] na ordem da loja."""
    cur.execute("SELECT id, pai_id, nome, slug, ordem, ativa FROM ocasioes"
                + (" WHERE ativa" if so_ativas else "") + " ORDER BY ordem, nome")
    linhas = cur.fetchall()
    grupos = [dict(o, filhos=[]) for o in linhas if o["pai_id"] is None]
    por_id = {g["id"]: g for g in grupos}
    for o in linhas:
        if o["pai_id"] in por_id:
            por_id[o["pai_id"]]["filhos"].append(o)
    return grupos


TAMANHOS = ("XPP", "PP", "P", "M", "G", "GG", "EG", "EGG", "U")
TAMANHOS_NUM = tuple(str(n) for n in range(34, 58, 2))


def ler_tamanhos(form: dict) -> str | None:
    """Várias opções marcadas (XPP, PP, P, M, G, GG, EG, 36, 38…) + outro escrito → 'P, M, G'."""
    escolhidos = [t for t in TAMANHOS + TAMANHOS_NUM if form.get(f"tam_{t}") == "1"]
    for extra in (form.get("tamanho_outro") or form.get("tamanho") or "").replace("/", ",").split(","):
        e = extra.strip().upper()[:10]
        if e and e not in escolhidos:
            escolhidos.append(e)
    return ", ".join(escolhidos[:12])[:60] or None


def tamanhos_marcados(texto: str | None) -> dict:
    lista = [t.strip().upper() for t in (texto or "").replace("/", ",").split(",") if t.strip()]
    v = {f"tam_{t}": "1" for t in lista if t in TAMANHOS + TAMANHOS_NUM}
    v["tamanho_outro"] = ", ".join(t for t in lista if t not in TAMANHOS + TAMANHOS_NUM)
    return v


def _ler_peca(form: dict, nova: bool) -> tuple[dict, str | None]:
    uso = form.get("uso", "")
    d = {"nome": (form.get("nome") or "").strip()[:120],
         "tamanho": ler_tamanhos(form),
         "cor": (form.get("cor") or "").strip()[:40] or None,
         "descricao": (form.get("descricao") or "").strip()[:500] or None,
         "pode_alugar": uso in ("alugar", "ambos"), "pode_vender": uso in ("vender", "ambos"),
         "preco_aluguel": ler_reais(form.get("preco_aluguel")) if uso in ("alugar", "ambos") else None,
         "preco_venda": ler_reais(form.get("preco_venda")) if uso in ("vender", "ambos") else None,
         "publico": form.get("publico") if form.get("publico") in PUBLICO else "FEMININO",
         "faixa": form.get("faixa") if form.get("faixa") in FAIXA else "ADULTO",
         "comprimento": form.get("comprimento") if form.get("comprimento") in COMPRIMENTO else None,
         "ocasioes": sorted({int(k[8:]) for k, v in form.items()
                             if k.startswith("ocasiao_") and k[8:].isdigit() and v == "1"})[:30],
         "no_site": form.get("no_site") == "1", "preco_no_site": form.get("preco_no_site") == "1"}
    try:
        principal = int(form.get("ocasiao_principal") or 0)
    except ValueError:
        principal = 0
    d["ocasiao_principal"] = principal if principal in d["ocasioes"] else (d["ocasioes"][0] if d["ocasioes"] else None)
    if nova:
        try:
            d["categoria_id"] = int(form.get("categoria") or 0)
        except ValueError:
            d["categoria_id"] = 0
        if not d["categoria_id"]:
            return d, "Escolha o tipo de peça (vestido, terno...)."
    if len(d["nome"]) < 3:
        return d, "Escreva um nome para a peça (ex.: Vestido longo azul marinho)."
    if uso not in ("alugar", "vender", "ambos"):
        return d, "Marque se a peça é para alugar, vender ou os dois."
    if d["pode_alugar"] and not d["preco_aluguel"]:
        return d, "Digite o preço do aluguel."
    if d["pode_vender"] and not d["preco_venda"]:
        return d, "Digite o preço de venda."
    return d, None


@router.get("/pecas/nova")
def nova_form(request: Request, usuario: dict = Depends(requer("catalogo.editar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        categorias = _categorias(cur)
        grupos = ocasioes_arvore(cur)
    return tela(request, "peca_form.html", usuario, categorias=categorias, grupos=grupos, nova=True,
                v={"chave": nova_chave(), "publico": "FEMININO", "faixa": "ADULTO", "no_site": "1",
                   "preco_no_site": "1"}, **_listas())


@router.post("/pecas/nova")
def nova(request: Request, usuario: dict = Depends(requer("catalogo.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_peca(form, nova=True)
    chave = chave_valida(form.get("chave"))
    if not erro and not chave:
        erro = "A página ficou aberta muito tempo. Comece de novo."
    pid = None
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                cur.execute(
                    """INSERT INTO produtos (loja_id, categoria_id, sku, nome, tamanho, cor, descricao,
                                            pode_alugar, pode_vender, preco_aluguel_centavos,
                                            preco_venda_centavos, chave_idempotencia, publico, faixa,
                                            comprimento, ocasioes, no_site, preco_no_site, ocasiao_principal)
                       VALUES (ctx_loja(), %s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id::text, sku""",
                    (d["categoria_id"], d["nome"], d["tamanho"], d["cor"], d["descricao"], d["pode_alugar"],
                     d["pode_vender"], d["preco_aluguel"], d["preco_venda"], chave, d["publico"], d["faixa"],
                     d["comprimento"], d["ocasioes"], d["no_site"], d["preco_no_site"], d["ocasiao_principal"]))
                p = cur.fetchone()
                pid = p["id"]
                auditar(cur, request, "peca_cadastrada", usuario["id"], "produto", pid, {"sku": p["sku"]})
        except psycopg.Error as e:
            if e_chave_repetida(e):
                with db.transacao(usuario_id=usuario["id"]) as cur:
                    cur.execute("SELECT id::text FROM produtos WHERE chave_idempotencia = %s", (chave,))
                    pid = cur.fetchone()["id"]
            else:
                erro = mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            categorias = _categorias(cur)
            grupos = ocasioes_arvore(cur)
        return tela(request, "peca_form.html", usuario, categorias=categorias, grupos=grupos, v=form, erro=erro,
                    nova=True, status=400, **_listas())
    return RedirectResponse(f"/pecas/{pid}?nova=1", status_code=303)


def _listas() -> dict:
    return {"PUBLICO": PUBLICO, "FAIXA": FAIXA, "COMPRIMENTO": COMPRIMENTO, "TAMANHOS": TAMANHOS,
            "TAMANHOS_NUM": TAMANHOS_NUM}


def _peca(cur, pid: UUID):
    cur.execute("""SELECT ps.*, ps.id::text AS id, c.nome AS categoria FROM produtos_situacao ps
                     JOIN categorias c ON c.id = ps.categoria_id WHERE ps.id = %s""", (pid,))
    return cur.fetchone()


@router.get("/pecas/{pid}")
def detalhe(request: Request, pid: UUID, nova: int = 0, usuario: dict = Depends(requer("catalogo.ver"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = _peca(cur, pid)
        if not p:
            return tela(request, "erro.html", usuario, status=404, titulo="Peça não encontrada")
        cur.execute(f"""
            SELECT pe.id::text, pe.tipo, pe.status, pe.data_retirada, pe.data_devolucao, cl.nome AS cliente
              FROM pedidos pe LEFT JOIN clientes cl ON cl.id = pe.cliente_id
             WHERE pe.produto_id = %s AND pe.status NOT IN {ATIVOS}
             ORDER BY pe.data_retirada NULLS LAST""", (pid,))
        reservas = cur.fetchall()
        cur.execute("SELECT id::text, arquivo, ordem FROM produto_fotos WHERE produto_id = %s AND ativa "
                    "ORDER BY ordem, criado_em", (pid,))
        lista_fotos = cur.fetchall()
        cur.execute("""SELECT string_agg(coalesce(pai.nome || ' › ', '') || o.nome, ', ' ORDER BY pai.ordem, o.ordem)
                         AS nomes FROM ocasioes o LEFT JOIN ocasioes pai ON pai.id = o.pai_id
                        WHERE o.id = ANY(%s)""", (p["ocasioes"],))
        nomes_ocasioes = cur.fetchone()["nomes"]
    return tela(request, "peca_detalhe.html", usuario, p=p, reservas=reservas, nova=nova, SITUACAO=SITUACAO,
                fotos=lista_fotos, nomes_ocasioes=nomes_ocasioes, max_fotos=fotos.MAX_FOTOS,
                erro_foto=request.query_params.get("erro_foto", ""), fotos_ok=request.query_params.get("fotos", ""),
                **_listas())


@router.get("/pecas/{pid}/editar")
def editar_form(request: Request, pid: UUID, usuario: dict = Depends(requer("catalogo.editar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = _peca(cur, pid)
    if not p:
        return RedirectResponse("/pecas", status_code=303)
    uso = "ambos" if p["pode_alugar"] and p["pode_vender"] else ("alugar" if p["pode_alugar"] else "vender")
    v = {"nome": p["nome"], **tamanhos_marcados(p["tamanho"]), "cor": p["cor"] or "", "descricao": p["descricao"] or "",
         "ocasiao_principal": str(p.get("ocasiao_principal") or ""),
         "uso": uso, "preco_aluguel": reais_para_campo(p["preco_aluguel_centavos"]),
         "preco_venda": reais_para_campo(p["preco_venda_centavos"]),
         "publico": p["publico"], "faixa": p["faixa"], "comprimento": p["comprimento"] or "",
         "no_site": "1" if p["no_site"] else "", "preco_no_site": "1" if p["preco_no_site"] else ""}
    v.update({f"ocasiao_{o}": "1" for o in p["ocasioes"]})
    with db.transacao(usuario_id=usuario["id"]) as cur:
        grupos = ocasioes_arvore(cur)
        cur.execute("SELECT ocasiao_principal FROM produtos WHERE id = %s", (str(pid),))
        v["ocasiao_principal"] = str((cur.fetchone() or {}).get("ocasiao_principal") or "")
    return tela(request, "peca_form.html", usuario, p=p, v=v, nova=False, grupos=grupos, **_listas())


@router.post("/pecas/{pid}/editar")
def editar(request: Request, pid: UUID, usuario: dict = Depends(requer("catalogo.editar")),
           form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_peca(form, nova=False)
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                cur.execute(
                    """UPDATE produtos SET nome=%s, tamanho=%s, cor=%s, descricao=%s, pode_alugar=%s,
                              pode_vender=%s, preco_aluguel_centavos=%s, preco_venda_centavos=%s,
                              publico=%s, faixa=%s, comprimento=%s, ocasioes=%s, no_site=%s, preco_no_site=%s,
                              ocasiao_principal=%s, atualizado_em=now()
                        WHERE id = %s AND status NOT IN ('VENDIDO','ARQUIVADO')""",
                    (d["nome"], d["tamanho"], d["cor"], d["descricao"], d["pode_alugar"], d["pode_vender"],
                     d["preco_aluguel"], d["preco_venda"], d["publico"], d["faixa"], d["comprimento"],
                     d["ocasioes"], d["no_site"], d["preco_no_site"], d["ocasiao_principal"], pid))
                if cur.rowcount == 0:
                    erro = "Peça vendida ou arquivada não pode ser editada."
                else:
                    auditar(cur, request, "peca_editada", usuario["id"], "produto", str(pid))
        except psycopg.Error as e:
            erro = mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            p = _peca(cur, pid)
            grupos = ocasioes_arvore(cur)
        return tela(request, "peca_form.html", usuario, p=p, v=form, erro=erro, nova=False, status=400,
                    grupos=grupos, **_listas())
    return RedirectResponse(f"/pecas/{pid}", status_code=303)


@router.post("/pecas/{pid}/arquivar")
def arquivar(request: Request, pid: UUID, usuario: dict = Depends(requer("catalogo.arquivar")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    if form.get("confirmar") != "1":
        return RedirectResponse(f"/pecas/{pid}", status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"SELECT count(*) AS n FROM pedidos WHERE produto_id = %s AND status NOT IN {ATIVOS}", (pid,))
        if cur.fetchone()["n"]:
            p = _peca(cur, pid)
            return tela(request, "erro.html", usuario, status=400, titulo="Esta peça tem pedido em andamento",
                        mensagem=f"Termine ou cancele os pedidos de {p['sku']} antes de arquivar.")
        cur.execute("UPDATE produtos SET status = 'ARQUIVADO' WHERE id = %s AND status <> 'VENDIDO'", (pid,))
        auditar(cur, request, "peca_arquivada", usuario["id"], "produto", str(pid))
    return RedirectResponse("/pecas", status_code=303)



# --------------------------------------------------------------------------- #
# Fotos
# --------------------------------------------------------------------------- #
def _gravar_fotos(usuario, request, pid, arquivos: list[bytes]) -> tuple[int, str]:
    """Roda fora do laço do servidor (processar foto gasta CPU). Devolve (quantas, erro)."""
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT count(*) AS n FROM produto_fotos WHERE produto_id = %s AND ativa", (pid,))
        ja = cur.fetchone()["n"]
        cur.execute("SELECT 1 FROM produtos WHERE id = %s AND status NOT IN ('VENDIDO','ARQUIVADO')", (pid,))
        if not cur.fetchone():
            return 0, "Peça vendida ou arquivada não recebe fotos."
    if not arquivos:
        return 0, "Escolha pelo menos uma foto."
    if ja + len(arquivos) > fotos.MAX_FOTOS:
        return 0, f"Cada peça pode ter até {fotos.MAX_FOTOS} fotos. Tire alguma antes de colocar outras."
    feitas, erro = [], ""
    for dados in arquivos:
        try:
            feitas.append(fotos.processar(dados))
        except fotos.FotoInvalida as e:
            erro = str(e)
    if feitas:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            for i, (nome, largura, altura) in enumerate(feitas):
                cur.execute("""INSERT INTO produto_fotos (loja_id, produto_id, arquivo, ordem, largura, altura, enviada_por)
                               VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s)""",
                            (pid, nome, ja + i, largura, altura, usuario["id"]))
            auditar(cur, request, "fotos_enviadas", usuario["id"], "produto", str(pid), {"quantas": len(feitas)})
    return len(feitas), erro


@router.post("/pecas/{pid}/fotos")
async def enviar_fotos(request: Request, pid: UUID, usuario: dict = Depends(requer("catalogo.editar"))):
    form = await request.form(max_files=fotos.MAX_FOTOS + 1, max_fields=10)
    checar_csrf(request, str(form.get("csrf") or ""))
    arquivos = []
    for f in form.getlist("fotos"):
        if hasattr(f, "read"):
            dados = await f.read(fotos.MAX_BYTES + 1)
            if dados:
                arquivos.append(dados)
    quantas, erro = await run_in_threadpool(_gravar_fotos, usuario, request, pid, arquivos)
    destino = f"/pecas/{pid}?fotos={quantas}"
    if erro:
        from urllib.parse import quote
        destino += f"&erro_foto={quote(erro)}"
    return RedirectResponse(destino + "#fotos", status_code=303)


@router.post("/pecas/{pid}/fotos/{fid}/{acao}")
def mexer_foto(request: Request, pid: UUID, fid: UUID, acao: str, usuario: dict = Depends(requer("catalogo.editar")),
               form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    if acao not in ("capa", "tirar"):
        return RedirectResponse(f"/pecas/{pid}", status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        if acao == "tirar":
            cur.execute("UPDATE produto_fotos SET ativa = false WHERE id = %s AND produto_id = %s", (fid, pid))
        else:
            # A escolhida vira a primeira; as outras mantêm a ordem entre si
            cur.execute("""UPDATE produto_fotos f SET ordem = x.nova
                             FROM (SELECT id, row_number() OVER (ORDER BY (id = %s) DESC, ordem, criado_em) - 1 AS nova
                                     FROM produto_fotos WHERE produto_id = %s AND ativa) x
                            WHERE f.id = x.id""", (fid, pid))
        auditar(cur, request, "foto_" + acao, usuario["id"], "produto", str(pid), {"foto": str(fid)})
    return RedirectResponse(f"/pecas/{pid}#fotos", status_code=303)


@router.get("/fotos/{arquivo}-{largura}.jpg")
def ver_foto(request: Request, arquivo: str, largura: int):
    """Foto de peça (ou banner) do site: qualquer um vê. Fora do site: só quem entrou no sistema."""
    if not re.fullmatch(r"[0-9a-f]{32}", arquivo) or largura not in fotos.TAMANHOS:
        return Response(status_code=404)
    logado = bool(request.session.get("usuario_id"))
    with db.transacao() as cur:
        cur.execute("""SELECT pr.no_site AND pr.status NOT IN ('ARQUIVADO') AND f.ativa AND l.site_ativo AS publica
                         FROM produto_fotos f JOIN produtos pr ON pr.id = f.produto_id JOIN lojas l ON l.id = f.loja_id
                        WHERE f.arquivo = %s
                       UNION ALL
                       SELECT b.ativo AND l.site_ativo FROM banners b JOIN lojas l ON l.id = b.loja_id
                        WHERE b.imagem = %s
                       LIMIT 1""", (arquivo, arquivo))
        linha = cur.fetchone()
    if not linha or not (linha["publica"] or logado):
        return Response(status_code=404)
    caminho = fotos.caminho(arquivo, largura)
    if not caminho.exists():
        return Response(status_code=404)
    return FileResponse(caminho, media_type="image/jpeg", headers={
        "Cache-Control": "public, max-age=2592000, immutable" if linha["publica"] else "private, max-age=3600"})
