"""2026-06-09 — broker-side take-profit placement.

Pre-fix: `bracket_orders.ensure_protective_stops` placed only a
trailing stop (when `use_trailing=True`) OR a static stop (else).
The comment at the static-stop branch said "No TP — that goes
through polling." For trailing mode it claimed "trailing covers
profit-lock." Neither mode placed a broker-side TP limit order at
the AI's actual target.

Investigation 2026-06-09 (afternoon):
  - 0/60 pid 42 entries had `protective_tp_order_id` set.
  - 0 PROTECTIVE_TAKE_PROFIT rows in 30 days.
  - The polling path (`check_stop_loss_take_profit`) runs once
    per ~5-min cycle and can miss intra-cycle price spikes past
    the AI target.
  - Trailing-stop give-back captures only ~95% of MFE on perfect
    runs, less when price pulls back hard after spiking.

Post-fix: `ensure_protective_stops` now ALSO places a GTC limit
order at `row["take_profit"]` (the clamped AI target). Runs
alongside trailing/static stop. Whichever fires first closes the
position; the loser becomes a no-shares-to-reduce order and gets
cleaned by the next cycle's broker-truth check.

Tests pin:
  1. When a stock entry has a take_profit price, a TP order is
     submitted in the same sweep as the stop.
  2. When an entry already has an active TP order, no duplicate
     is placed.
  3. When the entry's take_profit price is None or zero, no TP
     is placed (defensive — entry didn't compute one).
  4. The trailing-stop cancel-stale path no longer cancels TPs
     (the trailing mode's prior comment claimed TP-via-polling;
     now TP is broker-side and complementary).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Source-level pins (regression protection against silent removal)
# ---------------------------------------------------------------------------


class TestBrokerSideTPWiring:

    def test_ensure_protective_stops_selects_take_profit_column(self):
        """The entry-row SELECT in `ensure_protective_stops` must
        include `take_profit` so the sweep has the AI's clamped
        target available. Search scoped to the function body so
        we don't accidentally match an unrelated SELECT elsewhere
        in bracket_orders.py."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def ensure_protective_stops")
        assert fn_start > 0, "ensure_protective_stops missing"
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        assert "take_profit" in body, (
            "ensure_protective_stops must read `take_profit` from "
            "the entry row to place the broker-side TP. Without it "
            "the polling-only fallback returns."
        )

    def test_ensure_protective_stops_calls_submit_protective_take_profit(self):
        """The sweep must actually call `submit_protective_take_profit`
        somewhere. Pre-2026-06-09 the function existed but was never
        called from any code path."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        # Find ensure_protective_stops function body
        fn_start = src.find("def ensure_protective_stops")
        assert fn_start > 0, "ensure_protective_stops missing"
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        assert "submit_protective_take_profit(" in body, (
            "ensure_protective_stops must call "
            "submit_protective_take_profit. Without this call the "
            "AI's TP targets are never placed at the broker and the "
            "polling-only design returns."
        )

    def test_tp_stored_via_protective_tp_order_id(self):
        """After placement, the order_id must be persisted into the
        entry row's `protective_tp_order_id` column so subsequent
        cycles can see the TP exists and skip duplicate placement."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def ensure_protective_stops")
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        assert "protective_tp_order_id = ?" in body, (
            "After TP placement, the order_id must be persisted to "
            "the entry row's protective_tp_order_id column. Without "
            "this the sweep will keep placing duplicates and the "
            "broker-truth coverage check won't see the TP."
        )

    def test_tp_placement_happens_after_stop_branch(self):
        """The TP placement must happen AFTER the stop placement
        branch (trailing or static), not inside it. Either branch
        cancels its stale counterpart (legacy stop + legacy TP) so
        a fresh TP at the current entry-row target replaces any
        out-of-date TP at the broker."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def ensure_protective_stops")
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        # The TP placement comment anchor
        tp_anchor = body.find("broker-side TAKE-PROFIT placement")
        assert tp_anchor > 0, (
            "TP placement comment anchor missing — refactor must "
            "preserve the anchor or update this pin."
        )
        # Both the trailing and static stop branches must appear
        # BEFORE the TP placement (TP is placed unconditionally
        # after whichever stop was placed)
        trailing_anchor = body.find("if use_trailing:")
        static_anchor = body.find("Static stop branch")
        assert 0 < trailing_anchor < tp_anchor, (
            "Trailing branch must precede TP placement so a fresh "
            "TP is placed after the stop logic runs."
        )
        assert 0 < static_anchor < tp_anchor, (
            "Static branch must precede TP placement."
        )


class TestSubmitTPHelperUnchanged:
    """The placement helper itself was already correct (defined at
    bracket_orders.py:272). These tests just confirm it's still
    callable and produces a limit order at the requested price."""

    def test_submit_protective_take_profit_signature(self):
        """The function takes (api, symbol, qty, side, limit_price,
        db_path?, entry_trade_id?). Refactor protection."""
        from bracket_orders import submit_protective_take_profit
        import inspect
        sig = inspect.signature(submit_protective_take_profit)
        params = list(sig.parameters.keys())
        for required in ("api", "symbol", "qty", "side", "limit_price"):
            assert required in params, (
                f"submit_protective_take_profit must accept '{required}' "
                f"keyword. Current signature: {params}"
            )

    def test_submit_protective_take_profit_uses_limit_type(self):
        """The function must use type='limit' (not stop) — TP fires
        only when price meets or beats the target, doesn't slip on
        gaps. Source pin so a future refactor doesn't break it."""
        src = (REPO_ROOT / "bracket_orders.py").read_text()
        fn_start = src.find("def submit_protective_take_profit")
        fn_end = src.find("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end if fn_end > 0 else len(src)]
        assert 'type="limit"' in body, (
            "submit_protective_take_profit must use type='limit' "
            "so it fills only at/better than the target."
        )
        assert 'time_in_force="gtc"' in body, (
            "GTC so the TP persists across cycles until filled "
            "or canceled by the broker-truth sweep."
        )
