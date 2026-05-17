"""Tests for macro_data.get_cross_asset_vol — MOVE / OVX / GVZ
(#3 Tier-1 alt-data, 2026-05-17).

Pins the contract:
  - returns a dict with three sub-dicts (move, ovx, gvz)
  - each sub-dict has {current, p30d, p30d_label}
  - percentile labels map correctly (extreme >=95, elevated >=75,
    normal, low <=25)
  - yfinance failure for one index degrades that index to None
    rather than crashing the whole call
  - the unified macro cache picks up cross_asset_vol automatically
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _fake_history(values):
    """Build a yfinance-like DataFrame from a list of close prices."""
    return pd.DataFrame({"Close": values})


@pytest.fixture(autouse=True)
def _bypass_cache(monkeypatch):
    """Each test starts with no cache so they don't share state."""
    import macro_data
    monkeypatch.setattr(macro_data, "_get_cached", lambda *a, **k: None)
    monkeypatch.setattr(macro_data, "_set_cached", lambda *a, **k: None)


class TestStructure:
    def test_returns_three_index_keys(self):
        from macro_data import get_cross_asset_vol
        # Build a steady upward series so the current value is at the top
        values = list(range(50, 80))  # 30 values, current = 79
        with patch("macro_data._VOL_INDEX_TICKERS",
                   {"move": "^MOVE", "ovx": "^OVX", "gvz": "^GVZ"}), \
             patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = _fake_history(values)
            result = get_cross_asset_vol()
        assert set(result.keys()) == {"move", "ovx", "gvz"}

    def test_each_index_has_required_subkeys(self):
        from macro_data import get_cross_asset_vol
        values = list(range(50, 80))
        with patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = _fake_history(values)
            result = get_cross_asset_vol()
        for key in ("move", "ovx", "gvz"):
            assert set(result[key].keys()) == {
                "current", "p30d", "p30d_label",
            }


class TestPercentileLabels:
    def test_extreme_label_at_top_of_range(self):
        """Current = max of series → 100th percentile → 'extreme'."""
        from macro_data import get_cross_asset_vol
        values = list(range(70, 100))  # last = 99 = max
        with patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = _fake_history(values)
            result = get_cross_asset_vol()
        assert result["move"]["p30d_label"] == "extreme"
        assert result["move"]["p30d"] == 100.0

    def test_low_label_at_bottom_of_range(self):
        """Current = min of series → 1/30 = ~3rd percentile → 'low'."""
        from macro_data import get_cross_asset_vol
        # Closes: 100,99,98,...,72 — current is 72 (the min)
        values = list(range(100, 71, -1))
        with patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = _fake_history(values)
            result = get_cross_asset_vol()
        assert result["move"]["p30d_label"] == "low"
        assert result["move"]["p30d"] <= 25

    def test_normal_label_mid_range(self):
        """Current at the median of the window → ~50th percentile → 'normal'."""
        from macro_data import get_cross_asset_vol
        # 30 values with median in the middle; current=80 at position 16/30
        values = list(range(65, 95))  # 65..94
        # rearrange so 80 is the last entry
        values = [v for v in values if v != 80] + [80]
        with patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = _fake_history(values)
            result = get_cross_asset_vol()
        assert result["move"]["p30d_label"] == "normal"


class TestRobustness:
    def test_single_index_failure_doesnt_kill_others(self):
        """If yfinance fails for ^OVX, MOVE and GVZ still return data."""
        from macro_data import get_cross_asset_vol
        good = _fake_history(list(range(50, 80)))
        call_count = {"n": 0}

        def fake_ticker(symbol):
            t = MagicMock()
            # Make ^OVX raise; the other two succeed
            if symbol == "^OVX":
                t.history.side_effect = ConnectionError("network blip")
            else:
                t.history.return_value = good
            return t

        with patch("yfinance.Ticker", side_effect=fake_ticker):
            result = get_cross_asset_vol()
        # MOVE + GVZ have data
        assert result["move"]["current"] is not None
        assert result["gvz"]["current"] is not None
        # OVX degraded gracefully
        assert result["ovx"]["current"] is None
        assert result["ovx"]["p30d_label"] == "unavailable"

    def test_empty_history_returns_unavailable_not_crash(self):
        from macro_data import get_cross_asset_vol
        empty = pd.DataFrame()
        with patch("yfinance.Ticker") as fake_ticker:
            fake_ticker.return_value.history.return_value = empty
            result = get_cross_asset_vol()
        for key in ("move", "ovx", "gvz"):
            assert result[key]["p30d_label"] == "unavailable"


class TestUnifiedCacheIntegration:
    def test_cross_asset_vol_in_get_all_macro_data(self):
        """get_all_macro_data() must return cross_asset_vol so the
        unified alt-data cache (alternative_data._get_cached_macro)
        picks it up automatically — no second wiring needed."""
        from macro_data import get_all_macro_data
        # Patch every sub-fetcher so we focus on the aggregator
        with patch("macro_data.get_yield_curve", return_value={}), \
             patch("macro_data.get_etf_flows", return_value={}), \
             patch("macro_data.get_cboe_skew", return_value={}), \
             patch("macro_data.get_fred_macro", return_value={}), \
             patch("macro_data.get_sector_momentum_ranking", return_value={}), \
             patch("macro_data.get_market_gex_aggregate", return_value={}), \
             patch(
                "macro_data.get_cross_asset_vol",
                return_value={"move": {"p30d_label": "elevated"}},
        ):
            result = get_all_macro_data()
        assert "cross_asset_vol" in result
        assert result["cross_asset_vol"]["move"]["p30d_label"] == "elevated"
