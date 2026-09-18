"""Trend / dormancy helper rules shared by the ABC engine.

Pure functions, no pandas, so they are cheap to unit-test and can be
reused by the Slack bot. The engine in app.py calls these from its
row-wise classifiers; RULES.md §3 documents the buyer-facing meaning.

Labels
------
STABLE   — sold in at least 4 of the last 6 calendar months. Steady,
           repeatable baseline; the 12mo rate is trusted.
SPORADIC — has diversified history (3+ buyers over 12mo, no single
           buyer >50%) but sold in 3 or fewer of the last 6 months, or
           restarted from a zero prior-45d window with only 1-2 recent
           buyers. Real product, lumpy demand: the engine plans on the
           LOWER of the 12mo and last-6mo rates so a big historic
           month cannot drive a reorder on its own.
PROJECT  — concentrated one-off demand (1-2 buyers). Unchanged.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

STABLE = "Stable"
SPORADIC = "⚡ Sporadic"
PROJECT = "🎯 Project"
TREND = "📈 Trend"
MIXED = "🔀 Mixed"
DECLINE = "📉 Decline"
DORMANT = "💤 Dormant"

# "Stable" must have sold in at least this many of the last 6 months.
STABLE_MIN_ACTIVE_MONTHS_6 = 4
# Spike gate used by the 45d classifier (kept here so tests and the
# momentum helper agree on the boundary).
SPIKE_MOMENTUM = 1.5
# 90d-vs-12mo soft-dormancy threshold (share of the 12mo daily rate).
SOFT_DORMANCY_RATIO = 0.20


def momentum(units_45d: float, units_prior_45d: float) -> float:
    """45d / prior-45d ratio.

    A restart from a zero prior window is an *infinite* jump and must
    clear the spike gate (``> SPIKE_MOMENTUM``); returning exactly 1.5
    here (the pre-2026-09-18 behaviour) silently prevented every
    "nothing → one customer" restart from being decomposed into
    Project/Sporadic and left it labelled Stable.
    """
    u45 = float(units_45d or 0.0)
    prior = float(units_prior_45d or 0.0)
    if prior > 0:
        return u45 / max(prior, 1.0)
    return math.inf if u45 > 0 else 1.0


def is_restart(units_45d: float, units_prior_45d: float) -> bool:
    return float(units_prior_45d or 0) <= 0 and float(units_45d or 0) > 0


def active_months(buckets: Optional[Sequence[float]], last: int = 6) -> int:
    """Number of the most recent ``last`` monthly buckets with sales."""
    if not buckets:
        return 0
    vals = list(buckets)[-last:]
    n = 0
    for v in vals:
        try:
            if float(v or 0) > 0:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def sum_last(buckets: Optional[Sequence[float]], last: int = 6) -> float:
    if not buckets:
        return 0.0
    total = 0.0
    for v in list(buckets)[-last:]:
        try:
            total += float(v or 0)
        except (TypeError, ValueError):
            continue
    return total


def restart_label(customers_12mo: int, top_share_12mo: float) -> str:
    """Label for a spike concentrated to 1-2 recent buyers.

    Diversified history (3+ buyers, no one >50% of the year) means the
    product is real but lumpy → SPORADIC. Otherwise a genuine one-off
    → PROJECT (velocity discounts the top customer as before).
    """
    if int(customers_12mo or 0) >= 3 and float(top_share_12mo or 0) < 0.5:
        return SPORADIC
    return PROJECT


def is_sporadic_months(buckets: Optional[Sequence[float]]) -> bool:
    """True when the SKU sold in fewer than 4 of the last 6 months."""
    if not buckets or sum_last(buckets, 6) <= 0:
        return False
    return active_months(buckets, 6) < STABLE_MIN_ACTIVE_MONTHS_6


def a_class_grace_holds(abc: str,
                        eff_12mo: float,
                        eff_90d: float,
                        customers_45d: int,
                        top_share_12mo: Optional[float] = None,
                        active_months_6: Optional[int] = None) -> bool:
    """Should an A-class SKU be exempt from soft-dormancy?

    ABC is a $-value rank, not a steadiness signal, so the exemption
    needs evidence that demand is still alive rather than "one straggler
    sale in 90 days":
      * 2+ distinct buyers in the last 45d, OR
      * 90d rate still ≥20% of the 12mo rate (no cliff), OR
      * sales in 3+ of the last 6 months (when the caller has buckets).
    and never when one customer took ≥50% of the year.
    """
    if str(abc or "C").strip().upper() != "A":
        return False
    eff_12mo = float(eff_12mo or 0)
    eff_90d = float(eff_90d or 0)
    if eff_12mo <= 0 or eff_90d <= 0:
        return False
    if top_share_12mo is not None and float(top_share_12mo) >= 0.5:
        return False
    if int(customers_45d or 0) >= 2:
        return True
    rate_12 = eff_12mo / 365.0
    rate_90 = eff_90d / 90.0
    if rate_12 > 0 and rate_90 >= SOFT_DORMANCY_RATIO * rate_12:
        return True
    if active_months_6 is not None and int(active_months_6) >= 3:
        return True
    return False


def sporadic_daily_rate(eff_12mo: float,
                        last_6mo_units: float,
                        days_in_last_6mo: float,
                        window_days: int = 365) -> float:
    """Planning rate for a SPORADIC SKU = lower of 12mo and last-6mo rate."""
    r12 = float(eff_12mo or 0) / max(int(window_days or 365), 1)
    d6 = max(float(days_in_last_6mo or 0), 1.0)
    r6 = float(last_6mo_units or 0) / d6
    return max(0.0, min(r12, r6))


def evidence_text(customers_45d: int,
                  active_months_6: int,
                  top_share_12mo: float,
                  customers_12mo: int) -> str:
    """Short, checkable evidence shown next to the Trend badge."""
    c45 = int(customers_45d or 0)
    parts = [f"{c45} buyer{'s' if c45 != 1 else ''}/45d",
             f"sold {int(active_months_6 or 0)}/6 mo"]
    share = float(top_share_12mo or 0)
    if share >= 0.3:
        parts.append(f"top buyer {share:.0%} of 12mo")
    else:
        parts.append(f"{int(customers_12mo or 0)} buyers/12mo")
    return " · ".join(parts)
