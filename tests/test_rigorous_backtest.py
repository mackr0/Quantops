"""Tests for rigorous_backtest.py — Phase 2 of Quant Fund Evolution.

These tests validate each gate individually using synthetic trade data.
The validate_strategy() integration is tested separately (slow, needs network).
"""

import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Statistical significance gate
# ---------------------------------------------------------------------------

class TestStatisticalSignificance:
    def test_profitable_strategy_is_significant(self):
        from rigorous_backtest import check_statistical_significance
        # 50 positive returns averaging 1%, small noise — should be highly sig
        trades = [{"return_pct": 1.0 + i * 0.01} for i in range(50)]
        result = check_statistical_significance(trades)
        assert result["p_value"] < 0.05
        assert result["significant"] is True
        assert result["n"] == 50

    def test_random_strategy_not_significant(self):
        from rigorous_backtest import check_statistical_significance
        # Noisy returns near zero with high variance
        import random
        random.seed(123)
        trades = [{"return_pct": random.uniform(-5, 5)} for _ in range(30)]
        result = check_statistical_significance(trades)
        # p-value should be large — not rejecting H0
        assert result["p_value"] > 0.05
        assert result["significant"] is False

    def test_empty_trades(self):
        from rigorous_backtest import check_statistical_significance
        result = check_statistical_significance([])
        assert result["p_value"] == 1.0
        assert result["significant"] is False


# ---------------------------------------------------------------------------
# Monte Carlo gate
# ---------------------------------------------------------------------------

class TestMonteCarlo:
    def test_positive_strategy_mostly_positive_bootstraps(self):
        from rigorous_backtest import monte_carlo_stress
        # Strong positive edge — bootstraps should reflect that
        trades = [{"return_pct": 2.0} for _ in range(50)]
        result = monte_carlo_stress(trades, iterations=200, transaction_cost_pct=0.004)
        assert result["positive_pct"] > 90
        assert result["median"] > 0

    def test_negative_strategy_mostly_negative(self):
        from rigorous_backtest import monte_carlo_stress
        trades = [{"return_pct": -2.0} for _ in range(50)]
        result = monte_carlo_stress(trades, iterations=200)
        assert result["positive_pct"] < 10

    def test_empty_trades(self):
        from rigorous_backtest import monte_carlo_stress
        result = monte_carlo_stress([], iterations=100)
        assert result["iterations"] == 0
        assert result["positive_pct"] == 0.0


# ---------------------------------------------------------------------------
# Regime analysis gate
# ---------------------------------------------------------------------------

class TestRegimeAnalysis:
    def test_profitable_across_regimes(self):
        from rigorous_backtest import regime_analysis
        trades = (
            [{"return_pct": 3.0, "regime": "bull"} for _ in range(10)]
            + [{"return_pct": 2.0, "regime": "bear"} for _ in range(10)]
            + [{"return_pct": 1.0, "regime": "sideways"} for _ in range(10)]
        )
        result = regime_analysis(trades)
        assert result["regimes_profitable"] == 3
        assert result["regimes_tested"] == 3

    def test_one_regime_only(self):
        from rigorous_backtest import regime_analysis
        trades = (
            [{"return_pct": 3.0, "regime": "bull"} for _ in range(10)]
            + [{"return_pct": -2.0, "regime": "bear"} for _ in range(10)]
        )
        result = regime_analysis(trades)
        assert result["regimes_profitable"] == 1

    def test_missing_regime_tag(self):
        from rigorous_backtest import regime_analysis
        trades = [{"return_pct": 1.0} for _ in range(10)]
        result = regime_analysis(trades)
        assert result["regimes_profitable"] == 0  # unknown doesn't count
        assert "unknown" in result["per_regime"]


# ---------------------------------------------------------------------------
# Capacity analysis gate
# ---------------------------------------------------------------------------

class TestCapacity:
    def test_small_positions_high_capacity(self):
        from rigorous_backtest import capacity_analysis
        trades = [
            {"cost_basis": 500, "avg_daily_dollar_volume": 10_000_000}
            for _ in range(30)
        ]
        result = capacity_analysis(trades, initial_capital=10000)
        assert result["max_pct_of_volume"] < 0.001
        assert result["capacity_usd"] > 10000

    def test_large_positions_low_capacity(self):
        from rigorous_backtest import capacity_analysis
        trades = [
            {"cost_basis": 5000, "avg_daily_dollar_volume": 100_000}  # 5%!
            for _ in range(30)
        ]
        result = capacity_analysis(trades, initial_capital=10000)
        assert result["max_pct_of_volume"] > 0.01

    def test_missing_volume_uses_approximation(self):
        from rigorous_backtest import capacity_analysis
        trades = [{"cost_basis": 1000} for _ in range(10)]
        result = capacity_analysis(trades, initial_capital=10000)
        assert "approximated" in result


# ---------------------------------------------------------------------------
# Validation persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_and_retrieve(self, tmp_path):
        from rigorous_backtest import save_validation, get_recent_validations

        db = str(tmp_path / "test_validations.db")
        fake_result = {
            "verdict": "PASS",
            "score": 100.0,
            "passed_gates": ["sharpe", "win_rate"],
            "failed_gates": [],
            "metrics": {"baseline": {"sharpe_ratio": 1.5}},
            "config": {"market_type": "midcap"},
            "elapsed_sec": 12.3,
        }
        row_id = save_validation("test_strategy", fake_result, db_path=db)
        assert row_id > 0

        rows = get_recent_validations(limit=5, db_path=db)
        assert len(rows) == 1
        assert rows[0]["strategy_name"] == "test_strategy"
        assert rows[0]["verdict"] == "PASS"

    def test_empty_db_returns_empty_list(self, tmp_path):
        from rigorous_backtest import get_recent_validations
        rows = get_recent_validations(db_path=str(tmp_path / "nonexistent.db"))
        assert rows == []


# ---------------------------------------------------------------------------
# Thresholds sanity
# ---------------------------------------------------------------------------

class TestThresholds:
    def test_thresholds_are_reasonable(self):
        from rigorous_backtest import THRESHOLDS
        assert THRESHOLDS["min_sharpe"] >= 1.0
        assert THRESHOLDS["max_drawdown_pct"] < 0  # negative percent
        assert 0 < THRESHOLDS["max_p_value"] < 0.1
        assert 0 < THRESHOLDS["max_pct_daily_volume"] <= 0.05
