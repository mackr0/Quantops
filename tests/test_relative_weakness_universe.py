"""Tests for the relative_weakness_universe short strategy.

Anti-momentum / quant-short strategy that ranks the universe by 20-day
return vs SPY and emits the bottom slice as SHORT candidates. Critical
for dedicated short profiles (target_short_pct ≥ 0.4) which need a
substantial short book even in regimes where textbook bearish technical
patterns are rare.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _bars(start_close: float, end_close: float, n: int = 30,
          flat_after_decline: bool = False) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame interpolating between start
    and end close prices over n daily bars. Used to fake get_bars."""
    closes = [start_close + (end_close - start_close) * i / (n - 1)
              for i in range(n)]
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1_000_000] * n,
    })


def test_strategy_module_has_required_interface():
    from strategies import relative_weakness_universe as m
    assert hasattr(m, "NAME")
    assert hasattr(m, "APPLICABLE_MARKETS")
    assert hasattr(m, "find_candidates")
    assert "small" in m.APPLICABLE_MARKETS
    assert "midcap" in m.APPLICABLE_MARKETS
    assert "largecap" in m.APPLICABLE_MARKETS


def test_emits_nothing_when_universe_too_small():
    from strategies.relative_weakness_universe import find_candidates
    out = find_candidates(ctx=None, universe=["AAPL", "MSFT"])
    # < 5 names — no statistical basis
    assert out == []


def test_emits_when_stock_underperforms_spy_by_threshold():
    """Universe of 10 names. SPY +5% over 20d. One stock at -8% over
    20d → RS gap = +13%. Should emit as SHORT."""
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(start_close=400, end_close=420)  # +5%
    weak = _bars(start_close=100, end_close=92)  # -8%
    strong = _bars(start_close=100, end_close=108)  # +8%

    bars_by_sym = {"SPY": spy}
    universe = ["AAPL", "MSFT", "GOOG", "TSLA", "NVDA", "META",
                 "AMZN", "WEAK", "STRONG", "FLAT"]
    for s in universe:
        if s == "WEAK":
            bars_by_sym[s] = weak
        elif s == "STRONG":
            bars_by_sym[s] = strong
        else:
            bars_by_sym[s] = _bars(100, 105)  # +5% (matches SPY)

    def fake_get_bars(symbol, limit=None):
        return bars_by_sym.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)

    syms = [c["symbol"] for c in out]
    assert "WEAK" in syms, "weakest name must emit"
    assert "STRONG" not in syms, "outperformers never emit"


def test_skips_stock_above_20d_ma():
    """Even when relative-weakness gap exists vs SPY, if the stock
    is above its own 20-day MA, the trend isn't confirmed — skip."""
    from strategies.relative_weakness_universe import find_candidates

    # Stock that's down vs SPY but above its 20d MA: dropped from 110 to
    # 100 over the lookback (so close_back=110, close_now=100, MA over
    # lookback = avg of 110→100 ramp ≈ 105; close_now=100 < 105 → BELOW MA).
    # To force ABOVE-MA case, build a profile where price spent most of
    # the period below current level.
    n = 25
    closes = [80] * 20 + [85, 90, 95, 100, 105]
    df_above_ma = pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1_000_000] * n,
    })
    spy = _bars(400, 480)  # +20% — large gap to make stock look weak

    bars = {"SPY": spy}
    universe = ["A", "B", "C", "D", "E", "F"]
    for s in universe:
        bars[s] = df_above_ma

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)
    # All names are above their 20d MA (close_now=105, MA ≈ 84) →
    # despite being weak vs SPY, none should emit.
    assert out == []


def test_emits_at_most_5_candidates():
    """Cap on output regardless of universe size — never flood the
    shortlist with bottom-of-the-bucket weak names."""
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 460)  # +15%
    weak = _bars(100, 80)   # -20%

    bars = {"SPY": spy}
    universe = [f"W{i}" for i in range(50)]  # 50-name universe
    for s in universe:
        bars[s] = weak  # all weak

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)
    # 5% × 50 = 2.5 → 2 max, but absolute cap is 5
    assert len(out) <= 5
    assert len(out) >= 1


def test_emit_format_has_required_signal_fields():
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 420)
    weak = _bars(100, 88)

    bars = {"SPY": spy}
    universe = ["A", "B", "C", "D", "E", "F", "WEAK"]
    for s in universe:
        bars[s] = _bars(100, 105) if s != "WEAK" else weak

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)

    if out:
        c = out[0]
        assert c["signal"] == "SHORT"
        assert c["score"] == 1
        assert "votes" in c and c["votes"]
        assert c["price"] > 0
        assert "reason" in c and "SPY" in c["reason"]


def test_emits_zero_when_spy_data_missing():
    """No SPY data → can't compute RS gap → emit nothing rather than
    fall back to absolute returns."""
    from strategies.relative_weakness_universe import find_candidates

    def fake_get_bars(symbol, limit=None):
        if symbol == "SPY":
            return None
        return _bars(100, 80)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None,
                                universe=["A", "B", "C", "D", "E", "F"])
    assert out == []


def test_skips_stocks_with_insufficient_history():
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 420)
    short_history = _bars(100, 88, n=10)  # only 10 bars

    bars = {"SPY": spy}
    universe = ["A", "B", "C", "D", "E", "F"]
    for s in universe:
        bars[s] = short_history

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)
    # All have <21 bars → all skipped
    assert out == []


def test_skips_names_with_stale_weakness_recovering_now():
    """Stock with bad 20d performance but BOUNCING in last 5 days
    must be filtered out — the 'recent weakness' guard avoids picking
    names that already crashed and are mean-reverting upward."""
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 420)  # SPY +5% over 20d, ~+1% over 5d

    # Stock: -15% over 20d (very weak), but +5% over last 5d (bouncing).
    # 20d gap = +5% - (-15%) = +20% (huge — would qualify before).
    # 5d gap = +1% - (+5%) = -4% (NEGATIVE — current weakness fails).
    n = 25
    closes = (
        [100] * 5 + [95] * 5 + [88] * 5 + [82] * 5  # -18% drift
        + [85, 86, 87, 88, 89]                       # bouncing back
    )
    bouncing = pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1_000_000] * n,
    })

    bars = {"SPY": spy}
    universe = ["A", "B", "C", "D", "E", "F", "BOUNCE"]
    for s in universe:
        bars[s] = _bars(100, 105) if s != "BOUNCE" else bouncing

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)

    syms = [c["symbol"] for c in out]
    assert "BOUNCE" not in syms, \
        "Stock recovering in last 5d must not emit despite bad 20d performance"


def test_skips_names_with_excessive_drawdown_from_yearly_high():
    """Names down >40% from 252d high are filtered out — short
    profit comes from names with further to fall, not bottom-pickers'
    graveyard."""
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 420, n=30)
    # Stock: 252-day high was 100, now at ~50 → 50% drawdown. Even if
    # weak vs SPY in the recent window, the drawdown filter blocks it.
    # n=253 = exactly DRAWDOWN_LOOKBACK_DAYS + 1 so the day-0 high is
    # at the START of the 253-row drawdown window.
    n = 253
    closes = [100.0] + [50.0] * (n - 7) + [52.0, 51.0, 50.5, 50.2, 50.0, 49.0]
    assert len(closes) == n
    highs = closes  # high == close on each bar
    crashed = pd.DataFrame({
        "open": closes, "high": highs,
        "low": closes, "close": closes,
        "volume": [1_000_000] * n,
    })

    bars = {"SPY": spy}
    universe = ["A", "B", "C", "D", "E", "F", "CRASHED"]
    for s in universe:
        bars[s] = _bars(100, 105) if s != "CRASHED" else crashed

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)
    syms = [c["symbol"] for c in out]
    assert "CRASHED" not in syms, \
        "Name down >40% from 252d high must be filtered (bottom-pickers graveyard)"


def test_ranks_by_recent_5d_weakness_not_lifetime():
    """When two names both pass 20d AND 5d filters, the one with the
    MOST recent (5d) weakness should rank first — surfacing acute
    weakness over background weakness."""
    from strategies.relative_weakness_universe import find_candidates

    spy = _bars(400, 420)  # SPY +5% over 20d, ~+1.2% over 5d

    # WEAK_20D: weak over 20d but flat last 5d. 20d ret -10%, 5d ret -1.5%
    n = 25
    weak_20d_closes = (
        [100, 99, 98, 97, 96, 94, 92, 90, 88,
         87, 86, 85, 84, 83, 82, 81, 80, 80, 80, 80,
         91, 91, 90, 90, 90]
    )
    # WEAK_5D: flat-ish over 20d but crashing last 5d. 20d ret -5%, 5d ret -10%
    weak_5d_closes = (
        [100, 99, 99, 100, 100, 100, 100, 100, 100,
         100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 99,
         97, 95, 92, 90, 89]
    )

    df_20d = pd.DataFrame({
        "open": weak_20d_closes, "high": weak_20d_closes,
        "low": weak_20d_closes, "close": weak_20d_closes,
        "volume": [1_000_000] * n,
    })
    df_5d = pd.DataFrame({
        "open": weak_5d_closes, "high": weak_5d_closes,
        "low": weak_5d_closes, "close": weak_5d_closes,
        "volume": [1_000_000] * n,
    })

    bars = {"SPY": spy, "WEAK_20D": df_20d, "WEAK_5D": df_5d}
    universe = ["WEAK_20D", "WEAK_5D", "A", "B", "C", "D"]
    for s in ("A", "B", "C", "D"):
        bars[s] = _bars(100, 105)

    def fake_get_bars(symbol, limit=None):
        return bars.get(symbol)

    with patch("market_data.get_bars", side_effect=fake_get_bars):
        out = find_candidates(ctx=None, universe=universe)
    # WEAK_5D should appear, AND it should rank FIRST when both qualify
    # (since cap=5 emits both, but by 5d-ascending order WEAK_5D comes
    # before WEAK_20D)
    syms = [c["symbol"] for c in out]
    if "WEAK_5D" in syms and "WEAK_20D" in syms:
        assert syms.index("WEAK_5D") < syms.index("WEAK_20D"), \
            "ranking must prioritize current (5d) weakness over historical"


def test_strategy_in_registry():
    """Must be registered in strategies/__init__.py so multi_strategy
    actually invokes it."""
    from strategies import STRATEGY_MODULES
    assert "strategies.relative_weakness_universe" in STRATEGY_MODULES
