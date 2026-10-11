"""Configurações: maquininhas e regras comerciais (só Dona e Gerente alteram)."""
from decimal import Decimal, InvalidOperation

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import db, pagamento
from ..auth import auditar, pode, requer
from ..pagamento import formulario
from ..web import checar_csrf, ler_reais, mensagem_do_banco, tela

router = APIRouter()

OPERADORAS = ["Rede", "Stone", "Cielo", "Getnet", "PagBank", "Mercado Pago", "InfinitePay", "Ton", "SumUp", "Outra"]
CORES = {"#E4570F": "Laranja", "#0B5D1E": "Verde", "#1D4ED8": "Azul", "#6D28D9": "Roxo",
         "#111111": "Preta", "#B91C1C": "Vermelha", "#B45309": "Amarela"}


def _tela(request, usuario, erro=None, ok=None, status=200):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""SELECT id, apelido, operadora, cor_hex, ativa FROM maquininhas
                        ORDER BY ativa DESC, operadora = 'NAO_IDENTIFICADA', apelido""")
        maquininhas = cur.fetchall()
        cfg = pagamento.config_vigente(cur)
        cur.execute("SELECT parcelas, acrescimo_pct FROM config_juros WHERE config_id = %s ORDER BY parcelas",
                    (cfg["id"],))
        juros = {j["parcelas"]: j["acrescimo_pct"] for j in cur.fetchall()}
        cur.execute("""SELECT to_char(c.vigente_desde AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando,
                              u.nome FROM config_pagamento c JOIN usuarios u ON u.id = c.definido_por
                        WHERE c.id = %s""", (cfg["id"],))
        autor = cur.fetchone()
        cur.execute("SELECT nome, cnpj, endereco, telefone, rodape_comprovante FROM lojas WHERE id = ctx_loja()")
        loja = cur.fetchone()
    exemplo = {"aluguel": pagamento.simular(cfg, {k: float(v) for k, v in juros.items()}, 100000, "ALUGUEL"),
               "venda": pagamento.simular(cfg, {k: float(v) for k, v in juros.items()}, 100000, "VENDA")}
    return tela(request, "config.html", usuario, status=status, maquininhas=maquininhas, cfg=cfg, juros=juros,
                loja=loja, exemplo=exemplo,
                autor=autor, OPERADORAS=OPERADORAS, CORES=CORES, erro=erro, ok=ok,
                edita_maq=pode(usuario, "config.maquininhas"), edita_regras=pode(usuario, "config.pagamento"))


@router.get("/config")
def ver(request: Request, usuario: dict = Depends(requer("config.ver"))):
    return _tela(request, usuario)


@router.post("/config/maquininhas")
def nova_maquininha(request: Request, usuario: dict = Depends(requer("config.maquininhas")),
                    form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    apelido = (form.get("apelido") or "").strip()[:40]
    operadora = form.get("operadora") if form.get("operadora") in OPERADORAS else None
    cor = form.get("cor") if form.get("cor") in CORES else None
    if len(apelido) < 2 or not operadora or not cor:
        return _tela(request, usuario, erro="Preencha o apelido, a operadora e a cor.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute("""INSERT INTO maquininhas (loja_id, apelido, operadora, cor_hex)
                           VALUES (ctx_loja(), %s, %s, %s) RETURNING id""", (apelido, operadora, cor))
            auditar(cur, request, "maquininha_cadastrada", usuario["id"], "maquininha",
                    str(cur.fetchone()["id"]), {"apelido": apelido, "operadora": operadora})
    except psycopg.errors.UniqueViolation:
        return _tela(request, usuario, erro="Já existe uma maquininha com esse apelido.", status=400)
    return _tela(request, usuario, ok=f"Maquininha {apelido} cadastrada.")


@router.post("/config/maquininhas/{mid}/ativa")
def ativar_maquininha(request: Request, mid: int, usuario: dict = Depends(requer("config.maquininhas")),
                      form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    ativa = form.get("ativa") == "1"
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("UPDATE maquininhas SET ativa = %s WHERE id = %s", (ativa, mid))
        auditar(cur, request, "maquininha_" + ("ativada" if ativa else "desativada"), usuario["id"],
                "maquininha", str(mid))
    return RedirectResponse("/config", status_code=303)


REGRAS = ("entrada_aluguel", "entrada_venda", "multa_aluguel", "multa_venda")


def _ler_regras(form: dict, atual: dict, entrada_legado) -> tuple[dict, str | None]:
    """Entrada e multa de cancelamento: cada uma em % do total ou valor fixo (R$)."""
    r = {}
    for k in REGRAS:
        tipo = form.get(f"{k}_tipo")
        bruto = (form.get(f"{k}_valor") or "").strip()
        if tipo not in ("PCT", "FIXO") or not bruto:          # formulário antigo: mantém/usa a % de entrada
            r[f"{k}_tipo"] = atual[f"{k}_tipo"] if k.startswith("multa") else "PCT"
            r[f"{k}_valor"] = atual[f"{k}_valor"] if k.startswith("multa") else entrada_legado
            continue
        if tipo == "PCT":
            try:
                valor = Decimal(bruto.replace("%", "").replace(",", "."))
            except InvalidOperation:
                return {}, "Porcentagem inválida (use só números, ex.: 30)."
            if not 0 <= valor <= 100:
                return {}, "Porcentagem precisa ser de 0 a 100."
        else:
            valor = 0 if bruto in ("0", "0,00", "R$ 0") else ler_reais(bruto)
            if valor is None:
                return {}, "Valor fixo inválido (ex.: 150,00)."
        r[f"{k}_tipo"], r[f"{k}_valor"] = tipo, valor
    r["arrependimento_7d"] = (form.get("arrependimento_7d") == "1") if form.get("regras_v2") else atual["arrependimento_7d"]
    return r, None


@router.post("/config/regras")
def salvar_regras(request: Request, usuario: dict = Depends(requer("config.pagamento")),
                  form: dict = Depends(formulario)):
    """Regras novas = versão nova. Pedidos já feitos continuam com a regra da época."""
    checar_csrf(request, form.get("csrf"))
    try:
        entrada = Decimal(form["entrada"].replace(",", ".")) if form.get("entrada") else None
        parc_aluguel, parc_venda = int(form.get("parc_aluguel", "")), int(form.get("parc_venda", ""))
        desconto = Decimal((form.get("desconto") or "").replace(",", "."))
        dias_limpeza = int(form.get("dias_limpeza", ""))
        juros = {n: Decimal((form.get(f"juros_{n}") or "0").replace(",", ".")) for n in range(2, 13)}
    except (InvalidOperation, ValueError):
        return _tela(request, usuario, erro="Algum número ficou em branco ou com letra.", status=400)
    parcela_min, teto = ler_reais(form.get("parcela_min")), ler_reais(form.get("teto_saida"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        atual = pagamento.config_vigente(cur)
    regras, erro = _ler_regras(form, atual, entrada if entrada is not None else atual["entrada_minima_pct"])
    if erro:
        return _tela(request, usuario, erro=erro, status=400)
    if entrada is None:            # tela nova: a "entrada mínima %" antiga acompanha a entrada do aluguel
        entrada = (regras["entrada_aluguel_valor"] if regras["entrada_aluguel_tipo"] == "PCT"
                   else atual["entrada_minima_pct"])
    if not (0 <= entrada <= 100 and 1 <= parc_aluguel <= 12 and 1 <= parc_venda <= 12 and 0 <= desconto <= 100
            and 0 <= dias_limpeza <= 30 and parcela_min and teto and all(0 <= j <= 100 for j in juros.values())):
        return _tela(request, usuario, erro="Algum valor está fora do permitido.", status=400)
    try:
        with db.transacao(usuario_id=usuario["id"]) as cur:
            cur.execute(
                """INSERT INTO config_pagamento
                     (loja_id, entrada_minima_pct, max_parcelas_aluguel, max_parcelas_venda,
                      valor_minimo_parcela_centavos, desconto_maximo_pct, teto_saida_atendente_centavos,
                      dias_limpeza, definido_por, entrada_aluguel_tipo, entrada_aluguel_valor, entrada_venda_tipo,
                      entrada_venda_valor, multa_aluguel_tipo, multa_aluguel_valor, multa_venda_tipo, multa_venda_valor,
                      arrependimento_7d)
                   VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (entrada, parc_aluguel, parc_venda, parcela_min, desconto, teto, dias_limpeza, usuario["id"],
                 *(regras[k] for k in ("entrada_aluguel_tipo", "entrada_aluguel_valor", "entrada_venda_tipo",
                                       "entrada_venda_valor", "multa_aluguel_tipo", "multa_aluguel_valor",
                                       "multa_venda_tipo", "multa_venda_valor", "arrependimento_7d"))))
            novo = cur.fetchone()["id"]
            for n, pct in juros.items():
                if pct > 0:
                    cur.execute("INSERT INTO config_juros (loja_id, config_id, parcelas, acrescimo_pct) "
                                "VALUES (ctx_loja(), %s, %s, %s)", (novo, n, pct))
            auditar(cur, request, "regras_alteradas", usuario["id"], "config_pagamento", str(novo))
    except psycopg.Error as e:
        return _tela(request, usuario, erro=mensagem_do_banco(e), status=400)
    return _tela(request, usuario, ok="Regras salvas. Valem para os pedidos novos a partir de agora.")


@router.post("/config/loja")
def salvar_loja(request: Request, usuario: dict = Depends(requer("config.pagamento")),
                form: dict = Depends(formulario)):
    """Nome, CNPJ, endereço e telefone que aparecem nos comprovantes e extratos."""
    checar_csrf(request, form.get("csrf"))
    nome = (form.get("nome") or "").strip()[:80]
    cnpj = "".join(c for c in form.get("cnpj") or "" if c.isdigit()) or None
    if len(nome) < 2:
        return _tela(request, usuario, erro="Escreva o nome da loja.", status=400)
    if cnpj and len(cnpj) != 14:
        return _tela(request, usuario, erro="CNPJ tem 14 números. Confira ou deixe em branco.", status=400)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute("""UPDATE lojas SET nome=%s, cnpj=%s, endereco=%s, telefone=%s, rodape_comprovante=%s
                        WHERE id = ctx_loja()""",
                    (nome, cnpj, (form.get("endereco") or "").strip()[:160] or None,
                     (form.get("telefone") or "").strip()[:20] or None,
                     (form.get("rodape") or "").strip()[:300] or None))
        auditar(cur, request, "dados_loja_alterados", usuario["id"], "loja")
    return _tela(request, usuario, ok="Dados da loja salvos. Já aparecem nos comprovantes.")
