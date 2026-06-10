"""2026-06-10 — ensure_protective_stops must skip entries whose
parent order is order_class='bracket'.

Pre-fix (deployed yesterday): execute_trade started submitting
bracket entries, but the SDK's submit_order response doesn't
populate `.legs`. The code couldn't stamp child IDs onto the
journal row, so the next ensure_protective_stops sweep ran with
no own_protective_ids, called _cancel_stale_other_orders to clean
"stale" coverage, and CANCELLED the bracket's live TP (CCO entry
at 13:41 → bracket TP cancelled at 13:46; reproducible).

Two fixes:

  1. The submit path re-fetches with get_order(order_id, nested=True)
     to surface the bracket children before journal write. Tested
     in test_bracket_entry_2026_06_09.py.

  2. The sweep checks the entry's parent order_class; if it's
     'bracket', skip — the broker manages stop+TP atomically as
     OCO sub-orders. This file pins that contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_ensure_protective_stops_skips_bracket_parent_orders():
    """Source-code pin: the per-position loop in
    ensure_protective_stops must call api.get_order on the entry's
    parent order_id and skip placement when order_class is
    'bracket'. Without this skip, the sweep cancels the bracket's
    live OCO sub-orders within minutes."""
    src = (REPO_ROOT / "bracket_orders.py").read_text()
    fn_start = src.find("def ensure_protective_stops")
    assert fn_start > 0
    fn_end = src.find("\ndef ", fn_start + 1)
    body = src[fn_start:fn_end if fn_end > 0 else len(src)]
    assert "BRACKET SKIP" in body, (
        "Bracket-skip anchor comment missing — refactor must "
        "preserve the anchor or update this pin."
    )
    assert "api.get_order" in body, (
        "ensure_protective_stops must call api.get_order to read "
        "the parent order_class. Without this it can't tell "
        "bracket entries apart from legacy plain entries."
    )
    # The class check must be on order_class='bracket'
    assert '"bracket"' in body, (
        "The skip must be conditional on order_class == 'bracket'. "
        "Removing the literal would skip ALL entries, including "
        "legacy entries that legitimately need the sweep."
    )
    # And the actual continue must be present
    bracket_section = body[body.find("BRACKET SKIP"):
                            body.find("BRACKET SKIP") + 2500]
    assert "continue" in bracket_section, (
        "After detecting a bracket entry, the sweep must `continue` "
        "to skip placement. Without `continue`, the cancel + place "
        "logic still runs and destroys the bracket."
    )


def test_submit_path_uses_nested_true_for_legs():
    """Source pin on trade_pipeline.py — the BUY and SHORT bracket
    submits must re-fetch with get_order(order_id, nested=True) to
    surface child legs. Without nested=True the SDK returns None
    for `.legs` and the protective-id stamp silently fails."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Find both bracket-submit blocks (BUY + SHORT)
    n_occurrences = src.count("api.get_order(order.id, nested=True)")
    assert n_occurrences >= 2, (
        f"Expected at least 2 nested=True fetches (BUY + SHORT "
        f"bracket paths); found {n_occurrences}. Without this re-"
        f"fetch the SDK returns .legs=None at submit time and "
        f"protective IDs never reach the journal — the sweep then "
        f"cancels the bracket's live TP within minutes."
    )
