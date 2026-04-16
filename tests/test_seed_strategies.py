"""Tests for the expanded seed strategy library (10 modules).

Each strategy gets: registry shape checks, a happy-path scenario where
conditions should trigger, and a no-trigger scenario where they shouldn't.
"""

from __future__ import annotations

import pandas as pd
import numpy as np
import pytest


VALID_SIGNALS = {"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"}
REQUIRED_KEYS = {"symbol", "signal", "score", "votes", "reason"}


def _assert_shape(candidates, expected_symbol=None, expected_signal=None):
    assert isinstance(candidates, list)
    for c in candidates:
        assert isinstance(c, dict)
        for k in REQUIRED_KEYS:
            assert k in c, f"missing {k} in {c}"
        assert c["signal"] in VALID_SIGNALS
        if expected_symbol:
            assert c["symbol"] == expected_symbol
        if expected_signal:
            assert c["signal"] == expected_signal


def _bars(price_series, vol_series=None, high_series=None, low_series=None,
          dates=None):
    """Build a DataFrame with OHLCV + placeholder indicators so a strategy
    that reads `rsi`/`macd_histogram` etc. can skip add_indicators."""
    n = len(price_series)
    if dates is None:
        dates = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = pd.Series(price_series, index=dates, dtype=float)
    highs = pd.Series(high_series if high_series else
                      [p * 1.01 for p in price_series], index=dates, dtype=float)
    lows = pd.Series(low_series if low_series else
                     [p * 0.99 for p in price_series], index=dates, dtype=float)
    opens = closes.shift(1).fillna(closes)
    vols = pd.Series(vol_series if vol_series else [1_000_000] * n,
                     index=dates, dtype=float)

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })
    # Pre-populate indicator columns so strategies skip add_indicators
    df["rsi"] = 50.0
    df["macd"] = 0.0
    df["macd_signal"] = 0.0
    df["macd_histogram"] = 0.0
    df["sma_20"] = closes.rolling(20, min_periods=1).mean()
    df["sma_50"] = closes.rolling(50, min_periods=1).mean()
    df["ema_12"] = closes.ewm(span=12).mean()
    df["bb_upper"] = df["sma_20"] + 2 * closes.rolling(20, min_periods=1).std().fillna(0)
    df["bb_middle"] = df["sma_20"]
    df["bb_lower"] = df["sma_20"] - 2 * closes.rolling(20, min_periods=1).std().fillna(0)
    df["volume_sma_20"] = vols.rolling(20, min_periods=1).mean()
    df["sma_10"] = closes.rolling(10, min_periods=1).mean()
    df["high_10"] = highs.rolling(10, min_periods=1).max()
    df["high_20"] = highs.rolling(20, min_periods=1).max()
    df["low_5"] = lows.rolling(5, min_periods=1).min()
    df["low_10"] = lows.rolling(10, min_periods=1).min()
    return df


class _Ctx:
    segment = "small"


# ---------------------------------------------------------------------------
# short_term_reversal
# ---------------------------------------------------------------------------

class TestShortTermReversal:
    def test_triggers_on_three_day_decline(self, monkeypatch):
        from strategies import short_term_reversal as mod
        # 10 rising days then 3 strictly declining days from a fresh high
        prices = [10.0] * 6 + [10.0, 11.0, 12.0, 13.0, 14.0, 13.5, 13.0, 12.5]
        df = _bars(prices)
        df.loc[df.index[-1], "rsi"] = 20.0  # oversold
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=20: df)

        out = mod.find_candidates(_Ctx(), ["TEST"])
        _assert_shape(out, expected_symbol="TEST", expected_signal="BUY")
        assert len(out) == 1

    def test_no_trigger_when_rsi_not_oversold(self, monkeypatch):
        from strategies import short_term_reversal as mod
        prices = [10.0] * 6 + [10.0, 11.0, 12.0, 13.0, 14.0, 13.5, 13.0, 12.5]
        df = _bars(prices)
        df.loc[df.index[-1], "rsi"] = 55.0  # not oversold
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=20: df)
        assert mod.find_candidates(_Ctx(), ["TEST"]) == []


# ---------------------------------------------------------------------------
# sector_momentum_rotation
# ---------------------------------------------------------------------------

class TestSectorMomentumRotation:
    def test_buys_top_sector_stock(self, monkeypatch):
        from strategies import sector_momentum_rotation as mod

        monkeypatch.setattr(
            "market_data.get_sector_rotation",
            lambda: {
                "tech": {"return_5d": 8.0},
                "energy": {"return_5d": 6.0},
                "finance": {"return_5d": 1.0},
                "real_estate": {"return_5d": -4.0},
            },
        )
        monkeypatch.setattr("market_data._guess_sector", lambda s: "tech")
        monkeypatch.setattr("market_data.get_bars",
                            lambda s, limit=5: _bars([100.0] * 3))

        out = mod.find_candidates(_Ctx(), ["NVDA"])
        _assert_shape(out, expected_signal="BUY")

    def test_sells_bottom_sector_stock(self, monkeypatch):
        from strategies import sector_momentum_rotation as mod

        monkeypatch.setattr(
            "market_data.get_sector_rotation",
            lambda: {
                "tech": {"return_5d": 8.0},
                "energy": {"return_5d": 6.0},
                "finance": {"return_5d": 1.0},
                "real_estate": {"return_5d": -4.0},
            },
        )
        monkeypatch.setattr("market_data._guess_sector", lambda s: "real_estate")
        monkeypatch.setattr("market_data.get_bars",
                            lambda s, limit=5: _bars([100.0] * 3))

        out = mod.find_candidates(_Ctx(), ["REIT"])
        _assert_shape(out, expected_signal="SELL")

    def test_no_trigger_for_middle_sector(self, monkeypatch):
        from strategies import sector_momentum_rotation as mod
        # Need at least 5 sectors so that a middle-rank one is in
        # neither top-2 nor bottom-2
        monkeypatch.setattr(
            "market_data.get_sector_rotation",
            lambda: {
                "tech":         {"return_5d":  8.0},
                "energy":       {"return_5d":  6.0},
                "healthcare":   {"return_5d":  3.0},   # middle rank
                "finance":      {"return_5d":  1.0},
                "real_estate":  {"return_5d": -4.0},
            },
        )
        monkeypatch.setattr("market_data._guess_sector", lambda s: "healthcare")
        monkeypatch.setattr("market_data.get_bars",
                            lambda s, limit=5: _bars([100.0] * 3))
        assert mod.find_candidates(_Ctx(), ["JNJ"]) == []


# ---------------------------------------------------------------------------
# analyst_upgrade_drift
# ---------------------------------------------------------------------------

class TestAnalystUpgradeDrift:
    def test_no_crash_when_yfinance_unavailable(self, monkeypatch):
        """Strategy must not crash if yfinance returns None or empty."""
        from strategies import analyst_upgrade_drift as mod
        import datetime

        class _T:
            @property
            def recommendations(self):
                return None
        monkeypatch.setattr("yfinance.Ticker", lambda s: _T())
        out = mod.find_candidates(_Ctx(), ["AAPL"])
        assert out == []

    def test_triggers_on_recent_upgrade_with_price_confirm(self, monkeypatch):
        from strategies import analyst_upgrade_drift as mod
        import datetime
        now = datetime.datetime.utcnow()

        idx = pd.DatetimeIndex([now - datetime.timedelta(days=2)])
        recs = pd.DataFrame(
            {"To Grade": ["Buy"], "From Grade": ["Hold"]},
            index=idx,
        )

        class _T:
            @property
            def recommendations(self):
                return recs
        monkeypatch.setattr("yfinance.Ticker", lambda s: _T())
        # Price rising (confirms upgrade)
        df = _bars([100.0, 101.0, 102.5])
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=5: df)

        out = mod.find_candidates(_Ctx(), ["AAPL"])
        _assert_shape(out, expected_signal="BUY")


# ---------------------------------------------------------------------------
# fifty_two_week_breakout
# ---------------------------------------------------------------------------

class TestFiftyTwoWeekBreakout:
    def test_triggers_on_new_high_with_volume(self, monkeypatch):
        from strategies import fifty_two_week_breakout as mod
        prices = list(range(100, 250))  # 150 days ascending
        highs = [p + 1 for p in prices]
        vols = [1_000_000] * (len(prices) - 1) + [2_000_000]  # vol surge today
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=260: df)

        out = mod.find_candidates(_Ctx(), ["NVDA"])
        _assert_shape(out, expected_signal="BUY")
        assert out[0]["score"] == 2

    def test_no_trigger_without_volume_confirm(self, monkeypatch):
        from strategies import fifty_two_week_breakout as mod
        prices = list(range(100, 250))
        highs = [p + 1 for p in prices]
        vols = [1_000_000] * len(prices)  # flat volume
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=260: df)
        assert mod.find_candidates(_Ctx(), ["NVDA"]) == []


# ---------------------------------------------------------------------------
# short_squeeze_setup
# ---------------------------------------------------------------------------

class TestShortSqueezeSetup:
    def test_triggers_with_high_short_and_breakout(self, monkeypatch):
        from strategies import short_squeeze_setup as mod
        monkeypatch.setattr(
            "alternative_data.get_short_interest",
            lambda s: {"short_pct_float": 25.0, "days_to_cover": 6.0},
        )
        # 20 days of flat highs, then break above
        prices = [100.0] * 24 + [110.0]
        highs = [h + 1 for h in prices]
        vols = [1_000_000] * 24 + [2_500_000]
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=25: df)

        out = mod.find_candidates(_Ctx(), ["GME"])
        _assert_shape(out, expected_signal="BUY")
        assert out[0]["score"] == 2  # high days-to-cover bumps score

    def test_no_trigger_when_short_interest_low(self, monkeypatch):
        from strategies import short_squeeze_setup as mod
        monkeypatch.setattr(
            "alternative_data.get_short_interest",
            lambda s: {"short_pct_float": 5.0, "days_to_cover": 1.0},
        )
        prices = [100.0] * 24 + [110.0]
        highs = [h + 1 for h in prices]
        vols = [1_000_000] * 24 + [2_500_000]
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=25: df)
        assert mod.find_candidates(_Ctx(), ["GME"]) == []


# ---------------------------------------------------------------------------
# high_iv_rank_fade
# ---------------------------------------------------------------------------

class TestHighIvRankFade:
    def test_fade_sell_at_high_iv_and_overbought(self, monkeypatch):
        from strategies import high_iv_rank_fade as mod
        monkeypatch.setattr("options_oracle.get_options_oracle",
                            lambda s: {"iv_rank": 90})
        df = _bars([100.0] * 25)
        df.loc[df.index[-1], "rsi"] = 80.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=40: df)

        out = mod.find_candidates(_Ctx(), ["SPY"])
        _assert_shape(out, expected_signal="SELL")

    def test_fade_buy_at_high_iv_and_oversold(self, monkeypatch):
        from strategies import high_iv_rank_fade as mod
        monkeypatch.setattr("options_oracle.get_options_oracle",
                            lambda s: {"iv_rank": 85})
        df = _bars([100.0] * 25)
        df.loc[df.index[-1], "rsi"] = 20.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=40: df)

        out = mod.find_candidates(_Ctx(), ["SPY"])
        _assert_shape(out, expected_signal="BUY")

    def test_no_trigger_at_low_iv_rank(self, monkeypatch):
        from strategies import high_iv_rank_fade as mod
        monkeypatch.setattr("options_oracle.get_options_oracle",
                            lambda s: {"iv_rank": 40})
        df = _bars([100.0] * 25)
        df.loc[df.index[-1], "rsi"] = 80.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=40: df)
        assert mod.find_candidates(_Ctx(), ["SPY"]) == []


# ---------------------------------------------------------------------------
# insider_selling_cluster
# ---------------------------------------------------------------------------

class TestInsiderSellingCluster:
    def test_triggers_on_cluster(self, monkeypatch):
        from strategies import insider_selling_cluster as mod
        monkeypatch.setattr(
            "alternative_data.get_insider_activity",
            lambda s: {
                "recent_buys": 0, "recent_sells": 4,
                "total_buy_value": 0, "total_sell_value": 2_000_000,
            },
        )
        monkeypatch.setattr("market_data.get_bars",
                            lambda s, limit=5: _bars([50.0] * 3))
        out = mod.find_candidates(_Ctx(), ["AAPL"])
        _assert_shape(out, expected_signal="SELL")
        assert out[0]["score"] == 2

    def test_no_trigger_when_buys_dominate(self, monkeypatch):
        from strategies import insider_selling_cluster as mod
        monkeypatch.setattr(
            "alternative_data.get_insider_activity",
            lambda s: {
                "recent_buys": 5, "recent_sells": 2,
                "total_buy_value": 1_000_000, "total_sell_value": 800_000,
            },
        )
        monkeypatch.setattr("market_data.get_bars",
                            lambda s, limit=5: _bars([50.0] * 3))
        assert mod.find_candidates(_Ctx(), ["AAPL"]) == []


# ---------------------------------------------------------------------------
# news_sentiment_spike
# ---------------------------------------------------------------------------

class TestNewsSentimentSpike:
    def test_bullish_spike_with_price_confirm(self, monkeypatch):
        from strategies import news_sentiment_spike as mod
        monkeypatch.setattr(
            "news_sentiment.get_sentiment_signal",
            lambda s: {"direction": "bullish", "score": 85},
        )
        df = _bars([100.0, 102.5])  # +2.5% move confirms
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=5: df)
        out = mod.find_candidates(_Ctx(), ["NVDA"])
        _assert_shape(out, expected_signal="BUY")

    def test_no_trigger_when_price_contradicts(self, monkeypatch):
        from strategies import news_sentiment_spike as mod
        monkeypatch.setattr(
            "news_sentiment.get_sentiment_signal",
            lambda s: {"direction": "bullish", "score": 90},
        )
        df = _bars([100.0, 99.5])  # price going down despite bullish news
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=5: df)
        assert mod.find_candidates(_Ctx(), ["NVDA"]) == []

    def test_no_trigger_below_score_threshold(self, monkeypatch):
        from strategies import news_sentiment_spike as mod
        monkeypatch.setattr(
            "news_sentiment.get_sentiment_signal",
            lambda s: {"direction": "bullish", "score": 50},
        )
        df = _bars([100.0, 103.0])
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=5: df)
        assert mod.find_candidates(_Ctx(), ["NVDA"]) == []


# ---------------------------------------------------------------------------
# volume_dryup_breakout
# ---------------------------------------------------------------------------

class TestVolumeDryupBreakout:
    def test_triggers_after_quiet_consolidation(self, monkeypatch):
        from strategies import volume_dryup_breakout as mod
        # 10 days of rising prices with DECREASING volume, then spike up
        prices = list(range(100, 112)) + [112, 118]
        # Declining volumes (days -6 to -2 in the code's look-back window)
        vols = [5_000_000, 4_000_000, 3_500_000, 3_000_000, 2_500_000, 2_000_000,
                1_800_000, 1_500_000, 1_200_000, 1_100_000, 1_000_000,
                900_000, 800_000, 4_000_000]
        highs = [p + 1 for p in prices]
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=30: df)

        out = mod.find_candidates(_Ctx(), ["AAPL"])
        _assert_shape(out, expected_signal="BUY")

    def test_no_trigger_without_volume_surge(self, monkeypatch):
        from strategies import volume_dryup_breakout as mod
        prices = list(range(100, 115))
        vols = [1_000_000] * len(prices)  # flat volume, no spike
        highs = [p + 1 for p in prices]
        df = _bars(prices, vol_series=vols, high_series=highs)
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=30: df)
        assert mod.find_candidates(_Ctx(), ["AAPL"]) == []


# ---------------------------------------------------------------------------
# macd_cross_confirmation
# ---------------------------------------------------------------------------

class TestMacdCrossConfirmation:
    def test_bullish_cross_with_confirmation(self, monkeypatch):
        from strategies import macd_cross_confirmation as mod
        prices = [100.0] * 30
        vols = [1_000_000] * 29 + [1_500_000]
        df = _bars(prices, vol_series=vols)
        # Force a zero-cross bullish: prev negative, now positive
        df.loc[df.index[-2], "macd_histogram"] = -0.5
        df.loc[df.index[-1], "macd_histogram"] = 0.3
        df.loc[df.index[-1], "rsi"] = 55.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=60: df)

        out = mod.find_candidates(_Ctx(), ["TEST"])
        _assert_shape(out, expected_signal="BUY")

    def test_bearish_cross_with_confirmation(self, monkeypatch):
        from strategies import macd_cross_confirmation as mod
        prices = [100.0] * 30
        vols = [1_000_000] * 29 + [1_500_000]
        df = _bars(prices, vol_series=vols)
        df.loc[df.index[-2], "macd_histogram"] = 0.5
        df.loc[df.index[-1], "macd_histogram"] = -0.3
        df.loc[df.index[-1], "rsi"] = 45.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=60: df)

        out = mod.find_candidates(_Ctx(), ["TEST"])
        _assert_shape(out, expected_signal="SELL")

    def test_no_trigger_without_volume(self, monkeypatch):
        from strategies import macd_cross_confirmation as mod
        prices = [100.0] * 30
        vols = [1_000_000] * 30  # no surge on cross day
        df = _bars(prices, vol_series=vols)
        df.loc[df.index[-2], "macd_histogram"] = -0.5
        df.loc[df.index[-1], "macd_histogram"] = 0.3
        df.loc[df.index[-1], "rsi"] = 55.0
        monkeypatch.setattr("market_data.get_bars", lambda s, limit=60: df)
        assert mod.find_candidates(_Ctx(), ["TEST"]) == []


# ---------------------------------------------------------------------------
# Registry-level checks
# ---------------------------------------------------------------------------

class TestExpandedRegistry:
    def test_sixteen_strategies_registered(self):
        from strategies import STRATEGY_MODULES
        assert len(STRATEGY_MODULES) == 16

    def test_every_new_strategy_has_display_name(self):
        from display_names import display_name
        from strategies import STRATEGY_MODULES
        for mod_path in STRATEGY_MODULES:
            name = mod_path.rsplit(".", 1)[1]
            label = display_name(name)
            # Label must be human-readable (no raw snake_case underscores)
            assert "_" not in label, f"{name} missing display_names.py entry"
            # First alphanumeric char should be uppercase (digits like "52-Week" allowed)
            first_alpha = next((c for c in label if c.isalpha()), None)
            assert first_alpha is not None and first_alpha.isupper(), \
                f"{name} display name '{label}' should start with a capital"

    def test_crypto_has_new_applicable_strategies(self):
        """news_sentiment_spike and macd_cross_confirmation should apply to crypto."""
        from strategies import discover_strategies
        crypto_names = {m.NAME for m in discover_strategies("crypto")}
        assert "news_sentiment_spike" in crypto_names
        assert "macd_cross_confirmation" in crypto_names

    def test_insider_strategies_excluded_from_crypto(self):
        from strategies import discover_strategies
        crypto_names = {m.NAME for m in discover_strategies("crypto")}
        assert "insider_cluster" not in crypto_names
        assert "insider_selling_cluster" not in crypto_names
        assert "short_squeeze_setup" not in crypto_names


# ---------------------------------------------------------------------------
# Dynamic DEFAULT_WEIGHT
# ---------------------------------------------------------------------------

class TestDynamicDefaultWeight:
    def test_default_weight_is_one_over_n(self):
        from multi_strategy import _default_weight
        assert _default_weight(6) == pytest.approx(1.0 / 6)
        assert _default_weight(16) == pytest.approx(1.0 / 16)
        assert _default_weight(0) == 1.0   # safe default for empty

    def test_allocation_equal_weight_at_sixteen(self, tmp_profile_db):
        """With no track record, 16 strategies should each get ~6.25%."""
        from multi_strategy import compute_capital_allocations
        names = [f"strat_{i}" for i in range(16)]
        weights = compute_capital_allocations(names, tmp_profile_db)
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0
        for w in weights.values():
            assert pytest.approx(w, abs=1e-6) == 1.0 / 16
