"""Contrato de locação/venda: texto da loja com os dados do pedido, assinatura da cliente na tela,
código de verificação e hash (qualquer alteração no texto ou na assinatura muda o hash)."""
import hashlib
import io
import re
import secrets
from datetime import datetime, timezone

from PIL import Image

from . import cripto
from .web import brl, cpf as formatar_cpf, data_br, telefone as formatar_fone

LETRAS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PADRAO = """CONTRATO DE {operacao} DE TRAJE · PEDIDO {numero}

LOJA: {loja}{cnpj}.
CLIENTE: {cliente}, CPF {cpf}, telefone {telefone}, endereço {endereco}.

1. OBJETO. A loja {verbo} à cliente a peça {sku} – {peca}{tamanho}, em perfeito estado, conferida pelas duas partes na {momento}.

2. DATAS. Retirada em {retirada}.{devolucao_frase}

3. VALORES. Valor total {total}. Pago até agora {pago}. O restante ({saldo}) deve ser pago até a retirada. A peça só sai da loja quitada.

4. AJUSTES E PROVAS. Os ajustes são feitos pela costureira da loja nas datas combinadas. Ajustes que alterem o modelo original dependem de autorização da loja.

5. CUIDADOS. A cliente se compromete a usar a peça com cuidado, não lavar, não passar e não fazer consertos por conta própria. A limpeza é feita pela loja.

6. ATRASO, DANO OU PERDA. Atraso na devolução, manchas que não saem na limpeza, rasgos, queimaduras ou perda da peça serão cobrados conforme a tabela da loja, informada no balcão, até o valor de reposição da peça.

7. DESISTÊNCIA. Se a cliente desistir, a loja fica com {multa_cancelamento}, limitado ao valor já pago, para cobrir a reserva da peça e os ajustes; o restante é devolvido.{arrependimento_frase}

8. DADOS PESSOAIS. Os dados da cliente são usados só para este atendimento (LGPD) e ficam guardados de forma protegida.

Ao assinar, a cliente declara que leu e concorda com todas as cláusulas acima."""

CAMPOS = ("loja", "cnpj", "cliente", "cpf", "telefone", "endereco", "operacao", "verbo", "momento", "numero", "sku",
          "peca", "tamanho", "retirada", "devolucao_frase", "devolucao", "total", "pago", "saldo", "entrada", "hoje",
          "multa_cancelamento", "arrependimento_frase")


class ContratoInvalido(ValueError):
    pass


class _Seguro(dict):
    def __missing__(self, chave):                 # campo desconhecido no texto da Dona: fica como está
        return "{" + chave + "}"


def dados_do_pedido(cur, pedido_id: str) -> dict | None:
    cur.execute("""SELECT pe.*, pe.id::text AS id, pr.sku, pr.nome AS peca, pr.tamanho, cl.nome AS cliente,
                          cl.cpf_c, cl.telefone_c, cl.endereco_c, valor_pago(pe.id) AS pago,
                          l.nome AS loja, l.cnpj,
                          COALESCE((SELECT m.texto FROM contrato_modelos m WHERE m.loja_id = l.id
                                     ORDER BY m.definido_em DESC LIMIT 1), l.contrato_texto) AS contrato_texto
                          , cfg.multa_aluguel_tipo, cfg.multa_aluguel_valor, cfg.multa_venda_tipo, cfg.multa_venda_valor,
                          cfg.arrependimento_7d
                     FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id JOIN lojas l ON l.id = pe.loja_id
                     JOIN config_pagamento cfg ON cfg.id = pe.config_id
                     LEFT JOIN clientes cl ON cl.id = pe.cliente_id WHERE pe.id = %s""", (pedido_id,))
    return cripto.abrir(cur.fetchone(), "clientes", "cpf", "telefone", "endereco")


def _multa_texto(p: dict) -> str:
    from .pagamento import texto_regra
    t = "aluguel" if p["tipo"] == "ALUGUEL" else "venda"
    tipo, valor = p.get(f"multa_{t}_tipo", "PCT"), p.get(f"multa_{t}_valor", 25)
    return texto_regra(tipo, valor) + (" do valor total" if tipo == "PCT" else "")


def texto(p: dict) -> str:
    aluguel = p["tipo"] == "ALUGUEL"
    cnpj = p["cnpj"] or ""
    valores = _Seguro(
        loja=p["loja"], cnpj=f", CNPJ {cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}" if len(cnpj) == 14 else "",
        cliente=p["cliente"] or "_______________________",
        cpf=formatar_cpf(p["cpf"]) if p.get("cpf") else "___.___.___-__",
        telefone=formatar_fone(p["telefone"]) if p.get("telefone") else "(__) _____-____", endereco=p.get("endereco") or "_______________________",
        operacao="LOCAÇÃO" if aluguel else "VENDA", verbo="aluga" if aluguel else "vende",
        momento="retirada" if aluguel else "entrega", numero=p["id"].replace("-", "")[:8].upper(),
        sku=p["sku"], peca=p["peca"], tamanho=f" (tamanho {p['tamanho']})" if p.get("tamanho") else "",
        retirada=data_br(p["data_retirada"]) if p.get("data_retirada") else "no ato",
        devolucao=data_br(p["data_devolucao"]) if p.get("data_devolucao") else "",
        devolucao_frase=f" Devolução até {data_br(p['data_devolucao'])}, no horário da loja." if p.get("data_devolucao") else "",
        total=brl(p["valor_total_centavos"]), pago=brl(p["pago"]),
        saldo=brl(max(p["valor_total_centavos"] - p["pago"], 0)), entrada=brl(p["entrada_minima_centavos"]),
        hoje=datetime.now().strftime("%d/%m/%Y"),
        multa_cancelamento=_multa_texto(p),
        arrependimento_frase=(" Pedido feito fora da loja (link, WhatsApp ou site) pode ser cancelado em até 7 dias "
                              "com devolução total (Código de Defesa do Consumidor, art. 49).")
        if p.get("arrependimento_7d", True) else "")
    return (p.get("contrato_texto") or PADRAO).format_map(valores)


DESCRICAO_CAMPOS = {
    "loja": "nome da loja", "cnpj": "CNPJ da loja (com vírgula na frente)", "cliente": "nome da cliente",
    "cpf": "CPF", "telefone": "telefone", "endereco": "endereço", "operacao": "LOCAÇÃO ou VENDA",
    "verbo": "aluga ou vende", "momento": "retirada ou entrega", "numero": "número do pedido", "sku": "código da peça",
    "peca": "nome da peça", "tamanho": "(tamanho 40)", "retirada": "data da retirada", "devolucao": "data da devolução",
    "devolucao_frase": "frase da devolução (só no aluguel)", "total": "valor total", "pago": "valor já pago",
    "saldo": "quanto falta", "entrada": "entrada mínima", "hoje": "data de hoje",
    "multa_cancelamento": "o que a loja fica se a cliente desistir (regra das Configurações)",
    "arrependimento_frase": "frase dos 7 dias para compra fora da loja (se ligado)",
}


def conferir_modelo(texto: str) -> str | None:
    """Erro de digitação no modelo (chave { } sobrando, campo que não existe). None se está bom."""
    import string
    try:
        campos = {c for _, c, _, _ in string.Formatter().parse(texto) if c is not None}
    except ValueError:
        return "Tem uma chave { ou } sobrando. Os campos são escritos assim: {cliente}"
    desconhecidos = sorted(c for c in campos if c not in CAMPOS)
    if desconhecidos:
        return "Campo que não existe: " + ", ".join("{" + c + "}" for c in desconhecidos)
    if "cliente" not in campos:
        return "O contrato precisa ter o campo {cliente}."
    return None


def exemplo() -> dict:
    """Pedido de mentira para a prévia do modelo."""
    from datetime import date, timedelta
    return {"tipo": "ALUGUEL", "loja": "Sua Loja", "cnpj": "12345678000195", "cliente": "Maria da Silva",
            "cpf": "52998224725", "telefone": "19999991111", "endereco": "Rua das Flores, 10 - Centro",
            "id": "4bd6916b-0000-0000-0000-000000000000", "sku": "VES-0001", "peca": "Vestido longo azul",
            "tamanho": "40", "data_retirada": date.today() + timedelta(days=30),
            "data_devolucao": date.today() + timedelta(days=32), "valor_total_centavos": 120000, "pago": 30000,
            "entrada_minima_centavos": 30000}


def ler_assinatura(data_url: str) -> bytes:
    """Confere a imagem da assinatura (PNG do quadro da tela) e devolve PNG limpo."""
    m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", (data_url or "").strip())
    if not m or len(m.group(1)) > 600_000:
        raise ContratoInvalido("Faça a assinatura no quadro antes de confirmar.")
    import base64
    try:
        img = Image.open(io.BytesIO(base64.b64decode(m.group(1), validate=True)))
        img.load()
    except Exception as e:
        raise ContratoInvalido("Não consegui ler a assinatura. Apague e assine de novo.") from e
    if img.format != "PNG" or not (100 <= img.width <= 2400 and 50 <= img.height <= 1200):
        raise ContratoInvalido("Assinatura inválida. Apague e assine de novo.")
    cinza = img.convert("LA")
    traco = sum(1 for (lum, alfa) in cinza.getdata() if alfa > 40 and lum < 128)
    if traco < 150:
        raise ContratoInvalido("A assinatura ficou muito pequena. Assine com o dedo ou a caneta, de um lado ao outro.")
    saida = io.BytesIO()
    fundo = Image.new("RGB", img.size, (255, 255, 255))
    fundo.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
    fundo.convert("L").save(saida, "PNG", optimize=True)
    return saida.getvalue()


def novo_codigo() -> str:
    partes = ["".join(secrets.choice(LETRAS) for _ in range(4)) for _ in range(3)]
    return "-".join(partes)


def calcular_hash(pedido_id: str, texto_final: str, png: bytes, quando: datetime) -> str:
    h = hashlib.sha256()
    for parte in (pedido_id.encode(), texto_final.encode(), png,
                  quando.astimezone(timezone.utc).isoformat(timespec="microseconds").encode()):
        h.update(len(parte).to_bytes(8, "big"))
        h.update(parte)
    return h.hexdigest()


def assinado(cur, pedido_id: str) -> dict | None:
    cur.execute("""SELECT c.*, to_char(c.assinado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS quando,
                          u.nome AS atendente
                     FROM contratos c LEFT JOIN usuarios u ON u.id = c.atendente_id WHERE c.pedido_id = %s""", (pedido_id,))
    c = cur.fetchone()
    if not c:
        return None
    c["texto"] = cripto.decifrar(c.pop("texto_c"), "contratos.texto")
    c["assinatura"] = cripto.decifrar_bytes(c.pop("assinatura_c"), "contratos.assinatura")
    c["confere"] = calcular_hash(str(c["pedido_id"]), c["texto"], c["assinatura"], c["assinado_em"]) == c["hash"]
    return c


def mascarar_cpf(texto_contrato: str) -> str:
    """Esconde CPF e telefone do texto (página pública e quem só pode ver mascarado)."""
    t = re.sub(r"\d{3}\.\d{3}\.\d{3}-(\d{2})", r"***.***.***-\1", texto_contrato)
    t = re.sub(r"\((\d{2})\) \d{4,5}-(\d{4})", r"(\1) •••••-\2", t)
    return re.sub(r"\b\d{6,9}(\d{4})\b", r"•••••\1", t)
