"""Cifra dos dados pessoais (LGPD): AES-256-GCM com a chave DADOS_CHAVE, que fica fora do banco.

* Quem rouba só o banco (ou um backup) vê bytes sem sentido no lugar de CPF, telefone, e-mail,
  endereço, observações, medidas, assinaturas e conversas.
* Cada valor leva um "rótulo" (ex.: b"cliente.cpf") autenticado junto: um CPF cifrado não pode ser
  copiado para o lugar de um telefone sem a verificação falhar.
* Para achar alguém pelo telefone sem guardar o número, gravamos um índice cego:
  HMAC-SHA256(chave de índice, número normalizado).
Formato: versão (1 byte) + nonce (12 bytes) + texto cifrado com etiqueta GCM.
"""
import hashlib
import hmac
import json
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config

VERSAO = b"\x01"
_CHAVE = bytes.fromhex(config.DADOS_CHAVE)
_AES = AESGCM(_CHAVE)
_CHAVE_INDICE = hmac.new(_CHAVE, b"indice-cego", hashlib.sha256).digest()


class DadoCorrompido(ValueError):
    pass


def cifrar(texto: str | bytes | None, rotulo: str) -> bytes | None:
    if texto is None or texto == "":
        return None
    dados = texto if isinstance(texto, bytes) else str(texto).encode()
    nonce = os.urandom(12)
    return VERSAO + nonce + _AES.encrypt(nonce, dados, rotulo.encode())


def decifrar_bytes(cifrado, rotulo: str) -> bytes | None:
    if cifrado is None:
        return None
    b = bytes(cifrado)
    if b[:1] != VERSAO:
        raise DadoCorrompido("versão de cifra desconhecida")
    try:
        return _AES.decrypt(b[1:13], b[13:], rotulo.encode())
    except Exception as e:                                   # etiqueta inválida = dado adulterado
        raise DadoCorrompido("dado cifrado inválido") from e


def decifrar(cifrado, rotulo: str) -> str | None:
    b = decifrar_bytes(cifrado, rotulo)
    return b.decode() if b is not None else None


def cifrar_json(valor, rotulo: str) -> bytes:
    return cifrar(json.dumps(valor, separators=(",", ":")), rotulo)


def decifrar_json(cifrado, rotulo: str, padrao=None):
    texto = decifrar(cifrado, rotulo)
    return json.loads(texto) if texto else padrao


def so_digitos(texto) -> str:
    return re.sub(r"\D", "", texto or "")


def indice(valor: str | None, tipo: str) -> str | None:
    """Índice cego (para buscar sem guardar o valor)."""
    if not valor:
        return None
    return hmac.new(_CHAVE_INDICE, f"{tipo}:{valor}".encode(), hashlib.sha256).hexdigest()


def indice_telefone(fone: str | None) -> str | None:
    d = so_digitos(fone)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    return indice(d, "tel") if len(d) in (10, 11) else None


# --------------------------------------------------------------------------- #
# Linhas do banco: abre as colunas *_c num dicionário
# --------------------------------------------------------------------------- #
def abrir(linha: dict | None, tabela: str, *campos: str) -> dict | None:
    """Troca linha['telefone_c'] por linha['telefone'] (aberto), etc."""
    if linha is None:
        return None
    for campo in campos:
        chave = f"{campo}_c"
        if chave in linha:
            try:
                linha[campo] = decifrar(linha.pop(chave), f"{tabela}.{campo}")
            except DadoCorrompido:
                linha[campo] = None
    return linha


def abrir_todas(linhas: list, tabela: str, *campos: str) -> list:
    for linha in linhas:
        abrir(linha, tabela, *campos)
    return linhas
