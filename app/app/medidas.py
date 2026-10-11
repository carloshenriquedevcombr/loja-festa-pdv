"""Fichas de medidas da costura (feminina e masculina).

Cada ponto tem o mesmo número do desenho da tela (templates/_figuras.html), igual às fichas de
modelagem que as costureiras já usam. Os valores ficam em centímetros.
"""
from decimal import Decimal, InvalidOperation

# (número, chave, nome, onde medir, mínimo, máximo, nas_costas)
FICHAS = {
    "FEMININO": {
        "nome": "Feminina",
        "pontos": [
            (1, "cintura", "Contorno da cintura", "Na parte mais fina, sem apertar", 40, 200, False),
            (2, "quadril", "Contorno do quadril", "Na parte mais larga do bumbum", 50, 220, False),
            (3, "busto", "Contorno do busto", "Na altura dos bicos, fita reta nas costas", 50, 220, False),
            (4, "frente", "Comprimento da frente", "Base do pescoço até a cintura, passando pelo busto", 20, 70, False),
            (5, "altura_busto", "Altura do busto", "Do ombro até o bico do seio", 10, 50, False),
            (6, "separacao_busto", "Separação do busto", "De um bico ao outro", 8, 35, False),
            (7, "costas", "Comprimento das costas", "Da nuca até a cintura", 20, 70, True),
            (8, "largura_costas", "Largura das costas", "De uma axila à outra, pelas costas", 20, 60, True),
            (9, "ombros", "Largura dos ombros", "De uma ponta de ombro à outra", 25, 60, False),
            (10, "braco", "Comprimento do braço", "Da ponta do ombro até o punho", 30, 90, False),
            (11, "contorno_braco", "Contorno do braço", "Na parte mais grossa", 15, 60, False),
            (12, "punho", "Contorno do punho", "Em volta do pulso", 10, 30, False),
            (13, "pescoco", "Contorno do pescoço", "Na base do pescoço", 25, 55, False),
            (14, "comprimento_saia", "Cintura ao chão", "Com o salto que vai usar na festa", 30, 140, False),
            (15, "gancho", "Altura do gancho", "Sentada: da cintura até a cadeira", 15, 45, False),
            (16, "altura_quadril", "Altura do quadril", "Da cintura até a parte mais larga", 10, 35, False),
            (17, "joelho", "Cintura ao joelho", "Para vestidos curtos e midi", 30, 80, False),
            (18, "altura", "Altura total", "Descalça", 60, 210, False),
            (19, "comprimento_vestido", "Comprimento do vestido", "Do ombro até onde a barra deve ficar", 30, 180, False),
        ],
    },
    "MASCULINO": {
        "nome": "Masculina",
        "pontos": [
            (1, "torax", "Tórax", "Na altura do peito, braços soltos", 50, 180, False),
            (2, "cintura", "Cintura", "Onde a calça fica (altura do umbigo)", 40, 180, False),
            (3, "quadril", "Quadril", "Na parte mais larga", 50, 200, False),
            (4, "ombro", "Ombro a ombro", "Pelas costas, de uma ponta à outra", 30, 65, True),
            (5, "manga", "Manga", "Da ponta do ombro até o punho", 30, 80, False),
            (6, "paleto", "Comprimento do paletó", "Da base da gola até a barra", 40, 100, True),
            (7, "colarinho", "Colarinho", "Em volta do pescoço + 1 dedo", 25, 55, False),
            (8, "calca", "Comprimento da calça", "Da cintura até o chão, com o sapato", 40, 130, False),
            (9, "entrepernas", "Entrepernas", "Do gancho até o chão", 30, 100, False),
            (10, "coxa", "Contorno da coxa", "Na parte mais grossa", 25, 90, False),
            (11, "altura", "Altura total", "Descalço", 60, 210, False),
        ],
    },
}

# Tabela de referência (a que a loja usa com o fornecedor de ternos). Confira sempre na prova.
BLAZER = [(44, 104), (46, 108), (48, 114), (50, 120), (52, 124), (54, 128), (56, 132)]          # tamanho, tórax
CALCA = [(36, 72), (38, 76), (40, 82), (42, 86), (44, 92), (46, 98), (48, 104), (50, 108), (52, 114), (54, 118)]  # cintura


def ler(ficha: str, form: dict) -> tuple[dict, Decimal | None, str | None]:
    """Lê os campos 'm_<FICHA>_<chave>' (cada ficha tem os seus: a cintura da feminina não se mistura
    com a da masculina). Devolve (valores, salto, erro)."""
    valores = {}
    for n, chave, nome, _, minimo, maximo, _ in FICHAS[ficha]["pontos"]:
        texto = (form.get(f"m_{ficha}_{chave}") or "").strip().replace(",", ".")
        if not texto:
            continue
        try:
            v = Decimal(texto)
        except InvalidOperation:
            return {}, None, f"{n}. {nome}: digite só o número em centímetros (ex.: 92,5)."
        if not minimo <= v <= maximo:
            return {}, None, f"{n}. {nome}: {texto.replace('.', ',')} cm parece errado. Confira a fita."
        valores[chave] = float(v.quantize(Decimal("0.1")))
    salto = None
    texto = (form.get("salto") or "").strip().replace(",", ".")
    if texto:
        try:
            salto = Decimal(texto)
        except InvalidOperation:
            return {}, None, "Salto: digite só o número em centímetros (ex.: 10)."
        if not 0 <= salto <= 25:
            return {}, None, "Salto: entre 0 e 25 cm."
    if not valores:
        return {}, None, "Preencha pelo menos uma medida."
    return valores, salto, None


def sugestao_masculina(valores: dict) -> dict:
    """Tamanho de paletó e calça pela tabela de referência."""
    saida = {}
    if valores.get("torax"):
        saida["paleto"] = next((t for t, torax in BLAZER if torax >= valores["torax"]), None)
    if valores.get("cintura"):
        saida["calca"] = next((t for t, cintura in CALCA if cintura >= valores["cintura"]), None)
    return saida


def numero(v) -> str:
    if v is None:
        return ""
    texto = f"{float(v):.1f}".replace(".", ",")
    return texto[:-2] if texto.endswith(",0") else texto
