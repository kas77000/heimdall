<#
Sets Gjallarhorn up for the current Windows user: no admin rights, nothing installed.

    powershell -NoProfile -ExecutionPolicy Bypass -File \\server\share\Gjallarhorn\app\install-gjallarhorn.ps1 -Desk "Equities"

It adds a scheduled task that starts Gjallarhorn, hidden, at every logon, and a
"Gjallarhorn" entry in the Start menu that opens the list of alerts. Both run
the copy on the share, so publishing a new version from Bifrost updates
everyone at their next logon.

    -Desk        the desks this PC is for, comma separated ("Equities,Rates");
                 alerts meant for everyone, for this Windows user or for one
                 of these desks pop up here
    -Uninstall   remove the task and the Start menu entry
#>
param(
    [string]$Desk = "",
    [string]$Root = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TaskName = "Heimdall Gjallarhorn"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Gjallarhorn.lnk"

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*gjallarhorn.ps1*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force }
    Write-Host "Gjallarhorn removed for $env:USERNAME."
    exit 0
}

$script = Join-Path $PSScriptRoot "gjallarhorn.ps1"
if (-not (Test-Path $script)) { throw "gjallarhorn.ps1 is not next to this installer ($PSScriptRoot)." }
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$common = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$script`" -Root `"$Root`""
if ($Desk) { $common += " -Desk `"$Desk`"" }

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
Write-Host "Gjallarhorn is running for $env:USERNAME$(if ($Desk) { " (desks: $Desk)" })."
Write-Host "A sample pop-up is showing at the bottom right. Its icon sits by the clock;"
Write-Host "'Gjallarhorn' in the Start menu opens the list of alerts."
