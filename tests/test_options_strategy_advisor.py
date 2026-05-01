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
        # 100 shares, +20% gain, IV rank 70 — covered call sweet spot
        # (post-2026-05-01 calibration: gain ≥ 15, IV rank ≥ 60).
        pos = _pos("AAPL", 100, 150.0, 180.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=70)
        names = [r["strategy"] for r in recs]
        assert "covered_call" in names
        cc = next(r for r in recs if r["strategy"] == "covered_call")
        # Strike should be above current price (~7%)
        assert cc["strike"] > 180.0
        # 1 contract per 100 shares
        assert cc["contracts"] == 1

    def test_no_covered_call_when_iv_rank_low(self):
        """Premium isn't rich — covered call doesn't pay enough.
        Gain easily passes (+20%) so this test isolates the IV gate."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=40)
        assert "covered_call" not in [r["strategy"] for r in recs]

    def test_no_covered_call_when_gain_below_threshold(self):
        """Gain ≥ 15% required (post-2026-05-01 calibration). At +10%
        we shouldn't be capping further upside on a winner that's still
        running."""
        from options_strategy_advisor import evaluate_position_for_strategies
        # +10% gain, IV rank passes
        pos = _pos("AAPL", 100, 150.0, 165.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=80)
        assert "covered_call" not in [r["strategy"] for r in recs]

    def test_no_covered_call_at_small_gain(self):
        """Just barely profitable — no point capping upside yet."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 152.0)  # +1.3%
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=85)
        assert "covered_call" not in [r["strategy"] for r in recs]

    def test_protective_put_recommended_at_substantial_gain_and_cheap_iv(self):
        """PP requires gain ≥ 10% AND IV rank ≤ 50 (post-2026-05-01
        calibration — buying insurance only when premium is cheap)."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)  # +20% gain
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=30)
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
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=30)
        assert "protective_put" not in [r["strategy"] for r in recs]

    def test_no_protective_put_when_iv_expensive(self):
        """Insurance is expensive when IV is rich — defer."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)  # +20% gain (passes)
        # IV rank 80 → puts are overpriced, skip
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=80)
        assert "protective_put" not in [r["strategy"] for r in recs]

    def test_strategies_are_iv_regime_exclusive(self):
        """Post-calibration the gates split by IV regime — covered call
        when IV is rich (≥60), protective put when IV is cheap (≤50).
        At a single IV reading, only one fires (or neither). Both
        firing simultaneously is not possible by design — the AI
        sees opportunities one at a time as IV shifts."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 200, 150.0, 180.0)  # +20% gain, qualifies on gain

        # Rich IV: covered call fires, put does not
        recs_rich = evaluate_position_for_strategies(pos, iv_rank_pct=80)
        names_rich = [r["strategy"] for r in recs_rich]
        assert "covered_call" in names_rich
        assert "protective_put" not in names_rich

        # Cheap IV: protective put fires, call does not
        recs_cheap = evaluate_position_for_strategies(pos, iv_rank_pct=30)
        names_cheap = [r["strategy"] for r in recs_cheap]
        assert "protective_put" in names_cheap
        assert "covered_call" not in names_cheap

    def test_short_position_no_recs(self):
        from options_strategy_advisor import evaluate_position_for_strategies
        # qty=-100 (short)
        pos = _pos("AAPL", -100, 150.0, 145.0)
        assert evaluate_position_for_strategies(pos, iv_rank_pct=80) == []

    def test_iv_rank_none_skips_both_strategies(self):
        """When IV rank is unknown, BOTH strategies are skipped.
        Pre-2026-05-01 the protective put fired without IV data — the
        bug that calibration corrected. Don't guess on insurance pricing."""
        from options_strategy_advisor import evaluate_position_for_strategies
        pos = _pos("AAPL", 100, 150.0, 180.0)
        recs = evaluate_position_for_strategies(pos, iv_rank_pct=None)
        names = [r["strategy"] for r in recs]
        assert "covered_call" not in names
        assert "protective_put" not in names


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
        # +20% gain, IV rank 85 → covered call fires (post-2026-05-01
        # gates: gain ≥ 15, IV ≥ 60)
        positions = [_pos("AAPL", 100, 150.0, 180.0)]
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
        """Bad lookup function shouldn't crash the render. Since both
        strategies now require IV (post-2026-05-01 calibration), an
        oracle failure means no strategies fire — the block returns
        empty rather than blowing up."""
        from options_strategy_advisor import render_for_prompt
        positions = [_pos("AAPL", 100, 150.0, 180.0)]

        def broken_lookup(s):
            raise RuntimeError("oracle blew up")

        # No exception
        block = render_for_prompt(positions, iv_rank_lookup=broken_lookup)
        # Empty block is the correct response — we don't fake IV data
        assert block == ""


# ---------------------------------------------------------------------------
# Phase B3 — multi-leg advisor (per-candidate)
# ---------------------------------------------------------------------------

def _candidate(symbol="AAPL", signal="BUY", price=150.0, **extras):
    return {"symbol": symbol, "signal": signal, "price": price, **extras}


class TestEvaluateCandidateForMultileg:
    def test_bullish_with_rich_iv_recommends_bull_put_spread(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="BUY"), iv_rank_pct=80,
        )
        names = [r["strategy"] for r in recs]
        assert "bull_put_spread" in names
        rec = next(r for r in recs if r["strategy"] == "bull_put_spread")
        # Strikes: short ~5% OTM = ~142.5, long ~5% below short = ~135
        assert rec["strikes"]["short"] < 150
        assert rec["strikes"]["long"] < rec["strikes"]["short"]

    def test_bullish_with_cheap_iv_recommends_bull_call_spread(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="BUY"), iv_rank_pct=30,
        )
        names = [r["strategy"] for r in recs]
        assert "bull_call_spread" in names
        rec = next(r for r in recs if r["strategy"] == "bull_call_spread")
        # Long lower strike, short upper strike, both above current
        assert rec["strikes"]["long"] > 150
        assert rec["strikes"]["short"] > rec["strikes"]["long"]

    def test_bearish_with_rich_iv_recommends_bear_call_spread(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="SHORT"), iv_rank_pct=80,
        )
        names = [r["strategy"] for r in recs]
        assert "bear_call_spread" in names

    def test_bearish_with_cheap_iv_recommends_bear_put_spread(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="SHORT"), iv_rank_pct=30,
        )
        names = [r["strategy"] for r in recs]
        assert "bear_put_spread" in names

    def test_neutral_iv_no_recs(self):
        """IV in the 50-60 neutral band → no recs (no edge either way)."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="BUY"), iv_rank_pct=55,
        )
        assert recs == []

    def test_iv_unknown_no_recs(self):
        """No IV data → don't recommend (we don't price-blind on premium)."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="BUY"), iv_rank_pct=None,
        )
        assert recs == []

    def test_ranging_regime_rich_iv_recommends_iron_condor(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="HOLD"), iv_rank_pct=80, regime="ranging",
        )
        names = [r["strategy"] for r in recs]
        assert "iron_condor" in names
        rec = next(r for r in recs if r["strategy"] == "iron_condor")
        # 4 strikes, ordered low→high
        s = rec["strikes"]
        assert s["put_long"] < s["put_short"] < s["call_short"] < s["call_long"]

    def test_trending_regime_no_iron_condor(self):
        """Iron condor only fires when regime is explicitly ranging."""
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="HOLD"), iv_rank_pct=80, regime="trending",
        )
        assert "iron_condor" not in [r["strategy"] for r in recs]

    def test_long_strangle_when_expansion_expected_and_iv_cheap(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        recs = evaluate_candidate_for_multileg(
            _candidate(signal="HOLD", volatility_view="expansion"),
            iv_rank_pct=30,
        )
        names = [r["strategy"] for r in recs]
        assert "long_strangle" in names

    def test_no_recs_for_unknown_symbol_or_zero_price(self):
        from options_strategy_advisor import evaluate_candidate_for_multileg
        # Missing symbol
        assert evaluate_candidate_for_multileg(
            {"signal": "BUY", "price": 150}, iv_rank_pct=80,
        ) == []
        # Zero price
        assert evaluate_candidate_for_multileg(
            _candidate(price=0), iv_rank_pct=80,
        ) == []


class TestRenderMultilegRecs:
    def test_empty_candidates_returns_empty(self):
        from options_strategy_advisor import render_multileg_recs_for_prompt
        assert render_multileg_recs_for_prompt([]) == ""

    def test_no_iv_returns_empty(self):
        from options_strategy_advisor import render_multileg_recs_for_prompt
        cands = [_candidate(signal="BUY")]
        assert render_multileg_recs_for_prompt(
            cands, iv_rank_lookup=lambda s: None,
        ) == ""

    def test_rendered_block_includes_strategy_and_rationale(self):
        from options_strategy_advisor import render_multileg_recs_for_prompt
        cands = [_candidate(signal="BUY")]
        block = render_multileg_recs_for_prompt(
            cands, iv_rank_lookup=lambda s: 70,
        )
        assert "MULTI-LEG OPTIONS STRATEGIES" in block
        assert "bull_put_spread" in block
        assert "Rationale" in block

    def test_caps_at_8_recommendations(self):
        from options_strategy_advisor import render_multileg_recs_for_prompt
        # 12 candidates × bull_put_spread = 12 recs, capped at 8
        cands = [_candidate(symbol=f"S{i}", signal="BUY") for i in range(12)]
        block = render_multileg_recs_for_prompt(
            cands, iv_rank_lookup=lambda s: 80,
        )
        assert "and 4 more" in block
