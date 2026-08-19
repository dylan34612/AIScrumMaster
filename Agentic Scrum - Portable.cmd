@echo off
setlocal
cd /d "%~dp0"
echo.
echo Agentic Scrum - Portable Launcher
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_app.ps1"
echo.
pause
