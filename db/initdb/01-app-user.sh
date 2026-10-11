#!/usr/bin/env bash
# Roda uma única vez, na criação do volume do banco.
# Cria o papel que o app usa: sem superusuário, sem DELETE, sujeito a RLS.
set -euo pipefail

: "${APP_DB_PASSWORD:?defina APP_DB_PASSWORD no .env}"

psql -v ON_ERROR_STOP=1 -X -q \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v pw="$APP_DB_PASSWORD" -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE app_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'pw';
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO app_user;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
SQL

echo "Papel app_user criado."
