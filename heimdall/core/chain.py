"""Running an alert's steps in order, each able to use the tables before it."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import pandas as pd

from heimdall.core import compute, refs, scripts
from heimdall.core.bloomberg import BloombergSource, BpipeSource, FakeBloomberg
from heimdall.core.kdb import KdbClient, MockKdbClient, PyKxClient, build_q
from heimdall.core.models import (Alert, BloombergStep, ComputeStep, Connection, KdbStep,
                                  Script, ScriptStep)


class Resources(Protocol):
    def kdb(self, name: str) -> KdbClient: ...
    def bloomberg(self, name: str) -> BloombergSource: ...
    def script(self, name: str) -> Script: ...


class StepFailed(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


@dataclass
class StepOutcome:
    name: str
    kind: str
    request: str = ""                      # the resolved query / securities / command
    df: Optional[pd.DataFrame] = None
    error: Optional[str] = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> dict:
        return {"name": self.name, "kind": self.kind, "request": self.request,
                "rows": None if self.df is None else len(self.df),
                "columns": [] if self.df is None else [str(c) for c in self.df.columns],
                "error": self.error, "duration": round(self.duration, 3)}


# --------------------------------------------------------------------------
# One step
# --------------------------------------------------------------------------

def _run_kdb(step: KdbStep, outputs, res: Resources) -> tuple[str, pd.DataFrame]:
    q = refs.fill_q(build_q(step), outputs)
    if not q:
        raise ValueError("the query is empty")
    return q, res.kdb(step.connection).query(q)


def _run_bloomberg(step: BloombergStep, outputs, res: Resources) -> tuple[str, pd.DataFrame]:
    keyed = refs.expand_lines_keyed(step.securities, outputs)
    keys: dict[str, dict] = {}
    for sec, vals in keyed:
        keys.setdefault(sec, vals)
    securities = list(keys)
    fields = [f.strip() for f in step.fields if f.strip()]
    overrides = {k: refs.fill_text(v, outputs) for k, v in step.overrides.items() if k.strip()}
    request = f"{step.operation} {len(securities)} securities × {', '.join(fields) or 'default fields'}"
    if securities:
        request += ": " + ", ".join(securities[:8]) + (" …" if len(securities) > 8 else "")
    if not securities:
        # Nothing to ask about (an upstream step returned no rows) is an empty
        # answer, not an error: "no orders, so no limits to check".
        return request, pd.DataFrame(columns=["security", *fields])
    if step.operation == "bdp" and not fields:
        raise ValueError("a bdp request needs at least one field")
    df = res.bloomberg(step.connection).fetch(step.operation, securities, fields, overrides)
    return request, _attach_keys(df, keys)


def _attach_keys(df: pd.DataFrame, keys: dict[str, dict]) -> pd.DataFrame:
    """Put the values each security was built from beside it (``sym`` next to
    ``AAPL US Equity``), so the result joins back to its source step."""
    names = list(dict.fromkeys(k for vals in keys.values() for k in vals))
    names = [n for n in names if n not in df.columns]
    if not names or "security" not in df.columns:
        return df
    df = df.copy()
    pos = 1
    for n in names:
        df.insert(pos, n, df["security"].map(lambda s, n=n: keys.get(s, {}).get(n)))
        pos += 1
    return df


def _run_script(step: ScriptStep, outputs, res: Resources) -> tuple[str, pd.DataFrame]:
    script = res.script(step.script)
    given = {k: refs.fill_text(v, outputs) for k, v in step.args.items()}
    values = scripts.resolve_params(script, given)
    stdin = None
    if step.stdin_step:
        if step.stdin_step not in outputs:
            raise ValueError(f"unknown stdin step '{step.stdin_step}'")
        stdin = outputs[step.stdin_step].to_json(orient="records", date_format="iso")
    argv = scripts.build_argv(script, values)
    result = scripts.run(script, values, stdin=stdin)
    return " ".join(_quote(a) for a in argv), scripts.parse_output(script, result)


def _quote(a: str) -> str:
    return f'"{a}"' if (" " in a or a == "") else a


def _run_compute(step: ComputeStep, outputs, res: Resources) -> tuple[str, pd.DataFrame]:
    parts = [f"from {step.source}"]
    if step.join_step:
        parts.append(f"{step.join_how} join {step.join_step} on "
                     + ", ".join(f"{l}={r}" for l, r in zip(step.left_on, step.right_on or step.left_on)))
    parts += [f"{d.column} = {d.expr}" for d in step.derive]
    if step.where.strip():
        parts.append(f"where {step.where.strip()}")
    if step.keep:
        parts.append("keep " + ", ".join(step.keep))
    return "; ".join(parts), compute.run_compute(step, outputs)


_RUNNERS: dict[str, Callable] = {"kdb": _run_kdb, "bloomberg": _run_bloomberg,
                                 "script": _run_script, "compute": _run_compute}


def run_steps(alert: Alert, res: Resources, stop_on_error: bool = True) -> list[StepOutcome]:
    """Every step's outcome. The chain stops at the first failure — later steps
    would only fail again for want of its table."""
    outputs: dict[str, pd.DataFrame] = {}
    results: list[StepOutcome] = []
    for step in alert.steps:
        started = time.monotonic()
        outcome = StepOutcome(step.name, step.kind)
        try:
            outcome.request, df = _RUNNERS[step.kind](step, outputs, res)
            if not isinstance(df, pd.DataFrame):
                df = pd.DataFrame(df)
            outcome.df = df
            outputs[step.name] = df
        except Exception as exc:  # noqa: BLE001 - every failure is reported, none escapes
            outcome.error = _message(exc)
        outcome.duration = time.monotonic() - started
        results.append(outcome)
        if outcome.error and stop_on_error:
            break
    return results


def _message(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    if isinstance(exc, KeyError) and text.startswith("'") and text.endswith("'"):
        text = text[1:-1]
    return text


# --------------------------------------------------------------------------
# Checks before saving
# --------------------------------------------------------------------------

def validate(alert: Alert, connections: dict[str, Connection], scripts_by_name: dict[str, Script]) -> list[str]:
    """Everything wrong with an alert that can be found without running it."""
    problems: list[str] = []
    if not alert.steps:
        problems.append("The alert has no steps.")
    seen: list[str] = []
    for i, step in enumerate(alert.steps, start=1):
        label = f"Step {i} ({step.name})"
        if step.name in seen:
            problems.append(f"{label}: another step already has this name.")
        texts: list[str] = []
        if isinstance(step, KdbStep):
            _need_conn(problems, label, step.connection, "kdb", connections)
            if step.mode == "raw":
                if not step.raw_q.strip():
                    problems.append(f"{label}: the q query is empty.")
                texts.append(step.raw_q)
            else:
                if not step.table:
                    problems.append(f"{label}: choose a table.")
                texts += [str(f.value) for f in step.filters]
        elif isinstance(step, BloombergStep):
            _need_conn(problems, label, step.connection, "bloomberg", connections)
            if not step.securities.strip():
                problems.append(f"{label}: list at least one security.")
            if step.operation == "bdp" and not [f for f in step.fields if f.strip()]:
                problems.append(f"{label}: a bdp request needs at least one field.")
            texts += [step.securities, *step.overrides.values()]
            if "{{table:" in step.securities:
                problems.append(f"{label}: {{{{table:...}}}} only works inside a q query.")
        elif isinstance(step, ScriptStep):
            sc = scripts_by_name.get(step.script)
            if not step.script:
                problems.append(f"{label}: choose a registered script.")
            elif sc is None:
                problems.append(f"{label}: no registered script called '{step.script}'.")
            else:
                declared = {p.name for p in sc.params}
                for k in step.args:
                    if k not in declared:
                        problems.append(f"{label}: '{step.script}' has no parameter '{k}'.")
                for p in sc.params:
                    if p.required and not step.args.get(p.name, "").strip() and not p.default:
                        problems.append(f"{label}: '{p.name}' needs a value.")
            texts += list(step.args.values())
            if step.stdin_step and step.stdin_step not in seen:
                problems.append(f"{label}: stdin step '{step.stdin_step}' must come earlier.")
        elif isinstance(step, ComputeStep):
            for ref, what in ((step.source, "source"), (step.join_step, "join step")):
                if what == "source" and not ref:
                    problems.append(f"{label}: choose the step to start from.")
                elif ref and ref not in seen:
                    problems.append(f"{label}: {what} '{ref}' must be an earlier step.")
            if step.join_step and not step.left_on:
                problems.append(f"{label}: choose the columns to join on.")
            problems += [f"{label}: {p.split(': ', 1)[-1]}" for p in compute.vet(step)]
        for t in texts:
            for ref in refs.referenced_steps(t):
                if ref not in seen:
                    problems.append(f"{label}: refers to '{ref}', which is not an earlier step.")
        seen.append(step.name)
    target = alert.condition.step
    if target and target not in seen:
        problems.append(f"The condition judges step '{target}', which does not exist.")
    if alert.condition.type in ("any_row", "all_rows", "aggregate") and not alert.condition.column:
        problems.append("The condition needs a column.")
    return problems


def _need_conn(problems, label, name, kind, connections):
    if not name:
        problems.append(f"{label}: choose a connection.")
        return
    conn = connections.get(name)
    if conn is None:
        problems.append(f"{label}: no connection called '{name}'.")
    elif conn.kind != kind:
        problems.append(f"{label}: '{name}' is a {conn.kind} connection, not {kind}.")


# --------------------------------------------------------------------------
# The real resources
# --------------------------------------------------------------------------

class Pool:
    """Clients for named connections, made on first use and kept.

    ``lookup`` returns the current definition of a connection or script, so an
    edit made in Bifrost is seen on the next run: a client is rebuilt when its
    connection's address changes.
    """

    def __init__(self, connections: Callable[[str], Optional[Connection]],
                 scripts_: Callable[[str], Optional[Script]],
                 kdb_factory: Callable[[str, int], KdbClient] = PyKxClient,
                 bpipe_factory: Callable[[Connection], BloombergSource] | None = None):
        self._connections = connections
        self._scripts = scripts_
        self._kdb_factory = kdb_factory
        self._bpipe_factory = bpipe_factory or (lambda c: BpipeSource(c.host, c.port, c.app_name))
        self._cache: dict[str, tuple[tuple, object]] = {}
        self._lock = threading.Lock()

    def _conn(self, name: str, kind: str) -> Connection:
        conn = self._connections(name)
        if conn is None:
            raise LookupError(f"no connection called '{name}'")
        if conn.kind != kind:
            raise LookupError(f"'{name}' is a {conn.kind} connection, not {kind}")
        return conn

    def _cached(self, name: str, key: tuple, make: Callable[[], object]):
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] == key:
                return hit[1]
        client = make()
        with self._lock:
            old = self._cache.get(name)
            self._cache[name] = (key, client)
        if old and old[1] is not client:
            _close(old[1])          # e.g. a B-PIPE session to an address that was edited away
        return client

    def kdb(self, name: str) -> KdbClient:
        c = self._conn(name, "kdb")
        if c.host.strip().lower() == "demo":
            return self._cached(name, ("demo",), MockKdbClient)
        return self._cached(name, (c.host, c.port), lambda: self._kdb_factory(c.host, c.port))

    def bloomberg(self, name: str) -> BloombergSource:
        c = self._conn(name, "bloomberg")
        if c.host.strip().lower() == "demo":
            return self._cached(name, ("demo",), FakeBloomberg)
        return self._cached(name, (c.host, c.port, c.app_name), lambda: self._bpipe_factory(c))

    def script(self, name: str) -> Script:
        s = self._scripts(name)
        if s is None:
            raise LookupError(f"no registered script called '{name}'")
        return s

    def for_one(self, conn: Connection) -> "Pool":
        """A separate pool that knows only ``conn`` (e.g. one not saved yet), made
        with the same client factories as this one."""
        return Pool(lambda name: conn if name == conn.name else None, self._scripts,
                    kdb_factory=self._kdb_factory, bpipe_factory=self._bpipe_factory)

    def forget(self, name: str) -> None:
        with self._lock:
            hit = self._cache.pop(name, None)
        if hit:
            _close(hit[1])


def _close(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing a client that is already broken
            pass


def frame_to_records(df: Optional[pd.DataFrame], limit: int) -> dict:
    """A table as JSON-safe columns + rows, capped, for history and the API."""
    if df is None:
        return {"columns": [], "rows": [], "total": 0}
    head = df.head(limit)
    data = json.loads(head.to_json(orient="split", date_format="iso", default_handler=str))
    return {"columns": [str(c) for c in data["columns"]], "rows": data["data"], "total": len(df)}


def records_to_frame(rec: Optional[dict]) -> pd.DataFrame:
    if not rec:
        return pd.DataFrame()
    return pd.DataFrame(rec.get("rows") or [], columns=rec.get("columns") or [])
