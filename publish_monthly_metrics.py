"""publish_monthly_metrics.py
============================
Headless publisher for the dashboard's Monthly Metrics table.

Computes the exact table the "Monthly Metrics" page shows (via
engine/monthly_metrics.py — the page renders from the same module) and
writes it to Postgres `dataset_files` so external consumers can read it
without a browser session:

    key                             content
    ------------------------------  ------------------------------------
    monthly_metrics.csv             Section, Metric, Format, <YYYY-MM>..., YTD, Avg
    monthly_metrics_for_chatgpt.md  the page's "LLM-ready markdown" export
    monthly_metrics.json            {generated_at, months, channel, rows:[...]}

James (2026-09-10): this is the source of truth for Viktor's monthly
financial report. Runs from daily_sync.sh after the CIN7 sync and
dataset_mirror publish, so the nightly numbers land before US morning.
Also runnable by hand:

    python publish_monthly_metrics.py            # publish
    python publish_monthly_metrics.py --dry-run  # compute + print, no DB write
    python publish_monthly_metrics.py --months 24

Inputs mirror the page exactly: sale_lines / purchase_lines / shopify
orders / sales headers from the OUTPUT_DIR CSV union loaders,
stock value from CIN7 StockOnHand, and the db tables the page reads
(shopify_monthly_discounts, qbo_monthly_pl, stock_goal_snapshots,
dormancy warnings, slow_mover_value_snapshots). Current-month slow-stock
value comes from engine_output.csv via engine.value_snapshots.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from data_paths import OUTPUT_DIR  # noqa: E402
from engine.monthly_metrics import (  # noqa: E402
    MonthlyMetricsInputs, build_table, compute, export_table, llm_markdown)

KEY_CSV = "monthly_metrics.csv"
KEY_MD = "monthly_metrics_for_chatgpt.md"
KEY_JSON = "monthly_metrics.json"
PUBLISHER = "publish_monthly_metrics"


def _emit(msg: str) -> None:
    sys.stderr.write(f"[publish_monthly_metrics] {msg}\n")
    sys.stderr.flush()


def _load_sales_headers(output_dir: Path, filter_fn) -> pd.DataFrame:
    """Union of sales_last_*d_*.csv (widest window + newer narrower
    ones), deduped on SaleID — same as app.py's _load_longest_sales."""
    files = []
    for p in output_dir.glob("sales_last_*d_*.csv"):
        m = re.match(r"sales_last_(\d+)d_", p.name)
        if m:
            files.append((int(m.group(1)), p.stat().st_mtime, p))
    if not files:
        return pd.DataFrame()
    files.sort(key=lambda x: (-x[0], -x[1]))
    base_mtime = files[0][1]
    frames = []
    for i, (_d, mtime, p) in enumerate(files):
        if i and mtime <= base_mtime:
            continue
        try:
            frames.append(pd.read_csv(
                p, low_memory=False,
                usecols=lambda c: c in ("SaleID", "SalesRepresentative",
                                        "Customer", "CustomerID")))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return pd.DataFrame()
    base = pd.concat(frames, ignore_index=True)
    if "SaleID" in base.columns:
        base = base.drop_duplicates(subset=["SaleID"], keep="last")
    try:
        base = filter_fn(base)
    except Exception:  # noqa: BLE001
        pass
    return base.reset_index(drop=True)


def _headline_stock_value(stock: pd.DataFrame, products: pd.DataFrame) -> float:
    """Same as app.py's _headline_stock_value: sum CIN7 StockOnHand."""
    if stock is None or stock.empty:
        return 0.0
    if "StockOnHand" in stock.columns:
        return float(pd.to_numeric(stock["StockOnHand"], errors="coerce").fillna(0).sum())
    if products is None or products.empty or "OnHand" not in stock.columns:
        return 0.0
    cost = products.set_index("SKU")["AverageCost"].to_dict()
    on_hand = pd.to_numeric(stock["OnHand"], errors="coerce").fillna(0)
    return float(sum(q * float(cost.get(str(s), 0) or 0)
                     for q, s in zip(on_hand, stock["SKU"].astype(str))))


def _live_slow_stock_value(output_dir: Path) -> Optional[float]:
    p = output_dir / "engine_output.csv"
    if not p.exists():
        return None
    try:
        from engine.value_snapshots import slow_stock_totals
        return float(slow_stock_totals(pd.read_csv(p, low_memory=False))["value"])
    except Exception as exc:  # noqa: BLE001
        _emit(f"live slow-stock value unavailable: {exc!r}")
        return None


def _safe(fn, default):
    try:
        return fn() or default
    except Exception as exc:  # noqa: BLE001
        _emit(f"{getattr(fn, '__name__', 'db call')} failed: {exc!r}")
        return default


def gather_inputs(lookback: int = 14, channel: str = "(All channels)"
                  ) -> MonthlyMetricsInputs:
    import db
    from sales_exclusions import filter_excluded_sales_customers
    from monthly_metrics_report import _load_cin7_data

    data = _load_cin7_data()
    mappings = _safe(db.get_qbo_account_mappings, {})
    qb_by_month = (_safe(lambda: db.qbo_monthly_pl_summary_by_category(mappings), {})
                   if mappings else {})
    return MonthlyMetricsInputs(
        sale_lines=data["sale_lines"],
        purchase_lines=data["purchase_lines"],
        shopify_orders=data["shopify_orders"],
        sales_headers=_load_sales_headers(OUTPUT_DIR, filter_excluded_sales_customers),
        inv_value_now=_headline_stock_value(data["stock"], data["products"]),
        shopify_discounts=_safe(db.all_shopify_monthly_discounts, {}),
        qb_by_month=qb_by_month,
        stock_goal_rows=_safe(lambda: db.list_stock_goal_snapshots(limit=800), []),
        dormancy_warnings=_safe(db.get_dormancy_warnings, {}),
        slow_mover_snapshots=_safe(lambda: db.list_slow_mover_snapshots(limit=365 * 2), []),
        live_slow_stock_value=_live_slow_stock_value(OUTPUT_DIR),
        channel=channel,
        lookback_months=lookback,
    )


def build_artifacts(inp: MonthlyMetricsInputs) -> Dict[str, Any]:
    res = compute(inp)
    table = build_table(res.rows, res.month_labels, res.current_month, show_ytd=True)
    csv_df = export_table(res.rows, table)
    md = llm_markdown(res.rows, table, res.month_labels, inp.channel, show_ytd=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "channel": inp.channel,
        "months": res.month_labels,
        "rows": [
            {"section": r["Section"], "metric": r["Metric"], "format": r["Format"],
             "values": {lbl: (None if v is None or pd.isna(v) else float(v))
                        for lbl, v in zip(res.month_labels, r["Values"])},
             "ytd": (None if pd.isna(table.at[i, "YTD"]) else float(table.at[i, "YTD"])),
             "avg": (None if pd.isna(table.at[i, "Avg"]) else float(table.at[i, "Avg"]))}
            for i, r in enumerate(res.rows)
        ],
    }
    return {"table": csv_df, "markdown": md, "json": payload, "result": res}


def _put(db, key: str, filename: str, raw: bytes) -> None:
    import gzip
    gz = gzip.compress(raw, compresslevel=6)
    db.put_dataset_file(
        key, filename=filename, mtime=time.time(), size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(), payload=gz, publisher=PUBLISHER)


def publish(artifacts: Dict[str, Any]) -> None:
    import db
    buf = io.StringIO()
    artifacts["table"].to_csv(buf, index=False)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    _put(db, KEY_CSV, f"monthly_metrics_{stamp}.csv", buf.getvalue().encode("utf-8"))
    _put(db, KEY_MD, f"monthly_metrics_for_chatgpt_{stamp}.md",
         artifacts["markdown"].encode("utf-8"))
    _put(db, KEY_JSON, f"monthly_metrics_{stamp}.json",
         json.dumps(artifacts["json"], indent=1).encode("utf-8"))
    # Local copy too, so the dashboard disk has the latest export.
    (OUTPUT_DIR / "monthly_metrics_latest.csv").write_text(buf.getvalue())
    (OUTPUT_DIR / "monthly_metrics_for_chatgpt_latest.md").write_text(artifacts["markdown"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=14)
    ap.add_argument("--channel", default="(All channels)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    inp = gather_inputs(args.months, args.channel)
    art = build_artifacts(inp)
    res = art["result"]
    _emit(f"computed {len(res.rows)} rows × {len(res.month_labels)} months "
          f"({res.month_labels[0]}..{res.month_labels[-1]}) in {time.time() - t0:.1f}s")
    if args.dry_run:
        print(art["markdown"])
        return 0
    publish(art)
    _emit(f"published {KEY_CSV}, {KEY_MD}, {KEY_JSON} to dataset_files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
