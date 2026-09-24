"""Mobile page helpers (RULES §9.13). Pure pandas / json; no Streamlit.

The Mobile page is a phone-sized view onto numbers other pages already
compute. Nothing here re-derives buying math:
  * buy list      -> engine/buying_priority.py rows (same as Buying Priority)
  * SKU lookup    -> the Ordering-context engine_df row
  * to approve    -> Cin7 purchase headers (Status ORDERING / DRAFT) and
                     local po_drafts still in 'editing'
  * metrics tiles -> the published Monthly Metrics JSON
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Iterable, Optional

import pandas as pd

CIN7_BASE = "https://inventory.dearsystems.com"

# Monthly Metrics rows shown as tiles: (section prefix, metric, label).
HEADLINE_METRICS: list[tuple[str, str, str]] = [
    ("1.", "Sales $", "Sales"),
    ("1.", "# Orders", "Orders"),
    ("1.", "GP %", "GP %"),
    ("2.", "Avg Order Value", "Avg order"),
    ("2.", "Purchase $", "Purchases"),
    ("5.", "Shopify Total", "Shopify"),
    ("5.", "B2B / Direct", "B2B / Direct"),
    ("5.", "Amazon", "Amazon"),
    ("4.", "Stock Over / (Short of) Goal", "Stock vs goal"),
]


# ---------------------------------------------------------------- SKU lookup

def search_skus(engine_df: pd.DataFrame, query: str,
                limit: int = 20) -> pd.DataFrame:
    """Case-insensitive match on SKU or Name. Exact SKU first, then SKU
    prefix, then anything containing every query word."""
    q = str(query or "").strip().lower()
    if not q or engine_df is None or engine_df.empty:
        return pd.DataFrame()
    sku = engine_df["SKU"].astype(str)
    name = (engine_df["Name"].fillna("").astype(str)
            if "Name" in engine_df.columns
            else pd.Series("", index=engine_df.index))
    hay = (sku + " " + name).str.lower()
    words = q.split()
    mask = pd.Series(True, index=engine_df.index)
    for w in words:
        mask &= hay.str.contains(w, regex=False)
    hits = engine_df[mask]
    if hits.empty:
        return hits
    s = sku[mask].str.lower()
    rank = pd.Series(2, index=hits.index)
    rank = rank.mask(s.str.startswith(q), 1)
    rank = rank.mask(s == q, 0)
    order = (pd.DataFrame({"r": rank, "s": s})
             .sort_values(["r", "s"], kind="mergesort").index)
    return hits.loc[order].head(limit)


def _f(row, col, default=0.0) -> float:
    try:
        v = float(row.get(col, default))
    except (TypeError, ValueError):
        return default
    return default if pd.isna(v) else v


def sku_card(row) -> dict[str, Any]:
    """Plain facts for one engine row (dict or Series)."""
    onhand = _f(row, "OnHand")
    allocated = _f(row, "Allocated")
    on_order = _f(row, "OnOrder")
    avg_daily = _f(row, "avg_daily")
    available = onhand - allocated
    position = available + on_order
    cover = (position / avg_daily) if avg_daily > 0 else None
    goal = _f(row, "goal_units", float("nan"))
    target = _f(row, "target_stock", float("nan"))
    return {
        "sku": str(row.get("SKU", "")),
        "name": str(row.get("Name", "") or ""),
        "supplier": str(row.get("Supplier", "") or ""),
        "status": str(row.get("Status", "") or ""),
        "abc": str(row.get("ABC", "") or ""),
        "on_hand": onhand,
        "allocated": allocated,
        "available": available,
        "on_order": on_order,
        "backorder": max(0.0, allocated - onhand),
        "reorder_qty": max(0.0, _f(row, "reorder_qty")),
        "goal_units": None if pd.isna(goal) else goal,
        "order_up_to": None if pd.isna(target) else target,
        "lead_time_days": _f(row, "lead_time_days") or None,
        "units_12mo": _f(row, "units_12mo"),
        "avg_month": avg_daily * 30.0,
        "cover_days": cover,
        "unit_cost": (_f(row, "unit_cost_for_goal")
                      or _f(row, "AverageCost")),
    }


# ---------------------------------------------------------------- approvals

def cin7_purchase_url(po_id: str, po_type: str = "") -> str:
    page = ("PurchaseAdvanced" if "advanced" in str(po_type).lower()
            else "Purchase")
    return f"{CIN7_BASE}/{page}#{po_id}"


def latest_purchase_headers(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Union rolling purchases_last_*d exports; keep the newest row per PO."""
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    if "ID" not in df.columns:
        return pd.DataFrame()
    if "LastUpdatedDate" in df.columns:
        df = df.sort_values("LastUpdatedDate", kind="mergesort")
    return df.drop_duplicates("ID", keep="last").reset_index(drop=True)


def draft_purchase_orders(headers: pd.DataFrame) -> pd.DataFrame:
    """Cin7 POs still in draft (not yet authorised), oldest first."""
    if headers is None or headers.empty:
        return pd.DataFrame()
    status = headers.get("Status", pd.Series("", index=headers.index))
    order_status = headers.get("OrderStatus",
                               pd.Series("", index=headers.index))
    mask = (status.fillna("").astype(str).str.upper() == "ORDERING") | (
        order_status.fillna("").astype(str).str.upper() == "DRAFT")
    mask &= status.fillna("").astype(str).str.upper() != "VOIDED"
    out = headers[mask].copy()
    if out.empty:
        return out
    out["url"] = [cin7_purchase_url(i, t) for i, t in zip(
        out["ID"].astype(str),
        out.get("Type", pd.Series("", index=out.index)).fillna(""))]
    out["age_days"] = (
        pd.Timestamp.now().normalize()
        - pd.to_datetime(out.get("OrderDate"), errors="coerce")
    ).dt.days
    return out.sort_values("OrderDate", kind="mergesort").reset_index(
        drop=True)


# ---------------------------------------------------------------- metrics

def decode_payload(raw: Optional[bytes]) -> Optional[dict]:
    """dataset_files payload (gzip or plain) -> dict."""
    if not raw:
        return None
    try:
        raw = gzip.decompress(raw)
    except (OSError, EOFError):
        pass
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def headline_tiles(mm: Optional[dict]) -> list[dict[str, Any]]:
    """Current month (MTD) vs prior full month for the headline rows."""
    if not mm or not mm.get("months") or not mm.get("rows"):
        return []
    months = list(mm["months"])
    cur = months[-1]
    prev = months[-2] if len(months) > 1 else None
    out = []
    for prefix, metric, label in HEADLINE_METRICS:
        row = next((r for r in mm["rows"]
                    if str(r.get("section", "")).startswith(prefix)
                    and r.get("metric") == metric), None)
        if row is None:
            continue
        vals = row.get("values") or {}
        out.append({
            "label": label,
            "format": row.get("format", "num"),
            "current": vals.get(cur),
            "previous": vals.get(prev) if prev else None,
            "month": cur,
            "prev_month": prev,
        })
    return out


def fmt_value(v, fmt: str) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if fmt == "money":
        sign = "-" if v < 0 else ""
        a = abs(v)
        if a >= 1_000_000:
            return f"{sign}${a / 1_000_000:,.2f}M"
        if a >= 10_000:
            return f"{sign}${a / 1_000:,.0f}k"
        return f"{sign}${a:,.0f}"
    if fmt == "pct":
        return f"{v:.1f}%"  # Monthly Metrics pct rows are already x100
    if fmt == "int":
        return f"{v:,.0f}"
    return f"{v:,.1f}"
