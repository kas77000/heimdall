"""Sleipnir: carries out schedules — enable, disable or run an alert on time.

Sleipnir owns no data. Each tick it reads the schedules from Huginn, works out
which have come due since their ``last_fired_at``, acts through the API and
records the firing back, so a restart neither repeats nor loses one.

After downtime only the most recent missed firing of an ``enable``/``disable``
schedule is applied — the state it leaves is what matters. A ``run`` that is
later than the grace period is recorded as skipped: the 16:30 check at 18:10
is not the 16:30 check.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from heimdall.client import HuginnClient, HuginnError
from heimdall.core import cron
from heimdall.muninn.store import iso, parse_iso

log = logging.getLogger("heimdall.sleipnir")


@dataclass
class Due:
    schedule_id: int
    alert_id: int
    action: str
    at: datetime            # the firing being acted on (UTC)
    late_secs: float
    missed: int             # how many earlier firings were folded into this one


def due_firings(schedules: list[dict], now: datetime) -> list[Due]:
    out: list[Due] = []
    for s in schedules:
        if not s.get("active", True):
            continue
        last = parse_iso(s.get("last_fired_at"))
        if last is None:
            # Never fired and no starting point: start counting from now.
            last = now
        try:
            times = list(cron.fire_times(s["cron"], s.get("tz") or "UTC", last, now))
        except cron.CronError as exc:
            log.warning("schedule %s: %s", s.get("id"), exc)
            continue
        if not times:
            continue
        latest = times[-1]
        out.append(Due(s["id"], s["alert_id"], s["action"], latest,
                       (now - latest).total_seconds(), len(times) - 1))
    return out


class Sleipnir:
    def __init__(self, client: HuginnClient, tick_secs: float = 15.0, run_grace_secs: int = 300,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.tick_secs = tick_secs
        self.run_grace_secs = run_grace_secs
        self.clock = clock
        self._stop = threading.Event()

    def tick(self) -> list[dict]:
        now = self.clock()
        schedules = self.client.get("/schedules")
        done = []
        for d in due_firings(schedules, now):
            done.append(self._act(d))
        return done

    def _act(self, d: Due) -> dict:
        note = f"due {d.at.strftime('%Y-%m-%d %H:%M')} UTC"
        if d.missed:
            note += f", {d.missed} earlier firing(s) missed while Sleipnir was down"
        outcome, detail = "applied", note
        try:
            if d.action == "run":
                if d.late_secs > self.run_grace_secs:
                    outcome = "skipped"
                    detail = f"{note}; {int(d.late_secs // 60)} min late, beyond the {self.run_grace_secs // 60} min grace"
                else:
                    run = self.client.post(f"/alerts/{d.alert_id}/run", source="schedule")
                    detail = f"{note}; run {run['id']}: {run['status']}"
            else:
                r = self.client.post(f"/alerts/{d.alert_id}/{d.action}", detail=f"by schedule {d.schedule_id}")
                if not r.get("changed"):
                    outcome, detail = "unchanged", f"{note}; already {d.action}d"
        except HuginnError as exc:
            if exc.status == 409 and d.action == "run":
                outcome, detail = "skipped", f"{note}; the alert was already running"
            else:
                outcome, detail = "failed", f"{note}; {exc}"
        self.client.post(f"/schedules/{d.schedule_id}/fired",
                         {"at": iso(d.at), "outcome": outcome, "detail": detail})
        log.info("schedule %s (%s alert %s): %s — %s", d.schedule_id, d.action, d.alert_id, outcome, detail)
        return {"schedule_id": d.schedule_id, "action": d.action, "outcome": outcome, "detail": detail}

    def run_forever(self) -> None:
        log.info("Sleipnir running against %s every %ss", self.client.url, self.tick_secs)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep galloping
                log.warning("tick failed: %s", exc)
            self._stop.wait(self.tick_secs)

    def stop(self) -> None:
        self._stop.set()
