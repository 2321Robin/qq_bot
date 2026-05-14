$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectDir = "C:\Users\Robin\Documents\GitHub\qq_bot"
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$BotScript = Join-Path $ProjectDir "bot.py"
$BotPort = 8081

$NapCatDir = "C:\Users\Robin\Documents\NapCatQQ\NapCat.44498.Shell"
$NapCatExe = Join-Path $NapCatDir "NapCatWinBootMain.exe"
$NapCatAccount = "2381444078"
$WebUiUrl = "http://127.0.0.1:6099/webui?token=8ebcfb93900e"

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

    $normalizedScriptPath = $ScriptPath.ToLowerInvariant()
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($normalizedScriptPath)
    })
}

function Get-BotConnection {
    param([int]$Port)

    $localConnections = @(Get-NetTCPConnection -LocalPort $Port -State Established -ErrorAction SilentlyContinue)
    $remoteConnections = @(Get-NetTCPConnection -RemotePort $Port -State Established -ErrorAction SilentlyContinue)
    return @($localConnections + $remoteConnections)
}

function Start-BotBackend {
    Write-Host "Starting bot backend on port $BotPort..."
    $botCommand = "chcp 65001 > `$null; [Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '$ProjectDir'; & '$PythonExe' '$BotScript'"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $botCommand -WorkingDirectory $ProjectDir
    Start-Sleep -Seconds 5
}

function Stop-BotProcesses {
    param([object[]]$Processes)

    foreach ($process in $Processes) {
        Stop-Process -Id $process.ProcessId -Force
    }
}

function Test-NapCatRunning {
    param([string]$Directory)

    $processes = @(Get-Process -Name NapCatWinBootMain,QQ -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -like "$Directory*"
    })
    return $processes.Count -gt 0
}

Write-Host "Checking startup files..."
Assert-DirectoryExists -Path $ProjectDir -Label "Project directory"
Assert-FileExists -Path $PythonExe -Label "Python executable"
Assert-FileExists -Path $BotScript -Label "Bot script"
Assert-DirectoryExists -Path $NapCatDir -Label "NapCat directory"
Assert-FileExists -Path $NapCatExe -Label "NapCat executable"

$botProcesses = @(Get-BotProcess -ScriptPath $BotScript)
if ($botProcesses.Count -gt 1) {
    Write-Host "Restarting bot backend because multiple bot.py processes exist."
    Stop-BotProcesses -Processes $botProcesses
    Start-BotBackend
} elseif (Test-PortListening -Port $BotPort) {
    Write-Host "Restarting bot backend to load current code."
    Stop-BotProcesses -Processes $botProcesses
    Start-BotBackend
} elseif ($botProcesses.Count -gt 0) {
    Write-Host "Bot backend process already exists. Waiting for port $BotPort..."
    for ($i = 1; $i -le 6; $i++) {
        if (Test-PortListening -Port $BotPort) {
            break
        }
        Start-Sleep -Seconds 5
    }

    if (-not (Test-PortListening -Port $BotPort)) {
        Write-Host "Restarting bot backend because existing bot.py process is not listening on port $BotPort."
        Stop-BotProcesses -Processes $botProcesses
        Start-BotBackend
    }
} else {
    Start-BotBackend
}

if (Test-NapCatRunning -Directory $NapCatDir) {
    Write-Host "NapCat is already running."
} else {
    Write-Host "Starting NapCat for account $NapCatAccount..."
    $napCatCommand = "chcp 65001 > `$null; [Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; `$OutputEncoding = [System.Text.Encoding]::UTF8; Set-Location -LiteralPath '$NapCatDir'; & '$NapCatExe' $NapCatAccount"
    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $napCatCommand -WorkingDirectory $NapCatDir
    Start-Sleep -Seconds 8
}

Write-Host "NapCat WebUI: $WebUiUrl"

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
    Write-Host "Keep NapCat logged in, then check the WebUI OneBot reverse WebSocket config."
    Write-Host "Expected URL: ws://127.0.0.1:$BotPort/onebot/v11/ws"
}
