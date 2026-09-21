"""Finance › Cashflow page (read-only mirror of the W4S Cashflow app)."""
from __future__ import annotations

import os
from unittest import mock

import pytest
import requests

from app_pages import cashflow_viktor as cv


def test_forecast_frame_columns_and_flags():
    weeks = [{
        "weekStart": "2026-09-21", "isActual": True, "opening": 100.0,
        "salesIn": 50.0, "otherIn": 0.0, "bankOut": 20.0, "committedOut": 10.0,
        "notInvoicedOut": 5.0, "plannedOut": 0.0, "totalOut": 35.0,
        "closing": 115.0, "belowFloor": True, "billCount": 3,
    }]
    df = cv.forecast_frame(weeks)
    assert list(df["Week"]) == ["2026-09-21"]
    assert df.loc[0, "Closing"] == 115.0
    assert df.loc[0, "Below floor"] == "⚠️"
    assert df.loc[0, "Bills"] == 3


def test_money_formatting():
    assert cv._money(1234.6) == "$1,235"
    assert cv._money(-61799) == "-$61,799"
    assert cv._money(None) == "—"


def test_legacy_flag_env():
    with mock.patch.dict(os.environ, {"CASHFLOW_LEGACY_PAGE": "1"}):
        assert cv.legacy_page_enabled()
    with mock.patch.dict(os.environ, {"CASHFLOW_LEGACY_PAGE": ""}):
        assert not cv.legacy_page_enabled()


def test_fetch_summary_sends_bearer_and_raises_on_401():
    class Resp:
        status_code = 401
        def raise_for_status(self):
            raise requests.HTTPError("401")
    with mock.patch.object(cv.requests, "get", return_value=Resp()) as g:
        with pytest.raises(requests.HTTPError):
            cv.fetch_summary("https://x.convex.site/", "tok")
    assert g.call_args.args[0] == "https://x.convex.site/api/summary"
    assert g.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_fmt_ts_converts_to_eastern():
    assert cv._fmt_ts("2026-09-21T16:35:00Z") == "Mon 21 Sep 12:35 ET"
    assert cv._fmt_ts("2026-09-21T09:02:02.486970-04:00") == "Mon 21 Sep 09:02 ET"
