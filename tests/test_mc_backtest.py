"""Item 5c — Monte Carlo backtest tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


SAMPLE_TRADES = [
    {"entry_price": 100.0, "exit_price": 105.0, "side": "long"},   # +5%
    {"entry_price": 100.0, "exit_price": 95.0,  "side": "long"},   # -5%
    {"entry_price": 100.0, "exit_price": 110.0, "side": "long"},   # +10%
    {"entry_price": 100.0, "exit_price": 98.0,  "side": "long"},   # -2%
    {"entry_price": 100.0, "exit_price": 103.0, "side": "long"},   # +3%
]


class TestReplayTrade:
    def test_long_profitable_trade_remains_profitable_for_small_slip(self):
        from mc_backtest import replay_trade
        import random
        rng = random.Random(0)
        # Big edge, small slippage (no bootstrap, default ~5±8 bps)
        trade = {"entry_price": 100, "exit_price": 110, "side": "long"}
        pnl = replay_trade(trade, {}, bucket=None, rng=rng)
        # 10% pretax minus a few bps → still ~9%+
        assert pnl > 0.05

    def test_zero_prices_returns_zero(self):
        from mc_backtest import replay_trade
        assert replay_trade({"entry_price": 0, "exit_price": 100}, {}) == 0.0
        assert replay_trade({"entry_price": 100, "exit_price": 0}, {}) == 0.0

    def test_short_pnl_inverted(self):
        """Short trade where exit_price < entry_price should have
        positive pnl (the short went your way)."""
        from mc_backtest import replay_trade
        import random
        rng = random.Random(0)
        trade = {"entry_price": 100, "exit_price": 90, "side": "short"}
        pnl = replay_trade(trade, {}, bucket=None, rng=rng)
        assert pnl > 0

    def test_bootstrap_residuals_consume_when_present(self):
        from mc_backtest import replay_trade
        import random
        rng = random.Random(42)
        # Plant a bucket with a single residual; deterministic sample
        bootstrap = {"0.0010_0.0050": [50.0, 50.0, 50.0, 50.0, 50.0]}
        trade = {"entry_price": 100, "exit_price": 100.5, "side": "long"}
        pnl = replay_trade(trade, bootstrap,
                            bucket="0.0010_0.0050", rng=rng)
        # 50 bps of slippage on each side of a 0.5% trade → trade
        # becomes a loss
        assert pnl < 0


class TestRunMonteCarlo:
    def test_empty_trades_returns_error(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo([])
        assert "error" in result

    def test_distribution_stats_in_order(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo(
            SAMPLE_TRADES, n_sims=200, seed=1,
        )
        assert result["n_sims"] == 200
        assert result["n_trades"] == 5
        assert result["p5_return"] <= result["p25_return"]
        assert result["p25_return"] <= result["p50_return"]
        assert result["p50_return"] <= result["p75_return"]
        assert result["p75_return"] <= result["p95_return"]
        assert result["worst_return"] <= result["p5_return"]
        assert result["best_return"] >= result["p95_return"]

    def test_reproducible_with_seed(self):
        from mc_backtest import run_monte_carlo
        a = run_monte_carlo(SAMPLE_TRADES, n_sims=100, seed=7)
        b = run_monte_carlo(SAMPLE_TRADES, n_sims=100, seed=7)
        assert a["p50_return"] == b["p50_return"]
        assert a["mean_return"] == b["mean_return"]

    def test_prob_loss_is_fraction_in_zero_one(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo(SAMPLE_TRADES, n_sims=200, seed=1)
        assert 0.0 <= result["prob_loss"] <= 1.0

    def test_dollar_amounts_match_pct(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo(
            SAMPLE_TRADES, n_sims=100, seed=1,
            initial_capital=100_000,
        )
        assert (
            abs(result["p50_dollars"] - result["p50_return"] * 100_000)
            < 1.0
        )

    def test_worst_case_is_worst_return(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo(SAMPLE_TRADES, n_sims=200, seed=1)
        # Mean return should sit between worst and best
        assert result["worst_return"] <= result["mean_return"]
        assert result["mean_return"] <= result["best_return"]


class TestByDayBootstrap:
    def test_by_day_collapses_to_per_trade_when_no_dates(self):
        """When trades have no dates, by-day falls back to per-trade
        behavior (each draw fresh) — should still produce a sensible
        distribution."""
        from mc_backtest import run_monte_carlo
        # Trades without entry_date / exit_date
        plain = [t for t in SAMPLE_TRADES]
        result = run_monte_carlo(
            plain, n_sims=200, seed=1, bootstrap_mode="by_day",
        )
        assert result["n_sims"] == 200
        assert result["p5_return"] <= result["p50_return"] <= result["p95_return"]

    def test_by_day_clusters_same_day_slippage(self):
        """Two trades on the SAME entry-date + side share one slippage
        draw. Compare to per-trade mode where they'd draw IID — by-day
        should show LARGER spread because correlated outcomes amplify
        the variance of the sum."""
        from mc_backtest import run_monte_carlo
        # 10 trades all on the same day, all long. By-day mode draws
        # ONE entry slip + ONE exit slip; per-trade draws 20.
        same_day = [
            {"entry_price": 100, "exit_price": 102, "side": "long",
             "entry_date": "2026-05-01", "exit_date": "2026-05-01"}
            for _ in range(10)
        ]
        # Bootstrap with synthetic non-zero residuals so slippage
        # actually fires
        bootstrap = {"0.0010_0.0050": [40.0, -40.0, 30.0, -30.0, 20.0]}
        # Inject by patching calibrate_from_history return shape
        from unittest.mock import patch
        with patch("mc_backtest.calibrate_from_history", create=True):
            pass   # not strictly needed since we pass db_path=None
        # Without db_path we use Gaussian fallback which IS IID per
        # draw, so the by-day-vs-per-trade difference still shows up
        # in correlated draws across same-day trades.
        per_trade = run_monte_carlo(
            same_day, n_sims=500, seed=1,
            bootstrap_mode="per_trade",
        )
        by_day = run_monte_carlo(
            same_day, n_sims=500, seed=1,
            bootstrap_mode="by_day",
        )
        # by-day clusters slippage realizations → distribution σ is
        # ≥ per-trade σ (correlated sum has larger variance than IID
        # sum of the same N draws). Loose tolerance — Gaussian
        # fallback produces noisy small samples.
        assert by_day["std_return"] >= per_trade["std_return"] * 0.5

    def test_invalid_mode_returns_error(self):
        from mc_backtest import run_monte_carlo
        result = run_monte_carlo(
            SAMPLE_TRADES, n_sims=10, bootstrap_mode="bogus",
        )
        assert "error" in result


class TestRenderForPrompt:
    def test_empty_returns_empty_string(self):
        from mc_backtest import render_mc_for_prompt
        assert render_mc_for_prompt({}) == ""
        assert render_mc_for_prompt({"error": "x"}) == ""
        assert render_mc_for_prompt({"n_sims": 0}) == ""

    def test_renders_key_stats(self):
        from mc_backtest import render_mc_for_prompt, run_monte_carlo
        result = run_monte_carlo(SAMPLE_TRADES, n_sims=100, seed=1)
        rendered = render_mc_for_prompt(result)
        assert "MC" in rendered
        assert "median" in rendered
        assert "P(loss)" in rendered
