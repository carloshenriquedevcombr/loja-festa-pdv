"""Configuração lida do ambiente. Nenhum segredo tem valor padrão no código."""
import os
import uuid


def _obrigatoria(nome: str) -> str:
    valor = os.environ.get(nome, "").strip()
    if not valor:
        raise RuntimeError(f"Variável de ambiente {nome} não definida (veja .env.example).")
    return valor


DATABASE_URL = _obrigatoria("DATABASE_URL")
SESSION_SECRET = _obrigatoria("SESSION_SECRET")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET precisa ter pelo menos 32 caracteres.")

# Chave dos dados pessoais cifrados (64 letras hex = 32 bytes). Gere com: openssl rand -hex 32
# GUARDE uma cópia fora da VPS: sem ela, CPF, telefones e medidas não abrem nem no backup.
DADOS_CHAVE = _obrigatoria("DADOS_CHAVE").lower()
if len(DADOS_CHAVE) != 64 or any(c not in "0123456789abcdef" for c in DADOS_CHAVE):
    raise RuntimeError("DADOS_CHAVE precisa ter 64 caracteres hexadecimais (openssl rand -hex 32).")

# Vazio até a primeira loja ser criada com: python -m app.cli criar-loja
LOJA_ID = os.environ.get("LOJA_ID", "").strip() or None
if LOJA_ID:
    LOJA_ID = str(uuid.UUID(LOJA_ID))

# 0 só em desenvolvimento/CI sem HTTPS. Em produção o cookie é sempre Secure.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"

SESSAO_HORAS = int(os.environ.get("SESSAO_HORAS", "12"))
LOGIN_MAX_TENTATIVAS = 5

# Fotos das peças (volume persistente no Docker; entra no backup)
FOTOS_DIR = os.environ.get("FOTOS_DIR", "/dados/fotos")

# Endereço público do site (ex.: https://www.minhaloja.com.br). Sem ele, o Mercado Pago não
# consegue avisar o pagamento sozinho e a loja usa o botão "Conferir pagamento".
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")

# API do Mercado Pago (só muda nos testes automáticos)
MP_API = os.environ.get("MP_API", "https://api.mercadopago.com").rstrip("/")

# Chave que cifra o token do pagamento online dentro do banco (derivada do segredo da sessão)
import hashlib as _hashlib  # noqa: E402
CHAVE_TOKENS = _hashlib.sha256(("tokens-pagamento:" + SESSION_SECRET).encode()).hexdigest()

# Sessão: sai sozinha depois de N minutos sem uso (balcão compartilhado)
SESSAO_OCIOSA_MIN = int(os.environ.get("SESSAO_OCIOSA_MIN", "30"))

# Cloudflare na frente (1 = confiar no IP e no país que a Cloudflare informa)
CLOUDFLARE = os.environ.get("CLOUDFLARE", "0") == "1"
# Área da equipe só do Brasil (precisa da Cloudflare para saber o país)
SO_BRASIL = os.environ.get("SO_BRASIL", "1") == "1"
# Atrás do Nginx Proxy Manager (VPS): o IP real é o que o PRÓPRIO proxy anotou (último do X-Forwarded-For).
# Nunca o primeiro: esse qualquer um inventa.
ATRAS_DO_PROXY = os.environ.get("ATRAS_DO_PROXY", "0") == "1"
# Endereços aceitos no cabeçalho Host (evita links envenenados "https://site-do-golpista/...").
# Vazio = aceita qualquer um (PC local). Na VPS: o domínio da loja (e o do SITE_URL entra sozinho).
from urllib.parse import urlsplit as _urlsplit  # noqa: E402
HOSTS_PERMITIDOS = [h.strip().lower() for h in os.environ.get("HOSTS_PERMITIDOS", "").split(",") if h.strip()]
if SITE_URL:                                   # com SITE_URL, o domínio dele já basta
    HOSTS_PERMITIDOS.append((_urlsplit(SITE_URL).hostname or "").lower())
# Tamanho máximo de um envio (bytes): fotos podem ser maiores; o resto é pequeno
LIMITE_ENVIO = 1 * 1024 * 1024
LIMITE_ENVIO_FOTOS = 80 * 1024 * 1024
# Limites de pedidos por IP (0 só nos testes automáticos)
LIMITES_ATIVOS = os.environ.get("LIMITES_ATIVOS", "1") == "1"

# Nota do Google (Places API New) — só o trabalhador usa; vazio = a Dona digita a nota
GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "").strip()
GOOGLE_PLACES_API = os.environ.get("GOOGLE_PLACES_API", "https://places.googleapis.com/v1").rstrip("/")
# Frete pelo Melhor Envio (vazio = só retirada na loja e entrega local)
MELHOR_ENVIO_TOKEN = os.environ.get("MELHOR_ENVIO_TOKEN", "").strip()
MELHOR_ENVIO_API_FIXA = os.environ.get("MELHOR_ENVIO_API", "").strip().rstrip("/")   # só para teste/servidor próprio
MELHOR_ENVIO_API = MELHOR_ENVIO_API_FIXA or (
    "https://sandbox.melhorenvio.com.br" if os.environ.get("MELHOR_ENVIO_SANDBOX", "0") == "1"
    else "https://melhorenvio.com.br")
