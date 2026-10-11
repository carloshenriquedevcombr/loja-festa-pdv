#!/usr/bin/env bash
# Um backup completo: dump comprimido, conferência do arquivo, retenção local
# e cópia criptografada para fora da VPS (restic), quando configurada.
set -euo pipefail

DESTINO="${BACKUP_DIR:-/backups}"
RETENCAO_LOCAL_DIAS="${RETENCAO_LOCAL_DIAS:-7}"
DB="${PGDATABASE:?defina PGDATABASE}"
ts="$(date +%Y-%m-%d_%H%M%S)"
arquivo="$DESTINO/loja_${ts}.dump"

mkdir -p "$DESTINO"
echo "[$(date -Is)] backup: iniciando dump de $DB"

pg_dump --format=custom --compress=6 --file="$arquivo.parcial" "$DB"
# Um dump que o pg_restore não consegue listar está corrompido: falha aqui.
pg_restore --list "$arquivo.parcial" > /dev/null
mv "$arquivo.parcial" "$arquivo"
tamanho="$(du -h "$arquivo" | cut -f1)"
echo "[$(date -Is)] backup: $arquivo ($tamanho) conferido"

# Fotos das peças: nunca são apagadas nem trocadas pelo app (nome único), então a cópia só acrescenta
if [ -d /fotos ]; then
  mkdir -p "$DESTINO/fotos"
  cp -Rn /fotos/. "$DESTINO/fotos/"
  echo "[$(date -Is)] backup: fotos copiadas ($(find "$DESTINO/fotos" -type f | wc -l) arquivos)"
fi

find "$DESTINO" -name 'loja_*.dump' -mtime +"$RETENCAO_LOCAL_DIAS" -delete
find "$DESTINO" -name '*.parcial' -delete

externo="nao_configurado"
if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  : "${RESTIC_PASSWORD:?defina RESTIC_PASSWORD}"
  restic snapshots --latest 1 > /dev/null 2>&1 || restic init
  restic backup --tag loja --host loja-festa "$arquivo" $( [ -d "$DESTINO/fotos" ] && echo "$DESTINO/fotos" )
  restic forget --tag loja --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
  externo="ok"
  echo "[$(date -Is)] backup: cópia externa enviada"
else
  echo "[$(date -Is)] ATENÇÃO: sem cópia externa (RESTIC_REPOSITORY vazio). Se a VPS se perder, o backup se perde junto." >&2
fi

printf '{"quando":"%s","arquivo":"%s","tamanho":"%s","externo":"%s"}\n' \
  "$(date -Is)" "$(basename "$arquivo")" "$tamanho" "$externo" > "$DESTINO/ultimo_backup.json"
