"""Structural guardrail: when a position is being managed by the
trailing-stop / conviction-TP-override logic (not the displayed
fixed take-profit), the UI MUST communicate that explicitly so the
operator doesn't see a position past its target with no exit and
assume a bug.

The bug class.
A position opens with `take_profit=$290.12`. Profile has
`use_conviction_tp_override=1` and the entry's `ai_confidence=72`
(>= the 70 threshold). The conviction-TP override therefore lets
the trailing stop manage the exit — the position can run past
$290.12. The dashboard displays the static `take_profit=$290.12`
without context. Operator sees position at $300.87 (+9.9%, target
exceeded) with no sale and concludes the system has a bug.

The fix is structural: every open position the dashboard renders
must carry an `exit_logic` field describing which exit path is
active. The template uses this to display the correct framing
(strikethrough fixed target + "LET WINNERS RUN" badge when
trailing-stop manages exit).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestResolveExitLogic:
    def test_conviction_override_active_above_threshold(self):
        from views import _resolve_exit_logic
        ctx = SimpleNamespace(
            use_conviction_tp_override=1,
            conviction_tp_min_confidence=70.0,
        )
        meta = {"ai_confidence": 72.0}
        out = _resolve_exit_logic(ctx, meta)
        assert out["kind"] == "conviction_trailing", (
            f"Confidence 72 above threshold 70 must enable conviction "
            f"override; got {out}"
        )
        assert out["fixed_target_active"] is False
        assert "trailing" in out["label"].lower()

    def test_conviction_override_inactive_below_threshold(self):
        from views import _resolve_exit_logic
        ctx = SimpleNamespace(
            use_conviction_tp_override=1,
            conviction_tp_min_confidence=70.0,
        )
        meta = {"ai_confidence": 65.0}
        out = _resolve_exit_logic(ctx, meta)
        assert out["kind"] == "fixed", (
            f"Confidence 65 below threshold 70 must NOT enable "
            f"override; got {out}"
        )
        assert out["fixed_target_active"] is True

    def test_override_setting_off(self):
        from views import _resolve_exit_logic
        ctx = SimpleNamespace(
            use_conviction_tp_override=0,
            conviction_tp_min_confidence=70.0,
        )
        meta = {"ai_confidence": 90.0}  # well above threshold
        out = _resolve_exit_logic(ctx, meta)
        assert out["kind"] == "fixed", (
            f"Profile setting use_conviction_tp_override=0 must "
            f"force fixed-target regardless of confidence; got {out}"
        )

    def test_no_ai_confidence_metadata(self):
        """Position with missing ai_confidence (e.g. legacy row,
        manual entry) must not crash — falls back to fixed."""
        from views import _resolve_exit_logic
        ctx = SimpleNamespace(
            use_conviction_tp_override=1,
            conviction_tp_min_confidence=70.0,
        )
        out = _resolve_exit_logic(ctx, {})  # no ai_confidence
        assert out["kind"] == "fixed"

    def test_no_ctx(self):
        from views import _resolve_exit_logic
        out = _resolve_exit_logic(None, {"ai_confidence": 80.0})
        assert out["kind"] == "fixed"

    def test_template_branches_on_exit_logic_kind(self):
        """The trades table template must consult t.exit_logic.kind
        when rendering the Target line. Without this, the rendering
        falls back to a fixed display and the operator misses that
        the trailing stop is actually in charge."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates", "_trades_table.html",
        )
        with open(path) as f:
            src = f.read()
        # The template must reference exit_logic AND the
        # conviction_trailing kind, both of which are required for
        # the LET WINNERS RUN badge to render.
        assert "exit_logic" in src, (
            "templates/_trades_table.html doesn't reference "
            "exit_logic — the dashboard will display stale fixed "
            "targets for positions managed by the trailing stop."
        )
        assert "conviction_trailing" in src, (
            "templates/_trades_table.html doesn't branch on "
            "exit_logic.kind == 'conviction_trailing' — operator "
            "will see fixed target with no badge for "
            "trailing-stop-managed positions."
        )
        assert "LET WINNERS RUN" in src or "trailing stop" in src.lower(), (
            "Expected a 'LET WINNERS RUN' or 'trailing stop' badge "
            "in the rendering."
        )
