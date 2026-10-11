"""Pedidos de prova que chegaram pelo site: ver, chamar no WhatsApp, marcar horário ou arquivar."""
import re
from datetime import date, time
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import cripto, db, pessoas
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import TZ, checar_csrf, tela

router = APIRouter()
PERIODO = {"MANHA": "manhã", "TARDE": "tarde"}


def _lista(cur, status: tuple):
    cur.execute(f"""
        SELECT s.id::text, s.nome, s.telefone_c, s.data_evento, s.data_desejada, s.periodo, s.mensagem_c, s.status,
               to_char(s.criado_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS chegou,
               to_char(s.agendada_para AT TIME ZONE '{TZ}', 'DD/MM/YYYY HH24:MI') AS quando,
               p.id::text AS produto_id, p.sku, p.nome AS peca, p.slug, s.cliente_id::text AS cliente_id,
               (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = p.id AND f.ativa
                 ORDER BY f.ordem LIMIT 1) AS capa, l.nome AS loja
          FROM solicitacoes_prova s LEFT JOIN produtos p ON p.id = s.produto_id JOIN lojas l ON l.id = s.loja_id
         WHERE s.status = ANY(%s)
         ORDER BY CASE WHEN s.status = 'NOVA' THEN s.criado_em END DESC, s.agendada_para DESC NULLS LAST
         LIMIT 100""", (list(status),))
    itens = cur.fetchall()
    for s in itens:
        s["telefone"] = cripto.decifrar(s.pop("telefone_c"), "solicitacoes.telefone")
        s["mensagem"] = cripto.decifrar(s.pop("mensagem_c"), "solicitacoes.mensagem")
        primeiro = s["nome"].split()[0]
        texto = (f"Olá, {primeiro}! Aqui é da {s['loja']}. Recebemos seu pedido de prova pelo site"
                 + (f" da peça {s['peca']} ({s['sku']})" if s["sku"] else "")
                 + f" para {s['data_desejada']:%d/%m} de {PERIODO[s['periodo']]}. Que horário fica bom para você?")
        s["whatsapp"] = f"https://wa.me/55{s['telefone']}?text={quote(texto)}"
    return itens


@router.get("/solicitacoes")
def lista(request: Request, ok: str = "", usuario: dict = Depends(requer("site.solicitacoes"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        novas = _lista(cur, ("NOVA",))
        tratadas = _lista(cur, ("AGENDADA", "RECUSADA"))[:20]
    return tela(request, "solicitacoes.html", usuario, novas=novas, tratadas=tratadas, ok=ok, PERIODO=PERIODO,
                hoje=date.today())


@router.post("/solicitacoes/{sid}")
def tratar(request: Request, sid: UUID, usuario: dict = Depends(requer("site.solicitacoes")),
           form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    acao = form.get("acao")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT * FROM solicitacoes_prova WHERE id = %s AND status = 'NOVA' FOR UPDATE", (sid,))
        s = cur.fetchone()
        if not s:
            return RedirectResponse("/solicitacoes", status_code=303)
        if acao == "recusar":
            cur.execute("""UPDATE solicitacoes_prova SET status = 'RECUSADA', tratada_por = %s, tratada_em = now()
                            WHERE id = %s""", (usuario["id"], sid))
            auditar(cur, request, "solicitacao_arquivada", usuario["id"], "solicitacao", str(sid))
            return RedirectResponse(f"/solicitacoes?ok={quote('Pedido arquivado.')}", status_code=303)
        try:
            dia = date.fromisoformat(form.get("dia") or "")
            hora = time.fromisoformat(form.get("hora") or "")
        except ValueError:
            return RedirectResponse(f"/solicitacoes?ok={quote('Escolha o dia e a hora para marcar.')}", status_code=303)
        # A pessoa vira cliente (autorizou o contato no site); se o telefone já existe, usa a ficha
        fone = cripto.decifrar(s["telefone_c"], "solicitacoes.telefone")
        cli = pessoas.achar_por_telefone(cur, fone)
        if cli:
            cliente_id = cli["id"]
        else:
            cliente_id = pessoas.gravar_cliente(cur, {"nome": s["nome"], "telefone": fone, "observacoes": "Veio pelo site"},
                                                consentimento=True, consentimento_em=s["consentimento_em"])
        cur.execute("""UPDATE solicitacoes_prova SET status = 'AGENDADA', cliente_id = %s, tratada_por = %s,
                              tratada_em = now(), agendada_para = (%s::date + %s::time) AT TIME ZONE 'America/Sao_Paulo'
                        WHERE id = %s""", (cliente_id, usuario["id"], dia, hora, sid))
        from .. import eventos
        eventos.prova_marcada(cur, cliente_id, s["nome"], dia, hora, str(sid))
        auditar(cur, request, "solicitacao_agendada", usuario["id"], "solicitacao", str(sid), {"para": f"{dia} {hora}"})
    recado = f"Prova marcada para {dia:%d/%m} às {hora:%H:%M}. Está na Agenda."
    return RedirectResponse(f"/solicitacoes?ok={quote(recado)}", status_code=303)
