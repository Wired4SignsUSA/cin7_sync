"""stockout_risk_alert.py — morning stock-out risk list (RULES 9.16).

Posts stocked SKUs that are out, or will run out before a PO could land,
with nothing on order. Maths: engine/stockout_risk.py.

Channel: SLACK_STOCKOUT_RISK_CHANNEL_ID. Unset = log the message and post
nothing (the bot must be a member of the channel).

CLI:
  python stockout_risk_alert.py run [--dryrun]
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db  # noqa: E402
from data_paths import OUTPUT_DIR  # noqa: E402
from engine import stockout_risk as sr  # noqa: E402
from engine import stockouts as so  # noqa: E402

log = logging.getLogger("stockout_risk_alert")

APP_URL = os.environ.get("APP_PUBLIC_URL",
                         "https://wired4signs-app.onrender.com")


def _latest(pattern: str) -> str | None:
    files = sorted(glob.glob(str(OUTPUT_DIR / pattern)))
    return files[-1] if files else None


def _build_skus() -> dict:
    """{sku: 'Finishing'|'Corners'} from the BOM service lines."""
    try:
        from fablab_stock_alert import _build_bom_parents
        from app_pages.fablab_work_orders import bom_service_skus
        from outsource_flows import FABLAB, FINISHING
    except Exception as exc:  # noqa: BLE001
        log.warning("build-flow imports failed: %s", exc)
        return {}
    path = _latest("boms_*.csv")
    if not path:
        return {}
    parents = _build_bom_parents(pd.read_csv(path, low_memory=False))
    out = {s: "Corners" for s in bom_service_skus(parents, FABLAB)}
    out.update({s: "Finishing" for s in bom_service_skus(parents, FINISHING)})
    return out


def build_table() -> pd.DataFrame:
    path = _latest("engine_output.csv")
    if not path:
        raise SystemExit("engine_output.csv not available on this worker")
    engine_df = pd.read_csv(path, low_memory=False)
    try:
        cfgs = db.all_supplier_configs()
    except Exception:  # noqa: BLE001
        cfgs = {}
    try:
        ip_lt = db.get_ip_lead_times()
    except Exception:  # noqa: BLE001
        ip_lt = {}
    try:
        sku_lt = {str(dict(r).get("sku")): dict(r).get("lead_time_days")
                  for r in db.all_sku_pack()}
    except Exception:  # noqa: BLE001
        sku_lt = {}
    try:
        summ = so.summarise_12mo(db.list_ip_stockout_events())
        counts = dict(zip(summ["SKU"], summ["stockouts_12mo"]))
    except Exception:  # noqa: BLE001
        counts = {}

    def _lt(sku: str, supplier: str) -> int:
        return sr.lead_time_days(sku, supplier, supplier_cfgs=cfgs,
                                 ip_lead_times=ip_lt, sku_lead_times=sku_lt)

    return sr.risk_table(engine_df, lead_time_fn=_lt, stockouts_12mo=counts,
                         build_skus=_build_skus())


def run(dryrun: bool = False) -> int:
    table = build_table()
    text = sr.format_message(table, app_url=APP_URL)
    if dryrun:
        print(text)
        return 0
    channel = os.environ.get("SLACK_STOCKOUT_RISK_CHANNEL_ID", "").strip()
    if not channel:
        log.warning("SLACK_STOCKOUT_RISK_CHANNEL_ID not set; not posting")
        print(text)
        return 0
    import fablab_slack
    ts, err = fablab_slack.post(text, channel_id=channel)
    if err:
        log.error("post failed: %s", err)
        return 1
    log.info("posted %d row(s) to %s (ts %s)", len(table), channel, ts)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--dryrun", action="store_true")
    args = ap.parse_args()
    return run(dryrun=args.dryrun)


if __name__ == "__main__":
    sys.exit(main())
