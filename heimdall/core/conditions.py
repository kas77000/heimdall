"""Whether a table meets an alert's condition, and how to say what it checks."""
from __future__ import annotations

import operator
from datetime import datetime
from typing import Optional

import pandas as pd

from heimdall.core.models import Condition, Rearm

_OPS = {"=": operator.eq, "<>": operator.ne, "<": operator.lt,
        "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_AGG = {"max": "max", "min": "min", "avg": "mean", "sum": "sum", "count": "count"}
_WORDS = {"=": "equals", "<>": "is not", "<": "is below", "<=": "is at most",
          ">": "is above", ">=": "is at least"}


class ConditionError(ValueError):
    pass


def _coerce(series: pd.Series, value):
    """Compare like with like: a number typed as text against a numeric column."""
    if pd.api.types.is_numeric_dtype(series) and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise ConditionError(f"'{value}' is not a number, and the column is numeric") from None
    return value


def _col(df: pd.DataFrame, cond: Condition) -> pd.Series:
    if cond.column not in df.columns:
        raise ConditionError(f"the condition's column '{cond.column}' is not in the result "
                             f"(columns: {', '.join(map(str, df.columns)) or 'none'})")
    return df[cond.column]


def evaluate(cond: Condition, df: pd.DataFrame) -> bool:
    n = len(df)
    if cond.type == "has_rows":
        return n > 0
    if cond.type == "no_rows":
        return n == 0
    if cond.type == "row_count":
        return bool(_OPS[cond.op](n, float(cond.value)))
    if cond.type in ("any_row", "all_rows"):
        if n == 0:
            return False
        col = _col(df, cond)
        hits = _OPS[cond.op](col, _coerce(col, cond.value))
        return bool(hits.any() if cond.type == "any_row" else hits.all())
    if cond.type == "aggregate":
        col = _col(df, cond)
        if cond.agg == "count":
            agg = int(col.count())
        else:
            if n == 0:
                return False
            if not pd.api.types.is_numeric_dtype(col):
                raise ConditionError(f"cannot take the {cond.agg} of non-numeric column '{cond.column}'")
            agg = getattr(col, _AGG[cond.agg])()
            if pd.isna(agg):
                return False
        return bool(_OPS[cond.op](agg, float(cond.value)))
    raise ConditionError(f"unknown condition type: {cond.type}")


def matching_rows(cond: Condition, df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """The rows that made a row-wise condition true, for the notification."""
    if cond.type not in ("any_row", "all_rows") or df.empty or cond.column not in df.columns:
        return None
    col = df[cond.column]
    try:
        return df[_OPS[cond.op](col, _coerce(col, cond.value))]
    except Exception:  # noqa: BLE001
        return None


def describe(cond: Condition, last_step: str = "") -> str:
    step = cond.step or last_step or "the last step"
    v = cond.value
    if cond.type == "has_rows":
        return f"Triggers when {step} returns any rows"
    if cond.type == "no_rows":
        return f"Triggers when {step} returns no rows"
    if cond.type == "row_count":
        return f"Triggers when the number of rows from {step} {_WORDS[cond.op]} {v}"
    if cond.type == "any_row":
        return f"Triggers when any row of {step} has {cond.column} that {_WORDS[cond.op]} {v}"
    if cond.type == "all_rows":
        return f"Triggers when every row of {step} has {cond.column} that {_WORDS[cond.op]} {v}"
    if cond.type == "aggregate":
        return f"Triggers when the {cond.agg} of {cond.column} in {step} {_WORDS[cond.op]} {v}"
    return cond.type


def measure(rearm: Rearm, df: pd.DataFrame) -> dict[str, float]:
    """The values a ``delta`` re-arm compares: {key: value}, key "" for the whole table."""
    if rearm.delta_measure == "value":
        if not rearm.delta_column:
            raise ConditionError("re-notify on a move: choose the column to watch")
        if rearm.delta_column not in df.columns:
            raise ConditionError(f"re-notify on a move: column '{rearm.delta_column}' is not in the result "
                                 f"(columns: {', '.join(map(str, df.columns)) or 'none'})")
        col = pd.to_numeric(df[rearm.delta_column], errors="coerce")
        if df[rearm.delta_column].notna().any() and col.isna().all():
            raise ConditionError(f"re-notify on a move: column '{rearm.delta_column}' is not numeric")
    if rearm.delta_key:
        if rearm.delta_key not in df.columns:
            raise ConditionError(f"re-notify on a move: key column '{rearm.delta_key}' is not in the result")
        keys = df[rearm.delta_key].astype(str)
        if rearm.delta_measure == "rows":
            out = keys.value_counts()
        else:
            out = col.groupby(keys).agg(_AGG[rearm.delta_agg])
        return {str(k): float(v) for k, v in out.items() if not pd.isna(v)}
    if rearm.delta_measure == "rows":
        return {"": float(len(df))}
    v = getattr(col, _AGG[rearm.delta_agg])()
    return {} if pd.isna(v) else {"": float(v)}


def _fmt(v: float) -> str:
    return f"{v:,.6g}"


def moves(rearm: Rearm, before: dict[str, float], now: dict[str, float]) -> list[str]:
    """What moved enough since ``before`` to notify again, one sentence per key.

    A key that was not there before counts as a move. Keys that went away do not.
    """
    what = "rows" if rearm.delta_measure == "rows" else f"{rearm.delta_agg} {rearm.delta_column}"
    out = []
    for key, v in now.items():
        name = f"{what} for {key}" if key else what
        if key not in before:
            if rearm.delta_key:
                out.append(f"{name} is new ({_fmt(v)})")
            continue
        ref = before[key]
        diff = v - ref
        if rearm.delta_unit == "pct":
            size = abs(diff) / abs(ref) * 100 if ref else (float("inf") if diff else 0.0)
        else:
            size = abs(diff)
        if diff == 0 or size < rearm.delta_amount:
            continue
        if (rearm.delta_direction == "up" and diff < 0) or (rearm.delta_direction == "down" and diff > 0):
            continue
        if rearm.delta_measure == "rows" and not key:
            n = abs(int(diff))
            out.append(f"the result went from {_fmt(ref)} to {_fmt(v)} rows "
                       f"({n} {'more' if diff > 0 else 'fewer'})")
            continue
        pct = f", {diff / abs(ref) * 100:+.2f}%" if ref else ""
        out.append(f"{name} moved {_fmt(ref)} → {_fmt(v)} ({'+' if diff > 0 else ''}{_fmt(diff)}{pct})")
    return out


def describe_delta(rearm: Rearm) -> str:
    what = "the number of rows" if rearm.delta_measure == "rows" else \
        f"the {rearm.delta_agg} of {rearm.delta_column or '…'}"
    if rearm.delta_key:
        what += f" for any {rearm.delta_key}"
    by = f"{_fmt(rearm.delta_amount)}%" if rearm.delta_unit == "pct" else _fmt(rearm.delta_amount)
    way = {"either": "moves by", "up": "rises by", "down": "falls by"}[rearm.delta_direction]
    return f"Notifies again while firing when {what} {way} at least {by} since the last notification"


def should_notify(prev_triggered: bool, prev_notified_at: Optional[datetime],
                  triggered: bool, rearm: Rearm, now: datetime) -> bool:
    """Whether a triggered run sends notifications, under the alert's re-arm policy.

    ``on_change`` is gated before this point (an unchanged result is not
    counted as triggered), so here it behaves like ``every_run``.
    """
    if not triggered:
        return False
    if rearm.mode in ("every_run", "on_change", "delta", "rows_change"):
        return True
    if rearm.mode == "transition":
        return not prev_triggered
    if rearm.mode == "cooldown":
        if not prev_triggered or prev_notified_at is None:
            return True
        return (now - prev_notified_at).total_seconds() >= rearm.cooldown_secs
    raise ValueError(f"unknown re-arm mode: {rearm.mode}")
