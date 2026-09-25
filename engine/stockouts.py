"""Stock-out history helpers (RULES 9.15, James 2026-09-25).

Source: Inventory Planner `stockouts_hist` per variant — a list of
[iso_timestamp, flag] transitions where flag 1 = went out of stock and
0 = back in stock. `ip_stockouts.py` turns that into episodes
(sku, out_date, back_date|None) stored in `ip_stockout_events`.

Pure functions only — pages and the Monthly Metrics chart call these.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

import pandas as pd

NON_ORDER_STATUSES = {"ESTIMATING", "ESTIMATED", "QUOTE", "CREDITED",
                      "VOIDED", "VOID", "CANCELLED", "DRAFT"}


def episodes_from_hist(hist: Iterable) -> list[tuple[date, Optional[date]]]:
    """[[ts, 1|0], ...] -> [(out_date, back_date or None), ...].
    Repeated flags are ignored (only transitions count)."""
    eps: list[tuple[date, Optional[date]]] = []
    cur: Optional[date] = None
    for item in hist or []:
        try:
            ts, flag = item[0], int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        try:
            # Day in IP's own timezone offset (US Eastern).
            d = datetime.fromisoformat(str(ts)).date()
        except ValueError:
            continue
        if flag == 1 and cur is None:
            cur = d
        elif flag == 0 and cur is not None:
            eps.append((cur, d))
            cur = None
    if cur is not None:
        eps.append((cur, None))
    return eps


def _events_frame(events) -> pd.DataFrame:
    df = pd.DataFrame(events) if not isinstance(events, pd.DataFrame) \
        else events.copy()
    if df.empty:
        return pd.DataFrame(columns=["sku", "out_date", "back_date"])
    df["out_date"] = pd.to_datetime(df["out_date"], errors="coerce")
    df["back_date"] = pd.to_datetime(df["back_date"], errors="coerce")
    return df.dropna(subset=["out_date"])


def eligible_skus(engine_df: pd.DataFrame, dropship_skus=()) -> set:
    """SKUs where a stock-out means a customer order could not ship at
    once: stocked products in the engine, minus dropship (order-on-
    demand), minus made-to-order assemblies (BOM with no stock goal —
    kits are built per order, so IP shows them 'out' by design)."""
    if engine_df is None or engine_df.empty or "SKU" not in engine_df:
        return set()
    sku = engine_df["SKU"].astype(str)
    bom = engine_df.get("BillOfMaterial",
                        pd.Series(False, index=engine_df.index))
    bom = bom.astype(str).str.lower().isin({"true", "1", "yes"})
    goal = pd.to_numeric(engine_df.get(
        "goal_units", pd.Series(0, index=engine_df.index)),
        errors="coerce").fillna(0)
    keep = ~bom | (goal > 0)
    return set(sku[keep]) - {str(s) for s in (dropship_skus or ())}


def _order_lines(sale_lines: pd.DataFrame) -> pd.DataFrame:
    if sale_lines is None or sale_lines.empty:
        return pd.DataFrame(columns=["SaleID", "SKU", "order_date"])
    sl = sale_lines
    status = sl.get("Status", pd.Series("", index=sl.index)) \
        .astype(str).str.upper()
    stype = sl.get("SaleType", pd.Series("", index=sl.index)).astype(str)
    sl = sl[~status.isin(NON_ORDER_STATUSES) & (stype != "Service Sale")]
    d = pd.to_datetime(sl["OrderDate"], errors="coerce", utc=True)
    out = pd.DataFrame({
        "SaleID": sl["SaleID"].astype(str).values,
        "SKU": sl["SKU"].astype(str).values,
        "order_date": d.dt.tz_localize(None).dt.normalize().values})
    return out.dropna(subset=["order_date"])


def orders_hit(sale_lines: pd.DataFrame, events, eligible: set) -> pd.DataFrame:
    """Order lines placed while that SKU was out of stock (IP episode
    started BEFORE the order day and had not ended by it — strict, so the
    order that took the last unit is not counted: a floor). Dropship and
    made-to-order SKUs are excluded via `eligible`.
    Returns SaleID, SKU, order_date (one row per order line hit)."""
    cols = ["SaleID", "SKU", "order_date"]
    ev = _events_frame(events)
    lines = _order_lines(sale_lines)
    if ev.empty or lines.empty or not eligible:
        return pd.DataFrame(columns=cols)
    lines = lines[lines["SKU"].isin(eligible)]
    ev = ev[ev["sku"].astype(str).isin(eligible)]
    m = lines.merge(ev, left_on="SKU", right_on="sku", how="inner")
    back = m["back_date"].fillna(pd.Timestamp.max.normalize())
    hit = m[(m["out_date"] < m["order_date"]) & (m["order_date"] < back)]
    return hit[cols].drop_duplicates().reset_index(drop=True)


def summarise_12mo(events, today: Optional[date] = None,
                   hits: Optional[pd.DataFrame] = None,
                   eligible: Optional[set] = None) -> pd.DataFrame:
    """Per SKU over the trailing 12 months:
    stockouts_12mo  = stock-outs that STARTED in the window
    days_out_12mo   = days out of stock inside the window (incl. an
                      episode that started before it)
    out_now         = currently out of stock
    orders_hit_12mo = customer orders placed while it was out (see
                      orders_hit); None when hits not supplied
    """
    df = _events_frame(events)
    cols = ["SKU", "stockouts_12mo", "days_out_12mo", "out_now",
            "orders_hit_12mo", "stockouts_label"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    if eligible is not None:
        df = df[df["sku"].astype(str).isin(eligible)]
        if df.empty:
            return pd.DataFrame(columns=cols)
    now = pd.Timestamp(today or date.today())
    start = now - pd.DateOffset(months=12)
    end = df["back_date"].fillna(now).clip(upper=now)
    beg = df["out_date"].clip(lower=start)
    df["_days"] = (end - beg).dt.days.clip(lower=0)
    df["_started"] = (df["out_date"] >= start) & (df["out_date"] <= now)
    df["_open"] = df["back_date"].isna()
    g = df.groupby("sku").agg(stockouts_12mo=("_started", "sum"),
                              days_out_12mo=("_days", "sum"),
                              out_now=("_open", "any")).reset_index()
    g = g.rename(columns={"sku": "SKU"})
    g["SKU"] = g["SKU"].astype(str)
    g["stockouts_12mo"] = g["stockouts_12mo"].astype(int)
    g["days_out_12mo"] = g["days_out_12mo"].astype(int)
    if hits is not None:
        h = hits[pd.to_datetime(hits["order_date"]) >= start] \
            if not hits.empty else hits
        per = h.groupby("SKU")["SaleID"].nunique() if not h.empty \
            else pd.Series(dtype=int)
        g["orders_hit_12mo"] = g["SKU"].map(per).fillna(0).astype(int)
    else:
        g["orders_hit_12mo"] = None
    g["stockouts_label"] = g.apply(stockouts_label, axis=1)
    return g[cols]


def stockouts_label(row) -> str:
    """'3 (41d) · 7 orders' = stock-outs started in 12 mo, days out,
    customer orders placed while out. '0' when never out."""
    n = int(row.get("stockouts_12mo") or 0)
    d = int(row.get("days_out_12mo") or 0)
    o = row.get("orders_hit_12mo")
    if n == 0 and d == 0:
        return "0"
    s = f"{n} ({d}d)"
    if o is not None and not (isinstance(o, float) and pd.isna(o)):
        s += f" · {int(o)} order" + ("" if int(o) == 1 else "s")
    if bool(row.get("out_now")):
        s += " · out now"
    return s


def attach(df: pd.DataFrame, summary: pd.DataFrame, sku_col: str = "SKU",
           label_col: str = "Stock-outs 12 mo") -> pd.DataFrame:
    """Add the label column to a page table by SKU ('' when no data)."""
    out = df.copy()
    if out.empty or sku_col not in out.columns:
        out[label_col] = ""
        return out
    m = {} if summary is None or summary.empty else dict(
        zip(summary["SKU"].astype(str), summary["stockouts_label"]))
    out[label_col] = out[sku_col].astype(str).map(m).fillna("")
    return out


def monthly_order_hits(hits: pd.DataFrame, sale_lines: pd.DataFrame,
                       months: list) -> dict:
    """{Period: (orders hit by a stock-out, all orders)} per month —
    distinct SaleIDs."""
    lines = _order_lines(sale_lines)
    tot = (lines.groupby(lines["order_date"].dt.to_period("M"))["SaleID"]
           .nunique() if not lines.empty else pd.Series(dtype=int))
    if hits is None or hits.empty:
        hit = pd.Series(dtype=int)
    else:
        hd = pd.to_datetime(hits["order_date"])
        hit = hits.groupby(hd.dt.to_period("M"))["SaleID"].nunique()
    out = {}
    for m in months:
        p = pd.Period(m, freq="M")
        out[m] = (int(hit.get(p, 0)), int(tot.get(p, 0)))
    return out
