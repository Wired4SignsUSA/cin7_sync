import math

import numpy as np
import pandas as pd

from engine.frame_utils import row_records_apply


def _status_like(r):
    eff = float(r.get("effective_units_12mo", r.get("units_12mo", 0)) or 0)
    oh = float(r.get("OnHand") or 0)
    tgt = float(r.get("target_stock") or 0)
    if eff <= 0 and oh > 0:
        return "dead"
    if oh < tgt:
        return "reorder"
    return "ok"


def _cost_like(r):
    sv = float(r["StockOnHand"] or 0)
    oh = float(r["OnHand"] or 0)
    if sv > 0 and oh > 0:
        return sv / oh
    return float(r["AverageCost"] or 0)


def _frame():
    rng = np.random.default_rng(7)
    n = 500
    df = pd.DataFrame({
        "SKU": [f"S{i}" for i in range(n)],
        "OnHand": rng.integers(0, 20, n).astype(float),
        "StockOnHand": rng.integers(0, 200, n).astype(float),
        "AverageCost": rng.random(n) * 10,
        "target_stock": rng.integers(0, 10, n).astype(float),
        "effective_units_12mo": rng.integers(0, 5, n).astype(float),
        "noise": ["x"] * n,
    }, index=pd.RangeIndex(100, 100 + n))
    df.loc[df.index[::7], "StockOnHand"] = np.nan
    df.loc[df.index[::11], "AverageCost"] = np.nan
    df.loc[df.index[::13], "effective_units_12mo"] = np.nan
    return df


def test_matches_pandas_apply_including_nan():
    df = _frame()
    expected = df.apply(_status_like, axis=1)
    got = row_records_apply(df, _status_like,
                            ("SKU", "OnHand", "target_stock",
                             "effective_units_12mo", "units_12mo"))
    pd.testing.assert_series_equal(got, expected, check_names=False)

    exp_cost = df.apply(_cost_like, axis=1)
    got_cost = row_records_apply(df, _cost_like,
                                 ("StockOnHand", "OnHand", "AverageCost"))
    # NaN AverageCost with zero stock → NaN in both paths
    assert got_cost.isna().equals(exp_cost.isna())
    pd.testing.assert_series_equal(got_cost.fillna(-1), exp_cost.fillna(-1),
                                   check_names=False)


def test_missing_column_falls_back_to_get_default():
    df = _frame().drop(columns=["effective_units_12mo"])
    df["units_12mo"] = 0.0
    got = row_records_apply(df, _status_like,
                            ("OnHand", "target_stock",
                             "effective_units_12mo", "units_12mo"))
    assert set(got.unique()) <= {"dead", "reorder", "ok"}
    assert list(got.index) == list(df.index)


def test_empty_frame():
    df = pd.DataFrame(columns=["OnHand"])
    assert row_records_apply(df, _cost_like, ("OnHand",)).empty
