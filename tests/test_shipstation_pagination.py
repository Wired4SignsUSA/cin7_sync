"""v2 /shipments pagination: stay under ShipEngine's offset cap by
splitting the date window, filter statuses, de-duplicate."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import shipstation_sync as ss

FMT = "%Y-%m-%d %H:%M:%S"


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.ok = True
        self.headers = {}
        self.text = ""

    def json(self):
        return self._p


class FakeSession:
    """Shipments spread one per minute; enforces the offset cap."""

    def __init__(self, shipments, cap=ss.V2_MAX_OFFSET_ROWS + 1000):
        self.shipments = shipments
        self.cap = cap
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        start = datetime.strptime(params["created_at_start"], FMT)
        end = datetime.strptime(params["created_at_end"], FMT)
        st = params.get("shipment_status")
        rows = [s for s in self.shipments
                if start <= s["_ts"] <= end
                and (st is None or s["shipment_status"] == st)]
        size = params["page_size"]
        page = params["page"]
        if page * size > self.cap:
            raise AssertionError("offset cap exceeded")
        chunk = rows[(page - 1) * size: page * size]
        pages = max(1, -(-len(rows) // size))
        return _Resp({"shipments": chunk, "total": len(rows),
                      "page": page, "pages": pages})


def _make(n, status="label_purchased", start=datetime(2026, 9, 1)):
    return [{"shipment_id": f"se-{status}-{i}", "shipment_status": status,
             "_ts": start + timedelta(minutes=i)} for i in range(n)]


class V2PaginationTest(unittest.TestCase):
    def setUp(self):
        self._p = [mock.patch.object(ss, "_respect_rate_limits"),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for p in self._p:
            p.start()
        os.environ.pop("SHIPSTATION_V2_STATUSES", None)

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_splits_window_over_cap(self):
        sess = FakeSession(_make(20000))
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 30, tzinfo=timezone.utc)
        out = list(ss._iter_v2_shipments(sess, since, end))
        self.assertEqual(len(out), 20000)
        self.assertEqual(len({s["shipment_id"] for s in out}), 20000)

    def test_default_statuses_skip_pending(self):
        data = (_make(50) + _make(30, "pending") + _make(5, "cancelled"))
        sess = FakeSession(data)
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 30, tzinfo=timezone.utc)
        out = list(ss._iter_v2_shipments(sess, since, end))
        self.assertEqual(len(out), 55)
        self.assertTrue(all(c.get("shipment_status") in
                            ("label_purchased", "cancelled")
                            for c in sess.calls))

    def test_all_statuses_override(self):
        os.environ["SHIPSTATION_V2_STATUSES"] = "all"
        data = _make(50) + _make(30, "pending")
        sess = FakeSession(data)
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(len(list(ss._iter_v2_shipments(sess, since, end))),
                         80)

    def test_dedupes_on_window_edges(self):
        # Two rows exactly on a split boundary are returned by both
        # halves (inclusive ends) — must appear once.
        rows = _make(ss.V2_MAX_OFFSET_ROWS + 10)
        sess = FakeSession(rows)
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = rows[-1]["_ts"].replace(tzinfo=timezone.utc)
        out = list(ss._iter_v2_shipments(sess, since, end))
        self.assertEqual(len(out), len(rows))


if __name__ == "__main__":
    unittest.main()
