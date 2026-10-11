<#
  Roda o sistema da loja no seu PC SEM Docker: Python + PostgreSQL instalados direto no Windows.
  Nada vai para o GitHub nem para a VPS.

  Antes (uma vez só):
    1. Python 3.12   https://www.python.org/downloads/   (marque "Add python.exe to PATH")
    2. PostgreSQL 16 https://www.enterprisedb.com/downloads/postgres-postgresql-downloads
       (anote a senha do usuário "postgres" que o instalador pede; porta 5432)

  Uso, dentro da pasta do projeto (ou pelo sem-docker.cmd):
    .\sem-docker.ps1            prepara tudo na primeira vez e liga o sistema (feche a janela ou Ctrl+C para desligar)
    .\sem-docker.ps1 usuario    cria um funcionário, para testar o que cada cargo vê
    .\sem-docker.ps1 parar      desliga a fila de avisos, se ficou ligada
    .\sem-docker.ps1 zerar      apaga o banco "loja" deste PC e começa do zero
#>
param(
    [ValidateSet("subir", "usuario", "parar", "zerar")]
    [string]$Acao = "subir"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $utf8
try { [Console]::OutputEncoding = $utf8 } catch { }

$EnvArq   = Join-Path $PSScriptRoot ".env.semdocker"
$Venv     = Join-Path $PSScriptRoot ".venv"
$PidArq   = Join-Path $PSScriptRoot ".env.semdocker.avisos"
$Fotos    = Join-Path $PSScriptRoot "dados-local\fotos"
$Banco    = "loja"
$Papeis   = @("ATENDENTE", "COSTUREIRA", "AJUDANTE_COSTURA", "GERENTE", "LIMPEZA",
              "DONO", "RH", "FINANCEIRO", "DESENVOLVEDOR", "MASTER")
$script:PgBin = $null

# --------------------------------------------------------------------------- #
function Escrever([string]$texto, [string]$cor = "Cyan") { Write-Host $texto -ForegroundColor $cor }

# Roda um programa sem derrubar o script por causa de texto no stderr (PowerShell 5.1) e devolve saída + código
function Rodar([string]$exe, [string[]]$argumentos, [string]$entrada = $null) {
    $antes = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($null -ne $entrada) { $saida = $entrada | & $exe @argumentos 2>&1 }
        else { $saida = & $exe @argumentos 2>&1 }
        return @{ Codigo = $LASTEXITCODE; Texto = (($saida | ForEach-Object { "$_" }) -join "`n") }
    } finally { $ErrorActionPreference = $antes }
}

function Novo-Segredo {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes); $rng.Dispose()
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
    $linhas = @("# Gerado por sem-docker.ps1. Só para testes no seu PC: NUNCA use estes valores na VPS.")
    foreach ($k in @("DB_OWNER_PASSWORD", "APP_DB_PASSWORD", "SESSION_SECRET", "DADOS_CHAVE", "LOJA_ID", "DONA_LOGIN",
                     "PORTA_LOCAL", "PG_PORTA")) {
        $linhas += "$k=$($h[$k])"
    }
    [System.IO.File]::WriteAllLines($EnvArq, [string[]]$linhas, $utf8)
}

function Garantir-Env {
    $h = Ler-Env
    $mudou = $false
    # Segredos só em hexadecimal: entram no SQL e na URL do banco sem precisar de escape
    foreach ($k in @("DB_OWNER_PASSWORD", "APP_DB_PASSWORD", "SESSION_SECRET", "DADOS_CHAVE")) {
        if ($h[$k] -notmatch '^[0-9a-f]{64}$') { $h[$k] = Novo-Segredo; $mudou = $true }
    }
    if (-not $h["PORTA_LOCAL"]) { $h["PORTA_LOCAL"] = "8000"; $mudou = $true }
    if (-not $h["PG_PORTA"])    { $h["PG_PORTA"] = "5432"; $mudou = $true }
    if ($mudou) { Gravar-Env $h; Escrever "Senhas internas geradas em .env.semdocker" "DarkGray" }
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
        if ($p1 -ne $p2) { Escrever "As senhas não conferem. Tente de novo." "Yellow"; continue }
        if ($p1.Length -lt 8 -or $p1.Length -gt 32) { Escrever "A senha precisa ter de 8 a 32 caracteres." "Yellow"; continue }
        if ($p1 -cnotmatch '[A-Z]' -or $p1 -cnotmatch '[a-z]' -or $p1 -notmatch '\d' -or $p1 -notmatch '[^A-Za-z0-9\s]') {
            Escrever "Falta maiúscula, minúscula, número ou símbolo." "Yellow"; continue
        }
        return $p1
    }
}

# --------------------------------------------------------------------------- #
# Programas: PostgreSQL e Python
# --------------------------------------------------------------------------- #
function Achar-Postgres {
    $psql = Get-Command psql -ErrorAction SilentlyContinue
    if ($psql) { $script:PgBin = Split-Path -Parent $psql.Source }
    else {
        $pastas = @()
        foreach ($raiz in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
            if ($raiz -and (Test-Path -LiteralPath (Join-Path $raiz "PostgreSQL"))) {
                $pastas += Get-ChildItem -LiteralPath (Join-Path $raiz "PostgreSQL") -Directory |
                           Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "bin\psql.exe") }
            }
        }
        $maior = $pastas | Sort-Object { [int]($_.Name -replace '\D', '') } -Descending | Select-Object -First 1
        if (-not $maior) {
            throw "PostgreSQL não encontrado. Instale o PostgreSQL 16: https://www.enterprisedb.com/downloads/postgres-postgresql-downloads"
        }
        $script:PgBin = Join-Path $maior.FullName "bin"
    }
    $versao = (Rodar (Psql-Exe) @("--version")).Texto
    if ($versao -notmatch '(\d+)\.') { throw "Não consegui ler a versão do PostgreSQL ($versao)." }
    if ([int]$Matches[1] -lt 16) { throw "Seu PostgreSQL é o $($Matches[1]). O sistema precisa do 16 ou mais novo." }
}

function Psql-Exe {
    foreach ($nome in @("psql.exe", "psql")) {
        $c = Join-Path $script:PgBin $nome
        if (Test-Path -LiteralPath $c) { return $c }
    }
    throw "psql não encontrado em $script:PgBin"
}

function Achar-Python {
    # Prefere o 3.12 (o mesmo da VPS); aceita 3.11 ou mais novo
    $candidatos = @(@("py", "-3.12"), @("py", "-3"), @("python"), @("python3"))
    foreach ($c in $candidatos) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $extra = @($c | Select-Object -Skip 1)
        $r = Rodar $c[0] ($extra + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])"))
        if ($r.Codigo -eq 0 -and $r.Texto -match '^(\d+)\.(\d+)') {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) { return $c }
        }
    }
    throw "Python 3.11 ou mais novo não encontrado. Instale o Python 3.12 (marque 'Add python.exe to PATH'): https://www.python.org/downloads/"
}

function Python-Venv {
    foreach ($p in @("Scripts\python.exe", "bin/python")) {
        $c = Join-Path $Venv $p
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Preparar-Python {
    $py = Python-Venv
    if (-not $py) {
        $base = Achar-Python
        Escrever "Criando o ambiente do Python em .venv..."
        $extra = @($base | Select-Object -Skip 1)
        $r = Rodar $base[0] ($extra + @("-m", "venv", $Venv))
        if ($r.Codigo -ne 0) { throw "Não consegui criar o .venv: $($r.Texto)" }
        $py = Python-Venv
    }
    $req = Join-Path $PSScriptRoot "app\requirements.txt"
    $marca = Join-Path $Venv ".instalado"
    $hash = (Get-FileHash -LiteralPath $req -Algorithm SHA256).Hash
    if ((Test-Path -LiteralPath $marca) -and ((Get-Content -LiteralPath $marca -Raw).Trim() -eq $hash)) { return $py }
    Escrever "Instalando as bibliotecas do sistema (na primeira vez leva alguns minutos)..."
    # --only-binary: igual à VPS, nenhum pacote roda código de instalação no seu PC
    $lock = Join-Path $PSScriptRoot "app\requirements.lock"
    if (Test-Path -LiteralPath $lock) { $args2 = @("-m", "pip", "install", "--only-binary=:all:", "--require-hashes", "-r", $lock) }
    else { $args2 = @("-m", "pip", "install", "--only-binary=:all:", "-r", $req) }
    & $py -m pip install --upgrade pip --quiet
    & $py @args2
    if ($LASTEXITCODE -ne 0) { throw "A instalação das bibliotecas falhou (veja acima). Confira a internet e se o Python é o 3.12." }
    [System.IO.File]::WriteAllText($marca, $hash, $utf8)
    return $py
}

# --------------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------------- #
function Psql-Como([string]$usuario, [string]$senha, [string]$porta, [string]$banco, [string[]]$extra, [string]$entrada = $null) {
    $env:PGPASSWORD = $senha
    $env:PGCLIENTENCODING = "UTF8"             # os arquivos .sql são UTF-8 (acentos das mensagens)
    $env:PGOPTIONS = "-c client_min_messages=warning"
    try {
        return Rodar (Psql-Exe) (@("-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", "localhost", "-p", $porta,
                                   "-U", $usuario, "-d", $banco) + $extra) $entrada
    } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}

function Dono-Conecta([hashtable]$h) {
    return (Psql-Como "loja_owner" $h["DB_OWNER_PASSWORD"] $h["PG_PORTA"] $Banco @("-c", "SELECT 1")).Codigo -eq 0
}

function Pedir-Senha-Postgres([hashtable]$h) {
    Escrever ""
    Escrever "Preciso da senha do usuário 'postgres' (a que você criou ao instalar o PostgreSQL)." "Yellow"
    Escrever "Ela é usada só agora, para criar o banco da loja. Não fica guardada." "DarkGray"
    for ($i = 0; $i -lt 3; $i++) {
        $s = Texto-Seguro (Read-Host "Senha do postgres (não aparece na tela)" -AsSecureString)
        $r = Psql-Como "postgres" $s $h["PG_PORTA"] "postgres" @("-c", "SELECT 1")
        if ($r.Codigo -eq 0) { return $s }
        if ($r.Texto -match 'Connection refused|could not connect|[Rr]ecusad|não pôde conectar|No such host') {
            throw "O PostgreSQL não está ligado na porta $($h['PG_PORTA']). Abra 'Serviços' do Windows e inicie 'postgresql-x64-16'."
        }
        Escrever "Senha não confere. Tente de novo." "Yellow"
    }
    throw "Senha do postgres não confere. Se esqueceu, reinstale o PostgreSQL ou troque a senha pelo pgAdmin."
}

# Primeira vez: cria os dois usuários do banco e o banco "loja", como o container faz na VPS.
# Se já existem (ex.: .env.semdocker apagado), só realinha as senhas com o arquivo.
function Preparar-Banco([hashtable]$h) {
    if (Dono-Conecta $h) { return }
    $pg = Pedir-Senha-Postgres $h
    $porta = $h["PG_PORTA"]
    $sqlPapeis = @"
DO `$`$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'loja_owner') THEN
    CREATE ROLE loja_owner LOGIN SUPERUSER PASSWORD '$($h["DB_OWNER_PASSWORD"])';
  ELSE
    ALTER ROLE loja_owner LOGIN SUPERUSER PASSWORD '$($h["DB_OWNER_PASSWORD"])';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$($h["APP_DB_PASSWORD"])';
  ELSE
    ALTER ROLE app_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$($h["APP_DB_PASSWORD"])';
  END IF;
END `$`$;
"@
    $r = Psql-Como "postgres" $pg $porta "postgres" @() $sqlPapeis
    if ($r.Codigo -ne 0) { throw "Não consegui criar os usuários do banco: $($r.Texto)" }
    $existe = (Psql-Como "postgres" $pg $porta "postgres" @("-c", "SELECT 1 FROM pg_database WHERE datname = '$Banco'")).Texto.Trim()
    if ($existe -ne "1") {
        Escrever "Criando o banco '$Banco'..."
        $r = Psql-Como "postgres" $pg $porta "postgres" @("-c", "CREATE DATABASE $Banco OWNER loja_owner ENCODING 'UTF8' TEMPLATE template0")
        if ($r.Codigo -ne 0) { throw "Não consegui criar o banco: $($r.Texto)" }
    }
    $sqlBanco = @"
ALTER DATABASE $Banco SET timezone TO 'America/Sao_Paulo';
REVOKE ALL ON DATABASE $Banco FROM PUBLIC;
GRANT CONNECT ON DATABASE $Banco TO app_user;
"@
    $r = Psql-Como "postgres" $pg $porta "postgres" @() $sqlBanco
    if ($r.Codigo -ne 0) { throw "Não consegui ajustar o banco: $($r.Texto)" }
    if (-not (Dono-Conecta $h)) { throw "O banco foi criado, mas o usuário da loja não conecta. Confira o pg_hba.conf (localhost com scram-sha-256)." }
    Escrever "Banco da loja pronto." "Green"
}

# Mesmo processo do scripts/migrate.sh: cada arquivo numa transação só, junto com o registro da versão
function Aplicar-Migracoes([hashtable]$h) {
    Escrever "Aplicando as migrações..."
    $senha, $porta = $h["DB_OWNER_PASSWORD"], $h["PG_PORTA"]
    $r = Psql-Como "loja_owner" $senha $porta $Banco @("-c", "CREATE TABLE IF NOT EXISTS schema_migrations (versao TEXT PRIMARY KEY, aplicada_em TIMESTAMPTZ NOT NULL DEFAULT now())")
    if ($r.Codigo -ne 0) { throw "Falha ao preparar as migrações: $($r.Texto)" }
    $feitas = @((Psql-Como "loja_owner" $senha $porta $Banco @("-c", "SELECT versao FROM schema_migrations")).Texto -split "`n" |
                ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $arquivos = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot "migrations") -Filter "*.sql" |
                Where-Object { $_.Name -match '^\d{4}_[a-z0-9_]+\.sql$' } | Sort-Object Name
    $novas = 0
    foreach ($f in $arquivos) {
        $v = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        if ($feitas -contains $v) { continue }
        Write-Host "  > $v"
        $r = Psql-Como "loja_owner" $senha $porta $Banco @("-1", "-f", $f.FullName, "-c", "INSERT INTO schema_migrations (versao) VALUES ('$v')")
        if ($r.Codigo -ne 0) { throw "A migração $v falhou (nada dela foi gravado):`n$($r.Texto)" }
        $novas++
    }
    if ($novas -eq 0) { Escrever "  banco já estava em dia" "DarkGray" }
}

# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
function Ambiente-App([hashtable]$h) {
    New-Item -ItemType Directory -Force -Path $Fotos | Out-Null
    $env:DATABASE_URL   = "postgresql://app_user:$($h['APP_DB_PASSWORD'])@localhost:$($h['PG_PORTA'])/$Banco"
    $env:SESSION_SECRET = $h["SESSION_SECRET"]
    $env:DADOS_CHAVE    = $h["DADOS_CHAVE"]
    $env:LOJA_ID        = $h["LOJA_ID"]
    $env:COOKIE_SECURE  = "0"                   # http://localhost (sem HTTPS). Na VPS é sempre 1.
    $env:FOTOS_DIR      = $Fotos
    $env:TZ             = "America/Sao_Paulo"
    $env:PYTHONUTF8     = "1"
    $env:PYTHONUNBUFFERED = "1"
}

# Roda a CLI do app mandando a senha pela entrada padrão (nunca pela linha de comando)
function Cli-Com-Senha([string]$py, [hashtable]$h, [string]$senha, [string[]]$argumentos) {
    Ambiente-App $h
    Push-Location (Join-Path $PSScriptRoot "app")
    try { $r = Rodar $py (@("-m", "app.cli") + $argumentos + @("--senha-stdin")) $senha }
    finally { Pop-Location }
    Write-Host $r.Texto
    if ($r.Codigo -ne 0) { throw "O banco recusou o cadastro (veja a mensagem acima)." }
    return $r.Texto
}

function Consultar([hashtable]$h, [string]$consulta) {
    $r = Psql-Como "loja_owner" $h["DB_OWNER_PASSWORD"] $h["PG_PORTA"] $Banco @("-c", $consulta)
    if ($r.Codigo -ne 0) { throw "Falha ao consultar o banco: $($r.Texto)" }
    return @($r.Texto -split "`n" | Where-Object { $_ })
}

function Escolher-Ou-Criar-Loja([string]$py, [hashtable]$h) {
    $lojas = @(Consultar $h "SELECT l.id || '|' || l.nome || '|' || COALESCE((SELECT u.login FROM usuarios u JOIN usuario_papeis up ON up.usuario_id = u.id JOIN papeis p ON p.id = up.papel_id WHERE u.loja_id = l.id AND p.codigo = 'DONO' ORDER BY u.criado_em LIMIT 1), '') FROM lojas l ORDER BY l.criado_em")
    if ($lojas.Count -gt 0) {
        Escrever ""
        Escrever "Encontrei loja(s) já cadastrada(s) neste banco:" "Green"
        for ($i = 0; $i -lt $lojas.Count; $i++) {
            $p = $lojas[$i].Split("|"); Write-Host ("  {0}. {1}  (login da Dona: {2})" -f ($i + 1), $p[1], $p[2])
        }
        $r = Perguntar "Número da loja para usar, ou N para criar uma nova" "1"
        if ($r -notmatch '^[nN]') {
            $n = 0
            if (-not [int]::TryParse($r, [ref]$n) -or $n -lt 1 -or $n -gt $lojas.Count) { throw "Opção inválida." }
            $p = $lojas[$n - 1].Split("|")
            $h["LOJA_ID"] = $p[0]; $h["DONA_LOGIN"] = $p[2]; Gravar-Env $h
            return
        }
    }
    Escrever ""
    Escrever "== Primeira vez: vamos criar a loja e a conta da Dona ==" "Green"
    $nomeLoja = Perguntar "Nome da loja" "Loja de Festa (teste)"
    $nomeDona = Perguntar "Nome da Dona" "Maria"
    $login    = (Perguntar "Login da Dona" "dona").ToLower()
    $senha    = Pedir-Senha $nomeDona
    $texto = Cli-Com-Senha $py $h $senha @("criar-loja", "--nome", $nomeLoja, "--login", $login, "--nome-dona", $nomeDona)
    if ($texto -notmatch 'LOJA_ID=([0-9a-f-]{36})') { throw "Não encontrei o LOJA_ID na resposta." }
    $h["LOJA_ID"] = $Matches[1]; $h["DONA_LOGIN"] = $login; Gravar-Env $h
}

function Parar-Avisos {
    if (-not (Test-Path -LiteralPath $PidArq)) { return }
    $id = (Get-Content -LiteralPath $PidArq -Raw).Trim()
    if ($id -match '^\d+$') {
        $proc = Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue
        if ($proc) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item -LiteralPath $PidArq -Force -ErrorAction SilentlyContinue
}

function Porta-Livre([int]$porta) {
    try { $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $porta); $l.Start(); $l.Stop(); return $true }
    catch { return $false }
}

function Acao-Subir {
    Achar-Postgres
    $h = Garantir-Env
    $py = Preparar-Python
    Preparar-Banco $h
    Aplicar-Migracoes $h
    if (-not $h["LOJA_ID"]) { Escolher-Ou-Criar-Loja $py $h; $h = Ler-Env }
    $porta = [int]$h["PORTA_LOCAL"]
    if (-not (Porta-Livre $porta)) {
        throw "A porta $porta já está em uso (outro programa ou o sistema já ligado). Troque PORTA_LOCAL no .env.semdocker (ex.: 8080)."
    }
    Ambiente-App $h
    $pastaApp = Join-Path $PSScriptRoot "app"

    # Fila de WhatsApp/e-mail e lembretes numa janela minimizada (só envia se estiver configurado no sistema)
    Parar-Avisos
    $opcoes = @{ FilePath = $py; ArgumentList = @("-m", "app.trabalhador"); WorkingDirectory = $pastaApp; PassThru = $true }
    if (-not ($IsLinux -or $IsMacOS)) { $opcoes["WindowStyle"] = "Minimized" }     # $IsLinux só existe no PowerShell 7
    $avisos = Start-Process @opcoes
    [System.IO.File]::WriteAllText($PidArq, "$($avisos.Id)", $utf8)

    $url = "http://localhost:$porta"
    Escrever ""
    Escrever "Pronto! Abra $url" "Green"
    Escrever "Entre com o login '$($h['DONA_LOGIN'])' e a senha que você criou."
    Escrever "Deixe esta janela aberta. Para desligar: Ctrl+C ou feche a janela." "Yellow"
    try { Start-Process $url } catch { }
    try {
        # Só o seu PC acessa (127.0.0.1). Mesmos limites da VPS contra lentidão proposital.
        & $py -m uvicorn app.main:app --app-dir $pastaApp --host 127.0.0.1 --port $porta `
            --no-proxy-headers --no-server-header --no-date-header --limit-concurrency 200 --timeout-keep-alive 5
    } finally {
        Parar-Avisos
        Escrever "Sistema desligado. Os dados continuam guardados no PostgreSQL." "Green"
    }
}

function Acao-Usuario {
    Achar-Postgres
    $h = Garantir-Env
    if (-not $h["LOJA_ID"]) { throw "Rode primeiro '.\sem-docker.ps1' para criar a loja." }
    $py = Preparar-Python
    if (-not (Dono-Conecta $h)) { throw "O banco da loja não conecta. Rode '.\sem-docker.ps1' primeiro." }
    Escrever ""
    Escrever "== Novo funcionário ==" "Green"
    $nome  = Perguntar "Nome"
    $login = (Perguntar "Login").ToLower()
    if (-not $nome -or -not $login) { throw "Nome e login são obrigatórios." }
    for ($i = 0; $i -lt $Papeis.Count; $i++) { Write-Host ("  {0}. {1}" -f ($i + 1), $Papeis[$i]) }
    $n = 0
    if (-not [int]::TryParse((Perguntar "Número do cargo" "1"), [ref]$n) -or $n -lt 1 -or $n -gt $Papeis.Count) { throw "Cargo inválido." }
    $papel = $Papeis[$n - 1]
    $ate = Perguntar "Freelance? Último dia de acesso (AAAA-MM-DD) ou Enter para fixo" ""
    $quem = Perguntar "Login de quem autoriza" $h["DONA_LOGIN"]
    $senha = Pedir-Senha $nome
    $argumentos = @("criar-usuario", "--login", $login, "--nome", $nome, "--papel", $papel, "--concedido-por", $quem)
    if ($ate) { $argumentos += @("--ate", $ate) }
    Cli-Com-Senha $py $h $senha $argumentos | Out-Null
    Escrever "Entre em http://localhost:$($h['PORTA_LOCAL']) com o login '$login'." "Green"
}

function Acao-Zerar {
    Escrever "Isto apaga o banco '$Banco' deste PC (loja, usuários e todos os dados de teste) e as fotos locais." "Yellow"
    if ((Read-Host "Digite ZERAR para confirmar") -cne "ZERAR") { Escrever "Nada foi apagado."; return }
    Achar-Postgres
    $h = Garantir-Env
    Parar-Avisos
    $pg = Pedir-Senha-Postgres $h
    $r = Psql-Como "postgres" $pg $h["PG_PORTA"] "postgres" @("-c", "DROP DATABASE IF EXISTS $Banco WITH (FORCE)")
    if ($r.Codigo -ne 0) { throw "Não consegui apagar o banco: $($r.Texto)" }
    Psql-Como "postgres" $pg $h["PG_PORTA"] "postgres" @("-c", "DROP ROLE IF EXISTS app_user") | Out-Null
    Psql-Como "postgres" $pg $h["PG_PORTA"] "postgres" @("-c", "DROP ROLE IF EXISTS loja_owner") | Out-Null
    Remove-Item -LiteralPath $EnvArq -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot "dados-local") -Recurse -Force -ErrorAction SilentlyContinue
    Escrever "Zerado. Rode .\sem-docker.ps1 para começar de novo." "Green"
}

# --------------------------------------------------------------------------- #
try {
    switch ($Acao) {
        "subir"   { Acao-Subir }
        "usuario" { Acao-Usuario }
        "parar"   { Parar-Avisos; Escrever "Fila de avisos desligada." "Green" }
        "zerar"   { Acao-Zerar }
    }
}
catch {
    Escrever ""
    Escrever "ERRO: $($_.Exception.Message)" "Red"
    exit 1
}
