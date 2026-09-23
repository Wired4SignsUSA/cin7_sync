"""Buying › Buying Priority page (RULES §9.11).

One cross-vendor view of what needs buying: uncovered backorders first,
then engine reorder priority. Every vendor row has a button that opens
the Ordering page on that vendor. Ranking lives in
engine/buying_priority.py; this module only renders.
"""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from engine.buying_priority import (
    TIER_LABELS,
    build_priority_rows,
    build_vendor_summary,
)

NAV_REQUEST_KEY = "_nav_request"
VENDORS_SHOWN = 12


def request_ordering_for_supplier(supplier: str) -> None:
    """Button callback: ask the sidebar to open Ordering on `supplier`.

    Consumed at the top of the sidebar navigation block (page switch) and
    at the Ordering supplier picker (vendor preselect)."""
    st.session_state[NAV_REQUEST_KEY] = {
        "page": "Ordering",
        "supplier": str(supplier or ""),
    }


def _money(v) -> str:
    """Plain dollars — for st.metric values (not markdown)."""
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _money_md(v) -> str:
    """Escaped dollars — for captions/markdown."""
    return _money(v).replace("$", "\\$")


def _qty(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.2f}"


def _cover_label(days) -> str:
    try:
        d = float(days)
    except (TypeError, ValueError):
        return "—"
    if math.isinf(d):
        return "no demand"
    return f"{d:,.0f}d"


def _vendor_card(v: pd.Series, rows: pd.DataFrame, key_prefix: str) -> None:
    sup = str(v["Supplier"])
    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 3, 2], vertical_alignment="center")
        with c1:
            st.markdown(f"**{sup}**")
            chips = []
            if v["backorder_skus"]:
                chips.append(f"🚨 {int(v['backorder_skus'])} backorder")
            if v["now_skus"]:
                chips.append(f"🔴 {int(v['now_skus'])} now")
            if v["soon_skus"]:
                chips.append(f"🟠 {int(v['soon_skus'])} soon")
            st.caption("  ·  ".join(chips))
        with c2:
            top = rows[rows["Supplier"] == sup].head(3)
            st.caption("Top: " + ", ".join(
                f"`{s}`" for s in top["SKU"].astype(str)))
            if v["backorder_value"] > 0:
                st.caption(
                    f"Customers waiting: {_money_md(v['backorder_value'])}")
        with c3:
            st.metric("Suggested buy", _money(v["suggested_value"]),
                      label_visibility="visible")
            st.button(
                "Open in Ordering →",
                key=f"{key_prefix}_{sup}",
                on_click=request_ordering_for_supplier,
                args=(sup,),
                type="primary" if v["worst_tier"] == 1 else "secondary",
                width="stretch",
            )


def render_buying_priority(*, engine_df: pd.DataFrame, fold_note) -> None:
    st.header("🎯 Buying Priority")
    rows = build_priority_rows(engine_df)
    if rows.empty:
        st.success("Nothing needs buying right now — no uncovered "
                   "backorders and no engine reorder suggestions.")
        return
    vendors = build_vendor_summary(rows)

    t = rows["tier"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🚨 Backorders not on a PO", f"{int((t == 1).sum())} SKUs",
              help="Customers are waiting and open POs don't cover the "
                   "shortfall.")
    m2.metric("🔴 Reorder now", f"{int((t == 2).sum())} SKUs")
    m3.metric("🟠 Reorder soon", f"{int((t == 3).sum())} SKUs")
    m4.metric("Suggested buy (all vendors)",
              _money(rows["suggested_value"].sum()),
              help=f"{len(vendors)} vendors. Engine suggestions at cost; "
                   "the Ordering page may round for MOQ/pack rules.")
    fold_note(
        "Order of the list: 1) SKUs on customer backorder that no open "
        "PO covers (biggest customer exposure first); 2) engine "
        "'Reorder now' items; 3) 'Reorder soon' items. Within 2 and 3: "
        "A-class first, then fewest days of cover. Quantities and "
        "statuses are the same engine figures the Ordering page uses, "
        "for master SKUs only. Do-not-reorder SKUs are excluded; "
        "dropship and discontinued SKUs appear only if a customer "
        "backorder is uncovered.",
        title="ℹ️ How this list is ranked",
    )

    st.markdown("#### Vendors to order from")
    for _, v in vendors.head(VENDORS_SHOWN).iterrows():
        _vendor_card(v, rows, "bp_open")
    if len(vendors) > VENDORS_SHOWN:
        with st.expander(
                f"{len(vendors) - VENDORS_SHOWN} more vendors",
                expanded=False):
            for _, v in vendors.iloc[VENDORS_SHOWN:].iterrows():
                _vendor_card(v, rows, "bp_open_more")

    st.markdown("#### Every item, in priority order")
    f1, f2 = st.columns([2, 3])
    with f1:
        tier_pick = st.multiselect(
            "Show", options=list(TIER_LABELS.values()),
            default=list(TIER_LABELS.values()), key="bp_tiers")
    with f2:
        vendor_pick = st.selectbox(
            "Vendor", ["All vendors"] + list(vendors["Supplier"]),
            key="bp_vendor")
    view = rows[rows["tier_label"].isin(tier_pick)]
    if vendor_pick != "All vendors":
        view = view[view["Supplier"] == vendor_pick]

    table = pd.DataFrame({
        "#": view["priority_rank"],
        "Priority": view["tier_label"],
        "SKU": view["SKU"].astype(str),
        "Name": view.get("Name", pd.Series("", index=view.index)),
        "Vendor": view["Supplier"],
        "Class": view["class"],
        "Available": view["available"].round(2),
        "On PO": view["on_order"].round(2),
        "Backorder not on PO": view["backorder_uncovered"].round(2),
        "Cover": view["cover_days"].map(_cover_label),
        "Lead time": view["lead_time_days"].map(
            lambda d: f"{d:,.0f}d" if d else "—"),
        "Suggested qty": view["suggested_qty"].round(2),
        "Suggested $": view["suggested_value"].round(0),
    })
    event = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=min(38 + 35 * len(table), 560),
        on_select="rerun",
        selection_mode="single-row",
        key="bp_table",
        column_config={
            "Suggested $": st.column_config.NumberColumn(format="$%d"),
            "Name": st.column_config.TextColumn(width="medium"),
        },
    )
    picked = None
    try:
        sel = event.selection.rows if event else []
        if sel:
            picked = table.iloc[sel[0]]
    except Exception:  # noqa: BLE001
        picked = None
    if picked is not None:
        st.button(
            f"Open {picked['Vendor']} in Ordering → (for {picked['SKU']})",
            key="bp_open_selected",
            on_click=request_ordering_for_supplier,
            args=(picked["Vendor"],),
            type="primary",
        )
    else:
        st.caption("Tip: click a row to jump to its vendor on Ordering.")
