"""P3.4 of LONG_SHORT_PLAN.md — iv_regime_short strategy."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _bars(prices, volumes=None, rsi=50.0):
    n = len(prices)
    if volumes is None:
        volumes = [1_000_000] * n
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.01 for p in prices],
        "low":    [p * 0.99 for p in prices],
        "close":  prices,
        "volume": volumes,
        "rsi": [rsi] * n,
        "macd_histogram": [0.0] * n,
        "bb_upper": [max(prices) * 1.02] * n,
        "bb_lower": [min(prices) * 0.98] * n,
        "bb_middle": [(max(prices) + min(prices)) / 2] * n,
        "volume_sma_20": [1_000_000] * n,
    })
    return df


def test_module_has_required_interface():
    from strategies import iv_regime_short as m
    assert m.NAME == "iv_regime_short"
    assert callable(m.find_candidates)


def test_in_strategy_registry():
    from strategies import STRATEGY_MODULES
    assert "strategies.iv_regime_short" in STRATEGY_MODULES


def test_NOT_in_catalyst_set():
    """IV regime is a market condition, not a company-specific catalyst."""
    from trade_pipeline import _CATALYST_SHORT_STRATEGIES
    assert "iv_regime_short" not in _CATALYST_SHORT_STRATEGIES


def test_no_candidate_when_iv_rank_low():
    from strategies.iv_regime_short import find_candidates
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 50}):
        results = find_candidates(None, ["AAPL"])
    assert results == []


def test_no_candidate_when_above_sma():
    """Even with high IV, an uptrend isn't a short setup."""
    from strategies.iv_regime_short import find_candidates
    # 30 bars rising from 90 to 100 — clearly above SMA
    prices = [90 + (10 * i / 29) for i in range(30)]
    bars = _bars(prices)
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 80}), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["AAPL"])
    assert results == []


def test_candidate_emitted_in_iv_regime_downtrend():
    """High IV + downtrend + active selling + RSI mid-range + volume spike."""
    from strategies.iv_regime_short import find_candidates
    # 30 bars: trending down from 110 to 95 (15% over 30 days)
    prices = [110 - (15 * i / 29) for i in range(30)]
    # Final volume 2x avg
    volumes = [1_000_000] * 29 + [2_500_000]
    bars = _bars(prices, volumes=volumes, rsi=45.0)
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 80}), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["TSLA"])
    assert len(results) == 1
    r = results[0]
    assert r["signal"] == "SHORT"
    assert r["score"] == 2
    assert "iv rank" in r["reason"].lower() or "iv" in r["reason"].lower()


def test_no_candidate_when_oversold():
    """RSI below 35 = mean-reversion territory; not this strategy's setup."""
    from strategies.iv_regime_short import find_candidates
    prices = [110 - (20 * i / 29) for i in range(30)]
    bars = _bars(prices, rsi=25.0)  # oversold
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 80}), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["TSLA"])
    assert results == []


def test_no_candidate_when_volume_thin():
    """Without volume confirmation, the move could be noise."""
    from strategies.iv_regime_short import find_candidates
    prices = [110 - (15 * i / 29) for i in range(30)]
    volumes = [1_000_000] * 30  # avg volume, no spike
    bars = _bars(prices, volumes=volumes, rsi=45.0)
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 80}), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["TSLA"])
    assert results == []


def test_no_candidate_when_sideways_under_sma():
    """Below SMA but no actual decline over 10 days — not the pattern."""
    from strategies.iv_regime_short import find_candidates
    prices = [100, 99, 100, 99, 100] * 6  # sideways
    bars = _bars(prices)
    with patch("options_oracle.get_options_oracle",
                return_value={"iv_rank": 80}), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["TSLA"])
    assert results == []
