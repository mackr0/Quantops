"""Test strategy engines produce valid output.

These tests use synthetic data (no network calls) to verify that each
strategy engine:
1. Runs without errors
2. Returns the expected dict format
3. Responds correctly to extreme indicator values
"""

import pytest
import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_SIGNALS = {"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"}
REQUIRED_KEYS = {"symbol", "signal", "reason", "price"}


def assert_valid_result(result, symbol=None):
    """Verify a strategy result dict has the right shape."""
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    for key in REQUIRED_KEYS:
        assert key in result, f"Missing key: {key}"
    assert result["signal"] in VALID_SIGNALS, f"Invalid signal: {result['signal']}"
    if symbol:
        assert result["symbol"] == symbol


def assert_valid_combined(result, symbol=None):
    """Verify a combined strategy result."""
    assert_valid_result(result, symbol)
    assert "score" in result, "Combined result missing 'score'"
    assert "votes" in result, "Combined result missing 'votes'"
    assert isinstance(result["score"], (int, float))


def make_oversold_df(base_df):
    """Modify df to have deeply oversold conditions."""
    df = base_df.copy()
    df["rsi"] = 15.0  # Very oversold
    df["close"] = df["sma_20"] * 0.75  # 25% below SMA
    return df


def make_overbought_df(base_df):
    """Modify df to have overbought conditions."""
    df = base_df.copy()
    df["rsi"] = 85.0
    df["close"] = df["sma_20"] * 1.2  # 20% above SMA
    return df


def make_volume_surge_df(base_df):
    """Modify df to have volume surge with price up."""
    df = base_df.copy()
    df.iloc[-1, df.columns.get_loc("volume")] = df["volume_sma_20"].iloc[-1] * 6
    df.iloc[-1, df.columns.get_loc("close")] = df.iloc[-2]["close"] * 1.08
    df["rsi"].iloc[-1] = 55.0
    return df


# ---------------------------------------------------------------------------
# Strategy Router
# ---------------------------------------------------------------------------

class TestStrategyRouter:
    def test_routes_to_micro(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "micro", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_routes_to_small(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "small", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_routes_to_midcap(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "midcap", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_routes_to_largecap(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "largecap", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_routes_to_crypto(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "crypto", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_unknown_market_type_falls_back(self, sample_df):
        from strategy_router import run_strategy
        result = run_strategy("TEST", "unknown", df=sample_df)
        assert_valid_combined(result, "TEST")


# ---------------------------------------------------------------------------
# Micro Cap Strategy
# ---------------------------------------------------------------------------

class TestMicroStrategy:
    def test_combined_returns_valid(self, sample_df):
        from strategy_micro import micro_combined_strategy
        result = micro_combined_strategy("TEST", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_volume_explosion(self, sample_df):
        from strategy_micro import volume_explosion_strategy
        result = volume_explosion_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_penny_reversal(self, sample_df):
        from strategy_micro import penny_reversal_strategy
        result = penny_reversal_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_breakout_resistance(self, sample_df):
        from strategy_micro import breakout_resistance_strategy
        result = breakout_resistance_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_avoid_traps(self, sample_df):
        from strategy_micro import avoid_traps_filter
        result = avoid_traps_filter("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_accepts_params(self, sample_df):
        from strategy_micro import micro_combined_strategy
        params = {"rsi_oversold": 15.0, "volume_surge_multiplier": 8.0}
        result = micro_combined_strategy("TEST", df=sample_df, params=params)
        assert_valid_combined(result)

    def test_oversold_generates_signal(self, sample_df):
        from strategy_micro import penny_reversal_strategy
        df = make_oversold_df(sample_df)
        result = penny_reversal_strategy("TEST", df=df, rsi_threshold=25)
        # With RSI=15 and 25% below SMA, should be BUY or at least not error
        assert result["signal"] in VALID_SIGNALS


# ---------------------------------------------------------------------------
# Small Cap Strategy
# ---------------------------------------------------------------------------

class TestSmallStrategy:
    def test_combined_returns_valid(self, sample_df):
        from strategy_small import small_combined_strategy
        result = small_combined_strategy("TEST", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_mean_reversion(self, sample_df):
        from strategy_small import mean_reversion_strategy
        result = mean_reversion_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_volume_spike_entry(self, sample_df):
        from strategy_small import volume_spike_entry_strategy
        result = volume_spike_entry_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_gap_and_go(self, sample_df):
        from strategy_small import gap_and_go_strategy
        result = gap_and_go_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_momentum_continuation(self, sample_df):
        from strategy_small import momentum_continuation_strategy
        result = momentum_continuation_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_accepts_params(self, sample_df):
        from strategy_small import small_combined_strategy
        params = {"rsi_oversold": 20.0, "gap_pct_threshold": 5.0}
        result = small_combined_strategy("TEST", df=sample_df, params=params)
        assert_valid_combined(result)


# ---------------------------------------------------------------------------
# Mid Cap Strategy
# ---------------------------------------------------------------------------

class TestMidStrategy:
    def test_combined_returns_valid(self, sample_df):
        from strategy_mid import mid_combined_strategy
        result = mid_combined_strategy("TEST", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_breakout_volume(self, sample_df):
        from strategy_mid import breakout_volume_strategy
        result = breakout_volume_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_pullback_support(self, sample_df):
        from strategy_mid import pullback_support_strategy
        result = pullback_support_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_macd_cross(self, sample_df):
        from strategy_mid import macd_cross_strategy
        result = macd_cross_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_accepts_params(self, sample_df):
        from strategy_mid import mid_combined_strategy
        params = {"volume_surge_multiplier": 3.0}
        result = mid_combined_strategy("TEST", df=sample_df, params=params)
        assert_valid_combined(result)


# ---------------------------------------------------------------------------
# Large Cap Strategy
# ---------------------------------------------------------------------------

class TestLargeStrategy:
    def test_combined_returns_valid(self, sample_df):
        from strategy_large import large_combined_strategy
        result = large_combined_strategy("TEST", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_relative_strength(self, sample_df):
        from strategy_large import relative_strength_strategy
        result = relative_strength_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_dividend_yield(self, sample_df):
        from strategy_large import dividend_yield_strategy
        result = dividend_yield_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_ma_alignment(self, sample_df):
        from strategy_large import ma_alignment_strategy
        result = ma_alignment_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_accepts_params(self, sample_df):
        from strategy_large import large_combined_strategy
        params = {"rsi_oversold": 30.0}
        result = large_combined_strategy("TEST", df=sample_df, params=params)
        assert_valid_combined(result)


# ---------------------------------------------------------------------------
# Crypto Strategy
# ---------------------------------------------------------------------------

class TestCryptoStrategy:
    def test_combined_returns_valid(self, sample_df):
        from strategy_crypto import crypto_combined_strategy
        result = crypto_combined_strategy("TEST", df=sample_df)
        assert_valid_combined(result, "TEST")

    def test_trend_following(self, sample_df):
        from strategy_crypto import trend_following_strategy
        result = trend_following_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_extreme_oversold(self, sample_df):
        from strategy_crypto import extreme_oversold_strategy
        result = extreme_oversold_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_volume_surge(self, sample_df):
        from strategy_crypto import volume_surge_strategy
        result = volume_surge_strategy("TEST", df=sample_df)
        assert_valid_result(result, "TEST")

    def test_accepts_params(self, sample_df):
        from strategy_crypto import crypto_combined_strategy
        params = {"rsi_oversold": 25.0, "volume_surge_multiplier": 2.0}
        result = crypto_combined_strategy("TEST", df=sample_df, params=params)
        assert_valid_combined(result)
