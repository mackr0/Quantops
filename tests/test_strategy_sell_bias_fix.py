"""Regression tests for the 2026-04-15 strategy SELL-bias fix.

Prior bug: long-only entry strategies (mean_reversion, momentum_continuation,
MA-alignment, etc.) emitted SELL votes for conditions that are just
"exit-a-hypothetical-long," such as `price >= sma_20` or `rsi > 55`. In a
normal market, ~60-70% of stocks satisfy those conditions, so aggregation
piled up multiple SELL votes per symbol → STRONG_SELL label on the entire
universe → AI declines to trade. Small Cap profile went days with zero
trades despite hundreds of AI predictions.

Fix:
  (a) multi_strategy.aggregate_candidates() treats SELL votes as HOLD when
      the profile has enable_short_selling=False, so long-only setups
      cannot bias the score negative.
  (b) The broken SELL branches themselves (exit-condition-as-short) are
      replaced with HOLD in strategy_small/_mid/_large/_micro. Legit
      bearish setups (failed gap, MACD bearish cross, 10-day-low break,
      falling-knife consecutive red days) are preserved.

These tests guard both fixes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Fix #1 — aggregation respects enable_short_selling
# ---------------------------------------------------------------------------

class TestAggregationRespectsShortFlag:
    def _fake_strategy(self, name, signals_by_symbol):
        """Build a minimal strategy module with a NAME + find_candidates."""
        def find_candidates(ctx, universe):
            return [{"symbol": s, "signal": sig, "score": -1 if "SELL" in sig else (1 if "BUY" in sig else 0)}
                    for s, sig in signals_by_symbol.items()]
        mod = SimpleNamespace(NAME=name, find_candidates=find_candidates)
        return mod

    def test_sell_votes_become_hold_when_shorting_disabled(self, monkeypatch):
        from multi_strategy import aggregate_candidates

        mod_a = self._fake_strategy("alpha", {"AAPL": "SELL"})
        mod_b = self._fake_strategy("beta",  {"AAPL": "SELL"})
        monkeypatch.setattr("strategies.get_active_strategies",
                            lambda *a, **kw: [mod_a, mod_b])

        ctx = SimpleNamespace(segment="small", enable_short_selling=False)
        out = aggregate_candidates(ctx, ["AAPL"])
        cand = out["candidates"][0]

        # Both strategies voted SELL — but with shorting off, each vote should
        # have been coerced to HOLD before aggregation.
        assert cand["votes"]["alpha"] == "HOLD"
        assert cand["votes"]["beta"] == "HOLD"
        # Score must not be negative just from exit-condition SELL leakage.
        assert cand.get("score", 0) >= 0
        assert cand["signal"] != "STRONG_SELL"

    def test_sell_votes_pass_through_when_shorting_enabled(self, monkeypatch):
        from multi_strategy import aggregate_candidates

        mod_a = self._fake_strategy("alpha", {"AAPL": "SELL"})
        mod_b = self._fake_strategy("beta",  {"AAPL": "SELL"})
        monkeypatch.setattr("strategies.get_active_strategies",
                            lambda *a, **kw: [mod_a, mod_b])

        ctx = SimpleNamespace(segment="small", enable_short_selling=True)
        out = aggregate_candidates(ctx, ["AAPL"])
        cand = out["candidates"][0]

        assert cand["votes"]["alpha"] == "SELL"
        assert cand["votes"]["beta"] == "SELL"

    def test_buy_votes_untouched_by_short_flag(self, monkeypatch):
        from multi_strategy import aggregate_candidates

        mod = self._fake_strategy("alpha", {"AAPL": "BUY"})
        monkeypatch.setattr("strategies.get_active_strategies",
                            lambda *a, **kw: [mod])

        ctx = SimpleNamespace(segment="small", enable_short_selling=False)
        out = aggregate_candidates(ctx, ["AAPL"])
        cand = out["candidates"][0]
        assert cand["votes"]["alpha"] == "BUY"


# ---------------------------------------------------------------------------
# Fix #2 — strategy-level fixes (no more exit-condition-as-SELL)
# ---------------------------------------------------------------------------

def _make_df(rows, rsi, sma_20, price, volume=1_000_000, vol_sma=500_000):
    """Build a fake indicators-attached OHLCV frame sufficient for strategies."""
    idx = pd.date_range("2026-01-01", periods=rows, freq="B", tz="US/Eastern")
    base = {
        "open":  [price] * rows,
        "high":  [price * 1.01] * rows,
        "low":   [price * 0.99] * rows,
        "close": [price] * rows,
        "volume": [volume] * rows,
        "rsi":    [rsi] * rows,
        "sma_20": [sma_20] * rows,
        "volume_sma_20": [vol_sma] * rows,
        "sma_50": [sma_20] * rows,
        "ema_12": [sma_20] * rows,
        "macd": [0.0] * rows,
        "macd_signal": [0.0] * rows,
        "macd_histogram": [0.0] * rows,
    }
    return pd.DataFrame(base, index=idx)


class TestSmallCapMeanReversionNoBogusSell:
    def test_rsi_above_55_returns_hold_not_sell(self):
        from strategy_small import mean_reversion_strategy
        df = _make_df(30, rsi=60, sma_20=100, price=99)  # below SMA, RSI 60
        r = mean_reversion_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD", \
            f"RSI 60 with price below SMA should HOLD, not {r['signal']}"

    def test_price_above_sma_returns_hold_not_sell(self):
        from strategy_small import mean_reversion_strategy
        df = _make_df(30, rsi=45, sma_20=100, price=102)  # above SMA, neutral RSI
        r = mean_reversion_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"

    def test_oversold_still_emits_buy(self):
        from strategy_small import mean_reversion_strategy
        df = _make_df(30, rsi=20, sma_20=100, price=90)  # RSI 20 + 10% below SMA
        r = mean_reversion_strategy("AAPL", df=df.copy())
        assert r["signal"] == "BUY"


class TestSmallCapMomentumContinuationNoBogusSell:
    def test_price_below_sma_returns_hold_not_sell(self):
        from strategy_small import momentum_continuation_strategy
        df = _make_df(30, rsi=45, sma_20=100, price=95)
        r = momentum_continuation_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"


class TestMidCapSectorMomentumNoBogusSell:
    def test_price_below_sma_returns_hold_not_sell(self, monkeypatch):
        import strategy_mid
        monkeypatch.setattr(strategy_mid, "_get_spy_data", lambda: None)
        df = _make_df(30, rsi=45, sma_20=100, price=95)
        r = strategy_mid.sector_momentum_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"


class TestMidCapPullbackSupportNoBogusSell:
    def test_price_below_sma50_returns_hold_not_sell(self):
        from strategy_mid import pullback_support_strategy
        df = _make_df(30, rsi=45, sma_20=100, price=95)  # below SMA50 (set to 100)
        r = pullback_support_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"


class TestLargeCapStrategiesNoBogusSell:
    def test_dividend_yield_rsi_normalized_returns_hold(self):
        from strategy_large import dividend_yield_strategy
        df = _make_df(30, rsi=60, sma_20=100, price=60)  # RSI 60, price > $50
        r = dividend_yield_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"

    def test_ma_alignment_price_below_sma_returns_hold(self):
        from strategy_large import ma_alignment_strategy
        df = _make_df(30, rsi=45, sma_20=100, price=95)
        r = ma_alignment_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"

    def test_relative_strength_underperforming_returns_hold(self, monkeypatch):
        import strategy_large
        monkeypatch.setattr(strategy_large, "_get_spy_data", lambda: None)
        df = _make_df(30, rsi=65, sma_20=100, price=99)  # underperforming + RSI>60
        r = strategy_large.relative_strength_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"


class TestMicroCapStrategiesNoBogusSell:
    def test_volume_explosion_catalyst_fading_returns_hold(self):
        from strategy_micro import volume_explosion_strategy
        df = _make_df(30, rsi=65, sma_20=100, price=100, volume=800_000, vol_sma=500_000)
        r = volume_explosion_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"

    def test_penny_reversal_price_above_sma10_returns_hold(self):
        from strategy_micro import penny_reversal_strategy
        df = _make_df(30, rsi=55, sma_20=100, price=102)
        r = penny_reversal_strategy("AAPL", df=df.copy())
        assert r["signal"] == "HOLD"


# ---------------------------------------------------------------------------
# Legit bearish setups are PRESERVED (guard against over-stripping)
# ---------------------------------------------------------------------------

class TestLegitBearishSignalsPreserved:
    def test_breakout_volume_low_10_break_still_sells(self):
        """Mid cap breakout strategy should still emit SELL when price
        breaks the 10-day low — that IS a legit bearish signal, not an
        exit-condition leak."""
        from strategy_mid import breakout_volume_strategy
        df = _make_df(30, rsi=40, sma_20=100, price=50)
        # low_10 = rolling min of `low` over last 10 bars. Keep all prior
        # lows at 60 and close at 50 on the last row so price < low_10 = 60.
        df["low"] = 60.0
        df.loc[df.index[-1], "close"] = 50.0
        r = breakout_volume_strategy("AAPL", df=df.copy())
        assert r["signal"] == "SELL"

    def test_macd_bearish_cross_still_sells(self):
        """MACD bearish cross is a real short signal — keep it."""
        from strategy_mid import macd_cross_strategy
        df = _make_df(30, rsi=55, sma_20=100, price=100)
        df.loc[df.index[-2], "macd"] = 0.5
        df.loc[df.index[-2], "macd_signal"] = 0.4
        df.loc[df.index[-1], "macd"] = 0.3
        df.loc[df.index[-1], "macd_signal"] = 0.4
        r = macd_cross_strategy("AAPL", df=df.copy())
        assert r["signal"] == "SELL"

    def test_micro_avoid_traps_falling_knife_still_sells(self):
        """10 consecutive red days is a real falling-knife short setup."""
        from strategy_micro import avoid_traps_filter
        df = _make_df(20, rsi=35, sma_20=100, price=95, vol_sma=200_000)
        # Make every row red (close < open)
        for i in range(len(df)):
            df.loc[df.index[i], "open"] = 100.0
            df.loc[df.index[i], "close"] = 95.0
        r = avoid_traps_filter("AAPL", df=df.copy())
        assert r["signal"] == "SELL"


# ---------------------------------------------------------------------------
# End-to-end: production universe no longer uniformly STRONG_SELL
# ---------------------------------------------------------------------------

class TestUniverseNotAllStrongSell:
    def test_diverse_universe_has_diverse_signals(self, monkeypatch):
        """Against a realistic mixed universe, aggregation should not label
        >=90% of candidates STRONG_SELL. This is the condition that caused
        Small Cap to freeze for days."""
        from multi_strategy import aggregate_candidates

        # Fake strategies: one that BUYs half, one that HOLDs rest
        def mk(name, buy_list):
            def fc(ctx, universe):
                return [{"symbol": s,
                         "signal": "BUY" if s in buy_list else "HOLD",
                         "score": 1 if s in buy_list else 0}
                        for s in universe]
            return SimpleNamespace(NAME=name, find_candidates=fc)

        universe = [f"SYM{i}" for i in range(10)]
        buy_a = {f"SYM{i}" for i in range(5)}
        buy_b = {f"SYM{i}" for i in range(3, 8)}
        monkeypatch.setattr("strategies.get_active_strategies",
                            lambda *a, **kw: [mk("a", buy_a), mk("b", buy_b)])

        ctx = SimpleNamespace(segment="small", enable_short_selling=True)
        out = aggregate_candidates(ctx, universe)
        labels = [c["signal"] for c in out["candidates"]]
        strong_sell = sum(1 for s in labels if s == "STRONG_SELL")
        # With no SELL votes in this fixture, STRONG_SELL must be 0.
        assert strong_sell == 0
