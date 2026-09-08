"""865FabLab order documents (James, 2026-09-08).

Two PDFs per placed order, built from the Cin7 assemblies actually raised
(fablab_assemblies.response_json → OrderLines) so they match what Cin7
will consume:

  pick list  — Letter. Consolidated components across all assemblies,
               whole lengths, stock locator, on hand, tick box; then a
               per-assembly breakdown. For the W4S warehouse to pick and
               hand over to 865FabLab.
  labels     — 2.25 x 1.25 in, one label per finished unit (SKU, name,
               Code128 barcode of the product barcode or SKU, PO). For
               Richard to print and give to 865FabLab for each pack.

Usage:
  python fablab_pick_pdf.py --draft 7            # write PDFs to OUTPUT_DIR/fablab_docs
  python fablab_pick_pdf.py --draft 7 --post     # also upload to #865-corner-manufacture
Called automatically by fablab_assemblies.check_po_authorised after the
per-assembly Slack posts.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from reportlab.graphics.barcode import code128
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

log = logging.getLogger("fablab_pick_pdf")

LABEL_W, LABEL_H = 2.25 * inch, 1.25 * inch


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _clean_code(v) -> str:
    """Barcode from a pandas row may arrive as float (723098691584.0) or nan."""
    t = str(v if v is not None else "").strip()
    if t.lower() in ("", "nan", "none"):
        return ""
    if t.endswith(".0") and t[:-2].isdigit():
        t = t[:-2]
    return t


def _ceil(v: float) -> int:
    return int(math.ceil(_num(v) - 1e-9))


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def collect(draft_id: int) -> dict:
    """{order, po_number, assemblies:[{number, sku, name, qty, comps:[(sku,name,total)]}],
        totals:{sku:{name,total,loc,on_hand}}}"""
    import db
    import fablab_assemblies as fa
    d = db.get_po_draft(draft_id)
    if not d:
        raise SystemExit(f"order #{draft_id} not found")
    note = db.fablab_po_notification_get(draft_id) or {}
    po_number = (note.get("cin7_po_number") if hasattr(note, "get") else None) \
        or d.get("cin7_po_number") or ""
    product_map, stock_map, _ = fa._picker_maps()

    rows, totals = [], {}
    for a in db.list_fablab_assemblies(draft_id):
        if a["status"] in ("voided", "cancelled"):
            continue
        try:
            task = json.loads(a["response_json"] or "{}")
        except ValueError:
            task = {}
        qty = _num(a["quantity"])
        comps = []
        for ln in task.get("OrderLines", []) or []:
            csku = str(ln.get("ProductCode") or "").strip()
            if not csku or fa._is_labor(csku):
                continue
            tot = _num(ln.get("TotalQuantity")) or qty * _num(ln.get("Quantity"))
            cname = str(ln.get("Name") or "")
            comps.append((csku, cname, tot))
            t = totals.setdefault(csku, {"name": cname, "total": 0.0})
            t["total"] += tot
        rows.append({"number": a["assembly_number"], "sku": a["sku"],
                     "name": task.get("ProductName") or fa._name_of(product_map, a["sku"]),
                     "qty": qty, "comps": comps,
                     "barcode": _clean_code((product_map.get(a["sku"]) or {}).get("Barcode"))})
    for csku, t in totals.items():
        t["loc"] = fa._locator_of(product_map, csku)
        stk = stock_map.get(csku) or {}
        t["on_hand"] = _num(stk.get("OnHand", stk.get("Available", 0))) if stk else None
        t["whole"] = _ceil(t["total"])
    return {"order": d, "po_number": po_number, "assemblies": rows, "totals": totals}


# ---------------------------------------------------------------------------
# pick list
# ---------------------------------------------------------------------------

def write_pick_list(data: dict, path: Path) -> Path:
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    h = styles["Heading1"]
    d = data["order"]
    units = sum(r["qty"] for r in data["assemblies"])
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch, topMargin=0.5 * inch,
                            bottomMargin=0.5 * inch,
                            title=f"Pick list {data['po_number']}")
    story = [Paragraph(f"865FabLab pick list — {data['po_number'] or 'PO pending'}", h),
             Paragraph(f"Order #{d['id']} · {d.get('name') or ''} · {units:g} units across "
                       f"{len(data['assemblies'])} assemblies · printed {date.today():%Y-%m-%d}",
                       styles["Normal"]),
             Paragraph("Pick the <b>whole lengths</b> below from the locator shown and hand "
                       "over to 865FabLab. Tick each line as picked. Shortages: note in the "
                       "assembly's Slack thread.", small),
             Spacer(1, 8),
             Paragraph("Materials to pick (all assemblies combined)", styles["Heading2"])]

    hdr = ["", "Component SKU", "Description", "Locator", "Pick", "BOM exact", "On hand"]
    body = [hdr]
    for csku in sorted(data["totals"], key=lambda s: (data["totals"][s]["loc"] or "zzz", s)):
        t = data["totals"][csku]
        oh = "" if t["on_hand"] is None else f"{t['on_hand']:g}"
        flag = " !!" if t["on_hand"] is not None and t["on_hand"] < t["whole"] else ""
        body.append(["[  ]", csku, Paragraph(t["name"][:90], small),
                     Paragraph(t["loc"] or "—", small),
                     f"{t['whole']}", f"{t['total']:.3f}".rstrip("0").rstrip("."), oh + flag])
    tbl = Table(body, colWidths=[0.3 * inch, 1.7 * inch, 2.6 * inch, 1.1 * inch,
                                 0.5 * inch, 0.7 * inch, 0.6 * inch], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("FONT", (4, 1), (4, -1), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
    ]))
    story += [tbl, Spacer(1, 6),
              Paragraph("!! = on hand below pick qty (may need cutting from master length). "
                        "Pick = BOM exact rounded up to whole pieces.", small),
              Spacer(1, 14),
              Paragraph("Breakdown by assembly (what 865FabLab builds)", styles["Heading2"])]

    body = [["Assembly", "Finished SKU", "Description", "Qty", "Components"]]
    for r in data["assemblies"]:
        comp_txt = "<br/>".join(f"{c[0]} × {c[2]:.3f}".rstrip("0").rstrip(".")
                                for c in r["comps"]) or "—"
        body.append([r["number"], r["sku"], Paragraph(r["name"][:80], small),
                     f"{r['qty']:g}", Paragraph(comp_txt, small)])
    tbl = Table(body, colWidths=[0.8 * inch, 2.0 * inch, 2.4 * inch, 0.5 * inch, 1.8 * inch],
                repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [tbl, Spacer(1, 18),
              Paragraph("Picked by: ______________________   Date: ____________   "
                        "Handed to 865FabLab (sign): ______________________", styles["Normal"])]
    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------

def _wrap(text: str, width_chars: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width_chars:
            cur = (cur + " " + w).strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:width_chars - 1] + "…"
    return lines


def write_labels(data: dict, path: Path) -> tuple[Path, int]:
    """One 2.25x1.25in page per finished unit. Returns (path, label_count)."""
    c = canvas.Canvas(str(path), pagesize=(LABEL_W, LABEL_H))
    c.setTitle(f"Labels {data['po_number']}")
    n = 0
    for r in data["assemblies"]:
        count = _ceil(r["qty"])
        for i in range(count):
            _draw_label(c, r, i + 1, count, data["po_number"])
            c.showPage()
            n += 1
    c.save()
    return path, n


def _draw_label(c: canvas.Canvas, r: dict, idx: int, count: int, po: str) -> None:
    m = 0.08 * inch
    sku = r["sku"]
    # SKU — shrink to fit
    size = 11
    while c.stringWidth(sku, "Helvetica-Bold", size) > LABEL_W - 2 * m and size > 6:
        size -= 0.5
    c.setFont("Helvetica-Bold", size)
    c.drawString(m, LABEL_H - m - size, sku)
    # name, 2 lines
    c.setFont("Helvetica", 6)
    y = LABEL_H - m - size - 8
    for ln in _wrap(r["name"], 52, 2):
        c.drawString(m, y, ln)
        y -= 7
    # barcode
    value = r.get("barcode") or sku
    bc = code128.Code128(value, barHeight=0.30 * inch, barWidth=0.5,
                         humanReadable=False, quiet=False)
    while bc.width > LABEL_W - 2 * m and bc.barWidth > 0.25:
        bc = code128.Code128(value, barHeight=0.30 * inch, barWidth=bc.barWidth - 0.05,
                             humanReadable=False, quiet=False)
    bc.drawOn(c, (LABEL_W - bc.width) / 2, m + 13)
    c.setFont("Helvetica", 5.5)
    c.drawCentredString(LABEL_W / 2, m + 7, value)
    c.setFont("Helvetica", 5)
    c.drawString(m, m, f"{po} · {idx}/{count}")
    c.drawRightString(LABEL_W - m, m, "Made by 865FabLab for Wired4Signs USA")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def build_docs(draft_id: int, out_dir: Optional[Path] = None) -> dict:
    from data_paths import OUTPUT_DIR
    out_dir = Path(out_dir or (OUTPUT_DIR / "fablab_docs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    data = collect(draft_id)
    tag = data["po_number"] or f"order{draft_id}"
    pick = write_pick_list(data, out_dir / f"{tag}_pick_list.pdf")
    labels, n = write_labels(data, out_dir / f"{tag}_labels_2.25x1.25.pdf")
    return {"pick_list": pick, "labels": labels, "label_count": n,
            "po_number": data["po_number"], "order": data["order"],
            "components": len(data["totals"]), "assemblies": len(data["assemblies"])}


def post_docs(draft_id: int, channel_id: Optional[str] = None,
              thread_ts: Optional[str] = None) -> dict:
    """Build + upload both PDFs to Slack. Returns build summary + errors."""
    import fablab_assemblies as fa
    import fablab_slack
    res = build_docs(draft_id)
    channel_id = channel_id or fa.CORNER_CHANNEL_ID
    po = res["po_number"]
    errs = []
    _fid, err = fablab_slack.upload_file(
        res["pick_list"], channel_id, thread_ts=thread_ts,
        title=f"{po} pick list",
        initial_comment=(f":clipboard: *{po} — pick list for W4S warehouse* "
                         f"({res['components']} components, {res['assemblies']} assemblies, "
                         "whole lengths + stock locators). Pick, tick, hand over to 865FabLab."))
    if err:
        errs.append(f"pick list upload: {err}")
    _fid, err = fablab_slack.upload_file(
        res["labels"], channel_id, thread_ts=thread_ts,
        title=f"{po} labels 2.25x1.25",
        initial_comment=(f":label: *{po} — {res['label_count']} pack labels* "
                         "(2.25×1.25\", one per finished unit, SKU + barcode). "
                         "<@U08K72UC7S9> please print and pass to 865FabLab."))
    if err:
        errs.append(f"labels upload: {err}")
    res["errors"] = errs
    return res


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", type=int, required=True)
    ap.add_argument("--post", action="store_true", help="upload to #865-corner-manufacture")
    ap.add_argument("--thread", default=None, help="thread_ts to post under")
    args = ap.parse_args(argv)
    res = post_docs(args.draft, thread_ts=args.thread) if args.post else build_docs(args.draft)
    res.pop("order", None)
    print(json.dumps({k: str(v) for k, v in res.items()}, indent=1))
    return 1 if res.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
