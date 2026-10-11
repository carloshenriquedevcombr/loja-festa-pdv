# PostgreSQL 16 com locale pt_BR e as migrações embutidas.
# Contexto de build: raiz do repositório (docker build -f db/Dockerfile .)
FROM postgres:18-bookworm

RUN localedef -i pt_BR -c -f UTF-8 -A /usr/share/locale/locale.alias pt_BR.UTF-8
ENV LANG=pt_BR.utf8 \
    TZ=America/Sao_Paulo

COPY db/initdb/ /docker-entrypoint-initdb.d/
COPY scripts/migrate.sh /usr/local/bin/migrate.sh
COPY migrations/ /migrations/
RUN chmod 0755 /usr/local/bin/migrate.sh /docker-entrypoint-initdb.d/*.sh \
 && chmod -R a+rX /migrations
