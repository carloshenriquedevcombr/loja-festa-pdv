"""Site: sacola (aluguel com datas e compra), entrega e pagamento no Mercado Pago.

Fluxo: peça → "Colocar na sacola" → sacola (entrega, entrada ou total) → entrar/criar conta → "Pagar"
→ página do Mercado Pago (Pix/cartão; a loja nunca vê o cartão) → volta para o site. O aviso do Mercado Pago
lança no caixa e o pedido aparece no sistema sozinho (aluguel já reservado; compra registrada e paga).
A sacola fica no cookie cifrado (só ids e datas), então não guarda nada da cliente sem ela pedir.
"""
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import contas, cripto, db, frete, pagamento, pagamento_online as po
from ..pagamento import formulario
from ..web import checar_csrf, mensagem_do_banco
from .pedidos import DIAS_ANTES_PROVA
from .site import SQL_CARD, VISIVEL, _cards, _loja

router = APIRouter()
MAX_ITENS = 5
MAX_DIAS_ALUGUEL = 7
UFS = ("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO").split()


class Recusa(Exception):
    """Motivo para não fechar a compra (desfaz tudo o que já foi gravado)."""


def _pagina(request, nome, cur, **ctx):
    from .site import _pagina as pagina_site
    ctx.setdefault("destaques", [])
    ctx.setdefault("avaliacoes", None)
    return pagina_site(request, nome, cur, **ctx)


def _data(txt):
    try:
        return date.fromisoformat(txt) if txt else None
    except (TypeError, ValueError):
        return None


def _hoje(cur) -> date:
    cur.execute("SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date AS d")
    return cur.fetchone()["d"]


# --------------------------------------------------------------------------- #
# Regras de cada item (conferidas ao pôr na sacola, ao abrir a sacola e de novo ao pagar)
# --------------------------------------------------------------------------- #
def conferir_item(cur, it: dict) -> tuple[dict | None, str | None]:
    """Devolve a peça (com preço e dados) ou o motivo de não dar."""
    try:
        pid = str(uuid.UUID(str(it.get("peca"))))
    except ValueError:
        return None, "Peça não encontrada."
    cur.execute(SQL_CARD + f" WHERE {VISIVEL} AND p.id = %s", (pid,))
    p = cur.fetchone()
    if not p or not p["preco_no_site"]:
        return None, "Esta peça não está mais disponível pelo site."
    _cards([p])
    tipo = it.get("tipo")
    hoje = _hoje(cur)
    if tipo == "ALUGUEL":
        if not p["pode_alugar"] or not p["preco_aluguel_centavos"]:
            return None, "Esta peça não é para aluguel."
        ret, dev = _data(it.get("retirada")), _data(it.get("devolucao"))
        cur.execute("SELECT prova_antecedencia_dias AS ant FROM lojas WHERE id = ctx_loja()")
        ant = cur.fetchone()["ant"]
        if not ret or not dev:
            return None, "Escolha a data de retirada e a de devolução."
        if ret < hoje + timedelta(days=max(ant, 1)):
            return None, f"A retirada precisa ser a partir de {(hoje + timedelta(days=max(ant, 1))):%d/%m/%Y} (tempo para prova e ajuste)."
        if ret > hoje + timedelta(days=540):
            return None, "Escolha uma retirada nos próximos 18 meses."
        if not ret <= dev <= ret + timedelta(days=MAX_DIAS_ALUGUEL):
            return None, f"A devolução é de 0 a {MAX_DIAS_ALUGUEL} dias depois da retirada."
        limpeza = pagamento.config_vigente(cur)["dias_limpeza"]
        cur.execute("""SELECT NOT EXISTS (SELECT 1 FROM pedidos pe
                         WHERE pe.produto_id = %s AND pe.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
                           AND pe.periodo_bloqueado && daterange(%s::date - %s, %s::date + %s, '[]')) AS livre""",
                        (pid, ret, DIAS_ANTES_PROVA, dev, limpeza))
        if not cur.fetchone()["livre"]:
            return None, "Esta peça já está reservada nessas datas. Escolha outras datas ou outra peça."
        return {**p, "tipo": "ALUGUEL", "valor": p["preco_aluguel_centavos"], "retirada": ret, "devolucao": dev}, None
    if tipo == "VENDA":
        if not p["pode_vender"] or not p["preco_venda_centavos"]:
            return None, "Esta peça não está à venda."
        cur.execute("""SELECT EXISTS (SELECT 1 FROM pedidos WHERE produto_id = %s AND status NOT IN ('CANCELADO','CONCLUIDO'))
                          OR EXISTS (SELECT 1 FROM compras_itens i JOIN compras_site c ON c.id = i.compra_id
                                      WHERE i.produto_id = %s AND i.tipo = 'VENDA' AND c.status = 'ABERTA'
                                        AND c.expira_em > now()) AS ocupada""", (pid, pid))
        if cur.fetchone()["ocupada"]:
            return None, "Esta peça está reservada agora. Chame a loja no WhatsApp."
        if it.get("ajuste"):                       # compra com ajuste na loja: soma o valor da loja e marca a data
            cur.execute("SELECT ajuste_site_centavos AS aj, prova_antecedencia_dias AS ant FROM lojas WHERE id = ctx_loja()")
            regra = cur.fetchone()
            if not regra["aj"]:
                return None, "A compra com ajuste pelo site não está disponível. Chame a loja no WhatsApp."
            ret = _data(it.get("retirada"))
            minimo = hoje + timedelta(days=max(regra["ant"], 1))
            if not ret:
                return None, "Compra com ajuste: escolha para quando você precisa da peça pronta."
            if ret < minimo:
                return None, f"Com ajuste, a peça fica pronta a partir de {minimo:%d/%m/%Y} (tempo para prova e costura)."
            if ret > hoje + timedelta(days=540):
                return None, "Escolha uma data nos próximos 18 meses."
            return {**p, "tipo": "VENDA", "valor": p["preco_venda_centavos"] + regra["aj"], "ajuste": regra["aj"],
                    "retirada": ret}, None
        return {**p, "tipo": "VENDA", "valor": p["preco_venda_centavos"], "ajuste": 0}, None
    return None, "Escolha aluguel ou compra."


def _itens(cur, sacola: list) -> tuple[list, list]:
    ok, problemas = [], []
    for it in sacola[:MAX_ITENS]:
        p, erro = conferir_item(cur, it)
        if erro:
            problemas.append({"it": it, "erro": erro})
        else:
            ok.append(p)
    return ok, problemas


def _valores(cur, itens: list, pagar: str) -> dict:
    cfg = pagamento.config_vigente(cur)
    total = entrada = 0
    for p in itens:
        p["entrada"] = pagamento.entrada(cfg, p["valor"], "ALUGUEL") if p["tipo"] == "ALUGUEL" else p["valor"]
        p["pagar_agora"] = p["valor"] if pagar == "TOTAL" or p["tipo"] == "VENDA" else p["entrada"]
        total += p["valor"]
        entrada += p["entrada"]
    return {"total": total, "entrada": entrada, "agora": sum(p["pagar_agora"] for p in itens),
            "tem_aluguel": any(p["tipo"] == "ALUGUEL" for p in itens), "tem_venda": any(p["tipo"] == "VENDA" for p in itens)}


# --------------------------------------------------------------------------- #
# Sacola
# --------------------------------------------------------------------------- #
@router.post("/site/sacola/adicionar")
def adicionar(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    it = {"peca": form.get("peca"), "tipo": form.get("tipo"),
          "retirada": form.get("retirada") or None, "devolucao": form.get("devolucao") or None,
          "ajuste": form.get("tipo") == "VENDA" and form.get("ajuste") == "1"}
    with db.transacao() as cur:
        loja = _loja(cur)
        if not loja["sacola_ativa"]:
            return RedirectResponse("/site", status_code=303)
        p, erro = conferir_item(cur, it)
        cur.execute("SELECT slug FROM produtos WHERE id::text = %s", (str(it["peca"])[:40],))
        slug = (cur.fetchone() or {}).get("slug")
    volta = f"/site/p/{slug}" if slug else "/site"
    if erro:
        from urllib.parse import quote
        return RedirectResponse(f"{volta}?sacola_erro={quote(erro)}#sacola", status_code=303)
    sacola = [x for x in (request.session.get("sacola") or []) if x.get("peca") != p["id"]]
    if len(sacola) >= MAX_ITENS:
        return RedirectResponse(f"{volta}?sacola_erro=Até+{MAX_ITENS}+peças+por+compra#sacola", status_code=303)
    sacola.append({"peca": p["id"], "tipo": p["tipo"],
                   "retirada": p.get("retirada").isoformat() if p.get("retirada") else None,
                   "devolucao": p.get("devolucao").isoformat() if p.get("devolucao") else None,
                   "ajuste": bool(p.get("ajuste"))})
    request.session["sacola"] = sacola
    return RedirectResponse("/site/sacola", status_code=303)


@router.post("/site/sacola/remover")
def remover(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    request.session["sacola"] = [x for x in (request.session.get("sacola") or []) if x.get("peca") != form.get("peca")]
    return RedirectResponse("/site/sacola", status_code=303)


def _tela(request, cur, erro=None, status=200, v=None):
    loja = _loja(cur)
    cur.execute("""SELECT sacola_pagamento, cep_origem, pacote_peso_g, pacote_altura_cm, pacote_largura_cm,
                          pacote_comprimento_cm, entrega_retirada, entrega_envio FROM lojas WHERE id = ctx_loja()""")
    regras = cur.fetchone()
    regras["me"] = frete.acesso(cur)
    v = v or {}
    itens, problemas = _itens(cur, request.session.get("sacola") or [])
    pagar = v.get("pagar") if v.get("pagar") in ("ENTRADA", "TOTAL") and regras["sacola_pagamento"] == "ENTRADA_OU_TOTAL" else "TOTAL"
    if regras["sacola_pagamento"] == "ENTRADA_OU_TOTAL" and not v.get("pagar"):
        pagar = "ENTRADA"
    valores = _valores(cur, itens, pagar)
    lista_cidades = frete.cidades(cur, aluguel=valores["tem_aluguel"])
    envio_ok = frete.envio_disponivel(regras) and not valores["tem_aluguel"]
    opcoes_envio, erro_frete = [], None
    cep = frete.cep_limpo(v.get("cep"))
    if envio_ok and cep and itens:
        try:
            opcoes_envio = frete.cotar(regras, cep, valores["total"], len(itens))
        except frete.ErroFrete as e:
            erro_frete = str(e)
    conta = contas.conta_atual(cur, request.session, request.headers.get("user-agent"))
    return _pagina(request, "site/sacola.html", cur, itens=itens, problemas=problemas, valores=valores, pagar=pagar,
                   regras=regras, cidades=lista_cidades, envio_ok=envio_ok, opcoes_envio=opcoes_envio,
                   erro_frete=erro_frete, v=v, erro=erro, conta=conta, UFS=UFS, pagamento_ok=bool(po.configurado(cur)),
                   status=status, sacola_ativa=loja["sacola_ativa"])


@router.get("/site/sacola")
def ver(request: Request):
    with db.transacao() as cur:
        cur.execute("SELECT liberar_pedidos_vencidos()")
        return _tela(request, cur, v=dict(request.query_params))


# --------------------------------------------------------------------------- #
# Fechar e pagar
# --------------------------------------------------------------------------- #
def _endereco(form: dict) -> tuple[dict | None, str | None]:
    e = {"cep": frete.cep_limpo(form.get("cep")), "rua": (form.get("rua") or "").strip()[:120],
         "numero": (form.get("numero") or "").strip()[:12], "complemento": (form.get("complemento") or "").strip()[:60],
         "bairro": (form.get("bairro") or "").strip()[:60], "cidade": (form.get("cidade") or "").strip()[:80],
         "uf": (form.get("uf") or "").strip().upper()[:2]}
    if not e["rua"] or not e["numero"] or not e["bairro"]:
        return None, "Preencha rua, número e bairro para a entrega."
    return e, None


@router.post("/site/sacola/fechar")
def fechar(request: Request, form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    ua = request.headers.get("user-agent")
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, ua)
        if not conta:
            return RedirectResponse("/site/conta/entrar?proximo=/site/sacola", status_code=303)
    chave = form.get("chave") or ""
    try:
        chave = str(uuid.UUID(chave))
    except ValueError:
        chave = str(uuid.uuid4())
    erro, compra_id = None, None
    try:
        with db.transacao() as cur:
            cur.execute("SELECT id::text FROM compras_site WHERE chave_idempotencia = %s", (chave,))
            ja = cur.fetchone()
            if ja:                                     # clique duplo
                return RedirectResponse(f"/site/sacola/pronta/{ja['id']}", status_code=303)
            compra_id = _criar_compra(request, cur, conta, form, chave)
    except Recusa as e:
        erro = str(e)
    except po.ErroProvedor as e:
        erro = str(e)
    except psycopg.Error as e:
        erro = mensagem_do_banco(e)
    if erro:
        with db.transacao() as cur:
            return _tela(request, cur, erro=erro, status=400, v=form)
    request.session["sacola"] = []
    return RedirectResponse(f"/site/sacola/pronta/{compra_id}", status_code=303)


def _criar_compra(request, cur, conta, form, chave) -> str:
    loja = _loja(cur)
    if not loja["sacola_ativa"]:
        raise Recusa("As compras pelo site estão desligadas agora.")
    if not po.configurado(cur):
        raise Recusa("O pagamento online está indisponível agora. Chame a loja no WhatsApp.")
    cur.execute("""SELECT sacola_pagamento, sacola_minutos, cep_origem, pacote_peso_g, pacote_altura_cm, pacote_largura_cm,
                          pacote_comprimento_cm, entrega_retirada, entrega_envio FROM lojas WHERE id = ctx_loja()""")
    regras = cur.fetchone()
    regras["me"] = frete.acesso(cur)
    itens, problemas = _itens(cur, request.session.get("sacola") or [])
    if problemas:
        raise Recusa(problemas[0]["erro"])
    if not itens:
        raise Recusa("Sua sacola está vazia.")
    if form.get("aceito") != "1":
        raise Recusa("Marque que leu e concorda com as regras de aluguel, troca e cancelamento.")
    pagar = form.get("pagar") if regras["sacola_pagamento"] == "ENTRADA_OU_TOTAL" and form.get("pagar") in ("ENTRADA", "TOTAL") else "TOTAL"
    valores = _valores(cur, itens, pagar)

    entrega = form.get("entrega")
    frete_c, cidade_id, endereco, cep, serv_id, serv_nome, prazo = 0, None, None, None, None, None, None
    if entrega == "RETIRADA":
        if not regras["entrega_retirada"]:
            raise Recusa("Escolha a entrega.")
    elif entrega == "LOCAL":
        cid = form.get("cidade_entrega") or ""
        cidade = next((c for c in frete.cidades(cur, aluguel=valores["tem_aluguel"]) if c["id"] == cid), None)
        if not cidade:
            raise Recusa("Escolha a cidade da entrega.")
        endereco, erro = _endereco(form)
        if erro:
            raise Recusa(erro)
        endereco.update(cidade=cidade["cidade"], uf=cidade["uf"])
        frete_c, cidade_id = cidade["valor_centavos"], cidade["id"]
    elif entrega == "ENVIO":
        if valores["tem_aluguel"] or not frete.envio_disponivel(regras):
            raise Recusa("Envio pelos Correios/transportadora é só para compra.")
        cep = frete.cep_limpo(form.get("cep"))
        if not cep:
            raise Recusa("Digite o CEP e calcule o frete.")
        try:
            opcoes = frete.cotar(regras, cep, valores["total"], len(itens))
        except frete.ErroFrete as e:
            raise Recusa(str(e))
        try:
            escolhido = next(o for o in opcoes if o["id"] == int(form.get("servico") or 0))
        except (StopIteration, ValueError):
            raise Recusa("Escolha uma opção de frete.")
        endereco, erro = _endereco(form)
        if erro:
            raise Recusa(erro)
        if not endereco["cidade"] or endereco["uf"] not in UFS:
            raise Recusa("Preencha a cidade e o estado (UF).")
        frete_c, serv_id, serv_nome, prazo = escolhido["preco_centavos"], escolhido["id"], escolhido["nome"], escolhido["prazo_dias"]
    else:
        raise Recusa("Escolha como quer receber.")

    cur.execute("SELECT usuario_site() AS u")
    site_uid = cur.fetchone()["u"]
    cur.execute("SELECT set_config('app.usuario_id', %s, true)", (str(site_uid),))
    cfg = pagamento.config_vigente(cur)
    expira = datetime.now(timezone.utc) + timedelta(minutes=regras["sacola_minutos"])
    cur.execute("""INSERT INTO compras_site (loja_id, conta_id, cliente_id, pagar, valor_itens_centavos, frete_centavos,
                                             valor_total_centavos, entrega, entrega_cidade_id, endereco_c, cep, frete_servico,
                                             frete_servico_id, frete_prazo_dias, expira_em, chave_idempotencia)
                   VALUES (ctx_loja(), %s, %s, %s, 1, %s, 1 + %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                (conta["id"], conta["cliente_id"], pagar, frete_c, frete_c, entrega, cidade_id,
                 cripto.cifrar(json.dumps(endereco, ensure_ascii=False), "compras.endereco") if endereco else None,
                 cep, serv_nome, serv_id, prazo, expira, chave))
    compra_id = cur.fetchone()["id"]
    total_agora = 0
    for p in itens:
        pedido_id, pagar_item = None, p["pagar_agora"]
        if p["tipo"] == "ALUGUEL":         # a peça fica presa para a cliente enquanto o pagamento está aberto
            cur.execute(
                """INSERT INTO pedidos (loja_id, tipo, com_ajuste, produto_id, cliente_id, config_id, status,
                                        valor_peca_centavos, valor_ajuste_centavos, valor_total_centavos,
                                        entrada_minima_centavos, max_parcelas_permitidas, inicio_bloqueio,
                                        data_retirada, data_devolucao, chave_idempotencia, criado_por, pagar_pelo_link)
                   VALUES (ctx_loja(), 'ALUGUEL', true, %s, %s, %s, 'NOVO', %s, 0, 0, 0, 1,
                           GREATEST((now() AT TIME ZONE 'America/Sao_Paulo')::date, %s::date - %s), %s, %s, %s, %s, true)
                   RETURNING id::text, entrada_minima_centavos, valor_total_centavos""",
                (p["id"], conta["cliente_id"], cfg["id"], p["valor"], p["retirada"], DIAS_ANTES_PROVA, p["retirada"], p["devolucao"],
                 str(uuid.uuid5(uuid.UUID(compra_id), p["id"])), str(site_uid)))
            novo = cur.fetchone()
            pedido_id = novo["id"]
            pagar_item = novo["valor_total_centavos"] if pagar == "TOTAL" else novo["entrada_minima_centavos"]
        ajuste = p.get("ajuste") or 0 if p["tipo"] == "VENDA" else 0
        cur.execute("""INSERT INTO compras_itens (loja_id, compra_id, produto_id, tipo, data_retirada, data_devolucao,
                                                  valor_peca_centavos, ajuste_centavos, valor_pagar_centavos, pedido_id)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (compra_id, p["id"], p["tipo"], p.get("retirada"), p.get("devolucao"), p["valor"] - ajuste, ajuste,
                     pagar_item, pedido_id))
        total_agora += pagar_item
    cur.execute("""UPDATE compras_site SET valor_itens_centavos = %s, valor_total_centavos = %s + frete_centavos
                    WHERE id = %s RETURNING valor_total_centavos""", (total_agora, total_agora, compra_id))
    valor_link = cur.fetchone()["valor_total_centavos"]
    token = po.token_da_loja(cur)
    titulo = f"{loja['nome']} - " + ", ".join(f"{'Aluguel' if p['tipo'] == 'ALUGUEL' else 'Compra'} {p['sku']}"
                                             + (" com ajuste" if p.get("ajuste") else "") for p in itens)
    maximo = min(cfg["max_parcelas_aluguel"] if valores["tem_aluguel"] else cfg["max_parcelas_venda"], 12)
    id_externo, url = po.criar_preferencia(token, f"C-{compra_id}", titulo, valor_link, maximo, expira)
    cur.execute("UPDATE compras_site SET id_externo = %s, url_externa = %s WHERE id = %s", (id_externo, url, compra_id))
    cur.execute("""INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados)
                   VALUES (ctx_loja(), %s, 'compra_site_criada', 'compra', %s, %s::jsonb)""",
                (str(site_uid), compra_id, json.dumps({"itens": len(itens), "valor": valor_link, "entrega": entrega})))
    return compra_id


@router.get("/site/sacola/pronta/{cid}")
def pronta(request: Request, cid: str):
    """Depois de fechar: página nossa que leva ao Mercado Pago (a política de segurança não deixa um formulário
    pular direto para outro site)."""
    try:
        cid = str(uuid.UUID(cid))
    except ValueError:
        return RedirectResponse("/site/conta", status_code=303)
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, request.headers.get("user-agent"))
        if not conta:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        cur.execute("""SELECT id::text, status, valor_total_centavos, frete_centavos, entrega,
                              to_char(expira_em AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS ate
                         FROM compras_site WHERE id = %s AND conta_id = %s""", (cid, conta["id"]))
        c = cur.fetchone()
        if not c:
            return RedirectResponse("/site/conta", status_code=303)
        return _pagina(request, "site/sacola_pronta.html", cur, c=c)


@router.get("/site/sacola/pagar/{cid}")
def pagar(request: Request, cid: str):
    try:
        cid = str(uuid.UUID(cid))
    except ValueError:
        return RedirectResponse("/site/conta", status_code=303)
    with db.transacao() as cur:
        conta = contas.conta_atual(cur, request.session, request.headers.get("user-agent"))
        if not conta:
            return RedirectResponse("/site/conta/entrar", status_code=303)
        cur.execute("""SELECT status, url_externa, expira_em > now() AS valendo FROM compras_site
                        WHERE id = %s AND conta_id = %s""", (cid, conta["id"]))
        c = cur.fetchone()
    if c and c["status"] == "ABERTA" and c["valendo"] and c["url_externa"]:
        return RedirectResponse(c["url_externa"], status_code=303)
    return RedirectResponse("/site/conta", status_code=303)
