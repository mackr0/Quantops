"""P3.1 of LONG_SHORT_PLAN.md — earnings_disaster_short strategy."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd


def _make_bars(prices, volumes=None):
    """Build a fake bars DataFrame matching what get_bars returns."""
    n = len(prices)
    if volumes is None:
        volumes = [1_000_000] * n
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.01 for p in prices],
        "low":    [p * 0.99 for p in prices],
        "close":  prices,
        "volume": volumes,
    })
    return df


def test_module_has_required_interface():
    from strategies import earnings_disaster_short as m
    assert m.NAME == "earnings_disaster_short"
    assert "small" in m.APPLICABLE_MARKETS
    assert callable(m.find_candidates)


def test_no_candidates_when_no_catalyst_bar():
    """A perfectly trending stock should not trigger."""
    from strategies.earnings_disaster_short import find_candidates
    # 270 days of smooth 0.1% daily gains → no gap, no drop, no trigger
    prices = [100 * (1.001 ** i) for i in range(270)]
    bars = _make_bars(prices)
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["AAPL"])
    assert results == []


def test_no_candidates_when_too_close_to_52w_high():
    """A stock 5% off its high doesn't qualify (too healthy)."""
    from strategies.earnings_disaster_short import find_candidates
    # 270 bars, then a sharp drop in the last 10. But high is right
    # there — disaster signal too close to highs to be a real disaster.
    prices = [100] * 260 + [95, 100] + [80] + [78, 79, 78, 77, 78, 77, 76, 77]
    volumes = [1_000_000] * 260 + [1_000_000, 1_000_000, 5_000_000] + [1_000_000] * 8
    # Last close = 77, high = ~101 → distance ~24% — actually triggers.
    # To make it NOT trigger, keep last close at 95+
    prices = [100] * 268 + [95, 96]
    bars = _make_bars(prices)
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["AAPL"])
    assert results == [], "stock near highs should not trigger disaster pattern"


def test_candidate_emitted_for_recent_gap_down_with_volume():
    """Classic earnings disaster: 270 bars steady around 100, then a
    -10% gap-down on 3x volume 5 days ago, no recovery, latest close
    still well off high."""
    from strategies.earnings_disaster_short import find_candidates
    # 260 days of steady upward drift: 80 → 110 (so 52w high = 110)
    # Last 10 days: stock around 110, then sharp gap-down to 85, no recovery.
    pre_disaster = [80 + (30 * i / 259) for i in range(260)]
    # gap-down bar at index -6: open = 100, close = 85 (vs prior close = 110)
    last_10 = [110, 109, 110, 111] + [85, 86, 85, 84, 85, 84]  # gap at index 4 of last 10
    prices = pre_disaster + last_10
    bars = _make_bars(prices)
    # Make the catalyst bar (-6 from end, idx 264) a real gap with 5x volume
    catalyst_idx = -6
    bars.iloc[catalyst_idx, bars.columns.get_loc("open")] = 100  # gap below 111
    bars.iloc[catalyst_idx, bars.columns.get_loc("close")] = 85
    bars.iloc[catalyst_idx, bars.columns.get_loc("volume")] = 5_000_000
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["TSLA"])
    assert len(results) == 1, (
        f"expected 1 result, got {len(results)} (last close={prices[-1]}, "
        f"high={max(prices)}, sma20={sum(prices[-20:])/20:.2f})"
    )
    r = results[0]
    assert r["symbol"] == "TSLA"
    assert r["signal"] == "SHORT"
    assert r["score"] == 3
    assert "disaster" in r["reason"].lower()
    assert r["votes"] == {"earnings_disaster_short": "SHORT"}


def test_no_candidate_when_recovery_happened():
    """If the stock has already recovered above the catalyst-bar close,
    the disaster has resolved — don't short."""
    from strategies.earnings_disaster_short import find_candidates
    n = 270
    prices = [100] * 250 + [95, 100, 105] + [88]  # gap-down at index -8
    # Then recovery — last close is 110, well above catalyst_close=88
    prices += [92, 95, 100, 105, 108, 110, 112]
    bars = _make_bars(prices)
    bars.iloc[-8, bars.columns.get_loc("volume")] = 5_000_000
    bars.iloc[-8, bars.columns.get_loc("open")] = 95  # gap from 105
    bars.iloc[-8, bars.columns.get_loc("close")] = 88
    with patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["AMZN"])
    assert results == [], "recovered stock should not trigger disaster pattern"


def test_strategy_in_catalyst_tagged_set():
    """P3.1 strategy must be in _CATALYST_SHORT_STRATEGIES so it
    survives the strong_bull regime gate."""
    from trade_pipeline import _CATALYST_SHORT_STRATEGIES
    assert "earnings_disaster_short" in _CATALYST_SHORT_STRATEGIES


def test_strategy_in_registry():
    from strategies import STRATEGY_MODULES
    assert "strategies.earnings_disaster_short" in STRATEGY_MODULES
