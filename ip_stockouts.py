"""
ip_stockouts.py (RULES 9.15, James 2026-09-25)
==============================================

Pull every Inventory Planner variant's `stockouts_hist` (day-level
out-of-stock / back-in-stock transitions) and store the episodes in
`ip_stockout_events` (sku, out_date, back_date|NULL). The table is
replaced wholesale each run.

Consumers: "Stock-outs 12 mo" column on Ordering, Finishing and
865FabLab corners; red "orders hit by a stock-out" line on Monthly
Metrics' Stock optimisation progress chart (maths: engine/stockouts.py).

CLI:  python ip_stockouts.py sync [--dry-run] [--limit-pages N]
Env:  IP_API_KEY, IP_ACCOUNT. ~14 pages x 1000 variants, ~30 s.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import db  # noqa: E402
import ip_lead_times as _iplt  # noqa: E402  (pagination + UA reuse)
from engine.stockouts import episodes_from_hist  # noqa: E402

FIELDS = "id,connections,stockouts_hist"
KEEP_DAYS = 800  # ~26 months: covers the 12-mo column and the MM chart

log = logging.getLogger("ip_stockouts")


def cmd_sync(args) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    load_dotenv()
    key, account = os.environ.get("IP_API_KEY"), os.environ.get("IP_ACCOUNT")
    if not key or not account:
        log.error("IP_API_KEY / IP_ACCOUNT not set")
        return 1
    headers = {"Authorization": key, "Account": account,
               "Accept": "application/json",
               "User-Agent": _iplt.USER_AGENT}
    _iplt.FIELDS = FIELDS  # fetch_variants reads the module constant
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    rows: List[tuple] = []
    seen: set = set()
    n_var = 0
    for v in _iplt.fetch_variants(headers, float(
            os.environ.get("IP_RATE_SECONDS", "1.0")), args.limit_pages):
        n_var += 1
        sku = _iplt._master_sku(v)
        if not sku or sku in seen:
            continue
        seen.add(sku)
        for out_d, back_d in episodes_from_hist(v.get("stockouts_hist")):
            if back_d is not None and back_d < cutoff:
                continue
            rows.append((sku, out_d.isoformat(),
                         back_d.isoformat() if back_d else None))
    log.info("variants=%d skus=%d episodes=%d", n_var, len(seen), len(rows))
    if n_var < 1000 and not args.limit_pages:
        log.error("suspiciously few variants (%d) — not replacing table",
                  n_var)
        return 1
    if args.dry_run:
        return 0
    n = db.replace_ip_stockout_events(rows)
    log.info("ip_stockout_events replaced: %d rows", n)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Sync IP stock-out history.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--limit-pages", type=int, default=None)
    s.set_defaults(func=cmd_sync)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
