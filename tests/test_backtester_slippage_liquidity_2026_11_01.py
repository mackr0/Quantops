"""2026-11-01 — OPEN_ITEMS P2.3: the strategy backtester's slippage
no longer prices every symbol as if it traded a million shares a day
at the model's default volatility. ADV and realized daily vol come
from the symbol's own bars (`backtester._window_liquidity`), so a thin
name pays more impact than a liquid one in the backtest, as it does
live.
"""
from __future__ import annotations

import os
import re
import sys

import pandas as pd
import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, REPO)

import backtester  # noqa: E402


def _bars(n=30, vol=1_000_000, step=0.5, start=100.0, with_volume=True):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = [start + i * step for i in range(n)]
    data = {"open": closes, "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes], "close": closes}
    if with_volume:
        data["volume"] = [vol] * n
    return pd.DataFrame(data, index=idx)


class TestWindowLiquidity:
    def test_adv_is_the_20_bar_mean_volume(self):
        df = _bars(n=40, vol=250_000)
        df.iloc[:20, df.columns.get_loc("volume")] = 9_999_999  # outside the lookback
        adv, vol = backtester._window_liquidity(df)
        assert adv == pytest.approx(250_000)
        assert vol is not None and vol > 0

    def test_zero_volume_bars_are_ignored(self):
        df = _bars(n=25, vol=400_000)
        df.iloc[-1, df.columns.get_loc("volume")] = 0
        adv, _ = backtester._window_liquidity(df)
        assert adv == pytest.approx(400_000)

    def test_realized_vol_in_bps(self):
        # +1% every day → std of returns ≈ 0 ; alternate ±1% → ~100 bps
        n = 30
        df = _bars(n=n)
        df["close"] = [100.0 * (1.01 ** ((i + 1) % 2)) for i in range(n)]
        _, vol = backtester._window_liquidity(df)
        assert 80 < vol < 120

    def test_missing_volume_or_short_window_falls_back_to_model_defaults(self):
        adv, vol = backtester._window_liquidity(_bars(n=30, with_volume=False))
        assert adv is None and vol is not None
        adv, vol = backtester._window_liquidity(_bars(n=2))
        assert vol is None
        assert backtester._window_liquidity(object()) == (None, None)


class TestCallSitesUseTheWindow:
    def test_no_hardcoded_adv_remains(self):
        src = open(os.path.join(REPO, "backtester.py")).read()
        assert not re.search(r"adv_shares=1_000_000\s*,", src), (
            "a slippage call still prices impact against a flat 1M-share ADV")
        assert src.count("adv_shares=_adv, daily_vol_bps=_vol") == 2, (
            "both the entry and the exit slippage calls must take the "
            "symbol's own ADV and vol from the window")

    def test_thin_symbol_pays_more_impact_than_liquid_one(self):
        from slippage_model import estimate_slippage
        thin = backtester._window_liquidity(_bars(n=30, vol=50_000))
        liquid = backtester._window_liquidity(_bars(n=30, vol=20_000_000))
        qty = int(100_000 * 0.10 / 100.0)   # the backtest's 10% sizing at $100
        e_thin = estimate_slippage(symbol="T", qty=qty, side="buy", decision_price=100.0,
                                   adv_shares=thin[0], daily_vol_bps=thin[1])
        e_liq = estimate_slippage(symbol="L", qty=qty, side="buy", decision_price=100.0,
                                  adv_shares=liquid[0], daily_vol_bps=liquid[1])
        assert e_thin["impact_bps"] > e_liq["impact_bps"] > 0
        assert e_thin["fill_price"] > e_liq["fill_price"] > 100.0
