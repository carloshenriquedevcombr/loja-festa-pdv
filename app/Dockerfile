FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin loja \
 && mkdir -p /dados/fotos && chown loja:loja /dados/fotos
WORKDIR /srv

# Cadeia de fornecimento:
#  * versões fixas; --only-binary: nenhum pacote roda código de instalação (setup.py) no build;
#  * se existir requirements.lock (gerado por scripts/travar-dependencias.sh), cada arquivo baixado
#    precisa bater com o hash anotado — pacote trocado no caminho não instala.
COPY requirements.txt requirements.lock* ./
RUN if [ -f requirements.lock ]; then pip install --only-binary=:all: --require-hashes -r requirements.lock; \
    else pip install --only-binary=:all: -r requirements.txt; fi

COPY app ./app

USER loja
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Sem --proxy-headers: o IP real é lido pelo app (auth.ip), que só confia no que o proxy escreveu.
# Limites contra lentidão proposital (slowloris) e excesso de conexões.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--no-access-log", "--no-server-header", "--no-date-header", \
     "--limit-concurrency", "200", "--backlog", "256", "--timeout-keep-alive", "5", \
     "--h11-max-incomplete-event-size", "16384", "--workers", "2"]
