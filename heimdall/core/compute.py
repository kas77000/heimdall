"""The compute step: join, derive, filter, keep — over tables already fetched."""
from __future__ import annotations

import pandas as pd

from heimdall.core import safeexpr
from heimdall.core.models import ComputeStep


class ComputeError(ValueError):
    pass


def run_compute(step: ComputeStep, outputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if not step.source:
        raise ComputeError(f"step '{step.name}' has no source step")
    if step.source not in outputs:
        raise ComputeError(f"step '{step.name}': unknown source step '{step.source}'")
    df = outputs[step.source].copy()

    if step.join_step:
        if step.join_step not in outputs:
            raise ComputeError(f"step '{step.name}': unknown join step '{step.join_step}'")
        right = outputs[step.join_step]
        if not step.left_on or len(step.left_on) != len(step.right_on or step.left_on):
            raise ComputeError(f"step '{step.name}': give the join columns on both sides, in pairs")
        right_on = step.right_on or step.left_on
        for c in step.left_on:
            if c not in df.columns:
                raise ComputeError(f"step '{step.name}': '{step.source}' has no column '{c}'")
        for c in right_on:
            if c not in right.columns:
                raise ComputeError(f"step '{step.name}': '{step.join_step}' has no column '{c}'")
        df = df.merge(right, how=step.join_how, left_on=step.left_on, right_on=right_on,
                      suffixes=("", f"_{step.join_step}"))

    for d in step.derive:
        name = (d.column or "").strip()
        if not name:
            raise ComputeError(f"step '{step.name}': a derived column needs a name")
        try:
            value = safeexpr.evaluate(d.expr, df)
        except safeexpr.ExpressionError as exc:
            raise ComputeError(f"step '{step.name}', column '{name}': {exc}") from None
        df[name] = value

    if step.where.strip():
        try:
            mask = safeexpr.evaluate(step.where, df)
        except safeexpr.ExpressionError as exc:
            raise ComputeError(f"step '{step.name}', filter: {exc}") from None
        if isinstance(mask, pd.Series):
            df = df[mask.fillna(False).astype(bool)]
        elif not mask:
            df = df.iloc[0:0]

    if step.keep:
        missing = [c for c in step.keep if c not in df.columns]
        if missing:
            raise ComputeError(f"step '{step.name}': cannot keep unknown column(s) {', '.join(missing)}")
        df = df[list(step.keep)]
    return df.reset_index(drop=True)


def vet(step: ComputeStep) -> list[str]:
    """Problems with a compute step's expressions, found without running it."""
    problems = []
    for d in step.derive:
        try:
            safeexpr.parse(d.expr)
        except safeexpr.ExpressionError as exc:
            problems.append(f"step '{step.name}', column '{d.column}': {exc}")
    if step.where.strip():
        try:
            safeexpr.parse(step.where)
        except safeexpr.ExpressionError as exc:
            problems.append(f"step '{step.name}', filter: {exc}")
    return problems
