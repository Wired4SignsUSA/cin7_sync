"""finishing_autoassembly_off.py — turn CIN7 AutoAssembly OFF for finished
SKUs in the All Star finishing flow (James, 2026-09-10).

Why: most powder-coated / anodized profile SKUs are AutoAssembly=true, so a
web sale of a black 2 m makes CIN7 consume the raw profile + service line
on the spot with no coating done. That fights the WIP-based planner
(Finishing Work Orders) and hides real shortages. Finished stock now comes
from completed assemblies, so the flag must be off.

Scope: every SKU whose BOM carries an OSC-POWDERCOAT-* / OSC-ANODIZING-*
service line (same candidate list as the planner). Dry-run by default.

  python finishing_autoassembly_off.py            # list what would change
  python finishing_autoassembly_off.py --apply    # PUT /product with AutoAssembly=false
  python finishing_autoassembly_off.py --sku LED-AL-PL55B-FL-2 --apply

CIN7 rules: PUT /product needs the GET body minus CreatedDate (else 409);
GET /product?Sku= is an exact match with IncludeBOM optional. Rate limit
honoured via cin7_post_finishedgoods._http (DEFAULT_RATE_S).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from cin7_post_finishedgoods import BASE_URL, DEFAULT_RATE_S, _credentials, _http
from outsource_flows import FINISHING

log = logging.getLogger("finishing_autoassembly_off")


def _headers() -> dict:
    account_id, app_key = _credentials()
    if not account_id or not app_key:
        raise SystemExit("CIN7 credentials missing (CIN7_ACCOUNT_ID / CIN7_APPLICATION_KEY).")
    return {"api-auth-accountid": account_id, "api-auth-applicationkey": app_key,
            "Content-Type": "application/json", "Accept": "application/json"}


def candidate_skus() -> list[str]:
    """Finished SKUs with a finishing service line, from the synced BOM CSV."""
    from app_pages.fablab_work_orders import bom_service_skus
    from fablab_stock_alert import _load_data
    _products, _stock, _engine, bom_parents = _load_data()
    return sorted(bom_service_skus(bom_parents, FINISHING))


def run(skus: list[str], apply: bool) -> dict:
    headers = _headers()
    out = {"checked": 0, "already_off": [], "changed": [], "would_change": [],
           "not_found": [], "errors": []}
    last_call = 0.0
    for sku in skus:
        out["checked"] += 1
        resp, last_call = _http("GET", f"{BASE_URL}/product", headers,
                                params={"Sku": sku, "Limit": 1}, log=log,
                                rate_s=DEFAULT_RATE_S, last_call=last_call)
        if resp is None or resp.status_code != 200:
            out["errors"].append(f"{sku}: GET {resp.status_code if resp is not None else 'network'}")
            continue
        prods = [p for p in (resp.json() or {}).get("Products") or []
                 if str(p.get("SKU") or "").strip().upper() == sku.upper()]
        if not prods:
            out["not_found"].append(sku)
            continue
        prod = prods[0]
        if not bool(prod.get("AutoAssembly")):
            out["already_off"].append(sku)
            continue
        if not apply:
            out["would_change"].append(sku)
            continue
        body = dict(prod)
        body.pop("CreatedDate", None)
        body["AutoAssembly"] = False
        resp, last_call = _http("PUT", f"{BASE_URL}/product", headers, json_body=body,
                                log=log, rate_s=DEFAULT_RATE_S, last_call=last_call)
        if resp is None or resp.status_code != 200:
            out["errors"].append(
                f"{sku}: PUT {resp.status_code if resp is not None else 'network'} "
                f"{(resp.text[:200] if resp is not None else '')}")
            continue
        out["changed"].append(sku)
        log.info("AutoAssembly OFF: %s", sku)
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sku", action="append", help="limit to these SKUs (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write to CIN7 (default: dry run)")
    args = ap.parse_args(argv)
    skus = args.sku or candidate_skus()
    res = run(skus, apply=args.apply)
    res["dry_run"] = not args.apply
    print(json.dumps(res, indent=1))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
