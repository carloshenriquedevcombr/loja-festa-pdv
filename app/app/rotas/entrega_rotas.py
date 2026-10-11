"""Painel: sacola do site (ligar, entrada ou total, tempo para pagar) e entrega (retirada, cidades, Melhor Envio)."""
import re
from urllib.parse import quote
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import config, db, frete, pagamento_online as po
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import checar_csrf, ler_reais, mensagem_do_banco, tela

router = APIRouter()
UFS = ("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO").split()


def _tela(request, usuario, erro=None, ok="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT sacola_ativa, sacola_pagamento, sacola_minutos, entrega_retirada, entrega_envio, cep_origem,
                              pacote_peso_g, pacote_altura_cm, pacote_largura_cm, pacote_comprimento_cm
                         FROM lojas WHERE id = ctx_loja()""")
        loja = cur.fetchone()
        cur.execute("""SELECT id::text, cidade, uf, meio, valor_centavos, prazo, aluguel, ativa FROM entrega_cidades
                        ORDER BY ativa DESC, cidade""")
        cidades = cur.fetchall()
        pagamento_ok = bool(po.configurado(cur))
        cur.execute("""SELECT token_final, sandbox, conta, to_char(definido_em AT TIME ZONE 'America/Sao_Paulo',
                              'DD/MM/YYYY HH24:MI') AS quando FROM frete_config""")
        me = cur.fetchone()
        cur.execute("SELECT ajuste_site_centavos FROM lojas WHERE id = ctx_loja()")
        loja["ajuste_site_centavos"] = cur.fetchone()["ajuste_site_centavos"]
    return tela(request, "config_entrega.html", usuario, status=status, loja=loja, cidades=cidades, erro=erro,
                ok=ok or request.query_params.get("ok", ""), UFS=UFS, pagamento_ok=pagamento_ok,
                me=me, tem_token_envio=bool(me or config.MELHOR_ENVIO_TOKEN), token_no_env=bool(config.MELHOR_ENVIO_TOKEN))


@router.get("/config/entrega")
def ver(request: Request, usuario: dict = Depends(requer("entrega.config"))):
    return _tela(request, usuario)


def _int(v, padrao, minimo, maximo):
    try:
        return max(minimo, min(int(re.sub(r"\D", "", v or "") or padrao), maximo))
    except ValueError:
        return padrao


@router.post("/config/entrega")
def salvar(request: Request, usuario: dict = Depends(requer("entrega.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    cep = frete.cep_limpo(form.get("cep_origem"))
    if form.get("entrega_envio") == "1" and not cep:
        return _tela(request, usuario, erro="Para enviar pelo Melhor Envio, informe o CEP da loja (de onde sai o pacote).",
                     status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        ativa = form.get("sacola_ativa") == "1"
        if ativa and not po.configurado(cur):
            return _tela(request, usuario, erro="Ligue o pagamento online (Mercado Pago) antes de ligar a sacola.", status=400)
        cur.execute("""UPDATE lojas SET sacola_ativa = %s, sacola_pagamento = %s, sacola_minutos = %s,
                              entrega_retirada = %s, entrega_envio = %s, cep_origem = %s, pacote_peso_g = %s,
                              pacote_altura_cm = %s, pacote_largura_cm = %s, pacote_comprimento_cm = %s
                        WHERE id = ctx_loja()""",
                    (ativa, "TOTAL" if form.get("sacola_pagamento") == "TOTAL" else "ENTRADA_OU_TOTAL",
                     _int(form.get("sacola_minutos"), 60, 15, 1440), form.get("entrega_retirada") == "1",
                     form.get("entrega_envio") == "1", cep, _int(form.get("pacote_peso_g"), 1500, 100, 30000),
                     _int(form.get("pacote_altura_cm"), 10, 2, 100), _int(form.get("pacote_largura_cm"), 30, 10, 100),
                     _int(form.get("pacote_comprimento_cm"), 40, 15, 100)))
        ajuste = ler_reais(form.get("ajuste_site")) if (form.get("ajuste_site") or "").strip() not in ("", "0", "0,00") else None
        cur.execute("UPDATE lojas SET ajuste_site_centavos = %s WHERE id = ctx_loja()", (ajuste,))
        auditar(cur, request, "sacola_entrega_config", usuario["id"], "loja", None, {"sacola": ativa})
    return RedirectResponse("/config/entrega?ok=Guardado", status_code=303)


def _ler_cidade(form):
    d = {"cidade": re.sub(r"\s+", " ", (form.get("cidade") or "").strip())[:80], "uf": (form.get("uf") or "").upper()[:2],
         "meio": "APLICATIVO" if form.get("meio") == "APLICATIVO" else "MOTOBOY",
         "valor": ler_reais(form.get("valor")) if (form.get("valor") or "").strip() not in ("", "0", "0,00") else 0,
         "prazo": (form.get("prazo") or "").strip()[:60] or None, "aluguel": form.get("aluguel") == "1",
         "ativa": form.get("ativa", "1") == "1"}
    if len(d["cidade"]) < 2 or d["uf"] not in UFS:
        return d, "Escreva a cidade e escolha o estado."
    if d["valor"] is None:
        return d, "Valor da entrega inválido (ex.: 15,00 ou 0 para grátis)."
    return d, None


@router.post("/config/entrega/cidades")
def nova_cidade(request: Request, usuario: dict = Depends(requer("entrega.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_cidade(form)
    if erro:
        return _tela(request, usuario, erro=erro, status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""INSERT INTO entrega_cidades (loja_id, cidade, uf, meio, valor_centavos, prazo, aluguel)
                           VALUES (ctx_loja(), %(cidade)s, %(uf)s, %(meio)s, %(valor)s, %(prazo)s, %(aluguel)s)""", d)
            auditar(cur, request, "entrega_cidade_criada", usuario["id"], "loja", None, {"cidade": d["cidade"]})
    except psycopg.Error as e:
        erro = "Essa cidade já está na lista." if "uq_entrega_cidade" in str(e) else mensagem_do_banco(e)
        return _tela(request, usuario, erro=erro, status=400)
    return RedirectResponse("/config/entrega?ok=Cidade+incluída#cidades", status_code=303)


@router.post("/config/entrega/cidades/{cid}")
def editar_cidade(request: Request, cid: UUID, usuario: dict = Depends(requer("entrega.config")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    d, erro = _ler_cidade(form)
    if erro:
        return _tela(request, usuario, erro=erro, status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""UPDATE entrega_cidades SET cidade = %(cidade)s, uf = %(uf)s, meio = %(meio)s, valor_centavos = %(valor)s,
                                  prazo = %(prazo)s, aluguel = %(aluguel)s, ativa = %(ativa)s WHERE id = %(id)s""",
                        {**d, "id": str(cid)})
            auditar(cur, request, "entrega_cidade_alterada", usuario["id"], "loja", None, {"cidade": d["cidade"]})
    except psycopg.Error as e:
        return _tela(request, usuario, erro=mensagem_do_banco(e), status=400)
    return RedirectResponse("/config/entrega?ok=Guardado#cidades", status_code=303)


# --------------------------------------------------------------------------- #
# Melhor Envio: a Dona cola o token na tela (fica cifrado; ninguém lê de volta)
# --------------------------------------------------------------------------- #
@router.post("/config/entrega/melhor-envio")
def melhor_envio(request: Request, usuario: dict = Depends(requer("entrega.config")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    acao = form.get("acao")
    if acao == "tirar":
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("DELETE FROM frete_config WHERE loja_id = ctx_loja()")
            auditar(cur, request, "melhor_envio_token_removido", usuario["id"], "loja", None)
        return RedirectResponse("/config/entrega?ok=Token+do+Melhor+Envio+removido#melhor-envio", status_code=303)
    if acao == "testar":
        with db.transacao(usuario_id=usuario["id"]) as cur:
            me = frete.acesso(cur)
        if not me:
            return _tela(request, usuario, erro="Cole o token do Melhor Envio primeiro.", status=400)
        try:
            conta = frete.testar(me)
        except frete.ErroFrete as e:
            return _tela(request, usuario, erro=f"Não conectou: {e}", status=400)
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("UPDATE frete_config SET conta = %s WHERE loja_id = ctx_loja()", (conta,))
        return RedirectResponse("/config/entrega?ok=" + quote(f"Melhor Envio conectado: {conta}") + "#melhor-envio",
                                status_code=303)
    token = re.sub(r"\s+", "", form.get("token") or "")
    if not 20 <= len(token) <= 4000 or not re.fullmatch(r"[A-Za-z0-9._\-]+", token):
        return _tela(request, usuario, erro="Esse não parece um token do Melhor Envio. Copie o token inteiro (é bem comprido).",
                     status=400)
    sandbox = form.get("sandbox") == "1"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE frete_config SET token_cifrado = pgp_sym_encrypt(%s, %s), token_final = %s, sandbox = %s,
                              conta = NULL, definido_por = %s, definido_em = now() WHERE loja_id = ctx_loja()""",
                    (token, config.CHAVE_TOKENS, token[-4:], sandbox, usuario["id"]))
        if cur.rowcount == 0:
            cur.execute("""INSERT INTO frete_config (loja_id, token_cifrado, token_final, sandbox, definido_por)
                           VALUES (ctx_loja(), pgp_sym_encrypt(%s, %s), %s, %s, %s)""",
                        (token, config.CHAVE_TOKENS, token[-4:], sandbox, usuario["id"]))
        auditar(cur, request, "melhor_envio_token_salvo", usuario["id"], "loja", None, {"sandbox": sandbox})
    return RedirectResponse("/config/entrega?ok=Token+guardado.+Toque+em+Testar+conexão#melhor-envio", status_code=303)
