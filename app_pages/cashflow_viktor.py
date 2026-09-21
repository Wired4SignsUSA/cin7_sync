"""Finance › Cashflow page — read-only view of the W4S Cashflow app.

The Cashflow app (Viktor Space, Convex backend) is the single source for
cash position, payables and the 13-week forecast. This page only *reads*
its summary endpoint and renders tiles/tables; approvals, holds, notes and
"Send to Cheran" live in the Cashflow app itself (link button below).

Config (Render env group `cin7-shared`):
  CASHFLOW_API_URL    e.g. https://focused-fox-809.convex.site
  CASHFLOW_API_TOKEN  read-only bearer token (SUMMARY_TOKEN on the Space)
  CASHFLOW_APP_URL    link target for the "Open Cashflow app" button
  CASHFLOW_LEGACY_PAGE=1 restores the old in-app forecast (app.py) instead.

If the endpoint is unreachable the page says so and nothing else breaks.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

DEFAULT_APP_URL = "https://preview-w4s-cashflow-w4susa.viktor.space"
_TIMEOUT_S = 20


def _cfg(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def legacy_page_enabled() -> bool:
    return _cfg("CASHFLOW_LEGACY_PAGE") in {"1", "true", "yes"}


def fetch_summary(base_url: str, token: str, horizon: int = 13) -> dict[str, Any]:
    """GET /api/summary from the Cashflow app. Raises on any failure."""
    url = f"{base_url.rstrip('/')}/api/summary"
    r = requests.get(
        url,
        params={"horizon": horizon},
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_summary(base_url: str, token: str, horizon: int) -> dict[str, Any]:
    return fetch_summary(base_url, token, horizon)


def _money(x: Optional[float], dash: str = "—") -> str:
    if x is None:
        return dash
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


_ET = ZoneInfo("America/New_York")


def _fmt_ts(iso: Optional[str]) -> str:
    """ISO timestamp → 'Mon 21 Sep 12:35 ET' (Render runs in UTC)."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).strftime("%a %d %b %H:%M ET")
    except ValueError:
        return iso[:16]


def forecast_frame(weeks: list[dict[str, Any]]) -> pd.DataFrame:
    """Tidy the forecast weeks into a display DataFrame (pure; unit-testable)."""
    rows = []
    for w in weeks:
        rows.append({
            "Week": w["weekStart"],
            "Opening": w["opening"],
            "Sales in": w["salesIn"],
            "Other in": w["otherIn"],
            "Bank / recurring": w["bankOut"],
            "Bills due": w["committedOut"],
            "Not yet invoiced": w["notInvoicedOut"],
            "Planned": w["plannedOut"],
            "Total out": w["totalOut"],
            "Closing": w["closing"],
            "Below floor": "⚠️" if w["belowFloor"] else "",
            "Bills": w["billCount"],
        })
    return pd.DataFrame(rows)


def _tiles(data: dict[str, Any]) -> None:
    cash = data.get("cash") or {}
    pay = data.get("payables") or {}
    fc = data.get("forecast") or {}
    appr = data.get("approvals") or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Cash available (bank, live)",
        _money(cash.get("total")),
        help="Chase + Pinnacle available balances via bank feed, "
             f"as of {_fmt_ts(cash.get('asOf'))}. Includes pending deposits.",
    )
    c2.metric(
        "Open QBO bills",
        _money(pay.get("openBillsValue")),
        f"{pay.get('openBills', 0)} bills",
        delta_color="off",
        help="Open QuickBooks bills after reconciliation marks and "
             "recurring-covered vendors are removed.",
    )
    c3.metric(
        "Overdue (QBO past due, unverified)",
        _money(pay.get("overdue")),
        f"{pay.get('overdueCount', 0)} bills",
        delta_color="off",
        help="Bills past their QBO due date that are not yet marked paid or "
             "matched to a Settle debit. Settle is the source for actual pay dates.",
    )
    c4.metric(
        "Approved, awaiting payment",
        _money(appr.get("approvedAwaitingPayment")),
        help="Bills James has approved in the Cashflow app that Cheran has "
             "not yet marked paid.",
    )

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Bills due next 7 days", _money(pay.get("dueNext7")))
    d2.metric("Bills due next 30 days", _money(pay.get("dueNext30")))
    d3.metric(
        "Cin7 on order (not yet billed)",
        _money(pay.get("cin7OnOrderValue")),
        f"{pay.get('cin7OnOrderCount', 0)} POs",
        delta_color="off",
        help="Open Cin7 purchase orders with no matching QBO bill yet. "
             "Information only — not counted as committed payables.",
    )
    low = fc.get("minClosing")
    floor = fc.get("cashFloor") or 0
    d4.metric(
        f"Lowest forecast close ({fc.get('minClosingWeek', '')})",
        _money(low),
        (f"{_money(low - floor)} vs ${floor:,.0f} floor" if low is not None else None),
        delta_color=("normal" if (low or 0) >= floor else "inverse"),
    )


def _forecast_table(data: dict[str, Any]) -> None:
    fc = data.get("forecast") or {}
    weeks = fc.get("weeks") or []
    if not weeks:
        st.info("No forecast rows returned.")
        return
    st.subheader(":bar_chart: 13-week cash forecast")
    st.caption(
        "Opening cash = Monday bank anchor, then rolls forward. "
        "**Bills due** = open QBO bills by due date. **Not yet invoiced** = "
        "top-up to the supplier run rate for invoices that have not arrived yet"
        + (" (on)." if fc.get("includeNotInvoiced") else " (off — pure due-date view).")
    )
    df = forecast_frame(weeks)
    money_cols = [c for c in df.columns if c not in {"Week", "Below floor", "Bills"}]
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={c: st.column_config.NumberColumn(c, format="$%,.0f") for c in money_cols},
    )
    chart = df[["Week", "Closing"]].set_index("Week")
    st.line_chart(chart, height=220)
    if fc.get("breachCount"):
        st.warning(
            f"{fc['breachCount']} week(s) close below the ${floor_fmt(fc)} cash floor. "
            "Review holds and approvals in the Cashflow app."
        )


def floor_fmt(fc: dict[str, Any]) -> str:
    return f"{(fc.get('cashFloor') or 0):,.0f}"


def _overdue_table(data: dict[str, Any]) -> None:
    rows = (data.get("payables") or {}).get("topOverdue") or []
    if not rows:
        return
    st.subheader(":receipt: Oldest past-due bills (QBO, unverified)")
    df = pd.DataFrame(rows).rename(columns={
        "vendor": "Vendor", "docNumber": "Doc #", "dueDate": "Due",
        "balance": "Balance", "daysPastDue": "Days past due",
    })
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={"Balance": st.column_config.NumberColumn("Balance", format="$%,.0f")},
    )


def render_cashflow_viktor() -> None:
    """Page body below the QBO connection block."""
    base_url = _cfg("CASHFLOW_API_URL")
    token = _cfg("CASHFLOW_API_TOKEN")
    app_url = _cfg("CASHFLOW_APP_URL", DEFAULT_APP_URL)

    top_l, top_r = st.columns([3, 1])
    with top_l:
        st.subheader(":bar_chart: Cashflow dashboard")
        st.caption(
            "Read-only mirror of the W4S Cashflow app — one set of numbers. "
            "Approve, hold, annotate and send payments to Cheran in the app."
        )
    with top_r:
        st.link_button(":moneybag: Open Cashflow app", app_url, width="stretch")
        if st.button(":arrows_counterclockwise: Refresh numbers", key="_cfv_refresh",
                     width="stretch"):
            _cached_summary.clear()

    if not base_url or not token:
        st.warning(
            "Cashflow app connection is not configured on this server "
            "(`CASHFLOW_API_URL` / `CASHFLOW_API_TOKEN`). Numbers are shown "
            "in the Cashflow app via the button above."
        )
        return

    try:
        data = _cached_summary(base_url, token, 13)
    except requests.RequestException as exc:
        st.error(
            "Cashflow app is unavailable right now, so no figures are shown "
            f"here. Use the **Open Cashflow app** button. ({type(exc).__name__})"
        )
        return
    except ValueError:
        st.error("Cashflow app returned an unreadable response. Try again shortly.")
        return

    _tiles(data)
    st.caption(
        f"Cashflow app snapshot {_fmt_ts(data.get('generatedAt'))} · "
        f"bills synced {_fmt_ts(_epoch_iso((data.get('payables') or {}).get('syncedAt')))} · "
        "cached up to 5 min — use Refresh numbers for live."
    )
    st.divider()
    _forecast_table(data)
    _overdue_table(data)


def _epoch_iso(ms: Optional[float]) -> Optional[str]:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None
