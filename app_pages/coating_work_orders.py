"""Finishing Work Orders — outsourced powder coating & anodizing at All Star.

Same mechanism as 865FabLab Production (James, 2026-09-10), minus Odoo:

  plan  → candidates are every SKU whose CIN7 BOM carries an All Star
          service line (OSC-POWDERCOAT-* / OSC-ANODIZING-*). Suggested
          batch = weeks-of-cover demand − on hand − WIP, whole units.
  order → tick, set batch qty, create/save the order (po_drafts, supplier
          "All Star Metal Finishers").
  place → one AUTHORISED CIN7 assembly per finished SKU (raw profile
          reserved, pick list on the assembly) + one Draft PO to All Star
          with the service SKUs × feet / end caps; memo = end product,
          colour, qty.
  go    → James authorises the PO in CIN7 → worker posts each assembly to
          #powdercoating-anodize-control with the pick list PDF and the
          All Star instruction sheet PDF.
  back  → stores reply `done` (or `done 35`) in the assembly's thread, or
          complete it in the Receiving section below; finished stock lands,
          raw profile + service line are consumed.

All the order / place / receiving widgets are the corner page's helpers
(app_pages.fablab_work_orders) called with flow=FINISHING.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from app_pages.fablab_work_orders import (
    _num, _rows_by_sku, _stock_by_sku, bom_service_skus,
    build_materials_rollup, build_planner_table,
    _render_assembly_receiving, _render_bom_reality_check,
    _render_draft_lifecycle, _render_order_docs, _render_place_order,
)
from outsource_flows import FINISHING, parse_finishing_service

FINISHING_SUPPLIER = FINISHING.supplier
# 2026-09-25 (James): finishing SKUs selling < 1/month are not built for
# stock; they only appear when there is an open order / backorder.
FINISHING_MIN_MONTHLY_FOR_STOCK = 1.0
_LEGACY_SERVICE_CATEGORIES = ("Services - Powder Coating", "Services - Anodizing")


# ── Helpers ──────────────────────────────────────────────────────────────

def finishing_columns(sku: str, bom_parents: dict, product_map: dict) -> dict[str, Any]:
    """Process / colour / service / raw-profile text for one finished SKU
    from its BOM (service lines decoded via outsource_flows)."""
    procs, colours, svc_bits, raw_bits = [], [], [], []
    for comp in bom_parents.get(sku, []) or []:
        csku = str(comp.get("ComponentSKU") or "").strip()
        per = _num(comp.get("Quantity"))
        if not csku or per <= 0:
            continue
        if FINISHING.is_service(csku):
            p = parse_finishing_service(csku)
            if p:
                procs.append(p["process"])
                colours.append(p["colour"])
            svc_bits.append(f"{csku} × {per:g}")
        elif not str(csku).upper().startswith("OSC-"):
            raw_bits.append(f"{csku} × {per:g}")
    prod = product_map.get(sku, {}) or {}
    auto = str(prod.get("AutoAssembly", "")).strip().lower() == "true"
    return {
        "Process": " + ".join(dict.fromkeys(procs)) or "—",
        "Colour": " + ".join(dict.fromkeys(colours)) or "—",
        "Service (BOM)": ", ".join(svc_bits),
        "Raw profile": ", ".join(raw_bits) or "—",
        "Auto-assembly": auto,
    }


def legacy_finishing_skus(products: pd.DataFrame, bom_parents: dict) -> pd.DataFrame:
    """Finished SKUs whose BOM uses an old finishing service SKU (category
    Services - Powder Coating / Anodizing but not OSC-*). Not planned here
    until the BOM is moved to the OSC service SKUs (James, 2026-09-10)."""
    if products is None or products.empty or "Category" not in products.columns:
        return pd.DataFrame()
    cat = dict(zip(products["SKU"].astype(str), products["Category"].astype(str)))
    rows = []
    for sku, comps in (bom_parents or {}).items():
        old = [str(c.get("ComponentSKU") or "") for c in comps
               if str(cat.get(str(c.get("ComponentSKU") or ""), "")).startswith(
                   _LEGACY_SERVICE_CATEGORIES)
               and not FINISHING.is_service(c.get("ComponentSKU"))]
        if old:
            rows.append({"Finished SKU": sku, "Legacy service SKU(s)": ", ".join(old),
                         "Category": cat.get(str(sku), "")})
    return pd.DataFrame(rows).sort_values(["Category", "Finished SKU"]) if rows else pd.DataFrame()


def _render_setup_notes(planner_df: pd.DataFrame, products: pd.DataFrame,
                        bom_parents: dict) -> None:
    """Folded housekeeping: SKUs still on AutoAssembly and BOMs on legacy
    service SKUs. Never blocks the planner."""
    n_auto = int(planner_df["Auto-assembly"].sum()) if "Auto-assembly" in planner_df else 0
    legacy = legacy_finishing_skus(products, bom_parents)
    if not n_auto and legacy.empty:
        return
    with st.expander(
            f"⚠️ CIN7 setup — {n_auto} SKU(s) still auto-assemble, "
            f"{len(legacy)} on legacy service SKUs", expanded=False):
        if n_auto:
            st.markdown(
                f"**Auto-assembly is ON for {n_auto} finished SKU(s).** A web sale "
                "of one of these makes CIN7 consume the raw profile and the service "
                "line instantly, with no coating done — it fights the WIP shown here. "
                "Agreed with James (2026-09-10): switch it off for SKUs in this flow "
                "(`scripts/finishing_autoassembly_off.py`).")
            st.dataframe(planner_df.loc[planner_df["Auto-assembly"], ["SKU", "Name", "Process"]],
                         hide_index=True, use_container_width=True)
        if not legacy.empty:
            st.markdown(
                f"**{len(legacy)} finished SKU(s) use legacy service SKUs** "
                "(PowderCoatGen, ANODIZE-SMOKIES*, …). They are not in this planner "
                "until their BOM points at an `OSC-POWDERCOAT-*` / `OSC-ANODIZING-*` line.")
            st.dataframe(legacy, hide_index=True, use_container_width=True)


def _render_receiving(actor: str) -> None:
    import db

    st.markdown("### \U0001f4e5 Receiving")
    st.caption(
        "When coated / anodized stock is back from All Star: reply `done` "
        "(or `done 35`) in the assembly's Slack thread, or complete it here.")
    placed = [
        d for d in db.list_po_drafts(supplier=FINISHING_SUPPLIER, include_archived=True)
        if d["status"] in ("submitted", "finalized")
    ]
    if not placed:
        st.info("No placed finishing orders yet.")
        return
    opt_to_id = {f"{d['name']} (#{d['id']}, {d['status']})": d["id"] for d in placed}
    picked = st.selectbox("Order", list(opt_to_id.keys()),
                          key="finishing_receiving_picker")
    draft_id = opt_to_id[picked]
    if _render_assembly_receiving(draft_id, actor, flow=FINISHING):
        _render_bom_reality_check()
    else:
        st.warning("This order has no CIN7 assemblies — it was not placed "
                   "through this page.")


# ── Main render ──────────────────────────────────────────────────────────

def render_finishing_work_orders(
    *,
    products: pd.DataFrame,
    stock: pd.DataFrame,
    engine_df: pd.DataFrame,
    bom_parents: dict,
    fmt_number,
    fmt_money,
) -> None:
    import db

    st.header("\U0001f3a8 Finishing Work Orders")
    st.info(
        "**Outsourced powder coating and anodizing at All Star — you supply "
        "the raw profiles, All Star finishes them.** Pick or create an order, "
        "tick Include on the SKUs you want (Batch qty is pre-filled with the "
        "suggestion — edit if needed), save, check raw stock, place the "
        "order. Each SKU becomes a CIN7 assembly and one PO goes to All Star. "
        "Authorise the PO in CIN7 and the pick list + instruction sheet land "
        "in the finishing Slack channel; when the stock is back, reply `done` "
        "in its thread.",
        icon="ℹ️",
    )

    if not bom_parents:
        st.warning("No BOM data loaded. Run the CIN7 BOM sync first.")
        return

    current_user = st.session_state.get("current_user", "").strip() or "anonymous"
    product_map = _rows_by_sku(products)
    excluded = set(db.all_do_not_reorder_skus() or [])
    candidates = sorted(s for s in bom_service_skus(bom_parents, FINISHING)
                        if s not in excluded)
    if not candidates:
        st.info("No SKU has an `OSC-POWDERCOAT-*` / `OSC-ANODIZING-*` service "
                "line in its CIN7 BOM yet.")
        return

    # ── Plan & order ─────────────────────────────────────────────────────
    st.markdown("### \U0001f4cb Plan & order")
    draft_id, can_edit, is_submitted = _render_draft_lifecycle(current_user, flow=FINISHING)
    saved_lines: dict = db.get_po_draft_lines(draft_id) if draft_id else {}

    pc1, pc2, pc3, pc4 = st.columns([2, 2, 2, 3])
    with pc1:
        weeks_cover = st.number_input(
            "Weeks of cover per batch", min_value=1.0, max_value=16.0,
            value=8.0, step=1.0, key="finishing_weeks_cover",
            help="Suggested batch tops up on-hand + WIP to this many weeks of "
                 "forecast demand. Finishing turnaround is slower than corners, "
                 "so the default is 8 weeks.")
    with pc2:
        action_only = st.checkbox(
            "Action needed only", value=True, key="finishing_action_only",
            help="Hide SKUs with no suggested batch and nothing on the order.")
        pretick_all = st.checkbox(
            "Pre-tick all suggested", value=False,
            key=f"finishing_pretick_{draft_id or 'none'}")
    with pc3:
        process_filter = st.selectbox(
            "Process", ["All", "Powder coat", "Anodize"], key="finishing_process")
    with pc4:
        search = st.text_input("Search SKU, name, colour", key="finishing_search")

    try:
        wip_map = db.fablab_wip_by_sku()
    except Exception:  # noqa: BLE001
        wip_map = {}
    planner_df = build_planner_table(
        candidates, products, stock, engine_df, bom_parents, weeks_cover,
        wip_map=wip_map, min_monthly_for_stock=FINISHING_MIN_MONTHLY_FOR_STOCK)
    if planner_df.empty:
        st.warning("No data for finishing SKUs.")
        return
    planner_df = planner_df.drop(columns=["BOM rule"], errors="ignore").copy()
    extra = pd.DataFrame([finishing_columns(s, bom_parents, product_map)
                          for s in planner_df["SKU"]], index=planner_df.index)
    planner_df = pd.concat([planner_df, extra], axis=1)
    try:  # RULES 9.15 — stock-outs in the last 12 months
        from app_pages import stockout_data as _sod
        planner_df = _sod.attach(planner_df)
    except Exception:  # noqa: BLE001
        pass

    remembered: dict = st.session_state.get("finishing_ticked", {})
    planner_df["Batch qty"] = [
        float(saved_lines.get(sku, remembered.get(sku, sug if pd.notna(sug) else 0)))
        for sku, sug in zip(planner_df["SKU"], planner_df["Suggested batch"])]
    planner_df["Include"] = [
        (sku in saved_lines) or (sku in remembered)
        or (pretick_all and pd.notna(sug) and float(sug) > 0)
        for sku, sug in zip(planner_df["SKU"], planner_df["Suggested batch"])]
    order = ["Include", "SKU", "Name", "Process", "Colour", "ABC", "Status",
             "On hand", "Open SO", "Backorder", "WIP", "WIP ref", "Last 6 months",
             "Stock-outs 12 mo", "Monthly demand", "Suggested batch",
             "Batch qty", "Raw profile", "Buildable from stock", "Materials status",
             "Materials", "Service (BOM)", "Auto-assembly"]
    # Slow sellers (< 1/mo) drop off unless something is owed or in hand.
    slow_hidden = (
        planner_df["Below min demand"].fillna(False).astype(bool)
        & (planner_df["Open SO"].fillna(0) <= 0)
        & (planner_df["Backorder"].fillna(0) <= 0)
        & (planner_df["WIP"].fillna(0) <= 0)
        & (planner_df["Batch qty"].fillna(0) <= 0)
    ) if "Below min demand" in planner_df.columns else pd.Series(
        False, index=planner_df.index)
    n_slow_hidden = int(slow_hidden.sum())
    planner_df = planner_df[~slow_hidden]
    planner_df = planner_df[[c for c in order if c in planner_df.columns]]

    view = planner_df
    if process_filter != "All":
        view = view[view["Process"].str.contains(process_filter, na=False)]
    if action_only:
        view = view[(view["Suggested batch"].fillna(0) > 0)
                    | (view["Batch qty"].fillna(0) > 0)
                    | (view["WIP"].fillna(0) > 0)]
    if search:
        q = search.strip()
        mask = pd.Series(False, index=view.index)
        for col in ("SKU", "Name", "Colour", "Process", "Raw profile"):
            mask |= view[col].astype(str).str.contains(q, case=False, na=False, regex=False)
        view = view[mask]
    view = view.copy()

    if n_slow_hidden:
        st.caption(f"{n_slow_hidden} SKUs selling under 1/month with no open "
                   "order or backorder are hidden (not built for stock).")

    qty_editable = (draft_id is None) or can_edit
    if draft_id and is_submitted:
        st.caption("This order has already been placed — quantities are read-only.")
        _render_order_docs(draft_id, flow=FINISHING)
    elif draft_id and not can_edit:
        st.caption("Take the lock above to edit quantities.")
    edited = st.data_editor(
        view,
        key=f"finishing_planner_editor_{draft_id or 'none'}_{int(pretick_all)}_{process_filter}",
        use_container_width=True, hide_index=True,
        disabled=[c for c in view.columns
                  if c not in ("Batch qty", "Include") or not qty_editable],
        column_config={
            "Include": st.column_config.CheckboxColumn(
                "✔ Include", help="Tick to put this SKU on the order."),
            "On hand": st.column_config.NumberColumn(format="%.1f"),
            "Open SO": st.column_config.NumberColumn(
                "📦 Open SO", format="%d",
                help="Units on open (authorised, unshipped) sales orders in CIN7, "
                     "backorders included. Added to the target."),
            "WIP": st.column_config.NumberColumn(
                "🎨 WIP", format="%d",
                help="Already out at All Star (open assemblies not yet marked "
                     "done). Counted as covered."),
            "Backorder": st.column_config.NumberColumn(
                "⏳ Backorder", format="%d",
                help="Units on backorder in CIN7 (sold, not in stock)."),
            "Last 6 months": st.column_config.TextColumn(
                "Last 6 months",
                help="Units sold in each of the last 6 calendar months — "
                     "oldest on the left, current month on the right. "
                     "Same numbers as the Ordering page.",
                width="medium"),
            "Stock-outs 12 mo": st.column_config.TextColumn(
                "Stock-outs 12 mo", width="small",
                help="Last 12 months: times out of stock, days out in "
                     "brackets, customer orders placed while out. "
                     "Source: Inventory Planner stock history."),
            "Monthly demand": st.column_config.NumberColumn(format="%.2f"),
            "Suggested batch": st.column_config.NumberColumn(
                format="%d", help="Target + open SO − on hand − WIP, whole units. "
                                  "Under 1/month: open SO / backorder only."),
            "Buildable from stock": st.column_config.NumberColumn(
                "Raw covers", format="%.1f",
                help="How many can be made from raw profile on hand."),
            "Batch qty": st.column_config.NumberColumn(
                "✏ Batch qty (order)", format="%d", step=1, min_value=0),
            "Auto-assembly": st.column_config.CheckboxColumn(
                "Auto-asm", help="CIN7 AutoAssembly still on (see setup notes below)."),
            "Materials": st.column_config.TextColumn("Raw needed / on hand"),
        },
    )

    included = edited[edited["Include"].fillna(False).astype(bool)]
    if qty_editable:
        visible = set(edited["SKU"])
        remembered = {k: v for k, v in remembered.items() if k not in visible}
        remembered.update({r["SKU"]: float(_num(r.get("Batch qty", 0)))
                           for _, r in included.iterrows()})
        st.session_state["finishing_ticked"] = remembered

    n_short = int((included["Materials status"] == "Raw short").sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Finishing SKUs", fmt_number(len(planner_df)))
    m2.metric("On this order",
              f"{fmt_number(included['Batch qty'].fillna(0).sum())} pcs · {len(included)} SKUs")
    m3.metric("Raw short (included)", fmt_number(n_short))
    m4.metric("Out at All Star (WIP)", fmt_number(int(planner_df["WIP"].fillna(0).sum())))

    def _save_ticks(target_draft: int) -> int:
        n = 0
        for _, row in edited.iterrows():
            qty = _num(row.get("Batch qty", 0))
            if bool(row.get("Include", False)) and qty > 0:
                db.upsert_po_draft_line(target_draft, row["SKU"], qty, current_user)
                n += 1
            elif row["SKU"] in saved_lines:
                db.delete_po_draft_line(target_draft, row["SKU"])
        st.session_state["finishing_ticked"] = {}
        return n

    n_ticked = len(included)
    if draft_id is None:
        if n_ticked == 0:
            st.info("**Step 1** — tick the SKUs to send in the ✔ Include column, "
                    "then name and create the order.", icon="\U0001f4cb")
        else:
            st.info(f"**Step 2** — {n_ticked} SKU(s) ticked. Name the order and "
                    "create it; the ticked items go straight onto it.", icon="\U0001f4e6")
        nc1, nc2 = st.columns([3, 2])
        with nc1:
            new_name = st.text_input("Order name", key="finishing_quick_order_name",
                                     placeholder="e.g. September black powder coat")
        with nc2:
            st.write("")
            label = (f"\U0001f4e6 Create order with {n_ticked} ticked item(s)"
                     if n_ticked else "\U0001f4e6 Create empty order")
            if st.button(label, key="finishing_quick_create", type="primary",
                         disabled=not new_name.strip()):
                new_draft = db.create_po_draft(
                    supplier=FINISHING_SUPPLIER, name=new_name.strip(), actor=current_user)
                n = _save_ticks(new_draft)
                st.session_state["finishing_active_draft"] = new_draft
                st.success(f"Order #{new_draft} created with {n} SKU(s).")
                st.rerun()
    elif can_edit:
        if not saved_lines:
            st.info("**Step 2** — tick items, then save them to this order.", icon="\U0001f4e6")
        if st.button("\U0001f4be Save ticked items to this order",
                     key=f"finishing_save_{draft_id}", type="primary",
                     disabled=(n_ticked == 0 and not saved_lines)):
            n = _save_ticks(draft_id)
            st.success(f"Saved {n} SKU(s) to the order.")
            st.rerun()

    if draft_id and saved_lines:
        with st.expander(f"Lines saved on this order ({len(saved_lines)} SKUs)"):
            st.dataframe(pd.DataFrame([
                {"SKU": sku, "Name": product_map.get(sku, {}).get("Name", ""),
                 "Colour": finishing_columns(sku, bom_parents, product_map)["Colour"],
                 "Qty": qty} for sku, qty in saved_lines.items()]),
                use_container_width=True, hide_index=True)
    if draft_id and can_edit and saved_lines:
        st.info("**Step 3** — review below and place the order with All Star.",
                icon="\U0001f680")
        _render_place_order(draft_id, bom_parents, product_map, current_user,
                            stock_map=_stock_by_sku(stock), flow=FINISHING)

    # ── Raw profile shortfall ────────────────────────────────────────────
    st.divider()
    st.markdown("### \U0001f9f1 Raw profile shortfall")

    def _order_qtys(df: pd.DataFrame) -> dict:
        inc = df["Include"].fillna(False).astype(bool)
        return dict(zip(df["SKU"], df["Batch qty"].fillna(0).where(inc, 0.0)))
    batch_qtys = _order_qtys(planner_df)
    batch_qtys.update(_order_qtys(edited))
    rollup_df = build_materials_rollup(candidates, batch_qtys, bom_parents,
                                       _stock_by_sku(stock))
    if rollup_df.empty:
        st.success("No raw profile needed for the current batch quantities.")
    else:
        short_rollup = rollup_df[rollup_df["Short by"] > 0]
        if short_rollup.empty:
            st.success("Enough raw profile on hand for this batch.")
        else:
            st.warning(f"Short on {len(short_rollup)} raw profile(s) — order or "
                       "cut these before placing the All Star batch:")
        st.dataframe(rollup_df, use_container_width=True, hide_index=True)

    # ── Receiving ────────────────────────────────────────────────────────
    st.divider()
    _render_receiving(current_user)

    # ── Setup notes (folded) ─────────────────────────────────────────────
    st.divider()
    _render_setup_notes(planner_df, products, bom_parents)


# Backwards-compatible name used by app.py before 2026-09-10.
render_anodizing_powder_coating = render_finishing_work_orders

__all__ = ["render_finishing_work_orders", "render_anodizing_powder_coating",
           "finishing_columns", "legacy_finishing_skus"]
