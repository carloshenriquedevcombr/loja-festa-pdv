"""Comandos de administração, rodados dentro do container do app.

  docker compose exec app python -m app.cli criar-loja --nome "Loja X" --login dona --nome-dona "Maria"
  docker compose exec app python -m app.cli criar-usuario --login ana --nome "Ana" \
        --papel ATENDENTE --concedido-por dona [--ate 2026-12-31]

A senha é pedida no terminal (não aparece na tela nem no histórico).
--senha-stdin lê a senha da entrada padrão (uso em testes automatizados).
Tudo passa pelas mesmas travas do banco que o app usa, e fica na auditoria.
"""
import argparse
import getpass
import json
import sys
import uuid

from . import db, seguranca

CATEGORIAS_INICIAIS = [("Vestido", "VES"), ("Terno", "TER"), ("Acessório", "ACE"), ("Sapato", "SAP")]


def _ler_senha(args) -> str:
    if args.senha_stdin:
        senha = sys.stdin.readline().rstrip("\r\n")
    else:
        senha = getpass.getpass("Senha: ")
        if getpass.getpass("Repita a senha: ") != senha:
            sys.exit("As senhas não conferem.")
    erro = seguranca.validar_senha_nova(senha)
    if erro:
        sys.exit(erro)
    return senha


def criar_loja(args) -> None:
    senha = _ler_senha(args)
    loja_id = str(uuid.uuid4())
    dona_id = str(uuid.uuid4())
    with db.transacao(loja_id=loja_id) as cur:
        cur.execute("INSERT INTO lojas (id, nome) VALUES (%s, %s)", (loja_id, args.nome))
        cur.execute(
            """INSERT INTO usuarios (id, loja_id, nome, login, senha_hash)
               VALUES (%s, %s, %s, %s, %s)""",
            (dona_id, loja_id, args.nome_dona, args.login.strip().lower(), seguranca.gerar_hash(senha)))
        cur.execute(
            """INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por)
               SELECT %s, %s, id, %s FROM papeis WHERE codigo = 'DONO'""",
            (loja_id, dona_id, dona_id))
        cur.execute("SELECT set_config('app.usuario_id', %s, true)", (dona_id,))
        cur.execute("INSERT INTO config_pagamento (loja_id, definido_por) VALUES (%s, %s)",
                    (loja_id, dona_id))
        cur.execute("SELECT semear_ocasioes(%s)", (loja_id,))
        for nome, prefixo in CATEGORIAS_INICIAIS:
            cur.execute("INSERT INTO categorias (loja_id, nome, prefixo_sku) VALUES (%s, %s, %s)",
                        (loja_id, nome, prefixo))
        cur.execute(
            """INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados)
               VALUES (%s, %s, 'loja_criada', 'loja', %s, %s::jsonb)""",
            (loja_id, dona_id, loja_id, json.dumps({"via": "cli"})))
    print("Loja criada.")
    print(f"Coloque no .env:  LOJA_ID={loja_id}")
    print("Depois: docker compose up -d app")


def criar_usuario(args) -> None:
    senha = _ler_senha(args)
    novo_id = str(uuid.uuid4())
    with db.transacao() as cur:
        cur.execute("SELECT id::text FROM usuarios WHERE login = %s AND ativo", (args.concedido_por.lower(),))
        quem = cur.fetchone()
        if not quem:
            sys.exit(f"Usuário '{args.concedido_por}' não encontrado ou inativo.")
        cur.execute("SELECT set_config('app.usuario_id', %s, true)", (quem["id"],))
        cur.execute(
            """INSERT INTO usuarios (id, loja_id, nome, login, senha_hash, vinculo)
               VALUES (%s, ctx_loja(), %s, %s, %s, %s)""",
            (novo_id, args.nome, args.login.strip().lower(), seguranca.gerar_hash(senha),
             "FREELANCE" if args.ate else "FIXO"))
        for papel in args.papel:
            cur.execute(
                """INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por, valido_ate)
                   SELECT ctx_loja(), %s, id, %s, %s::date + 1 FROM papeis WHERE codigo = %s
                   RETURNING papel_id""",
                (novo_id, quem["id"], args.ate, papel.upper()))
            if not cur.fetchone():
                sys.exit(f"Papel desconhecido: {papel}")
        cur.execute(
            """INSERT INTO auditoria (loja_id, usuario_id, acao, entidade, entidade_id, dados)
               VALUES (ctx_loja(), %s, 'usuario_criado', 'usuario', %s, %s::jsonb)""",
            (quem["id"], novo_id, json.dumps({"papeis": args.papel, "ate": args.ate, "via": "cli"})))
    print(f"Usuário '{args.login}' criado com {', '.join(args.papel)}.")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="comando", required=True)

    a = sub.add_parser("criar-loja", help="Cria a loja, a Dona e as regras iniciais")
    a.add_argument("--nome", required=True, help="Nome da loja")
    a.add_argument("--login", required=True, help="Login da Dona")
    a.add_argument("--nome-dona", required=True)
    a.add_argument("--senha-stdin", action="store_true")
    a.set_defaults(func=criar_loja)

    b = sub.add_parser("criar-usuario", help="Cria um funcionário")
    b.add_argument("--login", required=True)
    b.add_argument("--nome", required=True)
    b.add_argument("--papel", required=True, action="append",
                   help="ATENDENTE, COSTUREIRA, AJUDANTE_COSTURA, GERENTE, LIMPEZA, "
                        "DONO, RH, FINANCEIRO, DESENVOLVEDOR, MASTER (acesso total) (pode repetir)")
    b.add_argument("--concedido-por", required=True, help="Login de quem autoriza (Dona, RH...)")
    b.add_argument("--ate", help="Último dia de acesso (AAAA-MM-DD). Obrigatório para freelance.")
    b.add_argument("--senha-stdin", action="store_true")
    b.set_defaults(func=criar_usuario)

    args = p.parse_args()
    try:
        db.pool.open(wait=True, timeout=10)
    except Exception:
        sys.exit("Não consegui conectar ao banco com a senha do app. Confira APP_DB_PASSWORD no .env "
                 "(ela precisa ser a mesma de quando o banco foi criado).")
    try:
        args.func(args)
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
