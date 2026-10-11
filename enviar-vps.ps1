<#
  Envia o sistema da loja do seu PC (Windows) para a VPS e instala/atualiza lá, tudo pelo PowerShell.
  Usa o ssh/scp que já vêm no Windows 10/11 (OpenSSH). Não passa pelo GitHub.

  Uso, dentro da pasta do projeto (ou pelo enviar-vps.cmd):
    .\enviar-vps.ps1           empacota, envia e instala (1ª vez) ou atualiza (com backup antes)
    .\enviar-vps.ps1 testar    só confere se consegue entrar na VPS e se ela tem Docker
    .\enviar-vps.ps1 status    mostra os containers da loja na VPS
    .\enviar-vps.ps1 logs      últimas linhas do app na VPS
    .\enviar-vps.ps1 destino   troca o endereço/usuário/porta da VPS guardados

  O endereço da VPS fica em .env.vps (sem senha nenhuma). A senha do SSH é pedida pelo próprio ssh
  a cada conexão; com chave SSH (ssh-keygen + ssh-copy-id) não pede nada.
#>
param(
    [ValidateSet("enviar", "testar", "status", "logs", "destino")]
    [string]$Acao = "enviar"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $utf8
try { [Console]::OutputEncoding = $utf8 } catch { }

$Arq = Join-Path $PSScriptRoot ".env.vps"
$Pasta = "/opt/loja-festa"

function Escrever([string]$texto, [string]$cor = "Cyan") { Write-Host $texto -ForegroundColor $cor }

function Exigir([string]$programa, [string]$ajuda) {
    if (-not (Get-Command $programa -ErrorAction SilentlyContinue)) { throw "$programa não encontrado. $ajuda" }
}

function Ler-Destino {
    $h = @{}
    if (Test-Path -LiteralPath $Arq) {
        foreach ($l in (Get-Content -LiteralPath $Arq -Encoding UTF8)) {
            if ($l -match '^\s*([A-Z_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $h
}

function Pedir-Destino([hashtable]$h) {
    Escrever "== Dados da VPS (ficam em .env.vps; nenhuma senha é guardada) ==" "Green"
    $r = Read-Host "Endereço da VPS (IP ou nome) [$($h['VPS_HOST'])]"; if ($r) { $h["VPS_HOST"] = $r.Trim() }
    $padrao = if ($h["VPS_USUARIO"]) { $h["VPS_USUARIO"] } else { "root" }
    $r = Read-Host "Usuário do SSH [$padrao]"; $h["VPS_USUARIO"] = if ($r) { $r.Trim() } else { $padrao }
    $padrao = if ($h["VPS_PORTA"]) { $h["VPS_PORTA"] } else { "22" }
    $r = Read-Host "Porta do SSH [$padrao]"; $h["VPS_PORTA"] = if ($r) { $r.Trim() } else { $padrao }
    # Só letras, números, ponto, hífen e sublinhado: o valor vai para a linha de comando do ssh
    if ($h["VPS_HOST"] -notmatch '^[A-Za-z0-9.\-]{1,253}$') { throw "Endereço da VPS inválido." }
    if ($h["VPS_USUARIO"] -notmatch '^[a-z_][a-z0-9_\-]{0,31}$') { throw "Usuário do SSH inválido." }
    if ($h["VPS_PORTA"] -notmatch '^\d{1,5}$') { throw "Porta do SSH inválida." }
    $linhas = @("# Destino do enviar-vps.ps1 (sem senhas).", "VPS_HOST=$($h['VPS_HOST'])",
                "VPS_USUARIO=$($h['VPS_USUARIO'])", "VPS_PORTA=$($h['VPS_PORTA'])")
    [System.IO.File]::WriteAllLines($Arq, [string[]]$linhas, $utf8)
    return $h
}

function Destino {
    $h = Ler-Destino
    if (-not $h["VPS_HOST"]) { $h = Pedir-Destino $h }
    return $h
}

# Comando na VPS. Com -t o terminal fica interativo (perguntas da instalação e senha do sudo).
# Não devolve nada de propósito: a saída do ssh vai direto para a tela (se fosse capturada, as perguntas
# sem quebra de linha não apareceriam). O resultado fica em $LASTEXITCODE.
function Vps([hashtable]$h, [string]$comando, [switch]$Interativo) {
    $a = @("-p", $h["VPS_PORTA"], "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=accept-new")
    if ($Interativo) { $a += "-t" }
    $a += @("$($h['VPS_USUARIO'])@$($h['VPS_HOST'])", $comando)
    & ssh @a
}

# root roda direto; outro usuário usa sudo (pede a senha dele na VPS)
function Com-Sudo([hashtable]$h, [string]$comando) {
    if ($h["VPS_USUARIO"] -eq "root") { return $comando }
    return "sudo $comando"
}

function Acao-Testar([hashtable]$h) {
    Escrever "Entrando em $($h['VPS_USUARIO'])@$($h['VPS_HOST']):$($h['VPS_PORTA'])..."
    Vps $h ("echo 'SSH ok'; uname -sr; " + (Com-Sudo $h "docker compose version") + " && df -h / | tail -1 && free -h | sed -n 2p") -Interativo
    $c = $LASTEXITCODE
    if ($c -ne 0) { throw "Não consegui entrar ou a VPS não tem Docker/compose (código $c)." }
    Escrever "VPS pronta para receber o sistema." "Green"
}

function Empacotar {
    Exigir "tar" "O Windows 10 (1803+) e o 11 já trazem o tar. Atualize o Windows."
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "docker-compose.yml"))) { throw "Rode dentro da pasta do projeto loja-festa." }
    $nome = "loja-festa-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".tar.gz"
    $saida = Join-Path ([IO.Path]::GetTempPath()) $nome
    # Fora do pacote: segredos e coisas só do seu PC (o .env da VPS nunca sai de lá)
    $fora = @(".env", ".env.local", ".env.semdocker", ".env.semdocker.avisos", ".env.vps", ".venv", "dados-local",
              ".git", "__pycache__", "*.pyc", "backups", "*.dump", ".pytest_cache")
    $args2 = @("-czf", $saida)
    foreach ($f in $fora) { $args2 += "--exclude=$f" }
    $args2 += @("-C", $PSScriptRoot, ".")
    & tar @args2
    if ($LASTEXITCODE -ne 0) { throw "Não consegui empacotar o projeto." }
    $mb = [math]::Round((Get-Item -LiteralPath $saida).Length / 1MB, 1)
    Escrever "Pacote pronto: $nome ($mb MB)" "DarkGray"
    return $saida
}

function Acao-Enviar([hashtable]$h) {
    Exigir "ssh" "Ative o 'Cliente OpenSSH' em Configurações > Aplicativos > Recursos opcionais."
    Exigir "scp" "Ative o 'Cliente OpenSSH' em Configurações > Aplicativos > Recursos opcionais."
    Escrever "Destino: $($h['VPS_USUARIO'])@$($h['VPS_HOST']):$($h['VPS_PORTA']) -> $Pasta" "Green"
    $r = Read-Host "Atualize fora do horário da loja. Continuar? [S/n]"
    if ($r -match '^[nN]') { Escrever "Nada foi enviado."; return }

    $pacote = Empacotar
    try {
        $remoto = "/tmp/" + (Split-Path -Leaf $pacote)
        Escrever "Enviando para a VPS (a senha do SSH pode ser pedida)..."
        & scp -P $h["VPS_PORTA"] -o StrictHostKeyChecking=accept-new $pacote "$($h['VPS_USUARIO'])@$($h['VPS_HOST']):$remoto"
        if ($LASTEXITCODE -ne 0) { throw "O envio falhou (scp)." }

        # O instalador sai de dentro do próprio pacote: o que roda na VPS é exatamente a versão enviada
        $instalar = "tar -xzOf $remoto ./scripts/instalar-vps.sh | sed 's/\r`$//' > /tmp/instalar-loja.sh && " +
                    (Com-Sudo $h "bash /tmp/instalar-loja.sh $remoto $Pasta") +
                    "; c=`$?; rm -f /tmp/instalar-loja.sh; exit `$c"
        Escrever "Instalando na VPS (responda as perguntas que aparecerem)..."
        Vps $h $instalar -Interativo
        $c = $LASTEXITCODE
        if ($c -ne 0) { throw "A instalação na VPS parou com erro (código $c). Veja a mensagem acima." }
        Escrever ""
        Escrever "Sistema enviado e rodando na VPS." "Green"
    } finally {
        Remove-Item -LiteralPath $pacote -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------------------------- #
try {
    switch ($Acao) {
        "destino" { Pedir-Destino (Ler-Destino) | Out-Null; Escrever "Destino guardado em .env.vps." "Green" }
        "testar"  { Acao-Testar (Destino) }
        "status"  { $h = Destino; Vps $h ("cd $Pasta && " + (Com-Sudo $h "docker compose ps")) -Interativo }
        "logs"    { $h = Destino; Vps $h ("cd $Pasta && " + (Com-Sudo $h "docker compose logs --tail 80 app")) -Interativo }
        "enviar"  { Acao-Enviar (Destino) }
    }
}
catch {
    Escrever ""
    Escrever "ERRO: $($_.Exception.Message)" "Red"
    exit 1
}
