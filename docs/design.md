# Heimdall — design

Heimdall checks conditions across KDB, Bloomberg and in-house scripts and raises
an alert when one is met. It replaces the alerting half of KdbMonitor, whose
checks only ran while a browser had the Streamlit app open. Dashboards stay in
KdbMonitor. Everything is Python, except Gjallarhorn, the traders' pop-up app,
which is Windows PowerShell so traders install nothing.

## The parts

```
            HTTP (JSON)                  local pipe (JSON)
 Bifrost  ───────────────►  Huginn  ◄──────────────────  Sleipnir
 Streamlit                  FastAPI + engine              cron loop
 (build, read)              │                             (enable / disable / run on time)
                            ▼
                          Muninn
                          SQLite (definitions, state, history)
```

* **Huginn** is the only process that touches Muninn. It serves the HTTP API
  and runs the engine: every enabled alert is evaluated on its own interval, a
  run is recorded, and notifications go out. A test run (`/alerts/preview`)
  goes through the same code on the same machine, so what the builder shows is
  what the service will see — the same KDB hosts, the same B-PIPE, the same
  scripts.
* **Muninn** is a library, not a process: the SQLite store Huginn owns. It
  holds connections, the script registry, alerts, schedules, per-alert state,
  the history of every run and every notification, and settings. History is
  cleaned by age, by keeping the newest N per alert, or per alert — on demand
  from Bifrost, and automatically by a retention setting.
* **Sleipnir** is a separate process that reads schedules from Huginn and, when
  one is due, asks Huginn to enable, disable or run an alert. It owns no data;
  each schedule's last firing is recorded back through the API, so a restart
  neither repeats nor loses a firing (see *Missed firings*). It reaches a
  Huginn on the same machine through a local pipe, not HTTP (see *Sleipnir's
  pipe*), and a Huginn elsewhere over HTTP.
* **Bifrost** is the Streamlit app. It talks only to Huginn's API and never
  opens the database, so it can run on any desk.
* **Gjallarhorn** puts alerts on traders' screens. Huginn writes one JSON file
  per notification to a shared folder (`alerts/<UTC day>/<run id>.json`,
  written to a temporary name and renamed); a Windows PowerShell + WPF app on
  each trader's PC polls it, shows one card at the bottom right (never a stack)
  and a list to go through, and writes acknowledgements back
  (`acks/<run id>/<user>.json`), which Huginn reads every 10 seconds onto the
  run. The share, not HTTP, joins them: trader PCs never talk to Huginn, so no
  proxy is in the way, and a share blip loses nothing on either side. The app
  is published from Bifrost into `<share>/app`; see *Gjallarhorn* below.

Why an API rather than a shared database: a test run must execute where the
connections and scripts live, which is wherever Huginn runs, and one writer
keeps SQLite out of trouble.

### Sleipnir's pipe

Sleipnir runs beside Huginn and serves no one, so it does not use HTTP or the
network: on Windows it talks to Huginn through the named pipe
`\\.\pipe\heimdall-huginn-<port>` (a Unix socket elsewhere). Nothing is
listening on a TCP port for it, so no proxy or firewall can come between them.
This is what broke HTTP on work PCs, where the company proxy caught even calls
to `127.0.0.1`.

Each message is the API call Sleipnir would have made (method, path, headers,
body) as JSON; Huginn passes it to its own API app, so validation, the token,
History and Sleipnir's heartbeat behave as over HTTP. On every start Huginn
writes a random key to `heimdall-huginn-<port>.key` in the user's temp folder;
a client must prove it holds the key (an HMAC challenge, both ways) before
Huginn reads its request, and nothing on the pipe is unpickled.
`python -m heimdall sleipnir --http` forces HTTP, and a `[sleipnir] url` naming
another machine uses HTTP. Code: `heimdall/pipe.py`.

## Alerts are chains of steps

An alert is an ordered list of named steps. Each step yields a table
(pandas DataFrame) that later steps can reference. The trigger condition is
judged on one step's table — the last by default.

| kind | what it does | output |
|---|---|---|
| `kdb` | a guided query (table, filters) or raw q, against a named KDB connection | the query's table |
| `bloomberg` | `bdp` (reference / current values: a ReferenceDataRequest on `//blp/refdata`) or `snap` (live values: a short `//blp/mktdata` subscription keeping each field's first value, up to about 10 s) against a B-PIPE connection, through `blpapi` | one row per security, a `security` column plus one per field |
| `script` | runs a **registered** script with parameters | parsed from stdout: JSON, CSV, a single value, or just the exit code |
| `compute` | joins two earlier tables, derives columns from guarded expressions, filters rows, keeps columns | the computed table |

### References

Text in a step may refer to earlier results:

* `{{step.column}}` — that column's distinct values. In q it becomes a q list
  (`` `AAPL`MSFT ``) for a `where` clause; in a Bloomberg security line it
  expands the line once per value (`{{orders.sym}} US Equity`); in a script
  parameter it becomes a comma-separated list.
* `{{step.column[0]}}` — the first value alone, as a q atom or plain text.
* `{{table:step}}` — in q only, the whole table as a q table literal, to join
  against.
* `{{rows:step}}` — the row count.

A script may also take one earlier table on stdin, as JSON records.

### Scripts are registered, never typed into an alert

An administrator registers a script once: a name, its command as a list of
arguments with `{param}` placeholders, a working directory, a timeout and the
output format. An alert step names the script and fills its parameters. The
command never goes through a shell and every parameter lands as one argument,
so an alert — including one imported from someone else — cannot run anything
that has not been registered.

### Compute expressions

Derived columns and row filters are Python-syntax expressions over column
names (`price > LIMIT_UP_PRICE`, `abs(px - ref) / ref * 100`). They are
checked against an allow-list of AST nodes and a few functions (`abs`, `min`,
`max`, `round`) before anything is evaluated: names, constants, arithmetic,
comparisons, `and`/`or`/`not`. Attribute access, calls to anything else,
subscripts and lambdas are refused at save time.

## Triggering and notification

* **Condition** on the chosen step: no rows, has rows, row count compared to
  N, any row / all rows `column op value`, or an aggregate (min, max, avg, sum,
  count) compared to a value.
* **Re-arm policy** decides whether a triggered run notifies: every run, only
  on the transition into triggered, after a cooldown, or only when the result
  differs from the last triggered one (order-insensitive fingerprint), or only
  when a measure (row count or a column total, whole table or per key) moved by
  an amount or percent since the last notification (`alert_state.delta_ref`).
* **Severity** (info, warning, critical) belongs to the alert: it prefixes a
  firing's email subject, is a column and filter in Bifrost, and sets how a
  Gjallarhorn pop-up behaves. With `severity_rules` it comes from the result
  instead (`conditions.grade`: the first matching rule on rows / a row's
  column / a column's total / the value; else `severity_else`, where `skip`
  makes the check not fire at all). `alert_state.severity` keeps the severity
  notified in the current firing, so a rise notifies again whatever the
  re-arm; it resets when the firing clears. **Sound** is the alert's too: empty means the
  severity's default (Settings; Gjallarhorn's horns, generated by
  `heimdall/gjallarhorn/make_sounds.py`), or a horn, a Windows sound, an
  uploaded .wav, or none. The resolved sound travels in the pop-up's file.
* **Channels**: email (SMTP, HTML with the rows of the result, in the body or
  as CSV) and Gjallarhorn pop-ups (for every trader, or groups of traders kept
  in Heimdall's settings, or single Windows user names; Huginn opens the
  groups up into user names in the pop-up's file). Optionally notify on error after N consecutive errors, and on
  recovery (a recovery pop-up is separate from a recovery mail). Every attempt
  is recorded with its outcome.
* **Interval**: each enabled alert runs every `interval_secs`. An interval of
  0 means *only when Sleipnir or a person says so*.

## Schedules (Sleipnir)

A schedule is `(alert, action, cron, timezone)` with action `enable`,
`disable` or `run`. Bifrost writes the common shapes for you (these days, at
this time) and accepts raw cron for the rest.

### Missed firings

Sleipnir works from each schedule's `last_fired_at`. After downtime, for
`enable`/`disable` only the most recent missed firing is applied (the state it
leaves is what matters); a `run` that is more than five minutes late is
recorded as skipped rather than run, since "the 16:30 check" at 18:10 is not
the 16:30 check.

## History

Every evaluation is a run: when, what started it (interval, schedule, manual),
status (`ok`, `triggered`, `error`), whether it notified, the duration, each
step's resolved request, row count and error, and a snapshot of the judged
table (capped at `snapshot_rows`, default 200). Triggered runs can be
acknowledged in Bifrost. Cleanup is explicit and previewable: delete older
than N days, keep the newest N per alert, everything for one alert, or all.

## Configuration

`heimdall.toml` (see `heimdall.example.toml`), overridable with
`HEIMDALL_*` environment variables. The one secret, the API token, is read
from the environment variable named in the config, never stored in Muninn.
SMTP is a server, port and sender address, with no login or TLS (the company
relay). A B-PIPE connection is a host, port and application name
(application-only authentication).

## Without the real systems

A KDB connection with host `demo` is served by an in-memory mock with live
-looking prices, orders and limits; a Bloomberg connection with host `demo` is
an in-process fake returning deterministic prices and limit up/down fields.
The whole system can be exercised on a laptop.

## Bifrost

Always dark: the theme is set in `.streamlit/config.toml` and the toolbar menu
that would let a viewer switch it is removed. Neutrals are tinted toward
indigo, one violet accent marks primary actions and the current selection, and
red, green, amber and grey mean fired, ok, error and off everywhere, always
beside a word so colour is never the only signal.

| Page | For |
|---|---|
| Overview | What needs attention now (fired, failing, unacknowledged), today's counts, every alert with its last and next check. Refreshes every 10 s. |
| History | Runs with filters and a 14-day chart; a run's detail shows each step's request, the judged table and every delivery. Changes (the audit log). Clean up, now or by policy. |
| Alerts | Every definition: open, duplicate, switch on or off, export, delete (history kept or not). |
| Builder | Steps, Trigger, Notify, Details. Test run executes the chain on Huginn without recording or notifying, and lists each step's columns for the next step's references. Save is disabled while Huginn reports problems. `?alert=<id>` opens one directly. |
| Schedules | Days-and-time or raw cron, in a named zone, with the next firing shown before saving; warns when Sleipnir has not checked in. |
| Settings | Connections (new or edit, tested with the typed values before saving), the script registry (with a trial run), SMTP, import and export of chosen parts (connections, alerts, schedules, scripts). |

## Acknowledgement

A run is a *firing* when it starts an episode (the condition was false before)
or when it sent a notification. Only firings wait to be acknowledged, so an
alert that stays true for an hour asks once, not once per check.

## Gjallarhorn

The trader app is one ASCII PowerShell script (Windows PowerShell reads a script
without a BOM in the local code page), with no compiled code: its taskbar
identity is set through a dynamic P/Invoke, and its horn icon is drawn at run
time, for the tray (with the count), the window and the Start menu (`-WriteIcon`).
It keeps per-PC state in `%LOCALAPPDATA%\Gjallarhorn`: the alerts already popped
up (so a restart does not pop them again, and a new PC lists old alerts without
popping them) and whether sounds are on. `-DryRun` runs the same reading,
filtering and acknowledging without a window; the tests drive it that way.
