"""Configurações do site: dados da vitrine, ocasiões, tipos de peça e pagamento online."""
import re

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from urllib.parse import quote

from .. import db, pagamento_online as po
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import checar_csrf, mensagem_do_banco, tela
from .catalogo import ocasioes_arvore

router = APIRouter()
VER = ("site.editar", "config.pagamento")


def slugificar(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:50]


def _tela(request, usuario, ok="", erro="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT nome, whatsapp, instagram, horario, frase_site, site_ativo FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()
        grupos = ocasioes_arvore(cur, so_ativas=False)
        cur.execute("""SELECT c.id, c.nome, c.prefixo_sku, count(p.id) AS pecas FROM categorias c
                         LEFT JOIN produtos p ON p.categoria_id = c.id GROUP BY c.id ORDER BY c.id""")
        tipos = cur.fetchall()
        online = po.configurado(cur)
        cur.execute("SELECT count(*) AS n FROM produtos WHERE no_site AND status = 'DISPONIVEL'")
        no_site = cur.fetchone()["n"]
    return tela(request, "config_site.html", usuario, status=status, loja=loja, grupos=grupos, tipos=tipos,
                online=online, no_site=no_site, ok=ok or request.query_params.get("ok", ""), erro=erro,
                edita_site=pode(usuario, "site.editar"), edita_pagamento=pode(usuario, "config.pagamento"))


def _volta(recado: str, ancora: str = ""):
    return RedirectResponse(f"/config/site?ok={quote(recado)}{ancora}", status_code=303)


@router.get("/config/site")
def ver(request: Request, usuario: dict = Depends(requer(*VER))):
    return _tela(request, usuario)


@router.post("/config/site/dados")
def dados(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    zap = re.sub(r"\D", "", form.get("whatsapp") or "")
    if zap.startswith("55") and len(zap) in (12, 13):
        zap = zap[2:]
    insta = (form.get("instagram") or "").strip().lstrip("@")[:30]
    if zap and len(zap) not in (10, 11):
        return _tela(request, usuario, erro="WhatsApp com DDD, por exemplo (19) 99999-9999.", status=400)
    if insta and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", insta):
        return _tela(request, usuario, erro="Instagram: só o nome do perfil, sem espaços (ex.: minhaloja).", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE lojas SET whatsapp=%s, instagram=%s, horario=%s, frase_site=%s, site_ativo=%s
                        WHERE id = ctx_loja()""",
                    (zap or None, insta or None, (form.get("horario") or "").strip()[:120] or None,
                     (form.get("frase_site") or "").strip()[:200] or None, form.get("site_ativo") == "1"))
        auditar(cur, request, "site_dados_alterados", usuario["id"], "loja")
    return _volta("Dados do site salvos.", "#dados")


@router.post("/config/site/ocasioes")
def nova_ocasiao(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    nome = (form.get("nome") or "").strip()[:60]
    pai = form.get("pai") or ""
    if len(nome) < 2:
        return _tela(request, usuario, erro="Escreva o nome da ocasião.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""INSERT INTO ocasioes (loja_id, pai_id, nome, slug, ordem)
                           VALUES (ctx_loja(), %s, %s, %s,
                                   (SELECT coalesce(max(ordem), 0) + 1 FROM ocasioes WHERE pai_id IS NOT DISTINCT FROM %s))""",
                        (int(pai) if pai.isdigit() else None, nome, slugificar(nome),
                         int(pai) if pai.isdigit() else None))
            auditar(cur, request, "ocasiao_criada", usuario["id"], "ocasiao", None, {"nome": nome})
    except psycopg.errors.UniqueViolation:
        return _tela(request, usuario, erro=f"Já existe '{nome}' nesse grupo.", status=400)
    except psycopg.Error as e:
        return _tela(request, usuario, erro=mensagem_do_banco(e), status=400)
    return _volta(f"'{nome}' criada.", "#ocasioes")


@router.post("/config/site/ocasioes/{oid}")
def mudar_ocasiao(request: Request, oid: int, usuario: dict = Depends(requer("site.editar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    acao = form.get("acao")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        if acao == "renomear":
            nome = (form.get("nome") or "").strip()[:60]
            if len(nome) < 2:
                return _tela(request, usuario, erro="Escreva o nome.", status=400)
            try:
                cur.execute("UPDATE ocasioes SET nome = %s, slug = %s WHERE id = %s", (nome, slugificar(nome), oid))
            except psycopg.errors.UniqueViolation:
                return _tela(request, usuario, erro=f"Já existe '{nome}' nesse grupo.", status=400)
        elif acao in ("ativar", "desativar"):
            cur.execute("UPDATE ocasioes SET ativa = %s WHERE id = %s OR pai_id = %s", (acao == "ativar", oid, oid))
        elif acao in ("subir", "descer"):
            cur.execute("SELECT id, pai_id, ordem FROM ocasioes WHERE id = %s", (oid,))
            o = cur.fetchone()
            if o:
                cur.execute(f"""SELECT id, ordem FROM ocasioes WHERE pai_id IS NOT DISTINCT FROM %s AND id <> %s
                                  AND ordem {'<=' if acao == 'subir' else '>='} %s
                                ORDER BY ordem {'DESC' if acao == 'subir' else 'ASC'}, id LIMIT 1""",
                            (o["pai_id"], oid, o["ordem"]))
                vizinho = cur.fetchone()
                if vizinho:
                    novo = vizinho["ordem"] + (-1 if acao == "subir" else 1) if vizinho["ordem"] == o["ordem"] else vizinho["ordem"]
                    cur.execute("UPDATE ocasioes SET ordem = %s WHERE id = %s", (o["ordem"], vizinho["id"]))
                    cur.execute("UPDATE ocasioes SET ordem = %s WHERE id = %s", (novo, oid))
        auditar(cur, request, "ocasiao_" + (acao or "?"), usuario["id"], "ocasiao", str(oid))
    return _volta("Ocasiões atualizadas.", "#ocasioes")


@router.post("/config/site/tipos")
def novo_tipo(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    nome = (form.get("nome") or "").strip()[:40]
    prefixo = (form.get("prefixo") or "").strip().upper()
    if len(nome) < 2 or not re.fullmatch(r"[A-Z]{2,5}", prefixo):
        return _tela(request, usuario, erro="Escreva o nome e um código de 2 a 5 letras (ex.: Vestido infantil = VIN).",
                     status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("INSERT INTO categorias (loja_id, nome, prefixo_sku) VALUES (ctx_loja(), %s, %s)", (nome, prefixo))
            auditar(cur, request, "tipo_peca_criado", usuario["id"], "categoria", None, {"nome": nome, "prefixo": prefixo})
    except psycopg.errors.UniqueViolation:
        return _tela(request, usuario, erro="Já existe um tipo com esse nome ou esse código.", status=400)
    return _volta(f"Tipo '{nome}' criado. As peças novas dele terão código {prefixo}-0001, {prefixo}-0002…", "#tipos")


@router.post("/config/site/pagamento")
def salvar_token(request: Request, usuario: dict = Depends(requer("config.pagamento")),
                 form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    token = re.sub(r"\s", "", form.get("token") or "")
    if not re.fullmatch(r"(APP_USR|TEST)-[A-Za-z0-9-]{20,200}", token):
        return _tela(request, usuario, status=400,
                     erro="Esse não parece um Access Token do Mercado Pago. Ele começa com APP_USR- e é bem comprido.")
    try:
        conta = po.conferir_token(token)
    except po.ErroProvedor as e:
        return _tela(request, usuario, erro=str(e), status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        po.salvar_token(cur, token, conta, usuario["id"])
        cur.execute("UPDATE lojas SET sacola_ativa = true WHERE id = ctx_loja() AND NOT sacola_ativa")
        auditar(cur, request, "pagamento_online_configurado", usuario["id"], "loja", None,
                {"provedor": "MERCADO_PAGO", "final": token[-4:], "conta": conta})
    return _volta(f"Pagamento online ligado na conta {conta}. A sacola do site foi ligada: os botões Alugar e Comprar "
                  "já aparecem nas peças com preço no site (desligue em Sacola e entrega, se quiser).", "#pagamento")


@router.post("/config/site/pagamento/desligar")
def desligar(request: Request, usuario: dict = Depends(requer("config.pagamento")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("UPDATE pagamento_online SET ativo = false WHERE loja_id = ctx_loja()")
        auditar(cur, request, "pagamento_online_desligado", usuario["id"], "loja")
    return _volta("Pagamento online desligado. Os links já enviados continuam valendo até vencer.", "#pagamento")


# --------------------------------------------------------------------------- #
# Redes sociais (botão próprio na tela inicial)
# --------------------------------------------------------------------------- #
REDES = [  # (campo, nome, domínios aceitos, exemplo)
    ("instagram", "Instagram", ("instagram.com",), "minhaloja  (só o nome do perfil)"),
    ("facebook", "Facebook", ("facebook.com", "fb.com", "fb.me"), "https://facebook.com/minhaloja"),
    ("tiktok", "TikTok", ("tiktok.com",), "https://tiktok.com/@minhaloja"),
    ("youtube", "YouTube", ("youtube.com", "youtu.be"), "https://youtube.com/@minhaloja"),
    ("pinterest", "Pinterest", ("pinterest.com", "pinterest.com.br", "pin.it"), "https://pinterest.com/minhaloja"),
    ("site_externo", "Outro site (loja virtual, Linktree…)", (), "https://minhaloja.com.br"),
]


def _url_rede(texto: str, dominios: tuple) -> str | None:
    """Aceita 'facebook.com/loja' ou o link completo; devolve https://... ou levanta ValueError."""
    from urllib.parse import urlsplit
    t = (texto or "").strip()
    if not t:
        return None
    if not re.match(r"^https?://", t, re.I):
        t = "https://" + t
    t = re.sub(r"^http://", "https://", t, flags=re.I)
    u = urlsplit(t)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if len(t) > 200 or " " in t or not host or "." not in host or u.username or u.password:
        raise ValueError
    if dominios and not any(host == d or host.endswith("." + d) for d in dominios):
        raise ValueError
    return t


def _tela_redes(request, usuario, v=None, ok="", erro="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("SELECT instagram, facebook, tiktok, youtube, pinterest, site_externo FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()
    return tela(request, "config_redes.html", usuario, status=status, v=v or loja, REDES=REDES,
                ok=ok or request.query_params.get("ok", ""), erro=erro)


@router.get("/config/redes")
def redes(request: Request, usuario: dict = Depends(requer("site.editar"))):
    return _tela_redes(request, usuario)


@router.post("/config/redes")
def salvar_redes(request: Request, usuario: dict = Depends(requer("site.editar")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    valores = {}
    for campo, nome, dominios, exemplo in REDES:
        if campo == "instagram":
            insta = (form.get("instagram") or "").strip()
            m = re.match(r"^(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?", insta)
            insta = (m.group(1) if m else insta.lstrip("@"))[:30]
            if insta and not re.fullmatch(r"[A-Za-z0-9._]{1,30}", insta):
                return _tela_redes(request, usuario, form, erro="Instagram: só o nome do perfil, sem espaços (ex.: minhaloja).",
                                   status=400)
            valores[campo] = insta or None
            continue
        try:
            valores[campo] = _url_rede(form.get(campo), dominios)
        except ValueError:
            return _tela_redes(request, usuario, form, status=400,
                               erro=f"{nome}: o link não parece certo. Exemplo: {exemplo}")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"UPDATE lojas SET {', '.join(f'{k} = %s' for k in valores)} WHERE id = ctx_loja()",
                    tuple(valores.values()))
        auditar(cur, request, "redes_sociais_alteradas", usuario["id"], "loja")
    return RedirectResponse(f"/config/redes?ok={quote('Redes sociais salvas. Já aparecem no site.')}", status_code=303)


# --------------------------------------------------------------------------- #
# Regras da vitrine e das reservas (a loja ajusta; nada fica "no código")
# --------------------------------------------------------------------------- #
def _tela_vitrine(request, usuario, ok="", erro="", status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT reserva_modo, alugada_no_site, venda_ajuste_some, prova_antecedencia_dias,
                              atraso_aviso_hora, dias_abertos FROM lojas WHERE id = ctx_loja()""")
        regras = cur.fetchone()
        cur.execute("SELECT dias_limpeza FROM config_pagamento WHERE loja_id = ctx_loja() ORDER BY vigente_desde DESC LIMIT 1")
        cfg = cur.fetchone()
    return tela(request, "config_vitrine.html", usuario, status=status, r=regras, dias_limpeza=cfg["dias_limpeza"] if cfg else 3,
                ok=ok or request.query_params.get("ok", ""), erro=erro)


@router.get("/config/vitrine")
def vitrine(request: Request, usuario: dict = Depends(requer("vitrine.regras"))):
    return _tela_vitrine(request, usuario)


@router.post("/config/vitrine")
def salvar_vitrine(request: Request, usuario: dict = Depends(requer("vitrine.regras")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    from datetime import time as _time
    modo, alugada = form.get("reserva_modo"), form.get("alugada_no_site")
    try:
        antecedencia = int(form.get("prova_antecedencia_dias") or "")
        hora = _time.fromisoformat(form.get("atraso_aviso_hora") or "")
    except ValueError:
        return _tela_vitrine(request, usuario, erro="Confira os números e o horário.", status=400)
    if modo not in ("ATE_DEVOLVER", "DATAS") or alugada not in ("OCUPADA", "ESCONDE") or not 0 <= antecedencia <= 30:
        return _tela_vitrine(request, usuario, erro="Escolha uma opção em cada pergunta (antecedência de 0 a 30 dias).",
                             status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT reserva_modo, alugada_no_site, venda_ajuste_some, prova_antecedencia_dias
                         FROM lojas WHERE id = ctx_loja()""")
        antes = cur.fetchone()
        cur.execute("""UPDATE lojas SET reserva_modo = %s, alugada_no_site = %s, venda_ajuste_some = %s,
                              prova_antecedencia_dias = %s, atraso_aviso_hora = %s WHERE id = ctx_loja()""",
                    (modo, alugada, form.get("venda_ajuste_some") == "1", antecedencia, hora))
        auditar(cur, request, "regras_vitrine_alteradas", usuario["id"], "loja", None,
                {"antes": {k: str(v) for k, v in antes.items()},
                 "depois": {"reserva_modo": modo, "alugada_no_site": alugada, "prova_antecedencia_dias": antecedencia}})
    return RedirectResponse(f"/config/vitrine?ok={quote('Regras salvas. Valem para os pedidos novos a partir de agora.')}",
                            status_code=303)
