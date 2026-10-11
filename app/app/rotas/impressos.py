"""Impressos que ficam na peça: checklist do pedido (preso no cabide até a entrega/devolução) e etiquetas.

Formatos:
  checklist: "10x15" (impressora de etiqueta 100 x 150 mm) ou "a4"
  etiqueta:  "pendurar" (50 x 90 mm, com furo), "adesiva" (60 x 40 mm) ou "a4" (folha com 21 etiquetas 63,5 x 38,1 mm)
"""
from uuid import UUID

from psycopg import sql
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import codigos, cripto, db
from ..auth import auditar, pode, requer
from ..web import TZ_SQL, brl, tela
from .pedidos import STATUS, TIPO_NOME

router = APIRouter()

# Passos do checklist por tipo de pedido: (status que marca como feito, texto do passo)
PASSOS = {
    "ALUGUEL": [
        ("RESERVADO", "Reserva paga e contrato assinado"),
        ("PROVA_1", "1ª prova feita — anotar os ajustes abaixo"),
        ("EM_AJUSTE", "Enviado para a costureira"),
        ("PROVA_2", "Ajuste pronto · 2ª prova"),
        ("PRONTO", "Aprovado: passado, embalado e conferido (capa, cabide, acessórios)"),
        ("ALUGADO", "Saldo quitado · cliente retirou (conferir a peça com ela)"),
        ("DEVOLVIDO", "Devolvido — conferir manchas, rasgos, bainha, zíper e acessórios"),
        ("EM_LIMPEZA", "Enviado para a limpeza"),
        ("CONCLUIDO", "Voltou da limpeza — de volta ao estoque"),
    ],
    "VENDA_AJUSTE": [
        ("RESERVADO", "Venda registrada e entrada paga"),
        ("PROVA_1", "1ª prova feita — anotar os ajustes abaixo"),
        ("EM_AJUSTE", "Enviado para a costureira"),
        ("PROVA_2", "Ajuste pronto · 2ª prova"),
        ("PRONTO", "Aprovado: passado, embalado e conferido"),
        ("RETIRADO", "Saldo quitado · cliente levou"),
    ],
    "VENDA_SIMPLES": [
        ("VENDIDO", "Venda paga e conferida com a cliente"),
    ],
}


def _fluxo(p: dict) -> str:
    return "ALUGUEL" if p["tipo"] == "ALUGUEL" else ("VENDA_AJUSTE" if p["com_ajuste"] else "VENDA_SIMPLES")


@router.get("/pedidos/{pid}/checklist")
def checklist(request: Request, pid: UUID, formato: str = "10x15", usuario: dict = Depends(requer("pedidos.ver"))):
    formato = formato if formato in ("10x15", "a4") else "10x15"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(sql.SQL("""SELECT pe.id::text AS id, pe.tipo, pe.com_ajuste, pe.status, pe.data_retirada, pe.data_devolucao,
                               pe.data_festa, pe.valor_total_centavos, valor_pago(pe.id) AS pago,
                               pr.sku, pr.nome AS peca, pr.tamanho, pr.cor, cl.nome AS cliente, cl.telefone_c,
                               to_char(pe.criado_em AT TIME ZONE {tz}, 'DD/MM/YYYY') AS feito, u.nome AS atendente
                          FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                          JOIN usuarios u ON u.id = pe.criado_por
                          LEFT JOIN clientes cl ON cl.id = pe.cliente_id WHERE pe.id = %s""").format(tz=TZ_SQL), (str(pid),))
        p = cur.fetchone()
        if not p:
            return tela(request, "erro.html", usuario, status=404, titulo="Pedido não encontrado")
        cur.execute(sql.SQL("""SELECT h.para, to_char(h.criado_em AT TIME ZONE {tz}, 'DD/MM') AS quando, u.nome AS quem
                          FROM historico_status h LEFT JOIN usuarios u ON u.id = h.usuario_id
                         WHERE h.pedido_id = %s ORDER BY h.id""").format(tz=TZ_SQL), (str(pid),))
        feitos = {h["para"]: h for h in cur.fetchall()}
        cur.execute(sql.SQL("""SELECT tipo, to_char(inicio AT TIME ZONE {tz}, 'DD/MM HH24:MI') AS quando FROM agendamentos
                         WHERE pedido_id = %s AND status <> 'CANCELADO' ORDER BY inicio""").format(tz=TZ_SQL), (str(pid),))
        agenda = {a["tipo"]: a["quando"] for a in cur.fetchall()}
        auditar(cur, request, "checklist_impresso", usuario["id"], "pedido", str(pid))
    fone = cripto.decifrar(p.pop("telefone_c"), "clientes.telefone") if p.get("telefone_c") else None
    codigo = codigos.codigo_pedido(p["id"])
    passos = [{"texto": t, "feito": feitos.get(s)} for s, t in PASSOS[_fluxo(p)]]
    return tela(request, "checklist.html", usuario, p=p, formato=formato, codigo=codigo, passos=passos, agenda=agenda,
                fone_final=fone[-4:] if fone else None, tipo_nome=TIPO_NOME[_fluxo(p)], STATUS=STATUS,
                barras=codigos.barras_svg(codigo),
                qr=codigos.qr_svg(f"{codigos.base(request)}/pedidos/{p['id']}", f"QR do pedido {codigo}"),
                saldo=max(p["valor_total_centavos"] - p["pago"], 0))


# --------------------------------------------------------------------------- #
# Etiquetas das peças
# --------------------------------------------------------------------------- #
def _dados_etiqueta(request, p: dict) -> dict:
    precos = [(nome, brl(valor)) for nome, valor, ok in (
        ("Aluguel", p["preco_aluguel_centavos"], p["pode_alugar"]),
        ("Venda", p["preco_venda_centavos"], p["pode_vender"])) if ok and valor]
    destino = (f"{codigos.base(request)}/site/p/{p['slug']}" if p.get("no_site") and p.get("slug")
               else f"{codigos.base(request)}/pecas/{p['id']}")
    return {**p, "precos": precos, "barras": codigos.barras_svg(p["sku"]),
            "qr": codigos.qr_svg(destino, f"QR da peça {p['sku']}")}


SQL_PECAS = """SELECT p.id::text AS id, p.sku, p.nome, p.tamanho, p.cor, p.preco_aluguel_centavos, p.preco_venda_centavos,
                      p.pode_alugar, p.pode_vender, p.no_site, p.slug
                 FROM produtos p WHERE p.status <> 'ARQUIVADO'"""
FORMATOS_ETIQUETA = {"pendurar": "Para pendurar (50 × 90 mm)", "adesiva": "Adesiva (60 × 40 mm)",
                     "a4": "Folha A4 com 21 etiquetas (63,5 × 38,1 mm)"}


@router.get("/pecas/{pid}/etiqueta")
def etiqueta(request: Request, pid: UUID, formato: str = "pendurar", copias: int = 1,
             usuario: dict = Depends(requer("catalogo.ver"))):
    return RedirectResponse(f"/pecas/etiquetas/imprimir?ids={pid}&formato={formato}&copias={max(1, min(copias, 21))}",
                            status_code=303)


@router.get("/pecas/etiquetas")
def escolher(request: Request, q: str = "", usuario: dict = Depends(requer("catalogo.ver"))):
    """Escolher várias peças para imprimir as etiquetas de uma vez."""
    with db.transacao(usuario_id=usuario["id"]) as cur:
        filtro = ""
        params: list = []
        if q.strip():
            filtro = " AND (p.sku ILIKE %s OR f_unaccent(lower(p.nome)) LIKE f_unaccent(lower(%s)))"
            params = [f"%{q.strip()[:40]}%", f"%{q.strip()[:40]}%"]
        cur.execute(SQL_PECAS + filtro + " ORDER BY p.criado_em DESC LIMIT 120", params)
        pecas = cur.fetchall()
    return tela(request, "etiquetas_escolher.html", usuario, pecas=pecas, q=q, FORMATOS=FORMATOS_ETIQUETA)


@router.get("/pecas/etiquetas/imprimir")
def imprimir(request: Request, ids: str = "", formato: str = "pendurar", copias: int = 1,
             usuario: dict = Depends(requer("catalogo.ver"))):
    formato = formato if formato in FORMATOS_ETIQUETA else "pendurar"
    escolhidos = []
    for x in ids.split(",")[:63]:
        try:
            escolhidos.append(str(UUID(x.strip())))
        except ValueError:
            continue
    if not escolhidos:
        return RedirectResponse("/pecas/etiquetas", status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(SQL_PECAS + " AND p.id = ANY(%s::uuid[]) ORDER BY p.sku", (escolhidos,))
        pecas = [_dados_etiqueta(request, p) for p in cur.fetchall()]
    copias = max(1, min(copias, 21))
    lista = [p for p in pecas for _ in range(copias)]
    return tela(request, "etiquetas.html", usuario, etiquetas=lista, formato=formato, FORMATOS=FORMATOS_ETIQUETA,
                ids=",".join(escolhidos), copias=copias, voltar=f"/pecas/{escolhidos[0]}" if len(escolhidos) == 1 else "/pecas/etiquetas")
