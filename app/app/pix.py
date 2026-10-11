"""Pix "copia e cola" (BR Code estático do Banco Central) para o carnê da loja.

É só texto montado aqui, sem falar com banco nenhum: a cliente lê o QR no app do banco dela e o
valor e a chave da loja já aparecem. A loja confere o Pix no extrato e dá baixa na parcela.
"""
import re
import unicodedata


def _limpo(texto: str, tamanho: int) -> str:
    """Nome e cidade: sem acento, maiúsculas, só letras/números/espaço (o padrão pede ASCII)."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]", "", t).upper().strip()[:tamanho]


def _campo(ident: str, valor: str) -> str:
    return f"{ident}{len(valor):02d}{valor}"


def crc16(dados: str) -> str:
    crc = 0xFFFF
    for byte in dados.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def chave_valida(chave: str | None) -> bool:
    c = (chave or "").strip()
    return bool(
        re.fullmatch(r"\d{11}|\d{14}", c)                                  # CPF / CNPJ
        or re.fullmatch(r"\+55\d{10,11}", c)                                # celular
        or re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}", c) and len(c) <= 77   # e-mail
        or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", c))   # aleatória


def br_code(chave: str, nome: str, cidade: str, valor_centavos: int | None = None, txid: str = "***") -> str:
    conta = _campo("00", "br.gov.bcb.pix") + _campo("01", chave.strip())
    txid = re.sub(r"[^A-Za-z0-9]", "", txid)[:25] or "***"
    corpo = (_campo("00", "01") + _campo("26", conta) + _campo("52", "0000") + _campo("53", "986")
             + (_campo("54", f"{valor_centavos / 100:.2f}") if valor_centavos else "")
             + _campo("58", "BR") + _campo("59", _limpo(nome, 25) or "LOJA") + _campo("60", _limpo(cidade, 15) or "BRASIL")
             + _campo("62", _campo("05", txid)) + "6304")
    return corpo + crc16(corpo)
