@echo off
setlocal
rem Install Gjallarhorn for this Windows user: it starts at every logon and pops up
rem Heimdall's alerts. No admin rights, nothing to install. Double-click it, or:
rem
rem   install-gjallarhorn.cmd "\\server\share\Gjallarhorn" "Equities,Rates"
rem
rem The first value is the shared folder Heimdall writes the alerts to (the one in
rem Bifrost, Settings, Gjallarhorn); the second the desks this PC is for (optional).

for %%I in ("%~dp0..") do set "HERE=%%~fI"
set "ROOT=%~1"
set "DESK=%~2"
if not "%~1"=="" goto install

echo Gjallarhorn - Heimdall's alerts on this screen
echo.
echo The shared folder Heimdall writes the alerts to: the one in Bifrost,
echo Settings, Gjallarhorn. Press Enter to keep the one in brackets.
set /p "ROOT=Shared folder [%HERE%]: "
echo.
echo The desks this PC is for, comma separated (Equities,Rates). Alerts meant for
echo everyone, for %USERNAME% or for one of these desks pop up here. Enter for none.
set /p "DESK=Desks: "
echo.

:install
if "%ROOT%"=="" set "ROOT=%HERE%"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gjallarhorn.ps1" -Root "%ROOT%" -Desk "%DESK%"
if errorlevel 1 echo. & echo Gjallarhorn is NOT installed: see the message above.
echo.
pause
