"""Trabalhador dos avisos: envia a fila de WhatsApp/e-mail e roda as rotinas do dia.

Roda num container próprio (mesma imagem do app):  python -m app.trabalhador
  * a cada 15 s: envia mensagens pendentes (com nova tentativa em 1, 2, 4, 8, 16 min)
  * a cada minuto: lembretes da véspera, agenda do dia, fila da costura, relatório do caixa,
    e libera peças presas por link de pagamento vencido.
"""
import logging
import smtplib
import ssl
import time
from datetime import date, datetime, time as hora, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import avisos, cripto, db, vitrine, whatsapp
from .web import brl, data_br

log = logging.getLogger("loja")
FUSO = ZoneInfo("America/Sao_Paulo")
SMTP_HOST, SMTP_PORTA = "smtp.gmail.com", 587
COMPROMISSO = {"PROVA_1": "1ª prova", "PROVA_2": "2ª prova", "RETIRADA": "retirada", "DEVOLUCAO": "devolução"}


# --------------------------------------------------------------------------- #
# Envio
# --------------------------------------------------------------------------- #
def enviar_email(cfg: dict, destino: str, assunto: str, texto: str, anexo: bytes | None, anexo_nome: str | None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["remetente"], destino, assunto or "Mensagem da loja"
    msg.set_content(texto)
    if anexo:
        msg.add_attachment(anexo, maintype="application", subtype="pdf", filename=anexo_nome or "documento.pdf")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORTA, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(cfg["remetente"], cfg["senha"])
        s.send_message(msg)


def email_config(cur) -> dict | None:
    cur.execute("SELECT remetente, senha_c FROM email_config WHERE ativo")
    cfg = cur.fetchone()
    if cfg:
        cfg["senha"] = cripto.decifrar(cfg.pop("senha_c"), "email.senha")
    return cfg


def enviar_pendentes(limite: int = 20) -> int:
    enviadas = 0
    with db.transacao() as cur:
        wpp = whatsapp.config_loja(cur)
        mail = email_config(cur)
        cur.execute("""SELECT * FROM mensagens WHERE status = 'PENDENTE' AND enviar_apos <= now()
                        ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (limite,))
        for m in cur.fetchall():
            destino = cripto.decifrar(m["destino_c"], "mensagens.destino")
            texto = cripto.decifrar(m["texto_c"], "mensagens.texto")
            anexo = cripto.decifrar_bytes(m["anexo_c"], "mensagens.anexo") if m["anexo_c"] else None
            try:
                if m["canal"] == "WHATSAPP":
                    if not wpp or not wpp["ativo"] or not wpp["token"]:
                        raise whatsapp.ErroWhatsApp("WhatsApp desconectado")
                    ident = whatsapp.enviar_texto(wpp, destino, texto)
                    if anexo:
                        whatsapp.enviar_arquivo(wpp, destino, m["anexo_nome"] or "documento.pdf", anexo)
                else:
                    if not mail:
                        raise RuntimeError("E-mail não configurado")
                    enviar_email(mail, destino, m["assunto"], texto, anexo, m["anexo_nome"])
                    ident = None
                cur.execute("""UPDATE mensagens SET status = 'ENVIADA', enviada_em = now(), id_externo = %s,
                                      tentativas = tentativas + 1, erro = NULL, anexo_c = NULL WHERE id = %s""",
                            (ident, m["id"]))
                enviadas += 1
            except (whatsapp.ErroWhatsApp, OSError, smtplib.SMTPException, RuntimeError) as e:
                falhou = m["tentativas"] + 1 >= 5
                cur.execute("""UPDATE mensagens SET tentativas = tentativas + 1, erro = %s,
                                      status = CASE WHEN %s THEN 'FALHOU' ELSE 'PENDENTE' END,
                                      enviar_apos = now() + make_interval(mins => power(2, tentativas)::int)
                                WHERE id = %s""", (str(e)[:200], falhou, m["id"]))
    return enviadas


# --------------------------------------------------------------------------- #
# Rotinas do dia
# --------------------------------------------------------------------------- #
def _loja(cur) -> dict:
    cur.execute("SELECT nome, relatorio_as FROM lojas WHERE id = ctx_loja()")
    return cur.fetchone()


def lembretes(cur, agora: datetime) -> int:
    """Véspera, a partir das 10h: lembra a cliente da prova, retirada ou devolução de amanhã."""
    if agora.time() < hora(10, 0):
        return 0
    amanha = agora.date() + timedelta(days=1)
    loja = _loja(cur)["nome"]
    cur.execute("""SELECT a.id::text, a.tipo, to_char(a.inicio AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS hora,
                          p.cliente_id::text, cl.nome, pr.nome AS peca
                     FROM agendamentos a JOIN pedidos p ON p.id = a.pedido_id JOIN produtos pr ON pr.id = p.produto_id
                     JOIN clientes cl ON cl.id = p.cliente_id
                    WHERE a.status IN ('AGENDADO','REMARCADO') AND p.status <> 'CANCELADO'
                      AND (a.inicio AT TIME ZONE 'America/Sao_Paulo')::date = %s""", (amanha,))
    n = 0
    for a in cur.fetchall():
        texto = (f"Olá, {avisos.primeiro_nome(a['nome'])}! Lembrete da {loja}: amanhã ({amanha:%d/%m}) às {a['hora']} "
                 f"é a sua {COMPROMISSO[a['tipo']]} ({a['peca']}). Se precisar mudar, responda esta mensagem. 💐")
        n += avisos.para_cliente(cur, a["cliente_id"], texto, "lembrete", f"lembrete:{a['id']}:{amanha}")
    cur.execute("""SELECT s.id::text, s.nome, s.telefone_c, to_char(s.agendada_para AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS hora
                     FROM solicitacoes_prova s
                    WHERE s.status = 'AGENDADA' AND (s.agendada_para AT TIME ZONE 'America/Sao_Paulo')::date = %s""", (amanha,))
    for s in cur.fetchall():
        fone = cripto.decifrar(s["telefone_c"], "solicitacoes.telefone")
        texto = (f"Olá, {avisos.primeiro_nome(s['nome'])}! Lembrete da {loja}: sua prova é amanhã ({amanha:%d/%m}) "
                 f"às {s['hora']}. Te esperamos! 💐")
        n += avisos.whatsapp(cur, fone, texto, "lembrete", f"lembrete-site:{s['id']}:{amanha}")
    return n


def agenda_do_dia(cur, agora: datetime) -> int:
    if agora.time() < hora(8, 0):
        return 0
    hoje = agora.date()
    cur.execute("""SELECT to_char(a.inicio AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS hora, a.tipo, cl.nome, pr.sku
                     FROM agendamentos a JOIN pedidos p ON p.id = a.pedido_id JOIN produtos pr ON pr.id = p.produto_id
                     LEFT JOIN clientes cl ON cl.id = p.cliente_id
                    WHERE a.status IN ('AGENDADO','REMARCADO') AND (a.inicio AT TIME ZONE 'America/Sao_Paulo')::date = %s
                    ORDER BY a.inicio""", (hoje,))
    itens = cur.fetchall()
    if not itens:
        return 0
    texto = f"📅 Agenda de hoje ({hoje:%d/%m}):\n" + "\n".join(
        f"{i['hora']} · {COMPROMISSO[i['tipo']]} · {i['nome'] or '—'} · {i['sku']}" for i in itens)
    return avisos.para_equipe(cur, "agenda", texto, "agenda_dia", f"agenda:{hoje}")


def fila_costura(cur, agora: datetime) -> int:
    if agora.time() < hora(8, 0):
        return 0
    hoje = agora.date()
    cur.execute("""SELECT pr.sku, pr.nome, p.data_retirada, p.status FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id
                    WHERE p.status IN ('EM_AJUSTE','PROVA_1') ORDER BY p.data_retirada NULLS LAST LIMIT 20""")
    itens = cur.fetchall()
    if not itens:
        return 0
    texto = f"✂ Costura de hoje ({hoje:%d/%m}):\n" + "\n".join(
        f"{i['sku']} · {i['nome']} · retirada {data_br(i['data_retirada'])}" for i in itens)
    return avisos.para_equipe(cur, "costura", texto, "costura_dia", f"costura:{hoje}")


def atrasos(cur, agora: datetime) -> int:
    """Peças que passaram da data de devolução: aviso diário para a cliente e resumo para a equipe."""
    cur.execute("SELECT atraso_aviso_hora FROM lojas WHERE id = ctx_loja()")
    if agora.time() < cur.fetchone()["atraso_aviso_hora"]:
        return 0
    hoje = agora.date()
    loja = _loja(cur)["nome"]
    cur.execute("""SELECT p.id::text, p.cliente_id::text, p.data_devolucao, cl.nome, pr.sku, pr.nome AS peca
                     FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id LEFT JOIN clientes cl ON cl.id = p.cliente_id
                    WHERE p.status = 'ALUGADO' AND p.data_devolucao < %s ORDER BY p.data_devolucao""", (hoje,))
    itens = cur.fetchall()
    n = 0
    for i in itens:
        dias = (hoje - i["data_devolucao"]).days
        texto = (f"Olá, {avisos.primeiro_nome(i['nome'])}! {loja}: a devolução da peça {i['peca']} ({i['sku']}) era "
                 f"em {data_br(i['data_devolucao'])} — {dias} dia{'s' if dias != 1 else ''} de atraso. "
                 "Por favor, traga hoje ou responda esta mensagem para combinarmos. Atrasos podem gerar cobrança (contrato, cláusula 6).")
        n += avisos.para_cliente(cur, i["cliente_id"], texto, "atraso", f"atraso:{i['id']}:{hoje}")
    if itens:
        resumo = f"⏰ Peças em atraso ({hoje:%d/%m}):\n" + "\n".join(
            f"{i['sku']} · {i['nome'] or '—'} · era {data_br(i['data_devolucao'])} ({(hoje - i['data_devolucao']).days}d)"
            for i in itens)
        n += avisos.para_equipe(cur, "pedidos", resumo, "atrasos", f"atrasos:{hoje}")
    cur.execute("""SELECT p.id::text, p.cliente_id::text, cl.nome, pr.nome AS peca FROM pedidos p
                     JOIN produtos pr ON pr.id = p.produto_id LEFT JOIN clientes cl ON cl.id = p.cliente_id
                    WHERE p.status = 'ALUGADO' AND p.data_devolucao = %s""", (hoje,))
    for i in cur.fetchall():
        n += avisos.para_cliente(cur, i["cliente_id"],
                                 f"Olá, {avisos.primeiro_nome(i['nome'])}! {loja}: hoje é o dia de devolver a peça {i['peca']}. "
                                 "Esperamos você no horário da loja. 💐", "devolucao_hoje", f"devolucao-hoje:{i['id']}")
    return n


def crediario(cur, agora: datetime) -> int:
    """Crediário da loja: lembrete 2 dias antes, no dia, e cobrança gentil com 1, 5, 10, 20 e 30 dias de atraso.
    Resumo das parcelas vencidas para quem recebe avisos de "pagamentos"."""
    cur.execute("SELECT atraso_aviso_hora FROM lojas WHERE id = ctx_loja()")
    if agora.time() < cur.fetchone()["atraso_aviso_hora"]:
        return 0
    hoje = agora.date()
    loja = _loja(cur)["nome"]
    cur.execute("""SELECT p.id, p.numero, p.valor_centavos, p.vencimento, c.parcelas, c.id::text AS cid,
                          c.cliente_id::text AS cliente_id, cl.nome
                     FROM crediario_parcelas p JOIN crediarios c ON c.id = p.crediario_id
                     JOIN clientes cl ON cl.id = c.cliente_id
                    WHERE p.paga_em IS NULL AND c.status = 'ATIVO' AND p.vencimento <= %s + 2
                    ORDER BY p.vencimento""", (hoje,))
    itens = cur.fetchall()
    n = 0
    vencidas = []
    for i in itens:
        dias = (hoje - i["vencimento"]).days
        base = (f"Olá, {avisos.primeiro_nome(i['nome'])}! {loja}: parcela {i['numero']}/{i['parcelas']} do seu carnê, "
                f"{brl(i['valor_centavos'])}")
        if dias == -2:
            texto = base + f", vence em {data_br(i['vencimento'])}. Pode pagar com o Pix do carnê ou na loja. 💐"
        elif dias == 0:
            texto = base + ", vence hoje. Pode pagar com o Pix do carnê ou na loja. Se já pagou, desconsidere. 💐"
        elif dias in (1, 5, 10, 20, 30):
            texto = base + (f", venceu em {data_br(i['vencimento'])}. Se já pagou, desconsidere; "
                            "se precisar, responda esta mensagem para combinarmos.")
        else:
            texto = None
        if texto:
            n += avisos.para_cliente(cur, i["cliente_id"], texto, "crediario", f"crediario:{i['id']}:{hoje}")
        if dias > 0:
            vencidas.append(i)
    if vencidas:
        resumo = f"📒 Crediário: {len(vencidas)} parcela{'s' if len(vencidas) != 1 else ''} vencida{'s' if len(vencidas) != 1 else ''} ({hoje:%d/%m}):\n" + "\n".join(
            f"{i['nome']} · {i['numero']}/{i['parcelas']} · {brl(i['valor_centavos'])} · venceu {data_br(i['vencimento'])}"
            for i in vencidas[:30])
        n += avisos.para_equipe(cur, "pagamentos", resumo, "crediario_vencido", f"crediario-vencido:{hoje}")
    return n


def resumo_caixa(cur, dia: date) -> str:
    cur.execute("""SELECT COALESCE(sum(valor_centavos) FILTER (WHERE tipo = 'ENTRADA'), 0) AS entrou,
                          COALESCE(sum(valor_centavos) FILTER (WHERE tipo = 'SAIDA'), 0) AS saiu,
                          COALESCE(sum(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END)
                                   FILTER (WHERE forma_pagamento = 'DINHEIRO'), 0) AS gaveta, count(*) AS n
                     FROM transacoes WHERE (criado_em AT TIME ZONE 'America/Sao_Paulo')::date = %s""", (dia,))
    c = cur.fetchone()
    cur.execute("""SELECT count(*) FILTER (WHERE (criado_em AT TIME ZONE 'America/Sao_Paulo')::date = %s) AS novos
                     FROM pedidos WHERE status <> 'CANCELADO'""", (dia,))
    p = cur.fetchone()
    cur.execute("""SELECT count(*) AS n FROM solicitacoes_prova
                    WHERE (criado_em AT TIME ZONE 'America/Sao_Paulo')::date = %s""", (dia,))
    s = cur.fetchone()
    return (f"📊 Caixa de {dia:%d/%m}\nEntrou {brl(c['entrou'])} · Saiu {brl(c['saiu'])}\n"
            f"Dinheiro na gaveta: {brl(c['gaveta'])}\nLançamentos: {c['n']} · Pedidos novos: {p['novos']} · "
            f"Pedidos de prova do site: {s['n']}")


def relatorio_do_dia(cur, agora: datetime) -> int:
    if agora.time() < _loja(cur)["relatorio_as"]:
        return 0
    return avisos.para_equipe(cur, "relatorio", resumo_caixa(cur, agora.date()), "relatorio", f"relatorio:{agora.date()}")


def rodar_rotinas(agora: datetime | None = None) -> None:
    agora = agora or datetime.now(FUSO)
    with db.transacao() as cur:
        cur.execute("SELECT liberar_pedidos_vencidos()")
        try:
            vitrine.atualizar_google(cur)
        except Exception:                      # nota do Google nunca derruba as rotinas
            log.exception("nota do Google")
        if avisos.whatsapp_ativo(cur):
            lembretes(cur, agora)
            agenda_do_dia(cur, agora)
            fila_costura(cur, agora)
            relatorio_do_dia(cur, agora)
            atrasos(cur, agora)
            crediario(cur, agora)


def principal() -> None:
    from . import logs
    logs.configurar()
    db.pool.open(wait=True, timeout=60)
    log.info("trabalhador de avisos iniciado", extra={"evento": "trabalhador"})
    ultima_rotina = 0.0
    while True:
        try:
            enviar_pendentes()
            if time.monotonic() - ultima_rotina > 60:
                rodar_rotinas()
                ultima_rotina = time.monotonic()
        except Exception:                                        # o trabalhador nunca para de vez
            log.exception("erro no trabalhador de avisos")
        time.sleep(15)


if __name__ == "__main__":
    principal()
