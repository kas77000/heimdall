<#
.SYNOPSIS
    Host Heimdall on this PC, as described in docs/LOCAL-HOSTING.md.

.DESCRIPTION
    local-host.cmd setup      install if needed, configure, keep the PC awake,
                              start Heimdall now and at every logon
    local-host.cmd install    only create .venv and install Heimdall
                              (both accept -Python C:\path\to\python.exe; Python 3.10+)
    local-host.cmd status     is it running, and the link to share
    local-host.cmd start      start it (after stop)
    local-host.cmd stop       stop it (it starts again at next logon)
    local-host.cmd restart    stop, then start (e.g. after editing heimdall.toml)
    local-host.cmd firewall   let other PCs reach Bifrost (asks for administrator)
    local-host.cmd remove     stop it and remove the logon task

    Nothing here needs administrator except "firewall".
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "install", "status", "start", "stop", "restart", "firewall", "remove", "help")]
    [string]$Action = "help",
    # Python 3.10+ to build .venv with, when it is not on PATH (setup / install)
    [string]$Python
)

$ErrorActionPreference = "Stop"
$Root     = Split-Path -Parent $PSScriptRoot          # the Heimdall checkout
$Py       = Join-Path $Root ".venv\Scripts\python.exe"
$Config   = Join-Path $Root "heimdall.toml"
$TaskName = "Heimdall"
$RuleName = "Heimdall Bifrost"
$Port     = 8501

function Say($msg)  { Write-Host "  $msg" }
function Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }

# Heimdall's own processes, and nothing else: an editor's Python tools, a notebook or
# another Streamlit app started from this folder must not be touched.
#   the logon task's loop      powershell ... -m heimdall all ...
#   the parts                  python -m heimdall all|huginn|sleipnir|bifrost, python -m heimdall.huginn|.sleipnir
#   Bifrost's Streamlit        python -m streamlit run ...\heimdall\bifrost\app.py
$HeimdallPattern = '-m\s+heimdall(\.huginn|\.sleipnir|\s+(all|huginn|sleipnir|bifrost))\b|streamlit\s+run\s+"?[^"]*\\heimdall\\bifrost\\app\.py'

function Get-HeimdallProcesses {
    $own = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe' OR Name='heimdall.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match $HeimdallPattern })
    # Whatever holds Huginn's or Bifrost's port is Heimdall too, even when Windows does not
    # let us read its command line (started from an administrator window, for example).
    $ids = @($own | ForEach-Object { $_.ProcessId })
    foreach ($c in @(Get-NetTCPConnection -State Listen -LocalPort 8765, $Port -ErrorAction SilentlyContinue)) {
        if ($ids -notcontains $c.OwningProcess) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
            if ($p -and $p.Name -match '^python') { $own += $p; $ids += $p.ProcessId }
        }
    }
    $own
}

function Get-ListeningPorts {
    @(Get-NetTCPConnection -State Listen -LocalPort 8765, $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty LocalPort -Unique)
}

function Test-Url($url) {
    try { (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $false }
}

function Get-ShareLink {
    $name = $env:COMPUTERNAME
    if (Test-Path $Config) {
        $m = Select-String -Path $Config -Pattern '^\s*url\s*=\s*"(http[^"]*:8501[^"]*)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    "http://${name}:$Port"
}

# --- setup steps -----------------------------------------------------------

function Test-PythonVersion($exe, [string[]]$pre = @()) {
    # True when this interpreter is Python 3.10 or newer.
    try { $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
    $parts = "$v".Trim().Split(".")
    [pscustomobject]@{ Version = "$v".Trim(); Ok = ([int]$parts[0] -gt 3) -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) }
}

function Find-BasePython {
    # -Python wins; then the py launcher; then python on PATH.
    $candidates = @()
    if ($Python) {
        if (-not (Test-Path $Python)) { throw "-Python: $Python does not exist." }
        $candidates += , @($Python)          # named explicitly: use it or stop, never fall back
    } else {
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @("py", "-3") }
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python") }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        $v = Test-PythonVersion $exe $pre
        if ($v -and $v.Ok) { return [pscustomobject]@{ Exe = $exe; Pre = $pre; Version = $v.Version } }
        if ($v) { Warn "$($c -join ' ') is Python $($v.Version); Heimdall needs 3.10 or newer" }
    }
    throw "No Python 3.10 or newer found. Install one from python.org, or pass its path: local-host.cmd setup -Python C:\path\to\python.exe"
}

function Install-Heimdall {
    if (Test-Path $Py) {
        & $Py -c "import heimdall.config, streamlit, uvicorn" 2>$null
        if ($LASTEXITCODE -eq 0) { Ok "Python environment ready (.venv, Python $((Test-PythonVersion $Py).Version))"; return }
    }
    if (-not (Test-Path $Py)) {
        $base = Find-BasePython
        Say "Creating .venv with Python $($base.Version)..."
        & $base.Exe @($base.Pre) -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Py)) { throw "Could not create .venv with $($base.Exe)." }
    }
    Say "Installing Heimdall into .venv (a few minutes the first time)..."
    & $Py -m pip install --quiet --disable-pip-version-check --upgrade pip
    & $Py -m pip install --quiet --disable-pip-version-check -e "$Root[kdb]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed; see the output above (behind a proxy? set HTTPS_PROXY first)." }
    & $Py -c "import heimdall.config, streamlit, uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Heimdall was installed but does not import; see the output above." }
    Ok "Heimdall installed (Python $((Test-PythonVersion $Py).Version))"
}

function New-Config {
    if (Test-Path $Config) { Ok "heimdall.toml already exists; left as it is"; return }
    $text = @"
[muninn]
path = "data/heimdall.db"

[huginn]
host = "127.0.0.1"          # only Bifrost and Sleipnir, on this PC, talk to Huginn
port = 8765
token_env = "HEIMDALL_TOKEN"
workers = 4
snapshot_rows = 200

[sleipnir]
url = "http://127.0.0.1:8765"
tick_secs = 15
run_grace_secs = 300

[bifrost]
url = "http://$($env:COMPUTERNAME):$Port"   # the link put in alert emails; must work from colleagues' PCs

"@
    [IO.File]::WriteAllText($Config, $text, (New-Object Text.UTF8Encoding $false))
    Ok "heimdall.toml created (link: http://$($env:COMPUTERNAME):$Port)"
}

function Set-KeepAwake {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Ok "PC set never to sleep or hibernate while plugged in"
    if (Get-CimInstance Win32_Battery) {
        Warn "Laptop: also set 'When I close the lid' to 'Do nothing' (plugged in), and keep it plugged in"
    }
}

function Register-HeimdallTask {
    if ($Root.Contains("'")) { throw "The folder path contains an apostrophe ('): move the Heimdall folder and run setup again." }
    $loop     = "Set-Location -LiteralPath '$Root'; while (`$true) { & '.\.venv\Scripts\python.exe' -m heimdall all; Start-Sleep 30 }"
    $action   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$loop`""
    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Ok "Logon task '$TaskName' registered: Heimdall starts when you log in, and restarts if it crashes"
}

# --- running ---------------------------------------------------------------

function Stop-Heimdall {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName
    }
    # The loop first, so it cannot start anything again; then each part with everything it
    # started (taskkill /T). Repeat until nothing is left, for up to 15 seconds.
    $deadline = (Get-Date).AddSeconds(15)
    $denied = @{}
    do {
        $procs = @(Get-HeimdallProcesses | Sort-Object { if ($_.Name -eq 'powershell.exe') { 0 } else { 1 } })
        foreach ($p in $procs) {
            # through cmd, so taskkill's stderr ("not found": a tree kill already took it)
            # comes back as text instead of a PowerShell error
            $out = cmd.exe /c "taskkill /F /T /PID $($p.ProcessId) 2>&1"
            if ("$out" -match 'Access is denied|Accès refusé') { $denied[$p.ProcessId] = $p }
        }
        if ($procs.Count) { Start-Sleep -Milliseconds 700 }
        $left = @(Get-HeimdallProcesses)
        $ports = @(Get-ListeningPorts)
    } while (($left.Count -or $ports.Count) -and (Get-Date) -lt $deadline)

    if (-not $left.Count -and -not $ports.Count) { Ok "Heimdall stopped"; return }

    Warn "Heimdall could not be stopped completely. Still running:"
    foreach ($p in $left) {
        $what = if ($p.CommandLine) { ($p.CommandLine -replace '\s+', ' ').Trim() } else { "(command line not readable)" }
        if ($what.Length -gt 110) { $what = $what.Substring(0, 107) + "..." }
        Say "  PID $($p.ProcessId)  $($p.Name)  $what"
    }
    if ($ports.Count) { Say "  Ports still in use: $($ports -join ', ')" }
    if ($denied.Count -or ($left | Where-Object { -not $_.CommandLine })) {
        Say "Windows refused to stop them: they were probably started from an administrator window."
        Say "Right-click stop-heimdall.cmd > Run as administrator, or close that window."
    }
    throw "Heimdall is not fully stopped (see above)."
}

function Start-Heimdall {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw "Not set up yet: run 'local-host.cmd setup' first."
    }
    if (@(Get-HeimdallProcesses).Count -gt 0) { Say "Heimdall is already running; stopping it first"; Stop-Heimdall }
    Start-ScheduledTask -TaskName $TaskName
    Say "Starting (up to a minute)..."
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ((Test-Url "http://127.0.0.1:8765/health") -and (Test-Url "http://localhost:$Port/_stcore/health")) { break }
        Start-Sleep 2
    }
    Show-Status
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host ""
    if ($task) { Say "Logon task:  $($task.State)" } else { Warn "Logon task:  not set up (run setup)" }
    $huginn  = Test-Url "http://127.0.0.1:8765/health"
    $bifrost = Test-Url "http://localhost:$Port/_stcore/health"
    if ($huginn)  { Ok "Huginn (service) answering" } else { Warn "Huginn (service) not answering; see data\logs\huginn.log" }
    if ($huginn) {
        try {
            $seen = (Invoke-RestMethod "http://127.0.0.1:8765/status" -TimeoutSec 3).engine.sleipnir_seen
            if ($seen -and ((Get-Date).ToUniversalTime() - ([DateTime]::Parse($seen)).ToUniversalTime()).TotalSeconds -lt 90) {
                Ok "Sleipnir (scheduler) on time"
            } else { Warn "Sleipnir (scheduler) not seen recently; see data\logs\sleipnir.log" }
        } catch { Warn "Could not read Huginn's status: $($_.Exception.Message)" }
    }
    if ($bifrost) { Ok "Bifrost (web app) answering" } else { Warn "Bifrost (web app) not answering; see data\logs\bifrost.log" }
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        Say "Firewall:    other PCs cannot connect yet (run 'local-host.cmd firewall' when they need to)"
    } else { Say "Firewall:    open to other PCs on port $Port" }
    Write-Host ""
    Say "On this PC:         http://localhost:$Port"
    Say "Link for others:    $(Get-ShareLink)"
    Write-Host ""
}

function Open-Firewall {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        Say "Asking Windows for administrator rights to add the firewall rule..."
        Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "firewall")
        if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) { Ok "Firewall rule '$RuleName' in place" }
        else { Warn "The firewall rule was not added (administrator rights refused?)" }
        return
    }
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Action Allow -Profile Domain, Private | Out-Null
    }
    Ok "Firewall rule '$RuleName' allows port $Port on Domain and Private networks"
    $public = @(Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq "Public" })
    foreach ($p in $public) {
        Warn "Network '$($p.InterfaceAlias)' is set to Public, where this rule does not apply. On the office network ask IT, or set it to Private if it is trusted."
    }
    Start-Sleep 5   # when run elevated, this window closes on its own: leave time to read it
}

# --- main ------------------------------------------------------------------

Write-Host ""
Write-Host "  Heimdall local hosting: $Action" -ForegroundColor Cyan
Write-Host ""
try {
    switch ($Action) {
        "setup" {
            Install-Heimdall
            New-Config
            New-Item -ItemType Directory -Force (Join-Path $Root "data\logs") | Out-Null
            Set-KeepAwake
            Register-HeimdallTask
            Start-Heimdall
            Say "Done. Heimdall now starts by itself whenever you log in."
        }
        "install"  { Install-Heimdall }
        "status"   { Show-Status }
        "start"    { Start-Heimdall }
        "stop"     { Stop-Heimdall }
        "restart"  { Stop-Heimdall; Start-Heimdall }
        "firewall" { Open-Firewall }
        "remove" {
            Stop-Heimdall
            if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            }
            Ok "Logon task removed. Your data (data\heimdall.db) and heimdall.toml are untouched."
            if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
                Say "The firewall rule remains; to remove it, in an administrator PowerShell:"
                Say "  Remove-NetFirewallRule -DisplayName '$RuleName'"
            }
        }
        default { Get-Help $PSCommandPath -Detailed | Select-Object -ExpandProperty Description | Out-String | Write-Host }
    }
} catch {
    Write-Host "  [x]  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    exit 1
}
