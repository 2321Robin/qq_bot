@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
pause
popd
