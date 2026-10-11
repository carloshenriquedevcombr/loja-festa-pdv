"""Filas de trabalho (costura, limpeza), relatório do mês e painel técnico."""
from datetime import date

from fastapi import APIRouter, Depends, Request

from .. import config, db
from ..auth import requer
from ..web import TZ, tela
from .pedidos import STATUS, etapas_possiveis

router = APIRouter()


def _fila(request, usuario, titulo, grupos, vazio):
    """grupos: [(título do grupo, [status...])]"""
    blocos = []
    with db.transacao(usuario_id=usuario["id"]) as cur:
        for nome, status in grupos:
            cur.execute(f"""
                SELECT pe.id::text, pe.status, pe.fluxo, pe.tipo, pe.data_retirada, pe.data_devolucao,
                       pr.sku, pr.nome AS peca, cl.nome AS cliente, cl.id::text AS cliente_id,
                       (pe.data_retirada - (now() AT TIME ZONE '{TZ}')::date) AS dias
                  FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                  LEFT JOIN clientes cl ON cl.id = pe.cliente_id
                 WHERE pe.status = ANY(%s)
                 ORDER BY pe.data_retirada NULLS LAST, pe.criado_em""", (list(status),))
            itens = cur.fetchall()
            for item in itens:
                item["etapas"] = etapas_possiveis(cur, usuario, item)
            blocos.append((nome, itens))
    return tela(request, "fila.html", usuario, titulo=titulo, blocos=blocos, vazio=vazio, STATUS=STATUS)


@router.get("/costura")
def costura(request: Request, usuario: dict = Depends(requer("ajustes.ver"))):
    return _fila(request, usuario, "Fila de costura",
                 [("Para ajustar agora", ["EM_AJUSTE"]),
                  ("Esperando prova", ["RESERVADO", "PROVA_1", "PROVA_2"])],
                 "Nenhuma peça para ajustar.")


@router.get("/limpeza")
def limpeza(request: Request, usuario: dict = Depends(requer("limpeza.ver"))):
    return _fila(request, usuario, "Limpeza",
                 [("Levar para limpar", ["DEVOLVIDO"]), ("Na limpeza: marcar quando voltar", ["EM_LIMPEZA"])],
                 "Nenhuma peça esperando limpeza.")


@router.get("/relatorios")
def relatorios(request: Request, mes: str = "", usuario: dict = Depends(requer("relatorios.completo"))):
    try:
        inicio = date.fromisoformat(f"{mes}-01") if mes else None
    except ValueError:
        inicio = None
    with db.transacao(usuario_id=usuario["id"]) as cur:
        if not inicio:
            cur.execute(f"SELECT date_trunc('month', now() AT TIME ZONE '{TZ}')::date AS m")
            inicio = cur.fetchone()["m"]
        fim = date(inicio.year + (inicio.month == 12), inicio.month % 12 + 1, 1)
        dia = f"(t.criado_em AT TIME ZONE '{TZ}')::date"
        cur.execute(f"""
            SELECT {dia} AS dia,
                   sum(valor_centavos) FILTER (WHERE tipo = 'ENTRADA') AS entrou,
                   sum(valor_centavos) FILTER (WHERE tipo = 'SAIDA')   AS saiu
              FROM transacoes t WHERE {dia} >= %s AND {dia} < %s GROUP BY 1 ORDER BY 1""", (inicio, fim))
        dias = cur.fetchall()
        cur.execute(f"""
            SELECT forma_pagamento AS forma,
                   sum(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END) AS liquido,
                   sum(acrescimo_juros_centavos) AS juros
              FROM transacoes t WHERE {dia} >= %s AND {dia} < %s GROUP BY 1 ORDER BY 2 DESC""", (inicio, fim))
        formas = cur.fetchall()
        cur.execute("""
            SELECT count(*) FILTER (WHERE tipo = 'ALUGUEL')                  AS alugueis,
                   count(*) FILTER (WHERE tipo = 'VENDA')                    AS vendas,
                   COALESCE(sum(valor_ajuste_centavos), 0)                   AS ajustes
              FROM pedidos WHERE status <> 'CANCELADO'
               AND (criado_em AT TIME ZONE 'America/Sao_Paulo')::date >= %s
               AND (criado_em AT TIME ZONE 'America/Sao_Paulo')::date < %s""", (inicio, fim))
        pedidos = cur.fetchone()
        cur.execute(f"""SELECT COALESCE(sum(valor_total_centavos - valor_pago(id)), 0) AS a_receber
                          FROM pedidos WHERE status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')""")
        a_receber = cur.fetchone()["a_receber"]
    total_entrou = sum(d["entrou"] or 0 for d in dias)
    total_saiu = sum(d["saiu"] or 0 for d in dias)
    maior = max([d["entrou"] or 0 for d in dias] + [1])
    anterior = date(inicio.year - (inicio.month == 1), (inicio.month - 2) % 12 + 1, 1)
    return tela(request, "relatorios.html", usuario, inicio=inicio, dias=dias, formas=formas, pedidos=pedidos,
                a_receber=a_receber, total_entrou=total_entrou, total_saiu=total_saiu, maior=maior,
                anterior=anterior.strftime("%Y-%m"), seguinte=fim.strftime("%Y-%m"))


@router.get("/sistema")
def sistema(request: Request, usuario: dict = Depends(requer("sistema.ver_tudo"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT (SELECT count(*) FROM produtos) AS pecas, (SELECT count(*) FROM pedidos) AS pedidos,
                              (SELECT count(*) FROM transacoes) AS lancamentos,
                              (SELECT count(*) FROM usuarios WHERE ativo AND NOT sistema) AS usuarios,
                              (SELECT count(*) FROM usuarios WHERE bloqueado_ate > now()) AS bloqueados,
                              pg_size_pretty(pg_database_size(current_database())) AS tamanho,
                              version() AS versao""")
        numeros = cur.fetchone()
        cur.execute(f"""SELECT to_char(a.criado_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI:SS') AS quando, a.acao,
                               a.entidade, u.nome AS quem, host(a.ip) AS ip
                          FROM auditoria a LEFT JOIN usuarios u ON u.id = a.usuario_id
                         ORDER BY a.id DESC LIMIT 40""")
        eventos = cur.fetchall()
    return tela(request, "sistema.html", usuario, numeros=numeros, eventos=eventos,
                cookie_seguro=config.COOKIE_SECURE)
