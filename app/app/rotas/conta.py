"""Site: conta da cliente — entrar (código no WhatsApp ou e-mail e senha), criar conta, Minha conta."""
import re
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import avisos, codigos, contas, cripto, db, seguranca
from ..auth import auditar, ip
from ..pagamento import formulario
from ..web import checar_csrf

router = APIRouter()


def _proximo(valor: str | None) -> str:
    """Volta só para páginas do próprio site (nada de redirecionar para fora)."""
    v = (valor or "").strip()
    return v if re.fullmatch(r"/site(/[A-Za-z0-9/_?=&.%-]*)?", v) and ".." not in v and "//" not in v else "/site/conta"


def _fone(texto: str | None) -> str:
    d = re.sub(r"\D", "", texto or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    return d


def _pagina(request, nome, cur, **ctx):
    from .site import _pagina as pagina_site
    ctx.setdefault("destaques", [])
    ctx.setdefault("avaliacoes", None)
    return pagina_site(request, nome, cur, **ctx)


def _ua(request) -> str | None:
    return request.headers.get("user-agent")


# --------------------------------------------------------------------------- #
# Entrar
# --------------------------------------------------------------------------- #
@router.get("/site/conta/entrar")
def entrar_form(request: Request, proximo: str = "", aviso: str = ""):
    with db.transacao() as cur:
        if contas.conta_atual(cur, request.session, _ua(request)):
            return RedirectResponse(_proximo(proximo), status_code=303)
        zap = avisos.whatsapp_ativo(cur)
        return _pagina(request, "site/conta_entrar.html", cur, proximo=_proximo(proximo), zap=zap, aviso=aviso[:120], v={})


@router.post("/site/conta/codigo/pedir")
def pedir_codigo(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    fone = _fone(form.get("telefone"))
    proximo = _proximo(form.get("proximo"))
    with db.transacao() as cur:
        if len(fone) not in (10, 11):
            return _pagina(request, "site/conta_entrar.html", cur, proximo=proximo, zap=avisos.whatsapp_ativo(cur),
                           erro_zap="Digite o WhatsApp com DDD, ex.: (19) 99999-9999.", v=form, status=400)
        if not contas.enviar_codigo(cur, request.session, fone, "entrar"):
            return _pagina(request, "site/conta_entrar.html", cur, proximo=proximo, zap=False,
                           erro_zap="O código pelo WhatsApp está indisponível agora. Entre com e-mail e senha.", v=form, status=400)
    request.session["cliente_proximo"] = proximo
    return RedirectResponse("/site/conta/codigo", status_code=303)


@router.get("/site/conta/codigo")
def codigo_form(request: Request):
    p = request.session.get("cliente_codigo")
    with db.transacao() as cur:
        if not p:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        return _pagina(request, "site/conta_codigo.html", cur, final=p["fone"][-4:], motivo=p.get("motivo"))


@router.post("/site/conta/codigo")
def codigo(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    p = request.session.get("cliente_codigo") or {}
    fone, erro = contas.conferir_codigo(request.session, form.get("codigo"))
    with db.transacao() as cur:
        if erro:
            if not request.session.get("cliente_codigo"):
                return RedirectResponse(f"/site/conta/entrar?aviso={quote(erro)}", status_code=303)
            return _pagina(request, "site/conta_codigo.html", cur, final=(p.get("fone") or "")[-4:], erro=erro,
                           motivo=p.get("motivo"), status=400)
        novo = request.session.pop("cliente_cadastro", None)
        achou = contas.cliente_pelo_telefone(cur, fone)
        try:
            if achou:
                cliente_id = achou["id"]
            elif novo and novo.get("fone") == fone:
                cliente_id = contas.criar_cliente(cur, novo["nome"], fone, novo.get("email"))
            else:                                 # entrou pelo código e ainda não tem ficha: completar o cadastro
                request.session["cliente_fone_ok"] = {"fone": fone}
                return RedirectResponse("/site/conta/completar", status_code=303)
            conta_id = contas.conta_do_cliente(cur, cliente_id)
            if novo and novo.get("fone") == fone:
                with cur.connection.transaction():
                    _email_senha(cur, conta_id, cliente_id, novo.get("email"), novo.get("senha_hash"))
        except psycopg.Error as e:
            if "uq_conta_email" in str(e):
                return RedirectResponse("/site/conta/entrar?aviso=" + quote("Esse e-mail já tem conta. Entre com ele."),
                                        status_code=303)
            raise
        contas.abrir_sessao(cur, request.session, conta_id, _ua(request))
        auditar(cur, request, "cliente_entrou", None, "conta", conta_id, {"como": "whatsapp", "novo": bool(novo)})
    return RedirectResponse(_proximo(request.session.pop("cliente_proximo", None)), status_code=303)


def _email_senha(cur, conta_id, cliente_id, email, senha_hash):
    if email:
        contas.definir_email_senha(cur, conta_id, cliente_id, email, None)
    if senha_hash:
        cur.execute("UPDATE contas_clientes SET senha_hash = %s WHERE id = %s", (senha_hash, conta_id))


@router.post("/site/conta/entrar")
def entrar(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    email = (form.get("email") or "").strip().lower()[:120]
    senha = (form.get("senha") or "")[:64]
    proximo = _proximo(form.get("proximo"))
    with db.transacao() as cur:
        conta_id, erro = contas.entrar_com_senha(cur, email, senha)
        if not erro:
            contas.abrir_sessao(cur, request.session, conta_id, _ua(request))
            auditar(cur, request, "cliente_entrou", None, "conta", conta_id, {"como": "senha"})
    if erro:
        with db.transacao() as cur:
            auditar(cur, request, "cliente_login_falhou", None, "conta", None, {"ip": ip(request)})
            return _pagina(request, "site/conta_entrar.html", cur, proximo=proximo, zap=avisos.whatsapp_ativo(cur),
                           erro_senha=erro, v={"email": email}, status=400)
    return RedirectResponse(proximo, status_code=303)


# --------------------------------------------------------------------------- #
# Criar conta
# --------------------------------------------------------------------------- #
def _ler_cadastro(form: dict, exigir_fone=True) -> tuple[dict, str | None]:
    d = {"nome": re.sub(r"\s+", " ", (form.get("nome") or "").strip())[:120],
         "fone": _fone(form.get("telefone")), "email": (form.get("email") or "").strip().lower()[:120] or None,
         "senha": form.get("senha") or ""}
    if len(d["nome"].split()) < 2:
        return d, "Escreva seu nome e sobrenome."
    if exigir_fone and len(d["fone"]) not in (10, 11):
        return d, "Digite o WhatsApp com DDD."
    if d["email"] and not contas.email_valido(d["email"]):
        return d, "E-mail inválido."
    if d["senha"]:
        if not d["email"]:
            return d, "Para entrar com senha, informe também o e-mail."
        erro = seguranca.validar_senha_nova(d["senha"], (d["nome"], d["email"].split("@")[0]))
        if erro:
            return d, erro
    if form.get("lgpd") != "1":
        return d, "Marque que concorda com a Política de privacidade."
    return d, None


@router.get("/site/conta/criar")
def criar_form(request: Request, proximo: str = ""):
    with db.transacao() as cur:
        return _pagina(request, "site/conta_criar.html", cur, v={}, proximo=_proximo(proximo), zap=avisos.whatsapp_ativo(cur))


@router.post("/site/conta/criar")
def criar(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    proximo = _proximo(form.get("proximo"))
    d, erro = _ler_cadastro(form)
    with db.transacao() as cur:
        zap = avisos.whatsapp_ativo(cur)
        if not erro and d["email"]:
            cur.execute("SELECT 1 FROM contas_clientes WHERE email_idx = %s", (contas.indice_email(d["email"]),))
            if cur.fetchone():
                erro = "Esse e-mail já tem conta. Entre com ele (ou peça o código no WhatsApp)."
        if not erro and not zap:
            if not d["senha"]:
                erro = "Crie uma senha para entrar com o e-mail."
            elif contas.cliente_pelo_telefone(cur, d["fone"]):
                erro = ("Esse WhatsApp já tem cadastro na loja. Para sua segurança, fale com a loja para ativar sua "
                        "conta (o código pelo WhatsApp está desligado agora).")
        if erro:
            return _pagina(request, "site/conta_criar.html", cur, v={**form, "senha": ""}, erro=erro, proximo=proximo,
                           zap=zap, status=400)
        if zap:        # confirma o telefone antes de criar/ligar a conta
            request.session["cliente_cadastro"] = {"nome": d["nome"], "fone": d["fone"], "email": d["email"],
                                                   "senha_hash": seguranca.gerar_hash(d["senha"]) if d["senha"] else None}
            contas.enviar_codigo(cur, request.session, d["fone"], "confirmar")
            request.session["cliente_proximo"] = proximo
            return RedirectResponse("/site/conta/codigo", status_code=303)
        cliente_id = contas.criar_cliente(cur, d["nome"], d["fone"], d["email"])
        conta_id = contas.conta_do_cliente(cur, cliente_id)
        contas.definir_email_senha(cur, conta_id, cliente_id, d["email"], d["senha"])
        contas.abrir_sessao(cur, request.session, conta_id, _ua(request))
        auditar(cur, request, "cliente_conta_criada", None, "conta", conta_id, {"como": "senha"})
    return RedirectResponse(proximo, status_code=303)


@router.get("/site/conta/completar")
def completar_form(request: Request):
    with db.transacao() as cur:
        if not request.session.get("cliente_fone_ok"):
            return RedirectResponse("/site/conta/entrar", status_code=303)
        return _pagina(request, "site/conta_criar.html", cur, v={}, completar=True, zap=True,
                       proximo=_proximo(request.session.get("cliente_proximo")))


@router.post("/site/conta/completar")
def completar(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    ok = request.session.get("cliente_fone_ok")
    if not ok:
        return RedirectResponse("/site/conta/entrar", status_code=303)
    d, erro = _ler_cadastro(form, exigir_fone=False)
    with db.transacao() as cur:
        if not erro and d["email"]:
            cur.execute("SELECT 1 FROM contas_clientes WHERE email_idx = %s", (contas.indice_email(d["email"]),))
            if cur.fetchone():
                erro = "Esse e-mail já tem conta."
        if erro:
            return _pagina(request, "site/conta_criar.html", cur, v={**form, "senha": ""}, erro=erro, completar=True,
                           zap=True, proximo=_proximo(request.session.get("cliente_proximo")), status=400)
        cliente_id = (contas.cliente_pelo_telefone(cur, ok["fone"]) or {}).get("id") \
            or contas.criar_cliente(cur, d["nome"], ok["fone"], d["email"])
        conta_id = contas.conta_do_cliente(cur, cliente_id)
        contas.definir_email_senha(cur, conta_id, cliente_id, d["email"], d["senha"])
        contas.abrir_sessao(cur, request.session, conta_id, _ua(request))
        auditar(cur, request, "cliente_conta_criada", None, "conta", conta_id, {"como": "whatsapp"})
    request.session.pop("cliente_fone_ok", None)
    return RedirectResponse(_proximo(request.session.pop("cliente_proximo", None)), status_code=303)


# --------------------------------------------------------------------------- #
# Minha conta
# --------------------------------------------------------------------------- #
STATUS_SITE = {
    "AGUARDANDO_PAGAMENTO": "Esperando o pagamento", "RESERVADO": "Reservado", "PROVA_1": "1ª prova",
    "EM_AJUSTE": "Em ajuste", "PROVA_2": "2ª prova", "PRONTO": "Pronto para retirar", "ALUGADO": "Com você",
    "DEVOLVIDO": "Devolvido", "EM_LIMPEZA": "Devolvido", "CONCLUIDO": "Concluído", "RETIRADO": "Entregue",
    "VENDIDO": "Entregue", "CANCELADO": "Cancelado"}
STATUS_COMPRA = {"ABERTA": "Esperando o pagamento", "PAGA": "Paga ✓", "CONFERIR": "Pagamento em conferência pela loja",
                 "VENCIDA": "Não foi paga (venceu)", "CANCELADA": "Cancelada"}


@router.get("/site/conta")
def minha_conta(request: Request, ok: str = ""):
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, _ua(request))
        if not conta:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        cur.execute("""SELECT c.telefone_c, c.email_c, (cc.senha_hash IS NOT NULL) AS tem_senha, cc.exclusao_pedida_em
                         FROM clientes c JOIN contas_clientes cc ON cc.cliente_id = c.id WHERE cc.id = %s""", (conta["id"],))
        dados = cripto.abrir(cur.fetchone(), "clientes", "telefone", "email")
        cur.execute("""SELECT pe.id::text AS id, pe.tipo, pe.status, pe.token_publico, pe.data_retirada, pe.data_devolucao,
                              pe.valor_total_centavos, pe.valor_total_centavos - valor_pago(pe.id) AS falta, pr.nome AS peca,
                              pr.sku, (SELECT f.arquivo FROM produto_fotos f WHERE f.produto_id = pr.id AND f.ativa
                                        ORDER BY f.ordem LIMIT 1) AS foto,
                              EXISTS (SELECT 1 FROM contratos ct WHERE ct.pedido_id = pe.id) AS contrato
                         FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                        WHERE pe.cliente_id = %s ORDER BY pe.criado_em DESC LIMIT 50""", (conta["cliente_id"],))
        pedidos = cur.fetchall()
        cur.execute("""SELECT id::text, status, valor_total_centavos, frete_centavos, entrega, forma_paga, rastreio,
                              to_char(criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando,
                              status = 'ABERTA' AND expira_em > now() AS pode_pagar
                         FROM compras_site WHERE conta_id = %s ORDER BY criado_em DESC LIMIT 20""", (conta["id"],))
        compras = cur.fetchall()
        for p in pedidos:
            p["verificar"] = codigos.codigo_verificacao(p["id"])
        return _pagina(request, "site/conta.html", cur, conta=conta, dados=dados, pedidos=pedidos, compras=compras,
                       STATUS_SITE=STATUS_SITE, STATUS_COMPRA=STATUS_COMPRA, ok=ok[:80])


@router.post("/site/conta/sair")
def sair(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao() as cur:
        contas.encerrar(cur, request.session, "SAIU")
    request.session.pop("sacola", None)
    return RedirectResponse("/site", status_code=303)


@router.post("/site/conta/senha")
def trocar_senha(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, _ua(request))
        if not conta:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        cur.execute("""SELECT cc.senha_hash, cc.email_idx, c.email_c FROM contas_clientes cc JOIN clientes c ON c.id = cc.cliente_id
                        WHERE cc.id = %s""", (conta["id"],))
        atual = cur.fetchone()
        email = (form.get("email") or "").strip().lower() or (cripto.decifrar(atual["email_c"], "clientes.email")
                                                              if atual["email_c"] else "")
        nova = form.get("nova") or ""
        if atual["senha_hash"] and not seguranca.verificar(atual["senha_hash"], form.get("atual") or ""):
            erro = "A senha atual não confere."
        elif not contas.email_valido(email):
            erro = "Informe um e-mail válido para entrar com senha."
        else:
            erro = seguranca.validar_senha_nova(nova, (conta["nome"], email.split("@")[0]))
        if erro:
            return RedirectResponse(f"/site/conta?ok={quote(erro)}#senha", status_code=303)
        try:
            with cur.connection.transaction():
                contas.definir_email_senha(cur, conta["id"], conta["cliente_id"], email, nova)
        except psycopg.Error as e:
            if "uq_conta_email" in str(e):
                return RedirectResponse(f"/site/conta?ok={quote('Esse e-mail já é de outra conta.')}#senha", status_code=303)
            raise
        contas.encerrar_todas(cur, conta["id"], "SENHA_TROCADA")
        contas.abrir_sessao(cur, request.session, conta["id"], _ua(request))
        auditar(cur, request, "cliente_senha_trocada", None, "conta", conta["id"])
    return RedirectResponse(f"/site/conta?ok={quote('Senha guardada. Outros aparelhos foram desconectados.')}", status_code=303)


@router.post("/site/conta/excluir")
def pedir_exclusao(request: Request, form: dict = Depends(formulario)):
    """LGPD: a cliente pede para apagar os dados. A loja confere o que a lei manda guardar (contratos, notas)
    e responde; a conta é desligada na hora."""
    checar_csrf(request, form.get("csrf"))
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, _ua(request))
        if not conta:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        cur.execute("UPDATE contas_clientes SET exclusao_pedida_em = now(), ativa = false WHERE id = %s", (conta["id"],))
        contas.encerrar_todas(cur, conta["id"], "EXCLUSAO_PEDIDA")
        request.session.pop(contas.CHAVE_SESSAO, None)
        auditar(cur, request, "cliente_pediu_exclusao", None, "conta", conta["id"])
        avisos.para_equipe(cur, "site", f"🗑 {conta['nome']} pediu pelo site para apagar os dados (LGPD). "
                                        "Confira na ficha da cliente e responda em até 15 dias.", "lgpd_exclusao",
                           f"lgpd:{conta['id']}")
    return RedirectResponse("/site?conta_excluida=1", status_code=303)
