# Implantação na VPS

A VPS já roda Docker, Portainer, Nginx Proxy Manager (NPM), n8n, WPPConnect e Absenteísmo.
O sistema da loja entra como mais um projeto Docker, **sem mexer nos outros**.

```
Internet → Domínio → NPM (HTTPS) → loja-app:8000 ──rede interna──→ loja-db
                                                         └──────→ loja-backup → cópia externa
```

Todos os comandos abaixo rodam na VPS, via SSH. Tempo estimado: 1 a 2 horas.

---

## Caminho curto: do Windows, pelo PowerShell (sem GitHub)

No seu PC, dentro da pasta do projeto:

```powershell
.\enviar-vps.cmd testar     # 1ª vez: pede IP, usuário e porta do SSH (guarda em .env.vps, sem senha) e testa
.\enviar-vps.cmd            # empacota, envia por scp e instala/atualiza em /opt/loja-festa
.\enviar-vps.cmd status     # containers da loja   |   .\enviar-vps.cmd logs   # últimas linhas do app
```

Na 1ª vez o instalador (`scripts/instalar-vps.sh`, que vai dentro do pacote) gera o `.env` com segredos novos,
acha a rede do Nginx Proxy Manager, sobe o banco, aplica as migrações, cria a loja e a Dona e oferece criar o seu
usuário Master. Nas próximas vezes mantém o `.env`, **faz backup antes**, troca o código (o anterior fica em
`/opt/loja-festa.anterior`), reconstrói, migra e confere se o app ficou saudável. Se o build falhar, os containers
antigos continuam no ar. Depois siga os passos 0 (auditoria), 6 (domínio no NPM) e 7 (backup externo) abaixo.

## Passo 0 — Auditoria rápida (só leitura, não muda nada)

Um sistema com dinheiro de cliente vai dividir a máquina com outros serviços. Antes de subir,
confirme que nenhum deles está aberto para a internet sem proteção.

```bash
nproc; free -h; df -h /
lscpu | grep -E 'Model name|^CPU\(s\)'
docker ps --format 'table {{.Names}}\t{{.Ports}}'     # quais portas cada container publica
sudo ss -tlnp | grep -v 127.0.0.1                      # o que escuta para fora
sudo ufw status verbose
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication)'
docker network ls
```

**O que procurar e corrigir antes de continuar:**

| Achado | Risco | Correção |
| --- | --- | --- |
| Portainer em `0.0.0.0:9000` ou `9443` | Quem entra no Portainer controla o servidor inteiro, incluindo o banco da loja | Publicar só em `127.0.0.1` e acessar por túnel SSH, ou colocar atrás do NPM com lista de acesso |
| Painel do NPM em `0.0.0.0:81` | Controle de todos os domínios e certificados | Mesma correção |
| n8n, WPPConnect ou Streamlit com porta publicada direto | Acesso sem passar pelo HTTPS do NPM | Remover a porta publicada; deixar só o NPM expor |
| `passwordauthentication yes` ou `permitrootlogin yes` | Força bruta no SSH | Login só com chave SSH, sem root |

> **Atenção:** o Docker ignora o UFW. Uma porta publicada com `-p 9000:9000` fica aberta para a
> internet mesmo com `ufw deny`. Por isso o `docker ps` acima é o teste que vale.

Firewall do host (se ainda não estiver assim):

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH        # ou a porta SSH personalizada
sudo ufw allow 80,443/tcp
sudo ufw enable
sudo apt install -y fail2ban  # bloqueia força bruta no SSH
```

## Passo 1 — Baixar o projeto

```bash
sudo mkdir -p /opt/loja-festa && sudo chown "$USER" /opt/loja-festa
git clone git@github.com:SEU_USUARIO/loja-festa.git /opt/loja-festa
cd /opt/loja-festa
```

Use uma *deploy key* só de leitura do GitHub, não a sua chave pessoal.

## Passo 2 — Segredos

```bash
cp .env.example .env && chmod 600 .env
for v in DB_OWNER_PASSWORD APP_DB_PASSWORD SESSION_SECRET DADOS_CHAVE; do
  sed -i "s|^$v=.*|$v=$(openssl rand -hex 32)|" .env
done
```

Descubra a rede do NPM e coloque em `NPM_NETWORK` no `.env`:

```bash
docker inspect "$(docker ps --filter ancestor=jc21/nginx-proxy-manager -q | head -1)" \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

**Guarde o `DADOS_CHAVE` fora da VPS agora** (gerenciador de senhas e uma cópia em papel no cofre).
Ele cifra CPF, telefone, e-mail, endereço, medidas e contratos. Sem ele, esses dados e os backups ficam
ilegíveis para sempre. Nunca troque o valor depois que a loja começar a usar.

**Não** crie esta stack pelo editor do Portainer colando o `.env`: os segredos ficariam salvos lá.
Suba pela linha de comando. O Portainer continua servindo para ver os containers.

## Passo 3 — Banco e backup

```bash
docker compose build
docker compose up -d db backup
docker compose exec db migrate.sh
```

Saída esperada: `> aplicando 0001_schema ... ok` e `> aplicando 0002_ajustes_operacionais ... ok`.

## Passo 4 — Criar a loja e a conta da Dona

Faça **junto com a Dona**, com ela digitando a própria senha (mínimo de 10 caracteres):

```bash
docker compose run --rm app python -m app.cli criar-loja \
  --nome "Nome da Loja" --login dona --nome-dona "Nome Dela"
```

Copie o `LOJA_ID=...` que aparecer para o `.env` e suba o app:

```bash
docker compose up -d app
docker compose ps          # os três containers "healthy"
```

## Passo 5 — Sua conta de Desenvolvedor

O papel de Desenvolvedor só pode ser concedido pela Dona, e isso fica na auditoria:

```bash
docker compose run --rm app python -m app.cli criar-usuario \
  --login dev --nome "Carlos" --papel DESENVOLVEDOR --concedido-por dona
```

Funcionários (atendentes, costureiras) serão cadastrados pela tela do sistema a partir da Sprint 1.
Até lá, o mesmo comando serve (`--papel ATENDENTE`, `--papel GERENTE`, `--ate AAAA-MM-DD` para freelance).

## Passo 6 — Domínio no Nginx Proxy Manager

1. No DNS do domínio, crie um registro `A` para `loja.seudominio.com.br` apontando para o IP da VPS.
2. No NPM, em **Hosts → Proxy Hosts → Add Proxy Host**:
   - Domain Names: `loja.seudominio.com.br`
   - Scheme `http`, Forward Hostname `loja-app`, Forward Port `8000`
   - Marque **Block Common Exploits**
3. Aba **SSL**: *Request a new SSL Certificate*, marque **Force SSL**, **HTTP/2** e **HSTS Enabled**.
4. Teste: `https://loja.seudominio.com.br/health` deve mostrar `"status":"ok"`.
5. Coloque o endereço no `.env` (`SITE_URL=https://loja.seudominio.com.br`) e rode
   `docker compose up -d app`. É para esse endereço que o Mercado Pago avisa os pagamentos dos links.
   Quem abre o domínio vê a vitrine (`/site`); a equipe entra por "Área da equipe" no rodapé.
6. Opcional: no NPM, aba **Advanced**, limite o tamanho do envio de fotos: `client_max_body_size 60m;`

## Passo 6b — Cloudflare na frente (recomendado)

A Cloudflare esconde o IP da VPS, segura robôs e ataques e deixa a área da equipe abrir só do Brasil.

1. Crie a conta grátis em cloudflare.com, adicione o domínio e troque os *nameservers* no registro.br
   pelos dois que a Cloudflare mostrar (leva de minutos a algumas horas).
2. **DNS**: o registro `A` de `loja.seudominio.com.br` com a **nuvem laranja ligada** (Proxied).
3. **SSL/TLS → Overview**: modo **Full (strict)** (o NPM já tem o certificado). Ligue **Always Use HTTPS**.
4. **Security → Bots**: ligue **Bot Fight Mode**. **Security → WAF → Managed rules**: ligue as regras grátis.
5. **Security → WAF → Rate limiting rules** (grátis, 1 regra): caminho contém `/login`, 10 pedidos em 1 minuto → Block.
6. No `.env`: `CLOUDFLARE=1` (o sistema passa a ler o IP real e o país) e `SO_BRASIL=1`
   (a área da equipe só abre do Brasil; o site da vitrine e os avisos do Mercado Pago/WhatsApp continuam abertos).
   Aplique: `docker compose up -d app`.
7. Recomendado: no firewall da VPS, aceite as portas 80/443 **só** dos IPs da Cloudflare
   (lista em cloudflare.com/ips). Assim ninguém chega na VPS dando a volta na Cloudflare.

> Com `CLOUDFLARE=1` **sem** a Cloudflare na frente, qualquer um poderia inventar o próprio IP no cabeçalho.
> Só ligue depois do passo 2 funcionando.

## Passo 6c — WhatsApp (WPPConnect que já roda na VPS) e e-mail

1. Descubra o nome do container e a rede do WPPConnect:
   `docker ps --format '{{.Names}}' | grep -i wpp` e
   `docker inspect <nome> --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'`.
   Se ele já estiver na rede do NPM (`NPM_NETWORK`), nada a fazer. Senão:
   `docker network connect "$NPM_NETWORK" <nome-do-wppconnect>`.
2. Pegue a chave secreta do WPPConnect (variável `SECRET_KEY` do container dele).
3. No sistema, como Dona: **WhatsApp e e-mail** → *Dados do WPPConnect*:
   endereço `http://<nome-do-wppconnect>:21465`, sessão `loja`, a chave, o número da loja e o
   endereço do sistema (`https://loja.seudominio.com.br`). Salve e toque em **Gerar código de conexão**.
4. No celular da loja: WhatsApp → Dispositivos conectados → Conectar dispositivo →
   **Conectar com número de telefone** → digite o código (ex.: `A1A4-FCB3`). A tela mostra "Conectado".
   (Precisa de uma versão do WPPConnect Server com "phone code"; nas antigas, a tela mostra o QR code.)
5. Envie o teste pelo botão **Mandar teste para mim**. O container `loja-avisos` é quem envia
   a fila; confira com `docker compose logs -f avisos`.
6. E-mail: com o Gmail da loja, ative a verificação em 2 etapas, crie uma **senha de app** e cole
   na mesma tela. A senha fica cifrada no banco.
7. Em **Equipe**, cadastre o WhatsApp de cada funcionário e marque os avisos que cada um recebe.

## Passo 6d — Nota do Google e frete (opcional)

* **Nota do Google automática**: no Google Cloud, ative a *Places API (New)*, crie uma chave e **restrinja ao IP da
  VPS**. Coloque em `GOOGLE_PLACES_KEY=` no `.env` e reinicie o container `loja-avisos`. Depois a Dona cola o
  *Place ID* da loja em **Avaliações → Nota do Google**. O Google cobra por chamada acima da cota grátis; o sistema
  busca só 1 vez por dia. Sem a chave, a Dona digita a nota à mão.
* **Frete pelo Melhor Envio**: a Dona cola o token em **Sacola e entrega → Melhor Envio** (tem o passo a passo na
  tela e o botão *Testar conexão*); fica guardado cifrado no banco. Quem preferir pode deixar no `.env`
  (`MELHOR_ENVIO_TOKEN=`, `MELHOR_ENVIO_SANDBOX=1` para teste) — o da tela vale mais. Permissões do token:
  `shipping-calculate`, `shipping-tracking` e `users-read`. As etiquetas são geradas e pagas no painel do Melhor
  Envio; o id da etiqueta ou o código de rastreio vai no pedido (a cliente recebe no WhatsApp).
* **Pix de qualquer banco (Inter, Itaú…)**: basta a chave Pix da loja em **Crediário → Chave Pix**. O link Pix mostra
  QR e copia-e-cola com o valor certo; a loja confirma ao ver no extrato. Baixa automática pelo banco exige a API Pix
  do banco (certificado digital emitido no internet banking PJ, cadastro de webhook) — dá para ligar depois, banco a banco.
* **Usuário Master** (acesso total): `docker compose exec loja-app python -m app.cli criar-usuario --login carlos
  --nome "Carlos" --papel MASTER --concedido-por dona` — ou em **Equipe**, pela Dona.
* **Sacola do site**: exige o pagamento online (Mercado Pago) configurado e o `SITE_URL` com https (é para lá que
  o Mercado Pago manda o aviso de pagamento).

## Passo 7 — Backup fora da VPS

O backup leva o banco e também as fotos das peças (volume `fotos`).

Sem cópia externa, perder a VPS é perder a loja. Recomendado: um bucket no Backblaze B2
(poucos centavos por mês para esse volume).

1. Crie o bucket e uma chave de aplicação com acesso só a ele.
2. Preencha no `.env`: `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` (gere com `openssl rand -hex 32`),
   `RESTIC_S3_ACCESS_KEY`, `RESTIC_S3_SECRET_KEY`.
3. **Guarde o `RESTIC_PASSWORD` também fora da VPS** (gerenciador de senhas). Sem ele o backup não abre.
4. Aplique e teste:

```bash
docker compose up -d backup
docker compose exec backup backup.sh
docker compose exec backup restore-test.sh      # deve terminar com "RESTAURAÇÃO OK"
```

**Todo mês:** rode `restore-test.sh`. Backup que nunca foi restaurado não é backup.

## Travar as dependências (uma vez, e quando mudar o requirements.txt)

```bash
bash scripts/travar-dependencias.sh      # gera app/requirements.lock com o hash de cada pacote
git add app/requirements.lock && git commit -m "Trava dependências"
```
A partir daí o build só instala pacotes idênticos aos conferidos (pacote trocado no caminho não entra).

## Atualizar o sistema

```bash
cd /opt/loja-festa
git pull
docker compose up -d --build      # os dados ficam no volume; o banco reinicia em segundos
docker compose exec db migrate.sh
```

Atualize fora do horário da loja. Antes de uma atualização grande, rode `docker compose exec backup backup.sh`.

## Consultas do dia a dia

```bash
docker compose ps                         # saúde dos containers
docker compose logs --tail 100 app        # erros do app (JSON, com request_id)
docker stats --no-stream                  # memória: a loja usa no máximo ~1,4 GB
docker compose exec backup cat /backups/ultimo_backup.json
```

Quando a tela mostrar "Código para o suporte: abc123...", procure por ele:
`docker compose logs app | grep abc123`.

## Limite honesto desta arquitetura

Quem tem acesso root à VPS (você) tecnicamente consegue tudo, inclusive mexer no banco.
As proteções são a **auditoria**, que a Diretoria consulta, e a **cópia externa**, que fica fora
do seu alcance se a chave do bucket for da Dona. Para um nível acima disso, a chave do bucket pode
ser só de escrita (sem permissão de apagar), guardada pela Diretoria.
