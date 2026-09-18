"""Tests for finishing_oneoff.py — request parsing, feet-per-unit, the
plan → bom_parents bridge and the DB request table."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import finishing_oneoff as fo  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_andrew_style(self):
        r = fo.parse_request("@Jamie Powder coat LED-C7020001-2 Begton12 white x 20 from raw stock")
        self.assertEqual(r, {"finished_sku": "LED-C7020001-2", "raw_sku": None, "qty": 20,
                             "colour": "white", "process": "POWDERCOAT"})

    def test_explicit_raw_and_anodize(self):
        r = fo.parse_request("anodize 50 pcs LED-89030021-2 black from LED-89030000-2")
        self.assertEqual(r["raw_sku"], "LED-89030000-2")
        self.assertEqual(r["finished_sku"], "LED-89030021-2")
        self.assertEqual((r["qty"], r["colour"], r["process"]), (50, "black", "ANODIZING"))

    def test_not_a_request(self):
        self.assertIsNone(fo.parse_request("done 35"))
        self.assertIsNone(fo.parse_request("can we powder coat the Begton12 in white?"))
        self.assertIsNone(fo.parse_request("LED-C7020001-2 = 0"))
        self.assertIsNone(fo.parse_request("powder coat LED-C7020001-2 white"))  # no qty

    def test_feet(self):
        self.assertEqual(fo.feet_per_unit('Model Begton12 (White, 2m (78"))'), 7.0)
        self.assertEqual(fo.feet_per_unit("SLW10 (2390mm (94\"), Silver)"), 8.0)
        self.assertEqual(fo.feet_per_unit("(White, 1m (39\"))"), 4.0)
        self.assertEqual(fo.feet_per_unit("(Black, 609mm (24\"))"), 2.0)
        self.assertIsNone(fo.feet_per_unit("Post plate"))


class PlanTests(unittest.TestCase):
    PLAN = {"finished_sku": "LED-C7020001-2", "qty": 20.0, "raw_sku": "LED-C7020000-2",
            "raw_name": "raw", "service_sku": "OSC-POWDERCOAT-WH-SML-FT",
            "service_name": "svc", "per_unit": 7.0, "existing_bom": False,
            "name": "Begton12 white", "notes": [], "errors": []}

    def test_bom_parents_bridge(self):
        bp = fo.bom_parents_for(self.PLAN)
        comps = bp["LED-C7020001-2"]
        self.assertEqual([c["ComponentSKU"] for c in comps],
                         ["LED-C7020000-2", "OSC-POWDERCOAT-WH-SML-FT"])
        self.assertEqual(comps[1]["Quantity"], 7.0)
        import fablab_assemblies as fa
        totals, no_svc = fa.service_totals({"LED-C7020001-2": 20}, bp, fo.FINISHING)
        self.assertEqual(totals, {"OSC-POWDERCOAT-WH-SML-FT": 140.0})
        self.assertEqual(no_svc, [])

    def test_plan_text(self):
        t = fo.plan_text(self.PLAN)
        self.assertIn("140 ft", t)
        self.assertIn("BOM will be added", t)
        self.assertIn("approve", t)
        bad = dict(self.PLAN, errors=["nope"])
        self.assertTrue(fo.plan_text(bad).startswith(":x:"))


class DbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._p = patch.object(db, "DB_PATH", str(Path(self._tmp.name) / "t.db"))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_request_lifecycle(self):
        db.create_finishing_oneoff_request("C1", "1.0", "andrew", "powder coat X x1",
                                           {"finished_sku": "X"}, "proposed")
        rows = db.list_finishing_oneoff_requests("C1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["plan_json"])["finished_sku"], "X")
        db.update_finishing_oneoff_request(rows[0]["id"], status="placed",
                                           approved_by="james", draft_id=5, po_number="PO-1")
        self.assertEqual(db.list_finishing_oneoff_requests("C1"), [])
        allrows = db.list_finishing_oneoff_requests("C1", open_only=False)
        self.assertEqual((allrows[0]["status"], allrows[0]["po_number"]), ("placed", "PO-1"))


if __name__ == "__main__":
    unittest.main()
