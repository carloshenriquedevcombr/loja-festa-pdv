# Cliente PostgreSQL 16 (mesma versão do servidor) + restic para cópia externa criptografada
FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends restic ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Sao_Paulo
COPY backup.sh restore-test.sh backup-loop.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/restore-test.sh /usr/local/bin/backup-loop.sh

CMD ["backup-loop.sh"]
