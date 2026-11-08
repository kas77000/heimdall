# heimdall/core/qfmt.py — carried over from KdbMonitor
"""Python values -> q literals, for the where clause a guided filter builds.

Every type here has to be *told* what it is. q has no way to look at the text
"2026-07-30" and know whether it was meant as a date or as arithmetic, and it
does not ask: it reads it as 2026 minus 7 minus 30 and returns 1989. So the
value type is part of the filter, and each one has exactly one meaning.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

VALUE_TYPES = ("symbol", "number", "string", "date", "time", "expression")

# A placeholder another stage fills in: {{dataset.column}} from an earlier
# dataset, {{param:name}} from the reader, {{date_from}} from the period. It is
# not a value yet, so it is passed through rather than formatted — a symbol type
# would otherwise turn {{orders.sym}} into `{`{`o`r`d`e`r`s..., one backtick per
# character, and the substitution that came next would find nothing to replace.
_PLACEHOLDER = re.compile(r"^\s*\{\{[^{}]+\}\}\s*$")


def is_placeholder(value: Any) -> bool:
    """Whether this value is a token for a later stage rather than a value."""
    return isinstance(value, str) and _PLACEHOLDER.match(value) is not None

# A date written any of the ordinary ways. q wants dots; people type dashes,
# slashes, or paste whatever their last export used.
_DATE_TEXT = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$")


def q_date(value: Any) -> str:
    """A q date literal — ``2026.07.30``.

    Accepts a ``date``/``datetime``, or text written with dashes, slashes or
    dots. Anything else raises rather than being passed through: a date that
    silently became subtraction is the failure this type exists to prevent, and
    it fails as a wrong number rather than as an error.
    """
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f"{value:%Y.%m.%d}"

    match = _DATE_TEXT.match(str(value))
    if not match:
        raise ValueError(
            f"'{value}' is not a date. Write it as 2026-07-30, or use the "
            f"expression type for something q works out itself, like .z.D-1.")
    year, month, day = (int(p) for p in match.groups())
    return f"{date(year, month, day):%Y.%m.%d}"


def format_q_value(value: Any, value_type: str) -> str:
    if is_placeholder(value):
        return str(value).strip()
    if value_type == "symbol":
        return "`" + str(value)
    if value_type == "number":
        return str(value)
    if value_type == "string":
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if value_type == "date":
        return q_date(value)
    if value_type == "time":
        # A time of day, or a timestamp, written as q already spells it.
        return str(value).strip()
    if value_type == "expression":
        # q the author wrote, sent as it stands: .z.D-1, .z.D, .z.P, or a
        # sub-select. Guided mode has always been able to reach raw q through
        # the raw mode beside it, so this adds no reach — it saves rewriting a
        # whole query to compute one value.
        return str(value).strip()
    raise ValueError(f"unknown value_type: {value_type}")


def q_string(text: Any) -> str:
    '''A q char vector — ``"abc"`` — with quotes and backslashes escaped.'''
    body = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{body}"'


def q_symbol(text: Any) -> str:
    r"""A q symbol literal, in the form that can carry any name at all.

    ``\`sym`` is only legal for text that is already a q name. A column called
    "order qty" or "P&L" comes back from a file, or from a rename transform,
    and ``\`order qty`` is two symbols and a syntax error. ``\`$"order qty"``
    is one symbol whatever is in it.
    """
    return "`$" + q_string(text)


# What each inferred column type calls a missing value, and what an empty
# column of it looks like. A frame reaches q with its gaps intact: 'nan' is not
# a q word, and a column that silently dropped its nulls would join against the
# wrong rows rather than against none.
_Q_NULL = {"number": "0n", "boolean": "0b", "date": "0Nd",
           "timestamp": "0Np", "time": "0Nt", "symbol": "`"}
_Q_EMPTY = {"number": "0#0n", "boolean": "0#0b", "date": "0#0Nd",
            "timestamp": "0#0Np", "time": "0#0Nt", "symbol": "`$()"}


def q_column_type(series) -> str:
    """What a frame's column is, in q's terms, read from its dtype.

    Wider than the two kinds a ``{{name.column}}`` reference infers, because
    those only ever build a ``where`` clause and these build a table somebody
    will join against. A timestamp column pushed through as a symbol is not
    merely untidy: the text has spaces and colons in it, so the literal does
    not parse at all.
    """
    import pandas as pd

    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_timedelta64_dtype(series):
        return "time"
    if pd.api.types.is_datetime64_any_dtype(series):
        # A date column and a timestamp column are different types to q, and
        # only the values can say which this is: every time at midnight is a
        # column of dates that pandas is holding as timestamps.
        stamps = series.dropna()
        if stamps.empty or (stamps.dt.normalize() == stamps).all():
            return "date"
        return "timestamp"
    return "symbol"


def _is_null(value: Any) -> bool:
    """Whether this cell holds nothing, without trusting what is in it.

    ``pd.isna`` answers elementwise for a list or an array, and a truth test on
    the answer raises — a frame can hold one, and a reference that blew up on
    the *shape* of a value it was only going to print would be a poor trade.
    """
    import pandas as pd

    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def q_cell(value: Any, value_type: str) -> str:
    """One value as a q literal of ``value_type``, nulls included."""
    import math

    import pandas as pd

    if _is_null(value):
        return _Q_NULL[value_type]
    if value_type == "boolean":
        return "1b" if value else "0b"
    if value_type == "number":
        # An infinity has a q spelling, and it is not the word "inf". Transforms
        # already turn the ones they produce into nulls (see core.transform), so
        # this is for the ones that arrive from a file or a query as they are.
        if isinstance(value, float) and math.isinf(value):
            return "0w" if value > 0 else "-0w"
        return str(value)
    if value_type == "date":
        return f"{pd.Timestamp(value):%Y.%m.%d}"
    if value_type == "timestamp":
        return f"{pd.Timestamp(value):%Y.%m.%dD%H:%M:%S.%f}000"
    if value_type == "time":
        # Three decimals, because q's `time` is a count of milliseconds and
        # 09:15:03.221000 is not a time literal to it. The same precision the
        # rest of the app prints a clock column at.
        #
        # Counted in whole milliseconds and truncated, never rounded: a value a
        # hair under the minute rounds *up* to 09:15:60.000, which is not a
        # time at all. Integer arithmetic from the nanoseconds also keeps a
        # long duration exact, where seconds-as-a-float would not.
        nanos = pd.Timedelta(value).value
        sign = "-" if nanos < 0 else ""
        hours, rest = divmod(abs(nanos) // 1_000_000, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, millis = divmod(rest, 1_000)
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return q_symbol(value)


def q_table(frame) -> str:
    r"""A pandas frame as a q table literal, to select from or join against.

    ``flip (\`$("a";"b"))!(...)`` rather than ``([] a:...; b:...)`` because a
    column name is whatever the dataset produced and only the ``$`` form can
    carry one that is not a q name — see :func:`q_symbol`.

    Every column is bracketed as ``(x;y;z)`` rather than space-joined, so one
    rule covers every type and a null in the middle of a column cannot change
    how the vector beside it reads.

    ``enlist`` appears wherever there is exactly one of something, because
    ``(x)`` is just ``x`` in q — parentheses do not make a list. Without it a
    one-column table is keyed by an atom and a one-row table holds atoms, and
    neither of those is a table.
    """
    columns = list(frame.columns)
    if not columns:
        raise ValueError("this dataset has no columns, so there is no table "
                         "to build from it")
    names = ([f"enlist {q_string(columns[0])}"] if len(columns) == 1
             else [f'({";".join(q_string(c) for c in columns)})'])
    parts = []
    for name in columns:
        series = frame[name]
        kind = q_column_type(series)
        if len(series) == 0:
            parts.append(_Q_EMPTY[kind])
            continue
        cells = [q_cell(v, kind) for v in series]
        parts.append(f"enlist {cells[0]}" if len(cells) == 1
                     else "(" + ";".join(cells) + ")")
    values = (f"enlist {parts[0]}" if len(parts) == 1
              else "(" + ";".join(parts) + ")")
    return f"flip (`${names[0]})!{values}"


def format_q_list(values: list, value_type: str) -> str:
    # One placeholder standing in for the whole list. Whatever fills it in
    # produces a q list already, so it must not be enlisted or type-formatted
    # here — `sym in {{orders.sym}}` becomes `sym in `AAPL`MSFT`, which is the
    # list, not a list holding one thing.
    if len(values) == 1 and is_placeholder(values[0]):
        return str(values[0]).strip()
    if value_type == "symbol":
        if not values:
            return "`$()"          # empty symbol vector — keeps `x in ...` valid
        joined = "".join("`" + str(v) for v in values)
        return joined if len(values) > 1 else "enlist " + joined
    if value_type == "number":
        if not values:
            return "0#0"           # empty numeric vector
        joined = " ".join(str(v) for v in values)
        return joined if len(values) > 1 else "enlist " + joined
    if value_type == "string":
        if not values:
            return "()"            # empty list
        parts = [format_q_value(v, "string") for v in values]
        return "(" + ";".join(parts) + ")" if len(values) > 1 else "enlist " + parts[0]
    if value_type in ("date", "time"):
        if not values:
            # An empty date vector. `0#0d` types it, so `date in ...` stays a
            # comparison of dates rather than of longs.
            return "0#0d" if value_type == "date" else "0#0t"
        parts = [format_q_value(v, value_type) for v in values]
        joined = " ".join(parts)
        return joined if len(values) > 1 else "enlist " + parts[0]
    if value_type == "expression":
        if not values:
            return "()"
        parts = [format_q_value(v, "expression") for v in values]
        return "(" + ";".join(parts) + ")" if len(values) > 1 else "enlist " + parts[0]
    raise ValueError(f"unknown value_type: {value_type}")
