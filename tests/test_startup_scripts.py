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


def test_batch_entrypoint_invokes_powershell_script() -> None:
    script = (ROOT / "一键启动.bat").read_text(encoding="utf-8")

    assert "start_all.ps1" in script
    assert "-ExecutionPolicy Bypass" in script
    assert "%~dp0" in script
    assert "chcp 65001" in script
