"""P3.3 of LONG_SHORT_PLAN.md — sector_rotation_short strategy."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _bars(prices, with_indicators=True):
    n = len(prices)
    df = pd.DataFrame({
        "open":   prices,
        "high":   [p * 1.01 for p in prices],
        "low":    [p * 0.99 for p in prices],
        "close":  prices,
        "volume": [1_000_000] * n,
    })
    if with_indicators:
        # Stub a moderate RSI value
        df["rsi"] = [50.0] * n
        df["macd_histogram"] = [0.0] * n
        df["bb_upper"] = [max(prices) * 1.02] * n
        df["bb_lower"] = [min(prices) * 0.98] * n
        df["bb_middle"] = [(max(prices) + min(prices)) / 2] * n
        df["volume_sma_20"] = [1_000_000] * n
    return df


def test_module_has_required_interface():
    from strategies import sector_rotation_short as m
    assert m.NAME == "sector_rotation_short"
    assert callable(m.find_candidates)


def test_in_strategy_registry():
    from strategies import STRATEGY_MODULES
    assert "strategies.sector_rotation_short" in STRATEGY_MODULES


def test_NOT_in_catalyst_set_so_regime_gate_filters_it():
    """Sector rotation isn't a catalyst — it should be filtered by
    the strong_bull regime gate. Verify it's NOT in the catalyst set
    (matches the design intent)."""
    from trade_pipeline import _CATALYST_SHORT_STRATEGIES
    assert "sector_rotation_short" not in _CATALYST_SHORT_STRATEGIES


def test_no_candidates_when_no_sector_data():
    from strategies.sector_rotation_short import find_candidates
    with patch("macro_data.get_sector_momentum_ranking",
                return_value={"bottom_3": [], "top_3": [], "rankings": []}):
        results = find_candidates(None, ["AAPL"])
    assert results == []


def test_no_candidates_when_symbol_not_in_bottom_3():
    from strategies.sector_rotation_short import find_candidates
    ranking = {
        "bottom_3": ["energy", "utilities", "consumer_staples"],
        "top_3": ["tech", "consumer_disc", "finance"],
        "rankings": [
            {"sector": "tech", "return_5d": 4.0, "rank": 1},
            {"sector": "energy", "return_5d": -3.5, "rank": 9},
        ],
        "rotation_phase": "risk_on",
    }
    with patch("macro_data.get_sector_momentum_ranking", return_value=ranking), \
         patch("sector_classifier.get_sector", return_value="tech"):
        results = find_candidates(None, ["AAPL"])
    assert results == []


def test_candidate_emitted_when_sector_in_bottom_3_and_stock_weak():
    """Symbol in bottom-3 sector + below SMA + 5d return negative + RSI 35-70."""
    from strategies.sector_rotation_short import find_candidates
    ranking = {
        "bottom_3": ["energy", "utilities", "consumer_staples"],
        "top_3": ["tech", "consumer_disc", "finance"],
        "rankings": [
            {"sector": "energy", "return_5d": -3.5, "rank": 9},
        ],
        "rotation_phase": "risk_off",
    }
    # 30 bars: 26 days at 100, then 4 days dropping to 92 (below SMA, -8% in 5d)
    prices = [100.0] * 26 + [98, 96, 94, 92]
    bars = _bars(prices)
    bars["rsi"] = [50.0] * len(prices)  # neither overbought nor oversold

    with patch("macro_data.get_sector_momentum_ranking", return_value=ranking), \
         patch("sector_classifier.get_sector", return_value="energy"), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["XOM"])
    assert len(results) == 1
    r = results[0]
    assert r["symbol"] == "XOM"
    assert r["signal"] == "SHORT"
    assert r["score"] == 2
    assert "energy" in r["reason"].lower()


def test_no_candidate_when_oversold_rsi():
    """RSI < 35 (oversold) means bounce risk — skip even if sector weak."""
    from strategies.sector_rotation_short import find_candidates
    ranking = {
        "bottom_3": ["energy"], "top_3": ["tech"],
        "rankings": [{"sector": "energy", "return_5d": -3.5, "rank": 9}],
        "rotation_phase": "risk_off",
    }
    prices = [100.0] * 26 + [95, 90, 85, 80]
    bars = _bars(prices)
    bars["rsi"] = [25.0] * len(prices)  # deeply oversold
    with patch("macro_data.get_sector_momentum_ranking", return_value=ranking), \
         patch("sector_classifier.get_sector", return_value="energy"), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["XOM"])
    assert results == []


def test_no_candidate_when_stock_positive_in_weak_sector():
    """Sector weak overall but THIS name is up → not the right short."""
    from strategies.sector_rotation_short import find_candidates
    ranking = {
        "bottom_3": ["energy"], "top_3": ["tech"],
        "rankings": [{"sector": "energy", "return_5d": -3.5, "rank": 9}],
        "rotation_phase": "risk_off",
    }
    prices = [95.0] * 26 + [97, 99, 100, 102]  # sector down, stock up
    bars = _bars(prices)
    with patch("macro_data.get_sector_momentum_ranking", return_value=ranking), \
         patch("sector_classifier.get_sector", return_value="energy"), \
         patch("market_data.get_bars", return_value=bars):
        results = find_candidates(None, ["CVX"])
    assert results == []
