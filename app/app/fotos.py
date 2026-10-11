"""Fotos das peças: confere, gira pelo EXIF, apaga dados escondidos (GPS etc.) e gera 3 tamanhos.

Arquivos em FOTOS_DIR:  <nome>-480.jpg, <nome>-960.jpg, <nome>-1600.jpg
O nome é aleatório (32 letras hex), então ninguém adivinha a foto de outra peça.
"""
import io
import secrets
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config

TAMANHOS = (480, 960, 1600)
MAX_BYTES = 15 * 1024 * 1024          # 15 MB por foto (celular bom tira fotos de 3 a 8 MB)
MAX_FOTOS = 12
Image.MAX_IMAGE_PIXELS = 60_000_000    # evita "bomba" de imagem gigante
FORMATOS = {"JPEG", "PNG", "WEBP", "MPO"}


class FotoInvalida(ValueError):
    pass


def pasta() -> Path:
    p = Path(config.FOTOS_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def caminho(arquivo: str, largura: int) -> Path:
    return pasta() / f"{arquivo}-{largura}.jpg"


def processar(dados: bytes) -> tuple[str, int, int]:
    """Salva os 3 tamanhos e devolve (nome, largura, altura) da maior."""
    if not dados:
        raise FotoInvalida("A foto veio vazia.")
    if len(dados) > MAX_BYTES:
        raise FotoInvalida("Foto grande demais (máximo 15 MB).")
    try:
        with Image.open(io.BytesIO(dados)) as teste:
            teste.verify()
        img = Image.open(io.BytesIO(dados))
        if img.format not in FORMATOS:
            raise FotoInvalida("Formato de foto não aceito. Use JPG, PNG ou WEBP.")
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new("RGB", img.size, (255, 255, 255))
            fundo.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
            img = fundo
        else:
            img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise FotoInvalida("Não consegui abrir essa foto. Tire outra ou use JPG/PNG.") from e
    if min(img.size) < 200:
        raise FotoInvalida("Foto pequena demais. Tire uma foto mais de perto e com boa luz.")

    nome = secrets.token_hex(16)
    feitos = []
    try:
        for largura in TAMANHOS:
            copia = img.copy()
            copia.thumbnail((largura, largura * 2), Image.Resampling.LANCZOS)
            destino = caminho(nome, largura)
            # Sem exif=: a foto nova não leva localização nem dados do celular
            copia.save(destino, "JPEG", quality=84, optimize=True, progressive=True)
            feitos.append(destino)
        return nome, copia.width, copia.height
    except OSError:
        for f in feitos:
            f.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Foto da nota / recibo (Paguei algo): pasta separada, nunca aparece no site
# --------------------------------------------------------------------------- #
def pasta_notas() -> Path:
    p = Path(config.FOTOS_DIR) / "notas"
    p.mkdir(parents=True, exist_ok=True)
    return p


def caminho_nota(nome: str) -> Path | None:
    if not (nome and len(nome) == 32 and all(c in "0123456789abcdef" for c in nome)):
        return None
    return pasta_notas() / f"{nome}.jpg"


def salvar_nota(dados: bytes) -> str:
    """Confere, tira dados escondidos e grava uma cópia de até 1600 px. Devolve o nome."""
    if not dados:
        raise FotoInvalida("A foto da nota veio vazia.")
    if len(dados) > MAX_BYTES:
        raise FotoInvalida("Foto grande demais (máximo 15 MB).")
    try:
        with Image.open(io.BytesIO(dados)) as teste:
            teste.verify()
        img = Image.open(io.BytesIO(dados))
        if img.format not in FORMATOS:
            raise FotoInvalida("Use foto em JPG, PNG ou WEBP.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as e:
        raise FotoInvalida("Não consegui abrir a foto da nota. Tire outra.") from e
    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    nome = secrets.token_hex(16)
    img.save(caminho_nota(nome), "JPEG", quality=82, optimize=True)
    return nome
