@echo off
rem Quit Gjallarhorn now. It starts again at your next logon, or with start-gjallarhorn.cmd.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gjallarhorn.ps1" -Stop
echo.
pause
