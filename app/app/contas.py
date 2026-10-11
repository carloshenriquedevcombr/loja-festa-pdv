"""Conta da cliente no site: entrar com código no WhatsApp ou com e-mail e senha.

Segurança (mesmas ideias da área da equipe):
* A sessão da cliente é separada da sessão da equipe (outra chave dentro do cookie cifrado) e fica
  registrada no banco: "Sair" encerra de verdade; presa ao navegador; vale 30 dias sem uso no máximo.
* Código do WhatsApp: 6 dígitos, 5 minutos, 5 tentativas; o hash fica no cookie cifrado (nada no banco).
* E-mail e senha: Argon2; conta trava 15 min após 5 erros; mensagem igual para e-mail inexistente e senha errada.
* Telefone que já tem ficha na loja só vira conta depois do código no WhatsApp (ninguém "toma" a ficha de outra
  pessoa cadastrando o telefone dela).
* Dados de pagamento nunca passam por aqui: o pagamento é na página do Mercado Pago.
"""
import hashlib
import hmac
import re
import secrets
import time

from . import avisos, config, cripto, pessoas, seguranca, whatsapp

CHAVE_SESSAO = "cliente"
DIAS_SESSAO = 30
CODIGO_SEGUNDOS = 300
CODIGO_TENTATIVAS = 5
FALHAS_TRAVA = 5


def _h(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def email_valido(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}", email or "")) and len(email) <= 120


def indice_email(email: str) -> str | None:
    return cripto.indice((email or "").strip().lower(), "email") if email else None


# --------------------------------------------------------------------------- #
# Código no WhatsApp
# --------------------------------------------------------------------------- #
def hash_codigo(codigo: str, fone: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), f"cliente:{fone}:{codigo}".encode(), hashlib.sha256).hexdigest()


def enviar_codigo(cur, sessao: dict, fone: str, motivo: str = "entrar") -> bool:
    """Manda o código e guarda o hash na sessão cifrada. False se o WhatsApp da loja não estiver ligado."""
    if not avisos.whatsapp_ativo(cur):
        return False
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    cur.execute("SELECT nome FROM lojas WHERE id = ctx_loja()")
    loja = cur.fetchone()["nome"]
    texto = (f"🔐 {loja}: seu código para {'entrar na sua conta' if motivo == 'entrar' else 'confirmar seu cadastro'} "
             f"no site é {codigo}. Vale por 5 minutos. Não passe para ninguém — a loja nunca pede.")
    cfg = whatsapp.config_loja(cur)
    try:
        whatsapp.enviar_texto(cfg, whatsapp.numero_whatsapp(fone), texto)
    except whatsapp.ErroWhatsApp:
        avisos.whatsapp(cur, fone, texto, "codigo_cliente")
    sessao["cliente_codigo"] = {"fone": fone, "hash": hash_codigo(codigo, fone), "ate": int(time.time()) + CODIGO_SEGUNDOS,
                                "tentativas": 0, "motivo": motivo}
    return True


def conferir_codigo(sessao: dict, codigo: str) -> tuple[str | None, str | None]:
    """(telefone confirmado, erro)."""
    p = sessao.get("cliente_codigo")
    if not p:
        return None, "Peça um código novo."
    if p["ate"] < time.time():
        sessao.pop("cliente_codigo", None)
        return None, "O código venceu. Peça outro."
    p["tentativas"] = p.get("tentativas", 0) + 1
    sessao["cliente_codigo"] = p
    if p["tentativas"] > CODIGO_TENTATIVAS:
        sessao.pop("cliente_codigo", None)
        return None, "Muitas tentativas. Peça um código novo."
    codigo = re.sub(r"\D", "", codigo or "")[:6]
    if not hmac.compare_digest(hash_codigo(codigo, p["fone"]), p["hash"]):
        return None, "Código errado. Confira no WhatsApp."
    sessao.pop("cliente_codigo", None)
    return p["fone"], None


# --------------------------------------------------------------------------- #
# Contas
# --------------------------------------------------------------------------- #
def conta_do_cliente(cur, cliente_id: str) -> str:
    cur.execute("SELECT id::text FROM contas_clientes WHERE cliente_id = %s", (cliente_id,))
    linha = cur.fetchone()
    if linha:
        return linha["id"]
    cur.execute("INSERT INTO contas_clientes (loja_id, cliente_id) VALUES (ctx_loja(), %s) RETURNING id::text", (cliente_id,))
    return cur.fetchone()["id"]


def cliente_pelo_telefone(cur, fone: str) -> dict | None:
    return pessoas.achar_por_telefone(cur, fone)


def criar_cliente(cur, nome: str, fone: str, email: str | None) -> str:
    return pessoas.gravar_cliente(cur, {"nome": nome, "telefone": fone, "email": email}, consentimento=True)


def definir_email_senha(cur, conta_id: str, cliente_id: str, email: str | None, senha: str | None) -> None:
    if email:
        cur.execute("UPDATE clientes SET email_c = %s, atualizado_em = now() WHERE id = %s",
                    (cripto.cifrar(email, "clientes.email"), cliente_id))
        cur.execute("UPDATE contas_clientes SET email_idx = %s WHERE id = %s", (indice_email(email), conta_id))
    if senha:
        cur.execute("UPDATE contas_clientes SET senha_hash = %s, falhas = 0, travada_ate = NULL WHERE id = %s",
                    (seguranca.gerar_hash(senha), conta_id))


def entrar_com_senha(cur, email: str, senha: str) -> tuple[str | None, str | None]:
    """(conta_id, erro). Mensagem igual para e-mail inexistente e senha errada."""
    cur.execute("""SELECT id::text, senha_hash, ativa, (travada_ate IS NOT NULL AND travada_ate > now()) AS travada
                     FROM contas_clientes WHERE email_idx = %s FOR UPDATE""", (indice_email(email) or "-",))
    c = cur.fetchone()
    ok = seguranca.verificar(c["senha_hash"] if c else None, senha)
    if c and c["travada"]:
        return None, "Muitas tentativas erradas. Espere 15 minutos ou entre com o código no WhatsApp."
    if not c or not ok or not c["ativa"]:
        if c:
            cur.execute("""UPDATE contas_clientes SET falhas = falhas + 1,
                                  travada_ate = CASE WHEN falhas + 1 >= %s THEN now() + interval '15 minutes' END
                            WHERE id = %s""", (FALHAS_TRAVA, c["id"]))
        return None, "E-mail ou senha não conferem."
    cur.execute("UPDATE contas_clientes SET falhas = 0, travada_ate = NULL WHERE id = %s", (c["id"],))
    return c["id"], None


# --------------------------------------------------------------------------- #
# Sessão da cliente
# --------------------------------------------------------------------------- #
def abrir_sessao(cur, sessao: dict, conta_id: str, ua: str | None) -> None:
    antiga = sessao.get(CHAVE_SESSAO) or {}
    if antiga.get("sid"):
        encerrar(cur, sessao, "TROCA")
    sid = secrets.token_urlsafe(32)
    cur.execute("""INSERT INTO sessoes_clientes (id, loja_id, conta_id, navegador, expira_em)
                   VALUES (%s, ctx_loja(), %s, %s, now() + make_interval(days => %s))""",
                (_h(sid), conta_id, _h("ua:" + (ua or "")[:400]), DIAS_SESSAO))
    cur.execute("UPDATE contas_clientes SET ultimo_acesso = now() WHERE id = %s", (conta_id,))
    sessao[CHAVE_SESSAO] = {"conta": conta_id, "sid": sid}


def conta_atual(cur, sessao: dict, ua: str | None) -> dict | None:
    s = sessao.get(CHAVE_SESSAO)
    if not isinstance(s, dict) or not s.get("sid") or len(str(s["sid"])) > 100:
        return None
    cur.execute("""SELECT sc.conta_id::text AS conta, sc.navegador, sc.encerrada_em IS NOT NULL AS encerrada,
                          sc.expira_em < now() AS vencida, c.ativa, c.cliente_id::text AS cliente_id, cl.nome
                     FROM sessoes_clientes sc JOIN contas_clientes c ON c.id = sc.conta_id
                     JOIN clientes cl ON cl.id = c.cliente_id WHERE sc.id = %s""", (_h(s["sid"]),))
    linha = cur.fetchone()
    if (not linha or linha["encerrada"] or linha["vencida"] or not linha["ativa"] or linha["conta"] != s.get("conta")
            or linha["navegador"] != _h("ua:" + (ua or "")[:400])):
        if linha and not linha["encerrada"]:
            cur.execute("UPDATE sessoes_clientes SET encerrada_em = now(), motivo = 'INVALIDA' WHERE id = %s", (_h(s["sid"]),))
        sessao.pop(CHAVE_SESSAO, None)
        return None
    cur.execute("""UPDATE sessoes_clientes SET visto_em = now(), expira_em = now() + make_interval(days => %s)
                    WHERE id = %s AND visto_em < now() - interval '5 minutes'""", (DIAS_SESSAO, _h(s["sid"])))
    return {"id": linha["conta"], "cliente_id": linha["cliente_id"], "nome": linha["nome"]}


def encerrar(cur, sessao: dict, motivo: str = "SAIU") -> None:
    s = sessao.pop(CHAVE_SESSAO, None) or {}
    if s.get("sid") and len(str(s["sid"])) <= 100:
        cur.execute("UPDATE sessoes_clientes SET encerrada_em = now(), motivo = %s WHERE id = %s AND encerrada_em IS NULL",
                    (motivo, _h(s["sid"])))


def encerrar_todas(cur, conta_id: str, motivo: str) -> None:
    cur.execute("UPDATE sessoes_clientes SET encerrada_em = now(), motivo = %s WHERE conta_id = %s AND encerrada_em IS NULL",
                (motivo, conta_id))
