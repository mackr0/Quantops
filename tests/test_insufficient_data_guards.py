"""Systematic regression tests for the Performance Dashboard audit (2026-04-14).

Every widget that previously showed a misleading `0.00` when data was
insufficient now emits a `{metric}_computable` boolean the template
checks to render `N/A` instead. This file locks in the contract for
every flag so the pattern can't silently regress.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


def _empty_metrics():
    """Run calculate_all_metrics with no data."""
    from metrics import calculate_all_metrics
    with patch("metrics._gather_snapshots", return_value=[]), \
         patch("metrics._gather_trades", return_value=[]):
        return calculate_all_metrics({"fake.db"}, initial_capital=10000)


def _one_trade_metrics():
    """Run with a single losing closed trade (matches real production state
    when we started this audit)."""
    from metrics import calculate_all_metrics
    one_trade = [{
        "timestamp": "2026-04-14T14:10:00",
        "symbol": "LUNR", "side": "sell", "qty": 18,
        "price": 23.29, "pnl": -29.7, "status": "closed",
    }]
    with patch("metrics._gather_snapshots",
               return_value=[
                   {"date": "2026-04-12", "equity": 30000, "daily_pnl": 0},
                   {"date": "2026-04-13", "equity": 29979.80, "daily_pnl": -20.20},
               ]), \
         patch("metrics._gather_trades", return_value=one_trade):
        return calculate_all_metrics({"fake.db"}, initial_capital=10000)


# ---------------------------------------------------------------------------
# Flag presence — every guarded metric must emit a _computable flag
# ---------------------------------------------------------------------------

REQUIRED_FLAGS = [
    "sharpe_ratio_computable",
    "sortino_ratio_computable",
    "annualized_volatility_computable",
    "calmar_ratio_computable",
    "var_95_computable",
    "cvar_95_computable",
    "win_rate_computable",
    "profit_factor_computable",
    "win_loss_ratio_computable",
    "monthly_win_rate_computable",
    "slippage_vs_gross_computable",
    "alpha_computable",
    "beta_spy_computable",
    "correlation_spy_computable",
    "correlation_qqq_computable",
    "correlation_btc_computable",
]


class TestFlagsExist:
    def test_every_guarded_metric_has_computable_flag(self):
        """Empty data: all flags present and False."""
        m = _empty_metrics()
        for flag in REQUIRED_FLAGS:
            assert flag in m, f"missing flag {flag!r}"
            assert m[flag] is False, (
                f"{flag} should be False with empty data, got {m[flag]!r}"
            )

    def test_current_streak_has_computable_flag(self):
        m = _empty_metrics()
        assert "computable" in m["current_streak"]
        assert m["current_streak"]["computable"] is False


# ---------------------------------------------------------------------------
# Behavior on the exact production scenario (1 losing trade, 2 snapshots)
# ---------------------------------------------------------------------------

class TestOneTradeScenario:
    """With a single losing closed trade + 2 snapshots, nothing with a
    minimum-sample requirement should be computable. This is the exact
    state that had the user seeing 0.00 everywhere."""

    def test_sharpe_and_sortino_not_computable(self):
        m = _one_trade_metrics()
        # We have 1 daily return (2 snapshots) — below the 2-return minimum
        assert m["sharpe_ratio_computable"] is False
        assert m["sortino_ratio_computable"] is False

    def test_volatility_not_computable(self):
        m = _one_trade_metrics()
        assert m["annualized_volatility_computable"] is False

    def test_var_not_computable_with_one_trade(self):
        m = _one_trade_metrics()
        # 1 trade < 5-trade minimum for VaR
        assert m["var_95_computable"] is False
        assert m["cvar_95_computable"] is False

    def test_win_rate_computable_with_closed_trade(self):
        """Win rate CAN be computed with 1 closed trade (just equals 0%)."""
        m = _one_trade_metrics()
        assert m["win_rate_computable"] is True
        assert m["win_rate"] == 0.0

    def test_profit_factor_not_computable_without_both_sides(self):
        """One losing trade, zero wins — profit factor is undefined."""
        m = _one_trade_metrics()
        assert m["profit_factor_computable"] is False

    def test_win_loss_ratio_not_computable_without_both_sides(self):
        m = _one_trade_metrics()
        assert m["win_loss_ratio_computable"] is False

    def test_calmar_not_computable_with_small_data(self):
        m = _one_trade_metrics()
        # 1 day of data + tiny DD → Calmar guarded
        assert m["calmar_ratio_computable"] is False

    def test_alpha_beta_not_computable_without_benchmark_alignment(self):
        """Without a real SPY benchmark fetch + 20 aligned days, flags stay False."""
        m = _one_trade_metrics()
        assert m["alpha_computable"] is False
        assert m["beta_spy_computable"] is False

    def test_correlations_not_computable_without_benchmark(self):
        m = _one_trade_metrics()
        assert m["correlation_spy_computable"] is False
        assert m["correlation_qqq_computable"] is False
        assert m["correlation_btc_computable"] is False


# ---------------------------------------------------------------------------
# Behavior when data IS sufficient
# ---------------------------------------------------------------------------

class TestSufficientData:
    def _make_snapshots(self, n_days: int, start_eq: float = 10000):
        snaps = []
        eq = start_eq
        import random
        random.seed(42)
        for i in range(n_days):
            eq *= (1 + random.uniform(-0.02, 0.02))
            snaps.append({
                "date": f"2026-{(1 + i // 30):02d}-{(1 + i % 30):02d}",
                "equity": eq,
                "daily_pnl": 0,
            })
        return snaps

    def test_sharpe_computable_with_enough_returns(self):
        from metrics import calculate_all_metrics
        snaps = self._make_snapshots(30)
        with patch("metrics._gather_snapshots", return_value=snaps), \
             patch("metrics._gather_trades", return_value=[]):
            m = calculate_all_metrics({"fake.db"}, initial_capital=10000)
        assert m["sharpe_ratio_computable"] is True
        assert m["annualized_volatility_computable"] is True

    def test_var_computable_with_five_plus_trades(self):
        from metrics import calculate_all_metrics
        trades = [
            {"timestamp": f"2026-04-{i:02d}T10:00:00",
             "symbol": f"T{i}", "side": "sell", "qty": 10,
             "price": 100, "pnl": -10 * i, "status": "closed"}
            for i in range(1, 7)   # 6 trades
        ]
        with patch("metrics._gather_snapshots", return_value=[]), \
             patch("metrics._gather_trades", return_value=trades):
            m = calculate_all_metrics({"fake.db"}, initial_capital=10000)
        assert m["var_95_computable"] is True
        assert m["cvar_95_computable"] is True

    def test_profit_factor_computable_with_win_and_loss(self):
        from metrics import calculate_all_metrics
        trades = [
            {"timestamp": "2026-04-01T10:00:00", "symbol": "A", "side": "sell",
             "qty": 10, "price": 100, "pnl": 200, "status": "closed"},
            {"timestamp": "2026-04-02T10:00:00", "symbol": "B", "side": "sell",
             "qty": 10, "price": 100, "pnl": -100, "status": "closed"},
        ]
        with patch("metrics._gather_snapshots", return_value=[]), \
             patch("metrics._gather_trades", return_value=trades):
            m = calculate_all_metrics({"fake.db"}, initial_capital=10000)
        assert m["profit_factor_computable"] is True
        assert m["profit_factor"] == 2.0
        assert m["win_loss_ratio_computable"] is True
