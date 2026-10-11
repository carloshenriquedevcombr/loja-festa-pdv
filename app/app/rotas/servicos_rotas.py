"""Serviços avulsos (limpeza, conserto, taxas): o motivo cadastrado de todo dinheiro que entra sem pedido."""
import re
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import checar_csrf, ler_reais, tela

router = APIRouter()


def _tela(request, usuario, erro=None, ok="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT s.id::text, s.nome, s.preco_centavos, s.descricao, s.ativo,
                              (SELECT count(*) FROM transacoes t WHERE t.servico_id = s.id) AS usos
                         FROM servicos s ORDER BY s.ativo DESC, s.nome""")
        itens = cur.fetchall()
    return tela(request, "servicos.html", usuario, itens=itens, erro=erro, ok=ok or request.query_params.get("ok", ""),
                status=status)


@router.get("/servicos")
def ver(request: Request, usuario: dict = Depends(requer("servicos.editar"))):
    return _tela(request, usuario)


def _ler(form):
    nome = re.sub(r"\s+", " ", (form.get("nome") or "").strip())[:80]
    preco = ler_reais(form.get("preco")) if (form.get("preco") or "").strip() else None
    return {"nome": nome, "preco": preco, "descricao": (form.get("descricao") or "").strip()[:200] or None,
            "ativo": form.get("ativo", "1") == "1"}


@router.post("/servicos")
def novo(request: Request, usuario: dict = Depends(requer("servicos.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d = _ler(form)
    if len(d["nome"]) < 2:
        return _tela(request, usuario, erro="Escreva o nome do serviço.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""INSERT INTO servicos (loja_id, nome, preco_centavos, descricao)
                           VALUES (ctx_loja(), %(nome)s, %(preco)s, %(descricao)s)""", d)
            auditar(cur, request, "servico_criado", usuario["id"], "servico", None, {"nome": d["nome"]})
    except psycopg.Error as e:
        return _tela(request, usuario, erro="Já existe um serviço com esse nome." if "uq_servico_nome" in str(e)
                     else "Não consegui guardar.", status=400)
    return RedirectResponse("/servicos?ok=Serviço+cadastrado", status_code=303)


@router.post("/servicos/{sid}")
def editar(request: Request, sid: UUID, usuario: dict = Depends(requer("servicos.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d = _ler(form)
    if len(d["nome"]) < 2:
        return _tela(request, usuario, erro="Escreva o nome do serviço.", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE servicos SET nome = %(nome)s, preco_centavos = %(preco)s, descricao = %(descricao)s,
                              ativo = %(ativo)s WHERE id = %(id)s""", {**d, "id": str(sid)})
        auditar(cur, request, "servico_alterado", usuario["id"], "servico", str(sid))
    return RedirectResponse("/servicos?ok=Guardado", status_code=303)
