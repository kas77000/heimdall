"""Gjallarhorn: pop-ups on traders' screens, through a folder on the shared drive.

Huginn writes one JSON file per notification; the Gjallarhorn app on each
trader's PC (Windows PowerShell and WPF, nothing to install, see ``app/``)
reads the folder, shows the pop-ups and writes acknowledgements back, which
Huginn records on the run.

    <root>/alerts/<YYYY-MM-DD>/<run id>.json    one per notification (UTC day)
    <root>/acks/<run id>/<user>.json            one per acknowledgement
    <root>/app/                                 the trader app, published from Bifrost
    <root>/sounds/<name>.wav                    sounds uploaded from Bifrost
    <root>/users/<user>.json                    each trader running Gjallarhorn, refreshed while it runs

Groups of traders are kept in Heimdall (Settings, Gjallarhorn): a pop-up for a
group lists the group's members in ``recipients``, so a PC only needs to know
its Windows user name.

Every file is written to a temporary name and renamed, so a reader never sees
half of one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from heimdall.core import conditions
from heimdall.core.models import SEVERITIES, SOUND_FILE_RE, check_sound

SCHEMA = 1
MAX_ROWS = 500              # rows a pop-up's details carry; the count says how many there were
MAX_CELL = 300              # characters per cell
APP_DIR = Path(__file__).with_name("app")
APP_FILES = ("gjallarhorn.ps1", "install-gjallarhorn.ps1", "README.txt",
             "install-gjallarhorn.cmd", "start-gjallarhorn.cmd", "stop-gjallarhorn.cmd", "uninstall-gjallarhorn.cmd",
             "sounds/horn-info.wav", "sounds/horn-warning.wav", "sounds/horn-critical.wav")
DEFAULT_SOUNDS = {"info": "horn:info", "warning": "horn:warning", "critical": "horn:critical"}
MAX_SOUND_BYTES = 2 * 1024 * 1024
USER_FRESH_MINUTES = 15     # the app refreshes its file every 5 minutes while it runs
USER_KEEP_DAYS = 60         # a trader not seen for this long leaves the list
_SAFE = re.compile(r"[^A-Za-z0-9._@-]+")


@dataclass
class Settings:
    root: str = ""
    keep_days: int = 7
    sounds: Optional[dict] = None             # severity -> sound; Gjallarhorn's horns by default
    groups: Optional[dict] = None             # group name -> Windows user names

    def sound_for(self, severity: str) -> str:
        chosen = (self.sounds or {}).get(severity)
        try:
            return check_sound(chosen) or DEFAULT_SOUNDS[severity] if chosen else DEFAULT_SOUNDS[severity]
        except ValueError:
            return DEFAULT_SOUNDS[severity]

    @property
    def configured(self) -> bool:
        return bool(self.root.strip())

    @classmethod
    def from_store(cls, settings: dict) -> "Settings":
        g = settings.get("gjallarhorn") or {}
        return cls(root=str(g.get("root", "") or "").strip(), keep_days=int(g.get("keep_days", 7) or 7),
                   sounds={k: v for k, v in (g.get("sounds") or {}).items() if k in SEVERITIES},
                   groups=clean_groups(g.get("groups")))


# -- groups of traders -----------------------------------------------------------------

def clean_groups(raw) -> dict[str, list[str]]:
    """``{group: [user, ...]}``: names trimmed, no empty group name, each member once
    (Windows user names compare without case)."""
    out: dict[str, list[str]] = {}
    for name, members in (raw or {}).items():
        name = str(name or "").strip()
        if not name or name.lower() in {k.lower() for k in out}:
            continue
        if isinstance(members, str):
            members = re.split(r"[,;\s]+", members)
        seen, keep = set(), []
        for m in members or []:
            m = str(m or "").strip()
            if m and m.lower() not in seen:
                seen.add(m.lower())
                keep.append(m)
        out[name] = keep
    return out


def recipients(settings: Optional[Settings], audience: Iterable[str]) -> list[str]:
    """Who a pop-up is for, as the trader app matches it: every name of the audience
    (a user name, or a desk set on a PC) plus the members of the groups among them.
    Empty: every trader."""
    groups = {k.lower(): v for k, v in ((settings.groups if settings else None) or {}).items()}
    out, seen = [], set()
    for name in audience:
        name = str(name or "").strip()
        for who in [name] + groups.get(name.lower(), []):
            if who and who.lower() not in seen:
                seen.add(who.lower())
                out.append(who)
    return out


def users(settings: Settings, now: Optional[datetime] = None) -> list[dict]:
    """The traders whose Gjallarhorn wrote to the share: user, machine, desks, last seen."""
    d = Path(settings.root) / "users"
    if not settings.configured or not d.is_dir():
        return []
    now = now or datetime.now(timezone.utc)
    out = []
    for f in d.glob("*.json"):
        if f.name.startswith("."):
            continue
        try:
            u = json.loads(f.read_text(encoding="utf-8-sig"))
            seen = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        except (OSError, ValueError):
            continue
        out.append({"user": str(u.get("user") or f.stem), "machine": str(u.get("machine") or ""),
                    "desks": [str(x) for x in (u.get("desks") or []) if x], "version": str(u.get("version") or ""),
                    "seen": seen.isoformat(timespec="seconds"),
                    "running": (now - seen) < timedelta(minutes=USER_FRESH_MINUTES)})
    return sorted(out, key=lambda u: u["user"].lower())


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _cell(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    return s if len(s) <= MAX_CELL else s[:MAX_CELL - 1] + "…"


def _rows(table: Optional[pd.DataFrame]) -> dict:
    if table is None or table.empty:
        return {"columns": [], "data": [], "total": 0}
    head = table.head(MAX_ROWS)
    return {"columns": [str(c) for c in head.columns],
            "data": [[_cell(v) for v in row] for row in head.itertuples(index=False)],
            "total": int(len(table))}


def payload(alert, ev, kind: str, run_id: int, at: datetime, settings: Optional[Settings] = None) -> dict:
    """What a trader sees: no links to Bifrost, everything is in the file."""
    g = alert.channels.gjallarhorn
    recovery = kind == "recovery"
    from heimdall.core.evaluate import severity_of
    severity = "info" if recovery else severity_of(alert, ev)
    sound = (settings or Settings()).sound_for("info") if recovery else \
        (alert.sound or (settings or Settings()).sound_for(severity))
    # One section per kind of information, for the trader app to lay out; ``lines``
    # keeps the same as plain sentences for older copies of the app.
    details: list[dict] = []
    if alert.description:
        details.append({"kind": "about", "label": "About this alert", "text": alert.description})
    if recovery:
        details.append({"kind": "cleared", "label": "Cleared", "text": "The condition is no longer met."})
        table = None
    else:
        rule = conditions.describe(alert.condition, ev.judged_step).removeprefix("Triggers when ")
        details.append({"kind": "rule", "label": "Fires when", "text": rule + "."})   # starts with a step name
        if alert.severity_rules and getattr(ev, "severity_why", ""):
            details.append({"kind": "severity", "label": f"Why {severity}", "text": ev.severity_why[:1].upper()
                            + ev.severity_why[1:] + "."})
        if ev.moves:
            details.append({"kind": "again", "label": "Firing again because",
                            "text": "; ".join(m[:1].upper() + m[1:] for m in ev.moves) + "."})
        table = ev.matching if ev.matching is not None and not ev.matching.empty else ev.judged
    lines = [d["text"] if d["kind"] in ("about", "cleared") else
             ("Triggers when " + d["text"] if d["kind"] == "rule" else
              f"{d['label']}: {d['text']}") for d in details]
    n = ev.row_count
    return {
        "schema": SCHEMA,
        "id": str(run_id),
        "run_id": run_id,
        "alert_id": alert.id,
        "alert": alert.name,
        "group": alert.group,
        "kind": kind,                                # trigger | recovery | test
        "severity": severity,
        "sound": sound,
        "title": f"Cleared: {alert.name}" if recovery else alert.name,
        "summary": ("No longer firing" if recovery
                    else f"{n} row{'s' if n != 1 else ''}" if n is not None else "Fired"),
        "lines": lines,
        "details": details,
        "audience": list(g.audience),               # as chosen, groups by name
        "recipients": recipients(settings, g.audience),
        "needs_ack": not recovery,
        "fired_at": at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "rows": _rows(table),
    }


def alert_path(root: str | Path, data: dict) -> Path:
    day = data["fired_at"][:10]
    return Path(root) / "alerts" / day / f"{data['id']}.json"


def publish(settings: Settings, data: dict) -> Path:
    if not settings.configured:
        raise RuntimeError("Gjallarhorn has no shared folder (Settings → Gjallarhorn)")
    path = alert_path(settings.root, data)
    _write_json(path, data)
    return path


def publish_test(settings: Settings, audience: Iterable[str] = (), severity: str = "warning",
                 now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows = pd.DataFrame({"sym": ["AAPL", "MSFT", "7203.T"], "last": [231.4, 418.2, 2875.0],
                         "limit": [230.0, 415.0, 2850.0]})
    data = {"schema": SCHEMA, "id": f"test-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}", "run_id": None,
            "alert_id": None, "alert": "Gjallarhorn test", "group": "Test", "kind": "test",
            "severity": severity, "sound": settings.sound_for(severity), "title": "Gjallarhorn test",
            "summary": "3 rows", "lines": ["A test pop-up sent from Bifrost.",
                                           "Acknowledge it to check the way back to Huginn."],
            "details": [{"kind": "about", "label": "About this alert", "text": "A test pop-up sent from Bifrost."},
                        {"kind": "rule", "label": "Fires when", "text": "Someone presses Send a test pop-up."},
                        {"kind": "severity", "label": f"Why {severity}",
                         "text": f"It was sent as a {severity} test, to hear its horn."},
                        {"kind": "todo", "label": "What to do",
                         "text": "Acknowledge it: Bifrost then shows who did, which checks the way back."}],
            "audience": [a for a in audience if a], "recipients": recipients(settings, audience),
            "needs_ack": True,
            "fired_at": now.isoformat(timespec="seconds"), "rows": _rows(rows)}
    publish(settings, data)
    return data


def acknowledge(settings: Settings, alert_id: str, user: str, note: str = "",
                now: Optional[datetime] = None) -> Path:
    """What the trader app does; used by Bifrost too, so pop-ups close when a run
    is acknowledged there."""
    now = now or datetime.now(timezone.utc)
    user = _SAFE.sub("_", user).strip("_") or "someone"
    path = Path(settings.root) / "acks" / str(alert_id) / f"{user}.json"
    _write_json(path, {"id": str(alert_id), "user": user, "at": now.isoformat(timespec="seconds"),
                       "note": note})
    return path


@dataclass
class Ack:
    run_id: int
    user: str
    at: str


def acks(settings: Settings, since: Optional[datetime] = None) -> list[Ack]:
    """Acknowledgements of runs (not tests), the first per run, newer than ``since``."""
    base = Path(settings.root) / "acks"
    if not settings.configured or not base.is_dir():
        return []
    cutoff = since.timestamp() if since else 0
    out = []
    for d in base.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                continue
            files = sorted((f for f in d.glob("*.json")), key=lambda f: f.stat().st_mtime)
            for f in files:
                try:
                    a = json.loads(f.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                out.append(Ack(int(d.name), str(a.get("user") or f.stem), str(a.get("at") or "")))
                break
        except OSError:
            continue
    return out


def clean(settings: Settings, now: Optional[datetime] = None) -> int:
    """Remove day folders and acknowledgements older than ``keep_days``."""
    if not settings.configured:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, settings.keep_days))
    root, removed = Path(settings.root), 0
    for d in (root / "alerts").glob("????-??-??"):
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day + timedelta(days=1) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    for d in (root / "acks").glob("*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff.timestamp():
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    for f in (root / "users").glob("*.json"):
        try:
            if f.stat().st_mtime < now.timestamp() - USER_KEEP_DAYS * 86400:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def deploy_app(settings: Settings) -> list[str]:
    """Copy the trader app to ``<root>/app``; traders' PCs run it from there."""
    if not settings.configured:
        raise RuntimeError("Gjallarhorn has no shared folder (Settings → Gjallarhorn)")
    dest = Path(settings.root) / "app"
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in APP_FILES:
        src = APP_DIR / name
        if src.is_file():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, target)
            copied.append(name)
    return copied


# -- sounds ----------------------------------------------------------------------------

def sound_files(settings: Settings) -> list[dict]:
    """Sounds uploaded to the share, as ``file:<name>`` choices."""
    d = Path(settings.root) / "sounds"
    if not settings.configured or not d.is_dir():
        return []
    return [{"name": f.name, "sound": f"file:{f.name}", "bytes": f.stat().st_size}
            for f in sorted(d.glob("*.wav"), key=lambda f: f.name.lower()) if SOUND_FILE_RE.match(f.name)]


def save_sound(settings: Settings, name: str, data: bytes) -> str:
    if not settings.configured:
        raise RuntimeError("Gjallarhorn has no shared folder (Settings → Gjallarhorn)")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).name).strip()
    if not name.lower().endswith(".wav"):
        name += ".wav"
    if not SOUND_FILE_RE.match(name):
        raise ValueError("a sound's name: letters, digits, spaces, . _ - and .wav, up to 80 characters")
    if len(data) > MAX_SOUND_BYTES:
        raise ValueError(f"a sound is at most {MAX_SOUND_BYTES // (1024 * 1024)} MB")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("only .wav files play on every trader's PC without anything installed")
    path = Path(settings.root) / "sounds" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return f"file:{name}"


def delete_sound(settings: Settings, name: str) -> bool:
    if not SOUND_FILE_RE.match(name):
        raise ValueError("unknown sound")
    path = Path(settings.root) / "sounds" / name
    if path.is_file():
        path.unlink()
        return True
    return False


def sound_bytes(settings: Settings, sound: str) -> Optional[bytes]:
    """The .wav behind a horn or an uploaded sound, for a preview in Bifrost."""
    if sound in DEFAULT_SOUNDS.values():
        path = APP_DIR / "sounds" / f"horn-{sound.split(':')[1]}.wav"
    elif sound.startswith("file:") and SOUND_FILE_RE.match(sound[5:]) and settings.configured:
        path = Path(settings.root) / "sounds" / sound[5:]
    else:
        return None
    return path.read_bytes() if path.is_file() else None


def install_command(settings: Settings) -> str:
    """What a trader runs once: the .cmd on the share, given the folder. Who gets
    which alerts is set in Bifrost (groups), not on the PC."""
    root = settings.root.rstrip("\\/") if settings.configured else r"\\server\share\Gjallarhorn"
    return f'"{root}\\app\\install-gjallarhorn.cmd" "{root}"'


def status(settings: Settings, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"configured": settings.configured, "root": settings.root, "reachable": False,
           "app_published": False, "today": 0, "install": install_command(settings)}
    if not settings.configured:
        return out
    root = Path(settings.root)
    try:
        out["reachable"] = root.is_dir()
        out["app_published"] = (root / "app" / "gjallarhorn.ps1").is_file()
        today = root / "alerts" / f"{now:%Y-%m-%d}"
        out["today"] = sum(1 for _ in today.glob("*.json")) if today.is_dir() else 0
    except OSError:
        pass
    return out
