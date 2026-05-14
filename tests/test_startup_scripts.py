from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
    assert "bot.py" in script
    assert "Start-Process $WebUiUrl" not in script
    assert "chcp 65001" in script
    assert "OutputEncoding" in script
    assert "powershell.exe" in script
    assert "-NoExit" in script
    assert "Stop-Process -Id" in script
    assert "Restarting bot backend" in script


def test_start_all_script_does_not_keep_stale_backend_process() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    stale_process_branch = script[script.index("} elseif ($botProcesses.Count -gt 0) {") : script.index("} else {")]
    assert "Warning: bot.py is running but port $BotPort is not listening." not in stale_process_branch
    assert "Stop-BotProcesses -Processes $botProcesses" in stale_process_branch
    assert "Start-BotBackend" in stale_process_branch


def test_start_all_script_restarts_when_multiple_bot_processes_exist() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "$botProcesses.Count -gt 1" in script
    assert "Restarting bot backend because multiple bot.py processes exist." in script


def test_start_all_script_restarts_bot_after_code_changes() -> None:
    script = (ROOT / "start_all.ps1").read_text(encoding="utf-8")

    assert "Restarting bot backend to load current code." in script


def test_batch_entrypoint_invokes_powershell_script() -> None:
    script = (ROOT / "一键启动.bat").read_text(encoding="utf-8")

    assert "start_all.ps1" in script
    assert "-ExecutionPolicy Bypass" in script
    assert "%~dp0" in script
    assert "chcp 65001" in script
