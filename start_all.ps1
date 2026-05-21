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
$StartupLogDir = Join-Path $ProjectDir "logs\startup"
$BotStdoutLog = Join-Path $StartupLogDir "bot.out.log"
$BotStderrLog = Join-Path $StartupLogDir "bot.err.log"
$NapCatStdoutLog = Join-Path $StartupLogDir "napcat.out.log"
$NapCatStderrLog = Join-Path $StartupLogDir "napcat.err.log"

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
    Write-Host "Starting NapCat for account $NapCatAccount..."
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
