"""865FabLab BOM setup audit.

James's rule (2026-09-08): a corner connector should be picked up by the
FabLab planner as soon as someone fills in its BOM in CIN7 — but only if
the BOM carries the 865FabLab service SKU (OSC-865FABLAB-*). When whoever
created the item forgot the service line, prompt James instead of silently
skipping the item; and vice versa, flag service lines on items that do not
look like FabLab corners.

Heuristic (validated 2026-09-08 against all 126 FabLab SKUs — every one
is Category "Accessories - Profiles - Joints"; the 16 corners Nicolas
found missing were exactly the Joints assemblies built from a cut
profile piece without a service line):

  MISSING_SERVICE  Category == Joints AND BOM has a cut-profile component
                   (ComponentSKU ends in 0609) AND no OSC-865FABLAB-* line.
  ODD_SERVICE      BOM has an OSC-865FABLAB-* line but Category != Joints.
  SERVICE_ONLY     BOM is only the service line (no materials).

Usage:
  python fablab_bom_audit.py            # print findings
  python fablab_bom_audit.py --post     # also post NEW findings to Slack
                                        # (dedupe via fablab_settings)
Run by daily_sync.sh right after `cin7_sync.py boms`.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

log = logging.getLogger("fablab_bom_audit")

JOINTS_CATEGORY = "Accessories - Profiles - Joints"
SERVICE_PREFIX = "OSC-865FABLAB"
SETTING_KEY = "bom_audit_flagged"          # JSON list of "CODE:SKU" already posted
ALERT_CHANNEL = os.environ.get("FABLAB_BOM_ALERT_CHANNEL_ID", "C0BUPSA67QE")  # #cin7-sync-improvements
ALERT_USER = os.environ.get("FABLAB_BOM_ALERT_USER_ID", "U05UWJGD9GQ")        # James


def _is_service(sku) -> bool:
    return str(sku or "").upper().startswith(SERVICE_PREFIX)


def _is_cut_piece(sku) -> bool:
    return str(sku or "").upper().endswith("0609")


def find_issues(products: pd.DataFrame, boms: pd.DataFrame) -> list[dict]:
    """Return [{code, sku, name, category, detail}] sorted by code, sku."""
    if products is None or products.empty or boms is None or boms.empty:
        return []
    cat = products.set_index("SKU")["Category"].astype(str).to_dict()
    name = products.set_index("SKU")["Name"].astype(str).to_dict()

    svc_by_asm: dict[str, set] = {}
    cut_by_asm: dict[str, bool] = {}
    mat_by_asm: dict[str, int] = {}
    for asm, comp in zip(boms["AssemblySKU"], boms["ComponentSKU"]):
        asm = str(asm)
        if _is_service(comp):
            svc_by_asm.setdefault(asm, set()).add(str(comp))
        else:
            mat_by_asm[asm] = mat_by_asm.get(asm, 0) + 1
            if _is_cut_piece(comp):
                cut_by_asm[asm] = True
        svc_by_asm.setdefault(asm, set())

    out = []
    for asm, svcs in svc_by_asm.items():
        c = cat.get(asm, "")
        if not svcs and c == JOINTS_CATEGORY and cut_by_asm.get(asm):
            out.append({"code": "MISSING_SERVICE", "sku": asm, "name": name.get(asm, ""),
                        "category": c,
                        "detail": "Joints item built from cut profile, no OSC-865FABLAB line"})
        elif svcs and c != JOINTS_CATEGORY:
            out.append({"code": "ODD_SERVICE", "sku": asm, "name": name.get(asm, ""),
                        "category": c,
                        "detail": f"has {', '.join(sorted(svcs))} but category is '{c or 'blank'}'"})
        elif svcs and not mat_by_asm.get(asm):
            out.append({"code": "SERVICE_ONLY", "sku": asm, "name": name.get(asm, ""),
                        "category": c, "detail": "BOM has only the service line, no materials"})
    out.sort(key=lambda r: (r["code"], r["sku"]))
    return out


def load_frames(output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    prod = sorted(glob.glob(str(output_dir / "products_*.csv")))
    boms = sorted(glob.glob(str(output_dir / "boms_*.csv")))
    if not prod or not boms:
        return pd.DataFrame(), pd.DataFrame()
    return (pd.read_csv(prod[-1], low_memory=False),
            pd.read_csv(boms[-1], low_memory=False))


def format_message(issues: list[dict]) -> str:
    labels = {"MISSING_SERVICE": "Corner BOMs missing the 865FabLab service line (planner will skip these)",
              "ODD_SERVICE": "Service line on items that are not Joints",
              "SERVICE_ONLY": "Service line but no materials in BOM"}
    lines = [f"<@{ALERT_USER}> BOM setup check — {len(issues)} item(s) need a look in CIN7:"]
    for code, label in labels.items():
        rows = [r for r in issues if r["code"] == code]
        if not rows:
            continue
        lines.append(f"*{label}*")
        for r in rows[:40]:
            lines.append(f"• `{r['sku']}` — {r['name'][:60]}")
        if len(rows) > 40:
            lines.append(f"  …and {len(rows) - 40} more")
    lines.append("_Fix: add `OSC-865FABLAB-JOINT` ×1 to the BOM and set Fixed Purchase Cost "
                 "(or remove the service line / fix the category). Re-checked after every BOM sync._")
    return "\n".join(lines)


def post_new(issues: list[dict]) -> dict:
    """Post only findings not already announced. Returns summary."""
    import db
    import fablab_slack
    seen = set(json.loads(db.fablab_setting_get(SETTING_KEY, "[]") or "[]"))
    keys = {f"{r['code']}:{r['sku']}" for r in issues}
    new = [r for r in issues if f"{r['code']}:{r['sku']}" not in seen]
    summary = {"total": len(issues), "new": len(new), "posted": False, "error": None}
    if new:
        ts, err = fablab_slack.post(format_message(new), channel_id=ALERT_CHANNEL)
        summary["posted"] = bool(ts)
        summary["error"] = err
    if not new or summary["posted"]:
        # Keep only currently-open items so a fixed-then-broken SKU is re-announced.
        db.fablab_setting_set(SETTING_KEY, json.dumps(sorted(keys)), "fablab_bom_audit")
    return summary


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="post new findings to Slack")
    args = ap.parse_args(argv)
    from data_paths import OUTPUT_DIR
    products, boms = load_frames(OUTPUT_DIR)
    issues = find_issues(products, boms)
    for r in issues:
        print(f"{r['code']:16} {r['sku']:34} {r['name'][:50]}")
    print(f"{len(issues)} issue(s)")
    if args.post:
        print(json.dumps(post_new(issues)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
