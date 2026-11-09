@echo off
rem Host Heimdall on this PC. Usage: local-host.cmd setup ^| install ^| status ^| start ^| stop ^| restart ^| firewall ^| remove
rem setup and install accept -Python C:\path\to\python.exe (Python 3.10 or newer).
rem Double-clicking it runs "setup". See docs\LOCAL-HOSTING.md.
setlocal
if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-host.ps1" setup
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-host.ps1" %*
)
set "RC=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RC%
