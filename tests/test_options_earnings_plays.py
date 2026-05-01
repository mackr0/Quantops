"""Phase F of OPTIONS_PROGRAM_PLAN.md — earnings/event opportunism."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestEvaluateEarningsPlay:
    def test_pre_earnings_with_rich_iv_recommends_iron_condor(self):
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=2, iv_rank_pct=85,
            current_price=150.0,
        )
        assert rec is not None
        assert rec["play"] == "pre_earnings_iron_condor"
        assert rec["strategy"] == "iron_condor"
        s = rec["strikes"]
        # ±6% inner, ±12% outer
        assert s["put_short"] < 150 < s["call_short"]
        assert s["put_long"] < s["put_short"]
        assert s["call_long"] > s["call_short"]

    def test_pre_earnings_with_cheap_iv_recommends_long_straddle(self):
        """Rare opportunistic case: cheap pre-earnings IV."""
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=2, iv_rank_pct=15,
            current_price=150.0,
        )
        assert rec is not None
        assert rec["play"] == "pre_earnings_long_straddle"
        assert rec["strategy"] == "long_straddle"

    def test_neutral_iv_no_play(self):
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=2, iv_rank_pct=50,
            current_price=150.0,
        )
        assert rec is None

    def test_outside_pre_window_no_play(self):
        """Pre-window is 3 days. Earnings 5 days out → outside."""
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=5, iv_rank_pct=85,
            current_price=150.0,
        )
        assert rec is None

    def test_post_earnings_no_play(self):
        """Post-earnings recommendations not yet implemented."""
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=-1, iv_rank_pct=85,
            current_price=150.0,
        )
        assert rec is None

    def test_no_iv_data_no_play(self):
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=2, iv_rank_pct=None,
            current_price=150.0,
        )
        assert rec is None

    def test_zero_price_no_play(self):
        from options_earnings_plays import evaluate_earnings_play
        rec = evaluate_earnings_play(
            "AAPL", days_until_earnings=2, iv_rank_pct=85,
            current_price=0,
        )
        assert rec is None


class TestRenderEarningsPlays:
    def test_empty_candidates_returns_empty(self):
        from options_earnings_plays import render_earnings_plays_for_prompt
        out = render_earnings_plays_for_prompt(
            [], earnings_lookup=lambda s: None,
            iv_rank_lookup=lambda s: None,
        )
        assert out == ""

    def test_renders_iron_condor_recommendation(self):
        from options_earnings_plays import render_earnings_plays_for_prompt
        cands = [{"symbol": "AAPL", "price": 150.0}]
        def earn_lookup(sym):
            return {"days_until": 2}
        def iv_lookup(sym):
            return 85
        out = render_earnings_plays_for_prompt(
            cands, earnings_lookup=earn_lookup,
            iv_rank_lookup=iv_lookup,
        )
        assert "EARNINGS PLAYS" in out
        assert "AAPL" in out
        assert "iron_condor" in out

    def test_skips_candidates_without_earnings(self):
        from options_earnings_plays import render_earnings_plays_for_prompt
        cands = [{"symbol": "MSFT", "price": 200.0}]
        out = render_earnings_plays_for_prompt(
            cands, earnings_lookup=lambda s: None,
            iv_rank_lookup=lambda s: 85,
        )
        assert out == ""

    def test_skips_candidates_without_iv(self):
        from options_earnings_plays import render_earnings_plays_for_prompt
        cands = [{"symbol": "AAPL", "price": 150.0}]
        out = render_earnings_plays_for_prompt(
            cands,
            earnings_lookup=lambda s: {"days_until": 2},
            iv_rank_lookup=lambda s: None,
        )
        assert out == ""
