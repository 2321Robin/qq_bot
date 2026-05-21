@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_all.ps1"
set "exitcode=%errorlevel%"
popd
echo.
pause
exit /b %exitcode%
