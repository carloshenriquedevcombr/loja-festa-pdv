"""Documentos para imprimir: comprovante do pedido, recibo de pagamento, extrato do caixa e da cliente.

Cada função devolve um documento "neutro" (dicionário com blocos). O mesmo documento vira:
  * página HTML para imprimir em folha A4 ou em impressora térmica (80 mm / 58 mm);
  * PDF (pdf.py) nos mesmos formatos.
Assim o que sai no papel e no PDF é sempre igual.

Blocos:
  {"t": "secao", "texto": ...}
  {"t": "pares", "itens": [(rótulo, valor, destaque)]}
  {"t": "tabela", "colunas": [(nome, "L"|"R", peso)], "linhas": [[...]], "total": [...] | None}
  {"t": "texto", "texto": ..., "pequeno": bool}
  {"t": "assinaturas", "nomes": [...]}
  {"t": "contrato", "texto": ..., "link": ...}        (A4: texto inteiro; bobina: aviso + link)
  {"t": "imagem", "png": bytes, "legenda": ...}       (assinatura da cliente)
  {"t": "codigo", "texto": ..., "detalhe": ...}
"""
from datetime import date

from psycopg import sql

from . import cripto
from .web import EM_FORMA, FORMAS, TZ_SQL, brl, cpf, data_br, telefone

COMPROMISSOS = {"PROVA_1": "1ª prova", "PROVA_2": "2ª prova", "RETIRADA": "Retirada", "DEVOLUCAO": "Devolução"}
SITUACAO_AGENDA = {"AGENDADO": "marcado", "REMARCADO": "remarcado", "FEITO": "feito", "FALTOU": "faltou",
                   "CANCELADO": "cancelado"}


from . import config  # noqa: E402


def _loja(cur) -> dict:
    cur.execute(sql.SQL("""SELECT nome, cnpj, endereco, telefone, rodape_comprovante, logo_arquivo,
                           to_char(now() AT TIME ZONE {tz}, 'DD/MM/YYYY HH24:MI') AS agora
                      FROM lojas WHERE id = ctx_loja()""").format(tz=TZ_SQL))
    loja = cur.fetchone()
    if loja["cnpj"]:
        c = loja["cnpj"]
        loja["cnpj"] = f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
    return loja


def _doc(cur, titulo: str, numero: str | None = None) -> dict:
    loja = _loja(cur)
    return {"titulo": titulo, "numero": numero, "loja": loja, "emitido": loja["agora"], "blocos": []}


def numero_pedido(pid: str) -> str:
    return pid.replace("-", "")[:8].upper()


def _tipo_nome(p) -> str:
    if p["tipo"] == "ALUGUEL":
        return "Aluguel"
    return "Venda com ajuste" if p["com_ajuste"] else "Venda"


# --------------------------------------------------------------------------- #
def pedido(cur, pid, status_nomes: dict, mascarar=lambda t: t, publico: bool = False, base: str = "") -> dict | None:
    cur.execute(sql.SQL("""
        SELECT pe.*, pe.id::text AS id, pr.sku, pr.nome AS peca, pr.tamanho, pr.cor,
               cl.nome AS cliente, cl.telefone_c, cl.cpf_c, cl.endereco_c, u.nome AS atendente,
               to_char(pe.criado_em AT TIME ZONE {tz}, 'DD/MM/YYYY HH24:MI') AS criado,
               valor_pago(pe.id) AS pago, pe.valor_total_centavos - valor_pago(pe.id) AS saldo
          FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
          JOIN usuarios u ON u.id = pe.criado_por
          LEFT JOIN clientes cl ON cl.id = pe.cliente_id
         WHERE pe.id = %s""").format(tz=TZ_SQL), (pid,))
    p = cripto.abrir(cur.fetchone(), "clientes", "telefone", "cpf", "endereco")
    if not p:
        return None
    tipo = _tipo_nome(p)
    d = _doc(cur, f"Comprovante de {tipo.lower()}", numero_pedido(p["id"]))
    b = d["blocos"]
    peca = f"{p['sku']} - {p['peca']}" + (f" (tam. {p['tamanho']})" if p["tamanho"] else "") + \
           (f", {p['cor']}" if p["cor"] else "")
    pares = [("Cliente", p["cliente"] or "Venda no balcão", True)]
    if p["telefone"]:
        pares.append(("Telefone", telefone(mascarar(p["telefone"])), False))
    if p["cpf"]:
        pares.append(("CPF", cpf(mascarar(p["cpf"])), False))
    pares += [("Peça", peca, False), ("Tipo", tipo, False), ("Feito em", p["criado"], False),
              ("Atendente", p["atendente"], False), ("Situação", status_nomes.get(p["status"], p["status"]), False)]
    if p["data_festa"]:
        pares.append(("Festa", data_br(p["data_festa"]), False))
    b.append({"t": "pares", "itens": pares})

    cur.execute(sql.SQL("""SELECT tipo, status, to_char(inicio AT TIME ZONE {tz}, 'DD/MM/YYYY') AS dia,
                           to_char(inicio AT TIME ZONE {tz}, 'HH24:MI') AS hora
                      FROM agendamentos WHERE pedido_id = %s AND status <> 'CANCELADO' ORDER BY inicio""").format(tz=TZ_SQL), (pid,))
    agenda = cur.fetchall()
    if agenda:
        b.append({"t": "secao", "texto": "Agenda"})
        b.append({"t": "tabela", "colunas": [("Compromisso", "L", 3), ("Quando", "R", 3)],
                  "linhas": [[COMPROMISSOS[a["tipo"]] + (" (feito)" if a["status"] == "FEITO" else ""),
                              f"{a['dia']} {a['hora']}"] for a in agenda], "total": None})

    b.append({"t": "secao", "texto": "Valores"})
    valores = [("Aluguel" if p["tipo"] == "ALUGUEL" else "Peça", brl(p["valor_peca_centavos"]), False)]
    if p["valor_ajuste_centavos"]:
        valores.append(("Ajuste", brl(p["valor_ajuste_centavos"]), False))
    valores += [("Total", brl(p["valor_total_centavos"]), True), ("Já pago", brl(p["pago"]), False),
                ("Falta pagar" if p["saldo"] > 0 else "Situação", brl(p["saldo"]) if p["saldo"] > 0 else "QUITADO", True)]
    b.append({"t": "pares", "itens": valores})

    cur.execute(sql.SQL("""SELECT t.id, t.tipo, t.valor_centavos, t.forma_pagamento, t.parcelas,
                           to_char(t.criado_em AT TIME ZONE {tz}, 'DD/MM/YY') AS dia
                      FROM transacoes t WHERE t.pedido_id = %s ORDER BY t.id""").format(tz=TZ_SQL), (pid,))
    pags = cur.fetchall()
    if pags:
        b.append({"t": "secao", "texto": "Pagamentos"})
        b.append({"t": "tabela", "colunas": [("Data", "L", 2), ("Forma", "L", 3), ("Valor", "R", 3)],
                  "linhas": [[t["dia"], FORMAS[t["forma_pagamento"]] + (f" {t['parcelas']}x" if t["parcelas"] > 1 else "")
                              + (" (devolvido)" if t["tipo"] == "SAIDA" else ""),
                              ("- " if t["tipo"] == "SAIDA" else "") + brl(t["valor_centavos"])] for t in pags],
                  "total": None})

    if p["tipo"] == "ALUGUEL" and p["data_devolucao"]:
        b.append({"t": "texto", "texto": f"Retirada em {data_br(p['data_retirada'])}. "
                                         f"A peça deve ser devolvida até {data_br(p['data_devolucao'])}.",
                  "pequeno": False})
    elif p["com_ajuste"] and p["data_retirada"]:
        b.append({"t": "texto", "texto": f"Retirada prevista para {data_br(p['data_retirada'])}, depois das provas.",
                  "pequeno": False})
    if p["saldo"] > 0 and p["status"] != "CANCELADO":
        b.append({"t": "texto", "texto": f"O restante ({brl(p['saldo'])}) deve ser pago até a retirada.",
                  "pequeno": False})
    _contrato(cur, d, p, publico or mascarar("1234567890") != "1234567890")
    from . import codigos
    raiz = (base or config.SITE_URL or "").rstrip("/")
    cod = codigos.codigo_verificacao(p["id"])
    d["blocos"].append({"t": "qr", "url": f"{raiz}/v/{cod}", "codigo": cod,
                        "texto": "Escaneie para ver a situação do pedido e conferir o contrato"})
    return d


def _contrato(cur, d: dict, p: dict, publico: bool) -> None:
    """O contrato vai em TODO comprovante: completo na folha A4 e no PDF; na bobina, o código e o link."""
    from . import avisos, config, contrato
    if not p["cliente"]:
        return
    d["link_cliente"] = avisos.link(f"/site/meu-pedido/{p['token_publico']}")
    assinado = contrato.assinado(cur, p["id"])
    if assinado:
        texto = contrato.mascarar_cpf(assinado["texto"]) if publico else assinado["texto"]
        d["blocos"].append({"t": "contrato", "texto": texto, "link": d["link_cliente"]})
        d["blocos"].append({"t": "imagem", "png": assinado["assinatura"],
                            "legenda": f"Assinado por {assinado['assinante']} em {assinado['quando']}"})
        d["blocos"].append({"t": "codigo", "texto": f"Código de verificação: {assinado['codigo']}",
                            "detalhe": f"Confira em {avisos.link('/site/contrato')}?codigo={assinado['codigo']}"})
        d["contrato_codigo"] = assinado["codigo"]
    else:
        dados = contrato.dados_do_pedido(cur, p["id"])
        texto = contrato.texto(dados)
        d["blocos"].append({"t": "contrato", "texto": contrato.mascarar_cpf(texto) if publico else texto,
                            "link": d["link_cliente"]})
        d["blocos"].append({"t": "assinaturas", "nomes": [p["cliente"], d["loja"]["nome"]]})


# --------------------------------------------------------------------------- #
def recibo(cur, tid: int, mascarar=lambda t: t) -> dict | None:
    cur.execute(sql.SQL("""
        SELECT t.*, u.nome AS quem, m.apelido AS maquininha, pr.sku, pr.nome AS peca, cl.nome AS cliente,
               pe.id::text AS pedido, to_char(t.criado_em AT TIME ZONE {tz}, 'DD/MM/YYYY HH24:MI') AS quando
          FROM transacoes t JOIN usuarios u ON u.id = t.usuario_id
          LEFT JOIN maquininhas m ON m.id = t.maquininha_id
          LEFT JOIN pedidos pe ON pe.id = t.pedido_id
          LEFT JOIN produtos pr ON pr.id = pe.produto_id
          LEFT JOIN clientes cl ON cl.id = pe.cliente_id
         WHERE t.id = %s""").format(tz=TZ_SQL), (tid,))
    t = cur.fetchone()
    if not t:
        return None
    if t["natureza"] == "ESTORNO":
        titulo = "Comprovante de devolução" if t["tipo"] == "SAIDA" else "Comprovante de estorno"
    else:
        titulo = "Recibo" if t["tipo"] == "ENTRADA" else "Comprovante de pagamento feito"
    d = _doc(cur, titulo, str(t["id"]))
    forma = FORMAS[t["forma_pagamento"]] + (f" em {t['parcelas']}x" if t["parcelas"] > 1 else "")
    pares = [("Valor", brl(t["valor_centavos"]), True), ("Forma", forma, False), ("Data", t["quando"], False)]
    if t["acrescimo_juros_centavos"]:
        pares.append(("Acréscimo do parcelamento", brl(t["acrescimo_juros_centavos"]), False))
    if t["maquininha"]:
        pares.append(("Maquininha", t["maquininha"], False))
    if t["nsu"]:
        pares.append(("NSU", t["nsu"], False))
    if t["cliente"]:
        pares.append(("Cliente", t["cliente"], False))
    if t["sku"]:
        pares.append(("Peça", f"{t['sku']} - {t['peca']}", False))
        pares.append(("Pedido", numero_pedido(t["pedido"]), False))
    pares += [("Referente a", t["descricao"], False), ("Atendido por", t["quem"], False)]
    d["blocos"].append({"t": "pares", "itens": pares})
    if t["tipo"] == "ENTRADA" and t["natureza"] != "ESTORNO":
        quem = t["cliente"] or "o cliente"
        d["blocos"].append({"t": "texto", "pequeno": False,
                            "texto": f"Recebemos de {quem} a quantia de {brl(t['valor_centavos'])} "
                                     f"{EM_FORMA[t['forma_pagamento']]}."})
    if t["pedido"]:
        cur.execute("SELECT valor_total_centavos - valor_pago(id) AS saldo FROM pedidos WHERE id = %s", (t["pedido"],))
        saldo = cur.fetchone()["saldo"]
        d["blocos"].append({"t": "pares", "itens": [("Falta pagar no pedido" if saldo > 0 else "Pedido",
                                                     brl(saldo) if saldo > 0 else "QUITADO", True)]})
    return d


# --------------------------------------------------------------------------- #
def extrato_caixa(cur, de: date, ate: date) -> dict:
    cur.execute(sql.SQL("""
        SELECT t.id, t.tipo, t.natureza, t.valor_centavos, t.forma_pagamento, t.parcelas, t.descricao, t.estorno_de,
               to_char(t.criado_em AT TIME ZONE {tz}, 'DD/MM HH24:MI') AS quando, u.nome AS quem,
               EXISTS (SELECT 1 FROM transacoes e WHERE e.estorno_de = t.id) AS desfeito
          FROM transacoes t JOIN usuarios u ON u.id = t.usuario_id
         WHERE (t.criado_em AT TIME ZONE {tz})::date BETWEEN %s AND %s
         ORDER BY t.id""").format(tz=TZ_SQL), (de, ate))
    lanc = cur.fetchall()
    periodo = data_br(de) if de == ate else f"{data_br(de)} a {data_br(ate)}"
    d = _doc(cur, "Extrato do caixa", None)
    d["subtitulo"] = periodo
    entrou = sum(t["valor_centavos"] for t in lanc if t["tipo"] == "ENTRADA")
    saiu = sum(t["valor_centavos"] for t in lanc if t["tipo"] == "SAIDA")
    por_forma = {f: 0 for f in FORMAS}
    for t in lanc:
        por_forma[t["forma_pagamento"]] += t["valor_centavos"] * (1 if t["tipo"] == "ENTRADA" else -1)
    b = d["blocos"]
    b.append({"t": "pares", "itens": [("Período", periodo, True), ("Entrou", brl(entrou), False),
                                      ("Saiu", brl(saiu), False), ("Resultado", brl(entrou - saiu), True)]})
    b.append({"t": "secao", "texto": "Por forma de pagamento"})
    b.append({"t": "pares", "itens": [(FORMAS[f], brl(v), f == "DINHEIRO") for f, v in por_forma.items()]})
    b.append({"t": "secao", "texto": f"Lançamentos ({len(lanc)})"})
    if lanc:
        b.append({"t": "tabela",
                  "colunas": [("Nº", "L", 1), ("Quando", "L", 2), ("Descrição", "L", 5), ("Forma", "L", 2),
                              ("Valor", "R", 3)],
                  "linhas": [[str(t["id"]), t["quando"],
                              t["descricao"] + (" (desfeito)" if t["desfeito"] else "") + f" · {t['quem']}",
                              FORMAS[t["forma_pagamento"]] + (f" {t['parcelas']}x" if t["parcelas"] > 1 else ""),
                              ("- " if t["tipo"] == "SAIDA" else "") + brl(t["valor_centavos"])] for t in lanc],
                  "total": ["", "", "Resultado", "", brl(entrou - saiu)]})
    else:
        b.append({"t": "texto", "texto": "Nenhum lançamento no período.", "pequeno": False})
    b.append({"t": "texto", "pequeno": True,
              "texto": "Lançamentos desfeitos continuam aparecendo junto com o lançamento que os desfez: "
                       "nada é apagado do caixa."})
    return d


# --------------------------------------------------------------------------- #
def extrato_cliente(cur, cid, status_nomes: dict, mascarar=lambda t: t) -> dict | None:
    cur.execute("SELECT nome, telefone_c, cpf_c FROM clientes WHERE id = %s", (cid,))
    c = cripto.abrir(cur.fetchone(), "clientes", "telefone", "cpf")
    if not c:
        return None
    d = _doc(cur, "Extrato da cliente", None)
    d["subtitulo"] = c["nome"]
    pares = [("Cliente", c["nome"], True)]
    if c["telefone"]:
        pares.append(("Telefone", telefone(mascarar(c["telefone"])), False))
    if c["cpf"]:
        pares.append(("CPF", cpf(mascarar(c["cpf"])), False))
    cur.execute(sql.SQL("""
        SELECT p.id::text, p.tipo, p.com_ajuste, p.status, p.valor_total_centavos, pr.sku, pr.nome AS peca,
               valor_pago(p.id) AS pago, to_char(p.criado_em AT TIME ZONE {tz}, 'DD/MM/YY') AS dia
          FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
         WHERE p.cliente_id = %s ORDER BY p.criado_em""").format(tz=TZ_SQL), (cid,))
    pedidos = cur.fetchall()
    ativos = [p for p in pedidos if p["status"] != "CANCELADO"]
    falta = sum(p["valor_total_centavos"] - p["pago"] for p in ativos if p["valor_total_centavos"] > p["pago"])
    pares += [("Pedidos", str(len(ativos)), False), ("Total pago", brl(sum(p["pago"] for p in pedidos)), False),
              ("Falta pagar", brl(falta) if falta else "nada", True)]
    b = d["blocos"]
    b.append({"t": "pares", "itens": pares})
    if pedidos:
        b.append({"t": "secao", "texto": "Pedidos"})
        b.append({"t": "tabela",
                  "colunas": [("Data", "L", 2), ("Pedido", "L", 5), ("Total", "R", 3), ("Falta", "R", 3)],
                  "linhas": [[p["dia"], f"{_tipo_nome(p)} {p['sku']} - {p['peca']} ({status_nomes.get(p['status'], p['status'])})",
                              brl(p["valor_total_centavos"]),
                              "-" if p["status"] == "CANCELADO" else brl(max(p["valor_total_centavos"] - p["pago"], 0))]
                             for p in pedidos],
                  "total": None})
        cur.execute(sql.SQL("""
            SELECT t.id, t.tipo, t.valor_centavos, t.forma_pagamento, t.parcelas, pr.sku,
                   to_char(t.criado_em AT TIME ZONE {tz}, 'DD/MM/YY') AS dia
              FROM transacoes t JOIN pedidos p ON p.id = t.pedido_id JOIN produtos pr ON pr.id = p.produto_id
             WHERE p.cliente_id = %s ORDER BY t.id""").format(tz=TZ_SQL), (cid,))
        pags = cur.fetchall()
        if pags:
            b.append({"t": "secao", "texto": "Pagamentos"})
            b.append({"t": "tabela", "colunas": [("Data", "L", 2), ("Peça", "L", 2), ("Forma", "L", 3), ("Valor", "R", 3)],
                      "linhas": [[t["dia"], t["sku"],
                                  FORMAS[t["forma_pagamento"]] + (f" {t['parcelas']}x" if t["parcelas"] > 1 else ""),
                                  ("- " if t["tipo"] == "SAIDA" else "") + brl(t["valor_centavos"])] for t in pags],
                      "total": None})
    return d
