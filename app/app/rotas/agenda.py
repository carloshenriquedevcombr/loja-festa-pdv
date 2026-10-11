"""Agenda: provas, retiradas e devoluções dos próximos dias; lembrete por WhatsApp; remarcar."""
from datetime import date, time, timedelta
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import TZ, checar_csrf, mensagem_do_banco, tela
from .pedidos import COMPROMISSOS as COMPROMISSOS_PEDIDO

router = APIRouter()
COMPROMISSOS = {**COMPROMISSOS_PEDIDO, "VISITA": "Prova (veio pelo site)"}
ABERTOS = "('AGENDADO','REMARCADO','FALTOU')"
DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

SQL_ITENS = f"""
    SELECT a.id, a.tipo, a.status, a.observacao, a.pedido_id::text AS pedido_id,
           (a.inicio AT TIME ZONE '{TZ}')::date AS dia,
           to_char(a.inicio AT TIME ZONE '{TZ}', 'HH24:MI') AS hora,
           cl.id::text AS cliente_id, cl.nome AS cliente, cl.telefone_c, pr.sku, pr.nome AS peca, l.nome AS loja
      FROM agendamentos a
      JOIN pedidos pe ON pe.id = a.pedido_id
      JOIN produtos pr ON pr.id = pe.produto_id
      JOIN lojas l ON l.id = a.loja_id
      LEFT JOIN clientes cl ON cl.id = pe.cliente_id
"""


def whatsapp(item) -> str | None:
    """Link que abre o WhatsApp com a mensagem de lembrete pronta."""
    fone = "".join(c for c in (item["telefone"] or "") if c.isdigit())
    if len(fone) not in (10, 11):
        return None
    texto = (f"Olá, {item['cliente'].split()[0]}! Aqui é da {item['loja']}. Lembrando da sua "
             f"{'prova' if item['tipo'] == 'VISITA' else COMPROMISSOS[item['tipo']].lower()} em {item['dia']:%d/%m} às {item['hora']}. Até lá!")
    return f"https://wa.me/55{fone}?text={quote(texto)}"


@router.get("/agenda")
def agenda(request: Request, dia: str = "", usuario: dict = Depends(requer("agenda.ver"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS hoje")
        hoje = cur.fetchone()["hoje"]
        try:
            inicio = date.fromisoformat(dia) if dia else hoje
        except ValueError:
            inicio = hoje
        fim = inicio + timedelta(days=7)
        cur.execute(SQL_ITENS + f"""
             WHERE (a.inicio AT TIME ZONE '{TZ}')::date >= %s AND (a.inicio AT TIME ZONE '{TZ}')::date < %s
               AND a.status <> 'CANCELADO'
             ORDER BY a.inicio""", (inicio, fim))
        itens = cur.fetchall()
        cur.execute(SQL_ITENS + f"""
             WHERE a.inicio < date_trunc('day', now() AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}'
               AND a.status IN {ABERTOS}
               AND pe.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
             ORDER BY a.inicio""")
        atrasados = cur.fetchall()
        # Provas marcadas a partir dos pedidos do site (ainda sem pedido na loja)
        cur.execute(f"""
            SELECT s.id::text AS solicitacao, 'VISITA' AS tipo, 'AGENDADO' AS status, s.mensagem_c AS observacao_c,
                   NULL AS pedido_id, (s.agendada_para AT TIME ZONE '{TZ}')::date AS dia,
                   to_char(s.agendada_para AT TIME ZONE '{TZ}', 'HH24:MI') AS hora, s.cliente_id::text AS cliente_id,
                   s.nome AS cliente, s.telefone_c AS telefone_sol_c, pr.sku, pr.nome AS peca, l.nome AS loja
              FROM solicitacoes_prova s LEFT JOIN produtos pr ON pr.id = s.produto_id JOIN lojas l ON l.id = s.loja_id
             WHERE s.status = 'AGENDADA' AND (s.agendada_para AT TIME ZONE '{TZ}')::date >= %s
               AND (s.agendada_para AT TIME ZONE '{TZ}')::date < %s""", (inicio, fim))
        itens = sorted(itens + cur.fetchall(), key=lambda i: (i["dia"], i["hora"]))
    from .. import cripto
    for it in itens + atrasados:
        if "telefone_sol_c" in it:
            it["telefone"] = cripto.decifrar(it.pop("telefone_sol_c"), "solicitacoes.telefone")
            it["observacao"] = cripto.decifrar(it.pop("observacao_c"), "solicitacoes.mensagem")
        else:
            cripto.abrir(it, "clientes", "telefone")
    dias = {}
    for it in itens + atrasados:
        it["whatsapp"] = whatsapp(it)
    for it in itens:
        dias.setdefault(it["dia"], []).append(it)
    return tela(request, "agenda.html", usuario, dias=dias, atrasados=atrasados, hoje=hoje, inicio=inicio,
                anterior=inicio - timedelta(days=7), seguinte=fim, COMPROMISSOS=COMPROMISSOS, DIAS_SEMANA=DIAS_SEMANA)


def _item(cur, aid):
    from .. import cripto
    cur.execute(SQL_ITENS + " WHERE a.id = %s", (aid,))
    return cripto.abrir(cur.fetchone(), "clientes", "telefone")


@router.get("/agenda/{aid}/remarcar")
def remarcar_form(request: Request, aid: UUID, usuario: dict = Depends(requer("agenda.editar"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        it = _item(cur, aid)
    if not it:
        return RedirectResponse("/agenda", status_code=303)
    return tela(request, "remarcar.html", usuario, it=it, v={"dia": it["dia"].isoformat(), "hora": it["hora"]},
                COMPROMISSOS=COMPROMISSOS)


@router.post("/agenda/{aid}/remarcar")
def remarcar(request: Request, aid: UUID, usuario: dict = Depends(requer("agenda.editar")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    erro = None
    try:
        novo_dia = date.fromisoformat(form.get("dia") or "")
        nova_hora = time.fromisoformat(form.get("hora") or "10:00")
    except ValueError:
        novo_dia, nova_hora, erro = None, None, "Escolha o dia e a hora (ex.: 14:30)."
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            it = _item(cur, aid)
            if not it:
                return RedirectResponse("/agenda", status_code=303)
            cur.execute("SELECT * FROM pedidos WHERE id = %s", (it["pedido_id"],))
            pe = cur.fetchone()
            cur.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS hoje")
            hoje = cur.fetchone()["hoje"]
            if not erro and it["status"] in ("FEITO", "CANCELADO"):
                erro = "Este compromisso já foi feito ou cancelado."
            elif not erro and novo_dia < hoje:
                erro = "Escolha hoje ou um dia depois de hoje."
            elif not erro and it["tipo"] in ("PROVA_1", "PROVA_2") and novo_dia > pe["data_retirada"]:
                erro = "A prova tem que ser antes da retirada. Se a retirada mudou, remarque a retirada primeiro."
            elif not erro and it["tipo"] == "RETIRADA" and pe["data_devolucao"] and novo_dia > pe["data_devolucao"]:
                erro = "A retirada não pode ser depois da devolução. Remarque a devolução primeiro."
            elif not erro and it["tipo"] == "DEVOLUCAO" and novo_dia < pe["data_retirada"]:
                erro = "A devolução não pode ser antes da retirada."
            if not erro and it["tipo"] == "RETIRADA":
                cur.execute("""SELECT count(*) AS n FROM agendamentos WHERE pedido_id = %s AND tipo LIKE 'PROVA%%'
                                  AND status <> 'CANCELADO' AND (inicio AT TIME ZONE 'America/Sao_Paulo')::date > %s""",
                            (it["pedido_id"], novo_dia))
                if cur.fetchone()["n"]:
                    erro = "Há prova marcada depois desse dia. Remarque as provas primeiro."
            if not erro:
                cur.execute("""UPDATE agendamentos SET inicio = (%s::date + %s::time) AT TIME ZONE 'America/Sao_Paulo',
                                      status = 'REMARCADO', observacao = %s WHERE id = %s""",
                            (novo_dia, nova_hora, (form.get("observacao") or "").strip()[:200] or None, aid))
                if it["tipo"] == "RETIRADA":
                    cur.execute("UPDATE pedidos SET data_retirada = %s WHERE id = %s", (novo_dia, it["pedido_id"]))
                elif it["tipo"] == "DEVOLUCAO":
                    cur.execute("UPDATE pedidos SET data_devolucao = %s WHERE id = %s", (novo_dia, it["pedido_id"]))
                # A reserva da peça acompanha a agenda (o banco recusa se bater com outro pedido)
                cur.execute(f"""
                    UPDATE pedidos p
                       SET inicio_bloqueio = x.primeiro,
                           periodo_bloqueado = daterange(x.primeiro,
                               CASE WHEN p.tipo = 'ALUGUEL' THEN p.data_devolucao + c.dias_limpeza ELSE p.data_retirada END, '[]')
                      FROM config_pagamento c,
                           (SELECT min((inicio AT TIME ZONE '{TZ}')::date) AS primeiro FROM agendamentos
                             WHERE pedido_id = %s AND status <> 'CANCELADO') x
                     WHERE c.id = p.config_id AND p.id = %s AND p.periodo_bloqueado IS NOT NULL""",
                            (it["pedido_id"], it["pedido_id"]))
                from .. import eventos
                eventos.remarcado(cur, str(it["pedido_id"]), COMPROMISSOS[it["tipo"]].lower(),
                                  f"{novo_dia:%d/%m} às {nova_hora:%H:%M}", str(aid))
                auditar(cur, request, "compromisso_remarcado", usuario["id"], "agendamento", str(aid),
                        {"tipo": it["tipo"], "para": f"{novo_dia} {nova_hora}"})
    except psycopg.Error as e:
        erro = mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            it = _item(cur, aid)
        return tela(request, "remarcar.html", usuario, it=it, v=form, erro=erro, COMPROMISSOS=COMPROMISSOS, status=400)
    return RedirectResponse(f"/pedidos/{it['pedido_id']}", status_code=303)


@router.post("/agenda/{aid}/faltou")
def faltou(request: Request, aid: UUID, usuario: dict = Depends(requer("agenda.editar")),
           form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE agendamentos SET status = 'FALTOU' WHERE id = %s AND status IN ('AGENDADO','REMARCADO')
                       RETURNING pedido_id::text""", (aid,))
        linha = cur.fetchone()
        if linha:
            auditar(cur, request, "cliente_faltou", usuario["id"], "agendamento", str(aid))
    return RedirectResponse(form.get("volta") if (form.get("volta") or "").startswith("/agenda") else "/agenda",
                            status_code=303)
