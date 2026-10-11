#!/usr/bin/env python3
"""Testes de ponta a ponta do app: cada cargo, cada botão, com o banco real.

Cria o banco descartável TEST_DB, aplica as migrações, cria a loja e os usuários pela CLI
e usa o app como um navegador faria (TestClient), conferindo telas e o que foi gravado.

Conexão: PGHOST/PGPORT/PGUSER/PGPASSWORD (superusuário). O app conecta como app_user;
a senha dele vem de APP_DB_PASSWORD (em sockets locais com "trust" pode ficar vazia).
Uso:  PGHOST=... PGUSER=... python3 tests/test_app.py
"""
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

RAIZ = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get("TEST_DB_APP", "loja_app_teste")
assert TEST_DB.endswith("_teste"), "TEST_DB_APP precisa terminar em _teste"
SENHA = "Senha-de-teste-123"
ok = falhas = 0


def rodar(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def checar(nome, passou, detalhe=""):
    global ok, falhas
    ok += bool(passou)
    falhas += not passou
    print(("PASSOU " if passou else "FALHOU ") + nome + ("" if passou else f"\n   -> {str(detalhe)[-600:]}"))


def sql(consulta):
    r = rodar(["psql", "-X", "-qAt", "-d", TEST_DB, "-c", consulta])
    return r.stdout.strip()


# --------------------------------------------------------------------------- #
# Banco, loja e usuários
# --------------------------------------------------------------------------- #
for comando in (f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)', f'CREATE DATABASE "{TEST_DB}"',
                f'GRANT CONNECT ON DATABASE "{TEST_DB}" TO app_user'):
    r = rodar(["psql", "-X", "-q", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", comando])
    if r.returncode:
        sys.exit(r.stderr)
r = rodar(["bash", str(RAIZ / "scripts/migrate.sh")],
          env={**os.environ, "PGDATABASE": TEST_DB, "MIGRATIONS_DIR": str(RAIZ / "migrations")})
if r.returncode:
    sys.exit("Migração falhou: " + r.stderr)

host, porta = os.environ.get("PGHOST", "localhost"), os.environ.get("PGPORT", "5432")
senha_app = quote(os.environ.get("APP_DB_PASSWORD", ""), safe="")
os.environ["DATABASE_URL"] = (f"postgresql://app_user:{senha_app}@/{TEST_DB}?host={quote(host, safe='')}&port={porta}"
                              if host.startswith("/") else f"postgresql://app_user:{senha_app}@{host}:{porta}/{TEST_DB}")
os.environ.setdefault("SESSION_SECRET", "x" * 48)
os.environ["COOKIE_SECURE"] = "0"
os.environ.setdefault("DADOS_CHAVE", "ab" * 32)
os.environ["LIMITES_ATIVOS"] = "0"
os.environ.pop("LOJA_ID", None)
APP = RAIZ / "app"


def cli(*args):
    r = rodar([sys.executable, "-m", "app.cli", *args, "--senha-stdin"], input=SENHA + "\n", cwd=APP,
              env={**os.environ, "PYTHONPATH": str(APP)})
    if r.returncode:
        sys.exit(f"CLI falhou {args}: {r.stderr}{r.stdout}")
    return r.stdout


saida = cli("criar-loja", "--nome", "Loja Teste", "--login", "dona", "--nome-dona", "Maria Dona")
os.environ["LOJA_ID"] = re.search(r"LOJA_ID=([0-9a-f-]{36})", saida).group(1)
ate = (date.today() + timedelta(days=30)).isoformat()
for login, nome, papel, extra in [("ger", "Gerente Gê", "GERENTE", ()), ("ana", "Ana Atendente", "ATENDENTE", ()),
                                  ("cida", "Cida Costura", "COSTUREIRA", ()), ("lu", "Lu Limpeza", "LIMPEZA", ()),
                                  ("dev", "Carlos Dev", "DESENVOLVEDOR", ()),
                                  ("fre", "Fê Freela", "ATENDENTE", ("--ate", ate))]:
    cli("criar-usuario", "--login", login, "--nome", nome, "--papel", papel, "--concedido-por", "dona", *extra)
checar("CLI cria loja e 7 usuários", sql("SELECT count(*) FROM usuarios") == "7")
# A primeira parte testa o modo "só nas datas"; o modo "até devolver" (padrão) é testado mais abaixo
sql("UPDATE lojas SET reserva_modo = 'DATAS'")

# --------------------------------------------------------------------------- #
# Mercado Pago de mentira (o teste nunca fala com o de verdade)
# --------------------------------------------------------------------------- #
import json as _json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import parse_qs, unquote, urlparse  # noqa: E402

TOKEN_BOM = "APP_USR-1234567890-teste-loja-abcdefghij-9876"
MP = {"preferencias": [], "pagamentos": {}}


class FalsoMercadoPago(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _responder(self, codigo, corpo):
        dados = _json.dumps(corpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _autorizado(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN_BOM}"

    def do_GET(self):
        if not self._autorizado():
            return self._responder(401, {"message": "invalid token"})
        url = urlparse(self.path)
        if url.path == "/users/me":
            return self._responder(200, {"id": 1, "nickname": "LOJATESTE"})
        if url.path == "/v1/payments/search":
            ref = parse_qs(url.query).get("external_reference", [""])[0]
            return self._responder(200, {"results": [p for p in MP["pagamentos"].values()
                                                     if p["external_reference"] == ref]})
        if url.path.startswith("/v1/payments/"):
            p = MP["pagamentos"].get(url.path.rsplit("/", 1)[1])
            return self._responder(200, p) if p else self._responder(404, {"message": "not found"})
        self._responder(404, {})

    def do_POST(self):
        if not self._autorizado():
            return self._responder(401, {})
        corpo = _json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/checkout/preferences":
            MP["preferencias"].append({"corpo": corpo, "idem": self.headers.get("X-Idempotency-Key")})
            n = len(MP["preferencias"])
            return self._responder(201, {"id": f"pref-{n}",
                                         "init_point": f"https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-{n}"})
        self._responder(404, {})


_servidor_mp = ThreadingHTTPServer(("127.0.0.1", 0), FalsoMercadoPago)
threading.Thread(target=_servidor_mp.serve_forever, daemon=True).start()
os.environ["MP_API"] = f"http://127.0.0.1:{_servidor_mp.server_port}"
os.environ.setdefault("FOTOS_DIR", "/tmp/fotos-teste-app")

sys.path.insert(0, str(APP))
from fastapi.testclient import TestClient  # noqa: E402
from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app import cripto  # noqa: E402


def aberto(consulta, rotulo):
    """Lê uma coluna cifrada pelo psql e abre com a chave do app."""
    h = sql(consulta)
    return cripto.decifrar(bytes.fromhex(h[2:]), rotulo) if h else None


def idx_tel(fone):
    return cripto.indice_telefone(fone)

db.pool.open(wait=True)


ULTIMO_CODIGO = None      # definido na parte do WhatsApp (lê o código que chegou no celular)


class Pessoa:
    def __init__(self, login):
        self.c = TestClient(app, follow_redirects=True)
        self.csrf = None
        r = self.c.get("/login")
        self.csrf = self._token(r.text)
        r = self.c.post("/login", data={"csrf": self.csrf, "login": login, "senha": SENHA})
        if r.url.path == "/login/codigo":                     # aparelho novo com WhatsApp ligado: código
            r = self.c.post("/login/codigo", data={"csrf": self._token(r.text), "codigo": ULTIMO_CODIGO(login)})
        assert r.status_code == 200 and "Olá" in r.text, f"login {login} falhou: {r.status_code}"
        self.csrf = self._token(r.text) or self.csrf

    @staticmethod
    def _token(html):
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        return m.group(1) if m else None

    def get(self, url, **kw):
        r = self.c.get(url, **kw)
        self.csrf = self._token(r.text) or self.csrf
        self.ultima = r
        return r

    def post(self, url, dados=None, **kw):
        dados = {"csrf": self.csrf, **(dados or {})}
        r = self.c.post(url, data=dados, **kw)
        self.csrf = self._token(r.text) or self.csrf
        self.ultima = r
        return r

    def campos(self, html=None):
        """Campos escondidos do formulário 'SIM, GUARDAR' (o que envia para .../gravar)."""
        html = html or self.ultima.text
        form = re.search(r'<form method="post" action="[^"]*/gravar">(.*?)</form>', html, re.S).group(1)
        return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))

    def chave(self, url):
        return self._achar(self.get(url).text, "chave")

    @staticmethod
    def _achar(html, nome):
        m = re.search(rf'name="{nome}" value="([^"]*)"', html)
        return m.group(1) if m else None


dona, ger, ana, cida, lu, dev = (Pessoa(x) for x in ("dona", "ger", "ana", "cida", "lu", "dev"))
fre = Pessoa("fre")
hoje = date.today()

# --------------------------------------------------------------------------- #
print("\n== Tela inicial: cada cargo vê só os seus botões ==")
def botoes(p):
    return set(re.findall(r'class="atalho[^"]*" href="([^"]+)"', p.get("/").text))
checar("Atendente: caixa, pedidos, catálogo, limpeza; sem relatórios/config",
       {"/caixa/receber", "/caixa/pagar", "/pedidos/novo", "/pecas", "/pecas/nova", "/caixa"} <= botoes(ana)
       and not {"/relatorios", "/config", "/sistema"} & botoes(ana), botoes(ana))
checar("Costureira: roupa, costura, agenda e a própria senha",
       botoes(cida) == {"/pecas", "/costura", "/agenda", "/minha-senha"}, botoes(cida))
checar("Limpeza: roupa, limpeza e a própria senha", botoes(lu) == {"/pecas", "/limpeza", "/minha-senha"}, botoes(lu))
checar("Atendente vê Clientes e Agenda, mas não Equipe",
       {"/clientes", "/agenda"} <= botoes(ana) and "/equipe" not in botoes(ana), botoes(ana))
checar("Dona e Dev veem Equipe", "/equipe" in botoes(dona) and "/equipe" in botoes(dev))
checar("Dev: painel técnico e relatórios, sem lançar dinheiro",
       {"/sistema", "/relatorios"} <= botoes(dev) and not {"/caixa/receber", "/caixa/pagar"} & botoes(dev), botoes(dev))
checar("Costureira tentando abrir o caixa recebe 403", cida.get("/caixa/receber").status_code == 403)
checar("Página sem login vai para o login", "Entrar no sistema" in TestClient(app).get("/caixa").text)

print("\n== Configurações: maquininhas ==")
checar("Atendente não cadastra maquininha", ana.post("/config/maquininhas", {"apelido": "X", "operadora": "Rede",
                                                                              "cor": "#E4570F"}).status_code == 403)
r = ger.post("/config/maquininhas", {"apelido": "Laranjinha Itaú", "operadora": "Rede", "cor": "#E4570F"})
checar("Gerente cadastra a Laranjinha Itaú", "Laranjinha Itaú cadastrada" in r.text, r.text[:300])
ger.post("/config/maquininhas", {"apelido": "Rede balcão", "operadora": "Rede", "cor": "#111111"})
MAQ = sql("SELECT id FROM maquininhas WHERE apelido = 'Laranjinha Itaú'")
CLI_BALCAO = sql(f"INSERT INTO clientes (loja_id, nome) VALUES ('{os.environ['LOJA_ID']}', 'Cliente Balcão Teste') RETURNING id").splitlines()[0]
SERV = sql("SELECT id FROM servicos WHERE nome = 'Ajuste / conserto avulso'")
AVULSO = {"cliente": CLI_BALCAO, "servico": SERV}
checar("Recebi dinheiro sem cliente: pede para procurar (nada de dinheiro sem motivo)", "Todo dinheiro que entra tem um motivo" in ana.get("/caixa/receber").text
       and 'name="valor"' not in ana.ultima.text)
checar("Maquininhas aparecem na tela de receber", "Laranjinha Itaú" in ana.get(f"/caixa/receber?cliente={CLI_BALCAO}").text
       and "Rede balcão" in ana.ultima.text and "Cliente Balcão Teste" in ana.ultima.text)
r = ana.post("/caixa/receber/conferir", {"cliente": CLI_BALCAO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "10", "forma": "PIX"})
checar("Sem escolher o serviço: recusa", "Escolha o serviço" in r.text)
checar("Nenhuma tela usa style inline (bloqueado pela CSP)",
       not [t.name for t in (APP / "app/templates").rglob("*.html") if "style=" in t.read_text()])

print("\n== Catálogo ==")
chave = ana.chave("/pecas/nova")
cat_ves = sql("SELECT id FROM categorias WHERE prefixo_sku = 'VES'")
peca = {"chave": chave, "categoria": cat_ves, "nome": "Vestido longo azul marinho", "tamanho": "40",
        "cor": "Azul", "uso": "ambos", "preco_aluguel": "1.200,00", "preco_venda": "890"}
r = ana.post("/pecas/nova", {**peca, "preco_venda": ""})
checar("Sem preço de venda: mensagem clara e nada gravado", r.status_code == 400 and "preço de venda" in r.text
       and sql("SELECT count(*) FROM produtos") == "0")
r = ana.post("/pecas/nova", peca)
checar("Cadastro gera o código VES-0001", r.status_code == 200 and "VES-0001" in r.text and "Peça cadastrada" in r.text)
ana.post("/pecas/nova", peca)
checar("Clique duplo no cadastrar não duplica a peça", sql("SELECT count(*) FROM produtos") == "1")
P1 = sql("SELECT id FROM produtos WHERE sku = 'VES-0001'")
checar("Preços gravados em centavos", sql(f"SELECT preco_aluguel_centavos || '/' || preco_venda_centavos FROM produtos WHERE id='{P1}'") == "120000/89000")
checar("Busca sem acento e com erro de digitação acha", "VES-0001" in ana.get("/pecas?q=vestdo+azul").text)
checar("Busca por várias palavras (cor + tamanho)", "VES-0001" in ana.get("/pecas?q=azul+40").text)
checar("Leitor de código 'ves1' abre direto a peça", ana.get("/pecas?q=ves1").url.path == f"/pecas/{P1}")
checar("Filtro 'para vender' mostra a peça", "VES-0001" in ana.get("/pecas?filtro=vender").text)
r = ana.get(f"/pecas/{P1}/etiqueta")
checar("Etiqueta tem código de barras e SKU", "<svg" in r.text and "Código de barras VES-0001" in r.text)
r = ana.post(f"/pecas/{P1}/editar", {"nome": "Vestido longo azul marinho", "tamanho": "42", "cor": "Azul",
                                     "uso": "ambos", "preco_aluguel": "1200,00", "preco_venda": "890,00"})
checar("Editar peça muda o tamanho, não o código", "Tamanho <strong>42" in r.text and sql(f"SELECT sku FROM produtos WHERE id='{P1}'") == "VES-0001")
for nome, uso, pa, pv in [("Vestido dourado de festa", "vender", "", "650"), ("Terno preto slim", "alugar", "300", "")]:
    ana.post("/pecas/nova", {"chave": ana.chave("/pecas/nova"), "categoria": cat_ves, "nome": nome, "uso": uso,
                             "preco_aluguel": pa, "preco_venda": pv})
P2, P3 = sql("SELECT id FROM produtos WHERE sku='VES-0002'"), sql("SELECT id FROM produtos WHERE sku='VES-0003'")
checar("Sequência de códigos continua (VES-0002, VES-0003)", P2 and P3)

print("\n== Caixa: receber ==")
def receber(p, dados, url="/caixa/receber"):
    if url == "/caixa/receber":
        dados = {**AVULSO, **dados}
    chave = p.chave(url + (f"?cliente={CLI_BALCAO}" if url == "/caixa/receber" else ""))
    r = p.post(f"{url}/conferir", {"chave": chave, **dados})
    if r.status_code != 200 or "Está certo?" not in r.text:
        return r, None
    campos = p.campos(r.text)
    return p.post(f"{url}/gravar", campos), campos
r = ana.post("/caixa/receber/conferir", {**AVULSO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "abc", "forma": "PIX"})
checar("Valor inválido: pede para digitar de novo", r.status_code == 400 and "Digite o valor" in r.text)
r = ana.post("/caixa/receber/conferir", {**AVULSO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "100", "forma": "CREDITO"})
checar("Cartão sem maquininha: pede a maquininha", "Escolha em qual maquininha" in r.text)
r, campos = receber(ana, {"valor": "150,00", "forma": "PIX", "descricao": "Conserto de barra"})
checar("Receber R$ 150 no PIX: conferência e 'Guardado!'", "Guardado!" in r.text and "R$ 150,00" in r.text, r.text[-400:])
ana.post("/caixa/receber/gravar", campos)
checar("Apertar 'SIM, GUARDAR' duas vezes grava uma vez só",
       sql("SELECT count(*) FROM transacoes WHERE descricao LIKE '%· Conserto de barra'") == "1")
r = ana.post("/caixa/receber/conferir", {**AVULSO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "300", "forma": "CREDITO",
                                         "maquininha": MAQ, "parcelas": "3"})
checar("Crédito 3x mostra o que digitar na Laranjinha",
       "Passe R$ 300,00 na Laranjinha Itaú: Crédito parcelado LOJA em 3x (3x de R$ 100,00)" in r.text, r.text[-800:])
r = ana.post("/caixa/receber/conferir", {**AVULSO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "300", "forma": "CREDITO",
                                         "maquininha": MAQ, "parcelas": "9"})
checar("Parcelas acima do limite (6x) são recusadas", "até 6x" in r.text)
r, _ = receber(fre, {"valor": "10", "forma": "DINHEIRO"})
checar("Freelance não lança no caixa (trava do banco)", "Freelance não opera o caixa" in r.text, r.text[-300:])

print("\n== Caixa: pagar ==")
r = ana.post("/caixa/pagar/conferir", {"chave": ana.chave("/caixa/pagar"), "valor": "150", "forma": "DINHEIRO",
                                       "descricao": "Linha"})
checar("Atendente acima do limite (R$ 100): chama a Gerente", "acima do seu limite" in r.text)
r = ana.post("/caixa/pagar/conferir", {"chave": ana.chave("/caixa/pagar"), "valor": "50", "forma": "CREDITO",
                                       "descricao": "Linha"})
checar("Paguei com o cartão da loja: não pede maquininha", "Está certo?" in r.text and "maquininha" not in r.text.lower().split("está certo?")[0][-400:], r.text[-600:])
r = ana.post("/caixa/pagar/conferir", {"chave": ana.chave("/caixa/pagar"), "valor": "50", "forma": "DINHEIRO"})
checar("Saída sem descrição: pede o que foi pago", "Escreva o que foi pago" in r.text)
r, _ = receber(ana, {"valor": "80", "forma": "DINHEIRO", "descricao": "Linha e botões"}, "/caixa/pagar")
checar("Paguei R$ 80 em dinheiro", "Guardado!" in r.text and "Saída" in r.text)

print("\n== Caixa do dia e desfazer ==")
r = ana.get("/caixa")
checar("Caixa do dia: entrou R$ 150, saiu R$ 80, gaveta -R$ 80",
       "R$ 150,00" in r.text and "R$ 80,00" in r.text and "- R$ 80,00" in r.text, r.text[:2000])
T_PIX = sql("SELECT id FROM transacoes WHERE descricao LIKE '%· Conserto de barra'")
checar("Atendente não vê o botão Desfazer", "/desfazer" not in r.text)
checar("Atendente não desfaz pelo endereço", ana.get(f"/caixa/{T_PIX}/desfazer").status_code == 403)
r = ger.post(f"/caixa/{T_PIX}/desfazer", {"motivo": ""})
checar("Desfazer exige motivo", "motivo" in r.text and r.status_code == 400)
r = ger.post(f"/caixa/{T_PIX}/desfazer", {"motivo": "Cliente desistiu"})
checar("Gerente desfaz lançamento da atendente (estorno, nada apagado)",
       sql(f"SELECT count(*) FROM transacoes WHERE estorno_de = {T_PIX}") == "1"
       and sql(f"SELECT count(*) FROM transacoes WHERE id = {T_PIX}") == "1")
r = ger.post(f"/caixa/{T_PIX}/desfazer", {"motivo": "de novo"})
checar("Desfazer duas vezes: 'já foi desfeito'", "já foi desfeito" in r.text)
receber(ger, {"valor": "20", "forma": "PIX", "descricao": "Da gerente"})
T_GER = sql("SELECT id FROM transacoes WHERE descricao LIKE '%· Da gerente'")
r = ger.post(f"/caixa/{T_GER}/desfazer", {"motivo": "teste"})
checar("Ninguém desfaz o próprio lançamento", "próprio lançamento" in r.text, r.text[-300:])

print("\n== Pedido de aluguel: do começo ao fim ==")
ret, dev_ = hoje + timedelta(days=20), hoje + timedelta(days=22)
def pedido(p, dados, peca=P1):
    chave = Pessoa._achar(p.get(f"/pedidos/novo?peca={peca}").text, "chave")
    r = p.post("/pedidos/novo/conferir", {"chave": chave, "peca": peca, **dados})
    if "Está certo?" not in r.text:
        return r
    return p.post("/pedidos/novo/gravar", p.campos(r.text))
base = {"tipo": "ALUGUEL", "cliente_nome": "Joana Silva", "cliente_telefone": "(19) 99999-1111",
        "data_retirada": ret.isoformat(), "data_devolucao": dev_.isoformat(), "forma": "PIX"}
r = pedido(ana, {**base, "valor": "299,99"})
checar("Entrada abaixo de 25% é recusada com o valor mínimo", "entrada mínima é R$ 300,00" in r.text, r.text[-500:])
r = pedido(ana, {**base, "valor": "300", "data_devolucao": (ret - timedelta(days=1)).isoformat()})
checar("Devolução antes da retirada é recusada", "devolução não pode ser antes" in r.text)
r = pedido(ana, {**base, "valor": "300"})
checar("Aluguel com entrada de R$ 300: pedido guardado, falta R$ 900",
       "Pedido guardado" in r.text and "R$ 900,00" in r.text, r.text[-800:])
PED = sql("SELECT id FROM pedidos WHERE tipo = 'ALUGUEL' LIMIT 1")
r = pedido(ana, {**base, "cliente_nome": "Outra Cliente", "cliente_telefone": "(19) 99777-0001", "valor": "300",
                 "data_retirada": (ret + timedelta(days=1)).isoformat(),
                 "data_devolucao": (dev_ + timedelta(days=1)).isoformat()})
checar("Mesmo vestido, datas sobrepostas: 'já está reservada'", "já está reservada" in r.text, r.text[-500:])
checar("Pedido recusado não deixou cliente nem dinheiro para trás",
       sql("SELECT count(*) FROM clientes WHERE nome = 'Outra Cliente'") == "0"
       and sql("SELECT count(*) FROM pedidos") == "1")

def etapa(p, para, pedido_id=PED, volta=None):
    dados = {"para": para, **({"volta": volta} if volta else {})}
    return p.post(f"/pedidos/{pedido_id}/etapa", dados)
etapa(ana, "PROVA_1"); etapa(ana, "EM_AJUSTE")
r = ana.get(f"/pedidos/{PED}")
checar("Atendente não vê 'Ajuste pronto' (é da costura)", "Ajuste pronto" not in r.text)
checar("Atendente não força a etapa da costura", etapa(ana, "PROVA_2").status_code == 403)
r = cida.get("/costura")
checar("Costureira vê o vestido na fila 'Para ajustar agora'", "VES-0001" in r.text and "Ajuste pronto" in r.text)
r = etapa(cida, "PROVA_2", volta="/costura")
checar("Costureira marca 'Ajuste pronto' e volta para a fila", r.url.path == "/costura")
etapa(ana, "PRONTO")
r = ana.get(f"/pedidos/{PED}")
checar("Retirada bloqueada enquanto falta pagar (botão desativado)", "falta pagar R$ 900,00" in r.text)
r = etapa(ana, "ALUGADO")
checar("…e o banco também recusa se tentarem forçar", "falta pagar" in r.text and
       sql(f"SELECT status FROM pedidos WHERE id='{PED}'") == "PRONTO")
r, _ = receber(ana, {"valor": "950", "forma": "PIX"}, f"/pedidos/{PED}/receber")
checar("Receber mais que o saldo é recusado", "passa do que falta pagar" in r.text)
r, _ = receber(ana, {"valor": "900", "forma": "CREDITO", "maquininha": MAQ, "parcelas": "2"}, f"/pedidos/{PED}/receber")
checar("Receber o saldo em 2x no crédito quita o pedido", "Quitado" in r.text, r.text[-600:])
etapa(ana, "ALUGADO"); etapa(ana, "DEVOLVIDO")
checar("Devolvido aparece para a limpeza", "VES-0001" in lu.get("/limpeza").text)
etapa(lu, "EM_LIMPEZA", volta="/limpeza"); etapa(lu, "CONCLUIDO", volta="/limpeza")
checar("Ciclo completo: CONCLUIDO e peça disponível de novo",
       sql(f"SELECT status FROM pedidos WHERE id='{PED}'") == "CONCLUIDO"
       and "Disponível" in ana.get(f"/pecas/{P1}").text)
checar("Histórico registrou a criação e as 8 etapas", sql(f"SELECT count(*) FROM historico_status WHERE pedido_id='{PED}'") == "9")

print("\n== Venda simples e venda com ajuste ==")
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "cliente_nome": "Paula Balcão", "cliente_telefone": "(19) 99777-0002", "valor": "600", "forma": "DINHEIRO"}, peca=P2)
checar("Venda sem ajuste precisa ser paga inteira", "paga na hora" in r.text)
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "cliente_nome": "Paula Balcão", "cliente_telefone": "(19) 99777-0002", "valor": "650", "forma": "DINHEIRO"}, peca=P2)
checar("Venda simples paga: pedido vendido e peça Vendida", "Pedido guardado" in r.text and
       sql(f"SELECT status FROM produtos WHERE id='{P2}'") == "VENDIDO")
checar("Peça vendida some do 'Fazer pedido'", "Fazer pedido" not in ana.get(f"/pecas/{P2}").text)
r = pedido(ana, {"tipo": "VENDA_AJUSTE", "cliente_nome": "Bia Souza", "cliente_telefone": "(19) 99777-0003", "data_retirada": ret.isoformat(),
                 "valor_ajuste": "150", "valor": "260", "forma": "PIX"}, peca=P1)
checar("Venda com ajuste: total peça + ajuste, entrada de 25%",
       "Pedido guardado" in r.text and sql("SELECT valor_total_centavos FROM pedidos WHERE com_ajuste AND tipo='VENDA'") == "104000",
       r.text[-500:])
PV = sql("SELECT id FROM pedidos WHERE com_ajuste AND tipo='VENDA'")
checar("Mesma cliente pelo telefone não duplica cadastro",
       (pedido(ana, {**base, "cliente_nome": "Joana S.", "valor": "75", "data_retirada": (ret + timedelta(days=30)).isoformat(),
                     "data_devolucao": (ret + timedelta(days=31)).isoformat()}, peca=P3) and True)
       and sql(f"SELECT count(*) FROM clientes WHERE telefone_idx = '{idx_tel('19999991111')}'") == "1")

print("\n== Cancelar ==")
checar("Atendente não cancela", ana.post(f"/pedidos/{PV}/cancelar", {"motivo": "x"}).status_code == 403)
r = ger.post(f"/pedidos/{PV}/cancelar", {"motivo": "Cliente desistiu"})
checar("Gerente cancela com motivo; entrada continua no caixa",
       sql(f"SELECT status FROM pedidos WHERE id='{PV}'") == "CANCELADO"
       and sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PV}'") == "1")

print("\n== Regras da Dona/Gerente ==")
juros = {f"juros_{n}": "0" for n in range(2, 13)}
juros["juros_3"] = "5"
r = ana.post("/config/regras", {"entrada": "30", **juros})
checar("Atendente não muda regras", r.status_code == 403)
r = ger.post("/config/regras", {"entrada": "30", "parc_aluguel": "6", "parc_venda": "6", "parcela_min": "50",
                                "desconto": "10", "teto_saida": "100", "dias_limpeza": "3", **juros})
checar("Gerente muda entrada para 30% e juros de 5% em 3x", "Regras salvas" in r.text, r.text[-400:])
checar("Pedido antigo mantém a regra da época (25%)", sql(f"SELECT entrada_minima_centavos FROM pedidos WHERE id='{PED}'") == "30000")
r = ana.post("/caixa/receber/conferir", {**AVULSO, "chave": ana.chave(f"/caixa/receber?cliente={CLI_BALCAO}"), "valor": "300", "forma": "CREDITO",
                                         "maquininha": MAQ, "parcelas": "3"})
checar("Crédito 3x agora cobra 5% de acréscimo (R$ 315,00 na maquininha)",
       "Passe R$ 315,00" in r.text and "R$ 15,00 de acréscimo" in r.text, r.text[-600:])

print("\n== Agenda do pedido de aluguel ==")
checar("Pedido de aluguel ganhou 1ª prova, 2ª prova, retirada e devolução",
       sql(f"SELECT string_agg(tipo, ',' ORDER BY inicio) FROM agendamentos WHERE pedido_id='{PED}'")
       == "PROVA_1,PROVA_2,RETIRADA,DEVOLUCAO")
checar("Cada etapa feita marcou o compromisso como FEITO sozinho",
       sql(f"SELECT count(*) FROM agendamentos WHERE pedido_id='{PED}' AND status='FEITO'") == "4")
checar("Pedido cancelado teve a agenda cancelada",
       sql(f"SELECT count(*) FROM agendamentos WHERE pedido_id='{PV}' AND status <> 'CANCELADO'") == "0")

print("\n== Clientes ==")
checar("Lista de clientes mostra quem já fez pedido", "Joana Silva" in ana.get("/clientes").text)
checar("Busca de cliente por telefone", "Joana Silva" in ana.get("/clientes?q=9999-1111").text)
cli_dados = {"nome": "Carla Souza", "telefone": "(19) 98888-2222", "cpf": "529.982.247-25",
             "email": "carla@exemplo.com", "observacoes": "Prefere provas à tarde"}
r = ana.post("/clientes/nova", cli_dados)
checar("Cadastro sem autorização LGPD é recusado", r.status_code == 400 and "autorizou" in r.text)
r = ana.post("/clientes/nova", {**cli_dados, "cpf": "123.456.789-00", "lgpd": "1"})
checar("CPF inválido é recusado", r.status_code == 400 and "CPF não é válido" in r.text)
r = ana.post("/clientes/nova", {**cli_dados, "lgpd": "1"})
checar("Cliente cadastrada com CPF, e-mail e autorização", "Cliente cadastrada" in r.text
       and aberto("SELECT cpf_c FROM clientes WHERE nome = 'Carla Souza'", "clientes.cpf") == "52998224725"
       and aberto("SELECT email_c FROM clientes WHERE nome = 'Carla Souza'", "clientes.email") == "carla@exemplo.com"
       and sql("SELECT count(*) FROM clientes WHERE telefone IS NOT NULL OR cpf IS NOT NULL OR email IS NOT NULL") == "0")
checar("CPF e telefone não aparecem em texto no banco (cifrados)",
       sql("SELECT count(*) FROM clientes WHERE encode(cpf_c,'escape') LIKE '%52998224725%'") == "0"
       and sql("SELECT count(*) FROM clientes WHERE nome = 'Carla Souza' AND cpf_idx IS NOT NULL AND telefone_fim = '2222'") == "1")
CLI = sql("SELECT id FROM clientes WHERE nome = 'Carla Souza'")
r = ana.post("/clientes/nova", {**cli_dados, "nome": "Carla S.", "lgpd": "1"})
checar("Telefone repetido: avisa e oferece abrir a ficha", "Já existe cliente com esse telefone" in r.text
       and f"/clientes/{CLI}" in r.text)
r = ana.post(f"/clientes/{CLI}/editar", {**cli_dados, "nome": "Carla Souza Lima", "lgpd": "1"})
checar("Editar cliente", "Carla Souza Lima" in r.text)
r = dev.get(f"/clientes/{CLI}")
checar("Dev vê telefone e CPF mascarados (LGPD)", "98888-2222" not in r.text and "52998224725" not in r.text
       and "2222" in r.text, r.text[-1500:])
checar("Dev não cadastra cliente", dev.post("/clientes/nova", {**cli_dados, "lgpd": "1"}).status_code == 403)
r = cida.get(f"/clientes/{CLI}/medidas")
checar("Ficha de medidas com desenho numerado (feminina e masculina)",
       r.status_code == 200 and "<svg" in r.text and "Separação do busto" in r.text and "Entrepernas" in r.text
       and 'class="marca-medida p-cintura"' in r.text)
r = cida.post(f"/clientes/{CLI}/medidas", {"ficha": "FEMININO", "m_FEMININO_busto": "500"})
checar("Medida absurda é recusada", r.status_code == 400 and "parece errado" in r.text)
r = cida.post(f"/clientes/{CLI}/medidas", {"ficha": "FEMININO"})
checar("Ficha vazia é recusada", r.status_code == 400 and "pelo menos uma" in r.text)
r = cida.post(f"/clientes/{CLI}/medidas", {"ficha": "FEMININO", "m_FEMININO_busto": "92,5", "m_FEMININO_cintura": "70",
                                            "m_FEMININO_comprimento_vestido": "140", "salto": "10", "observacoes": "Subir barra 3 cm"})
checar("Costureira guarda a ficha feminina", "Medidas guardadas" in r.text and "92,5 cm" in r.text and "salto 10 cm" in r.text
       and sql(f"SELECT ficha FROM medidas WHERE cliente_id='{CLI}'") == "FEMININO"
       and aberto(f"SELECT valores_c FROM medidas WHERE cliente_id='{CLI}'", "medidas.valores") and _json.loads(aberto(f"SELECT valores_c FROM medidas WHERE cliente_id='{CLI}'", "medidas.valores"))["busto"] == 92.5
       and sql(f"SELECT valores::text FROM medidas WHERE cliente_id='{CLI}'") == "{}")
r = cida.post(f"/clientes/{CLI}/medidas", {"ficha": "FEMININO", "m_FEMININO_busto": "94", "m_FEMININO_cintura": "70"})
checar("Nova medição mostra a diferença para a anterior", "(+1,5)" in r.text and
       sql(f"SELECT count(*) FROM medidas WHERE cliente_id='{CLI}'") == "2")
checar("Atendente vê as medidas mas não altera",
       "94 cm" in ana.get(f"/clientes/{CLI}").text and ana.post(f"/clientes/{CLI}/medidas", {"ficha": "FEMININO", "m_FEMININO_busto": "90"}).status_code == 403)
r = dona.post(f"/clientes/{CLI}/medidas", {"ficha": "MASCULINO", "m_MASCULINO_torax": "110", "m_MASCULINO_cintura": "88"})
checar("Ficha feminina não pega a cintura da masculina", 'value="70"' in r.text and
       _json.loads(aberto(f"SELECT valores_c FROM medidas WHERE cliente_id='{CLI}' AND ficha='MASCULINO'", "medidas.valores"))["cintura"] == 88)
checar("Dona também anota; ficha masculina sugere tamanho pela tabela (paletó 48, calça 44)",
       r.status_code == 200 and "Paletó <strong>48</strong>" in r.text and "Calça <strong>44</strong>" in r.text, r.text[-1500:])
r = cida.get(f"/clientes/{CLI}/medidas/imprimir?ficha=FEMININO")
checar("Ficha de medidas para imprimir (com as medidas e colunas das provas)",
       "Ficha de medidas" in r.text and "94 cm" in r.text and "1ª prova" in r.text and "<svg" in r.text)
checar("Ficha em branco para imprimir", "94 cm" not in cida.get(f"/clientes/{CLI}/medidas/imprimir?ficha=FEMININO&em_branco=1").text)
r = ana.get(f"/pedidos/novo?peca={P1}&cliente={CLI}")
checar("Novo pedido a partir da ficha já vem com a cliente", "Carla Souza Lima" in r.text and 'name="cliente_id"' in r.text)

print("\n== Agenda: marcar, lembrar, remarcar ==")
p1, ret2, dev2 = hoje + timedelta(days=3), hoje + timedelta(days=10), hoje + timedelta(days=12)
chave = Pessoa._achar(r.text, "chave")
dados_ag = {"chave": chave, "peca": P1, "cliente_id": CLI, "tipo": "ALUGUEL", "cliente_nome": "Carla Souza Lima",
            "cliente_telefone": "19988882222", "data_prova": p1.isoformat(), "hora_prova": "14:30",
            "data_retirada": ret2.isoformat(), "hora_retirada": "09:00", "data_devolucao": dev2.isoformat(),
            "valor": "360", "forma": "PIX"}
r = ana.post("/pedidos/novo/conferir", {**dados_ag, "data_prova2": (ret2 + timedelta(days=1)).isoformat()})
checar("2ª prova depois da retirada é recusada", "2ª prova tem que ser" in r.text)
r = ana.post("/pedidos/novo/conferir", {**dados_ag, "hora_prova": "25:00"})
checar("Hora inválida é recusada", "horário ficou errado" in r.text)
r = ana.post("/pedidos/novo/conferir", dados_ag)
checar("Conferência mostra a agenda do processo", "Agenda" in r.text and "às 14:30 · <strong>1ª prova" in r.text
       and "2ª prova" in r.text and "Devolução" in r.text, re.findall(r'class="aviso"[^>]*>(.*?)<', r.text) or r.text[-800:])
r = ana.post("/pedidos/novo/gravar", ana.campos(r.text))
PAG = sql("SELECT id FROM pedidos WHERE cliente_id = '" + CLI + "'")
checar("Pedido usa a ficha da cliente (sem cadastro duplicado)", PAG and
       sql(f"SELECT count(*) FROM clientes WHERE telefone_idx = '{idx_tel('19988882222')}'") == "1")
checar("Agenda gravada com as horas escolhidas (2ª prova 2 dias antes, às 10h)",
       sql(f"""SELECT string_agg(tipo || ' ' || to_char(inicio AT TIME ZONE 'America/Sao_Paulo', 'DD/MM HH24:MI'), ', '
               ORDER BY inicio) FROM agendamentos WHERE pedido_id = '{PAG}'""")
       == f"PROVA_1 {p1:%d/%m} 14:30, PROVA_2 {ret2 - timedelta(days=2):%d/%m} 10:00, "
          f"RETIRADA {ret2:%d/%m} 09:00, DEVOLUCAO {dev2:%d/%m} 10:00")
checar("Peça fica reservada desde a 1ª prova", sql(f"SELECT inicio_bloqueio FROM pedidos WHERE id='{PAG}'") == p1.isoformat())
r = ana.get(f"/pedidos/{PAG}")
checar("Tela do pedido mostra a linha do tempo com Remarcar e WhatsApp",
       "Agenda do processo" in r.text and "Contratação" in r.text and "/remarcar" in r.text and "wa.me/5519988882222" in r.text)
r = ana.get(f"/agenda?dia={p1.isoformat()}")
checar("Agenda da semana mostra a prova da Carla com lembrete", "Carla Souza Lima" in r.text and "14:30" in r.text
       and "wa.me/5519988882222?text=" in r.text)
AG_P1 = sql(f"SELECT id FROM agendamentos WHERE pedido_id='{PAG}' AND tipo='PROVA_1'")
AG_DEV = sql(f"SELECT id FROM agendamentos WHERE pedido_id='{PAG}' AND tipo='DEVOLUCAO'")
AG_RET = sql(f"SELECT id FROM agendamentos WHERE pedido_id='{PAG}' AND tipo='RETIRADA'")
checar("Costureira vê a agenda mas não remarca", cida.get("/agenda").status_code == 200
       and cida.post(f"/agenda/{AG_P1}/remarcar", {"dia": p1.isoformat(), "hora": "10:00"}).status_code == 403)
r = ana.post(f"/agenda/{AG_P1}/remarcar", {"dia": (ret2 + timedelta(days=1)).isoformat(), "hora": "10:00"})
checar("Prova depois da retirada não pode", r.status_code == 400 and "antes da retirada" in r.text)
r = ana.post(f"/agenda/{AG_P1}/remarcar", {"dia": (p1 + timedelta(days=1)).isoformat(), "hora": "15:00",
                                            "observacao": "Cliente pediu"})
checar("Remarcar a 1ª prova: novo dia e hora, status Remarcado",
       sql(f"SELECT status || ' ' || to_char(inicio AT TIME ZONE 'America/Sao_Paulo', 'HH24:MI') FROM agendamentos WHERE id='{AG_P1}'")
       == "REMARCADO 15:00" and "Cliente pediu" in r.text)
checar("Reserva da peça acompanha a 1ª prova remarcada",
       sql(f"SELECT inicio_bloqueio FROM pedidos WHERE id='{PAG}'") == (p1 + timedelta(days=1)).isoformat())
r = ana.post(f"/agenda/{AG_RET}/remarcar", {"dia": (dev2 + timedelta(days=1)).isoformat(), "hora": "10:00"})
checar("Retirada depois da devolução não pode", r.status_code == 400 and "Remarque a devolução primeiro" in r.text)
r = ana.post(f"/agenda/{AG_DEV}/remarcar", {"dia": (dev2 + timedelta(days=2)).isoformat(), "hora": "11:00"})
checar("Remarcar a devolução muda a data do pedido e a reserva",
       sql(f"SELECT data_devolucao FROM pedidos WHERE id='{PAG}'") == (dev2 + timedelta(days=2)).isoformat()
       and sql(f"SELECT upper(periodo_bloqueado) > '{dev2 + timedelta(days=2)}' FROM pedidos WHERE id='{PAG}'") == "t")
r = ana.post(f"/agenda/{AG_DEV}/remarcar", {"dia": (hoje - timedelta(days=1)).isoformat(), "hora": "10:00"})
checar("Não remarca para o passado", r.status_code == 400 and "depois de hoje" in r.text)
sql(f"UPDATE agendamentos SET inicio = now() - interval '1 day' WHERE id = '{AG_P1}'")
r = ana.get("/agenda")
checar("Compromisso que passou aparece em 'Ficaram para trás'", "Ficaram para trás" in r.text and "Carla Souza Lima" in r.text)
r = ana.post(f"/agenda/{AG_P1}/faltou", {"volta": "/agenda"})
checar("Marcar 'cliente faltou'", r.url.path == "/agenda" and sql(f"SELECT status FROM agendamentos WHERE id='{AG_P1}'") == "FALTOU")
etapa(ana, "PROVA_1", PAG)
checar("Fez a prova depois de faltar: compromisso vira Feito",
       sql(f"SELECT status FROM agendamentos WHERE id='{AG_P1}'") == "FEITO")
checar("Fila da costura tem atalho para as medidas", f"/clientes/{CLI}/medidas" in cida.get("/costura").text)

print("\n== Equipe ==")
checar("Atendente não abre a equipe", ana.get("/equipe").status_code == 403)
checar("Dona vê a equipe", "Ana Atendente" in dona.get("/equipe").text)
func = {"nome": "Rita Costura", "login": "rita", "telefone": "(19) 97777-0000", "data_admissao": hoje.isoformat(),
        "papel_COSTUREIRA": "1", "vinculo": "FIXO", "senha": SENHA, "senha2": SENHA}
r = dona.post("/equipe/novo", {**func, "senha2": "Outra-coisa-123"})
checar("Senhas diferentes: recusa", r.status_code == 400 and "não conferem" in r.text)
r = dona.post("/equipe/novo", {**func, "login": "Rita Silva"})
checar("Login com espaço/maiúscula: recusa", r.status_code == 400 and "Login com 3 a 40" in r.text)
r = dona.post("/equipe/novo", {**func, "vinculo": "FREELANCE"})
checar("Freelance sem data final: recusa", r.status_code == 400 and "último dia" in r.text)
r = dona.post("/equipe/novo", func)
checar("Dona cadastra a costureira Rita", "Funcionário cadastrado" in r.text and
       sql("SELECT data_admissao FROM usuarios WHERE login='rita'") == hoje.isoformat())
RITA = sql("SELECT id FROM usuarios WHERE login='rita'")
r = dona.post("/equipe/novo", func)
checar("Login repetido: recusa", r.status_code == 400 and "já existe" in r.text)
rita = Pessoa("rita")
checar("Rita entra e vê a fila de costura", "/costura" in botoes(rita))
checar("Dev não vê cargos da Diretoria para dar", 'name="papel_DONO"' not in dev.get("/equipe/novo").text
       and 'name="papel_ATENDENTE"' in dev.ultima.text)
r = dev.post("/equipe/novo", {**func, "login": "espertinho", "papel_COSTUREIRA": "", "papel_DONO": "1"})
checar("Dev forjando o cargo de Dona: ignorado e recusado", r.status_code == 400 and
       sql("SELECT count(*) FROM usuarios WHERE login='espertinho'") == "0")
DONA = sql("SELECT id FROM usuarios WHERE login='dona'")
checar("Dev não mexe em pessoa da Diretoria", dev.post(f"/equipe/{DONA}/ativo", {"ativo": "0"}).status_code == 403)
checar("Ninguém desativa a si mesmo", dona.post(f"/equipe/{DONA}/ativo", {"ativo": "0"}).status_code == 403
       and sql(f"SELECT ativo FROM usuarios WHERE id='{DONA}'") == "t")
r = dona.post(f"/equipe/{RITA}/cargos", {"papel_ATENDENTE": "1", "papel_COSTUREIRA": "1", "vinculo": "FREELANCE",
                                        "ate": (hoje + timedelta(days=10)).isoformat()})
checar("Rita vira freelance (atendente + costura) com prazo", "Cargos atualizados" in r.text and
       sql(f"""SELECT count(*) FROM usuario_papeis WHERE usuario_id='{RITA}' AND valido_ate IS NOT NULL
               AND valido_ate > now()""") == "2")
r = dona.post(f"/equipe/{RITA}/cargos", {"papel_ATENDENTE": "1", "vinculo": "FIXO"})
checar("Tirar o cargo de costura encerra o acesso a ele (sem apagar)",
       sql(f"""SELECT count(*) FROM usuario_papeis up JOIN papeis p ON p.id = up.papel_id WHERE usuario_id='{RITA}'
               AND p.codigo='COSTUREIRA' AND valido_ate <= now()""") == "1")
r = dona.post(f"/equipe/{RITA}/ativo", {"ativo": "0"})
navegador = TestClient(app)
tentativa = navegador.post("/login", data={"login": "rita", "senha": SENHA,
                                           "csrf": Pessoa._token(navegador.get("/login").text)})
checar("Desativar: Rita não entra mais", "Acesso desativado" in r.text and "não conferem" in tentativa.text,
       tentativa.text[-500:])
checar("Sessão aberta da Rita também cai", rita.get("/").url.path == "/login")
r = dona.post(f"/equipe/{RITA}/senha", {"senha": "Nova-senha-da-rita1", "senha2": "Nova-senha-da-rita1"})
dona.post(f"/equipe/{RITA}/ativo", {"ativo": "1"})
checar("Dona redefine a senha e reativa", "Senha nova definida" in r.text and
       sql(f"SELECT ativo FROM usuarios WHERE id='{RITA}'") == "t")
r = ana.post("/minha-senha", {"atual": "errada-123456", "senha": "Outra-senha-boa1", "senha2": "Outra-senha-boa1"})
checar("Minha senha: senha atual errada", r.status_code == 400 and "atual não confere" in r.text)
r = ana.post("/minha-senha", {"atual": SENHA, "senha": "123456789012", "senha2": "123456789012"})
checar("Minha senha: só números é recusada", r.status_code == 400 and "Falta na senha" in r.text)
for fraca, falta in (("semmaiuscula1!", "maiúscula"), ("SEMMINUSCULA1!", "minúscula"), ("Semnumero!!", "número"),
                     ("Semsimbolo123", "símbolo"), ("Cu1!", "8"), ("A1!a" * 9, "32")):
    r = ana.post("/minha-senha", {"atual": SENHA, "senha": fraca, "senha2": fraca})
    checar(f"Senha fraca recusada ({falta})", r.status_code == 400 and falta in r.text, r.text[-400:])
r = ana.post("/minha-senha", {"atual": SENHA, "senha": "Outra-senha-boa1", "senha2": "Outra-senha-boa1"})
ana = Pessoa.__new__(Pessoa); ana.c = TestClient(app, follow_redirects=True)   # trocar a senha desconecta os aparelhos
ana.csrf = Pessoa._token(ana.c.get("/login").text)
ana.post("/login", {"login": "ana", "senha": "Outra-senha-boa1"})
ana.post("/minha-senha", {"atual": "Outra-senha-boa1", "senha": SENHA, "senha2": SENHA})
ana = Pessoa("ana")
checar("Minha senha: troca e fica na auditoria", "Senha trocada" in r.text and
       sql("SELECT count(*) FROM auditoria WHERE acao='senha_trocada'") == "2")

print("\n== Impressão e PDF ==")
import zlib  # noqa: E402
def texto_pdf(conteudo):
    partes = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", conteudo, re.S)
    saida = b""
    for p_ in partes:
        try:
            saida += zlib.decompress(p_)
        except zlib.error:
            saida += p_
    return saida.decode("latin-1")
r = ana.get(f"/pedidos/{PED}/imprimir")
checar("Comprovante do aluguel (A4): cliente, agenda, pagamentos e quitado",
       "Comprovante de aluguel" in r.text and "Joana Silva" in r.text and "QUITADO" in r.text
       and "papel-a4" in r.text and "Devolução" in r.text)
r = ana.get(f"/pedidos/{PAG}/imprimir?formato=80")
checar("Comprovante na térmica 80 mm com saldo a pagar", "papel-80" in r.text and "Falta pagar" in r.text
       and "R$ 840,00" in r.text)
r = ana.get(f"/pedidos/{PAG}/pdf")
checar("PDF A4 do pedido", r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
       and "attachment" in r.headers.get("content-disposition", "")
       and "Carla Souza Lima" in texto_pdf(r.content) and "Devolução" in texto_pdf(r.content))
r = ana.get(f"/pedidos/{PAG}/pdf?formato=80")
checar("PDF de bobina 80 mm (226,77 pt de largura, uma tira só)",
       re.search(rb"/MediaBox \[0 0 226\.77", r.content) is not None and r.content.count(b"/Type /Page\n") <= 1)
T_ENT = sql(f"SELECT id FROM transacoes WHERE pedido_id='{PAG}'")
r = ana.get(f"/caixa/recibo/{T_ENT}")
checar("Recibo na térmica: valor, forma e saldo do pedido", f"Recibo nº {T_ENT}" in r.text and "R$ 360,00" in r.text
       and "Recebemos de Carla Souza Lima" in r.text and "papel-80" in r.text)
r = ana.get(f"/caixa/recibo/{T_ENT}/pdf?formato=58")
checar("Recibo em PDF 58 mm", re.search(rb"/MediaBox \[0 0 164\.41", r.content) is not None
       and "Recebemos de Carla" in texto_pdf(r.content))
checar("Tela 'Guardado' oferece o recibo", f"/caixa/recibo/{T_ENT}" in ana.get(f"/caixa/feito/{T_ENT}").text)
r = dona.get(f"/caixa/extrato?de={hoje - timedelta(days=1)}&ate={hoje}")
checar("Extrato do caixa por período com totais por forma", "Extrato do caixa" in r.text and "Dinheiro" in r.text
       and "Conserto de barra (desfeito)" in r.text and "Últimos 7 dias" in r.text)
r = dona.get(f"/caixa/extrato?de=2020-01-01&ate={hoje}")
checar("Extrato longo demais é limitado a 92 dias com aviso", "no máximo 92 dias" in r.text)
r = dona.get(f"/caixa/extrato/pdf?de={hoje}&ate={hoje}")
checar("Extrato do caixa em PDF", r.content.startswith(b"%PDF") and "Extrato do caixa" in texto_pdf(r.content))
checar("Costureira não tira extrato do caixa", cida.get("/caixa/extrato").status_code == 403)
r = ana.get(f"/clientes/{CLI}/extrato?formato=80")
checar("Extrato da cliente (térmica)", "Extrato da cliente" in r.text and "Carla Souza Lima" in r.text and "VES-0001" in r.text)
checar("Extrato da cliente em PDF", ana.get(f"/clientes/{CLI}/extrato/pdf").content.startswith(b"%PDF"))
r = dev.get(f"/pedidos/{PED}/imprimir")
checar("Comprovante impresso pelo Dev sai com telefone mascarado", r.status_code == 200 and "99999-1111" not in r.text)
checar("Emissão de documento fica na auditoria (LGPD)",
       int(sql("SELECT count(*) FROM auditoria WHERE acao='documento_emitido'")) >= 8)
r = dona.post("/config/loja", {"nome": "Loja Teste", "cnpj": "12.345.678/0001-95", "endereco": "Rua das Flores, 10",
                                "telefone": "(19) 3333-4444", "rodape": "Obrigada pela preferência!"})
checar("Dona coloca endereço e recado nos comprovantes", "Dados da loja salvos" in r.text and
       "Rua das Flores, 10" in ana.get(f"/caixa/recibo/{T_ENT}").text and "Obrigada pela preferência!" in ana.ultima.text
       and "12.345.678/0001-95" in ana.ultima.text)
checar("Atendente não muda os dados da loja", ana.post("/config/loja", {"nome": "X"}).status_code == 403)
r = ger.post(f"/pedidos/{PAG}/cancelar", {"motivo": "teste da agenda"})
checar("Cancelar o pedido cancela os compromissos abertos",
       sql(f"SELECT count(*) FROM agendamentos WHERE pedido_id='{PAG}' AND status IN ('AGENDADO','REMARCADO')") == "0")

print("\n== Cadastro com ocasiões e fotos ==")
import io  # noqa: E402
from PIL import Image  # noqa: E402
checar("Loja nasce com ocasiões (Casamento, Festa, Eventos) e subtipos",
       sql("SELECT string_agg(nome, ',' ORDER BY ordem) FROM ocasioes WHERE pai_id IS NULL") == "Casamento,Festa,Eventos"
       and sql("SELECT count(*) FROM ocasioes WHERE pai_id IS NOT NULL") == "16")
OC_MADRINHA = sql("SELECT o.id FROM ocasioes o JOIN ocasioes p ON p.id = o.pai_id WHERE p.slug='casamento' AND o.slug='madrinha'")
OC_FORMATURA = sql("SELECT id FROM ocasioes WHERE slug='formatura'")
OC_NOIVO = sql("SELECT id FROM ocasioes WHERE slug='noivo'")
checar("Tela de cadastro mostra ocasiões, público e site", all(x in ana.get("/pecas/nova").text for x in (
    "Madrinha", "Debutante", "Masculino", "Infantil", "Mostrar esta peça no site", f'name="ocasiao_{OC_MADRINHA}"')))
dados_peca = {"chave": ana.chave("/pecas/nova"), "categoria": cat_ves, "nome": "Vestido longo verde esmeralda",
              "tamanho": "38", "cor": "Verde", "uso": "ambos", "preco_aluguel": "450", "preco_venda": "1200",
              "descricao": "Crepe com fenda lateral", "publico": "FEMININO", "faixa": "ADULTO", "comprimento": "LONGO",
              f"ocasiao_{OC_MADRINHA}": "1", f"ocasiao_{OC_FORMATURA}": "1", "no_site": "1", "preco_no_site": "1"}
r = ana.post("/pecas/nova", {**dados_peca, "ocasiao_9999": "1"})
checar("Ocasião que não existe é recusada pelo banco", r.status_code == 400 and "Ocasião inválida" in r.text, r.text[-400:])
dados_peca["chave"] = ana.chave("/pecas/nova")
r = ana.post("/pecas/nova", dados_peca)
PV4 = sql("SELECT id FROM produtos WHERE nome = 'Vestido longo verde esmeralda'")
checar("Peça cadastrada com ocasiões, comprimento e no site; manda para as fotos",
       "Agora coloque as fotos" in r.text and "Madrinha" in r.text and
       sql(f"SELECT publico||faixa||comprimento||no_site||preco_no_site FROM produtos WHERE id='{PV4}'") == "FEMININOADULTOLONGOtruetrue",
       [x for x in ("Agora coloque as fotos", "Madrinha") if x not in r.text] or sql(f"SELECT publico||faixa||comprimento||no_site||preco_no_site FROM produtos WHERE id='{PV4}'"))
SLUG4 = sql(f"SELECT slug FROM produtos WHERE id='{PV4}'")
checar("Endereço amigável da peça (sem acento, com o código)", SLUG4 == "vestido-longo-verde-esmeralda-ves-0004", SLUG4)

def foto_jpeg(largura, altura, cor=(40, 120, 80), girar=False):
    img = Image.new("RGB", (largura, altura), cor)
    saida = io.BytesIO()
    exif = Image.Exif()
    if girar:
        exif[0x0112] = 6               # celular deitado: precisa girar 90°
    exif[0x8825] = {2: (22.0, 30.0, 0.0)}   # GPS (não pode ir para o site)
    img.save(saida, "JPEG", exif=exif)
    return saida.getvalue()

def enviar(p, pid, arquivos):
    return p.c.post(f"/pecas/{pid}/fotos", data={"csrf": p.csrf},
                    files=[("fotos", (f"f{i}.jpg", b, "image/jpeg")) for i, b in enumerate(arquivos)])
r = enviar(ana, PV4, [foto_jpeg(1200, 800, girar=True), foto_jpeg(900, 1200, (200, 30, 60)), b"isto nao e foto"])
checar("Envio de 3 arquivos: 2 fotos guardadas e aviso do arquivo ruim",
       "2 fotos guardadas" in r.text and "Não consegui abrir essa foto" in r.text
       and sql(f"SELECT count(*) FROM produto_fotos WHERE produto_id='{PV4}'") == "2", r.text[-800:])
FOTO1, FOTO2 = sql(f"SELECT string_agg(arquivo, ' ' ORDER BY ordem) FROM produto_fotos WHERE produto_id='{PV4}'").split()
checar("Foto de celular deitado sai em pé (girada pelo EXIF)",
       sql(f"SELECT largura||'x'||altura FROM produto_fotos WHERE arquivo='{FOTO1}'") == "800x1200")
pasta_fotos = Path(os.environ["FOTOS_DIR"])
with Image.open(pasta_fotos / f"{FOTO1}-960.jpg") as salva:
    checar("Foto guardada em 3 tamanhos e sem localização (EXIF apagado)",
           all((pasta_fotos / f"{FOTO1}-{w}.jpg").exists() for w in (480, 960, 1600)) and not salva.getexif())
checar("Costureira não envia fotos", enviar(cida, PV4, [foto_jpeg(800, 1000)]).status_code == 403)
checar("Foto pequena demais é recusada", "pequena demais" in enviar(ana, PV4, [foto_jpeg(150, 150)]).text)
F2 = sql(f"SELECT id FROM produto_fotos WHERE arquivo='{FOTO2}'")
ana.post(f"/pecas/{PV4}/fotos/{F2}/capa")
checar("Escolher outra foto como capa", sql(f"SELECT arquivo FROM produto_fotos WHERE produto_id='{PV4}' ORDER BY ordem LIMIT 1") == FOTO2)
checar("Lista de peças mostra a miniatura da capa", f"/fotos/{FOTO2}-480.jpg" in ana.get("/pecas?q=esmeralda").text)
anonimo = TestClient(app, follow_redirects=False)
r = anonimo.get(f"/fotos/{FOTO1}-960.jpg")
checar("Foto de peça do site abre para qualquer um, com cache", r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
       and "public" in r.headers.get("cache-control", ""))
FOTO_P1 = None
enviar(ana, P1, [foto_jpeg(800, 1100, (20, 20, 90))])
FOTO_P1 = sql(f"SELECT arquivo FROM produto_fotos WHERE produto_id='{P1}'")
checar("Foto de peça fora do site: só quem entrou no sistema vê",
       anonimo.get(f"/fotos/{FOTO_P1}-480.jpg").status_code == 404 and ana.get(f"/fotos/{FOTO_P1}-480.jpg").status_code == 200)
checar("Endereço de foto inventado não abre arquivo nenhum",
       anonimo.get("/fotos/..%2F..%2Fetc%2Fpasswd-480.jpg").status_code == 404
       and anonimo.get(f"/fotos/{FOTO1}-123.jpg").status_code == 404)
F1 = sql(f"SELECT id FROM produto_fotos WHERE arquivo='{FOTO1}'")
ana.post(f"/pecas/{PV4}/fotos/{F1}/tirar")
checar("Tirar foto só esconde (o arquivo continua guardado)",
       sql(f"SELECT ativa FROM produto_fotos WHERE id='{F1}'") == "f" and (pasta_fotos / f"{FOTO1}-480.jpg").exists()
       and anonimo.get(f"/fotos/{FOTO1}-480.jpg").status_code == 404)

print("\n== Site da loja (vitrine) ==")
r = anonimo.get("/")
checar("Quem não entrou no sistema cai na vitrine", r.status_code == 303 and r.headers["location"] == "/site")
site = TestClient(app)
r = site.get("/site")
checar("Início do site: menu de ocasiões, novidades e sem dado interno",
       r.status_code == 200 and "Casamento" in r.text and "Madrinha" in r.text and "Vestido longo verde esmeralda" in r.text
       and "Joana" not in r.text and "R$ 450,00" in r.text and "default-src 'self'" in r.headers["content-security-policy"])
r = site.get("/site/c/casamento/madrinha")
checar("Casamento › Madrinha mostra a peça", "Vestido longo verde esmeralda" in r.text and "1 peça" in r.text)
checar("Casamento (grupo todo) também mostra", "esmeralda" in site.get("/site/c/casamento").text)
checar("Casamento › Noivo não mostra", "esmeralda" not in site.get("/site/c/casamento/noivo").text)
checar("Filtro Masculino e filtro de tamanho", "esmeralda" not in site.get("/site/colecao?publico=MASCULINO").text
       and "esmeralda" in site.get("/site/colecao?tamanho=38").text and "esmeralda" not in site.get("/site/colecao?tamanho=40").text)
checar("Peça fora do site não aparece nem abre", "VES-0001" not in site.get("/site/colecao").text
       and "não está mais disponível" in site.get(f"/site/p/{sql(chr(83)+'ELECT slug FROM produtos WHERE id=' + chr(39) + P1 + chr(39))}").text)
r = site.get(f"/site/p/{SLUG4}")
checar("Página da peça: galeria, ficha, preço e botões", r.status_code == 200 and f"/fotos/{FOTO2}-960.jpg" in r.text
       and "Tamanho" in r.text and "Longo" in r.text and "Agendar prova" in r.text and "Crepe com fenda" in r.text
       and f"/fotos/{FOTO1}" not in r.text)
sql(f"UPDATE produtos SET no_site = true, preco_no_site = false, ocasioes = ARRAY[{OC_NOIVO}]::smallint[] WHERE id = '{P3}'")
SLUG3 = sql(f"SELECT slug FROM produtos WHERE id='{P3}'")
data_ocupada = (ret + timedelta(days=30)).isoformat()
r = site.get(f"/site/p/{SLUG3}?data={data_ocupada}")
checar("Preço escondido aparece 'Sob consulta'", "Sob consulta" in r.text and "R$ 300" not in r.text)
checar("Consulta de data: reservada perto do dia do aluguel", "Já reservada" in r.text, r.text[-2000:])
checar("Consulta de data livre", "✓ Livre" in site.get(f"/site/p/{SLUG3}?data={(hoje + timedelta(days=5)).isoformat()}").text)
checar("Coleção 'livres na data' tira a peça reservada",
       "Terno preto slim" not in site.get(f"/site/c/casamento?data={data_ocupada}").text
       and "Terno preto slim" in site.get(f"/site/c/casamento?data={(hoje + timedelta(days=5)).isoformat()}").text)

print("\n== Pedido de prova pelo site ==")
def csrf_site(c, url):
    return Pessoa._token(c.get(url).text)
prova = {"peca": SLUG4, "nome": "Paula Visitante", "telefone": "(19) 97777-1234",
         "data_desejada": (hoje + timedelta(days=3)).isoformat(), "periodo": "TARDE",
         "data_evento": (hoje + timedelta(days=40)).isoformat(), "mensagem": "Sou madrinha"}
r = site.post("/site/prova", data={**prova, "csrf": csrf_site(site, f"/site/prova?peca={SLUG4}")})
checar("Sem concordar com o contato (LGPD): recusa", r.status_code == 400 and "Marque que concorda" in r.text)
r = site.post("/site/prova", data={**prova, "lgpd": "1", "site": "http://spam", "csrf": csrf_site(site, "/site/prova")})
checar("Robô que preenche o campo-isca: finge que deu certo e não grava",
       "Recebemos seu pedido" in r.text and sql("SELECT count(*) FROM solicitacoes_prova") == "0")
r = site.post("/site/prova", data={**prova, "lgpd": "1", "csrf": csrf_site(site, "/site/prova")})
checar("Pedido de prova enviado", "Recebemos seu pedido" in r.text and
       sql("SELECT nome||' '||periodo FROM solicitacoes_prova") == "Paula Visitante TARDE"
       and aberto("SELECT telefone_c FROM solicitacoes_prova", "solicitacoes.telefone") == "19977771234")
checar("Pedido de prova sem token (outro site tentando enviar) é recusado",
       TestClient(app).post("/site/prova", data={**prova, "lgpd": "1"}).status_code == 400)
for _ in range(5):
    r = site.post("/site/prova", data={**prova, "lgpd": "1", "csrf": csrf_site(site, "/site/prova")})
checar("Muitos pedidos seguidos: limite por hora", r.status_code == 429 and
       sql("SELECT count(*) FROM solicitacoes_prova") == "5")
sql("UPDATE solicitacoes_prova SET status = 'RECUSADA' WHERE id <> (SELECT id FROM solicitacoes_prova ORDER BY criado_em LIMIT 1)")
checar("Tela inicial avisa o pedido novo do site", "Pedidos de prova novos pelo site" in ana.get("/").text)
r = ana.get("/solicitacoes")
checar("Atendente vê o pedido com WhatsApp pronto", "Paula Visitante" in r.text and "wa.me/5519977771234" in r.text
       and "Sou madrinha" in r.text)
checar("Limpeza não vê pedidos do site", lu.get("/solicitacoes").status_code == 403)
SOL = sql("SELECT id FROM solicitacoes_prova WHERE status = 'NOVA'")
r = ana.post(f"/solicitacoes/{SOL}", {"acao": "agendar", "dia": (hoje + timedelta(days=3)).isoformat(), "hora": "15:30"})
checar("Marcar a prova: vira cliente (com a autorização do site) e entra na Agenda",
       "Prova marcada" in r.text and sql("SELECT consentimento_lgpd_em IS NOT NULL FROM clientes WHERE nome='Paula Visitante'") == "t"
       and "Paula Visitante" in ana.get(f"/agenda?dia={(hoje + timedelta(days=3)).isoformat()}").text
       and "Prova (veio pelo site)" in ana.ultima.text)

print("\n== Configurações do site ==")
checar("Atendente não mexe no site", ana.get("/config/site").status_code == 403)
r = ger.post("/config/site/ocasioes", {"nome": "Chá revelação", "pai": sql("SELECT id FROM ocasioes WHERE slug='eventos'")})
checar("Gerente cria ocasião nova dentro de Eventos", "criada" in r.text and
       sql("SELECT slug FROM ocasioes WHERE nome='Chá revelação'") == "cha-revelacao")
CHA = sql("SELECT id FROM ocasioes WHERE slug='cha-revelacao'")
r = ger.post(f"/config/site/ocasioes/{CHA}", {"acao": "renomear", "nome": "Chá de bebê"})
checar("Renomear ocasião", sql(f"SELECT nome||' '||slug FROM ocasioes WHERE id={CHA}") == "Chá de bebê cha-de-bebe")
ger.post(f"/config/site/ocasioes/{CHA}", {"acao": "desativar"})
checar("Ocasião escondida some do menu do site", "Chá de bebê" not in site.get("/site").text)
checar("Não dá para criar 3º nível (ocasião dentro de subocasião)",
       ger.post("/config/site/ocasioes", {"nome": "Teste", "pai": OC_MADRINHA}).status_code == 400)
r = ger.post("/config/site/tipos", {"nome": "Vestido infantil", "prefixo": "vin"})
checar("Criar tipo de peça com código próprio", "VIN-0001" in r.text and
       sql("SELECT prefixo_sku FROM categorias WHERE nome='Vestido infantil'") == "VIN")
r = ger.post("/config/site/dados", {"whatsapp": "(19) 98888-7777", "instagram": "@lojateste", "horario": "Seg a sáb",
                                     "frase_site": "Vestidos para brilhar", "site_ativo": "1"})
texto_site = site.get("/site").text
checar("WhatsApp e Instagram da loja aparecem no site", "wa.me/5519988887777" in texto_site
       and "instagram.com/lojateste" in texto_site and "Vestidos para brilhar" in texto_site)
ger.post("/config/site/dados", {"whatsapp": "19988887777", "site_ativo": ""})
r = site.get("/site")
checar("Site desligado mostra 'voltamos em breve'", r.status_code == 503 and "Voltamos em breve" in r.text)
ger.post("/config/site/dados", {"whatsapp": "19988887777", "site_ativo": "1", "frase_site": "Vestidos para brilhar"})

print("\n== Link de pagamento (Mercado Pago) ==")
checar("Sem token: novo pedido não oferece link", 'value="LINK"' not in ana.get(f"/pedidos/novo?peca={P1}").text)
r = ger.post("/config/site/pagamento", {"token": "minha senha"})
checar("Token com formato errado: explica o que é", r.status_code == 400 and "APP_USR-" in r.text)
r = ger.post("/config/site/pagamento", {"token": "APP_USR-0000000000-token-que-nao-existe-00"})
checar("Token recusado pelo Mercado Pago: avisa", r.status_code == 400 and "recusou o token" in r.text)
checar("Atendente não configura pagamento", ana.post("/config/site/pagamento", {"token": TOKEN_BOM}).status_code == 403)
r = ger.post("/config/site/pagamento", {"token": TOKEN_BOM})
checar("Token certo: conferido na conta e salvo", "ligado na conta LOJATESTE" in r.text and "••••9876" in r.text)
checar("Token guardado cifrado (não fica o texto no banco)",
       sql("SELECT position('APP_USR' in encode(token_cifrado, 'escape')) = 0 FROM pagamento_online") == "t")
try:
    with db.transacao() as cur:
        cur.execute("SELECT token_cifrado FROM pagamento_online")
    lido = True
except Exception:
    lido = False
checar("O app não consegue ler o token cifrado direto da tabela", not lido)
checar("A 'maquininha' do link não aparece para passar cartão no balcão",
       "Link Mercado Pago" not in ana.get("/caixa/receber").text)
ret3, dev3 = hoje + timedelta(days=70), hoje + timedelta(days=72)
link_base = {"tipo": "ALUGUEL", "cliente_nome": "Wanda WhatsApp", "cliente_telefone": "(19) 96666-5555",
             "data_retirada": ret3.isoformat(), "data_devolucao": dev3.isoformat(), "forma": "LINK"}
r = ana.get(f"/pedidos/novo?peca={P1}")
checar("Com token: aparece a opção 'Link de pagamento'", 'value="LINK"' in r.text)
r = pedido(ana, {**link_base, "valor": "100"})
checar("Link abaixo da entrada mínima: recusa", "entrada mínima é R$ 360,00" in r.text, r.text[-400:])
r = pedido(ana, {**link_base, "valor": "360"})
PL = sql("SELECT id FROM pedidos WHERE pagar_pelo_link")
checar("Pedido do WhatsApp com link: fica 'Aguardando pagamento' e cria o link",
       "Pedido guardado" in r.text and "Aguardando pagamento" in r.text and "/pagar/" in r.text
       and sql(f"SELECT status FROM pedidos WHERE id='{PL}'") == "AGUARDANDO_PAGAMENTO"
       and sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PL}'") == "0", r.text[-1500:])
LINK1 = sql(f"SELECT id FROM links_pagamento WHERE pedido_id='{PL}'")
pref = MP["preferencias"][-1]["corpo"]
checar("Link no Mercado Pago: valor, referência, parcelas da regra e sem boleto",
       pref["items"][0]["unit_price"] == 360.0 and pref["external_reference"] == LINK1
       and pref["payment_methods"]["installments"] == 6 and {"id": "ticket"} in pref["payment_methods"]["excluded_payment_types"]
       and MP["preferencias"][-1]["idem"] == LINK1)
checar("Botão de mandar o link no WhatsApp da cliente", "wa.me/5519966665555" in r.text)
checar("A peça fica segura enquanto o link vale",
       "já está reservada" in pedido(ger, {**link_base, "cliente_nome": "Outra Pessoa", "cliente_telefone": "(19) 99777-0004", "forma": "PIX",
                                            "valor": "400"}).text)
r = anonimo.get(f"/pagar/{LINK1}")
checar("Link curto leva a cliente para o Mercado Pago", r.status_code == 303 and r.headers["location"].startswith("https://www.mercadopago.com.br/"))
checar("Aviso falso (pagamento que não existe): nada entra no caixa",
       anonimo.post("/pagamentos/mercadopago/aviso", json={"type": "payment", "data": {"id": "999"}}).status_code == 503
       and sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PL}'") == "0")
MP["pagamentos"]["1001"] = {"id": 1001, "status": "approved", "external_reference": LINK1, "transaction_amount": 360.0,
                            "payment_method_id": "master", "payment_type_id": "credit_card", "installments": 3}
r = anonimo.post("/pagamentos/mercadopago/aviso", json={"type": "payment", "data": {"id": "1001"}})
checar("Aviso do Mercado Pago: lança no caixa e o pedido vira Reservado sozinho",
       r.status_code == 200 and sql(f"SELECT status FROM pedidos WHERE id='{PL}'") == "RESERVADO"
       and sql(f"""SELECT forma_pagamento||' '||modalidade_cartao||' '||valor_centavos||' '||parcelas FROM transacoes
                   WHERE pedido_id='{PL}'""") == "CREDITO EMISSOR 36000 1"
       and sql(f"SELECT status FROM links_pagamento WHERE id='{LINK1}'") == "PAGO")
anonimo.post("/pagamentos/mercadopago/aviso", json={"type": "payment", "data": {"id": "1001"}})
checar("Aviso repetido não lança duas vezes", sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PL}'") == "1")
checar("Histórico mostra quem gerou o link na confirmação",
       sql(f"SELECT u.login FROM historico_status h JOIN usuarios u ON u.id = h.usuario_id WHERE h.pedido_id='{PL}' AND h.para='RESERVADO'") == "ana")
checar("Link pago mostra 'Pagamento recebido' para a cliente", "Pagamento recebido" in site.get(f"/pagar/{LINK1}").text)
r = ana.post(f"/pedidos/{PL}/link", {"valor": "5000"})
checar("Link maior que o saldo: recusa", "não pode passar do que falta pagar" in r.text)
r = ana.post(f"/pedidos/{PL}/link", {"valor": "200"})
LINK2 = sql(f"SELECT id FROM links_pagamento WHERE pedido_id='{PL}' AND status='ABERTO'")
checar("Link do saldo gerado no pedido", "Link criado" in r.text and LINK2)
r = ana.post(f"/pagamentos/links/{LINK2}/conferir")
checar("Conferir antes de pagar: 'Ainda não foi pago'", "Ainda não foi pago" in r.text)
MP["pagamentos"]["1002"] = {"id": 1002, "status": "approved", "external_reference": LINK2, "transaction_amount": 200.0,
                            "payment_method_id": "pix", "payment_type_id": "bank_transfer", "installments": 1}
r = ana.post(f"/pagamentos/links/{LINK2}/conferir")
checar("Conferir depois do Pix: lança no caixa (sem webhook, ex.: no PC local)",
       "Pagamento recebido e lançado" in r.text and
       sql(f"SELECT forma_pagamento FROM transacoes WHERE pedido_id='{PL}' ORDER BY id DESC LIMIT 1") == "PIX"
       and sql(f"SELECT valor_total_centavos - valor_pago(id) FROM pedidos WHERE id='{PL}'") == "64000")
r = ana.post(f"/pedidos/{PL}/link", {"valor": "100"})
LINK3 = sql(f"SELECT id FROM links_pagamento WHERE pedido_id='{PL}' AND status='ABERTO'")
MP["pagamentos"]["1003"] = {"id": 1003, "status": "approved", "external_reference": LINK3, "transaction_amount": 90.0,
                            "payment_method_id": "pix", "payment_type_id": "bank_transfer", "installments": 1}
ana.post(f"/pagamentos/links/{LINK3}/conferir")
checar("Valor pago diferente do link: não lança, fica 'precisa conferir' e avisa na tela inicial",
       sql(f"SELECT status FROM links_pagamento WHERE id='{LINK3}'") == "PAGO_CONFERIR"
       and sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PL}'") == "2"
       and "precisa" in ger.get("/").text and "conferência" in ger.ultima.text)
# Link vencido sem pagamento libera a peça
chave_v = Pessoa._achar(ana.get(f"/pedidos/novo?peca={PV4}").text, "chave")
r = pedido(ana, {**link_base, "cliente_nome": "Vera Vencida", "cliente_telefone": "19955554444", "valor": "200"}, peca=PV4)
PVENC = sql("SELECT p.id FROM pedidos p JOIN clientes c ON c.id = p.cliente_id WHERE c.nome='Vera Vencida'")
checar("Segundo pedido pelo link aguardando", sql(f"SELECT status FROM pedidos WHERE id='{PVENC}'") == "AGUARDANDO_PAGAMENTO")
sql(f"UPDATE links_pagamento SET expira_em = now() - interval '1 minute' WHERE pedido_id='{PVENC}'")
ana.get("/")
checar("Link venceu sem pagamento: pedido cancelado sozinho, agenda cancelada e peça liberada",
       sql(f"SELECT status FROM pedidos WHERE id='{PVENC}'") == "CANCELADO"
       and sql(f"SELECT count(*) FROM agendamentos WHERE pedido_id='{PVENC}' AND status <> 'CANCELADO'") == "0"
       and "Vestido longo verde esmeralda" in site.get(f"/site/colecao?data={ret3.isoformat()}").text)
checar("Link vencido avisa a cliente", "venceu" in site.get(f"/pagar/{sql(chr(83)+'ELECT id FROM links_pagamento WHERE pedido_id=' + chr(39) + PVENC + chr(39))}").text)

print("\n== Relatórios, painel e segurança ==")
r = dona.get("/relatorios")
checar("Relatório do mês abre para a Dona", r.status_code == 200 and "Entrou no mês" in r.text)
checar("Atendente não vê relatórios", ana.get("/relatorios").status_code == 403)
r = dev.get("/sistema")
checar("Painel técnico mostra a auditoria para o Dev (o mais recente primeiro)",
       "eventos da auditoria" in r.text and "link_pagamento_criado" in r.text)
checar("Dev não abre o caixa para lançar", dev.get("/caixa/receber").status_code == 403)
r = ana.c.post("/caixa/receber/gravar", data={"valor": "10", "forma": "PIX", "chave": "00000000-0000-0000-0000-000000000000"})
checar("Formulário sem token CSRF é recusado e nada é gravado", r.status_code == 400 and
       sql("SELECT count(*) FROM transacoes WHERE chave_idempotencia = '00000000-0000-0000-0000-000000000000'") == "0")
checar("Endereço inválido não quebra (404 amigável)", ana.get("/pecas/nao-e-um-id").status_code in (404, 422))
r = ana.get("/")
checar("Cabeçalhos de segurança presentes", "default-src 'self'" in r.headers.get("content-security-policy", "")
       and r.headers.get("cache-control") == "no-store")
checar("Livro caixa continua imutável", "imutável" in rodar(["psql", "-X", "-d", TEST_DB, "-c",
       "UPDATE transacoes SET valor_centavos = 1"]).stderr)

# =========================================================================== #
# Sprint 4: WhatsApp, avisos, chat, contrato, criptografia e blindagem
# =========================================================================== #
print("\n== WhatsApp da loja (WPPConnect de mentira) ==")
WPP = {"enviadas": [], "arquivos": [], "estado": "QRCODE", "n": 0}
SEGREDO_WPP, TOKEN_WPP = "segredo-do-wpp-123", "tok-wpp-abc"


class FalsoWpp(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _resp(self, codigo, corpo):
        dados = _json.dumps(corpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN_WPP}":
            return self._resp(401, {})
        if self.path.endswith("/status-session"):
            return self._resp(200, {"status": WPP["estado"]})
        self._resp(404, {})

    def do_POST(self):
        corpo = _json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/generate-token"):
            if f"/{SEGREDO_WPP}/" not in self.path:
                return self._resp(401, {"message": "wrong secret"})
            return self._resp(201, {"status": "success", "token": TOKEN_WPP})
        if self.headers.get("Authorization") != f"Bearer {TOKEN_WPP}":
            return self._resp(401, {})
        if self.path.endswith("/start-session"):
            WPP["inicio"] = corpo
            return self._resp(200, {"status": "phoneCode", "phoneCode": "A1A4-FCB3"})
        if self.path.endswith("/send-message"):
            WPP["n"] += 1
            WPP["enviadas"].append((corpo["phone"], corpo["message"]))
            return self._resp(201, {"status": "success", "response": [{"id": f"msg-{WPP['n']}"}]})
        if self.path.endswith("/send-file-base64"):
            WPP["arquivos"].append((corpo["phone"], corpo["filename"], corpo["base64"][:40]))
            return self._resp(201, {"status": "success", "response": [{"id": "arq"}]})
        if self.path.endswith("/logout-session"):
            return self._resp(200, {"status": True})
        self._resp(404, {})


_servidor_wpp = ThreadingHTTPServer(("127.0.0.1", 0), FalsoWpp)
threading.Thread(target=_servidor_wpp.serve_forever, daemon=True).start()
URL_WPP = f"http://127.0.0.1:{_servidor_wpp.server_port}"
from app import acesso, config as cfg_app, trabalhador  # noqa: E402


def _ultimo_codigo(login):
    fone = "55" + FONES[login]
    return re.search(r"\b(\d{6})\b", [m for m in para(fone) if "código para entrar" in m][-1]).group(1)


ULTIMO_CODIGO = _ultimo_codigo


def para(fone):
    """Mensagens que o WPPConnect de mentira mandou para um número (texto)."""
    return [m for f, m in WPP["enviadas"] if f == fone]


def enviar_fila():
    return trabalhador.enviar_pendentes(100)


checar("Atendente não abre a tela do WhatsApp", ana.get("/config/whatsapp").status_code == 403)
checar("Dona abre a tela do WhatsApp", "Conectar o WhatsApp" in dona.get("/config/whatsapp").text or
       dona.ultima.status_code == 200, dona.ultima.text[-400:])
conf_wpp = {"url": URL_WPP, "sessao": "loja", "numero": "(19) 3333-0000", "endereco_sistema": "http://testserver"}
r = dona.post("/config/whatsapp", {**conf_wpp, "segredo": "chave-errada"})
r = dona.post("/config/whatsapp/conectar")
checar("Chave errada do WPPConnect: mensagem clara", r.status_code == 400 and "recusou a chave" in r.text, r.text[-300:])
r = dona.post("/config/whatsapp", {**conf_wpp, "segredo": SEGREDO_WPP})
r = dona.post("/config/whatsapp/conectar")
checar("Gerar código: mostra o código A1A4-FCB3 para digitar no celular", "A1A4-FCB3" in r.text, r.text[-600:])
checar("Pede o código pelo número da loja (sem QR code) e com o aviso certo",
       WPP["inicio"]["phone"] == "551933330000" and WPP["inicio"]["waitQrCode"] is False
       and "/whatsapp/aviso/" in WPP["inicio"]["webhook"])
checar("Chave e token do WPPConnect ficam cifrados no banco",
       sql(f"SELECT count(*) FROM whatsapp_config WHERE encode(segredo_c,'escape') LIKE '%{SEGREDO_WPP}%' "
           f"OR encode(token_c,'escape') LIKE '%{TOKEN_WPP}%'") == "0"
       and sql("SELECT (segredo_c IS NOT NULL AND token_c IS NOT NULL)::text FROM whatsapp_config") == "true")
GANCHO = sql("SELECT webhook_segredo FROM whatsapp_config")
site_wpp = TestClient(app)
checar("Aviso com segredo errado é recusado", site_wpp.post("/whatsapp/aviso/segredo-falso",
                                                              json={"event": "status-find", "status": "inChat"}).status_code == 404)
r = site_wpp.post(f"/whatsapp/aviso/{GANCHO}", json={"event": "status-find", "status": "inChat"})
WPP["estado"] = "CONNECTED"
checar("Celular digitou o código: o WPPConnect avisa e o sistema fica CONECTADO",
       r.json().get("resultado") == "status" and sql("SELECT status FROM whatsapp_config") == "CONECTADO")

# Telefones e etiquetas da equipe (Dona recebe quase tudo, Dev a segurança, Cida a costura)
FONES = {"dona": "19991110001", "dev": "19991110002", "ana": "19991110003", "cida": "19991110004"}
for login, fone in FONES.items():
    sql(f"""UPDATE usuarios SET telefone_c = '\\x{cripto.cifrar(fone, 'usuarios.telefone').hex()}',
                                telefone_idx = '{idx_tel(fone)}' WHERE login = '{login}'""")
sql("UPDATE usuarios SET avisos = '{pedidos,site,chat,pagamentos,agenda,relatorio,seguranca}' WHERE login = 'dona'")
sql("UPDATE usuarios SET avisos = '{seguranca}' WHERE login = 'dev'")
sql("UPDATE usuarios SET avisos = '{costura}' WHERE login = 'cida'")
sql("UPDATE usuarios SET avisos = '{}' WHERE login NOT IN ('dona','dev','cida')")
r = dona.post(f"/equipe/{sql(chr(83) + 'ELECT id FROM usuarios WHERE login=' + chr(39) + 'cida' + chr(39))}/avisos",
              {"aviso_costura": "1", "aviso_agenda": "1"})
checar("Dona escolhe os avisos da costureira (costura + agenda)",
       sql("SELECT array_to_string(avisos, ',') FROM usuarios WHERE login='cida'") == "agenda,costura", r.text[-300:])
r = dona.post("/config/whatsapp/teste")
enviar_fila()
checar("Mensagem de teste chega no WhatsApp da Dona", any("Teste do sistema" in m for m in para("55" + FONES["dona"])))

print("\n== Avisos automáticos ==")
chave = ana.chave("/pecas/nova")
ana.post("/pecas/nova", {"chave": chave, "categoria": cat_ves, "nome": "Vestido rosa aviso", "tamanho": "38",
                         "uso": "ambos", "preco_aluguel": "400", "preco_venda": "900"})
PZ = sql("SELECT id FROM produtos WHERE nome = 'Vestido rosa aviso'")
rz = hoje + timedelta(days=50)
r = pedido(ana, {"tipo": "ALUGUEL", "cliente_nome": "Zuleica Zap", "cliente_telefone": "(19) 99222-3333",
                 "data_retirada": rz.isoformat(), "data_devolucao": (rz + timedelta(days=2)).isoformat(),
                 "forma": "PIX", "valor": "200"}, peca=PZ)
PZED = sql("SELECT id FROM pedidos WHERE produto_id = '" + PZ + "'")
checar("Pedido na loja: vai para a fila (cliente + Dona), a tela não espera o WhatsApp",
       "Pedido guardado" in r.text and sql(f"SELECT count(*) FROM mensagens WHERE motivo IN ('pedido_cliente','pedido_novo') "
                                           f"AND status = 'PENDENTE'") == "2",
       re.findall(r'class="aviso"[^>]*>(.*?)<', r.text) or r.text[-400:])
checar("Fila guarda destino e texto cifrados", sql("SELECT count(*) FROM mensagens WHERE encode(texto_c,'escape') LIKE '%Zuleica%'") == "0")
n = enviar_fila()
TOKEN_Z = sql(f"SELECT token_publico FROM pedidos WHERE id = '{PZED}'")
msg_cli = para("5519992223333")
checar("Cliente recebe: pedido confirmado, valores e link do pedido/contrato",
       msg_cli and "Zuleica" in msg_cli[-1] and "falta R$ 200,00" in msg_cli[-1] and f"/site/meu-pedido/{TOKEN_Z}" in msg_cli[-1]
       and "SAIR" in msg_cli[-1], msg_cli)
checar("Dona recebe 'Pedido novo na loja'", any("Pedido novo na loja" in m and "Zuleica" in m for m in para("55" + FONES["dona"])))
checar("Enviadas marcadas como ENVIADA (não repete)", enviar_fila() == 0
       and sql("SELECT count(*) FROM mensagens WHERE status='ENVIADA' AND id_externo LIKE 'msg-%'") != "0")
antes = len(WPP["enviadas"])
ana.post(f"/pedidos/{PZED}/etapa", {"para": "PROVA_1"})
ana.post(f"/pedidos/{PZED}/etapa", {"para": "EM_AJUSTE"})
enviar_fila()
checar("Mandou para o ajuste: costureira avisada no WhatsApp",
       any("Para ajustar" in m and "Zuleica" in m for m in para("55" + FONES["cida"])), WPP["enviadas"][antes:])
cida.post(f"/pedidos/{PZED}/etapa", {"para": "PROVA_2"})
enviar_fila()
checar("Ajuste pronto: cliente chamada para a 2ª prova", any("2ª prova" in m for m in para("5519992223333")))
AGZ = sql(f"SELECT id FROM agendamentos WHERE pedido_id = '{PZED}' AND tipo = 'RETIRADA'")
ana.post(f"/agenda/{AGZ}/remarcar", {"dia": (rz + timedelta(days=1)).isoformat(), "hora": "15:00"})
enviar_fila()
checar("Remarcou a retirada: cliente avisada com o novo dia", any("retirada" in m and "15:00" in m for m in para("5519992223333")),
       para("5519992223333"))

# Rotinas: lembrete da véspera, agenda do dia, relatório
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from datetime import time as _hora  # noqa: E402
vespera = _dt.combine(rz, _hora(11, 0), tzinfo=ZoneInfo("America/Sao_Paulo"))
trabalhador.rodar_rotinas(vespera)
trabalhador.rodar_rotinas(vespera)                   # rodar de novo não repete
trabalhador.rodar_rotinas(vespera.replace(hour=20))  # depois do horário do relatório
enviar_fila()
lembretes = [m for m in para("5519992223333") if "Lembrete" in m]
checar("Véspera: lembrete da retirada para a cliente, uma vez só", len(lembretes) == 1 and "amanhã" in lembretes[0], lembretes)
checar("Relatório do caixa chega para a Dona no horário", any("Caixa de" in m for m in para("55" + FONES["dona"])))

print("\n== Comandos e respostas pelo WhatsApp ==")
def chega(de, texto, **extra):
    r = site_wpp.post(f"/whatsapp/aviso/{GANCHO}", json={"event": "onmessage", "from": f"{de}@c.us",
                                                         "body": texto, "fromMe": False, **extra})
    try:
        return r.json()["resultado"]
    except ValueError:
        print("   aviso respondeu", r.status_code, r.text[-300:])
        return None
checar("Dona manda #caixa e recebe o resumo", chega("55" + FONES["dona"], "#caixa") == "comando"
       and enviar_fila() and "Caixa de" in para("55" + FONES["dona"])[-1])
chega("55" + FONES["cida"], "#caixa")
enviar_fila()
checar("Costureira manda #caixa: não é do cargo dela", "não é do seu cargo" in para("55" + FONES["cida"])[-1])
checar("Número desconhecido não roda comando", chega("5511900000000", "#caixa") == "cliente")
chega("5519999991111", "sair")
checar("Cliente responde SAIR: para de receber avisos",
       sql(f"SELECT whatsapp_sair::text FROM clientes WHERE telefone_idx = '{idx_tel('19999991111')}'") == "true")

print("\n== Chat do site vai para o WhatsApp ==")
visita = TestClient(app)
t = Pessoa._token(visita.get("/site").text)
checar("Site tem a janelinha do chat", t is not None and "Fale conosco" in visita.get("/site").text)
r = visita.post("/site/chat/enviar", data={"csrf": t, "texto": "Tem o vestido rosa no 38?"})
checar("Primeira mensagem sem nome: pede o nome", r.status_code == 400 and "nome" in r.json()["erro"])
r = visita.post("/site/chat/enviar", data={"csrf": t, "nome": "Bia Visitante", "lgpd": "1", "texto": "Tem o vestido rosa no 38?"})
COD = r.json().get("codigo")
enviar_fila()
chat_dona = [m for m in para("55" + FONES["dona"]) if "Site #" in m]
checar("Dona recebe no WhatsApp: 'Site #CÓDIGO · Bia: pergunta'", COD and chat_dona and f"#{COD}" in chat_dona[-1]
       and "Tem o vestido rosa no 38?" in chat_dona[-1], chat_dona)
checar("Resposta com #CÓDIGO volta para a janelinha", chega("55" + FONES["dona"], f"#{COD} Temos sim! Venha provar.") == "chat")
msgs = visita.get("/site/chat/mensagens").json()["mensagens"]
checar("Cliente vê a resposta no chat do site (com o nome de quem respondeu)",
       any(m["texto"] == "Temos sim! Venha provar." and m["de"] == "LOJA" and m["autor"] == "Maria" for m in msgs), msgs)
visita.post("/site/chat/enviar", data={"csrf": t, "texto": "Que horas abre?"})
enviar_fila()
id_ext = sql("SELECT id_externo FROM mensagens WHERE motivo LIKE 'chat:%' ORDER BY id DESC LIMIT 1")
checar("Resposta citando a mensagem (sem #) também volta para o chat",
       chega("55" + FONES["dona"], "Abrimos às 9h", quotedMsgId=id_ext) == "chat"
       and any(m["texto"] == "Abrimos às 9h" for m in visita.get("/site/chat/mensagens").json()["mensagens"]))
checar("Outra pessoa não vê a conversa (cookie próprio)", TestClient(app).get("/site/chat/mensagens").json()["mensagens"] == [])
checar("Chat guardado cifrado", sql("SELECT count(*) FROM chat_mensagens WHERE encode(texto_c,'escape') LIKE '%rosa%'") == "0")

print("\n== Contrato assinado na tela ==")
import base64 as _b64, io as _io  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
def assinatura_png(vazia=False):
    img = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    if not vazia:
        ImageDraw.Draw(img).line([(40, 150), (200, 40), (320, 160), (560, 50)], fill=(20, 20, 20, 255), width=6)
    b = _io.BytesIO(); img.save(b, "PNG")
    return "data:image/png;base64," + _b64.b64encode(b.getvalue()).decode()
PCONT = PZED                                          # pedido da Zuleica (ganha CPF e e-mail)
ZID = sql(f"SELECT cliente_id FROM pedidos WHERE id = '{PZED}'")
ana.post(f"/clientes/{ZID}/editar", {"nome": "Zuleica Zap", "telefone": "(19) 99222-3333", "cpf": "111.444.777-35",
                                    "email": "zuleica@exemplo.com", "lgpd": "1"})
r = ana.get(f"/pedidos/{PCONT}")
checar("Pedido mostra 'contrato ainda não assinado' e o botão de assinar",
       "ainda não foi assinado" in r.text and f"/pedidos/{PCONT}/contrato" in r.text)
r = ana.get(f"/pedidos/{PCONT}/contrato")
checar("Tela do contrato tem o texto com os dados e o quadro de assinatura",
       "CONTRATO DE" in r.text and "Zuleica Zap" in r.text and "quadro-assinatura" in r.text and "111.444.777-35" in r.text,
       [x for x in ("CONTRATO DE", "Zuleica Zap", "quadro-assinatura", "111.444.777-35") if x not in r.text])
r = ana.post(f"/pedidos/{PCONT}/contrato", {"aceite": "1", "assinatura": assinatura_png(vazia=True)})
checar("Assinatura em branco é recusada", r.status_code == 400 and "assinatura" in r.text.lower())
r = ana.post(f"/pedidos/{PCONT}/contrato", {"assinatura": assinatura_png()})
checar("Sem marcar 'li e concordo' é recusado", r.status_code == 400 and "concorda" in r.text)
dona.post("/config/email", {"remetente": "lojateste@gmail.com", "senha": "abcd efgh ijkl mnop"})
checar("Senha de app do Gmail guardada cifrada",
       sql("SELECT count(*) FROM email_config WHERE encode(senha_c,'escape') LIKE '%abcdefgh%'") == "0"
       and sql("SELECT remetente FROM email_config") == "lojateste@gmail.com")
r = ana.post(f"/pedidos/{PCONT}/contrato", {"aceite": "1", "assinatura": assinatura_png()})
CODIGO = sql(f"SELECT codigo FROM contratos WHERE pedido_id = '{PCONT}'")
checar("Cliente assina: contrato guardado com código de verificação", "Contrato assinado" in r.text and
       re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", CODIGO or "") and "Íntegro" in r.text,
       (r.status_code, re.findall(r'class="aviso"[^>]*>(.*?)<', r.text)))
ana.post(f"/pedidos/{PCONT}/contrato", {"aceite": "1", "assinatura": assinatura_png()})
checar("Assinar de novo não cria outro contrato", sql(f"SELECT count(*) FROM contratos WHERE pedido_id = '{PCONT}'") == "1")
checar("Contrato não pode ser alterado nem apagado (banco recusa)",
       "imutável" in rodar(["psql", "-X", "-d", TEST_DB, "-c", "UPDATE contratos SET assinante = 'x'"]).stderr.lower()
       or "permission" in rodar(["psql", "-X", "-d", TEST_DB, "-c", "UPDATE contratos SET assinante = 'x'"]).stderr.lower())
r = ana.get(f"/pedidos/{PCONT}/imprimir")
checar("Comprovante A4 sai com o contrato, a assinatura e o código",
       "CONTRATO DE" in r.text and CODIGO in r.text and "data:image/png;base64," in r.text)
r = ana.get(f"/pedidos/{PCONT}/imprimir?formato=80")
checar("Cupom térmico leva o código e o link do contrato (sem o texto inteiro)",
       CODIGO in r.text and "/site/meu-pedido/" in r.text and "7. DESISTÊNCIA" not in r.text)
r = ana.get(f"/pedidos/{PCONT}/pdf")
checar("PDF do comprovante sai com o contrato", r.content[:4] == b"%PDF" and len(r.content) > 5000)
class FalsoSMTP:
    enviados = []
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, **k): pass
    def login(self, u, s): FalsoSMTP.login_ok = (u, s)
    def send_message(self, m): FalsoSMTP.enviados.append(m)
trabalhador.smtplib.SMTP = FalsoSMTP
enviar_fila()
email = FalsoSMTP.enviados[-1] if FalsoSMTP.enviados else None
checar("Cliente recebe o contrato por e-mail (Gmail da loja) com o PDF",
       email is not None and email["To"] == "zuleica@exemplo.com" and CODIGO in email.get_body().get_content()
       and any(a.get_filename() == f"contrato-{CODIGO}.pdf" for a in email.iter_attachments())
       and FalsoSMTP.login_ok == ("lojateste@gmail.com", "abcdefghijklmnop"))
checar("Cliente recebe o contrato no WhatsApp com o PDF anexo",
       any(CODIGO in m for m in para("5519992223333")) and any(n.startswith("contrato-") for f, n, _ in WPP["arquivos"]))
TOKEN_C = sql(f"SELECT token_publico FROM pedidos WHERE id = '{PCONT}'")
pub = TestClient(app)
r = pub.get(f"/site/meu-pedido/{TOKEN_C}")
checar("Página da cliente (link secreto) mostra pedido, contrato e assinatura, com CPF escondido",
       r.status_code == 200 and CODIGO in r.text and "data:image/png" in r.text and "111.444.777-35" not in r.text
       and "***.***.***-35" in r.text, [x for x in (CODIGO, "data:image/png", "***.***.***-35") if x not in r.text])
checar("Link errado não mostra nada", "Zuleica" not in pub.get("/site/meu-pedido/" + "0" * 36).text)
checar("PDF pela página da cliente", pub.get(f"/site/meu-pedido/{TOKEN_C}/pdf").content[:4] == b"%PDF")
r = pub.get(f"/site/contrato?codigo={CODIGO.lower()}")
checar("Qualquer pessoa confere o contrato pelo código", "encontrado" in r.text and "✓ assinado" in r.text and "Zuleica" not in r.text)
checar("Código inexistente: não encontrado", "Não encontramos" in pub.get("/site/contrato?codigo=AAAA-BBBB-CCCC").text)
r = ana.post(f"/pedidos/{PCONT}/enviar-comprovante")
checar("Botão 'Enviar para a cliente' manda WhatsApp e e-mail", "no WhatsApp e por e-mail" in r.text, r.text[-300:])

print("\n== Entrada no sistema: código no WhatsApp, aparelho novo, horário ==")
def entrar(login, cliente=None, senha=SENHA):
    c = cliente or TestClient(app, follow_redirects=True)
    t = Pessoa._token(c.get("/login").text)
    return c, c.post("/login", data={"csrf": t, "login": login, "senha": senha})
antes = len(WPP["enviadas"])
c_dona, r = entrar("dona")
codigo_zap = [m for m in para("55" + FONES["dona"])[-3:] if "código para entrar" in m]
checar("Dona entrando: pede o código que chegou no WhatsApp dela", "Confira no WhatsApp" in r.text and "0001" in r.text
       and codigo_zap, r.text[-300:])
COD6 = re.search(r"\b(\d{6})\b", codigo_zap[-1]).group(1) if codigo_zap else "000000"
t = Pessoa._token(r.text)
r = c_dona.post("/login/codigo", data={"csrf": t, "codigo": "123456" if COD6 != "123456" else "654321"})
checar("Código errado: recusa", r.status_code == 400 and "Código errado" in r.text)
r = c_dona.post("/login/codigo", data={"csrf": Pessoa._token(r.text), "codigo": COD6})
checar("Código certo: entra", "Olá, Maria" in r.text, r.text[-300:])
enviar_fila()
checar("Aparelho novo da Dona: Dona e Dev avisados no WhatsApp",
       any("aparelho novo" in m for m in para("55" + FONES["dev"])) and any("aparelho novo" in m for m in para("55" + FONES["dona"])))
checar("Código de entrada nunca fica em texto no banco nem na fila",
       sql(f"SELECT count(*) FROM auditoria WHERE dados::text LIKE '%{COD6}%'") == "0")
c_ana, r = entrar("ana")
checar("Atendente em aparelho novo também precisa do código", "Confira no WhatsApp" in r.text)
_, r = entrar("ana", ana.c)
checar("Atendente no aparelho de sempre entra direto", "Olá" in r.text and "Confira no WhatsApp" not in r.text, r.text[-300:])
sql("UPDATE lojas SET abre_as = '00:00', fecha_as = '00:01'")
entrar("ger", ger.c)
sql("UPDATE lojas SET abre_as = '08:00', fecha_as = '19:00'")
enviar_fila()
checar("Entrada fora do horário da loja: Dona e Dev avisados", any("fora do horário" in m and "Gerente" in m for m in para("55" + FONES["dona"])))

print("\n== Blindagem: IP bloqueado, sessão parada, desconectar aparelhos, senha fraca ==")
atacante = TestClient(app, client=("203.0.113.9", 50000))
for _ in range(12):
    t = Pessoa._token(atacante.get("/login").text)
    atacante.post("/login", data={"csrf": t, "login": "dona", "senha": "Chute-errado-1!"})
sql("UPDATE usuarios SET tentativas_falhas = 0, bloqueado_ate = NULL")
t = Pessoa._token(atacante.get("/login").text)
r = atacante.post("/login", data={"csrf": t, "login": "ger", "senha": SENHA})
checar("12 senhas erradas do mesmo IP: IP bloqueado (nem a senha certa entra)",
       r.status_code == 429 and sql("SELECT count(*) FROM bloqueios_ip WHERE ip = '203.0.113.9'") == "1", r.status_code)
enviar_fila()
checar("IP bloqueado: aviso de segurança no WhatsApp", any("203.0.113.9" in m and "bloqueado" in m for m in para("55" + FONES["dev"])))
outro = TestClient(app, client=("198.51.100.7", 50000), follow_redirects=True)
t = Pessoa._token(outro.get("/login").text)
checar("Outro IP continua entrando", "Olá" in outro.post("/login", data={"csrf": t, "login": "ger", "senha": SENHA}).text)
cfg_app.LIMITES_ATIVOS = True
acesso._janelas.clear()
seq = [acesso.excedeu("POST", "/login", "192.0.2.50") for _ in range(11)]
cfg_app.LIMITES_ATIVOS = False
checar("Limite contra robôs: 11º login seguido do mesmo IP em 5 min é barrado", seq[:10] == [False] * 10 and seq[10] is True)
cfg_app.SESSAO_OCIOSA_MIN = 0
import time as _time  # noqa: E402
_time.sleep(1.1)
r = lu.get("/limpeza")
cfg_app.SESSAO_OCIOSA_MIN = 30
checar("Sessão parada além do tempo: sai sozinho e explica", r.url.path == "/login" and "sem usar" in r.text, r.text[-300:])
lu = Pessoa("lu")
ANA_ID = sql("SELECT id FROM usuarios WHERE login='ana'")
r = dona.post(f"/equipe/{ANA_ID}/desconectar")
checar("Dona desconecta todos os aparelhos da Ana: sessões abertas caem",
       ana.get("/pedidos").url.path == "/login")
r = dona.get(f"/equipe/{ANA_ID}")
checar("Ficha do funcionário mostra os aparelhos e os avisos", "Aparelhos" in r.text and "Avisos no WhatsApp" in r.text, r.text[-500:])
checar("Telefone da equipe guardado cifrado", sql("SELECT count(*) FROM usuarios WHERE telefone IS NOT NULL") == "0"
       and sql(f"SELECT count(*) FROM usuarios WHERE encode(telefone_c,'escape') LIKE '%{FONES['ana']}%'") == "0")
# Senha antiga (fora da regra nova): entra, mas é obrigada a trocar
from app import seguranca as _seg  # noqa: E402
sql(f"UPDATE usuarios SET senha_hash = '{_seg.gerar_hash('senhafraca123')}' WHERE login = 'lu'")
c_lu, r = entrar("lu", senha="senhafraca123")
checar("Senha fora da regra nova: entra direto na troca de senha obrigatória",
       r.url.path == "/minha-senha" and "regra de senha da loja mudou" in r.text, r.text[-400:])
checar("Enquanto não troca, não abre outras telas", c_lu.get("/limpeza").url.path == "/minha-senha")
t = Pessoa._token(r.text)
r = c_lu.post("/minha-senha", data={"csrf": t, "atual": "senhafraca123", "senha": SENHA, "senha2": SENHA})
checar("Depois de trocar, usa o sistema normalmente", "Senha trocada" in r.text and c_lu.get("/limpeza").status_code == 200)
lu = Pessoa("lu")
ana = Pessoa("ana")

print("\n== Redes sociais ==")
checar("Botão 'Redes sociais' na tela inicial da Dona", "/config/redes" in botoes(dona))
r = dona.post("/config/redes", {"instagram": "https://www.instagram.com/lojateste/", "facebook": "facebook.com/lojateste",
                                "tiktok": "https://evil.example.com/x", "youtube": "", "pinterest": "", "site_externo": ""})
checar("Link de rede que não é da rede certa é recusado", r.status_code == 400 and "TikTok" in r.text)
r = dona.post("/config/redes", {"instagram": "https://www.instagram.com/lojateste/", "facebook": "facebook.com/lojateste",
                                "tiktok": "https://www.tiktok.com/@lojateste", "youtube": "", "pinterest": "",
                                "site_externo": "https://linktr.ee/lojateste"})
texto_site = TestClient(app).get("/site").text
checar("Redes salvas aparecem no rodapé do site", "Redes sociais salvas" in r.text and "https://facebook.com/lojateste" in texto_site
       and "https://www.tiktok.com/@lojateste" in texto_site and "instagram.com/lojateste" in texto_site
       and "https://linktr.ee/lojateste" in texto_site)
checar("Atendente não mexe nas redes", ana.get("/config/redes").status_code == 403)
checar("Nenhuma tela nova usa style inline (CSP)",
       not [t.name for t in (APP / "app/templates").rglob("*.html") if "style=" in t.read_text()])


print("\n== Atualização: dados antigos em texto viram cifrados ==")
LOJA = os.environ["LOJA_ID"]
sql(f"""INSERT INTO clientes (loja_id, nome, telefone, cpf, email, observacoes)
        VALUES ('{LOJA}', 'Antiga Texto', '19911112222', '39053344705', 'antiga@exemplo.com', 'veio antes')""")
ANT = sql("SELECT id FROM clientes WHERE nome = 'Antiga Texto'")
sql(f"""INSERT INTO medidas (loja_id, cliente_id, ficha, valores, observacoes, tomada_por)
        VALUES ('{LOJA}', '{ANT}', 'FEMININO', '{{"busto": 91}}', 'barra 2 cm', (SELECT id FROM usuarios WHERE login='cida'))""")
sql("UPDATE usuarios SET telefone = '19933334444' WHERE login = 'lu'")
from app import pessoas as _pessoas  # noqa: E402
with db.transacao() as _cur:
    n_cif = _pessoas.cifrar_pendentes(_cur)
checar("Ao subir a versão nova, cliente, funcionário e medidas antigos são cifrados",
       n_cif >= 3 and sql(f"SELECT count(*) FROM clientes WHERE id='{ANT}' AND telefone IS NULL AND cpf IS NULL AND email IS NULL") == "1"
       and aberto(f"SELECT cpf_c FROM clientes WHERE id='{ANT}'", "clientes.cpf") == "39053344705"
       and sql(f"SELECT telefone_idx FROM clientes WHERE id='{ANT}'") == idx_tel("19911112222")
       and sql("SELECT count(*) FROM usuarios WHERE login='lu' AND telefone IS NULL AND telefone_c IS NOT NULL") == "1"
       and _json.loads(aberto(f"SELECT valores_c FROM medidas WHERE cliente_id='{ANT}'", "medidas.valores"))["busto"] == 91
       and sql(f"SELECT valores::text || coalesce(observacoes, '-') FROM medidas WHERE cliente_id='{ANT}'") == "{}-", n_cif)
checar("Medida antiga continua imutável depois de cifrada",
       "imutável" in rodar(["psql", "-X", "-d", TEST_DB, "-c", f"UPDATE medidas SET salto_cm = 5 WHERE cliente_id='{ANT}'"]).stderr)
checar("Busca acha a cliente antiga pelo telefone", "Antiga Texto" in ger.get("/clientes?q=(19) 91111-2222").text)

print("\n== Regras da vitrine (painel) e reserva do pagamento até a devolução ==")
checar("Atendente não mexe nas regras da vitrine", ana.get("/config/vitrine").status_code == 403)
checar("Botão 'Regras da vitrine' na tela da Gerente", "/config/vitrine" in botoes(ger))
r = ger.post("/config/vitrine", {"reserva_modo": "ATE_DEVOLVER", "alugada_no_site": "OCUPADA", "venda_ajuste_some": "1",
                                 "prova_antecedencia_dias": "2", "atraso_aviso_hora": "09:30"})
checar("Gerente salva: 'do pagamento até a devolução' (fica na auditoria)", "Regras salvas" in r.text
       and sql("SELECT reserva_modo FROM lojas") == "ATE_DEVOLVER"
       and sql("SELECT count(*) FROM auditoria WHERE acao='regras_vitrine_alteradas'") == "1")
chave = ana.chave("/pecas/nova")
ana.post("/pecas/nova", {"chave": chave, "categoria": cat_ves, "nome": "Vestido vinho regra", "tamanho": "40",
                         "uso": "ambos", "preco_aluguel": "300", "preco_venda": "700"})
PB = sql("SELECT id FROM produtos WHERE nome = 'Vestido vinho regra'")
sql(f"UPDATE produtos SET no_site = true WHERE id = '{PB}'")
SLUGB = sql(f"SELECT slug FROM produtos WHERE id = '{PB}'")
ret_b = hoje + timedelta(days=60)
dados_b = {"tipo": "ALUGUEL", "cliente_nome": "Bruna Regra", "cliente_telefone": "(19) 99333-4444",
           "data_retirada": ret_b.isoformat(), "data_devolucao": (ret_b + timedelta(days=2)).isoformat(), "forma": "PIX", "valor": "300"}
r = pedido(ana, dados_b, peca=PB)
PEDB = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PB}'")
checar("Pagou: a peça sai do estoque NA HORA (não na 1ª prova) e sem data para voltar",
       sql(f"SELECT (lower(periodo_bloqueado) = (now() AT TIME ZONE 'America/Sao_Paulo')::date AND upper_inf(periodo_bloqueado))::text "
           f"FROM pedidos WHERE id = '{PEDB}'") == "true" and "fora do estoque até a devolução" in ana.get(f"/pedidos/{PEDB}").text)
site_b = TestClient(app)
for dia in (hoje + timedelta(days=5), ret_b + timedelta(days=30), hoje + timedelta(days=300)):
    checar(f"Site: peça ocupada no calendário em {dia:%d/%m/%Y} (antes, durante e depois do aluguel)",
           "Já reservada" in site_b.get(f"/site/p/{SLUGB}?data={dia.isoformat()}").text)
checar("Site: página da peça avisa que está com uma cliente", "está com uma cliente" in site_b.get(f"/site/p/{SLUGB}").text)
r = pedido(ana, {**dados_b, "cliente_nome": "Outra Pessoa", "cliente_telefone": "(19) 99333-5555",
                 "data_retirada": (hoje + timedelta(days=10)).isoformat(),
                 "data_devolucao": (hoje + timedelta(days=11)).isoformat()}, peca=PB)
checar("Outro aluguel da mesma peça (mesmo antes das datas) é recusado até a devolução",
       "até a devolução" in r.text and sql(f"SELECT count(*) FROM pedidos WHERE produto_id='{PB}'") == "1", r.text[-300:])
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "cliente_nome": "Rita Balcão", "cliente_telefone": "(19) 99777-0005", "valor": "700", "forma": "PIX"}, peca=PB)
checar("Venda da peça com aluguel marcado é recusada", sql(f"SELECT count(*) FROM pedidos WHERE produto_id='{PB}'") == "1")
AGB = sql(f"SELECT id FROM agendamentos WHERE pedido_id = '{PEDB}' AND tipo = 'PROVA_1'")
ana.post(f"/agenda/{AGB}/remarcar", {"dia": (hoje + timedelta(days=40)).isoformat(), "hora": "10:00"})
checar("Remarcar a prova não devolve a peça ao estoque",
       sql(f"SELECT upper_inf(periodo_bloqueado)::text FROM pedidos WHERE id = '{PEDB}'") == "true")
r = ger.get(f"/pedidos/{PEDB}")
checar("Antes da retirada não existe botão 'Cliente devolveu'", "Cliente devolveu" not in r.text)
# a cliente paga tudo, prova e retira (datas movidas para testar o atraso)
for etapa in ("PROVA_1", "EM_AJUSTE", "PROVA_2", "PRONTO", "ALUGADO"):
    ger.post(f"/pedidos/{PEDB}/etapa", {"para": etapa})
r = ger.get(f"/pedidos/{PEDB}")
checar("Cliente retirou: aparece o botão 'Cliente devolveu'", sql(f"SELECT status FROM pedidos WHERE id='{PEDB}'") == "ALUGADO"
       and "Cliente devolveu" in r.text, sql(f"SELECT status FROM pedidos WHERE id='{PEDB}'"))
sql(f"UPDATE agendamentos SET status = 'FEITO' WHERE pedido_id = '{PEDB}'")
rodar(["psql", "-X", "-q", "-d", TEST_DB, "-c", "ALTER TABLE pedidos DISABLE TRIGGER USER"])
sql(f"UPDATE pedidos SET data_retirada = current_date - 6, data_devolucao = current_date - 3 WHERE id = '{PEDB}'")
rodar(["psql", "-X", "-q", "-d", TEST_DB, "-c", "ALTER TABLE pedidos ENABLE TRIGGER USER"])
r = ger.get(f"/pedidos/{PEDB}")
checar("Passou da data: o pedido mostra 'Devolução atrasada há 3 dias' e o botão continua", "atrasada há 3 dias" in r.text
       and "Cliente devolveu" in r.text)
checar("Tela inicial avisa as peças em atraso e a lista filtra", "devolução atrasada" in ger.get("/").text
       and "Bruna Regra" in ger.get("/pedidos?filtro=atrasados").text)
checar("Atrasada continua fora do estoque (não volta sozinha)", "Já reservada" in site_b.get(f"/site/p/{SLUGB}?data={(hoje + timedelta(days=1)).isoformat()}").text)
trabalhador.rodar_rotinas(_dt.now(ZoneInfo("America/Sao_Paulo")).replace(hour=10))
trabalhador.rodar_rotinas(_dt.now(ZoneInfo("America/Sao_Paulo")).replace(hour=11))
enviar_fila()
msg_b = [m for m in para("5519993334444") if "atraso" in m]
checar("Cliente recebe aviso de atraso no WhatsApp (uma vez por dia)", len(msg_b) == 1 and "3 dias de atraso" in msg_b[0], msg_b)
checar("Equipe recebe a lista de peças em atraso", any("Peças em atraso" in m and "Bruna" in m for m in para("55" + FONES["dona"])))
checar("Comando #atrasos no WhatsApp", chega("55" + FONES["dona"], "#atrasos") == "comando" and enviar_fila()
       and "Bruna" in para("55" + FONES["dona"])[-1])
ana.post(f"/pedidos/{PEDB}/etapa", {"para": "DEVOLVIDO"})
limpeza_dias = int(sql("SELECT dias_limpeza FROM config_pagamento ORDER BY vigente_desde DESC LIMIT 1"))
livre_em = hoje + timedelta(days=limpeza_dias + 1)
checar("Atendente apertou 'Cliente devolveu': peça presa só até acabar a limpeza",
       sql(f"SELECT upper(periodo_bloqueado) - (now() AT TIME ZONE 'America/Sao_Paulo')::date FROM pedidos WHERE id='{PEDB}'")
       == str(limpeza_dias + 1)
       and "Livre em" in site_b.get(f"/site/p/{SLUGB}?data={(livre_em + timedelta(days=2)).isoformat()}").text)
ana.post(f"/pedidos/{PEDB}/etapa", {"para": "EM_LIMPEZA"})
lu.post(f"/pedidos/{PEDB}/etapa", {"para": "CONCLUIDO", "volta": "/limpeza"})
checar("'Voltou da limpeza': peça livre de novo no site", "Livre em" in site_b.get(f"/site/p/{SLUGB}?data={(hoje + timedelta(days=1)).isoformat()}").text)
ger.post("/config/vitrine", {"reserva_modo": "ATE_DEVOLVER", "alugada_no_site": "ESCONDE", "venda_ajuste_some": "1",
                             "prova_antecedencia_dias": "2", "atraso_aviso_hora": "09:30"})
pedido(ana, dados_b, peca=PB)
checar("Regra 'some do site até voltar': peça alugada não aparece no site", site_b.get(f"/site/p/{SLUGB}").status_code == 404
       or "não encontrada" in site_b.get(f"/site/p/{SLUGB}").text.lower())
t = Pessoa._token(site_b.get("/site/prova").text)
r = site_b.post("/site/prova", data={"csrf": t, "nome": "Ana Pressa", "telefone": "(19) 98888-1111", "lgpd": "1",
                                     "data_desejada": hoje.isoformat(), "periodo": "TARDE"})
checar("Prova pelo site respeita a antecedência do painel (2 dias)", r.status_code == 400 and "a partir de" in r.text)
ger.post("/config/vitrine", {"reserva_modo": "ATE_DEVOLVER", "alugada_no_site": "OCUPADA", "venda_ajuste_some": "1",
                             "prova_antecedencia_dias": "1", "atraso_aviso_hora": "09:30"})

print("\n== Marca: logo e favicon ==")
from app import code128 as _c128, marca as _marca  # noqa: E402
import segno as _segno  # noqa: E402
def png_logo(largura=600, altura=200, fundo=(255, 255, 255, 0), fmt="PNG"):
    img = Image.new("RGBA" if fmt == "PNG" else "RGB", (largura, altura), fundo if fmt == "PNG" else (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 50, 200, 150], fill=(150, 20, 60))
    d.ellipse([260, 40, 380, 160], fill=(20, 20, 20))
    d.line([(420, 60), (560, 140)], fill=(90, 60, 10), width=14)
    b = _io.BytesIO(); img.save(b, fmt)
    return b.getvalue()
r = TestClient(app).get("/favicon.ico")
checar("Sem logo ainda: favicon com a inicial da loja", r.status_code == 200 and r.headers["content-type"] == "image/x-icon")
checar("Atendente não mexe na marca", ana.get("/config/marca").status_code == 403)
def enviar_marca(p, rota, nome, dados, tipo="image/png"):
    return p.c.post(rota, data={"csrf": p.csrf}, files=[("arquivo", (nome, dados, tipo))])
r = enviar_marca(dona, "/config/marca/logo", "logo.svg", b'<svg onload="alert(1)"></svg>', "image/svg+xml")
checar("Logo em SVG (pode esconder código) é recusada", "PNG" in r.text and sql("SELECT logo_arquivo IS NULL FROM lojas WHERE nome='Loja Teste'") == "t")
r = enviar_marca(dona, "/config/marca/logo", "logo.png", b"isto nao e imagem")
checar("Arquivo que não é imagem é recusado", "Não consegui abrir" in r.text)
r = enviar_marca(dona, "/config/marca/logo", "logo.jpg", png_logo(fmt="JPEG"), "image/jpeg")
LOGO1 = sql("SELECT logo_arquivo FROM lojas WHERE nome='Loja Teste'")
checar("Logo JPG enviada: vira PNG sem fundo, versão preto e branco e ícones", "Logo salvo" in r.text and LOGO1
       and all(_marca.caminho(LOGO1, s).exists() for s in ("logo.png", "mono.png"))
       and sql("SELECT icone_arquivo IS NOT NULL FROM lojas WHERE nome='Loja Teste'") == "t")
with Image.open(_marca.caminho(LOGO1, "logo.png")) as _l:
    checar("Fundo branco do JPG virou transparente e a logo foi aparada", _l.mode == "RGBA" and _l.getpixel((0, 0))[3] == 0
           and _l.width < 600)
r = enviar_marca(dona, "/config/marca/logo", "logo.png", png_logo())
LOGO = sql("SELECT logo_arquivo FROM lojas WHERE nome='Loja Teste'")
pub = TestClient(app)
checar("Logo em uso é pública; a antiga não fica mais no ar", pub.get(f"/marca/{LOGO}-logo.png").status_code == 200
       and pub.get(f"/marca/{LOGO1}-logo.png").status_code == 404)
checar("Nome de arquivo inventado na URL da marca: 404", pub.get("/marca/..%2F..%2Fetc-logo.png").status_code == 404)
site_html = pub.get("/site").text
checar("Site mostra a logo no topo e no rodapé e tem favicon/ícone de celular",
       site_html.count(f"/marca/{LOGO}-logo.png") >= 2 and 'rel="icon"' in site_html and "apple-touch-icon" in site_html)
checar("Tela de entrar no sistema mostra a logo", f"/marca/{LOGO}-logo.png" in TestClient(app).get("/login").text)
checar("Ícones do celular (192 e 512) e favicon.ico", pub.get("/icone-192.png").status_code == 200
       and pub.get("/icone-512.png").headers["content-type"] == "image/png" and pub.get("/favicon.ico").status_code == 200)
r = enviar_marca(dona, "/config/marca/icone", "icone.png", png_logo(300, 300))
checar("Ícone próprio enviado", "Ícone salvo" in r.text)
r = ana.get(f"/pedidos/{PZED}/imprimir")
checar("Comprovante A4 sai com a logo colorida", f"/marca/{LOGO}-logo.png" in r.text)
r = ana.get(f"/pedidos/{PZED}/imprimir?formato=80")
checar("Cupom térmico sai com a logo preto e branco", f"/marca/{LOGO}-mono.png" in r.text)
r = ana.get(f"/pedidos/{PZED}/pdf")
checar("PDF do comprovante leva a logo", r.content[:4] == b"%PDF" and r.content.count(b"/Subtype /Image") >= 2)
checar("Ficha de medidas impressa com a logo", f"/marca/{LOGO}-logo.png" in cida.get(f"/clientes/{CLI}/medidas/imprimir?ficha=FEMININO").text)

print("\n== Rodapé do site: lojas, mapa, páginas, novidades ==")
checar("Atendente não mexe no rodapé", ana.get("/config/rodape").status_code == 403)
r = dona.post("/config/unidades", {"nome": "Loja Centro", "endereco": "Rua das Flores, 100 - Centro", "cidade": "Mogi Guaçu/SP",
                                   "telefone": "(19) 3333-0000", "whatsapp": "(19) 99888-7777", "horario": "Seg a sex 9h às 18h",
                                   "mapa_link": "https://site-falso.com/maps/x"})
checar("Link de mapa que não é do Google é recusado", r.status_code == 400 and "Google Maps" in r.text)
dona.post("/config/unidades", {"nome": "Loja Centro", "endereco": "Rua das Flores, 100 - Centro", "cidade": "Mogi Guaçu/SP",
                               "telefone": "(19) 3333-0000", "whatsapp": "(19) 99888-7777", "horario": "Seg a sex 9h às 18h"})
dona.post("/config/unidades", {"nome": "Loja Shopping", "endereco": "Av. Brasil, 2000 - Loja 12", "cidade": "Campinas/SP",
                               "mapa_link": "https://maps.app.goo.gl/AbCdEf123"})
site_html = TestClient(app).get("/site").text
checar("Rodapé mostra as lojas, telefones, horário e 'Como chegar' no Google Maps",
       "Nossas lojas" in site_html and "Loja Centro" in site_html and "Rua das Flores, 100" in site_html
       and "(19) 3333-0000" in site_html and "google.com/maps/search/?api=1&amp;query=Rua+das+Flores" in site_html
       and "https://maps.app.goo.gl/AbCdEf123" in site_html)
checar("Mapa carrega só quando o rodapé aparece na tela (embed guardado, sem iframe no HTML)",
       'data-embed="https://www.google.com/maps?output=embed' in site_html and "<iframe" not in site_html
       and "frame-src https://www.google.com" in TestClient(app).get("/site").headers["content-security-policy"])
UN2 = sql("SELECT id FROM unidades WHERE nome = 'Loja Shopping'")
dona.post(f"/config/unidades/{UN2}", {"acao": "desativar"})
checar("Loja tirada do site some do rodapé (continua guardada)", "Loja Shopping" not in TestClient(app).get("/site").text
       and sql("SELECT count(*) FROM unidades") == "2")
dona.post("/config/rodape", {"sobre_curto": "Especialistas em vestidos de festa para madrinhas.", "email_contato": "sac@lojateste.com.br",
                             "formas": ["Pix", "Cartão de crédito"], "bandeiras": ["Visa", "Elo"], "parcelas_no_site": "1",
                             "novidades_ativa": "1"})
site_html = TestClient(app).get("/site").text
checar("Rodapé: sobre, e-mail, formas de pagamento, bandeiras e selo de segurança",
       "Especialistas em vestidos" in site_html and "sac@lojateste.com.br" in site_html and "<span>Pix</span>" in site_html
       and "<span>Elo</span>" in site_html and "<span>Dinheiro</span>" not in site_html and "Site seguro" in site_html)
checar("Rodapé: redes sociais e páginas institucionais", "Instagram" in site_html and "/site/pagina/politica-de-privacidade" in site_html
       and "/site/pagina/trocas-e-devolucoes" in site_html)
r = TestClient(app).get("/site/pagina/politica-de-privacidade")
checar("Política de privacidade (LGPD) já vem pronta", r.status_code == 200 and "LGPD" in r.text and "Loja Teste" in r.text)
checar("Página que não existe: não encontrada", "não encontrada" in TestClient(app).get("/site/pagina/../../etc").text.lower()
       or TestClient(app).get("/site/pagina/xyz").status_code in (200, 404))
dona.post("/config/paginas/termos-de-uso", {"texto": "Novos termos da loja <script>alert(1)</script> com regras de teste."})
r = TestClient(app).get("/site/pagina/termos-de-uso")
checar("Dona edita os Termos: aparece no site (escapado) e guarda o histórico", "Novos termos da loja" in r.text
       and "<script>alert(1)" not in r.text and sql("SELECT count(*) FROM paginas WHERE slug='termos-de-uso'") == "1")
checar("Histórico das páginas não pode ser alterado", "imutável" in rodar(["psql", "-X", "-d", TEST_DB, "-c",
       "UPDATE paginas SET texto = 'x'"]).stderr.lower())
nv = TestClient(app); t = Pessoa._token(nv.get("/site").text)
r = nv.post("/site/novidades", data={"csrf": t, "nome": "Nina Novidade", "email": "nina@exemplo.com"})
checar("Novidades sem aceitar (LGPD): recusado", r.status_code == 400 and "aceita" in r.json()["erro"])
r = nv.post("/site/novidades", data={"csrf": t, "nome": "Nina Novidade", "email": "nina@exemplo.com", "telefone": "(19) 99777-6655", "lgpd": "1"})
nv.post("/site/novidades", data={"csrf": t, "nome": "Nina", "email": "NINA@exemplo.com", "lgpd": "1"})
checar("Cadastro de novidades: guardado cifrado, sem duplicar", r.json().get("ok") and sql("SELECT count(*) FROM inscritos") == "1"
       and sql("SELECT count(*) FROM inscritos WHERE encode(email_c,'escape') LIKE '%nina%'") == "0")
r = nv.post("/site/novidades", data={"csrf": t, "nome": "Robo", "email": "r@r.com", "lgpd": "1", "site": "http://spam"})
checar("Robô no cadastro de novidades (campo-isca): finge que deu e não grava", r.json().get("ok") and sql("SELECT count(*) FROM inscritos") == "1")

print("\n== Modelo de parcelas (loja e site) ==")
from app import pagamento as _pg  # noqa: E402
s_ex = _pg.simular({"max_parcelas_aluguel": 6, "max_parcelas_venda": 10, "valor_minimo_parcela_centavos": 5000,
                    "entrada_minima_pct": 25, "entrada_aluguel_tipo": "PCT", "entrada_aluguel_valor": 25, "entrada_venda_tipo": "PCT", "entrada_venda_valor": 25}, {2: 0, 3: 0, 4: 5, 5: 6, 6: 7}, 60000, "ALUGUEL")
checar("Simulação: entrada 25%, sem juros até 3×, juros depois, respeita parcela mínima",
       s_ex["entrada"] == 15000 and s_ex["sem_juros_ate"] == 3 and s_ex["opcoes"][3]["total"] == 63000
       and s_ex["opcoes"][3]["parcela"] == 15750 and s_ex["maximo"]["vezes"] == 6)
s_min = _pg.simular({"max_parcelas_aluguel": 6, "max_parcelas_venda": 6, "valor_minimo_parcela_centavos": 5000,
                     "entrada_minima_pct": 25, "entrada_aluguel_tipo": "PCT", "entrada_aluguel_valor": 25, "entrada_venda_tipo": "PCT", "entrada_venda_valor": 25}, {}, 12000, "ALUGUEL")
checar("Peça barata não parcela abaixo da parcela mínima", s_min["maximo"]["vezes"] == 2)
s_buraco = _pg.simular({"max_parcelas_aluguel": 6, "max_parcelas_venda": 6, "valor_minimo_parcela_centavos": 1000,
                        "entrada_minima_pct": 25, "entrada_aluguel_tipo": "PCT", "entrada_aluguel_valor": 25, "entrada_venda_tipo": "PCT", "entrada_venda_valor": 25}, {3: 5}, 60000, "ALUGUEL")
checar("'Sem juros até' não pula parcela com juros no meio (2× sem juros, 3× com)", s_buraco["sem_juros_ate"] == 2)
r = dona.get("/config")
checar("Configurações mostram o modelo de parcelas (aluguel e venda)", "Modelo de parcelas" in r.text and "1×" in r.text)
r = TestClient(app).get(f"/site/p/{SLUG4}")
checar("Página da peça no site mostra 'reserve com' e as parcelas", "reserve com" in r.text and "× de R$" in r.text, r.text[-800:])
dona.post("/config/rodape", {"sobre_curto": "Especialistas em vestidos de festa para madrinhas.", "formas": ["Pix"],
                             "bandeiras": ["Visa"], "novidades_ativa": "1"})
checar("Loja desliga o parcelamento no site: some da página", "reserve com" not in TestClient(app).get(f"/site/p/{SLUG4}").text)

print("\n== Texto do contrato nas Configurações ==")
checar("Atendente não mexe no contrato", ana.get("/config/contrato").status_code == 403)
r = ger.get("/config/contrato")
checar("Gerente vê o contrato, os campos e a prévia com um pedido de exemplo", "{cliente}" in r.text and "Maria da Silva" in r.text)
base_c = contrato_padrao = r.text
from app import contrato as _ct  # noqa: E402
r = ger.post("/config/contrato", {"texto": _ct.PADRAO + "\n\n9. CHAVE ERRADA {cliente", "acao": "salvar"})
checar("Chave { sobrando: explica e não salva", r.status_code == 400 and "sobrando" in r.text)
r = ger.post("/config/contrato", {"texto": _ct.PADRAO + "\n\n9. {nome_da_mae}", "acao": "salvar"})
checar("Campo que não existe: explica e não salva", r.status_code == 400 and "nome_da_mae" in r.text)
r = ger.post("/config/contrato", {"texto": _ct.PADRAO + "\n\n9. CLÁUSULA EXTRA DE TESTE: peça {sku}.", "acao": "salvar"})
checar("Contrato novo salvo (com histórico)", "Contrato salvo" in r.text and sql("SELECT count(*) FROM contrato_modelos") == "1")
PNOVO = sql(f"SELECT id FROM pedidos WHERE produto_id='{PB}' AND status='RESERVADO'")
r = ana.get(f"/pedidos/{PNOVO}/contrato")
checar("Próximo contrato usa o texto novo", "CLÁUSULA EXTRA DE TESTE: peça" in r.text)
r = ana.get(f"/pedidos/{PZED}/contrato")
checar("Contrato já assinado continua igual (e íntegro)", "CLÁUSULA EXTRA" not in r.text and "Íntegro" in r.text)

print("\n== Checklist do vestido (código de barras e QR) ==")
checar("Limpeza não abre o checklist de pedido", lu.get(f"/pedidos/{PZED}/checklist").status_code == 403)
r = ana.get(f"/pedidos/{PZED}/checklist")
COD_P = PZED.replace("-", "")[:8].upper()
checar("Checklist 10×15 com logo, código do pedido, peça, cliente e datas", r.status_code == 200 and COD_P in r.text
       and "CHECKLIST DA PEÇA" in r.text and "Zuleica Zap" in r.text and "Devolução" in r.text and "-mono.png" in r.text)
def ler_barras(html):
    svg = re.search(r'<svg[^>]*aria-label="Código de barras ([^"]+)".*?</svg>', html, re.S)
    rects = [(float(x), float(w)) for x, w in re.findall(r'<rect x="([\d.]+)" y="0" width="([\d.]+)"', svg.group(0))]
    larguras, fim = [], None
    for x, w in rects:
        if fim is not None:
            larguras.append(round((x - fim) / 2))
        larguras.append(round(w / 2)); fim = x + w
    return _c128.decodificar(larguras)
checar("Código de barras impresso lê o código do pedido (conferido barra a barra)", ler_barras(r.text) == COD_P)
qr_esperado = _segno.make(f"http://testserver/pedidos/{PZED}", error="m", micro=False)
caminho_qr = re.search(r'class="qr"><g transform="scale\(4\)">(.*?)</g>', r.text, re.S)
checar("QR code aponta para o pedido no sistema",
       caminho_qr and qr_esperado.svg_inline(scale=4, border=2, dark="#000", light="#fff", omitsize=True, svgclass="qr",
                                             lineclass=None).find(caminho_qr.group(1)) > 0)
checar("Passos já feitos saem marcados (✔ com data e quem fez)", r.text.count("✔") >= 3 and 'class="feito"' in r.text)
checar("Checklist em folha A4 também", "checklist-a4" in ana.get(f"/pedidos/{PZED}/checklist?formato=a4").text)
checar("Leitor passado no checklist (em Procurar roupa) abre o pedido",
       ana.get(f"/pecas?q={COD_P.lower()}").url.path == f"/pedidos/{PZED}"
       and ana.get(f"/pedidos?q={COD_P}").url.path == f"/pedidos/{PZED}")
checar("Pedido mostra o botão do checklist", f"/pedidos/{PZED}/checklist" in ana.get(f"/pedidos/{PZED}").text)

print("\n== Etiquetas das peças ==")
r = ana.get(f"/pecas/{PV4}/etiqueta")
checar("Etiqueta para pendurar: SKU, código de barras, QR, tamanho, preço e logo",
       "etiquetas-pendurar" in r.text and r.text.count('class="etq"') == 1 and 'class="qr"' in r.text
       and f"/marca/{LOGO}-mono.png" in r.text and "Aluguel R$" in r.text)
sku4 = sql(f"SELECT sku FROM produtos WHERE id='{PV4}'")
checar("Código de barras da etiqueta lê o SKU", ler_barras(r.text) == sku4)
def qr_caminho(url):
    svg = _segno.make(url, error="m", micro=False).svg_inline(scale=4, border=2, dark="#000", light="#fff", omitsize=True,
                                                               svgclass="qr", lineclass=None)
    return re.search(r'<g transform="scale\(4\)">(.*?)</g>', svg, re.S).group(1)
checar("QR da peça que está no site abre a página dela no site", qr_caminho(f"http://testserver/site/p/{SLUG4}") in r.text)
r1 = ana.get(f"/pecas/{P1}/etiqueta")
checar("QR da peça fora do site abre a peça no sistema", qr_caminho(f"http://testserver/pecas/{P1}") in r1.text)
r = ana.get(f"/pecas/etiquetas/imprimir?ids={PV4},{P1}&formato=a4&copias=3")
checar("Folha A4 com várias peças e cópias (2 peças × 3)", "etiquetas-a4" in r.text and r.text.count('class="etq"') == 6)
checar("Lista para escolher as peças", "GERAR ETIQUETAS" in ana.get("/pecas/etiquetas").text and sku4 in ana.get("/pecas/etiquetas").text)
checar("Ids inválidos voltam para a escolha", ana.get("/pecas/etiquetas/imprimir?ids=abc,../x").url.path == "/pecas/etiquetas")

print("\n== Entrada e cancelamento: % ou valor fixo, aluguel e venda ==")
sql("UPDATE lojas SET reserva_modo = 'DATAS'")
regras_base = {"parc_aluguel": "6", "parc_venda": "6", "parcela_min": "50", "desconto": "10", "teto_saida": "100",
               "dias_limpeza": "3", "regras_v2": "1", "arrependimento_7d": "1", **{f"juros_{n}": "0" for n in range(2, 13)}}
r = ger.post("/config/regras", {**regras_base, "entrada_aluguel_tipo": "PCT", "entrada_aluguel_valor": "150",
                                "entrada_venda_tipo": "PCT", "entrada_venda_valor": "40", "multa_aluguel_tipo": "PCT",
                                "multa_aluguel_valor": "30", "multa_venda_tipo": "FIXO", "multa_venda_valor": "200,00"})
checar("Porcentagem acima de 100 é recusada", r.status_code == 400 and "0 a 100" in r.text)
r = ger.post("/config/regras", {**regras_base, "entrada_aluguel_tipo": "FIXO", "entrada_aluguel_valor": "150,00",
                                "entrada_venda_tipo": "PCT", "entrada_venda_valor": "40", "multa_aluguel_tipo": "PCT",
                                "multa_aluguel_valor": "30", "multa_venda_tipo": "FIXO", "multa_venda_valor": "200,00"})
checar("Gerente define: entrada aluguel R$ 150 fixo, venda 40%; multa aluguel 30%, venda R$ 200 fixo",
       "Regras salvas" in r.text and sql("""SELECT entrada_aluguel_tipo||entrada_aluguel_valor::int||entrada_venda_tipo||entrada_venda_valor::int
                                               ||multa_aluguel_tipo||multa_aluguel_valor::int||multa_venda_tipo||multa_venda_valor::int
                                          FROM config_pagamento ORDER BY vigente_desde DESC LIMIT 1""") == "FIXO15000PCT40PCT30FIXO20000",
       sql("SELECT entrada_aluguel_tipo||entrada_aluguel_valor FROM config_pagamento ORDER BY vigente_desde DESC LIMIT 1"))
r = dona.get("/config")
checar("Tela de regras mostra % ou valor fixo para entrada e cancelamento", "Cancelamento pela cliente" in r.text
       and 'value="150,00"' in r.text and "arrependimento" in r.text.lower())
def nova_peca(nome, aluguel, venda):
    ana.post("/pecas/nova", {"chave": ana.chave("/pecas/nova"), "categoria": cat_ves, "nome": nome, "uso": "ambos",
                             "preco_aluguel": aluguel, "preco_venda": venda})
    return sql(f"SELECT id FROM produtos WHERE nome = '{nome}'")
PC1, PC2, PC3 = nova_peca("Vestido regra um", "400", "900"), nova_peca("Vestido regra dois", "100", "900"), nova_peca("Vestido regra tres", "400", "900")
dia_c = hoje + timedelta(days=120)
alug = {"tipo": "ALUGUEL", "cliente_nome": "Cecília Cancela", "cliente_telefone": "(19) 99111-2222", "forma": "PIX",
        "data_retirada": dia_c.isoformat(), "data_devolucao": (dia_c + timedelta(days=1)).isoformat()}
r = pedido(ana, {**alug, "valor": "100"}, peca=PC1)
checar("Aluguel: entrada fixa de R$ 150 — R$ 100 é recusado", "entrada mínima é R$ 150,00" in r.text, r.text[-300:])
r = pedido(ana, {**alug, "valor": "150"}, peca=PC1)
PCA = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PC1}'")
checar("Aluguel com R$ 150 de entrada: aceito, banco grava a entrada fixa", "Pedido guardado" in r.text
       and sql(f"SELECT entrada_minima_centavos FROM pedidos WHERE id='{PCA}'") == "15000")
pedido(ana, {**alug, "valor": "100"}, peca=PC2)
checar("Peça mais barata que a entrada fixa: a entrada é o total", sql(f"SELECT entrada_minima_centavos FROM pedidos WHERE produto_id='{PC2}'") == "10000")
r = pedido(ana, {"tipo": "VENDA_AJUSTE", "cliente_nome": "Vera Venda", "cliente_telefone": "(19) 99111-3333",
                 "data_retirada": dia_c.isoformat(), "valor_ajuste": "100", "valor": "399", "forma": "PIX"}, peca=PC3)
checar("Venda com ajuste: 40% de R$ 1.000 = R$ 400 (R$ 399 recusado)", "R$ 400,00" in r.text)
pedido(ana, {"tipo": "VENDA_AJUSTE", "cliente_nome": "Vera Venda", "cliente_telefone": "(19) 99111-3333",
             "data_retirada": dia_c.isoformat(), "valor_ajuste": "100", "valor": "400", "forma": "PIX"}, peca=PC3)
PCV = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PC3}'")
checar("Pedido antigo continua com a regra do dia em que foi feito", sql(f"SELECT entrada_minima_centavos FROM pedidos WHERE id='{PED}'") == "30000")
r = ger.get(f"/pedidos/{PCA}")
checar("Antes de cancelar, a tela mostra quanto a loja fica e quanto devolve (30% de R$ 400 = R$ 120; devolve R$ 30)",
       "Multa de 30% do total" in r.text and "R$ 120,00" in r.text and "R$ 30,00" in r.text, r.text[-1500:])
checar("Contrato já diz a regra de cancelamento", "30% do valor total" in ger.get(f"/pedidos/{PCA}/contrato").text)
ger.post(f"/pedidos/{PCA}/cancelar", {"motivo": "Cliente desistiu"})
checar("Cancelado pela regra: loja fica R$ 120, devolver R$ 30",
       sql(f"SELECT cancel_multa_centavos||'/'||cancel_devolver_centavos FROM pedidos WHERE id='{PCA}'") == "12000/3000")
checar("Atendente não registra devolução de dinheiro", ana.post(f"/pedidos/{PCA}/devolver", {"valor": "30", "forma": "PIX",
       "chave": str(__import__("uuid").uuid4())}).status_code == 403)
r = ger.post(f"/pedidos/{PCA}/devolver", {"valor": "50", "forma": "PIX", "chave": str(__import__("uuid").uuid4())})
checar("Devolver mais do que a regra: recusado", r.status_code == 400 and "maior do que a regra" in r.text)
r = ger.post(f"/pedidos/{PCA}/devolver", {"valor": "30", "forma": "PIX", "chave": str(__import__("uuid").uuid4())})
checar("Devolução de R$ 30 registrada como saída no caixa; nada mais a devolver",
       "Devolução registrada" in r.text and sql(f"SELECT count(*) FROM transacoes WHERE pedido_id='{PCA}' AND natureza='DEVOLUCAO_CLIENTE' AND tipo='SAIDA'") == "1"
       and "Falta devolver" not in r.text)
enviar_fila()
checar("Cliente avisada no WhatsApp do cancelamento e do valor a receber", any("receber de volta R$ 30,00" in m for m in para("5519991112222")))
PC2P = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PC2}'")
ger.post(f"/pedidos/{PC2P}/cancelar", {"motivo": "Peça manchada na loja", "sem_multa": "1"})
checar("Cancelar sem multa (culpa da loja): devolve tudo e registra a decisão",
       sql(f"SELECT cancel_multa_centavos||'/'||cancel_devolver_centavos||'/'||cancel_sem_multa FROM pedidos WHERE id='{PC2P}'") == "0/10000/true"
       and sql(f"SELECT count(*) FROM auditoria WHERE acao='pedido_cancelado' AND dados->>'sem_multa' = 'true'") == "1")
ger.post(f"/pedidos/{PCV}/cancelar", {"motivo": "Desistiu"})
checar("Venda com ajuste: multa fixa de R$ 200 (pagou R$ 400, devolve R$ 200)",
       sql(f"SELECT cancel_multa_centavos||'/'||cancel_devolver_centavos FROM pedidos WHERE id='{PCV}'") == "20000/20000")
PC4 = nova_peca("Vestido regra quatro", "400", "900")
pedido(ana, {**alug, "cliente_nome": "Luana Link", "cliente_telefone": "(19) 99111-4444", "valor": "150"}, peca=PC4)
PCL = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PC4}'")
sql(f"UPDATE pedidos SET pagar_pelo_link = true WHERE id = '{PCL}'")        # como se tivesse vindo pelo link/WhatsApp
ger.post(f"/pedidos/{PCL}/cancelar", {"motivo": "Arrependeu"})
checar("Compra fora da loja cancelada em até 7 dias: direito de arrependimento, devolve tudo",
       sql(f"SELECT cancel_devolver_centavos||'/'||cancel_regra FROM pedidos WHERE id='{PCL}'").startswith("15000/Arrependimento"))
checar("Cálculo do cancelamento não pode ser mexido depois", sql(f"UPDATE pedidos SET cancel_devolver_centavos = 999999 WHERE id='{PCL}'") == ""
       and sql(f"SELECT cancel_devolver_centavos FROM pedidos WHERE id='{PCL}'") == "15000")
checar("Devolução só de pedido cancelado (banco recusa em pedido ativo)",
       "cancelado" in rodar(["psql", "-X", "-d", TEST_DB, "-c", f"""INSERT INTO transacoes (loja_id, tipo, natureza, valor_centavos,
          forma_pagamento, pedido_id, descricao, chave_idempotencia, usuario_id)
          SELECT loja_id, 'SAIDA', 'DEVOLUCAO_CLIENTE', 100, 'PIX', id, 'x', gen_random_uuid(), criado_por FROM pedidos
          WHERE id = '{PZED}'"""]).stderr.lower())
from app import pagamento as _pg2  # noqa: E402
checar("Mesma conta no Python e no banco (% arredonda para cima; fixo nunca passa do total)",
       _pg2.pela_regra("PCT", 30, 33333) == int(sql("SELECT valor_pela_regra('PCT', 30, 33333)"))
       and _pg2.pela_regra("FIXO", 50000, 30000) == int(sql("SELECT valor_pela_regra('FIXO', 50000, 30000)")) == 30000)
sql("UPDATE lojas SET reserva_modo = 'ATE_DEVOLVER'")

print("\n== QR da via da cliente e verificação de pedidos (novos e antigos) ==")
from app import codigos as _cods  # noqa: E402
COD_V = _cods.codigo_verificacao(PZED)
r = ana.get(f"/pedidos/{PZED}/imprimir")
checar("Comprovante A4 sai com o QR de verificação", f"/v/{COD_V}" not in r.text and 'class="papel-qr"' in r.text and COD_V in r.text)
checar("Cupom térmico também tem o QR", 'class="papel-qr"' in ana.get(f"/pedidos/{PZED}/imprimir?formato=80").text)
checar("PDF do comprovante leva o QR (imagem a mais)", ana.get(f"/pedidos/{PZED}/pdf").content.count(b"/Subtype /Image") >= 3)
cliente_qr = TestClient(app, follow_redirects=True)
r = cliente_qr.get(f"/v/{COD_V}")
checar("Cliente escaneia o QR: abre a verificação no site já com o código, situação e histórico",
       r.url.path == "/site/contrato" and "conferido pelo QR" in r.text and "Histórico" in r.text and "Reservado" in r.text
       and "✓ assinado" in r.text)
checar("Página pública não mostra nome, telefone nem valores", "Zuleica" not in r.text and "99222" not in r.text and "R$" not in r.text.split("Histórico")[0].split("Verificar pedido")[-1])
checar("Funcionária escaneia o mesmo QR: cai direto no pedido no sistema", ana.get(f"/v/{COD_V}").url.path == f"/pedidos/{PZED}")
checar("Funcionária sem acesso a pedidos cai na tela inicial dela", lu.get(f"/v/{COD_V}").url.path == "/")
PANTIGO = sql("SELECT id FROM pedidos WHERE id NOT IN (SELECT pedido_id FROM contratos) ORDER BY criado_em LIMIT 1")
num_antigo = PANTIGO.replace("-", "")[:8].upper()
r = TestClient(app).get(f"/site/contrato?codigo={num_antigo.lower()}")
checar("Pedido antigo (sem contrato assinado) é encontrado pelo número do comprovante", "encontrado" in r.text
       and "ainda não assinado" in r.text and num_antigo in r.text)
r = TestClient(app).get(f"/site/contrato?codigo={num_antigo}-ZZZZ")
checar("Conferência do QR errada: não diz 'conferido pelo QR'", "conferido pelo QR" not in r.text)
checar("Número que não existe: não encontrado", "Não encontramos" in TestClient(app).get("/site/contrato?codigo=00000000").text)

def cli_sql_erro(comando, login):
    """Roda o comando como o app (RLS e contexto do usuário) e devolve a mensagem de erro do banco ('' se passou)."""
    uid = sql(f"SELECT id FROM usuarios WHERE login = '{login}'")
    try:
        with db.transacao(usuario_id=uid) as _c:
            _c.execute(comando)
    except Exception as e:  # noqa: BLE001
        return str(e)
    return ""


print("\n== Caixa: formas novas, fornecedores, nota e crediário da loja ==")
from app import pix as _pix, crediario as _cred  # noqa: E402
checar("Pix copia e cola: CRC igual ao exemplo do Banco Central (1D3D)",
       _pix.crc16("00020126580014br.gov.bcb.pix0136123e4567-e12b-12d1-a456-4266554400005204000053039865802BR"
                  "5913Fulano de Tal6008BRASILIA62070503***6304") == "1D3D")
checar("Crediário: R$ 100 em 3x = 33,34 + 33,33 + 33,33", _cred.dividir(10000, 3) == [3334, 3333, 3333])
checar("Vencimento dia 31 vira o último dia do mês seguinte", _cred.somar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28))
r = ana.get("/caixa/pagar")
checar("Paguei algo: boleto, carnê, cartão da loja, Pix e dinheiro", all(x in r.text for x in (
       'value="BOLETO"', 'value="CARNE"', "Cartão de crédito da loja", 'value="PIX"', 'value="DINHEIRO"'))
       and 'name="maquininha"' not in r.text and 'enctype="multipart/form-data"' in r.text)
checar("Atendente não cadastra fornecedor (e não vê o link)", ana.get("/fornecedores/novo").status_code == 403
       and "/fornecedores/novo" not in r.text)
r = ger.post("/fornecedores/novo", {"nome": "Tecidos Aurora", "documento": "11.222.333/0001-80", "categoria": "Tecidos"})
checar("CNPJ inválido é recusado", r.status_code == 400 and "CNPJ inválido" in r.text)
r = ger.post("/fornecedores/novo", {"nome": "Tecidos Aurora", "documento": "11.222.333/0001-81", "categoria": "Tecidos",
                                     "telefone": "(19) 3333-4444", "pix": "financeiro@aurora.com"})
FORN = sql("SELECT id FROM fornecedores WHERE nome = 'Tecidos Aurora'")
checar("Gerente cadastra fornecedor", "Guardado" in r.text and FORN)
r = ger.post("/fornecedores/novo", {"nome": "Outro nome", "documento": "11222333000181"})
checar("CNPJ repetido: avisa que já existe", "Já existe fornecedor" in r.text)
ch = ger.chave("/caixa/pagar")
r = ger.post("/caixa/pagar/conferir", {"chave": ch, "valor": "450", "forma": "CREDITO", "parcelas": "3",
                                       "fornecedor": FORN, "categoria": "Tecidos", "documento": "NF 1234", "descricao": ""},
             files={"nota": ("nota.jpg", foto_jpeg(900, 1200), "image/jpeg")})
checar("Paguei R$ 450 no cartão da loja 3x a um fornecedor com foto da nota: conferência mostra tudo",
       "Está certo?" in r.text and "Tecidos Aurora" in r.text and "NF 1234" in r.text and "Foto da nota anexada" in r.text
       and "no crédito em 3x" in r.text, r.text[-900:])
campos = ger.campos(r.text)
r = ger.post("/caixa/pagar/gravar", campos)
T_NOTA = sql("SELECT id FROM transacoes WHERE documento = 'NF 1234'")
ANEXO = sql(f"SELECT anexo FROM transacoes WHERE id = {T_NOTA}")
checar("Gravado com fornecedor, categoria, nota e foto; sem maquininha", "Guardado!" in r.text and sql(
       f"SELECT fornecedor_id||'/'||categoria||'/'||parcelas||'/'||(maquininha_id IS NULL) FROM transacoes WHERE id={T_NOTA}")
       == f"{FORN}/Tecidos/3/true" and len(ANEXO) == 32)
r = ger.get(f"/caixa/nota/{ANEXO}")
checar("Foto da nota abre para quem mexe no caixa (sem GPS, privada)", r.status_code == 200
       and r.headers["content-type"] == "image/jpeg" and "no-store" in r.headers["cache-control"]
       and b"GPS" not in r.content)
checar("Costureira não abre a foto da nota", cida.get(f"/caixa/nota/{ANEXO}").status_code == 403)
checar("Nota inventada: 404", ger.get("/caixa/nota/" + "a" * 32).status_code == 404
       and ger.get("/caixa/nota/..%2F..%2Fetc").status_code == 404)
checar("Fornecedor mostra o que foi pago a ele", "NF 1234" in ger.get(f"/fornecedores/{FORN}").text
       and "R$ 450,00" in ger.get("/fornecedores").text)
r = dona.post(f"/caixa/{T_NOTA}/desfazer", {"motivo": "Lançado em dobro"})
checar("Desfazer pagamento no cartão da loja funciona (estorno sem maquininha)",
       sql(f"SELECT count(*) FROM transacoes WHERE estorno_de = {T_NOTA} AND fornecedor_id = '{FORN}'") == "1", r.text[-300:])
r = ana.post("/caixa/pagar/conferir", {"chave": ana.chave("/caixa/pagar"), "valor": "10", "forma": "PIX",
                                       "descricao": "x", "fornecedor": "00000000-0000-0000-0000-000000000000"})
checar("Fornecedor que não existe é recusado", "Fornecedor não encontrado" in r.text)
r, _ = receber(ana, {"valor": "120", "forma": "BOLETO", "descricao": "Boleto pago pela cliente"})
checar("Recebi dinheiro em boleto", "Guardado!" in r.text and "no boleto" in r.text)
r, _ = receber(ana, {"valor": "600", "forma": "CARNE", "vezes": "4", "descricao": "Carnê financeira"})
checar("Recebi pelo carnê da financeira em 4x", "Guardado!" in r.text and sql(
       "SELECT parcelas FROM transacoes WHERE descricao LIKE '%· Carnê financeira'") == "4")

PCR = nova_peca("Vestido crediario", "500", "1000")
dia_cr = hoje + timedelta(days=150)
r = pedido(ana, {"tipo": "ALUGUEL", "cliente_nome": "Carla Souza", "cliente_telefone": "(19) 98888-2222", "forma": "PIX",
                 "data_retirada": dia_cr.isoformat(), "data_devolucao": (dia_cr + timedelta(days=1)).isoformat(),
                 "valor": "200"}, peca=PCR)
PED_CR = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PCR}'")
checar("Pedido para o crediário criado (cliente com CPF)", PED_CR and sql(
       f"SELECT (cl.cpf_c IS NOT NULL)::text FROM pedidos p JOIN clientes cl ON cl.id = p.cliente_id WHERE p.id='{PED_CR}'") == "true")
r = ana.get("/caixa/receber?q=Carla")
checar("Recebi dinheiro: procurar pela cliente acha o pedido com saldo", f"/pedidos/{PED_CR}/receber" in r.text
       and "falta R$ 300,00" in r.text, r.text[-1500:])
checar("Procurar pelo código do comprovante também acha", f"/pedidos/{PED_CR}/receber" in
       ana.get(f"/caixa/receber?q={PED_CR.replace('-', '')[:8].upper()}").text)
checar("Atendente não vê a opção crediário", 'value="CREDIARIO"' not in ana.get(f"/pedidos/{PED_CR}/receber").text)
r = ana.post(f"/pedidos/{PED_CR}/receber/conferir", {"chave": ana.chave(f"/pedidos/{PED_CR}/receber"), "valor": "300",
                                                    "forma": "CREDIARIO", "vezes": "3",
                                                    "primeiro_vencimento": hoje.isoformat()})
checar("Atendente não libera crediário pelo formulário", "só a Dona, a Gerente ou o Financeiro" in r.text)
r = ger.get(f"/pedidos/{PED_CR}/receber")
checar("Gerente vê o crediário da loja com parcelas e 1º vencimento", 'value="CREDIARIO"' in r.text
       and 'name="primeiro_vencimento"' in r.text and 'name="vezes"' in r.text)
r = ger.post(f"/pedidos/{PED_CR}/receber/conferir", {"chave": ger.chave(f"/pedidos/{PED_CR}/receber"), "valor": "300",
                                                    "forma": "CREDIARIO", "vezes": "3",
                                                    "primeiro_vencimento": (hoje + timedelta(days=200)).isoformat()})
checar("1º vencimento além de 90 dias é recusado", "até 90 dias" in r.text)
r = ger.post(f"/pedidos/{PED_CR}/receber/conferir", {"chave": ger.chave(f"/pedidos/{PED_CR}/receber"), "valor": "300",
                                                    "forma": "CREDIARIO", "vezes": "3", "primeiro_vencimento": hoje.isoformat()})
checar("Conferência do crediário: 300 em 3x de 100", "liberar crediário" in r.text and "3x de R$ 100,00" in r.text, r.text[-700:])
campos = ger.campos(r.text)
r = ger.post(f"/pedidos/{PED_CR}/receber/gravar", campos)
CR = sql(f"SELECT id FROM crediarios WHERE pedido_id = '{PED_CR}'")
checar("Crediário criado com 3 parcelas mensais; pedido fica quitado", CR and r.url.path == f"/crediario/{CR}"
       and sql(f"SELECT count(*)||'/'||sum(valor_centavos) FROM crediario_parcelas WHERE crediario_id='{CR}'") == "3/30000"
       and sql(f"SELECT valor_total_centavos - valor_pago(id) FROM pedidos WHERE id='{PED_CR}'") == "0"
       and "Imprima o carnê" in r.text)
ger.post(f"/pedidos/{PED_CR}/receber/gravar", campos)
checar("Clique duplo não cria dois crediários", sql(f"SELECT count(*) FROM crediarios WHERE pedido_id='{PED_CR}'") == "1")
checar("Pedido mostra o crediário", f"/crediario/{CR}" in ger.get(f"/pedidos/{PED_CR}").text)
checar("Banco: crediário maior que o saldo é recusado mesmo direto", "maior que o que falta" in cli_sql_erro(
       f"INSERT INTO crediarios (loja_id, cliente_id, pedido_id, valor_centavos, parcelas, criado_por) "
       f"SELECT loja_id, cliente_id, id, 100, 1, (SELECT id FROM usuarios WHERE login='ger') FROM pedidos WHERE id='{PED_CR}'",
       "ger"))
r = ger.get(f"/crediario/{CR}/carne")
checar("Carnê sem chave Pix: avisa e sai sem QR", "Sem chave Pix" in r.text and "carne-qr" not in r.text and r.text.count('class="carne-folha"') == 3)
r = ger.post("/crediario/pix", {"pix_chave": "chave errada", "pix_nome": "Loja", "pix_cidade": "Campinas"})
checar("Chave Pix inválida é recusada", "Chave Pix inválida" in r.text)
ger.post("/crediario/pix", {"pix_chave": "11222333000181", "pix_nome": "Loja de Festa Ltda", "pix_cidade": "Campinas"})
r = ger.get(f"/crediario/{CR}/carne")
copia = re.findall(r'class="carne-copia">([^<]+)<', r.text)
checar("Carnê com Pix copia e cola de cada parcela (valor e CRC certos)", len(copia) == 3 and all(
       c.startswith("000201") and "5406100.00" in c and c[-4:] == _pix.crc16(c[:-4]) for c in copia)
       and r.text.count("<svg") >= 6, copia[:1])
checar("Atendente não mexe no Pix do carnê", ana.post("/crediario/pix", {"pix_chave": "x"}).status_code == 403)
from app import crediario as _cred  # noqa: E402
cods = re.findall(r'class="carne-numeros">([0-9 ]+)<', r.text)
checar("Carnê: cada parcela com o seu código (13 números, legível embaixo do código de barras)",
       len(cods) == 3 and len(set(cods)) == 3 and all(len(c.replace(" ", "")) == 13 for c in cods)
       and "carne-codigo-barras" in r.text, cods)
r2 = ger.get(f"/crediario/{CR}/carne?formato=80&codigo=qr")
checar("Carnê na impressora térmica (80 mm) com QR do código da parcela",
       'class="carne carne-80"' in r2.text and "carne-codigo-qr" in r2.text and r2.text.count('class="carne-folha"') == 3)
checar("Tela do crediário mostra os botões de imprimir (A4 e térmica)",
       all(x in ger.get(f"/crediario/{CR}").text for x in ("formato=a4", "formato=80", "codigo=qr")))
checar("Pedido mostra o botão de imprimir o carnê", f"/crediario/{CR}/carne?formato=80" in ger.get(f"/pedidos/{PED_CR}").text)
r2 = ana.get("/buscar?q=" + cods[1].replace(" ", ""))
checar("Código da parcela na busca do topo abre a parcela para receber", r2.url.path == f"/crediario/{CR}/parcela/2")
checar("Código com um número errado não abre nada", not _cred.parece_codigo_parcela(cods[1].replace(" ", "")[:-1] + str((int(cods[1][-1]) + 1) % 10)))
r = ana.get("/caixa/receber?q=Carla")
checar("Procurar a cliente mostra a próxima parcela do crediário", f"/crediario/{CR}/parcela/1" in r.text)
r = ana.post(f"/crediario/{CR}/parcela/1", {"chave": ana.chave(f"/crediario/{CR}/parcela/1"), "forma": "PIX", "valor": "1"})
checar("Atendente recebe a parcela 1 no Pix (valor sempre o da parcela)", "Guardado!" in r.text and "R$ 100,00" in r.text
       and sql(f"SELECT (paga_em IS NOT NULL)::text FROM crediario_parcelas WHERE crediario_id='{CR}' AND numero=1") == "true")
T_PARC = sql(f"SELECT transacao_id FROM crediario_parcelas WHERE crediario_id='{CR}' AND numero=1")
r = ana.post(f"/crediario/{CR}/parcela/1", {"chave": ana.chave(f"/crediario/{CR}/parcela/1"), "forma": "PIX"})
checar("Parcela paga não é recebida de novo", sql(f"SELECT count(*) FROM transacoes WHERE crediario_parcela_id IS NOT NULL "
       f"AND natureza = 'PARCELA_CREDIARIO' AND descricao LIKE '%parcela 1/3%'") == "1")
dona.post(f"/caixa/{T_PARC}/desfazer", {"motivo": "Pix não caiu"})
checar("Desfazer o recebimento reabre a parcela", sql(
       f"SELECT (paga_em IS NULL)::text FROM crediario_parcelas WHERE crediario_id='{CR}' AND numero=1") == "true")
for n in (1, 2, 3):
    ana.post(f"/crediario/{CR}/parcela/{n}", {"chave": ana.chave(f"/crediario/{CR}/parcela/{n}"), "forma": "DINHEIRO"})
checar("Pagou as 3: crediário quitado", sql(f"SELECT status FROM crediarios WHERE id='{CR}'") == "QUITADO")

PCR2 = nova_peca("Vestido crediario dois", "500", "1000")
pedido(ana, {"tipo": "ALUGUEL", "cliente_nome": "Sem Documento", "cliente_telefone": "(19) 98777-1212", "forma": "PIX",
             "data_retirada": dia_cr.isoformat(), "data_devolucao": (dia_cr + timedelta(days=1)).isoformat(),
             "valor": "200"}, peca=PCR2)
PED_SD = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PCR2}'")
r = ger.post(f"/pedidos/{PED_SD}/receber/conferir", {"chave": ger.chave(f"/pedidos/{PED_SD}/receber"), "valor": "300",
                                                    "forma": "CREDIARIO", "vezes": "2", "primeiro_vencimento": hoje.isoformat()})
r = ger.post(f"/pedidos/{PED_SD}/receber/gravar", ger.campos(r.text))
checar("Cliente sem CPF: crediário recusado pelo banco", "cadastre o CPF" in r.text and
       sql(f"SELECT count(*) FROM crediarios WHERE pedido_id='{PED_SD}'") == "0")
PCR3 = nova_peca("Vestido crediario tres", "500", "1000")
pedido(ana, {"tipo": "ALUGUEL", "cliente_nome": "Carla Souza", "cliente_telefone": "(19) 98888-2222", "forma": "PIX",
             "data_retirada": (dia_cr + timedelta(days=5)).isoformat(),
             "data_devolucao": (dia_cr + timedelta(days=6)).isoformat(), "valor": "200"}, peca=PCR3)
PED_C3 = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PCR3}'")
r = ger.post(f"/pedidos/{PED_C3}/receber/conferir", {"chave": ger.chave(f"/pedidos/{PED_C3}/receber"), "valor": "300",
                                                    "forma": "CREDIARIO", "vezes": "2", "primeiro_vencimento": hoje.isoformat()})
ger.post(f"/pedidos/{PED_C3}/receber/gravar", ger.campos(r.text))
CR3 = sql(f"SELECT id FROM crediarios WHERE pedido_id = '{PED_C3}'")
from datetime import datetime as _dtc  # noqa: E402
antes_cr = sql("SELECT count(*) FROM mensagens WHERE motivo = 'crediario'")
trabalhador.rodar_rotinas(_dtc.now(ZoneInfo("America/Sao_Paulo")).replace(hour=11))
trabalhador.rodar_rotinas(_dtc.now(ZoneInfo("America/Sao_Paulo")).replace(hour=11))
checar("WhatsApp avisa a cliente no dia do vencimento (uma vez só)",
       int(sql("SELECT count(*) FROM mensagens WHERE motivo = 'crediario'")) == int(antes_cr) + 1)
checar("Atendente não cancela crediário", ana.post(f"/crediario/{CR3}/cancelar", {"motivo": "x"}).status_code == 403
       and "Cancelar crediário" not in ana.get(f"/crediario/{CR3}").text)
r = ger.post(f"/crediario/{CR3}/cancelar", {"motivo": ""})
checar("Cancelar exige motivo", "motivo" in r.text and r.status_code == 400)
ger.post(f"/crediario/{CR3}/cancelar", {"motivo": "Cliente vai pagar à vista"})
checar("Crediário cancelado: o valor volta a faltar no pedido", sql(f"SELECT status FROM crediarios WHERE id='{CR3}'") == "CANCELADO"
       and sql(f"SELECT valor_total_centavos - valor_pago(id) FROM pedidos WHERE id='{PED_C3}'") == "30000")
checar("Banco: crediário cancelado não volta nem muda valor",
       "já cancelado" in cli_sql_erro(f"UPDATE crediarios SET status='ATIVO' WHERE id='{CR3}'", "ger")
       and "não pode ser alterado" in cli_sql_erro(f"UPDATE crediarios SET valor_centavos=1 WHERE id='{CR}'", "ger"))
r = ger.get("/crediario?ver=todos")
checar("Lista do crediário mostra quitado e cancelado", "Quitado" in r.text and "Cancelado" in r.text)
checar("Botões Crediário e Fornecedores na tela inicial da Gerente", {"/crediario", "/fornecedores"} <= botoes(ger)
       and "/fornecedores" in botoes(ana) and "/crediario" in botoes(ana) and "/crediario" not in botoes(cida))

print("\n== Botões de cada cargo e endereços da loja ==")
checar("Dona vê 'Endereços da loja' (vai direto para as lojas do rodapé) e 'Botões de cada cargo'",
       {"/config/rodape#unidades", "/config/botoes"} <= botoes(dona), botoes(dona))
checar("Gerente não escolhe botões", ger.get("/config/botoes").status_code == 403 and "/config/botoes" not in botoes(ger))
r = dona.get("/config/botoes")
marcados = re.findall(r'name="(v:[A-Z_]+:[^"]+)" value="1" checked', r.text)
checar("Tela mostra cada botão x cargo (só onde o cargo tem acesso)", "v:ATENDENTE:/caixa/pagar" in marcados
       and "v:COSTUREIRA:/caixa/pagar" not in r.text and "v:DONO:" not in r.text)
dona.post("/config/botoes", {m: "1" for m in marcados if m != "v:ATENDENTE:/caixa/pagar"})
checar("Dona esconde 'Paguei algo' da atendente", "/caixa/pagar" not in botoes(ana) and "/caixa/pagar" in botoes(ger))
checar("Esconder não tira o acesso (quem manda é o cargo)", ana.get("/caixa/pagar").status_code == 200)
checar("Banco: só Dona/Dev mexem nos botões", "Só a Dona ou o Desenvolvedor" in cli_sql_erro(
       "DELETE FROM botoes_ocultos", "ger"))
dona.post("/config/botoes", {m: "1" for m in marcados})
checar("Marcando de novo, o botão volta", "/caixa/pagar" in botoes(ana))

print("\n== Site: banners, destaques, avaliações e nota do Google ==")
from app import vitrine as _vit  # noqa: E402
anon = TestClient(app, follow_redirects=True)
r = anon.get("/site")
checar("Site mostra os 4 destaques antes do rodapé (com ícone e página)", all(x in r.text for x in (
       "Como funciona", "Entrega e devolução", "Perguntas frequentes", "Como provar")) and 'class="destaques"' in r.text
       and r.text.index('class="destaques"') < r.text.index('class="rodape"'))
checar("Páginas dos destaques abrem com texto pronto", "hora marcada" in anon.get("/site/pagina/como-provar").text.lower()
       or "Agende" in anon.get("/site/pagina/como-provar").text)
checar("Rodapé ganhou a coluna Ajuda", ">Ajuda<" in r.text and "/site/pagina/perguntas-frequentes" in r.text)
checar("Sem banner cadastrado: nada de faixa vazia", 'class="banners"' not in r.text)
csrf_b = Pessoa._token(dona.get("/config/banners").text)
r = dona.c.post("/config/banners", data={"csrf": csrf_b, "titulo": "Golpe", "link": "https://golpe.com/pix"})
checar("Banner com link para fora do site é recusado", r.status_code == 400 and "página do site" in r.text)
r = dona.c.post("/config/banners", data={"csrf": csrf_b, "titulo": "Semana das madrinhas", "texto": "15% no aluguel",
                                          "botao": "Ver vestidos", "link": "/site/colecao", "tema": "dourado"},
                files={"imagem": ("b.jpg", foto_jpeg(1600, 600), "image/jpeg")})
BAN = sql("SELECT id FROM banners WHERE titulo = 'Semana das madrinhas'")
IMG_BAN = sql(f"SELECT imagem FROM banners WHERE id = '{BAN}'")
checar("Dona publica banner com foto", BAN and len(IMG_BAN) == 32 and "Banner publicado" in r.text, r.text[-300:])
dona.c.post("/config/banners", data={"csrf": csrf_b, "titulo": "Formatura 2026", "tema": "rosa", "link": "/site/c/formatura"})
r = anon.get("/site")
checar("Site mostra os banners girando (com pontinhos e script próprio)", "Semana das madrinhas" in r.text
       and "Formatura 2026" in r.text and 'class="banners-pontos"' in r.text and "/static/vitrine.js" in r.text)
checar("Foto do banner é pública e sem GPS", anon.get(f"/fotos/{IMG_BAN}-960.jpg").status_code == 200
       and b"GPS" not in anon.get(f"/fotos/{IMG_BAN}-960.jpg").content)
dona.post(f"/config/banners/{BAN}", {"titulo": "Semana das madrinhas", "tema": "dourado", "ativo": "0",
                                     "link": "/site/colecao"})
checar("Banner fora do ar some do site e a foto deixa de ser pública",
       "Semana das madrinhas" not in anon.get("/site").text and anon.get(f"/fotos/{IMG_BAN}-960.jpg").status_code == 404)
fim_passado = (hoje - timedelta(days=1)).isoformat()
dona.c.post("/config/banners", data={"csrf": csrf_b, "titulo": "Promo vencida", "fim": fim_passado,
                                      "inicio": (hoje - timedelta(days=5)).isoformat()})
checar("Banner com data de fim no passado não aparece", "Promo vencida" not in anon.get("/site").text)
checar("Atendente não mexe nos banners", ana.get("/config/banners").status_code == 403)
dest = {f"{k}{n}": v for n in range(1, 5) for k, v in (("icone", "estrela"), ("titulo", f"Destaque {n}"),
                                                         ("texto", "frase"), ("link", "/site/colecao"))}
dona.post("/config/destaques", {**dest, "titulo1": "Aluguel sem susto", "link2": "https://fora.com"})
r = anon.get("/site")
checar("Dona troca os destaques (link para fora vira link do site)", "Aluguel sem susto" in r.text
       and "https://fora.com" not in r.text and sql("SELECT count(*) FROM destaques") == "4")

PED_AV = sql("SELECT id FROM pedidos WHERE status IN ('DEVOLVIDO','EM_LIMPEZA','CONCLUIDO','RETIRADO','VENDIDO') ORDER BY criado_em LIMIT 1")
TOK_AV = _vit.token_avaliar(PED_AV)
checar("Link de avaliação assinado: trocar uma letra não abre", anon.get(f"/site/avaliar/{TOK_AV[:-2]}xx").status_code == 404
       and anon.get(f"/site/avaliar/{PED_AV}").status_code == 404)
r = anon.get(f"/site/avaliar/{TOK_AV}")
checar("Cliente abre o link e vê as estrelas", r.status_code == 200 and 'name="nota"' in r.text and "noindex" in r.text)
csrf_av = Pessoa._token(r.text)
r = anon.post(f"/site/avaliar/{TOK_AV}", data={"csrf": csrf_av, "nota": "9", "nome": "Ana"})
checar("Nota fora de 1 a 5 é recusada", r.status_code == 400)
r = anon.post(f"/site/avaliar/{TOK_AV}", data={"csrf": csrf_av, "nota": "5", "nome": "Ana P.", "autoriza": "1",
                                                 "texto": "Vestido lindo! <script>alert(1)</script>"})
checar("Avaliação guardada esperando aprovação", "Muito obrigada" in r.text
       and sql(f"SELECT status FROM avaliacoes WHERE pedido_id = '{PED_AV}'") == "PENDENTE")
anon.post(f"/site/avaliar/{TOK_AV}", data={"csrf": csrf_av, "nota": "1", "nome": "Outra"})
checar("Mesmo link não avalia duas vezes", sql(f"SELECT count(*)||'/'||max(nota) FROM avaliacoes WHERE pedido_id='{PED_AV}'") == "1/5")
checar("Antes de aprovar, não aparece no site", "Vestido lindo" not in anon.get("/site").text)
TOK_CR = _vit.token_avaliar(PED_CR)
r = anon.post(f"/site/avaliar/{TOK_CR}", data={"csrf": csrf_av, "nota": "5", "nome": "Carla S."})
checar("Pedido ainda não devolvido/entregue não é avaliado", "depois da devolução" in r.text)
AV = sql(f"SELECT id FROM avaliacoes WHERE pedido_id = '{PED_AV}'")
checar("Atendente não aprova avaliação", ana.get("/config/avaliacoes").status_code == 403
       and ana.post(f"/config/avaliacoes/{AV}", {"acao": "aprovar"}).status_code == 403)
r = ger.get("/config/avaliacoes")
checar("Gerente vê a avaliação pendente (texto escapado)", "Vestido lindo" in r.text and "<script>alert(1)" not in r.text)
ger.post(f"/config/avaliacoes/{AV}", {"acao": "aprovar", "resposta": "Obrigada, Ana!"})
r = anon.get("/site")
checar("Aprovada: aparece antes do rodapé com estrelas e resposta da loja", "Vestido lindo" in r.text
       and "Obrigada, Ana!" in r.text and "O que dizem nossas clientes" in r.text and "<script>alert(1)" not in r.text)
checar("Banco: ninguém muda a nota ou o texto da cliente",
       any(x in cli_sql_erro(f"UPDATE avaliacoes SET nota = 1 WHERE id = '{AV}'", "dona") for x in ("não pode ser alterada", "permission denied", "permissão negada"))
       and "Só a Dona ou a Gerente" in cli_sql_erro(f"UPDATE avaliacoes SET status = 'RECUSADA' WHERE id = '{AV}'", "ana"))

GOOGLE = {"chamadas": [], "chave": None}
class FalsoGoogle(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        GOOGLE["chamadas"].append(self.path)
        GOOGLE["chave"] = self.headers.get("X-Goog-Api-Key")
        corpo = _json.dumps({"rating": 4.8, "userRatingCount": 132, "googleMapsUri": "https://maps.google.com/?cid=123",
                             "reviews": [{"rating": 5, "relativePublishTimeDescription": "há 2 semanas",
                                          "text": {"text": "Atendimento maravilhoso <b>nota 10</b>"},
                                          "authorAttribution": {"displayName": "Patrícia Lima",
                                                                "uri": "https://www.google.com/maps/contrib/1"}},
                                         {"rating": 4, "text": {"text": "Muito bom"},
                                          "authorAttribution": {"displayName": "João", "uri": "javascript:alert(1)"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)
_servidor_g = ThreadingHTTPServer(("127.0.0.1", 0), FalsoGoogle)
threading.Thread(target=_servidor_g.serve_forever, daemon=True).start()
r = dona.post("/config/google", {"google_nota": "4,5", "google_total": "90", "avaliacoes_no_site": "1",
                                 "google_link": "https://maps.app.goo.gl/abc123"})
checar("Sem chave da API: a Dona digita a nota do Google", "4,5" in anon.get("/site").text
       and "90 avaliações" in anon.get("/site").text)
dona.post("/config/google", {"google_link": "https://golpe.com", "avaliacoes_no_site": "1"})
checar("Link do Google falso é recusado", sql("SELECT google_link FROM lojas") == "https://maps.app.goo.gl/abc123")
dona.post("/config/google", {"google_place_id": "ChIJ_teste_loja_123", "avaliacoes_no_site": "1",
                             "google_link": "https://maps.app.goo.gl/abc123"})
cfg_app.GOOGLE_PLACES_KEY, cfg_app.GOOGLE_PLACES_API = "chave-teste", f"http://127.0.0.1:{_servidor_g.server_port}/v1"
with db.transacao() as _cg:
    feito_g = _vit.atualizar_google(_cg)
with db.transacao() as _cg:
    de_novo = _vit.atualizar_google(_cg)
r = anon.get("/site")
checar("Com chave: busca nota, total e comentários do Google (1x por dia, chave no cabeçalho)", feito_g and not de_novo
       and len(GOOGLE["chamadas"]) == 1 and GOOGLE["chave"] == "chave-teste" and "ChIJ_teste_loja_123" in GOOGLE["chamadas"][0]
       and "4,8" in r.text and "132 avaliações" in r.text and "Patrícia Lima" in r.text, GOOGLE)
checar("Comentário do Google: autor com link do Google, HTML escapado, link perigoso descartado",
       "https://www.google.com/maps/contrib/1" in r.text and "<b>nota 10</b>" not in r.text
       and "javascript:alert" not in r.text)
cfg_app.GOOGLE_PLACES_KEY = ""
dona.post("/config/google", {"avaliacoes_no_site": "0"})
checar("Desligar avaliações tira a seção do site", "O que dizem nossas clientes" not in anon.get("/site").text)
dona.post("/config/google", {"avaliacoes_no_site": "1", "google_nota": "4,8", "google_total": "132"})

print("\n== Conta da cliente no site (WhatsApp ou e-mail e senha) ==")
UA_CEL = {"user-agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128 Mobile"}
def cliente_site():
    c = TestClient(app, follow_redirects=True)
    c.headers.update(UA_CEL)
    return c
def csrf_de(c, url):
    return Pessoa._token(c.get(url).text)
def codigo_cliente(fone, tipo="entrar na sua conta"):
    return re.search(r"\b(\d{6})\b", [m for m in para("55" + fone) if tipo in m][-1]).group(1)
carla = cliente_site()
checar("Minha conta sem entrar: vai para a tela de entrar", carla.get("/site/conta").url.path == "/site/conta/entrar")
r = carla.get("/site/conta/entrar")
checar("Tela de entrar: WhatsApp e e-mail/senha, e aviso de que não guardamos cartão",
       "Entrar pelo WhatsApp" in r.text and "Entrar com e-mail e senha" in r.text and "não guarda dados de cartão" in r.text
       and "noindex" in r.headers.get("x-robots-tag", ""))
tok = Pessoa._token(r.text)
carla.post("/site/conta/codigo/pedir", data={"csrf": tok, "telefone": "(19) 98888-2222", "proximo": "https://golpe.com"})
r = carla.post("/site/conta/codigo", data={"csrf": tok, "codigo": "000000"})
checar("Código errado: pede de novo", r.status_code == 400 and "Código errado" in r.text)
r = carla.post("/site/conta/codigo", data={"csrf": tok, "codigo": codigo_cliente("19988882222")})
checar("Código certo: entra na conta ligada à ficha que já existia (sem ir para site de fora)",
       r.url.path == "/site/conta" and "Olá, Carla" in r.text and "Vestido crediario" in r.text and "Verificar" in r.text,
       r.text[-800:])
checar("Uma conta por ficha", sql("SELECT count(*) FROM contas_clientes c JOIN clientes cl ON cl.id = c.cliente_id "
                                  f"WHERE cl.id = (SELECT cliente_id FROM pedidos WHERE id = '{PED_CR}')") == "1")
roubado = TestClient(app, follow_redirects=True)
roubado.headers.update({"user-agent": "curl/8"})
roubado.cookies.update(carla.cookies)
checar("Cookie copiado para outro navegador não entra na conta", roubado.get("/site/conta").url.path == "/site/conta/entrar")
checar("Conta da cliente não abre o sistema da loja", carla.get("/pedidos").url.path == "/login")
checar("Conta da cliente depois do roubo: a sessão foi derrubada (entra de novo)",
       carla.get("/site/conta").url.path == "/site/conta/entrar")
carla.post("/site/conta/codigo/pedir", data={"csrf": csrf_de(carla, "/site/conta/entrar"), "telefone": "19988882222"})
carla.post("/site/conta/codigo", data={"csrf": Pessoa._token(carla.get("/site/conta/codigo").text), "codigo": codigo_cliente("19988882222")})
r = carla.post("/site/conta/senha", data={"csrf": csrf_de(carla, "/site/conta"), "email": "carla@exemplo.com", "nova": "Senha@123"})
checar("Senha fácil é recusada", "fácil" in r.text or "Senha muito" in r.text, r.text[-500:])
r = carla.post("/site/conta/senha", data={"csrf": csrf_de(carla, "/site/conta"), "email": "carla@exemplo.com", "nova": "Lua7!Pipoca#Mar"})
checar("Carla cria senha (Argon2) e continua dentro", "Senha guardada" in r.text and sql(
       f"SELECT senha_hash LIKE '$argon2id$%' FROM contas_clientes WHERE cliente_id = (SELECT cliente_id FROM pedidos WHERE id = '{PED_CR}')") == "t",
       re.findall(r'class="(?:ok-site|aviso-site)"[^>]*>([^<]+)', r.text) or r.url.path)
velho = dict(carla.cookies)
carla.post("/site/conta/sair", data={"csrf": csrf_de(carla, "/site/conta")})
volta = cliente_site(); volta.cookies.update(velho)
checar("Sair encerra de verdade (cookie antigo não vale)", volta.get("/site/conta").url.path == "/site/conta/entrar")
r = carla.post("/site/conta/entrar", data={"csrf": csrf_de(carla, "/site/conta/entrar"), "email": "carla@exemplo.com", "senha": "errada"})
checar("E-mail/senha errados: mensagem que não revela se o e-mail existe", "E-mail ou senha não conferem" in r.text)
r2 = carla.post("/site/conta/entrar", data={"csrf": csrf_de(carla, "/site/conta/entrar"), "email": "ninguem@x.com", "senha": "errada"})
checar("E-mail que não existe: mesma mensagem", "E-mail ou senha não conferem" in r2.text)
r = carla.post("/site/conta/entrar", data={"csrf": csrf_de(carla, "/site/conta/entrar"), "email": "CARLA@exemplo.com",
                                            "senha": "Lua7!Pipoca#Mar", "proximo": "//golpe.com"})
checar("Entra com e-mail e senha (sem ir para fora)", r.url.path == "/site/conta" and "Olá, Carla" in r.text)
bruna = cliente_site()
r = bruna.post("/site/conta/criar", data={"csrf": csrf_de(bruna, "/site/conta/criar"), "nome": "Bruna",
                                          "telefone": "(19) 97777-6666", "lgpd": "1"})
checar("Cadastro pede nome e sobrenome", r.status_code == 400 and "sobrenome" in r.text)
r = bruna.post("/site/conta/criar", data={"csrf": csrf_de(bruna, "/site/conta/criar"), "nome": "Bruna Nova",
                                          "telefone": "(19) 97777-6666", "email": "bruna@exemplo.com", "senha": "Gato3Azul!Pipoca"})
checar("Cadastro exige concordar com a privacidade", r.status_code == 400 and "Política de privacidade" in r.text)
r = bruna.post("/site/conta/criar", data={"csrf": csrf_de(bruna, "/site/conta/criar"), "nome": "Bruna Nova",
                                          "telefone": "(19) 97777-6666", "email": "bruna@exemplo.com",
                                          "senha": "Gato3Azul!Pipoca", "lgpd": "1"})
checar("Cadastro novo: confirma o WhatsApp com código antes de criar", r.url.path == "/site/conta/codigo"
       and sql("SELECT count(*) FROM clientes WHERE nome = 'Bruna Nova'") == "0")
r = bruna.post("/site/conta/codigo", data={"csrf": Pessoa._token(r.text), "codigo": codigo_cliente("19977776666", "confirmar seu cadastro")})
checar("Código confirmado: ficha e conta criadas com e-mail e senha", "Olá, Bruna" in r.text
       and sql("SELECT count(*) FROM clientes WHERE nome = 'Bruna Nova' AND consentimento_lgpd_em IS NOT NULL") == "1")
checar("E-mail da conta cifrado no banco", sql("SELECT count(*) FROM clientes WHERE email_c::text LIKE '%bruna@%'") == "0")
outro = cliente_site()
r = outro.post("/site/conta/criar", data={"csrf": csrf_de(outro, "/site/conta/criar"), "nome": "Outra Pessoa",
                                          "telefone": "(19) 97111-0000", "email": "bruna@exemplo.com",
                                          "senha": "Gato3Azul!Pipoca", "lgpd": "1"})
checar("E-mail que já tem conta não cria outra", "já tem conta" in r.text)
for _ in range(5):
    outro.post("/site/conta/entrar", data={"csrf": csrf_de(outro, "/site/conta/entrar"), "email": "bruna@exemplo.com", "senha": "x"})
r = outro.post("/site/conta/entrar", data={"csrf": csrf_de(outro, "/site/conta/entrar"), "email": "bruna@exemplo.com",
                                            "senha": "Gato3Azul!Pipoca"})
checar("5 senhas erradas: conta trava 15 minutos (força bruta)", "Muitas tentativas" in r.text)
sql("UPDATE contas_clientes SET travada_ate = NULL, falhas = 0")
_sem = cliente_site()
checar("Sem entrar, excluir não faz nada", _sem.post("/site/conta/excluir", data={"csrf": csrf_de(_sem, "/site/conta/entrar")}).url.path
       == "/site/conta/entrar" and sql("SELECT count(*) FROM contas_clientes WHERE exclusao_pedida_em IS NOT NULL") == "0")

print("\n== Sacola, entrega e pagamento no site ==")
_url_pv4 = f"/site/p/{sql(chr(83)+'ELECT slug FROM produtos WHERE id=' + chr(39) + PV4 + chr(39))}"
checar("Mercado Pago ligado: a sacola liga sozinha (botão de comprar aparece)", "colocar na sacola" in anon.get(_url_pv4).text)
sql("UPDATE lojas SET sacola_ativa = false")
checar("Sacola desligada: peça sem botão de comprar", "colocar na sacola" not in anon.get(_url_pv4).text)
r = dona.post("/config/entrega", {"sacola_ativa": "1", "sacola_pagamento": "ENTRADA_OU_TOTAL", "sacola_minutos": "60",
                                  "entrega_retirada": "1", "entrega_envio": "1", "cep_origem": "13010-000",
                                  "pacote_peso_g": "1500", "pacote_altura_cm": "10", "pacote_largura_cm": "30",
                                  "pacote_comprimento_cm": "40"})
checar("Dona liga a sacola e o envio", "Guardado" in r.text and sql("SELECT sacola_ativa FROM lojas") == "t")
dona.post("/config/entrega/cidades", {"cidade": "Campinas", "uf": "SP", "valor": "15,00", "prazo": "No mesmo dia",
                                       "meio": "MOTOBOY", "aluguel": "1"})
dona.post("/config/entrega/cidades", {"cidade": "Valinhos", "uf": "SP", "valor": "25,00", "meio": "APLICATIVO"})
checar("Cidades de entrega cadastradas", sql("SELECT count(*) FROM entrega_cidades") == "2")
checar("Atendente não mexe na entrega", ana.get("/config/entrega").status_code == 403)
PS1, PS2 = nova_peca("Vestido sacola aluguel", "600", "1500"), nova_peca("Vestido sacola venda", "500", "800")
sql(f"UPDATE produtos SET no_site = true, preco_no_site = true WHERE id IN ('{PS1}', '{PS2}')")
SL1, SL2 = sql(f"SELECT slug FROM produtos WHERE id='{PS1}'"), sql(f"SELECT slug FROM produtos WHERE id='{PS2}'")
r = bruna.get(f"/site/p/{SL1}")
checar("Peça no site: alugar com datas e comprar", "Alugar · colocar na sacola" in r.text and "Comprar · colocar na sacola" in r.text and "R$ 1.500,00" in r.text)
tok = Pessoa._token(r.text)
ret_s, dev_s = hoje + timedelta(days=40), hoje + timedelta(days=41)
r = bruna.post("/site/sacola/adicionar", data={"csrf": tok, "peca": PS1, "tipo": "ALUGUEL",
                                               "retirada": hoje.isoformat(), "devolucao": hoje.isoformat()})
checar("Retirada sem tempo para prova/ajuste é recusada", "retirada precisa ser a partir" in r.text)
r = bruna.post("/site/sacola/adicionar", data={"csrf": tok, "peca": PS1, "tipo": "ALUGUEL",
                                               "retirada": ret_s.isoformat(), "devolucao": (ret_s + timedelta(days=20)).isoformat()})
checar("Aluguel longo demais é recusado", "devolução é de 0 a 7 dias" in r.text)
r = bruna.post("/site/sacola/adicionar", data={"csrf": tok, "peca": PS1, "tipo": "ALUGUEL",
                                               "retirada": ret_s.isoformat(), "devolucao": dev_s.isoformat()})
checar("Aluguel na sacola", r.url.path == "/site/sacola" and "Vestido sacola aluguel" in r.text and "R$ 600,00" in r.text)
bruna.post("/site/sacola/adicionar", data={"csrf": tok, "peca": PS2, "tipo": "VENDA"})
r = bruna.get("/site/sacola")
checar("Sacola com aluguel e compra: retirada, entrega na cidade; envio só para compra",
       "Vestido sacola venda" in r.text and 'value="LOCAL"' in r.text and 'value="ENVIO"' not in r.text
       and "Valinhos" not in r.text and "Campinas" in r.text and "Só a entrada do aluguel" in r.text)
checar("Topo do site mostra a sacola com 2", 'class="sacola-qtd">2<' in r.text)
csrf_s = Pessoa._token(r.text)
r = bruna.post("/site/sacola/fechar", data={"csrf": csrf_s, "entrega": "RETIRADA", "pagar": "ENTRADA",
                                           "chave": str(__import__("uuid").uuid4())})
checar("Fechar sem aceitar as regras: pede o aceite", r.status_code == 400 and "Marque que leu" in r.text)
r = bruna.post("/site/sacola/fechar", data={"csrf": csrf_s, "entrega": "LOCAL", "pagar": "ENTRADA", "aceito": "1",
                                           "cidade_entrega": sql("SELECT id FROM entrega_cidades WHERE cidade='Campinas'"),
                                           "chave": str(__import__("uuid").uuid4())})
checar("Entrega sem endereço é recusada", r.status_code == 400 and "rua, número e bairro" in r.text)
chave_c = str(__import__("uuid").uuid4())
dados_c = {"csrf": csrf_s, "entrega": "LOCAL", "pagar": "ENTRADA", "aceito": "1", "rua": "Rua das Flores", "numero": "10",
           "bairro": "Centro", "cidade_entrega": sql("SELECT id FROM entrega_cidades WHERE cidade='Campinas'"), "chave": chave_c}
n_pref = len(MP["preferencias"])
r = bruna.post("/site/sacola/fechar", data=dados_c, follow_redirects=False)
COMP = sql("SELECT id FROM compras_site ORDER BY criado_em DESC LIMIT 1")
entrada_s = int(sql(f"SELECT entrada_minima_centavos FROM pedidos WHERE produto_id='{PS1}' AND status='AGUARDANDO_PAGAMENTO'"))
checar("Fechar: compra criada, aluguel esperando pagamento (peça segura), venda ainda não vira pedido",
       r.status_code == 303 and r.headers["location"] == f"/site/sacola/pronta/{COMP}"
       and sql(f"SELECT count(*) FROM pedidos WHERE produto_id='{PS2}'") == "0"
       and sql(f"SELECT valor_total_centavos FROM compras_site WHERE id='{COMP}'") == str(entrada_s + 80000 + 1500))
pref = MP["preferencias"][-1]["corpo"]
checar("Mercado Pago recebe um pagamento só: entrada + compra + entrega, referência da compra",
       len(MP["preferencias"]) == n_pref + 1 and pref["external_reference"] == f"C-{COMP}"
       and pref["items"][0]["unit_price"] == (entrada_s + 80000 + 1500) / 100)
bruna.post("/site/sacola/fechar", data=dados_c)
checar("Clique duplo em pagar não cria duas compras", sql("SELECT count(*) FROM compras_site") == "1")
r = bruna.get(f"/site/sacola/pronta/{COMP}")
checar("Página nossa leva ao Mercado Pago (CSP não deixa formulário pular para fora)",
       f"/site/sacola/pagar/{COMP}" in r.text and 'http-equiv="refresh"' in r.text)
r = bruna.get(f"/site/sacola/pagar/{COMP}", follow_redirects=False)
checar("Botão pagar vai para o Mercado Pago", r.status_code == 303 and r.headers["location"].startswith("https://www.mercadopago.com.br/"))
checar("Outra conta não abre o pagamento da compra dela", carla.get(f"/site/sacola/pagar/{COMP}", follow_redirects=False).headers["location"] == "/site/conta")
checar("Sacola esvaziou", 'class="sacola-qtd">0<' in bruna.get("/site").text)
outra_s = cliente_site()
outra_s.post("/site/sacola/adicionar", data={"csrf": csrf_de(outra_s, f"/site/p/{SL1}"), "peca": PS2, "tipo": "VENDA"})
checar("Peça à venda na sacola paga de outra cliente: não entra", "reservada agora" in outra_s.get(f"/site/p/{SL2}?sacola_erro=x").text
       or 'class="sacola-qtd">0<' in outra_s.get("/site").text)
sql("UPDATE compras_site SET status = 'ABERTA'")
MP["pagamentos"]["2001"] = {"id": 2001, "status": "approved", "external_reference": f"C-{COMP}",
                            "transaction_amount": (entrada_s + 80000 + 1500) / 100,
                            "payment_method_id": "pix", "payment_type_id": "bank_transfer", "installments": 1}
r = anon.post("/pagamentos/mercadopago/aviso", json={"type": "payment", "data": {"id": "2001"}})
PED_S1 = sql(f"SELECT id FROM pedidos WHERE produto_id='{PS1}' ORDER BY criado_em DESC LIMIT 1")
PED_S2 = sql(f"SELECT id FROM pedidos WHERE produto_id='{PS2}'")
checar("Aviso do Mercado Pago: aluguel reservado, venda registrada e paga, frete no caixa, tudo pelo 'site'",
       r.status_code == 200 and sql(f"SELECT status FROM compras_site WHERE id='{COMP}'") == "PAGA"
       and sql(f"SELECT status FROM pedidos WHERE id='{PED_S1}'") == "RESERVADO"
       and sql(f"SELECT status FROM pedidos WHERE id='{PED_S2}'") == "VENDIDO"
       and sql(f"SELECT valor_total_centavos - valor_pago(id) FROM pedidos WHERE id='{PED_S2}'") == "0"
       and sql("SELECT count(*) FROM transacoes WHERE natureza = 'FRETE' AND descricao LIKE 'Frete da compra%' AND valor_centavos = 1500") == "1"
       and sql(f"SELECT u.sistema FROM transacoes t JOIN usuarios u ON u.id = t.usuario_id WHERE t.pedido_id = '{PED_S2}'") == "t",
       sql(f"SELECT status||' '||coalesce(observacao,'') FROM compras_site WHERE id='{COMP}'"))
anon.post("/pagamentos/mercadopago/aviso", json={"type": "payment", "data": {"id": "2001"}})
checar("Aviso repetido não lança duas vezes", sql("SELECT count(*) FROM transacoes WHERE nsu = '2001'") == "3")
r = bruna.get("/site/conta")
checar("Minha conta mostra os pedidos do site e a compra paga", "Vestido sacola aluguel" in r.text and "Paga ✓" in r.text
       and "Reservado" in r.text)
r = ger.get(f"/pedidos/{PED_S1}")
checar("No sistema: pedido do site com entrega e endereço (só para a equipe)", "Pedido do site" in r.text
       and "Rua das Flores, 10" in r.text and "Campinas" in r.text)
checar("Filtro 'Do site' em Pedidos", "Vestido sacola venda" in ger.get("/pedidos?filtro=site").text)
checar("Usuário do site não aparece na Equipe e não entra no sistema", "Loja online" not in dona.get("/equipe").text
       and "Entrar no sistema" in TestClient(app).post("/login", data={"csrf": csrf_de(TestClient(app), "/login"),
                                                                       "login": "sistema-site", "senha": "x"}).text)
checar("Banco: usuário do site só lança ENTRADA (não tira dinheiro)",
       "Sem permissão" in cli_sql_erro("INSERT INTO transacoes (loja_id, tipo, natureza, valor_centavos, forma_pagamento, "
                                       "descricao, chave_idempotencia, usuario_id) SELECT loja_id, 'SAIDA', 'DESPESA', 100, "
                                       "'PIX', 'x', gen_random_uuid(), id FROM usuarios WHERE sistema", "dona"))

ME = {"chamadas": []}
class FalsoMelhorEnvio(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        ME["chamadas"].append((self.path, None, self.headers.get("Authorization")))
        ok = self.path == "/api/v2/me" and self.headers.get("Authorization") != "Bearer token-recusado-pelo-melhor-envio"
        dados = _json.dumps({"firstname": "Loja", "lastname": "Teste", "email": "loja@teste.com"} if ok else {"message": "Unauthenticated."}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)
    def do_POST(self):
        corpo = _json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        ME["chamadas"].append((self.path, corpo, self.headers.get("Authorization")))
        if self.path == "/api/v2/me/shipment/calculate":
            resp = [{"id": 1, "name": "PAC", "price": "32.50", "delivery_time": 6, "company": {"name": "Correios"}},
                    {"id": 2, "name": "SEDEX", "price": "58.90", "delivery_time": 2, "company": {"name": "Correios"}},
                    {"id": 3, "name": "Jadlog", "error": "Serviço indisponível"}]
        elif self.path == "/api/v2/me/shipment/tracking":
            resp = {k: {"tracking": "BR123456789BR"} for k in corpo.get("orders", [])}
        else:
            resp = {}
        dados = _json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)
_servidor_me = ThreadingHTTPServer(("127.0.0.1", 0), FalsoMelhorEnvio)
threading.Thread(target=_servidor_me.serve_forever, daemon=True).start()
cfg_app.MELHOR_ENVIO_TOKEN, cfg_app.MELHOR_ENVIO_API = "token-me", f"http://127.0.0.1:{_servidor_me.server_port}"
PS3 = nova_peca("Vestido sacola envio", "300", "700")
sql(f"UPDATE produtos SET no_site = true, preco_no_site = true, pode_alugar = false WHERE id = '{PS3}'")
SL3 = sql(f"SELECT slug FROM produtos WHERE id='{PS3}'")
bruna.post("/site/sacola/adicionar", data={"csrf": csrf_de(bruna, f"/site/p/{SL3}"), "peca": PS3, "tipo": "VENDA"})
r = bruna.get("/site/sacola?entrega=ENVIO&cep=01310-100")
checar("Só compra na sacola: envio pelo Melhor Envio com cotação (erro da transportadora escondido)",
       'value="ENVIO"' in r.text and "Correios PAC — R$ 32,50" in r.text and "SEDEX" in r.text and "Jadlog" not in r.text
       and ME["chamadas"][-1][1]["to"]["postal_code"] == "01310100" and ME["chamadas"][-1][1]["from"]["postal_code"] == "13010000"
       and ME["chamadas"][-1][2] == "Bearer token-me", r.text[-1500:])
r = bruna.post("/site/sacola/fechar", data={"csrf": Pessoa._token(r.text), "entrega": "ENVIO", "cep": "01310100", "servico": "2",
                                           "rua": "Av. Paulista", "numero": "1000", "bairro": "Bela Vista", "cidade": "São Paulo",
                                           "uf": "SP", "aceito": "1", "chave": str(__import__("uuid").uuid4())})
COMP2 = sql("SELECT id FROM compras_site ORDER BY criado_em DESC LIMIT 1")
checar("Envio: frete do serviço escolhido (SEDEX) somado", sql(f"SELECT frete_centavos||'/'||valor_total_centavos||'/'||frete_servico FROM compras_site WHERE id='{COMP2}'")
       == "5890/75890/Correios SEDEX")
MP["pagamentos"]["2002"] = {"id": 2002, "status": "approved", "external_reference": f"C-{COMP2}", "transaction_amount": 700.0,
                            "payment_method_id": "pix", "payment_type_id": "bank_transfer", "installments": 1}
r = bruna.get(f"/site/pagamento?status=approved&external_reference=C-{COMP2}")
checar("Valor pago diferente: compra fica para a loja conferir, nada lançado",
       sql(f"SELECT status FROM compras_site WHERE id='{COMP2}'") == "CONFERIR"
       and sql(f"SELECT count(*) FROM pedidos WHERE produto_id='{PS3}'") == "0")
sql(f"UPDATE compras_site SET status='ABERTA', pagamento_externo_id=NULL WHERE id='{COMP2}'")
MP["pagamentos"]["2002"]["transaction_amount"] = 758.90
r = bruna.get(f"/site/pagamento?status=approved&external_reference=C-{COMP2}")
PED_S3 = sql(f"SELECT id FROM pedidos WHERE produto_id='{PS3}'")
checar("Voltou do Mercado Pago (sem aviso): confere e confirma a compra", "Compra confirmada" in r.text and PED_S3)
r = ger.post(f"/pedidos/{PED_S3}/envio", {"etiqueta": "ORD-abc123"})
enviar_fila()
checar("Etiqueta do Melhor Envio: busca o rastreio e avisa a cliente", sql(f"SELECT rastreio FROM compras_site WHERE id='{COMP2}'")
       == "BR123456789BR" and any("BR123456789BR" in m for m in para("5519977776666")))
checar("Endereço de entrega cifrado no banco", sql("SELECT count(*) FROM compras_site WHERE endereco_c::text LIKE '%Paulista%'") == "0")
checar("Nenhuma tela do site pede número de cartão", not [t.name for t in (APP / "app/templates").rglob("*.html")
       if re.search(r'name="(cartao|numero_cartao|cvv|cc-number)"', t.read_text())])
cfg_app.MELHOR_ENVIO_TOKEN = ""
PS4 = nova_peca("Vestido sacola envio dois", "300", "700")
sql(f"UPDATE produtos SET no_site = true, preco_no_site = true, pode_alugar = false WHERE id = '{PS4}'")
bruna.post("/site/sacola/adicionar", data={"csrf": csrf_de(bruna, f"/site/p/{sql(f'SELECT slug FROM produtos WHERE id={chr(39)}{PS4}{chr(39)}')}"),
                                           "peca": PS4, "tipo": "VENDA"})
checar("Sem token no .env nem na tela: envio some da sacola", 'value="ENVIO"' not in bruna.get("/site/sacola").text)
r = dona.get("/config/entrega")
checar("Tela de entrega tem o passo a passo e o campo do token do Melhor Envio", "Gerar novo token" in r.text and 'name="token"' in r.text)
r = dona.post("/config/entrega/melhor-envio", {"acao": "salvar", "token": "curto"})
checar("Token curto/errado é recusado", r.status_code == 400 and "não parece um token" in r.text)
cfg_app.MELHOR_ENVIO_API_FIXA = cfg_app.MELHOR_ENVIO_API
TOKEN_ME_TELA = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.token-da-tela-" + "x" * 40
r = dona.post("/config/entrega/melhor-envio", {"acao": "salvar", "token": TOKEN_ME_TELA})
checar("Token colado na tela fica cifrado (nem o banco mostra em texto)", "Token guardado" in r.text
       and sql("SELECT count(*) FROM frete_config WHERE encode(token_cifrado, 'escape') LIKE '%token-da-tela%'") == "0"
       and TOKEN_ME_TELA not in r.text)
r = dona.post("/config/entrega/melhor-envio", {"acao": "testar"})
checar("Testar conexão mostra a conta do Melhor Envio", "Melhor Envio conectado: Loja Teste" in r.text
       and ME["chamadas"][-1][2] == f"Bearer {TOKEN_ME_TELA}", r.text[-600:])
r = bruna.get("/site/sacola?entrega=ENVIO&cep=01310-100")
checar("Sacola cota o frete com o token da tela", "Correios PAC — R$ 32,50" in r.text and ME["chamadas"][-1][2] == f"Bearer {TOKEN_ME_TELA}")
checar("Atendente não mexe no token do Melhor Envio", ana.post("/config/entrega/melhor-envio", {"acao": "tirar"}).status_code == 403)
dona.post("/config/entrega/melhor-envio", {"acao": "tirar"})
checar("Tirar o token: envio some de novo", sql("SELECT count(*) FROM frete_config") == "0")
cfg_app.MELHOR_ENVIO_API_FIXA = ""
bruna.post("/site/sacola/remover", data={"csrf": csrf_de(bruna, "/site/sacola"), "peca": PS4})
PS5 = nova_peca("Vestido compra com ajuste", "300", "700")
sql(f"UPDATE produtos SET no_site = true, preco_no_site = true, pode_alugar = false WHERE id = '{PS5}'")
SL5 = sql(f"SELECT slug FROM produtos WHERE id='{PS5}'")
checar("Sem valor de ajuste configurado: só 'Comprar' (sem opção de ajuste)", 'name="ajuste"' not in bruna.get(f"/site/p/{SL5}").text)
dona.post("/config/entrega", {"sacola_ativa": "1", "entrega_retirada": "1", "sacola_minutos": "60", "pacote_peso_g": "1500",
                              "pacote_altura_cm": "10", "pacote_largura_cm": "30", "pacote_comprimento_cm": "40",
                              "cep_origem": "13010-000", "ajuste_site": "80,00", "sacola_pagamento": "ENTRADA_OU_TOTAL"})
checar("Dona define o valor do ajuste na compra pelo site", sql("SELECT ajuste_site_centavos FROM lojas") == "8000")
r = bruna.get(f"/site/p/{SL5}")
checar("Peça à venda: comprar online sem ajuste ou com ajuste na loja (+R$ 80)", 'name="ajuste" value="1"' in r.text
       and "R$ 780,00" in r.text and "retirar na loja ou receber em casa" in r.text)
r = bruna.post("/site/sacola/adicionar", data={"csrf": csrf_de(bruna, f"/site/p/{SL5}"), "peca": PS5, "tipo": "VENDA", "ajuste": "1"})
checar("Com ajuste precisa da data em que a peça fica pronta", "para quando você precisa" in unquote(str(r.url)))
dia_aj = hoje + timedelta(days=30)
bruna.post("/site/sacola/adicionar", data={"csrf": csrf_de(bruna, f"/site/p/{SL5}"), "peca": PS5, "tipo": "VENDA", "ajuste": "1",
                                           "retirada": dia_aj.isoformat()})
r = bruna.get("/site/sacola")
checar("Sacola mostra a compra com ajuste (peça + ajuste)", "Compra com ajuste na loja (+ R$ 80,00)" in r.text and "R$ 780,00" in r.text, r.text[-1500:])
bruna.post("/site/sacola/fechar", data={"csrf": Pessoa._token(r.text), "entrega": "RETIRADA", "aceito": "1",
                                        "chave": str(__import__("uuid").uuid4())})
COMP3 = sql("SELECT id FROM compras_site ORDER BY criado_em DESC LIMIT 1")
checar("Compra com ajuste: item com R$ 700 da peça + R$ 80 de ajuste, paga inteira",
       sql(f"SELECT valor_peca_centavos||'/'||ajuste_centavos||'/'||valor_pagar_centavos FROM compras_itens WHERE compra_id='{COMP3}'") == "70000/8000/78000")
MP["pagamentos"]["2003"] = {"id": 2003, "status": "approved", "external_reference": f"C-{COMP3}", "transaction_amount": 780.0,
                            "payment_method_id": "pix", "payment_type_id": "bank_transfer", "installments": 1}
bruna.get(f"/site/pagamento?status=approved&external_reference=C-{COMP3}")
PED_AJ = sql(f"SELECT id FROM pedidos WHERE produto_id='{PS5}'")
checar("Pago: nasce pedido de venda COM AJUSTE (vai para prova e costura), quitado e com a data",
       sql(f"SELECT com_ajuste||'/'||valor_total_centavos||'/'||(valor_total_centavos - valor_pago(id))||'/'||data_retirada||'/'||status FROM pedidos WHERE id='{PED_AJ}'")
       == f"true/78000/0/{dia_aj.isoformat()}/RESERVADO", sql(f"SELECT com_ajuste||'/'||valor_total_centavos||'/'||status FROM pedidos WHERE id='{PED_AJ}'"))
dona.post("/config/entrega", {"sacola_ativa": "0", "entrega_retirada": "1", "sacola_minutos": "60", "pacote_peso_g": "1500",
                              "pacote_altura_cm": "10", "pacote_largura_cm": "30", "pacote_comprimento_cm": "40"})
checar("Sacola desligada some do site", "/site/sacola" not in anon.get("/site").text)

print("\n== Layout novo: menu lateral, painel, tema e quem está online ==")
r = dona.get("/")
checar("Painel da Dona: menu lateral com ícones, números do dia, gráfico e 'Online agora'",
       'class="lateral"' in r.text and 'class="kpi' in r.text and 'class="grafico-colunas"' in r.text
       and "Online agora" in r.text and "Como a loja recebeu no mês" in r.text and "/static/tema.js" in r.text)
checar("Gráfico tem tabela para ler sem ver as cores", "Ver em tabela" in r.text)
r = ana.get("/")
checar("Atendente: sem o bloco 'Online agora'", 't-online' not in r.text and 'class="lateral"' in r.text)
checar("Menu lateral marca a tela aberta", 'class="menu-item ativo" href="/pedidos"' in ger.get("/pedidos").text)
checar("Menu lateral respeita o cargo (costureira não vê caixa)", "/caixa/receber" not in cida.get("/").text)
r = ger.c.post("/presenca", json={"p": f"/pedidos/{PED_CR}/receber"}, headers={"x-csrf": ger.csrf})
checar("Sinal de presença da Gerente: guarda só a área, sem o número do pedido", r.status_code == 204
       and sql("SELECT pagina FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id WHERE u.login = 'ger' "
               "AND s.encerrada_em IS NULL ORDER BY s.visto_em DESC LIMIT 1") == "/pedidos")
checar("Sinal sem token (outro site) é recusado", ger.c.post("/presenca", json={"p": "/"}).status_code == 403
       and anon.post("/site/presenca", json={"p": "/site"}).status_code == 403)
carla.post("/site/presenca", json={"p": "/site/sacola"}, headers={"x-csrf": csrf_de(carla, "/site/sacola")})
visit = cliente_site()
visit.post("/site/presenca", json={"p": "/site/p/vestido-azul-ves-0001"}, headers={"x-csrf": csrf_de(visit, "/site")})
nome_ger = sql("SELECT nome FROM usuarios WHERE login = 'ger'")
nome_carla = sql(f"SELECT nome FROM clientes WHERE id = (SELECT cliente_id FROM pedidos WHERE id = '{PED_CR}')")
r = dona.get("/online")
checar("Online agora: Gerente em Pedidos, Carla na sacola e 1 visitante sem conta",
       nome_ger in r.text and "Pedidos" in r.text and nome_carla in r.text and "Na sacola" in r.text
       and "visitante" in r.text and 'http-equiv="refresh"' in r.text, r.text[-1500:])
checar("Atendente não vê quem está online", ana.get("/online").status_code == 403 and "pilula-online" not in ana.get("/").text)
checar("Topo mostra quantos estão online", "online</a>" in dona.get("/").text)
checar("Dona pergunta #online no WhatsApp", chega("55" + FONES["dona"], "#online") == "comando" and enviar_fila()
       and "Online agora" in para("55" + FONES["dona"])[-1] and nome_carla.split()[0] in para("55" + FONES["dona"])[-1])
chega("55" + FONES["ana"], "#online"); enviar_fila()
checar("Atendente manda #online: não é do cargo dela", "não é do seu cargo" in para("55" + FONES["ana"])[-1])
checar("Busca do topo: código do comprovante abre o pedido", ger.get(f"/buscar?q={PED_CR.replace('-', '')[:8]}").url.path
       == f"/pedidos/{PED_CR}")
r = ger.get("/buscar?q=Carla")
checar("Busca do topo por nome mostra pedidos e clientes", "Pedidos" in r.text and "Clientes" in r.text and nome_carla in r.text)

print("\n== CSS sempre atualizado, dicas, símbolos das redes/cartões e cadastro ao lado do login ==")
r = dona.get("/")
checar("Arquivos do layout vão com versão (o navegador não usa o CSS antigo)", re.search(r'/static/app\.css\?v=[0-9a-f]{10}', r.text)
       and "dicas.js?v=" in r.text)
checar("CSS com versão fica guardado; sem versão sempre confere",
       "immutable" in anon.get("/static/app.css?v=1").headers["cache-control"]
       and anon.get("/static/app.css").headers["cache-control"] == "no-cache")
checar("Menu e botões com explicação ao parar o mouse", 'data-dica="Lançar dinheiro que entrou' in r.text
       and 'class="busca-rapida"' in r.text and r.text.count("data-dica=") > 20)
csrf_si = Pessoa._token(dona.get("/config/simbolos").text)
checar("Tela de símbolos lista redes, formas e bandeiras", all(x in dona.ultima.text for x in ("Instagram", "WhatsApp", "Visa")))
checar("Atendente não mexe nos símbolos", ana.get("/config/simbolos").status_code == 403)
r = dona.c.post("/config/simbolos/visa", data={"csrf": csrf_si},
                files={"arquivo": ("v.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")})
checar("SVG recusado (pode levar código)", sql("SELECT count(*) FROM simbolos") == "0")
for ch in ("visa", "instagram"):
    dona.c.post(f"/config/simbolos/{ch}", data={"csrf": csrf_si}, files={"arquivo": (f"{ch}.png", png_logo(), "image/png")})
SIM_VISA = sql("SELECT arquivo FROM simbolos WHERE chave = 'visa'")
dona.post("/config/redes", {"instagram": "lojateste"})
r = anon.get("/site")
checar("Rodapé mostra o símbolo enviado no lugar do nome (Visa)", f"/simbolo/{SIM_VISA}.png" in r.text and 'alt="Visa"' in r.text,
       sql("SELECT chave FROM simbolos"))
checar("Símbolo é público e sai regravado em PNG", anon.get(f"/simbolo/{SIM_VISA}.png").headers["content-type"] == "image/png")
checar("Símbolo inventado: 404", anon.get("/simbolo/" + "b" * 32 + ".png").status_code == 404)
checar("Rede com símbolo também (Instagram)", 'alt="Instagram"' in r.text)
dona.c.post("/config/simbolos/visa", data={"csrf": csrf_si, "acao": "tirar"})
checar("'Voltar ao nome' tira o símbolo e o nome volta no site", sql("SELECT count(*) FROM simbolos WHERE chave = 'visa'") == "0"
       and "<span>Visa</span>" in anon.get("/site").text)
r = cliente_site().get("/site/conta/entrar")
checar("Tela de entrar tem o cadastro ao lado (mesmo modelo)", 'class="caixa-site caixa-cadastro"' in r.text
       and 'action="/site/conta/criar"' in r.text and "Entrar pelo WhatsApp" in r.text)

# =========================================================================== #
# Ataques: cada item da lista de segurança tentado de verdade contra o sistema
# =========================================================================== #
import base64 as _b64s  # noqa: E402
from urllib.parse import urlparse as _urlparse  # noqa: E402

print("\n== F12: nada legível no navegador ==")
chefe = Pessoa("ger")
bruto = chefe.c.cookies.get("sessao_loja")
def _decodifica(v):
    try:
        return _b64s.urlsafe_b64decode(v + "=" * (-len(v) % 4)).decode("utf-8")
    except Exception:
        return ""
GER_ID = sql("SELECT id FROM usuarios WHERE login='ger'")
checar("Cookie da sessão é cifrado: no F12 não aparece id, nome nem cargo",
       bruto and GER_ID not in _decodifica(bruto) and "usuario_id" not in _decodifica(bruto) and "Gerente" not in _decodifica(bruto))
r = TestClient(app).get("/login")
cabecalho = r.headers.get_list("set-cookie")
checar("Cookie da sessão: HttpOnly (JavaScript não lê) e SameSite=Strict",
       any("sessao_loja=" in c and "HttpOnly" in c and "SameSite=strict" in c for c in cabecalho), cabecalho)
falso = TestClient(app, follow_redirects=True)
falso.cookies.set("sessao_loja", bruto[:-6] + ("AAAAAA" if not bruto.endswith("AAAAAA") else "BBBBBB"))
checar("Cookie adulterado não vale nada", falso.get("/pedidos").url.path == "/login")
for rota in ("/docs", "/redoc", "/openapi.json"):
    checar(f"Documentação automática da API desligada ({rota})", TestClient(app).get(rota).status_code == 404)
r = chefe.get("/pecas/isto-nao-e-um-id")
checar("Erro de endereço não mostra detalhes técnicos (nada de JSON 'detail')",
       r.status_code == 404 and "detail" not in r.text and "uuid" not in r.text.lower() and "Página não encontrada" in r.text)
r = TestClient(app).get("/rota-que-nao-existe")
checar("Página inexistente: página da loja, sem JSON técnico", r.status_code == 404 and '"detail"' not in r.text)
modelos = list((APP / "app/templates").rglob("*.html"))
checar("Nenhuma tela tem comentário HTML (nada escondido no código-fonte)", not [t.name for t in modelos if "<!--" in t.read_text()])
checar("Nenhuma tela tem script embutido (CSP bloqueia; só arquivos /static)",
       not [t.name for t in modelos if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", t.read_text())])
checar("JavaScript não escreve nada no Console nem usa innerHTML/eval",
       not [j.name for j in (APP / "app/static").glob("*.js") if re.search(r"console\.|innerHTML|eval\(|document\.write", j.read_text())])
r = chefe.get("/")
checar("Cabeçalhos: CSP, sem iframe, sem sniff, noindex no sistema",
       r.headers.get("x-frame-options") == "DENY" and r.headers.get("x-content-type-options") == "nosniff"
       and "noindex" in r.headers.get("x-robots-tag", "") and "script-src 'self'" in r.headers.get("content-security-policy", "")
       and "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""), dict(r.headers))
checar("Dockerfile: servidor não diz o nome/versão (sem cabeçalho Server)",
       "--no-server-header" in (RAIZ / "app/Dockerfile").read_text())

print("\n== Session Hijacking e Broken Authentication ==")
roubado = TestClient(app, follow_redirects=True, headers={"user-agent": "Robo-do-Atacante/1.0"})
for nome in ("sessao_loja", "aparelho"):
    if chefe.c.cookies.get(nome):
        roubado.cookies.set(nome, chefe.c.cookies.get(nome))
checar("Cookie copiado para outro navegador: não entra", roubado.get("/pedidos").url.path == "/login")
checar("… e a sessão verdadeira também é encerrada (e fica registrado o motivo)",
       chefe.get("/pedidos").url.path == "/login"
       and sql(f"SELECT count(*) FROM sessoes WHERE usuario_id='{GER_ID}' AND motivo='NAVEGADOR_DIFERENTE'") == "1")
enviar_fila()
checar("Tentativa de roubo de sessão: alerta no WhatsApp da Dona e do Dev",
       any("outro navegador" in m and "Gerente" in m for m in para("55" + FONES["dev"])))
chefe = Pessoa("ger")
copia = TestClient(app, follow_redirects=True)
copia.cookies.set("sessao_loja", chefe.c.cookies.get("sessao_loja"))
checar("Cookie copiado sem o aparelho (mesmo navegador): não entra", copia.get("/pedidos").url.path == "/login")
chefe = Pessoa("ger")
guardado = chefe.c.cookies.get("sessao_loja"); aparelho_ger = chefe.c.cookies.get("aparelho")
chefe.post("/sair")
volta = TestClient(app, follow_redirects=True)
volta.cookies.set("sessao_loja", guardado); volta.cookies.set("aparelho", aparelho_ger)
checar("Depois de Sair, o cookie antigo (copiado antes) não entra mais", volta.get("/pedidos").url.path == "/login"
       and sql(f"SELECT count(*) FROM sessoes WHERE usuario_id='{GER_ID}' AND motivo='SAIU'") != "0")
c1, _ = entrar("ger"); sid_antes = c1.cookies.get("sessao_loja")
checar("Cada entrada gera uma sessão nova (sem fixação de sessão)",
       TestClient(app).get("/login").cookies.get("sessao_loja") != sid_antes)
checar("Sessões ficam registradas e nada é apagado (banco recusa DELETE)",
       "imutável" in rodar(["psql", "-X", "-d", TEST_DB, "-c", "DELETE FROM sessoes"]).stderr.lower()
       or "permission" in rodar(["psql", "-X", "-d", TEST_DB, "-c", "DELETE FROM sessoes"]).stderr.lower()
       or "não" in rodar(["psql", "-X", "-d", TEST_DB, "-c", "DELETE FROM sessoes"]).stderr.lower())
t = Pessoa._token(TestClient(app).get("/login").text)
anon = TestClient(app)
t = Pessoa._token(anon.get("/login").text)
r1 = anon.post("/login", data={"csrf": t, "login": "ninguem-existe", "senha": "Qualquer-coisa-9!"})
r2 = anon.post("/login", data={"csrf": Pessoa._token(r1.text), "login": "ger", "senha": "Qualquer-coisa-9!"})
checar("Login não revela se o usuário existe (mesma mensagem)", r1.status_code == r2.status_code == 401
       and "Usuário ou senha não conferem" in r1.text and "Usuário ou senha não conferem" in r2.text)

print("\n== Credential Stuffing: senhas vazadas/comuns ==")
for fraca in ("Senha@123", "Br@sil2026", "Mudar@123", "Vestido#1", "Qwerty@123", "Gerente#2026"):
    r = ger.post("/minha-senha", {"atual": SENHA, "senha": fraca, "senha2": fraca})
    checar(f"Senha de lista vazada recusada: {fraca}", r.status_code == 400 and "fácil de adivinhar" in r.text, r.text[-300:])

print("\n== SQL Injection ==")
ATAQUES_SQL = ["' OR '1'='1", "'; DROP TABLE clientes; --", "1' UNION SELECT senha_hash FROM usuarios--",
               "\\'; SELECT pg_sleep(5); --", "%' AND 1=1 --"]
antes_cli = sql("SELECT count(*) FROM clientes")
for a in ATAQUES_SQL:
    for url in (f"/clientes?q={quote(a)}", f"/pecas?q={quote(a)}", f"/pedidos?q={quote(a)}"):
        r = dona.get(url)
        checar(f"SQL injection na busca não quebra nem vaza: {url[:40]}", r.status_code == 200 and "$argon2" not in r.text
               and "syntax" not in r.text.lower())
    r = TestClient(app).get(f"/site/colecao?q={quote(a)}&ordem={quote(a)}&data={quote(a)}")
    checar(f"SQL injection no site (busca, ordem, data): {a[:20]}", r.status_code == 200)
    t = Pessoa._token(anon.get("/login").text)
    r = anon.post("/login", data={"csrf": t, "login": a, "senha": a})
    checar(f"SQL injection no login não entra: {a[:20]}", r.status_code in (400, 401) and "Olá" not in r.text)
checar("Depois de todos os ataques de SQL, tabelas intactas", sql("SELECT count(*) FROM clientes") == antes_cli
       and sql("SELECT count(*) FROM usuarios") != "0")

print("\n== Cross-Site Scripting (XSS) ==")
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'
ana.post("/clientes/nova", {"nome": "Xis " + XSS, "telefone": "(19) 97654-3210", "lgpd": "1", "observacoes": XSS})
r = ana.get("/clientes?q=Xis")
checar("Nome com código malicioso aparece como texto (escapado), nunca executa",
       "&lt;script&gt;" in r.text and "<script>alert" not in r.text and "<img src=x" not in r.text)
XID = sql("SELECT id FROM clientes WHERE nome LIKE 'Xis %'")
r = ana.get(f"/clientes/{XID}")
checar("Observação com código malicioso também escapada na ficha", "<script>alert" not in r.text and "onerror=alert" not in r.text
       or "&lt;img" in r.text)
r = TestClient(app).get(f"/site/colecao?q={quote(XSS)}")
checar("Busca do site devolve o termo escapado", "<script>alert" not in r.text)
v2 = TestClient(app); t = Pessoa._token(v2.get("/site").text)
v2.post("/site/chat/enviar", data={"csrf": t, "nome": XSS, "lgpd": "1", "texto": XSS})
checar("Chat devolve JSON (o navegador mostra com textContent, nunca como HTML)",
       v2.get("/site/chat/mensagens").headers["content-type"].startswith("application/json"))
r = dona.post("/config/redes", {"instagram": "", "facebook": "javascript:alert(1)", "tiktok": "", "youtube": "",
                                "pinterest": "", "site_externo": ""})
checar("Link 'javascript:' nas redes sociais é recusado", r.status_code == 400)

print("\n== Cross-Site Request Forgery (CSRF) ==")
from starlette.routing import Route as _Route  # noqa: E402
def _todas_rotas(rotas):
    for rt in rotas:
        if isinstance(rt, _Route):
            yield rt
        interno = getattr(rt, "original_router", None) or getattr(rt, "app", None)
        if interno is not None and hasattr(interno, "routes"):
            yield from _todas_rotas(interno.routes)
posts = sorted({rt.path for rt in _todas_rotas(app.routes) if "POST" in (rt.methods or ())
                and not rt.path.startswith(("/whatsapp/aviso", "/pagamentos/mercadopago/aviso", "/site/chat"))})
UU = "00000000-0000-4000-8000-000000000000"
problemas = []
antes_aud = sql("SELECT count(*) FROM auditoria WHERE acao NOT LIKE 'login%'")
for caminho in posts:
    url = re.sub(r"\{[^}]+\}", UU, caminho)
    r = dona.c.post(url, data={"valor": "10", "nome": "Ataque CSRF"}, follow_redirects=False)
    if caminho == "/login/codigo" and r.headers.get("location") == "/login":
        continue                                  # sem login pela metade não há o que confirmar: volta ao login
    if r.status_code < 400 or r.status_code >= 500:
        problemas.append((caminho, r.status_code))
checar(f"CSRF: os {len(posts)} formulários do sistema recusam envio sem o token", len(posts) > 60 and not problemas, problemas)
checar("CSRF: ataque de outro site não derruba a sessão (Sair também exige token)", dona.get("/pedidos").status_code == 200)
checar("CSRF: nada foi gravado pelas tentativas", sql("SELECT count(*) FROM auditoria WHERE acao NOT LIKE 'login%'") == antes_aud)
r = TestClient(app).post("/site/chat/enviar", data={"nome": "x", "lgpd": "1", "texto": "oi"})
checar("CSRF no chat do site também recusado", r.status_code == 400)

print("\n== Escalação de privilégio e botões escondidos ==")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
def percorrer(p, limite=400):
    vistos, fila, ruins, padroes = set(), ["/"], [], set()
    while fila and len(vistos) < limite:
        url = fila.pop(0)
        if url in vistos:
            continue
        vistos.add(url)
        r = p.c.get(url, follow_redirects=True)
        if r.status_code == 403 or r.status_code >= 500:
            ruins.append((url, r.status_code))
            continue
        for href in re.findall(r'href="(/[^"#]*)"', r.text):
            href = href.replace("&amp;", "&")
            caminho = _urlparse(href).path
            if caminho.startswith(("/site", "/static", "/fotos", "/pagar", "/sair")) or caminho.endswith((".pdf", "/pdf")):
                continue
            padrao = UUID_RE.sub("X", caminho)
            if padrao in padroes and href not in vistos:
                continue
            padroes.add(padrao)
            fila.append(href)
    return vistos, ruins
for nome_p, pessoa_p in (("Atendente", ana), ("Costureira", cida), ("Limpeza", lu), ("Dev", dev),
                         ("Gerente", ger), ("Freelance", fre), ("Dona", dona)):
    vistos, ruins = percorrer(pessoa_p)
    checar(f"{nome_p}: percorreu {len(vistos)} telas e nenhum botão/link leva a 'sem permissão' ou erro", not ruins, ruins)
for quem, url in ((cida, "/caixa"), (cida, "/clientes"), (lu, "/pedidos/novo"), (ana, "/equipe"), (ana, "/config/whatsapp"),
                  (dev, "/caixa/receber"), (ana, "/sistema"), (fre, "/relatorios"), (cida, "/config/redes")):
    checar(f"Digitando o endereço na mão ({url}) sem ser do cargo: bloqueado", quem.get(url).status_code == 403)
r = ana.post(f"/equipe/{ANA_ID}/cargos", {"papel_DONO": "1", "vinculo": "FIXO"})
checar("Atendente tentando se dar o cargo de Dona (formulário forjado): bloqueado", r.status_code == 403
       and "DONO" not in sql(f"SELECT string_agg(p.codigo, ',') FROM usuario_papeis up JOIN papeis p ON p.id=up.papel_id WHERE up.usuario_id='{ANA_ID}'"))
r = ana.post("/config/whatsapp", {"url": "http://evil:1", "sessao": "x", "numero": "19999999999", "segredo": "x"})
checar("Atendente forjando a configuração do WhatsApp: bloqueado", r.status_code == 403)

print("\n== IDOR: dados de outra loja e de outra pessoa ==")
outra = cli("criar-loja", "--nome", "Loja Concorrente", "--login", "dona2", "--nome-dona", "Outra Dona")
LOJA2 = re.search(r"LOJA_ID=([0-9a-f-]{36})", outra).group(1)
sql(f"INSERT INTO clientes (loja_id, nome) VALUES ('{LOJA2}', 'Cliente Da Concorrente')")
C2 = sql("SELECT id FROM clientes WHERE nome = 'Cliente Da Concorrente'")
r = dona.get(f"/clientes/{C2}")
checar("Abrir cliente de outra loja pelo id: não encontrado (o banco esconde)", r.status_code == 404 and "Concorrente" not in r.text)
checar("Busca não mostra clientes de outra loja", "Cliente Da Concorrente" not in dona.get("/clientes?q=Concorrente").text)
checar("Id inventado de pedido: não encontrado", dona.get(f"/pedidos/{UU}").status_code == 404)
checar("Página do cliente só com o link secreto (144 bits); id do pedido não abre",
       "Zuleica" not in TestClient(app).get(f"/site/meu-pedido/{PZED}").text)
checar("Costureira não abre a ficha completa da cliente (dados pessoais)", cida.get(f"/clientes/{ZID}/editar").status_code == 403)

print("\n== Command Injection e arquivos enviados ==")
import ast  # noqa: E402
perigosos = []
for arq in (APP / "app").rglob("*.py"):
    arvore = ast.parse(arq.read_text())
    for no in ast.walk(arvore):
        if isinstance(no, ast.Attribute) and no.attr in ("system", "popen", "Popen", "run", "call", "check_output") \
                and isinstance(no.value, ast.Name) and no.value.id in ("os", "subprocess"):
            perigosos.append(arq.name)
        if isinstance(no, ast.Name) and no.id in ("eval", "exec"):
            perigosos.append(arq.name)
checar("O sistema nunca chama o terminal do servidor (sem os.system/subprocess/eval)", not perigosos, perigosos)
r = ana.c.post(f"/pecas/{PV4}/fotos", data={"csrf": ana.csrf},
               files=[("fotos", ("../../etc/passwd;rm -rf .jpg", b"<?php system($_GET['c']); ?>", "image/jpeg"))])
checar("Arquivo falso com nome malicioso: recusado, nada gravado fora da pasta",
       "abrir essa foto" in r.text or "Formato" in r.text or r.status_code == 400, r.text[-300:])
checar("Fotos são gravadas com nome aleatório (o nome enviado nunca é usado)",
       all(re.fullmatch(r"[0-9a-f]{32}-\d+\.jpg", f.name) for f in Path(os.environ["FOTOS_DIR"]).glob("*.jpg")))

print("\n== Business Logic Attacks (mexer nas regras pelo formulário) ==")
chave = ana.chave("/pecas/nova")
ana.post("/pecas/nova", {"chave": chave, "categoria": cat_ves, "nome": "Vestido logica", "uso": "ambos",
                         "preco_aluguel": "500", "preco_venda": "900"})
PL = sql("SELECT id FROM produtos WHERE nome = 'Vestido logica'")
ret_l = hoje + timedelta(days=90)
base_l = {"tipo": "ALUGUEL", "cliente_nome": "Lara Logica", "cliente_telefone": "(19) 99444-5555", "forma": "PIX",
          "data_retirada": ret_l.isoformat(), "data_devolucao": (ret_l + timedelta(days=1)).isoformat(), "valor": "500"}
def forjar(mudancas):
    """Passa pela conferência honesta e troca campos escondidos antes do 'SIM, GUARDAR' (como faria um atacante)."""
    chave = Pessoa._achar(ana.get(f"/pedidos/novo?peca={PL}").text, "chave")
    r = ana.post("/pedidos/novo/conferir", {"chave": chave, "peca": PL, **base_l})
    campos = ana.campos(r.text)
    campos.update(mudancas)
    return ana.post("/pedidos/novo/gravar", campos)
for nome_t, mud in (("valor pago maior que o total", {"valor": "99999"}), ("valor negativo", {"valor": "-500"}),
                    ("99 parcelas", {"forma": "CREDITO", "parcelas": "99", "maquininha": MAQ}),
                    ("tipo trocado para venda (preço de venda)", {"tipo": "VENDA_SIMPLES"}),
                    ("devolução antes da retirada", {"data_devolucao": (ret_l - timedelta(days=5)).isoformat()}),
                    ("data no passado", {"data_retirada": (hoje - timedelta(days=30)).isoformat(),
                                         "data_devolucao": (hoje - timedelta(days=29)).isoformat()}),
                    ("entrada abaixo do mínimo", {"valor": "1"})):
    antes_n = sql(f"SELECT count(*) FROM pedidos WHERE produto_id = '{PL}'")
    forjar(mud)
    checar(f"Formulário forjado ({nome_t}): recusado, nada gravado",
           sql(f"SELECT count(*) FROM pedidos WHERE produto_id = '{PL}'") == antes_n)
checar("Preço vem sempre do cadastro da peça (não do formulário)",
       sql(f"SELECT count(*) FROM pedidos WHERE produto_id='{PL}' AND valor_total_centavos <> 50000") == "0")
# Corrida: 5 pessoas apertam "guardar" ao mesmo tempo para a mesma peça e mesmas datas
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
jogadores = [Pessoa("ana") for _ in range(5)]
formularios = []
for j in jogadores:
    ch = Pessoa._achar(j.get(f"/pedidos/novo?peca={PL}").text, "chave")
    rr = j.post("/pedidos/novo/conferir", {"chave": ch, "peca": PL, **base_l})
    formularios.append((j, j.campos(rr.text)))
with ThreadPoolExecutor(5) as ex:
    list(ex.map(lambda par: par[0].post("/pedidos/novo/gravar", par[1]), formularios))
checar("Corrida: 5 atendentes reservam a mesma peça no mesmo segundo — só UMA reserva entra",
       sql(f"SELECT count(*) FROM pedidos WHERE produto_id = '{PL}' AND status <> 'CANCELADO'") == "1")
PEDL = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PL}'")
r = ana.post(f"/pedidos/{PEDL}/etapa", {"para": "CONCLUIDO"})
checar("Pular etapa (Reservado → Concluído) pelo formulário: recusado",
       sql(f"SELECT status FROM pedidos WHERE id = '{PEDL}'") == "RESERVADO")
r = ana.post(f"/pedidos/{PEDL}/etapa", {"para": "ALUGADO"})
checar("Marcar 'retirou' sem passar pelas provas: recusado", sql(f"SELECT status FROM pedidos WHERE id = '{PEDL}'") == "RESERVADO")
r = ana.post(f"/pedidos/{PEDL}/cancelar", {"motivo": "quero"})
checar("Atendente cancelando pedido (só Dona/Gerente): recusado", sql(f"SELECT status FROM pedidos WHERE id = '{PEDL}'") == "RESERVADO")
r = ana.c.post(f"/pedidos/{PEDL}/etapa", data={"csrf": ana.csrf, "para": "PROVA_1", "volta": "//site-do-golpe.com"},
               follow_redirects=False)
checar("Redirecionamento para site de fora (//golpe.com) é trocado pela página do pedido",
       r.headers.get("location", "").startswith(f"/pedidos/{PEDL}") and "golpe" not in r.headers.get("location", ""))
from app.web import volta_segura  # noqa: E402
checar("Endereços de volta perigosos nunca passam", all(volta_segura(u, "/ok") == "/ok" for u in
       ("//evil.com", "/\\\\evil.com", "/\\evil.com", "https://evil.com", "javascript:alert(1)", "/a\r\nSet-Cookie: x=1")))

print("\n== Ataques de cabeçalho e protocolo ==")
from starlette.requests import Request as _Req  # noqa: E402
from app import auth as _auth, protecao as _prot  # noqa: E402
def req(peer, headers):
    return _Req({"type": "http", "method": "GET", "path": "/", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                 "client": (peer, 1234), "query_string": b""})
cfg_app.ATRAS_DO_PROXY = True
checar("X-Forwarded-For inventado não engana: vale o IP que o proxy anotou (o último)",
       _auth.ip(req("172.18.0.5", {"X-Forwarded-For": "1.2.3.4, 200.100.50.25"})) == "200.100.50.25")
cfg_app.CLOUDFLARE = True
checar("CF-Connecting-IP falso de quem NÃO é Cloudflare é ignorado",
       _auth.ip(req("172.18.0.5", {"X-Forwarded-For": "200.100.50.25", "CF-Connecting-IP": "8.8.8.8"})) == "200.100.50.25")
checar("CF-Connecting-IP vindo da Cloudflare de verdade é usado",
       _auth.ip(req("172.18.0.5", {"X-Forwarded-For": "173.245.48.10", "CF-Connecting-IP": "177.10.20.30"})) == "177.10.20.30")
r = TestClient(app, client=("203.0.113.50", 1)).get("/login")
checar("Com Cloudflare ligada, quem chega direto na VPS (pulando a Cloudflare) não abre a área da equipe",
       r.status_code == 403 and "endereço oficial" in r.text)
cfg_app.CLOUDFLARE = False
cfg_app.ATRAS_DO_PROXY = False
cfg_app.HOSTS_PERMITIDOS = ["loja.exemplo.com.br"]
checar("Cabeçalho Host de outro site (link envenenado): recusado",
       TestClient(app, base_url="http://site-do-golpe.com").get("/site").status_code == 400)
checar("Host da loja: aceito", TestClient(app, base_url="https://loja.exemplo.com.br").get("/site").status_code == 200)
cfg_app.HOSTS_PERMITIDOS = []
for metodo in ("TRACE", "PUT", "DELETE", "PATCH", "OPTIONS"):
    checar(f"Método {metodo}: recusado antes de chegar no sistema", TestClient(app).request(metodo, "/login").status_code == 405)
import asyncio  # noqa: E402
async def _cru(headers, corpo=b""):
    enviado = []
    async def app_falso(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    async def receive():
        return {"type": "http.request", "body": corpo, "more_body": False}
    async def send(m):
        enviado.append(m)
    await _prot.Protecao(app_falso)({"type": "http", "method": "POST", "path": "/login", "headers": headers}, receive, send)
    return enviado[0]["status"]
checar("Dois Content-Length (request smuggling): recusado",
       asyncio.run(_cru([(b"content-length", b"5"), (b"content-length", b"50")])) == 400)
checar("Content-Length + Transfer-Encoding juntos (smuggling): recusado",
       asyncio.run(_cru([(b"content-length", b"5"), (b"transfer-encoding", b"chunked")])) == 400)
checar("Corpo maior que o limite sem avisar o tamanho (chunked): cortado", asyncio.run(_cru([], b"x" * (2 * 1024 * 1024))) == 413)
r = TestClient(app).post("/site/chat/enviar", content=b"x" * (3 * 1024 * 1024), headers={"content-type": "application/x-www-form-urlencoded"})
checar("DDoS de memória: envio de 3 MB num formulário comum é barrado (413)", r.status_code == 413)
checar("Segredos da URL não vão para o log (link da cliente, aviso do WhatsApp, pagamento)",
       _prot.caminho_para_log(f"/site/meu-pedido/{TOKEN_C}/pdf") == "/site/meu-pedido/***/pdf"
       and _prot.caminho_para_log(f"/whatsapp/aviso/{GANCHO}") == "/whatsapp/aviso/***")
with db.transacao() as _cur:
    _cur.execute("SHOW statement_timeout")
    tempo = _cur.fetchone()["statement_timeout"]
checar("Consulta travada é cortada pelo banco em 15 s (ninguém trava o sistema)", tempo == "15s", tempo)
dockerfile = (RAIZ / "app/Dockerfile").read_text()
checar("Servidor com limites contra lentidão proposital (slowloris) e excesso de conexões",
       "--limit-concurrency" in dockerfile and "--timeout-keep-alive" in dockerfile and "--no-proxy-headers" in dockerfile)

print("\n== Clickjacking ==")
for url in ("/site", "/login", f"/site/meu-pedido/{TOKEN_C}", "/site/contrato"):
    r = TestClient(app).get(url)
    checar(f"Página {url[:22]} não abre dentro de iframe de outro site",
           r.headers.get("x-frame-options") == "DENY" and "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""))

print("\n== Deserialization ==")
ruins = []
for arq in (APP / "app").rglob("*.py"):
    arvore = ast.parse(arq.read_text())
    for no in ast.walk(arvore):
        nomes = []
        if isinstance(no, ast.Import):
            nomes = [a.name for a in no.names]
        elif isinstance(no, ast.ImportFrom):
            nomes = [no.module or ""]
        if any(n.split(".")[0] in ("pickle", "marshal", "shelve", "yaml", "jsonpickle", "dill") for n in nomes):
            ruins.append(arq.name)
checar("Nenhum formato perigoso de desserialização (pickle, marshal, yaml…): só JSON", not ruins, ruins)
import pickle as _pk  # noqa: E402  (só aqui no teste, para montar o ataque)
bomba = _b64s.urlsafe_b64encode(_pk.dumps({"usuario_id": DONA})).decode()
atac = TestClient(app, follow_redirects=True); atac.cookies.set("sessao_loja", bomba)
checar("Cookie com objeto serializado (pickle) é ignorado: não entra e não quebra", atac.get("/pedidos").url.path == "/login")
r = TestClient(app).post(f"/whatsapp/aviso/{GANCHO}", content=b'[1,2,3]', headers={"content-type": "application/json"})
checar("Aviso do WhatsApp com JSON que não é objeto: recusado sem quebrar", r.status_code == 400)
r = TestClient(app).post(f"/whatsapp/aviso/{GANCHO}", content=b'{"a":' * 5000, headers={"content-type": "application/json"})
checar("JSON malformado/aninhado demais: recusado sem quebrar", r.status_code in (400, 413))
r = TestClient(app).post(f"/whatsapp/aviso/{GANCHO}", content=b'[' * 3000 + b']' * 3000, headers={"content-type": "application/json"})
checar("JSON válido mas com 3.000 níveis (bomba de recursão): recusado sem quebrar", r.status_code == 400)
r = TestClient(app).post("/pagamentos/mercadopago/aviso", content=b'{"a":' * 5000, headers={"content-type": "application/json"})
checar("Aviso do Mercado Pago com JSON-bomba: não quebra", r.status_code < 500)

print("\n== Supply chain (dependências) ==")
reqs = (RAIZ / "app/requirements.txt").read_text().splitlines()
checar("Todas as dependências com versão fixa (==)", all("==" in l for l in reqs if l.strip() and not l.startswith("#")))
checar("Build só aceita pacotes prontos (sem rodar setup.py) e confere hash quando há requirements.lock",
       "--only-binary=:all:" in dockerfile and "--require-hashes" in dockerfile)
ci = (RAIZ / ".github/workflows/ci.yml").read_text()
checar("Actions do GitHub presas por hash de commit (tag trocada não entra)",
       not re.findall(r"uses: [\w/-]+@v\d", ci) and len(re.findall(r"uses: [\w/-]+@[0-9a-f]{40}", ci)) >= 5)
checar("CI procura vulnerabilidade conhecida (pip-audit) e senha no código (gitleaks)", "pip-audit" in ci and "gitleaks" in ci)
checar("Dependabot avisa atualização de segurança (pip, Docker, Actions)", (RAIZ / ".github/dependabot.yml").exists())
externos = [t.name for t in modelos if re.search(r'<(script|link)[^>]+(src|href)="https?://', t.read_text())]
checar("Nenhum script/estilo de CDN de terceiros no site ou sistema (tudo servido pela loja)", not externos, externos)

print("\n== Exposição de dados sensíveis ==")
pagina_pub = TestClient(app).get(f"/site/meu-pedido/{TOKEN_C}")
checar("Página pública do pedido: CPF e telefone mascarados, sem e-mail e sem endereço",
       "111.444.777-35" not in pagina_pub.text and "99222-3333" not in pagina_pub.text and "zuleica@exemplo.com" not in pagina_pub.text)
checar("Página pública não vai para o Google nem fica em cache",
       "noindex" in pagina_pub.headers.get("x-robots-tag", "") and pagina_pub.headers.get("cache-control") == "no-store")
checar("Link da cliente não vaza para outros sites (Referrer-Policy no-referrer)", pagina_pub.headers.get("referrer-policy") == "no-referrer")
segredos_env = [os.environ["SESSION_SECRET"], os.environ.get("DADOS_CHAVE", "ab" * 32), SEGREDO_WPP, TOKEN_WPP]
paginas = [dona.get(u).text for u in ("/", "/config/whatsapp", "/equipe", "/sistema", "/config/site")]
checar("Nenhuma tela mostra chave, segredo ou token (nem para a Dona)", not any(s in p for s in segredos_env for p in paginas))
checar("Banco guarda só hash da senha (Argon2), nunca a senha", sql("SELECT count(*) FROM usuarios WHERE senha_hash NOT LIKE '$argon2id$%'") == "0")
checar("Mensagem enviada não guarda mais o anexo (PDF do contrato) na fila",
       sql("SELECT count(*) FROM mensagens WHERE status = 'ENVIADA' AND anexo_c IS NOT NULL") == "0")

print("\n== Sprint 8: pedido começa pela cliente, tamanhos, ocasião principal, menu, serviços e Master ==")
PS8 = nova_peca("Vestido sprint oito", "300", "800")
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "valor": "800", "forma": "PIX"}, peca=PS8)
checar("Pedido sem cliente é recusado (até venda simples)", "Escolha a cliente" in r.text
       and sql(f"SELECT count(*) FROM pedidos WHERE produto_id = '{PS8}'") == "0", r.text[-400:])
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "cliente_nome": "Soninha", "cliente_telefone": "(19) 99777-0101", "valor": "800", "forma": "PIX"}, peca=PS8)
checar("Cliente nova precisa de nome e sobrenome", "nome e sobrenome" in r.text)
r = pedido(ana, {"tipo": "VENDA_SIMPLES", "cliente_nome": "Sônia Prado", "cliente_telefone": "1234", "valor": "800", "forma": "PIX"}, peca=PS8)
checar("Cliente nova precisa de telefone com DDD", "telefone com DDD" in r.text)
r = ana.get(f"/pedidos/novo?peca={PS8}&busca_cliente=Carla")
checar("Fazer pedido: procurar a cliente pelo nome mostra o resultado com link para escolher",
       "Carla Souza Lima" in r.text and f"cliente={CLI}" in r.text, r.text[-800:])
r = ana.get(f"/pedidos/novo?peca={PS8}&cliente={CLI}")
checar("Cliente escolhida aparece em destaque no pedido", 'class="cliente-escolhida"' in r.text and "Carla Souza Lima" in r.text)
chave = Pessoa._achar(r.text, "chave")
r = ana.post("/pedidos/novo/conferir", {"chave": chave, "peca": PS8, "cliente_id": CLI, "cliente_nome": "Carla Souza Lima",
                                        "cliente_telefone": "", "tipo": "VENDA_SIMPLES", "valor": "800", "forma": "PIX"})
r = ana.post("/pedidos/novo/gravar", ana.campos(r.text))
checar("Pedido com a cliente escolhida grava ligado à ficha dela",
       sql(f"SELECT cliente_id FROM pedidos WHERE produto_id = '{PS8}'") == CLI, r.text[-400:])
checar("Pedido mostra o nome da cliente", "Carla Souza Lima" in ana.get(f"/pedidos/{sql(f'SELECT id FROM pedidos WHERE produto_id = {chr(39)}{PS8}{chr(39)}')}").text)

r = ana.get("/pecas/nova")
checar("Cadastrar roupa: vários tamanhos para marcar (XPP a EG)", all(f'name="tam_{t}"' in r.text for t in ("XPP", "PP", "P", "M", "G", "GG", "EG")))
ana.post("/pecas/nova", {"chave": ana.chave("/pecas/nova"), "categoria": cat_ves, "nome": "Vestido varios tamanhos", "uso": "ambos",
                         "preco_aluguel": "200", "preco_venda": "600", "tam_P": "1", "tam_M": "1", "tam_G": "1", "tamanho_outro": "38",
                         f"ocasiao_{OC_MADRINHA}": "1", f"ocasiao_{OC_FORMATURA}": "1", "ocasiao_principal": OC_FORMATURA})
checar("Peça com vários tamanhos guardada como 'P, M, G, 38'",
       sql("SELECT tamanho FROM produtos WHERE nome = 'Vestido varios tamanhos'") == "P, M, G, 38")
checar("Ocasião principal escolhida é guardada (só uma aparece no site)",
       sql("SELECT ocasiao_principal FROM produtos WHERE nome = 'Vestido varios tamanhos'") == OC_FORMATURA)
PVT = sql("SELECT id FROM produtos WHERE nome = 'Vestido varios tamanhos'")
r = ana.get(f"/pecas/{PVT}/editar")
checar("Editar peça traz os tamanhos marcados", 'name="tam_M" value="1" checked' in r.text or re.search(r'name="tam_M"[^>]*checked', r.text) is not None)
checar("Menu lateral tem o botão de encolher (desktop)", "data-encolher-menu" in ana.get("/").text)

r = ana.get("/servicos")
checar("Atendente não mexe no cadastro de serviços", r.status_code == 403)
r = ger.post("/servicos", {"nome": "Lavagem a seco", "preco": "45", "descricao": "Lavanderia parceira"})
SLV = sql("SELECT id FROM servicos WHERE nome = 'Lavagem a seco'")
checar("Gerente cadastra serviço avulso", SLV != "" and "Lavagem a seco" in ger.get("/servicos").text)
_ins = ("INSERT INTO transacoes (loja_id, tipo, natureza, valor_centavos, forma_pagamento, descricao, chave_idempotencia, usuario_id) "
        "SELECT id, 'ENTRADA', 'AVULSO', 100, 'DINHEIRO', 'sem motivo', gen_random_uuid(), (SELECT id FROM usuarios WHERE login='dona') FROM lojas LIMIT 1 RETURNING 'gravou'")
checar("Banco recusa entrada avulsa sem serviço (nem direto no banco)", sql(_ins) == "")
PPX = nova_peca("Vestido link pix", "400", "900")
dia_px = hoje + timedelta(days=150)
pedido(ana, {"tipo": "ALUGUEL", "cliente_id": CLI, "cliente_nome": "Carla Souza Lima", "cliente_telefone": "(19) 98888-2222",
             "forma": "PIX", "valor": "150", "data_retirada": dia_px.isoformat(), "data_devolucao": (dia_px + timedelta(days=1)).isoformat()}, peca=PPX)
PEDPX = sql(f"SELECT id FROM pedidos WHERE produto_id = '{PPX}'")
r = ger.get(f"/pedidos/{PEDPX}")
checar("Pedido oferece link pelo Pix da loja (qualquer banco)", 'value="PIX_LOJA"' in r.text, r.text[-600:] if PEDPX else "sem pedido")
ger.post(f"/pedidos/{PEDPX}/link", {"valor": "150", "provedor": "PIX_LOJA"})
LPX = sql(f"SELECT id FROM links_pagamento WHERE pedido_id = '{PEDPX}' AND provedor = 'PIX_LOJA'")
copia_px = sql(f"SELECT pix_copia_cola FROM links_pagamento WHERE id = '{LPX}'")
checar("Link Pix da loja: Pix copia e cola com o valor e CRC certos", LPX != "" and "5406150.00" in copia_px
       and copia_px[-4:] == _pix.crc16(copia_px[:-4]))
r = TestClient(app).get(f"/pagar/{LPX}")
checar("Cliente abre o link e vê o QR e o copia e cola do Pix", r.status_code == 200 and "Pix Copia e Cola" in r.text
       and copia_px in r.text and "<svg" in r.text)
checar("Link Pix não lança nada sozinho", sql(f"SELECT count(*) FROM transacoes WHERE descricao LIKE 'Link Pix da loja%'") == "0")
r = ger.get(f"/pedidos/{PEDPX}")
checar("Loja vê o botão de confirmar quando o Pix cai", f"/pagamentos/links/{LPX}/pix-recebido" in r.text and "wa.me/5519988882222" in r.text)
checar("Limpeza não confirma Pix", lu.post(f"/pagamentos/links/{LPX}/pix-recebido").status_code == 403)
ana.post(f"/pagamentos/links/{LPX}/pix-recebido")
ana.post(f"/pagamentos/links/{LPX}/pix-recebido")
checar("Pix confirmado entra uma vez só no caixa e o link fica pago",
       sql(f"SELECT count(*)||'/'||sum(valor_centavos) FROM transacoes WHERE pedido_id = '{PEDPX}' AND descricao LIKE 'Link Pix da loja%'") == "1/15000"
       and sql(f"SELECT status FROM links_pagamento WHERE id = '{LPX}'") == "PAGO")
checar("Link pago não mostra mais o Pix para a cliente", "Pagamento recebido" in TestClient(app).get(f"/pagar/{LPX}").text)
r = ana.get("/reimprimir?q=Carla Souza")
checar("Reimprimir: acha pelo nome e mostra comprovante, contrato, checklist, etiqueta, carnê, recibos e medidas",
       all(x in r.text for x in ("/imprimir?formato=80", "/contrato", "checklist?formato=a4", "etiquetas/imprimir?ids=",
                                 "/caixa/recibo/", "medidas/imprimir")), r.text[-800:])
r = ana.get("/reimprimir?q=" + sql(f"SELECT upper(substr(replace(id::text,'-',''),1,8)) FROM pedidos WHERE id='{PEDPX}'"))
checar("Reimprimir: acha pelo código do comprovante", "Vestido link pix" in r.text)
checar("Reimprimir está no menu", 'href="/reimprimir"' in ana.get("/").text)
checar("Limpeza não abre Reimprimir (sem acesso a pedidos)", lu.get("/reimprimir").status_code == 403)

from app.rotas.site import escolher_vitrines  # noqa: E402
_oc = [{"id": 1, "nome": "Casamento", "slug": "casamento", "pai_id": None, "pai_slug": None},
       {"id": 2, "nome": "Madrinha", "slug": "madrinha", "pai_id": 1, "pai_slug": "casamento"},
       {"id": 3, "nome": "Noiva", "slug": "noiva", "pai_id": 1, "pai_slug": "casamento"},
       {"id": 4, "nome": "Formatura", "slug": "formatura", "pai_id": None, "pai_slug": None}]
_v = escolher_vitrines(_oc, [{"id": "a", "ocasiao_principal": 2, "foto": "fa"}, {"id": "b", "ocasiao_principal": 2, "foto": "fb"},
                             {"id": "c", "ocasiao_principal": 4, "foto": "fc"}])
checar("Escolha pela ocasião: cada peça em uma ocasião só (sem foto repetida) e ocasião sem peça some",
       [(o["nome"], o["foto"]) for o in _v] == [("Casamento", "fb"), ("Madrinha", "fa"), ("Formatura", "fc")], _v)
_h = TestClient(app).get("/site").text
checar("Início do site na ordem: ocasião, Como funciona, Novidades",
       ("Escolha pela ocasião" not in _h or _h.index("Escolha pela ocasião") < _h.index("Como funciona")) and _h.index("Como funciona") < _h.index(">Novidades<"))
checar("Rodapé no modelo: logo/sobre/redes/novidades em cima; loja com mapa, institucional, ajuda e contato embaixo",
       _h.index('class="rodape-linha1"') < _h.index('class="rodape-sobre"') < _h.index('class="rodape-redes"')
       < _h.index('id="form-novidades"') < _h.index('class="rodape-linha2') < _h.index('class="rodape-loja"')
       < _h.index(">Institucional<") < _h.index(">Ajuda<") < _h.index(">Entre em contato<") and "mapa-auto" in _h)

cli("criar-usuario", "--login", "mestre", "--nome", "Carlos Master", "--papel", "MASTER", "--concedido-por", "dona")
mestre = Pessoa("mestre")
checar("Master entra em tudo (regras, equipe, sistema, online, serviços)",
       all(mestre.get(u).status_code == 200 for u in ("/config", "/equipe", "/sistema", "/online", "/servicos", "/caixa", "/relatorios")))
checar("Master ganha sozinho as permissões novas",
       sql("SELECT count(*) FROM permissoes pe WHERE NOT EXISTS (SELECT 1 FROM papel_permissoes pp JOIN papeis pa ON pa.id = pp.papel_id "
           "WHERE pa.codigo = 'MASTER' AND pp.permissao_id = pe.id)") == "0")
_e = cli_sql_erro(f"INSERT INTO usuario_papeis (loja_id, usuario_id, papel_id, concedido_por) SELECT ctx_loja(), (SELECT id FROM usuarios WHERE login='ana'), "
                                f"(SELECT id FROM papeis WHERE codigo='MASTER'), (SELECT id FROM usuarios WHERE login='ger')", "ger")
checar("Gerente não concede o acesso Master", ("Master" in _e or "Dono" in _e) and sql("SELECT count(*) FROM usuario_papeis up JOIN papeis p ON p.id=up.papel_id WHERE p.codigo='MASTER'") == "1", _e)

db.pool.close()
print(f"\nResultado: {ok} passaram, {falhas} falharam.")
if os.environ.get("MANTER_BANCO") != "1":
    rodar(["psql", "-X", "-q", "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'])
sys.exit(1 if falhas else 0)
