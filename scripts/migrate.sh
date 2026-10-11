#!/usr/bin/env bash
# Aplica as migrações SQL pendentes, em ordem, cada uma numa única transação.
# Uso na VPS:   docker compose exec db migrate.sh
# Uso local/CI: PGHOST=... PGUSER=... PGDATABASE=... MIGRATIONS_DIR=migrations scripts/migrate.sh
set -euo pipefail

export PGUSER="${PGUSER:-${POSTGRES_USER:-postgres}}"
export PGDATABASE="${PGDATABASE:-${POSTGRES_DB:-postgres}}"
DIR="${MIGRATIONS_DIR:-/migrations}"
export PGOPTIONS="${PGOPTIONS:--c client_min_messages=warning}"
PSQL=(psql -X -q -At -v ON_ERROR_STOP=1)

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS schema_migrations (
                   versao      TEXT PRIMARY KEY,
                   aplicada_em TIMESTAMPTZ NOT NULL DEFAULT now())"

shopt -s nullglob
arquivos=("$DIR"/[0-9]*.sql)
[ ${#arquivos[@]} -gt 0 ] || { echo "Nenhuma migração em $DIR" >&2; exit 1; }

for f in "${arquivos[@]}"; do
  v="$(basename "$f" .sql)"
  [[ "$v" =~ ^[0-9]{4}_[a-z0-9_]+$ ]] || { echo "Nome inválido: $v" >&2; exit 1; }
  if [ "$("${PSQL[@]}" -c "SELECT 1 FROM schema_migrations WHERE versao = '$v'")" = "1" ]; then
    echo "= $v (já aplicada)"
    continue
  fi
  echo "> aplicando $v"
  # -1: o arquivo e o registro da versão entram na MESMA transação
  "${PSQL[@]}" -1 -f "$f" -c "INSERT INTO schema_migrations (versao) VALUES ('$v')"
  echo "  ok"
done
