"""Whether a table meets an alert's condition, and how to say what it checks."""
from __future__ import annotations

import operator
from datetime import datetime
from typing import Optional

import pandas as pd

from heimdall.core.models import TEXT_OPS, Alert, Condition, Rearm, SeverityRule

_OPS = {"=": operator.eq, "<>": operator.ne, "<": operator.lt,
        "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_AGG = {"max": "max", "min": "min", "avg": "mean", "sum": "sum", "count": "count"}
_WORDS = {"=": "equals", "<>": "is not", "<": "is below", "<=": "is at most",
          ">": "is above", ">=": "is at least", "contains": "contains", "not_contains": "does not contain",
          "starts_with": "starts with", "ends_with": "ends with"}
_SIGNS = {"=": "=", "<>": "≠", "<": "<", "<=": "≤", ">": ">", ">=": "≥"}


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


def _col(df: pd.DataFrame, cond, what: str = "the condition's") -> pd.Series:
    if cond.column not in df.columns:
        raise ConditionError(f"{what} column '{cond.column}' is not in the result "
                             f"(columns: {', '.join(map(str, df.columns)) or 'none'})")
    return df[cond.column]


def compare(op: str, left, value):
    """``left op value``, for a column (a boolean Series) or a single value (a bool).
    The text comparisons ignore case and read any value as text."""
    if op in TEXT_OPS:
        needle = str(value).lower()
        if isinstance(left, pd.Series):
            text = left.astype("string").fillna("").str.lower()
            hits = {"contains": lambda: text.str.contains(needle, regex=False),
                    "not_contains": lambda: ~text.str.contains(needle, regex=False),
                    "starts_with": lambda: text.str.startswith(needle),
                    "ends_with": lambda: text.str.endswith(needle)}[op]()
            return hits.fillna(False).astype(bool)
        text = "" if left is None or (isinstance(left, float) and pd.isna(left)) else str(left).lower()
        return {"contains": needle in text, "not_contains": needle not in text,
                "starts_with": text.startswith(needle), "ends_with": text.endswith(needle)}[op]
    if isinstance(left, pd.Series):
        return _OPS[op](left, _coerce(left, value))
    if isinstance(left, str) or isinstance(value, str):
        try:
            left, value = float(left), float(value)
        except (TypeError, ValueError):
            left, value = str(left), str(value)      # = and <> on text are exact, as on a column
    return bool(_OPS[op](left, value))


def _number_op(op: str, what: str) -> None:
    if op in TEXT_OPS:
        raise ConditionError(f"'{_WORDS[op]}' compares text; {what} is a number")


def evaluate(cond: Condition, df: pd.DataFrame) -> bool:
    n = len(df)
    if cond.type == "has_rows":
        return n > 0
    if cond.type == "no_rows":
        return n == 0
    if cond.type == "row_count":
        _number_op(cond.op, "the number of rows")
        return bool(_OPS[cond.op](n, float(cond.value)))
    if cond.type in ("any_row", "all_rows"):
        if n == 0:
            return False
        col = _col(df, cond)
        hits = compare(cond.op, col, cond.value)
        return bool(hits.any() if cond.type == "any_row" else hits.all())
    if cond.type == "aggregate":
        _number_op(cond.op, f"the {cond.agg} of {cond.column}")
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
        return df[compare(cond.op, col, cond.value)]
    except Exception:  # noqa: BLE001
        return None


# -- severity from the result ---------------------------------------------------------

SEVERITY_WORDS = {"info": "Info", "warning": "Warning", "critical": "Critical", "skip": "Don't send"}


def _show(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, (int, float)) and not isinstance(v, bool) else repr(str(v))


def rule_value(rule: SeverityRule, df: pd.DataFrame):
    """What a rule compares: a number, a text, or (any_row) the column itself."""
    if rule.measure == "rows":
        _number_op(rule.op, "the number of rows")
        return len(df)
    if rule.measure == "any_row":
        return _col(df, rule, "a severity rule's")
    if rule.measure == "value":
        _number_op(rule.op, f"the {rule.agg} of {rule.column or '...'}")
        col = pd.to_numeric(_col(df, rule, "a severity rule's"), errors="coerce")
        v = getattr(col, _AGG[rule.agg])()
        return None if pd.isna(v) else float(v)
    # text: the result's value
    if df.empty:
        return None
    col = _col(df, rule, "a severity rule's") if rule.column else df.iloc[:, 0]
    v = col.iloc[0]
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def rule_matches(rule: SeverityRule, df: pd.DataFrame) -> tuple[bool, str]:
    """Whether the rule applies, and in words what was found (for the mail and the pop-up)."""
    v = rule_value(rule, df)
    sign = _SIGNS.get(rule.op) or _WORDS[rule.op]
    if rule.measure == "any_row":
        if df.empty:
            return False, ""
        n = int(compare(rule.op, v, rule.value).sum())
        return n > 0, f"{n} row{'s' if n != 1 else ''} with {rule.column} {sign} {_show(rule.value)}"
    if v is None:
        return False, ""
    if rule.measure == "text" or rule.op in TEXT_OPS:
        hit = compare(rule.op, v, rule.value)
    else:
        try:
            hit = bool(_OPS[rule.op](float(v), float(rule.value)))
        except (TypeError, ValueError):
            raise ConditionError(f"a severity rule compares a number with '{rule.value}'") from None
    what = {"rows": f"{_show(v)} rows", "value": f"{rule.agg} {rule.column} {_show(v)}",
            "text": f"the result {_show(v)}"}[rule.measure]
    return hit, f"{what} {sign} {_show(rule.value)}"


def grade(alert: Alert, df: pd.DataFrame) -> tuple[str, str]:
    """The severity of a result under the alert's rules: (info|warning|critical|skip, why)."""
    for rule in alert.severity_rules:
        hit, why = rule_matches(rule, df)
        if hit:
            return rule.severity, why
    return alert.severity_else, "no severity rule matched"


def describe_rule(rule: SeverityRule) -> str:
    what = {"rows": "the number of rows", "any_row": f"any row's {rule.column or '...'}",
            "value": f"the {rule.agg} of {rule.column or '...'}",
            "text": "the result" + (f" ({rule.column})" if rule.column else "")}[rule.measure]
    return f"{what} {_WORDS[rule.op]} {_show(rule.value)}: {SEVERITY_WORDS[rule.severity]}"


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
