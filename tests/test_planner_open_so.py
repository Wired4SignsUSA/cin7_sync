"""Planner: open sales-order allocations count as demand (James, 2026-09-18,
SO-62212 — 50 auto-assembled units hid a 50-unit shortage)."""
import pandas as pd

from app_pages.fablab_work_orders import build_planner_table


def _frames(on_hand, allocated, units_12mo=12.0):
    products = pd.DataFrame([{"SKU": "FIN-1", "Name": "Finished"}])
    stock = pd.DataFrame([{"SKU": "FIN-1", "OnHand": on_hand,
                           "Allocated": allocated, "Available": on_hand - allocated,
                           "OnOrder": 0}])
    engine = pd.DataFrame([{"SKU": "FIN-1", "Name": "Finished", "OnHand": on_hand,
                            "units_12mo": units_12mo, "ABC": "B", "Status": ""}])
    bom_parents = {"FIN-1": [{"ComponentSKU": "RAW-1", "Quantity": 1},
                             {"ComponentSKU": "OSC-POWDERCOAT-BK-SML-FT", "Quantity": 8}]}
    return products, stock, engine, bom_parents


def test_open_so_adds_to_suggested_batch():
    df = build_planner_table(["FIN-1"], *_frames(on_hand=0, allocated=50),
                             weeks_cover=8.0)
    row = df.iloc[0]
    assert row["Open SO"] == 50
    # 1/mo * 8/4.345 = 1.84 -> +50 -> ceil = 52
    assert row["Suggested batch"] == 52


def test_auto_assembled_stock_allocated_to_same_order_still_covered():
    # AutoAssembly ON case: 50 on hand, 50 allocated -> stock covers the SO,
    # only the forecast window remains (planner cannot see it's phantom).
    df = build_planner_table(["FIN-1"], *_frames(on_hand=50, allocated=50),
                             weeks_cover=8.0)
    assert df.iloc[0]["Suggested batch"] == 2


def test_no_allocated_column_is_fine():
    products, stock, engine, bom = _frames(on_hand=5, allocated=0)
    stock = stock.drop(columns=["Allocated"])
    df = build_planner_table(["FIN-1"], products, stock, engine, bom, weeks_cover=4.345)
    assert df.iloc[0]["Open SO"] == 0
    assert df.iloc[0]["Suggested batch"] == 0


def test_below_min_monthly_no_stock_topup():
    # 6/yr = 0.5/mo < 1 -> no stock batch (James 2026-09-25, finishing)
    df = build_planner_table(["FIN-1"], *_frames(on_hand=0, allocated=0, units_12mo=6.0),
                             weeks_cover=8.0, min_monthly_for_stock=1.0)
    row = df.iloc[0]
    assert bool(row["Below min demand"]) is True
    assert row["Suggested batch"] == 0


def test_below_min_monthly_still_covers_open_orders():
    df = build_planner_table(["FIN-1"], *_frames(on_hand=0, allocated=5, units_12mo=6.0),
                             weeks_cover=8.0, min_monthly_for_stock=1.0)
    assert df.iloc[0]["Suggested batch"] == 5


def test_last_6_months_and_backorder_columns():
    products, stock, engine, bom = _frames(on_hand=0, allocated=0)
    engine["last_6mo_series"] = "4  0  0  0  15  50"
    engine["unfulfilled"] = 50
    df = build_planner_table(["FIN-1"], products, stock, engine, bom, weeks_cover=8.0)
    assert df.iloc[0]["Last 6 months"] == "4  0  0  0  15  50"
    assert df.iloc[0]["Backorder"] == 50
