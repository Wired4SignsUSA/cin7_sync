"""sync_assemblies reuses cached finishedGoods details (2026-09-24)."""
import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import cin7_sync


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


TASKS = [
    {"TaskID": "t-new", "AssemblyNumber": "FG-1", "ProductCode": "P1",
     "ProductName": "P1", "Quantity": 2, "Date": _iso(3), "Status": "COMPLETED"},
    {"TaskID": "t-old", "AssemblyNumber": "FG-2", "ProductCode": "P2",
     "ProductName": "P2", "Quantity": 1, "Date": _iso(100), "Status": "COMPLETED"},
]
DETAILS = {
    "t-new": {"CompletionDate": _iso(3), "Status": "COMPLETED",
              "PickLines": [{"ProductCode": "C1", "Quantity": 4, "Name": "c1"}]},
    "t-old": {"CompletionDate": _iso(100), "Status": "COMPLETED",
              "PickLines": [{"ProductCode": "C2", "Quantity": 1, "Name": "c2"}]},
}


class FakeClient:
    def __init__(self, tasks):
        self.tasks = tasks
        self.detail_calls = []

    def paginate(self, path, result_key=None, params=None):
        return iter([dict(t) for t in self.tasks])

    def get(self, path, params=None):
        self.detail_calls.append(params["TaskID"])
        return DETAILS[params["TaskID"]]


class AssemblyDetailCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(cin7_sync, "OUTPUT_DIR", self.out),
            mock.patch.dict(os.environ, {"CIN7_ASSEMBLY_DETAIL_CACHE": "1"}),
        ]
        for p in self.patches:
            p.start()
        # Postgres sharing is best-effort; keep it out of unit tests.
        self.db = mock.patch.dict("sys.modules", {"db": mock.MagicMock()})
        self.db.start()

    def tearDown(self):
        self.db.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _rows(self):
        f = sorted(self.out.glob("assemblies_last_30d_*.csv"),
                   key=lambda p: p.stat().st_mtime)[-1]
        with f.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_second_run_hits_cache_and_output_matches(self):
        c1 = FakeClient(TASKS)
        cin7_sync.sync_assemblies(c1, 30)
        self.assertEqual(sorted(c1.detail_calls), ["t-new", "t-old"])
        first = self._rows()
        self.assertEqual([r["ComponentSKU"] for r in first], ["C1"])

        c2 = FakeClient(TASKS)
        with mock.patch("time.sleep"):
            cin7_sync.sync_assemblies(c2, 30)
        self.assertEqual(c2.detail_calls, [])
        second = self._rows()
        self.assertEqual([(r["TaskID"], r["ComponentSKU"], r["Quantity"])
                          for r in second],
                         [(r["TaskID"], r["ComponentSKU"], r["Quantity"])
                          for r in first])

    def test_edited_task_is_refetched(self):
        cin7_sync.sync_assemblies(FakeClient(TASKS), 30)
        edited = [dict(TASKS[0], Quantity=5), TASKS[1]]
        c2 = FakeClient(edited)
        cin7_sync.sync_assemblies(c2, 30)
        self.assertEqual(c2.detail_calls, ["t-new"])

    def test_ttl_expiry_refetches_in_window_only(self):
        cin7_sync.sync_assemblies(FakeClient(TASKS), 30)
        c2 = FakeClient(TASKS)
        with mock.patch.dict(os.environ, {"CIN7_ASSEMBLY_CACHE_TTL_DAYS": "0"}):
            cin7_sync.sync_assemblies(c2, 30)
        self.assertEqual(c2.detail_calls, ["t-new"])

    def test_disabled(self):
        cin7_sync.sync_assemblies(FakeClient(TASKS), 30)
        c2 = FakeClient(TASKS)
        with mock.patch.dict(os.environ, {"CIN7_ASSEMBLY_DETAIL_CACHE": "0"}):
            cin7_sync.sync_assemblies(c2, 30)
        self.assertEqual(sorted(c2.detail_calls), ["t-new", "t-old"])


if __name__ == "__main__":
    unittest.main()
