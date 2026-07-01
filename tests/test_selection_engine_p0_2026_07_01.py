"""Selection engine P0 — option predictions classify distinctly (2026-07-01).

Option opens (MULTILEG_OPEN / OPTIONS) used to fall through to
prediction_type='directional_long', conflating every option outcome with stock
longs — which corrupts the per-expression stats + meta-model attribution the
risk-adjusted selection engine depends on. They now classify as 'option_open'.
Resolution is unaffected (options resolve P&L-wise via the option_resolver,
keyed on the signal, not this label). See docs/SELECTION_ENGINE_DESIGN.md.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from ai_tracker import classify_prediction_type


def test_option_opens_are_not_directional_long():
    assert classify_prediction_type("MULTILEG_OPEN") == "option_open"
    assert classify_prediction_type("OPTIONS") == "option_open"
    assert classify_prediction_type("OPTION_EXERCISE") == "option_open"
    # the P0 invariant
    assert classify_prediction_type("MULTILEG_OPEN") != "directional_long"


def test_stock_and_exit_classification_unchanged():
    assert classify_prediction_type("BUY") == "directional_long"
    assert classify_prediction_type("SHORT") == "directional_short"
    assert classify_prediction_type("HOLD") == "directional_long"
    assert classify_prediction_type("SELL", held_qty=10) == "exit_long"
    assert classify_prediction_type("SELL", held_qty=-10) == "exit_short"
    # SELL on something we don't hold = directional bearish
    assert classify_prediction_type("SELL", held_qty=0) == "directional_short"


def test_case_insensitive_and_none_safe():
    assert classify_prediction_type("multileg_open") == "option_open"
    assert classify_prediction_type(None) == "directional_long"
    assert classify_prediction_type("") == "directional_long"
