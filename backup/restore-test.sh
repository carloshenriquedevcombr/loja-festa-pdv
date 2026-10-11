#!/usr/bin/env bash
# Prova que o backup mais recente restaura: carrega num banco temporário,
# compara contagens com a produção e apaga o banco temporário.
# Não toca no banco de produção. Rode todo mês:
#   docker compose exec backup restore-test.sh
set -euo pipefail

DESTINO="${BACKUP_DIR:-/backups}"
PROD="${PGDATABASE:?defina PGDATABASE}"
TESTE="loja_restauracao_teste"
export PGOPTIONS="${PGOPTIONS:--c client_min_messages=warning}"
PSQL=(psql -X -q -At -v ON_ERROR_STOP=1)

arquivo="${1:-$(ls -1t "$DESTINO"/loja_*.dump 2>/dev/null | head -1)}"
if [ -z "$arquivo" ] && [ -n "${RESTIC_REPOSITORY:-}" ]; then
  echo "Sem dump local: buscando o último no restic..."
  restic restore latest --tag loja --target /tmp/restic-restore
  arquivo="$(ls -1t /tmp/restic-restore/backups/loja_*.dump | head -1)"
fi
[ -n "$arquivo" ] || { echo "FALHOU: nenhum backup encontrado." >&2; exit 1; }
echo "Restaurando $arquivo em $TESTE"

"${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS $TESTE" -c "CREATE DATABASE $TESTE"
limpar() { "${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS $TESTE" || true; }
trap limpar EXIT

pg_restore --exit-on-error --dbname="$TESTE" "$arquivo"

falhou=0
# Informativo: a produção pode ter registros mais novos que o backup.
for tabela in lojas usuarios produtos pedidos transacoes auditoria; do
  a="$("${PSQL[@]}" -d "$TESTE" -c "SELECT count(*) FROM $tabela")"
  b="$("${PSQL[@]}" -d "$PROD" -c "SELECT count(*) FROM $tabela")"
  echo "  $tabela: backup=$a  produção agora=$b"
done

# O livro caixa do backup tem que bater, lançamento por lançamento, com a produção
soma_teste="$("${PSQL[@]}" -d "$TESTE" -c "SELECT count(*) || ':' || COALESCE(sum(valor_centavos),0) FROM transacoes")"
max_id="$("${PSQL[@]}" -d "$TESTE" -c "SELECT COALESCE(max(id),0) FROM transacoes")"
soma_prod="$("${PSQL[@]}" -d "$PROD" -c "SELECT count(*) || ':' || COALESCE(sum(valor_centavos),0) FROM transacoes WHERE id <= $max_id")"
if [ "$soma_teste" = "$soma_prod" ]; then
  echo "  livro caixa: confere ($soma_teste)"
else
  echo "  livro caixa: DIVERGE backup=$soma_teste produção=$soma_prod" >&2
  falhou=1
fi

# As travas vieram junto?
travas="$("${PSQL[@]}" -d "$TESTE" -c "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_transacoes_imutaveis'")"
[ "$travas" = "1" ] && echo "  travas do banco: presentes" || { echo "  travas do banco: AUSENTES" >&2; falhou=1; }

if [ "$falhou" = 0 ]; then echo "RESTAURAÇÃO OK"; else echo "RESTAURAÇÃO FALHOU" >&2; exit 1; fi
