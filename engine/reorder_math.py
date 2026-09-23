"""Small reorder-math helpers shared by the dashboard engine.

The dashboard keeps most ordering logic in app.py today. These helpers are
kept Streamlit-free so the subtle quantity rules can be unit-tested without
importing the full app and loading CSV snapshots.
"""

from __future__ import annotations


BULK_RESIDUE_FLOOR_METRES = 5.0
UNIT_EPSILON = 1e-6
NEONICA_FRACTIONAL_ROLL_METRES = 100.0


def bulk_residue_floor_units(is_bulk_master: bool,
                             bulk_length_m: float,
                             *,
                             floor_metres: float = (
                                 BULK_RESIDUE_FLOOR_METRES)) -> float:
    """Return the bulk-roll quantity below which stock is just residue.

    Example: for a 100m master roll, 5m = 0.05 rolls. Anything below that
    is too small to buy/sell/plan around and should not create an overstock
    signal.
    """
    try:
        length = float(bulk_length_m or 0)
    except (TypeError, ValueError):
        length = 0.0
    if not is_bulk_master or length <= 0:
        return UNIT_EPSILON
    return max(UNIT_EPSILON, float(floor_metres) / length)


def normalise_planning_quantity(quantity,
                                *,
                                is_bulk_master: bool = False,
                                bulk_length_m: float = 0.0) -> float:
    """Treat tiny quantities as zero for planning/status purposes."""
    try:
        qty = float(quantity or 0)
    except (TypeError, ValueError):
        return 0.0
    floor = bulk_residue_floor_units(is_bulk_master, bulk_length_m)
    if abs(qty) < floor:
        return 0.0
    return qty


def excess_units_over_target(onhand,
                             target,
                             *,
                             is_bulk_master: bool = False,
                             bulk_length_m: float = 0.0) -> float:
    """Excess stock after ignoring non-actionable bulk-roll residue."""
    try:
        excess = max(0.0, float(onhand or 0) - float(target or 0))
    except (TypeError, ValueError):
        return 0.0
    return normalise_planning_quantity(
        excess,
        is_bulk_master=is_bulk_master,
        bulk_length_m=bulk_length_m,
    )


def fractional_bulk_order_allowed(supplier_name,
                                  is_bulk_master: bool,
                                  bulk_length_m,
                                  supplier_config: dict | None = None) -> bool:
    """Return whether a bulk roll can be ordered as a decimal quantity.

    James 2026-09-23: partial rolls of strip apply ONLY to Neonica Polska
    Sp. z o.o. (40m required -> 0.40 of a 100m roll). Every other supplier
    buys whole rolls, whatever its supplier config says (RULES 2.5.1).
    ``supplier_config`` is accepted for call-site compatibility only.
    """
    try:
        length_m = float(bulk_length_m or 0)
    except (TypeError, ValueError):
        length_m = 0.0
    if not is_bulk_master or length_m <= 0:
        return False
    return is_fractional_roll_supplier(supplier_name)


def is_fractional_roll_supplier(supplier_name) -> bool:
    """True for the only supplier that sells partial strip rolls (Neonica)."""
    supplier_key = " ".join(str(supplier_name or "").lower().split())
    return "neonica" in supplier_key


def round_to_pack_nearest(quantity, pack, *, min_qty=0.0) -> float:
    """Round a buy quantity to the NEAREST whole pack/roll (RULES 5.7 SKU EOQ).

    James 2026-09-23: roll/pack sizes (SKU EOQ) round to the nearest roll,
    not always up. Guard rails so rounding down never leaves us short:
      * halves round up (75m on a 50m roll -> 100m);
      * a positive need never rounds to zero (at least one pack);
      * never below ``min_qty`` (MOQ, or the lead-time + safety cover still
        needed) -- if nearest falls below it, round UP instead.
    Non-positive quantity or pack returns ``quantity`` unchanged.
    """
    import math
    try:
        qty = float(quantity or 0)
        size = float(pack or 0)
    except (TypeError, ValueError):
        return quantity
    if qty <= 0 or size <= 0:
        return quantity
    packs = max(1, math.floor(qty / size + 0.5 + 1e-9))
    rounded = packs * size
    try:
        floor_qty = float(min_qty or 0)
    except (TypeError, ValueError):
        floor_qty = 0.0
    if rounded < floor_qty - 1e-9:
        rounded = math.ceil((floor_qty - 1e-9) / size) * size
    return float(rounded)
