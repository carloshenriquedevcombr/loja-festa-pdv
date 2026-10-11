"""Crediário da loja (carnê próprio): a cliente paga o pedido em parcelas mensais direto à loja.

Regras que o banco garante (migração 0012): só Dona/Gerente/Financeiro liberam; nunca maior que o saldo;
cliente com CPF; parcela recebida pelo valor exato e só uma vez; cancelar só com motivo; nada é apagado.
"""
import calendar
from datetime import date

from . import pix


def somar_meses(d: date, meses: int) -> date:
    ano, mes = divmod(d.month - 1 + meses, 12)
    ano, mes = d.year + ano, mes + 1
    return date(ano, mes, min(d.day, calendar.monthrange(ano, mes)[1]))


def dividir(valor: int, parcelas: int) -> list[int]:
    """R$ 100,00 em 3x → 33,34 + 33,33 + 33,33 (os centavos que sobram vão nas primeiras)."""
    base, resto = divmod(valor, parcelas)
    return [base + (1 if i < resto else 0) for i in range(parcelas)]


def criar(cur, *, cliente_id: str, pedido_id: str | None, valor: int, parcelas: int, primeiro: date,
          usuario_id: str, chave: str | None = None) -> str:
    cur.execute("""INSERT INTO crediarios (loja_id, cliente_id, pedido_id, valor_centavos, parcelas, criado_por,
                                           chave_idempotencia)
                   VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                (cliente_id, pedido_id, valor, parcelas, usuario_id, chave))
    cid = cur.fetchone()["id"]
    for i, v in enumerate(dividir(valor, parcelas)):
        cur.execute("""INSERT INTO crediario_parcelas (loja_id, crediario_id, numero, valor_centavos, vencimento)
                       VALUES (ctx_loja(), %s, %s, %s, %s)""", (cid, i + 1, v, somar_meses(primeiro, i)))
    return cid


def carregar(cur, cid: str) -> dict | None:
    cur.execute("""SELECT c.*, c.id::text AS id, c.pedido_id::text AS pedido_id, cl.nome AS cliente,
                          cl.id::text AS cliente_id, pr.sku, pr.nome AS peca, u.nome AS criado_por_nome,
                          uc.nome AS cancelado_por_nome
                     FROM crediarios c JOIN clientes cl ON cl.id = c.cliente_id
                     JOIN usuarios u ON u.id = c.criado_por
                     LEFT JOIN usuarios uc ON uc.id = c.cancelado_por
                     LEFT JOIN pedidos pe ON pe.id = c.pedido_id
                     LEFT JOIN produtos pr ON pr.id = pe.produto_id
                    WHERE c.id = %s""", (cid,))
    c = cur.fetchone()
    if not c:
        return None
    cur.execute("""SELECT p.id, p.numero, p.valor_centavos, p.vencimento, p.paga_em, p.transacao_id,
                          t.forma_pagamento,
                          p.paga_em IS NULL AND p.vencimento < (now() AT TIME ZONE 'America/Sao_Paulo')::date AS vencida
                     FROM crediario_parcelas p LEFT JOIN transacoes t ON t.id = p.transacao_id
                    WHERE p.crediario_id = %s ORDER BY p.numero""", (cid,))
    c["lista"] = cur.fetchall()
    c["pago"] = sum(p["valor_centavos"] for p in c["lista"] if p["paga_em"])
    c["falta"] = c["valor_centavos"] - c["pago"] if c["status"] == "ATIVO" else 0
    abertas = [p for p in c["lista"] if not p["paga_em"]]
    c["proxima"] = abertas[0] if abertas and c["status"] == "ATIVO" else None
    return c


def pix_da_loja(cur) -> dict | None:
    cur.execute("SELECT pix_chave, pix_nome, pix_cidade, nome FROM lojas WHERE id = ctx_loja()")
    l = cur.fetchone()
    if not l or not l["pix_chave"]:
        return None
    return {"chave": l["pix_chave"], "nome": l["pix_nome"] or l["nome"], "cidade": l["pix_cidade"] or "BRASIL"}


def codigo_pix(dados_pix: dict | None, parcela: dict, cid: str) -> str | None:
    if not dados_pix:
        return None
    txid = f"CR{cid.replace('-', '')[:12]}P{parcela['numero']:02d}"
    return pix.br_code(dados_pix["chave"], dados_pix["nome"], dados_pix["cidade"], parcela["valor_centavos"], txid)


# --------------------------------------------------------------------------- #
# Código de cada parcela (13 números): 10 do crediário + 2 da parcela + 1 de conferência.
# Sai no carnê em código de barras (com os números embaixo) ou QR; na busca do topo abre a parcela para receber.
# --------------------------------------------------------------------------- #
def _dv(numeros: str) -> str:
    soma = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(numeros)))
    return str((10 - soma % 10) % 10)


def _base(cid: str) -> int:
    return int(cid.replace("-", "")[:10], 16) % 10**10


def codigo_parcela(cid: str, numero: int) -> str:
    n = f"{_base(cid):010d}{numero:02d}"
    return n + _dv(n)


def codigo_legivel(codigo: str) -> str:
    return f"{codigo[:5]} {codigo[5:10]} {codigo[10:12]} {codigo[12:]}"


def parece_codigo_parcela(texto: str) -> bool:
    n = "".join(ch for ch in (texto or "") if ch.isdigit())
    return len(n) == 13 and len((texto or "").replace(" ", "").replace(".", "").replace("-", "")) == 13 and _dv(n[:12]) == n[12]


def achar_parcela(cur, texto: str) -> tuple[str, int] | None:
    """Código lido no leitor/digitado → (crediário, número da parcela), ou None."""
    if not parece_codigo_parcela(texto):
        return None
    n = "".join(ch for ch in texto if ch.isdigit())
    base, numero = int(n[:10]), int(n[10:12])
    cur.execute("""SELECT c.id::text AS id FROM crediarios c
                    WHERE (('x' || substr(replace(c.id::text, '-', ''), 1, 10))::bit(40)::bigint %% 10000000000) = %s
                      AND EXISTS (SELECT 1 FROM crediario_parcelas p WHERE p.crediario_id = c.id AND p.numero = %s)
                    LIMIT 2""", (base, numero))
    achados = cur.fetchall()
    return (achados[0]["id"], numero) if len(achados) == 1 else None
