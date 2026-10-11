<#
  Roda o sistema da loja no seu PC, com o Docker Desktop. Nada vai para o GitHub nem para a VPS.

  Uso, dentro da pasta do projeto:
    .\local.ps1            sobe tudo e abre o navegador (na primeira vez cria a loja e a conta da Dona)
    .\local.ps1 usuario    cria um funcionário, para testar o que cada cargo vê
    .\local.ps1 celular    libera para abrir no celular (no mesmo Wi-Fi do PC)
    .\local.ps1 testes     roda os testes das travas do banco e do backup
    .\local.ps1 logs       mostra o que o app está registrando (Ctrl+C para sair)
    .\local.ps1 parar      desliga tudo (os dados continuam guardados)
    .\local.ps1 zerar      apaga TODOS os dados locais e começa do zero

  Se o Windows bloquear o script, use o local.cmd (duplo clique ou "local.cmd testes").
#>
param(
    [ValidateSet("subir", "celular", "usuario", "testes", "logs", "parar", "zerar")]
    [string]$Acao = "subir"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $utf8
try { [Console]::OutputEncoding = $utf8 } catch { }

$EnvArq = Join-Path $PSScriptRoot ".env.local"
$Base = @("compose", "-f", "docker-compose.local.yml", "--env-file", ".env.local")
$Papeis = @("ATENDENTE", "COSTUREIRA", "AJUDANTE_COSTURA", "GERENTE", "LIMPEZA",
            "DONO", "RH", "FINANCEIRO", "DESENVOLVEDOR")

# --------------------------------------------------------------------------- #
function Escrever([string]$texto, [string]$cor = "Cyan") {
    Write-Host $texto -ForegroundColor $cor
}

# docker compose com os arquivos do ambiente local; para o script se der erro
function Dc {
    & docker @Base @args
    if ($LASTEXITCODE -ne 0) { throw "O comando falhou: docker compose $($args -join ' ')" }
}

# Roda um comando sem mostrar nada e devolve o código de saída.
# (No PowerShell 5.1, redirecionar o erro de um programa com "Stop" ativo derrubaria o script.)
function Silencioso([scriptblock]$bloco) {
    $antes = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $bloco *> $null; return $LASTEXITCODE }
    finally { $ErrorActionPreference = $antes }
}

function Checar-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker não encontrado. Instale o Docker Desktop: https://www.docker.com/products/docker-desktop/"
    }
    if ((Silencioso { docker info }) -ne 0) {
        throw "O Docker Desktop não está rodando. Abra o Docker Desktop, espere aparecer 'Engine running' e rode de novo."
    }
    if ((Silencioso { docker compose version }) -ne 0) {
        throw "Seu Docker não tem o 'docker compose'. Atualize o Docker Desktop."
    }
}

function Novo-Segredo {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Ler-Env {
    $h = @{}
    if (Test-Path -LiteralPath $EnvArq) {
        foreach ($linha in (Get-Content -LiteralPath $EnvArq -Encoding UTF8)) {
            if ($linha -match '^\s*([A-Z_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $h
}

function Gravar-Env([hashtable]$h) {
    $linhas = @("# Gerado por local.ps1. Só para testes no seu PC: NUNCA use estes valores na VPS.")
    foreach ($k in @("DB_OWNER_PASSWORD", "APP_DB_PASSWORD", "SESSION_SECRET", "DADOS_CHAVE", "LOJA_ID", "DONA_LOGIN", "PORTA_LOCAL")) {
        $linhas += "$k=$($h[$k])"
    }
    [System.IO.File]::WriteAllLines($EnvArq, [string[]]$linhas, $utf8)
}

function Garantir-Env {
    $h = Ler-Env
    $mudou = $false
    foreach ($k in @("DB_OWNER_PASSWORD", "APP_DB_PASSWORD", "SESSION_SECRET", "DADOS_CHAVE")) {
        if (-not $h[$k]) { $h[$k] = Novo-Segredo; $mudou = $true }
    }
    if (-not $h["PORTA_LOCAL"]) { $h["PORTA_LOCAL"] = "8000"; $mudou = $true }
    if ($mudou) { Gravar-Env $h; Escrever "Senhas internas geradas em .env.local" "DarkGray" }
    return $h
}

function Texto-Seguro([System.Security.SecureString]$s) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Perguntar([string]$pergunta, [string]$padrao = "") {
    $sufixo = ""
    if ($padrao) { $sufixo = " [$padrao]" }
    $r = Read-Host "$pergunta$sufixo"
    if (-not $r) { return $padrao }
    return $r.Trim()
}

function Pedir-Senha([string]$quem) {
    while ($true) {
        Escrever "A senha precisa ter de 8 a 32 caracteres, com MAIÚSCULA, minúscula, número e símbolo. Ex.: Gato3Azul!Pipoca (evite nome da loja ou palavras comuns)" "DarkGray"
        $p1 = Texto-Seguro (Read-Host "Senha de $quem (não aparece na tela)" -AsSecureString)
        $p2 = Texto-Seguro (Read-Host "Repita a senha" -AsSecureString)
        if ($p1 -ne $p2)          { Escrever "As senhas não conferem. Tente de novo." "Yellow"; continue }
        if ($p1.Length -lt 8 -or $p1.Length -gt 32) { Escrever "A senha precisa ter de 8 a 32 caracteres." "Yellow"; continue }
        if ($p1 -cnotmatch '[A-Z]' -or $p1 -cnotmatch '[a-z]' -or $p1 -notmatch '\d' -or $p1 -notmatch '[^A-Za-z0-9\s]') {
            Escrever "Falta maiúscula, minúscula, número ou símbolo." "Yellow"; continue
        }
        return $p1
    }
}

# Roda a CLI do app mandando a senha pela entrada padrão (nunca pela linha de comando)
function Cli-Com-Senha([string]$senha, [string[]]$argumentos) {
    $saida = $senha | & docker @Base run --rm -T app python -m app.cli @argumentos "--senha-stdin"
    $codigo = $LASTEXITCODE
    $saida | ForEach-Object { Write-Host $_ }
    if ($codigo -ne 0) { throw "O banco recusou o cadastro (veja a mensagem acima)." }
    return ($saida -join "`n")
}

# O banco guarda as senhas de quando o volume foi criado. Se o .env.local foi gerado de novo
# (outra pasta, arquivo apagado), elas não batem mais e o app é recusado. Como o comando roda
# dentro do container do banco (acesso local), dá para realinhar com o .env.local, que é a referência.
function Sincronizar-Senhas([hashtable]$h) {
    $sql = "ALTER ROLE app_user PASSWORD '$($h['APP_DB_PASSWORD'])';`nALTER ROLE loja_owner PASSWORD '$($h['DB_OWNER_PASSWORD'])';`n"
    $sql | & docker @Base exec -T db psql -X -q -v ON_ERROR_STOP=1 -U loja_owner -d loja | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Não consegui ajustar as senhas internas do banco." }
}

function Consultar([string]$consulta) {
    $saida = & docker @Base exec -T db psql -X -q -At -U loja_owner -d loja -c $consulta
    if ($LASTEXITCODE -ne 0) { throw "Falha ao consultar o banco." }
    return $saida | Where-Object { $_ }      # quem chama embrulha com @() (lista de 0, 1 ou mais itens)
}

# Se o banco já tem uma loja (de uma pasta anterior), oferece usar essa em vez de criar outra.
function Usar-Loja-Existente([hashtable]$h) {
    $lojas = @(Consultar "SELECT l.id || '|' || l.nome || '|' || COALESCE((SELECT u.login FROM usuarios u JOIN usuario_papeis up ON up.usuario_id = u.id JOIN papeis p ON p.id = up.papel_id WHERE u.loja_id = l.id AND p.codigo = 'DONO' ORDER BY u.criado_em LIMIT 1), '') FROM lojas l ORDER BY l.criado_em")
    if ($lojas.Count -eq 0) { return $false }
    Escrever ""
    Escrever "Encontrei loja(s) já cadastrada(s) neste computador:" "Green"
    for ($i = 0; $i -lt $lojas.Count; $i++) {
        $p = $lojas[$i].Split("|")
        Write-Host ("  {0}. {1}  (login da Dona: {2})" -f ($i + 1), $p[1], $p[2])
    }
    $r = Perguntar "Número da loja para usar, ou N para criar uma nova" "1"
    if ($r -match '^[nN]') { return $false }
    $n = 0
    if (-not [int]::TryParse($r, [ref]$n) -or $n -lt 1 -or $n -gt $lojas.Count) { throw "Opção inválida." }
    $p = $lojas[$n - 1].Split("|")
    $h["LOJA_ID"] = $p[0]
    $h["DONA_LOGIN"] = $p[2]
    Gravar-Env $h
    Escrever "Usando a loja '$($p[1])'. Entre com a senha que você criou para '$($p[2])' da primeira vez." "Green"
    return $true
}

function Url([hashtable]$h) { return "http://localhost:$($h['PORTA_LOCAL'])" }

# --------------------------------------------------------------------------- #
function Criar-Loja([hashtable]$h) {
    Escrever ""
    Escrever "== Primeira vez: vamos criar a loja e a conta da Dona ==" "Green"
    $nomeLoja = Perguntar "Nome da loja" "Loja de Festa (teste)"
    $nomeDona = Perguntar "Nome da Dona" "Maria"
    $login    = (Perguntar "Login da Dona" "dona").ToLower()
    $senha    = Pedir-Senha "$nomeDona"

    $texto = Cli-Com-Senha $senha @("criar-loja", "--nome", $nomeLoja, "--login", $login, "--nome-dona", $nomeDona)
    if ($texto -notmatch 'LOJA_ID=([0-9a-f-]{36})') { throw "Não encontrei o LOJA_ID na resposta." }
    $h["LOJA_ID"] = $Matches[1]
    $h["DONA_LOGIN"] = $login
    Gravar-Env $h
}

function Acao-Subir {
    Checar-Docker
    $h = Garantir-Env
    Escrever "Construindo as imagens (na primeira vez leva alguns minutos)..."
    Dc build
    Escrever "Subindo o banco..."
    Dc up -d --wait db
    Escrever "Aplicando as migrações..."
    Dc exec -T db migrate.sh
    Sincronizar-Senhas $h
    if (-not $h["LOJA_ID"]) {
        if (-not (Usar-Loja-Existente $h)) { Criar-Loja $h }
        $h = Ler-Env
    }
    Escrever "Subindo o app..."
    try { Dc up -d --wait app avisos }
    catch {
        Escrever "O app não subiu. Se o erro falar em 'port is already allocated', troque PORTA_LOCAL no .env.local (ex.: 8080)." "Yellow"
        throw
    }
    $url = Url $h
    Escrever ""
    Escrever "Pronto! Abra $url" "Green"
    Escrever "Entre com o login '$($h['DONA_LOGIN'])' e a senha que você criou."
    Escrever "Para testar outro cargo: .\local.ps1 usuario   |   Para desligar: .\local.ps1 parar" "DarkGray"
    try { Start-Process $url } catch { }
}

# IPs do PC na rede de casa (só placas ligadas e com roteador; ignora as virtuais do Docker/WSL)
function Ips-Da-Rede {
    $ips = @()
    try {
        $ips = @(Get-NetIPConfiguration -ErrorAction Stop |
                 Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
                 ForEach-Object { $_.IPv4Address } | ForEach-Object { $_.IPAddress })
    } catch { }
    return @($ips | Where-Object { $_ -and $_ -notlike "127.*" -and $_ -notlike "169.254.*" })
}

# Regra do Firewall do Windows: só aparelhos da mesma rede (LocalSubnet) entram na porta do app
function Tem-Regra([string]$nome) {
    return [bool](Get-NetFirewallRule -DisplayName $nome -ErrorAction SilentlyContinue)
}

function Liberar-Firewall([string]$porta) {
    $nome = "Loja local (celular) - porta $porta"
    if (Tem-Regra $nome) { Escrever "Firewall: a porta $porta já está liberada para a sua rede." "DarkGray"; return }
    $comando = "New-NetFirewallRule -DisplayName '$nome' -Direction Inbound -Protocol TCP -LocalPort $porta " +
               "-RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null"
    $eu = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if ($eu.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        try { Invoke-Expression $comando } catch { }
    } else {
        Escrever "O Firewall do Windows precisa liberar a porta $porta para os aparelhos da sua rede." "Yellow"
        Escrever "Vai aparecer uma janela pedindo permissão de administrador: clique em 'Sim'." "Yellow"
        # -EncodedCommand evita qualquer problema de aspas ao passar o comando para a janela de administrador
        $codificado = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($comando))
        try {
            Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden `
                -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $codificado"
        } catch { }
    }
    if (Tem-Regra $nome) {
        Escrever "Firewall liberado para os aparelhos da sua rede." "Green"
    } else {
        Escrever "Não consegui liberar o Firewall. Se o celular não abrir, abra o PowerShell como administrador" "Yellow"
        Escrever "(botão direito no menu Iniciar > 'Terminal (Admin)') e cole:" "Yellow"
        Escrever "   $comando" "White"
    }
}

function Acao-Celular {
    Checar-Docker
    $h = Garantir-Env
    if (-not $h["LOJA_ID"]) { throw "Rode primeiro '.\local.ps1' para criar a loja." }
    $ips = @(Ips-Da-Rede)
    if ($ips.Count -eq 0) { throw "Não achei o endereço do PC na rede. Confira se o PC está conectado ao Wi-Fi ou ao cabo." }
    $porta = $h["PORTA_LOCAL"]
    $env:ENDERECO_LOCAL = "0.0.0.0"          # o app passa a aceitar conexões da rede de casa
    Escrever "Preparando..."
    Dc build
    Dc up -d --wait db
    Dc exec -T db migrate.sh
    Sincronizar-Senhas $h
    Dc up -d --wait app avisos
    Liberar-Firewall $porta
    Escrever ""
    Escrever "Pronto! No celular, conectado no MESMO Wi-Fi do PC, abra no navegador:" "Green"
    foreach ($ip in $ips) { Escrever "   http://${ip}:$porta" "Green" }
    Escrever ""
    Escrever "Não abriu? Confira se o celular está no Wi-Fi (e não nos dados móveis) e se o PC não está numa rede de convidados." "DarkGray"
    Escrever "Enquanto estiver assim, qualquer aparelho do seu Wi-Fi vê a tela de login." "Yellow"
    Escrever "Para voltar a ser só no PC: .\local.ps1   |   Para desligar: .\local.ps1 parar" "Yellow"
}

function Acao-Usuario {
    Checar-Docker
    $h = Garantir-Env
    if (-not $h["LOJA_ID"]) { throw "Rode primeiro '.\local.ps1' para criar a loja." }
    Dc up -d --wait db
    Sincronizar-Senhas $h

    Escrever ""
    Escrever "== Novo funcionário ==" "Green"
    $nome  = Perguntar "Nome"
    $login = (Perguntar "Login").ToLower()
    if (-not $nome -or -not $login) { throw "Nome e login são obrigatórios." }
    for ($i = 0; $i -lt $Papeis.Count; $i++) { Write-Host ("  {0}. {1}" -f ($i + 1), $Papeis[$i]) }
    $n = [int](Perguntar "Número do cargo" "1")
    if ($n -lt 1 -or $n -gt $Papeis.Count) { throw "Cargo inválido." }
    $papel = $Papeis[$n - 1]
    $ate = Perguntar "Freelance? Último dia de acesso (AAAA-MM-DD) ou Enter para fixo" ""
    $quem = Perguntar "Login de quem autoriza" $h["DONA_LOGIN"]
    $senha = Pedir-Senha $nome

    $argumentos = @("criar-usuario", "--login", $login, "--nome", $nome, "--papel", $papel, "--concedido-por", $quem)
    if ($ate) { $argumentos += @("--ate", $ate) }
    Cli-Com-Senha $senha $argumentos | Out-Null
    Escrever "Entre em $(Url $h) com o login '$login' para ver a tela desse cargo." "Green"
}

function Acao-Testes {
    Checar-Docker
    $h = Garantir-Env
    Dc up -d --wait db
    Sincronizar-Senhas $h
    Dc --profile testes build testes
    Escrever "Rodando os testes (usam um banco separado; seus dados de teste não são tocados)..."
    Dc --profile testes run --rm testes
    Escrever "Todos os testes passaram." "Green"
}

function Acao-Zerar {
    Escrever "Isto apaga a loja, os usuários e todos os dados do ambiente LOCAL." "Yellow"
    if ((Read-Host "Digite ZERAR para confirmar") -cne "ZERAR") { Escrever "Nada foi apagado."; return }
    Checar-Docker
    if (Test-Path -LiteralPath $EnvArq) {
        Dc --profile testes down -v --remove-orphans
        Remove-Item -LiteralPath $EnvArq -Force
    }
    Escrever "Ambiente local zerado. Rode .\local.ps1 para começar de novo." "Green"
}

# --------------------------------------------------------------------------- #
try {
    switch ($Acao) {
        "subir"   { Acao-Subir }
        "celular" { Acao-Celular }
        "usuario" { Acao-Usuario }
        "testes"  { Acao-Testes }
        "logs"    { Checar-Docker; Dc logs --tail 100 -f app }
        "parar"   { Checar-Docker; if (Test-Path -LiteralPath $EnvArq) { Dc --profile testes down }; Escrever "Desligado. Os dados continuam guardados." "Green" }
        "zerar"   { Acao-Zerar }
    }
}
catch {
    Escrever ""
    Escrever "ERRO: $($_.Exception.Message)" "Red"
    exit 1
}
