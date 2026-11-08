"""References from one step to the results of earlier ones.

The same reference reads differently depending on where it lands:

=========================  ===============================  ===================  ======================
reference                  in q                             in a security line   in a script parameter
=========================  ===============================  ===================  ======================
``{{s.col}}``              q list of distinct values        line repeated        comma-separated
``{{s.col[0]}}``           q atom (first value)             first value          first value
``{{table:s}}``            q table literal                  refused              refused
``{{rows:s}}``             row count                        row count            row count
=========================  ===============================  ===================  ======================
"""
from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from heimdall.core.qfmt import format_q_list, format_q_value, q_table

_COLUMN = re.compile(r"\{\{\s*(\w+)\.(\w+)\s*(\[0\])?\s*\}\}")
_TABLE = re.compile(r"\{\{\s*table:(\w+)\s*\}\}")
_ROWS = re.compile(r"\{\{\s*rows:(\w+)\s*\}\}")
_ANY = re.compile(r"\{\{\s*(?:table:|rows:)?(\w+)(?:\.(\w+))?")


class ReferenceError_(ValueError):
    """A reference that cannot be filled: unknown step or column, or empty."""


def referenced_steps(text: str) -> set[str]:
    """Names of every step a piece of text refers to."""
    return {m.group(1) for m in _ANY.finditer(text or "")}


def _frame(outputs: dict[str, pd.DataFrame], name: str) -> pd.DataFrame:
    if name not in outputs:
        raise ReferenceError_(f"unknown step '{name}' (only earlier steps can be referenced)")
    return outputs[name]


def _column(outputs, name: str, col: str) -> pd.Series:
    df = _frame(outputs, name)
    if col not in df.columns:
        raise ReferenceError_(f"step '{name}' has no column '{col}' "
                              f"(it has: {', '.join(map(str, df.columns)) or 'none'})")
    return df[col]


def _distinct(series: pd.Series) -> list:
    return list(dict.fromkeys(v for v in series.tolist() if not _isna(v)))


def _isna(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _first(series: pd.Series, name: str, col: str):
    values = _distinct(series)
    if not values:
        raise ReferenceError_(f"{{{{{name}.{col}[0]}}}}: step '{name}' returned no value")
    return values[0]


def _value_type(series: pd.Series) -> str:
    return "number" if pd.api.types.is_numeric_dtype(series) else "symbol"


def _plain(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# -- q -------------------------------------------------------------------------

def fill_q(text: str, outputs: dict[str, pd.DataFrame]) -> str:
    def table(m):
        return "(" + q_table(_frame(outputs, m.group(1))) + ")"

    def rows(m):
        return str(len(_frame(outputs, m.group(1))))

    def column(m):
        name, col, first = m.group(1), m.group(2), m.group(3)
        series = _column(outputs, name, col)
        kind = _value_type(series)
        if first:
            return format_q_value(_first(series, name, col), kind)
        return format_q_list(_distinct(series), kind)

    text = _TABLE.sub(table, text or "")
    text = _ROWS.sub(rows, text)
    return _COLUMN.sub(column, text)


# -- plain text ------------------------------------------------------------------

def _refuse_table(text: str, where: str) -> None:
    if _TABLE.search(text or ""):
        raise ReferenceError_(f"{{{{table:...}}}} only works inside a q query, not in {where}")


def fill_text(text: str, outputs: dict[str, pd.DataFrame], sep: str = ",") -> str:
    """One string: a column reference becomes its values joined by ``sep``."""
    _refuse_table(text, "a parameter")

    def rows(m):
        return str(len(_frame(outputs, m.group(1))))

    def column(m):
        name, col, first = m.group(1), m.group(2), m.group(3)
        series = _column(outputs, name, col)
        if first:
            return _plain(_first(series, name, col))
        return sep.join(_plain(v) for v in _distinct(series))

    return _COLUMN.sub(column, _ROWS.sub(rows, text or ""))


def expand_lines(text: str, outputs: dict[str, pd.DataFrame]) -> list[str]:
    """Security lines: a line holding a column reference repeats once per value."""
    return [line for line, _ in expand_lines_keyed(text, outputs)]


def expand_lines_keyed(text: str, outputs: dict[str, pd.DataFrame]) -> list[tuple[str, dict]]:
    """Like :func:`expand_lines`, keeping the values each line was built from.

    ``{{orders.sym}} US Equity`` yields ``("AAPL US Equity", {"sym": "AAPL"})``,
    so the Bloomberg result can carry ``sym`` beside ``security`` and be joined
    straight back to the step it came from. Two list references on one line
    pair up row by row (``{{s.ticker}} {{s.market}} Equity``), so they must
    come from the same step.
    """
    _refuse_table(text, "a security list")
    out: list[tuple[str, dict]] = []
    for raw in (text or "").splitlines():
        line = _ROWS.sub(lambda m: str(len(_frame(outputs, m.group(1)))), raw.strip())
        if not line:
            continue
        lists = [m for m in _COLUMN.finditer(line) if not m.group(3)]
        if not lists:
            out.append((_COLUMN.sub(lambda m: _plain(_first(_column(outputs, m.group(1), m.group(2)),
                                                            m.group(1), m.group(2))), line), {}))
            continue
        steps = {m.group(1) for m in lists}
        if len(steps) > 1:
            raise ReferenceError_(f"one security line can only expand over one step's rows: {raw.strip()}")
        df = _frame(outputs, steps.pop())
        for m in lists:
            _column(outputs, m.group(1), m.group(2))
        seen: dict[str, dict] = {}
        for _, row in df.iterrows():
            if any(_isna(row[m.group(2)]) for m in lists):
                continue

            def sub(m, row=row):
                if m.group(3):
                    return _plain(_first(_column(outputs, m.group(1), m.group(2)), m.group(1), m.group(2)))
                return _plain(row[m.group(2)])
            key = _COLUMN.sub(sub, line)
            if key not in seen:
                seen[key] = {m.group(2): row[m.group(2)] for m in lists}
        out.extend(seen.items())
    return out


def unique_names(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))
