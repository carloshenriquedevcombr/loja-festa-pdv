# Imagem para rodar TODOS os testes sem instalar nada no PC:
# travas do banco, app de ponta a ponta (cada cargo, cada botão) e backup/restauração.
# Contexto de build: raiz do repositório.
FROM postgres:18-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /repo
COPY app/requirements.txt app/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -q -r app/requirements.txt
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 LOG_LEVEL=WARNING FOTOS_DIR=/tmp/fotos LIMITES_ATIVOS=0 \
    DADOS_CHAVE=abababababababababababababababababababababababababababababababab

COPY migrations/ migrations/
COPY scripts/ scripts/
COPY app/ app/
COPY tests/test_banco.py tests/test_app.py tests/test_migracao.py tests/
COPY backup/backup.sh backup/restore-test.sh backup/

CMD set -e; \
    echo "== Travas do banco =="; python tests/test_banco.py; \
    echo; echo "== App de ponta a ponta =="; python tests/test_app.py; \
    echo; echo "== Atualização de banco com dados =="; python tests/test_migracao.py; \
    echo; echo "== Backup e restauração =="; \
    MANTER_BANCO=1 python tests/test_banco.py > /dev/null; \
    PGDATABASE=loja_teste bash backup/backup.sh; \
    PGDATABASE=loja_teste bash backup/restore-test.sh; \
    psql -X -q -d postgres -c "DROP DATABASE IF EXISTS loja_teste"
