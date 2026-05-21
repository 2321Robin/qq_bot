@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start_all.ps1"
popd
