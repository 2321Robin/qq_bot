<#
.SYNOPSIS
    Stops the QQ bot backend and NapCatQQ processes.

.DESCRIPTION
    Loads startup configuration from startup.local.ps1 (or a custom path),
    validates the NapCat directory, then stops matching bot and NapCat
    processes. Does not require the Python virtual environment.

.PARAMETER ConfigPath
    Path to the startup configuration file. Defaults to startup.local.ps1
    in the script directory.

.PARAMETER ValidateOnly
    When set, only validates the configuration without stopping any
    processes. Exit code is 0 on success, non-zero on failure.
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

# ---- Validate NapCatDir ----
if (-not $StartupConfig.ContainsKey("NapCatDir") -or [string]::IsNullOrEmpty($StartupConfig["NapCatDir"])) {
    Write-Host "ERROR: NapCatQQ directory (NapCatDir) is not configured."
    exit 1
}

$NapCatDir = $StartupConfig["NapCatDir"]
if (-not (Test-Path -LiteralPath $NapCatDir -PathType Container)) {
    Write-Host "ERROR: Configured NapCatDir was not found. Check the path in your startup configuration."
    exit 1
}

# ---- All validations passed; stop here if ValidateOnly ----
if ($ValidateOnly) {
    Write-Host "Configuration is valid."
    exit 0
}

# ---- Process management functions ----
function Get-BotProcess {
    param([string]$ScriptPath)

    $escapedScriptPath = [regex]::Escape($ScriptPath)
    $scriptPattern = '(^|\s)"?' + $escapedScriptPath + '"?(\s|$)'
    return @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine -match $scriptPattern
    })
}

function Get-NapCatProcess {
    param([string]$Directory)

    $directoryPrefix = [System.IO.Path]::GetFullPath($Directory).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    return @(Get-Process -Name NapCatWinBootMain,QQ -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and [System.IO.Path]::GetFullPath($_.Path).StartsWith($directoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    })
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

$botProcesses = @(Get-BotProcess -ScriptPath $BotScript)
if ($botProcesses.Count -gt 0) {
    Write-Host "Stopping bot backend..."
    Stop-BotProcesses -Processes $botProcesses
} else {
    Write-Host "Bot backend is not running."
}

$napCatProcesses = @(Get-NapCatProcess -Directory $NapCatDir)
if ($napCatProcesses.Count -gt 0) {
    Write-Host "Stopping NapCat..."
    Stop-NapCatProcesses -Processes $napCatProcesses
} else {
    Write-Host "NapCat is not running."
}

Write-Host "Shutdown complete."
