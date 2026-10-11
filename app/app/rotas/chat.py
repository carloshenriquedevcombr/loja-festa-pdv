"""Chat do site: a cliente conversa numa janelinha do site; a equipe responde pelo WhatsApp.

Mensagem da cliente -> WhatsApp de quem marcou o aviso "Chat do site":
    💬 Site #K3F9 · Maria: Tem o vestido azul no 40?
    ↩ Responda começando com #K3F9 (ou respondendo esta mensagem)
Resposta "#K3F9 Temos sim!" -> aparece na janelinha da cliente.
"""
import hashlib
import re
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import avisos, config, cripto, db
from ..pagamento import formulario
from ..web import CsrfInvalido, checar_csrf

router = APIRouter()
COOKIE = "chat_site"
LETRAS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # sem 0/O e 1/I para não confundir


def _hash(segredo: str) -> str:
    return hashlib.sha256(("chat:" + segredo).encode()).hexdigest()


def _conversa(cur, request) -> dict | None:
    segredo = request.cookies.get(COOKIE)
    if not segredo or len(segredo) > 100:
        return None
    cur.execute("SELECT id::text, codigo, nome_c FROM chat_conversas WHERE segredo_hash = %s AND aberta", (_hash(segredo),))
    return cur.fetchone()


def _mensagens(cur, conversa_id: str, depois: int = 0) -> list[dict]:
    cur.execute("""SELECT id, de, texto_c, autor, to_char(criado_em AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') AS hora
                     FROM chat_mensagens WHERE conversa_id = %s AND id > %s ORDER BY id LIMIT 100""",
                (conversa_id, depois))
    return [{"id": m["id"], "de": m["de"], "autor": m["autor"], "hora": m["hora"],
             "texto": cripto.decifrar(m["texto_c"], "chat.texto")} for m in cur.fetchall()]


@router.get("/site/chat/mensagens")
def mensagens(request: Request, depois: int = 0):
    with db.transacao() as cur:
        c = _conversa(cur, request)
        if not c:
            return JSONResponse({"conversa": False, "mensagens": []}, headers={"Cache-Control": "no-store"})
        return JSONResponse({"conversa": True, "codigo": c["codigo"], "mensagens": _mensagens(cur, c["id"], depois)},
                            headers={"Cache-Control": "no-store"})


@router.post("/site/chat/enviar")
def enviar(request: Request, form: dict = Depends(formulario)):
    try:
        checar_csrf(request, form.get("csrf"))
    except CsrfInvalido:
        return JSONResponse({"erro": "Recarregue a página e tente de novo."}, status_code=400)
    texto = (form.get("texto") or "").strip()[:1000]
    if len(texto) < 1:
        return JSONResponse({"erro": "Escreva sua mensagem."}, status_code=400)
    novo_segredo = None
    with db.transacao() as cur:
        c = _conversa(cur, request)
        if not c:
            nome = (form.get("nome") or "").strip()[:60]
            if len(nome) < 2:
                return JSONResponse({"erro": "Escreva seu nome para começar."}, status_code=400)
            if form.get("lgpd") != "1":
                return JSONResponse({"erro": "Marque que concorda com o atendimento pelo chat."}, status_code=400)
            fone = cripto.so_digitos(form.get("telefone"))[:13] or None
            from ..auth import ip
            origem = hashlib.sha256(f"{ip(request) or '-'}|{config.SESSION_SECRET}".encode()).hexdigest()[:32]
            cur.execute("""SELECT count(*) AS n FROM chat_conversas WHERE origem_hash = %s
                             AND criado_em > now() - interval '1 hour'""", (origem,))
            if cur.fetchone()["n"] >= 5:
                return JSONResponse({"erro": "Muitas conversas agora. Fale pelo WhatsApp."}, status_code=429)
            novo_segredo = secrets.token_urlsafe(32)
            for _ in range(10):
                codigo = "".join(secrets.choice(LETRAS) for _ in range(4))
                cur.execute("SELECT 1 FROM chat_conversas WHERE codigo = %s", (codigo,))
                if not cur.fetchone():
                    break
            cur.execute("""INSERT INTO chat_conversas (loja_id, codigo, segredo_hash, nome_c, telefone_c, origem_hash)
                           VALUES (ctx_loja(), %s, %s, %s, %s, %s) RETURNING id::text, codigo, nome_c""",
                        (codigo, _hash(novo_segredo), cripto.cifrar(nome, "chat.nome"), cripto.cifrar(fone, "chat.telefone"),
                         origem))
            c = cur.fetchone()
        nome = cripto.decifrar(c["nome_c"], "chat.nome")
        cur.execute("""INSERT INTO chat_mensagens (loja_id, conversa_id, de, texto_c) VALUES (ctx_loja(), %s, 'CLIENTE', %s)""",
                    (c["id"], cripto.cifrar(texto, "chat.texto")))
        cur.execute("UPDATE chat_conversas SET ultima_em = now() WHERE id = %s", (c["id"],))
        enviados = avisos.para_equipe(
            cur, "chat", f"💬 Site #{c['codigo']} · {nome}: {texto}\n↩ Responda começando com #{c['codigo']}",
            f"chat:{c['codigo']}")
        if not enviados:
            cur.execute("SELECT whatsapp FROM lojas WHERE id = ctx_loja()")
            zap = cur.fetchone()["whatsapp"]
            if not _mensagens(cur, c["id"])[:-1]:        # primeira mensagem: explica
                resposta = ("Recebemos sua mensagem! No momento a equipe está atendendo na loja."
                            + (f" Se preferir, chame no WhatsApp: https://wa.me/55{zap}" if zap else ""))
                cur.execute("""INSERT INTO chat_mensagens (loja_id, conversa_id, de, texto_c, autor)
                               VALUES (ctx_loja(), %s, 'LOJA', %s, 'Loja')""", (c["id"], cripto.cifrar(resposta, "chat.texto")))
        lista = _mensagens(cur, c["id"], int(form.get("depois") or 0) if str(form.get("depois") or "0").isdigit() else 0)
    resp = JSONResponse({"conversa": True, "codigo": c["codigo"], "mensagens": lista}, headers={"Cache-Control": "no-store"})
    if novo_segredo:
        resp.set_cookie(COOKIE, novo_segredo, max_age=7 * 86400, httponly=True, secure=config.COOKIE_SECURE,
                        samesite="strict")
    return resp


def resposta_da_equipe(cur, funcionario: dict, texto: str, citada: str | None) -> bool:
    """Chamado pelo aviso do WhatsApp quando um funcionário escreve. True se era resposta de chat."""
    codigo, corpo = None, texto
    m = re.match(r"^#([A-Za-z0-9]{4})\b[\s:,-]*(.*)$", texto, re.S)
    if m:
        cur.execute("SELECT 1 FROM chat_conversas WHERE codigo = %s", (m.group(1).upper(),))
        if cur.fetchone():
            codigo, corpo = m.group(1).upper(), m.group(2).strip()
    if not codigo and citada:
        cur.execute("SELECT motivo FROM mensagens WHERE id_externo = %s AND motivo LIKE 'chat:%%'", (str(citada),))
        linha = cur.fetchone()
        if linha:
            codigo = linha["motivo"].split(":")[1]
    if not codigo or not corpo:
        return False
    cur.execute("SELECT id::text FROM chat_conversas WHERE codigo = %s AND aberta", (codigo,))
    conversa = cur.fetchone()
    if not conversa:
        return False
    cur.execute("""INSERT INTO chat_mensagens (loja_id, conversa_id, de, texto_c, autor) VALUES (ctx_loja(), %s, 'LOJA', %s, %s)""",
                (conversa["id"], cripto.cifrar(corpo[:1000], "chat.texto"), avisos.primeiro_nome(funcionario["nome"])))
    cur.execute("UPDATE chat_conversas SET ultima_em = now() WHERE id = %s", (conversa["id"],))
    return True
