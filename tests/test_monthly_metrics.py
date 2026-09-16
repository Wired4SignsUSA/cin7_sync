"""engine/monthly_metrics.py — the Monthly Metrics page's calculation,
now headless. These pin the formulas the page (and the nightly
publisher) rely on."""
from datetime import datetime

import pandas as pd

from engine import monthly_metrics as mm


def _sale_lines():
    rows = [
        # SaleID, CustomerID, InvoiceDate, SKU, Name, Qty, Price, Disc, Tax, Total, AvgCost, Status, SourceChannel
        ("S1", "C1", "2026-07-05", "LED-1", "Strip", 2, 100, 0, 8, 200, 40, "COMPLETED", "Shopify"),
        ("S1", "C1", "2026-07-05", "Shipping - UPS", "Shipping", 1, 15, 0, 0, 15, 0, "COMPLETED", "Shopify"),
        ("S2", "C2", "2026-07-20", "PRO-1", "Profile", 5, 20, 5, 0, 95, 8, "COMPLETED", "Amazon_US"),
        ("S3", "C1", "2026-08-02", "LED-1", "Strip", 1, 100, 0, 4, 100, 40, "COMPLETED", "Shopify"),
        ("S4", "C3", "2026-08-15", "PRO-1", "Profile", 10, 20, 0, 0, 200, 8, "VOIDED", "Shopify"),
        ("S5", "C4", "2026-08-20", "PRO-1", "Profile", 1, 20, 0, 0, 20, 8, "COMPLETED", None),
    ]
    return pd.DataFrame(rows, columns=[
        "SaleID", "CustomerID", "InvoiceDate", "SKU", "Name", "Quantity",
        "Price", "Discount", "Tax", "Total", "AverageCost", "Status",
        "SourceChannel"])


def _inputs(**kw):
    base = dict(
        sale_lines=_sale_lines(),
        purchase_lines=pd.DataFrame({
            "PurchaseID": ["P1", "P1", "P2"],
            "OrderDate": ["2026-08-01", "2026-08-01", "2026-07-10"],
            "Total": [300.0, 200.0, 100.0]}),
        inv_value_now=10_000.0,
        lookback_months=3,
        today=datetime(2026, 8, 25),
    )
    base.update(kw)
    return mm.MonthlyMetricsInputs(**base)


def _row(res, section, metric):
    return next(r for r in res.rows if r["Section"] == section and r["Metric"] == metric)


def test_sales_overview_excludes_shipping_lines_and_voided():
    res = mm.compute(_inputs())
    assert res.month_labels == ["2026-06", "2026-07", "2026-08"]
    sales = _row(res, "1. Sales Overview [App]", "Sales $")["Values"]
    assert sales == [0.0, 295.0, 120.0]          # S4 voided, shipping line excluded
    orders = _row(res, "1. Sales Overview [App]", "# Orders")["Values"]
    assert orders == [0.0, 2.0, 2.0]
    cogs = _row(res, "1. Sales Overview [App]", "COGS")["Values"]
    assert cogs == [0.0, 2 * 40 + 5 * 8, 40 + 8]
    gp_pct = _row(res, "1. Sales Overview [App]", "GP %")["Values"]
    assert round(gp_pct[1]) == round((295 - 120) / 295 * 100)


def test_discounts_prefer_shopify_api_over_cin7_proxy():
    res = mm.compute(_inputs(shopify_discounts={"2026-08": 55.0}))
    disc = _row(res, "1. Sales Overview [App]", "Discounts")["Values"]
    assert disc == [0.0, -5.0, -55.0]
    assert res.discount_label.startswith("Less: Discounts (Shopify")


def test_customer_metrics():
    res = mm.compute(_inputs())
    s3 = "3. Customer Metrics [App]"
    assert _row(res, s3, "New Customers")["Values"] == [0, 2, 1]        # C3 voided, C4 new
    assert _row(res, s3, "Running Customer Count")["Values"] == [0, 2, 3]
    # Aug: customers C1 (repeat) + C4 (new) -> 50%
    assert _row(res, s3, "Repeat Customer %")["Values"][2] == 50.0


def test_purchases_and_aov():
    res = mm.compute(_inputs())
    s2 = "2. Margins & Purchasing [App]"
    assert _row(res, s2, "# of Purchases")["Values"] == [0.0, 1.0, 1.0]
    assert _row(res, s2, "Purchase $")["Values"] == [0.0, 100.0, 500.0]
    assert _row(res, s2, "Avg Order Value")["Values"][2] == 60.0


def test_channels_and_order_counts():
    res = mm.compute(_inputs())
    s5, s9 = "5. Revenue by Channel [Cin7/DEAR]", "9. Order Counts [Cin7/DEAR]"
    assert _row(res, s5, "Amazon")["Values"] == [0.0, 95.0, 0.0]
    assert _row(res, s5, "B2B / Direct")["Values"] == [0.0, 0.0, 20.0]
    assert _row(res, s5, "Total (CIN7)")["Values"] == [0.0, 295.0, 120.0]
    assert _row(res, s9, "Total Orders")["Values"] == [0, 2, 2]


def test_qb_rows_show_none_for_unsynced_months():
    qb = {"2026-07": {"sales": 1000.0, "cogs": 400.0, "total_income": 1100.0,
                      "qb_gross_profit": 700.0, "shipping_charged": 100.0,
                      "shipping_cost": 150.0}}
    res = mm.compute(_inputs(qb_by_month=qb))
    s7 = "7. Cost & Profitability [QuickBooks]"
    assert _row(res, s7, "Product COGS (QB 500)")["Values"] == [None, 400.0, None]
    s8 = "8. Shipping Detail [QuickBooks]"
    assert _row(res, s8, "Shipping Margin")["Values"] == [0.0, -50.0, 0.0]
    s6 = "6. Sales & Adjustments [QuickBooks]"
    assert _row(res, s6, "Gross Sales (est.)")["Values"][1] == 1005.0  # + CIN7 proxy discount


def test_inventory_walkback_anchors_on_current_value():
    res = mm.compute(_inputs())
    avg_inv = _row(res, "4. Inventory [App]", "Avg Inventory Value")["Values"]
    assert all(v > 0 for v in avg_inv)
    # Current month end-of-month value is the live headline number.
    assert res.details["end_of_month_inv"][res.current_month] == 10_000.0
    goal = _row(res, "4. Inventory [App]", "Stock Goal (suggested)")["Values"]
    assert goal == [None, None, None]


def test_table_ytd_avg_and_markdown_roundtrip():
    res = mm.compute(_inputs())
    table = mm.build_table(res.rows, res.month_labels, res.current_month)
    sales_idx = next(i for i, r in enumerate(res.rows)
                     if r["Section"] == "1. Sales Overview [App]" and r["Metric"] == "Sales $")
    assert table.at[sales_idx, "YTD"] == 415.0
    assert round(table.at[sales_idx, "Avg"], 2) == round(415 / 3, 2)
    md = mm.llm_markdown(res.rows, table, res.month_labels, "(All channels)",
                         generated=datetime(2026, 9, 10, 9, 0))
    assert "## 1. Sales Overview [App]" in md
    assert "| Sales $ | $0 | $295 | $120 | $415 | $138 |" in md
    exp = mm.export_table(res.rows, table)
    assert list(exp.columns[:3]) == ["Section", "Metric", "Format"]
    assert mm.fmt_cell(None, "money") == "—"
    assert mm.fmt_cell(1234.6, "money") == "$1,235"


def test_sale_lines_coverage_gap_detects_uncovered_days():
    """Regression for the Aug-2026 report that went out ~200 orders
    light: a stale long-window file plus a short rolling file must
    produce a gap warning; a fresh long window must not."""
    from datetime import date, datetime, timedelta
    import monthly_metrics_report as r

    today = date.today()
    # Closed month = previous month.
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    month = last_prev.strftime("%Y-%m")
    ts = lambda d: datetime(d.year, d.month, d.day, 12).timestamp()  # noqa: E731

    # Long file last refreshed before the closed month started; 30d
    # rolling file refreshed today -> everything before today-30d in
    # the closed month is uncovered.
    stale_long = ts(last_prev.replace(day=1) - timedelta(days=10))
    wins = [(730, stale_long), (30, ts(today))]
    gap = r.sale_lines_coverage_gap(month, wins)
    if last_prev.replace(day=1) < today - timedelta(days=30):
        assert gap is not None and "UNDERSTATED" in gap
    # Fresh 90d refresh covers the whole closed month -> no warning.
    wins.append((90, ts(today)))
    assert r.sale_lines_coverage_gap(month, wins) is None


def test_purchase_loader_does_not_reset_sale_line_windows(tmp_path):
    """Regression: the 2026-09-16 corrected report warned that all of
    Aug 2026 was uncovered because _load_longest_purchase_lines
    clobbered _SALE_LINES_WINDOWS. Loading purchases after sales must
    leave the sale windows intact."""
    import os
    import time
    import monthly_metrics_report as r

    now = time.time()
    sl = tmp_path / "sale_lines_last_90d_x.csv"
    sl.write_text("SaleID,SKU,Quantity,InvoiceNumber,OrderNumber,"
                  "InvoiceDate,Customer,Total\n"
                  "a,s,1,i,o,2026-08-05,Cust,10\n")
    os.utime(sl, (now, now))
    pl = tmp_path / "purchase_lines_last_1825d_x.csv"
    pl.write_text("PurchaseID,SKU,Quantity,Total\np,s,1,5\n")
    old = now - 200 * 86400
    os.utime(pl, (old, old))

    r._load_longest_sale_lines(tmp_path, pd, lambda df: df)
    before = list(r._SALE_LINES_WINDOWS)
    assert before and before[0][0] == 90
    r._load_longest_purchase_lines(tmp_path, pd)
    assert list(r._SALE_LINES_WINDOWS) == before
