"""Pagamento: ler o formulário (valor, forma, maquininha, parcelas), calcular juros e gravar
no livro caixa. Usado pelo caixa e pelos pedidos, para as regras serem uma só."""
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request

from .web import EM_FORMA, FORMAS, brl, ler_reais


FUSO = ZoneInfo("America/Sao_Paulo")
MAX_ANEXO = 15 * 1024 * 1024


async def formulario(request: Request) -> dict:
    """Dependência: o formulário enviado, como dicionário de textos."""
    return {k: str(v) for k, v in (await request.form()).items()}


async def formulario_com_nota(request: Request) -> dict:
    """Como formulario(), mas aceita UMA foto da nota (campo "nota"): vai em form["_nota"] como bytes."""
    dados: dict = {}
    async with request.form(max_files=1, max_fields=40) as f:
        for k, v in f.multi_items():
            if hasattr(v, "read"):
                if k == "nota" and v.filename:
                    dados["_nota"] = await v.read(MAX_ANEXO + 1)
            elif k not in dados:
                dados[k] = str(v)
    return dados


@dataclass
class Pagamento:
    valor: int
    forma: str
    parcelas: int = 1
    maquininha_id: int | None = None
    maquininha: str | None = None
    nsu: str | None = None
    acrescimo: int = 0                     # juros cobrados da cliente (parcelado na loja)
    saida: bool = False                    # Paguei algo: cartão é o da loja (sem maquininha)
    primeiro_vencimento: object = None     # crediário da loja

    @property
    def na_maquininha(self) -> int:
        return self.valor + self.acrescimo

    def frase(self) -> str:
        texto = f"{brl(self.valor)} {EM_FORMA[self.forma]}"
        if self.forma == "CREDIARIO":
            return texto + f" em {self.parcelas}x de {brl(math.ceil(self.valor / self.parcelas))}" \
                + (f", 1ª em {self.primeiro_vencimento:%d/%m/%Y}" if self.primeiro_vencimento else "")
        if self.forma in ("CREDITO", "CARNE", "BOLETO") and self.parcelas > 1:
            texto += f" em {self.parcelas}x"
        if self.maquininha:
            texto += f" ({self.maquininha})"
        return texto

    def instrucao_maquininha(self) -> str | None:
        if self.forma not in ("DEBITO", "CREDITO") or self.saida:
            return None
        modo = "Débito" if self.forma == "DEBITO" else (
            "Crédito à vista" if self.parcelas == 1 else f"Crédito parcelado LOJA em {self.parcelas}x")
        texto = f"Passe {brl(self.na_maquininha)} na {self.maquininha}: {modo}"
        if self.parcelas > 1:
            texto += f" ({self.parcelas}x de {brl(math.ceil(self.na_maquininha / self.parcelas))})"
        return texto

    def campos(self) -> dict:
        """Para os campos escondidos da tela de conferência."""
        return {"valor": f"{self.valor / 100:.2f}".replace(".", ","), "forma": self.forma,
                "parcelas": str(self.parcelas), "maquininha": str(self.maquininha_id or ""),
                "nsu": self.nsu or "",
                "vezes": str(self.parcelas),
                "primeiro_vencimento": self.primeiro_vencimento.isoformat() if self.primeiro_vencimento else ""}


def opcoes(cur, so_dinheiro_pix: bool = False, modo: str = "receber") -> dict:
    """O que a tela de pagamento precisa mostrar. modo: receber (avulso), pedido (+ crediário) ou pagar."""
    cur.execute("""SELECT id, apelido, operadora, cor_hex FROM maquininhas
                    WHERE ativa AND apelido <> 'Link Mercado Pago'
                    ORDER BY operadora = 'NAO_IDENTIFICADA', apelido""")
    maquininhas = cur.fetchall()
    formas = {k: v for k, v in FORMAS.items() if not so_dinheiro_pix or k in ("DINHEIRO", "PIX")}
    if modo == "pagar":
        formas = {**{k: v for k, v in FORMAS.items()}, "DEBITO": "Cartão de débito da loja",
                  "CREDITO": "Cartão de crédito da loja"}
    elif modo == "pedido":
        formas = {**formas, "CREDIARIO": "Crediário da loja (carnê)"}
    return {"maquininhas": maquininhas, "formas": formas, "modo_pagamento": modo}


def config_vigente(cur) -> dict:
    cur.execute("SELECT * FROM config_pagamento WHERE id = config_vigente(ctx_loja())")
    return cur.fetchone()


def pela_regra(tipo: str, valor, total: int) -> int:
    """Igual à função valor_pela_regra do banco: PCT = % do total; FIXO = centavos, nunca mais que o total."""
    if tipo == "FIXO":
        return min(int(round(float(valor))), total)
    return math.ceil(total * float(valor) / 100)


def entrada(cfg: dict, total: int, tipo: str) -> int:
    """Entrada mínima da reserva. tipo: 'ALUGUEL' ou 'VENDA'."""
    t = "aluguel" if tipo == "ALUGUEL" else "venda"
    return pela_regra(cfg[f"entrada_{t}_tipo"], cfg[f"entrada_{t}_valor"], total)


def texto_regra(tipo: str, valor) -> str:
    if tipo == "FIXO":
        from .web import brl
        return brl(int(round(float(valor))))
    v = float(valor)
    return (f"{v:.0f}" if v == int(v) else f"{v:.2f}".replace(".", ",")) + "%"


def juros_vigentes(cur, config_id: int) -> dict:
    cur.execute("SELECT parcelas, acrescimo_pct FROM config_juros WHERE config_id = %s", (config_id,))
    return {j["parcelas"]: float(j["acrescimo_pct"]) for j in cur.fetchall()}


def simular(cfg: dict, juros: dict, valor: int, tipo: str = "ALUGUEL") -> dict:
    """O mesmo modelo de parcelas do balcão, para mostrar no site e na configuração.
    Respeita o máximo de vezes (aluguel/venda), a parcela mínima e o acréscimo de cada vez."""
    maximo = cfg["max_parcelas_aluguel"] if tipo == "ALUGUEL" else cfg["max_parcelas_venda"]
    opcoes = []
    for n in range(1, maximo + 1):
        acrescimo = math.ceil(valor * float(juros.get(n, 0)) / 100) if n > 1 else 0
        total = valor + acrescimo
        parcela = math.ceil(total / n)
        if n > 1 and parcela < cfg["valor_minimo_parcela_centavos"]:
            break
        opcoes.append({"vezes": n, "parcela": parcela, "total": total, "sem_juros": acrescimo == 0})
    sem_juros = 1                                   # até quantas vezes SEGUIDAS não tem acréscimo
    for o in opcoes:
        if not o["sem_juros"]:
            break
        sem_juros = o["vezes"]
    t = "aluguel" if tipo == "ALUGUEL" else "venda"
    return {"entrada": entrada(cfg, valor, tipo), "opcoes": opcoes,
            "maximo": opcoes[-1] if opcoes else None, "sem_juros_ate": sem_juros,
            "entrada_texto": texto_regra(cfg[f"entrada_{t}_tipo"], cfg[f"entrada_{t}_valor"])}


def ler(form: dict, cur, max_parcelas: int, config_id: int,
        so_dinheiro_pix: bool = False, modo: str = "receber") -> tuple[Pagamento | None, str | None]:
    valor = ler_reais(form.get("valor"))
    if valor is None:
        return None, "Digite o valor (por exemplo: 150,00)."
    forma = form.get("forma", "")
    permitidas = set(FORMAS) | ({"CREDIARIO"} if modo == "pedido" else set())
    if forma not in permitidas or (so_dinheiro_pix and forma not in ("DINHEIRO", "PIX")):
        return None, "Escolha como foi pago."
    pag = Pagamento(valor=valor, forma=forma, saida=(modo == "pagar"))

    if forma in ("CREDIARIO", "CARNE"):
        try:
            pag.parcelas = int(form.get("vezes") or form.get("parcelas") or 1)
        except ValueError:
            pag.parcelas = 0
        if not 1 <= pag.parcelas <= 12:
            return None, "Escolha em quantas parcelas (até 12)."
    if forma == "CREDIARIO":
        try:
            pag.primeiro_vencimento = date.fromisoformat(form.get("primeiro_vencimento") or "")
        except ValueError:
            return None, "Crediário: escolha a data da 1ª parcela."
        hoje = datetime.now(FUSO).date()
        if not hoje <= pag.primeiro_vencimento <= hoje + timedelta(days=90):
            return None, "A 1ª parcela do crediário vence de hoje até 90 dias."
        return pag, None
    if forma in ("CARNE", "BOLETO"):
        return pag, None

    if forma in ("DEBITO", "CREDITO") and modo == "pagar":      # cartão da loja
        try:
            pag.parcelas = int(form.get("parcelas") or 1) if forma == "CREDITO" else 1
        except ValueError:
            pag.parcelas = 1
        pag.parcelas = max(1, min(pag.parcelas, 12))
        return pag, None

    if forma in ("DEBITO", "CREDITO"):
        try:
            maq_id = int(form.get("maquininha") or 0)
        except ValueError:
            maq_id = 0
        cur.execute("SELECT id, apelido FROM maquininhas WHERE id = %s AND ativa", (maq_id,))
        maq = cur.fetchone()
        if not maq:
            return None, "Escolha em qual maquininha o cartão foi passado."
        pag.maquininha_id, pag.maquininha = maq["id"], maq["apelido"]
        nsu = (form.get("nsu") or "").strip()
        if nsu and not nsu.isdigit() or len(nsu) > 20:
            return None, "O número do comprovante (NSU) tem só números."
        pag.nsu = nsu or None

    if forma == "CREDITO":
        try:
            pag.parcelas = int(form.get("parcelas") or 1)
        except ValueError:
            pag.parcelas = 1
        if not 1 <= pag.parcelas <= max_parcelas:
            return None, f"Parcelamento em até {max_parcelas}x."
        if pag.parcelas > 1:
            cur.execute("SELECT acrescimo_pct FROM config_juros WHERE config_id = %s AND parcelas = %s",
                        (config_id, pag.parcelas))
            linha = cur.fetchone()
            if linha and linha["acrescimo_pct"]:
                pag.acrescimo = math.ceil(valor * float(linha["acrescimo_pct"]) / 100)
    return pag, None


def gravar(cur, pag: Pagamento, *, tipo: str, natureza: str, descricao: str, chave: str,
           usuario_id: str, pedido_id: str | None = None, aprovado_por: str | None = None,
           fornecedor_id: str | None = None, categoria: str | None = None, documento: str | None = None,
           anexo: str | None = None, crediario_parcela_id: int | None = None,
           servico_id: str | None = None, cliente_id: str | None = None) -> int:
    cur.execute(
        """INSERT INTO transacoes
             (loja_id, tipo, natureza, valor_centavos, acrescimo_juros_centavos, forma_pagamento,
              parcelas, modalidade_cartao, maquininha_id, nsu, pedido_id, descricao,
              chave_idempotencia, usuario_id, aprovado_por, fornecedor_id, categoria, documento, anexo,
              crediario_parcela_id, servico_id, cliente_id)
           VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (tipo, natureza, pag.valor, pag.acrescimo, pag.forma, pag.parcelas,
         "LOJISTA" if pag.forma == "CREDITO" and tipo == "ENTRADA" else None, pag.maquininha_id, pag.nsu,
         pedido_id, descricao[:200], chave, usuario_id, aprovado_por, fornecedor_id, categoria, documento, anexo,
         crediario_parcela_id, servico_id, cliente_id))
    return cur.fetchone()["id"]
