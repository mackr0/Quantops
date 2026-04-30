"""Tests for options_strategy_advisor — the read-side that surfaces
recommendations to the AI prompt without executing.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _pos(symbol, qty, entry, current):
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": entry,
        "current_price": current,
        "unrealized_plpc": (current - entry) / entry,
    }


# ---------------------------------------------------------------------------
# evaluate_position_for_strategies
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_no_recs_when_position_below_100_shares(self):
        from options_strategy_advisor import evaluate_position_for_strategies
        # 50 shares — can't write a covered call (need 100 per contract)
        pos = _pos("AAPL", 50, 150.0, 165.0)
        assert evaluate_position_for_strategies(pos, iv_rank_pct=85) == []

    def test_covered_call_recommended_at_gain_with_rich_iv(self):
        from options_strategy_advisor import evaluate_position_for_strategies
        # 100 shares, +10% gain, IV rank 80 — covered call sweet spot
        pos = _pos("AAPL", 100, 150.0, 165.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=80)
        names = [r["strategy"] for r in recs]
        assert "covered_call" in names
        cc = next(r for r in recs if r["strategy"] == "covered_call")
        # Strike should be above current price (~7%)
        assert cc["strike"] > 165.0
        # 1 contract per 100 shares
        assert cc["contracts"] == 1

    def test_no_covered_call_when_iv_rank_low(self):
        """Premium isn't rich — covered call doesn't pay enough."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 165.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=40)
        assert "covered_call" not in [r["strategy"] for r in recs]

    def test_no_covered_call_at_small_gain(self):
        """Just barely profitable — no point capping upside yet."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 152.0)  # +1.3%
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=85)
        assert "covered_call" not in [r["strategy"] for r in recs]

    def test_protective_put_recommended_at_substantial_gain(self):
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)  # +20% gain
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=50)
        names = [r["strategy"] for r in recs]
        assert "protective_put" in names
        pp = next(r for r in recs if r["strategy"] == "protective_put")
        # Strike ~5% below current
        assert 165 < pp["strike"] < 175
        assert pp["contracts"] == 1

    def test_no_protective_put_at_small_gain(self):
        """Not enough unrealized P&L to be worth protecting."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 158.0)  # +5.3%
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=50)
        assert "protective_put" not in [r["strategy"] for r in recs]

    def test_both_recs_when_gain_high_and_iv_rich(self):
        """+15% gain and IV rank 80 — both covered call AND protective
        put might apply. AI decides which (or both)."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 200, 150.0, 172.0)  # +14.7%, 200 shares
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=80)
        names = [r["strategy"] for r in recs]
        assert "covered_call" in names
        assert "protective_put" in names
        # Covered call uses 2 contracts (200 shares / 100)
        cc = next(r for r in recs if r["strategy"] == "covered_call")
        assert cc["contracts"] == 2

    def test_short_position_no_recs(self):
        from options_strategy_advisor import evaluate_position_for_strategies
        # qty=-100 (short)
        pos = _pos("AAPL", -100, 150.0, 145.0)
        assert evaluate_position_for_strategies(pos, iv_rank_pct=80) == []

    def test_iv_rank_none_skips_covered_call_but_allows_put(self):
        """When IV rank is unknown (no oracle data), covered call is
        skipped but protective put still ok if gain qualifies."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=None)
        names = [r["strategy"] for r in recs]
        assert "covered_call" not in names
        assert "protective_put" in names


# ---------------------------------------------------------------------------
# render_for_prompt
# ---------------------------------------------------------------------------

class TestRender:
    def test_empty_when_no_positions(self):
        from options_strategy_advisor import render_for_prompt
        assert render_for_prompt([]) == ""

    def test_empty_when_no_recommendations(self):
        from options_strategy_advisor import render_for_prompt
        positions = [_pos("AAPL", 50, 150.0, 152.0)]  # too few shares
        assert render_for_prompt(positions) == ""

    def test_includes_strategy_block_header(self):
        from options_strategy_advisor import render_for_prompt
        positions = [_pos("AAPL", 100, 150.0, 165.0)]
        block = render_for_prompt(positions, iv_rank_lookup=lambda s: 85)
        assert "OPTIONS STRATEGIES" in block
        assert "COVERED_CALL" in block.upper()

    def test_caps_at_5_recommendations(self):
        from options_strategy_advisor import render_for_prompt
        # 8 positions all qualifying for both strategies = 16 recs
        positions = [_pos(f"S{i}", 100, 150.0, 180.0) for i in range(8)]
        block = render_for_prompt(positions, iv_rank_lookup=lambda s: 85)
        # Lines starting with " • " — count bullets
        bullets = [l for l in block.split("\n") if l.lstrip().startswith("•")]
        # 5 strategy bullets + maybe an "and N more" bullet
        assert len(bullets) <= 6

    def test_iv_rank_lookup_failure_falls_back_to_none(self):
        """Bad lookup function shouldn't crash the render."""
        from options_strategy_advisor import render_for_prompt
        positions = [_pos("AAPL", 100, 150.0, 180.0)]

        def broken_lookup(s):
            raise RuntimeError("oracle blew up")

        block = render_for_prompt(positions, iv_rank_lookup=broken_lookup)
        # Render still works — protective_put fires regardless of IV
        assert "PROTECTIVE_PUT" in block.upper()
