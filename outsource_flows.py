"""outsource_flows.py — profiles for the outsourced-work flows (2026-09-10).

The 865FabLab corner flow (fablab_assemblies.py) and the All Star
finishing flow (powder coating / anodizing) share one engine:

    planner → order (po_drafts) → AUTHORISED CIN7 assembly per finished SKU
    + Draft PO to the vendor for the BOM's service SKUs → buyer authorises
    the PO in CIN7 → worker posts pick list + docs to the flow's Slack
    channel → `done` replies (or the app) complete the assemblies.

What differs per flow is captured here so the engine stays generic:
which supplier, which service-SKU prefixes count as "vendor work" (never
material to pick), which Slack channel, whether an Odoo quote is raised,
and which documents are produced.

No db / streamlit imports — safe to import from anywhere.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Flow:
    key: str                      # "fablab" | "finishing"
    supplier: str                 # exact CIN7 supplier name (= po_drafts.supplier)
    short: str                    # vendor short name for messages/docs
    label: str                    # order label used in CIN7 notes / Slack headers
    service_prefixes: tuple       # SKU prefixes that are vendor service lines
    channel_id: str               # Slack channel for pick lists / docs / `done`
    fallback_service_sku: Optional[str] = None   # charge this if a BOM has no service line
    default_price: Optional[float] = None        # last-resort unit price for the PO line
    odoo: bool = False            # raise Odoo lead + quote on PO authorise
    pack_labels: bool = False     # 2.25x1.25in pack labels PDF
    vendor_sheet: bool = False    # vendor instruction sheet PDF
    what_vendor_does: str = ""    # one-liner for Slack/PDF text

    def is_service(self, sku) -> bool:
        s = str(sku or "").strip().upper()
        return any(s.startswith(p) for p in self.service_prefixes)


FABLAB = Flow(
    key="fablab",
    supplier="865FabLab",
    short="865FabLab",
    label="865FabLab corner assembly",
    service_prefixes=("OSC-865FABLAB",),
    channel_id=os.environ.get("SLACK_FABLAB_CORNER_CHANNEL_ID", "C0BU77GC3SS"),
    fallback_service_sku="OSC-865FABLAB-JOINT",
    default_price=15.0,
    odoo=True,
    pack_labels=True,
    vendor_sheet=False,
    what_vendor_does="assembles the corners",
)

FINISHING = Flow(
    key="finishing",
    supplier="All Star Metal Finishers",
    short="All Star",
    label="All Star finishing",
    service_prefixes=("OSC-POWDERCOAT", "OSC-ANODIZING"),
    channel_id=os.environ.get("SLACK_FINISHING_CHANNEL_ID", "C0C0NLLBQMQ"),
    fallback_service_sku=None,     # a finished SKU without a service line is a BOM error
    default_price=None,
    odoo=False,
    pack_labels=False,
    vendor_sheet=True,
    what_vendor_does="powder coats / anodizes the raw profiles",
)

FLOWS: dict[str, Flow] = {f.key: f for f in (FABLAB, FINISHING)}


def is_any_service(sku) -> bool:
    """True if the SKU is a vendor service line in ANY flow. Service lines
    are Non-Inventory in CIN7 — never pick them, never count them as
    material. (A corner BOM can carry both a JOINT line and a powder-coat
    line; both are services.)"""
    return any(f.is_service(sku) for f in FLOWS.values())


def flow_for_supplier(supplier) -> Optional[Flow]:
    s = str(supplier or "").strip().lower()
    for f in FLOWS.values():
        if f.supplier.lower() == s:
            return f
    return None


def flow_for_draft(draft) -> Flow:
    """Flow for a po_drafts row (dict-like). Unknown supplier → FABLAB, the
    original behaviour, so legacy corner orders keep working."""
    try:
        sup = draft["supplier"]
    except (KeyError, TypeError, IndexError):
        sup = None
    return flow_for_supplier(sup) or FABLAB


# ---------------------------------------------------------------------------
# Finishing service SKU vocabulary
#   OSC-POWDERCOAT-{BK|WH|SL}-{SML|LRG}-FT   per foot of profile
#   OSC-POWDERCOAT-{BK|WH|SL}-POST           per post plate
#   OSC-ANODIZING-{BK|SL}-{SML|LRG}-FT       per foot of profile
#   OSC-ANODIZING-{BK|SL}-EC-{SML|LRG}       per end cap
# ---------------------------------------------------------------------------

_COLOURS = {"BK": "Black Matt", "WH": "White Matt", "SL": "Silver"}   # powder coat
_ANOD_COLOURS = {"BK": "Black", "SL": "Silver"}                        # anodize
_SIZES = {"SML": "small profile", "LRG": "large profile"}
_PROCESS = {"POWDERCOAT": "Powder coat", "ANODIZING": "Anodize"}
_FIN_RE = re.compile(
    r"^OSC-(?P<proc>POWDERCOAT|ANODIZING)-(?P<col>BK|WH|SL)"
    r"(?:-(?P<a>SML|LRG|EC|POST))?(?:-(?P<b>FT|SML|LRG))?$", re.I)


def parse_finishing_service(sku) -> Optional[dict]:
    """Decode a finishing service SKU into words for the vendor sheet.
    Returns {process, colour, size, unit, unit_label} or None if the SKU
    is not one of the OSC finishing codes."""
    m = _FIN_RE.match(str(sku or "").strip().upper())
    if not m:
        return None
    proc = _PROCESS[m.group("proc").upper()]
    col = m.group("col").upper()
    colour = _COLOURS[col] if proc == "Powder coat" else _ANOD_COLOURS.get(col, _COLOURS[col])
    a, b = (m.group("a") or "").upper(), (m.group("b") or "").upper()
    size, unit = "", ""
    if a in _SIZES:
        size = _SIZES[a]
        unit = "ft" if b == "FT" else ""
    elif a == "EC":
        size = _SIZES.get(b, "")
        unit = "end cap"
    elif a == "POST":
        unit = "post plate"
    unit_label = {"ft": "per foot", "end cap": "per end cap",
                  "post plate": "per post plate"}.get(unit, "")
    return {"process": proc, "colour": colour, "size": size, "unit": unit,
            "unit_label": unit_label}


def describe_finishing_service(sku, name: str = "") -> str:
    """'Powder coat · Black Matt · large profile · per foot' or the CIN7
    name when the SKU is not in the OSC vocabulary."""
    p = parse_finishing_service(sku)
    if not p:
        return name or str(sku or "")
    bits = [p["process"], p["colour"]]
    if p["size"]:
        bits.append(p["size"])
    if p["unit_label"]:
        bits.append(p["unit_label"])
    return " · ".join(bits)
