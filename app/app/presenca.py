"""Quem está online agora — funcionárias no sistema, clientes com conta no site e visitantes do site.

O navegador manda um "ainda estou aqui" a cada 45 segundos enquanto a página está aberta (static/presenca.js).
Online = sinal nos últimos 3 minutos. Guardamos só a área (ex.: "/pedidos", "/site/sacola"): nada de números,
nomes de peça ou dados. Visitante sem conta vira só um número (hash do IP+navegador, na memória, some sozinho).
"""
import hashlib
import re
import threading
import time

from psycopg import sql

JANELA_S = 180
_JANELA = sql.SQL("make_interval(secs => {})").format(sql.Literal(JANELA_S))   # = interval 'JANELA_S seconds'
_visitantes: dict[str, tuple[float, str]] = {}
_trava = threading.Lock()

SITE = [("/site/sacola", "Na sacola"), ("/site/conta", "Minha conta"), ("/site/p", "Vendo uma peça"),
        ("/site/colecao", "Na coleção"), ("/site/c", "Na coleção"), ("/site/prova", "Agendando prova"),
        ("/site/pagamento", "Pagando"), ("/site/contrato", "Verificando pedido"), ("/site/pagina", "Lendo as páginas"),
        ("/site/avaliar", "Avaliando"), ("/site", "Início do site")]


def area(caminho: str | None) -> str | None:
    """'/pedidos/3f2a…/receber' → '/pedidos'; '/site/p/vestido-azul-ves-0001' → '/site/p'. Só letras, sem ids."""
    partes = [p for p in (caminho or "").lower().split("/") if p][:3]
    limpo = []
    for p in partes:
        if re.search(r"\d", p) or not re.fullmatch(r"[a-z_-]{1,30}", p):
            break
        limpo.append(p)
    if limpo[:1] == ["site"]:
        limpo = limpo[:2]
    texto = "/" + "/".join(limpo)
    return texto if re.fullmatch(r"/[a-z0-9/_-]{0,79}", texto) else "/"


def rotulo_site(pagina: str | None) -> str:
    for prefixo, nome in SITE:
        if (pagina or "").startswith(prefixo):
            return nome
    return "No site"


def rotulo_sistema(pagina: str | None) -> str:
    from .botoes import BOTOES, base
    melhor, nome = "", "No sistema"
    for _, rotulo, _, url, _ in BOTOES:
        u = base(url)
        if u != "/" and (pagina or "").startswith(u) and len(u) > len(melhor):
            melhor, nome = u, rotulo
    return "Tela inicial" if pagina in ("/", None) else nome


def visitante(ip: str | None, ua: str | None, pagina: str) -> None:
    chave = hashlib.sha256(f"{ip}|{(ua or '')[:200]}".encode()).hexdigest()[:24]
    agora = time.time()
    with _trava:
        _visitantes[chave] = (agora, pagina)
        if len(_visitantes) > 5000:                       # nunca cresce sem limite
            for k in [k for k, (t, _) in _visitantes.items() if agora - t > JANELA_S]:
                _visitantes.pop(k, None)


def visitantes_agora() -> list[str]:
    agora = time.time()
    with _trava:
        for k in [k for k, (t, _) in _visitantes.items() if agora - t > JANELA_S]:
            _visitantes.pop(k, None)
        return [p for _, p in _visitantes.values()]


def online(cur) -> dict:
    cur.execute(sql.SQL("""SELECT DISTINCT ON (s.usuario_id) u.nome, s.pagina, s.descricao,
                           to_char(s.criada_em AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS desde,
                           COALESCE((SELECT string_agg(p.nome, ', ' ORDER BY p.id) FROM usuario_papeis up
                                      JOIN papeis p ON p.id = up.papel_id
                                     WHERE up.usuario_id = u.id AND (up.valido_ate IS NULL OR up.valido_ate > now())), '') AS cargos
                      FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id
                     WHERE s.encerrada_em IS NULL AND s.expira_em > now() AND NOT u.sistema
                       AND s.visto_em > now() - {janela}
                     ORDER BY s.usuario_id, s.visto_em DESC""").format(janela=_JANELA))
    equipe = sorted(cur.fetchall(), key=lambda r: r["nome"])
    for f in equipe:
        f["onde"] = rotulo_sistema(f["pagina"])
        f["cargos"] = f["cargos"].replace("Diretoria: ", "")
    cur.execute(sql.SQL("""SELECT DISTINCT ON (sc.conta_id) cl.nome, sc.pagina,
                           to_char(sc.criada_em AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS desde
                      FROM sessoes_clientes sc JOIN contas_clientes c ON c.id = sc.conta_id
                      JOIN clientes cl ON cl.id = c.cliente_id
                     WHERE sc.encerrada_em IS NULL AND sc.expira_em > now()
                       AND sc.visto_em > now() - {janela}
                     ORDER BY sc.conta_id, sc.visto_em DESC""").format(janela=_JANELA))
    clientes = sorted(cur.fetchall(), key=lambda r: r["nome"])
    for c in clientes:
        c["onde"] = rotulo_site(c["pagina"])
    anonimos = visitantes_agora()
    return {"equipe": equipe, "clientes": clientes, "visitantes": len(anonimos),
            "total": len(equipe) + len(clientes) + len(anonimos)}


def texto_whatsapp(dados: dict) -> str:
    linhas = [f"🟢 Online agora ({dados['total']})", "", f"Equipe ({len(dados['equipe'])}):"]
    linhas += [f"• {f['nome']} · {f['onde']}" for f in dados["equipe"]] or ["• ninguém"]
    linhas += ["", f"Clientes com conta ({len(dados['clientes'])}):"]
    linhas += [f"• {c['nome'].split()[0]} {c['nome'].split()[-1][:1]}. · {c['onde']}" if len(c["nome"].split()) > 1
               else f"• {c['nome']} · {c['onde']}" for c in dados["clientes"]] or ["• ninguém"]
    linhas += ["", f"Visitantes sem conta no site: {dados['visitantes']}"]
    return "\n".join(linhas)
