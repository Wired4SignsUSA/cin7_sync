"""engine/monthly_metrics.py — the Monthly Metrics calculation, headless.

One implementation of every row on the dashboard's "Monthly Metrics"
page (Sales & Marketing section). The Streamlit page renders what this
module returns; `publish_monthly_metrics.py` publishes the same table
to Postgres `dataset_files` nightly so external consumers (Viktor's
monthly financial report) read exactly the figures the page shows —
by construction, not by re-derivation.

James (2026-09-10): the app's Monthly Metrics page is the source of
truth for the monthly financial report. Any change to a formula here
changes the page, the CSV export, the LLM markdown and the published
dataset together.

Pure pandas — no Streamlit, no db. Callers load the inputs (see
`MonthlyMetricsInputs`) and pass them in.

Formulas are lifted verbatim from app.py (page block that used to live
inline, v2.67.298 – v2.67.3xx) — see the methodology expander on the
page for the plain-language definitions of each row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

SECTION_ORDER: List[str] = [
    "1. Sales Overview [App]",
    "2. Margins & Purchasing [App]",
    "3. Customer Metrics [App]",
    "4. Inventory [App]",
    "5. Revenue by Channel [Cin7/DEAR]",
    "6. Sales & Adjustments [QuickBooks]",
    "7. Cost & Profitability [QuickBooks]",
    "8. Shipping Detail [QuickBooks]",
    "9. Order Counts [Cin7/DEAR]",
]

BAD_SALE_STATUSES = ("VOIDED", "CREDITED", "CANCELLED", "CANCELED")
SHIPPING_LINE_RE = r"(?i)^(shipping|freight|handling|delivery)"


@dataclass
class MonthlyMetricsInputs:
    """Everything the calculation needs. All frames are the dashboard's
    already-loaded (union + dedup + excluded-customer-filtered) frames."""
    sale_lines: pd.DataFrame
    purchase_lines: pd.DataFrame = field(default_factory=pd.DataFrame)
    shopify_orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    sales_headers: pd.DataFrame = field(default_factory=pd.DataFrame)
    inv_value_now: float = 0.0                 # _headline_stock_value(stock, products)
    shopify_discounts: Dict[str, float] = field(default_factory=dict)  # db.all_shopify_monthly_discounts()
    qb_by_month: Dict[str, Dict[str, float]] = field(default_factory=dict)  # db.qbo_monthly_pl_summary_by_category()
    stock_goal_rows: List[dict] = field(default_factory=list)   # db.list_stock_goal_snapshots()
    dormancy_warnings: Dict[str, dict] = field(default_factory=dict)  # db.get_dormancy_warnings()
    slow_mover_snapshots: List[dict] = field(default_factory=list)  # db.list_slow_mover_snapshots()
    live_slow_stock_value: Optional[float] = None  # _compute_slow_stock_holding(...)["value_held"]
    channel: str = "(All channels)"
    lookback_months: int = 14
    today: Optional[datetime] = None


@dataclass
class MonthlyMetricsResult:
    rows: List[dict]                 # [{Section, Metric, Format, Values}]
    months: pd.PeriodIndex
    month_labels: List[str]
    current_month: pd.Period
    channels: List[str]              # channel options offered on the page
    discount_label: str
    qb_anomaly_input: Dict[str, Dict[str, float]]  # qb_by_month, for the page's anomaly caption
    details: Dict[str, Any] = field(default_factory=dict)  # intermediates the page's charts/tooltips reuse


def _to_num(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def prepare_sale_lines(sale_lines: pd.DataFrame) -> pd.DataFrame:
    """Type the sale_lines frame for monthly grouping and drop
    voided / credited / cancelled statuses (booked-and-kept only)."""
    sl = sale_lines.copy()
    sl["InvoiceDate"] = pd.to_datetime(
        sl["InvoiceDate"], errors="coerce", utc=True).dt.tz_localize(None)
    sl["Quantity"] = _to_num(sl["Quantity"]).fillna(0)
    sl["Price"] = _to_num(sl["Price"]).fillna(0)
    sl["Discount"] = _to_num(sl["Discount"]).fillna(0)
    sl["Tax"] = _to_num(sl["Tax"]).fillna(0)
    sl["Total"] = _to_num(sl["Total"]).fillna(0)
    sl["AverageCost"] = _to_num(sl.get("AverageCost", 0)).fillna(0)
    sl = sl.dropna(subset=["InvoiceDate"])
    sl["MonthKey"] = sl["InvoiceDate"].dt.to_period("M")
    if "Status" in sl.columns:
        stat_upper = sl["Status"].astype(str).str.upper()
        sl = sl[~stat_upper.isin(BAD_SALE_STATUSES)]
    return sl


def channel_options(sl: pd.DataFrame) -> List[str]:
    channels = ["(All channels)"]
    if "SourceChannel" in sl.columns:
        channels += sorted(
            sl["SourceChannel"].dropna().astype(str).unique().tolist())
    return channels


def channel_of_row(sc_val, sr_val) -> str:
    sc = (str(sc_val) if sc_val is not None else "").strip().lower()
    sr = (str(sr_val) if sr_val is not None else "").strip().upper()
    if "shopify" in sc:
        return "Shopify"
    if "amazon" in sc or sr == "AMAZON":
        return "Amazon"
    if "ebay" in sc or sr == "EBAY":
        return "eBay"
    if sr == "SHOPIFY":
        return "Shopify"
    return "B2B / Direct"


def _series_get(series, m) -> float:
    try:
        v = series.get(m, 0)
        return float(v) if pd.notna(v) else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def compute(inp: MonthlyMetricsInputs) -> MonthlyMetricsResult:  # noqa: C901
    """Build every row of the Monthly Metrics table."""
    sl = prepare_sale_lines(inp.sale_lines)
    channels = channel_options(sl)
    if inp.channel != "(All channels)" and "SourceChannel" in sl.columns:
        sl = sl[sl["SourceChannel"].astype(str) == inp.channel]

    today_ts = pd.Timestamp((inp.today or datetime.now()).date())
    current_month = today_ts.to_period("M")
    months = pd.period_range(end=current_month,
                             periods=int(inp.lookback_months), freq="M")
    month_labels = [str(m) for m in months]

    # Shipping/freight fake-SKU lines are excluded from product metrics.
    _ship_skus = sl["SKU"].astype(str).str.match(SHIPPING_LINE_RE, na=False)
    _ship_names = sl["Name"].astype(str).str.match(SHIPPING_LINE_RE, na=False)
    sl_prod = sl[~(_ship_skus | _ship_names)].copy()

    gl = sl_prod.groupby("MonthKey")
    sales_per_month = gl["Total"].sum()
    quantity_per_month = gl["Quantity"].sum()
    discount_per_month = gl["Discount"].sum()
    tax_per_month = gl["Tax"].sum()
    cogs_per_month = (sl_prod["Quantity"] * sl_prod["AverageCost"]
                      ).groupby(sl_prod["MonthKey"]).sum()
    orders_per_month = sl.groupby("MonthKey")["SaleID"].nunique()

    # Customers
    cust_first_seen = (sl.dropna(subset=["CustomerID"])
                       .groupby("CustomerID")["MonthKey"].min())
    cust_last_seen = (sl.dropna(subset=["CustomerID"])
                      .groupby("CustomerID")["MonthKey"].max())
    new_customers = cust_first_seen.value_counts()

    def _running_customers(m):
        return int((cust_first_seen <= m).sum())

    def _lost_customers(m):
        return int((cust_last_seen == (m - 3)).sum())

    def _repeat_customer_pct(m):
        month_df = sl[sl["MonthKey"] == m]
        if month_df.empty:
            return 0.0
        month_customers = month_df["CustomerID"].dropna().unique()
        repeat_count = 0
        for cust in month_customers:
            first = cust_first_seen.get(cust)
            if first is not None and first < m:
                repeat_count += 1
        total = len(month_customers)
        return (repeat_count / total * 100) if total else 0.0

    # Purchases
    pl_mm = pd.DataFrame()
    if inp.purchase_lines is not None and not inp.purchase_lines.empty:
        pl_mm = inp.purchase_lines.copy()
        pl_mm["OrderDate"] = pd.to_datetime(pl_mm["OrderDate"], errors="coerce")
        pl_mm = pl_mm.dropna(subset=["OrderDate"])
        pl_mm["Total"] = _to_num(pl_mm.get("Total", 0)).fillna(0)
        pl_mm["MonthKey"] = pl_mm["OrderDate"].dt.to_period("M")
    if not pl_mm.empty:
        po_per_month = pl_mm.groupby("MonthKey")["PurchaseID"].nunique()
        po_spend_per_month = pl_mm.groupby("MonthKey")["Total"].sum()
    else:
        po_per_month = pd.Series(dtype=float)
        po_spend_per_month = pd.Series(dtype=float)

    _get = _series_get
    rows: List[dict] = []

    def _row(section, label, values, fmt="money"):
        rows.append({"Section": section, "Metric": label,
                     "Format": fmt, "Values": values})

    def _per_month(fn: Callable[[pd.Period], Any]):
        return [fn(m) for m in months]

    # Discounts: Shopify Admin API value when present, CIN7 proxy otherwise.
    _shopify_disc = inp.shopify_discounts or {}

    def _discounts_for(m):
        v = _shopify_disc.get(str(m))
        if v is not None and float(v) > 0:
            return float(v)
        return abs(_get(discount_per_month, m))

    # ===== 1 · Sales Overview [App] ====================================
    S1 = "1. Sales Overview [App]"
    _row(S1, "Sales $", _per_month(lambda m: _get(sales_per_month, m)))
    _row(S1, "Sales $ with Tax",
         _per_month(lambda m: _get(sales_per_month, m) + _get(tax_per_month, m)))
    _row(S1, "# Orders", _per_month(lambda m: _get(orders_per_month, m)), fmt="int")
    _row(S1, "Quantity Sold", _per_month(lambda m: _get(quantity_per_month, m)), fmt="int")
    _row(S1, "COGS", _per_month(lambda m: _get(cogs_per_month, m)))
    _row(S1, "Discounts", _per_month(lambda m: -_discounts_for(m)))
    _row(S1, "Tax $", _per_month(lambda m: _get(tax_per_month, m)))
    _row(S1, "Gross Profit",
         _per_month(lambda m: _get(sales_per_month, m) - _get(cogs_per_month, m)))
    _row(S1, "GP %",
         _per_month(lambda m: (
             (_get(sales_per_month, m) - _get(cogs_per_month, m))
             / _get(sales_per_month, m) * 100
             if _get(sales_per_month, m) else 0.0)),
         fmt="pct")

    # QuickBooks helpers
    _qb_by_month = inp.qb_by_month or {}

    def _qb(m, cat):
        return float((_qb_by_month.get(str(m)) or {}).get(cat, 0.0) or 0.0)

    def _qb_has_data(cat):
        return any(_qb(m, cat) for m in months)

    def _qb_month_synced(m):
        return str(m) in _qb_by_month

    def _qb_per_month(fn):
        return [(fn(m) if _qb_month_synced(m) else None) for m in months]

    # ===== 2 · Margins & Purchasing [App] ==============================
    S2 = "2. Margins & Purchasing [App]"
    _row(S2, "Avg Order Value",
         _per_month(lambda m: (_get(sales_per_month, m) / _get(orders_per_month, m)
                               if _get(orders_per_month, m) else 0.0)))
    _row(S2, "# of Purchases", _per_month(lambda m: _get(po_per_month, m)), fmt="int")
    _row(S2, "Purchase $", _per_month(lambda m: _get(po_spend_per_month, m)))

    # ===== 8 · Shipping Detail [QuickBooks] ============================
    S8 = "8. Shipping Detail [QuickBooks]"
    if _qb_has_data("shipping_charged"):
        _row(S8, "Shipping Charged (QB 405)",
             _per_month(lambda m: _qb(m, "shipping_charged")))
    if _qb_has_data("shipping_cost"):
        _row(S8, "Shipping-Out Cost (QB 694)",
             _per_month(lambda m: _qb(m, "shipping_cost")))
    if _qb_has_data("shipping_charged") and _qb_has_data("shipping_cost"):
        _row(S8, "Shipping Margin",
             _per_month(lambda m: _qb(m, "shipping_charged") - _qb(m, "shipping_cost")))
        _row(S8, "Margin %",
             _per_month(lambda m: (
                 (_qb(m, "shipping_charged") - _qb(m, "shipping_cost"))
                 / _qb(m, "shipping_charged") * 100
                 if _qb(m, "shipping_charged") else 0.0)),
             fmt="pct")

    # ===== 6 · Sales & Adjustments [QuickBooks] ========================
    S6 = "6. Sales & Adjustments [QuickBooks]"
    _disc_label = ("Less: Discounts (Shopify Admin API)" if _shopify_disc
                   else "Less: Discounts (CIN7 — proxy until Shopify sync runs)")
    if _qb_has_data("sales"):
        _row(S6, "Gross Sales (est.)",
             _per_month(lambda m: _qb(m, "sales") + _discounts_for(m)))
        _row(S6, _disc_label, _per_month(_discounts_for))
        _row(S6, "Net Sales (QB 400)", _per_month(lambda m: _qb(m, "sales")))
    if _qb_has_data("shipping_charged"):
        _row(S6, "Shipping Income (QB 405)",
             _per_month(lambda m: _qb(m, "shipping_charged")))
    if _qb_has_data("total_income"):
        _row(S6, "Total Revenue (QB Total Income)",
             _per_month(lambda m: _qb(m, "total_income")))

    # ===== 7 · Cost & Profitability [QuickBooks] =======================
    S7 = "7. Cost & Profitability [QuickBooks]"
    if _qb_has_data("cogs"):
        _row(S7, "Product COGS (QB 500)", _qb_per_month(lambda m: _qb(m, "cogs")))
    if _qb_has_data("cogs_amazon_fees"):
        _row(S7, "Amazon Fees (QB 502)",
             _qb_per_month(lambda m: _qb(m, "cogs_amazon_fees")))
    if _qb_has_data("inventory_adjustment"):
        _row(S7, "Inventory Adj (QB 550)",
             _qb_per_month(lambda m: _qb(m, "inventory_adjustment")))
    if _qb_has_data("total_cogs"):
        _row(S7, "Total COGS", _qb_per_month(lambda m: _qb(m, "total_cogs")))
    if _qb_has_data("qb_gross_profit"):
        _row(S7, "Gross Profit", _qb_per_month(lambda m: _qb(m, "qb_gross_profit")))
        if _qb_has_data("total_income"):
            _row(S7, "GP %",
                 _qb_per_month(lambda m: (
                     _qb(m, "qb_gross_profit") / _qb(m, "total_income") * 100
                     if _qb(m, "total_income") else 0.0)),
                 fmt="pct")
    if _qb_has_data("qb_total_expenses"):
        _row(S7, "Total OpEx", _qb_per_month(lambda m: _qb(m, "qb_total_expenses")))
    if _qb_has_data("qb_net_operating_income"):
        _row(S7, "Operating Profit",
             _qb_per_month(lambda m: _qb(m, "qb_net_operating_income")))
        if _qb_has_data("total_income"):
            _row(S7, "Op Margin %",
                 _qb_per_month(lambda m: (
                     _qb(m, "qb_net_operating_income") / _qb(m, "total_income") * 100
                     if _qb(m, "total_income") else 0.0)),
                 fmt="pct")
    if _qb_has_data("qb_net_income"):
        _row(S7, "Net Income (QB)", _qb_per_month(lambda m: _qb(m, "qb_net_income")))

    # ===== 5 / 9 · Channels ===========================================
    _sales_hdr = inp.sales_headers if inp.sales_headers is not None else pd.DataFrame()
    if (not _sales_hdr.empty and "SaleID" in sl_prod.columns
            and "SalesRepresentative" in _sales_hdr.columns):
        _rep_map = (_sales_hdr.dropna(subset=["SaleID"])
                    .drop_duplicates("SaleID")
                    .set_index("SaleID")["SalesRepresentative"].to_dict())
        if "SalesRepresentative" not in sl_prod.columns:
            sl_prod = sl_prod.assign(
                SalesRepresentative=sl_prod["SaleID"].map(_rep_map))
        else:
            sl_prod = sl_prod.assign(
                SalesRepresentative=sl_prod["SalesRepresentative"].fillna(
                    sl_prod["SaleID"].map(_rep_map)))

    _shop_rev_by_month_source = pd.Series(dtype=float)
    _shop_cnt_by_month_source = pd.Series(dtype="int64")
    _shop_rev_by_month_total = pd.Series(dtype=float)
    _shop_cnt_by_month_total = pd.Series(dtype="int64")
    so = inp.shopify_orders
    if (so is not None and not so.empty
            and "CreatedAt" in so.columns and "SourceName" in so.columns):
        _so = so.copy()
        _so["_dt"] = pd.to_datetime(_so["CreatedAt"], errors="coerce",
                                    utc=True).dt.tz_localize(None)
        _so = _so.dropna(subset=["_dt"])
        _so["MonthKey"] = _so["_dt"].dt.to_period("M")
        _so["TotalPrice"] = pd.to_numeric(_so.get("TotalPrice"),
                                          errors="coerce").fillna(0)
        _shop_rev_by_month_source = _so.groupby(["MonthKey", "SourceName"])["TotalPrice"].sum()
        _shop_cnt_by_month_source = _so.groupby(["MonthKey", "SourceName"]).size()
        _shop_rev_by_month_total = _so.groupby("MonthKey")["TotalPrice"].sum()
        _shop_cnt_by_month_total = _so.groupby("MonthKey").size()

    def _shopify_split_rev(m):
        online = float(_shop_rev_by_month_source.get((m, "web"), 0.0))
        draft = float(_shop_rev_by_month_source.get((m, "shopify_draft_order"), 0.0))
        total = float(_shop_rev_by_month_total.get(m, 0.0))
        return online, draft, max(total - online - draft, 0.0)

    def _shopify_split_cnt(m):
        online = int(_shop_cnt_by_month_source.get((m, "web"), 0))
        draft = int(_shop_cnt_by_month_source.get((m, "shopify_draft_order"), 0))
        total = int(_shop_cnt_by_month_total.get(m, 0))
        return online, draft, max(total - online - draft, 0)

    S5 = "5. Revenue by Channel [Cin7/DEAR]"
    S9 = "9. Order Counts [Cin7/DEAR]"
    _has_sc = "SourceChannel" in sl_prod.columns
    _has_sr = "SalesRepresentative" in sl_prod.columns
    if _has_sc or _has_sr:
        _sc_col = (sl_prod["SourceChannel"] if _has_sc
                   else pd.Series([""] * len(sl_prod), index=sl_prod.index))
        _sr_col = (sl_prod["SalesRepresentative"] if _has_sr
                   else pd.Series([""] * len(sl_prod), index=sl_prod.index))
        _chans = pd.Series([channel_of_row(sc, sr) for sc, sr in zip(_sc_col, _sr_col)],
                           index=sl_prod.index)
        _slp_rev = sl_prod.assign(_chan=_chans)
        _rev_by_chan_month = _slp_rev.groupby(["MonthKey", "_chan"])["Total"].sum()
        _orders_by_chan_month = _slp_rev.groupby(["MonthKey", "_chan"])["SaleID"].nunique()

        _other_channels = ["B2B / Direct", "Amazon", "eBay"]
        _shopify_sub_labels = ["Shopify (Online Store)", "Shopify (Draft Orders)",
                               "Shopify (Other/Unclassified)"]

        for _idx, _chan in enumerate(_shopify_sub_labels):
            _row(S5, _chan, _per_month(lambda m, i=_idx: _shopify_split_rev(m)[i]))
        _row(S5, "Shopify Total", _per_month(lambda m: sum(_shopify_split_rev(m))))
        for _chan in _other_channels:
            _row(S5, _chan, _per_month(
                lambda m, c=_chan: float(_rev_by_chan_month.get((m, c), 0) or 0)))
        _row(S5, "Total (CIN7)", _per_month(
            lambda m: float(sum(_rev_by_chan_month.get((m, c), 0) or 0
                                for c in ["Shopify"] + _other_channels))))
        if _qb_has_data("sales"):
            _row(S5, "Net Sales (QB 400)", _per_month(lambda m: _qb(m, "sales")))

        for _idx, _chan in enumerate(_shopify_sub_labels):
            _cnt_label = f"{_chan} Count" if "Order" in _chan else f"{_chan} Orders"
            _row(S9, _cnt_label,
                 _per_month(lambda m, i=_idx: _shopify_split_cnt(m)[i]), fmt="int")
        _row(S9, "Shopify Total Orders",
             _per_month(lambda m: sum(_shopify_split_cnt(m))), fmt="int")
        for _chan in _other_channels:
            _row(S9, f"{_chan} Orders", _per_month(
                lambda m, c=_chan: int(_orders_by_chan_month.get((m, c), 0) or 0)),
                fmt="int")
        _row(S9, "Total Orders", _per_month(
            lambda m: int(sum(_orders_by_chan_month.get((m, c), 0) or 0
                              for c in ["Shopify"] + _other_channels))),
            fmt="int")

    # ===== 3 · Customer Metrics [App] ==================================
    S3 = "3. Customer Metrics [App]"
    _row(S3, "New Customers", _per_month(lambda m: int(new_customers.get(m, 0))), fmt="int")
    _row(S3, "Running Customer Count", _per_month(_running_customers), fmt="int")
    _row(S3, "Lost Customers (3mo)", _per_month(_lost_customers), fmt="int")
    _row(S3, "Repeat Customer %", _per_month(_repeat_customer_pct), fmt="pct")

    # ===== 4 · Inventory [App] =========================================
    S4 = "4. Inventory [App]"
    inv_value_now = float(inp.inv_value_now or 0.0)
    raw_end: dict = {}
    running_inv = inv_value_now
    raw_end[current_month] = running_inv
    for m in reversed(months[:-1]):
        next_m = m + 1
        running_inv = (running_inv + _get(cogs_per_month, next_m)
                       - _get(po_spend_per_month, next_m))
        raw_end[m] = running_inv

    end_of_month_inv: dict = {}
    oldest_m = months[0]
    raw_oldest = raw_end.get(oldest_m, inv_value_now)
    target_oldest = (raw_oldest + inv_value_now) / 2.0
    cap_delta = 0.15 * max(inv_value_now, 1.0)
    if abs(target_oldest - inv_value_now) > cap_delta:
        target_oldest = inv_value_now + cap_delta * (
            1 if target_oldest > inv_value_now else -1)
    n = len(months)
    for idx, m in enumerate(months):
        alpha = idx / max(n - 1, 1)
        raw_v = raw_end.get(m, inv_value_now)
        ideal = target_oldest + (inv_value_now - target_oldest) * (idx / max(n - 1, 1))
        end_of_month_inv[m] = max(alpha * raw_v + (1 - alpha) * ideal, 0.0)

    def _avg_inv(m):
        end_v = end_of_month_inv.get(m, inv_value_now)
        begin_v = end_of_month_inv.get(m - 1, end_v)
        return (begin_v + end_v) / 2.0

    _row(S4, "Avg Inventory Value", _per_month(_avg_inv))
    _row(S4, "Stock Turn (annualised)",
         _per_month(lambda m: ((_get(cogs_per_month, m) * 12) / _avg_inv(m)
                               if _avg_inv(m) else 0.0)),
         fmt="num1")

    _goal_by_month: dict = {}
    _goal_month_complete: dict = {}
    for _gr in (inp.stock_goal_rows or []):
        try:
            _gd = pd.to_datetime(_gr.get("snapshot_date"), errors="coerce")
            _gv = float(_gr.get("goal_value") or 0)
            if pd.isna(_gd) or _gv <= 0:
                continue
            _gm = pd.Period(_gd, freq="M")
            _complete = float(_gr.get("reorder_level_value") or 0) > 0
            if _complete or not _goal_month_complete.get(_gm):
                _goal_by_month[_gm] = _gv
                _goal_month_complete[_gm] = _complete
        except Exception:  # noqa: BLE001
            continue

    def _goal_for(m):
        return _goal_by_month.get(m)

    def _gap_vs_goal(m):
        g = _goal_for(m)
        if g is None:
            return None
        return end_of_month_inv.get(m, inv_value_now) - g

    _row(S4, "Stock Goal (suggested)", _per_month(_goal_for))
    _row(S4, "Stock Over / (Short of) Goal", _per_month(_gap_vs_goal))

    _slow_skus = set((inp.dormancy_warnings or {}).keys())
    if _slow_skus and not sl_prod.empty:
        _slow_mask = sl_prod["SKU"].astype(str).isin(_slow_skus)
        _slow_sold_per_month = (
            (sl_prod.loc[_slow_mask, "Quantity"] * sl_prod.loc[_slow_mask, "AverageCost"])
            .groupby(sl_prod.loc[_slow_mask, "MonthKey"]).sum())
    else:
        _slow_sold_per_month = pd.Series(dtype=float)
    _row(S4, "Slow Stock Cleared", _per_month(lambda m: _get(_slow_sold_per_month, m)))

    _snap_by_month: dict = {}
    for _sr in (inp.slow_mover_snapshots or []):
        try:
            _sd = pd.to_datetime(_sr.get("snapshot_date"), errors="coerce")
            if pd.isna(_sd):
                continue
            _mk = pd.Period(_sd, freq="M")
            _val = float(_sr.get("value_on_shelf") or 0)
            _prev = _snap_by_month.get(_mk)
            if _prev is None or _val > 0:
                _snap_by_month[_mk] = _val
        except Exception:  # noqa: BLE001
            continue
    if inp.live_slow_stock_value is not None:
        _snap_by_month[current_month] = float(inp.live_slow_stock_value or 0)
    _row(S4, "Slow Stock Value (EOM)", _per_month(lambda m: _snap_by_month.get(m)))

    details = {
        "get": _get, "qb": _qb, "discounts_for": _discounts_for,
        "sales_per_month": sales_per_month, "cogs_per_month": cogs_per_month,
        "orders_per_month": orders_per_month,
        "rev_by_chan_month": (_rev_by_chan_month if (_has_sc or _has_sr)
                              else pd.Series(dtype=float)),
        "goal_by_month": _goal_by_month, "end_of_month_inv": end_of_month_inv,
        "snap_by_month": _snap_by_month, "inv_value_now": inv_value_now,
        "stock_goal_rows": list(inp.stock_goal_rows or []),
        "new_customers": new_customers, "lost_customers": _lost_customers,
        "shopify_split_rev": _shopify_split_rev,
        "shopify_split_cnt": _shopify_split_cnt,
        "orders_by_chan_month": (_orders_by_chan_month if (_has_sc or _has_sr)
                                 else pd.Series(dtype=float)),
    }
    return MonthlyMetricsResult(
        rows=rows, months=months, month_labels=month_labels,
        current_month=current_month, channels=channels,
        discount_label=_disc_label, qb_anomaly_input=_qb_by_month,
        details=details)


# ---------------------------------------------------------------------------
# Table + formatting (shared by the page, the CSV export and the publisher)
# ---------------------------------------------------------------------------
def build_table(rows: List[dict], month_labels: List[str],
                current_month: pd.Period, show_ytd: bool = True) -> pd.DataFrame:
    """Rows → wide DataFrame (Section, Metric, <month>..., [YTD, Avg])."""
    display_rows = []
    for r in rows:
        row = {"Section": r["Section"], "Metric": r["Metric"]}
        for lbl, v in zip(month_labels, r["Values"]):
            row[lbl] = v
        display_rows.append(row)
    table_df = pd.DataFrame(display_rows)
    if show_ytd and not table_df.empty:
        ytd_year = current_month.year
        ytd_labels = [lbl for lbl in month_labels if int(lbl.split("-")[0]) == ytd_year]
        for idx, r in enumerate(rows):
            ytd_vals = [v for lbl, v in zip(month_labels, r["Values"])
                        if lbl in ytd_labels and v is not None]
            avg_vals = [v for v in r["Values"] if v is not None]
            if r["Format"] == "pct":
                table_df.at[idx, "YTD"] = sum(ytd_vals) / len(ytd_vals) if ytd_vals else 0.0
            else:
                table_df.at[idx, "YTD"] = sum(ytd_vals)
            table_df.at[idx, "Avg"] = sum(avg_vals) / len(avg_vals) if avg_vals else 0.0
    return table_df


def fmt_cell(v, fmt: str) -> str:
    try:
        if pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    try:
        v = float(v)
    except (ValueError, TypeError):
        return str(v)
    if fmt == "money":
        return f"${v:,.0f}"
    if fmt == "pct":
        return f"{v:.0f}%"
    if fmt == "int":
        return f"{v:,.0f}"
    if fmt == "num1":
        return f"{v:.1f}"
    return f"{v:,.2f}"


def ordered_sections(rows: List[dict]) -> List[str]:
    """SECTION_ORDER first, then any section not listed (first appearance)."""
    out = list(SECTION_ORDER)
    for r in rows:
        if r["Section"] not in out:
            out.append(r["Section"])
    return out


def llm_markdown(rows: List[dict], table_df: pd.DataFrame, month_labels: List[str],
                 channel: str, show_ytd: bool = True,
                 generated: Optional[datetime] = None) -> str:
    """The 'LLM-ready markdown' export — identical to the page's."""
    lines = [
        "# Monthly Metrics — Wired4Signs USA",
        f"**Channel:** {channel}  "
        f"**Months:** {month_labels[0]} to {month_labels[-1]}  "
        f"**Generated:** {(generated or datetime.now()):%Y-%m-%d %H:%M}",
        "",
        "Please write a business commentary based on these numbers. "
        "Highlight: MoM trends, which channels / customer segments "
        "are driving growth, any metric that shifted >10% vs "
        "prior month, and flag anything that warrants a closer look. "
        "Keep it punchy — paste-to-Slack length.",
        "",
    ]
    for section in ordered_sections(rows):
        sect_rows = [r for r in rows if r["Section"] == section]
        if not sect_rows:
            continue
        lines.append(f"## {section}")
        headers = ["Metric"] + list(month_labels) + (["YTD", "Avg"] if show_ytd else [])
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        for r in sect_rows:
            vals = [fmt_cell(v, r["Format"]) for v in r["Values"]]
            if show_ytd:
                idx = next(i for i, rr in enumerate(rows) if rr is r)
                vals.append(fmt_cell(table_df.at[idx, "YTD"], r["Format"]))
                vals.append(fmt_cell(table_df.at[idx, "Avg"], r["Format"]))
            lines.append("| " + r["Metric"] + " | " + " | ".join(vals) + " |")
        lines.append("")
    return "\n".join(lines)


def export_table(rows: List[dict], table_df: pd.DataFrame) -> pd.DataFrame:
    """Machine-readable export: table_df plus the Format column so a
    consumer can render values the same way the page does."""
    out = table_df.copy()
    out.insert(2, "Format", [r["Format"] for r in rows])
    return out
