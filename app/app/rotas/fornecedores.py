"""Fornecedores (costureira, lavanderia, tecidos...): cadastro simples, nada é apagado (ativo = false)."""
import re
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import TZ, checar_csrf, mensagem_do_banco, tela
from .caixa import CATEGORIAS
from .clientes import cpf_valido

router = APIRouter()


def cnpj_valido(c: str) -> bool:
    if len(c) != 14 or c == c[0] * 14:
        return False
    for tam in (12, 13):
        pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2] if tam == 12 else [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        resto = sum(int(c[i]) * pesos[i] for i in range(tam)) % 11
        if int(c[tam]) != (0 if resto < 2 else 11 - resto):
            return False
    return True


def _ler(form: dict) -> tuple[dict, str | None]:
    d = {"nome": (form.get("nome") or "").strip()[:120],
         "documento": re.sub(r"\D", "", form.get("documento") or "") or None,
         "telefone": re.sub(r"\D", "", form.get("telefone") or "") or None,
         "email": (form.get("email") or "").strip()[:120] or None,
         "pix": (form.get("pix") or "").strip()[:120] or None,
         "categoria": (form.get("categoria") or "").strip() or None,
         "observacoes": (form.get("observacoes") or "").strip()[:500] or None,
         "ativo": form.get("ativo", "1") == "1"}
    if d["categoria"] not in (None, *CATEGORIAS):
        d["categoria"] = None
    if len(d["nome"]) < 2:
        return d, "Escreva o nome do fornecedor."
    if d["documento"] and not (cpf_valido(d["documento"]) or cnpj_valido(d["documento"])):
        return d, "CPF ou CNPJ inválido (confira os números)."
    if d["telefone"] and not re.fullmatch(r"\d{10,11}", d["telefone"]):
        return d, "Telefone com DDD, só números (10 ou 11)."
    if d["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", d["email"]):
        return d, "E-mail inválido."
    return d, None


@router.get("/fornecedores")
def lista(request: Request, q: str = "", usuario: dict = Depends(requer("fornecedores.editar", "caixa.saida"))):
    q = q.strip()[:60]
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"""SELECT f.id::text AS id, f.nome, f.categoria, f.telefone, f.ativo,
                               COALESCE(sum(CASE WHEN t.tipo = 'SAIDA' THEN t.valor_centavos ELSE -t.valor_centavos END)
                                        FILTER (WHERE t.criado_em > now() - interval '12 months'), 0) AS pago_12m,
                               to_char(max(t.criado_em) AT TIME ZONE '{TZ}', 'DD/MM/YYYY') AS ultimo
                          FROM fornecedores f LEFT JOIN transacoes t ON t.fornecedor_id = f.id
                         WHERE %(q)s = '' OR f_unaccent(lower(f.nome)) LIKE f_unaccent(lower(%(like)s))
                            OR f.documento = %(dig)s
                         GROUP BY f.id ORDER BY f.ativo DESC, f.nome LIMIT 300""",
                    {"q": q, "like": f"%{q}%", "dig": re.sub(r"\D", "", q) or "#"})
        itens = cur.fetchall()
    return tela(request, "fornecedores_lista.html", usuario, itens=itens, q=q)


@router.get("/fornecedores/novo")
def novo_form(request: Request, usuario: dict = Depends(requer("fornecedores.editar"))):
    return tela(request, "fornecedor_form.html", usuario, v={"ativo": True}, novo=True, CATEGORIAS=CATEGORIAS)


@router.post("/fornecedores/novo")
def novo(request: Request, usuario: dict = Depends(requer("fornecedores.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler(form)
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                cur.execute("""INSERT INTO fornecedores (loja_id, nome, documento, telefone, email, pix, categoria, observacoes)
                               VALUES (ctx_loja(), %(nome)s, %(documento)s, %(telefone)s, %(email)s, %(pix)s,
                                       %(categoria)s, %(observacoes)s) RETURNING id::text""", d)
                fid = cur.fetchone()["id"]
                auditar(cur, request, "fornecedor_criado", usuario["id"], "fornecedor", fid)
            return RedirectResponse(f"/fornecedores/{fid}?ok=1", status_code=303)
        except psycopg.Error as e:
            erro = "Já existe fornecedor com esse CPF/CNPJ." if "uq_fornecedor_documento" in str(e) else mensagem_do_banco(e)
    return tela(request, "fornecedor_form.html", usuario, v=d, novo=True, erro=erro, CATEGORIAS=CATEGORIAS, status=400)


def _carregar(cur, fid):
    cur.execute("SELECT *, id::text AS id FROM fornecedores WHERE id = %s", (fid,))
    return cur.fetchone()


@router.get("/fornecedores/{fid}")
def detalhe(request: Request, fid: UUID, ok: int = 0,
            usuario: dict = Depends(requer("fornecedores.editar", "caixa.saida"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        f = _carregar(cur, str(fid))
        if not f:
            return tela(request, "erro.html", usuario, status=404, titulo="Fornecedor não encontrado")
        cur.execute(f"""SELECT t.id, t.tipo, t.natureza, t.valor_centavos, t.forma_pagamento, t.parcelas, t.descricao,
                               t.documento, t.anexo, to_char(t.criado_em AT TIME ZONE '{TZ}', 'DD/MM/YYYY') AS quando
                          FROM transacoes t WHERE t.fornecedor_id = %s ORDER BY t.id DESC LIMIT 100""", (str(fid),))
        pagos = cur.fetchall()
    return tela(request, "fornecedor_form.html", usuario, v=f, f=f, pagos=pagos, ok=ok, CATEGORIAS=CATEGORIAS)


@router.post("/fornecedores/{fid}")
def salvar(request: Request, fid: UUID, usuario: dict = Depends(requer("fornecedores.editar")),
           form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler(form)
    if not erro:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                cur.execute("""UPDATE fornecedores SET nome = %(nome)s, documento = %(documento)s, telefone = %(telefone)s,
                                      email = %(email)s, pix = %(pix)s, categoria = %(categoria)s,
                                      observacoes = %(observacoes)s, ativo = %(ativo)s WHERE id = %(id)s""",
                            {**d, "id": str(fid)})
                auditar(cur, request, "fornecedor_alterado", usuario["id"], "fornecedor", str(fid))
            return RedirectResponse(f"/fornecedores/{fid}?ok=1", status_code=303)
        except psycopg.Error as e:
            erro = "Já existe fornecedor com esse CPF/CNPJ." if "uq_fornecedor_documento" in str(e) else mensagem_do_banco(e)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        f = _carregar(cur, str(fid))
    return tela(request, "fornecedor_form.html", usuario, v=d, f=f, pagos=[], erro=erro, CATEGORIAS=CATEGORIAS, status=400)
