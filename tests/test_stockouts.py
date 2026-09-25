from datetime import date

import pandas as pd

from engine import stockouts as so


def _events():
    return pd.DataFrame([
        {"sku": "A", "out_date": "2026-01-10", "back_date": "2026-01-20"},
        {"sku": "A", "out_date": "2026-09-20", "back_date": None},
        {"sku": "A", "out_date": "2024-01-01", "back_date": "2024-02-01"},
        {"sku": "K", "out_date": "2026-01-01", "back_date": None},
        {"sku": "D", "out_date": "2026-03-01", "back_date": "2026-04-01"},
    ])


def test_episodes_from_hist_transitions_only():
    hist = [["2025-01-01T20:00:00-05:00", 1], ["2025-01-02T20:00:00-05:00", 1],
            ["2025-01-05T20:00:00-05:00", 0], ["2025-02-01T20:00:00-05:00", 1]]
    assert so.episodes_from_hist(hist) == [
        (date(2025, 1, 1), date(2025, 1, 5)), (date(2025, 2, 1), None)]


def test_eligible_excludes_dropship_and_made_to_order():
    eng = pd.DataFrame({"SKU": ["A", "K", "F", "D"],
                        "BillOfMaterial": [False, True, True, False],
                        "goal_units": [5, 0, 3, 2]})
    assert so.eligible_skus(eng, {"D"}) == {"A", "F"}


def test_orders_hit_strict_and_summary():
    def line(sid, sku, d, status="COMPLETED"):
        return {"SaleID": sid, "SKU": sku, "OrderDate": d, "Status": status,
                "SaleType": "Simple Sale"}
    sl = pd.DataFrame([
        line("1", "A", "2026-01-10"),              # same day it went out
        line("2", "A", "2026-01-15"),              # hit
        line("3", "A", "2026-01-16", "CREDITED"),  # not an order
        line("4", "A", "2026-09-22", "BACKORDERED"),  # hit, still out
        line("5", "D", "2026-03-10"),              # dropship, not eligible
        line("6", "A", "2026-01-20"),              # back in that day
    ])
    hits = so.orders_hit(sl, _events(), {"A"})
    assert sorted(hits["SaleID"]) == ["2", "4"]
    sm = so.summarise_12mo(_events(), today=date(2026, 9, 25), hits=hits,
                           eligible={"A"}).set_index("SKU")
    assert list(sm.index) == ["A"]
    assert sm.loc["A", "stockouts_12mo"] == 2
    assert sm.loc["A", "days_out_12mo"] == 15
    assert sm.loc["A", "stockouts_label"] == "2 (15d) · 2 orders · out now"
    m = so.monthly_order_hits(hits, sl, [pd.Period("2026-01"),
                                         pd.Period("2026-09")])
    assert m[pd.Period("2026-01")] == (1, 3)  # credit excluded
    assert m[pd.Period("2026-09")] == (1, 1)


def test_attach_blank_when_no_data():
    out = so.attach(pd.DataFrame({"SKU": ["X"]}), pd.DataFrame())
    assert out["Stock-outs 12 mo"].tolist() == [""]
