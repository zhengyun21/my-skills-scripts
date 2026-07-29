@echo off
rem Double-click entry for download.ps1 (no Python required)
rem Switch console to UTF-8 code page to avoid garbled Chinese output
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0download.ps1" %*
echo.
pause
