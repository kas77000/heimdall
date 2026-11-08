"""Muninn: the SQLite store Huginn owns.

Definitions (connections, scripts, alerts, schedules) are stored as validated
JSON documents beside the few columns queries need. History is relational:
``runs`` (every evaluation), ``deliveries`` (every notification attempt) and
``events`` (every change of state — enabled by a schedule, disabled by a
person, a schedule skipped). One connection guarded by a lock: Huginn is the
only writer and SQLite in WAL mode is happy with that.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from heimdall.core.models import Alert, Connection, Schedule, Script

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scripts (name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    last_fired_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_state (
    alert_id INTEGER PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
    last_run_id INTEGER,
    last_run_at TEXT,
    last_status TEXT,
    last_message TEXT,
    last_triggered INTEGER NOT NULL DEFAULT 0,
    last_notified_at TEXT,
    last_triggered_hash TEXT NOT NULL DEFAULT '',
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    alert_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    condition_met INTEGER NOT NULL DEFAULT 0,
    triggered INTEGER NOT NULL DEFAULT 0,
    notified TEXT,
    row_count INTEGER,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    steps TEXT NOT NULL DEFAULT '[]',
    snapshot TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS runs_alert_time ON runs(alert_id, started_at);
CREATE INDEX IF NOT EXISTS runs_time ON runs(started_at);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deliveries_run ON deliveries(run_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    alert_id INTEGER,
    alert_name TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_time ON events(at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class Muninn:
    def __init__(self, path: str | Path = "heimdall.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    # -- connections -----------------------------------------------------------

    def connections(self) -> list[Connection]:
        return [Connection.model_validate_json(r["body"])
                for r in self._all("SELECT body FROM connections ORDER BY name COLLATE NOCASE")]

    def connection(self, name: str) -> Optional[Connection]:
        r = self._one("SELECT body FROM connections WHERE name = ?", (name,))
        return Connection.model_validate_json(r["body"]) if r else None

    def save_connection(self, conn: Connection, old_name: str | None = None) -> Connection:
        with self._tx() as db:
            if old_name and old_name != conn.name:
                db.execute("DELETE FROM connections WHERE name = ?", (old_name,))
            db.execute("INSERT INTO connections(name, body) VALUES (?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET body = excluded.body",
                       (conn.name, conn.model_dump_json()))
        return conn

    def delete_connection(self, name: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM connections WHERE name = ?", (name,))

    # -- scripts ---------------------------------------------------------------

    def scripts(self) -> list[Script]:
        return [Script.model_validate_json(r["body"])
                for r in self._all("SELECT body FROM scripts ORDER BY name COLLATE NOCASE")]

    def script(self, name: str) -> Optional[Script]:
        r = self._one("SELECT body FROM scripts WHERE name = ?", (name,))
        return Script.model_validate_json(r["body"]) if r else None

    def save_script(self, script: Script, old_name: str | None = None) -> Script:
        with self._tx() as db:
            if old_name and old_name != script.name:
                db.execute("DELETE FROM scripts WHERE name = ?", (old_name,))
            db.execute("INSERT INTO scripts(name, body) VALUES (?, ?) "
                       "ON CONFLICT(name) DO UPDATE SET body = excluded.body",
                       (script.name, script.model_dump_json()))
        return script

    def delete_script(self, name: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM scripts WHERE name = ?", (name,))

    # -- alerts ----------------------------------------------------------------

    @staticmethod
    def _alert(row: sqlite3.Row) -> Alert:
        a = Alert.model_validate_json(row["body"])
        a.id = row["id"]
        a.enabled = bool(row["enabled"])
        return a

    def alerts(self) -> list[Alert]:
        return [self._alert(r) for r in self._all("SELECT * FROM alerts ORDER BY name COLLATE NOCASE")]

    def alert(self, alert_id: int) -> Alert:
        r = self._one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if r is None:
            raise NotFound(f"no alert {alert_id}")
        return self._alert(r)

    def alert_by_name(self, name: str) -> Optional[Alert]:
        r = self._one("SELECT * FROM alerts WHERE name = ?", (name,))
        return self._alert(r) if r else None

    def save_alert(self, alert: Alert) -> Alert:
        now = iso(utcnow())
        body = alert.model_copy(update={"id": None}).model_dump_json()
        try:
            with self._tx() as db:
                if alert.id is None:
                    cur = db.execute("INSERT INTO alerts(name, enabled, body, created_at, updated_at) "
                                     "VALUES (?, ?, ?, ?, ?)",
                                     (alert.name, int(alert.enabled), body, now, now))
                    alert_id = cur.lastrowid
                else:
                    cur = db.execute("UPDATE alerts SET name = ?, enabled = ?, body = ?, updated_at = ? "
                                     "WHERE id = ?", (alert.name, int(alert.enabled), body, now, alert.id))
                    if cur.rowcount == 0:
                        raise NotFound(f"no alert {alert.id}")
                    alert_id = alert.id
        except sqlite3.IntegrityError:
            raise Conflict(f"an alert called '{alert.name}' already exists") from None
        return self.alert(alert_id)

    def set_enabled(self, alert_id: int, enabled: bool) -> bool:
        """Returns whether anything changed."""
        with self._tx() as db:
            r = db.execute("SELECT enabled FROM alerts WHERE id = ?", (alert_id,)).fetchone()
            if r is None:
                raise NotFound(f"no alert {alert_id}")
            if bool(r["enabled"]) == enabled:
                return False
            db.execute("UPDATE alerts SET enabled = ?, updated_at = ? WHERE id = ?",
                       (int(enabled), iso(utcnow()), alert_id))
            return True

    def delete_alert(self, alert_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))

    def alert_meta(self) -> dict[int, dict]:
        return {r["id"]: {"created_at": r["created_at"], "updated_at": r["updated_at"]}
                for r in self._all("SELECT id, created_at, updated_at FROM alerts")}

    # -- state -----------------------------------------------------------------

    def state(self, alert_id: int) -> dict:
        r = self._one("SELECT * FROM alert_state WHERE alert_id = ?", (alert_id,))
        return dict(r) if r else {"alert_id": alert_id, "last_run_id": None, "last_run_at": None,
                                  "last_status": None, "last_message": None, "last_triggered": 0,
                                  "last_notified_at": None, "last_triggered_hash": "",
                                  "consecutive_errors": 0}

    def states(self) -> dict[int, dict]:
        return {r["alert_id"]: dict(r) for r in self._all("SELECT * FROM alert_state")}

    def reset_state(self, alert_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM alert_state WHERE alert_id = ?", (alert_id,))

    # -- runs ------------------------------------------------------------------

    def record_run(self, *, alert: Alert, started_at: datetime, duration: float, source: str,
                   status: str, condition_met: bool, triggered: bool, notified: Optional[str],
                   row_count: Optional[int], message: str, error: Optional[str],
                   steps: list[dict], snapshot: Optional[dict], result_hash: str,
                   update_state: bool = True) -> int:
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO runs(alert_id, alert_name, started_at, duration, source, status, "
                "condition_met, triggered, notified, row_count, message, error, steps, snapshot) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (alert.id, alert.name, iso(started_at), duration, source, status, int(condition_met),
                 int(triggered), notified, row_count, message, error, json.dumps(steps),
                 json.dumps(snapshot) if snapshot is not None else None))
            run_id = cur.lastrowid
            if update_state:
                prev = db.execute("SELECT * FROM alert_state WHERE alert_id = ?", (alert.id,)).fetchone()
                prev = dict(prev) if prev else {}
                errors = prev.get("consecutive_errors", 0) + 1 if status == "error" else 0
                notified_at = iso(started_at) if notified == "trigger" else prev.get("last_notified_at")
                trig_hash = result_hash if condition_met and (triggered or not prev.get("last_triggered_hash"))                     else prev.get("last_triggered_hash", "")
                if not condition_met and status != "error":
                    trig_hash = ""
                last_trig = int(condition_met) if status != "error" else prev.get("last_triggered", 0)
                db.execute(
                    "INSERT INTO alert_state(alert_id, last_run_id, last_run_at, last_status, last_message, "
                    "last_triggered, last_notified_at, last_triggered_hash, consecutive_errors) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(alert_id) DO UPDATE SET "
                    "last_run_id=excluded.last_run_id, last_run_at=excluded.last_run_at, "
                    "last_status=excluded.last_status, last_message=excluded.last_message, "
                    "last_triggered=excluded.last_triggered, last_notified_at=excluded.last_notified_at, "
                    "last_triggered_hash=excluded.last_triggered_hash, "
                    "consecutive_errors=excluded.consecutive_errors",
                    (alert.id, run_id, iso(started_at), status, message, last_trig, notified_at,
                     trig_hash, errors))
        return run_id

    def record_deliveries(self, run_id: int, kind: str, deliveries: list) -> None:
        now = iso(utcnow())
        with self._tx() as db:
            for d in deliveries:
                db.execute("INSERT INTO deliveries(run_id, at, kind, channel, target, ok, error) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (run_id, now, kind, d.channel, d.target, int(d.ok), d.error))

    @staticmethod
    def _run_row(r: sqlite3.Row, full: bool) -> dict:
        d = dict(r)
        d["condition_met"] = bool(d["condition_met"])
        d["triggered"] = bool(d["triggered"])
        d["steps"] = json.loads(d["steps"] or "[]")
        if full:
            d["snapshot"] = json.loads(d["snapshot"]) if d.get("snapshot") else None
        else:
            d.pop("snapshot", None)
        return d

    def runs(self, alert_id: int | None = None, status: str | None = None,
             since: datetime | None = None, until: datetime | None = None,
             triggered_only: bool = False, unacknowledged: bool = False,
             limit: int = 200, offset: int = 0) -> list[dict]:
        where, args = self._run_filter(alert_id, status, since, until, triggered_only, unacknowledged)
        rows = self._all(f"SELECT * FROM runs {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                         (*args, limit, offset))
        return [self._run_row(r, full=False) for r in rows]

    def count_runs(self, alert_id: int | None = None, status: str | None = None,
                   since: datetime | None = None, until: datetime | None = None,
                   triggered_only: bool = False, unacknowledged: bool = False) -> int:
        where, args = self._run_filter(alert_id, status, since, until, triggered_only, unacknowledged)
        return self._one(f"SELECT COUNT(*) AS n FROM runs {where}", args)["n"]

    @staticmethod
    def _run_filter(alert_id, status, since, until, triggered_only, unacknowledged):
        clauses, args = [], []
        if alert_id is not None:
            clauses.append("alert_id = ?"); args.append(alert_id)
        if status:
            clauses.append("status = ?"); args.append(status)
        if since:
            clauses.append("started_at >= ?"); args.append(iso(since))
        if until:
            clauses.append("started_at < ?"); args.append(iso(until))
        if triggered_only:
            clauses.append("triggered = 1")
        if unacknowledged:
            clauses.append("triggered = 1 AND acknowledged_at IS NULL")
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(args)

    def run(self, run_id: int) -> dict:
        r = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if r is None:
            raise NotFound(f"no run {run_id}")
        d = self._run_row(r, full=True)
        d["deliveries"] = [dict(x) | {"ok": bool(x["ok"])} for x in
                           self._all("SELECT * FROM deliveries WHERE run_id = ? ORDER BY id", (run_id,))]
        return d

    def acknowledge(self, run_ids: list[int], by: str = "") -> int:
        if not run_ids:
            return 0
        marks = ",".join("?" * len(run_ids))
        with self._tx() as db:
            cur = db.execute(f"UPDATE runs SET acknowledged_at = ?, acknowledged_by = ? "
                             f"WHERE id IN ({marks}) AND acknowledged_at IS NULL",
                             (iso(utcnow()), by, *run_ids))
            return cur.rowcount

    def acknowledge_all(self, alert_id: int | None = None, by: str = "") -> int:
        with self._tx() as db:
            sql = "UPDATE runs SET acknowledged_at = ?, acknowledged_by = ? WHERE triggered = 1 AND acknowledged_at IS NULL"
            args: list[Any] = [iso(utcnow()), by]
            if alert_id is not None:
                sql += " AND alert_id = ?"; args.append(alert_id)
            return db.execute(sql, args).rowcount

    def stats(self, since: datetime) -> dict:
        r = self._one("SELECT COUNT(*) AS runs, SUM(status='triggered') AS triggered_runs, "
                      "SUM(triggered) AS firings, SUM(status='error') AS errors, "
                      "SUM(notified IS NOT NULL) AS notified FROM runs WHERE started_at >= ?",
                      (iso(since),))
        unack = self._one("SELECT COUNT(*) AS n FROM runs WHERE triggered = 1 AND acknowledged_at IS NULL")["n"]
        total = self._one("SELECT COUNT(*) AS n, MIN(started_at) AS oldest FROM runs")
        failed = self._one("SELECT COUNT(*) AS n FROM deliveries WHERE ok = 0 AND at >= ?", (iso(since),))["n"]
        return {"runs": r["runs"] or 0, "triggered_runs": r["triggered_runs"] or 0,
                "firings": r["firings"] or 0, "errors": r["errors"] or 0,
                "notified": r["notified"] or 0, "unacknowledged": unack,
                "history_runs": total["n"], "oldest_run": total["oldest"],
                "failed_deliveries": failed}

    def daily_counts(self, since: datetime, alert_id: int | None = None) -> list[dict]:
        sql = ("SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS runs, SUM(triggered) AS firings, "
               "SUM(status='error') AS errors FROM runs WHERE started_at >= ?")
        args: list[Any] = [iso(since)]
        if alert_id is not None:
            sql += " AND alert_id = ?"; args.append(alert_id)
        sql += " GROUP BY day ORDER BY day"
        return [dict(r) for r in self._all(sql, tuple(args))]

    # -- cleanup ---------------------------------------------------------------

    def purge(self, *, older_than_days: float | None = None, keep_last: int | None = None,
              alert_id: int | None = None, everything: bool = False,
              include_events: bool = True, dry_run: bool = False,
              keep_unacknowledged: bool = False) -> dict:
        """Delete history. Criteria combine: ``alert_id`` narrows any of them.

        * ``older_than_days`` — runs (and events) started before now - N days.
        * ``keep_last``       — all but the newest N runs of each alert.
        * ``everything``      — all runs (of the alert, if given).
        """
        if older_than_days is None and keep_last is None and not everything:
            raise ValueError("say what to clean: older than N days, keep the last N, or everything")
        clauses, args = [], []
        if alert_id is not None:
            clauses.append("alert_id = ?"); args.append(alert_id)
        if keep_unacknowledged:
            clauses.append("NOT (triggered = 1 AND acknowledged_at IS NULL)")
        selectors = []
        if older_than_days is not None:
            cutoff = iso(utcnow() - timedelta(days=older_than_days))
            selectors.append(("started_at < ?", [cutoff]))
        if keep_last is not None:
            selectors.append(("id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
                              "(PARTITION BY alert_id ORDER BY started_at DESC, id DESC) AS rn FROM runs) "
                              "WHERE rn > ?)", [int(keep_last)]))
        if selectors and not everything:
            clauses.append("(" + " OR ".join(s for s, _ in selectors) + ")")
            for _, a in selectors:
                args += a
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        ev_where, ev_args = "", []
        if include_events and (older_than_days is not None or everything):
            ev_clauses = []
            if alert_id is not None:
                ev_clauses.append("alert_id = ?"); ev_args.append(alert_id)
            if older_than_days is not None and not everything:
                ev_clauses.append("at < ?"); ev_args.append(iso(utcnow() - timedelta(days=older_than_days)))
            ev_where = ("WHERE " + " AND ".join(ev_clauses)) if ev_clauses else ""
        with self._tx() as db:
            runs = db.execute(f"SELECT COUNT(*) AS n FROM runs {where}", args).fetchone()["n"]
            events = (db.execute(f"SELECT COUNT(*) AS n FROM events {ev_where}", ev_args).fetchone()["n"]
                      if include_events and (older_than_days is not None or everything) else 0)
            if not dry_run:
                db.execute(f"DELETE FROM deliveries WHERE run_id IN (SELECT id FROM runs {where})", args)
                db.execute(f"DELETE FROM runs {where}", args)
                if events:
                    db.execute(f"DELETE FROM events {ev_where}", ev_args)
                db.execute("UPDATE alert_state SET last_run_id = NULL "
                           "WHERE last_run_id IS NOT NULL AND last_run_id NOT IN (SELECT id FROM runs)")
        if not dry_run and runs > 1000 and self.path != ":memory:":
            with self._lock:
                self._db.execute("VACUUM")
        return {"runs": runs, "events": events, "dry_run": dry_run}

    # -- events ----------------------------------------------------------------

    def log_event(self, actor: str, action: str, alert: Alert | None = None,
                  detail: str = "", alert_id: int | None = None, alert_name: str = "") -> None:
        with self._tx() as db:
            db.execute("INSERT INTO events(at, alert_id, alert_name, actor, action, detail) VALUES (?,?,?,?,?,?)",
                       (iso(utcnow()), alert.id if alert else alert_id,
                        alert.name if alert else alert_name, actor, action, detail))

    def events(self, alert_id: int | None = None, limit: int = 200,
               since: datetime | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", []
        clauses = []
        if alert_id is not None:
            clauses.append("alert_id = ?"); args.append(alert_id)
        if since:
            clauses.append("at >= ?"); args.append(iso(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._all(sql, tuple(args))]

    # -- schedules -------------------------------------------------------------

    def _schedule(self, r: sqlite3.Row) -> Schedule:
        s = Schedule.model_validate_json(r["body"])
        s.id, s.alert_id, s.last_fired_at = r["id"], r["alert_id"], r["last_fired_at"]
        return s

    def schedules(self, alert_id: int | None = None) -> list[Schedule]:
        if alert_id is None:
            rows = self._all("SELECT * FROM schedules ORDER BY alert_id, id")
        else:
            rows = self._all("SELECT * FROM schedules WHERE alert_id = ? ORDER BY id", (alert_id,))
        return [self._schedule(r) for r in rows]

    def schedule(self, schedule_id: int) -> Schedule:
        r = self._one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        if r is None:
            raise NotFound(f"no schedule {schedule_id}")
        return self._schedule(r)

    def save_schedule(self, s: Schedule) -> Schedule:
        self.alert(s.alert_id)  # must exist
        body = s.model_copy(update={"id": None, "last_fired_at": None}).model_dump_json()
        with self._tx() as db:
            if s.id is None:
                # A new schedule starts from now: it should not "catch up" on
                # every firing since the epoch.
                cur = db.execute("INSERT INTO schedules(alert_id, body, last_fired_at) VALUES (?,?,?)",
                                 (s.alert_id, body, iso(utcnow())))
                sid = cur.lastrowid
            else:
                cur = db.execute("UPDATE schedules SET alert_id = ?, body = ? WHERE id = ?",
                                 (s.alert_id, body, s.id))
                if cur.rowcount == 0:
                    raise NotFound(f"no schedule {s.id}")
                sid = s.id
        return self.schedule(sid)

    def mark_fired(self, schedule_id: int, at: datetime) -> None:
        with self._tx() as db:
            db.execute("UPDATE schedules SET last_fired_at = ? WHERE id = ?", (iso(at), schedule_id))

    def delete_schedule(self, schedule_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    # -- settings --------------------------------------------------------------

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self._all("SELECT * FROM settings")}

    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(r["value"]) if r else default

    def set_settings(self, values: dict) -> None:
        with self._tx() as db:
            for k, v in values.items():
                db.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
