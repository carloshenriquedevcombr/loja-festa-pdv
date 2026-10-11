"""Código de barras Code128 (conjunto B) em SVG, sem dependências.
Lido por qualquer leitor de código de barras USB comum (ele "digita" o SKU)."""

# Larguras barra/espaço de cada símbolo 0..105, e o STOP (106).
PADROES = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]
START_B, STOP = 104, 106

# Autoconferência da tabela: cada símbolo tem 11 módulos (STOP tem 13) e nenhum se repete.
assert len(PADROES) == 107 and len(set(PADROES)) == 107
assert all(sum(map(int, p)) == 11 for p in PADROES[:106]) and sum(map(int, PADROES[STOP])) == 13


def simbolos(texto: str) -> list[int]:
    if not texto or any(not 32 <= ord(c) <= 126 for c in texto):
        raise ValueError("Code128-B aceita só caracteres ASCII visíveis.")
    valores = [ord(c) - 32 for c in texto]
    soma = START_B + sum(i * v for i, v in enumerate(valores, start=1))
    return [START_B, *valores, soma % 103, STOP]


def larguras(texto: str) -> list[int]:
    """Sequência de larguras (barra, espaço, barra, ...) em módulos."""
    return [int(w) for s in simbolos(texto) for w in PADROES[s]]


def svg(texto: str, modulo: float = 2.0, altura: float = 70.0, margem: int = 10) -> str:
    x = margem * modulo
    barras = []
    for i, w in enumerate(larguras(texto)):
        if i % 2 == 0:                                     # posições pares são barras
            barras.append(f'<rect x="{x:.1f}" y="0" width="{w * modulo:.1f}" height="{altura:.0f}"/>')
        x += w * modulo
    largura = x + margem * modulo
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {largura:.1f} {altura:.0f}" '
            f'width="{largura:.0f}" height="{altura:.0f}" role="img" aria-label="Código de barras {texto}">'
            f'<rect width="100%" height="100%" fill="#fff"/><g fill="#000">{"".join(barras)}</g></svg>')


def decodificar(ws: list[int]) -> str:
    """Leitura de volta (usada nos testes): larguras -> texto, conferindo o dígito verificador."""
    tabela = {p: i for i, p in enumerate(PADROES)}
    corpo, fim = ws[:-7], ws[-7:]
    if "".join(map(str, fim)) != PADROES[STOP]:
        raise ValueError("sem STOP")
    simb = [tabela["".join(map(str, corpo[i:i + 6]))] for i in range(0, len(corpo), 6)]
    if simb[0] != START_B:
        raise ValueError("sem START B")
    dados, verif = simb[1:-1], simb[-1]
    if (START_B + sum(i * v for i, v in enumerate(dados, 1))) % 103 != verif:
        raise ValueError("dígito verificador errado")
    return "".join(chr(v + 32) for v in dados)
