# Heimdall — design

Heimdall checks conditions across KDB, Bloomberg and in-house scripts and raises
an alert when one is met. It replaces the alerting half of KdbMonitor, whose
checks only ran while a browser had the Streamlit app open. Dashboards stay in
KdbMonitor. Everything is Python.

## The four parts

```
            HTTP (JSON)                     HTTP (JSON)
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
  what the service will see — the same KDB hosts, the same Bloomberg relay, the
  same scripts.
* **Muninn** is a library, not a process: the SQLite store Huginn owns. It
  holds connections, the script registry, alerts, schedules, per-alert state,
  the history of every run and every notification, and settings. History is
  cleaned by age, by keeping the newest N per alert, or per alert — on demand
  from Bifrost, and automatically by a retention setting.
* **Sleipnir** is a separate process that reads schedules from Huginn and, when
  one is due, asks Huginn to enable, disable or run an alert. It owns no data;
  each schedule's last firing is recorded back through the API, so a restart
  neither repeats nor loses a firing (see *Missed firings*).
* **Bifrost** is the Streamlit app. It talks only to Huginn's API and never
  opens the database, so it can run on any desk.

Why an API rather than a shared database: a test run must execute where the
connections and scripts live, which is wherever Huginn runs, and one writer
keeps SQLite out of trouble.

## Alerts are chains of steps

An alert is an ordered list of named steps. Each step yields a table
(pandas DataFrame) that later steps can reference. The trigger condition is
judged on one step's table — the last by default.

| kind | what it does | output |
|---|---|---|
| `kdb` | a guided query (table, filters) or raw q, against a named KDB connection | the query's table |
| `bloomberg` | `bdp` (reference / current values) or `snap` (live values) through a BbgRelay connection | one row per security, a `security` column plus one per field |
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
  differs from the last triggered one (order-insensitive fingerprint).
* **Channels**: email (SMTP, HTML with the first rows of the result) and
  webhooks (Teams/Slack JSON). Optionally notify on error after N consecutive
  errors, and on recovery. Every attempt is recorded with its outcome.
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
`HEIMDALL_*` environment variables. Secrets — the API token, SMTP password,
relay tokens — are read from environment variables named in the config, never
stored in Muninn.

## Without the real systems

A KDB connection with host `demo` is served by an in-memory mock with live
-looking prices, orders and limits; a Bloomberg connection with URL `demo` is
an in-process fake returning deterministic prices and limit up/down fields.
The whole system can be exercised on a laptop.
