# Hosting Heimdall on your own machine

Until Heimdall has a server, it can run on one person's Windows PC (the
**host**) while everyone else uses it from their browser. Other users install
nothing: they open a link.

This guide covers only what is different about hosting on a PC. Installing,
connections, scripts, email and alerts are described in [SETUP.md](SETUP.md).

## Quick setup: one script does it all

`scripts\local-host.cmd` does sections 2, 3, 5, 6 and 7 for you. Double-click it,
or from the Heimdall folder:

```powershell
scripts\local-host.cmd setup
```

It creates `.venv` and installs Heimdall if they are missing, writes
`heimdall.toml` with this PC's name (an existing one is left as it is), stops
the PC sleeping while plugged in, registers the logon task, starts Heimdall, and
prints the link to share. No administrator rights needed.

Afterwards:

| Command | What it does |
|---|---|
| `scripts\local-host.cmd status` | Whether Huginn, Sleipnir and Bifrost are running, and the links |
| `scripts\local-host.cmd stop` | Stops Heimdall (it starts again at next logon) |
| `scripts\local-host.cmd start` | Starts it again |
| `scripts\local-host.cmd restart` | Stop then start, e.g. after editing `heimdall.toml` |
| `scripts\local-host.cmd firewall` | Lets other PCs connect (section 4); Windows asks for administrator |
| `scripts\local-host.cmd remove` | Stops it and removes the logon task; your data and config stay |

The rest of this guide explains each step, for doing it by hand or
understanding what the script did.

- [1. How it works](#1-how-it-works)
- [2. Install](#2-install)
- [3. Configure](#3-configure)
- [4. Open the firewall](#4-open-the-firewall)
- [5. Keep the PC awake](#5-keep-the-pc-awake)
- [6. Start Heimdall](#6-start-heimdall)
- [7. Start it automatically at logon](#7-start-it-automatically-at-logon)
- [8. Check from another PC](#8-check-from-another-pc)
- [9. Tell people how to connect](#9-tell-people-how-to-connect)
- [10. Back up](#10-back-up)
- [11. Things to know](#11-things-to-know)
- [12. Troubleshooting](#12-troubleshooting)
- [13. Moving to a server later](#13-moving-to-a-server-later)

---

## 1. How it works

```
 Colleagues' PCs                      Host PC (yours)
 ───────────────                      ───────────────
 browser ──── http://HOST:8501 ────►  Bifrost (Streamlit, port 8501)
 browser ───────────┘                    │ http://127.0.0.1:8765
                                         ▼
                                      Huginn (API + engine) ──► KDB, BbgRelay, SMTP
                                         ▲            │
                                      Sleipnir     Muninn (data\heimdall.db)
```

- All four parts run on the host PC.
- Only **Bifrost** is reachable from the network. Huginn listens on
  `127.0.0.1`, so no other machine can reach it directly and no API token is
  needed.
- The database stays on the host's local disk. **Do not put the Heimdall folder
  or the database on a shared drive**: SQLite on a network share can be
  corrupted, and users would need Python installed to run it from there.
- Alerts only run, and the page is only up, while the host PC is on, awake,
  logged in and on the network.

## 2. Install

Follow [SETUP.md §3](SETUP.md#3-install) on the host PC. Clone into a folder
on the local disk, for example `C:\heimdall`. The rest of this guide calls
that folder `C:\heimdall`; adjust the paths if yours is different.

## 3. Configure

Find the host PC's network name (and its address, as a fallback):

```powershell
hostname
ipconfig        # the "IPv4 Address" of the adapter on the office network
```

Create `heimdall.toml` in `C:\heimdall`:

```powershell
cd C:\heimdall
copy heimdall.example.toml heimdall.toml
```

and edit it so it contains:

```toml
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
url = "http://YOUR-PC-NAME:8501"   # the link put in alert emails; must work from colleagues' PCs
```

Replace `YOUR-PC-NAME` with the output of `hostname`. In `ipconfig`, ignore
adapters of a personal VPN (NordVPN's *NordLynx*, for example): colleagues
cannot reach that address. If colleagues cannot
reach the PC by name (section 8), use the IPv4 address instead. An address
handed out by DHCP can change, so ask IT for a fixed one or a DNS name if you
rely on it.

Bifrost (Streamlit) listens on all network interfaces by default, so nothing
else is needed for it to be reachable.

## 4. Open the firewall

Windows blocks incoming connections unless a rule allows them. In an
**administrator** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "Heimdall Bifrost" -Direction Inbound `
  -Protocol TCP -LocalPort 8501 -Action Allow -Profile Domain,Private
```

Only port 8501 is opened. Port 8765 (Huginn) stays closed.

Check which network profile you are on:

```powershell
Get-NetConnectionProfile
```

If `NetworkCategory` is `Public`, the rule above does not apply. On a company
network it is normally `DomainAuthenticated`. If it is `Public`, ask IT rather
than opening the port on public networks.

> Company laptops may have firewall rules managed by IT that override local
> ones. If section 8 fails even with the rule in place, ask IT to allow inbound
> TCP 8501 to your PC.

## 5. Keep the PC awake

If the PC sleeps, alerts stop and the page goes down. While it is hosting, on
mains power:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

On a laptop, also set **Control Panel → Power Options → Choose what closing the
lid does → When plugged in: Do nothing**, and keep it plugged in.

Locking the screen (Win+L) is fine: Heimdall keeps running. Signing out or
shutting down stops it.

## 6. Start Heimdall

From `C:\heimdall`:

```powershell
.venv\Scripts\python -m heimdall all
```

This starts Huginn, Sleipnir and Bifrost, writes their logs to `data\logs\`,
and stops all three on Ctrl+C. Leave the window open.

Open http://localhost:8501 on the host. The sidebar should show **Huginn
watching** and **Sleipnir on time**. Then set up connections, scripts and
email as in [SETUP.md §8](SETUP.md#8-inside-bifrost-connections-scripts-email).

Run once with `--demo` if you want sample alerts to look at; delete them in
Bifrost afterwards.

## 7. Start it automatically at logon

So Heimdall comes back after a reboot, a Windows update or a crash without you
starting it, create one scheduled task that runs when you log on. It runs
`heimdall all` in a loop. `heimdall all` stops every part as soon as one of them
stops, and the loop then starts them all again 30 seconds later.

In a normal PowerShell (administrator is **not** needed), as yourself:

```powershell
$h = "C:\heimdall"   # the folder you cloned into; spaces in the path are fine

$loop = "Set-Location -LiteralPath '$h'; while (`$true) { & '.\.venv\Scripts\python.exe' -m heimdall all; Start-Sleep 30 }"
$action   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$loop`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Heimdall" -Action $action -Trigger $trigger -Settings $settings -Force
```

(If the folder path itself contains an apostrophe `'`, move the folder: the
loop quotes the path with apostrophes.)

The settings mean: no time limit, keep running on battery, and start late if
the logon was missed. Logs go to `data\logs\huginn.log`, `sleipnir.log` and
`bifrost.log`, as with `heimdall all` by hand.

**Start it now** without logging off:

```powershell
Start-ScheduledTask -TaskName "Heimdall"
```

**Stop it.** Ending the task alone is not enough: Task Scheduler ends the loop,
but the Python processes it started keep running. Stop both:

```powershell
Stop-ScheduledTask -TaskName "Heimdall"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'heimdall|streamlit' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**Remove it** (stop it first, as above):

```powershell
Unregister-ScheduledTask -TaskName "Heimdall" -Confirm:$false
```

Do not also run `python -m heimdall all` by hand while the task is running: the
ports would already be in use.

The task runs only while you are logged on. After a reboot, Heimdall starts
once you log in.

> Why not three tasks with Task Scheduler's *If the task fails, restart every
> 1 minute*? That option only restarts a task that fails to **start**. A part
> that starts and later crashes is not restarted, so the loop above is used
> instead.

## 8. Check from another PC

On a colleague's PC, in PowerShell:

```powershell
Test-NetConnection YOUR-PC-NAME -Port 8501
```

`TcpTestSucceeded : True` means the network path is open. Then open
`http://YOUR-PC-NAME:8501` in their browser. If the name does not work but the
IPv4 address does, use the address in the link and in `[bifrost] url`.

## 9. Tell people how to connect

Send colleagues:

> Heimdall is at **http://YOUR-PC-NAME:8501**. Open it in Chrome or Edge while
> on the office network or VPN. Nothing to install. It is hosted on
> [your name]'s PC, so it is unavailable when that PC is off.

What they need: a browser and network access to your PC. They do **not** need
Python, the repository, or any environment variables.

## 10. Back up

Everything (alerts, schedules, connections, history) is in
`C:\heimdall\data\heimdall.db`. Copy it somewhere else regularly. A shared
drive is a good place for **backups**. Use SQLite's online backup, which is
safe while Heimdall runs; never copy the live file while Huginn is running:

```powershell
cd C:\heimdall
.venv\Scripts\python -c "import sqlite3; s=sqlite3.connect('data/heimdall.db'); d=sqlite3.connect(r'S:\Backups\Heimdall\heimdall.db'); s.backup(d)"
```

(Replace `S:\Backups\Heimdall` with your share.) This can be added as a fourth
scheduled task, for example daily.

Settings → Import / export in Bifrost also downloads every alert, schedule,
connection and script as one JSON file. That is a good second copy, though it
does not include run history.

## 11. Things to know

- **Availability.** Shutdown, sleep, signing out, losing the network, or a
  Windows update restart all stop alerts until the PC is back and you are
  logged in.
- **Who changed what.** Bifrost records changes under the Windows login of the
  account that runs it. With one shared Bifrost on your PC, every change in
  History → Changes shows **your** name, whoever made it.
- **No login.** Anyone who can reach `http://YOUR-PC-NAME:8501` can create,
  change and delete alerts. Share the link only with the people who should use
  it.
- **Scripts and connections run as you.** Scripts registered in Heimdall run
  on your PC with your Windows account, and KDB, Bloomberg and email are reached
  from your PC. Only register scripts you trust.
- **Secrets.** SMTP passwords and relay tokens go in environment variables on
  your PC (`setx NAME "value"`, then restart Heimdall), never in the database or
  `heimdall.toml`. See [SETUP.md §5](SETUP.md#5-configure).
- **Performance.** Every user's page and every alert check runs on your PC. A
  handful of users is fine; heavy use will be felt on your machine.

## 12. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Works at `localhost:8501` on the host, not from other PCs | Firewall (section 4), network profile is `Public`, or IT policy blocks inbound connections. Check with `Test-NetConnection` (section 8). |
| Works by IP address, not by name | The PC name is not in DNS. Use the IP address, and ask IT for a fixed address or DNS name. |
| Links in alert emails do not open | `[bifrost] url` in `heimdall.toml` is still `localhost`. Set it to `http://YOUR-PC-NAME:8501` and restart. |
| Page down every morning | The PC slept or rebooted overnight, or nobody logged in after a reboot. Section 5; check that the *Heimdall* task from section 7 exists and is *Running* (`Get-ScheduledTask Heimdall`). |
| **Address already in use** / port 8501 or 8765 busy | Heimdall is already running (the task, another `heimdall all` window, or processes left behind by ending the task alone). Stop it as in section 7. |
| Bifrost says **Huginn is not answering** | Huginn stopped; read `data\logs\huginn.log`. With the task from section 7, everything restarts within about a minute. |
| Sidebar shows **Sleipnir not seen** | Sleipnir stopped or cannot reach Huginn; read `data\logs\sleipnir.log`. |

Everything else is in [SETUP.md §15](SETUP.md#15-troubleshooting).

## 13. Moving to a server later

When a server is available:

1. Install Heimdall on the server ([SETUP.md](SETUP.md)).
2. Stop Heimdall on your PC (section 7), then copy `data\heimdall.db` to the server's
   `data\` folder (or use the online backup in section 10). Alerts, history,
   connections and schedules all come with it.
3. On the server, set `[bifrost] url` to the server's address, recreate the
   environment variables (SMTP password, relay tokens), and start Heimdall as in
   [SETUP.md §12](SETUP.md#12-keep-it-running-windows).
4. Check that the server can reach the same KDB hosts, BbgRelay and SMTP, and
   that any registered scripts exist at the same paths.
5. Send everyone the new link, then remove the task on your PC (section 7)
   and the firewall rule
   (`Remove-NetFirewallRule -DisplayName "Heimdall Bifrost"`).
