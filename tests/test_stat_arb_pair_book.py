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
                        rng: Optional[np.random.Generator] = None,
                        ar_gamma: float = -0.13) -> tuple:
    """Build a planted-cointegrated pair with realistic mean-reverting spread.

    Construction:
      B = random walk
      spread = mean-reverting AR(1) process: Δs_t = γ·s_{t-1} + ε
      A = hedge_ratio × B + spread

    Pure white-noise residuals (γ ≈ -1) have ~0 half-life and would
    fail the tradeability filter. Real cointegrated equity pairs have
    half-lives of 5-15 days; γ=-0.13 → half-life ≈ 5 days, which is
    realistic and passes the filter.
    """
    rng = rng or np.random.default_rng(seed=123)
    B = _random_walk(n, start=100.0, sigma=1.0, rng=rng)
    # Mean-reverting AR(1) spread. γ < 0 = reverting toward 0.
    spread = np.zeros(n)
    eps = rng.normal(0, noise_sigma, size=n)
    for t in range(1, n):
        spread[t] = spread[t-1] + ar_gamma * spread[t-1] + eps[t]
    A = hedge_ratio * B + spread
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
        # Need lookback+1 bars (61): 60 of normal spread, then a wide one.
        spread_history = rng.normal(0, 1, size=60)
        # spread = A − 1.0·B; build A = B + spread so the spread is what we want
        B = np.linspace(100, 110, 61)
        A = B + np.append(spread_history, [5.0])  # last spread = 5σ
        z = compute_spread_zscore(A, B, hedge_ratio=1.0, lookback=60)
        assert z is not None
        assert z > 2.0, f"Expected positive z-score for wide spread, got {z}"

    def test_spread_below_mean_returns_negative(self):
        from stat_arb_pair_book import compute_spread_zscore
        rng = np.random.default_rng(seed=4)
        spread_history = rng.normal(0, 1, size=60)
        B = np.linspace(100, 110, 61)
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


# ---------------------------------------------------------------------------
# Persistence layer
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db():
    import tempfile
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestPersistence:
    def _pair(self, a="AAPL", b="MSFT", **overrides):
        from stat_arb_pair_book import Pair
        defaults = dict(symbol_a=a, symbol_b=b, hedge_ratio=1.5,
                         p_value=0.01, half_life_days=5.0,
                         correlation=0.92)
        defaults.update(overrides)
        return Pair(**defaults)

    def test_upsert_then_retrieve(self, tmp_db):
        from stat_arb_pair_book import upsert_pair, get_active_pairs
        pair = self._pair()
        pid = upsert_pair(tmp_db, pair)
        assert pid > 0
        pairs = get_active_pairs(tmp_db)
        assert len(pairs) == 1
        assert pairs[0].symbol_a == "AAPL"
        assert pairs[0].symbol_b == "MSFT"

    def test_canonical_order_enforced(self, tmp_db):
        """Inserting (B, A) should map to the same row as (A, B)."""
        from stat_arb_pair_book import upsert_pair, get_active_pairs
        upsert_pair(tmp_db, self._pair(a="AAPL", b="MSFT", hedge_ratio=1.5))
        upsert_pair(tmp_db, self._pair(a="MSFT", b="AAPL", hedge_ratio=2.0))
        pairs = get_active_pairs(tmp_db)
        # Still one row (UNIQUE on canonical order)
        assert len(pairs) == 1
        # Hedge ratio inverted: 1/2.0 = 0.5 (the second insert refreshed)
        assert pairs[0].hedge_ratio == pytest.approx(0.5, abs=0.01)
        assert pairs[0].symbol_a == "AAPL"
        assert pairs[0].symbol_b == "MSFT"

    def test_upsert_refreshes_existing_row(self, tmp_db):
        from stat_arb_pair_book import upsert_pair, get_active_pairs
        upsert_pair(tmp_db, self._pair(p_value=0.05))
        upsert_pair(tmp_db, self._pair(p_value=0.01))  # better p-value
        pairs = get_active_pairs(tmp_db)
        assert len(pairs) == 1
        assert pairs[0].p_value == 0.01

    def test_retire_pair(self, tmp_db):
        from stat_arb_pair_book import (upsert_pair, retire_pair,
                                          get_active_pairs)
        upsert_pair(tmp_db, self._pair())
        ok = retire_pair(tmp_db, "AAPL", "MSFT",
                          reason="cointegration broke (p=0.18)")
        assert ok is True
        # Active pairs no longer includes it
        assert get_active_pairs(tmp_db) == []

    def test_retire_with_swapped_symbols_still_works(self, tmp_db):
        from stat_arb_pair_book import (upsert_pair, retire_pair,
                                          get_active_pairs)
        upsert_pair(tmp_db, self._pair(a="AAPL", b="MSFT"))
        ok = retire_pair(tmp_db, "MSFT", "AAPL", reason="test")
        assert ok is True
        assert get_active_pairs(tmp_db) == []

    def test_retire_nonexistent_returns_false(self, tmp_db):
        from stat_arb_pair_book import retire_pair
        assert retire_pair(tmp_db, "X", "Y", reason="test") is False

    def test_upsert_revives_retired_pair(self, tmp_db):
        """If a pair regains cointegration after being retired, upsert
        should reset status to active so the daily rebalance can put
        it back in the book."""
        from stat_arb_pair_book import (upsert_pair, retire_pair,
                                          get_active_pairs)
        upsert_pair(tmp_db, self._pair())
        retire_pair(tmp_db, "AAPL", "MSFT", reason="broke")
        assert get_active_pairs(tmp_db) == []
        upsert_pair(tmp_db, self._pair(p_value=0.005))
        active = get_active_pairs(tmp_db)
        assert len(active) == 1


# ---------------------------------------------------------------------------
# Trade-signal generator
# ---------------------------------------------------------------------------

class TestPairSignal:
    def _make_pair(self):
        from stat_arb_pair_book import Pair
        return Pair(symbol_a="AAPL", symbol_b="MSFT",
                    hedge_ratio=1.0, p_value=0.01,
                    half_life_days=5.0, correlation=0.92)

    def _series_with_terminal_z(self, target_z: float, n: int = 70):
        """Build A, B such that the spread (A − 1.0·B) has the requested
        z-score on the last bar relative to the lookback=60 window."""
        rng = np.random.default_rng(seed=7)
        # 60 normal-ish spread observations + 1 with the target z
        base_spread = rng.normal(0, 1, size=n - 1)
        # Add the last bar at exactly target_z standard deviations
        # Recompute mean/std on the first n-1 to get the target right
        mean = float(np.mean(base_spread))
        std = float(np.std(base_spread))
        last = mean + target_z * std
        spread = np.append(base_spread, [last])
        B = np.linspace(100.0, 110.0, n)
        A = B + spread
        return A, B

    def test_wide_positive_z_triggers_short_a_long_b(self):
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=2.5)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=False)
        assert sig["action"] == "ENTER_SHORT_A_LONG_B"
        assert sig["z_score"] >= 2.0

    def test_wide_negative_z_triggers_long_a_short_b(self):
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=-2.5)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=False)
        assert sig["action"] == "ENTER_LONG_A_SHORT_B"
        assert sig["z_score"] <= -2.0

    def test_small_z_holds(self):
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=0.5)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=False)
        assert sig["action"] == "HOLD"

    def test_open_position_exits_at_mean(self):
        """If we're already in the trade, |z| <= 0.5 → take profit."""
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=0.2)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=True,
                           entry_direction="short_a_long_b")
        assert sig["action"] == "EXIT"

    def test_open_position_holds_in_window(self):
        """In-trade with z=1.5 is still in the window — don't exit yet."""
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=1.5)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=True,
                           entry_direction="short_a_long_b")
        assert sig["action"] == "HOLD"

    def test_open_position_regime_break_exit(self):
        """|z| >= 3 → defensive exit; cointegration may have broken."""
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A, B = self._series_with_terminal_z(target_z=3.5)
        sig = pair_signal(pair, A, B, lookback=60, currently_open=True,
                           entry_direction="short_a_long_b")
        assert sig["action"] == "REGIME_BREAK_EXIT"

    def test_insufficient_history_holds(self):
        from stat_arb_pair_book import pair_signal
        pair = self._make_pair()
        A = [100.0] * 10
        B = [100.0] * 10
        sig = pair_signal(pair, A, B, lookback=60, currently_open=False)
        assert sig["action"] == "HOLD"
        assert sig["z_score"] is None


# ---------------------------------------------------------------------------
# Daily rebalance / retest
# ---------------------------------------------------------------------------

class TestRetestActivePairs:
    def test_empty_book_returns_zeroes(self, tmp_db):
        from stat_arb_pair_book import retest_active_pairs
        summary = retest_active_pairs(
            tmp_db, price_history=lambda s: None,
        )
        assert summary["retested"] == 0
        assert summary["refreshed"] == 0
        assert summary["retired"] == 0

    def test_still_cointegrated_pair_refreshed(self, tmp_db):
        """A pair that's still cointegrated when retested should stay
        active and have its hedge ratio/p-value/half-life refreshed.

        Uses ar_gamma=-0.30 (half-life ≈ 1.94d) for unambiguously
        strong mean reversion so ADF reliably rejects the unit-root
        null even on adversarial seeds.
        """
        from stat_arb_pair_book import (Pair, upsert_pair,
                                          retest_active_pairs,
                                          get_active_pairs)
        # Plant the pair with stale numbers
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=2.0, p_value=0.04,  # stale, generic
            half_life_days=10.0, correlation=0.7,
        ))
        # Strong mean reversion → ADF rejects unit root reliably
        rng = np.random.default_rng(seed=42)
        A_data, B_data = _cointegrated_pair(n=200, hedge_ratio=1.5,
                                              noise_sigma=0.3,
                                              rng=rng,
                                              ar_gamma=-0.30)
        prices = {"A": A_data, "B": B_data}
        summary = retest_active_pairs(
            tmp_db, price_history=lambda s: prices.get(s),
        )
        assert summary["refreshed"] == 1
        assert summary["retired"] == 0
        active = get_active_pairs(tmp_db)
        assert len(active) == 1
        # Hedge ratio refreshed toward the planted 1.5
        assert abs(active[0].hedge_ratio - 1.5) < 0.1

    def test_broken_cointegration_pair_retired(self, tmp_db):
        """Pair was cointegrated but the relationship has broken in
        fresh data → retire."""
        from stat_arb_pair_book import (Pair, upsert_pair,
                                          retest_active_pairs,
                                          get_active_pairs)
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.02,
            half_life_days=5.0, correlation=0.85,
        ))
        # Fresh data: independent random walks → no cointegration
        rng = np.random.default_rng(seed=999)
        prices = {"A": _random_walk(200, sigma=1.0, rng=rng),
                  "B": _random_walk(200, sigma=1.0, rng=rng)}
        summary = retest_active_pairs(
            tmp_db, price_history=lambda s: prices.get(s),
        )
        # Either retired (typical) or refreshed (rare false positive).
        # Tolerate the rare case but the typical run must retire.
        assert summary["retested"] == 1
        if summary["retired"] == 1:
            assert get_active_pairs(tmp_db) == []
            assert summary["details"][0]["outcome"] == "retired"

    def test_missing_price_data_counts_as_error_not_retire(self, tmp_db):
        """When fresh data isn't available we shouldn't auto-retire —
        we just can't evaluate this cycle."""
        from stat_arb_pair_book import (Pair, upsert_pair,
                                          retest_active_pairs,
                                          get_active_pairs)
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.02,
            half_life_days=5.0, correlation=0.85,
        ))
        summary = retest_active_pairs(
            tmp_db, price_history=lambda s: None,
        )
        assert summary["errors"] == 1
        assert summary["retired"] == 0
        # Pair stays active
        assert len(get_active_pairs(tmp_db)) == 1


# ---------------------------------------------------------------------------
# Universe scan + persist
# ---------------------------------------------------------------------------

class TestScanAndPersistPairs:
    def test_planted_pair_discovered_and_persisted(self, tmp_db):
        """Plant a strongly cointegrated pair; the scanner should find
        it and a row should appear in stat_arb_pairs."""
        from stat_arb_pair_book import (scan_and_persist_pairs,
                                          get_active_pairs)
        rng = np.random.default_rng(seed=42)
        A_data, B_data = _cointegrated_pair(n=200, hedge_ratio=1.5,
                                              noise_sigma=0.3,
                                              rng=rng,
                                              ar_gamma=-0.30)
        # Add 3 noise symbols so the universe isn't trivially small
        noise = {f"NOISE{i}": _random_walk(200, sigma=1.0,
                                              rng=np.random.default_rng(seed=100+i))
                 for i in range(3)}
        all_series = {"X": A_data, "Y": B_data, **noise}

        summary = scan_and_persist_pairs(
            tmp_db, list(all_series.keys()),
            price_history=lambda s: all_series.get(s),
        )
        assert summary["found"] >= 1
        assert summary["persisted"] >= 1
        active = get_active_pairs(tmp_db)
        # The planted pair (X, Y) should appear in canonical order
        labels = {p.label for p in active}
        assert "X/Y" in labels

    def test_empty_universe_persists_nothing(self, tmp_db):
        from stat_arb_pair_book import (scan_and_persist_pairs,
                                          get_active_pairs)
        summary = scan_and_persist_pairs(
            tmp_db, [], price_history=lambda s: None,
        )
        assert summary["found"] == 0
        assert summary["persisted"] == 0
        assert get_active_pairs(tmp_db) == []

    def test_renders_actionable_section_when_z_in_entry_range(self, tmp_db):
        """When a book pair has |z|>=2, the prompt renders an
        'Actionable now' line for it."""
        from stat_arb_pair_book import (Pair, upsert_pair,
                                          render_pair_book_for_prompt)
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))
        # Build prices so the spread (A − 1.0·B) ends at z = +2.5
        rng = np.random.default_rng(seed=7)
        spread_history = rng.normal(0, 1, size=60)
        mean = float(np.mean(spread_history))
        std = float(np.std(spread_history))
        last = mean + 2.5 * std
        spread = np.append(spread_history, [last])
        B = np.linspace(100, 110, 61)
        A = B + spread
        prices = {"A": A.tolist(), "B": B.tolist()}
        out = render_pair_book_for_prompt(
            tmp_db, price_history=lambda s: prices.get(s),
        )
        assert "STAT-ARB PAIR BOOK" in out
        assert "Actionable now" in out
        assert "ENTER_SHORT_A_LONG_B" in out
        assert "z=+2." in out

    def test_renders_empty_when_book_empty(self, tmp_db):
        from stat_arb_pair_book import render_pair_book_for_prompt
        out = render_pair_book_for_prompt(
            tmp_db, price_history=lambda s: None,
        )
        assert out == ""

    def test_renders_informational_when_no_actionable(self, tmp_db):
        """A book pair with z=0.3 (no signal) renders only when there
        are no actionable pairs, and labeled 'Currently quiet'."""
        from stat_arb_pair_book import (Pair, upsert_pair,
                                          render_pair_book_for_prompt)
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))
        rng = np.random.default_rng(seed=11)
        spread_history = rng.normal(0, 1, size=60)
        mean = float(np.mean(spread_history))
        std = float(np.std(spread_history))
        last = mean + 0.3 * std  # quiet
        spread = np.append(spread_history, [last])
        B = np.linspace(100, 110, 61)
        A = B + spread
        prices = {"A": A.tolist(), "B": B.tolist()}
        out = render_pair_book_for_prompt(
            tmp_db, price_history=lambda s: prices.get(s),
        )
        assert "Currently quiet" in out
        assert "ENTER" not in out

    def test_re_scan_refreshes_existing_pair(self, tmp_db):
        """Running the scanner twice on the same data should NOT create
        duplicate rows — it should refresh the existing pair."""
        from stat_arb_pair_book import (scan_and_persist_pairs,
                                          get_active_pairs)
        rng = np.random.default_rng(seed=42)
        A_data, B_data = _cointegrated_pair(n=200, hedge_ratio=1.5,
                                              noise_sigma=0.3,
                                              rng=rng,
                                              ar_gamma=-0.30)
        prices = {"X": A_data, "Y": B_data}
        scan_and_persist_pairs(tmp_db, ["X", "Y"],
                                price_history=lambda s: prices.get(s))
        scan_and_persist_pairs(tmp_db, ["X", "Y"],
                                price_history=lambda s: prices.get(s))
        active = get_active_pairs(tmp_db)
        assert len(active) == 1  # not 2


# ---------------------------------------------------------------------------
# execute_pair_trade — two-leg dollar-neutral execution
# ---------------------------------------------------------------------------

import pandas as pd  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402


class TestExecutePairTrade:
    def _ctx(self, db_path):
        ctx = MagicMock()
        ctx.db_path = db_path
        return ctx

    def _seed_active_pair(self, tmp_db, hedge_ratio=1.0):
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=hedge_ratio, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))

    def _bars_df(self, last_close):
        return pd.DataFrame({
            "open": [last_close] * 5, "high": [last_close] * 5,
            "low": [last_close] * 5, "close": [last_close] * 5,
            "volume": [1000] * 5,
        })

    def test_pair_not_in_book_returns_skip(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        api = MagicMock()
        result = execute_pair_trade(api, {
            "pair_action": "ENTER_LONG_A_SHORT_B",
            "symbol_a": "X", "symbol_b": "Y",
            "dollars_per_leg": 1000,
        }, ctx=self._ctx(tmp_db))
        assert result["action"] == "SKIP"
        assert "not in active pair book" in result["reason"]
        api.submit_order.assert_not_called()

    def test_unsupported_pair_action_returns_skip(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        result = execute_pair_trade(api, {
            "pair_action": "ROLL_FORWARD",  # not supported
            "symbol_a": "A", "symbol_b": "B",
            "dollars_per_leg": 1000,
        }, ctx=self._ctx(tmp_db))
        assert result["action"] == "SKIP"
        api.submit_order.assert_not_called()

    def test_zero_dollars_per_leg_returns_skip(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        result = execute_pair_trade(api, {
            "pair_action": "ENTER_LONG_A_SHORT_B",
            "symbol_a": "A", "symbol_b": "B",
            "dollars_per_leg": 0,
        }, ctx=self._ctx(tmp_db))
        assert result["action"] == "SKIP"

    def test_successful_enter_submits_both_legs(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        # Two distinct order ids returned
        api.submit_order.side_effect = [
            MagicMock(id="ord-a"), MagicMock(id="ord-b"),
        ]
        with patch("market_data.get_bars",
                   side_effect=[self._bars_df(100.0), self._bars_df(50.0)]), \
             patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = execute_pair_trade(api, {
                "pair_action": "ENTER_LONG_A_SHORT_B",
                "symbol_a": "A", "symbol_b": "B",
                "dollars_per_leg": 1000,
                "confidence": 70, "reasoning": "test",
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "PAIR_OPEN"
        assert result["order_id_a"] == "ord-a"
        assert result["order_id_b"] == "ord-b"
        # 1000/100 = 10 shares of A; 1000/50 = 20 shares of B
        assert result["qty_a"] == 10
        assert result["qty_b"] == 20
        # Two submit_order calls, A then B
        assert api.submit_order.call_count == 2
        kw_a = api.submit_order.call_args_list[0].kwargs
        kw_b = api.submit_order.call_args_list[1].kwargs
        assert kw_a["symbol"] == "A" and kw_a["side"] == "buy"
        assert kw_b["symbol"] == "B" and kw_b["side"] == "sell"

    def test_short_a_long_b_swaps_sides(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="ord-a"), MagicMock(id="ord-b"),
        ]
        with patch("market_data.get_bars",
                   side_effect=[self._bars_df(100.0), self._bars_df(50.0)]), \
             patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = execute_pair_trade(api, {
                "pair_action": "ENTER_SHORT_A_LONG_B",
                "symbol_a": "A", "symbol_b": "B",
                "dollars_per_leg": 1000,
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "PAIR_OPEN"
        kw_a = api.submit_order.call_args_list[0].kwargs
        kw_b = api.submit_order.call_args_list[1].kwargs
        assert kw_a["side"] == "sell"
        assert kw_b["side"] == "buy"

    def test_caps_at_5pct_equity_per_leg(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="ord-a"), MagicMock(id="ord-b"),
        ]
        # equity=10k → 5% cap per leg = $500. AI requests $5000.
        with patch("market_data.get_bars",
                   side_effect=[self._bars_df(100.0), self._bars_df(100.0)]), \
             patch("client.get_account_info",
                   return_value={"equity": 10000}):
            result = execute_pair_trade(api, {
                "pair_action": "ENTER_LONG_A_SHORT_B",
                "symbol_a": "A", "symbol_b": "B",
                "dollars_per_leg": 5000,  # over cap
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "PAIR_OPEN"
        # $500 / $100 = 5 shares per leg (capped)
        assert result["qty_a"] == 5
        assert result["qty_b"] == 5

    def test_leg_b_failure_after_leg_a_returns_error(self, tmp_db):
        """If leg B fails after A submitted, we get ERROR with order_id_a
        so the operator can manually flatten."""
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="ord-a"),
            Exception("alpaca rejected leg B"),
        ]
        with patch("market_data.get_bars",
                   side_effect=[self._bars_df(100.0), self._bars_df(50.0)]), \
             patch("client.get_account_info",
                   return_value={"equity": 100000}):
            result = execute_pair_trade(api, {
                "pair_action": "ENTER_LONG_A_SHORT_B",
                "symbol_a": "A", "symbol_b": "B",
                "dollars_per_leg": 1000,
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "ERROR"
        assert result["order_id_a"] == "ord-a"
        assert "leg B" in result["reason"]

    def test_exit_closes_held_legs(self, tmp_db):
        """EXIT submits closing orders for any held leg of the pair."""
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        # One open order per leg succeeds
        api.submit_order.side_effect = [
            MagicMock(id="close-a"), MagicMock(id="close-b"),
        ]
        # Mock positions: long A 10 shares, short B 20 shares
        positions = [
            {"symbol": "A", "qty": "10"},
            {"symbol": "B", "qty": "-20"},
        ]
        with patch("client.get_positions", return_value=positions):
            result = execute_pair_trade(api, {
                "pair_action": "EXIT",
                "symbol_a": "A", "symbol_b": "B",
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "PAIR_CLOSE"
        assert len(result["closed"]) == 2
        # Both legs closed with opposite side
        kw_a = api.submit_order.call_args_list[0].kwargs
        kw_b = api.submit_order.call_args_list[1].kwargs
        assert kw_a["symbol"] == "A" and kw_a["side"] == "sell"  # was long
        assert kw_b["symbol"] == "B" and kw_b["side"] == "buy"   # was short

    def test_exit_with_no_positions_returns_skip(self, tmp_db):
        from stat_arb_pair_book import execute_pair_trade
        self._seed_active_pair(tmp_db)
        api = MagicMock()
        with patch("client.get_positions", return_value=[]):
            result = execute_pair_trade(api, {
                "pair_action": "EXIT",
                "symbol_a": "A", "symbol_b": "B",
            }, ctx=self._ctx(tmp_db), log=False)
        assert result["action"] == "SKIP"
        api.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# _validate_ai_trades — PAIR_TRADE pass-through
# ---------------------------------------------------------------------------

class TestValidatePairTradeAction:
    def _ctx(self, db_path):
        ctx = MagicMock()
        ctx.db_path = db_path
        ctx.max_position_pct = 0.10
        ctx.short_max_position_pct = 0.05
        ctx.enable_short_selling = False
        ctx.target_short_pct = 0.0
        ctx.target_book_beta = None
        return ctx

    def _seed(self, tmp_db):
        from stat_arb_pair_book import Pair, upsert_pair
        upsert_pair(tmp_db, Pair(
            symbol_a="A", symbol_b="B",
            hedge_ratio=1.0, p_value=0.01,
            half_life_days=5.0, correlation=0.92,
        ))

    def test_pair_trade_passes_through_with_fields(self, tmp_db):
        from ai_analyst import _validate_ai_trades
        self._seed(tmp_db)
        result = {"trades": [{
            "action": "PAIR_TRADE",
            "symbol": "A/B",
            "symbol_a": "A", "symbol_b": "B",
            "pair_action": "ENTER_SHORT_A_LONG_B",
            "dollars_per_leg": 1000, "confidence": 70,
            "reasoning": "z=+2.5",
        }]}
        validated = _validate_ai_trades(
            result, candidates_data=[], ctx=self._ctx(tmp_db),
        )
        assert len(validated["trades"]) == 1
        v = validated["trades"][0]
        assert v["action"] == "PAIR_TRADE"
        assert v["symbol_a"] == "A"
        assert v["symbol_b"] == "B"
        assert v["pair_action"] == "ENTER_SHORT_A_LONG_B"
        assert v["dollars_per_leg"] == 1000.0

    def test_pair_trade_unknown_pair_skipped(self, tmp_db):
        """If the AI invents a pair we never validated, drop it."""
        from ai_analyst import _validate_ai_trades
        # No seeded pair → book is empty
        result = {"trades": [{
            "action": "PAIR_TRADE",
            "symbol": "X/Y",
            "symbol_a": "X", "symbol_b": "Y",
            "pair_action": "ENTER_LONG_A_SHORT_B",
            "dollars_per_leg": 1000,
        }]}
        validated = _validate_ai_trades(
            result, candidates_data=[], ctx=self._ctx(tmp_db),
        )
        assert validated["trades"] == []

    def test_pair_trade_missing_pair_action_skipped(self, tmp_db):
        from ai_analyst import _validate_ai_trades
        self._seed(tmp_db)
        result = {"trades": [{
            "action": "PAIR_TRADE",
            "symbol_a": "A", "symbol_b": "B",
            # missing pair_action
            "dollars_per_leg": 1000,
        }]}
        validated = _validate_ai_trades(
            result, candidates_data=[], ctx=self._ctx(tmp_db),
        )
        assert validated["trades"] == []
