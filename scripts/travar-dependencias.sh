#!/usr/bin/env bash
# Gera app/requirements.lock com o hash (sha256) de cada pacote, incluindo os indiretos.
# Rode quando mudar o requirements.txt (precisa de Docker e internet):
#   bash scripts/travar-dependencias.sh
# Depois, o build da imagem só instala pacotes idênticos aos que foram conferidos aqui.
set -euo pipefail
cd "$(dirname "$0")/../app"
docker run --rm -v "$PWD":/w -w /w python:3.12-slim-bookworm sh -c '
  pip install -q pip-tools &&
  pip-compile -q --generate-hashes --allow-unsafe --output-file requirements.lock requirements.txt'
echo "Pronto: app/requirements.lock. Confira no git diff e faça o commit."
