# One-Click Startup Script Design

## Goal

Add a Windows one-click startup flow for the local QQ bot setup.

## Scope

The startup flow should launch both required local components:

- NoneBot backend from `C:\Users\Robin\Documents\GitHub\qq_bot` using `.venv\Scripts\python.exe bot.py`.
- NapCat shell from `C:\Users\Robin\Documents\NapCatQQ\NapCat.44498.Shell` using `NapCatWinBootMain.exe 2381444078`.

It should also open the NapCat WebUI at `http://127.0.0.1:6099/webui?token=8ebcfb93900e` and report whether NapCat has established a connection to the backend on port `8081`.

## Files

- `start_all.ps1`: PowerShell implementation with validation, idempotent startup checks, WebUI opening, and connection-status reporting.
- `一键启动.bat`: double-click entry point that invokes `start_all.ps1` from the project root.
- `tests/test_startup_scripts.py`: lightweight regression tests for script presence and critical configured paths/commands.

## Behavior

The PowerShell script should:

1. Fail clearly if the project Python executable, `bot.py`, or NapCat executable is missing.
2. Start the backend only when port `8081` is not already listening.
3. Start NapCat only when the installed NapCat shell process is not already running.
4. Open the WebUI URL.
5. Poll for an established TCP connection involving local port `8081` and print a clear success or warning message.
6. Avoid stopping or modifying unrelated system QQ processes.

The batch file should:

1. Run from the project directory regardless of the current shell location.
2. Use PowerShell execution-policy bypass for this one script invocation.
3. Keep the console open so status and errors remain visible.

## Verification

- Run `pytest tests/test_startup_scripts.py -v`.
- Run the batch or PowerShell script manually and confirm it reports the backend/NapCat startup state.
- Verify `Get-NetTCPConnection -LocalPort 8081 -State Established` shows a connection after NapCat logs in.
