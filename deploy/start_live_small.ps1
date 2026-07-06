param(
    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

function Get-ResolvedEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    foreach ($scope in @("Process", "User", "Machine")) {
        $value = [Environment]::GetEnvironmentVariable($Name, $scope)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $null
}

function Assert-RequiredEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $value = Get-ResolvedEnvironmentValue -Name $Name
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Variavel obrigatoria ausente: $Name"
    }
}

Assert-RequiredEnvironmentValue -Name "BINANCE_API_KEY"
Assert-RequiredEnvironmentValue -Name "BINANCE_SECRET_KEY"

$env:TESTNET = "false"
$env:ENABLE_LIVE_EXECUTION = "true"
$env:LIVE_TRADING_CONFIRMATION = "EU_ASSUMO_RISCO"
$env:LEVERAGE = "10"
$env:RISK_PER_TRADE_PCT = "2.0"
$env:MAX_REAL_RISK_PER_TRADE_PCT_START = "2.0"
$env:POSITION_SIZING_MODE = "hybrid"
$env:POSITION_MARGIN_ALLOCATION_PCT = "100.0"
$env:ENFORCE_LIVE_RISK_CAPPED_ALLOCATION = "true"
$env:MIN_LIVE_ACCOUNT_BALANCE_USDT = "20.0"
$env:REQUIRE_LIVE_TRAILING_STOP = "true"
$env:USE_ENTRY_HOUR_BLOCKS = "true"
$env:BLOCKED_SHORT_ENTRY_HOURS_UTC = "0,3,6,9,12,13,15,16,17"
$env:MAX_DAILY_REAL_LOSS_PCT = "2.0"
$env:MAX_CONSECUTIVE_REAL_LOSSES = "3"
$env:MAX_OPEN_TRADES = "1"
$env:MAX_OPEN_REAL_TRADES = "1"
$env:RUNTIME_REQUIRE_APPROVED_SYMBOL = "true"
$env:SINGLE_USER_RUNTIME_ACCOUNT_ID = "env-primary-real"
$env:SINGLE_USER_RUNTIME_ACCOUNT_ALIAS = "Primary Live Env Account"
$env:SINGLE_USER_RUNTIME_EXCHANGE = "binanceusdm"
$env:SYMBOL = "BTC/USDT"
$env:TIMEFRAME = "15m"

$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python da venv nao encontrado em $pythonExe"
}

Write-Host "Launcher micro real preparado." -ForegroundColor Cyan
Write-Host "TESTNET=$env:TESTNET | ENABLE_LIVE_EXECUTION=$env:ENABLE_LIVE_EXECUTION | RISK_PER_TRADE_PCT=$env:RISK_PER_TRADE_PCT" -ForegroundColor Cyan

if (-not $SkipPreflight) {
    Write-Host ""
    Write-Host "Executando preflight de go-live..." -ForegroundColor Yellow
    & $pythonExe -c "import sys; import live_go_live_check as g; report = g.build_go_live_report(); g.print_go_live_report(report); sys.exit(1 if any(item['status'] == 'FAIL' for item in report['checks']) else 0)"
    if ($LASTEXITCODE -ne 0) {
        throw "Preflight falhou. O bot nao sera iniciado em conta real."
    }
}

Write-Host ""
Write-Host "Subindo bot em micro real..." -ForegroundColor Green
& $pythonExe bot_runner.py
exit $LASTEXITCODE
