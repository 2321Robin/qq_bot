<#
.SYNOPSIS
    Starts the QQ bot backend and NapCatQQ using local configuration.

.DESCRIPTION
    Loads startup configuration from startup.local.ps1 (or a custom path),
    validates all paths and parameters, then starts the bot and NapCat
    as hidden background processes.

.PARAMETER ConfigPath
    Path to the startup configuration file. Defaults to startup.local.ps1
    in the script directory.

.PARAMETER ValidateOnly
    When set, only validates the configuration without starting or stopping
    any processes, creating log directories, or accessing the network.
    Exit code is 0 on success, non-zero on failure.
#>
param(
    [string]$ConfigPath = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# ---- Path derivation ----
$ProjectDir = $PSScriptRoot
if ($ConfigPath -eq "") {
    $ConfigPath = Join-Path $ProjectDir "startup.local.ps1"
}
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$BotScript = Join-Path $ProjectDir "bot.py"

# ---- Configuration loading ----
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    Write-Host "ERROR: Configuration file not found. Check the -ConfigPath parameter."
    Write-Host "Copy startup.example.ps1 to startup.local.ps1 and fill in your local values."
    exit 1
}

. $ConfigPath

if (-not $StartupConfig) {
    Write-Host "ERROR: Configuration file did not define `$StartupConfig."
    Write-Host "Check startup.example.ps1 for the required format."
    exit 1
}

# ---- Validation helpers ----
function Assert-ConfigKey {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Config,
        [Parameter(Mandatory = $true)]
        [string]$Key,
        [Parameter(Mandatory = $true)]
        [string]$ErrorMessage
    )

    if (-not $Config.ContainsKey($Key) -or [string]::IsNullOrEmpty($Config[$Key])) {
        Write-Host "ERROR: $ErrorMessage"
        Write-Host "Check the '$Key' setting in your startup configuration."
        exit 1
    }
}

function Assert-ConfigFileExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Key
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Host "ERROR: File configured in '$Key' was not found. Check the path in your startup configuration."
        exit 1
    }
}

function Assert-ConfigDirectoryExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Key
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Write-Host "ERROR: Directory configured in '$Key' was not found. Check the path in your startup configuration."
        exit 1
    }
}

# ---- Validate required configuration keys ----
Assert-ConfigKey -Config $StartupConfig -Key "NapCatDir" -ErrorMessage "NapCatQQ directory (NapCatDir) is not configured."
Assert-ConfigKey -Config $StartupConfig -Key "NapCatAccount" -ErrorMessage "NapCatQQ account (NapCatAccount) is not configured."
Assert-ConfigKey -Config $StartupConfig -Key "BotPort" -ErrorMessage "Bot port (BotPort) is not configured."

# ---- Validate NapCatDir exists and contains the boot executable ----
$NapCatDir = $StartupConfig["NapCatDir"]
$NapCatExe = Join-Path $NapCatDir "NapCatWinBootMain.exe"
Assert-ConfigDirectoryExists -Path $NapCatDir -Key "NapCatDir"
Assert-ConfigFileExists -Path $NapCatExe -Key "NapCatDir"

# ---- Validate NapCatAccount is a numeric string ----
$NapCatAccount = $StartupConfig["NapCatAccount"]
if ($NapCatAccount -notmatch '^\d+$') {
    Write-Host "ERROR: NapCatAccount must be a numeric string (QQ account number)."
    exit 1
}

# ---- Validate BotPort ----
$BotPort = $StartupConfig["BotPort"]
if ($BotPort -notmatch '^\d+$' -or [int]$BotPort -lt 1 -or [int]$BotPort -gt 65535) {
    Write-Host "ERROR: BotPort must be an integer between 1 and 65535."
    exit 1
}
$BotPort = [int]$BotPort

# ---- Validate Python and bot.py exist ----
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    Write-Host "ERROR: Python executable not found. Ensure the virtual environment exists at .venv\ in the project directory."
    exit 1
}

if (-not (Test-Path -LiteralPath $BotScript -PathType Leaf)) {
    Write-Host "ERROR: Bot script (bot.py) not found in the project directory."
    exit 1
}

# ---- All validations passed; stop here if ValidateOnly ----
if ($ValidateOnly) {
    Write-Host "Configuration is valid."
    exit 0
}

# ---- Process management functions ----
function Assert-FileExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function Assert-DirectoryExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label not found: $Path"
    }
}

function Test-PortListening {
    param([int]$Port)

    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-BotProcess {
    param([string]$ScriptPath)

    $escapedScriptPath = [regex]::Escape($ScriptPath)
    $scriptPattern = '(^|\s)"?' + $escapedScriptPath + '"?(\s|$)'
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine -match $scriptPattern
    })
}

function Get-BotConnection {
    param([int]$Port)

    $localConnections = @(Get-NetTCPConnection -LocalPort $Port -State Established -ErrorAction SilentlyContinue)
    $remoteConnections = @(Get-NetTCPConnection -RemotePort $Port -State Established -ErrorAction SilentlyContinue)
    return @($localConnections + $remoteConnections)
}

function Get-NapCatProcess {
    param([string]$Directory)

    $directoryPrefix = [System.IO.Path]::GetFullPath($Directory).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    return @(Get-Process -Name NapCatWinBootMain,QQ -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [System.IO.Path]::GetFullPath($_.Path).StartsWith($directoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    })
}

function Start-BotBackend {
    Write-Host "Starting bot backend on port $BotPort..."
    $botCommand = "chcp 65001 > `$null; [Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '$ProjectDir'; & '$PythonExe' '$BotScript'"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $botCommand -WorkingDirectory $ProjectDir -WindowStyle Hidden -RedirectStandardOutput $BotStdoutLog -RedirectStandardError $BotStderrLog
    Start-Sleep -Seconds 5
}

function Start-NapCat {
    Write-Host "Starting NapCat..."
    $napCatCommand = "chcp 65001 > `$null; [Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '$NapCatDir'; & '$NapCatExe' $NapCatAccount"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $napCatCommand -WorkingDirectory $NapCatDir -WindowStyle Hidden -RedirectStandardOutput $NapCatStdoutLog -RedirectStandardError $NapCatStderrLog
    Start-Sleep -Seconds 8
}

function Stop-BotProcesses {
    param([object[]]$Processes)

    foreach ($process in $Processes) {
        try {
            Stop-Process -Id $process.ProcessId -Force
        } catch {
            Write-Host "Warning: failed to stop bot process $($process.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Stop-NapCatProcesses {
    param([object[]]$Processes)

    foreach ($process in $Processes) {
        try {
            Stop-Process -Id $process.Id -Force
        } catch {
            Write-Host "Warning: failed to stop NapCat process $($process.Id): $($_.Exception.Message)"
        }
    }
}

# ---- Log directory ----
$StartupLogDir = Join-Path $ProjectDir "logs\startup"
$BotStdoutLog = Join-Path $StartupLogDir "bot.out.log"
$BotStderrLog = Join-Path $StartupLogDir "bot.err.log"
$NapCatStdoutLog = Join-Path $StartupLogDir "napcat.out.log"
$NapCatStderrLog = Join-Path $StartupLogDir "napcat.err.log"

Write-Host "Checking startup files..."
Assert-DirectoryExists -Path $ProjectDir -Label "Project directory"
Assert-FileExists -Path $PythonExe -Label "Python executable"
Assert-FileExists -Path $BotScript -Label "Bot script"
Assert-DirectoryExists -Path $NapCatDir -Label "NapCat directory"
Assert-FileExists -Path $NapCatExe -Label "NapCat executable"

New-Item -ItemType Directory -Path $StartupLogDir -Force | Out-Null

$botProcesses = @(Get-BotProcess -ScriptPath $BotScript)
if ($botProcesses.Count -gt 0) {
    Write-Host "Stopping existing bot backend before fresh startup."
    Stop-BotProcesses -Processes $botProcesses
    Start-Sleep -Seconds 2
}

$napCatProcesses = @(Get-NapCatProcess -Directory $NapCatDir)
if ($napCatProcesses.Count -gt 0) {
    Write-Host "Stopping existing NapCat before fresh startup."
    Stop-NapCatProcesses -Processes $napCatProcesses
    Start-Sleep -Seconds 2
}

if (Test-PortListening -Port $BotPort) {
    Write-Host "Restarting bot backend because port $BotPort is still listening."
}

Write-Host "Restarting bot backend to load current code."
Start-BotBackend

Start-NapCat

Write-Host "Waiting for NapCat to connect to bot backend..."
$connected = $false
for ($i = 1; $i -le 12; $i++) {
    $connections = @(Get-BotConnection -Port $BotPort)
    if ($connections.Count -gt 0) {
        $connected = $true
        break
    }
    Start-Sleep -Seconds 5
}

if ($connected) {
    Write-Host "Connected: NapCat has an Established connection to port $BotPort."
    Write-Host "You can test in the QQ group with: /ping"
} else {
    Write-Host "Warning: no Established connection to port $BotPort yet."
    Write-Host "Keep NapCat logged in, then check the OneBot reverse WebSocket config."
    Write-Host "Expected URL: ws://127.0.0.1:$BotPort/onebot/v11/ws"
}
