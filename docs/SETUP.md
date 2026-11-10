# Setting up Heimdall

This guide takes Heimdall from a fresh checkout to a running system that checks
alerts around the clock: first everything on one machine to try it, then each
part where it belongs, then the connections, scripts and email that alerts
need, and finally how to keep it running and healthy.

- [1. What runs where](#1-what-runs-where)
- [2. Prerequisites](#2-prerequisites)
- [3. Install](#3-install)
- [4. Try it on one machine](#4-try-it-on-one-machine)
- [5. Configure](#5-configure)
- [6. Bloomberg through B-PIPE](#6-bloomberg-through-b-pipe)
- [7. Start each part for real](#7-start-each-part-for-real)
- [8. Inside Bifrost: connections, scripts, email](#8-inside-bifrost-connections-scripts-email)
- [9. Your first alert](#9-your-first-alert)
- [10. Schedules](#10-schedules)
- [11. History and cleanup](#11-history-and-cleanup)
- [12. Keep it running (Windows)](#12-keep-it-running-windows)
- [13. Back up, move, upgrade](#13-back-up-move-upgrade)
- [14. Check that it works](#14-check-that-it-works)
- [15. Troubleshooting](#15-troubleshooting)

---

## 1. What runs where

```
 Your desk                     Server (always on)                      Bloomberg
 ─────────                     ──────────────────                      ─────────
 Bifrost  ──── HTTP ────►  Huginn  (API + engine)  ── blpapi, TCP host:port ──►  B-PIPE
 (browser)                   │   ▲         │
                             │   │ pipe    └──── IPC ────►  KDB servers
                             ▼   │
                          Muninn  Sleipnir                    SMTP server ◄── email
                          (SQLite) (scheduler)
```

| Part | What it is | Where it should run |
|---|---|---|
| **Huginn** | The service: HTTP API plus the engine that runs alerts and sends email. The only process that opens the database. | A machine that is always on and can reach the KDB servers, B-PIPE, the SMTP server, and the folder where your scripts live. |
| **Muninn** | A SQLite file owned by Huginn (`data/heimdall.db` by default). Not a process. | Beside Huginn, on a local disk (not a network share). |
| **Sleipnir** | The scheduler. Talks only to Huginn's API, through a local pipe when they share a machine (no HTTP, no network, nothing a proxy can block). | The same machine as Huginn. On another machine it falls back to HTTP. |
| **Bifrost** | The Streamlit app. Talks only to Huginn's API. | Anywhere that can reach Huginn: the server, or each person's desk. |

Scripts registered in Heimdall run **on Huginn's machine**, and KDB and
Bloomberg are reached **from Huginn's machine**. A test run in the Builder
also runs there, so what you see while building is what the service will see.

## 2. Prerequisites

- **Python 3.10 or newer** on every machine that runs a part of Heimdall (3.12
  is what Heimdall is developed on; the full test suite also passes on 3.10.6).
- **Git**, to clone the repository.
- **Network access** from the Huginn machine to:
  - every KDB server you will query (host and port, no authentication, as in
    KdbMonitor);
  - the B-PIPE host and port, if you use Bloomberg steps;
  - your SMTP server, if alerts send email.
- **pykx** for real KDB access. It is installed with the `kdb` extra below;
  the same version KdbMonitor uses works here.
- For Bloomberg: **blpapi**, Bloomberg's Python library, installed with the
  `bloomberg` extra below, and an application name that your B-PIPE
  authorises.

## 3. Install

```powershell
git clone https://github.com/kas77000/heimdall.git
cd heimdall

python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -e ".[dev,kdb]"
```

For Bloomberg steps, add the `bloomberg` extra. It installs `blpapi`, which
comes from Bloomberg's own package index:

```powershell
.venv\Scripts\pip install -e ".[dev,kdb,bloomberg]" --extra-index-url https://blpapi.bloomberg.com/repository/releases/python/simple/
```

Behind the company proxy, pip may also need `--proxy http://host:port`.

> Always use `.venv\Scripts\python`. A bare `python` may be another project's
> environment, and Heimdall's dependencies would land in the wrong place.

## 4. Try it on one machine

```powershell
.venv\Scripts\python -m heimdall all --demo
```

This starts Huginn, Sleipnir and Bifrost together, writes their logs to
`data\logs\`, and stops all three on Ctrl+C. `--demo` adds, if they are not
already there:

- `demo_kdb`: an in-memory KDB with `quote`, `orders` and `fills` tables whose
  prices move with the clock;
- `demo_bbg`: an in-process fake Bloomberg (limit up/down are ±10% of the base
  price);
- three registered example scripts (`examples\scripts\`);
- three alerts: *Price outside Bloomberg limits* (KDB → KDB → Bloomberg →
  compute), *Market-data feed lagging* (a script), and *Large fills by desk*
  (KDB → KDB → a script reading stdin), with schedules.

Open:

- Bifrost: http://localhost:8501
- Huginn's API documentation: http://127.0.0.1:8765/docs

The demo data lives in the same database as everything else. Delete the demo
alerts and connections in Bifrost when you no longer want them, or start from
an empty database (section 13).

## 5. Configure

Copy the example and edit it:

```powershell
copy heimdall.example.toml heimdall.toml
```

```toml
[muninn]
path = "data/heimdall.db"        # the SQLite file; keep it on a local disk

[huginn]
host = "0.0.0.0"                  # 127.0.0.1 = this machine only
port = 8765
token_env = "HEIMDALL_TOKEN"      # name of the variable holding the API token
workers = 4                       # alerts evaluated at the same time
snapshot_rows = 200               # rows of the judged table kept with each run

[sleipnir]
url = "http://huginn-host:8765"   # where Bifrost finds Huginn; Sleipnir too when Huginn is
                                  # on another machine (on the same one it uses a local pipe)
tick_secs = 15
run_grace_secs = 300              # a scheduled run later than this is skipped

[bifrost]
url = "http://bifrost-host:8501"  # used for the "Open in Bifrost" link in emails
```

Heimdall reads `heimdall.toml` from the folder it is started in, or the file
named by `HEIMDALL_CONFIG`. Any value can be overridden by an environment
variable:

| Variable | Overrides |
|---|---|
| `HEIMDALL_CONFIG` | path of the config file |
| `HEIMDALL_DB` | `muninn.path` |
| `HEIMDALL_HOST`, `HEIMDALL_PORT` | Huginn's bind address and port |
| `HEIMDALL_URL` | Huginn's URL as seen by Sleipnir and Bifrost |
| `HEIMDALL_BIFROST_URL` | `bifrost.url` |
| `HEIMDALL_WORKERS` | `huginn.workers` |
| `HEIMDALL_USER` | the name Bifrost records in History for changes (default: your Windows login) |

### The API token

As soon as Huginn listens on anything other than `127.0.0.1`, give it a token.
Every machine running a part needs the same value in the variable named by
`token_env`:

```powershell
# once, for the account that runs the part (opens a new shell to take effect)
setx HEIMDALL_TOKEN "a-long-random-string"
```

A request without the right `X-Heimdall-Token` header is refused (`/health`
stays open, so monitoring can check it). Huginn logs a warning if it starts on
a network address with no token.

**The token is never stored in the database or the config file**, only the
name of the environment variable that holds it.

## 6. Bloomberg through B-PIPE

Huginn talks to Bloomberg B-PIPE directly with `blpapi`. Nothing else needs to
run, on Huginn's machine or elsewhere.

1. **Install blpapi** into Heimdall's `.venv` on the Huginn machine, with the
   `bloomberg` extra (section 3).
2. **Check the network.** The Huginn machine must reach the B-PIPE host and
   port (8194 by default). `Test-NetConnection bpipe-host -Port 8194` should
   show `TcpTestSucceeded : True`.
3. **Add the connection** in Bifrost, Settings → Connections (section 8): kind
   *Bloomberg B-PIPE*, the B-PIPE host and port, and the application name that
   B-PIPE authorises Heimdall as (application-only authentication). Your
   Bloomberg or market-data team gives you the host, port and application name.
4. Press **Test** before saving. It should say the session started at
   host:port as the application, and that the reference (`//blp/refdata`) and
   market data (`//blp/mktdata`) services are open.

To develop without B-PIPE, use a Bloomberg connection whose host is `demo`: the
built-in fake Bloomberg.

## 7. Start each part for real

On the server, from the Heimdall folder:

```powershell
.venv\Scripts\python -m heimdall huginn         # API + engine
.venv\Scripts\python -m heimdall sleipnir       # scheduler
```

Bifrost, on the server or on a desk (with `heimdall.toml` or `HEIMDALL_URL`
pointing at Huginn, and `HEIMDALL_TOKEN` set):

```powershell
.venv\Scripts\python -m heimdall bifrost --port 8501
```

Options:

| Command | Options |
|---|---|
| `heimdall huginn` | `-c FILE`, `--host`, `--port`, `--demo`, `--log-level` |
| `heimdall sleipnir` | `-c FILE`, `--url`, `--log-level` |
| `heimdall bifrost` | `--port` |
| `heimdall all` | `--demo`, `--bifrost-port`; any other option goes to Huginn |

Bifrost's sidebar shows whether Huginn is answering and whether Sleipnir has
checked in during the last minute and a half.

## 8. Inside Bifrost: connections, scripts, email

Everything below is done in **Settings**, and is stored in Muninn.

### Connections

| Kind | Fill in | Notes |
|---|---|---|
| KDB | name, KDB host, KDB port, description | Host `demo` is the built-in mock. Use the address as **Huginn's machine** sees it. |
| Bloomberg B-PIPE | name, B-PIPE host, B-PIPE port (default 8194), application name, description | Host `demo` is the built-in fake. The application name is the one B-PIPE authorises Heimdall as. |

The *Connection* chooser picks **New connection** or **Edit** an existing one.
A new connection first asks for its kind, and only that kind's fields are shown.

**Test** tries the values as typed, from Huginn's machine, before anything is
saved. For KDB it lists the tables (the same `tables[]` the Builder uses for its
table picker); for B-PIPE it starts a session and opens the reference and
market data services. Then **Save**, or **Delete** an existing connection. A
connection that an alert uses cannot be deleted. In the API, `POST
/connections/test` tests a connection sent in the body, and `POST
/connections/{name}/test` a saved one.

Bloomberg connections saved by an older Heimdall still load. One whose URL was
`demo` becomes host `demo`; any other needs its B-PIPE host, port and
application name filled in.

### Scripts

Alerts can only run scripts registered here. Register each one once:

| Field | Meaning |
|---|---|
| Name | How alerts refer to it (letters, digits, `_`). |
| Command | One argument per line. `{name}` inside an argument is filled from the alert. Use the full path of the interpreter and of the script, e.g. `C:\heimdall\.venv\Scripts\python.exe`, `C:\scripts\check_sessions.py`, `--venue`, `{venue}`. |
| Parameters | Each `{name}` used in the command: required or not, a default, a description. |
| It prints | `json` (a list of objects, one object, or a value), `csv` (with a header), `value` (one value), or `exitcode`. |
| Timeout | The script is stopped after this many seconds and the step fails. |
| Working folder | Optional. |
| Non-zero exit is a failure | Usually on; turn off for scripts whose exit code is the answer. |

There is no shell: each argument is passed as exactly one argument, so a value
with spaces or `&` is just text. A script can also receive an earlier step's
table on stdin as JSON records (chosen in the Builder). Every result gets an
`exit_code` column.

**Try it** runs the selected script with its defaults on Huginn's machine and
shows the table it produced. `examples\scripts\` has three small scripts to
copy from.

### Email

| Field | Example |
|---|---|
| SMTP server | `smtp.company.com` |
| SMTP port | `25` |
| Sender address | `heimdall@company.com` |

Mail goes through the company relay without a login or TLS, as in KdbMonitor.

In the Builder's **Notify** section, **Send a test notification** sends a test
email through these settings, so you can check them immediately.

## 9. Your first alert

In **Builder**:

1. **Steps**: add steps in the order they should run. Each step has a name;
   later steps refer to earlier results with `{{name.column}}`.
   - *KDB*: pick the connection and table; add columns and *Where* conditions,
     or switch to *Raw q*. The q that will run is shown underneath.
   - *Bloomberg*: one security per line, e.g. `{{orders.sym}} JP Equity`
     (repeated for each value, and the result keeps `sym` beside `security` so
     you can join back), plus fields such as `LIMIT_UP`, `LIMIT_DOWN`,
     `PX_LAST`. `bdp` asks for reference and current values; `snap` subscribes
     briefly to live data, keeps the first value of each field (waiting up to
     about 10 seconds), then unsubscribes. Its default fields are `LAST_PRICE`,
     `BID` and `ASK`.
   - *Script*: pick a registered script and fill its parameters.
   - *Compute*: join two earlier steps, add columns
     (`abs(last - LIMIT_UP) / LIMIT_UP * 100`), keep rows where a test holds
     (`last >= LIMIT_UP or last <= LIMIT_DOWN`).
2. Press **Test run**. Every step runs on Huginn, nothing is recorded or
   emailed, and each step shows its request and its rows. The columns it finds
   are then offered in later steps and in the trigger.
3. **Trigger**: which step's result to judge and when it fires (any rows, no
   rows, a row count, any or every row where `column op value`, or a total).
   Then how often to notify while it keeps firing.
4. **Notify**: email addresses; optionally notify after N failures in a row,
   and when it clears.
5. **Details**: name, owner, description (it heads every email), tags, how
   often Huginn checks it (or *Schedule only*), and *Enabled*.
6. **Save alert**. Save stays disabled while anything is wrong, and the reasons
   are listed beside it.

Alerts can be exported (Alerts → select → Export, or Settings → Import /
export) and imported elsewhere (section 13). Imported alerts arrive switched
off.

## 10. Schedules

In **Schedules**, pick an alert and what to do (switch on, switch off, or run
it), then either days and a time or a raw five-field cron expression, in a time
zone (`Europe/London`, `Asia/Tokyo`, …; daylight saving is handled). The next
firing is shown before you save.

Sleipnir must be running for schedules to happen. After downtime it applies
only the most recent missed switch on or off, and skips a run that is more
than `run_grace_secs` late, noting it in History → Changes.

## 11. History and cleanup

- **History → Runs**: every check, filterable by alert, status and period, with
  each step's request, the judged table (first `snapshot_rows` rows) and every
  email sent. Firings can be acknowledged here or from Overview.
- **History → Changes**: who created, edited, switched on or off, scheduled,
  imported or cleaned what, and when.
- **History → Clean up**: remove runs older than N days, all but the newest N
  per alert, or everything for one alert (including alerts already deleted).
  The number that will be removed is shown before you confirm. *Keep firings
  nobody has acknowledged* protects open problems.
- **Clean up automatically**: set the age and/or count limits and Huginn
  applies them every hour.

## 12. Keep it running (Windows)

Heimdall should start with the machine and come back if it stops. Task
Scheduler is built into Windows and is enough, with one scheduled task that runs
`heimdall all` in a loop: `heimdall all` stops every part as soon as one of them
stops, and the loop starts them all again 30 seconds later. Logs go to
`data\logs\huginn.log`, `sleipnir.log` and `bifrost.log`.

> Do not rely on Task Scheduler's *If the task fails, restart every …* option.
> It only restarts a task that fails to **start**; a part that starts and later
> crashes stays down. Ending a task also leaves the Python processes it started
> running. Both were checked on Windows 11.

In an **administrator** PowerShell, with the path adjusted. The task runs as
the account that has the environment variables (the API token), whether or not
anyone is logged on:

```powershell
$h = "C:\heimdall"   # where you cloned it; spaces in the path are fine (not an apostrophe)
New-Item -ItemType Directory -Force "$h\data\logs" | Out-Null

$cred = Get-Credential "DOMAIN\svc-heimdall"   # asks for the account's password

$loop = "Set-Location -LiteralPath '$h'; while (`$true) { & '.\.venv\Scripts\python.exe' -m heimdall all; Start-Sleep 30 }"
$action   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command `"$loop`""
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Heimdall" -Action $action -Trigger $trigger -Settings $settings `
  -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force
```

The account needs the *Log on as a batch job* right on the server (your IT may
already grant it to service accounts). Start it now, without a reboot:

```powershell
Start-ScheduledTask -TaskName "Heimdall"
```

**Stop it.** End the task, then the Python processes it started:

```powershell
Stop-ScheduledTask -TaskName "Heimdall"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'heimdall|streamlit' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Start it again with `Start-ScheduledTask -TaskName "Heimdall"`; remove it (after
stopping) with `Unregister-ScheduledTask -TaskName "Heimdall" -Confirm:$false`.
Do not also run `heimdall all` by hand while the task runs: the ports would
already be in use.

**Parts on different machines.** The task above runs all three parts on one
machine. To run them separately (for example Bifrost elsewhere), use real
Windows services with NSSM, which restarts a service whenever it exits. For
each part:

```powershell
nssm install "Heimdall Huginn" C:\heimdall\.venv\Scripts\python.exe -m heimdall huginn
nssm set "Heimdall Huginn" AppDirectory C:\heimdall
nssm set "Heimdall Huginn" AppStdout C:\heimdall\data\logs\huginn.log
nssm set "Heimdall Huginn" AppStderr C:\heimdall\data\logs\huginn.log
nssm set "Heimdall Huginn" ObjectName DOMAIN\svc-heimdall <password>
nssm start "Heimdall Huginn"
```

and the same with `sleipnir`, or `bifrost --port 8501`, in place of `huginn`.

Allow the ports through the firewall for the machines that need them: 8765
(Huginn) for Bifrost on other machines, 8501 (Bifrost) for browsers. Sleipnir on
the same machine as Huginn needs no port: it uses a local pipe.

To host Heimdall on a desktop PC instead of a server, for colleagues to use from
their browsers, see [LOCAL-HOSTING.md](LOCAL-HOSTING.md): its script does all of
this for you.

## 13. Back up, move, upgrade

**Back up.** Everything is in the SQLite file (`muninn.path`). Copy it while
Huginn is stopped, or use SQLite's online backup, which is safe while Huginn
runs:

```powershell
mkdir backup -Force
.venv\Scripts\python -c "import sqlite3; s=sqlite3.connect('data/heimdall.db'); d=sqlite3.connect('backup/heimdall.db'); s.backup(d)"
```

**Move alerts to another install.** Settings → Import / export. **Export**:
pick Everything, Connections, Alerts, Schedules or Scripts, and download
`heimdall-<choice>.json`. **Import**: choose what to take from the file
(Connections, Alerts, Schedules, Scripts; all by default), tick *Replace what
has the same name* if wanted, and upload the file. Bifrost shows what the file
holds, then **Import**.

- Without *Replace*, what already exists is skipped.
- Imported alerts arrive switched off.
- A schedule attaches to the alert with the same name, imported in the same
  file or already in Heimdall. A schedule whose alert is not there is skipped
  and reported. An identical schedule (same action, cron and time zone) is not
  added twice. With *Replace*, the file's schedules replace that alert's
  existing ones.

The API does the same: `GET /export?parts=connections,alerts` and
`POST /import?parts=schedules&replace=true` (parts default to all).

**Start empty.** Stop Huginn, move `data\heimdall.db` aside, start Huginn
(without `--demo`).

**Upgrade.**

```powershell
git pull
.venv\Scripts\pip install -e ".[dev,kdb,bloomberg]" --extra-index-url https://blpapi.bloomberg.com/repository/releases/python/simple/
.venv\Scripts\python -m pytest -m "not e2e"     # quick check
```

then restart Huginn, Sleipnir and Bifrost. The database tables are created
if missing, so a newer Heimdall opens an older database.

## 14. Check that it works

```powershell
.venv\Scripts\python -m pytest                 # everything, about a minute
.venv\Scripts\python -m pytest -m "not e2e"    # unit and integration, seconds
.venv\Scripts\python -m pytest -m e2e          # end to end only
```

The end-to-end tests start the real Huginn, Sleipnir and Bifrost and a local
SMTP server, and use the built-in fake Bloomberg (host `demo`), so they need
nothing extra. They then take an alert through every step kind, email
delivery, error and recovery emails, a cron schedule, cleanup and a restart.
They use their own temporary database and free ports, so they never touch your
real data.

On a running system:

- `http://huginn-host:8765/health` answers `{"ok": true}`.
- Bifrost's sidebar shows **Huginn watching** and **Sleipnir on time**.
- Settings → Connections → **Test** on each connection.
- Builder → Notify → **Send a test notification**.

## 15. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Bifrost says **Huginn is not answering** | Huginn is not running, or `HEIMDALL_URL` / `[sleipnir] url` points elsewhere. Check `http://huginn-host:8765/health` from the Bifrost machine and the firewall. |
| **missing or wrong X-Heimdall-Token** | The token variable differs between machines, or was set with `setx` and the shell was not reopened. |
| Sidebar shows **Sleipnir not seen** | Sleipnir is not running or cannot reach Huginn; read `data\logs\sleipnir.log`. Schedules will not happen until it runs. It reaches a Huginn on the same machine through the pipe `\\.\pipe\heimdall-huginn-<port>`, so both must use the same `[huginn] port` and run as the same Windows account (the pipe's key is in that account's temp folder). |
| KDB **Test** fails | Huginn's machine cannot reach host:port, or pykx is missing from the environment (`pip install -e ".[kdb]"`). The same host and port work in KdbMonitor. |
| Bloomberg step: **blpapi is not installed** | Install it into Heimdall's `.venv` on the Huginn machine: `.venv\Scripts\pip install -e ".[bloomberg]" --extra-index-url https://blpapi.bloomberg.com/repository/releases/python/simple/` (add `--proxy http://host:port` behind the company proxy). |
| B-PIPE **Test** fails: **could not start a session** | Huginn's machine cannot reach the B-PIPE host:port, or the port is wrong. Check with `Test-NetConnection bpipe-host -Port 8194`. |
| **would not open //blp/refdata or //blp/mktdata for '&lt;app&gt;'** | The application name is not entitled or authorised on that B-PIPE. Ask the Bloomberg or market-data team. |
| Script step: **cannot start … not found** | Use full paths for the interpreter and the script in the registered command. |
| Script step: **did not print valid JSON** | The script printed something else besides its output (a log line, a warning). Send logs to stderr, or register it as `csv` or `value`. |
| No email arrives | Settings → Email, then Builder → Notify → **Send a test notification**; the error is shown, and each run's deliveries are listed in its History detail. |
| An alert never runs | It is switched off, its interval is *Schedule only* with no schedule, or Huginn is paused (Overview shows **Paused**). |
| The database grows | History → Clean up, and set an automatic policy. |

Huginn's own log (`data\logs\huginn.log` when started by `heimdall all` or as
above) has a line for every run.
