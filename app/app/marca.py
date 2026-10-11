"""Marca da loja: logo (horizontal) e ícone (quadrado) usados no site, no sistema e em tudo que imprime.

Arquivos em FOTOS_DIR/marca (nome aleatório, 32 letras hex):
  <nome>-logo.png   logo colorida, fundo transparente, até 900 x 300
  <nome>-mono.png   logo preto e branco (impressora térmica, etiqueta)
  <icone>-32/180/192/512.png e <icone>.ico   ícones do navegador, do celular e da tela inicial
Sem ícone enviado, o ícone sai da própria logo; sem logo nenhuma, da primeira letra do nome da loja.
SVG não é aceito (pode carregar código escondido); só PNG, JPG e WEBP, sempre regravados aqui.
"""
import io
import secrets
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from . import config

MAX_BYTES = 8 * 1024 * 1024
FORMATOS = {"PNG", "JPEG", "WEBP"}
ICONES = (32, 180, 192, 512)


class MarcaInvalida(ValueError):
    pass


def pasta() -> Path:
    p = Path(config.FOTOS_DIR) / "marca"
    p.mkdir(parents=True, exist_ok=True)
    return p


def caminho(nome: str, sufixo: str) -> Path:
    return pasta() / f"{nome}-{sufixo}" if sufixo != "ico" else pasta() / f"{nome}.ico"


def _abrir(dados: bytes) -> Image.Image:
    if not dados:
        raise MarcaInvalida("Escolha o arquivo da imagem.")
    if len(dados) > MAX_BYTES:
        raise MarcaInvalida("Imagem grande demais (máximo 8 MB).")
    try:
        with Image.open(io.BytesIO(dados)) as teste:
            teste.verify()
        img = Image.open(io.BytesIO(dados))
        if img.format not in FORMATOS:
            raise MarcaInvalida("Use PNG (de preferência com fundo transparente), JPG ou WEBP.")
        img = ImageOps.exif_transpose(img).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise MarcaInvalida("Não consegui abrir essa imagem. Use PNG, JPG ou WEBP.") from e
    if min(img.size) < 32:
        raise MarcaInvalida("Imagem pequena demais.")
    return _aparar(img)


def _fundo_branco_vira_transparente(img: Image.Image) -> Image.Image:
    """Logo em JPG vem com fundo branco: o branco quase puro vira transparente (fica bom no site)."""
    if img.getchannel("A").getextrema()[0] < 255:          # já tem transparência
        return img
    dados = [(r, g, b, 0) if r > 245 and g > 245 and b > 245 else (r, g, b, a) for (r, g, b, a) in img.getdata()]
    novo = Image.new("RGBA", img.size)
    novo.putdata(dados)
    return novo


def _aparar(img: Image.Image) -> Image.Image:
    img = _fundo_branco_vira_transparente(img)
    caixa = img.getchannel("A").getbbox()
    if not caixa:
        raise MarcaInvalida("A imagem está vazia (toda transparente ou toda branca).")
    return img.crop(caixa)


def _quadrado(img: Image.Image, lado: int, fundo=(255, 255, 255, 0)) -> Image.Image:
    base = Image.new("RGBA", (lado, lado), fundo)
    copia = img.copy()
    margem = max(1, lado // 10)
    copia.thumbnail((lado - 2 * margem, lado - 2 * margem), Image.Resampling.LANCZOS)
    base.paste(copia, ((lado - copia.width) // 2, (lado - copia.height) // 2), copia)
    return base


def salvar_logo(dados: bytes) -> str:
    img = _abrir(dados)
    nome = secrets.token_hex(16)
    logo = img.copy()
    logo.thumbnail((900, 300), Image.Resampling.LANCZOS)
    logo.save(caminho(nome, "logo.png"), "PNG", optimize=True)
    # Preto e branco para a térmica: tudo que tem tinta vira preto, fundo branco
    branco = Image.new("RGBA", logo.size, (255, 255, 255, 255))
    branco.alpha_composite(logo)
    mono = branco.convert("L").point(lambda v: 0 if v < 170 else 255, mode="1")
    mono.save(caminho(nome, "mono.png"), "PNG", optimize=True)
    return nome


def salvar_icone(dados: bytes | None, logo_nome: str | None = None, texto: str = "") -> str:
    if dados:
        img = _abrir(dados)
    elif logo_nome and caminho(logo_nome, "logo.png").exists():
        img = Image.open(caminho(logo_nome, "logo.png")).convert("RGBA")
    else:
        img = _letra(texto)
    nome = secrets.token_hex(16)
    for lado in ICONES:
        fundo = (255, 255, 255, 255) if lado >= 180 else (255, 255, 255, 0)     # celular não gosta de transparente
        _quadrado(img, lado, fundo).save(caminho(nome, f"{lado}.png"), "PNG", optimize=True)
    _quadrado(img, 64).save(caminho(nome, "ico"), "ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return nome


def _letra(texto: str) -> Image.Image:
    letra = (texto.strip()[:1] or "L").upper()
    img = Image.new("RGBA", (256, 256), (26, 26, 26, 255))
    d = ImageDraw.Draw(img)
    try:
        fonte = ImageFont.load_default(size=170)
    except TypeError:                                   # Pillow antigo
        fonte = ImageFont.load_default()
    caixa = d.textbbox((0, 0), letra, font=fonte)
    d.text(((256 - (caixa[2] - caixa[0])) / 2 - caixa[0], (256 - (caixa[3] - caixa[1])) / 2 - caixa[1]),
           letra, font=fonte, fill=(255, 255, 255, 255))
    return img


@lru_cache(maxsize=8)
def icone_padrao(texto: str, lado: int, ico: bool = False) -> bytes:
    """Ícone de letra (loja ainda sem marca), gerado na memória."""
    saida = io.BytesIO()
    img = _quadrado(_letra(texto), lado, (26, 26, 26, 255))
    if ico:
        img.save(saida, "ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    else:
        img.save(saida, "PNG")
    return saida.getvalue()


def arquivo_valido(nome: str | None) -> bool:
    return bool(nome) and len(nome) == 32 and all(c in "0123456789abcdef" for c in nome)


def logo_bytes(nome: str | None, mono: bool = False) -> bytes | None:
    if not arquivo_valido(nome):
        return None
    p = caminho(nome, "mono.png" if mono else "logo.png")
    return p.read_bytes() if p.exists() else None


# --------------------------------------------------------------------------- #
# Símbolos das redes sociais e bandeiras (arquivo oficial enviado pela loja)
# --------------------------------------------------------------------------- #
def pasta_simbolos() -> Path:
    p = Path(config.FOTOS_DIR) / "simbolos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def salvar_simbolo(dados: bytes) -> str:
    """Confere e regrava (PNG, até 240 x 120, sem dados escondidos). SVG não: pode levar código."""
    img = _abrir(dados)
    img.thumbnail((240, 120), Image.Resampling.LANCZOS)
    nome = secrets.token_hex(16)
    img.save(pasta_simbolos() / f"{nome}.png", "PNG", optimize=True)
    return nome
