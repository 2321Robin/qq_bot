import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_powershell_scripts_parse() -> None:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        import pytest

        pytest.skip("powershell.exe is not available")

    for script_name in ("start_all.ps1", "stop_all.ps1"):
        script_path = ROOT / script_name
        command = (
            "$errors = $null; "
            "$null = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{script_path}', [ref]$null, [ref]$errors); "
            "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }"
        )

        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr


def test_start_all_script_contains_required_startup_targets() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "C:\\Users\\Robin\\Documents\\GitHub\\qq_bot" in script
    assert "C:\\Users\\Robin\\Documents\\NapCatQQ\\NapCat.44498.Shell" in script
    assert "NapCatWinBootMain.exe" in script
    assert "2381444078" in script
    assert "http://127.0.0.1:6099/webui?token=8ebcfb93900e" in script
    assert "8081" in script
    assert "Get-NetTCPConnection" in script
    assert "Get-BotProcess" in script
    assert "Get-NapCatProcess" in script
    assert "bot.py" in script
    assert "Start-Process $WebUiUrl" not in script
    assert "chcp 65001" in script
    assert "OutputEncoding" in script
    assert "powershell.exe" in script
    assert "-WindowStyle Hidden" in script
    assert "-NoExit" not in script
    assert "Stop-Process -Id" in script
    assert "Restarting bot backend" in script
    assert "logs\\startup" in script


def test_start_all_script_does_not_keep_stale_backend_process() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    main_script = script[script.index('Write-Host "Checking startup files..."') :]
    assert "Warning: bot.py is running but port $BotPort is not listening." not in main_script
    assert "Stop-BotProcesses -Processes $botProcesses" in main_script
    assert "Start-BotBackend" in main_script
    assert main_script.index("Stop-BotProcesses -Processes $botProcesses") < main_script.index("Start-BotBackend")


def test_start_all_script_restarts_when_multiple_bot_processes_exist() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "$botProcesses.Count -gt 0" in script
    assert "Stopping existing bot backend before fresh startup." in script


def test_start_all_script_restarts_bot_after_code_changes() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "Restarting bot backend to load current code." in script


def test_start_all_script_stops_existing_services_before_starting() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "Stopping existing bot backend before fresh startup." in script
    assert "Stopping existing NapCat before fresh startup." in script
    assert "Stop-BotProcesses -Processes $botProcesses" in script
    assert "Stop-NapCatProcesses -Processes $napCatProcesses" in script
    main_script = script[script.index('Write-Host "Checking startup files..."') :]
    assert main_script.index("Stop-BotProcesses -Processes $botProcesses") < main_script.index("Start-BotBackend")
    assert main_script.index("Stop-NapCatProcesses -Processes $napCatProcesses") < main_script.index("Start-BotBackend")
    assert main_script.index("Stop-NapCatProcesses -Processes $napCatProcesses") < main_script.index("Start-NapCat")


def test_batch_entrypoint_invokes_hidden_powershell_script() -> None:
    script = (ROOT / "一键启动.bat").read_text(encoding="utf-8")

    assert "start_all.ps1" in script
    assert 'start "" powershell.exe' in script
    assert "-ExecutionPolicy Bypass" in script
    assert "-WindowStyle Hidden" in script
    assert "%~dp0" in script
    assert "chcp 65001" in script
    assert "pause" not in script.lower()


def test_stop_all_script_stops_bot_and_napcat() -> None:
    script = (ROOT / "stop_all.ps1").read_text(encoding="utf-8")

    assert "C:\\Users\\Robin\\Documents\\GitHub\\qq_bot" in script
    assert "C:\\Users\\Robin\\Documents\\NapCatQQ\\NapCat.44498.Shell" in script
    assert "NapCatWinBootMain" in script
    assert "QQ" in script
    assert "Get-BotProcess" in script
    assert "Get-NapCatProcess" in script
    assert "Stop-BotProcesses" in script
    assert "Stop-NapCatProcesses" in script
    assert "Stop-Process -Id" in script


def test_bot_process_matching_is_boundary_safe() -> None:
    for script_name in ("start_all.ps1", "stop_all.ps1"):
        script = (ROOT / script_name).read_text(encoding="utf-8")

        assert "[regex]::Escape($ScriptPath)" in script
        assert "$scriptPattern" in script
        assert ".Contains($normalizedScriptPath)" not in script


def test_stop_helpers_continue_if_process_exits_first() -> None:
    for script_name in ("start_all.ps1", "stop_all.ps1"):
        script = (ROOT / script_name).read_text(encoding="utf-8")

        assert "try {" in script
        assert "catch" in script
        assert "Warning: failed to stop" in script


def test_stop_batch_entrypoint_invokes_stop_script() -> None:
    script = (ROOT / "停止.bat").read_text(encoding="utf-8")

    assert "stop_all.ps1" in script
    assert "-ExecutionPolicy Bypass" in script
    assert "%~dp0" in script
    assert "chcp 65001" in script
    assert "pause" in script.lower()
    assert 'set "exitcode=%errorlevel%"' in script
    assert script.index("popd") < script.index("pause")
    assert "exit /b %exitcode%" in script
