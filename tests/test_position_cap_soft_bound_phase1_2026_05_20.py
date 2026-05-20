"""Phase 1 of #195 — position cap as soft bound (docs/23).

Guardrails for the five edits that landed together:
  1. Pre-filter no longer drops candidates when at max_total_positions.
  2. Execution orders SELL/STRONG_SELL before BUY/STRONG_BUY/etc. so
     the AI's same-cycle "SELL X + BUY Y" cooperates (close frees cash
     before open draws it).
  3. AI prompt includes a swap-directive block at/near the cap so the
     AI knows it can self-direct around the cap.
  4. Greeks gate wired into the multileg execution path (mirrors the
     existing single-leg gate).
  5. (Bundled.) virtual_audit.audit_cross_account keys virtual positions
     by OCC when present — so option-leg drift reports match Alpaca's
     OCC-keyed positions instead of always showing virtual_total=0.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# Edit 1: pre-filter no longer drops at-max candidates
# ---------------------------------------------------------------------------

class TestPreFilterDoesNotDropAtMax:
    """The at-max SKIP block (formerly trade_pipeline.py:1568-1574) is
    removed. Candidates pass through to the AI regardless of position
    count. Verified by reading the source — the literal SKIP reason
    'At max positions' must not appear in the source any more."""

    def test_atmax_skip_branch_removed_from_source(self):
        src_path = os.path.join(REPO, "trade_pipeline.py")
        with open(src_path) as f:
            src = f.read()
        assert "At max positions, can only close existing" not in src, (
            "The at-max pre-filter SKIP branch was supposed to be deleted in "
            "#195 Phase 1. If you re-added a cap-block branch, do it via "
            "the AI prompt (so the AI can self-direct), not via the "
            "pre-filter (which is the trade-not-hoard rule violation)."
        )


# ---------------------------------------------------------------------------
# Edit 2: SELL/STRONG_SELL execute before BUY/STRONG_BUY/etc.
# ---------------------------------------------------------------------------

class TestSellsBeforeBuysInDispatch:
    """The dispatch loop in run_trade_cycle's STEP 5 sorts ai_trades
    so that close-shaped actions (SELL, STRONG_SELL) execute before
    open-shaped actions. This makes the AI's "SELL X + BUY Y" pair
    cooperate within a single cycle — close frees cash, open draws it."""

    def test_sort_partitions_sells_then_others_stable(self):
        """The sort key (replicated here) groups closes-first.
        Within a class the relative order is preserved (stable sort —
        the AI's priority within the SELLs and within the BUYs is
        respected)."""
        # Replicates the dispatcher's sort key (trade_pipeline.py STEP 5)
        _CLOSE_ACTIONS = {"SELL", "STRONG_SELL"}
        def _key(t):
            return 0 if (t.get("action") or "").upper() in _CLOSE_ACTIONS else 1

        ai_trades = [
            {"symbol": "AAPL", "action": "BUY"},
            {"symbol": "MSFT", "action": "STRONG_SELL"},
            {"symbol": "NVDA", "action": "OPTIONS"},
            {"symbol": "TSLA", "action": "SELL"},
            {"symbol": "BKNG", "action": "MULTILEG_OPEN"},
            {"symbol": "AMD",  "action": "STRONG_BUY"},
        ]
        ordered = sorted(ai_trades, key=_key)

        # First two must be the SELLs, in original order (stable)
        assert ordered[0] == {"symbol": "MSFT", "action": "STRONG_SELL"}
        assert ordered[1] == {"symbol": "TSLA", "action": "SELL"}

        # Remaining four are opens, in original order
        opens_in_order = [d["symbol"] for d in ordered[2:]]
        assert opens_in_order == ["AAPL", "NVDA", "BKNG", "AMD"]

    def test_dispatcher_source_contains_sort(self):
        """Guardrail: a future refactor must not silently remove the
        SELLs-first ordering. The marker text in the source pins it."""
        src_path = os.path.join(REPO, "trade_pipeline.py")
        with open(src_path) as f:
            src = f.read()
        assert "_CLOSE_ACTIONS" in src, (
            "Phase 1 dispatch ordering was removed. Without it, the "
            "AI's 'SELL X + BUY Y' pair can race (BUY processes first, "
            "tries to draw cash that's about to be freed). Restore the "
            "sort or document the replacement mechanism."
        )


# ---------------------------------------------------------------------------
# Edit 3: AI prompt includes swap directive at/near cap
# ---------------------------------------------------------------------------

class TestAiPromptCapDirective:
    """When portfolio_state.num_positions >= max_total_positions, the
    prompt includes an explicit instruction to consider SELL + BUY in
    the same cycle. When near cap (>= 80%), a softer hint."""

    def _ctx(self, max_total_positions=10):
        return SimpleNamespace(
            max_position_pct=0.10,
            max_total_positions=max_total_positions,
            enable_short_selling=False,
            segment="stocks",
            signal_weights="{}",
            prompt_layout="{}",
        )

    def _market_context(self):
        return {"regime": "bull", "vix": 14.0, "political": ""}

    def _portfolio(self, num_positions, equity=100_000, cash=5_000):
        return {
            "equity": equity, "cash": cash,
            "num_positions": num_positions,
            "positions": [],
            "drawdown_pct": 0, "drawdown_action": "normal",
        }

    def test_at_cap_includes_swap_directive(self):
        from ai_analyst import _build_batch_prompt
        ctx = self._ctx(max_total_positions=10)
        prompt = _build_batch_prompt(
            candidates_data=[],
            portfolio_state=self._portfolio(num_positions=10),
            market_context=self._market_context(),
            ctx=ctx,
        )
        assert "AT POSITION CAP" in prompt or "ALSO emit SELL" in prompt, (
            "When at_max, the prompt must explicitly tell the AI it can "
            "emit SELL on a current holding to free room. Without this "
            "directive the AI has no signal it's allowed to swap."
        )

    def test_near_cap_includes_softer_hint(self):
        from ai_analyst import _build_batch_prompt
        ctx = self._ctx(max_total_positions=10)
        # 8/10 = 80%
        prompt = _build_batch_prompt(
            candidates_data=[],
            portfolio_state=self._portfolio(num_positions=8),
            market_context=self._market_context(),
            ctx=ctx,
        )
        assert "near cap" in prompt.lower(), (
            "At 80%+ of cap the prompt should hint at potential swap "
            "evaluation without forcing the directive language."
        )

    def test_well_below_cap_no_directive(self):
        from ai_analyst import _build_batch_prompt
        ctx = self._ctx(max_total_positions=10)
        prompt = _build_batch_prompt(
            candidates_data=[],
            portfolio_state=self._portfolio(num_positions=3),
            market_context=self._market_context(),
            ctx=ctx,
        )
        # 3/10 = 30% — neither block should fire
        assert "AT POSITION CAP" not in prompt
        assert "near cap" not in prompt.lower()


# ---------------------------------------------------------------------------
# Edit 4: Multileg execution applies the Greeks gate
# ---------------------------------------------------------------------------

class TestMultilegGreekGate:
    """The multileg path (pipelines/option.py:_execute_multileg) must
    invoke check_greeks_gates before submitting the strategy. Mirrors
    the single-leg call site at options_trader.py:497-540. Closes the
    regression from #189 where stock-only max_total_positions stopped
    being a de-facto cap on option-leg counts."""

    def test_multileg_source_contains_greeks_gate_call(self):
        """Static guardrail: a future refactor must not silently
        remove the multileg Greeks gate. The marker text pins it."""
        src_path = os.path.join(REPO, "pipelines", "option.py")
        with open(src_path) as f:
            src = f.read()
        assert "check_greeks_gates" in src, (
            "Greeks gate call was removed from the multileg execution "
            "path. Without it, multileg spreads have NO cap whatsoever "
            "(stock cap doesn't count option legs after #189; single-"
            "leg gate doesn't fire on the multileg path). Restore the "
            "gate call or document the replacement risk control."
        )
        # Also pin the marker comment so the intent stays visible
        assert "Greeks gate for the\n            # multileg path" in src, (
            "The intent-comment block for the multileg Greeks gate was "
            "removed. Either restore it or update this assertion to "
            "match the new comment."
        )


# ---------------------------------------------------------------------------
# Edit 5 (bundled): cross-account drift reconciler keys by OCC
# ---------------------------------------------------------------------------

class TestCrossAccountAuditOccKeying:
    """virtual_audit.audit_cross_account previously keyed virtual_totals
    by p["symbol"] which returns the UNDERLYING (Position dict-shim),
    while Alpaca's list_positions returns OCC for option positions. The
    two dicts never matched on option keys → every option leg in the
    broker appeared as `virtual total=0 vs Alpaca=N` drift.

    Fix: key by occ_symbol if present, else symbol. Both sides agree
    on OCC for options, underlying for stocks."""

    def test_virtual_totals_keys_by_occ_for_options(self):
        """When the virtual position has occ_symbol set (option leg),
        the dict key should be the OCC — matching what Alpaca returns
        as p.symbol for the same option position."""
        # Replicates the key-selection logic from virtual_audit.py:178
        # post-fix.
        virtual_pos = {
            "symbol": "ABNB",
            "occ_symbol": "ABNB260626P00121000",
            "qty": 2.0,
        }
        key = virtual_pos.get("occ_symbol") or virtual_pos["symbol"]
        assert key == "ABNB260626P00121000"

    def test_virtual_totals_keys_by_underlying_for_stocks(self):
        """When the virtual position has no occ_symbol (stock), the
        dict key falls back to the underlying ticker."""
        virtual_pos = {
            "symbol": "AAPL",
            "occ_symbol": None,
            "qty": 100.0,
        }
        key = virtual_pos.get("occ_symbol") or virtual_pos["symbol"]
        assert key == "AAPL"

    def test_source_uses_occ_first_keying(self):
        """Guardrail: ensure the source actually performs the OCC-first
        keying. A regression here re-introduces the cross-account drift
        false positives the operator caught 2026-05-20."""
        src_path = os.path.join(REPO, "virtual_audit.py")
        with open(src_path) as f:
            src = f.read()
        assert 'p.get("occ_symbol") or p["symbol"]' in src, (
            "audit_cross_account no longer keys by OCC for option "
            "positions. Without this, every option leg in the broker "
            "shows up as drift even when the journal correctly tracks "
            "it under the OCC."
        )
