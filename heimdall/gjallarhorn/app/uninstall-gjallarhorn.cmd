@echo off
rem Quit Gjallarhorn and stop it starting at logon (removes the logon task and the
rem Start menu entry). install-gjallarhorn.cmd puts it back.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gjallarhorn.ps1" -Uninstall
echo.
pause
