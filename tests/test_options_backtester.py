"""Phase H Layer 1 — historical IV approximation + Black-Scholes pricing
of arbitrary options at historical dates.

Synthetic backtester foundations. Tests verify:
  - historical_iv_approximation: computes annualized vol from
    bars correctly; returns None on insufficient data
  - historical_spot: returns the close at-or-before the as_of date
  - price_option_at_date: round-trip through compute_greeks; handles
    expired options as intrinsic-only; respects iv_override
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _build_bars(end_date, n_days, base=100.0, daily_sigma=0.01,
                  seed=42):
    """Build a synthetic OHLCV DataFrame with daily-frequency
    DatetimeIndex ending at end_date.

    Uses freq='D' (calendar days) rather than 'B' (business days) so
    n_days reliably matches index length regardless of the end_date's
    day of week. Filters in production code key off `index.date <=
    as_of` so the calendar-day index works fine for tests.
    """
    rng = np.random.default_rng(seed=seed)
    returns = rng.normal(0, daily_sigma, size=n_days)
    closes = base * np.cumprod(1 + returns)
    idx = pd.date_range(end=end_date, periods=n_days, freq="D")
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1000] * n_days,
    }, index=idx)


class TestHistoricalIvApproximation:
    def test_recovers_known_volatility(self):
        """Build bars with known daily_sigma; the annualized estimate
        should be close to daily_sigma * sqrt(252)."""
        from options_backtester import historical_iv_approximation
        as_of = date(2026, 5, 1)
        # daily_sigma=0.012 → annualized = 0.012 * sqrt(252) ≈ 0.190
        bars = _build_bars(as_of, n_days=60, daily_sigma=0.012)
        provider = lambda sym, dt, lookback: bars
        iv = historical_iv_approximation(
            "AAPL", as_of, lookback_days=30, bars_provider=provider,
        )
        assert iv is not None
        expected = 0.012 * math.sqrt(252)
        # ±20% tolerance — sample stdev varies on small samples
        assert abs(iv - expected) / expected < 0.30

    def test_higher_volatility_recovered(self):
        from options_backtester import historical_iv_approximation
        as_of = date(2026, 5, 1)
        # daily_sigma=0.025 → ~0.397 annualized
        bars = _build_bars(as_of, n_days=60, daily_sigma=0.025, seed=7)
        provider = lambda sym, dt, lookback: bars
        iv = historical_iv_approximation(
            "AAPL", as_of, lookback_days=30, bars_provider=provider,
        )
        assert iv is not None
        assert iv > 0.30  # well above the low-vol case

    def test_insufficient_data_returns_none(self):
        from options_backtester import historical_iv_approximation
        # Only 5 bars — below the half-of-lookback floor for 30-day lookback
        bars = _build_bars(date(2026, 5, 1), n_days=5)
        provider = lambda sym, dt, lookback: bars
        iv = historical_iv_approximation(
            "AAPL", date(2026, 5, 1), lookback_days=30,
            bars_provider=provider,
        )
        assert iv is None

    def test_provider_failure_returns_none(self):
        from options_backtester import historical_iv_approximation
        def failing(sym, dt, lookback):
            raise RuntimeError("alpaca down")
        iv = historical_iv_approximation(
            "AAPL", date(2026, 5, 1), bars_provider=failing,
        )
        assert iv is None

    def test_filters_to_dates_at_or_before_as_of(self):
        """The IV computation must NOT use future bars (look-ahead bias)."""
        from options_backtester import historical_iv_approximation
        as_of = date(2026, 5, 1)
        # Build bars that extend PAST as_of
        bars = _build_bars(as_of + timedelta(days=10), n_days=40,
                            daily_sigma=0.005, seed=123)
        # Append a high-vol jump AFTER as_of
        rng = np.random.default_rng(seed=999)
        future_idx = pd.date_range(start=as_of + timedelta(days=1),
                                       periods=10, freq="D")
        future_bars = pd.DataFrame({
            "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0 + rng.normal(0, 5, size=10),  # huge moves
            "volume": [1000] * 10,
        }, index=future_idx)
        bars = pd.concat([bars, future_bars])
        provider = lambda sym, dt, lookback: bars

        iv = historical_iv_approximation(
            "AAPL", as_of, lookback_days=30, bars_provider=provider,
        )
        assert iv is not None
        # Should NOT incorporate the post-as_of jumps → IV stays low
        assert iv < 0.20


class TestHistoricalSpot:
    def test_returns_close_at_as_of(self):
        from options_backtester import historical_spot
        as_of = date(2026, 5, 1)
        bars = _build_bars(as_of, n_days=10, base=150.0, daily_sigma=0)
        provider = lambda sym, dt, lookback: bars
        spot = historical_spot("AAPL", as_of, bars_provider=provider)
        assert spot == pytest.approx(150.0, abs=0.5)

    def test_no_data_returns_none(self):
        from options_backtester import historical_spot
        provider = lambda sym, dt, lookback: None
        assert historical_spot("AAPL", date(2026, 5, 1),
                                bars_provider=provider) is None


class TestPriceOptionAtDate:
    def test_atm_call_round_trip(self):
        """Build bars with known IV, price an ATM call, and verify
        the price matches a manual compute_greeks call with the
        same parameters."""
        from options_backtester import price_option_at_date
        from options_trader import compute_greeks

        as_of = date(2026, 5, 1)
        expiry = date(2026, 5, 31)  # 30 days
        bars = _build_bars(as_of, n_days=60, daily_sigma=0.012,
                            base=100.0)
        provider = lambda sym, dt, lookback: bars

        result = price_option_at_date(
            "AAPL", as_of, strike=100.0, expiry=expiry,
            is_call=True, bars_provider=provider,
        )
        assert result is not None
        assert "price" in result
        assert result["price"] > 0
        # Spot ≈ ending bar close; iv recovered from bars
        assert result["spot"] > 90 and result["spot"] < 110
        assert result["iv"] > 0.10
        # Sanity: re-compute greeks with the same iv → same price
        recomputed = compute_greeks(
            spot=result["spot"], strike=100.0, days_to_expiry=30,
            iv=result["iv"], is_call=True,
        )
        assert recomputed["price"] == pytest.approx(result["price"],
                                                       abs=0.01)

    def test_iv_override_used(self):
        from options_backtester import price_option_at_date
        as_of = date(2026, 5, 1)
        expiry = date(2026, 5, 31)
        bars = _build_bars(as_of, n_days=60, daily_sigma=0.012,
                            base=100.0)
        provider = lambda sym, dt, lookback: bars
        # Override IV at 80% — should make the option much more expensive
        cheap = price_option_at_date(
            "AAPL", as_of, 100.0, expiry, True, iv_override=0.10,
            bars_provider=provider,
        )
        rich = price_option_at_date(
            "AAPL", as_of, 100.0, expiry, True, iv_override=0.80,
            bars_provider=provider,
        )
        assert rich["price"] > cheap["price"] * 3
        assert cheap["iv"] == 0.10
        assert rich["iv"] == 0.80

    def test_expired_option_returns_intrinsic(self):
        """When expiry < as_of, return intrinsic value only."""
        from options_backtester import price_option_at_date
        as_of = date(2026, 5, 10)
        expiry = date(2026, 5, 1)  # already past
        bars = _build_bars(as_of, n_days=20, base=110.0, daily_sigma=0)
        provider = lambda sym, dt, lookback: bars
        # Call strike 100, spot ≈ 110 → intrinsic = 10
        result = price_option_at_date(
            "AAPL", as_of, strike=100.0, expiry=expiry,
            is_call=True, bars_provider=provider,
        )
        assert result is not None
        assert result["price"] == pytest.approx(10.0, abs=0.5)
        assert result.get("intrinsic_only") is True

    def test_no_spot_returns_none(self):
        from options_backtester import price_option_at_date
        provider = lambda sym, dt, lookback: None
        result = price_option_at_date(
            "AAPL", date(2026, 5, 1), 100.0, date(2026, 5, 31),
            True, bars_provider=provider,
        )
        assert result is None

    def test_otm_call_cheaper_than_atm(self):
        """Sanity: a 110 call at spot 100 should be cheaper than a
        100 call at spot 100, all else equal."""
        from options_backtester import price_option_at_date
        as_of = date(2026, 5, 1)
        expiry = date(2026, 5, 31)
        bars = _build_bars(as_of, n_days=60, daily_sigma=0.012,
                            base=100.0)
        provider = lambda sym, dt, lookback: bars
        atm = price_option_at_date("AAPL", as_of, 100.0, expiry, True,
                                       bars_provider=provider)
        otm = price_option_at_date("AAPL", as_of, 110.0, expiry, True,
                                       bars_provider=provider)
        assert otm["price"] < atm["price"]
        # Greeks for OTM call: lower delta
        assert otm["delta"] < atm["delta"]
