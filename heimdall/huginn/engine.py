"""Huginn's engine: decides which alerts are due, runs them, records and notifies."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Optional

from heimdall.config import Config
from heimdall.core import cron, hours
from heimdall.core.chain import Pool, frame_to_records
from heimdall.core.evaluate import AlertState, Evaluation, evaluate_alert
from heimdall.core.kdb import PyKxClient
from heimdall.core.models import Alert
from heimdall.core.notify import Delivery, SmtpSettings, compose, compose_all_clear, mail_prefix, deliver
from heimdall import gjallarhorn
from heimdall.muninn.store import Muninn, iso, parse_iso, utcnow

log = logging.getLogger("heimdall.huginn")

RETENTION_EVERY = timedelta(hours=1)
ACKS_EVERY = timedelta(seconds=10)         # how often Gjallarhorn acknowledgements are read back


def smtp_from(settings: dict) -> SmtpSettings:
    s = settings.get("smtp") or {}
    return SmtpSettings(host=s.get("host", ""), port=int(s.get("port", 25) or 25),
                        sender=s.get("sender", "") or "heimdall@localhost")


class Engine:
    def __init__(self, store: Muninn, cfg: Config, pool: Pool | None = None,
                 clock: Callable[[], datetime] = utcnow,
                 email_fn=None):
        self.store = store
        self.cfg = cfg
        self.pool = pool or Pool(store.connection, store.script, kdb_factory=PyKxClient)
        self.clock = clock
        self._deliver_kw = {"email_fn": email_fn} if email_fn else {}
        self._executor = ThreadPoolExecutor(max_workers=max(1, cfg.workers), thread_name_prefix="huginn")
        self._inflight: set[int] = set()
        self._inflight_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at: Optional[datetime] = None
        self.last_tick: Optional[datetime] = None
        self._last_retention: Optional[datetime] = None
        self._last_acks: Optional[datetime] = None
        self._acks_done: set[int] = set()
        self.paused = False
        self.sleipnir_seen: Optional[datetime] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.started_at = self.clock()
        self.paused = bool(self.store.get_setting("engine_paused", False))
        self._thread = threading.Thread(target=self._loop, name="huginn-engine", daemon=True)
        self._thread.start()
        log.info("Huginn engine started")

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if self._thread and wait:
            self._thread.join(timeout=10)
        self._executor.shutdown(wait=wait, cancel_futures=True)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_paused(self, paused: bool, actor: str = "bifrost") -> None:
        self.paused = paused
        self.store.set_settings({"engine_paused": paused})
        self.store.log_event(actor, "engine paused" if paused else "engine resumed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("engine tick failed")
            self._stop.wait(self.cfg.tick_secs)

    # -- scheduling ------------------------------------------------------------

    def due(self, now: datetime) -> list[Alert]:
        states = self.store.states()
        out = []
        for a in self.store.alerts():
            if not a.enabled or a.interval_secs <= 0 or not hours.is_active(a.active_hours, now):
                continue
            last = parse_iso((states.get(a.id) or {}).get("last_run_at"))
            if last is None or (now - last).total_seconds() >= a.interval_secs:
                out.append(a)
        return out

    def next_due(self, alert: Alert, state: dict) -> Optional[datetime]:
        if not alert.enabled or alert.interval_secs <= 0:
            return None
        now = self.clock()
        if not hours.is_active(alert.active_hours, now):
            return hours.next_open(alert.active_hours, now)
        last = parse_iso(state.get("last_run_at"))
        return self.clock() if last is None else last + timedelta(seconds=alert.interval_secs)

    def tick(self) -> None:
        now = self.clock()
        self.last_tick = now
        self._retention(now)
        self._gjallarhorn_acks(now)
        self._watch_hours(now)
        if self.paused:
            return
        for alert in self.due(now):
            if self._claim(alert.id):
                self._executor.submit(self._run_claimed, alert, "interval")

    def _watch_hours(self, now: datetime) -> None:
        """Notice alerts leaving and entering their active hours. Leaving disarms
        the alert, so the first firing after the window reopens notifies afresh."""
        states = self.store.states()
        for a in self.store.alerts():
            was_off = bool((states.get(a.id) or {}).get("off_hours"))
            inside = hours.is_active(a.active_hours, now)
            if a.enabled and not inside and not was_off:
                self.store.update_state(a.id, off_hours=1, last_triggered=0, last_triggered_hash="", severity=None,
                                       delta_ref=None)
                opens = hours.next_open(a.active_hours, now)
                when = opens.astimezone(cron.zone(a.active_hours.tz)).strftime("%a %d %b %H:%M")                     if opens else "never"
                self.store.log_event("huginn", "outside active hours", a,
                                     detail=f"{hours.describe(a.active_hours)}; checks resume {when}")
            elif inside and was_off:
                self.store.update_state(a.id, off_hours=0)

    def _claim(self, alert_id: int) -> bool:
        with self._inflight_lock:
            if alert_id in self._inflight:
                return False
            self._inflight.add(alert_id)
            return True

    def _release(self, alert_id: int) -> None:
        with self._inflight_lock:
            self._inflight.discard(alert_id)

    def inflight(self) -> set[int]:
        with self._inflight_lock:
            return set(self._inflight)

    def _run_claimed(self, alert: Alert, source: str):
        try:
            return self._execute(alert, source)
        except Exception:  # noqa: BLE001
            log.exception("running alert %s failed", alert.name)
        finally:
            self._release(alert.id)

    # -- running -------------------------------------------------------------

    def run_now(self, alert_id: int, source: str = "manual") -> Optional[int]:
        """Run an alert immediately, recorded like any other run. Returns the run id,
        or None when the alert is already running."""
        alert = self.store.alert(alert_id)
        if not self._claim(alert_id):
            return None
        try:
            return self._execute(alert, source)
        finally:
            self._release(alert_id)

    def _state(self, alert_id: int) -> AlertState:
        s = self.store.state(alert_id)
        return AlertState(last_status=s.get("last_status") or "",
                          last_triggered=bool(s.get("last_triggered")),
                          last_notified_at=parse_iso(s.get("last_notified_at")),
                          last_triggered_hash=s.get("last_triggered_hash") or "",
                          consecutive_errors=int(s.get("consecutive_errors") or 0),
                          delta_ref=json.loads(s["delta_ref"]) if s.get("delta_ref") else None,
                          severity=s.get("severity") or "")

    def _execute(self, alert: Alert, source: str) -> int:
        started = self.clock()
        t0 = time.monotonic()
        state = self._state(alert.id)
        ev = evaluate_alert(alert, self.pool, state, started)
        all_clear, period = self._all_clear(alert, ev, self.store.state(alert.id), started)
        # A firing is an event, not every check that is still true: the run that
        # starts an episode, or one that notified. Only those ask to be acknowledged,
        # otherwise an alert firing for an hour would ask 180 times.
        fired = ev.triggered and (ev.notify == "trigger" or not state.last_triggered)
        duration = time.monotonic() - t0
        snapshot = frame_to_records(ev.judged, self.cfg.snapshot_rows) if ev.judged is not None else None
        run_id = self.store.record_run(
            alert=alert, started_at=started, duration=duration, source=source, status=ev.status,
            condition_met=ev.condition_met, triggered=fired,
            notified=ev.notify or ("all_clear" if all_clear else None),
            row_count=ev.row_count, message=ev.message, error=ev.error,
            steps=[o.summary() for o in ev.steps], snapshot=snapshot, result_hash=ev.result_hash)
        if ev.notify == "trigger" and ev.delta_value is not None:
            period["delta_ref"] = json.dumps(ev.delta_value)
        elif ev.status == "ok" and not ev.condition_met:
            period["delta_ref"] = None
        if ev.notify == "trigger":
            period["severity"] = ev.severity
        elif ev.status == "ok" and not ev.condition_met:
            period["severity"] = None
        if period:
            self.store.update_state(alert.id, **period)
        if ev.notify:
            self._notify(alert, ev, run_id)
        elif all_clear:
            self._send_all_clear(alert, ev, run_id, started, **all_clear)
        log.info("%s [%s] %s", alert.name, source, ev.message)
        return run_id

    def _notify(self, alert: Alert, ev: Evaluation, run_id: int) -> None:
        url = f"{self.cfg.bifrost_url.rstrip('/')}/history?run={run_id}" if self.cfg.bifrost_url else ""
        settings = self.store.settings()
        msg = compose(alert, ev, ev.notify, url, prefix=mail_prefix(settings))
        # A recovery asked for only as a pop-up does not mail.
        mail = ev.notify != "recovery" or alert.channels.notify_on_recovery
        deliveries = deliver(alert, msg, smtp_from(settings), **self._deliver_kw) if mail else []
        deliveries += self._pop_up(alert, ev, run_id, settings)
        if deliveries:
            self.store.record_deliveries(run_id, ev.notify, deliveries)

    def _pop_up(self, alert: Alert, ev: Evaluation, run_id: int, settings: dict) -> list[Delivery]:
        """Gjallarhorn: a file on the share for traders' pop-ups (firings, and recoveries if asked)."""
        g = alert.channels.gjallarhorn
        if not g.enabled or ev.notify not in ("trigger", "recovery") or \
                (ev.notify == "recovery" and not g.on_recovery):
            return []
        who = ", ".join(g.audience) or "every trader"
        try:
            g_settings = gjallarhorn.Settings.from_store(settings)
            gjallarhorn.publish(g_settings, gjallarhorn.payload(alert, ev, ev.notify, run_id, self.clock(), g_settings))
            return [Delivery("gjallarhorn", who, True)]
        except Exception as exc:  # noqa: BLE001 - a share blip must not lose the run
            log.warning("gjallarhorn: %s", exc)
            return [Delivery("gjallarhorn", who, False, str(exc))]

    def _gjallarhorn_acks(self, now: datetime) -> None:
        """Record on the run what traders acknowledged in Gjallarhorn."""
        if self._last_acks and now - self._last_acks < ACKS_EVERY:
            return
        self._last_acks = now
        g = gjallarhorn.Settings.from_store(self.store.settings())
        if not g.configured:
            return
        try:
            found = gjallarhorn.acks(g, since=now - timedelta(days=g.keep_days))
        except Exception:  # noqa: BLE001
            log.warning("gjallarhorn: could not read acknowledgements from %s", g.root)
            return
        for a in found:
            if a.run_id in self._acks_done:
                continue
            self._acks_done.add(a.run_id)
            try:
                if self.store.acknowledge([a.run_id], by=f"{a.user} (Gjallarhorn)"):
                    run = self.store.run(a.run_id)
                    self.store.log_event(a.user, "acknowledged in Gjallarhorn", detail=f"run {a.run_id}",
                                         alert_id=run["alert_id"], alert_name=run["alert_name"])
            except Exception:  # noqa: BLE001
                log.exception("gjallarhorn: recording the acknowledgement of run %s failed", a.run_id)

    def _all_clear(self, alert: Alert, ev: Evaluation, row: dict, now: datetime) -> tuple[Optional[dict], dict]:
        """Whether this check sends the all-clear mail, and the period's new state.

        A clean check is one whose condition was not met and that did not fail.
        ``every_check`` (or a period no longer than the check interval) mails after
        each clean check. Otherwise the period mails when it ends, if it had clean
        checks and none that fired or failed; the next period starts at once.
        """
        ac = alert.channels.all_clear
        if not ac.enabled or not any(e.strip() for e in alert.channels.email_to):
            return None, {}
        clean = ev.status == "ok"
        if ac.mode == "every_check" or (alert.interval_secs and ac.interval_secs <= alert.interval_secs):
            # a recovery mail already says it is clear
            return ({"checks": 1, "since": now} if clean and not ev.notify else None), {}
        since = parse_iso(row.get("clear_since")) or now
        checks = int(row.get("clear_checks") or 0) + (1 if clean else 0)
        dirty = bool(row.get("clear_dirty")) or not clean
        if (now - since).total_seconds() >= ac.interval_secs:
            send = {"checks": checks, "since": since} if checks and not dirty else None
            return send, {"clear_since": iso(now), "clear_checks": 0, "clear_dirty": 0}
        return None, {"clear_since": iso(since), "clear_checks": checks, "clear_dirty": int(dirty)}

    def _send_all_clear(self, alert: Alert, ev: Evaluation, run_id: int, now: datetime, *,
                        checks: int, since: datetime) -> None:
        url = f"{self.cfg.bifrost_url.rstrip('/')}/history?run={run_id}" if self.cfg.bifrost_url else ""
        settings = self.store.settings()
        msg = compose_all_clear(alert, checks=checks, since=since, until=now, rows=ev.row_count,
                                bifrost_url=url, prefix=mail_prefix(settings))
        deliveries = deliver(alert, msg, smtp_from(settings), **self._deliver_kw)
        if deliveries:
            self.store.record_deliveries(run_id, "all_clear", deliveries)

    def preview(self, alert: Alert, limit: int = 200) -> dict:
        """Run an unsaved or saved alert without recording or notifying anything."""
        t0 = time.monotonic()
        ev = evaluate_alert(alert, self.pool, AlertState(), self.clock(), dry_run=True)
        return {
            "status": ev.status, "condition_met": ev.condition_met, "message": ev.message,
            "error": ev.error, "judged_step": ev.judged_step, "duration": round(time.monotonic() - t0, 3),
            "steps": [o.summary() | {"table": frame_to_records(o.df, limit)} for o in ev.steps],
            "matching": frame_to_records(ev.matching, limit) if ev.matching is not None else None,
        }

    def resend_test(self, alert: Alert) -> list[dict]:
        """Send a test notification through the alert's channels."""
        from heimdall.core.evaluate import Evaluation as Ev
        import pandas as pd
        sample = pd.DataFrame({"note": ["This is a test notification from Heimdall."]})
        ev = Ev("triggered", True, True, "trigger", f"{alert.name}: test", judged_step="test",
                judged=sample)
        settings = self.store.settings()
        msg = compose(alert, ev, "trigger", self.cfg.bifrost_url, prefix=mail_prefix(settings))
        msg.subject = "[TEST] " + msg.subject
        return [d.__dict__ for d in deliver(alert, msg, smtp_from(settings), **self._deliver_kw)]

    # -- retention -------------------------------------------------------------

    def _retention(self, now: datetime) -> None:
        if self._last_retention and now - self._last_retention < RETENTION_EVERY:
            return
        self._last_retention = now
        try:
            gjallarhorn.clean(gjallarhorn.Settings.from_store(self.store.settings()), now)
        except Exception:  # noqa: BLE001
            log.warning("gjallarhorn: cleaning the shared folder failed")
        policy = self.store.get_setting("retention") or {}
        days, keep = policy.get("days"), policy.get("keep_last")
        if not days and not keep:
            return
        try:
            result = self.store.purge(older_than_days=float(days) if days else None,
                                      keep_last=int(keep) if keep else None,
                                      keep_unacknowledged=bool(policy.get("keep_unacknowledged", True)))
            if result["runs"] or result["events"]:
                self.store.log_event("huginn", "history cleaned",
                                     detail=f"retention removed {result['runs']} run(s), {result['events']} event(s)")
        except Exception:  # noqa: BLE001
            log.exception("retention cleanup failed")
