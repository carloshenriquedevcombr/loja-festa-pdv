"""Transforma um documento (documentos.py) em PDF: folha A4 ou bobina térmica de 80 mm / 58 mm.

Usa as fontes que já vêm no leitor de PDF (Helvetica), sem arquivo de fonte extra: aceitam
acentos do português (latin-1). Símbolos fora disso (emoji, "•") são trocados antes.
Na bobina, a altura da página é calculada para caber tudo em uma tira só.
"""
from fpdf import FPDF

FORMATOS = {  # largura do papel, margem, fonte normal, fonte do título
    "a4": {"largura": 210, "margem": 15, "fonte": 10, "titulo": 16},
    "80": {"largura": 80, "margem": 4, "fonte": 8.5, "titulo": 11},
    "58": {"largura": 58, "margem": 3, "fonte": 7.5, "titulo": 9.5},
}
TROCAS = {"•": "*", "–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "...", "·": "-", "→": "->",
          " ": " ", " ": " "}


def limpar(texto) -> str:
    texto = "" if texto is None else str(texto)
    for de, para in TROCAS.items():
        texto = texto.replace(de, para)
    return texto.encode("latin-1", "ignore").decode("latin-1")


class _Papel(FPDF):
    def __init__(self, fmt: dict, altura: float | None):
        if altura is None:
            super().__init__(format="A4")
            self.set_auto_page_break(True, fmt["margem"])
        else:
            super().__init__(format=(fmt["largura"], altura))
            self.set_auto_page_break(False)
        self.fmt = fmt
        self.set_margins(fmt["margem"], fmt["margem"], fmt["margem"])
        self.set_creator("Sistema da loja")
        self.add_page()

    @property
    def util(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def fonte(self, tamanho=None, estilo=""):
        self.set_font("Helvetica", estilo, tamanho or self.fmt["fonte"])

    def texto(self, t, tamanho=None, estilo="", alinhar="L", altura=None):
        self.fonte(tamanho, estilo)
        self.multi_cell(self.util, altura or self.font_size * 1.45, limpar(t), align=alinhar,
                        new_x="LMARGIN", new_y="NEXT")

    def traco(self, espaco=1.5):
        self.ln(espaco)
        self.set_draw_color(0)
        self.set_line_width(0.2)
        if self.fmt["largura"] < 100:
            self.set_dash_pattern(dash=0.8, gap=0.8)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.set_dash_pattern()
        self.ln(espaco)


def _desenhar(pdf: _Papel, doc: dict) -> None:
    fmt, estreito = pdf.fmt, pdf.fmt["largura"] < 100
    loja = doc["loja"]
    # Cabeçalho da loja (com a logo, se tiver; na bobina, a versão preto e branco)
    _logo(pdf, loja.get("logo_arquivo"), estreito)
    pdf.texto(loja["nome"], fmt["titulo"], "B", "C")
    info = [x for x in (f"CNPJ {loja['cnpj']}" if loja["cnpj"] else None, loja["endereco"], loja["telefone"]) if x]
    for linha in info:
        pdf.texto(linha, fmt["fonte"] - 0.5, "", "C")
    pdf.traco()
    titulo = doc["titulo"] + (f" nº {doc['numero']}" if doc.get("numero") else "")
    pdf.texto(titulo.upper() if estreito else titulo, fmt["titulo"] - (1 if estreito else 2), "B", "C")
    if doc.get("subtitulo"):
        pdf.texto(doc["subtitulo"], fmt["fonte"], "", "C")
    pdf.texto(f"Emitido em {doc['emitido']}", fmt["fonte"] - 1, "", "C")
    pdf.traco()

    for b in doc["blocos"]:
        tipo = b["t"]
        if tipo == "secao":
            pdf.ln(1.5)
            pdf.texto(b["texto"].upper(), fmt["fonte"], "B")
            pdf.ln(0.5)
        elif tipo == "pares":
            _pares(pdf, b["itens"])
        elif tipo == "tabela":
            _tabela(pdf, b)
        elif tipo == "texto":
            pdf.ln(1.5)
            pdf.texto(b["texto"], fmt["fonte"] - (1 if b.get("pequeno") else 0))
        elif tipo == "assinaturas":
            _assinaturas(pdf, b["nomes"])
        elif tipo == "contrato":
            _contrato(pdf, b)
        elif tipo == "imagem":
            _imagem(pdf, b)
        elif tipo == "qr":
            _qr(pdf, b)
        elif tipo == "codigo":
            pdf.ln(1)
            pdf.texto(b["texto"], fmt["fonte"], "B", "C")
            if b.get("detalhe"):
                pdf.texto(b["detalhe"], fmt["fonte"] - 1.5, "", "C")

    if loja.get("rodape_comprovante"):
        pdf.traco()
        pdf.texto(loja["rodape_comprovante"], fmt["fonte"] - 0.5, "", "C")
    if estreito:
        pdf.ln(4)


def _pares(pdf: _Papel, itens) -> None:
    rotulo_w = pdf.util * (0.42 if pdf.fmt["largura"] < 100 else 0.30)
    for rotulo, valor, forte in itens:
        y = pdf.get_y()
        pdf.fonte(estilo="")
        altura = pdf.font_size * 1.45
        n = len(pdf.multi_cell(rotulo_w - 1, altura, limpar(rotulo), dry_run=True, output="LINES"))
        pdf.multi_cell(rotulo_w - 1, altura, limpar(rotulo), align="L", new_x="RIGHT", new_y="TOP")
        pdf.set_xy(pdf.l_margin + rotulo_w, y)
        pdf.fonte(estilo="B" if forte else "")
        alinhar = "R" if pdf.fmt["largura"] < 100 else "L"
        pdf.multi_cell(pdf.util - rotulo_w, altura, limpar(valor), align=alinhar, new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(max(pdf.get_y(), y + n * altura))


def _tabela(pdf: _Papel, b: dict) -> None:
    colunas, linhas = b["colunas"], list(b["linhas"])
    if b.get("total"):
        linhas.append(b["total"])
    if pdf.fmt["largura"] < 100:
        # Bobina: cada linha vira "primeira coluna ........ última coluna" + o meio em letra menor
        for i, linha in enumerate(linhas):
            forte = b.get("total") and i == len(linhas) - 1
            y = pdf.get_y()
            pdf.fonte(estilo="B" if forte else "")
            h = pdf.font_size * 1.45
            valor_w = pdf.util * 0.40
            n = len(pdf.multi_cell(pdf.util - valor_w, h, limpar(linha[0]), dry_run=True, output="LINES"))
            pdf.multi_cell(pdf.util - valor_w, h, limpar(linha[0]), align="L", new_x="RIGHT", new_y="TOP")
            pdf.set_xy(pdf.l_margin + pdf.util - valor_w, y)
            pdf.multi_cell(valor_w, h, limpar(linha[-1]), align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.set_y(max(pdf.get_y(), y + n * h))
            meio = " - ".join(x for x in linha[1:-1] if x)
            if meio:
                pdf.texto(meio, pdf.fmt["fonte"] - 1)
            pdf.ln(0.8)
        return
    total_peso = sum(c[2] for c in colunas)
    larguras = [pdf.util * c[2] / total_peso for c in colunas]
    pdf.fonte(pdf.fmt["fonte"] - 1, "B")
    h = pdf.font_size * 1.9
    pdf.set_fill_color(235, 235, 240)
    for (nome, alinhar, _), w in zip(colunas, larguras):
        pdf.cell(w, h, limpar(nome), border="B", align=alinhar, fill=True)
    pdf.ln(h)
    for i, linha in enumerate(linhas):
        forte = b.get("total") and i == len(linhas) - 1
        pdf.fonte(pdf.fmt["fonte"] - 0.5, "B" if forte else "")
        h = pdf.font_size * 1.6
        # altura da linha = a maior célula quebrada
        n = max(len(pdf.multi_cell(w, h, limpar(v), dry_run=True, output="LINES")) for v, w in zip(linha, larguras))
        if pdf.get_y() + n * h > pdf.page_break_trigger:
            pdf.add_page()
        y, x = pdf.get_y(), pdf.l_margin
        for (_, alinhar, _), w, v in zip(colunas, larguras, linha):
            pdf.set_xy(x, y)
            pdf.multi_cell(w, h, limpar(v), align=alinhar, new_x="RIGHT", new_y="TOP")
            x += w
        pdf.set_xy(pdf.l_margin, y + n * h)
        pdf.set_draw_color(200)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_draw_color(0)


def _contrato(pdf: _Papel, b: dict) -> None:
    pdf.ln(2)
    if pdf.fmt["largura"] < 100:                       # bobina: o contrato inteiro fica no link
        pdf.texto("CONTRATO", pdf.fmt["fonte"], "B")
        pdf.texto("As condições completas estão no contrato deste pedido:", pdf.fmt["fonte"] - 1)
        pdf.texto(b["link"], pdf.fmt["fonte"] - 1.5)
        return
    pdf.traco()
    for i, paragrafo in enumerate(b["texto"].split("\n")):
        if not paragrafo.strip():
            pdf.ln(1.2)
            continue
        pdf.texto(paragrafo, pdf.fmt["fonte"] - (0 if i == 0 else 1.5), "B" if i == 0 else "",
                  "C" if i == 0 else "J", altura=(pdf.fmt["fonte"] - 1.5) * 0.52)


def _logo(pdf: _Papel, nome: str | None, estreito: bool) -> None:
    import io
    from PIL import Image
    from . import marca
    dados = marca.logo_bytes(nome, mono=estreito)
    if not dados:
        return
    with Image.open(io.BytesIO(dados)) as img:
        proporcao = img.height / max(img.width, 1)
    largura = min(pdf.util * (0.8 if estreito else 0.45), (14 if estreito else 18) / max(proporcao, 0.01))
    pdf.image(io.BytesIO(dados), x=pdf.l_margin + (pdf.util - largura) / 2, w=largura)
    pdf.ln(1.5)


def _qr(pdf: _Papel, b: dict) -> None:
    import io
    import segno
    png = io.BytesIO()
    segno.make(b["url"], error="m", micro=False).save(png, kind="png", scale=6, border=2)
    lado = 24 if pdf.fmt["largura"] >= 100 else 26
    if pdf.get_y() + lado + 8 > pdf.page_break_trigger and pdf.fmt["largura"] >= 100:
        pdf.add_page()
    pdf.ln(2)
    pdf.image(io.BytesIO(png.getvalue()), x=pdf.l_margin + (pdf.util - lado) / 2, w=lado)
    pdf.texto(b["codigo"], pdf.fmt["fonte"], "B", "C")
    pdf.texto(b["texto"], pdf.fmt["fonte"] - 1.5, "", "C")


def _imagem(pdf: _Papel, b: dict) -> None:
    import io
    largura = min(70, pdf.util * 0.8)
    if pdf.get_y() + 32 > pdf.page_break_trigger and pdf.fmt["largura"] >= 100:
        pdf.add_page()
    pdf.ln(3)
    x = pdf.l_margin + (pdf.util - largura) / 2
    pdf.image(io.BytesIO(b["png"]), x=x, w=largura)
    pdf.set_draw_color(0)
    pdf.line(x, pdf.get_y(), x + largura, pdf.get_y())
    pdf.ln(1)
    pdf.texto(b["legenda"], pdf.fmt["fonte"] - 1.5, "", "C")


def _assinaturas(pdf: _Papel, nomes) -> None:
    estreito = pdf.fmt["largura"] < 100
    pdf.ln(12 if not estreito else 8)
    if estreito:
        for nome in nomes[:1]:          # na bobina: só a assinatura da cliente
            pdf.line(pdf.l_margin + 4, pdf.get_y(), pdf.w - pdf.r_margin - 4, pdf.get_y())
            pdf.ln(1)
            pdf.texto(nome, pdf.fmt["fonte"] - 1, "", "C")
        return
    w = (pdf.util - 20) / 2
    y = pdf.get_y()
    for i, nome in enumerate(nomes[:2]):
        x = pdf.l_margin + i * (w + 20)
        pdf.line(x, y, x + w, y)
        pdf.set_xy(x, y + 1)
        pdf.fonte(pdf.fmt["fonte"] - 1)
        pdf.cell(w, 5, limpar(nome), align="C")
    pdf.set_xy(pdf.l_margin, y + 8)


def gerar(doc: dict, formato: str = "a4") -> bytes:
    fmt = FORMATOS.get(formato, FORMATOS["a4"])
    if formato not in ("80", "58"):
        pdf = _Papel(fmt, None)
        _desenhar(pdf, doc)
    else:
        rascunho = _Papel(fmt, 3000)          # 1ª passada: mede a altura
        _desenhar(rascunho, doc)
        pdf = _Papel(fmt, rascunho.get_y() + fmt["margem"])
        _desenhar(pdf, doc)
    pdf.set_title(limpar(doc["titulo"]))
    return bytes(pdf.output())
