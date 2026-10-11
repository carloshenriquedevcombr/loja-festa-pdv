"""O que acontece na loja -> quem fica sabendo pelo WhatsApp (e e-mail).

Cada função é chamada DENTRO da transação que gravou o fato: se a gravação desfizer, o aviso some junto.
Se o WhatsApp não estiver ligado, nada acontece (as telas funcionam igual).
"""
from . import avisos
from .web import brl, data_br

ETAPA_CLIENTE = {
    "PRONTO": "Sua peça está pronta para retirar! 🎉 Retirada marcada para {retirada}.{falta}",
    "ALUGADO": "Obrigada pela preferência! Aproveite a festa ✨ Devolução até {devolucao}, no horário da loja.",
    "RETIRADO": "Obrigada pela compra! Aproveite muito ✨",
    "DEVOLVIDO": "Recebemos a peça de volta. Obrigada! Esperamos você na próxima festa 💐",
    "PROVA_2": "O ajuste da sua peça ficou pronto! Venha para a 2ª prova{prova2}.",
}
ETAPA_EQUIPE = {
    "EM_AJUSTE": ("costura", "✂ Para ajustar: {sku} · {peca} · {cliente}. Retirada {retirada}."),
    "DEVOLVIDO": ("limpeza", "🧺 Para a limpeza: {sku} · {peca} (devolvida por {cliente})."),
    "PRONTO": ("pedidos", "✅ Pronto para retirar: {sku} · {cliente} · retirada {retirada}."),
}


def _dados(cur, pedido_id: str) -> dict | None:
    cur.execute("""SELECT pe.id::text AS id, pe.tipo, pe.com_ajuste, pe.status, pe.data_retirada, pe.data_devolucao,
                          pe.valor_total_centavos, pe.token_publico, pe.cliente_id::text AS cliente_id,
                          valor_pago(pe.id) AS pago, pe.valor_total_centavos - valor_pago(pe.id) AS saldo,
                          pr.sku, pr.nome AS peca, cl.nome AS cliente, l.nome AS loja
                     FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id JOIN lojas l ON l.id = pe.loja_id
                     LEFT JOIN clientes cl ON cl.id = pe.cliente_id WHERE pe.id = %s""", (pedido_id,))
    return cur.fetchone()


def _campos(p: dict) -> dict:
    return {"sku": p["sku"], "peca": p["peca"], "cliente": p["cliente"] or "venda no balcão",
            "retirada": data_br(p["data_retirada"]) if p["data_retirada"] else "—",
            "devolucao": data_br(p["data_devolucao"]) if p["data_devolucao"] else "—",
            "falta": f" Falta pagar {brl(p['saldo'])}." if p["saldo"] > 0 else ""}


def link_cliente(p: dict, base: str = "") -> str:
    return avisos.link(f"/site/meu-pedido/{p['token_publico']}", base)


def pedido_criado(cur, pedido_id: str, base: str = "", link_pagamento: str | None = None) -> None:
    p = _dados(cur, pedido_id)
    if not p:
        return
    c = _campos(p)
    tipo = "Aluguel" if p["tipo"] == "ALUGUEL" else "Venda"
    avisos.para_equipe(cur, "pedidos", f"🛍 Pedido novo na loja: {tipo} {c['sku']} · {c['peca']} · {c['cliente']} · "
                                       f"total {brl(p['valor_total_centavos'])}", "pedido_novo", f"pedido:{pedido_id}")
    if not p["cliente_id"]:
        return
    texto = avisos.texto_pedido_cliente(p, p["loja"], link_cliente(p, base))
    if link_pagamento:
        texto += f"\n\nPara confirmar a reserva, pague pelo link (Pix ou cartão): {link_pagamento}"
    avisos.para_cliente(cur, p["cliente_id"], texto, "pedido_cliente", f"pedido-cliente:{pedido_id}")


def etapa(cur, pedido_id: str, para: str, base: str = "") -> None:
    p = _dados(cur, pedido_id)
    if not p:
        return
    c = _campos(p)
    if para in ETAPA_EQUIPE:
        etiqueta, modelo = ETAPA_EQUIPE[para]
        avisos.para_equipe(cur, etiqueta, modelo.format(**c), f"etapa_{para.lower()}", None)
    if para in ETAPA_CLIENTE and p["cliente_id"]:
        prova2 = ""
        if para == "PROVA_2":
            cur.execute("""SELECT to_char(inicio AT TIME ZONE 'America/Sao_Paulo', 'DD/MM "às" HH24:MI') AS q
                             FROM agendamentos WHERE pedido_id = %s AND tipo = 'PROVA_2'
                              AND status IN ('AGENDADO','REMARCADO') ORDER BY inicio LIMIT 1""", (pedido_id,))
            linha = cur.fetchone()
            prova2 = f", marcada para {linha['q']}" if linha else " (combine o dia com a loja)"
        texto = (f"Olá, {avisos.primeiro_nome(p['cliente'])}! {p['loja']}: "
                 + ETAPA_CLIENTE[para].format(**c, prova2=prova2) + f"\nSeu pedido: {link_cliente(p, base)}")
        if para in ("DEVOLVIDO", "RETIRADO"):
            cur.execute("SELECT avaliacoes_no_site FROM lojas WHERE id = ctx_loja()")
            if (cur.fetchone() or {}).get("avaliacoes_no_site"):
                from . import vitrine
                texto += ("\n\nConta pra gente como foi? Leva 1 minuto: "
                          + avisos.link(f"/site/avaliar/{vitrine.token_avaliar(pedido_id)}", base))
        avisos.para_cliente(cur, p["cliente_id"], texto, f"etapa_{para.lower()}", f"etapa:{pedido_id}:{para}")


def cancelado(cur, pedido_id: str, motivo: str) -> None:
    p = _dados(cur, pedido_id)
    if not p:
        return
    c = _campos(p)
    avisos.para_equipe(cur, "pedidos", f"❌ Pedido cancelado: {c['sku']} · {c['cliente']}. Motivo: {motivo}",
                       "pedido_cancelado", f"cancelado:{pedido_id}")
    if p["cliente_id"]:
        cur.execute("SELECT cancel_devolver_centavos AS d FROM pedidos WHERE id = %s", (pedido_id,))
        devolver = (cur.fetchone() or {}).get("d") or 0
        extra = f" Você vai receber de volta {brl(devolver)}." if devolver else ""
        avisos.para_cliente(cur, p["cliente_id"],
                            f"Olá, {avisos.primeiro_nome(p['cliente'])}. {p['loja']}: seu pedido da peça {c['sku']} "
                            f"foi cancelado.{extra} Qualquer dúvida, é só responder esta mensagem.",
                            "pedido_cancelado", f"cancelado-cliente:{pedido_id}")


def remarcado(cur, pedido_id: str, compromisso: str, quando: str, agendamento_id: str) -> None:
    p = _dados(cur, pedido_id)
    if not p or not p["cliente_id"]:
        return
    avisos.para_cliente(cur, p["cliente_id"],
                        f"Olá, {avisos.primeiro_nome(p['cliente'])}! {p['loja']}: sua {compromisso} ({p['sku']}) "
                        f"ficou para {quando}. Se não puder, responda esta mensagem.",
                        "remarcado", f"remarcado:{agendamento_id}:{quando}")


def pagamento_recebido(cur, pedido_id: str, valor_centavos: int, status_link: str) -> None:
    p = _dados(cur, pedido_id)
    if not p:
        return
    if status_link == "PAGO_CONFERIR":
        avisos.para_equipe(cur, "pagamentos", f"⚠ Pagamento pelo link de {p['sku']} ({p['cliente'] or '—'}) precisa de "
                                              "conferência. Abra o pedido no sistema.", "pagamento_conferir",
                           f"conferir:{pedido_id}:{valor_centavos}")
        return
    avisos.para_equipe(cur, "pagamentos", f"💰 Pagamento recebido pelo link: {brl(valor_centavos)} · {p['sku']} · "
                                          f"{p['cliente'] or '—'}", "pagamento_link", f"pago:{pedido_id}:{valor_centavos}:{p['pago']}")
    if p["cliente_id"]:
        falta = f" Falta {brl(p['saldo'])}." if p["saldo"] > 0 else " Pedido quitado ✅"
        avisos.para_cliente(cur, p["cliente_id"],
                            f"{p['loja']}: recebemos seu pagamento de {brl(valor_centavos)}. Obrigada!{falta}\n"
                            f"Seu pedido: {link_cliente(p)}", "pagamento_cliente",
                            f"pago-cliente:{pedido_id}:{p['pago']}")


def prova_pedida_no_site(cur, nome: str, peca: str | None, desejada, periodo: str) -> None:
    avisos.para_equipe(cur, "site", f"🌐 Pedido de prova pelo site: {nome}"
                                    + (f" · {peca}" if peca else "")
                                    + f" · quer vir {data_br(desejada)} ({'manhã' if periodo == 'MANHA' else 'tarde'}). "
                                      "Abra 'Pedidos do site' para marcar.", "site_prova", None)


def prova_marcada(cur, cliente_id: str, nome: str, dia, hora, solicitacao_id: str) -> None:
    cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
    loja = cur.fetchone()["nome"]
    avisos.para_cliente(cur, cliente_id,
                        f"Olá, {avisos.primeiro_nome(nome)}! {loja}: sua prova está marcada para {dia:%d/%m} às "
                        f"{hora:%H:%M}. Te esperamos! 💐 Se não puder vir, responda esta mensagem.",
                        "prova_marcada", f"prova-site:{solicitacao_id}")
