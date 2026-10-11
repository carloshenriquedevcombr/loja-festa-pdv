"""Dados pessoais de clientes e funcionários: gravar cifrado, ler aberto, achar pelo telefone."""
from . import cripto

CAMPOS_CLIENTE = ("telefone", "cpf", "email", "observacoes", "endereco")


def colunas_cliente(d: dict) -> dict:
    """{'telefone': '1999...', 'cpf': ...} -> colunas cifradas + índices, prontas para INSERT/UPDATE."""
    fone = cripto.so_digitos(d.get("telefone")) or None
    cpf = cripto.so_digitos(d.get("cpf")) or None
    return {
        "telefone_c": cripto.cifrar(fone, "clientes.telefone"),
        "telefone_idx": cripto.indice_telefone(fone),
        "telefone_fim": fone[-4:] if fone and len(fone) >= 4 else None,
        "cpf_c": cripto.cifrar(cpf, "clientes.cpf"),
        "cpf_idx": cripto.indice(cpf, "cpf"),
        "email_c": cripto.cifrar(d.get("email"), "clientes.email"),
        "observacoes_c": cripto.cifrar(d.get("observacoes"), "clientes.observacoes"),
        "endereco_c": cripto.cifrar(d.get("endereco"), "clientes.endereco"),
    }


def gravar_cliente(cur, d: dict, cid: str | None = None, consentimento: bool = True, consentimento_em=None) -> str:
    """Cria (cid=None) ou atualiza a ficha. Nome em texto (para a busca); o resto cifrado."""
    col = colunas_cliente(d)
    if cid is None:
        cur.execute(f"""INSERT INTO clientes (loja_id, nome, {', '.join(col)}, consentimento_lgpd_em)
                        VALUES (ctx_loja(), %s, {', '.join(['%s'] * len(col))},
                                CASE WHEN %s THEN COALESCE(%s, now()) END)
                        RETURNING id::text""",
                    (d["nome"], *col.values(), consentimento, consentimento_em))
        return cur.fetchone()["id"]
    cur.execute(f"""UPDATE clientes SET nome = %s, {', '.join(f'{k} = %s' for k in col)},
                           consentimento_lgpd_em = COALESCE(consentimento_lgpd_em, CASE WHEN %s THEN now() END),
                           atualizado_em = now()
                     WHERE id = %s""", (d["nome"], *col.values(), consentimento, cid))
    return cid


def achar_por_telefone(cur, fone: str | None) -> dict | None:
    idx = cripto.indice_telefone(fone)
    if not idx:
        return None
    cur.execute("SELECT id::text, nome FROM clientes WHERE telefone_idx = %s ORDER BY criado_em LIMIT 1", (idx,))
    return cur.fetchone()


def abrir_cliente(linha: dict | None) -> dict | None:
    return cripto.abrir(linha, "clientes", *CAMPOS_CLIENTE)


def abrir_usuario(linha: dict | None) -> dict | None:
    return cripto.abrir(linha, "usuarios", "telefone", "observacoes")


def telefone_usuario(cur, usuario_id: str) -> str | None:
    cur.execute("SELECT telefone_c FROM usuarios WHERE id = %s", (usuario_id,))
    linha = cur.fetchone()
    return cripto.decifrar(linha["telefone_c"], "usuarios.telefone") if linha else None


# --------------------------------------------------------------------------- #
# Cifra dos dados que ficaram em texto (primeira subida depois da atualização)
# --------------------------------------------------------------------------- #
def cifrar_pendentes(cur) -> int:
    n = 0
    cur.execute("""SELECT id, telefone, cpf, email, observacoes FROM clientes
                    WHERE telefone IS NOT NULL OR cpf IS NOT NULL OR email IS NOT NULL OR observacoes IS NOT NULL""")
    for c in cur.fetchall():
        col = colunas_cliente(c)
        col.pop("endereco_c")
        cur.execute(f"""UPDATE clientes SET {', '.join(f'{k} = %s' for k in col)},
                               telefone = NULL, cpf = NULL, email = NULL, observacoes = NULL WHERE id = %s""",
                    (*col.values(), c["id"]))
        n += 1
    cur.execute("SELECT id, telefone, observacoes FROM usuarios WHERE telefone IS NOT NULL OR observacoes IS NOT NULL")
    for u in cur.fetchall():
        cur.execute("""UPDATE usuarios SET telefone_c = %s, telefone_idx = %s, observacoes_c = %s,
                              telefone = NULL, observacoes = NULL WHERE id = %s""",
                    (cripto.cifrar(u["telefone"], "usuarios.telefone"), cripto.indice_telefone(u["telefone"]),
                     cripto.cifrar(u["observacoes"], "usuarios.observacoes"), u["id"]))
        n += 1
    cur.execute("SELECT id, telefone, mensagem FROM solicitacoes_prova WHERE telefone IS NOT NULL OR mensagem IS NOT NULL")
    for s in cur.fetchall():
        cur.execute("""UPDATE solicitacoes_prova SET telefone_c = %s, telefone_idx = %s, mensagem_c = %s,
                              telefone = NULL, mensagem = NULL WHERE id = %s""",
                    (cripto.cifrar(s["telefone"], "solicitacoes.telefone"), cripto.indice_telefone(s["telefone"]),
                     cripto.cifrar(s["mensagem"], "solicitacoes.mensagem"), s["id"]))
        n += 1
    cur.execute("""SELECT id, valores, observacoes, busto_cm, cintura_cm, quadril_cm, altura_cm, comprimento_cm
                     FROM medidas WHERE valores_c IS NULL""")
    for m in cur.fetchall():
        valores = dict(m["valores"] or {})
        for antigo, novo in (("busto_cm", "busto"), ("cintura_cm", "cintura"), ("quadril_cm", "quadril"),
                             ("altura_cm", "altura"), ("comprimento_cm", "comprimento_vestido")):
            if m[antigo] is not None and novo not in valores:
                valores[novo] = float(m[antigo])
        cur.execute("""UPDATE medidas SET valores_c = %s, observacoes_c = %s, valores = '{}', observacoes = NULL,
                              busto_cm = NULL, cintura_cm = NULL, quadril_cm = NULL, altura_cm = NULL,
                              comprimento_cm = NULL WHERE id = %s""",
                    (cripto.cifrar_json(valores, "medidas.valores"),
                     cripto.cifrar(m["observacoes"], "medidas.observacoes"), m["id"]))
        n += 1
    return n
