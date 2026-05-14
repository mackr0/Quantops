"""Structural guarantee: when actionable candidates exist, the AI
prompt MUST present stock-action recommendations with the same
level of pre-computed analysis as multi-leg options recommendations.

The bug class.
On 2026-05-12 the IV dead zone was closed. Side effect: every
candidate received a pre-built multileg recommendation in the AI
prompt while stocks got only a bare indicator dump. The AI
preferred the side with pre-computed analysis. Stock BUY signals
collapsed from 24/day to 0/day. The dead-zone restore (2026-05-14)
patched part of it but the structural asymmetry remained for any
candidate with IV outside the neutral band.

This test enforces the architectural guarantee per Mack:
"stocks and options are not in competition with each other —
they are two different opportunities; we should take the best
candidates from both and determine action."

Specifically:
  1. `stock_strategy_advisor.evaluate_candidate_for_stock_action`
     must return a sized + stop/TP-bearing rec for any candidate
     with a directional signal (BUY/SHORT/SELL).
  2. `stock_strategy_advisor.render_stock_recs_for_prompt` must
     produce a non-empty block when given actionable candidates.
  3. The output rec dict must carry the SAME information density as
     a multileg rec (action, size, stop, TP, rationale, confidence)
     — no missing fields that would let the AI feel one side is
     better-prepared than the other.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _candidate(signal="BUY", price=180.0, score=2.0):
    return {
        "symbol": "AAPL",
        "signal": signal,
        "price": price,
        "score": score,
        "rsi": 65.0,
        "atr": 4.5,
        "adx": 28.0,
        "mfi": 60.0,
        "volume_ratio": 1.4,
    }


class TestStocksAndOptionsEqualInPrompt:
    def test_buy_candidate_produces_stock_rec(self):
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        recs = evaluate_candidate_for_stock_action(_candidate(signal="BUY"))
        assert len(recs) == 1, (
            f"Expected exactly 1 BUY rec, got {len(recs)}: {recs}"
        )
        r = recs[0]
        assert r["action"] == "BUY"
        # All four planning fields the AI would need to execute the trade.
        for field in ("size_pct", "stop_loss_pct", "take_profit_pct",
                       "rationale", "confidence"):
            assert field in r, (
                f"Stock rec missing field {field!r}: {r}"
            )

    def test_short_candidate_produces_stock_rec(self):
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        recs = evaluate_candidate_for_stock_action(
            _candidate(signal="SHORT", score=-2.0))
        assert len(recs) == 1
        assert recs[0]["action"] == "SHORT"

    def test_hold_candidate_produces_no_rec(self):
        """HOLD has no directional thesis — no rec should be made.
        The AI should skip these candidates."""
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        recs = evaluate_candidate_for_stock_action(
            _candidate(signal="HOLD"))
        assert recs == []

    def test_render_block_non_empty_with_actionables(self):
        from stock_strategy_advisor import render_stock_recs_for_prompt
        cands = [
            _candidate(signal="BUY"),
            {**_candidate(signal="SHORT", score=-1.5), "symbol": "TSLA"},
        ]
        block = render_stock_recs_for_prompt(cands)
        assert "STOCK ACTION RECOMMENDATIONS" in block
        assert "AAPL" in block
        assert "TSLA" in block
        assert "size" in block.lower() or "% equity" in block

    def test_render_block_empty_when_no_actionables(self):
        from stock_strategy_advisor import render_stock_recs_for_prompt
        block = render_stock_recs_for_prompt(
            [_candidate(signal="HOLD"), _candidate(signal="HOLD")])
        assert block == ""

    def test_prompt_inserts_both_blocks(self):
        """The AI prompt builder must reference BOTH stock_recs_block
        AND multileg_block. If a future refactor removes either, the
        asymmetric-prompt bug recurs."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_root, "ai_analyst.py")
        with open(path) as f:
            src = f.read()
        assert "stock_recs_block" in src, (
            "ai_analyst.py is missing stock_recs_block. Without it, "
            "the AI prompt has options recs but no symmetric stock "
            "recs — the exact bug pattern from 2026-05-12."
        )
        assert "render_stock_recs_for_prompt" in src, (
            "ai_analyst.py must call render_stock_recs_for_prompt to "
            "build the stock-side block."
        )
        # Both blocks must appear in the f-string body, not just be
        # defined-but-unused.
        assert 'f"{stock_recs_block}"' in src or "{stock_recs_block}" in src
        assert 'f"{multileg_block}"' in src or "{multileg_block}" in src

    def test_stock_rec_fields_match_multileg_rec_density(self):
        """Both kinds of rec must carry the same number of "planning"
        fields the AI needs to execute. Otherwise the AI perceives
        one side as easier and biases that way."""
        from stock_strategy_advisor import evaluate_candidate_for_stock_action
        from options_strategy_advisor import evaluate_candidate_for_multileg
        stock_recs = evaluate_candidate_for_stock_action(
            _candidate(signal="BUY"))
        opt_recs = evaluate_candidate_for_multileg(
            _candidate(signal="BUY"), iv_rank_pct=70,
        )
        assert stock_recs and opt_recs, (
            f"Need both reck types non-empty for parity check. "
            f"stock={stock_recs}, opt={opt_recs}"
        )
        # Both must have a rationale string and a symbol.
        s = stock_recs[0]
        o = opt_recs[0]
        assert "rationale" in s and "rationale" in o
        assert "symbol" in s and "symbol" in o
        # Each side carries action-specific planning fields. Stock:
        # size_pct, stop_loss_pct, take_profit_pct. Options: strikes,
        # expiry, strategy. Both have at least 3 such fields.
        stock_planning = {"size_pct", "stop_loss_pct", "take_profit_pct"}
        opt_planning = {"strikes", "expiry", "strategy"}
        assert stock_planning <= set(s.keys()), (
            f"Stock rec missing planning fields: "
            f"{stock_planning - set(s.keys())}"
        )
        assert opt_planning <= set(o.keys()), (
            f"Options rec missing planning fields: "
            f"{opt_planning - set(o.keys())}"
        )
