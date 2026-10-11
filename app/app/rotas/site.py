"""Site público da loja (vitrine): coleção por ocasião, filtros, página da peça, pedido de prova.

Tudo aqui é aberto para qualquer pessoa na internet, então:
  * só aparecem peças marcadas "no site" e disponíveis (nada de dado de cliente, pedido ou caixa);
  * o pedido de prova tem limite por origem e campo-isca para robôs;
  * nenhum script: galeria e menu funcionam só com HTML e CSS.
"""
import hashlib
import re
from datetime import date, timedelta
from urllib.parse import quote, urlencode
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import config, contas, cripto, db, vitrine
from ..auth import ip
from ..pagamento import formulario
from ..web import TZ, brl, checar_csrf, tela, telefone
from .catalogo import COMPRIMENTO, FAIXA, PUBLICO, ocasioes_arvore

router = APIRouter()
POR_PAGINA = 24
ORDENS = {"novidades": "p.criado_em DESC", "menor": "preco_ref ASC NULLS LAST, p.criado_em DESC",
          "maior": "preco_ref DESC NULLS LAST, p.criado_em DESC"}
LIMITE_POR_HORA = 5

SQL_CARD = """
    SELECT p.id::text, p.slug, p.sku, p.nome, p.tamanho, p.cor, p.pode_alugar, p.pode_vender,
           p.preco_aluguel_centavos, p.preco_venda_centavos, p.preco_no_site, p.publico, p.faixa, p.comprimento,
           COALESCE(p.preco_aluguel_centavos, p.preco_venda_centavos) AS preco_ref,
           (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = p.id AND f.ativa
             ORDER BY f.ordem, f.criado_em LIMIT 1) AS capa,
           (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = p.id AND f.ativa
             ORDER BY f.ordem, f.criado_em OFFSET 1 LIMIT 1) AS capa2
      FROM produtos p
"""
# Na vitrine: marcada para o site, disponível e respeitando as regras da loja (Configurações > Regras da vitrine)
VISIVEL = """p.no_site AND p.status = 'DISPONIVEL' AND NOT EXISTS (
    SELECT 1 FROM pedidos pv JOIN lojas lv ON lv.id = pv.loja_id
     WHERE pv.produto_id = p.id AND pv.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
       AND ((pv.tipo = 'VENDA' AND lv.venda_ajuste_some)
         OR (pv.tipo = 'ALUGUEL' AND lv.alugada_no_site = 'ESCONDE')))"""


def _loja(cur) -> dict:
    cur.execute("""SELECT nome, endereco, telefone, whatsapp, instagram, facebook, tiktok, youtube, pinterest,
                          site_externo, horario, frase_site, site_ativo, parcelas_no_site, formas_site, bandeiras_site,
                          email_contato, sobre_curto, novidades_ativa, sacola_ativa, entrega_retirada, entrega_envio,
                          ajuste_site_centavos, prova_antecedencia_dias
                     FROM lojas WHERE id = ctx_loja()""")
    return cur.fetchone()


def link_whatsapp(loja: dict, texto: str) -> str | None:
    fone = loja.get("whatsapp") or re.sub(r"\D", "", loja.get("telefone") or "")
    if not fone or len(fone) not in (10, 11):
        return None
    return f"https://wa.me/55{fone}?text={quote(texto)}"


def _pagina(request, nome, cur, **ctx):
    loja = _loja(cur)
    if not loja["site_ativo"]:
        return tela(request, "site/fechado.html", loja=loja, status=503)
    grupos = ocasioes_arvore(cur)
    from .rodape import PAGINAS, unidades
    ctx.setdefault("destaques", vitrine.destaques(cur))
    ctx.setdefault("conta_site", contas.conta_atual(cur, request.session, request.headers.get("user-agent")))
    ctx.setdefault("sacola_qtd", len(request.session.get("sacola") or []))
    from .simbolos_rotas import mapa as mapa_simbolos
    ctx.setdefault("simbolos", mapa_simbolos(cur))
    ctx.setdefault("avaliacoes", vitrine.resumo_avaliacoes(cur))
    return tela(request, nome, loja=loja, grupos=grupos, whatsapp=link_whatsapp(loja, "Olá! Vim pelo site."),
                unidades=unidades(cur), PAGINAS=PAGINAS, **ctx)


def _preco(p) -> str:
    if not p["preco_no_site"]:
        return "Sob consulta"
    partes = []
    if p["pode_alugar"]:
        partes.append(f"Aluguel {brl(p['preco_aluguel_centavos'])}")
    if p["pode_vender"]:
        partes.append(f"Venda {brl(p['preco_venda_centavos'])}")
    return " · ".join(partes)


def _cards(linhas):
    for p in linhas:
        p["preco_texto"] = _preco(p)
    return linhas


# --------------------------------------------------------------------------- #
# Início
# --------------------------------------------------------------------------- #
def escolher_vitrines(ocasioes: list[dict], pecas: list[dict], limite: int = 8) -> list[dict]:
    """Uma foto por ocasião, cada peça numa ocasião só. Subcategoria usa as peças que têm ela como principal;
    a categoria mãe usa as peças dela ou das filhas que sobraram. Ocasião sem peça própria não aparece."""
    filhos: dict = {}
    for o in ocasioes:
        if o["pai_id"]:
            filhos.setdefault(o["pai_id"], set()).add(o["id"])
    usadas, foto = set(), {}
    for rodada in ("filhas", "maes"):
        for o in ocasioes:
            if (rodada == "filhas") != bool(o["pai_id"]):
                continue
            aceitas = {o["id"]} | (filhos.get(o["id"], set()) if rodada == "maes" else set())
            achada = next((x for x in pecas if x["id"] not in usadas and x["ocasiao_principal"] in aceitas), None)
            if achada:
                usadas.add(achada["id"])
                foto[o["id"]] = achada["foto"]
    return [{**o, "foto": foto[o["id"]]} for o in ocasioes if o["id"] in foto][:limite]


@router.get("/site")
def inicio(request: Request):
    with db.transacao() as cur:
        cur.execute(SQL_CARD + f" WHERE {VISIVEL} ORDER BY p.criado_em DESC LIMIT 8")
        novidades = _cards(cur.fetchall())
        # "Escolha pela ocasião": cada peça aparece em UMA ocasião só (a ocasião principal dela), sem repetir foto
        cur.execute("""SELECT o.id, o.nome, o.slug, o.pai_id, pai.slug AS pai_slug
                         FROM ocasioes o LEFT JOIN ocasioes pai ON pai.id = o.pai_id
                        WHERE o.ativa AND (pai.id IS NULL OR pai.ativa)
                        ORDER BY coalesce(pai.ordem, o.ordem), o.pai_id NULLS FIRST, o.ordem""")
        ocasioes = cur.fetchall()
        cur.execute(f"""SELECT p.id, p.ocasiao_principal,
                               (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = p.id AND f.ativa
                                 ORDER BY f.ordem LIMIT 1) AS foto
                          FROM produtos p WHERE {VISIVEL} AND p.ocasiao_principal IS NOT NULL
                         ORDER BY p.criado_em DESC LIMIT 400""")
        pecas = [x for x in cur.fetchall() if x["foto"]]
        vitrines = escolher_vitrines(ocasioes, pecas)
        destaque = next((p for p in novidades if p["capa"]), None)
        return _pagina(request, "site/inicio.html", cur, novidades=novidades, vitrines=vitrines, destaque=destaque,
                       banners=vitrine.banners_ativos(cur))


# --------------------------------------------------------------------------- #
# Coleção (ocasião + filtros)
# --------------------------------------------------------------------------- #
def _url(base: str, filtros: dict, **mudar) -> str:
    """Endereço da coleção trocando um filtro e mantendo os outros."""
    q = {**filtros, **mudar}
    q = {k: v for k, v in q.items()
         if v not in (None, "") and not (k == "ordem" and v == "novidades") and not (k == "pagina" and v == 1)}
    return base + ("?" + urlencode(q) if q else "")


def _colecao(request: Request, grupo: str | None = None, sub: str | None = None):
    qp = request.query_params
    filtros = {
        "publico": qp.get("publico") if qp.get("publico") in PUBLICO else "",
        "faixa": qp.get("faixa") if qp.get("faixa") in FAIXA else "",
        "comprimento": qp.get("comprimento") if qp.get("comprimento") in COMPRIMENTO else "",
        "uso": qp.get("uso") if qp.get("uso") in ("alugar", "comprar") else "",
        "tamanho": (qp.get("tamanho") or "")[:20],
        "cor": (qp.get("cor") or "")[:30],
        "data": qp.get("data") or "",
        "ordem": qp.get("ordem") if qp.get("ordem") in ORDENS else "novidades",
    }
    try:
        pagina = max(1, min(int(qp.get("pagina") or 1), 200))
    except ValueError:
        pagina = 1
    try:
        dia = date.fromisoformat(filtros["data"]) if filtros["data"] else None
    except ValueError:
        dia, filtros["data"] = None, ""
    base = request.url.path
    with db.transacao() as cur:
        titulo, ocasiao_ids, trilha = "Coleção", None, []
        if grupo:
            cur.execute("SELECT id, nome, slug FROM ocasioes WHERE slug = %s AND pai_id IS NULL AND ativa", (grupo,))
            g = cur.fetchone()
            if not g:
                return RedirectResponse("/site/colecao", status_code=303)
            trilha.append((g["nome"], f"/site/c/{g['slug']}"))
            titulo = g["nome"]
            if sub:
                cur.execute("SELECT id, nome FROM ocasioes WHERE slug = %s AND pai_id = %s AND ativa", (sub, g["id"]))
                s = cur.fetchone()
                if not s:
                    return RedirectResponse(f"/site/c/{grupo}", status_code=303)
                ocasiao_ids = [s["id"]]
                titulo = s["nome"]
                trilha.append((s["nome"], base))
            else:
                cur.execute("SELECT id FROM ocasioes WHERE (id = %s OR pai_id = %s) AND ativa", (g["id"], g["id"]))
                ocasiao_ids = [r["id"] for r in cur.fetchall()]
        elif filtros["publico"] == "MASCULINO":
            titulo = "Masculino"
        elif filtros["faixa"] == "INFANTIL":
            titulo = "Infantil"

        cond, params = [VISIVEL], {}
        if ocasiao_ids is not None:
            cond.append("p.ocasioes && %(ocasioes)s::smallint[]")
            params["ocasioes"] = ocasiao_ids
        for campo in ("publico", "faixa", "comprimento"):
            if filtros[campo]:
                cond.append(f"p.{campo} = %({campo})s")
                params[campo] = filtros[campo]
        if filtros["uso"] == "alugar":
            cond.append("p.pode_alugar")
        elif filtros["uso"] == "comprar":
            cond.append("p.pode_vender")
        if filtros["tamanho"]:
            cond.append("upper(%(tamanho)s) = ANY(string_to_array(upper(replace(p.tamanho, ' ', '')), ','))")
            params["tamanho"] = filtros["tamanho"]
        if filtros["cor"]:
            cond.append("f_unaccent(lower(p.cor)) LIKE f_unaccent(lower(%(cor)s))")
            params["cor"] = f"%{filtros['cor']}%"
        if dia:
            cond.append("""NOT EXISTS (SELECT 1 FROM pedidos pe WHERE pe.produto_id = p.id
                             AND pe.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
                             AND pe.periodo_bloqueado && daterange(%(dia)s::date - 1, %(dia)s::date + 1, '[]'))""")
            params["dia"] = dia
        onde = " AND ".join(cond)
        cur.execute(f"SELECT count(*) AS n FROM produtos p WHERE {onde}", params)
        total = cur.fetchone()["n"]
        cur.execute(SQL_CARD + f" WHERE {onde} ORDER BY {ORDENS[filtros['ordem']]} LIMIT {POR_PAGINA} OFFSET %(off)s",
                    {**params, "off": (pagina - 1) * POR_PAGINA})
        pecas = _cards(cur.fetchall())
        # Opções dos filtros: só o que existe nesta ocasião
        base_cond = " AND ".join([VISIVEL] + (["p.ocasioes && %(ocasioes)s::smallint[]"] if ocasiao_ids is not None else []))
        base_params = {"ocasioes": ocasiao_ids} if ocasiao_ids is not None else {}
        cur.execute(f"""SELECT DISTINCT t FROM produtos p,
                               unnest(string_to_array(upper(replace(p.tamanho, ' ', '')), ',')) AS t
                         WHERE {base_cond} AND p.tamanho IS NOT NULL AND t <> ''
                         ORDER BY 1""", base_params)
        tamanhos = [r["t"] for r in cur.fetchall()][:30]
        cur.execute(f"""SELECT initcap(lower(p.cor)) AS c, count(*) FROM produtos p
                         WHERE {base_cond} AND p.cor IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 16""", base_params)
        cores = [r["c"] for r in cur.fetchall()]
        subs = []
        if grupo:
            cur.execute("""SELECT o.nome, o.slug FROM ocasioes o JOIN ocasioes pai ON pai.id = o.pai_id
                            WHERE pai.slug = %s AND o.ativa ORDER BY o.ordem""", (grupo,))
            subs = cur.fetchall()
        return _pagina(request, "site/colecao.html", cur, pecas=pecas, total=total, titulo=titulo, trilha=trilha,
                       filtros=filtros, tamanhos=tamanhos, cores=cores, subs=subs, grupo=grupo, sub=sub,
                       pagina=pagina, paginas=max(1, -(-total // POR_PAGINA)), dia=dia,
                       url=lambda **m: _url(base, filtros, **m), PUBLICO=PUBLICO, FAIXA=FAIXA,
                       COMPRIMENTO=COMPRIMENTO, hoje=date.today())


@router.get("/site/colecao")
def colecao(request: Request):
    return _colecao(request)


@router.get("/site/c/{grupo}")
def colecao_grupo(request: Request, grupo: str):
    return _colecao(request, grupo[:60])


@router.get("/site/c/{grupo}/{sub}")
def colecao_sub(request: Request, grupo: str, sub: str):
    return _colecao(request, grupo[:60], sub[:60])


# --------------------------------------------------------------------------- #
# Peça
# --------------------------------------------------------------------------- #
def _peca_publica(cur, slug: str):
    cur.execute(SQL_CARD.replace("SELECT p.id::text,", "SELECT p.id::text, p.descricao, p.ocasioes,")
                + f" WHERE {VISIVEL} AND p.slug = %s", (slug[:160],))
    return cur.fetchone()


def disponivel_em(cur, produto_id: str, dia: date) -> bool:
    cur.execute("""SELECT NOT EXISTS (SELECT 1 FROM pedidos pe WHERE pe.produto_id = %s
                     AND pe.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
                     AND pe.periodo_bloqueado && daterange(%s::date - 1, %s::date + 1, '[]')) AS livre""",
                (produto_id, dia, dia))
    return cur.fetchone()["livre"]


@router.get("/site/p/{slug}")
def peca(request: Request, slug: str, data: str = "", sacola_erro: str = ""):
    with db.transacao() as cur:
        cur.execute("SELECT liberar_pedidos_vencidos()")
        p = _peca_publica(cur, slug)
        if not p:
            return _pagina(request, "site/nao_encontrada.html", cur)
        _cards([p])
        cur.execute("""SELECT arquivo, largura, altura FROM produto_fotos WHERE produto_id = %s AND ativa
                        ORDER BY ordem, criado_em""", (p["id"],))
        galeria = cur.fetchall()
        cur.execute("""SELECT o.nome, o.slug, pai.slug AS pai_slug, pai.nome AS pai_nome
                         FROM ocasioes o LEFT JOIN ocasioes pai ON pai.id = o.pai_id
                        WHERE o.id = ANY(%s) AND o.ativa ORDER BY pai.ordem, o.ordem""", (p["ocasioes"],))
        ocasioes = cur.fetchall()
        consulta = None
        parcelas = {}
        loja_regras = _loja(cur)
        if loja_regras["parcelas_no_site"] and p["preco_no_site"]:
            from .. import pagamento as _pg
            cfg = _pg.config_vigente(cur)
            juros = _pg.juros_vigentes(cur, cfg["id"])
            if p["pode_alugar"] and p["preco_aluguel_centavos"]:
                parcelas["Aluguel"] = _pg.simular(cfg, juros, p["preco_aluguel_centavos"], "ALUGUEL")
            if p["pode_vender"] and p["preco_venda_centavos"]:
                parcelas["Compra"] = _pg.simular(cfg, juros, p["preco_venda_centavos"], "VENDA")
        cur.execute("SELECT peca_presa_ate_devolver(%s) AS presa", (p["id"],))
        alugada_agora = cur.fetchone()["presa"]
        if data:
            try:
                dia = date.fromisoformat(data)
                if dia < date.today():
                    consulta = {"erro": "Escolha uma data de hoje em diante."}
                elif dia > date.today() + timedelta(days=730):
                    consulta = {"erro": "Escolha uma data nos próximos 2 anos."}
                else:
                    consulta = {"dia": dia, "livre": disponivel_em(cur, p["id"], dia)}
            except ValueError:
                consulta = {"erro": "Data inválida."}
        cur.execute(SQL_CARD + f""" WHERE {VISIVEL} AND p.id <> %s AND p.ocasioes && %s::smallint[]
                                     ORDER BY p.criado_em DESC LIMIT 4""", (p["id"], p["ocasioes"] or [0]))
        parecidas = _cards(cur.fetchall())
        loja = _loja(cur)
        texto = f"Olá! Vi no site a peça {p['nome']} ({p['sku']}) e quero saber mais."
        if consulta and consulta.get("dia"):
            texto += f" É para o dia {consulta['dia']:%d/%m/%Y}."
        return _pagina(request, "site/peca.html", cur, p=p, galeria=galeria, ocasioes=ocasioes, consulta=consulta,
                       parecidas=parecidas, zap_peca=link_whatsapp(loja, texto), data=data, hoje=date.today(),
                       alugada_agora=alugada_agora, parcelas=parcelas, sacola_erro=sacola_erro[:200],
                       PUBLICO=PUBLICO, FAIXA=FAIXA, COMPRIMENTO=COMPRIMENTO)


# --------------------------------------------------------------------------- #
# Pedido de prova
# --------------------------------------------------------------------------- #
def _origem(request) -> str:
    return hashlib.sha256(f"{ip(request) or '-'}|{config.SESSION_SECRET}".encode()).hexdigest()[:32]


def _form_prova(request, cur, v: dict, erro: str | None = None, status: int = 200):
    p = _peca_publica(cur, v.get("peca") or "") if v.get("peca") else None
    if p:
        _cards([p])
    return _pagina(request, "site/prova.html", cur, v=v, p=p, erro=erro, hoje=date.today(), status=status)


@router.get("/site/prova")
def prova_form(request: Request, peca: str = "", data: str = ""):
    with db.transacao() as cur:
        return _form_prova(request, cur, {"peca": peca[:160], "data_evento": data[:10], "periodo": "TARDE"})


@router.post("/site/prova")
def prova(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    v = {k: (form.get(k) or "").strip() for k in ("peca", "nome", "telefone", "data_desejada", "periodo",
                                                   "data_evento", "mensagem", "lgpd", "site")}
    if v["site"]:                                       # campo-isca: gente não vê, robô preenche
        return RedirectResponse("/site/prova/obrigado", status_code=303)
    fone = re.sub(r"\D", "", v["telefone"])
    if fone.startswith("55") and len(fone) in (12, 13):
        fone = fone[2:]
    erro = None
    try:
        desejada = date.fromisoformat(v["data_desejada"]) if v["data_desejada"] else None
        evento = date.fromisoformat(v["data_evento"]) if v["data_evento"] else None
    except ValueError:
        desejada = evento = None
        erro = "Confira as datas."
    if not erro:
        if len(v["nome"]) < 2:
            erro = "Escreva seu nome."
        elif len(fone) not in (10, 11):
            erro = "Escreva seu WhatsApp com DDD, por exemplo (19) 99999-9999."
        elif not desejada or desejada < date.today() or desejada > date.today() + timedelta(days=365):
            erro = "Escolha o dia em que gostaria de vir provar (de hoje em diante)."
        elif v["periodo"] not in ("MANHA", "TARDE"):
            erro = "Escolha manhã ou tarde."
        elif evento and evento < date.today():
            erro = "A data do evento já passou."
        elif v["lgpd"] != "1":
            erro = "Marque que concorda que a loja entre em contato com você."
    with db.transacao() as cur:
        if not erro:
            cur.execute("SELECT prova_antecedencia_dias AS ant, dias_abertos FROM lojas WHERE id = ctx_loja()")
            regra = cur.fetchone()
            if desejada < date.today() + timedelta(days=regra["ant"]):
                erro = (f"Para provar, escolha a partir de {(date.today() + timedelta(days=regra['ant'])):%d/%m} "
                        "(precisamos separar a peça). Com pressa? Chame no WhatsApp.")
            elif desejada.isoweekday() not in regra["dias_abertos"]:
                erro = "A loja não abre nesse dia da semana. Escolha outro dia."
        if erro:
            return _form_prova(request, cur, v, erro, 400)
        origem = _origem(request)
        cur.execute("""SELECT count(*) AS n FROM solicitacoes_prova
                        WHERE (origem_hash = %s OR telefone_idx = %s) AND criado_em > now() - interval '1 hour'""",
                    (origem, cripto.indice_telefone(fone)))
        if cur.fetchone()["n"] >= LIMITE_POR_HORA:
            return _form_prova(request, cur, v, "Recebemos vários pedidos seus agora há pouco. "
                                                "Aguarde nosso contato ou chame no WhatsApp.", 429)
        p = _peca_publica(cur, v["peca"]) if v["peca"] else None
        try:
            cur.execute("""INSERT INTO solicitacoes_prova (loja_id, produto_id, nome, telefone_c, telefone_idx, data_evento,
                                                           data_desejada, periodo, mensagem_c, consentimento_em, origem_hash)
                           VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)""",
                        (p["id"] if p else None, v["nome"][:120], cripto.cifrar(fone, "solicitacoes.telefone"),
                         cripto.indice_telefone(fone), evento, desejada, v["periodo"],
                         cripto.cifrar(v["mensagem"][:500] or None, "solicitacoes.mensagem"), origem))
            from .. import eventos
            eventos.prova_pedida_no_site(cur, v["nome"][:120], p["nome"] if p else None, desejada, v["periodo"])
        except psycopg.Error:
            return _form_prova(request, cur, v, "Não foi possível enviar agora. Tente de novo ou chame no WhatsApp.", 400)
    return RedirectResponse("/site/prova/obrigado", status_code=303)


@router.get("/site/prova/obrigado")
def obrigado(request: Request):
    with db.transacao() as cur:
        return _pagina(request, "site/obrigado.html", cur)
