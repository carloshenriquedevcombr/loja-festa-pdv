"""Tela da Dona/Dev: quais botões da tela inicial cada cargo vê."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import botoes, db
from ..auth import auditar, requer
from ..pagamento import formulario
from ..web import checar_csrf, tela

router = APIRouter()


def _dados(cur):
    cur.execute("""SELECT pa.codigo, pa.nome, array_agg(pe.codigo) FILTER (WHERE pe.codigo IS NOT NULL) AS perms
                     FROM papeis pa LEFT JOIN papel_permissoes pp ON pp.papel_id = pa.id
                     LEFT JOIN permissoes pe ON pe.id = pp.permissao_id
                    WHERE pa.codigo <> ALL(%s) GROUP BY pa.id ORDER BY pa.id""", (list(botoes.QUEM_ESCOLHE),))
    cargos = cur.fetchall()
    cur.execute("SELECT papel, endereco FROM botoes_ocultos")
    ocultos = {(r["papel"], r["endereco"]) for r in cur.fetchall()}
    linhas = []
    for perms, rotulo, icone, url, _ in botoes.BOTOES:
        end = botoes.base(url)
        if end in botoes.FIXOS:
            continue
        linhas.append({"rotulo": rotulo, "icone": icone, "end": end,
                       "cargos": [{"codigo": c["codigo"], "tem": not perms or bool(set(perms) & set(c["perms"] or [])),
                                   "oculto": (c["codigo"], end) in ocultos} for c in cargos]})
    return cargos, linhas


@router.get("/config/botoes")
def ver(request: Request, ok: int = 0, usuario: dict = Depends(requer("botoes.escolher"))):
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cargos, linhas = _dados(cur)
    return tela(request, "config_botoes.html", usuario, cargos=cargos, linhas=linhas, ok=ok)


@router.post("/config/botoes")
def salvar(request: Request, usuario: dict = Depends(requer("botoes.escolher")), form: dict = Depends(formulario)):
    checar_csrf(request, form.get("csrf"))
    with db.transacao(usuario_id=usuario["id"]) as cur:
        cargos, linhas = _dados(cur)
        validos = {c["codigo"] for c in cargos}
        esconder = set()
        for l in linhas:
            for c in l["cargos"]:
                # Marcado = mostra. Só entra na lista o botão que o cargo tem e foi desmarcado.
                if c["tem"] and c["codigo"] in validos and form.get(f"v:{c['codigo']}:{l['end']}") != "1":
                    esconder.add((c["codigo"], l["end"]))
        cur.execute("DELETE FROM botoes_ocultos WHERE loja_id = ctx_loja()")
        for papel, end in sorted(esconder):
            cur.execute("INSERT INTO botoes_ocultos (loja_id, papel, endereco) VALUES (ctx_loja(), %s, %s)", (papel, end))
        auditar(cur, request, "botoes_ocultos", usuario["id"], "loja", None,
                {"ocultos": [f"{p}:{e}" for p, e in sorted(esconder)]})
    return RedirectResponse("/config/botoes?ok=1", status_code=303)
