"""Buying Priority — one cross-vendor list of what needs buying (RULES §9.11).

Pure pandas; no Streamlit. Input is the Ordering-context engine_df (the
same rows, reorder_qty, target_stock and Status the Ordering page uses),
so every number here matches what the buyer sees when they click through
to that vendor on Ordering.

Tiers (lower = more urgent):
  1  Backorder, not on a PO   — customers are waiting and open POs do not
                                cover the shortfall (Allocated − OnHand −
                                OnOrder > 0).
  2  Reorder now              — engine suggests a buy and Status is
                                "🔴 Reorder now" (oversold / out / below
                                lead-time demand).
  3  Reorder soon             — engine suggests a buy, softer urgency.

Within a tier: tier 1 by uncovered-backorder value (biggest customer
exposure first); tiers 2–3 by ABC class (A first), then days of cover
ascending, then suggested value descending.
"""

from __future__ import annotations

import math

import pandas as pd

from engine.reorder_math import normalise_planning_quantity

TIER_LABELS = {
    1: "🚨 Backorder — not on PO",
    2: "🔴 Reorder now",
    3: "🟠 Reorder soon",
}
_CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0)


def _unit_cost(df: pd.DataFrame) -> pd.Series:
    cost = _num(df, "unit_cost_for_goal")
    fallback = _num(df, "AverageCost")
    return cost.where(cost > 0, fallback)


def build_priority_rows(engine_df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per master SKU that needs buying, ranked."""
    if engine_df is None or engine_df.empty:
        return pd.DataFrame()
    df = engine_df
    if "is_non_master_tube" in df.columns:
        df = df[~df["is_non_master_tube"].fillna(False).astype(bool)]
    df = df.copy()
    supplier = df.get("Supplier", pd.Series("", index=df.index))
    df["Supplier"] = supplier.fillna("").astype(str).str.strip()
    df = df[(df["Supplier"] != "") & (df["Supplier"] != "(unassigned)")]
    if df.empty:
        return pd.DataFrame()

    onhand = _num(df, "OnHand")
    allocated = _num(df, "Allocated")
    on_order = _num(df, "OnOrder")
    available = onhand - allocated
    backorder = (allocated - onhand).clip(lower=0)
    uncovered = (backorder - on_order).clip(lower=0)
    # Ignore non-actionable bulk-roll residue (same floor as Status).
    is_bulk = (df["is_bulk_master"].fillna(False).astype(bool)
               if "is_bulk_master" in df.columns
               else pd.Series(False, index=df.index))
    bulk_len = _num(df, "bulk_length_m")
    uncovered = pd.Series([
        normalise_planning_quantity(q, is_bulk_master=b, bulk_length_m=l)
        for q, b, l in zip(uncovered, is_bulk, bulk_len)
    ], index=df.index)
    reorder = _num(df, "reorder_qty").clip(lower=0)
    status = df.get("Status", pd.Series("", index=df.index)).fillna("")
    status = status.astype(str)
    discontinued = status.str.contains("Discontinued", case=False)

    tier = pd.Series(0, index=df.index)
    tier = tier.mask((reorder > 0) & ~discontinued, 3)
    tier = tier.mask(
        (reorder > 0) & ~discontinued
        & status.str.contains("Reorder now", case=False), 2)
    tier = tier.mask(uncovered > 1e-9, 1)

    df["tier"] = tier
    df = df[df["tier"] > 0].copy()
    if df.empty:
        return pd.DataFrame()
    idx = df.index
    df["available"] = available[idx]
    df["on_order"] = on_order[idx]
    df["backorder"] = backorder[idx]
    df["backorder_uncovered"] = uncovered[idx]
    suggested = reorder[idx].where(
        reorder[idx] >= uncovered[idx], uncovered[idx])
    df["suggested_qty"] = suggested
    cost = _unit_cost(df)
    df["unit_cost"] = cost
    df["suggested_value"] = suggested * cost
    df["backorder_value"] = uncovered[idx] * cost
    avg_daily = _num(df, "avg_daily")
    position = df["available"] + df["on_order"]

    def _cover(pos, rate):
        if rate <= 0:
            return math.inf if pos > 0 else 0.0
        return max(0.0, pos / rate)

    df["cover_days"] = [
        _cover(p, r) for p, r in zip(position, avg_daily)]
    df["lead_time_days"] = _num(df, "lead_time_days")
    cls = df.get("ABC", pd.Series("C", index=df.index)).fillna("C")
    df["class"] = cls.astype(str).str.strip().str.upper().str[:1]
    df["_class_rank"] = df["class"].map(_CLASS_RANK).fillna(4)
    df["tier_label"] = df["tier"].map(TIER_LABELS)

    # Tier 1 sorts by customer exposure; others by class → cover → value.
    df["_k1"] = df["backorder_value"].where(df["tier"] == 1, 0.0) * -1
    df["_k2"] = df["_class_rank"].where(df["tier"] != 1, 0)
    df["_k3"] = df["cover_days"].where(df["tier"] != 1, 0.0)
    df = df.sort_values(
        ["tier", "_k1", "_k2", "_k3", "suggested_value"],
        ascending=[True, True, True, True, False],
        kind="mergesort",
    )
    df["priority_rank"] = range(1, len(df) + 1)
    return df.drop(columns=["_k1", "_k2", "_k3", "_class_rank"]).reset_index(
        drop=True)


def build_vendor_summary(rows: pd.DataFrame) -> pd.DataFrame:
    """Roll ranked rows up to one line per vendor, most urgent first."""
    if rows is None or rows.empty:
        return pd.DataFrame(columns=[
            "Supplier", "top_rank", "worst_tier", "backorder_skus",
            "now_skus", "soon_skus", "sku_count", "suggested_value",
            "backorder_value", "top_skus"])
    g = rows.groupby("Supplier", sort=False)
    out = pd.DataFrame({
        "top_rank": g["priority_rank"].min(),
        "worst_tier": g["tier"].min(),
        "backorder_skus": g["tier"].apply(lambda s: int((s == 1).sum())),
        "now_skus": g["tier"].apply(lambda s: int((s == 2).sum())),
        "soon_skus": g["tier"].apply(lambda s: int((s == 3).sum())),
        "sku_count": g["SKU"].count(),
        "suggested_value": g["suggested_value"].sum(),
        "backorder_value": g["backorder_value"].sum(),
        "top_skus": g["SKU"].apply(lambda s: list(s.astype(str))[:3]),
    }).reset_index()
    out = out.sort_values(
        ["worst_tier", "backorder_value", "top_rank"],
        ascending=[True, False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    return out
