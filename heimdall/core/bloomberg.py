"""Bloomberg through B-PIPE, and a fake for demos and tests.

Heimdall talks to B-PIPE itself with Bloomberg's ``blpapi`` library: a session
to the B-PIPE host and port, authorised as an application (``app_name``).
Every source answers ``fetch(operation, securities, fields, overrides)`` with a
plain table: a ``security`` column, then one column per field.

* ``bdp``: reference and current values, a ReferenceDataRequest on ``//blp/refdata``.
* ``snap``: live values, a short subscription on ``//blp/mktdata`` that keeps the
  first value of each field and then unsubscribes.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Protocol

import pandas as pd

from heimdall.core.kdb import DEMO_SYMS, demo_base_price

SNAP_FIELDS = ["LAST_PRICE", "BID", "ASK"]
BLPAPI_INSTALL = ("pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ "
                  "blpapi")


class BloombergSource(Protocol):
    def fetch(self, operation: str, securities: list[str], fields: list[str],
              overrides: dict[str, str] | None = None) -> pd.DataFrame: ...


def _blpapi():
    try:
        import blpapi
    except ImportError as exc:  # pragma: no cover - depends on install
        raise RuntimeError("Bloomberg's blpapi is not installed on the Huginn machine: "
                           + BLPAPI_INSTALL) from exc
    return blpapi


def _value(element):
    """A plain Python value from a blpapi field element (dates and times become ISO text)."""
    try:
        v = element.getValue()
    except Exception:  # noqa: BLE001 - arrays and bulk fields: keep them readable
        return str(element)
    return v.isoformat() if hasattr(v, "isoformat") else v


def _table(rows: dict[str, dict], fields: list[str]) -> pd.DataFrame:
    # A field Bloomberg did not return is a gap, not an error.
    return pd.DataFrame([{"security": s, **{f: r.get(f, pd.NA) for f in fields}}
                         for s, r in rows.items()], columns=["security", *fields])


class BpipeSource:
    """A B-PIPE session, opened and authorised on first use and kept.

    Requests go through the session one at a time (a lock), which keeps the
    event handling simple; Huginn's alerts are small and spaced out. A session
    that breaks is dropped and reopened on the next request.
    """

    def __init__(self, host: str, port: int, app_name: str, timeout: float = 60.0,
                 snap_wait: float = 10.0):
        self.host, self.port, self.app_name = host.strip(), int(port or 0), app_name.strip()
        self.timeout, self.snap_wait = timeout, snap_wait
        self._session = None
        self._lock = threading.Lock()

    # -- the session -------------------------------------------------------------

    def _open(self):
        if not self.host or not self.port:
            raise RuntimeError("a B-PIPE connection needs a host and a port")
        if not self.app_name:
            raise RuntimeError("a B-PIPE connection needs an application name")
        blpapi = _blpapi()
        opts = blpapi.SessionOptions()
        opts.setServerAddress(self.host, self.port, 0)
        opts.setSessionIdentityOptions(blpapi.AuthOptions.createWithApp(appName=self.app_name))
        session = blpapi.Session(opts)
        if not session.start():
            raise RuntimeError(f"could not start a B-PIPE session at {self.host}:{self.port} as "
                               f"'{self.app_name}': {self._said(session)}")
        for service in ("//blp/refdata", "//blp/mktdata"):
            if not session.openService(service):
                why = self._said(session)
                session.stop()
                raise RuntimeError(f"B-PIPE at {self.host}:{self.port} would not open {service} "
                                   f"for '{self.app_name}': {why}")
        return session

    @staticmethod
    def _said(session) -> str:
        """What the session reported while failing (unreachable host, refused app name...)."""
        said = []
        while True:
            ev = session.tryNextEvent()
            if ev is None:
                break
            said += [f"{msg.messageType()} {msg}".strip() for msg in ev]
        return " | ".join(said) or "no reason given"

    def _drop(self) -> None:
        if self._session is not None:
            try:
                self._session.stop()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    # -- requests ----------------------------------------------------------------

    def fetch(self, operation, securities, fields, overrides=None):
        with self._lock:
            if self._session is None:
                self._session = self._open()
            try:
                if operation == "snap":
                    return self._snap(self._session, securities, list(fields) or list(SNAP_FIELDS))
                return self._bdp(self._session, securities, fields, overrides or {})
            except Exception:
                self._drop()        # reopened on the next request
                raise

    def _bdp(self, session, securities, fields, overrides):
        blpapi = _blpapi()
        req = session.getService("//blp/refdata").createRequest("ReferenceDataRequest")
        for s in securities:
            req.append("securities", s)
        for f in fields:
            req.append("fields", f)
        if overrides:
            ovs = req.getElement("overrides")
            for k, v in overrides.items():
                ov = ovs.appendElement()
                ov.setElement("fieldId", k)
                ov.setElement("value", str(v))
        cid = session.sendRequest(req)
        rows: dict[str, dict] = {s: {} for s in securities}
        problems: list[str] = []
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"B-PIPE did not answer within {self.timeout:.0f}s")
            ev = session.nextEvent(max(1, int(left * 1000)))
            for msg in ev:
                if cid not in msg.correlationIds():
                    continue
                if msg.hasElement("responseError"):
                    raise RuntimeError(f"B-PIPE refused the request: {msg.getElement('responseError')}")
                data = msg.getElement("securityData")
                for i in range(data.numValues()):
                    sd = data.getValueAsElement(i)
                    sec = sd.getElementAsString("security")
                    row = rows.setdefault(sec, {})
                    if sd.hasElement("securityError"):
                        problems.append(f"{sec}: {sd.getElement('securityError').getElementAsString('message')}")
                        continue
                    fd = sd.getElement("fieldData")
                    for f in fields:
                        if fd.hasElement(f) and not fd.getElement(f).isNull():
                            row[f] = _value(fd.getElement(f))
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        if problems and len(problems) >= len(securities):
            raise RuntimeError("; ".join(problems))
        return _table(rows, fields)

    def _snap(self, session, securities, fields):
        blpapi = _blpapi()
        subs = blpapi.SubscriptionList()
        by_id: dict[int, str] = {}
        for i, sec in enumerate(securities, start=1):
            by_id[i] = sec
            subs.add(sec, fields, "", blpapi.CorrelationId(i))
        rows: dict[str, dict] = {s: {} for s in securities}
        done: set[str] = set()
        problems: list[str] = []
        session.subscribe(subs)
        try:
            deadline = time.monotonic() + self.snap_wait
            while len(done) < len(securities):
                left = deadline - time.monotonic()
                if left <= 0:
                    break           # what has arrived is the answer; the rest are gaps
                ev = session.nextEvent(max(1, int(left * 1000)))
                for msg in ev:
                    sec = next((by_id[c.value()] for c in msg.correlationIds()
                                if c.value() in by_id), None)
                    if sec is None:
                        continue
                    kind = str(msg.messageType())
                    if kind in ("SubscriptionFailure", "SubscriptionTerminated"):
                        problems.append(f"{sec}: {kind} {msg}".strip())
                        done.add(sec)
                        continue
                    if ev.eventType() != blpapi.Event.SUBSCRIPTION_DATA:
                        continue
                    row = rows[sec]
                    for f in fields:
                        if f not in row and msg.hasElement(f) and not msg.getElement(f).isNull():
                            row[f] = _value(msg.getElement(f))
                    if all(f in row for f in fields):
                        done.add(sec)
        finally:
            session.unsubscribe(subs)
        if problems and len(problems) >= len(securities):
            raise RuntimeError("; ".join(problems))
        return _table(rows, fields)

    def ping(self) -> dict:
        with self._lock:
            if self._session is None:
                self._session = self._open()
        return {"pong": True, "ready": True, "server": f"{self.host}:{self.port}", "app": self.app_name}


class FakeBloomberg:
    """Deterministic made-up Bloomberg data.

    Securities are read as "<ticker> <market> <yellow key>". For the demo
    tickers the prices match the mock KDB server's, and any field mentioning a
    limit answers ±10% of the base price, so a demo alert can compare KDB
    prices with Bloomberg limits and actually trip.
    """

    def fetch(self, operation, securities, fields, overrides=None):
        if operation == "snap" and not fields:
            fields = list(SNAP_FIELDS)
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
