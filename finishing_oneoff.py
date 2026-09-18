"""finishing_oneoff.py — one-off finishing orders from raw stock (2026-09-18)

James's request (#cin7-sync-improvements 2026-09-18): items that are
normally bought pre-finished from the OEM (no BOM) sometimes need to be
powder coated / anodized from excess raw stock. Requests like

    Powder coat LED-C7020001-2 Begton12 white x 20 from raw stock

should be handled in chat (the finishing control channel) and the rest
done behind the scenes.

CIN7 needs a BOM on the finished SKU to raise an assembly, so the flow is:

  1. parse the request text → finished SKU, qty, colour, process,
     optional explicit raw SKU;
  2. resolve the plan → raw SKU (same product family, "(Raw," variant),
     service SKU (OSC-POWDERCOAT-{WH|BK|SL}-{SML|LRG}-FT …), feet per unit
     from the length in the product name, size from a sibling SKU's BOM;
  3. post the plan as a thread reply and wait for `approve`;
  4. on approve: ensure the BOM exists in CIN7 (raw ×1 + service × ft,
     AutoAssembly OFF), then fablab_assemblies.place_order() — AUTHORISED
     assembly + Draft PO to All Star, the normal pick-list/PDF flow takes
     over once the PO is authorised.

State lives in `finishing_oneoff_requests` (keyed by Slack channel/ts).

CLI:
  python finishing_oneoff.py parse "Powder coat LED-C7020001-2 white x20"
  python finishing_oneoff.py plan --sku LED-C7020001-2 --qty 20 [--raw SKU] [--colour white]
  python finishing_oneoff.py place --sku LED-C7020001-2 --qty 20 [--raw SKU] [--colour white] [--apply]
  python finishing_oneoff.py scan [--dry]          (worker, every 3 min)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date
from typing import Optional

import db
import fablab_slack
from cin7_post_finishedgoods import BASE_URL, DEFAULT_RATE_S, _http
from outsource_flows import FINISHING

log = logging.getLogger("finishing_oneoff")

SKU_RE = re.compile(r"\b(?:LED|LEDKIT|PWR|CTL|ACC|OSC)-[A-Z0-9][A-Z0-9-]{2,}\b", re.I)
QTY_RE = re.compile(r"(?:\bx\s*(\d{1,4})\b|\b(\d{1,4})\s*(?:x|pcs?|pieces?|units?|ea|off)\b"
                    r"|\bqty\s*[:=]?\s*(\d{1,4})\b)", re.I)
RAW_RE = re.compile(r"\bfrom\s+(?:raw\s+)?(" + SKU_RE.pattern[2:] + r")", re.I)
COLOURS = {"white": "WH", "black": "BK", "silver": "SL", "grey": "SL", "gray": "SL"}
PROCESS_WORDS = (("anodi", "ANODIZING"), ("powder", "POWDERCOAT"), ("coat", "POWDERCOAT"),
                 ("paint", "POWDERCOAT"))
APPROVE_WORDS = ("approve", "approved", "go ahead", "go for it", "do it", "place order",
                 "order it", "yes")
CANCEL_WORDS = ("cancel", "ignore", "no thanks", "scrap that")
LOOKBACK_S = 6 * 3600          # only consider channel messages this recent
MM_PER_FT = 304.8


# ---------------------------------------------------------------------------
# 1. parse
# ---------------------------------------------------------------------------

def parse_request(text: str) -> Optional[dict]:
    """Return {finished_sku, raw_sku, qty, colour, process} or None when the
    message is not a finishing request (no process word + SKU + qty)."""
    t = " ".join(str(text or "").split())
    low = t.lower()
    process = next((p for w, p in PROCESS_WORDS if w in low), None)
    if not process:
        return None
    raw = None
    m = RAW_RE.search(t)
    if m:
        raw = m.group(1).upper()
    skus = [s.upper() for s in SKU_RE.findall(t)]
    skus = [s for s in skus if not s.startswith("OSC-") and s != raw]
    if not skus:
        return None
    qty = None
    for m in QTY_RE.finditer(t):
        qty = int(next(g for g in m.groups() if g))
        break
    if not qty:
        return None
    colour = next((c for c in COLOURS if re.search(rf"\b{c}\b", low)), None)
    return {"finished_sku": skus[0], "raw_sku": raw, "qty": qty,
            "colour": colour, "process": process}


# ---------------------------------------------------------------------------
# 2. resolve a plan against CIN7
# ---------------------------------------------------------------------------

def _headers() -> Optional[dict]:
    from cin7_post_finishedgoods import _credentials
    acc, key = _credentials()
    if not acc or not key:
        return None
    return {"api-auth-accountid": acc, "api-auth-applicationkey": key,
            "Content-Type": "application/json", "Accept": "application/json"}


def _get_product(headers: dict, *, sku: str = None, pid: str = None,
                 last_call: float = 0.0, full: bool = False):
    params = {"Limit": 1}
    if pid:
        params["ID"] = pid
    else:
        params["Sku"] = sku
    if full:
        params.update({"IncludeBOM": "true", "IncludeSuppliers": "true",
                       "IncludeReorderLevels": "true"})
    resp, last_call = _http("GET", f"{BASE_URL}/product", headers, params=params,
                            log=log, rate_s=DEFAULT_RATE_S, last_call=last_call)
    if resp is None or resp.status_code != 200:
        return None, last_call
    prods = (resp.json() or {}).get("Products") or []
    if sku:
        prods = [p for p in prods if str(p.get("SKU") or "").strip().upper() == sku.upper()]
    return (prods[0] if prods else None), last_call


def _search_products(headers: dict, name_prefix: str, last_call: float = 0.0):
    out = []
    page = 1
    while True:
        resp, last_call = _http("GET", f"{BASE_URL}/product", headers,
                                params={"Name": name_prefix, "Limit": 100, "Page": page,
                                        "IncludeBOM": "true"},
                                log=log, rate_s=DEFAULT_RATE_S, last_call=last_call)
        if resp is None or resp.status_code != 200:
            break
        prods = (resp.json() or {}).get("Products") or []
        out.extend(prods)
        if len(prods) < 100 or page >= 5:
            break
        page += 1
    return out, last_call


_COLOUR_IN_NAME = re.compile(r"\((White|Black|Silver|Raw|Grey|Gray|Bronze|Natural)\b", re.I)


def feet_per_unit(name: str) -> Optional[float]:
    """Feet of profile from the length in the product name, rounded up
    like the existing BOMs (2m → 7 ft, 2390mm → 8 ft, 1m → 4 ft)."""
    n = str(name or "")
    m = re.search(r"(\d{3,4})\s*mm", n, re.I)
    if m:
        return float(math.ceil(int(m.group(1)) / MM_PER_FT))
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*m\b", n, re.I)
    if m:
        return float(math.ceil(float(m.group(1)) * 1000 / MM_PER_FT))
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:"|in\b|inch)', n, re.I)
    if m:
        return float(math.ceil(float(m.group(1)) / 12))
    return None


def resolve_plan(finished_sku: str, qty: float, *, colour: Optional[str] = None,
                 process: str = "POWDERCOAT", raw_sku: Optional[str] = None,
                 headers: Optional[dict] = None) -> dict:
    """Work out raw SKU, service SKU and feet for one finished SKU. Returns
    a plan dict; plan["errors"] non-empty means it cannot be placed."""
    plan = {"finished_sku": finished_sku.upper(), "qty": float(qty), "raw_sku": raw_sku,
            "service_sku": None, "per_unit": None, "existing_bom": False,
            "name": "", "raw_name": "", "size": None, "notes": [], "errors": []}
    headers = headers or _headers()
    if not headers:
        plan["errors"].append("CIN7 credentials missing.")
        return plan
    prod, lc = _get_product(headers, sku=plan["finished_sku"], full=True)
    if not prod:
        plan["errors"].append(f"{plan['finished_sku']} not found in CIN7.")
        return plan
    plan["name"] = prod.get("Name") or ""
    plan["product_id"] = prod.get("ID")
    plan["on_hand"] = None

    # Existing finishing BOM → nothing to add, just order it.
    bom = prod.get("BillOfMaterialsProducts") or []
    svc = [b for b in bom if FINISHING.is_service(b.get("ProductCode"))]
    if prod.get("BillOfMaterial") and svc:
        plan["existing_bom"] = True
        plan["service_sku"] = str(svc[0]["ProductCode"]).upper()
        plan["per_unit"] = float(svc[0].get("Quantity") or 0)
        raws = [b for b in bom if not FINISHING.is_service(b.get("ProductCode"))]
        plan["raw_sku"] = str(raws[0]["ProductCode"]).upper() if raws else None
        plan["raw_name"] = raws[0].get("Name", "") if raws else ""
        plan["bom"] = bom
        plan["autoassembly"] = bool(prod.get("AutoAssembly"))
        return plan
    if prod.get("BillOfMaterial") and bom:
        plan["errors"].append(
            f"{plan['finished_sku']} already has a BOM without a finishing service "
            "line — fix it in CIN7 first.")
        return plan

    # Colour: from the request, else from the product name.
    col = COLOURS.get((colour or "").lower())
    if not col:
        m = _COLOUR_IN_NAME.search(plan["name"])
        col = COLOURS.get(m.group(1).lower()) if m else None
    if not col:
        plan["errors"].append("Could not tell the colour — say white / black / silver.")
        return plan
    plan["colour_code"] = col
    if process == "ANODIZING" and col == "WH":
        plan["errors"].append("Anodizing has no white — did you mean powder coat?")
        return plan

    # Family siblings: same name up to the "(colour" bracket.
    m = _COLOUR_IN_NAME.search(plan["name"])
    family = plan["name"][:m.start()].strip() if m else plan["name"].split("(")[0].strip()
    siblings, lc = _search_products(headers, family, last_call=lc)
    siblings = [s for s in siblings if str(s.get("Name") or "").startswith(family)]

    # Raw SKU: explicit, else the sibling whose name says Raw with the same length.
    per = feet_per_unit(plan["name"])
    if not plan["raw_sku"]:
        raws = [s for s in siblings if re.search(r"\((Raw|Natural)\b", s.get("Name") or "", re.I)
                and feet_per_unit(s.get("Name")) == per and not s.get("BillOfMaterial")]
        if len(raws) == 1:
            plan["raw_sku"] = str(raws[0]["SKU"]).upper()
        elif raws:
            plan["notes"].append("Several raw candidates: " + ", ".join(r["SKU"] for r in raws))
            plan["raw_sku"] = str(raws[0]["SKU"]).upper()
        else:
            plan["errors"].append(
                f"No raw variant found for {plan['finished_sku']} — say `from <RAW-SKU>`.")
            return plan
    rawp, lc = _get_product(headers, sku=plan["raw_sku"], last_call=lc)
    if not rawp:
        plan["errors"].append(f"Raw SKU {plan['raw_sku']} not found in CIN7.")
        return plan
    plan["raw_name"] = rawp.get("Name") or ""
    plan["raw_product_id"] = rawp.get("ID")

    # Size (SML/LRG) from any sibling that already has a finishing line.
    size = None
    for s in siblings:
        for b in s.get("BillOfMaterialsProducts") or []:
            code = str(b.get("ProductCode") or "").upper()
            mm = re.match(r"OSC-(?:POWDERCOAT|ANODIZING)-\w\w-(SML|LRG)-FT$", code)
            if mm:
                size = mm.group(1)
                break
        if size:
            break
    if not size:
        size = "SML"
        plan["notes"].append("No sibling BOM to copy the profile size from — assumed "
                             "SMALL profile rate (edit the PO if it is a large profile).")
    plan["size"] = size
    if per is None:
        per = 1.0
        plan["notes"].append("Could not read a length from the name — charging 1 ft/unit.")
    plan["per_unit"] = per
    plan["service_sku"] = f"OSC-{process}-{col}-{size}-FT"
    svcp, lc = _get_product(headers, sku=plan["service_sku"], last_call=lc)
    if not svcp:
        plan["errors"].append(f"Service SKU {plan['service_sku']} not found in CIN7.")
        return plan
    plan["service_product_id"] = svcp.get("ID")
    plan["service_name"] = svcp.get("Name") or ""
    return plan


def plan_text(plan: dict) -> str:
    if plan["errors"]:
        return ":x: Can't set this up: " + " ".join(plan["errors"])
    q = plan["qty"]
    lines = [f"*Finishing request — {plan['finished_sku']} × {q:g}*",
             f"• Finished: `{plan['finished_sku']}` {plan['name']}",
             f"• Raw picked from stock: `{plan['raw_sku']}` × {q:g}",
             f"• All Star line: `{plan['service_sku']}` × {plan['per_unit'] * q:g} ft "
             f"({plan['per_unit']:g} ft each)"]
    if plan["existing_bom"]:
        lines.append("• BOM already in CIN7 — will order as-is.")
    else:
        lines.append("• BOM will be added to CIN7 (raw ×1 + service line, AutoAssembly off); "
                     "buying it pre-finished from the OEM still works.")
    for n in plan["notes"]:
        lines.append(f"• :warning: {n}")
    lines.append("Reply `approve` to raise the CIN7 assembly + All Star draft PO, "
                 "or `cancel`.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. BOM + place
# ---------------------------------------------------------------------------

def ensure_bom(plan: dict, headers: dict, *, apply: bool, last_call: float = 0.0) -> dict:
    """Add the finishing BOM to the finished SKU (idempotent)."""
    out = {"changed": False, "error": None}
    if plan.get("existing_bom"):
        if plan.get("autoassembly"):
            out["note"] = "AutoAssembly is ON for this SKU — consider running finishing_autoassembly_off.py"
        return out
    full, last_call = _get_product(headers, pid=plan["product_id"], full=True, last_call=last_call)
    if not full:
        out["error"] = "could not re-fetch product"
        return out
    body = dict(full)
    body.pop("CreatedDate", None)
    body["BillOfMaterial"] = True
    body["AutoAssembly"] = False
    body["AutoDisassembly"] = False
    body["AssemblyCostEstimationMethod"] = body.get("AssemblyCostEstimationMethod") or "Average Cost"
    body["BillOfMaterialsProducts"] = [
        {"ComponentProductID": plan["raw_product_id"], "ProductCode": plan["raw_sku"],
         "Name": plan["raw_name"], "Quantity": 1.0, "WastagePercent": 0.0,
         "WastageQuantity": 0.0, "CostPercentage": 0.0},
        {"ComponentProductID": plan["service_product_id"], "ProductCode": plan["service_sku"],
         "Name": plan.get("service_name", ""), "Quantity": float(plan["per_unit"]),
         "WastagePercent": 0.0, "WastageQuantity": 0.0, "CostPercentage": 0.0},
    ]
    if not apply:
        out["would_change"] = body["BillOfMaterialsProducts"]
        return out
    resp, last_call = _http("PUT", f"{BASE_URL}/product", headers, json_body=body,
                            log=log, rate_s=DEFAULT_RATE_S, last_call=last_call)
    if resp is None or resp.status_code != 200:
        out["error"] = (f"PUT product {resp.status_code if resp is not None else 'network'}: "
                        f"{resp.text[:300] if resp is not None else ''}")
        return out
    out["changed"] = True
    plan["existing_bom"] = True
    return out


def bom_parents_for(plan: dict) -> dict:
    """bom_parents entry for place_order() built from the plan, so the
    worker's (possibly stale) BOM CSV is not needed for a fresh BOM."""
    comps = plan.get("bom")
    if comps:
        return {plan["finished_sku"]: [
            {"ComponentSKU": c.get("ProductCode"), "ComponentName": c.get("Name"),
             "Quantity": c.get("Quantity"), "BOMType": "Component"} for c in comps]}
    return {plan["finished_sku"]: [
        {"ComponentSKU": plan["raw_sku"], "ComponentName": plan["raw_name"],
         "Quantity": 1.0, "BOMType": "Component"},
        {"ComponentSKU": plan["service_sku"], "ComponentName": plan.get("service_name", ""),
         "Quantity": plan["per_unit"], "BOMType": "Component"},
    ]}


def place(plan: dict, *, actor: str, apply: bool, requested_by: str = "") -> dict:
    """ensure BOM → draft order → fablab_assemblies.place_order(FINISHING)."""
    import fablab_assemblies
    res = {"ok": False, "errors": list(plan["errors"]), "warnings": [], "bom": None}
    if res["errors"]:
        return res
    headers = _headers()
    if not headers:
        res["errors"].append("CIN7 credentials missing.")
        return res
    bom = ensure_bom(plan, headers, apply=apply)
    res["bom"] = bom
    if bom.get("error"):
        res["errors"].append(f"BOM: {bom['error']}")
        return res
    product_map, stock_map, bom_parents = fablab_assemblies._picker_maps()
    bom_parents = dict(bom_parents)
    bom_parents.update(bom_parents_for(plan))
    if not apply:
        res["dry_run"] = True
        res["ok"] = True
        return res
    who = f" for {requested_by}" if requested_by else ""
    draft_id = db.create_po_draft(
        supplier=FINISHING.supplier,
        name=f"One-off {date.today().isoformat()} {plan['finished_sku']} x{plan['qty']:g}",
        actor=actor,
        note=f"One-off finishing from raw stock{who}: {plan['finished_sku']} × {plan['qty']:g} "
             f"from {plan['raw_sku']}")
    db.upsert_po_draft_line(draft_id, plan["finished_sku"], plan["qty"], actor)
    out = fablab_assemblies.place_order(draft_id, bom_parents, product_map, actor=actor,
                                        apply=True, stock_map=stock_map, flow=FINISHING)
    out["draft_id"] = draft_id
    out["bom"] = bom
    return out


# ---------------------------------------------------------------------------
# 4. Slack scan: new requests in the finishing channel + approvals
# ---------------------------------------------------------------------------

def _slack():
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return None, None
    import slack_sync
    return slack_sync, slack_sync._build_session(token)


def _user_name(slack_sync, session, uid: str) -> str:
    try:
        return slack_sync._resolve_user(session, uid) or uid
    except Exception:  # noqa: BLE001
        return uid


def scan(apply: bool = True) -> dict:
    stats = {"seen": 0, "proposed": 0, "placed": 0, "cancelled": 0, "errors": []}
    slack_sync, session = _slack()
    if not session:
        log.info("SLACK_BOT_TOKEN not set; skipping.")
        return stats
    ch = FINISHING.channel_id
    oldest = time.time() - LOOKBACK_S
    try:
        body = slack_sync._slack_get(session, "conversations.history",
                                     {"channel": ch, "oldest": f"{oldest:.6f}", "limit": 100})
    except Exception as exc:  # noqa: BLE001
        stats["errors"].append(f"history: {exc}")
        return stats
    known = {r["slack_ts"]: r for r in db.list_finishing_oneoff_requests(ch, open_only=False)}

    # --- new requests
    for m in body.get("messages") or []:
        ts = m.get("ts")
        if not ts or ts in known or m.get("bot_id") or m.get("subtype") == "bot_message":
            continue
        if m.get("thread_ts") and m.get("thread_ts") != ts:
            continue
        req = parse_request(m.get("text") or "")
        if not req:
            continue
        stats["seen"] += 1
        user = _user_name(slack_sync, session, m.get("user") or "")
        plan = resolve_plan(req["finished_sku"], req["qty"], colour=req["colour"],
                            process=req["process"], raw_sku=req["raw_sku"])
        text = plan_text(plan)
        status = "error" if plan["errors"] else "proposed"
        if apply:
            fablab_slack.post(text, channel_id=ch, thread_ts=ts)
            db.create_finishing_oneoff_request(ch, ts, user, m.get("text") or "", plan, status)
        else:
            log.info("[DRY] would reply:\n%s", text)
        stats["proposed"] += 1
        log.info("%s: %s x%s -> %s", status, plan["finished_sku"], plan["qty"], plan["errors"] or "ok")

    # --- approvals on proposed requests
    for r in db.list_finishing_oneoff_requests(ch, open_only=True):
        try:
            rep = slack_sync._slack_get(session, "conversations.replies",
                                        {"channel": ch, "ts": r["slack_ts"], "limit": 50})
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"replies {r['slack_ts']}: {exc}")
            continue
        msgs = (rep.get("messages") or [])[1:]
        for m in msgs:
            if m.get("bot_id") or m.get("subtype") == "bot_message":
                continue
            low = (m.get("text") or "").strip().lower()
            if not low:
                continue
            if any(w in low for w in CANCEL_WORDS):
                db.update_finishing_oneoff_request(r["id"], status="cancelled")
                if apply:
                    fablab_slack.post("Cancelled — nothing raised.", channel_id=ch,
                                      thread_ts=r["slack_ts"])
                stats["cancelled"] += 1
                break
            if not any(re.search(rf"\b{re.escape(w)}\b", low) for w in APPROVE_WORDS):
                continue
            who = _user_name(slack_sync, session, m.get("user") or "")
            plan = json.loads(r["plan_json"])
            if not apply:
                log.info("[DRY] would place %s x%s", plan["finished_sku"], plan["qty"])
                stats["placed"] += 1
                break
            out = place(plan, actor=f"slack:{who}", apply=True, requested_by=r["requested_by"])
            if out.get("ok"):
                db.update_finishing_oneoff_request(
                    r["id"], status="placed", approved_by=who, draft_id=out.get("draft_id"),
                    po_number=out.get("po_number"))
                asm = ", ".join(a.get("assembly_number") or "?" for a in out.get("assemblies") or [])
                bom_note = (" BOM added to CIN7." if (out.get("bom") or {}).get("changed") else "")
                warn = ("\n:warning: " + "\n:warning: ".join(out["warnings"])) if out.get("warnings") else ""
                fablab_slack.post(
                    f":white_check_mark: Approved by {who} — assembly {asm} AUTHORISED, "
                    f"All Star PO *{out.get('po_number')}* created as DRAFT (needs "
                    f"authorising in CIN7; pick list + vendor sheet post here once it is)."
                    f"{bom_note}{warn}", channel_id=ch, thread_ts=r["slack_ts"])
                stats["placed"] += 1
            else:
                err = "; ".join(out.get("errors") or ["place failed"])
                db.update_finishing_oneoff_request(r["id"], status="error", approved_by=who,
                                                   error=err)
                fablab_slack.post(f":x: Could not place: {err}", channel_id=ch,
                                  thread_ts=r["slack_ts"])
                stats["errors"].append(err)
            break
    return stats


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("parse"); sp.add_argument("text")
    for name in ("plan", "place"):
        s = sub.add_parser(name)
        s.add_argument("--sku", required=True); s.add_argument("--qty", type=float, required=True)
        s.add_argument("--raw"); s.add_argument("--colour")
        s.add_argument("--process", default="POWDERCOAT", choices=["POWDERCOAT", "ANODIZING"])
        if name == "place":
            s.add_argument("--apply", action="store_true")
            s.add_argument("--actor", default="cli")
    sub.add_parser("scan").add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "parse":
        out = parse_request(a.text)
    elif a.cmd == "plan":
        out = resolve_plan(a.sku, a.qty, colour=a.colour, process=a.process, raw_sku=a.raw)
        print(plan_text(out))
    elif a.cmd == "place":
        plan = resolve_plan(a.sku, a.qty, colour=a.colour, process=a.process, raw_sku=a.raw)
        print(plan_text(plan))
        out = place(plan, actor=a.actor, apply=a.apply)
    else:
        out = scan(apply=not a.dry)
    print(json.dumps(out, indent=1, default=str))
    return 1 if (isinstance(out, dict) and out.get("errors")) else 0


if __name__ == "__main__":
    sys.exit(main())
