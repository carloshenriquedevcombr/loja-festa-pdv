"""Impressão e PDF: comprovante do pedido, recibo, extrato do caixa e extrato da cliente.

Toda página tem três jeitos de sair: folha A4, impressora térmica (80 mm ou 58 mm) e PDF.
Emitir documento com dados pessoais fica na auditoria (LGPD).
"""
from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from .. import codigos, db, documentos, pdf
from ..auth import auditar, requer
from ..web import TZ, tela
from .clientes import mascarar
from .pedidos import STATUS

router = APIRouter()
FORMATOS = {"a4": "Folha A4", "80": "Térmica 80 mm", "58": "Térmica 58 mm"}
RECIBO = ("caixa.entrada", "caixa.saida", "caixa.fechar", "relatorios.completo")
EXTRATO = ("caixa.fechar", "relatorios.completo")
MAX_DIAS_EXTRATO = 92


def _formato(f: str, padrao: str = "a4") -> str:
    return f if f in FORMATOS else padrao


def _entregar(request, usuario, doc, formato, como_pdf, nome_arquivo, voltar, extra=None):
    if doc is None:
        return tela(request, "erro.html", usuario, status=404, titulo="Documento não encontrado")
    if como_pdf:
        return Response(pdf.gerar(doc, formato), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}.pdf"'})
    pagina = request.url.path
    pdf_url = pagina.removesuffix("/imprimir") + "/pdf"
    extra = extra or {}
    periodo = f"de={extra['de']}&ate={extra['ate']}&" if extra.get("periodo") else ""
    return tela(request, "impressao.html", usuario, doc=doc, formato=formato, FORMATOS=FORMATOS, voltar=voltar,
                extra=extra, pagina=pagina, pdf_url=pdf_url, periodo=periodo)


def _registrar(request, usuario, qual, entidade, entidade_id, formato, como_pdf):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        auditar(cur, request, "documento_emitido", usuario["id"], entidade, entidade_id,
                {"documento": qual, "formato": formato, "pdf": como_pdf})


# --------------------------------------------------------------------------- #
# Comprovante do pedido
# --------------------------------------------------------------------------- #
def _pedido(request, pid, formato, usuario, como_pdf):
    formato = _formato(formato)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        doc = documentos.pedido(cur, pid, STATUS, lambda t: mascarar(usuario, t), base=codigos.base(request))
    if doc:
        _registrar(request, usuario, "pedido", "pedido", str(pid), formato, como_pdf)
    return _entregar(request, usuario, doc, formato, como_pdf,
                     f"pedido-{doc['numero'] if doc else pid}", f"/pedidos/{pid}")


@router.get("/pedidos/{pid}/imprimir")
def pedido_imprimir(request: Request, pid: UUID, formato: str = "a4", usuario: dict = Depends(requer("pedidos.ver"))):
    return _pedido(request, pid, formato, usuario, False)


@router.get("/pedidos/{pid}/pdf")
def pedido_pdf(request: Request, pid: UUID, formato: str = "a4", usuario: dict = Depends(requer("pedidos.ver"))):
    return _pedido(request, pid, formato, usuario, True)


# --------------------------------------------------------------------------- #
# Recibo de um lançamento do caixa (sai direto na térmica)
# --------------------------------------------------------------------------- #
def _recibo(request, tid, formato, usuario, como_pdf):
    formato = _formato(formato, "80")
    with db.transacao(usuario_id=usuario["id"]) as cur:
        doc = documentos.recibo(cur, tid, lambda t: mascarar(usuario, t))
    return _entregar(request, usuario, doc, formato, como_pdf, f"recibo-{tid}", "/caixa")


@router.get("/caixa/recibo/{tid}")
def recibo_imprimir(request: Request, tid: int, formato: str = "80", usuario: dict = Depends(requer(*RECIBO))):
    return _recibo(request, tid, formato, usuario, False)


@router.get("/caixa/recibo/{tid}/pdf")
def recibo_pdf(request: Request, tid: int, formato: str = "80", usuario: dict = Depends(requer(*RECIBO))):
    return _recibo(request, tid, formato, usuario, True)


# --------------------------------------------------------------------------- #
# Extrato do caixa por período
# --------------------------------------------------------------------------- #
def _periodo(de: str, ate: str, hoje: date) -> tuple[date, date, str | None]:
    try:
        d1 = date.fromisoformat(de) if de else hoje
        d2 = date.fromisoformat(ate) if ate else d1
    except ValueError:
        return hoje, hoje, "Data inválida. Mostrando o dia de hoje."
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > MAX_DIAS_EXTRATO:
        return d1, d1 + timedelta(days=MAX_DIAS_EXTRATO), f"O extrato mostra no máximo {MAX_DIAS_EXTRATO} dias por vez."
    return d1, d2, None


def _extrato(request, de, ate, formato, usuario, como_pdf):
    formato = _formato(formato)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cur.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS hoje")
        hoje = cur.fetchone()["hoje"]
        d1, d2, aviso = _periodo(de, ate, hoje)
        doc = documentos.extrato_caixa(cur, d1, d2)
    _registrar(request, usuario, "extrato_caixa", "caixa", None, formato, como_pdf)
    return _entregar(request, usuario, doc, formato, como_pdf, f"extrato-caixa-{d1}-a-{d2}", f"/caixa?dia={d1}",
                     extra={"periodo": True, "de": d1.isoformat(), "ate": d2.isoformat(), "aviso": aviso,
                            "atalhos": [("Hoje", hoje, hoje), ("Ontem", hoje - timedelta(days=1), hoje - timedelta(days=1)),
                                        ("Últimos 7 dias", hoje - timedelta(days=6), hoje),
                                        ("Este mês", hoje.replace(day=1), hoje)]})


@router.get("/caixa/extrato")
def extrato_imprimir(request: Request, de: str = "", ate: str = "", formato: str = "a4",
                     usuario: dict = Depends(requer(*EXTRATO))):
    return _extrato(request, de, ate, formato, usuario, False)


@router.get("/caixa/extrato/pdf")
def extrato_pdf(request: Request, de: str = "", ate: str = "", formato: str = "a4",
                usuario: dict = Depends(requer(*EXTRATO))):
    return _extrato(request, de, ate, formato, usuario, True)


# --------------------------------------------------------------------------- #
# Extrato da cliente (pedidos e pagamentos)
# --------------------------------------------------------------------------- #
def _extrato_cliente(request, cid, formato, usuario, como_pdf):
    formato = _formato(formato)
    with db.transacao(usuario_id=usuario["id"]) as cur:
        doc = documentos.extrato_cliente(cur, cid, STATUS, lambda t: mascarar(usuario, t))
    if not doc:
        return RedirectResponse("/clientes", status_code=303)
    _registrar(request, usuario, "extrato_cliente", "cliente", str(cid), formato, como_pdf)
    return _entregar(request, usuario, doc, formato, como_pdf, "extrato-cliente", f"/clientes/{cid}")


@router.get("/clientes/{cid}/extrato")
def extrato_cliente(request: Request, cid: UUID, formato: str = "a4", usuario: dict = Depends(requer("pedidos.ver"))):
    return _extrato_cliente(request, cid, formato, usuario, False)


@router.get("/clientes/{cid}/extrato/pdf")
def extrato_cliente_pdf(request: Request, cid: UUID, formato: str = "a4",
                        usuario: dict = Depends(requer("pedidos.ver"))):
    return _extrato_cliente(request, cid, formato, usuario, True)
