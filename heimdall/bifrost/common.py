"""What every Bifrost page shares: the Huginn client, status vocabulary, small helpers."""
from __future__ import annotations

import getpass
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import pandas as pd
import streamlit as st

from heimdall import config as config_mod
from heimdall.client import HuginnClient, HuginnError, HuginnUnreachable

# One meaning per colour, everywhere: see .streamlit/config.toml.
STATUS = {
    "triggered": ("Fired", "red", ":material/notifications_active:"),
    "ok": ("OK", "green", ":material/check_circle:"),
    "error": ("Error", "orange", ":material/error:"),
    "off": ("Off", "gray", ":material/pause_circle:"),
    "never": ("Not run yet", "gray", ":material/schedule:"),
    "running": ("Running", "blue", ":material/progress_activity:"),
    "off_hours": ("Off-hours", "violet", ":material/bedtime:"),
}
KIND = {"kdb": ("KDB", ":material/database:"), "bloomberg": ("Bloomberg", ":material/candlestick_chart:"),
        "script": ("Script", ":material/terminal:"), "compute": ("Compute", ":material/function:")}
SOURCE = {"interval": "Interval", "schedule": "Schedule", "manual": "By hand"}


@st.cache_resource
def _client(url: str, token: str, user: str) -> HuginnClient:
    return HuginnClient(url, token=token, user=user)


def cfg():
    return config_mod.load()


def user() -> str:
    try:
        return os.environ.get("HEIMDALL_USER") or getpass.getuser()
    except Exception:  # noqa: BLE001
        return "bifrost"


def huginn() -> HuginnClient:
    c = cfg()
    return _client(c.huginn_url, c.token, user())


def call(fn: Callable[[], Any], *, quiet: bool = False) -> Any:
    """Run an API call; show an error in place instead of a traceback."""
    try:
        return fn()
    except HuginnUnreachable as exc:
        if not quiet:
            unreachable(str(exc))
        return None
    except HuginnError as exc:
        if not quiet:
            if len(exc.problems) > 1:
                st.error("**Huginn refused this:**\n\n" + "\n".join(f"- {p}" for p in exc.problems),
                         icon=":material/block:")
            else:
                st.error(str(exc), icon=":material/block:")
        return None


def unreachable(detail: str) -> None:
    st.error(f"**Huginn is not answering.** Bifrost reads and writes everything through it.\n\n"
             f"Start it with `python -m heimdall.huginn` (add `--demo` for sample alerts), "
             f"then refresh.\n\n`{detail}`", icon=":material/cloud_off:")


@st.cache_data(ttl=60, show_spinner=False)
def kdb_tables(connection: str) -> list[str]:
    return huginn().get(f"/connections/{connection}/tables")


@st.cache_data(ttl=60, show_spinner=False)
def kdb_columns(connection: str, table: str) -> list[str]:
    return huginn().get(f"/connections/{connection}/columns", table=table)


# -- formatting -----------------------------------------------------------------

def parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(ts: Optional[str], now: Optional[datetime] = None) -> str:
    dt = parse(ts)
    if dt is None:
        return "never"
    secs = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 60:
        text = f"{secs}s"
    elif secs < 3600:
        text = f"{secs // 60}m"
    elif secs < 86400:
        text = f"{secs // 3600}h {secs % 3600 // 60}m"
    else:
        text = f"{secs // 86400}d"
    return f"in {text}" if future else f"{text} ago"


def local(ts: Optional[str], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = parse(ts)
    return dt.astimezone().strftime(fmt) if dt else ""


def every(secs: int) -> str:
    if not secs:
        return "Schedule only"
    if secs % 3600 == 0:
        return f"Every {secs // 3600}h"
    if secs % 60 == 0:
        return f"Every {secs // 60} min"
    return f"Every {secs}s"


def status_key(row: dict) -> str:
    if row.get("running"):
        return "running"
    if not row.get("enabled"):
        return "off"
    if row.get("off_hours"):
        return "off_hours"
    return row.get("last_status") or "never"


def badge(key: str) -> str:
    label, colour, icon = STATUS[key]
    return f":{colour}-badge[{icon} {label}]"


def table(rec: Optional[dict]) -> pd.DataFrame:
    if not rec:
        return pd.DataFrame()
    return pd.DataFrame(rec.get("rows") or [], columns=rec.get("columns") or [])


def seed(key: str, value: Any) -> str:
    """Give a widget its starting value once, then let it own the value."""
    if key not in st.session_state:
        st.session_state[key] = value
    return key


def page_header(title: str, caption: str = "") -> None:
    st.markdown(f"## {title}")
    if caption:
        st.caption(caption)


_DOT_COLOURS = {"Fired": "#f06470", "Error": "#e8a33d", "OK": "#3ecf8e", "Running": "#5aa9e6", "Off-hours": "#a78bfa"}


def coloured(df: pd.DataFrame, column: str):
    """A styled frame whose status column carries its status colour (text, so it is not colour alone)."""
    def colour(v):
        for word, hexa in _DOT_COLOURS.items():
            if isinstance(v, str) and word in v:
                return f"color: {hexa}"
        return "color: #8b8ea0"
    return df.style.map(colour, subset=[column])


def height(rows: int, cap: int = 520) -> int:
    return min(38 + 35 * max(rows, 1), cap)


# -- who Gjallarhorn's pop-ups go to ------------------------------------------------

def audience_options(groups: dict, seen: list, *extra) -> list[str]:
    """Groups first, then traders running Gjallarhorn, then any other name in use."""
    group_names = sorted(groups, key=str.lower)
    lower = {g.lower() for g in group_names}
    others = {u["user"] for u in seen} | {m for g in groups.values() for m in g}
    for more in extra:
        others |= set(more)
    return group_names + sorted((o for o in others if o and o.lower() not in lower), key=str.lower)


def audience_label(name: str, groups: dict) -> str:
    members = next((v for k, v in groups.items() if k.lower() == str(name).lower()), None)
    return f"Group: {name} ({len(members)})" if members is not None else str(name)


def audience_reach(names: list, groups: dict) -> list[str]:
    """The Windows user names a pop-up for these names reaches (what Huginn sends)."""
    by = {k.lower(): v for k, v in groups.items()}
    out, low = [], set()
    for n in names:
        for who in by.get(str(n).lower(), [n]):
            if who.lower() not in low:
                low.add(who.lower())
                out.append(who)
    return out
