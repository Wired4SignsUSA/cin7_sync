"""Cached loaders for IP stock-out history (RULES 9.15).

Data: db.ip_stockout_events, refreshed daily by ip_stockouts.py.
Maths: engine/stockouts.py.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import db
from engine import stockouts as _so

COLUMN_LABEL = "Stock-outs 12 mo"
COLUMN_HELP = (
    "Last 12 months: times this SKU ran out of stock, total days out in "
    "brackets, then customer orders placed while it was out (could not "
    "ship at once). '· out now' = still out. Dropship and made-to-order "
    "kits excluded. Source: Inventory Planner daily stock history, "
    "refreshed each morning.")

# app.py registers a provider that adds order hits (needs sale lines +
# engine); without it we fall back to stock-out counts only.
_summary_provider = None


def set_summary_provider(fn) -> None:
    global _summary_provider
    _summary_provider = fn


@st.cache_data(ttl=3600, show_spinner=False, max_entries=1)
def load_events() -> pd.DataFrame:
    try:
        rows = db.list_ip_stockout_events()
    except Exception:  # noqa: BLE001 — table missing / DB hiccup
        rows = []
    return pd.DataFrame(rows, columns=["sku", "out_date", "back_date",
                                       "synced_at"])


@st.cache_data(ttl=3600, show_spinner=False, max_entries=1)
def load_summary() -> pd.DataFrame:
    return _so.summarise_12mo(load_events())


def attach(df: pd.DataFrame, sku_col: str = "SKU",
           label_col: str = COLUMN_LABEL) -> pd.DataFrame:
    try:
        summary = (_summary_provider() if _summary_provider is not None
                   else load_summary())
    except Exception:  # noqa: BLE001
        summary = pd.DataFrame()
    return _so.attach(df, summary, sku_col=sku_col, label_col=label_col)


def column_config(label: str = COLUMN_LABEL):
    return st.column_config.TextColumn(label, help=COLUMN_HELP,
                                       disabled=True, width="small")
