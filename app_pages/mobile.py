"""📱 Mobile page (RULES §9.13) — phone-sized view for buyers on the go.

James 2026-09-24: "Go build option 2" — a separate Mobile page with big
cards instead of grids. Four tabs:
  🎯 Buy       Buying Priority as cards (same engine rows as that page)
  🔍 SKU       look up one SKU: stock, goal, open POs, lead time, image
  ✅ Approve   Cin7 draft POs waiting to be authorised (links open Cin7)
               + in-app PO / FabLab / Finishing orders not yet placed
  📈 Metrics   headline Monthly Metrics tiles (MTD vs last month)

Read-only by design: authorising POs stays in Cin7, and placing orders
stays on the desktop pages. Every number comes from an existing source;
helpers live in engine/mobile_summary.py.
"""

from __future__ import annotations

from typing import Callable, Optional

import pandas as pd
import streamlit as st

from engine.buying_priority import build_priority_rows, build_vendor_summary
from engine import mobile_summary as ms

NAV_REQUEST_KEY = "_nav_request"
FLOW_PAGES = {
    "865fablab": "865FabLab Production",
    "all star metal finishers": "Finishing Work Orders",
}

_MOBILE_CSS = """
<style>
/* Mobile page only: big tap targets, tight padding, no wasted width. */
.block-container {padding-top: 1.2rem; padding-left: .8rem;
                  padding-right: .8rem;}
div[data-testid="stButton"] button,
div[data-testid="stLinkButton"] a {min-height: 2.8rem; font-size: 1rem;}
div[data-testid="stMetricValue"] {font-size: 1.45rem;}
div[data-testid="stTabs"] button p {font-size: 1rem;}
/* Keep 2-up tile rows side by side on phones (Streamlit stacks
   columns below 640px by default). */
div[data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important;
                                      gap: .6rem;}
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
  min-width: 0 !important; flex: 1 1 0 !important;
  width: auto !important;}
@media (max-width: 640px) {
  h2 {font-size: 1.35rem !important;}
  div[data-testid="stMetricValue"] {font-size: 1.25rem;}
}
</style>
"""


def is_mobile_user_agent(ua: str) -> bool:
    ua = (ua or "").lower()
    if "ipad" in ua:
        return False
    return any(k in ua for k in ("iphone", "android", "mobile", "ipod"))


def request_page(page: str, supplier: str = "") -> None:
    st.session_state[NAV_REQUEST_KEY] = {"page": page,
                                         "supplier": supplier}


def _money(v) -> str:
    return ms.fmt_value(v, "money")


def _md(text: str) -> str:
    return str(text).replace("$", "\\$")


def _qty(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.2f}"


# ------------------------------------------------------------------ Buy

def _render_buy(engine_df: pd.DataFrame) -> None:
    rows = build_priority_rows(engine_df)
    if rows.empty:
        st.success("Nothing needs buying right now.")
        return
    t = rows["tier"]
    c1, c2 = st.columns(2)
    c1.metric("🚨 Backorder, no PO", f"{int((t == 1).sum())}")
    c2.metric("🔴 Reorder now", f"{int((t == 2).sum())}")
    c3, c4 = st.columns(2)
    c3.metric("🟠 Reorder soon", f"{int((t == 3).sum())}")
    c4.metric("Suggested buy", _money(rows["suggested_value"].sum()))

    vendors = build_vendor_summary(rows)
    show_all = st.toggle("Show every vendor", key="mob_buy_all")
    shown = vendors if show_all else vendors.head(10)
    for _, v in shown.iterrows():
        sup = str(v["Supplier"])
        chips = []
        if v["backorder_skus"]:
            chips.append(f"🚨 {int(v['backorder_skus'])}")
        if v["now_skus"]:
            chips.append(f"🔴 {int(v['now_skus'])}")
        if v["soon_skus"]:
            chips.append(f"🟠 {int(v['soon_skus'])}")
        label = (f"{sup} · {_money(v['suggested_value'])} · "
                 + " ".join(chips))
        with st.expander(_md(label), expanded=False):
            if v["backorder_value"] > 0:
                st.caption("Customers waiting: "
                           + _md(_money(v["backorder_value"])))
            items = rows[rows["Supplier"] == sup].head(15)
            for _, r in items.iterrows():
                name = str(r.get("Name", "") or "")
                st.markdown(
                    f"{r['tier_label'].split(' ')[0]} **`{r['SKU']}`** "
                    f"— buy **{_qty(r['suggested_qty'])}** "
                    f"({_md(_money(r['suggested_value']))})  \n"
                    f"<small>{_md(name[:70])} · avail "
                    f"{_qty(r['available'])} · on PO "
                    f"{_qty(r['on_order'])}</small>",
                    unsafe_allow_html=True)
            left = int((rows["Supplier"] == sup).sum()) - len(items)
            if left > 0:
                st.caption(f"+{left} more on Ordering")
            st.button("Open in Ordering →", key=f"mob_buy_{sup}",
                      on_click=request_page, args=("Ordering", sup),
                      width="stretch")
    if not show_all and len(vendors) > 10:
        st.caption(f"{len(vendors) - 10} more vendors — turn on "
                   "'Show every vendor'.")


# ------------------------------------------------------------------ SKU

def _render_sku(engine_df: pd.DataFrame,
                image_lookup: Callable[[], dict],
                open_po_lines: Callable[[str], dict]) -> None:
    q = st.text_input("SKU or product name", key="mob_sku_q",
                      placeholder="e.g. LED-C7020001-2 or begton white")
    if not q.strip():
        st.caption("Type part of a SKU or name. Several words narrow it "
                   "down (all must match).")
        return
    hits = ms.search_skus(engine_df, q)
    if hits.empty:
        st.warning("No match.")
        return
    if len(hits) > 1:
        opts = [f"{r['SKU']} — {str(r.get('Name', '') or '')[:50]}"
                for _, r in hits.iterrows()]
        pick = st.selectbox(f"{len(hits)} matches", opts,
                            key="mob_sku_pick")
        row = hits.iloc[opts.index(pick)]
    else:
        row = hits.iloc[0]
    c = ms.sku_card(row)

    with st.container(border=True):
        img = (image_lookup() or {}).get(c["sku"])
        if img:
            st.image(img, width=160)
        st.markdown(f"**`{c['sku']}`**  \n{_md(c['name'])}")
        st.caption(" · ".join(x for x in (
            c["supplier"], f"Class {c['abc']}" if c["abc"] else "",
            c["status"]) if x))
        a, b = st.columns(2)
        a.metric("Available", _qty(c["available"]),
                 help="On hand minus allocated to open sales.")
        b.metric("On PO", _qty(c["on_order"]))
        a, b = st.columns(2)
        a.metric("On hand", _qty(c["on_hand"]))
        b.metric("Allocated", _qty(c["allocated"]))
        if c["backorder"] > 0:
            st.error(f"Backorder: {_qty(c['backorder'])} more allocated "
                     "than on hand.")
        a, b = st.columns(2)
        a.metric("Stock goal", _qty(c["goal_units"]))
        b.metric("Suggested buy", _qty(c["reorder_qty"]))
        a, b = st.columns(2)
        a.metric("Lead time",
                 f"{c['lead_time_days']:.0f}d" if c["lead_time_days"]
                 else "—")
        a2 = ("no demand" if c["cover_days"] is None
              else f"{c['cover_days']:,.0f}d")
        b.metric("Cover", a2, help="(Available + On PO) ÷ daily demand.")
        st.caption(f"Sold 12 mo: {_qty(c['units_12mo'])} · "
                   f"~{_qty(round(c['avg_month'], 1))}/mo · unit cost "
                   + _md(_money(c["unit_cost"])))

    res = open_po_lines(c["sku"]) or {}
    lines = res.get("open_purchase_lines") or []
    if lines:
        head = "**Open POs**"
        if res.get("is_parent_fallback"):
            head += f" (master roll `{res.get('parent_sku')}`)"
        st.markdown(head)
        for ln in lines[:8]:
            qty = ln.get("quantity_remaining")
            if qty is None or (isinstance(qty, float) and pd.isna(qty)):
                qty = ln.get("quantity_on_order")
            due = str(ln.get("expected_date") or "")
            due = "" if due in ("", "nan", "not available") else due[:10]
            st.markdown(f"- {ln.get('po_number') or ''} · "
                        f"{_md(ln.get('supplier') or '')} · qty {_qty(qty)}"
                        + (f" · due {due}" if due else ""))
    elif c["on_order"] > 0:
        st.caption("Cin7 shows stock on order, but the PO line is older "
                   "than the local sync window — check Cin7.")
    if c["supplier"]:
        st.button("Open vendor in Ordering →", key="mob_sku_open",
                  on_click=request_page, args=("Ordering", c["supplier"]),
                  width="stretch")


# ------------------------------------------------------------------ Approve

def _render_approve(cin7_drafts: pd.DataFrame,
                    local_drafts: list[dict],
                    visible_pages: list[str]) -> None:
    st.markdown("**Draft POs in Cin7**")
    st.caption("Waiting to be authorised (POs touched in the last 30 "
               "days). Tap to open in Cin7 and authorise there.")
    if cin7_drafts is None or cin7_drafts.empty:
        st.success("No draft POs in Cin7.")
    else:
        for _, p in cin7_drafts.iterrows():
            age = p.get("age_days")
            age_s = f" · {int(age)}d old" if pd.notna(age) else ""
            st.link_button(
                f"{p.get('OrderNumber', '')} · {p.get('Supplier', '')}"
                f"{age_s}",
                p["url"], width="stretch")

    st.markdown("**Orders not yet placed (in the app)**")
    editing = [d for d in local_drafts if d.get("status") == "editing"]
    if not editing:
        st.success("No unplaced app orders.")
        return
    for d in editing:
        sup = str(d.get("supplier") or "")
        page = FLOW_PAGES.get(sup.lower(), "Ordering")
        who = d.get("created_by") or ""
        when = str(d.get("created_at") or "")[:10]
        with st.container(border=True):
            st.markdown(f"**{_md(d.get('name') or sup)}**  \n"
                        f"<small>{_md(sup)} · started {when}"
                        f"{' by ' + _md(who) if who else ''}</small>",
                        unsafe_allow_html=True)
            if page in visible_pages:
                st.button(
                    f"Open in {page} →", key=f"mob_draft_{d.get('id')}",
                    on_click=request_page,
                    args=(page, sup if page == "Ordering" else ""),
                    width="stretch")


# ------------------------------------------------------------------ Metrics

def _render_metrics(mm: Optional[dict]) -> None:
    tiles = ms.headline_tiles(mm)
    if not tiles:
        st.info("Monthly Metrics hasn't been published yet.")
        return
    st.caption(f"{tiles[0]['month']} month-to-date vs "
               f"{tiles[0]['prev_month']} (full month). "
               f"Channel: {mm.get('channel', '')}. Published "
               f"{str(mm.get('generated_at', ''))[:16].replace('T', ' ')}.")
    for i in range(0, len(tiles), 2):
        cols = st.columns(2)
        for col, t in zip(cols, tiles[i:i + 2]):
            col.metric(t["label"], ms.fmt_value(t["current"], t["format"]))
            col.caption(f"last month {ms.fmt_value(t['previous'], t['format'])}")


# ------------------------------------------------------------------ page

def render_mobile(*, engine_df_fn: Callable[[], pd.DataFrame],
                  image_lookup: Callable[[], dict],
                  open_po_lines: Callable[[str], dict],
                  cin7_drafts_fn: Callable[[], pd.DataFrame],
                  local_drafts_fn: Callable[[], list],
                  metrics_fn: Callable[[], Optional[dict]],
                  can_buy: bool, can_metrics: bool,
                  visible_pages: list[str]) -> None:
    st.markdown(_MOBILE_CSS, unsafe_allow_html=True)
    st.header("📱 Mobile")
    labels = []
    if can_buy:
        labels += ["🎯 Buy", "🔍 SKU", "✅ Approve"]
    if can_metrics:
        labels += ["📈 Metrics"]
    if not labels:
        st.info("Nothing on this page is enabled for your profile.")
        return
    # segmented_control renders only the chosen section (tabs would run
    # all four on every tap — too slow on a phone).
    pick = st.segmented_control("Section", labels, default=labels[0],
                                key="mob_section",
                                label_visibility="collapsed") or labels[0]
    if pick == "🎯 Buy":
        _render_buy(engine_df_fn())
    elif pick == "🔍 SKU":
        _render_sku(engine_df_fn(), image_lookup, open_po_lines)
    elif pick == "✅ Approve":
        _render_approve(cin7_drafts_fn(), local_drafts_fn(),
                        visible_pages)
    elif pick == "📈 Metrics":
        _render_metrics(metrics_fn())
