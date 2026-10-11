#!/usr/bin/env python3
"""Testes das travas do banco: cria um banco descartável, aplica as migrações
e verifica cada regra que o PostgreSQL deve impor sozinho.

Conexão pelas variáveis padrão do psql (PGHOST, PGPORT, PGUSER, PGPASSWORD).
O usuário precisa ser superusuário (prepara os dados e assume o papel app_user).
NUNCA aponte para o banco de produção: o script cria e apaga o banco TEST_DB.

Uso:  PGHOST=localhost PGUSER=postgres PGPASSWORD=... python3 tests/test_banco.py
"""
import os, subprocess, sys, uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# O servidor responde no idioma dele (pt_BR na imagem do banco); as checagens comparam a mensagem em inglês
os.environ["PGOPTIONS"] = os.environ.get("PGOPTIONS", "-c client_min_messages=warning") + " -c lc_messages=C"
TEST_DB = os.environ.get("TEST_DB", "loja_teste")
if not TEST_DB.endswith("_teste"):
    sys.exit("TEST_DB precisa terminar em _teste (proteção contra apagar produção).")

L = "11111111-1111-1111-1111-111111111111"
L2 = "22222222-2222-2222-2222-222222222222"
DONA, GER, ATD, ATD2, FREE, DEV = [str(uuid.UUID(int=i)) for i in (101, 102, 103, 104, 105, 106)]
HASH = "$argon2id$v=19$teste"

def rodar(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)

def sair(msg):
    if os.environ.get("GITHUB_ACTIONS"):                         # aparece no resumo do CI
        print("::error title=Preparação do banco::" + str(msg)[-600:].replace("\n", " "))
    sys.exit(msg)

def preparar_banco():
    for sql in (f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)', f'CREATE DATABASE "{TEST_DB}"'):
        r = rodar(["psql", "-X", "-q", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql])
        if r.returncode:
            sair(r.stderr)
    env = {**os.environ, "PGDATABASE": TEST_DB, "MIGRATIONS_DIR": str(RAIZ / "migrations")}
    r = rodar(["bash", str(RAIZ / "scripts" / "migrate.sh")], env=env)
    print(r.stdout.strip())
    if r.returncode:
        sair("Migração falhou:\n" + r.stderr)

def psql(sql, superuser=False):
    pre = "" if superuser else f"SET ROLE app_user; SET app.loja_id = '{L}';\n"
    r = rodar(["psql", "-X", "-d", TEST_DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=pre + sql)
    return r.returncode, (r.stdout + r.stderr).strip()

def checar(nome, passou, detalhe=""):
    global ok, falhas
    ok += bool(passou); falhas += not passou
    print(("PASSOU " if passou else "FALHOU ") + nome + ("" if passou else f"\n   -> {detalhe[-400:]}"))
    if not passou and os.environ.get("GITHUB_ACTIONS"):          # aparece no resumo do CI
        print(f"::error title=Teste do banco::{nome}: {detalhe[-300:]}".replace("\n", " "))

preparar_banco()
ok = falhas = 0
def teste(nome, sql, espera_erro=None, espera_saida=None, usuario=ATD):
    code, out = psql(f"BEGIN; SET LOCAL app.usuario_id = '{usuario}';\n{sql}\nCOMMIT;")
    if espera_erro:
        passou = code != 0 and espera_erro in out
    else:
        passou = code == 0 and (espera_saida is None or espera_saida in out)
    checar(nome, passou, out)

# ---------- Preparação (superusuário, como a migração/onboarding faria) ----------
code, out = psql(f"""
BEGIN;
INSERT INTO lojas (id, nome, reserva_modo) VALUES ('{L}', 'Loja Teste', 'DATAS'), ('{L2}', 'Outra Loja', 'DATAS');
INSERT INTO usuarios (id, loja_id, nome, login, senha_hash, vinculo) VALUES
 ('{DONA}','{L}','Dona','dona','{HASH}','FIXO'),
 ('{GER}','{L}','Gerente','ger','{HASH}','FIXO'),
 ('{ATD}','{L}','Atendente','atd','{HASH}','FIXO'),
 ('{ATD2}','{L}','Atendente 2','atd2','{HASH}','FIXO'),
 ('{FREE}','{L}','Freela','free','{HASH}','FREELANCE'),
 ('{DEV}','{L}','Dev','dev','{HASH}','FIXO');
INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por)
 SELECT '{L}', '{DONA}', id, '{DONA}' FROM papeis WHERE codigo='DONO';
INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por, valido_ate)
 SELECT '{L}', u, p.id, '{DONA}', v FROM (VALUES
   ('{GER}'::uuid,'GERENTE',NULL::timestamptz), ('{ATD}','ATENDENTE',NULL), ('{ATD2}','ATENDENTE',NULL),
   ('{FREE}','ATENDENTE', now()+interval '10 days'), ('{DEV}','DESENVOLVEDOR',NULL)) x(u,c,v)
 JOIN papeis p ON p.codigo = x.c;
INSERT INTO config_pagamento (loja_id, definido_por) VALUES ('{L}', '{DONA}');
INSERT INTO categorias (loja_id, nome, prefixo_sku) VALUES ('{L}','Vestido','VES'), ('{L}','Terno','TER');
COMMIT;
""", superuser=True)
checar("Preparação dos dados de teste", code == 0, out)

CFG = "(SELECT config_vigente(ctx_loja()))"
VES = "(SELECT id FROM categorias WHERE prefixo_sku='VES')"
MAQ = "(SELECT id FROM maquininhas LIMIT 1)"

def nova_peca(nome, alugar=True, vender=True):
    code, out = psql(f"""BEGIN; SET LOCAL app.usuario_id='{ATD}';
      INSERT INTO produtos (loja_id, categoria_id, nome, pode_alugar, pode_vender,
                            preco_aluguel_centavos, preco_venda_centavos)
      VALUES (ctx_loja(), {VES}, '{nome}', {alugar}, {vender},
              {'120000' if alugar else 'NULL'}, {'89000' if vender else 'NULL'})
      RETURNING id || '|' || sku; COMMIT;""")
    return out.split("|") if code == 0 else (None, out)

print("\n== Catálogo e SKU ==")
p1, sku1 = nova_peca("Vestido azul longo")
p2, sku2 = nova_peca("Vestido vermelho")
checar("SKUs sequenciais VES-0001 e VES-0002", (sku1, sku2) == ("VES-0001", "VES-0002"), f"{sku1} {sku2}")
teste("Cadastro que falha não deixa buraco no SKU",
      f"INSERT INTO produtos (loja_id, categoria_id, nome, pode_alugar, pode_vender) VALUES (ctx_loja(), {VES}, 'x', true, false);",
      espera_erro="preco_aluguel_coerente")
p3, sku3 = nova_peca("Vestido verde", alugar=True, vender=False)
checar("Próximo SKU continua VES-0003", sku3 == "VES-0003", str(sku3))
teste("Peça sem modalidade é recusada",
      f"INSERT INTO produtos (loja_id, categoria_id, nome) VALUES (ctx_loja(), {VES}, 'x');",
      espera_erro="modalidade_definida")
teste("SKU nunca muda", f"UPDATE produtos SET sku='XXX-1' WHERE id='{p1}';", espera_erro="nunca muda")
teste("App não consegue apagar peça", f"DELETE FROM produtos WHERE id='{p1}';", espera_erro="permission denied")
teste("Busca sem acento e com erro de digitação",
      "SELECT sku FROM produtos WHERE f_unaccent(lower(nome)) % 'vestdo azul' ORDER BY 1;", espera_saida="VES-0001")

print("\n== Pedidos e entrada ==")
cli = psql(f"BEGIN; SET LOCAL app.usuario_id='{ATD}'; INSERT INTO clientes (loja_id,nome) VALUES (ctx_loja(),'Maria') RETURNING id; COMMIT;")[1]

def aluguel(prod, ini, ret, dev, entrada=None, chave=None, extra=""):
    chave = chave or str(uuid.uuid4())
    pid = str(uuid.uuid4())
    sql = f"""INSERT INTO pedidos (id, loja_id, tipo, com_ajuste, produto_id, cliente_id, config_id,
              status, valor_peca_centavos, valor_total_centavos, entrada_minima_centavos,
              max_parcelas_permitidas, inicio_bloqueio, data_retirada, data_devolucao,
              chave_idempotencia, criado_por {extra and ', ' + extra.split('=')[0]})
            VALUES ('{pid}', ctx_loja(), 'ALUGUEL', true, '{prod}', '{cli}', {CFG}, 'X', 120000, 0, 0, 0,
              '{ini}', '{ret}', '{dev}', '{chave}', '{ATD}' {extra and ', ' + extra.split('=')[1]});"""
    if entrada:
        sql += f"""INSERT INTO transacoes (loja_id, tipo, natureza, valor_centavos, forma_pagamento,
                   pedido_id, descricao, chave_idempotencia, usuario_id)
                   VALUES (ctx_loja(),'ENTRADA','ENTRADA_RESERVA',{entrada},'PIX','{pid}','Sinal',gen_random_uuid(),'{ATD}');"""
    return pid, sql

_, sql = aluguel(p1, "2026-10-01", "2026-10-10", "2026-10-12")
teste("Reserva sem entrada é recusada no commit", sql, espera_erro="Reserva sem entrada")
_, sql = aluguel(p1, "2026-10-01", "2026-10-10", "2026-10-12", entrada=29999)
teste("Entrada abaixo de 25% é recusada", sql, espera_erro="mínimo de R$ 300,00")
chave = str(uuid.uuid4())
PED, sql = aluguel(p1, "2026-10-01", "2026-10-10", "2026-10-12", entrada=30000, chave=chave)
teste("Reserva com 25% de entrada é aceita", sql + f"SELECT status||' '||valor_total_centavos||' '||entrada_minima_centavos FROM pedidos WHERE id='{PED}';",
      espera_saida="RESERVADO 120000 30000")
_, sql = aluguel(p1, "2026-11-01", "2026-11-05", "2026-11-06", entrada=30000, chave=chave)
teste("Mesma chave de idempotência não cria pedido duplicado", sql, espera_erro="duplicate key")
_, sql = aluguel(p1, "2026-10-12", "2026-10-13", "2026-10-14", entrada=30000)
teste("Mesmo vestido em datas sobrepostas é recusado (inclui dias de limpeza)", sql, espera_erro="sem_conflito_de_datas")
_, sql = aluguel(p1, "2026-10-16", "2026-10-20", "2026-10-22", entrada=30000)
teste("Mesmo vestido depois da limpeza é aceito", sql)
teste("Venda de peça com aluguel agendado é recusada",
      f"""INSERT INTO pedidos (loja_id,tipo,produto_id,config_id,status,valor_peca_centavos,valor_total_centavos,
          entrada_minima_centavos,max_parcelas_permitidas,chave_idempotencia,criado_por)
          VALUES (ctx_loja(),'VENDA','{p1}',{CFG},'X',89000,0,0,0,gen_random_uuid(),'{ATD}');""",
      espera_erro="aluguel em andamento")
teste("Venda de peça só de aluguel é recusada",
      f"""INSERT INTO pedidos (loja_id,tipo,produto_id,config_id,status,valor_peca_centavos,valor_total_centavos,
          entrada_minima_centavos,max_parcelas_permitidas,chave_idempotencia,criado_por)
          VALUES (ctx_loja(),'VENDA','{p3}',{CFG},'X',89000,0,0,0,gen_random_uuid(),'{ATD}');""",
      espera_erro="só para aluguel")

print("\n== Pagamentos ==")
def pag(valor, forma="PIX", parcelas=1, extra_cols="", extra_vals="", usuario=ATD, pedido=PED):
    return f"""INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,parcelas,
               pedido_id,descricao,chave_idempotencia,usuario_id{extra_cols})
               VALUES (ctx_loja(),'ENTRADA','SALDO',{valor},'{forma}',{parcelas},'{pedido}','Saldo',
               gen_random_uuid(),'{usuario}'{extra_vals});"""
teste("Parcelado no PIX é recusado", pag(30000, "PIX", 3), espera_erro="parcelado_so_no_credito")
teste("Crédito sem maquininha é recusado", pag(10000, "CREDITO", 1, ",modalidade_cartao", ",'LOJISTA'"),
      espera_erro="cartao_exige_maquininha")
teste("Parcelas acima do limite (6x) são recusadas",
      pag(90000, "CREDITO", 7, ",modalidade_cartao,maquininha_id", f",'LOJISTA',{MAQ}"), espera_erro="até 6x")
teste("Parcela abaixo do mínimo é recusada",
      pag(10000, "CREDITO", 6, ",modalidade_cartao,maquininha_id", f",'LOJISTA',{MAQ}"), espera_erro="Parcela abaixo")
teste("Pagamento maior que o saldo é recusado", pag(95000), espera_erro="maior que o saldo")
teste("Freelance não opera o caixa", pag(1000, usuario=FREE), espera_erro="Freelance")

print("\n== Máquina de estados ==")
def status(s, usuario=ATD, pedido=PED):
    return f"UPDATE pedidos SET status='{s}' WHERE id='{pedido}';"
teste("Pular a Prova 1 é recusado", status("PRONTO"), espera_erro="não é permitida")
teste("Sequência de provas até PRONTO", status("PROVA_1") + status("EM_AJUSTE") + status("PROVA_2") + status("PRONTO"))
teste("Retirada com saldo aberto é recusada", status("ALUGADO"), espera_erro="ainda falta pagar R$ 900,00")
teste("Quitar em 6x no lojista com juros e retirar",
      pag(90000, "CREDITO", 6, ",modalidade_cartao,maquininha_id,acrescimo_juros_centavos", f",'LOJISTA',{MAQ},7830")
      + status("ALUGADO") + f"SELECT saldo_centavos FROM pedidos_saldo WHERE pedido_id='{PED}';", espera_saida="0")
teste("Atendente não cancela pedido", status("CANCELADO"), espera_erro="Cancelamento precisa")
teste("Histórico registra todas as mudanças",
      f"SELECT count(*) FROM historico_status WHERE pedido_id='{PED}';", espera_saida="6")

print("\n== Livro caixa ==")
TID = psql(f"BEGIN; SET LOCAL app.usuario_id='{ATD}'; SELECT min(id) FROM transacoes; COMMIT;")[1]
teste("App não consegue editar lançamento", f"UPDATE transacoes SET valor_centavos=1 WHERE id={TID};",
      espera_erro="permission denied")
code, out = psql(f"UPDATE transacoes SET valor_centavos=1 WHERE id={TID};", superuser=True)
checar("Nem o superusuário edita o livro caixa (trigger)", "imutável" in out, out)
def estorno(aprovador, tipo="SAIDA", valor=30000):
    return f"""INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,estorno_de,descricao,
               chave_idempotencia,usuario_id,aprovado_por)
               VALUES (ctx_loja(),'{tipo}','ESTORNO',{valor},'PIX',{TID},'Estorno',gen_random_uuid(),'{GER}',{aprovador});"""
teste("Estorno sem aprovação é recusado", estorno("NULL"), espera_erro="precisa de aprovação")
teste("Atendente não aprova estorno", estorno(f"'{ATD}'"), espera_erro="Estorno precisa")
teste("Quem lançou não aprova o próprio estorno",
      f"""WITH t AS (INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,descricao,chave_idempotencia,usuario_id,servico_id)
            VALUES (ctx_loja(),'ENTRADA','AVULSO',5000,'DINHEIRO','Avulso',gen_random_uuid(),'{GER}',
                    (SELECT id FROM servicos WHERE loja_id = ctx_loja() LIMIT 1)) RETURNING id)
          INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,estorno_de,descricao,chave_idempotencia,usuario_id,aprovado_por)
          SELECT ctx_loja(),'SAIDA','ESTORNO',5000,'DINHEIRO',id,'Estorno',gen_random_uuid(),'{GER}','{GER}' FROM t;""",
      espera_erro="próprio lançamento", usuario=GER)
teste("Estorno com valor diferente é recusado", estorno(f"'{GER}'", valor=100), espera_erro="mesmo valor")
teste("Estorno aprovado pela Gerente é aceito", estorno(f"'{GER}'"), usuario=GER)
teste("Saída acima do teto pela atendente é recusada",
      f"""INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,descricao,chave_idempotencia,usuario_id)
          VALUES (ctx_loja(),'SAIDA','DESPESA',15000,'DINHEIRO','Material',gen_random_uuid(),'{ATD}');""",
      espera_erro="acima do limite")
teste("Saída acima do teto com aprovação da Gerente é aceita",
      f"""INSERT INTO transacoes (loja_id,tipo,natureza,valor_centavos,forma_pagamento,descricao,chave_idempotencia,usuario_id,aprovado_por)
          VALUES (ctx_loja(),'SAIDA','DESPESA',15000,'DINHEIRO','Material',gen_random_uuid(),'{ATD}','{GER}');""")

print("\n== Venda simples, desconto e permissões ==")
p4, _ = nova_peca("Vestido dourado", alugar=False, vender=True)
venda = lambda desc, aprov, pago: f"""
  INSERT INTO pedidos (id,loja_id,tipo,produto_id,config_id,status,valor_peca_centavos,desconto_centavos,
    desconto_motivo,desconto_aprovado_por,valor_total_centavos,entrada_minima_centavos,max_parcelas_permitidas,
    chave_idempotencia,criado_por)
  VALUES ('{{pid}}',ctx_loja(),'VENDA','{p4}',{CFG},'X',89000,{desc},'Cliente antiga',{aprov},0,0,0,gen_random_uuid(),'{ATD}');
  """ + (pag(pago, pedido="{pid}") if pago else "")
v = lambda d, a, p: venda(d, a, p).replace("{pid}", str(uuid.uuid4()))
teste("Desconto aprovado pela atendente é recusado", v(8900, f"'{ATD}'", 80100), espera_erro="aprovado pela Dona ou pela Gerente")
teste("Desconto acima do teto de 10% é recusado", v(9000, f"'{GER}'", 80000), espera_erro="acima do teto")
teste("Venda simples não paga é recusada no commit", v(0, "NULL", 50000), espera_erro="paga no ato")
teste("Venda simples com 10% de desconto aprovado e paga", v(8900, f"'{GER}'", 80100)
      + f"SELECT status FROM produtos WHERE id='{p4}';", espera_saida="VENDIDO")
teste("Peça vendida não entra em novo pedido", v(0, "NULL", 89000), espera_erro="não está mais disponível")
teste("Regra comercial pela atendente é recusada",
      f"INSERT INTO config_pagamento (loja_id, definido_por) VALUES (ctx_loja(), '{ATD}');", espera_erro="Só a Dona ou a Gerente")
teste("Regra comercial é imutável", f"UPDATE config_pagamento SET entrada_minima_pct=10;", espera_erro="permission denied")
code, out = psql(f"""BEGIN; INSERT INTO usuario_papeis (loja_id,usuario_id,papel_id,concedido_por)
  SELECT '{L}','{ATD2}',id,'{DEV}' FROM papeis WHERE codigo='FINANCEIRO'; COMMIT;""", superuser=True)
checar("Dev não atribui papel de Diretoria", "Diretoria" in out, out)
code, out = psql(f"""BEGIN; INSERT INTO usuario_papeis (loja_id,usuario_id,papel_id,concedido_por)
  SELECT '{L}','{FREE}',id,'{DONA}' FROM papeis WHERE codigo='COSTUREIRA'; COMMIT;""", superuser=True)
checar("Freelance sem data de expiração é recusado", "expiração" in out, out)

print("\n== Isolamento entre lojas (RLS) ==")
code, out = psql(f"SET app.loja_id = '{L2}'; SELECT count(*) FROM produtos;")
checar("Outra loja não vê as peças", out.endswith("0"), out)
code, out = psql(f"""SET app.loja_id = '{L2}'; INSERT INTO clientes (loja_id,nome) VALUES ('{L}','Invasor');""")
checar("Outra loja não grava dados nesta loja", "row-level security" in out, out)
code, out = psql("SELECT count(*) FROM schema_migrations;")
checar("App não enxerga o controle de migrações", "permission denied" in out, out)

print("\n== Padrão brasileiro ==")
code, out = psql("SELECT to_char(1234.5, 'FM999G999G990D00');")
checar("Valores no formato 1.234,50", out.endswith("1.234,50"), out)

print(f"\nResultado: {ok} passaram, {falhas} falharam.")
if os.environ.get("MANTER_BANCO") != "1":
    rodar(["psql", "-X", "-q", "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'])
sys.exit(1 if falhas else 0)
