"""Reimprimir: um lugar só para imprimir de novo comprovante, contrato, checklist, etiqueta, carnê, recibos
e ficha de medidas. Procura pelo código do comprovante/QR, código da peça, nome, telefone ou CPF da cliente."""
from psycopg import sql
from fastapi import APIRouter, Depends, Request

from .. import codigos, db
from ..auth import requer
from ..web import TZ_SQL, tela
from .caixa import filtro_cliente
from .pedidos import STATUS

router = APIRouter()


@router.get("/reimprimir")
def reimprimir(request: Request, q: str = "", usuario: dict = Depends(requer("pedidos.ver"))):
    q = q.strip()[:60]
    with db.transacao(usuario_id=usuario["id"]) as cur:
        params: dict = {}
        if q:
            numero, _ = codigos.separar(q)
            filtro, params = filtro_cliente(q)
            params.update({"sku": f"%{q}%",
                           "pref": f"{numero.lower()}%" if codigos.parece_codigo_pedido(numero) else "#"})
            onde = f"WHERE (pr.sku ILIKE %(sku)s OR replace(pe.id::text, '-', '') LIKE %(pref)s OR {filtro})"
        else:
            onde = ""
        cur.execute(sql.SQL("""SELECT pe.id::text AS id, pe.tipo, pe.com_ajuste, pe.status, pr.id::text AS peca_id, pr.sku,
                               pr.nome AS peca, cl.nome AS cliente, cl.id::text AS cliente_id,
                               to_char(pe.criado_em AT TIME ZONE {tz}, 'DD/MM/YYYY') AS quando,
                               EXISTS (SELECT 1 FROM contratos ct WHERE ct.pedido_id = pe.id) AS assinado
                          FROM pedidos pe JOIN produtos pr ON pr.id = pe.produto_id
                          LEFT JOIN clientes cl ON cl.id = pe.cliente_id
                          {onde} ORDER BY pe.criado_em DESC LIMIT {limite}""").format(
                            tz=TZ_SQL, onde=sql.SQL(onde), limite=sql.Literal(20 if q else 12)), params)
        pedidos = cur.fetchall()
        ids = [p["id"] for p in pedidos]
        recibos, carnes = {}, {}
        if ids:
            cur.execute(sql.SQL("""SELECT t.id, t.pedido_id::text AS pedido_id, t.valor_centavos, t.tipo,
                                   to_char(t.criado_em AT TIME ZONE {tz}, 'DD/MM') AS quando
                              FROM transacoes t WHERE t.pedido_id = ANY(%s::uuid[]) ORDER BY t.id""").format(tz=TZ_SQL), (ids,))
            for t in cur.fetchall():
                recibos.setdefault(t["pedido_id"], []).append(t)
            cur.execute("""SELECT c.id::text AS id, c.pedido_id::text AS pedido_id, c.parcelas, c.status
                             FROM crediarios c WHERE c.pedido_id = ANY(%s::uuid[]) AND c.status = 'ATIVO'""", (ids,))
            for c in cur.fetchall():
                carnes.setdefault(c["pedido_id"], []).append(c)
        clientes = []
        if q:
            cur.execute(sql.SQL("""SELECT cl.id::text AS id, cl.nome,
                                   EXISTS (SELECT 1 FROM medidas m WHERE m.cliente_id = cl.id) AS tem_medidas
                              FROM clientes cl WHERE {filtro} ORDER BY cl.nome LIMIT 10""").format(
                                filtro=sql.SQL(filtro_cliente(q)[0])), params)
            clientes = cur.fetchall()
    for p in pedidos:
        p["codigo"] = codigos.codigo_pedido(p["id"])
        p["recibos"] = recibos.get(p["id"], [])
        p["carnes"] = carnes.get(p["id"], [])
    return tela(request, "reimprimir.html", usuario, q=q, pedidos=pedidos, clientes=clientes, STATUS=STATUS)
