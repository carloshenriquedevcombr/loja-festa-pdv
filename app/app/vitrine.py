"""Partes do site que a loja ajusta: banners de promoção, os 4 destaques, avaliações e nota do Google."""
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
import uuid

from . import config

ICONES = {"cabide": "Cabide", "caminhao": "Entrega", "pergunta": "Pergunta", "fita": "Fita métrica",
          "estrela": "Estrela", "calendario": "Calendário", "tesoura": "Tesoura", "presente": "Presente",
          "coracao": "Coração", "cartao": "Cartão"}
TEMAS = {"escuro": "Escuro (texto claro)", "claro": "Claro (texto escuro)", "dourado": "Dourado", "rosa": "Rosa"}

DESTAQUES_PADRAO = [
    {"posicao": 1, "icone": "cabide", "titulo": "Como funciona", "texto": "Escolha, prove e brilhe: veja o passo a passo.",
     "link": "/site/pagina/como-funciona"},
    {"posicao": 2, "icone": "caminhao", "titulo": "Entrega e devolução", "texto": "Retire na loja ou receba em casa.",
     "link": "/site/pagina/entrega-e-devolucao"},
    {"posicao": 3, "icone": "pergunta", "titulo": "Perguntas frequentes", "texto": "Tamanhos, ajustes, pagamento e prazos.",
     "link": "/site/pagina/perguntas-frequentes"},
    {"posicao": 4, "icone": "fita", "titulo": "Como provar", "texto": "Agende sua prova com hora marcada.",
     "link": "/site/pagina/como-provar"},
]


def link_do_site(texto: str | None) -> str | None:
    """Link de banner/destaque: só páginas do próprio site (nada de mandar a cliente para fora)."""
    t = (texto or "").strip()
    if t and not t.startswith("/"):
        t = "/site/" + t
    return t if re.fullmatch(r"/site(/[A-Za-z0-9/_?=&.%-]*)?", t) and ".." not in t else None


def banners_ativos(cur) -> list[dict]:
    cur.execute("""SELECT id::text, titulo, texto, botao, link, imagem, tema FROM banners
                    WHERE ativo AND (inicio IS NULL OR inicio <= (now() AT TIME ZONE 'America/Sao_Paulo')::date)
                      AND (fim IS NULL OR fim >= (now() AT TIME ZONE 'America/Sao_Paulo')::date)
                    ORDER BY ordem, criado_em DESC LIMIT 6""")
    return cur.fetchall()


def destaques(cur) -> list[dict]:
    cur.execute("SELECT posicao, icone, titulo, texto, link FROM destaques ORDER BY posicao")
    salvos = {d["posicao"]: d for d in cur.fetchall()}
    return [salvos.get(d["posicao"], d) for d in DESTAQUES_PADRAO]


def resumo_avaliacoes(cur) -> dict:
    cur.execute("""SELECT google_nota, google_total, google_link, avaliacoes_no_site FROM lojas WHERE id = ctx_loja()""")
    loja = cur.fetchone() or {}
    cur.execute("""SELECT nome_exibicao, nota, texto, resposta,
                          to_char(criado_em AT TIME ZONE 'America/Sao_Paulo', 'MM/YYYY') AS quando
                     FROM avaliacoes WHERE status = 'APROVADA' ORDER BY criado_em DESC LIMIT 12""")
    proprias = cur.fetchall()
    cur.execute("SELECT count(*) AS n, round(avg(nota), 1) AS media FROM avaliacoes WHERE status = 'APROVADA'")
    tot = cur.fetchone()
    cur.execute("SELECT autor, autor_link, nota, texto, quando FROM google_avaliacoes ORDER BY id LIMIT 5")
    google = cur.fetchall()
    return {"ligado": loja.get("avaliacoes_no_site", True), "google_nota": loja.get("google_nota"),
            "google_total": loja.get("google_total"), "google_link": loja.get("google_link"),
            "proprias": proprias, "total": tot["n"], "media": tot["media"], "google": google}


# --------------------------------------------------------------------------- #
# Link secreto "avalie seu pedido": id do pedido + assinatura (ninguém inventa um link válido)
# --------------------------------------------------------------------------- #
def _assinar(pid: bytes) -> bytes:
    return hmac.new(config.SESSION_SECRET.encode(), b"avaliar:" + pid, hashlib.sha256).digest()[:12]


def token_avaliar(pedido_id: str) -> str:
    b = uuid.UUID(str(pedido_id)).bytes
    return base64.urlsafe_b64encode(b + _assinar(b)).decode().rstrip("=")


def ler_token(token: str) -> str | None:
    if not token or len(token) > 60:
        return None
    try:
        bruto = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if len(bruto) != 28 or not hmac.compare_digest(bruto[16:], _assinar(bruto[:16])):
        return None
    return str(uuid.UUID(bytes=bruto[:16]))


def nome_publico(nome: str | None) -> str:
    """'Maria Aparecida Souza' → 'Maria S.' (a cliente pode trocar ao avaliar)."""
    partes = (nome or "").split()
    if not partes:
        return "Cliente"
    return (partes[0] + (f" {partes[-1][0]}." if len(partes) > 1 else ""))[:40]


# --------------------------------------------------------------------------- #
# Google Places (New): nota, total e até 5 comentários — só com GOOGLE_PLACES_KEY e o Place ID da loja
# --------------------------------------------------------------------------- #
class ErroGoogle(Exception):
    pass


def buscar_google(place_id: str) -> dict:
    pedido = urllib.request.Request(
        f"{config.GOOGLE_PLACES_API}/places/{urllib.request.quote(place_id, safe='')}?languageCode=pt-BR",
        headers={"X-Goog-Api-Key": config.GOOGLE_PLACES_KEY, "User-Agent": "loja-festa/1.0",
                 "X-Goog-FieldMask": "rating,userRatingCount,googleMapsUri,reviews"})
    try:
        with urllib.request.urlopen(pedido, timeout=15) as r:
            dados = json.loads(r.read(2_000_000).decode() or "{}")
    except urllib.error.HTTPError as e:
        raise ErroGoogle(f"Google respondeu {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ErroGoogle("sem conexão com o Google") from e
    if not isinstance(dados, dict):
        raise ErroGoogle("resposta estranha")
    comentarios = []
    for r in (dados.get("reviews") or [])[:5]:
        autor = (r.get("authorAttribution") or {})
        link = str(autor.get("uri") or "")
        texto = ((r.get("text") or {}).get("text") or (r.get("originalText") or {}).get("text") or "")[:1000]
        try:
            nota = int(r.get("rating") or 0)
        except (TypeError, ValueError):
            nota = 0
        comentarios.append({"autor": str(autor.get("displayName") or "Cliente do Google")[:80],
                            "autor_link": link if link.startswith("https://www.google.") or link.startswith("https://google.") else None,
                            "nota": nota if 1 <= nota <= 5 else None, "texto": texto,
                            "quando": str(r.get("relativePublishTimeDescription") or "")[:40]})
    try:
        nota = round(float(dados.get("rating")), 1) if dados.get("rating") is not None else None
        total = int(dados.get("userRatingCount") or 0)
    except (TypeError, ValueError) as e:
        raise ErroGoogle("nota inválida") from e
    link = str(dados.get("googleMapsUri") or "")
    return {"nota": nota if nota and 1 <= nota <= 5 else None, "total": total, "comentarios": comentarios,
            "link": link if link.startswith("https://maps.google.") or link.startswith("https://www.google.") else None}


def atualizar_google(cur) -> bool:
    """Chamado pelo trabalhador uma vez por dia. Sem chave ou sem Place ID: não faz nada."""
    if not config.GOOGLE_PLACES_KEY:
        return False
    cur.execute("""SELECT google_place_id FROM lojas WHERE id = ctx_loja() AND google_place_id IS NOT NULL
                      AND (google_atualizado IS NULL OR google_atualizado < now() - interval '20 hours')""")
    linha = cur.fetchone()
    if not linha:
        return False
    try:
        g = buscar_google(linha["google_place_id"])
    except ErroGoogle:
        cur.execute("UPDATE lojas SET google_atualizado = now() - interval '18 hours' WHERE id = ctx_loja()")  # tenta de novo em 2 h
        return False
    cur.execute("""UPDATE lojas SET google_nota = COALESCE(%s, google_nota), google_total = %s,
                          google_link = COALESCE(google_link, %s), google_atualizado = now() WHERE id = ctx_loja()""",
                (g["nota"], g["total"], g["link"]))
    cur.execute("DELETE FROM google_avaliacoes")
    for c in g["comentarios"]:
        cur.execute("""INSERT INTO google_avaliacoes (loja_id, autor, autor_link, nota, texto, quando)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s)""",
                    (c["autor"], c["autor_link"], c["nota"], c["texto"], c["quando"]))
    return True
