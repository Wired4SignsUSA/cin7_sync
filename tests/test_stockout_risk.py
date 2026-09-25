import pandas as pd

from engine import stock_goal as sg
from engine import stockout_risk as sr


def _eng():
    return pd.DataFrame([
        # out, nothing on order -> flagged
        {"SKU": "OUT", "ABCD": "B", "goal_units": 5, "Available": 0,
         "OnOrder": 0, "unfulfilled": 3, "avg_daily": 0.5,
         "planning_avg_daily": 0.5, "Supplier": "S", "trend_flag": "Stable"},
        # 10 left at 1/day, 35d lead time -> will run out
        {"SKU": "SOON", "ABCD": "A", "goal_units": 40, "Available": 10,
         "OnOrder": 0, "unfulfilled": 0, "avg_daily": 1.0,
         "planning_avg_daily": 1.0, "Supplier": "S", "trend_flag": "Stable"},
        # PO on order -> skipped
        {"SKU": "PO", "ABCD": "A", "goal_units": 40, "Available": 0,
         "OnOrder": 50, "avg_daily": 1.0, "Supplier": "S"},
        # enough stock -> skipped
        {"SKU": "OK", "ABCD": "A", "goal_units": 40, "Available": 100,
         "OnOrder": 0, "avg_daily": 1.0, "Supplier": "S"},
        # dropship / project / no goal -> skipped
        {"SKU": "DS", "ABCD": "A", "goal_units": 4, "Available": 0,
         "OnOrder": 0, "excess_exempt": True, "Supplier": "S"},
        {"SKU": "PJ", "ABCD": "A", "goal_units": 4, "Available": 0,
         "OnOrder": 0, "trend_flag": "🎯 Project", "Supplier": "S"},
        {"SKU": "NG", "ABCD": "C", "goal_units": 0, "Available": 0,
         "OnOrder": 0, "Supplier": "S"},
        # finishing build SKU, 14d build lead time
        {"SKU": "FIN", "ABCD": "C", "goal_units": 6, "Available": 2,
         "OnOrder": 0, "avg_daily": 0.5, "Supplier": "X"},
        # 865FabLab supplier, no BOM service line -> Corners by supplier
        {"SKU": "CRN", "ABCD": "C", "goal_units": 6, "Available": 0,
         "OnOrder": 0, "avg_daily": 0.5, "Supplier": "865FabLab"},
    ])


def test_risk_table_flags_only_uncovered_stocked_skus():
    t = sr.risk_table(_eng(), lead_time_fn=lambda s, sup: 35,
                      stockouts_12mo={"OUT": 3},
                      build_skus={"FIN": "Finishing"},
                      build_suppliers={"865FabLab": "Corners"})
    assert list(t["SKU"]) == ["OUT", "CRN", "SOON", "FIN"]
    assert t.set_index("SKU").loc["CRN", "build"] == "Corners"
    assert t.set_index("SKU").loc["FIN", "lead_time"] == sr.BUILD_LEAD_TIME_DAYS
    msg = sr.format_message(t)
    assert "1 out now" in msg and "1 will run out" in msg
    assert "ran out 3×" in msg and "3 backordered" in msg


def test_lead_time_precedence():
    cfg = {"S": {"lead_time_sea_days": 40, "lead_time_air_days": 21,
                 "air_eligible_default": 1}}
    assert sr.lead_time_days("X", "S", supplier_cfgs=cfg) == 21
    assert sr.lead_time_days("X", "S", supplier_cfgs=cfg,
                             ip_lead_times={"X": {"observed_lead_time_days": 18}}) == 18
    assert sr.lead_time_days("X", "S", supplier_cfgs=cfg,
                             sku_lead_times={"X": 9}) == 9
    assert sr.lead_time_days("X", "unknown") == sr.DEFAULT_LEAD_TIME_DAYS


def test_empty_message():
    assert "nothing to flag" in sr.format_message(pd.DataFrame())


def test_trend_rows_use_higher_rate():
    # 12mo base 0.1/day, trend-adjusted 0.4/day
    assert sg.planning_avg_daily(0.4, 40, 30, avg_daily_base=0.1,
                                 trend_flag="📈 Trend") == 0.4
    # non-trend keeps the 12-month rate
    assert sg.planning_avg_daily(0.4, 40, 30, avg_daily_base=0.1,
                                 trend_flag="⚡ Sporadic") == 0.1
    # trend but nothing in 90 days -> still clamped to 0
    assert sg.planning_avg_daily(0.4, 40, 0, avg_daily_base=0.1,
                                 trend_flag="📈 Trend") == 0.0


def test_repeat_stockout_extra_pct():
    assert sg.repeat_stockout_extra_pct(0) == 0
    assert sg.repeat_stockout_extra_pct(1) == 0
    assert sg.repeat_stockout_extra_pct(2) == 25
    assert sg.repeat_stockout_extra_pct(3) == 25
    assert sg.repeat_stockout_extra_pct(4) == 50
    assert sg.repeat_stockout_extra_pct(None) == 0
