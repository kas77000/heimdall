"""Content fingerprint of a result table, for the 'on result change' re-arm mode.

Two results with the same rows (regardless of row order) hash identically, so an
alert that stays triggered on the *same* data won't re-notify; it only fires
again when the rows actually change.
"""
from __future__ import annotations

import hashlib

import pandas as pd


def result_fingerprint(df: pd.DataFrame | None) -> str:
    """Stable hex digest of a DataFrame's content, or "" for None/empty-ish input."""
    if df is None:
        return ""
    if len(df.columns) == 0:
        return "empty"
    frame = df
    try:                              # order-insensitive: same rows -> same hash
        frame = df.sort_values(list(df.columns), kind="stable").reset_index(drop=True)
    except Exception:                 # noqa: BLE001 - unsortable (mixed types); use as-is
        frame = df.reset_index(drop=True)
    try:
        payload = frame.to_csv(index=False)
    except Exception:                 # noqa: BLE001 - non-CSV-able cells
        payload = repr(frame.values.tolist())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
