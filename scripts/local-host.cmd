@echo off
rem Host Heimdall on this PC. Usage: local-host.cmd setup ^| status ^| start ^| stop ^| restart ^| firewall ^| remove
rem Double-clicking it runs "setup". See docs\LOCAL-HOSTING.md.
setlocal
set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=setup"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-host.ps1" %ACTION%
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
