param(
    [switch]$NoUi,
    [string]$AutoStartMode = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $script:ScriptDir ".."))
$script:DataDir = Join-Path $script:ProjectRoot "data"
$script:LogsDir = Join-Path $script:ProjectRoot "logs"
$script:DashboardCmd = Join-Path $script:ScriptDir "EvoCoinDashboard.cmd"
$script:DashboardUrl = "http://127.0.0.1:8080"
$script:UiWindowTitle = "Evo Coin Bot"
$script:UiBuildStamp = (Get-Item -LiteralPath $MyInvocation.MyCommand.Path).LastWriteTime.ToString("yyyy-MM-dd HH:mm")
$script:ProcessStatePath = Join-Path $script:LogsDir "trader_bot_process.json"
$script:StopSignalPath = Join-Path $script:LogsDir "trader_bot_stop.signal"
$script:BotExecutionLogPath = Join-Path $script:LogsDir "bot_execution.log"
$script:BotStdoutLogPath = Join-Path $script:LogsDir "bot_runner_stdout.log"
$script:BotStderrLogPath = Join-Path $script:LogsDir "bot_runner_stderr.log"
$script:LauncherErrorLogPath = Join-Path $script:LogsDir "desktop_launcher_error.log"
$script:CredentialStorePath = Join-Path $script:DataDir "desktop_runtime_credentials.json"
$script:UiStatePath = Join-Path $script:DataDir "desktop_ui_state.json"
$script:LastLauncherErrorSignature = ""
$script:UiHeavyRefreshRequested = $true
$script:CachedRuntimeDbState = $null
$script:CachedRuntimeDbStateFetchedAt = [DateTime]::MinValue
$script:CachedLivePreflightByMode = @{}
$script:CachedLivePreflightFetchedAt = @{}
$script:LastLivePreflightError = ""
$script:RuntimeCredentialSlots = @{
    testnet = @{
        ApiKey = ""
        ApiSecret = ""
        Source = "none"
    }
    real = @{
        ApiKey = ""
        ApiSecret = ""
        Source = "none"
    }
}
$script:UiState = [ordered]@{
    selected_symbol = "BTC/USDT"
    selected_mode = "Testnet"
}
$script:AutoStartMode = ($(if ($null -eq $AutoStartMode) { "" } else { [string]$AutoStartMode })).Trim().ToLowerInvariant()
$script:AutoStartExecuted = $false
$script:PythonExe = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $script:PythonExe)) {
    $script:PythonExe = "python"
}

if (-not (Test-Path -LiteralPath $script:LogsDir)) {
    New-Item -ItemType Directory -Path $script:LogsDir | Out-Null
}

if (-not (Test-Path -LiteralPath $script:DataDir)) {
    New-Item -ItemType Directory -Path $script:DataDir | Out-Null
}

function Write-LauncherErrorLog {
    param(
        [string]$Context,
        [string]$Message,
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    $summary = if ([string]::IsNullOrWhiteSpace([string]$Message)) {
        "Erro desconhecido."
    }
    else {
        [string]$Message
    }

    $signature = ($Context + "|" + $summary)
    if ($signature -eq $script:LastLauncherErrorSignature) {
        return
    }
    $script:LastLauncherErrorSignature = $signature

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("[" + ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")) + "] " + $Context)
    $lines.Add("Mensagem: " + $summary)

    if ($null -ne $ErrorRecord) {
        $lines.Add("Detalhe: " + ([string]$ErrorRecord))
        if ($null -ne $ErrorRecord.ScriptStackTrace) {
            $lines.Add("Stack: " + [string]$ErrorRecord.ScriptStackTrace)
        }
    }

    $lines.Add("")
    Add-Content -LiteralPath $script:LauncherErrorLogPath -Value ($lines -join [Environment]::NewLine) -Encoding UTF8
}

function Invoke-UiSafely {
    param(
        [string]$Context,
        [scriptblock]$Action,
        [switch]$ShowMessage
    )

    try {
        & $Action
    }
    catch {
        $summary = if ($null -ne $_.Exception -and -not [string]::IsNullOrWhiteSpace([string]$_.Exception.Message)) {
            [string]$_.Exception.Message
        }
        else {
            [string]$_
        }

        Write-LauncherErrorLog -Context $Context -Message $summary -ErrorRecord $_

        if (Get-Variable -Name metaStatusLabel -Scope Script -ErrorAction SilentlyContinue) {
            $script:metaStatusLabel.Text = "Erro na interface: $Context | veja logs\\desktop_launcher_error.log"
            $script:metaStatusLabel.ForeColor = [System.Drawing.Color]::DarkRed
        }

        if (Get-Variable -Name logTextBox -Scope Script -ErrorAction SilentlyContinue) {
            $existingText = [string]$script:logTextBox.Text
            if ([string]::IsNullOrWhiteSpace($existingText) -or $existingText -eq "Sem log operacional ainda.") {
                $script:logTextBox.Text = "A interface encontrou um erro em " + $Context + "." + [Environment]::NewLine + "Veja: logs\\desktop_launcher_error.log"
            }
        }

        if ($ShowMessage) {
            [System.Windows.Forms.MessageBox]::Show(
                "A interface encontrou um erro em " + $Context + "." + [Environment]::NewLine + "Detalhe: " + $summary + [Environment]::NewLine + [Environment]::NewLine + "Veja o arquivo logs\\desktop_launcher_error.log para mais detalhes.",
                "Evo Coin Bot",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
        }
    }
}

function Test-DashboardOnline {
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri $script:DashboardUrl -TimeoutSec 2
        return $true
    }
    catch {
        return $false
    }
}

function Get-EnvironmentVariableValue {
    param(
        [string]$Name
    )

    foreach ($scope in @($null, "User", "Machine")) {
        try {
            if ($null -eq $scope) {
                $value = [Environment]::GetEnvironmentVariable($Name)
            }
            else {
                $value = [Environment]::GetEnvironmentVariable($Name, $scope)
            }
        }
        catch {
            $value = $null
        }

        if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
            return [string]$value
        }
    }

    return ""
}

function Resolve-CredentialSlot {
    param(
        [bool]$UseTestnet
    )

    if ($UseTestnet) {
        return "testnet"
    }
    return "real"
}

function ConvertTo-PowerShellQuotedString {
    param(
        [string]$Value
    )

    $safeValue = if ($null -eq $Value) { "" } else { [string]$Value }
    return "'" + ($safeValue -replace "'", "''") + "'"
}

function Protect-LauncherSecret {
    param(
        [string]$Value
    )

    $rawValue = ($(if ($null -eq $Value) { "" } else { [string]$Value })).Trim()
    if ([string]::IsNullOrWhiteSpace($rawValue)) {
        return ""
    }

    $secureValue = ConvertTo-SecureString -String $rawValue -AsPlainText -Force
    return ConvertFrom-SecureString -SecureString $secureValue
}

function Unprotect-LauncherSecret {
    param(
        [string]$ProtectedValue
    )

    $rawValue = ($(if ($null -eq $ProtectedValue) { "" } else { [string]$ProtectedValue })).Trim()
    if ([string]::IsNullOrWhiteSpace($rawValue)) {
        return ""
    }

    try {
        $secureValue = ConvertTo-SecureString -String $rawValue
        return [System.Net.NetworkCredential]::new("", $secureValue).Password
    }
    catch {
        return ""
    }
}

function Read-LauncherCredentialStore {
    $emptyStore = [ordered]@{
        testnet = [ordered]@{
            api_key = ""
            api_secret = ""
        }
        real = [ordered]@{
            api_key = ""
            api_secret = ""
        }
    }

    if (-not (Test-Path -LiteralPath $script:CredentialStorePath)) {
        return $emptyStore
    }

    try {
        $payload = Get-Content -LiteralPath $script:CredentialStorePath -Raw | ConvertFrom-Json
        foreach ($slot in @("testnet", "real")) {
            $slotData = $payload.$slot
            if ($null -eq $slotData) {
                continue
            }
            $emptyStore[$slot]["api_key"] = [string]($(if ($null -ne $slotData.api_key) { $slotData.api_key } else { "" }))
            $emptyStore[$slot]["api_secret"] = [string]($(if ($null -ne $slotData.api_secret) { $slotData.api_secret } else { "" }))
        }
    }
    catch {
        return $emptyStore
    }

    return $emptyStore
}

function Write-LauncherCredentialStore {
    param(
        [hashtable]$Store
    )

    $payload = [ordered]@{
        testnet = [ordered]@{
            api_key = [string]($(if ($null -ne $Store["testnet"] -and $null -ne $Store["testnet"]["api_key"]) { $Store["testnet"]["api_key"] } else { "" }))
            api_secret = [string]($(if ($null -ne $Store["testnet"] -and $null -ne $Store["testnet"]["api_secret"]) { $Store["testnet"]["api_secret"] } else { "" }))
        }
        real = [ordered]@{
            api_key = [string]($(if ($null -ne $Store["real"] -and $null -ne $Store["real"]["api_key"]) { $Store["real"]["api_key"] } else { "" }))
            api_secret = [string]($(if ($null -ne $Store["real"] -and $null -ne $Store["real"]["api_secret"]) { $Store["real"]["api_secret"] } else { "" }))
        }
    }

    try {
        $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:CredentialStorePath -Encoding UTF8
    }
    catch {
    }
}

function Read-LauncherUiState {
    $defaultState = [ordered]@{
        selected_symbol = "BTC/USDT"
        selected_mode = "Testnet"
    }

    if (-not (Test-Path -LiteralPath $script:UiStatePath)) {
        return $defaultState
    }

    try {
        $payload = Get-Content -LiteralPath $script:UiStatePath -Raw | ConvertFrom-Json
        $rawSymbol = [string]($(if ($null -ne $payload.selected_symbol) { $payload.selected_symbol } else { $defaultState.selected_symbol }))
        $rawMode = [string]($(if ($null -ne $payload.selected_mode) { $payload.selected_mode } else { $defaultState.selected_mode }))
        if ($rawSymbol -in @("BTC/USDT", "XLM/USDT")) {
            $defaultState.selected_symbol = $rawSymbol
        }
        if ($rawMode -in @("Testnet", "Conta Real")) {
            $defaultState.selected_mode = $rawMode
        }
    }
    catch {
        return $defaultState
    }

    return $defaultState
}

function Write-LauncherUiState {
    param(
        [hashtable]$State
    )

    $payload = [ordered]@{
        selected_symbol = [string]($(if ($null -ne $State["selected_symbol"]) { $State["selected_symbol"] } else { "BTC/USDT" }))
        selected_mode = [string]($(if ($null -ne $State["selected_mode"]) { $State["selected_mode"] } else { "Testnet" }))
    }

    try {
        $payload | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $script:UiStatePath -Encoding UTF8
    }
    catch {
    }
}

function Initialize-LauncherUiState {
    $script:UiState = Read-LauncherUiState
}

function Save-LauncherUiSelection {
    $resolvedSymbol = "BTC/USDT"
    $resolvedMode = "Testnet"

    if ($null -ne $script:symbolComboBox -and $null -ne $script:symbolComboBox.SelectedItem) {
        $selectedSymbol = [string]$script:symbolComboBox.SelectedItem
        if (-not [string]::IsNullOrWhiteSpace($selectedSymbol)) {
            $resolvedSymbol = $selectedSymbol
        }
    }

    if ($null -ne $script:modeComboBox -and $null -ne $script:modeComboBox.SelectedItem) {
        $selectedMode = [string]$script:modeComboBox.SelectedItem
        if ($selectedMode -in @("Testnet", "Conta Real")) {
            $resolvedMode = $selectedMode
        }
    }

    $script:UiState = [ordered]@{
        selected_symbol = $resolvedSymbol
        selected_mode = $resolvedMode
    }
    Write-LauncherUiState -State $script:UiState
}

function Get-LaunchEnvironmentOverrides {
    param(
        [bool]$UseTestnet,
        [string]$Symbol = "BTC/USDT"
    )

    $accountId = if ($UseTestnet) { "env-primary" } else { "env-primary-real" }
    $accountAlias = if ($UseTestnet) { "Primary Env Account" } else { "Primary Live Env Account" }
    $resolvedSymbol = if ([string]::IsNullOrWhiteSpace([string]$Symbol)) { "BTC/USDT" } else { [string]$Symbol }
    $resolvedTimeframe = Get-SymbolPreferredTimeframe -Symbol $resolvedSymbol

    return [ordered]@{
        TESTNET = if ($UseTestnet) { "true" } else { "false" }
        ENABLE_LIVE_EXECUTION = if ($UseTestnet) { "false" } else { "true" }
        LIVE_TRADING_CONFIRMATION = if ($UseTestnet) { "" } else { "EU_ASSUMO_RISCO" }
        LEVERAGE = "10"
        RISK_PER_TRADE_PCT = "2.0"
        MAX_REAL_RISK_PER_TRADE_PCT_START = "2.0"
        POSITION_SIZING_MODE = "allocation"
        POSITION_MARGIN_ALLOCATION_PCT = "100.0"
        MAX_DAILY_REAL_LOSS_PCT = "2.0"
        MAX_CONSECUTIVE_REAL_LOSSES = "3"
        MAX_OPEN_TRADES = "1"
        MAX_OPEN_REAL_TRADES = "1"
        RUNTIME_REQUIRE_APPROVED_SYMBOL = "true"
        SINGLE_USER_RUNTIME_ACCOUNT_ID = $accountId
        SINGLE_USER_RUNTIME_ACCOUNT_ALIAS = $accountAlias
        SINGLE_USER_RUNTIME_EXCHANGE = "binanceusdm"
        SYMBOL = $resolvedSymbol
        TIMEFRAME = $resolvedTimeframe
    }
}

function Get-CurrentLaunchProfileSummary {
    param(
        [bool]$UseTestnet,
        [string]$Symbol = ""
    )

    $overrides = Get-LaunchEnvironmentOverrides -UseTestnet $UseTestnet -Symbol $Symbol
    $leverage = [string]$overrides.LEVERAGE
    $sizingMode = [string]$overrides.POSITION_SIZING_MODE
    $allocationPct = [string]$overrides.POSITION_MARGIN_ALLOCATION_PCT
    $riskPct = [string]$overrides.RISK_PER_TRADE_PCT
    $dailyLossPct = [string]$overrides.MAX_DAILY_REAL_LOSS_PCT
    $lossStreak = [string]$overrides.MAX_CONSECUTIVE_REAL_LOSSES

    return "Perfil atual: ${leverage}x, modo $sizingMode, ${allocationPct}% da banca, risco alvo ${riskPct}% por trade, limite diario ${dailyLossPct}% e trava apos ${lossStreak} losses."
}

function Get-SelectedBotSymbol {
    if ($null -ne $script:symbolComboBox -and $null -ne $script:symbolComboBox.SelectedItem) {
        $selected = [string]$script:symbolComboBox.SelectedItem
        if (-not [string]::IsNullOrWhiteSpace($selected)) {
            return $selected
        }
    }

    return "BTC/USDT"
}

function Get-SymbolPreferredTimeframe {
    param(
        [string]$Symbol
    )

    $resolvedSymbol = if ([string]::IsNullOrWhiteSpace([string]$Symbol)) { "BTC/USDT" } else { [string]$Symbol }
    $defaultTimeframe = "15m"
    $overridesPath = Join-Path $script:ProjectRoot "reports\\validation\\symbol_strategy_overrides.json"

    if (-not (Test-Path -LiteralPath $overridesPath)) {
        return $defaultTimeframe
    }

    try {
        $payload = Get-Content -LiteralPath $overridesPath -Raw | ConvertFrom-Json
        $symbols = $payload.symbols
        if ($null -eq $symbols) {
            return $defaultTimeframe
        }

        $record = $symbols.PSObject.Properties[$resolvedSymbol]
        if ($null -eq $record) {
            return $defaultTimeframe
        }

        $timeframe = [string]$record.Value.recommended_timeframe
        if ([string]::IsNullOrWhiteSpace($timeframe)) {
            $timeframe = [string]$record.Value.timeframe
        }

        if ([string]::IsNullOrWhiteSpace($timeframe)) {
            return $defaultTimeframe
        }

        return $timeframe.Trim().ToLowerInvariant()
    }
    catch {
        return $defaultTimeframe
    }
}

function Get-CredentialEnvNames {
    param(
        [bool]$UseTestnet
    )

    if ($UseTestnet) {
        return [pscustomobject]@{
            ApiKey = "BINANCE_TESTNET_API_KEY"
            ApiSecret = "BINANCE_TESTNET_SECRET_KEY"
        }
    }

    return [pscustomobject]@{
        ApiKey = "BINANCE_API_KEY"
        ApiSecret = "BINANCE_SECRET_KEY"
    }
}

function Mask-CredentialValue {
    param(
        [string]$Value
    )

    $rawValue = if ($null -eq $Value) { "" } else { [string]$Value }
    if ([string]::IsNullOrWhiteSpace($rawValue)) {
        return "-"
    }
    if ($rawValue.Length -le 8) {
        return ($rawValue.Substring(0, [Math]::Min(2, $rawValue.Length)) + "***" + $rawValue.Substring($rawValue.Length - 1))
    }
    return ($rawValue.Substring(0, 4) + "***" + $rawValue.Substring($rawValue.Length - 4))
}

function Save-RuntimeCredentials {
    param(
        [bool]$UseTestnet,
        [string]$ApiKey,
        [string]$ApiSecret
    )

    $slot = Resolve-CredentialSlot -UseTestnet $UseTestnet
    $script:RuntimeCredentialSlots[$slot] = @{
        ApiKey = ($(if ($null -eq $ApiKey) { "" } else { [string]$ApiKey })).Trim()
        ApiSecret = ($(if ($null -eq $ApiSecret) { "" } else { [string]$ApiSecret })).Trim()
        Source = "interface_saved"
    }

    $store = Read-LauncherCredentialStore
    $store[$slot] = @{
        api_key = Protect-LauncherSecret -Value $script:RuntimeCredentialSlots[$slot].ApiKey
        api_secret = Protect-LauncherSecret -Value $script:RuntimeCredentialSlots[$slot].ApiSecret
    }
    Write-LauncherCredentialStore -Store $store
}

function Clear-RuntimeCredentials {
    param(
        [bool]$UseTestnet
    )

    $slot = Resolve-CredentialSlot -UseTestnet $UseTestnet
    $script:RuntimeCredentialSlots[$slot] = @{
        ApiKey = ""
        ApiSecret = ""
        Source = "none"
    }

    $store = Read-LauncherCredentialStore
    $store[$slot] = @{
        api_key = ""
        api_secret = ""
    }
    Write-LauncherCredentialStore -Store $store
}

function Get-RuntimeCredentialData {
    param(
        [bool]$UseTestnet
    )

    $slot = Resolve-CredentialSlot -UseTestnet $UseTestnet
    $sessionData = $script:RuntimeCredentialSlots[$slot]
    $sessionApiKey = if ($null -eq $sessionData.ApiKey) { "" } else { [string]$sessionData.ApiKey }
    $sessionApiSecret = if ($null -eq $sessionData.ApiSecret) { "" } else { [string]$sessionData.ApiSecret }

    if (-not [string]::IsNullOrWhiteSpace($sessionApiKey) -and -not [string]::IsNullOrWhiteSpace($sessionApiSecret)) {
        $source = [string]($(if ($null -ne $sessionData.Source) { $sessionData.Source } else { "session" }))
        return [pscustomobject]@{
            Slot = $slot
            ApiKey = $sessionApiKey
            ApiSecret = $sessionApiSecret
            Source = $source
            SourceLabel = if ($source -eq "interface_saved") { "Interface" } else { "Sessao" }
            ApiKeyMasked = Mask-CredentialValue -Value $sessionApiKey
        }
    }

    $envNames = Get-CredentialEnvNames -UseTestnet $UseTestnet
    $rawEnvApiKey = Get-EnvironmentVariableValue -Name $envNames.ApiKey
    $rawEnvApiSecret = Get-EnvironmentVariableValue -Name $envNames.ApiSecret
    $envApiKey = if ($null -eq $rawEnvApiKey) { "" } else { [string]$rawEnvApiKey }
    $envApiSecret = if ($null -eq $rawEnvApiSecret) { "" } else { [string]$rawEnvApiSecret }
    if (-not [string]::IsNullOrWhiteSpace($envApiKey) -and -not [string]::IsNullOrWhiteSpace($envApiSecret)) {
        return [pscustomobject]@{
            Slot = $slot
            ApiKey = $envApiKey
            ApiSecret = $envApiSecret
            Source = "env"
            SourceLabel = "Ambiente"
            ApiKeyMasked = Mask-CredentialValue -Value $envApiKey
        }
    }

    if ($UseTestnet) {
        $genericEnvApiKey = Get-EnvironmentVariableValue -Name "BINANCE_API_KEY"
        $genericEnvApiSecret = Get-EnvironmentVariableValue -Name "BINANCE_SECRET_KEY"
        if (-not [string]::IsNullOrWhiteSpace($genericEnvApiKey) -and -not [string]::IsNullOrWhiteSpace($genericEnvApiSecret)) {
            return [pscustomobject]@{
                Slot = $slot
                ApiKey = [string]$genericEnvApiKey
                ApiSecret = [string]$genericEnvApiSecret
                Source = "env_generic"
                SourceLabel = "Ambiente Genérico"
                ApiKeyMasked = Mask-CredentialValue -Value $genericEnvApiKey
            }
        }
    }

    return $null
}

function Initialize-RuntimeCredentialSlots {
    $store = Read-LauncherCredentialStore

    foreach ($slot in @("testnet", "real")) {
        $slotData = $store[$slot]
        if ($null -eq $slotData) {
            continue
        }

        $apiKey = Unprotect-LauncherSecret -ProtectedValue ([string]$slotData["api_key"])
        $apiSecret = Unprotect-LauncherSecret -ProtectedValue ([string]$slotData["api_secret"])
        if ([string]::IsNullOrWhiteSpace($apiKey) -or [string]::IsNullOrWhiteSpace($apiSecret)) {
            continue
        }

        $script:RuntimeCredentialSlots[$slot] = @{
            ApiKey = $apiKey
            ApiSecret = $apiSecret
            Source = "interface_saved"
        }
    }
}

Initialize-RuntimeCredentialSlots
Initialize-LauncherUiState

function Sync-UiCredentialInputsToPersistentSlot {
    param(
        [bool]$UseTestnet
    )

    $apiKeyValue = ""
    $apiSecretValue = ""

    if ($UseTestnet) {
        if (Get-Variable -Name testnetApiKeyTextBox -Scope Script -ErrorAction SilentlyContinue) {
            $apiKeyValue = [string]$script:testnetApiKeyTextBox.Text
        }
        if (Get-Variable -Name testnetApiSecretTextBox -Scope Script -ErrorAction SilentlyContinue) {
            $apiSecretValue = [string]$script:testnetApiSecretTextBox.Text
        }
    }
    else {
        if (Get-Variable -Name realApiKeyTextBox -Scope Script -ErrorAction SilentlyContinue) {
            $apiKeyValue = [string]$script:realApiKeyTextBox.Text
        }
        if (Get-Variable -Name realApiSecretTextBox -Scope Script -ErrorAction SilentlyContinue) {
            $apiSecretValue = [string]$script:realApiSecretTextBox.Text
        }
    }

    $apiKeyValue = $apiKeyValue.Trim()
    $apiSecretValue = $apiSecretValue.Trim()
    if ([string]::IsNullOrWhiteSpace($apiKeyValue) -or [string]::IsNullOrWhiteSpace($apiSecretValue)) {
        return $false
    }

    Save-RuntimeCredentials -UseTestnet $UseTestnet -ApiKey $apiKeyValue -ApiSecret $apiSecretValue
    return $true
}

function Request-UiHeavyRefresh {
    $script:UiHeavyRefreshRequested = $true
}

function Invoke-ProjectPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$InlineScript,

        [string[]]$Arguments = @()
    )

    Push-Location -LiteralPath $script:ProjectRoot
    $previousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $composedPythonPath = if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
            $script:ProjectRoot
        }
        else {
            $script:ProjectRoot + [IO.Path]::PathSeparator + $previousPythonPath
        }
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $composedPythonPath, "Process")
        $raw = $InlineScript | & $script:PythonExe - @Arguments 2>$stderrPath
        $stderrText = ""
        if (Test-Path -LiteralPath $stderrPath) {
            $stderrRaw = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
            if ($null -eq $stderrRaw) {
                $stderrText = ""
            }
            else {
                $stderrText = ([string]$stderrRaw).Trim()
            }
        }
        if ($LASTEXITCODE -ne 0) {
            if ([string]::IsNullOrWhiteSpace($stderrText)) {
                throw "Python retornou codigo $LASTEXITCODE."
            }
            throw $stderrText
        }
        if (-not [string]::IsNullOrWhiteSpace($stderrText)) {
            Write-LauncherErrorLog -Context "Invoke-ProjectPython.Stderr" -Message $stderrText
        }
        return $raw
    }
    finally {
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
        [Environment]::SetEnvironmentVariable("PYTHONPATH", $previousPythonPath, "Process")
        Pop-Location
    }
}

function Get-BotProcessStatePayload {
    if (-not (Test-Path -LiteralPath $script:ProcessStatePath)) {
        return $null
    }

    try {
        return (Get-Content -LiteralPath $script:ProcessStatePath -Raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Clear-BotProcessStatePayload {
    try {
        Remove-Item -LiteralPath $script:ProcessStatePath -Force -ErrorAction SilentlyContinue
    }
    catch {
    }
}

function Write-BotProcessStatePayload {
    param(
        [int]$ProcessId,
        [bool]$UseTestnet,
        [string]$Source,
        [string]$Command = "",
        [hashtable]$Extra = $null
    )

    $payload = [ordered]@{
        pid = [int]$ProcessId
        use_testnet = [bool]$UseTestnet
        mode_label = if ($UseTestnet) { "Testnet" } else { "Conta Real" }
        entrypoint = (Join-Path $script:ProjectRoot "bot_runner.py")
        source = if ([string]::IsNullOrWhiteSpace([string]$Source)) { "desktop_launcher" } else { [string]$Source }
        command = if ([string]::IsNullOrWhiteSpace([string]$Command)) { "" } else { [string]$Command }
        started_at = [DateTime]::UtcNow.ToString("o")
    }

    if ($null -ne $Extra) {
        foreach ($key in $Extra.Keys) {
            if (-not [string]::IsNullOrWhiteSpace([string]$key)) {
                $payload[[string]$key] = $Extra[$key]
            }
        }
    }

    try {
        $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:ProcessStatePath -Encoding UTF8
    }
    catch {
    }

    return [pscustomobject]$payload
}

function Get-BotRunnerProcesses {
    $projectRootNormalized = $script:ProjectRoot.ToLowerInvariant()

    $processRows = $null
    try {
        $processRows = Get-CimInstance Win32_Process -ErrorAction Stop
    }
    catch {
        try {
            $processRows = Get-WmiObject Win32_Process -ErrorAction Stop
        }
        catch {
            return @()
        }
    }

    try {
        $candidates = $processRows | Where-Object {
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            if ([string]::IsNullOrWhiteSpace($commandLine)) {
                return $false
            }

            $normalizedCommand = $commandLine.ToLowerInvariant()
            $normalizedExecutable = $executablePath.ToLowerInvariant()
            return $normalizedCommand.Contains("bot_runner.py") -and (
                $normalizedCommand.Contains($projectRootNormalized) -or
                $normalizedExecutable.Contains($projectRootNormalized)
            )
        } | ForEach-Object {
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            $source = if ($commandLine -match "desktop_launcher") {
                "desktop_launcher"
            }
            elseif ($commandLine -match "dashboard") {
                "dashboard"
            }
            else {
                "detected_process"
            }

            [pscustomobject]@{
                Pid = [int]$_.ProcessId
                Name = [string]$_.Name
                CommandLine = $commandLine
                ExecutablePath = $executablePath
                Source = $source
                SourceRank = switch ($source) {
                    "desktop_launcher" { 0 }
                    "dashboard" { 1 }
                    default { 2 }
                }
            }
        }

        return @($candidates | Sort-Object SourceRank, Pid)
    }
    catch {
        return @()
    }
}

function Find-BotRunnerProcess {
    $candidates = Get-BotRunnerProcesses
    if ($null -eq $candidates -or $candidates.Count -eq 0) {
        return $null
    }
    return $candidates | Select-Object -First 1
}

function Get-BotState {
    param(
        [switch]$ForceRuntimeRefresh
    )

    $result = [ordered]@{
        Running = $false
        Pid = $null
        ModeLabel = "-"
        Source = "-"
        StartedAt = "-"
        RuntimeEnvironment = ""
        RuntimeStatus = ""
        RuntimeHeartbeatAt = ""
        RuntimeLiveEnabled = $false
    }

    $payload = Get-BotProcessStatePayload
    if ($null -ne $payload) {
        $processId = $payload.pid
        if ($processId) {
            $process = Get-Process -Id ([int]$processId) -ErrorAction SilentlyContinue
            if ($null -ne $process) {
                $result.Running = $true
                $result.Pid = [int]$processId
            }
        }
        if ($payload.mode_label) {
            $result.ModeLabel = [string]$payload.mode_label
        }
        elseif ($payload.use_testnet -eq $true) {
            $result.ModeLabel = "Testnet"
        }
        elseif ($payload.use_testnet -eq $false) {
            $result.ModeLabel = "Conta Real"
        }
        if ($payload.source) {
            $result.Source = [string]$payload.source
        }
        if ($payload.started_at) {
            $result.StartedAt = [string]$payload.started_at
        }
    }

    if (-not $result.Running) {
        $fallbackProcess = Find-BotRunnerProcess
        if ($null -ne $fallbackProcess) {
            $result.Running = $true
            $result.Pid = [int]$fallbackProcess.Pid
            if ($result.Source -eq "-" -or [string]::IsNullOrWhiteSpace([string]$result.Source)) {
                $result.Source = [string]$fallbackProcess.Source
            }
        }
    }

    if (-not $result.Running -and $null -ne $payload) {
        Clear-BotProcessStatePayload
    }

    $runtimeState = Get-BotRuntimeDatabaseState -ForceRefresh:$ForceRuntimeRefresh
    if ($null -ne $runtimeState -and $result.Running) {
        $result.RuntimeEnvironment = [string]($(if ($null -ne $runtimeState.environment) { $runtimeState.environment } else { "" }))
        $result.RuntimeStatus = [string]($(if ($null -ne $runtimeState.status) { $runtimeState.status } else { "" }))
        $result.RuntimeHeartbeatAt = [string]($(if ($null -ne $runtimeState.last_heartbeat_at) { $runtimeState.last_heartbeat_at } else { "" }))
        $result.RuntimeLiveEnabled = [bool]$runtimeState.live_enabled

        if ($result.ModeLabel -eq "-" -or [string]::IsNullOrWhiteSpace([string]$result.ModeLabel)) {
            if ([string]$runtimeState.environment -eq "mainnet") {
                $result.ModeLabel = "Conta Real"
            }
            elseif ([string]$runtimeState.environment -eq "testnet") {
                $result.ModeLabel = "Testnet"
            }
        }
    }

    return [pscustomobject]$result
}

function Get-LogTail {
    param(
        [string]$Path,
        [int]$Lines = 60
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    try {
        $content = Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction Stop
        return ($content -join [Environment]::NewLine)
    }
    catch {
        return ""
    }
}

function Test-LaunchLogProgress {
    param(
        [bool]$UseTestnet,
        [DateTime]$LaunchStartedAtUtc
    )

    if (-not (Test-Path -LiteralPath $script:BotExecutionLogPath)) {
        return [pscustomobject]@{
            HasProgress = $false
            FeedReady = $false
            LiveLayerReady = $false
            Tail = ""
        }
    }

    try {
        $tail = Get-Content -LiteralPath $script:BotExecutionLogPath -Tail 80 -ErrorAction Stop
        $recentLines = @()
        foreach ($line in $tail) {
            $lineText = [string]$line
            if ($lineText.Length -lt 19) {
                continue
            }

            $timestampText = $lineText.Substring(0, 19)
            $lineUtc = $null
            try {
                $lineUtc = [DateTime]::ParseExact(
                    $timestampText,
                    "yyyy-MM-dd HH:mm:ss",
                    [System.Globalization.CultureInfo]::InvariantCulture,
                    [System.Globalization.DateTimeStyles]::AssumeLocal
                ).ToUniversalTime()
            }
            catch {
                continue
            }

            if ($lineUtc -ge $LaunchStartedAtUtc.AddSeconds(-3)) {
                $recentLines += $lineText
            }
        }

        $joined = $recentLines -join [Environment]::NewLine
        $feedReady = $joined -match "Feed de mercado ativo"
        $liveLayerReady = $joined -match "Camada live do runner ativa"
        $bootReady = $joined -match "Inicializando bot \| modo:"

        return [pscustomobject]@{
            HasProgress = ($bootReady -or $feedReady -or $liveLayerReady)
            FeedReady = [bool]$feedReady
            LiveLayerReady = [bool]$liveLayerReady
            Tail = $joined
        }
    }
    catch {
        return [pscustomobject]@{
            HasProgress = $false
            FeedReady = $false
            LiveLayerReady = $false
            Tail = ""
        }
    }
}

function Get-BotRuntimeDatabaseState {
    param(
        [switch]$ForceRefresh
    )

    $cacheTtlSeconds = 8
    if (
        -not $ForceRefresh -and
        $null -ne $script:CachedRuntimeDbState -and
        $script:CachedRuntimeDbStateFetchedAt -ne [DateTime]::MinValue -and
        ((Get-Date) - $script:CachedRuntimeDbStateFetchedAt).TotalSeconds -lt $cacheTtlSeconds
    ) {
        return $script:CachedRuntimeDbState
    }

    $databasePath = Join-Path $script:ProjectRoot "data\trading_bot.db"
    if (-not (Test-Path -LiteralPath $databasePath)) {
        return $null
    }

    $symbol = "BTC/USDT"
    $timeframe = "15m"
    $payload = Get-BotProcessStatePayload
    if ($null -ne $payload) {
        if ($null -ne ($payload.PSObject.Properties["symbol"]) -and -not [string]::IsNullOrWhiteSpace([string]$payload.symbol)) {
            $symbol = [string]$payload.symbol
        }
        else {
            $symbol = Get-SelectedBotSymbol
        }

        if ($null -ne ($payload.PSObject.Properties["timeframe"]) -and -not [string]::IsNullOrWhiteSpace([string]$payload.timeframe)) {
            $timeframe = [string]$payload.timeframe
        }
        else {
            $timeframe = Get-SymbolPreferredTimeframe -Symbol $symbol
        }
    }
    else {
        $symbol = Get-SelectedBotSymbol
        $timeframe = Get-SymbolPreferredTimeframe -Symbol $symbol
    }

    $runtimeKey = "primary:{0}:{1}" -f $symbol, $timeframe

    $pythonScript = @'
import json
import sqlite3
import sys

database_path = sys.argv[1]
runtime_key = sys.argv[2]

conn = sqlite3.connect(database_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute(
    """
    SELECT *
    FROM bot_runtime_state
    WHERE (? = '' OR runtime_key = ?)
    ORDER BY updated_at DESC, id DESC
    LIMIT 1
    """,
    (runtime_key, runtime_key),
)
row = cur.fetchone()
if row is None:
    print("")
    raise SystemExit(0)

state_payload = {}
try:
    state_payload = json.loads(row["state_payload"] or "{}")
except Exception:
    state_payload = {}

snapshot = state_payload.get("snapshot") or {}
runtime_key_value = str(row["runtime_key"] or "")
symbol_value = str(state_payload.get("symbol") or snapshot.get("symbol") or "").strip()
timeframe_value = str(state_payload.get("timeframe") or snapshot.get("timeframe") or "").strip()
if (not symbol_value or not timeframe_value) and runtime_key_value:
    parts = runtime_key_value.split(":")
    if len(parts) >= 3:
        if not symbol_value:
            symbol_value = ":".join(parts[1:-1]).strip()
        if not timeframe_value:
            timeframe_value = parts[-1].strip()
payload = {
    "runtime_key": row["runtime_key"],
    "symbol": symbol_value,
    "timeframe": timeframe_value,
    "environment": row["environment"],
    "status": row["status"],
    "last_heartbeat_at": row["last_heartbeat_at"],
    "last_candle_timestamp": row["last_candle_timestamp"],
    "last_signal": row["last_signal"],
    "last_signal_reason": row["last_signal_reason"],
    "position_side": row["position_side"],
    "position_entry_price": row["position_entry_price"],
    "blocked": bool(row["blocked"]),
    "block_reason": row["block_reason"],
    "last_error": row["last_error"],
    "updated_at": row["updated_at"],
    "testnet": bool(snapshot.get("testnet")),
    "live_enabled": bool(snapshot.get("live_execution_enabled")),
}
print(json.dumps(payload, ensure_ascii=True))
'@

    try {
        $raw = Invoke-ProjectPython -InlineScript $pythonScript -Arguments @($databasePath, $runtimeKey)
        if ([string]::IsNullOrWhiteSpace([string]$raw)) {
            $script:CachedRuntimeDbState = $null
            $script:CachedRuntimeDbStateFetchedAt = Get-Date
            return $null
        }
        $script:CachedRuntimeDbState = ($raw | ConvertFrom-Json)
        $script:CachedRuntimeDbStateFetchedAt = Get-Date
        return $script:CachedRuntimeDbState
    }
    catch {
        $script:CachedRuntimeDbState = $null
        $script:CachedRuntimeDbStateFetchedAt = Get-Date
        return $null
    }
}

function Get-ExpectedModeLabel {
    param(
        [bool]$UseTestnet
    )

    if ($UseTestnet) {
        return "Testnet"
    }
    return "Conta Real"
}

function Get-ExpectedRuntimeEnvironment {
    param(
        [bool]$UseTestnet
    )

    if ($UseTestnet) {
        return "testnet"
    }
    return "mainnet"
}

function Resolve-BotModeLabel {
    param(
        $BotState,
        $RuntimeState
    )

    if ($null -ne $RuntimeState) {
        if ([string]$RuntimeState.environment -eq "mainnet") {
            return "Conta Real"
        }
        if ([string]$RuntimeState.environment -eq "testnet") {
            return "Testnet"
        }
    }

    if ($null -ne $BotState -and -not [string]::IsNullOrWhiteSpace([string]$BotState.ModeLabel) -and [string]$BotState.ModeLabel -ne "-") {
        return [string]$BotState.ModeLabel
    }

    return "-"
}

function Test-LiveCredentialConnection {
    param(
        [ref]$Reason
    )

    $credentialData = Get-RuntimeCredentialData -UseTestnet $false
    if ($null -eq $credentialData) {
        $Reason.Value = "Credencial real nao encontrada para validacao."
        return $false
    }

    $pythonScript = @'
import json
import config
from bot_runner import _build_single_user_execution_context
from database.database import db
from services.live_execution_service import LiveExecutionService

config.apply_symbol_strategy_overrides(config.SYMBOL)
context = _build_single_user_execution_context()
service = LiveExecutionService(database=db)
validation = service.validate_account_connection(context, testnet=False)
snapshot = config.build_runtime_strategy_snapshot()
result = {
    "validation": validation,
    "reconcile": None,
    "ok": False,
}
if validation.get("ok"):
    reconcile = service.reconcile_account_state(
        context=context,
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        strategy_version=snapshot.get("strategy_version"),
        testnet=False,
        source="desktop_preflight",
    )
    result["reconcile"] = reconcile
    result["ok"] = bool(reconcile.get("ok"))
else:
    result["ok"] = False
print(json.dumps(result, ensure_ascii=True))
'@

    $overrides = Get-LaunchEnvironmentOverrides -UseTestnet $false -Symbol (Get-SelectedBotSymbol)
    $envAssignments = [ordered]@{
        BINANCE_API_KEY = [string]$credentialData.ApiKey
        BINANCE_SECRET_KEY = [string]$credentialData.ApiSecret
    }

    $previousValues = @{}
    try {
        foreach ($entry in $overrides.GetEnumerator()) {
            $previousValues[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
            [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
        }
        foreach ($entry in $envAssignments.GetEnumerator()) {
            $previousValues[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
            [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
        }

        $raw = Invoke-ProjectPython -InlineScript $pythonScript
        if ([string]::IsNullOrWhiteSpace([string]$raw)) {
            $Reason.Value = "Nao foi possivel validar a conexao da conta real."
            return $false
        }

        $result = $raw | ConvertFrom-Json
        if (-not $result.ok) {
            $reconcileProperty = $null
            $validationProperty = $null
            if ($null -ne $result -and $null -ne $result.PSObject) {
                $reconcileProperty = $result.PSObject.Properties.Match("reconcile") | Select-Object -First 1
                $validationProperty = $result.PSObject.Properties.Match("validation") | Select-Object -First 1
            }

            $reconcile = if ($null -ne $reconcileProperty) { $reconcileProperty.Value } else { $null }
            $validation = if ($null -ne $validationProperty) { $validationProperty.Value } else { $null }

            $reconcileOkProperty = if ($null -ne $reconcile -and $null -ne $reconcile.PSObject) {
                $reconcile.PSObject.Properties.Match("ok") | Select-Object -First 1
            } else { $null }
            $validationOkProperty = if ($null -ne $validation -and $null -ne $validation.PSObject) {
                $validation.PSObject.Properties.Match("ok") | Select-Object -First 1
            } else { $null }

            if ($null -ne $reconcileOkProperty -and -not [bool]$reconcileOkProperty.Value) {
                $reconcileErrorProperty = if ($null -ne $reconcile -and $null -ne $reconcile.PSObject) {
                    $reconcile.PSObject.Properties.Match("error") | Select-Object -First 1
                } else { $null }
                $reconcileError = if ($null -ne $reconcileErrorProperty) { [string]$reconcileErrorProperty.Value } else { "erro nao informado" }
                if ($reconcileError -match "-1021|outside of the recvWindow|Timestamp for this request") {
                    $Reason.Value = "Falha na conexao real com a Binance Futures: $reconcileError Ajuste a data e hora do Windows para sincronizacao automatica antes de ligar o live."
                }
                else {
                    $Reason.Value = "Falha na conexao real com a Binance Futures: $reconcileError"
                }
            }
            elseif ($null -ne $validationOkProperty -and -not [bool]$validationOkProperty.Value) {
                $validationErrorProperty = if ($null -ne $validation -and $null -ne $validation.PSObject) {
                    $validation.PSObject.Properties.Match("error") | Select-Object -First 1
                } else { $null }
                $validationError = if ($null -ne $validationErrorProperty) { [string]$validationErrorProperty.Value } else { "erro nao informado" }
                if ($validationError -match "-1021|outside of the recvWindow|Timestamp for this request") {
                    $Reason.Value = "Falha na validacao da conta real: $validationError Ajuste a data e hora do Windows para sincronizacao automatica antes de ligar o live."
                }
                else {
                    $Reason.Value = "Falha na validacao da conta real: $validationError"
                }
            }
            else {
                $Reason.Value = "Falha na validacao da conta real."
            }
            return $false
        }

        $Reason.Value = ""
        return $true
    }
    catch {
        Write-LauncherErrorLog -Context "Test-LiveCredentialConnection" -Message ([string]$_.Exception.Message) -ErrorRecord $_
        $Reason.Value = "Falha ao validar credenciais reais: $($_.Exception.Message)"
        return $false
    }
    finally {
        foreach ($entry in $overrides.GetEnumerator()) {
            $previousValue = if ($previousValues.ContainsKey($entry.Key)) { $previousValues[$entry.Key] } else { $null }
            [Environment]::SetEnvironmentVariable($entry.Key, $previousValue, "Process")
        }
        foreach ($entry in $envAssignments.GetEnumerator()) {
            $previousValue = if ($previousValues.ContainsKey($entry.Key)) { $previousValues[$entry.Key] } else { $null }
            [Environment]::SetEnvironmentVariable($entry.Key, $previousValue, "Process")
        }
    }
}

function Wait-ForBotLaunchConfirmation {
    param(
        [bool]$UseTestnet,
        [DateTime]$LaunchStartedAtUtc,
        [string]$ExpectedSymbol = "",
        [string]$ExpectedTimeframe = ""
    )

    $deadline = (Get-Date).AddSeconds(150)
    $expectedEnvironment = Get-ExpectedRuntimeEnvironment -UseTestnet $UseTestnet
    $expectedModeLabel = Get-ExpectedModeLabel -UseTestnet $UseTestnet

    while ((Get-Date) -lt $deadline) {
        $payload = Get-BotProcessStatePayload
        $runtimeState = Get-BotRuntimeDatabaseState
        $payloadProcess = $null
        $runnerProcess = $null
        $logProgress = Test-LaunchLogProgress -UseTestnet $UseTestnet -LaunchStartedAtUtc $LaunchStartedAtUtc

        if ($null -ne $payload -and $payload.pid) {
            $payloadProcess = Get-Process -Id ([int]$payload.pid) -ErrorAction SilentlyContinue
        }

        $runnerProcess = Find-BotRunnerProcess
        if ($null -ne $runnerProcess -and $runnerProcess.Pid) {
            $launcherPid = $(if ($null -ne $payload -and $null -ne ($payload.PSObject.Properties["launcher_pid"])) { $payload.launcher_pid } else { $null })
            $requestedMode = $(if ($null -ne $payload -and $null -ne ($payload.PSObject.Properties["requested_mode"])) { [string]$payload.requested_mode } else { $expectedModeLabel })
            Write-BotProcessStatePayload `
                -ProcessId ([int]$runnerProcess.Pid) `
                -UseTestnet $UseTestnet `
                -Source ([string]$runnerProcess.Source) `
                -Command ([string]$runnerProcess.CommandLine) `
                -Extra @{
                    launcher_pid = $launcherPid
                    boot_phase = "runner_detected"
                    requested_mode = $requestedMode
                    symbol = $ExpectedSymbol
                    timeframe = $ExpectedTimeframe
                } | Out-Null
            $payload = Get-BotProcessStatePayload
        }

        if ($null -ne $payload -and $payload.use_testnet -ne $UseTestnet) {
            return [pscustomobject]@{
                Ready = $false
                Message = "O runtime subiu em modo diferente do solicitado. Esperado: $expectedModeLabel."
            }
        }

        $heartbeatOk = $false
        if ($null -ne $runtimeState -and -not [string]::IsNullOrWhiteSpace([string]$runtimeState.last_heartbeat_at)) {
            try {
                $heartbeatAtUtc = [DateTime]::Parse([string]$runtimeState.last_heartbeat_at).ToUniversalTime()
                $heartbeatOk = $heartbeatAtUtc -ge $LaunchStartedAtUtc.AddSeconds(-5)
            }
            catch {
                $heartbeatOk = $false
            }
        }

        $runtimeMatches = (
            $null -ne $runtimeState -and
            [string]$runtimeState.environment -eq $expectedEnvironment -and
            ([string]::IsNullOrWhiteSpace($ExpectedSymbol) -or [string]$runtimeState.symbol -eq [string]$ExpectedSymbol) -and
            ([string]::IsNullOrWhiteSpace($ExpectedTimeframe) -or [string]$runtimeState.timeframe -eq [string]$ExpectedTimeframe) -and
            ($UseTestnet -or [bool]$runtimeState.live_enabled)
        )

        if ($null -ne $payloadProcess -and $runtimeMatches -and $heartbeatOk) {
            return [pscustomobject]@{
                Ready = $true
                Message = ""
                RuntimeState = $runtimeState
            }
        }

        if ($null -ne $runnerProcess -and $runtimeMatches -and $heartbeatOk) {
            return [pscustomobject]@{
                Ready = $true
                Message = ""
                RuntimeState = $runtimeState
            }
        }

        if (
            $runtimeMatches -and
            (
                ($UseTestnet -and [bool]$logProgress.FeedReady) -or
                (-not $UseTestnet -and ([bool]$logProgress.LiveLayerReady -or [bool]$logProgress.FeedReady))
            )
        ) {
            return [pscustomobject]@{
                Ready = $true
                Message = ""
                RuntimeState = $runtimeState
            }
        }

        if (
            ($UseTestnet -and [bool]$logProgress.FeedReady) -or
            (-not $UseTestnet -and ([bool]$logProgress.LiveLayerReady -or [bool]$logProgress.FeedReady))
        ) {
            return [pscustomobject]@{
                Ready = $true
                Message = ""
                RuntimeState = $runtimeState
            }
        }

        if ($null -ne $runnerProcess -and [bool]$logProgress.HasProgress) {
            return [pscustomobject]@{
                Ready = $true
                Message = ""
                RuntimeState = $runtimeState
            }
        }

        Start-Sleep -Seconds 1
    }

    $logTail = Get-LogTail -Path $script:BotExecutionLogPath -Lines 25
    return [pscustomobject]@{
        Ready = $false
        Message = "O bot nao confirmou subida em $expectedModeLabel dentro do prazo." + [Environment]::NewLine + [Environment]::NewLine + $logTail
    }
}

function Get-LiveModePreflight {
    param(
        [bool]$UseTestnet = $false,
        [switch]$ForceRefresh
    )

    $selectedSymbol = Get-SelectedBotSymbol
    $cacheKey = if ($UseTestnet) { "testnet:$selectedSymbol" } else { "mainnet:$selectedSymbol" }
    $cacheTtlSeconds = 30
    $cachedAt = if ($script:CachedLivePreflightFetchedAt.ContainsKey($cacheKey)) { [DateTime]$script:CachedLivePreflightFetchedAt[$cacheKey] } else { [DateTime]::MinValue }
    if (
        -not $ForceRefresh -and
        $script:CachedLivePreflightByMode.ContainsKey($cacheKey) -and
        $cachedAt -ne [DateTime]::MinValue -and
        ((Get-Date) - $cachedAt).TotalSeconds -lt $cacheTtlSeconds
    ) {
        return $script:CachedLivePreflightByMode[$cacheKey]
    }

    $pythonScript = @'
import json
import os
import config

config.apply_symbol_strategy_overrides(config.SYMBOL)
record = config.get_symbol_validation_record(config.SYMBOL)
symbol_ready = True
symbol_reason = "ok"
if bool(getattr(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True)):
    if config.is_symbol_runtime_approved(config.SYMBOL):
        symbol_ready = True
        symbol_reason = str(record.get("approval_label") or "approved")
    else:
        status = str(record.get("status") or "unknown").strip().lower()
        reason = str(record.get("reason") or record.get("summary") or "sem detalhe").strip() or "sem detalhe"
        symbol_ready = False
        symbol_reason = f"{status}: {reason}"

payload = {
    "symbol": str(config.SYMBOL),
    "timeframe": str(config.TIMEFRAME),
    "testnet": bool(config.TESTNET),
    "live_enabled": bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION),
    "confirmation_ok": str(config.LIVE_TRADING_CONFIRMATION or "").strip().upper() == "EU_ASSUMO_RISCO",
    "risk_per_trade_pct": float(config.RISK_PER_TRADE_PCT or 0.0),
    "max_real_risk_per_trade_pct_start": float(config.MAX_REAL_RISK_PER_TRADE_PCT_START or 0.25),
    "max_daily_real_loss_pct": float(config.MAX_DAILY_REAL_LOSS_PCT or 0.0),
    "max_consecutive_real_losses": int(config.MAX_CONSECUTIVE_REAL_LOSSES or 0),
    "max_open_real_trades": int(config.MAX_OPEN_REAL_TRADES or 0),
    "runtime_require_approved_symbol": bool(getattr(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True)),
    "symbol_ready": bool(symbol_ready),
    "symbol_reason": str(symbol_reason),
    "account_id": str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or ""),
}
print(json.dumps(payload))
'@

    $overrides = Get-LaunchEnvironmentOverrides -UseTestnet $UseTestnet -Symbol $selectedSymbol
    $previousValues = @{}
    $script:LastLivePreflightError = ""
    try {
        foreach ($entry in $overrides.GetEnumerator()) {
            $previousValues[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
            [Environment]::SetEnvironmentVariable($entry.Key, [string]$entry.Value, "Process")
        }
        $raw = Invoke-ProjectPython -InlineScript $pythonScript
        if (-not $raw) {
            $script:LastLivePreflightError = "O script Python do preflight retornou vazio."
            $script:CachedLivePreflightByMode[$cacheKey] = $null
            $script:CachedLivePreflightFetchedAt[$cacheKey] = Get-Date
            return $null
        }
        $parsed = $raw | ConvertFrom-Json
        if ($null -eq $parsed) {
            $script:LastLivePreflightError = "O JSON do preflight retornou nulo."
            $script:CachedLivePreflightByMode[$cacheKey] = $null
            $script:CachedLivePreflightFetchedAt[$cacheKey] = Get-Date
            return $null
        }
        $script:CachedLivePreflightByMode[$cacheKey] = $parsed
        $script:CachedLivePreflightFetchedAt[$cacheKey] = Get-Date
        return $script:CachedLivePreflightByMode[$cacheKey]
    }
    catch {
        $script:LastLivePreflightError = [string]$_.Exception.Message
        Write-LauncherErrorLog -Context "Get-LiveModePreflight" -Message $script:LastLivePreflightError -ErrorRecord $_
        $script:CachedLivePreflightByMode[$cacheKey] = $null
        $script:CachedLivePreflightFetchedAt[$cacheKey] = Get-Date
        return $null
    }
    finally {
        foreach ($entry in $overrides.GetEnumerator()) {
            $previousValue = if ($previousValues.ContainsKey($entry.Key)) { $previousValues[$entry.Key] } else { $null }
            [Environment]::SetEnvironmentVariable($entry.Key, $previousValue, "Process")
        }
    }
}

function Test-LiveModeReady {
    param(
        [ref]$Reason
    )

    $credentialData = Get-RuntimeCredentialData -UseTestnet $false
    $preflight = Get-LiveModePreflight -UseTestnet $false -ForceRefresh
    if ($null -eq $preflight) {
        if ($null -eq $credentialData) {
            $detail = if ([string]::IsNullOrWhiteSpace($script:LastLivePreflightError)) { "" } else { " Detalhe: $($script:LastLivePreflightError)" }
            $Reason.Value = "API key/secret da conta real ainda nao estao configuradas nesta interface ou no ambiente.$detail"
            return $false
        }

        # Se o preflight local falhar, seguimos para a validacao forte da conta real
        # antes do start efetivo. Isso evita falso bloqueio da interface.
        $Reason.Value = ""
        return $true
    }
    if (-not $preflight.live_enabled) {
        $Reason.Value = "ENABLE_LIVE_EXECUTION=false. O live ainda esta desarmado."
        return $false
    }
    if (-not $preflight.confirmation_ok) {
        $Reason.Value = "LIVE_TRADING_CONFIRMATION ainda nao foi armado."
        return $false
    }
    if ($null -eq $credentialData) {
        $Reason.Value = "API key/secret da conta real ainda nao estao configuradas nesta interface ou no ambiente."
        return $false
    }
    if ([double]$preflight.risk_per_trade_pct -gt [double]$preflight.max_real_risk_per_trade_pct_start) {
        $Reason.Value = "Risco por trade acima do limite de go-live."
        return $false
    }
    if (-not $preflight.symbol_ready) {
        $Reason.Value = "Governanca do simbolo bloqueou o live: $($preflight.symbol_reason)"
        return $false
    }
    $Reason.Value = ""
    return $true
}

function Format-LivePreflightSummary {
    param(
        $Preflight,
        $CredentialData
    )

    if ($null -eq $Preflight) {
        if ([string]::IsNullOrWhiteSpace($script:LastLivePreflightError)) {
            return "Preflight indisponivel."
        }
        return "Preflight indisponivel." + [Environment]::NewLine + "Detalhe: $($script:LastLivePreflightError)"
    }

    $credentialState = if ($null -ne $CredentialData) {
        "credencial pronta via $($CredentialData.SourceLabel)"
    }
    else {
        "credencial ausente"
    }

    $symbolState = if ($Preflight.symbol_ready) {
        "sim"
    }
    else {
        "nao"
    }

    return @(
        "Launch profile: TESTNET=$($Preflight.testnet) | live_enabled=$($Preflight.live_enabled) | confirmation_ok=$($Preflight.confirmation_ok)"
        "Mercado: $($Preflight.symbol) $($Preflight.timeframe) | account_id=$($Preflight.account_id) | simbolo_aprovado=$symbolState ($($Preflight.symbol_reason))"
        "Risco: trade=$([double]$Preflight.risk_per_trade_pct)% | cap_live=$([double]$Preflight.max_real_risk_per_trade_pct_start)% | daily_loss=$([double]$Preflight.max_daily_real_loss_pct)% | streak=$([int]$Preflight.max_consecutive_real_losses) | open_real=$([int]$Preflight.max_open_real_trades)"
        "Credenciais: $credentialState"
    ) -join [Environment]::NewLine
}

function Start-DashboardUi {
    if (-not (Test-DashboardOnline)) {
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$script:DashboardCmd`"" -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 4
    }
    Start-Process $script:DashboardUrl | Out-Null
}

function Start-BotDirect {
    param(
        [bool]$UseTestnet
    )

    [void](Sync-UiCredentialInputsToPersistentSlot -UseTestnet $UseTestnet)

    $selectedSymbol = Get-SelectedBotSymbol
    $launchOverrides = Get-LaunchEnvironmentOverrides -UseTestnet $UseTestnet -Symbol $selectedSymbol
    $selectedTimeframe = [string]$launchOverrides.TIMEFRAME
    $state = Get-BotState
    $runtimeState = Get-BotRuntimeDatabaseState -ForceRefresh
    $expectedModeLabel = Get-ExpectedModeLabel -UseTestnet $UseTestnet
    $currentModeLabel = Resolve-BotModeLabel -BotState $state -RuntimeState $runtimeState
    if ($state.Running) {
        $sameSymbol = ($null -ne $runtimeState -and [string]$runtimeState.symbol -eq $selectedSymbol)
        $sameTimeframe = ($null -ne $runtimeState -and [string]$runtimeState.timeframe -eq $selectedTimeframe)
        if ($currentModeLabel -eq $expectedModeLabel -and $sameSymbol -and $sameTimeframe) {
            [System.Windows.Forms.MessageBox]::Show(
                "O bot ja esta rodando em $expectedModeLabel para $selectedSymbol $selectedTimeframe (PID $($state.Pid)).",
                "Evo Coin Bot",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Information
            ) | Out-Null
            return
        }

        $switchMode = [System.Windows.Forms.MessageBox]::Show(
            "Ja existe um bot rodando em $currentModeLabel." + [Environment]::NewLine + [Environment]::NewLine + "Deseja parar esse runtime e religar em $expectedModeLabel para $selectedSymbol $selectedTimeframe?",
            "Trocar Modo do Bot",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question
        )
        if ($switchMode -ne [System.Windows.Forms.DialogResult]::Yes) {
            return
        }

        Stop-BotDirect
        Start-Sleep -Seconds 2
        $state = Get-BotState
        if ($state.Running) {
            [System.Windows.Forms.MessageBox]::Show(
                "Nao foi possivel parar o runtime atual antes da troca de modo.",
                "Evo Coin Bot",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
            return
        }
    }

    if (-not $UseTestnet) {
        $reason = ""
        if (-not (Test-LiveModeReady -Reason ([ref]$reason))) {
            [System.Windows.Forms.MessageBox]::Show(
                $reason,
                "Conta Real Bloqueada",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
            return
        }

        $confirmed = [System.Windows.Forms.MessageBox]::Show(
            "Voce esta prestes a ligar o bot direto em CONTA REAL." + [Environment]::NewLine + [Environment]::NewLine + (Get-CurrentLaunchProfileSummary -UseTestnet $false -Symbol $selectedSymbol) + [Environment]::NewLine + [Environment]::NewLine + "Continue apenas se a virada estiver decidida.",
            "Confirmacao Conta Real",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )
        if ($confirmed -ne [System.Windows.Forms.DialogResult]::Yes) {
            return
        }

        $connectionReason = ""
        if (-not (Test-LiveCredentialConnection -Reason ([ref]$connectionReason))) {
            [System.Windows.Forms.MessageBox]::Show(
                $connectionReason,
                "Conta Real Bloqueada",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            ) | Out-Null
            return
        }
    }

    Remove-Item -LiteralPath $script:StopSignalPath -Force -ErrorAction SilentlyContinue

    $credentialData = Get-RuntimeCredentialData -UseTestnet $UseTestnet
    $apiKeyValue = if ($null -ne $credentialData) { [string]$credentialData.ApiKey } else { "" }
    $apiSecretValue = if ($null -ne $credentialData) { [string]$credentialData.ApiSecret } else { "" }
    $commandParts = @()
    foreach ($entry in $launchOverrides.GetEnumerator()) {
        $commandParts += ('$env:' + $entry.Key + '=' + (ConvertTo-PowerShellQuotedString -Value ([string]$entry.Value)))
    }
    $commandParts += @(
        '$env:TRADER_BOT_LAUNCH_SOURCE="desktop_launcher"'
        'Set-Location -LiteralPath "' + $script:ProjectRoot + '"'
        '& "' + $script:PythonExe + '" "bot_runner.py" 1>> "' + $script:BotStdoutLogPath + '" 2>> "' + $script:BotStderrLogPath + '"'
    )

    if ($null -ne $credentialData) {
        $commandParts = @(
            '$env:BINANCE_API_KEY=' + (ConvertTo-PowerShellQuotedString -Value $apiKeyValue)
            '$env:BINANCE_SECRET_KEY=' + (ConvertTo-PowerShellQuotedString -Value $apiSecretValue)
        ) + $commandParts
    }

    if ($UseTestnet) {
        if ($null -ne $credentialData) {
            $commandParts = @(
                '$env:BINANCE_TESTNET_API_KEY=' + (ConvertTo-PowerShellQuotedString -Value $apiKeyValue)
                '$env:BINANCE_TESTNET_SECRET_KEY=' + (ConvertTo-PowerShellQuotedString -Value $apiSecretValue)
            ) + $commandParts
        }
    }

    $command = $commandParts -join "; "
    $stateCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "& ' + $script:PythonExe + ' bot_runner.py"'
    $launchStartedAtUtc = [DateTime]::UtcNow

    Clear-BotProcessStatePayload

    $launcherProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command `
        -WindowStyle Hidden `
        -PassThru

    if ($null -ne $launcherProcess) {
        Write-BotProcessStatePayload `
            -ProcessId ([int]$launcherProcess.Id) `
            -UseTestnet $UseTestnet `
            -Source "desktop_launcher_pending" `
            -Command $stateCommand `
            -Extra @{
                launcher_pid = [int]$launcherProcess.Id
                boot_phase = "launcher_shell"
                requested_mode = $expectedModeLabel
                symbol = $selectedSymbol
                timeframe = $selectedTimeframe
            } | Out-Null
    }

    $launchConfirmation = Wait-ForBotLaunchConfirmation -UseTestnet $UseTestnet -LaunchStartedAtUtc $launchStartedAtUtc -ExpectedSymbol $selectedSymbol -ExpectedTimeframe $selectedTimeframe
    if (-not $launchConfirmation.Ready) {
        Stop-BotDirect
        throw $launchConfirmation.Message
    }
}

function Stop-BotDirect {
    $state = Get-BotState
    $payload = Get-BotProcessStatePayload
    $pidCandidates = New-Object System.Collections.Generic.List[int]

    if ($state.Running -and $state.Pid) {
        $pidCandidates.Add([int]$state.Pid)
    }

    if ($null -ne $payload) {
        $payloadPid = $(if ($null -ne ($payload.PSObject.Properties["pid"])) { $payload.pid } else { $null })
        $payloadLauncherPid = $(if ($null -ne ($payload.PSObject.Properties["launcher_pid"])) { $payload.launcher_pid } else { $null })
        foreach ($candidatePid in @($payloadPid, $payloadLauncherPid)) {
            if ($candidatePid) {
                $candidateValue = [int]$candidatePid
                if (-not $pidCandidates.Contains($candidateValue)) {
                    $pidCandidates.Add($candidateValue)
                }
            }
        }
    }

    if ($pidCandidates.Count -eq 0) {
        $fallbackProcesses = Get-BotRunnerProcesses
        foreach ($fallbackProcess in $fallbackProcesses) {
            if ($null -ne $fallbackProcess -and $fallbackProcess.Pid) {
                $candidateValue = [int]$fallbackProcess.Pid
                if (-not $pidCandidates.Contains($candidateValue)) {
                    $pidCandidates.Add($candidateValue)
                }
            }
        }
    }

    if ($pidCandidates.Count -eq 0) {
        if ($null -ne $payload) {
            Clear-BotProcessStatePayload
        }
        [System.Windows.Forms.MessageBox]::Show(
            "Nao existe bot direto rodando no momento. Se havia um estado antigo salvo, ele foi limpo.",
            "Evo Coin Bot",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
        return
    }

    Set-Content -LiteralPath $script:StopSignalPath -Value ([DateTime]::UtcNow.ToString("o")) -Encoding UTF8

    foreach ($pidToStop in $pidCandidates) {
        $deadline = (Get-Date).AddSeconds(8)

        while ((Get-Date) -lt $deadline) {
            $process = Get-Process -Id $pidToStop -ErrorAction SilentlyContinue
            if ($null -eq $process) {
                break
            }
            Start-Sleep -Milliseconds 500
        }

        $process = Get-Process -Id $pidToStop -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            try {
                Stop-Process -Id $pidToStop -Force -ErrorAction Stop
            }
            catch {
            }
        }
    }

    Remove-Item -LiteralPath $script:StopSignalPath -Force -ErrorAction SilentlyContinue
    Clear-BotProcessStatePayload
}

function Open-BotLog {
    if (-not (Test-Path -LiteralPath $script:BotExecutionLogPath)) {
        [System.Windows.Forms.MessageBox]::Show(
            "O log do bot ainda nao existe.",
            "Evo Coin Bot",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
        return
    }

    Start-Process "notepad.exe" -ArgumentList ('"' + $script:BotExecutionLogPath + '"') | Out-Null
}

function Close-OtherInterfaceWindows {
    try {
        $currentProcessId = [int]$PID
        $otherWindows = Get-Process powershell -ErrorAction SilentlyContinue | Where-Object {
            $_.Id -ne $currentProcessId -and
            $_.MainWindowHandle -ne 0 -and
            [string]$_.MainWindowTitle -eq $script:UiWindowTitle
        }

        foreach ($process in $otherWindows) {
            try {
                $null = $process.CloseMainWindow()
                Start-Sleep -Milliseconds 750
                if (-not $process.HasExited) {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                }
            }
            catch {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    catch {
    }
}

if ($NoUi) {
    Write-Output ("ProjectRoot=" + $script:ProjectRoot)
    Write-Output ("PythonExe=" + $script:PythonExe)
    Write-Output ("DashboardCmd=" + $script:DashboardCmd)
    exit 0
}

[System.Windows.Forms.Application]::SetUnhandledExceptionMode([System.Windows.Forms.UnhandledExceptionMode]::CatchException)
[System.Windows.Forms.Application]::add_ThreadException({
    param($sender, $eventArgs)

    $summary = if ($null -ne $eventArgs -and $null -ne $eventArgs.Exception) {
        [string]$eventArgs.Exception.Message
    }
    else {
        "Erro nao identificado no thread principal da interface."
    }

    Write-LauncherErrorLog -Context "Unhandled UI Exception" -Message $summary -ErrorRecord $null
    [System.Windows.Forms.MessageBox]::Show(
        "A interface encontrou um erro interno." + [Environment]::NewLine + "Detalhe: " + $summary + [Environment]::NewLine + [Environment]::NewLine + "Veja logs\\desktop_launcher_error.log.",
        "Evo Coin Bot",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
})

$form = New-Object System.Windows.Forms.Form
$form.Text = $script:UiWindowTitle
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(1140, 1080)
$form.MinimumSize = New-Object System.Drawing.Size(1140, 1080)
$form.AutoScroll = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(245, 247, 250)
$form.Font = New-Object System.Drawing.Font("Segoe UI", 10)

$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = "Evo Coin Bot | Interface Grafica"
$titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
$titleLabel.AutoSize = $true
$titleLabel.Location = New-Object System.Drawing.Point(18, 16)
$form.Controls.Add($titleLabel)

$subtitleLabel = New-Object System.Windows.Forms.Label
$subtitleLabel.Text = "Escolha entre abrir o painel Streamlit ou rodar o bot direto sem abrir o CMD."
$subtitleLabel.AutoSize = $true
$subtitleLabel.Location = New-Object System.Drawing.Point(20, 50)
$form.Controls.Add($subtitleLabel)

$statusGroup = New-Object System.Windows.Forms.GroupBox
$statusGroup.Text = "Status"
$statusGroup.Location = New-Object System.Drawing.Point(20, 82)
$statusGroup.Size = New-Object System.Drawing.Size(1080, 110)
$form.Controls.Add($statusGroup)

$dashboardStatusLabel = New-Object System.Windows.Forms.Label
$dashboardStatusLabel.AutoSize = $true
$dashboardStatusLabel.Location = New-Object System.Drawing.Point(16, 30)
$statusGroup.Controls.Add($dashboardStatusLabel)

$botStatusLabel = New-Object System.Windows.Forms.Label
$botStatusLabel.AutoSize = $true
$botStatusLabel.Location = New-Object System.Drawing.Point(16, 55)
$statusGroup.Controls.Add($botStatusLabel)

$metaStatusLabel = New-Object System.Windows.Forms.Label
$metaStatusLabel.AutoSize = $true
$metaStatusLabel.Location = New-Object System.Drawing.Point(16, 80)
$statusGroup.Controls.Add($metaStatusLabel)

$actionsGroup = New-Object System.Windows.Forms.GroupBox
$actionsGroup.Text = "Acoes"
$actionsGroup.Location = New-Object System.Drawing.Point(20, 205)
$actionsGroup.Size = New-Object System.Drawing.Size(1080, 138)
$form.Controls.Add($actionsGroup)

$modeLabel = New-Object System.Windows.Forms.Label
$modeLabel.Text = "Modo do bot direto:"
$modeLabel.AutoSize = $true
$modeLabel.Location = New-Object System.Drawing.Point(20, 31)
$actionsGroup.Controls.Add($modeLabel)

$script:modeComboBox = New-Object System.Windows.Forms.ComboBox
$script:modeComboBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
[void]$script:modeComboBox.Items.Add("Testnet")
[void]$script:modeComboBox.Items.Add("Conta Real")
$script:modeComboBox.SelectedItem = $(if ($script:UiState.selected_mode -eq "Conta Real") { "Conta Real" } else { "Testnet" })
$script:modeComboBox.Location = New-Object System.Drawing.Point(145, 27)
$script:modeComboBox.Size = New-Object System.Drawing.Size(140, 30)
$actionsGroup.Controls.Add($script:modeComboBox)

$symbolLabel = New-Object System.Windows.Forms.Label
$symbolLabel.Text = "Ativo:"
$symbolLabel.AutoSize = $true
$symbolLabel.Location = New-Object System.Drawing.Point(308, 31)
$actionsGroup.Controls.Add($symbolLabel)

$script:symbolComboBox = New-Object System.Windows.Forms.ComboBox
$script:symbolComboBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
[void]$script:symbolComboBox.Items.Add("BTC/USDT")
[void]$script:symbolComboBox.Items.Add("XLM/USDT")
$script:symbolComboBox.SelectedItem = $(if ($script:UiState.selected_symbol -in @("BTC/USDT", "XLM/USDT")) { $script:UiState.selected_symbol } else { "BTC/USDT" })
$script:symbolComboBox.Location = New-Object System.Drawing.Point(352, 27)
$script:symbolComboBox.Size = New-Object System.Drawing.Size(120, 30)
$actionsGroup.Controls.Add($script:symbolComboBox)

$symbolHintLabel = New-Object System.Windows.Forms.Label
$symbolHintLabel.AutoSize = $false
$symbolHintLabel.Location = New-Object System.Drawing.Point(484, 31)
$symbolHintLabel.Size = New-Object System.Drawing.Size(168, 34)
$symbolHintLabel.ForeColor = [System.Drawing.Color]::FromArgb(70, 70, 70)
$actionsGroup.Controls.Add($symbolHintLabel)

$modeHintLabel = New-Object System.Windows.Forms.Label
$modeHintLabel.AutoSize = $false
$modeHintLabel.Location = New-Object System.Drawing.Point(664, 31)
$modeHintLabel.Size = New-Object System.Drawing.Size(392, 34)
$modeHintLabel.ForeColor = [System.Drawing.Color]::FromArgb(120, 80, 20)
$actionsGroup.Controls.Add($modeHintLabel)

$openDashboardButton = New-Object System.Windows.Forms.Button
$openDashboardButton.Text = "Abrir Streamlit"
$openDashboardButton.Size = New-Object System.Drawing.Size(150, 34)
$openDashboardButton.Location = New-Object System.Drawing.Point(20, 80)
$actionsGroup.Controls.Add($openDashboardButton)

$startBotButton = New-Object System.Windows.Forms.Button
$startBotButton.Text = "Ligar Bot Direto"
$startBotButton.Size = New-Object System.Drawing.Size(150, 34)
$startBotButton.Location = New-Object System.Drawing.Point(188, 80)
$actionsGroup.Controls.Add($startBotButton)

$stopBotButton = New-Object System.Windows.Forms.Button
$stopBotButton.Text = "Parar Bot"
$stopBotButton.Size = New-Object System.Drawing.Size(120, 34)
$stopBotButton.Location = New-Object System.Drawing.Point(356, 80)
$actionsGroup.Controls.Add($stopBotButton)

$refreshButton = New-Object System.Windows.Forms.Button
$refreshButton.Text = "Atualizar"
$refreshButton.Size = New-Object System.Drawing.Size(100, 34)
$refreshButton.Location = New-Object System.Drawing.Point(494, 80)
$actionsGroup.Controls.Add($refreshButton)

$openLogButton = New-Object System.Windows.Forms.Button
$openLogButton.Text = "Abrir Log"
$openLogButton.Size = New-Object System.Drawing.Size(88, 34)
$openLogButton.Location = New-Object System.Drawing.Point(606, 80)
$actionsGroup.Controls.Add($openLogButton)

$autoFollowLogCheckBox = New-Object System.Windows.Forms.CheckBox
$autoFollowLogCheckBox.Text = "Seguir log automaticamente"
$autoFollowLogCheckBox.Checked = $true
$autoFollowLogCheckBox.AutoSize = $true
$autoFollowLogCheckBox.Location = New-Object System.Drawing.Point(710, 86)
$actionsGroup.Controls.Add($autoFollowLogCheckBox)

$livePreflightGroup = New-Object System.Windows.Forms.GroupBox
$livePreflightGroup.Text = "Preflight Conta Real"
$livePreflightGroup.Location = New-Object System.Drawing.Point(20, 352)
$livePreflightGroup.Size = New-Object System.Drawing.Size(1080, 126)
$form.Controls.Add($livePreflightGroup)

$livePreflightStatusLabel = New-Object System.Windows.Forms.Label
$livePreflightStatusLabel.AutoSize = $true
$livePreflightStatusLabel.Location = New-Object System.Drawing.Point(16, 28)
$livePreflightGroup.Controls.Add($livePreflightStatusLabel)

$livePreflightTextBox = New-Object System.Windows.Forms.TextBox
$livePreflightTextBox.Multiline = $true
$livePreflightTextBox.ReadOnly = $true
$livePreflightTextBox.ScrollBars = "Vertical"
$livePreflightTextBox.Location = New-Object System.Drawing.Point(16, 50)
$livePreflightTextBox.Size = New-Object System.Drawing.Size(1044, 60)
$livePreflightTextBox.Font = New-Object System.Drawing.Font("Consolas", 9)
$livePreflightGroup.Controls.Add($livePreflightTextBox)

$credentialsGroup = New-Object System.Windows.Forms.GroupBox
$credentialsGroup.Text = "Credenciais do Bot Direto"
$credentialsGroup.Location = New-Object System.Drawing.Point(20, 492)
$credentialsGroup.Size = New-Object System.Drawing.Size(1080, 230)
$form.Controls.Add($credentialsGroup)

$credentialsHintLabel = New-Object System.Windows.Forms.Label
$credentialsHintLabel.Text = "Selecione o modo desejado e use a aba correspondente abaixo. Para conta real, cole API Key e API Secret na aba Conta Real. As chaves ficam salvas localmente para esta interface, protegidas pelo usuario do Windows."
$credentialsHintLabel.AutoSize = $false
$credentialsHintLabel.Location = New-Object System.Drawing.Point(16, 24)
$credentialsHintLabel.Size = New-Object System.Drawing.Size(1044, 36)
$credentialsGroup.Controls.Add($credentialsHintLabel)

$credentialsTabControl = New-Object System.Windows.Forms.TabControl
$credentialsTabControl.Location = New-Object System.Drawing.Point(16, 62)
$credentialsTabControl.Size = New-Object System.Drawing.Size(1044, 150)
$credentialsGroup.Controls.Add($credentialsTabControl)

$testnetCredentialsTab = New-Object System.Windows.Forms.TabPage
$testnetCredentialsTab.Text = "Testnet"
$credentialsTabControl.TabPages.Add($testnetCredentialsTab)

$testnetCredentialStatusLabel = New-Object System.Windows.Forms.Label
$testnetCredentialStatusLabel.AutoSize = $true
$testnetCredentialStatusLabel.Location = New-Object System.Drawing.Point(12, 12)
$testnetCredentialsTab.Controls.Add($testnetCredentialStatusLabel)

$testnetApiKeyLabel = New-Object System.Windows.Forms.Label
$testnetApiKeyLabel.Text = "API Key:"
$testnetApiKeyLabel.AutoSize = $true
$testnetApiKeyLabel.Location = New-Object System.Drawing.Point(12, 44)
$testnetCredentialsTab.Controls.Add($testnetApiKeyLabel)

$testnetApiKeyTextBox = New-Object System.Windows.Forms.TextBox
$testnetApiKeyTextBox.Location = New-Object System.Drawing.Point(84, 40)
$testnetApiKeyTextBox.Size = New-Object System.Drawing.Size(340, 28)
$testnetApiKeyTextBox.UseSystemPasswordChar = $true
$testnetCredentialsTab.Controls.Add($testnetApiKeyTextBox)

$testnetApiSecretLabel = New-Object System.Windows.Forms.Label
$testnetApiSecretLabel.Text = "API Secret:"
$testnetApiSecretLabel.AutoSize = $true
$testnetApiSecretLabel.Location = New-Object System.Drawing.Point(354, 44)
$testnetCredentialsTab.Controls.Add($testnetApiSecretLabel)

$testnetApiSecretTextBox = New-Object System.Windows.Forms.TextBox
$testnetApiSecretTextBox.Location = New-Object System.Drawing.Point(440, 40)
$testnetApiSecretTextBox.Size = New-Object System.Drawing.Size(340, 28)
$testnetApiSecretTextBox.UseSystemPasswordChar = $true
$testnetCredentialsTab.Controls.Add($testnetApiSecretTextBox)

$saveTestnetCredentialsButton = New-Object System.Windows.Forms.Button
$saveTestnetCredentialsButton.Text = "Salvar na Interface"
$saveTestnetCredentialsButton.Size = New-Object System.Drawing.Size(140, 32)
$saveTestnetCredentialsButton.Location = New-Object System.Drawing.Point(884, 37)
$testnetCredentialsTab.Controls.Add($saveTestnetCredentialsButton)

$clearTestnetCredentialsButton = New-Object System.Windows.Forms.Button
$clearTestnetCredentialsButton.Text = "Limpar Interface"
$clearTestnetCredentialsButton.Size = New-Object System.Drawing.Size(140, 32)
$clearTestnetCredentialsButton.Location = New-Object System.Drawing.Point(884, 74)
$testnetCredentialsTab.Controls.Add($clearTestnetCredentialsButton)

$showTestnetSecretsCheckBox = New-Object System.Windows.Forms.CheckBox
$showTestnetSecretsCheckBox.Text = "Mostrar chaves nesta sessao"
$showTestnetSecretsCheckBox.AutoSize = $true
$showTestnetSecretsCheckBox.Location = New-Object System.Drawing.Point(84, 82)
$testnetCredentialsTab.Controls.Add($showTestnetSecretsCheckBox)

$testnetInfoLabel = New-Object System.Windows.Forms.Label
$testnetInfoLabel.Text = "Uso opcional. O modo testnet/paper roda sem chave; preencha so se quiser validar acesso a exchange."
$testnetInfoLabel.AutoSize = $false
$testnetInfoLabel.Location = New-Object System.Drawing.Point(300, 83)
$testnetInfoLabel.Size = New-Object System.Drawing.Size(560, 30)
$testnetCredentialsTab.Controls.Add($testnetInfoLabel)

$realCredentialsTab = New-Object System.Windows.Forms.TabPage
$realCredentialsTab.Text = "Conta Real"
$credentialsTabControl.TabPages.Add($realCredentialsTab)

$realCredentialStatusLabel = New-Object System.Windows.Forms.Label
$realCredentialStatusLabel.AutoSize = $true
$realCredentialStatusLabel.Location = New-Object System.Drawing.Point(12, 12)
$realCredentialsTab.Controls.Add($realCredentialStatusLabel)

$realApiKeyLabel = New-Object System.Windows.Forms.Label
$realApiKeyLabel.Text = "API Key:"
$realApiKeyLabel.AutoSize = $true
$realApiKeyLabel.Location = New-Object System.Drawing.Point(12, 44)
$realCredentialsTab.Controls.Add($realApiKeyLabel)

$realApiKeyTextBox = New-Object System.Windows.Forms.TextBox
$realApiKeyTextBox.Location = New-Object System.Drawing.Point(84, 40)
$realApiKeyTextBox.Size = New-Object System.Drawing.Size(340, 28)
$realApiKeyTextBox.UseSystemPasswordChar = $true
$realCredentialsTab.Controls.Add($realApiKeyTextBox)

$realApiSecretLabel = New-Object System.Windows.Forms.Label
$realApiSecretLabel.Text = "API Secret:"
$realApiSecretLabel.AutoSize = $true
$realApiSecretLabel.Location = New-Object System.Drawing.Point(354, 44)
$realCredentialsTab.Controls.Add($realApiSecretLabel)

$realApiSecretTextBox = New-Object System.Windows.Forms.TextBox
$realApiSecretTextBox.Location = New-Object System.Drawing.Point(440, 40)
$realApiSecretTextBox.Size = New-Object System.Drawing.Size(340, 28)
$realApiSecretTextBox.UseSystemPasswordChar = $true
$realCredentialsTab.Controls.Add($realApiSecretTextBox)

$saveRealCredentialsButton = New-Object System.Windows.Forms.Button
$saveRealCredentialsButton.Text = "Salvar na Interface"
$saveRealCredentialsButton.Size = New-Object System.Drawing.Size(140, 32)
$saveRealCredentialsButton.Location = New-Object System.Drawing.Point(884, 37)
$realCredentialsTab.Controls.Add($saveRealCredentialsButton)

$clearRealCredentialsButton = New-Object System.Windows.Forms.Button
$clearRealCredentialsButton.Text = "Limpar Interface"
$clearRealCredentialsButton.Size = New-Object System.Drawing.Size(140, 32)
$clearRealCredentialsButton.Location = New-Object System.Drawing.Point(884, 74)
$realCredentialsTab.Controls.Add($clearRealCredentialsButton)

$showRealSecretsCheckBox = New-Object System.Windows.Forms.CheckBox
$showRealSecretsCheckBox.Text = "Mostrar chaves nesta sessao"
$showRealSecretsCheckBox.AutoSize = $true
$showRealSecretsCheckBox.Location = New-Object System.Drawing.Point(84, 82)
$realCredentialsTab.Controls.Add($showRealSecretsCheckBox)

$realInfoLabel = New-Object System.Windows.Forms.Label
$realInfoLabel.Text = (Get-CurrentLaunchProfileSummary -UseTestnet $false -Symbol "BTC/USDT") + " As chaves ficam protegidas localmente e o start valida a conta antes de subir."
$realInfoLabel.AutoSize = $false
$realInfoLabel.Location = New-Object System.Drawing.Point(300, 83)
$realInfoLabel.Size = New-Object System.Drawing.Size(560, 34)
$realCredentialsTab.Controls.Add($realInfoLabel)

$logGroup = New-Object System.Windows.Forms.GroupBox
$logGroup.Text = "Ultimas linhas do bot_execution.log"
$logGroup.Location = New-Object System.Drawing.Point(20, 736)
$logGroup.Size = New-Object System.Drawing.Size(1080, 285)
$form.Controls.Add($logGroup)

$logTextBox = New-Object System.Windows.Forms.TextBox
$logTextBox.Multiline = $true
$logTextBox.ScrollBars = "Both"
$logTextBox.ReadOnly = $true
$logTextBox.WordWrap = $false
$logTextBox.Font = New-Object System.Drawing.Font("Consolas", 9)
$logTextBox.Location = New-Object System.Drawing.Point(16, 28)
$logTextBox.Size = New-Object System.Drawing.Size(1044, 241)
$logGroup.Controls.Add($logTextBox)

function Select-CredentialTabForMode {
    if ($script:modeComboBox.SelectedItem -eq "Conta Real") {
        $credentialsTabControl.SelectedTab = $realCredentialsTab
        return
    }

    $credentialsTabControl.SelectedTab = $testnetCredentialsTab
}

function Refresh-UiState {
    $forceHeavyRefresh = [bool]$script:UiHeavyRefreshRequested
    $script:UiHeavyRefreshRequested = $false

    $dashboardOnline = Test-DashboardOnline
    $botState = Get-BotState -ForceRuntimeRefresh:$forceHeavyRefresh
    $runtimeStateRaw = Get-BotRuntimeDatabaseState -ForceRefresh:$forceHeavyRefresh
    $runtimeState = if ($botState.Running) { $runtimeStateRaw } else { $null }
    $preflight = Get-LiveModePreflight -UseTestnet $false -ForceRefresh:$forceHeavyRefresh
    $testnetCredential = Get-RuntimeCredentialData -UseTestnet $true
    $realCredential = Get-RuntimeCredentialData -UseTestnet $false
    $selectedUseTestnet = $script:modeComboBox.SelectedItem -ne "Conta Real"
    $selectedCredential = if ($selectedUseTestnet) { $testnetCredential } else { $realCredential }
    $selectedSymbol = Get-SelectedBotSymbol
    $preferredTimeframe = Get-SymbolPreferredTimeframe -Symbol $selectedSymbol

    $dashboardStatusLabel.Text = "Dashboard Streamlit: " + ($(if ($dashboardOnline) { "ON | " + $script:DashboardUrl } else { "OFF" }))
    $dashboardStatusLabel.ForeColor = [System.Drawing.Color]::Black
    if ($botState.Running) {
        $runtimeSuffix = ""
        if ($null -ne $runtimeState) {
            $runtimeSymbolSuffix = ""
            if (-not [string]::IsNullOrWhiteSpace([string]$runtimeState.symbol) -and -not [string]::IsNullOrWhiteSpace([string]$runtimeState.timeframe)) {
                $runtimeSymbolSuffix = " | $($runtimeState.symbol) $($runtimeState.timeframe)"
            }
            $runtimeSuffix = "$runtimeSymbolSuffix | env=$($runtimeState.environment) | status=$($runtimeState.status)"
        }
        $botStatusLabel.Text = "Bot Direto: ON | PID $($botState.Pid) | modo $($botState.ModeLabel)$runtimeSuffix"
    }
    else {
        $botStatusLabel.Text = "Bot Direto: OFF"
    }
    $botStatusLabel.ForeColor = [System.Drawing.Color]::Black
    if ($null -ne $runtimeState -and -not [string]::IsNullOrWhiteSpace([string]$runtimeState.last_heartbeat_at)) {
        $metaStatusLabel.Text = "Build: $($script:UiBuildStamp) | Origem: $($botState.Source) | started_at: $($botState.StartedAt) | heartbeat: $($runtimeState.last_heartbeat_at)"
    }
    else {
        $metaStatusLabel.Text = "Build: $($script:UiBuildStamp) | Origem: $($botState.Source) | started_at: $($botState.StartedAt)"
    }
    $metaStatusLabel.ForeColor = [System.Drawing.Color]::Black

    if ($null -ne $testnetCredential) {
        $testnetCredentialStatusLabel.Text = "Status: ativo via $($testnetCredential.SourceLabel) | key: $($testnetCredential.ApiKeyMasked)"
    }
    else {
        $testnetCredentialStatusLabel.Text = "Status: paper/testnet pronto sem chave. Preencha aqui so se quiser validar credencial da exchange."
    }

    if ($null -ne $realCredential) {
        $realCredentialStatusLabel.Text = "Status: ativo via $($realCredential.SourceLabel) | key: $($realCredential.ApiKeyMasked)"
    }
    else {
        $realCredentialStatusLabel.Text = "Status: sem credencial configurada neste slot."
    }

    $symbolHintLabel.Text = "Perfil: $selectedSymbol $preferredTimeframe"

    if ($null -eq $preflight) {
        $livePreflightStatusLabel.Text = "Conta real: preflight indisponivel"
        $livePreflightStatusLabel.ForeColor = [System.Drawing.Color]::DarkRed
        if ([string]::IsNullOrWhiteSpace($script:LastLivePreflightError)) {
            $livePreflightTextBox.Text = "Nao foi possivel montar o perfil de live pequeno nesta sessao."
        }
        else {
            $livePreflightTextBox.Text = "Nao foi possivel montar o perfil de live pequeno nesta sessao." + [Environment]::NewLine + "Detalhe: $($script:LastLivePreflightError)"
        }
    }
    else {
        $realReady = (
            $preflight.live_enabled -and
            $preflight.confirmation_ok -and
            $preflight.symbol_ready -and
            ($null -ne $realCredential) -and
            ([double]$preflight.risk_per_trade_pct -le [double]$preflight.max_real_risk_per_trade_pct_start)
        )

        if ($realReady) {
            $livePreflightStatusLabel.Text = "Conta real: pronta para operar"
            $livePreflightStatusLabel.ForeColor = [System.Drawing.Color]::DarkGreen
        }
        else {
            $livePreflightStatusLabel.Text = "Conta real: ainda bloqueada por seguranca"
            $livePreflightStatusLabel.ForeColor = [System.Drawing.Color]::DarkRed
        }

        $livePreflightTextBox.Text = Format-LivePreflightSummary -Preflight $preflight -CredentialData $realCredential
    }

    if ($script:modeComboBox.SelectedItem -eq "Conta Real") {
        if ($null -eq $preflight) {
            if ([string]::IsNullOrWhiteSpace($script:LastLivePreflightError)) {
                $modeHintLabel.Text = "Preflight real indisponivel"
            }
            else {
                $modeHintLabel.Text = "Preflight real indisponivel | $($script:LastLivePreflightError)"
            }
            $modeHintLabel.ForeColor = [System.Drawing.Color]::DarkRed
        }
        elseif ($preflight.live_enabled -and $preflight.confirmation_ok -and $preflight.symbol_ready -and $null -ne $realCredential -and ([double]$preflight.risk_per_trade_pct -le [double]$preflight.max_real_risk_per_trade_pct_start)) {
            $modeHintLabel.Text = "Conta real pronta para operar | credencial: $($realCredential.SourceLabel)"
            $modeHintLabel.ForeColor = [System.Drawing.Color]::DarkGreen
        }
        else {
            if (-not $preflight.symbol_ready) {
                $modeHintLabel.Text = "Conta real bloqueada | simbolo ainda nao aprovado"
            }
            elseif ($null -eq $realCredential) {
                $modeHintLabel.Text = "Conta real bloqueada | faltam API Key e Secret"
            }
            else {
                $modeHintLabel.Text = "Conta real bloqueada por seguranca"
            }
            $modeHintLabel.ForeColor = [System.Drawing.Color]::DarkRed
        }
        $startBotButton.Text = "Ligar Bot Real"
    }
    else {
        if ($null -ne $selectedCredential) {
            $modeHintLabel.Text = "Modo seguro recomendado | credencial: $($selectedCredential.SourceLabel)"
        }
        else {
            $modeHintLabel.Text = "Modo seguro recomendado | paper/testnet pronto"
        }
        $modeHintLabel.ForeColor = [System.Drawing.Color]::FromArgb(20, 90, 150)
        $startBotButton.Text = "Ligar Bot Direto"
    }

    $previousSelectionStart = $logTextBox.SelectionStart
    $logTail = Get-LogTail -Path $script:BotExecutionLogPath -Lines 240
    if ([string]::IsNullOrWhiteSpace($logTail)) {
        $logTextBox.Text = "Sem log operacional ainda."
    }
    else {
        if (-not $botState.Running) {
            $logTail = "[bot parado] exibindo ultimo log salvo." + [Environment]::NewLine + [Environment]::NewLine + $logTail
        }
        $logTextBox.Text = $logTail
        if ($autoFollowLogCheckBox.Checked) {
            $logTextBox.SelectionStart = $logTextBox.TextLength
            $logTextBox.ScrollToCaret()
        }
        else {
            $restoreSelection = [Math]::Min($previousSelectionStart, $logTextBox.TextLength)
            $logTextBox.SelectionStart = $restoreSelection
            $logTextBox.SelectionLength = 0
        }
    }
}

function Invoke-AutoStartIfRequested {
    if ($script:AutoStartExecuted) {
        return
    }

    if ([string]::IsNullOrWhiteSpace($script:AutoStartMode)) {
        return
    }

    $script:AutoStartExecuted = $true

    switch ($script:AutoStartMode) {
        "testnet" {
            $script:modeComboBox.SelectedItem = "Testnet"
            Start-BotDirect -UseTestnet $true
            Refresh-UiState
        }
        "real" {
            $script:modeComboBox.SelectedItem = "Conta Real"
            Start-BotDirect -UseTestnet $false
            Refresh-UiState
        }
        default {
            return
        }
    }
}

$openDashboardButton.Add_Click({
    Invoke-UiSafely -Context "Abrir Streamlit" -Action {
        Request-UiHeavyRefresh
        Start-DashboardUi
        Refresh-UiState
    } -ShowMessage
})

$startBotButton.Add_Click({
    Invoke-UiSafely -Context "Ligar Bot Direto" -Action {
        $useTestnet = $script:modeComboBox.SelectedItem -ne "Conta Real"
        Request-UiHeavyRefresh
        Start-BotDirect -UseTestnet $useTestnet
        Refresh-UiState
    } -ShowMessage
})

$stopBotButton.Add_Click({
    Invoke-UiSafely -Context "Parar Bot" -Action {
        Request-UiHeavyRefresh
        Stop-BotDirect
        Refresh-UiState
    } -ShowMessage
})

$refreshButton.Add_Click({
    Invoke-UiSafely -Context "Atualizar Interface" -Action {
        Request-UiHeavyRefresh
        Refresh-UiState
    }
})

$script:modeComboBox.Add_SelectedIndexChanged({
    Invoke-UiSafely -Context "Trocar Modo" -Action {
        Save-LauncherUiSelection
        Select-CredentialTabForMode
        Request-UiHeavyRefresh
        Refresh-UiState
    }
})

$script:symbolComboBox.Add_SelectedIndexChanged({
    Invoke-UiSafely -Context "Trocar Ativo" -Action {
        Save-LauncherUiSelection
        Request-UiHeavyRefresh
        Refresh-UiState
    }
})

$openLogButton.Add_Click({
    Invoke-UiSafely -Context "Abrir Log" -Action {
        Open-BotLog
    } -ShowMessage
})

$saveTestnetCredentialsButton.Add_Click({
    $apiKey = [string]$testnetApiKeyTextBox.Text
    $apiSecret = [string]$testnetApiSecretTextBox.Text
    if ([string]::IsNullOrWhiteSpace($apiKey) -or [string]::IsNullOrWhiteSpace($apiSecret)) {
        [System.Windows.Forms.MessageBox]::Show(
            "Preencha API Key e API Secret da Testnet para salvar na sessao.",
            "Credenciais Testnet",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
        return
    }

    Invoke-UiSafely -Context "Salvar Credenciais Testnet" -Action {
        Save-RuntimeCredentials -UseTestnet $true -ApiKey $apiKey -ApiSecret $apiSecret
        $testnetApiKeyTextBox.Clear()
        $testnetApiSecretTextBox.Clear()
        Request-UiHeavyRefresh
        Refresh-UiState
    } -ShowMessage
})

$clearTestnetCredentialsButton.Add_Click({
    Invoke-UiSafely -Context "Limpar Credenciais Testnet" -Action {
        Clear-RuntimeCredentials -UseTestnet $true
        $testnetApiKeyTextBox.Clear()
        $testnetApiSecretTextBox.Clear()
        Request-UiHeavyRefresh
        Refresh-UiState
    } -ShowMessage
})

$showTestnetSecretsCheckBox.Add_CheckedChanged({
    $showSecrets = [bool]$showTestnetSecretsCheckBox.Checked
    $testnetApiKeyTextBox.UseSystemPasswordChar = -not $showSecrets
    $testnetApiSecretTextBox.UseSystemPasswordChar = -not $showSecrets
})

$saveRealCredentialsButton.Add_Click({
    $apiKey = [string]$realApiKeyTextBox.Text
    $apiSecret = [string]$realApiSecretTextBox.Text
    if ([string]::IsNullOrWhiteSpace($apiKey) -or [string]::IsNullOrWhiteSpace($apiSecret)) {
        [System.Windows.Forms.MessageBox]::Show(
            "Preencha API Key e API Secret da conta real para salvar na sessao.",
            "Credenciais Conta Real",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
        return
    }

    Invoke-UiSafely -Context "Salvar Credenciais Conta Real" -Action {
        Save-RuntimeCredentials -UseTestnet $false -ApiKey $apiKey -ApiSecret $apiSecret
        $realApiKeyTextBox.Clear()
        $realApiSecretTextBox.Clear()
        Request-UiHeavyRefresh
        Refresh-UiState
    } -ShowMessage
})

$clearRealCredentialsButton.Add_Click({
    Invoke-UiSafely -Context "Limpar Credenciais Conta Real" -Action {
        Clear-RuntimeCredentials -UseTestnet $false
        $realApiKeyTextBox.Clear()
        $realApiSecretTextBox.Clear()
        Request-UiHeavyRefresh
        Refresh-UiState
    } -ShowMessage
})

$showRealSecretsCheckBox.Add_CheckedChanged({
    $showSecrets = [bool]$showRealSecretsCheckBox.Checked
    $realApiKeyTextBox.UseSystemPasswordChar = -not $showSecrets
    $realApiSecretTextBox.UseSystemPasswordChar = -not $showSecrets
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 8000
$timer.Add_Tick({
    Invoke-UiSafely -Context "Timer Refresh" -Action {
        Refresh-UiState
    }
})
$timer.Start()

$form.Add_Shown({
    Invoke-UiSafely -Context "Abrir Interface" -Action {
        Select-CredentialTabForMode
        $metaStatusLabel.Text = "Build: $($script:UiBuildStamp)"
        Request-UiHeavyRefresh
        Refresh-UiState
    } -ShowMessage
})

try {
    Close-OtherInterfaceWindows
    [void]$form.ShowDialog()
}
catch {
    $summary = if ($null -ne $_.Exception -and -not [string]::IsNullOrWhiteSpace([string]$_.Exception.Message)) {
        [string]$_.Exception.Message
    }
    else {
        [string]$_
    }

    Write-LauncherErrorLog -Context "ShowDialog" -Message $summary -ErrorRecord $_
    [System.Windows.Forms.MessageBox]::Show(
        "A interface nao conseguiu permanecer aberta." + [Environment]::NewLine + "Detalhe: " + $summary + [Environment]::NewLine + [Environment]::NewLine + "Veja logs\\desktop_launcher_error.log.",
        "Evo Coin Bot",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
}
