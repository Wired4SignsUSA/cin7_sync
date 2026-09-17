"""stock_locator_audit._load_stock_bins must return one row per SKU
even when the CIN7 export has a row per SKU x bin/location."""
import pandas as pd

import stock_locator_audit as sla


def test_load_stock_bins_collapses_duplicate_skus(tmp_path, monkeypatch):
    csv = tmp_path / "stock_on_hand_test.csv"
    pd.DataFrame({
        "SKU": ["a1", "A1", "B2"],
        "Name": ["Prod A", "Prod A", "Prod B"],
        "Bin": ["", "R1-S2", "R3"],
        "Location": ["Main", "Main", "Main"],
        "OnHand": [2, 3, 5],
    }).to_csv(csv, index=False)
    monkeypatch.setattr(sla, "OUTPUT_DIR", tmp_path)
    out = sla._load_stock_bins()
    assert list(out["SKU"]) == ["A1", "B2"]
    a1 = out.set_index("SKU").loc["A1"]
    assert a1["Bin"] == "R1-S2"
    assert a1["OnHand"] == 5
    # the audit's lookup must not raise on the deduped frame
    out.set_index("SKU").to_dict("index")
