"""Bloomberg through BbgRelay, and a fake for demos and tests.

Heimdall never talks to blpapi itself. It uses the BbgRelay client
(``bbgrelay.Relay``), which reaches a relay server running beside a Terminal.
Every source answers ``fetch(operation, securities, fields, overrides)`` with a
plain table: a ``security`` column, then one column per field.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Protocol

import pandas as pd

from heimdall.core.kdb import DEMO_SYMS, demo_base_price


class BloombergSource(Protocol):
    def fetch(self, operation: str, securities: list[str], fields: list[str],
              overrides: dict[str, str] | None = None) -> pd.DataFrame: ...


def _flatten(df: pd.DataFrame, fields: list[str]) -> pd.DataFrame:
    out = df.copy()
    out.index.name = out.index.name or "security"
    out = out.reset_index()
    if out.columns[0] != "security":
        out = out.rename(columns={out.columns[0]: "security"})
    if fields:
        keep = ["security"] + [f for f in fields if f in out.columns]
        missing = [f for f in fields if f not in out.columns]
        out = out[keep]
        for f in missing:          # a field Bloomberg did not return is a gap, not an error
            out[f] = pd.NA
    return out.reset_index(drop=True)


class RelaySource:
    """A live BbgRelay connection, opened on first use and kept."""

    def __init__(self, url: str, token_env: str = "", timeout: float = 60.0):
        self.url, self.token_env, self.timeout = url, token_env, timeout
        self._relay = None
        self._lock = threading.Lock()

    def _get(self):
        if self._relay is None:
            try:
                from bbgrelay import Relay
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    "the BbgRelay client is not installed on the Huginn machine: "
                    "pip install -e <path-to>/BbgRelay") from exc
            token = os.environ.get(self.token_env) if self.token_env else None
            self._relay = Relay(self.url, token=token, timeout=self.timeout)
        return self._relay

    def fetch(self, operation, securities, fields, overrides=None):
        with self._lock:
            relay = self._get()
        if operation == "snap":
            df = relay.snap(securities)
        else:
            df = relay.bdp(securities, fields, overrides=overrides or None)
        return _flatten(df, fields)

    def ping(self) -> dict:
        return self._get().ping()


class FakeBloomberg:
    """Deterministic made-up Bloomberg data.

    Securities are read as "<ticker> <market> <yellow key>". For the demo
    tickers the prices match the mock KDB server's, and any field mentioning a
    limit answers ±10% of the base price, so a demo alert can compare KDB
    prices with Bloomberg limits and actually trip.
    """

    def fetch(self, operation, securities, fields, overrides=None):
        if operation == "snap" and not fields:
            fields = ["LAST_PRICE", "BID", "ASK"]
        rows = []
        for sec in securities:
            row = {"security": sec}
            for f in fields:
                row[f] = self._value(sec, f)
            rows.append(row)
        return pd.DataFrame(rows, columns=["security", *fields])

    @staticmethod
    def _base(sec: str) -> float:
        ticker = sec.split()[0].upper() if sec.strip() else ""
        if ticker in DEMO_SYMS:
            return demo_base_price(DEMO_SYMS.index(ticker))
        digest = int(hashlib.sha256(ticker.encode()).hexdigest()[:6], 16)
        return round(10 + digest % 490, 2)

    def _value(self, sec: str, field: str):
        f = field.upper()
        base = self._base(sec)
        if "LIMIT" in f and ("UP" in f or "HIGH" in f):
            return round(base * 1.10, 2)
        if "LIMIT" in f and ("DOWN" in f or "LOW" in f):
            return round(base * 0.90, 2)
        if f in ("NAME", "SECURITY_NAME", "LONG_COMP_NAME"):
            return sec.split()[0].title() + " Corp"
        if f in ("CRNCY", "CURRENCY"):
            return "USD"
        if f in ("PX_LAST", "LAST_PRICE", "PX_CLOSE_1D", "PX_OPEN"):
            return base
        if f == "BID":
            return round(base - 0.05, 2)
        if f == "ASK":
            return round(base + 0.05, 2)
        if "VOLUME" in f:
            return int(base * 1000)
        digest = int(hashlib.sha256(f"{sec}|{f}".encode()).hexdigest()[:6], 16)
        return round(digest % 10000 / 100, 2)

    def ping(self) -> dict:
        return {"pong": True, "ready": True, "fake": True}
