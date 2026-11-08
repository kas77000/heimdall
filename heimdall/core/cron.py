"""Cron expressions read in a schedule's own time zone, and the shapes Bifrost offers.

Sleipnir works in UTC; a schedule's cron is written the way the desk says it
("16:30 London, weekdays"), so the conversion happens here, once, with DST
taken at the date in question.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_CRON_DOW = {0: "1", 1: "2", 2: "3", 3: "4", 4: "5", 5: "6", 6: "0"}


class CronError(ValueError):
    pass


def zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo((tz or "UTC").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise CronError(f"unknown time zone '{tz}' (use a name like Europe/London or Asia/Tokyo)") from None


def validate(expr: str, tz: str = "UTC") -> None:
    zone(tz)
    if not croniter.is_valid((expr or "").strip()) or len(expr.split()) != 5:
        raise CronError(f"'{expr}' is not a five-field cron expression (minute hour day month weekday)")


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fire_times(expr: str, tz: str, after: datetime, until: datetime) -> Iterator[datetime]:
    """Firing instants (UTC) in (after, until]."""
    validate(expr, tz)
    z = zone(tz)
    it = croniter(expr.strip(), _utc(after).astimezone(z))
    end = _utc(until)
    while True:
        nxt = it.get_next(datetime).astimezone(timezone.utc)
        if nxt > end:
            return
        yield nxt


def next_fire(expr: str, tz: str, after: datetime) -> datetime:
    validate(expr, tz)
    return croniter(expr.strip(), _utc(after).astimezone(zone(tz))).get_next(datetime).astimezone(timezone.utc)


def previous_fire(expr: str, tz: str, before: datetime) -> datetime:
    validate(expr, tz)
    return croniter(expr.strip(), _utc(before).astimezone(zone(tz))).get_prev(datetime).astimezone(timezone.utc)


def simple(days: list[int], hhmm: str) -> str:
    """'these days, at this time' as cron. ``days``: 0=Mon … 6=Sun."""
    try:
        hh, mm = (int(p) for p in hhmm.strip().split(":"))
    except ValueError:
        raise CronError(f"'{hhmm}' is not a time (HH:MM)") from None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise CronError(f"'{hhmm}' is not a time of day")
    if not days:
        raise CronError("choose at least one day")
    dow = "*" if len(set(days)) == 7 else ",".join(_CRON_DOW[d] for d in sorted(set(days)))
    return f"{mm} {hh} * * {dow}"


def as_simple(expr: str) -> tuple[list[int], str] | None:
    """The reverse of :func:`simple`, when an expression has that shape."""
    parts = (expr or "").split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        return None
    mm, hh, dow = parts[0], parts[1], parts[4]
    if not (mm.isdigit() and hh.isdigit()):
        return None
    if dow == "*":
        days = list(range(7))
    else:
        back = {v: k for k, v in _CRON_DOW.items()} | {"7": 6}
        try:
            days = sorted({back[d] for d in dow.split(",")})
        except KeyError:
            if dow == "1-5":
                days = [0, 1, 2, 3, 4]
            else:
                return None
    return days, f"{int(hh):02d}:{int(mm):02d}"


def describe(expr: str, tz: str) -> str:
    s = as_simple(expr)
    if s is None:
        return f"cron '{expr}' ({tz})"
    days, hhmm = s
    if days == list(range(7)):
        when = "every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "weekdays"
    else:
        when = ", ".join(DAYS[d] for d in days)
    return f"{when} at {hhmm} ({tz})"
