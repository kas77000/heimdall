"""Talking to kdb+: the client contract, the real pykx client, and a mock.

Every client answers ``query(q) -> DataFrame``. Results pass through
``nulls_to_nan`` on arrival, because a kdb+ integer null *is* a value (the
lowest the width can hold) and pandas would happily sum it.
"""
from __future__ import annotations

import math
import re
import threading
from datetime import datetime, timezone
from typing import Callable, Protocol

import pandas as pd

from heimdall.core.models import Filter, KdbStep
from heimdall.core.qfmt import format_q_list, format_q_value

Q_INT_NULLS = {"int16": -32768, "int32": -2147483648, "int64": -9223372036854775808}


class KdbClient(Protocol):
    def query(self, q: str) -> pd.DataFrame: ...


def _scrub(values: pd.Series):
    sentinel = Q_INT_NULLS.get(str(values.dtype).lower())
    if sentinel is None:
        return None
    hit = values == sentinel
    return values.astype("float64").mask(hit) if hit.any() else None


def nulls_to_nan(data):
    """kdb+ integer nulls as NaN; untouched when there are none (or no table)."""
    if data is None:
        return data
    if isinstance(data, pd.Series):
        scrubbed = _scrub(data)
        return data if scrubbed is None else scrubbed
    if not isinstance(data, pd.DataFrame) or data.empty:
        return data
    out = data
    for col in data.columns:
        scrubbed = _scrub(data[col])
        if scrubbed is None:
            continue
        if out is data:
            out = data.copy()
        out[col] = scrubbed
    return out


def as_frame(data) -> pd.DataFrame:
    """Whatever q answered, as a table: a vector becomes one column, an atom one cell."""
    if isinstance(data, pd.DataFrame):
        return data.reset_index() if _has_named_index(data) else data
    if isinstance(data, pd.Series):
        return data.to_frame(name=data.name or "value").reset_index(drop=True)
    if data is None:
        return pd.DataFrame()
    if isinstance(data, (list, tuple)):
        return pd.DataFrame({"value": list(data)})
    return pd.DataFrame({"value": [data]})


def _has_named_index(df: pd.DataFrame) -> bool:
    # A keyed q table arrives with its keys on the index; a step result should
    # carry them as ordinary columns so later steps can reference them.
    names = [n for n in (df.index.names or []) if n is not None]
    return bool(names)


class PyKxClient:
    """Real client over a pykx IPC connection, reopened once if it drops."""

    def __init__(self, host: str, port: int):
        import pykx as kx  # imported lazily: tests and demo mode never need it
        self._kx = kx
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self):
        # Opened exactly as KdbMonitor opens it, which works against the real servers.
        return self._kx.SyncQConnection(host=self.host, port=self.port)

    def query(self, q: str) -> pd.DataFrame:
        with self._lock:
            try:
                answer = self._conn(q)
            except Exception:
                self._conn = self._open()
                answer = self._conn(q)
        return as_frame(nulls_to_nan(answer.pd()))


# --------------------------------------------------------------------------
# Building the query a guided step describes
# --------------------------------------------------------------------------

def filter_clause(f: Filter) -> str:
    if f.op == "in":
        values = f.value if isinstance(f.value, list) else [
            v.strip() for v in str(f.value).split(",") if v.strip()]
        clause = f"{f.column} in {format_q_list(values, f.value_type)}"
    elif f.op == "like":
        clause = f"{f.column} like {format_q_value(f.value, 'string')}"
    else:
        clause = f"{f.column}{f.op}{format_q_value(f.value, f.value_type)}"
    return f"not {clause}" if f.negated else clause


def build_q(step: KdbStep) -> str:
    if step.mode == "raw":
        return (step.raw_q or "").strip()
    if not step.table:
        raise ValueError(f"step '{step.name}' has no table")
    cols = ", ".join(c for c in step.columns if c.strip())
    base = f"select {cols} from {step.table}" if cols else f"select from {step.table}"
    if not step.filters:
        return base
    return base + " where " + ", ".join(filter_clause(f) for f in step.filters)


# --------------------------------------------------------------------------
# Mock server
# --------------------------------------------------------------------------

DEMO_SYMS = ["AAPL", "MSFT", "GOOG", "AMZN", "TSLA"]
DEMO_SCHEMA: dict[str, list[str]] = {
    "quote": ["sym", "bid", "ask", "last", "volume", "time"],
    "orders": ["orderId", "sym", "side", "qty", "limitPrice", "algo", "state"],
    "fills": ["orderId", "sym", "fillQty", "fillPrice", "time"],
}


def _phase() -> int:
    t = datetime.now(timezone.utc)
    return t.hour * 3600 + t.minute * 60 + t.second


def demo_base_price(i: int) -> float:
    return 100.0 + i * 25.0


def _quote() -> pd.DataFrame:
    phase = _phase()
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    rows = []
    for i, s in enumerate(DEMO_SYMS):
        base = demo_base_price(i)
        # Swings up to ±12% so prices cross the demo limits (±10%) now and then.
        last = round(base * (1 + 0.12 * math.sin(phase / 40.0 + i * 1.3)), 2)
        rows.append({"sym": s, "bid": round(last - 0.05, 2), "ask": round(last + 0.05, 2),
                     "last": last, "volume": 1000 * (i + 1) + phase % 700, "time": stamp})
    return pd.DataFrame(rows)


def _orders() -> pd.DataFrame:
    return pd.DataFrame([
        {"orderId": 1001, "sym": "AAPL", "side": "buy", "qty": 5000, "limitPrice": 105.0, "algo": "VWAP", "state": "working"},
        {"orderId": 1002, "sym": "MSFT", "side": "sell", "qty": 3000, "limitPrice": 120.0, "algo": "TWAP", "state": "working"},
        {"orderId": 1003, "sym": "GOOG", "side": "buy", "qty": 1200, "limitPrice": 151.0, "algo": "IS", "state": "new"},
        {"orderId": 1004, "sym": "TSLA", "side": "sell", "qty": 800, "limitPrice": 199.0, "algo": "POV", "state": "working"},
        {"orderId": 1005, "sym": "AMZN", "side": "buy", "qty": 2500, "limitPrice": 176.0, "algo": "VWAP", "state": "done"},
    ])


def _fills() -> pd.DataFrame:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    return pd.DataFrame([
        {"orderId": 1001, "sym": "AAPL", "fillQty": 1200, "fillPrice": 101.3, "time": stamp},
        {"orderId": 1002, "sym": "MSFT", "fillQty": 3000, "fillPrice": 124.9, "time": stamp},
        {"orderId": 1005, "sym": "AMZN", "fillQty": 2500, "fillPrice": 175.2, "time": stamp},
    ])


_TABLES: dict[str, Callable[[], pd.DataFrame]] = {
    "quote": _quote, "orders": _orders, "fills": _fills}

_SELECT = re.compile(r"^\s*select\s+(?P<cols>.*?)\s*from\s+(?P<table>\w+)(?:\s+where\s+(?P<where>.*))?$",
                     re.IGNORECASE | re.DOTALL)
_SYM_IN = re.compile(r"\bsym\s+in\s+(?:enlist\s+)?((?:`\w+)+)")
_SYM_EQ = re.compile(r"\bsym\s*=\s*`(\w+)")


class MockKdbClient:
    """Answers ``tables[]``, ``cols `t`` and simple selects against demo tables.

    Selects honour a column list and ``sym in``/``sym=`` filters; anything else
    in a where clause is ignored (it is a demo, not an interpreter). Prices move
    with the wall clock so alerts flip between armed and triggered.
    """

    def query(self, q: str) -> pd.DataFrame:
        text = q.strip()
        if text in ("tables[]", "tables`."):
            return pd.DataFrame({"value": list(DEMO_SCHEMA)})
        m = re.match(r"^cols\s*`(\w+)$", text)
        if m:
            return pd.DataFrame({"value": DEMO_SCHEMA.get(m.group(1), [])})
        if text.startswith("count "):
            inner = self.query(text[len("count "):])
            return pd.DataFrame({"value": [len(inner)]})
        m = _SELECT.match(text)
        if not m or m.group("table") not in _TABLES:
            raise ValueError(f"mock kdb cannot answer: {q}")
        df = _TABLES[m.group("table")]()
        where = m.group("where") or ""
        syms = None
        if (mi := _SYM_IN.search(where)):
            syms = [s for s in mi.group(1).split("`") if s]
        elif (me := _SYM_EQ.search(where)):
            syms = [me.group(1)]
        if syms is not None:
            df = df[df["sym"].isin(syms)]
        cols = [c.strip() for c in (m.group("cols") or "").split(",") if c.strip()]
        if cols:
            df = df[[c for c in cols if c in df.columns]]
        return df.reset_index(drop=True)


class FakeKdbClient:
    """Test double: canned frames keyed by exact query text."""

    def __init__(self, responses: dict[str, pd.DataFrame]):
        self.responses = responses
        self.calls: list[str] = []

    def query(self, q: str) -> pd.DataFrame:
        self.calls.append(q)
        if q not in self.responses:
            raise KeyError(f"no canned response for: {q}")
        return self.responses[q]


def list_tables(client: KdbClient) -> list[str]:
    df = client.query("tables[]")
    return [str(v) for v in df.iloc[:, 0].tolist()] if not df.empty else []


def list_columns(client: KdbClient, table: str) -> list[str]:
    df = client.query(f"cols `{table}")
    return [str(v) for v in df.iloc[:, 0].tolist()] if not df.empty else []
