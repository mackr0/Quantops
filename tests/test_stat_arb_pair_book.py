"""Tests for stat_arb_pair_book — Item 1b foundation.

Tests use synthetic series with KNOWN properties so the math can be
verified deterministically:
  - A pair built from a shared random walk + small noise IS cointegrated
  - Two independent random walks are NOT cointegrated
  - Hedge ratio recovery accuracy
  - Half-life recovery on a known mean-reverting process
  - Z-score sign and magnitude on known spreads
  - Universe scan: planted cointegrated pair survives, random pairs don't
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# Deterministic RNG so test results are stable across runs
RNG = np.random.default_rng(seed=42)


def _random_walk(n: int, start: float = 100.0, sigma: float = 1.0,
                  rng: Optional[np.random.Generator] = None) -> np.ndarray:
    """Generate a length-n random walk: independent Gaussian increments."""
    rng = rng or RNG
    increments = rng.normal(0, sigma, size=n)
    return start + np.cumsum(increments)


def _cointegrated_pair(n: int = 200, hedge_ratio: float = 1.5,
                        noise_sigma: float = 0.5,
                        rng: Optional[np.random.Generator] = None) -> tuple:
    """Build a planted-cointegrated pair.

    Construction: B is a random walk. A = hedge_ratio × B + stationary noise.
    By construction (A − hedge_ratio × B) is stationary, so the pair is
    cointegrated and the EG test should reject the null with low p-value.
    """
    rng = rng or np.random.default_rng(seed=123)
    B = _random_walk(n, start=100.0, sigma=1.0, rng=rng)
    noise = rng.normal(0, noise_sigma, size=n)
    A = hedge_ratio * B + noise
    return A, B


class TestEngleGranger:
    def test_planted_cointegrated_pair_detected(self):
        """A pair built to be cointegrated should yield p < 0.05."""
        from stat_arb_pair_book import engle_granger
        A, B = _cointegrated_pair(n=200, hedge_ratio=1.5, noise_sigma=0.5)
        result = engle_granger(A, B)
        assert result["p_value"] < 0.05, (
            f"Planted cointegrated pair rejected: p={result['p_value']:.3f}"
        )
        # Hedge ratio recovered within 5%
        assert abs(result["hedge_ratio"] - 1.5) < 0.075
        # High correlation by construction
        assert result["correlation"] > 0.95

    def test_independent_random_walks_not_cointegrated(self):
        """Two independent random walks fail the EG test (high p)."""
        from stat_arb_pair_book import engle_granger
        rng = np.random.default_rng(seed=999)
        A = _random_walk(200, sigma=1.0, rng=rng)
        B = _random_walk(200, sigma=1.0, rng=rng)
        result = engle_granger(A, B)
        # Most random pairs give p > 0.05; we tolerate occasional false
        # positives but the typical case must hold.
        assert result["p_value"] > 0.10, (
            f"Independent random walks falsely cointegrated: "
            f"p={result['p_value']:.3f}"
        )

    def test_short_series_returns_safe_default(self):
        """ADF needs 30+ obs; below that we return p=1.0 (rejected)."""
        from stat_arb_pair_book import engle_granger
        result = engle_granger([100, 101, 102], [100, 101, 102])
        assert result["p_value"] == 1.0
        assert result["n_obs"] == 3

    def test_mismatched_lengths_returns_safe_default(self):
        from stat_arb_pair_book import engle_granger
        result = engle_granger([100] * 50, [100] * 40)
        assert result["p_value"] == 1.0

    def test_nan_input_returns_safe_default(self):
        from stat_arb_pair_book import engle_granger
        a = list(range(50))
        b = [float("nan")] * 50
        result = engle_granger(a, b)
        assert result["p_value"] == 1.0


class TestHalfLife:
    def test_strongly_mean_reverting_series_short_half_life(self):
        """A strongly mean-reverting AR(1) has a short half-life."""
        from stat_arb_pair_book import _half_life
        # Δs_t = -0.4 * s_{t-1} + ε  → half-life = -ln(2)/ln(0.6) ≈ 1.36
        rng = np.random.default_rng(seed=7)
        n = 500
        s = np.zeros(n)
        for t in range(1, n):
            s[t] = s[t-1] - 0.4 * s[t-1] + rng.normal(0, 0.1)
        hl = _half_life(s)
        assert 1.0 < hl < 2.0, f"Expected half-life ~1.36; got {hl:.2f}"

    def test_random_walk_infinite_half_life(self):
        """A pure random walk isn't mean-reverting → half-life = inf."""
        from stat_arb_pair_book import _half_life
        rw = _random_walk(500, sigma=1.0,
                           rng=np.random.default_rng(seed=11))
        hl = _half_life(rw)
        # Random walk should give infinite (or at least very large) half-life.
        # Test allows finite values >> our tradeable range.
        from stat_arb_pair_book import MAX_HALF_LIFE_DAYS
        assert hl == float("inf") or hl > MAX_HALF_LIFE_DAYS


class TestSpreadZScore:
    def test_spread_at_mean_returns_zero(self):
        """When the current spread equals the mean, z = 0."""
        from stat_arb_pair_book import compute_spread_zscore
        # Constant spread series: A − 1.0·B = 0 always.
        A = list(range(100))
        B = list(range(100))
        z = compute_spread_zscore(A, B, hedge_ratio=1.0, lookback=60)
        # Constant spread → std == 0 → returns None (degenerate)
        assert z is None

    def test_spread_above_mean_returns_positive(self):
        """A current spread above the lookback mean yields z > 0."""
        from stat_arb_pair_book import compute_spread_zscore
        rng = np.random.default_rng(seed=3)
        # Random spread for first 60 bars, then jump up for the last bar
        spread_history = rng.normal(0, 1, size=59)
        # spread = A − 1.0·B; build A = B + spread so the spread is what we want
        B = np.linspace(100, 110, 60)
        A = B + np.append(spread_history, [5.0])  # last spread = 5σ
        z = compute_spread_zscore(A, B, hedge_ratio=1.0, lookback=60)
        assert z is not None
        assert z > 2.0, f"Expected positive z-score for wide spread, got {z}"

    def test_spread_below_mean_returns_negative(self):
        from stat_arb_pair_book import compute_spread_zscore
        rng = np.random.default_rng(seed=4)
        spread_history = rng.normal(0, 1, size=59)
        B = np.linspace(100, 110, 60)
        A = B + np.append(spread_history, [-5.0])
        z = compute_spread_zscore(A, B, hedge_ratio=1.0, lookback=60)
        assert z is not None
        assert z < -2.0

    def test_insufficient_history_returns_none(self):
        from stat_arb_pair_book import compute_spread_zscore
        z = compute_spread_zscore([1, 2, 3], [1, 2, 3],
                                     hedge_ratio=1.0, lookback=60)
        assert z is None


class TestIsPairTradeable:
    def test_passes_when_all_filters_pass(self):
        from stat_arb_pair_book import is_pair_tradeable
        result = {
            "p_value": 0.01, "correlation": 0.85,
            "half_life_days": 5.0, "hedge_ratio": 1.0, "n_obs": 200,
        }
        assert is_pair_tradeable(result) is True

    def test_rejects_high_pvalue(self):
        from stat_arb_pair_book import is_pair_tradeable
        result = {"p_value": 0.20, "correlation": 0.85,
                  "half_life_days": 5.0, "hedge_ratio": 1.0, "n_obs": 200}
        assert is_pair_tradeable(result) is False

    def test_rejects_low_correlation(self):
        from stat_arb_pair_book import is_pair_tradeable
        result = {"p_value": 0.01, "correlation": 0.30,
                  "half_life_days": 5.0, "hedge_ratio": 1.0, "n_obs": 200}
        assert is_pair_tradeable(result) is False

    def test_rejects_too_slow_mean_reversion(self):
        """A 60-day half-life is too slow to trade — typical hold is days."""
        from stat_arb_pair_book import is_pair_tradeable
        result = {"p_value": 0.01, "correlation": 0.85,
                  "half_life_days": 60.0, "hedge_ratio": 1.0, "n_obs": 200}
        assert is_pair_tradeable(result) is False

    def test_rejects_too_fast_mean_reversion(self):
        """Half-life < 1 day is noise, not signal."""
        from stat_arb_pair_book import is_pair_tradeable
        result = {"p_value": 0.01, "correlation": 0.85,
                  "half_life_days": 0.5, "hedge_ratio": 1.0, "n_obs": 200}
        assert is_pair_tradeable(result) is False


class TestFindCointegratedPairs:
    def test_planted_pair_recovered_from_universe(self):
        """Plant ONE cointegrated pair (A,B) in a universe of mostly random
        walks; the scanner should find at least the planted pair."""
        from stat_arb_pair_book import find_cointegrated_pairs
        rng = np.random.default_rng(seed=42)
        A, B = _cointegrated_pair(n=200, hedge_ratio=1.5, noise_sigma=0.3,
                                    rng=rng)
        # 5 unrelated random walks
        noise_syms = {f"NOISE{i}": _random_walk(200, sigma=1.0, rng=rng)
                      for i in range(5)}
        all_series = {"PLANTED_A": A, "PLANTED_B": B, **noise_syms}

        def fetch(sym):
            return all_series.get(sym)

        symbols = list(all_series.keys())
        pairs = find_cointegrated_pairs(symbols, price_history=fetch)
        labels = [p.label for p in pairs]
        # Either (PLANTED_A, PLANTED_B) or (PLANTED_B, PLANTED_A) order
        assert "PLANTED_A/PLANTED_B" in labels or "PLANTED_B/PLANTED_A" in labels

    def test_empty_universe_returns_empty(self):
        from stat_arb_pair_book import find_cointegrated_pairs
        pairs = find_cointegrated_pairs([], price_history=lambda s: None)
        assert pairs == []

    def test_skips_symbols_without_data(self):
        from stat_arb_pair_book import find_cointegrated_pairs
        pairs = find_cointegrated_pairs(
            ["A", "B"], price_history=lambda s: None)
        assert pairs == []

    def test_max_pairs_cap_respected(self):
        """If 20 cointegrated pairs are found, the cap should limit
        the return to max_pairs."""
        from stat_arb_pair_book import find_cointegrated_pairs
        rng = np.random.default_rng(seed=7)
        # Plant 5 strongly cointegrated pairs (A1/B1, A2/B2, …)
        all_series = {}
        for i in range(5):
            A, B = _cointegrated_pair(n=200, hedge_ratio=1.0 + i*0.1,
                                       noise_sigma=0.2, rng=rng)
            all_series[f"A{i}"] = A
            all_series[f"B{i}"] = B
        pairs = find_cointegrated_pairs(
            list(all_series.keys()),
            price_history=lambda s: all_series.get(s),
            max_pairs=3,
        )
        assert len(pairs) <= 3


class TestPairDataclass:
    def test_label_renders_a_over_b(self):
        from stat_arb_pair_book import Pair
        p = Pair(symbol_a="KO", symbol_b="PEP",
                 hedge_ratio=1.5, p_value=0.01,
                 half_life_days=5.0, correlation=0.92)
        assert p.label == "KO/PEP"
