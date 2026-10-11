"""QR code (segno, sem dependências nativas) e atalhos para os códigos impressos.

* Código do pedido: 8 letras/números (os 8 primeiros do id) — é o que o leitor USB "digita" do código de barras.
* QR do checklist abre o pedido no sistema (câmera do celular da loja); QR da etiqueta abre a peça no site
  (ou no sistema, se ela não estiver no site).
"""
import re

import segno
from markupsafe import Markup

from . import code128, config


def qr_svg(texto: str, rotulo: str = "QR code") -> Markup:
    q = segno.make(texto, error="m", micro=False)
    svg = q.svg_inline(scale=4, border=2, dark="#000", light="#fff", omitsize=True, svgclass="qr", lineclass=None)
    # SVG gerado pelo segno; o rótulo entra escapado
    return Markup(svg.replace("<svg ", f'<svg role="img" aria-label="{Markup.escape(rotulo)}" ', 1))  # nosec B704


def barras_svg(texto: str) -> Markup:
    # SVG montado em code128.svg, texto escapado.
    return Markup(code128.svg(texto, modulo=2.0, altura=60))  # nosec B704


def codigo_pedido(pedido_id: str) -> str:
    return str(pedido_id).replace("-", "")[:8].upper()


def parece_codigo_pedido(texto: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-Fa-f]{8}", (texto or "").strip()))


def base(request) -> str:
    return (config.SITE_URL or str(request.base_url)).rstrip("/")


# --------------------------------------------------------------------------- #
# Código de verificação impresso no QR da via da cliente: número do pedido + 4 letras de conferência
# (as 4 letras vêm de uma assinatura secreta do servidor: ninguém "chuta" o código de outro pedido pelo QR)
# --------------------------------------------------------------------------- #
_LETRAS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _conferencia(numero: str) -> str:
    import hashlib
    import hmac
    d = hmac.new(config.SESSION_SECRET.encode(), f"verificar:{numero}".encode(), hashlib.sha256).digest()
    return "".join(_LETRAS[b % len(_LETRAS)] for b in d[:4])


def codigo_verificacao(pedido_id: str) -> str:
    numero = codigo_pedido(pedido_id)
    return f"{numero}-{_conferencia(numero)}"


def separar(codigo: str) -> tuple[str, bool]:
    """'8B5DC7FF-K3Q9' -> ('8B5DC7FF', True se a conferência bate). Outros formatos voltam como vieram."""
    c = re.sub(r"[^A-Za-z0-9-]", "", codigo or "").upper()[:20]
    m = re.fullmatch(r"([0-9A-F]{8})-([A-Z0-9]{4})", c)
    if m:
        import hmac
        return m.group(1), hmac.compare_digest(m.group(2), _conferencia(m.group(1)))
    return c, False


def url_verificacao(request, pedido_id: str) -> str:
    return f"{base(request)}/v/{codigo_verificacao(pedido_id)}"
