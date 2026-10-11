"""Senhas e PINs com Argon2id (parâmetros padrão da argon2-cffi, recomendados pela OWASP)."""
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

# Hash descartável: quando o login não existe, verificamos contra ele mesmo assim,
# para o tempo de resposta não revelar quais logins existem.
_HASH_FALSO = _hasher.hash(secrets.token_urlsafe(16))

SENHA_MINIMA = 8
SENHA_MAXIMA = 32
REGRA_SENHA = "de 8 a 32 caracteres, com letra maiúscula, letra minúscula, número e símbolo (ex.: ! @ # $ %)"


def gerar_hash(senha: str) -> str:
    return _hasher.hash(senha)


def verificar(hash_salvo: str | None, senha: str) -> bool:
    try:
        return _hasher.verify(hash_salvo or _HASH_FALSO, senha) and hash_salvo is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def precisa_rehash(hash_salvo: str) -> bool:
    return _hasher.check_needs_rehash(hash_salvo)


# Palavras que aparecem nas listas de senhas vazadas (credential stuffing) e no dia a dia da loja.
# "Senha@123", "Brasil@2026", "Mudar@123", "Vestido#1" passam na regra dos 4 tipos, mas são as primeiras
# que um robô tenta. Aqui elas são recusadas.
PALAVRAS_FRACAS = frozenset("""
senha password passw0rd pass admin administrador root usuario user login acesso entrar mudar trocar mudeme
teste test demo qwerty qwert asdf asdfg zxcv abc abcd abcde abcdef qwertyuiop azerty brasil brazil
amor iloveyou teamo deus jesus cristo familia fe esperanca felicidade vida
loja festa vestido vestidos noiva noivas terno ternos traje trajes aluguel moda roupa roupas boutique
bemvindo welcome ola hello oi segredo secreto default padrao sistema empresa
flamengo corinthians palmeiras santos saopaulo vasco gremio inter cruzeiro atletico
janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro
""".split())


def _letras(texto: str, leet: bool = False) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", texto.lower())
    if leet:                                                       # "S3nh@" -> "senha"
        t = t.translate(str.maketrans("@4310$5!7", "aaeiosstt"))
    return "".join(c for c in t if c.isalpha() and c.isascii())


def senha_facil(senha: str, pessoais=()) -> bool:
    """Palavra comum (ou nome da pessoa) com poucos enfeites em volta: Senha@123, Br@sil2026, Carlos#1."""
    candidatos = {_letras(senha), _letras(senha, leet=True)}
    fracas = set(PALAVRAS_FRACAS)
    for p in pessoais:
        fracas.update(x for x in (_letras(parte) for parte in str(p or "").replace(".", " ").split()) if len(x) >= 3)
    for c in candidatos:
        if not c or len(set(c)) <= 2:
            return True
        for w in fracas:
            if len(w) >= 3 and w in c and len(c) - len(w) <= 3:
                return True
    return len(set(senha.lower())) <= 4


def validar_senha_nova(senha: str, pessoais=()) -> str | None:
    """Devolve a mensagem de erro, ou None se a senha serve (regra da loja: 8 a 32, com os 4 tipos)."""
    faltando = []
    if not any(c.isupper() for c in senha):
        faltando.append("uma letra maiúscula")
    if not any(c.islower() for c in senha):
        faltando.append("uma letra minúscula")
    if not any(c.isdigit() for c in senha):
        faltando.append("um número")
    if not any(not c.isalnum() and not c.isspace() for c in senha):
        faltando.append("um símbolo (ex.: ! @ # $ %)")
    if len(senha) < SENHA_MINIMA:
        return f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres."
    if len(senha) > SENHA_MAXIMA:
        return f"A senha pode ter no máximo {SENHA_MAXIMA} caracteres."
    if faltando:
        return "Falta na senha: " + ", ".join(faltando) + "."
    if senha_facil(senha, pessoais):
        return ("Senha muito fácil de adivinhar (palavra comum, nome da pessoa ou da loja). "
                "Misture palavras que só você conhece, ex.: Gato3Azul!Pipoca")
    return None


def token() -> str:
    return secrets.token_urlsafe(32)
