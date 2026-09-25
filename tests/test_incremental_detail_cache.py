"""Incremental detail cache for sale/purchase lines and BOMs (2026-09-25)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cin7_sync


SALES = [
    {"SaleID": "s1", "OrderNumber": "SO-1", "Status": "ORDERED",
     "Updated": "2026-09-20T10:00:00Z", "Type": "Simple Sale"},
    {"SaleID": "s2", "OrderNumber": "SO-2", "Status": "ORDERED",
     "Updated": "2026-09-21T10:00:00Z", "Type": "Simple Sale"},
]


def _sale_detail(sid, qty=1):
    return {"ID": sid, "Order": {"Lines": [
        {"ProductID": "p1", "SKU": "SKU-1", "Name": "n", "Quantity": qty,
         "Price": 10, "Total": 10 * qty}]}}


class FakeSaleClient:
    rate_seconds = 0.0

    def __init__(self, headers, qty=None):
        self.headers = headers
        self.qty = qty or {}
        self.calls = []

    def paginate(self, path, result_key=None, params=None):
        return iter([dict(h) for h in self.headers])

    def get(self, path, params=None):
        self.calls.append(params["ID"])
        return _sale_detail(params["ID"], self.qty.get(params["ID"], 1))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.written = {}

        def fake_write(name, rows):
            self.written[name] = [dict(r) for r in rows]
            return self.out / f"{name}.csv"

        self.patches = [
            mock.patch.object(cin7_sync, "OUTPUT_DIR", self.out),
            mock.patch.object(cin7_sync, "write_outputs", fake_write),
            mock.patch.dict(os.environ, {"CIN7_DETAIL_CACHE": "1",
                                         "CIN7_FULL_REFRESH_WEEKDAY": "-1",
                                         "CIN7_FULL_REFRESH": "0"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class SaleLinesCacheTests(Base):
    def test_unchanged_orders_reuse_cache_and_output_matches(self):
        c1 = FakeSaleClient(SALES)
        cin7_sync.sync_salelines(c1, 30)
        self.assertEqual(sorted(c1.calls), ["s1", "s2"])
        first = self.written["sale_lines_last_30d"]
        self.assertTrue(first)

        c2 = FakeSaleClient(SALES)
        cin7_sync.sync_salelines(c2, 30)
        self.assertEqual(c2.calls, [])
        self.assertEqual(self.written["sale_lines_last_30d"], first)

    def test_changed_order_is_refetched(self):
        cin7_sync.sync_salelines(FakeSaleClient(SALES), 30)
        changed = [dict(SALES[0]), dict(SALES[1], Updated="2026-09-24T09:00:00Z")]
        c2 = FakeSaleClient(changed, qty={"s2": 5})
        cin7_sync.sync_salelines(c2, 30)
        self.assertEqual(c2.calls, ["s2"])
        qtys = {str(r.get("SaleID")): r.get("Quantity")
                for r in self.written["sale_lines_last_30d"]}
        self.assertEqual(str(qtys["s2"]), "5")

    def test_weekly_full_refresh_ignores_cache(self):
        cin7_sync.sync_salelines(FakeSaleClient(SALES), 30)
        with mock.patch.dict(os.environ, {"CIN7_FULL_REFRESH": "1"}):
            c2 = FakeSaleClient(SALES)
            cin7_sync.sync_salelines(c2, 30)
        self.assertEqual(sorted(c2.calls), ["s1", "s2"])

    def test_disabled_cache_always_fetches(self):
        with mock.patch.dict(os.environ, {"CIN7_DETAIL_CACHE": "0"}):
            cin7_sync.sync_salelines(FakeSaleClient(SALES), 30)
            c2 = FakeSaleClient(SALES)
            cin7_sync.sync_salelines(c2, 30)
        self.assertEqual(sorted(c2.calls), ["s1", "s2"])
        self.assertFalse((self.out / ".sale_detail_cache.json").exists())

    def test_extractor_change_invalidates_cache(self):
        cin7_sync.sync_salelines(FakeSaleClient(SALES), 30)
        with mock.patch.object(cin7_sync, "_code_version", return_value="other"):
            c2 = FakeSaleClient(SALES)
            cin7_sync.sync_salelines(c2, 30)
        self.assertEqual(sorted(c2.calls), ["s1", "s2"])

    def test_save_merges_with_concurrent_writer(self):
        a = cin7_sync._DetailCache.load("sale", "v")
        b = cin7_sync._DetailCache.load("sale", "v")
        a.put("x", "u1", [1])
        a.save()
        b.put("y", "u2", [2])
        b.save()
        data = json.loads((self.out / ".sale_detail_cache.json").read_text())
        self.assertEqual(set(data["records"]), {"x", "y"})


class BomCacheTests(Base):
    PRODUCTS = [
        {"ID": "a1", "SKU": "ASM-1", "Name": "Asm 1", "BillOfMaterial": True,
         "LastModifiedOn": "2026-09-01T00:00:00Z"},
        {"ID": "c1", "SKU": "COMP-1", "Name": "Comp one"},
    ]

    class Client:
        rate_seconds = 0.0

        def __init__(self, products):
            self.products = products
            self.calls = []

        def paginate(self, path, result_key=None, params=None):
            return iter([dict(p) for p in self.products])

        def get(self, path, params=None):
            self.calls.append(params["ID"])
            return {"Products": [{"ID": "a1", "Name": "Asm 1",
                                  "BillOfMaterialsProducts": [
                                      {"ComponentProductID": "c1",
                                       "Quantity": 2}]}]}

    def test_unchanged_bom_reuses_cache_but_picks_up_component_rename(self):
        c1 = self.Client(self.PRODUCTS)
        cin7_sync.sync_boms(c1)
        self.assertEqual(c1.calls, ["a1"])
        self.assertEqual(self.written["boms"][0]["ComponentName"], "Comp one")

        renamed = [dict(self.PRODUCTS[0]),
                   dict(self.PRODUCTS[1], Name="Comp renamed")]
        c2 = self.Client(renamed)
        cin7_sync.sync_boms(c2)
        self.assertEqual(c2.calls, [])
        row = self.written["boms"][0]
        self.assertEqual(row["ComponentSKU"], "COMP-1")
        self.assertEqual(row["ComponentName"], "Comp renamed")
        self.assertEqual(row["Quantity"], 2)

    def test_modified_bom_is_refetched(self):
        cin7_sync.sync_boms(self.Client(self.PRODUCTS))
        bumped = [dict(self.PRODUCTS[0], LastModifiedOn="2026-09-25T00:00:00Z"),
                  dict(self.PRODUCTS[1])]
        c2 = self.Client(bumped)
        cin7_sync.sync_boms(c2)
        self.assertEqual(c2.calls, ["a1"])


if __name__ == "__main__":
    unittest.main()
