import gzip
import json

import pandas as pd

from app_pages.mobile import is_mobile_user_agent
from engine import mobile_summary as ms


def _engine():
    return pd.DataFrame({
        "SKU": ["LED-C7020001-2", "LED-C7020000-2", "ABC-1", "LED-C70"],
        "Name": ["Begton12 White 2m", "Begton12 Raw 2m", "Other thing",
                 "Short"],
        "OnHand": [10, 263, 0, 5],
        "Allocated": [12, 0, 0, 0],
        "OnOrder": [20, 0, 0, 0],
        "avg_daily": [0.5, 0, 0, 0.1],
        "goal_units": [30, float("nan"), 0, 1],
        "reorder_qty": [5, 0, 0, 0],
        "Supplier": ["Topmet", "Topmet", "X", "Y"],
    })


def test_search_ranks_exact_then_prefix_then_words():
    df = _engine()
    hits = ms.search_skus(df, "led-c70")
    assert list(hits["SKU"])[0] == "LED-C70"
    assert set(hits["SKU"]) == {"LED-C70", "LED-C7020001-2",
                                "LED-C7020000-2"}
    words = ms.search_skus(df, "begton white")
    assert list(words["SKU"]) == ["LED-C7020001-2"]
    assert ms.search_skus(df, "").empty
    assert ms.search_skus(df, "nomatch").empty


def test_sku_card_maths():
    c = ms.sku_card(_engine().iloc[0])
    assert c["available"] == -2
    assert c["backorder"] == 2
    assert c["cover_days"] == 36  # (-2 + 20) / 0.5
    assert c["goal_units"] == 30
    assert ms.sku_card(_engine().iloc[1])["goal_units"] is None
    assert ms.sku_card(_engine().iloc[1])["cover_days"] is None


def test_draft_pos_dedup_and_filter():
    old = pd.DataFrame({
        "ID": ["a", "b", "c"], "OrderNumber": ["PO-1", "PO-2", "PO-3"],
        "Status": ["ORDERING", "ORDERING", "ORDERED"],
        "OrderStatus": ["DRAFT", "DRAFT", "AUTHORISED"],
        "Type": ["Simple Purchase", "Advanced Purchase", ""],
        "OrderDate": ["2026-09-01", "2026-09-10", "2026-09-02"],
        "LastUpdatedDate": ["2026-09-01", "2026-09-10", "2026-09-02"],
    })
    new = pd.DataFrame({  # PO-1 authorised since the 30d export
        "ID": ["a"], "OrderNumber": ["PO-1"], "Status": ["ORDERED"],
        "OrderStatus": ["AUTHORISED"], "Type": ["Simple Purchase"],
        "OrderDate": ["2026-09-01"], "LastUpdatedDate": ["2026-09-20"],
    })
    heads = ms.latest_purchase_headers([old, new, pd.DataFrame()])
    drafts = ms.draft_purchase_orders(heads)
    assert list(drafts["OrderNumber"]) == ["PO-2"]
    assert drafts.iloc[0]["url"].endswith("/PurchaseAdvanced#b")
    assert ms.cin7_purchase_url("x", "Simple Purchase").endswith(
        "/Purchase#x")


def test_headline_tiles_and_payload():
    mm = {"months": ["2026-08", "2026-09"], "channel": "(All channels)",
          "rows": [
              {"section": "1. Sales Overview [App]", "metric": "Sales $",
               "format": "money",
               "values": {"2026-08": 612496.22, "2026-09": 496273.69}},
              {"section": "7. Cost [QuickBooks]", "metric": "GP %",
               "format": "pct", "values": {"2026-09": 1.0}},
              {"section": "1. Sales Overview [App]", "metric": "GP %",
               "format": "pct",
               "values": {"2026-08": 61.7, "2026-09": 62.1}},
          ]}
    raw = gzip.compress(json.dumps(mm).encode())
    tiles = ms.headline_tiles(ms.decode_payload(raw))
    by = {t["label"]: t for t in tiles}
    assert by["Sales"]["current"] == 496273.69
    assert by["GP %"]["current"] == 62.1  # section 1, not QuickBooks
    assert ms.fmt_value(496273.69, "money") == "$496k"
    assert ms.fmt_value(62.14, "pct") == "62.1%"
    assert ms.fmt_value(-1_250_000, "money") == "-$1.25M"
    assert ms.headline_tiles(None) == []
    assert ms.decode_payload(b"not json") is None


def test_mobile_user_agent():
    assert is_mobile_user_agent(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")
    assert is_mobile_user_agent("Mozilla/5.0 (Linux; Android 14; Pixel 8)")
    assert not is_mobile_user_agent(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
    assert not is_mobile_user_agent("Mozilla/5.0 (iPad; CPU OS 17_0)")
    assert not is_mobile_user_agent("")
