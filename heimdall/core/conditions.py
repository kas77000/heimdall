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


def should_notify(prev_triggered: bool, prev_notified_at: Optional[datetime],
                  triggered: bool, rearm: Rearm, now: datetime) -> bool:
    """Whether a triggered run sends notifications, under the alert's re-arm policy.

    ``on_change`` is gated before this point (an unchanged result is not
    counted as triggered), so here it behaves like ``every_run``.
    """
    if not triggered:
        return False
    if rearm.mode in ("every_run", "on_change"):
        return True
    if rearm.mode == "transition":
        return not prev_triggered
    if rearm.mode == "cooldown":
        if not prev_triggered or prev_notified_at is None:
            return True
        return (now - prev_notified_at).total_seconds() >= rearm.cooldown_secs
    raise ValueError(f"unknown re-arm mode: {rearm.mode}")
