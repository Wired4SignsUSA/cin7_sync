"""Small pandas helpers shared by the app and the engine."""
from __future__ import annotations

from typing import Callable, Iterable

import pandas as pd


def row_records_apply(df: pd.DataFrame, fn: Callable, cols: Iterable[str]) -> pd.Series:
    """Drop-in for ``df.apply(fn, axis=1)`` when ``fn`` only reads the
    columns in ``cols`` via ``r[...]`` / ``r.get(...)``.

    Iterating plain dicts over just those columns avoids building a
    pandas Series per row — on the 120-column, 11k-row engine frame that
    was the Ordering page's single biggest per-click cost (2026-09-18).
    Same values, same index; the function body is untouched. Columns
    missing from ``df`` are simply absent from the dict, so ``r.get``
    defaults still apply exactly as they did with a Series row.
    """
    if df.empty:
        return pd.Series([], index=df.index, dtype=object)
    present = [c for c in cols if c in df.columns]
    return pd.Series([fn(r) for r in df[present].to_dict("records")],
                     index=df.index)
