"""Phase E of OPTIONS_PROGRAM_PLAN.md — vol regime gate."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _oracle(rank=50.0, skew_ratio=1.0, near_iv=0.30, far_iv=0.30,
              has_options=True):
    return {
        "has_options": has_options,
        "iv_rank": {"rank_pct": rank, "signal": "neutral",
                    "realized_vol": 0.20},
        "skew": {"skew": skew_ratio, "put_iv": 0.30,
                 "call_iv": 0.30, "signal": "neutral"},
        "term_structure": {
            "near_iv": near_iv, "far_iv": far_iv,
            "slope": far_iv - near_iv,
            "inverted": near_iv > far_iv,
            "signal": "neutral",
        },
    }


class TestClassifyVolRegime:
    def test_no_options_returns_no_signals(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(has_options=False))
        assert result["has_signals"] is False

    def test_premium_rich_recommends_short_premium_plays(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(rank=85))
        assert result["premium_regime"] == "rich"
        # Sell-premium strategies should be favored
        assert "iron_condor" in result["favored_strategies"]
        assert "bull_put_spread" in result["favored_strategies"]
        # Long-premium plays should NOT appear
        assert "long_straddle" not in result["favored_strategies"]

    def test_premium_cheap_recommends_long_premium_plays(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(rank=15))
        assert result["premium_regime"] == "cheap"
        assert "long_strangle" in result["favored_strategies"]
        assert "bull_call_spread" in result["favored_strategies"]
        # Sell-premium plays should NOT appear
        assert "iron_condor" not in result["favored_strategies"]

    def test_neutral_premium_no_strong_recs(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(rank=50))
        assert result["premium_regime"] == "neutral"

    def test_steep_put_skew_classified(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(rank=80, skew_ratio=1.40))
        assert result["skew_regime"] == "steep_put"
        # Asymmetric condor mention
        assert "asymmetric" in result["rationale"].lower()

    def test_steep_call_skew_classified(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(_oracle(rank=50, skew_ratio=0.75))
        assert result["skew_regime"] == "steep_call"

    def test_contango_term_structure(self):
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(
            _oracle(near_iv=0.25, far_iv=0.30),  # back > front by 5pts
        )
        assert result["term_regime"] == "contango"

    def test_backwardation_drops_calendar(self):
        """Backwardation makes calendars lose (back decays faster
        than front). Don't recommend them in this regime."""
        from options_vol_regime import classify_vol_regime
        result = classify_vol_regime(
            _oracle(rank=15, near_iv=0.40, far_iv=0.30),  # front > back
        )
        assert result["term_regime"] == "backwardation"
        # Calendars excluded even though premium=cheap
        assert "calendar_spread" not in result["favored_strategies"]


class TestRenderVolRegime:
    def test_empty_candidates_returns_empty(self):
        from options_vol_regime import render_vol_regime_for_prompt
        out = render_vol_regime_for_prompt([], oracle_lookup=lambda s: None)
        assert out == ""

    def test_renders_actionable_lines_only(self):
        from options_vol_regime import render_vol_regime_for_prompt
        cands = [
            {"symbol": "AAPL"},  # has signals
            {"symbol": "BBB"},   # no oracle
        ]
        def lookup(sym):
            if sym == "AAPL":
                return _oracle(rank=85, skew_ratio=1.5)
            return None  # BBB has no oracle
        out = render_vol_regime_for_prompt(cands, oracle_lookup=lookup)
        assert "VOL REGIME" in out
        assert "AAPL" in out
        assert "BBB" not in out
        assert "iron_condor" in out

    def test_caps_at_max_lines(self):
        from options_vol_regime import render_vol_regime_for_prompt
        cands = [{"symbol": f"S{i}"} for i in range(10)]
        out = render_vol_regime_for_prompt(
            cands, oracle_lookup=lambda s: _oracle(rank=85),
            max_lines=3,
        )
        # max_lines caps how many candidates we evaluate; verify we
        # don't render all 10
        rendered = sum(1 for line in out.split("\n")
                       if line.startswith("  - "))
        assert rendered <= 3
