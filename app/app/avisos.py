"""Avisos por WhatsApp e e-mail: tudo passa por uma fila (tabela mensagens) e o trabalhador envia.

* A tela nunca espera o WhatsApp: grava na fila e segue. Se o WhatsApp cair, nada se perde.
* "chave" evita aviso repetido (ex.: o lembrete da prova de amanhã só sai uma vez).
* Destino, texto e anexo ficam cifrados na fila.
"""
from datetime import datetime

from . import config, cripto, pessoas
from .web import brl, data_br

ETIQUETAS = ("pedidos", "site", "chat", "pagamentos", "agenda", "costura", "limpeza", "relatorio", "seguranca")


def whatsapp_ativo(cur) -> bool:
    cur.execute("SELECT 1 FROM whatsapp_config WHERE ativo")
    return cur.fetchone() is not None


def email_ativo(cur) -> bool:
    cur.execute("SELECT 1 FROM email_config WHERE ativo")
    return cur.fetchone() is not None


def enfileirar(cur, canal: str, destino: str, texto: str, motivo: str, chave: str | None = None,
               enviar_apos: datetime | None = None, assunto: str | None = None,
               anexo: bytes | None = None, anexo_nome: str | None = None) -> bool:
    if not destino or not texto:
        return False
    cur.execute("""INSERT INTO mensagens (loja_id, canal, destino_c, texto_c, assunto, anexo_nome, anexo_c, motivo,
                                          chave, enviar_apos)
                   VALUES (ctx_loja(), %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
                   ON CONFLICT (loja_id, chave) DO NOTHING""",
                (canal, cripto.cifrar(destino, "mensagens.destino"), cripto.cifrar(texto, "mensagens.texto"),
                 assunto, anexo_nome, cripto.cifrar(anexo, "mensagens.anexo") if anexo else None, motivo, chave,
                 enviar_apos))
    return cur.rowcount == 1


def whatsapp(cur, fone: str | None, texto: str, motivo: str, chave: str | None = None, **kw) -> bool:
    from .whatsapp import numero_whatsapp
    numero = numero_whatsapp(fone)
    if not numero or not whatsapp_ativo(cur):
        return False
    return enfileirar(cur, "WHATSAPP", numero, texto, motivo, chave, **kw)


def para_cliente(cur, cliente_id: str | None, texto: str, motivo: str, chave: str | None = None, **kw) -> bool:
    if not cliente_id:
        return False
    cur.execute("SELECT nome, telefone_c, whatsapp_sair FROM clientes WHERE id = %s", (cliente_id,))
    c = pessoas.abrir_cliente(cur.fetchone())
    if not c or c["whatsapp_sair"]:
        return False
    return whatsapp(cur, c["telefone"], texto, motivo, chave, **kw)


def email_cliente(cur, cliente_id: str, assunto: str, texto: str, motivo: str, chave: str | None = None,
                  anexo: bytes | None = None, anexo_nome: str | None = None) -> bool:
    if not email_ativo(cur):
        return False
    cur.execute("SELECT email_c FROM clientes WHERE id = %s", (cliente_id,))
    linha = cur.fetchone()
    email = cripto.decifrar(linha["email_c"], "clientes.email") if linha else None
    if not email:
        return False
    return enfileirar(cur, "EMAIL", email, texto, motivo, chave, assunto=assunto, anexo=anexo, anexo_nome=anexo_nome)


def para_equipe(cur, etiqueta: str, texto: str, motivo: str, chave: str | None = None) -> int:
    """Manda para todo funcionário ativo que marcou essa etiqueta de aviso."""
    if not whatsapp_ativo(cur):
        return 0
    cur.execute("""SELECT id::text, telefone_c FROM usuarios
                    WHERE ativo AND %s = ANY(avisos) AND telefone_c IS NOT NULL""", (etiqueta,))
    n = 0
    for u in cur.fetchall():
        fone = cripto.decifrar(u["telefone_c"], "usuarios.telefone")
        n += whatsapp(cur, fone, texto, motivo, f"{chave}:{u['id']}" if chave else None)
    return n


def link(caminho: str, base: str = "") -> str:
    return (config.SITE_URL or base).rstrip("/") + caminho


def primeiro_nome(nome: str | None) -> str:
    return (nome or "").split()[0] if nome else ""


# --------------------------------------------------------------------------- #
# Textos (sempre curtos, com o nome da loja e o que fazer)
# --------------------------------------------------------------------------- #
def texto_pedido_cliente(pe: dict, loja: str, url: str) -> str:
    tipo = "Aluguel" if pe["tipo"] == "ALUGUEL" else ("Venda com ajuste" if pe["com_ajuste"] else "Compra")
    feito = "foi registrado" if pe.get("status") == "AGUARDANDO_PAGAMENTO" else "está confirmado ✅"
    linhas = [f"Olá, {primeiro_nome(pe['cliente'])}! Seu pedido na {loja} {feito}",
              f"{tipo}: {pe['sku']} · {pe['peca']}"]
    if pe.get("data_retirada"):
        linhas.append(f"Retirada: {data_br(pe['data_retirada'])}"
                      + (f" · Devolução: {data_br(pe['data_devolucao'])}" if pe.get("data_devolucao") else ""))
    linhas.append(f"Total {brl(pe['valor_total_centavos'])} · pago {brl(pe['pago'])}"
                  + (f" · falta {brl(pe['saldo'])}" if pe["saldo"] > 0 else " · quitado"))
    linhas.append(f"Seu pedido e o contrato: {url}")
    linhas.append("Para não receber mais mensagens, responda SAIR.")
    return "\n".join(linhas)
