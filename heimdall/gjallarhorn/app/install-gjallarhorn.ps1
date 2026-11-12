<#
Sets Gjallarhorn up for the current Windows user: no admin rights, nothing installed.
The .cmd files next to it do the same with a double-click:

    install-gjallarhorn.cmd     asks for the shared folder, then installs
    start-gjallarhorn.cmd       starts it now
    stop-gjallarhorn.cmd        quits it now (it starts again at the next logon)
    uninstall-gjallarhorn.cmd   quits it and stops it starting at logon

    powershell -NoProfile -ExecutionPolicy Bypass -File install-gjallarhorn.ps1 -Root "\\server\share\Gjallarhorn"

Who gets which alerts is set in Bifrost (Settings, Gjallarhorn, Groups), by
Windows user name: nothing to set on the PC.

It adds a scheduled task that starts Gjallarhorn, hidden, at every logon, and a
"Gjallarhorn" entry in the Start menu that opens the list of alerts. Both run
the copy on the share, so publishing a new version from Bifrost updates
everyone at their next logon.

    -Root        the shared folder Heimdall writes the alerts to: the one in
                 Bifrost, Settings, Gjallarhorn (default: the folder above this script's)
    -Desk        optional, the old way: desks this PC is for, comma separated
                 ("Equities,Rates"); an alert for one of these names pops up here too
    -Start       start it now (it must be installed)
    -Stop        quit it now; it starts again at the next logon
    -Uninstall   quit it, remove the logon task and the Start menu entry
#>
param(
    [string]$Desk = "",
    [string]$Root = "",
    [switch]$Start,
    [switch]$Stop,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "Heimdall Gjallarhorn"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Gjallarhorn.lnk"

function Stop-Gjallarhorn {
    $n = 0
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*gjallarhorn.ps1*" -and $_.ProcessId -ne $PID } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
    return $n
}

function Fail([string]$msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    [void](Stop-Gjallarhorn)
    if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force }
    Write-Host "Gjallarhorn is stopped and no longer starts at logon for $env:USERNAME."
    exit 0
}

if ($Stop) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $n = Stop-Gjallarhorn
    if ($n) { Write-Host "Gjallarhorn is stopped. It starts again at your next logon (or with start-gjallarhorn.cmd)." }
    else { Write-Host "Gjallarhorn was not running." }
    exit 0
}

if ($Start) {
    $task = Get-Task
    if (-not $task) {
        Fail "Gjallarhorn is not installed for $($env:USERNAME): run install-gjallarhorn.cmd first."
    }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Gjallarhorn is started: its horn sits by the clock."
    Write-Host "Reading $($task.Actions[0].Arguments -replace '.*-Root "([^"]*)".*', '$1')"
    exit 0
}

# -- install -------------------------------------------------------------------------------

$Root = $Root.Trim().Trim('"').Trim().TrimEnd('\')
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Desk = $Desk.Trim()
if (-not [IO.Directory]::Exists($Root)) {
    Fail "The shared folder $Root cannot be reached from this PC. Check the path (the one in Bifrost, Settings, Gjallarhorn) and that you can open it in Explorer."
}
# Run the copy on the share when Bifrost published it there, so new versions reach this PC.
$script = Join-Path $Root "app\gjallarhorn.ps1"
if (-not (Test-Path $script)) {
    $script = Join-Path $PSScriptRoot "gjallarhorn.ps1"
    if (-not (Test-Path $script)) { Fail "gjallarhorn.ps1 is neither in $Root\app nor next to this installer." }
    Write-Warning "The app is not published in $Root\app yet (Bifrost, Settings, Gjallarhorn, Publish the app). Using $script for now."
}
if (-not [IO.Directory]::Exists((Join-Path $Root "alerts"))) {
    Write-Warning "Heimdall has not written any alert in $Root yet. If a test pop-up from Bifrost does not show, check this is the folder set in Bifrost."
}
$common = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$script`" -Root `"$Root`""
if ($Desk) { $common += " -Desk `"$Desk`"" }

# A copy already running (maybe on another folder) would keep running: one per user.
[void](Stop-Gjallarhorn)

# At logon, hidden. Restarted if it ever stops.
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden $common"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Heimdall alerts as pop-ups on this screen" -Force | Out-Null

# Start menu: opens the list (and starts Gjallarhorn if it is not running).
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-WindowStyle Hidden $common -Open"
$lnk.WindowStyle = 7
$lnk.Description = "Heimdall alerts waiting to be acknowledged"
# The horn, not PowerShell's icon: drawn by Gjallarhorn itself into a local .ico.
$ico = Join-Path $env:LOCALAPPDATA "Gjallarhorn\gjallarhorn.ico"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -WriteIcon $ico
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Save()

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Root $Root -Test
Write-Host ""
Write-Host "Gjallarhorn is installed for $env:USERNAME$(if ($Desk) { " (desks: $Desk)" }) and starts at every logon."
Write-Host "Shared folder: $Root"
Write-Host "Windows user name: $env:USERNAME - in Bifrost (Settings, Gjallarhorn, Groups) it shows up"
Write-Host "within a few minutes, ready to be put in the groups whose alerts this trader gets."
Write-Host "A sample pop-up is showing at the bottom right. Its icon sits by the clock;"
Write-Host "'Gjallarhorn' in the Start menu opens the list of alerts."
