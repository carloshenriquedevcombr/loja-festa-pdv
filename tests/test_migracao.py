#!/usr/bin/env python3
"""Atualização de um banco QUE JÁ TEM DADOS (como o do PC da loja e o da VPS).

Os outros testes começam de um banco vazio; aqui o banco para numa versão antiga, recebe dados reais
(loja, regras de pagamento, equipe, cliente com telefone em texto, medidas, peça e pedido) e só então as
migrações novas são aplicadas, uma por uma. Pega erros como "registro imutável" que só aparecem com dados.

Uso:  PGHOST=... PGUSER=... python3 tests/test_migracao.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BANCO = os.environ.get("TEST_DB_MIGRACAO", "loja_migracao_teste")
assert BANCO.endswith("_teste")
PARA_ANTES = "0006"          # versão da loja antes das atualizações de segurança (Sprint 3)
ok = falhas = 0


def rodar(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def checar(nome, passou, detalhe=""):
    global ok, falhas
    ok += bool(passou)
    falhas += not passou
    print(("PASSOU " if passou else "FALHOU ") + nome + ("" if passou else f"\n   -> {str(detalhe)[-800:]}"))


def sql(consulta, banco=BANCO):
    r = rodar(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-d", banco, "-c", consulta])
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def migrar(pasta: str):
    return rodar(["bash", str(RAIZ / "scripts/migrate.sh")],
                 env={**os.environ, "PGDATABASE": BANCO, "MIGRATIONS_DIR": pasta})


for c in (f'DROP DATABASE IF EXISTS "{BANCO}" WITH (FORCE)', f'CREATE DATABASE "{BANCO}"',
          f'GRANT CONNECT ON DATABASE "{BANCO}" TO app_user'):
    sql(c, "postgres")

todas = sorted((RAIZ / "migrations").glob("[0-9]*.sql"))
antigas = [m for m in todas if m.name[:4] <= PARA_ANTES]
novas = [m for m in todas if m.name[:4] > PARA_ANTES]
with tempfile.TemporaryDirectory() as tmp:
    for m in antigas:
        shutil.copy(m, tmp)
    r = migrar(tmp)
    checar(f"Banco na versão antiga ({PARA_ANTES})", r.returncode == 0, r.stderr)

# Dados como os de uma loja em uso na versão antiga
L, D, U2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"
sql(f"""
BEGIN;
INSERT INTO lojas (id, nome, endereco, telefone, whatsapp, instagram, horario)
     VALUES ('{L}', 'Loja Antiga', 'Rua Velha, 10 - Centro', '(19) 3333-0000', '19999990000', 'lojaantiga', 'Seg a sex 9h às 18h');
INSERT INTO usuarios (id, loja_id, nome, login, senha_hash, telefone, observacoes)
     VALUES ('{D}', '{L}', 'Dona Antiga', 'dona', '$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo', '19988887777', 'fundadora'),
            ('{U2}', '{L}', 'Ana Antiga', 'ana', '$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo', NULL, NULL);
INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por)
     SELECT '{L}', '{D}', id, '{D}' FROM papeis WHERE codigo = 'DONO';
INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por)
     SELECT '{L}', '{U2}', id, '{D}' FROM papeis WHERE codigo = 'ATENDENTE';
SELECT set_config('app.usuario_id', '{D}', true);
INSERT INTO config_pagamento (loja_id, entrada_minima_pct, definido_por) VALUES ('{L}', 30, '{D}');
INSERT INTO config_pagamento (loja_id, entrada_minima_pct, definido_por) VALUES ('{L}', 40, '{D}');
INSERT INTO clientes (loja_id, nome, telefone, cpf, email, observacoes, consentimento_lgpd_em)
     VALUES ('{L}', 'Cliente Antiga', '19977776666', '52998224725', 'antiga@exemplo.com', 'gosta de azul', now());
INSERT INTO medidas (loja_id, cliente_id, ficha, valores, observacoes, tomada_por)
     SELECT '{L}', id, 'FEMININO', '{{"busto": 90}}', 'barra 2 cm', '{D}' FROM clientes WHERE nome = 'Cliente Antiga';
INSERT INTO solicitacoes_prova (loja_id, nome, telefone, data_desejada, periodo, mensagem, consentimento_em, origem_hash)
     VALUES ('{L}', 'Visitante Antiga', '19966665555', current_date + 3, 'TARDE', 'quero provar', now(), 'abc');
COMMIT;""")
checar("Dados da versão antiga gravados", sql("SELECT count(*) FROM config_pagamento") == "2"
       and sql("SELECT count(*) FROM medidas") == "1")

# Aplica as novas, uma por vez (cada uma numa transação, como na loja)
pasta = tempfile.mkdtemp()
for m in antigas:
    shutil.copy(m, pasta)
for m in novas:
    shutil.copy(m, pasta)
    r = migrar(pasta)
    checar(f"Atualização {m.stem} aplica sobre banco com dados", r.returncode == 0, r.stderr + r.stdout)
    if r.returncode:
        break
shutil.rmtree(pasta, ignore_errors=True)

if not falhas:
    checar("Regras antigas viram entrada e multa em % (sem mudar o valor)",
           sql("""SELECT string_agg(entrada_aluguel_tipo || entrada_aluguel_valor::int || multa_aluguel_valor::int, ','
                    ORDER BY id) FROM config_pagamento""") == "PCT3030,PCT4040")
    checar("Regras de pagamento continuam imutáveis depois da atualização",
           "imutável" in rodar(["psql", "-X", "-d", BANCO, "-c", "UPDATE config_pagamento SET dias_limpeza = 9"]).stderr.lower())
    checar("Endereço antigo virou a loja principal do rodapé",
           sql("SELECT nome || '|' || endereco || '|' || principal FROM unidades") == "Loja Antiga|Rua Velha, 10 - Centro|true")
    # O app sobe (cifra os dados que estavam em texto) e tudo continua legível
    env = {**os.environ, "DATABASE_URL": f"postgresql://app_user@/{BANCO}?host={os.environ.get('PGHOST', '/var/run/postgresql')}"
           f"&port={os.environ.get('PGPORT', '5432')}" if os.environ.get("PGHOST", "").startswith("/") else
           f"postgresql://app_user:{os.environ.get('APP_DB_PASSWORD', '')}@{os.environ.get('PGHOST', 'localhost')}:"
           f"{os.environ.get('PGPORT', '5432')}/{BANCO}",
           "LOJA_ID": L, "SESSION_SECRET": "x" * 48, "DADOS_CHAVE": os.environ.get("DADOS_CHAVE", "ab" * 32),
           "PYTHONPATH": str(RAIZ / "app"), "FOTOS_DIR": tempfile.mkdtemp()}
    r = rodar([sys.executable, "-c", """
from app import db, pessoas, cripto
db.pool.open(wait=True)
with db.transacao() as cur:
    n = pessoas.cifrar_pendentes(cur)
    cur.execute("SELECT cpf_c, telefone_idx FROM clientes")
    c = cur.fetchone()
    print(n, cripto.decifrar(c["cpf_c"], "clientes.cpf"), c["telefone_idx"] == cripto.indice_telefone("19977776666"))
"""], env=env, cwd=str(RAIZ / "app"))
    checar("Ao subir o sistema novo, dados antigos são cifrados e continuam legíveis",
           r.returncode == 0 and r.stdout.strip() == "4 52998224725 True", r.stderr[-800:] + r.stdout)
    checar("Nenhum dado pessoal ficou em texto",
           sql("SELECT count(*) FROM clientes WHERE telefone IS NOT NULL OR cpf IS NOT NULL OR email IS NOT NULL") == "0"
           and sql("SELECT count(*) FROM usuarios WHERE telefone IS NOT NULL") == "0"
           and sql("SELECT count(*) FROM medidas WHERE valores_c IS NULL") == "0")

print(f"\nResultado: {ok} passaram, {falhas} falharam.")
if os.environ.get("MANTER_BANCO") != "1":
    rodar(["psql", "-X", "-q", "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{BANCO}" WITH (FORCE)'])
sys.exit(1 if falhas else 0)
