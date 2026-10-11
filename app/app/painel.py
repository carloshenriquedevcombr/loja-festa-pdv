"""Dados do painel (tela inicial): cada bloco só aparece para quem tem o cargo para ver aquilo."""
import calendar
from datetime import date, timedelta

from psycopg import sql

from . import presenca
from .web import FORMAS, TZ_SQL, brl

HOJE = sql.SQL("(now() AT TIME ZONE {tz})::date").format(tz=TZ_SQL)
TIPOS_AGENDA = {"PROVA_1": "1ª prova", "PROVA_2": "2ª prova", "RETIRADA": "Retirada", "DEVOLUCAO": "Devolução",
                "PROVA_SITE": "Prova (site)"}


def _grafico_dias(linhas: list[dict], hoje: date, dias: int = 14) -> dict:
    """Colunas de um só tom (entradas por dia). Rótulo direto só no maior dia e em hoje; o resto no toque/tabela."""
    por_dia = {l["dia"]: l["valor"] for l in linhas}
    serie = [{"dia": hoje - timedelta(days=dias - 1 - i)} for i in range(dias)]
    for s in serie:
        s["valor"] = int(por_dia.get(s["dia"], 0))
    maximo = max([s["valor"] for s in serie] + [1])
    # escala "redonda" para as linhas de grade (3 linhas)
    passo = 10 ** max(len(str(maximo // 100)) - 1, 0) * 100
    teto = ((maximo + passo * 3 - 1) // (passo * 3)) * passo * 3 or 300
    largura, altura, esq, base = 640, 220, 56, 190
    util = largura - esq - 8
    larg_col = util / dias
    maior = max(serie, key=lambda s: s["valor"])
    for i, s in enumerate(serie):
        h = round((s["valor"] / teto) * (base - 20), 1)
        s.update(x=round(esq + i * larg_col + larg_col * .18, 1), w=round(larg_col * .64, 1), h=h, y=round(base - h, 1),
                 xc=round(esq + i * larg_col + larg_col / 2, 1), rotulo=brl(s["valor"]), dia_txt=f"{s['dia']:%d/%m}",
                 hoje=s["dia"] == hoje, mostrar=(s["valor"] > 0 and (s is maior or s["dia"] == hoje)),
                 eixo=(i % 2 == (dias - 1) % 2))
    grades = [{"y": round(base - (base - 20) * k / 3, 1), "rotulo": brl(teto * k // 3).replace(",00", "")} for k in range(4)]
    return {"serie": serie, "grades": grades, "largura": largura, "altura": altura, "base": base, "esq": esq,
            "total": sum(s["valor"] for s in serie)}


def _calendario(hoje: date, marcados: set[date]) -> dict:
    semanas = calendar.Calendar(firstweekday=6).monthdatescalendar(hoje.year, hoje.month)   # começa no domingo
    nomes = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro",
             "Novembro", "Dezembro"]
    return {"titulo": f"{nomes[hoje.month - 1]} de {hoje.year}", "hoje": hoje, "marcados": marcados,
            "semanas": semanas, "mes": hoje.month}


def montar(cur, usuario: dict) -> dict:
    perms = usuario["permissoes"]
    r: dict = {"kpis": []}
    cur.execute(sql.SQL("SELECT {hoje} AS hoje").format(hoje=HOJE))
    hoje = cur.fetchone()["hoje"]
    r["hoje"] = hoje

    if perms & {"caixa.fechar", "relatorios.completo"}:
        cur.execute(sql.SQL("""SELECT COALESCE(sum(valor_centavos) FILTER (WHERE tipo = 'ENTRADA'), 0) AS entrou,
                               COALESCE(sum(valor_centavos) FILTER (WHERE tipo = 'SAIDA'), 0) AS saiu,
                               count(*) FILTER (WHERE tipo = 'ENTRADA') AS n_entrou
                          FROM transacoes WHERE (criado_em AT TIME ZONE {tz})::date = {hoje}""").format(tz=TZ_SQL, hoje=HOJE))
        c = cur.fetchone()
        r["caixa"] = c
        r["kpis"] += [
            {"cor": "verde", "icone": "dinheiro_entra", "rotulo": "Entrou hoje", "valor": brl(c["entrou"]),
             "extra": f"{c['n_entrou']} recebimento{'s' if c['n_entrou'] != 1 else ''}", "url": "/caixa"},
            {"cor": "vinho", "icone": "dinheiro_sai", "rotulo": "Saiu hoje", "valor": brl(c["saiu"]),
             "extra": f"Saldo do dia {brl(c['entrou'] - c['saiu'])}", "url": "/caixa"}]
        cur.execute(sql.SQL("""SELECT (criado_em AT TIME ZONE {tz})::date AS dia,
                               sum(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END)
                                 FILTER (WHERE natureza <> 'DESPESA' AND natureza <> 'DEVOLUCAO_CLIENTE') AS valor
                          FROM transacoes WHERE criado_em > now() - interval '15 days'
                         GROUP BY 1""").format(tz=TZ_SQL))
        r["grafico"] = _grafico_dias([{"dia": l["dia"], "valor": max(l["valor"] or 0, 0)} for l in cur.fetchall()], hoje)
        cur.execute(sql.SQL("""SELECT forma_pagamento AS forma,
                               sum(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END) AS valor
                          FROM transacoes
                         WHERE date_trunc('month', criado_em AT TIME ZONE {tz}) = date_trunc('month', {hoje}::timestamp)
                           AND natureza NOT IN ('DESPESA','DEVOLUCAO_CLIENTE')
                         GROUP BY 1 HAVING sum(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END) > 0
                         ORDER BY 2 DESC""").format(tz=TZ_SQL, hoje=HOJE))
        formas = cur.fetchall()
        total = sum(f["valor"] for f in formas) or 1
        r["formas"] = [{"nome": FORMAS.get(f["forma"], f["forma"]), "valor": brl(f["valor"]),
                        "pct": round(f["valor"] * 100 / total), "nivel": round(f["valor"] * 20 / total)} for f in formas]
        r["formas_total"] = brl(sum(f["valor"] for f in formas))

    if "pedidos.ver" in perms:
        cur.execute(sql.SQL("""
            SELECT count(*) AS ativos,
                   count(*) FILTER (WHERE (status = 'PRONTO' AND data_retirada <= {hoje})
                                       OR (status = 'ALUGADO' AND data_devolucao <= {hoje})) AS hoje,
                   count(*) FILTER (WHERE status = 'PRONTO' AND data_retirada <= {hoje}) AS retiradas,
                   count(*) FILTER (WHERE status = 'ALUGADO' AND data_devolucao <= {hoje}) AS devolucoes,
                   count(*) FILTER (WHERE status = 'ALUGADO' AND data_devolucao < {hoje}) AS atrasadas,
                   count(*) FILTER (WHERE status IN ('RESERVADO','PROVA_1','EM_AJUSTE','PROVA_2')
                                      AND data_retirada <= {hoje} + 3) AS urgentes
              FROM pedidos WHERE status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')""").format(hoje=HOJE))
        p = cur.fetchone()
        r["pedidos"] = p
        r["kpis"] += [
            {"cor": "grafite", "icone": "lista", "rotulo": "Pedidos em andamento", "valor": p["ativos"],
             "extra": f"{p['urgentes']} com retirada em até 3 dias" if p["urgentes"] else "tudo no prazo", "url": "/pedidos"},
            {"cor": "ouro", "icone": "cabide", "rotulo": "Entregar ou receber hoje", "valor": p["hoje"],
             "extra": f"{p['retiradas']} retirada(s) · {p['devolucoes']} devolução(ões)", "url": "/pedidos?filtro=hoje"}]
        if p["atrasadas"]:
            r["kpis"].append({"cor": "alerta-kpi", "icone": "alerta", "rotulo": "Devolução atrasada", "valor": p["atrasadas"],
                              "extra": "ver a lista", "url": "/pedidos?filtro=atrasados"})
        cur.execute(sql.SQL("""SELECT pe.id::text AS id, pr.sku, pr.nome AS peca, cl.nome AS cliente, pe.status,
                               CASE WHEN pe.status = 'ALUGADO' THEN pe.data_devolucao ELSE pe.data_retirada END AS dia,
                               CASE WHEN pe.status = 'ALUGADO' THEN 'Devolução' ELSE 'Retirada' END AS o_que
                          FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id LEFT JOIN clientes cl ON cl.id = pe.cliente_id
                         WHERE pe.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO','DEVOLVIDO','EM_LIMPEZA',
                                                 'AGUARDANDO_PAGAMENTO')
                           AND (CASE WHEN pe.status = 'ALUGADO' THEN pe.data_devolucao ELSE pe.data_retirada END)
                               BETWEEN {hoje} - 30 AND {hoje} + 7
                         ORDER BY dia LIMIT 8""").format(hoje=HOJE))
        r["proximos"] = cur.fetchall()
        cur.execute("""SELECT count(*) AS n FROM compras_site WHERE status = 'PAGA' AND pago_em > now() - interval '7 days'""")
        site7 = cur.fetchone()["n"]
        if site7:
            r["kpis"].append({"cor": "creme", "icone": "sacola", "rotulo": "Compras pelo site (7 dias)", "valor": site7,
                              "extra": "ver em Pedidos → Do site", "url": "/pedidos?filtro=site"})

    if "caixa.entrada" in perms:
        cur.execute(sql.SQL("""SELECT count(*) AS n, COALESCE(sum(p.valor_centavos), 0) AS valor
                          FROM crediario_parcelas p JOIN crediarios c ON c.id = p.crediario_id
                         WHERE c.status = 'ATIVO' AND p.paga_em IS NULL AND p.vencimento < {hoje}""").format(hoje=HOJE))
        v = cur.fetchone()
        if v["n"]:
            r["kpis"].append({"cor": "vinho", "icone": "carne", "rotulo": "Parcelas vencidas (crediário)", "valor": v["n"],
                              "extra": brl(v["valor"]), "url": "/crediario?ver=vencidos"})

    if "agenda.ver" in perms:
        cur.execute(sql.SQL("""SELECT to_char(a.inicio AT TIME ZONE {tz}, 'HH24:MI') AS hora, a.tipo, cl.nome AS cliente,
                               pr.sku, a.pedido_id::text AS pedido_id
                          FROM agendamentos a JOIN pedidos p ON p.id = a.pedido_id JOIN produtos pr ON pr.id = p.produto_id
                          LEFT JOIN clientes cl ON cl.id = p.cliente_id
                         WHERE a.status IN ('AGENDADO','REMARCADO') AND (a.inicio AT TIME ZONE {tz})::date = {hoje}
                         ORDER BY a.inicio LIMIT 10""").format(tz=TZ_SQL, hoje=HOJE))
        r["agenda"] = [{**a, "tipo_nome": TIPOS_AGENDA.get(a["tipo"], a["tipo"].title())} for a in cur.fetchall()]
        cur.execute(sql.SQL("""SELECT DISTINCT (inicio AT TIME ZONE {tz})::date AS dia FROM agendamentos
                         WHERE status IN ('AGENDADO','REMARCADO')
                           AND date_trunc('month', inicio AT TIME ZONE {tz}) = date_trunc('month', {hoje}::timestamp)""")
                    .format(tz=TZ_SQL, hoje=HOJE))
        r["calendario"] = _calendario(hoje, {l["dia"] for l in cur.fetchall()})

    if "site.solicitacoes" in perms:
        cur.execute("SELECT count(*) AS n FROM solicitacoes_prova WHERE status = 'NOVA'")
        r["site"] = cur.fetchone()["n"]
        if r["site"]:
            r["kpis"].append({"cor": "creme", "icone": "envelope", "rotulo": "Pedidos de prova novos pelo site", "valor": r["site"],
                              "extra": "responder", "url": "/solicitacoes"})
    if perms & {"pagamento.link", "caixa.fechar"}:
        cur.execute("SELECT count(*) AS n FROM links_pagamento WHERE status = 'PAGO_CONFERIR'")
        r["conferir"] = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM compras_site WHERE status = 'CONFERIR'")
        r["conferir"] += cur.fetchone()["n"]
    if "avaliacoes.moderar" in perms:
        cur.execute("SELECT count(*) AS n FROM avaliacoes WHERE status = 'PENDENTE'")
        n = cur.fetchone()["n"]
        if n:
            r["kpis"].append({"cor": "creme", "icone": "estrela", "rotulo": "Avaliações para aprovar", "valor": n,
                              "extra": "aparecem no site depois", "url": "/config/avaliacoes"})
    if "presenca.ver" in perms:
        r["online"] = presenca.online(cur)
    return r
