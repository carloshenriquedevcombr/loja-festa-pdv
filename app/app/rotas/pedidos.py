"""Pedidos: aluguel, venda simples e venda com ajuste; entrada, saldo, etapas e cancelamento."""
import re
import uuid
from urllib.parse import quote
from datetime import date, time, timedelta
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import crediario, cripto, db, eventos, pagamento, pagamento_online, pessoas
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import (TZ, brl, chave_valida, checar_csrf, e_chave_repetida, ler_reais, mensagem_do_banco,
                   nova_chave, tela, telefone, volta_segura)

router = APIRouter()
FINAIS = ("CANCELADO", "CONCLUIDO", "RETIRADO", "VENDIDO")
FINAIS_SQL = "('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')"
DIAS_ANTES_PROVA = 7          # sem data de 1ª prova: 7 dias antes da retirada (a peça fica reservada desde então)
DIAS_ANTES_PROVA2 = 2         # sem data de 2ª prova: 2 dias antes da retirada
COMPROMISSOS = {"PROVA_1": "1ª prova", "PROVA_2": "2ª prova", "RETIRADA": "Retirada", "DEVOLUCAO": "Devolução"}

TIPOS = {"ALUGUEL": "Alugar", "VENDA_SIMPLES": "Vender (leva agora)", "VENDA_AJUSTE": "Vender com ajuste"}
TIPO_NOME = {"ALUGUEL": "Aluguel", "VENDA_SIMPLES": "Venda", "VENDA_AJUSTE": "Venda com ajuste"}

STATUS = {"AGUARDANDO_PAGAMENTO": "Aguardando pagamento", "RESERVADO": "Reservado", "PROVA_1": "1ª prova feita", "EM_AJUSTE": "Em ajuste",
          "PROVA_2": "2ª prova", "PRONTO": "Pronto para retirar", "ALUGADO": "Com a cliente",
          "DEVOLVIDO": "Devolvido", "EM_LIMPEZA": "Na limpeza", "CONCLUIDO": "Concluído",
          "RETIRADO": "Entregue", "VENDIDO": "Vendido", "CANCELADO": "Cancelado"}

# (de, para) -> (texto do botão, permissões). Dona e Gerente (pedidos.cancelar) podem todas.
ETAPAS = {
    ("RESERVADO", "PROVA_1"):   ("Cliente fez a 1ª prova",        ("agenda.editar", "ajustes.atualizar")),
    ("PROVA_1", "EM_AJUSTE"):   ("Mandar para o ajuste",          ("agenda.editar", "ajustes.atualizar")),
    ("EM_AJUSTE", "PROVA_2"):   ("Ajuste pronto: chamar 2ª prova", ("ajustes.atualizar",)),
    ("PROVA_2", "EM_AJUSTE"):   ("Precisa de mais ajuste",        ("agenda.editar", "ajustes.atualizar")),
    ("PROVA_2", "PRONTO"):      ("Aprovado: pronto para retirar", ("agenda.editar", "ajustes.atualizar")),
    ("PRONTO", "ALUGADO"):      ("Cliente retirou",               ("entregas.registrar",)),
    ("PRONTO", "RETIRADO"):     ("Cliente levou",                 ("entregas.registrar",)),
    ("ALUGADO", "DEVOLVIDO"):   ("Cliente devolveu",              ("entregas.registrar",)),
    ("DEVOLVIDO", "EM_LIMPEZA"): ("Levei para limpar",            ("limpeza.registrar",)),
    ("EM_LIMPEZA", "CONCLUIDO"): ("Voltou da limpeza",            ("limpeza.registrar",)),
}


def etapas_possiveis(cur, usuario, pedido) -> list[dict]:
    if pedido["status"] == "AGUARDANDO_PAGAMENTO":
        return []                       # vira Reservado sozinho quando o pagamento entra
    cur.execute("SELECT para FROM transicoes_permitidas WHERE fluxo = %s AND de = %s",
                (pedido["fluxo"], pedido["status"]))
    saida = []
    for linha in cur.fetchall():
        rotulo, perms = ETAPAS.get((pedido["status"], linha["para"]), (linha["para"], ()))
        if pode(usuario, *perms, "pedidos.cancelar"):
            saida.append({"para": linha["para"], "rotulo": rotulo,
                          "precisa_quitar": linha["para"] in ("ALUGADO", "RETIRADO")})
    return saida


def _hoje() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ)).date()


def _data(texto: str | None) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def _hora(texto: str | None, padrao: time = time(10, 0)) -> time | None:
    if not texto:
        return padrao
    try:
        return time.fromisoformat(texto)
    except ValueError:
        return None


def _peca(cur, pid) -> dict | None:
    try:
        pid = UUID(str(pid))
    except ValueError:
        return None
    cur.execute("""SELECT ps.*, ps.id::text AS id FROM produtos_situacao ps WHERE ps.id = %s""", (pid,))
    return cur.fetchone()


# --------------------------------------------------------------------------- #
# Novo pedido
# --------------------------------------------------------------------------- #
def _tipos_da_peca(p) -> dict:
    t = {}
    if p["pode_alugar"]:
        t["ALUGUEL"] = p["preco_aluguel_centavos"]
    if p["pode_vender"]:
        t["VENDA_SIMPLES"] = p["preco_venda_centavos"]
        t["VENDA_AJUSTE"] = p["preco_venda_centavos"]
    return t


def _form_novo(request, usuario, cur, p, v=None, erro=None, status=200, achadas=None, busca_cliente=""):
    cfg = pagamento.config_vigente(cur)
    v = v or {}
    v.setdefault("chave", nova_chave())
    tipos = _tipos_da_peca(p)
    if len(tipos) == 1 or "tipo" not in v:
        v.setdefault("tipo", next(iter(tipos)))
    return tela(request, "pedido_novo.html", usuario, status=status, p=p, v=v, erro=erro, tipos=tipos,
                link_online=bool(pagamento_online.configurado(cur)), horas_link=pagamento_online.HORAS_VALIDADE,
                TIPOS=TIPOS, cfg=cfg,
                entradas={c: pagamento.entrada(cfg, preco, "ALUGUEL" if c == "ALUGUEL" else "VENDA") for c, preco in tipos.items()},
                regra_entrada={"aluguel": pagamento.texto_regra(cfg["entrada_aluguel_tipo"], cfg["entrada_aluguel_valor"]),
                               "venda": pagamento.texto_regra(cfg["entrada_venda_tipo"], cfg["entrada_venda_valor"])},
                max_parcelas=max(cfg["max_parcelas_aluguel"], cfg["max_parcelas_venda"]),
                achadas=achadas, busca_cliente=busca_cliente, **pagamento.opcoes(cur))


def _validar_novo(cur, p, form) -> tuple[dict | None, str | None]:
    """Confere tudo antes da conferência e de novo antes de gravar."""
    cfg = pagamento.config_vigente(cur)
    tipos = _tipos_da_peca(p)
    tipo = form.get("tipo")
    if tipo not in tipos:
        return None, "Escolha se é aluguel ou venda."
    if p["situacao_hoje"] in ("VENDIDO", "ARQUIVADO"):
        return None, "Esta peça não está mais disponível."
    cur.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS hoje")
    hoje = cur.fetchone()["hoje"]

    d = {"tipo": tipo, "cfg": cfg, "hoje": hoje, "valor_peca": tipos[tipo], "valor_ajuste": 0,
         "cliente_nome": (form.get("cliente_nome") or "").strip()[:120],
         "cliente_telefone": re.sub(r"[^\d]", "", form.get("cliente_telefone") or "")[:13],
         "data_festa": _data(form.get("data_festa")), "data_prova": _data(form.get("data_prova")),
         "data_prova2": _data(form.get("data_prova2")),
         "data_retirada": _data(form.get("data_retirada")), "data_devolucao": _data(form.get("data_devolucao"))}

    # Todo pedido começa com a cliente: escolhida no cadastro ou nome + WhatsApp (cadastro novo)
    if not form.get("cliente_id"):
        if len(d["cliente_nome"].split()) < 2:
            return None, "Escolha a cliente (procure acima) ou escreva nome e sobrenome da cliente nova."
        if len(d["cliente_telefone"]) < 10:
            return None, "Cliente nova: telefone com DDD, por exemplo (19) 99999-9999."
    if tipo in ("ALUGUEL", "VENDA_AJUSTE"):
        if not d["data_retirada"]:
            return None, "Escolha a data de retirada."
        if d["data_retirada"] < hoje:
            return None, "A data de retirada já passou."
        # Agenda do processo: 1ª prova -> 2ª prova -> retirada (-> devolução no aluguel)
        horas = {k: _hora(form.get(k)) for k in ("hora_prova", "hora_prova2", "hora_retirada", "hora_devolucao")}
        if None in horas.values():
            return None, "Algum horário ficou errado. Use o formato 14:30."
        p1 = d["data_prova"] or max(hoje, d["data_retirada"] - timedelta(days=DIAS_ANTES_PROVA))
        p2 = d["data_prova2"] or max(p1, d["data_retirada"] - timedelta(days=DIAS_ANTES_PROVA2))
        if not hoje <= p1 <= d["data_retirada"]:
            return None, "A 1ª prova tem que ser entre hoje e a retirada."
        if not p1 <= p2 <= d["data_retirada"]:
            return None, "A 2ª prova tem que ser depois da 1ª e antes da retirada."
        d["agenda"] = [("PROVA_1", p1, horas["hora_prova"]), ("PROVA_2", p2, horas["hora_prova2"]),
                       ("RETIRADA", d["data_retirada"], horas["hora_retirada"])]
        d["inicio_bloqueio"] = p1
    if tipo == "ALUGUEL":
        if not d["data_devolucao"]:
            return None, "Escolha a data de devolução."
        if d["data_devolucao"] < d["data_retirada"]:
            return None, "A devolução não pode ser antes da retirada."
        d["agenda"].append(("DEVOLUCAO", d["data_devolucao"], horas["hora_devolucao"]))
    if tipo == "VENDA_AJUSTE":
        d["valor_ajuste"] = ler_reais(form.get("valor_ajuste")) or 0
        if not d["valor_ajuste"]:
            return None, "Digite o valor do ajuste."

    d["total"] = d["valor_peca"] + d["valor_ajuste"]
    d["entrada_min"] = d["total"] if tipo == "VENDA_SIMPLES" else \
        pagamento.entrada(cfg, d["total"], "ALUGUEL" if tipo == "ALUGUEL" else "VENDA")
    max_parc = cfg["max_parcelas_aluguel"] if tipo == "ALUGUEL" else cfg["max_parcelas_venda"]
    if form.get("forma") == "LINK":
        # Pedido do WhatsApp: a cliente paga a entrada (ou tudo) pelo link do Mercado Pago
        if tipo == "VENDA_SIMPLES":
            return None, "Venda para levar na hora é paga no balcão. Para o link, use aluguel ou venda com ajuste."
        if not pagamento_online.configurado(cur):
            return None, "O pagamento online ainda não foi configurado."
        valor = ler_reais(form.get("valor"))
        if not valor:
            return None, "Digite o valor que a cliente vai pagar pelo link."
        if valor > d["total"]:
            return None, f"O valor passa do total do pedido ({brl(d['total'])})."
        if valor < d["entrada_min"]:
            return None, f"A entrada mínima é {brl(d['entrada_min'])}."
        if not chave_valida(form.get("chave")):
            return None, "A página ficou aberta muito tempo. Comece de novo."
        d["link"], d["valor_link"], d["pag"], d["falta"] = True, valor, None, d["total"]
        return d, None
    pag, erro = pagamento.ler(form, cur, max_parc, cfg["id"])
    if erro:
        return None, erro
    if pag.valor > d["total"]:
        return None, f"O valor passa do total do pedido ({brl(d['total'])})."
    if pag.valor < d["entrada_min"]:
        return None, (f"Venda sem ajuste é paga na hora: {brl(d['total'])}." if tipo == "VENDA_SIMPLES"
                      else f"A entrada mínima é {brl(d['entrada_min'])}.")
    if not chave_valida(form.get("chave")):
        return None, "A página ficou aberta muito tempo. Comece de novo."
    d["pag"] = pag
    d["falta"] = d["total"] - pag.valor
    return d, None


@router.get("/pedidos/novo")
def novo_form(request: Request, peca: UUID | None = None, cliente: UUID | None = None, busca_cliente: str = "",
              usuario: dict = Depends(requer("pedidos.criar"))):
    if not peca:
        return RedirectResponse("/pecas?escolher=1" + (f"&cliente={cliente}" if cliente else ""), status_code=303)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = _peca(cur, peca)
        if not p or not _tipos_da_peca(p):
            return RedirectResponse("/pecas?escolher=1", status_code=303)
        v = {}
        if cliente:
            cur.execute("SELECT id::text, nome, telefone_c FROM clientes WHERE id = %s", (cliente,))
            c = pessoas.abrir_cliente(cur.fetchone())
            if c:
                v = {"cliente_id": c["id"], "cliente_nome": c["nome"], "cliente_telefone": telefone(c["telefone"]) if c["telefone"] else ""}
        achadas = None
        if busca_cliente.strip():
            from .caixa import filtro_cliente
            filtro, params = filtro_cliente(busca_cliente.strip()[:60])
            cur.execute(f"""SELECT cl.id::text AS id, cl.nome, cl.telefone_fim,
                                   (SELECT count(*) FROM pedidos x WHERE x.cliente_id = cl.id) AS pedidos
                              FROM clientes cl WHERE {filtro} ORDER BY cl.nome LIMIT 15""", params)
            achadas = cur.fetchall()
        return _form_novo(request, usuario, cur, p, v=v, achadas=achadas, busca_cliente=busca_cliente[:60])


@router.post("/pedidos/novo")
def novo_corrigir(request: Request, usuario: dict = Depends(requer("pedidos.criar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = _peca(cur, form.get("peca"))
        if not p:
            return RedirectResponse("/pecas?escolher=1", status_code=303)
        return _form_novo(request, usuario, cur, p, v=form)


@router.post("/pedidos/novo/conferir")
def novo_conferir(request: Request, usuario: dict = Depends(requer("pedidos.criar")),
                  form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        p = _peca(cur, form.get("peca"))
        if not p:
            return RedirectResponse("/pecas?escolher=1", status_code=303)
        d, erro = _validar_novo(cur, p, form)
        if erro:
            return _form_novo(request, usuario, cur, p, v=form, erro=erro, status=400)
    campos = {k: form.get(k, "") for k in ("peca", "tipo", "cliente_id", "cliente_nome", "cliente_telefone", "data_festa",
                                           "data_prova", "data_prova2", "data_retirada", "data_devolucao",
                                           "hora_prova", "hora_prova2", "hora_retirada", "hora_devolucao",
                                           "valor_ajuste", "chave")}
    if d.get("link"):
        campos.update({"forma": "LINK", "valor": f"{d['valor_link'] / 100:.2f}".replace(".", ",")})
    else:
        campos.update(d["pag"].campos())
    return tela(request, "pedido_conferir.html", usuario, p=p, d=d, TIPOS=TIPO_NOME, campos=campos,
                COMPROMISSOS=COMPROMISSOS)


def _cliente(cur, nome, telefone, cliente_id=None) -> str:
    if cliente_id:
        try:
            cur.execute("SELECT id::text FROM clientes WHERE id = %s", (UUID(str(cliente_id)),))
            achou = cur.fetchone()
        except ValueError:
            achou = None
        if achou:
            return achou["id"]
    achou = pessoas.achar_por_telefone(cur, telefone)
    if achou:
        return achou["id"]
    return pessoas.gravar_cliente(cur, {"nome": nome, "telefone": telefone or None}, consentimento=False)


@router.post("/pedidos/novo/gravar")
def novo_gravar(request: Request, usuario: dict = Depends(requer("pedidos.criar")),
                form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    chave = chave_valida(form.get("chave"))
    pedido_id, erro = None, None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            p = _peca(cur, form.get("peca"))
            if not p:
                return RedirectResponse("/pecas?escolher=1", status_code=303)
            d, erro = _validar_novo(cur, p, form)
            if not erro:
                tipo, venda_simples = d["tipo"], d["tipo"] == "VENDA_SIMPLES"
                cliente_id = _cliente(cur, d["cliente_nome"], d["cliente_telefone"], form.get("cliente_id"))
                cur.execute(
                    """INSERT INTO pedidos
                         (loja_id, tipo, com_ajuste, produto_id, cliente_id, config_id, status,
                          valor_peca_centavos, valor_ajuste_centavos, valor_total_centavos,
                          entrada_minima_centavos, max_parcelas_permitidas, data_festa, inicio_bloqueio,
                          data_retirada, data_devolucao, chave_idempotencia, criado_por, pagar_pelo_link)
                       VALUES (ctx_loja(), %s, %s, %s, %s, %s, 'NOVO', %s, %s, 0, 0, 1, %s,
                               COALESCE(%s, (now() AT TIME ZONE 'America/Sao_Paulo')::date), %s, %s, %s, %s, %s)
                       RETURNING id::text, max_parcelas_permitidas""",
                    ("ALUGUEL" if tipo == "ALUGUEL" else "VENDA", tipo != "VENDA_SIMPLES", p["id"], cliente_id,
                     d["cfg"]["id"], d["valor_peca"], d["valor_ajuste"], d["data_festa"],
                     d.get("inicio_bloqueio"), d["data_retirada"],
                     d["data_devolucao"] if tipo == "ALUGUEL" else None, chave, usuario["id"], bool(d.get("link"))))
                novo = cur.fetchone()
                pedido_id = novo["id"]
                for compromisso, dia, hora in d.get("agenda", []):
                    cur.execute("""INSERT INTO agendamentos (loja_id, pedido_id, tipo, inicio)
                                   VALUES (ctx_loja(), %s, %s, (%s::date + %s::time) AT TIME ZONE 'America/Sao_Paulo')""",
                                (pedido_id, compromisso, dia, hora))
                pag = d["pag"]
                if d.get("link"):
                    link_novo = pagamento_online.criar_link(cur, {"id": pedido_id, "max_parcelas_permitidas": novo["max_parcelas_permitidas"]},
                                                d["valor_link"], usuario["id"], f"{TIPO_NOME[tipo]} {p['sku']} - {p['nome']}")
                else:
                    pagamento.gravar(cur, pag, tipo="ENTRADA",
                                     natureza="PAGAMENTO_INTEGRAL" if pag.valor == d["total"] else "ENTRADA_RESERVA",
                                     descricao=f"{TIPO_NOME[tipo]} {p['sku']}", chave=str(uuid.uuid5(UUID(chave), "pagamento")),
                                     usuario_id=usuario["id"], pedido_id=pedido_id)
                auditar(cur, request, "pedido_criado", usuario["id"], "pedido", pedido_id,
                        {"tipo": tipo, "sku": p["sku"], "total": d["total"], "link": bool(d.get("link"))})
                from .pagamentos import base_publica
                base = base_publica(request)
                eventos.pedido_criado(cur, pedido_id, base,
                                      f"{base}/pagar/{link_novo['id']}" if d.get("link") else None)
    except pagamento_online.ErroProvedor as e:
        erro = str(e)
    except psycopg.Error as e:
        if e_chave_repetida(e):
            with db.transacao(usuario_id=usuario["id"]) as cur:
                cur.execute("SELECT id::text FROM pedidos WHERE chave_idempotencia = %s", (chave,))
                linha = cur.fetchone()
            if linha:
                return RedirectResponse(f"/pedidos/{linha['id']}", status_code=303)
        erro = mensagem_do_banco(e)
    if erro:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            p = _peca(cur, form.get("peca"))
            if not p:
                return RedirectResponse("/pecas?escolher=1", status_code=303)
            return _form_novo(request, usuario, cur, p, v=form, erro=erro, status=400)
    return RedirectResponse(f"/pedidos/{pedido_id}?novo=1", status_code=303)


# --------------------------------------------------------------------------- #
# Lista e detalhe
# --------------------------------------------------------------------------- #
@router.get("/pedidos")
def lista(request: Request, filtro: str = "ativos", q: str = "", usuario: dict = Depends(requer("pedidos.ver"))):
    filtro = filtro if filtro in ("ativos", "hoje", "atrasados", "site", "todos") else "ativos"
    from .. import codigos
    if codigos.parece_codigo_pedido(q):                     # leitor de código de barras no checklist
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT id::text FROM pedidos WHERE replace(id::text, '-', '') LIKE %s LIMIT 2", (q.strip().lower() + "%",))
            achados = cur.fetchall()
        if len(achados) == 1:
            return RedirectResponse(f"/pedidos/{achados[0]['id']}", status_code=303)
    condicoes, params = [], {"q": f"%{q.strip()[:60]}%"}
    if filtro == "ativos":
        condicoes.append(f"pe.status NOT IN {FINAIS_SQL}")
    elif filtro == "atrasados":
        condicoes.append(f"pe.status = 'ALUGADO' AND pe.data_devolucao < (now() AT TIME ZONE '{TZ}')::date")
    elif filtro == "site":
        condicoes.append("EXISTS (SELECT 1 FROM compras_itens ci WHERE ci.pedido_id = pe.id)")
    elif filtro == "hoje":
        condicoes.append(f"""((pe.status = 'PRONTO' AND pe.data_retirada <= (now() AT TIME ZONE '{TZ}')::date)
                          OR (pe.status = 'ALUGADO' AND pe.data_devolucao <= (now() AT TIME ZONE '{TZ}')::date))""")
    if q.strip():
        condicoes.append("(f_unaccent(lower(coalesce(cl.nome,''))) LIKE f_unaccent(lower(%(q)s)) OR pr.sku ILIKE %(q)s)")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"""
            SELECT pe.id::text, pe.tipo, pe.com_ajuste, pe.status, pe.data_retirada, pe.data_devolucao,
                   pe.valor_total_centavos, pr.sku, pr.nome AS peca, cl.nome AS cliente,
                   pe.valor_total_centavos - valor_pago(pe.id) AS saldo,
                   (pe.status = 'ALUGADO' AND pe.data_devolucao < (now() AT TIME ZONE '{TZ}')::date) AS atrasado,
                   (SELECT cs.entrega FROM compras_itens ci JOIN compras_site cs ON cs.id = ci.compra_id
                     WHERE ci.pedido_id = pe.id LIMIT 1) AS do_site
              FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
              LEFT JOIN clientes cl ON cl.id = pe.cliente_id
             {'WHERE ' + ' AND '.join(condicoes) if condicoes else ''}
             ORDER BY COALESCE(pe.data_retirada, pe.criado_em::date) {'DESC' if filtro == 'todos' else 'ASC'}
             LIMIT 200""", params)
        pedidos = cur.fetchall()
    return tela(request, "pedidos_lista.html", usuario, pedidos=pedidos, filtro=filtro, q=q, STATUS=STATUS)


def _pedido(cur, pid):
    cur.execute("""SELECT pe.*, pe.id::text AS id, pr.sku, pr.nome AS peca, pr.id::text AS produto,
                          cl.nome AS cliente, cl.telefone_c, cl.email_c, cl.id::text AS cliente_id,
                          pe.valor_total_centavos - valor_pago(pe.id) AS saldo, valor_pago(pe.id) AS pago
                     FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                     LEFT JOIN clientes cl ON cl.id = pe.cliente_id WHERE pe.id = %s""", (pid,))
    return cripto.abrir(cur.fetchone(), "clientes", "telefone", "email")


def _tela_detalhe(request, usuario, pid, erro=None, status=200, novo=0):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pagamento_online.liberar_vencidos(cur)
        pe = _pedido(cur, pid)
        if not pe:
            return tela(request, "erro.html", usuario, status=404, titulo="Pedido não encontrado")
        cur.execute(f"""SELECT t.id, t.tipo, t.natureza, t.valor_centavos, t.forma_pagamento, t.parcelas,
                               to_char(t.criado_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS quando, u.nome AS quem
                          FROM transacoes t JOIN usuarios u ON u.id = t.usuario_id
                         WHERE t.pedido_id = %s ORDER BY t.id""", (pid,))
        pagamentos = cur.fetchall()
        cur.execute(f"""SELECT h.para, to_char(h.criado_em AT TIME ZONE '{TZ}', 'DD/MM HH24:MI') AS quando,
                               u.nome AS quem
                          FROM historico_status h LEFT JOIN usuarios u ON u.id = h.usuario_id
                         WHERE h.pedido_id = %s ORDER BY h.id""", (pid,))
        historico = cur.fetchall()
        etapas = [] if pe["status"] in FINAIS else etapas_possiveis(cur, usuario, pe)
        cur.execute(f"""SELECT a.id, a.tipo, a.status, a.observacao,
                               (a.inicio AT TIME ZONE '{TZ}')::date AS dia,
                               to_char(a.inicio AT TIME ZONE '{TZ}', 'HH24:MI') AS hora,
                               (a.inicio < now() AND a.status IN ('AGENDADO','REMARCADO')) AS atrasado
                          FROM agendamentos a WHERE a.pedido_id = %s
                         ORDER BY a.inicio""", (pid,))
        agenda = cur.fetchall()
        cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()["nome"]
        cur.execute(f"""SELECT to_char(pe.criado_em AT TIME ZONE '{TZ}', 'DD/MM/YYYY') AS contratado,
                               pe.data_devolucao + c.dias_limpeza AS limpeza_ate
                          FROM pedidos pe JOIN config_pagamento c ON c.id = pe.config_id WHERE pe.id = %s""", (pid,))
        marcos = cur.fetchone()
        cur.execute(f"""SELECT codigo, to_char(assinado_em AT TIME ZONE '{TZ}', 'DD/MM/YYYY HH24:MI') AS quando
                          FROM contratos WHERE pedido_id = %s""", (pid,))
        contrato_assinado = cur.fetchone()
        cur.execute("""SELECT c.id::text AS id, c.valor_centavos, c.parcelas, c.status,
                              (SELECT count(*) FROM crediario_parcelas p WHERE p.crediario_id = c.id AND p.paga_em IS NOT NULL) AS pagas
                         FROM crediarios c WHERE c.pedido_id = %s ORDER BY c.criado_em""", (pid,))
        crediarios = cur.fetchall()
        cur.execute("""SELECT cs.id::text AS id, cs.status, cs.entrega, cs.endereco_c, cs.cep, cs.frete_centavos,
                              cs.frete_servico, cs.frete_prazo_dias, cs.forma_paga, cs.etiqueta_id, cs.rastreio,
                              cs.valor_total_centavos, ec.cidade AS cidade_entrega, ec.meio, cs.observacao
                         FROM compras_itens ci JOIN compras_site cs ON cs.id = ci.compra_id
                         LEFT JOIN entrega_cidades ec ON ec.id = cs.entrega_cidade_id
                        WHERE ci.pedido_id = %s LIMIT 1""", (pid,))
        compra_site = cur.fetchone()
        if compra_site and compra_site["endereco_c"]:
            import json as _json
            compra_site["endereco"] = _json.loads(cripto.decifrar(compra_site.pop("endereco_c"), "compras.endereco"))
        cancelamento = None
        if pe["status"] == "CANCELADO":
            cur.execute("""SELECT COALESCE(sum(valor_centavos), 0) AS devolvido FROM transacoes
                            WHERE pedido_id = %s AND natureza = 'DEVOLUCAO_CLIENTE'""", (pid,))
            devolvido = cur.fetchone()["devolvido"]
            cancelamento = {"multa": pe["cancel_multa_centavos"] or 0, "devolver": pe["cancel_devolver_centavos"] or 0,
                            "regra": pe["cancel_regra"], "devolvido": devolvido,
                            "falta": max((pe["cancel_devolver_centavos"] or 0) - devolvido, 0)}
        else:
            cur.execute("SELECT * FROM prever_cancelamento(%s, false)", (pid,))
            previa = cur.fetchone()
            cancelamento = {"previa": previa}
    from .agenda import whatsapp
    from .pagamentos import base_publica, links_do_pedido, texto_whatsapp
    with db.transacao(usuario_id=usuario["id"]) as cur:
        links = links_do_pedido(cur, pid, base_publica(request))
        link_online = bool(pagamento_online.configurado(cur))
        link_pix = bool(crediario.pix_da_loja(cur))
    fone = re.sub(r"\D", "", pe["telefone"] or "")
    for l in links:
        l["whatsapp"] = (f"https://wa.me/55{fone}?text=" + quote(texto_whatsapp(loja, pe["cliente"], l["valor_centavos"],
                                                                               l["curto"], pe["sku"], l["provedor"]))
                         if len(fone) in (10, 11) and l["status"] == "ABERTO" and not l["vencido"] else None)
    for a in agenda:
        a["whatsapp"] = whatsapp({**a, "telefone": pe["telefone"], "cliente": pe["cliente"] or "", "loja": loja}) \
            if pe["cliente"] and a["status"] in ("AGENDADO", "REMARCADO", "FALTOU") else None
    return tela(request, "pedido_detalhe.html", usuario, status=status, pe=pe, pagamentos=pagamentos,
                historico=historico, etapas=etapas, erro=erro, novo=novo, STATUS=STATUS, TIPOS=TIPO_NOME,
                agenda=agenda, marcos=marcos, COMPROMISSOS=COMPROMISSOS, contrato_assinado=contrato_assinado,
                cancelamento=cancelamento, crediarios=crediarios, compra_site=compra_site,
                dias_atraso=(_hoje() - pe["data_devolucao"]).days
                if pe["status"] == "ALUGADO" and pe["data_devolucao"] and pe["data_devolucao"] < _hoje() else 0, links=links, link_online=link_online, link_pix=link_pix,
                recado_link=request.query_params.get("recado_link", ""), erro_link=request.query_params.get("erro_link", ""),
                link_novo=request.query_params.get("link", ""),
                pode_cancelar=pode(usuario, "pedidos.cancelar") and pe["status"] not in
                ("CANCELADO", "CONCLUIDO", "RETIRADO", "VENDIDO", "ALUGADO", "DEVOLVIDO", "EM_LIMPEZA"))


@router.get("/pedidos/{pid}")
def detalhe(request: Request, pid: UUID, novo: int = 0, usuario: dict = Depends(requer("pedidos.ver"))):
    return _tela_detalhe(request, usuario, pid, novo=novo)


@router.post("/pedidos/{pid}/etapa")
def mudar_etapa(request: Request, pid: UUID,
                usuario: dict = Depends(requer("pedidos.ver", "ajustes.atualizar", "limpeza.registrar")),
                form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    para, volta = form.get("para", ""), form.get("volta") or f"/pedidos/{pid}"
    volta = volta_segura(volta, f"/pedidos/{pid}")
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            pe = _pedido(cur, pid)
            if not pe:
                return RedirectResponse("/pedidos", status_code=303)
            if para not in {e["para"] for e in etapas_possiveis(cur, usuario, pe)}:
                if not pode(usuario, "pedidos.ver"):
                    return tela(request, "erro.html", usuario, status=403, titulo="Esta etapa não é do seu cargo")
                return _tela_detalhe(request, usuario, pid, erro="Esta etapa não é do seu cargo.", status=403)
            cur.execute("UPDATE pedidos SET status = %s WHERE id = %s", (para, pid))
            from .pagamentos import base_publica
            eventos.etapa(cur, str(pid), para, base_publica(request))
    except psycopg.Error as e:
        if not pode(usuario, "pedidos.ver"):
            return tela(request, "erro.html", usuario, status=400, titulo="Não foi possível mudar a etapa",
                        mensagem=mensagem_do_banco(e))
        return _tela_detalhe(request, usuario, pid, erro=mensagem_do_banco(e), status=400)
    return RedirectResponse(volta, status_code=303)


@router.post("/pedidos/{pid}/cancelar")
def cancelar(request: Request, pid: UUID, usuario: dict = Depends(requer("pedidos.cancelar")),
             form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    motivo = (form.get("motivo") or "").strip()[:150]
    if not motivo:
        return _tela_detalhe(request, usuario, pid, erro="Escreva o motivo do cancelamento.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            sem_multa = form.get("sem_multa") == "1"
            cur.execute("""UPDATE pedidos SET status = 'CANCELADO', cancel_sem_multa = %s WHERE id = %s
                           RETURNING cancel_multa_centavos, cancel_devolver_centavos, cancel_regra""", (sem_multa, pid))
            calc = cur.fetchone()
            auditar(cur, request, "pedido_cancelado", usuario["id"], "pedido", str(pid),
                    {"motivo": motivo, "sem_multa": sem_multa, "multa": calc["cancel_multa_centavos"] if calc else None,
                     "devolver": calc["cancel_devolver_centavos"] if calc else None})
            eventos.cancelado(cur, str(pid), motivo)
    except psycopg.Error as e:
        return _tela_detalhe(request, usuario, pid, erro=mensagem_do_banco(e), status=400)
    return RedirectResponse(f"/pedidos/{pid}", status_code=303)


# --------------------------------------------------------------------------- #
# Receber o saldo de um pedido
# --------------------------------------------------------------------------- #
def _form_receber(request, usuario, pid, v=None, erro=None, status=200, forma=""):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pe = _pedido(cur, pid)
        opc = pagamento.opcoes(cur, modo="pedido")
    if not pe:
        return RedirectResponse("/pedidos", status_code=303)
    if not (pode(usuario, "crediario.liberar") and pe["cliente_id"]):
        opc["formas"].pop("CREDIARIO", None)
    v = v or {"valor": f"{pe['saldo'] // 100},{pe['saldo'] % 100:02d}",
              **({"forma": forma} if forma in opc["formas"] else {})}
    v.setdefault("chave", nova_chave())
    m = {"tipo": "ENTRADA", "titulo": f"Receber do pedido {pe['sku']}", "cor": "entrada"}
    return tela(request, "caixa_form.html", usuario, status=status, modo="pedido", m=m, v=v, erro=erro,
                pe=pe, base=f"/pedidos/{pid}/receber", max_parcelas=pe["max_parcelas_permitidas"], **opc)


def _validar_receber(cur, pid, form, usuario=None):
    pe = _pedido(cur, pid)
    pag, erro = pagamento.ler(form, cur, pe["max_parcelas_permitidas"], pe["config_id"], modo="pedido")
    if not erro and pag.forma == "CREDIARIO" and not (usuario and pode(usuario, "crediario.liberar")):
        erro = "Crediário só a Dona, a Gerente ou o Financeiro liberam."
    if not erro and pag.forma == "CREDIARIO" and not pe["cliente_id"]:
        erro = "Crediário precisa de cliente com cadastro (nome e CPF)."
    if not erro and pag.valor > pe["saldo"]:
        erro = f"O valor passa do que falta pagar ({brl(pe['saldo'])})."
    if not erro and not chave_valida(form.get("chave")):
        erro = "A página ficou aberta muito tempo. Comece de novo."
    return pe, pag, erro


@router.get("/pedidos/{pid}/receber")
def receber_form(request: Request, pid: UUID, forma: str = "", usuario: dict = Depends(requer("caixa.entrada"))):
    return _form_receber(request, usuario, pid, forma=forma)


@router.post("/pedidos/{pid}/receber")
def receber_corrigir(request: Request, pid: UUID, usuario: dict = Depends(requer("caixa.entrada")),
                     form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    return _form_receber(request, usuario, pid, v=form)


@router.post("/pedidos/{pid}/receber/conferir")
def receber_conferir(request: Request, pid: UUID, usuario: dict = Depends(requer("caixa.entrada")),
                     form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        pe, pag, erro = _validar_receber(cur, pid, form, usuario)
    if erro:
        return _form_receber(request, usuario, pid, v=form, erro=erro, status=400)
    m = {"tipo": "ENTRADA", "titulo": f"Receber do pedido {pe['sku']}", "cor": "entrada"}
    return tela(request, "conferir.html", usuario, m=m, modo="pedido", pag=pag, pe=pe,
                descricao=f"Pagamento do pedido {pe['sku']} ({pe['cliente'] or 'venda'})",
                campos={**pag.campos(), "chave": form["chave"]},
                url_gravar=f"/pedidos/{pid}/receber/gravar", url_corrigir=f"/pedidos/{pid}/receber")


@router.post("/pedidos/{pid}/receber/gravar")
def receber_gravar(request: Request, pid: UUID, usuario: dict = Depends(requer("caixa.entrada")),
                   form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    cid = None
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("SELECT id::text FROM crediarios WHERE chave_idempotencia = %s", (form.get("chave") or "-",))
            ja = cur.fetchone()                        # clique duplo: o crediário já foi feito
            if ja:
                return RedirectResponse(f"/crediario/{ja['id']}", status_code=303)
            pe, pag, erro = _validar_receber(cur, pid, form, usuario)
            if not erro and pag.forma == "CREDIARIO":
                cid = crediario.criar(cur, cliente_id=pe["cliente_id"], pedido_id=str(pid), valor=pag.valor,
                                      parcelas=pag.parcelas, primeiro=pag.primeiro_vencimento,
                                      usuario_id=usuario["id"], chave=form["chave"])
                auditar(cur, request, "crediario_liberado", usuario["id"], "pedido", str(pid),
                        {"crediario": cid, "valor": pag.valor, "parcelas": pag.parcelas})
            elif not erro:
                pagamento.gravar(cur, pag, tipo="ENTRADA", natureza="SALDO",
                                 descricao=f"Saldo {pe['sku']} ({pe['cliente'] or 'venda'})",
                                 chave=form["chave"], usuario_id=usuario["id"], pedido_id=str(pid))
    except psycopg.Error as e:
        if not e_chave_repetida(e):
            return _form_receber(request, usuario, pid, v=form, erro=mensagem_do_banco(e), status=400)
        erro = None
    if erro:
        return _form_receber(request, usuario, pid, v=form, erro=erro, status=400)
    if form.get("forma") == "CREDIARIO" and cid:
        return RedirectResponse(f"/crediario/{cid}?novo=1", status_code=303)
    return RedirectResponse(f"/pedidos/{pid}", status_code=303)


@router.post("/pedidos/{pid}/devolver")
def devolver(request: Request, pid: UUID, usuario: dict = Depends(requer("pedidos.cancelar")),
             form: dict = Depends(formulario)):
    """Registra o dinheiro devolvido à cliente de um pedido cancelado (saída no caixa, pela regra)."""
    checar_csrf(request, form.get("csrf"))
    valor = ler_reais(form.get("valor"))
    forma = form.get("forma")
    chave = chave_valida(form.get("chave"))
    if not valor or forma not in ("PIX", "DINHEIRO") or not chave:
        return _tela_detalhe(request, usuario, pid, erro="Digite o valor e escolha Pix ou dinheiro.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            pagamento.gravar(cur, pagamento.Pagamento(valor=valor, forma=forma), tipo="SAIDA", natureza="DEVOLUCAO_CLIENTE",
                             descricao="Devolução à cliente (pedido cancelado)", chave=chave,
                             usuario_id=usuario["id"], pedido_id=str(pid))
            auditar(cur, request, "devolucao_cliente", usuario["id"], "pedido", str(pid), {"valor": valor, "forma": forma})
    except psycopg.Error as e:
        if e_chave_repetida(e):
            return RedirectResponse(f"/pedidos/{pid}", status_code=303)
        return _tela_detalhe(request, usuario, pid, erro=mensagem_do_banco(e), status=400)
    return RedirectResponse(f"/pedidos/{pid}?recado_link={quote('Devolução registrada no caixa.')}", status_code=303)


@router.post("/pedidos/{pid}/envio")
def registrar_envio(request: Request, pid: UUID, usuario: dict = Depends(requer("pedidos.ver")),
                    form: dict = Depends(formulario)):
    """Compra do site enviada: guarda a etiqueta do Melhor Envio e/ou o código de rastreio e avisa a cliente."""
    checar_csrf(request, form.get("csrf"))
    from .. import frete
    etiqueta = re.sub(r"[^A-Za-z0-9-]", "", form.get("etiqueta") or "")[:80] or None
    rastreio = re.sub(r"[^A-Za-z0-9-]", "", form.get("rastreio") or "").upper()[:60] or None
    if etiqueta and not rastreio:
        try:
            with db.transacao(usuario_id=usuario["id"]) as cur:
                me = frete.acesso(cur)
            rastreio = frete.rastrear(etiqueta, me)
        except frete.ErroFrete:
            rastreio = None
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE compras_site cs SET etiqueta_id = COALESCE(%s, etiqueta_id), rastreio = COALESCE(%s, rastreio)
                        FROM compras_itens ci WHERE ci.compra_id = cs.id AND ci.pedido_id = %s AND cs.status = 'PAGA'
                        RETURNING cs.cliente_id::text AS cliente_id, cs.rastreio""", (etiqueta, rastreio, str(pid)))
        linha = cur.fetchone()
        if linha and rastreio:
            from .. import avisos
            avisos.para_cliente(cur, linha["cliente_id"],
                                f"📦 Seu pedido foi enviado! Código de rastreio: {rastreio}. "
                                "Acompanhe em https://www.melhorrastreio.com.br/rastreio/" + rastreio,
                                "rastreio", f"rastreio:{pid}:{rastreio}")
        auditar(cur, request, "envio_registrado", usuario["id"], "pedido", str(pid), {"rastreio": rastreio})
    return RedirectResponse(f"/pedidos/{pid}#entrega", status_code=303)
