import math
import unittest
from pathlib import Path

import pandas as pd

from app_config import PAGE_DESCRIPTIONS, PAGE_GROUPS
from engine.buying_priority import build_priority_rows, build_vendor_summary


def _row(sku, supplier, **kw):
    base = {
        "SKU": sku, "Supplier": supplier, "OnHand": 10, "Allocated": 0,
        "OnOrder": 0, "reorder_qty": 0, "Status": "🟢 On target",
        "ABC": "C", "avg_daily": 1.0, "AverageCost": 10.0,
        "unit_cost_for_goal": 10.0, "lead_time_days": 30,
        "is_non_master_tube": False, "is_bulk_master": False,
        "bulk_length_m": 0,
    }
    base.update(kw)
    return base


class BuyingPriorityTests(unittest.TestCase):
    def _df(self):
        return pd.DataFrame([
            _row("OK", "V1"),
            _row("SOON-A", "V1", reorder_qty=5, Status="🟠 Reorder soon",
                 ABC="A", OnHand=20),
            _row("NOW-C", "V2", reorder_qty=4, Status="🔴 Reorder now",
                 OnHand=2),
            _row("NOW-A", "V2", reorder_qty=4, Status="🔴 Reorder now",
                 ABC="A", OnHand=5),
            _row("BO-COVERED", "V3", OnHand=0, Allocated=5, OnOrder=5),
            _row("BO-OPEN", "V3", OnHand=0, Allocated=8, OnOrder=3,
                 reorder_qty=2, Status="🔴 Reorder now"),
            _row("BO-BIG", "V4", OnHand=0, Allocated=2,
                 unit_cost_for_goal=500.0),
            _row("CUT", "V4", OnHand=0, Allocated=9,
                 is_non_master_tube=True),
            _row("DISC", "V1", reorder_qty=3, Status="🚫 Discontinued"),
        ])

    def test_backorders_first_then_class_then_cover(self):
        rows = build_priority_rows(self._df())
        self.assertEqual(
            list(rows["SKU"]),
            ["BO-BIG", "BO-OPEN", "NOW-A", "NOW-C", "SOON-A"])
        self.assertEqual(list(rows["priority_rank"]), [1, 2, 3, 4, 5])

    def test_uncovered_backorder_sets_suggested_floor(self):
        rows = build_priority_rows(self._df()).set_index("SKU")
        self.assertAlmostEqual(rows.loc["BO-OPEN", "backorder_uncovered"], 5)
        self.assertAlmostEqual(rows.loc["BO-OPEN", "suggested_qty"], 5)
        self.assertNotIn("BO-COVERED", rows.index)
        self.assertNotIn("CUT", rows.index)
        self.assertNotIn("DISC", rows.index)

    def test_cover_days(self):
        rows = build_priority_rows(self._df()).set_index("SKU")
        self.assertAlmostEqual(rows.loc["NOW-C", "cover_days"], 2)
        df = pd.DataFrame([_row("X", "V", avg_daily=0, reorder_qty=1,
                                Status="🟠 Reorder soon")])
        self.assertTrue(math.isinf(build_priority_rows(df)["cover_days"][0]))

    def test_bulk_residue_backorder_ignored(self):
        df = pd.DataFrame([_row("ROLL", "V", OnHand=0, Allocated=0.01,
                                is_bulk_master=True, bulk_length_m=100)])
        self.assertTrue(build_priority_rows(df).empty)

    def test_vendor_summary_order(self):
        vendors = build_vendor_summary(build_priority_rows(self._df()))
        self.assertEqual(list(vendors["Supplier"]), ["V4", "V3", "V2", "V1"])
        v2 = vendors.set_index("Supplier").loc["V2"]
        self.assertEqual(v2["now_skus"], 2)
        self.assertEqual(v2["top_skus"], ["NOW-A", "NOW-C"])

    def test_empty(self):
        self.assertTrue(build_priority_rows(pd.DataFrame()).empty)
        self.assertTrue(build_vendor_summary(pd.DataFrame()).empty)

    def test_page_registered_and_wired(self):
        self.assertEqual(PAGE_GROUPS["Buying"][0], "Buying Priority")
        self.assertIn("Buying Priority", PAGE_DESCRIPTIONS)
        script = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8")
        self.assertIn('elif page == "Buying Priority":', script)
        self.assertIn('st.session_state.get("_nav_request")', script)


if __name__ == "__main__":
    unittest.main()
