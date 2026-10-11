#!/usr/bin/env bash
# Instala ou atualiza o sistema da loja na VPS a partir do pacote enviado pelo enviar-vps.ps1 (Windows).
#
#   sudo bash instalar-vps.sh /tmp/loja-festa-AAAAMMDD-HHMMSS.tar.gz [/opt/loja-festa]
#
# Primeira vez: gera o .env com segredos novos, acha a rede do Nginx Proxy Manager, sobe o banco,
#               aplica as migrações, cria a loja e a conta da Dona (e, se quiser, o seu Master).
# Atualização:  guarda o .env, faz backup do banco ANTES, troca o código, reconstrói e migra.
#               A versão anterior do código fica em /opt/loja-festa.anterior (para voltar, se precisar).
# Nada disso mexe nos outros projetos da VPS (NPM, n8n, WPPConnect, Portainer…).
set -Eeuo pipefail
umask 077                                   # o .env e as cópias temporárias nascem legíveis só pelo root

PACOTE="${1:?uso: instalar-vps.sh /caminho/do/pacote.tar.gz}"
DESTINO="${2:-/opt/loja-festa}"
[[ "$DESTINO" =~ ^/[A-Za-z0-9._/-]+$ ]] && [ "$DESTINO" != "/" ] || { echo "destino inválido: $DESTINO" >&2; exit 1; }
NOVO="${DESTINO}.novo"
ANTERIOR="${DESTINO}.anterior"

azul()    { printf '\033[36m%s\033[0m\n' "$*"; }
verde()   { printf '\033[32m%s\033[0m\n' "$*"; }
amarelo() { printf '\033[33m%s\033[0m\n' "$*"; }
erro()    { printf '\033[31mERRO: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || erro "rode como root (o enviar-vps.ps1 já usa sudo quando precisa)."
[ -f "$PACOTE" ] || erro "pacote não encontrado: $PACOTE"
command -v docker >/dev/null || erro "Docker não encontrado na VPS."
docker compose version >/dev/null 2>&1 || erro "a VPS não tem o 'docker compose' (plugin v2)."
command -v openssl >/dev/null || erro "openssl não encontrado (apt install -y openssl)."

# --------------------------------------------------------------------------- #
azul "== Abrindo o pacote =="
rm -rf "$NOVO"
mkdir -p "$NOVO"
tar -xzf "$PACOTE" -C "$NOVO" --no-same-owner
[ -f "$NOVO/docker-compose.yml" ] && [ -d "$NOVO/migrations" ] || erro "o pacote não parece ser do loja-festa."
# Arquivos que o Linux executa não podem ter fim de linha do Windows (CRLF)
find "$NOVO" -type f \( -name '*.sh' -o -name '*.sql' -o -name '*.py' -o -name 'Dockerfile' -o -name '*.yml' \) \
     -exec sed -i 's/\r$//' {} +
chmod 0755 "$NOVO"/scripts/*.sh "$NOVO"/backup/*.sh "$NOVO"/db/initdb/*.sh 2>/dev/null || true
chmod -R a+rX "$NOVO"                        # o build copia o código para as imagens (usuário sem privilégio)

PRIMEIRA=0
if [ -f "$DESTINO/.env" ]; then
  azul "== Atualização: mantendo o .env e os dados =="
  cp -p "$DESTINO/.env" "$NOVO/.env"
  [ -f "$DESTINO/app/requirements.lock" ] && [ ! -f "$NOVO/app/requirements.lock" ] \
    && cp -p "$DESTINO/app/requirements.lock" "$NOVO/app/requirements.lock"
else
  PRIMEIRA=1
  azul "== Primeira instalação: gerando o .env com segredos novos =="
  cp "$NOVO/.env.example" "$NOVO/.env"
  for v in DB_OWNER_PASSWORD APP_DB_PASSWORD SESSION_SECRET DADOS_CHAVE; do
    sed -i "s|^$v=.*|$v=$(openssl rand -hex 32)|" "$NOVO/.env"
  done
  # Rede do Nginx Proxy Manager: o app precisa estar nela para o NPM encaminhar o domínio
  npm_id="$(docker ps -q --filter ancestor=jc21/nginx-proxy-manager | head -1)"
  [ -n "$npm_id" ] || npm_id="$(docker ps -q --filter ancestor=jc21/nginx-proxy-manager:latest | head -1)"
  [ -n "$npm_id" ] || npm_id="$(docker ps --format '{{.ID}} {{.Image}}' | awk '/nginx-proxy-manager/ {print $1; exit}')"
  [ -n "$npm_id" ] || erro "não achei o container do Nginx Proxy Manager. Ligue o NPM e rode de novo."
  rede="$(docker inspect "$npm_id" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')"
  [ -n "$rede" ] || erro "não consegui descobrir a rede do Nginx Proxy Manager."
  sed -i "s|^NPM_NETWORK=.*|NPM_NETWORK=$rede|" "$NOVO/.env"
  verde "Rede do Nginx Proxy Manager: $rede"
fi
chmod 600 "$NOVO/.env"

# Atualização: backup do banco e das fotos antes de trocar qualquer coisa
if [ "$PRIMEIRA" -eq 0 ] && docker ps --format '{{.Names}}' | grep -qx loja-backup; then
  azul "== Backup antes de atualizar =="
  if ! docker exec loja-backup backup.sh; then
    [ "${FORCAR:-0}" = "1" ] || erro "o backup falhou; nada foi trocado. Veja 'docker logs loja-backup' (ou rode com FORCAR=1)."
    amarelo "Backup falhou, seguindo mesmo assim (FORCAR=1)."
  fi
fi

# --------------------------------------------------------------------------- #
azul "== Trocando o código =="
if [ -d "$DESTINO" ]; then
  rm -rf "$ANTERIOR"
  mv "$DESTINO" "$ANTERIOR"
fi
mv "$NOVO" "$DESTINO"
cd "$DESTINO"

voltar() {
  amarelo ""
  amarelo "Algo falhou. O código anterior está em $ANTERIOR e o banco não foi apagado."
  amarelo "Para voltar o código:  rm -rf $DESTINO && mv $ANTERIOR $DESTINO && cd $DESTINO && docker compose up -d --build"
}
trap voltar ERR

azul "== Construindo as imagens (alguns minutos na primeira vez) =="
docker compose build

azul "== Banco =="
docker compose up -d --wait db
docker compose up -d backup
docker compose exec -T db migrate.sh

ler_env() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

pedir_senha() {                 # senha digitada duas vezes, sem aparecer na tela; vai para a CLI pela entrada padrão
  local quem="$1" s1 s2
  while true; do
    amarelo "Senha de $quem: 8 a 32 caracteres, MAIÚSCULA, minúscula, número e símbolo; nada de nome da loja ou palavra comum." >&2
    amarelo "Ex. de formato: Gato3Azul!Pipoca" >&2
    read -rsp "Senha de $quem (não aparece): " s1 </dev/tty; echo >&2
    read -rsp "Repita a senha: " s2 </dev/tty; echo >&2
    [ "$s1" = "$s2" ] && [ -n "$s1" ] && { printf '%s' "$s1"; return; }
    amarelo "As senhas não conferem. De novo." >&2
  done
}

cli() {                          # roda a CLI do app com a senha pela entrada padrão (nunca na linha de comando)
  local senha="$1"; shift
  printf '%s\n' "$senha" | docker compose run --rm -T app python -m app.cli "$@" --senha-stdin
}

if [ -z "$(ler_env LOJA_ID)" ]; then
  azul ""
  azul "== Criar a loja e a conta da Dona =="
  amarelo "O ideal é a Dona digitar a própria senha agora (ela pode trocar depois em 'Minha senha')."
  read -rp "Nome da loja: " nome_loja </dev/tty
  read -rp "Nome da Dona: " nome_dona </dev/tty
  read -rp "Login da Dona [dona]: " login_dona </dev/tty
  login_dona="${login_dona:-dona}"
  while true; do
    senha="$(pedir_senha "$nome_dona")"
    if saida="$(cli "$senha" criar-loja --nome "$nome_loja" --login "$login_dona" --nome-dona "$nome_dona" 2>&1)"; then
      break
    fi
    printf '%s\n' "$saida" | tail -3
    amarelo "Tente outra senha."
  done
  unset senha
  loja_id="$(printf '%s\n' "$saida" | grep -oE 'LOJA_ID=[0-9a-f-]{36}' | head -1 | cut -d= -f2)"
  [ -n "$loja_id" ] || erro "não encontrei o LOJA_ID na resposta: $saida"
  sed -i "s|^LOJA_ID=.*|LOJA_ID=$loja_id|" .env
  verde "Loja criada (LOJA_ID guardado no .env)."

  read -rp "Criar agora o seu usuário MASTER (acesso total)? [S/n]: " quer </dev/tty
  if [[ ! "$quer" =~ ^[nN] ]]; then
    read -rp "Seu nome: " nome_m </dev/tty
    read -rp "Seu login [carlos]: " login_m </dev/tty
    login_m="${login_m:-carlos}"
    while true; do
      senha="$(pedir_senha "$nome_m")"
      if saida="$(cli "$senha" criar-usuario --login "$login_m" --nome "$nome_m" --papel MASTER --concedido-por "$login_dona" 2>&1)"; then
        verde "Master '$login_m' criado. Ele entra sempre com código no WhatsApp quando o WhatsApp estiver ligado."
        break
      fi
      printf '%s\n' "$saida" | tail -3
      amarelo "Tente outra senha."
    done
    unset senha
  fi
fi

azul "== Subindo o sistema =="
docker compose up -d --remove-orphans
for _ in $(seq 1 45); do
  estado="$(docker inspect -f '{{.State.Health.Status}}' loja-app 2>/dev/null || echo ausente)"
  [ "$estado" = "healthy" ] && break
  sleep 4
done
[ "$estado" = "healthy" ] || erro "o app não ficou saudável. Veja: cd $DESTINO && docker compose logs --tail 80 app"
trap - ERR
docker image prune -f >/dev/null 2>&1 || true
rm -f "$PACOTE"

docker compose ps
verde ""
verde "Pronto! Sistema no ar em $DESTINO."
if [ "$PRIMEIRA" -eq 1 ]; then
  amarelo ""
  amarelo "AGORA, sem falta:"
  amarelo " 1. Guarde o DADOS_CHAVE fora da VPS (gerenciador de senhas + papel no cofre). Para ver:"
  amarelo "      grep ^DADOS_CHAVE= $DESTINO/.env"
  amarelo "    Sem ele, CPF, telefones, medidas, contratos e backups ficam ilegíveis para sempre."
  amarelo " 2. Nginx Proxy Manager > Proxy Hosts > Add: seu domínio -> http, host 'loja-app', porta 8000,"
  amarelo "    Block Common Exploits; aba SSL: novo certificado, Force SSL, HTTP/2 e HSTS."
  amarelo " 3. Coloque o endereço no .env (SITE_URL=https://seudominio) e rode: cd $DESTINO && docker compose up -d"
  amarelo " 4. Backup externo (RESTIC_*) no .env: veja docs/VPS.md, passo 7."
else
  verde "Versão anterior do código guardada em $ANTERIOR (pode apagar depois de conferir)."
fi
