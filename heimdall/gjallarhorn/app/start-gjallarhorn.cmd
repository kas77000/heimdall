@echo off
rem Start Gjallarhorn now (after it was stopped). It must be installed: install-gjallarhorn.cmd.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gjallarhorn.ps1" -Start
echo.
pause
