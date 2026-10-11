"""Link de pagamento pelo Mercado Pago (Checkout Pro).

Para a loja é só colar o "Access Token" de produção em Configurações > Site e pagamento online.
O token fica cifrado no banco (pgcrypto) com uma chave que só o servidor tem.

Segurança do aviso de pagamento (webhook): o aviso só diz "olhe o pagamento X". O sistema
sempre pergunta ao Mercado Pago, com o token da loja, se o pagamento existe, se foi aprovado,
de quanto foi e a qual link pertence. Um aviso falso não lança nada no caixa.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

from . import config, db
from .web import brl, mensagem_do_banco

HORAS_VALIDADE = 48
MINIMO_CENTAVOS = 100


class ErroProvedor(Exception):
    pass


# --------------------------------------------------------------------------- #
# Chamadas ao Mercado Pago (só biblioteca padrão)
# --------------------------------------------------------------------------- #
def _chamar(metodo: str, caminho: str, token: str, corpo: dict | None = None, idem: str | None = None) -> dict:
    cabecalhos = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                  "User-Agent": "loja-festa/1.0"}
    if idem:
        cabecalhos["X-Idempotency-Key"] = idem
    pedido = urllib.request.Request(config.MP_API + caminho, method=metodo, headers=cabecalhos,
                                    data=json.dumps(corpo).encode() if corpo is not None else None)
    try:
        # API do Mercado Pago (https do config).
        with urllib.request.urlopen(pedido, timeout=20) as r:  # nosec B310
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ErroProvedor("O Mercado Pago recusou o token. Confira se copiou o Access Token "
                               "de PRODUÇÃO inteiro (começa com APP_USR-).") from e
        raise ErroProvedor(f"O Mercado Pago respondeu com erro ({e.code}). Tente de novo em alguns minutos.") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ErroProvedor("Não consegui falar com o Mercado Pago. Confira a internet e tente de novo.") from e


def conferir_token(token: str) -> str:
    """Devolve o nome da conta dona do token (mostra para a Dona confirmar que é a conta certa)."""
    eu = _chamar("GET", "/users/me", token)
    return str(eu.get("nickname") or eu.get("email") or eu.get("id") or "conta confirmada")[:80]


def criar_preferencia(token: str, link_id: str, titulo: str, valor_centavos: int, max_parcelas: int,
                      expira_em: datetime) -> tuple[str, str]:
    corpo = {
        "items": [{"id": link_id, "title": titulo[:120], "quantity": 1, "currency_id": "BRL",
                   "unit_price": round(valor_centavos / 100, 2)}],
        "external_reference": link_id,
        "payment_methods": {"excluded_payment_types": [{"id": "ticket"}, {"id": "atm"}],
                            "installments": max(1, min(max_parcelas, 12))},
        "expires": True,
        "expiration_date_to": expira_em.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
    }
    if config.SITE_URL:
        corpo["notification_url"] = f"{config.SITE_URL}/pagamentos/mercadopago/aviso"
        volta = f"{config.SITE_URL}/site/pagamento"
        corpo["back_urls"] = {"success": volta, "pending": volta, "failure": volta}
        corpo["auto_return"] = "approved"
    r = _chamar("POST", "/checkout/preferences", token, corpo, idem=link_id)
    url = r.get("init_point") or ""
    if not r.get("id") or not url.startswith("https://"):
        raise ErroProvedor("O Mercado Pago não devolveu o link. Tente de novo.")
    return str(r["id"]), url


def pagamentos_do_link(token: str, link_id: str) -> list[dict]:
    q = urllib.parse.urlencode({"external_reference": link_id, "sort": "date_created", "criteria": "desc"})
    return _chamar("GET", f"/v1/payments/search?{q}", token).get("results") or []


def pagamento(token: str, pagamento_id: str) -> dict:
    if not str(pagamento_id).isdigit():
        raise ErroProvedor("Aviso de pagamento inválido.")
    return _chamar("GET", f"/v1/payments/{pagamento_id}", token)


def forma_do_pagamento(p: dict) -> str | None:
    metodo, tipo = p.get("payment_method_id"), p.get("payment_type_id")
    if metodo == "pix" or tipo in ("bank_transfer", "account_money"):
        return "PIX"
    if tipo == "credit_card":
        return "CREDITO"
    if tipo in ("debit_card", "prepaid_card"):
        return "DEBITO"
    return None


# --------------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------------- #
def token_da_loja(cur) -> str | None:
    cur.execute("SELECT token_pagamento(%s) AS t", (config.CHAVE_TOKENS,))
    linha = cur.fetchone()
    return linha["t"] if linha else None


def configurado(cur) -> dict | None:
    cur.execute("""SELECT provedor, token_final, conta, ativo, maquininha_id,
                          to_char(definido_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando
                     FROM pagamento_online WHERE ativo""")
    return cur.fetchone()


def salvar_token(cur, token: str, conta: str, usuario_id: str) -> None:
    """Grava (ou troca) o token. Cria a 'maquininha' Link Mercado Pago para o caixa saber de onde veio."""
    cur.execute("SELECT id FROM maquininhas WHERE apelido = 'Link Mercado Pago'")
    m = cur.fetchone()
    if m:
        maq = m["id"]
        cur.execute("UPDATE maquininhas SET ativa = true WHERE id = %s", (maq,))
    else:
        cur.execute("""INSERT INTO maquininhas (loja_id, apelido, operadora, cor_hex)
                       VALUES (ctx_loja(), 'Link Mercado Pago', 'Mercado Pago', '#1D4ED8') RETURNING id""")
        maq = cur.fetchone()["id"]
    # UPDATE e depois INSERT (sem "ON CONFLICT": o app não tem leitura da coluna do token)
    cur.execute("""UPDATE pagamento_online
                      SET token_cifrado = pgp_sym_encrypt(%s, %s), token_final = %s, conta = %s, ativo = true,
                          maquininha_id = %s, definido_por = %s, definido_em = now()
                    WHERE loja_id = ctx_loja()""",
                (token, config.CHAVE_TOKENS, token[-4:], conta, maq, usuario_id))
    if cur.rowcount == 0:
        cur.execute("""INSERT INTO pagamento_online (loja_id, provedor, token_cifrado, token_final, conta, ativo,
                                                     maquininha_id, definido_por)
                       VALUES (ctx_loja(), 'MERCADO_PAGO', pgp_sym_encrypt(%s, %s), %s, %s, true, %s, %s)""",
                    (token, config.CHAVE_TOKENS, token[-4:], conta, maq, usuario_id))


def liberar_vencidos(cur) -> None:
    cur.execute("SELECT liberar_pedidos_vencidos()")


def criar_link(cur, pedido: dict, valor_centavos: int, usuario_id: str, titulo: str) -> dict:
    """Cria o link no Mercado Pago e grava. Chamar DENTRO da transação do pedido: se o Mercado Pago
    falhar, nada fica gravado."""
    token = token_da_loja(cur) if configurado(cur) else None
    if not token:
        raise ErroProvedor("O pagamento online ainda não foi configurado. Peça para a Dona colocar o token "
                           "em Configurações > Site e pagamento online.")
    if valor_centavos < MINIMO_CENTAVOS:
        raise ErroProvedor("O valor do link precisa ser de pelo menos R$ 1,00.")
    link_id = str(uuid.uuid4())
    expira = datetime.now(timezone.utc) + timedelta(hours=HORAS_VALIDADE)
    id_externo, url = criar_preferencia(token, link_id, titulo, valor_centavos,
                                        pedido["max_parcelas_permitidas"], expira)
    cur.execute("""INSERT INTO links_pagamento (id, loja_id, pedido_id, valor_centavos, max_parcelas, provedor,
                                                id_externo, url_externa, criado_por, expira_em)
                   VALUES (%s, ctx_loja(), %s, %s, %s, 'MERCADO_PAGO', %s, %s, %s, %s)""",
                (link_id, pedido["id"], valor_centavos, pedido["max_parcelas_permitidas"], id_externo, url,
                 usuario_id, expira))
    return {"id": link_id, "url": url, "expira_em": expira}


def aplicar(link_id: str, pag: dict) -> str:
    """Lança no caixa um pagamento APROVADO do link. Idempotente. Devolve o status final do link."""
    if pag.get("status") != "approved" or str(pag.get("external_reference")) != link_id:
        return "ABERTO"
    pag_id = str(pag.get("id"))
    with db.transacao() as cur:
        cur.execute("""SELECT l.*, l.id::text AS id, l.pedido_id::text AS pedido_id, l.criado_por::text AS criado_por,
                              pr.sku, p.status AS status_pedido
                         FROM links_pagamento l JOIN pedidos p ON p.id = l.pedido_id
                         JOIN produtos pr ON pr.id = p.produto_id
                        WHERE l.id = %s FOR UPDATE OF l""", (link_id,))
        link = cur.fetchone()
        if not link:
            return "ABERTO"
        if link["status"] in ("PAGO", "PAGO_CONFERIR"):
            return link["status"]
        cur.execute("SELECT maquininha_id FROM pagamento_online")
        cfg = cur.fetchone()
        valor = round(float(pag.get("transaction_amount") or 0) * 100)
        forma = forma_do_pagamento(pag)
        parcelas = int(pag.get("installments") or 1)
        # O pagamento pertence a este link a partir de agora (evita lançar duas vezes)
        cur.execute("""UPDATE links_pagamento SET pagamento_externo_id = %s, forma_paga = %s, pago_em = now()
                        WHERE id = %s""", (pag_id, forma, link_id))
        cur.execute("SELECT set_config('app.usuario_id', %s, true)", (link["criado_por"],))
        problema = None
        if not forma:
            problema = "Forma de pagamento não reconhecida. Confira no Mercado Pago."
        elif valor != link["valor_centavos"]:
            problema = f"Valor pago ({brl(valor)}) diferente do link ({brl(link['valor_centavos'])}). Confira no Mercado Pago."
        if not problema:
            try:
                with cur.connection.transaction():      # ponto de volta: se o caixa recusar, o link fica "conferir"
                    natureza = ("SALDO" if link["status_pedido"] != "AGUARDANDO_PAGAMENTO" else
                                "ENTRADA_RESERVA")
                    cur.execute(
                        """INSERT INTO transacoes
                             (loja_id, tipo, natureza, valor_centavos, forma_pagamento, parcelas, modalidade_cartao,
                              maquininha_id, nsu, pedido_id, descricao, chave_idempotencia, usuario_id)
                           VALUES (ctx_loja(), 'ENTRADA', %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)
                           RETURNING id""",
                        (natureza, valor, forma, "EMISSOR" if forma == "CREDITO" else None,
                         cfg["maquininha_id"] if forma in ("CREDITO", "DEBITO") else None, pag_id,
                         link["pedido_id"],
                         f"Link Mercado Pago {link['sku']}" + (f" (cliente parcelou em {parcelas}x)" if parcelas > 1 else ""),
                         str(uuid.uuid5(uuid.UUID(link_id), pag_id)), link["criado_por"]))
                    tid = cur.fetchone()["id"]
                    cur.execute("UPDATE links_pagamento SET status = 'PAGO', transacao_id = %s WHERE id = %s",
                                (tid, link_id))
                    cur.execute("""INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados)
                                   VALUES (ctx_loja(), %s, 'pagamento_link_recebido', 'pedido', %s, %s::jsonb)""",
                                (link["criado_por"], link["pedido_id"],
                                 json.dumps({"link": link_id, "mercado_pago": pag_id, "valor": valor, "forma": forma})))
                from . import eventos
                eventos.pagamento_recebido(cur, link["pedido_id"], valor, "PAGO")
                return "PAGO"
            except psycopg.Error as e:
                problema = "O caixa recusou: " + mensagem_do_banco(e)
        cur.execute("UPDATE links_pagamento SET status = 'PAGO_CONFERIR', observacao = %s WHERE id = %s",
                    (problema[:300], link_id))
        from . import eventos
        eventos.pagamento_recebido(cur, link["pedido_id"], valor, "PAGO_CONFERIR")
        return "PAGO_CONFERIR"


def conferir_link(link_id: str) -> str:
    """Pergunta ao Mercado Pago se o link já foi pago (botão 'Conferir pagamento')."""
    with db.transacao() as cur:
        token = token_da_loja(cur)
    if not token:
        raise ErroProvedor("Pagamento online não configurado.")
    status = "ABERTO"
    for p in pagamentos_do_link(token, link_id):
        if p.get("status") == "approved":
            status = aplicar(link_id, p)
            break
    return status


# --------------------------------------------------------------------------- #
# Compra do site (sacola): um pagamento para várias peças + frete
# --------------------------------------------------------------------------- #
def aplicar_compra(compra_id: str, pag: dict) -> str:
    """Lança no caixa um pagamento APROVADO da sacola. Idempotente. Aluguel: entrada/total no pedido que já
    estava esperando; compra: o pedido de venda nasce aqui, já pago; frete: entrada avulsa. Qualquer recusa do
    banco → a compra fica "conferir" (nada lançado) e a equipe é avisada."""
    try:
        compra_id = str(uuid.UUID(compra_id))
    except ValueError:
        return "ABERTA"
    if pag.get("status") != "approved" or str(pag.get("external_reference")) != f"C-{compra_id}":
        return "ABERTA"
    pag_id = str(pag.get("id"))
    with db.transacao() as cur:
        cur.execute("SELECT * FROM compras_site WHERE id = %s FOR UPDATE", (compra_id,))
        compra = cur.fetchone()
        if not compra:
            return "ABERTA"
        if compra["status"] in ("PAGA", "CONFERIR"):
            return compra["status"]
        cur.execute("SELECT usuario_site() AS u")
        site_uid = str(cur.fetchone()["u"])
        cur.execute("SELECT set_config('app.usuario_id', %s, true)", (site_uid,))
        cur.execute("SELECT maquininha_id FROM pagamento_online")
        cfg = cur.fetchone() or {}
        valor = round(float(pag.get("transaction_amount") or 0) * 100)
        forma = forma_do_pagamento(pag)
        parcelas = int(pag.get("installments") or 1)
        cur.execute("""UPDATE compras_site SET pagamento_externo_id = %s, forma_paga = %s, pago_em = now() WHERE id = %s""",
                    (pag_id, forma, compra_id))
        problema = None
        if not forma:
            problema = "Forma de pagamento não reconhecida. Confira no Mercado Pago."
        elif valor != compra["valor_total_centavos"]:
            problema = (f"Valor pago ({brl(valor)}) diferente da compra ({brl(compra['valor_total_centavos'])}). "
                        "Confira no Mercado Pago.")
        pedidos_ok = []
        if not problema:
            try:
                with cur.connection.transaction():
                    cur.execute("""SELECT i.*, pr.sku, pr.nome FROM compras_itens i JOIN produtos pr ON pr.id = i.produto_id
                                    WHERE i.compra_id = %s ORDER BY i.id""", (compra_id,))
                    itens = cur.fetchall()
                    cur.execute("SELECT config_vigente(ctx_loja()) AS id")
                    config_id = cur.fetchone()["id"]

                    def lancar(valor_c, natureza, pedido_id, descricao, n):
                        cur.execute(
                            """INSERT INTO transacoes
                                 (loja_id, tipo, natureza, valor_centavos, forma_pagamento, parcelas, modalidade_cartao,
                                  maquininha_id, nsu, pedido_id, descricao, chave_idempotencia, usuario_id)
                               VALUES (ctx_loja(), 'ENTRADA', %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s)""",
                            (natureza, valor_c, forma, "EMISSOR" if forma == "CREDITO" else None,
                             cfg.get("maquininha_id") if forma in ("CREDITO", "DEBITO") else None, pag_id, pedido_id,
                             descricao + (f" (cliente parcelou em {parcelas}x)" if parcelas > 1 else ""),
                             str(uuid.uuid5(uuid.UUID(compra_id), f"{pag_id}:{n}")), site_uid))

                    for n, it in enumerate(itens):
                        pedido_id = str(it["pedido_id"]) if it["pedido_id"] else None
                        if it["tipo"] == "VENDA" and not pedido_id:
                            cur.execute(
                                """INSERT INTO pedidos (loja_id, tipo, com_ajuste, produto_id, cliente_id, config_id, status,
                                                        valor_peca_centavos, valor_ajuste_centavos, valor_total_centavos,
                                                        entrada_minima_centavos, max_parcelas_permitidas, data_retirada,
                                                        chave_idempotencia, criado_por)
                                   VALUES (ctx_loja(), 'VENDA', %s, %s, %s, %s, 'NOVO', %s, %s, 0, 0, 1, %s, %s, %s)
                                   RETURNING id::text""",
                                (it["ajuste_centavos"] > 0, str(it["produto_id"]), str(compra["cliente_id"]), config_id,
                                 it["valor_peca_centavos"], it["ajuste_centavos"],
                                 it["data_retirada"] if it["ajuste_centavos"] > 0 else None,
                                 str(uuid.uuid5(uuid.UUID(compra_id), str(it["produto_id"]))), site_uid))
                            pedido_id = cur.fetchone()["id"]
                            cur.execute("UPDATE compras_itens SET pedido_id = %s WHERE id = %s", (pedido_id, it["id"]))
                        natureza = ("PAGAMENTO_INTEGRAL"
                                    if it["valor_pagar_centavos"] >= it["valor_peca_centavos"] + it["ajuste_centavos"]
                                    else "ENTRADA_RESERVA")
                        lancar(it["valor_pagar_centavos"], natureza, pedido_id,
                               f"Site {'aluguel' if it['tipo'] == 'ALUGUEL' else 'compra'} {it['sku']}"
                               + (" com ajuste na loja" if it["ajuste_centavos"] else ""), n)
                        pedidos_ok.append(pedido_id)
                    if compra["frete_centavos"]:
                        lancar(compra["frete_centavos"], "FRETE", None,
                               f"Frete da compra do site ({'entrega local' if compra['entrega'] == 'LOCAL' else compra['frete_servico'] or 'envio'})",
                               len(itens))
                    cur.execute("UPDATE compras_site SET status = 'PAGA' WHERE id = %s", (compra_id,))
                    cur.execute("""INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados)
                                   VALUES (ctx_loja(), %s, 'compra_site_paga', 'compra', %s, %s::jsonb)""",
                                (site_uid, compra_id, json.dumps({"mercado_pago": pag_id, "valor": valor, "forma": forma})))
            except psycopg.Error as e:
                problema = "O caixa recusou: " + mensagem_do_banco(e)
                pedidos_ok = []
        from . import avisos, eventos
        if problema:
            cur.execute("UPDATE compras_site SET status = 'CONFERIR', observacao = %s WHERE id = %s", (problema[:300], compra_id))
            avisos.para_equipe(cur, "pagamentos", f"⚠ Compra do site paga ({brl(valor)}) precisa de conferência: {problema}",
                               "compra_conferir", f"compra-conferir:{compra_id}")
            return "CONFERIR"
        entrega = {"RETIRADA": "retirar na loja", "LOCAL": "entrega na cidade", "ENVIO": "envio"}[compra["entrega"]]
        avisos.para_equipe(cur, "pedidos", f"🛍 Compra pelo site paga: {len(pedidos_ok)} peça(s), {brl(valor)}, {entrega}. "
                                           "Veja em Pedidos → Do site.", "compra_site", f"compra-site:{compra_id}")
        for pid in pedidos_ok:
            eventos.pedido_criado(cur, pid, config.SITE_URL or "")
        return "PAGA"


def conferir_compra(compra_id: str) -> str:
    with db.transacao() as cur:
        token = token_da_loja(cur)
    if not token:
        return "ABERTA"
    for p in pagamentos_do_link(token, f"C-{compra_id}"):
        if p.get("status") == "approved":
            return aplicar_compra(compra_id, p)
    return "ABERTA"


# --------------------------------------------------------------------------- #
# Link pelo Pix da loja (qualquer banco: Inter, Itaú, Nubank…): a cliente paga o Pix com o valor certo
# e a loja confirma quando cair no extrato. Não precisa de API do banco.
# --------------------------------------------------------------------------- #
def criar_link_pix(cur, pedido: dict, valor_centavos: int, usuario_id: str) -> dict:
    from . import crediario, pix
    dados = crediario.pix_da_loja(cur)
    if not dados:
        raise ErroProvedor("Cadastre a chave Pix da loja em Crediário (ou Configurações) para gerar link Pix.")
    if valor_centavos < MINIMO_CENTAVOS:
        raise ErroProvedor("O valor do link precisa ser de pelo menos R$ 1,00.")
    link_id = str(uuid.uuid4())
    expira = datetime.now(timezone.utc) + timedelta(hours=HORAS_VALIDADE)
    copia = pix.br_code(dados["chave"], dados["nome"], dados["cidade"], valor_centavos,
                        "LK" + link_id.replace("-", "")[:20])
    cur.execute("""INSERT INTO links_pagamento (id, loja_id, pedido_id, valor_centavos, max_parcelas, provedor,
                                                pix_copia_cola, criado_por, expira_em)
                   VALUES (%s, ctx_loja(), %s, %s, 1, 'PIX_LOJA', %s, %s, %s)""",
                (link_id, pedido["id"], valor_centavos, copia, usuario_id, expira))
    return {"id": link_id, "pix": copia, "expira_em": expira}


def confirmar_pix(cur, link_id: str, usuario_id: str) -> bool:
    """A loja viu o Pix no extrato: lança no caixa (uma vez só) e marca o link como pago."""
    cur.execute("""SELECT l.*, l.id::text AS id, l.pedido_id::text AS pedido_id, pr.sku, p.status AS status_pedido
                     FROM links_pagamento l JOIN pedidos p ON p.id = l.pedido_id JOIN produtos pr ON pr.id = p.produto_id
                    WHERE l.id = %s AND l.provedor = 'PIX_LOJA' FOR UPDATE OF l""", (link_id,))
    link = cur.fetchone()
    if not link or link["status"] != "ABERTO":
        return False
    natureza = "SALDO" if link["status_pedido"] != "AGUARDANDO_PAGAMENTO" else "ENTRADA_RESERVA"
    cur.execute("""INSERT INTO transacoes (loja_id, tipo, natureza, valor_centavos, forma_pagamento, parcelas, pedido_id,
                                           descricao, chave_idempotencia, usuario_id)
                   VALUES (ctx_loja(), 'ENTRADA', %s, %s, 'PIX', 1, %s, %s, %s, %s) RETURNING id""",
                (natureza, link["valor_centavos"], link["pedido_id"], f"Link Pix da loja {link['sku']}",
                 str(uuid.uuid5(uuid.UUID(link_id), "pix-loja")), usuario_id))
    tid = cur.fetchone()["id"]
    cur.execute("""UPDATE links_pagamento SET status = 'PAGO', transacao_id = %s, forma_paga = 'PIX', pago_em = now()
                    WHERE id = %s""", (tid, link_id))
    from . import eventos
    eventos.pagamento_recebido(cur, link["pedido_id"], link["valor_centavos"], "PAGO")
    return True
