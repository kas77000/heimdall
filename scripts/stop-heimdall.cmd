@echo off
rem Stop Heimdall on this PC. It starts again at your next logon, or with start-heimdall.cmd.
rem Double-click it. Same as: local-host.cmd stop
call "%~dp0local-host.cmd" stop
pause
