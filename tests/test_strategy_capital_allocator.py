"""Item 6b — strategy-level capital allocator tests.

Verifies:
  - Empty / single-strategy edge cases
  - Strategies with stronger sharpe × win-rate get higher weight
  - Weights bounded in [SCALE_FLOOR, SCALE_CEILING]
  - Mean weight ≈ 1.0 (mean-preserving across strategy set)
  - New strategies (< MIN_SAMPLES) get median fallback
  - All-no-data → all 1.0
  - Render block: empty when single strategy; populated otherwise
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _metrics(n=20, sharpe=1.0, win_rate=0.55):
    """Mock compute_rolling_metrics return shape."""
    return {
        "n_predictions": n,
        "wins": int(n * win_rate),
        "losses": n - int(n * win_rate),
        "win_rate": win_rate,
        "avg_return_pct": 1.5,
        "sharpe_ratio": sharpe,
        "profit_factor": 1.5,
    }


class TestComputeStrategyWeights:
    def test_empty_input_returns_empty(self):
        from strategy_capital_allocator import compute_strategy_weights
        assert compute_strategy_weights("/tmp/nonexistent.db", []) == {}

    def test_single_strategy_returns_unit_weight(self):
        from strategy_capital_allocator import compute_strategy_weights
        result = compute_strategy_weights("/tmp/x.db", ["momentum_breakout"])
        assert result == {"momentum_breakout": 1.0}

    def test_stronger_strategy_gets_higher_weight(self):
        """Strategy A has sharpe 2.0 + 65% WR; Strategy B has sharpe
        0.5 + 45% WR. A should get a higher weight than B."""
        from strategy_capital_allocator import compute_strategy_weights
        metrics_map = {
            "strong": _metrics(n=50, sharpe=2.0, win_rate=0.65),
            "weak": _metrics(n=50, sharpe=0.5, win_rate=0.45),
        }
        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lambda db, s, **kw: metrics_map[s]):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["strong", "weak"],
            )
        assert weights["strong"] > weights["weak"]

    def test_weights_bounded(self):
        """Even an extreme outlier strategy should not push weight
        beyond the [0.25, 2.0] bounds."""
        from strategy_capital_allocator import (
            compute_strategy_weights, SCALE_FLOOR, SCALE_CEILING,
        )
        metrics_map = {
            "amazing": _metrics(n=100, sharpe=10.0, win_rate=0.95),
            "terrible": _metrics(n=100, sharpe=-3.0, win_rate=0.20),
            "ok": _metrics(n=50, sharpe=1.0, win_rate=0.55),
        }
        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lambda db, s, **kw: metrics_map[s]):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["amazing", "terrible", "ok"],
            )
        for s, w in weights.items():
            assert SCALE_FLOOR <= w <= SCALE_CEILING, (
                f"{s}={w} out of bounds [{SCALE_FLOOR}, {SCALE_CEILING}]"
            )

    def test_mean_weight_approximately_one(self):
        """Mean weight should be near 1.0 so total exposure is
        unchanged from the unweighted baseline."""
        from strategy_capital_allocator import compute_strategy_weights
        metrics_map = {
            "a": _metrics(n=50, sharpe=1.5, win_rate=0.60),
            "b": _metrics(n=50, sharpe=0.8, win_rate=0.50),
            "c": _metrics(n=50, sharpe=1.2, win_rate=0.55),
        }
        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lambda db, s, **kw: metrics_map[s]):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["a", "b", "c"],
            )
        mean = sum(weights.values()) / len(weights)
        # Allow some drift from clamping; should stay within 10% of 1.0
        assert 0.85 <= mean <= 1.15

    def test_new_strategy_gets_median_weight(self):
        """A strategy with insufficient samples (n < MIN_SAMPLES)
        should get the median weight of the others, not auto-zero."""
        from strategy_capital_allocator import compute_strategy_weights
        metrics_map = {
            "old_strong": _metrics(n=50, sharpe=2.0, win_rate=0.60),
            "old_weak": _metrics(n=50, sharpe=0.5, win_rate=0.45),
            "brand_new": _metrics(n=3, sharpe=0.0, win_rate=0.0),  # not enough data
        }
        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lambda db, s, **kw: metrics_map[s]):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["old_strong", "old_weak", "brand_new"],
            )
        # The new strategy should have a weight between the other two
        # (the median imputation), not at the floor.
        assert weights["brand_new"] > 0.5  # not at the floor

    def test_all_no_data_returns_all_one(self):
        from strategy_capital_allocator import compute_strategy_weights
        metrics_map = {
            s: _metrics(n=2) for s in ["a", "b", "c"]  # all below MIN_SAMPLES
        }
        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lambda db, s, **kw: metrics_map[s]):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["a", "b", "c"],
            )
        assert all(w == 1.0 for w in weights.values())

    def test_metrics_failure_returns_neutral(self):
        """When compute_rolling_metrics raises for a strategy, that
        strategy is treated as unknown (median fallback)."""
        from strategy_capital_allocator import compute_strategy_weights

        def lookup(db, s, **kw):
            if s == "broken":
                raise RuntimeError("sql failed")
            return _metrics(n=50, sharpe=1.0, win_rate=0.55)

        with patch("alpha_decay.compute_rolling_metrics",
                   side_effect=lookup):
            weights = compute_strategy_weights(
                "/tmp/x.db", ["working", "broken"],
            )
        assert "broken" in weights
        # Both should be reasonable; broken gets median
        assert weights["broken"] > 0.5


class TestRenderWeightsForPrompt:
    def test_empty_returns_empty(self):
        from strategy_capital_allocator import render_weights_for_prompt
        assert render_weights_for_prompt({}) == ""
        assert render_weights_for_prompt({"only_one": 1.0}) == ""

    def test_renders_block_with_arrows(self):
        from strategy_capital_allocator import render_weights_for_prompt
        out = render_weights_for_prompt({
            "strong": 1.5, "weak": 0.6, "neutral": 1.0,
        })
        assert "STRATEGY CAPITAL WEIGHTS" in out
        assert "strong" in out and "weak" in out and "neutral" in out
        # Up/down arrows indicate scaling direction
        assert "⬆" in out  # strong is ≥ 1.2
        assert "⬇" in out  # weak is ≤ 0.8