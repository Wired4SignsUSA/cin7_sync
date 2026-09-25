"""Stock-out risk list (RULES 9.16, James 2026-09-25).

Morning list for the buyer: stocked SKUs (goal > 0, not dropship, not
Project) that are

* OUT  — Available <= 0 and nothing on order, or
* WILL RUN OUT — Available > 0 but it will not last the lead time and
  nothing is on order (a PO placed today would land after it runs out).

Finishing (All Star) and 865FabLab corner SKUs are listed separately:
they are replenished by a build, not a supplier PO.

Pure functions only (no Streamlit / DB) so the worker job
(`stockout_risk_alert.py`) and tests share one implementation.
"""
from __future__ import annotations

from typing import Callable, Mapping

import pandas as pd

DEFAULT_LEAD_TIME_DAYS = 35
BUILD_LEAD_TIME_DAYS = 14   # finishing / corner builds
MAX_LINES_PER_SECTION = 25
_CLASS_RANK = {"A": 0, "B": 1, "C": 2}


def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def lead_time_days(sku: str, supplier: str, *,
                   supplier_cfgs: Mapping[str, Mapping] | None = None,
                   ip_lead_times: Mapping[str, Mapping] | None = None,
                   sku_lead_times: Mapping[str, float] | None = None) -> int:
    """Same precedence as the Ordering engine: SKU setting > IP observed
    > IP configured > supplier air (if air by default) / sea > 35d."""
    s_lt = _num((sku_lead_times or {}).get(sku))
    if 1 <= s_lt <= 365:
        return int(s_lt)
    ip = (ip_lead_times or {}).get(sku) or {}
    for key in ("observed_lead_time_days", "configured_lead_time_days"):
        v = _num(ip.get(key))
        if 3 <= v <= 120:
            return int(v)
    norm = " ".join(str(supplier or "").split()).strip()
    cfg = (supplier_cfgs or {}).get(norm) or {}
    air = _num(cfg.get("lead_time_air_days"))
    if cfg.get("air_eligible_default") and air > 0:
        return int(air)
    sea = _num(cfg.get("lead_time_sea_days"))
    return int(sea) if sea > 0 else DEFAULT_LEAD_TIME_DAYS


def risk_table(engine_df: pd.DataFrame, *,
               lead_time_fn: Callable[[str, str], int],
               stockouts_12mo: Mapping[str, int] | None = None,
               build_skus: Mapping[str, str] | None = None,
               build_suppliers: Mapping[str, str] | None = None
               ) -> pd.DataFrame:
    """One row per at-risk SKU, most urgent first.

    build_skus: {sku: "Finishing" | "Corners"} for build-replenished SKUs.
    build_suppliers: {supplier name: flow label} — same tag by supplier
    (e.g. 865FabLab SKUs whose BOM has no service line).
    """
    cols = ["SKU", "Name", "ABCD", "Supplier", "status", "available",
            "backordered", "daily", "days_left", "lead_time", "stockouts_12mo",
            "build"]
    if engine_df is None or engine_df.empty:
        return pd.DataFrame(columns=cols)
    df = engine_df
    goal = pd.to_numeric(df.get("goal_units"), errors="coerce").fillna(0)
    exempt = df.get("excess_exempt", pd.Series(False, index=df.index))
    exempt = exempt.fillna(False).astype(str).str.lower().isin(("true", "1"))
    trend = df.get("trend_flag", pd.Series("", index=df.index)).astype(str)
    keep = (goal > 0) & ~exempt & ~trend.str.contains("Project")
    df = df[keep]
    so = stockouts_12mo or {}
    builds = build_skus or {}
    b_sup = {str(k).strip().lower(): v for k, v in (build_suppliers or {}).items()}
    out = []
    for _, r in df.iterrows():
        sku = str(r.get("SKU") or "")
        on_order = _num(r.get("OnOrder"))
        if on_order > 0:
            continue
        avail = r.get("Available")
        avail = (_num(avail) if avail is not None and avail == avail
                 else _num(r.get("OnHand")) - _num(r.get("Allocated")))
        daily = max(_num(r.get("planning_avg_daily")), _num(r.get("avg_daily")))
        supplier = str(r.get("Supplier") or "")
        build = builds.get(sku, "") or b_sup.get(supplier.strip().lower(), "")
        lt = BUILD_LEAD_TIME_DAYS if build else int(lead_time_fn(sku, supplier))
        if avail <= 0:
            status = "out"
            days_left = 0.0
        else:
            if daily <= 0:
                continue
            days_left = avail / daily
            if days_left >= lt:
                continue
            status = "will_run_out"
        out.append({
            "SKU": sku, "Name": str(r.get("Name") or ""),
            "ABCD": str(r.get("ABCD") or ""), "Supplier": supplier,
            "status": status, "available": max(avail, 0.0),
            "backordered": _num(r.get("unfulfilled")), "daily": daily,
            "days_left": days_left, "lead_time": lt,
            "stockouts_12mo": int(so.get(sku, 0) or 0), "build": build,
        })
    t = pd.DataFrame(out, columns=cols)
    if t.empty:
        return t
    t["_s"] = (t["status"] != "out").astype(int)
    t["_c"] = t["ABCD"].map(_CLASS_RANK).fillna(3)
    t = t.sort_values(["_s", "_c", "backordered", "days_left"],
                      ascending=[True, True, False, True])
    return t.drop(columns=["_s", "_c"]).reset_index(drop=True)


def _line(r) -> str:
    bits = [f"`{r['SKU']}`", r["ABCD"] or "—"]
    if r["build"]:
        bits.append(r["build"])
    elif r["Supplier"]:
        bits.append(r["Supplier"][:28])
    if r["status"] == "out":
        bits.append("*out*")
    else:
        bits.append(f"{round(r['available'], 1):g} left ≈ {r['days_left']:.0f}d, "
                    f"lead time {r['lead_time']}d")
    if r["backordered"] > 0:
        bits.append(f"*{round(r['backordered'], 1):g} backordered*")
    if r["stockouts_12mo"] >= 2:
        bits.append(f"ran out {r['stockouts_12mo']}× in 12 mo")
    return "• " + " · ".join(bits)


def format_message(t: pd.DataFrame, app_url: str = "",
                   max_lines: int = MAX_LINES_PER_SECTION) -> str:
    if t is None or t.empty:
        return ("*🚨 Stock-out risk* — nothing to flag today: every stocked "
                "item has stock that lasts the lead time, or a PO on order.")
    buy = t[t["build"] == ""]
    build = t[t["build"] != ""]
    n_out = int((buy["status"] == "out").sum())
    n_soon = int((buy["status"] == "will_run_out").sum())
    head = (f"*🚨 Stock-out risk — nothing on order* · {n_out} out now · "
            f"{n_soon} will run out before a PO could land")
    if not build.empty:
        head += f" · {len(build)} finishing/corner builds needed"
    parts = [head]

    def _section(title: str, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        parts.append(f"\n*{title} ({len(rows)})*")
        parts.extend(_line(r) for _, r in rows.head(max_lines).iterrows())
        if len(rows) > max_lines:
            parts.append(f"_+{len(rows) - max_lines} more — see Ordering_")

    _section("Out now", buy[buy["status"] == "out"])
    _section("Will run out before a PO lands", buy[buy["status"] == "will_run_out"])
    _section("Finishing / corner builds (Finishing & 865FabLab pages)", build)
    tail = ("\nOrdered A → C, backorders first. Stocked items only — "
            "dropship, Project and made-to-order kits are left out.")
    if app_url:
        tail += f" <{app_url}|Open the app › Buying Priority / Ordering>"
    parts.append(tail)
    return "\n".join(parts)
