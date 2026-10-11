#!/usr/bin/env bash
# Agenda simples: um backup por dia no horário BACKUP_HORA (padrão 03:00).
# Um backup que falha não derruba o container; o erro fica em `docker compose logs backup`.
set -uo pipefail

HORA="${BACKUP_HORA:-03:00}"
echo "Backup agendado para todo dia às $HORA."

while true; do
  agora=$(date +%s)
  alvo=$(date -d "today $HORA" +%s)
  [ "$alvo" -le "$agora" ] && alvo=$(date -d "tomorrow $HORA" +%s)
  sleep $((alvo - agora))
  backup.sh || echo "[$(date -Is)] ERRO: backup falhou" >&2
done
