"""Active hours: when an alert's regular checks are allowed to run.

A window runs from ``start`` to ``end`` (not included) in the alert's time zone,
on the chosen days (none chosen = every day). A window whose end is before its
start crosses midnight and belongs to the day it starts on, so "Fri 22:00-02:00"
runs into Saturday morning. Daylight saving is handled by the time zone.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from heimdall.core import cron
from heimdall.core.models import ActiveHours, Window

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_WINDOWS = 6


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _day_ok(h: ActiveHours, weekday: int) -> bool:
    return not h.days or weekday in h.days


def _usable(h: ActiveHours) -> list[Window]:
    return [w for w in h.windows if w.start != w.end]


def is_active(h: ActiveHours, now: datetime) -> bool:
    """True when regular checks may run. Hours that are off, or set up with no
    usable window, never hold an alert back."""
    windows = _usable(h)
    if not h.enabled or not windows:
        return True
    local = now.astimezone(cron.zone(h.tz))
    t, wd = local.time().replace(tzinfo=None), local.weekday()
    for w in windows:
        s, e = _t(w.start), _t(w.end)
        if s < e:
            if _day_ok(h, wd) and s <= t < e:
                return True
        else:                                    # crosses midnight
            if _day_ok(h, wd) and t >= s:
                return True
            if _day_ok(h, (wd - 1) % 7) and t < e:
                return True
    return False


def _starts(h: ActiveHours, day: date) -> list[datetime]:
    if not _day_ok(h, day.weekday()):
        return []
    tz = cron.zone(h.tz)
    return [datetime.combine(day, _t(w.start), tzinfo=tz) for w in _usable(h)]


def next_open(h: ActiveHours, now: datetime) -> Optional[datetime]:
    """When the next window opens (UTC), or None when hours are off or never open."""
    if not h.enabled or not _usable(h):
        return None
    local = now.astimezone(cron.zone(h.tz))
    for d in range(0, 9):
        day = local.date() + timedelta(days=d)
        later = sorted(s for s in _starts(h, day) if s > local)
        if later:
            return later[0].astimezone(timezone.utc)
    return None


def describe(h: ActiveHours) -> str:
    """'09:00-17:00 Mon-Fri (Europe/London)', or 'Any time'."""
    windows = _usable(h)
    if not h.enabled or not windows:
        return "Any time"
    spans = ", ".join(f"{w.start}-{w.end}" for w in windows)
    return f"{spans} {_days(h.days)} ({h.tz})"


def _days(days: list[int]) -> str:
    if not days or len(days) == 7:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon-Fri"
    if days == [5, 6]:
        return "weekends"
    runs, start = [], days[0]
    for prev, cur in zip(days, days[1:] + [None]):
        if cur != prev + 1:
            runs.append(DAY_NAMES[start] if start == prev else f"{DAY_NAMES[start]}-{DAY_NAMES[prev]}")
            start = cur
    return ", ".join(runs)


def problems(h: ActiveHours) -> list[str]:
    """What is wrong with active hours that are switched on."""
    if not h.enabled:
        return []
    out = []
    try:
        cron.zone(h.tz)
    except cron.CronError as exc:
        out.append(f"Active hours: {exc}")
    if not h.windows:
        out.append("Active hours: add at least one window, or switch active hours off.")
    if len(h.windows) > MAX_WINDOWS:
        out.append(f"Active hours: at most {MAX_WINDOWS} windows.")
    for i, w in enumerate(h.windows, start=1):
        if w.start == w.end:
            out.append(f"Active hours: window {i} starts and ends at {w.start}.")
    return out
