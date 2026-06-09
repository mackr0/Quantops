"""2026-06-09 (post-reset) — brain badge matched by (symbol, action_class).

Pre-fix: `api_cycle_data` enrichment built `drop_by_symbol` keyed
on the symbol alone. A single drop on NU contaminated ALL
trades_selected entries on NU (BUY, MULTILEG_OPEN, OPTIONS),
producing the operator-visible bug where a successful BUY rendered
"GATED · ERROR" because a sibling MULTILEG_OPEN proposal hit a
strike-snap collision.

Post-fix: drops are classified by their `drop_code` (and reason
text for ERROR/SKIP) into action surfaces — "stock", "multileg",
"option", or "any" (cross-cutting gates like CATASTROPHIC,
KILL_SWITCH). The trades_selected entry's action is classified
identically. Badge only applies when the surfaces match (or the
drop is "any" / cross-cutting).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — _drop_action_class buckets
# ---------------------------------------------------------------------------


class TestDropActionClass:

    def test_multileg_open_drop_code_is_multileg(self):
        from views import _drop_action_class
        assert _drop_action_class("MULTILEG_OPEN") == "multileg"
        assert _drop_action_class("MULTILEG_CLOSE") == "multileg"

    def test_error_drop_with_multileg_reason_is_multileg(self):
        """ERROR drops carry the surface in the reason. A combo
        rejection that mentions 'bull_put_spread' or 'Multi-leg
        build' must classify as multileg even though the code is
        just 'ERROR'."""
        from views import _drop_action_class
        assert _drop_action_class(
            "ERROR",
            "Strike-snap collision: bull_put_spread on NU collapsed",
        ) == "multileg"
        assert _drop_action_class(
            "ERROR",
            "Multi-leg build/submit failed: upper strike must be > lower",
        ) == "multileg"
        assert _drop_action_class(
            "ERROR",
            "Combo rejected with duplicate-leg symbol",
        ) == "multileg"

    def test_catastrophic_is_cross_cutting_any(self):
        """CATASTROPHIC_SINGLE_TRADE and other risk-gate drops
        legitimately apply to ANY action on the symbol — they're
        pre-broker per-position dollar checks, not action-specific."""
        from views import _drop_action_class
        assert _drop_action_class(
            "CATASTROPHIC_SINGLE_TRADE", "") == "any"
        assert _drop_action_class("KILL_SWITCH", "") == "any"
        assert _drop_action_class(
            "BOOK_CONCENTRATION_CAP", "") == "any"

    def test_unrecognized_drop_defaults_to_stock(self):
        """Unknown drop_codes with no multileg-keyword reason
        default to stock — the most common path."""
        from views import _drop_action_class
        assert _drop_action_class("SKIP", "Already short NU") == "stock"
        assert _drop_action_class(
            "SPECIALIST_VETOED", "concentration exposure") == "stock"


# ---------------------------------------------------------------------------
# Layer 2 — _action_class_for_trades_selected handles raw + humanized
# ---------------------------------------------------------------------------


class TestActionClassForTradesSelected:

    def test_raw_buy_is_stock(self):
        from views import _action_class_for_trades_selected
        assert _action_class_for_trades_selected("BUY") == "stock"
        assert _action_class_for_trades_selected("STRONG_BUY") == "stock"
        assert _action_class_for_trades_selected("SHORT") == "stock"

    def test_raw_multileg_open_is_multileg(self):
        from views import _action_class_for_trades_selected
        assert _action_class_for_trades_selected(
            "MULTILEG_OPEN") == "multileg"
        assert _action_class_for_trades_selected(
            "MULTILEG_CLOSE") == "multileg"

    def test_humanized_multileg_open_is_multileg(self):
        """`humanize()` runs BEFORE the badge enrichment in
        api_cycle_data — so the action string we receive is already
        title-cased: 'Multileg Open' not 'MULTILEG_OPEN'. Must still
        classify correctly."""
        from views import _action_class_for_trades_selected
        assert _action_class_for_trades_selected(
            "Multileg Open") == "multileg"

    def test_options_action_is_option(self):
        from views import _action_class_for_trades_selected
        assert _action_class_for_trades_selected("OPTIONS") == "option"


# ---------------------------------------------------------------------------
# Layer 3 — source pin
# ---------------------------------------------------------------------------


def test_enrichment_keys_by_symbol_and_action():
    """Source-level pin on views.py — the drop index must be keyed
    on (symbol, action_class), not symbol alone. A refactor that
    drops back to symbol-only re-introduces the bug class."""
    src = (REPO_ROOT / "views.py").read_text()
    # Anchor on the enrichment block's helper invocation
    anchor = src.find("drop_by_symbol_action")
    assert anchor > 0, (
        "drop_by_symbol_action anchor missing — refactor must keep "
        "this name or update this pin."
    )
    # The match lookup must use the action class
    assert "_action_class_for_trades_selected" in src, (
        "Match lookup must classify the trades_selected entry's "
        "action via _action_class_for_trades_selected. Without it "
        "the badge degenerates back to symbol-only matching."
    )
    assert "_drop_action_class" in src, (
        "Drop classification function _drop_action_class must be "
        "called when indexing drops. Without it every drop falls "
        "into the same bucket and contaminates other surfaces."
    )
