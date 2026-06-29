"""Dollar-ADV liquidity floor in the screener (2026-06-26).

Gap it closes: the screener's `min_volume` floor counts SHARES, which
can't tell a $2 stock from a $50 one. 500k shares of a $2 name is $1M of
average daily dollar volume (thin, wide spreads, whipsaws the ATR stops)
while 500k shares of a $50 name is $25M (deep). The new per-profile
`min_adv` floor gates on average daily DOLLAR volume — price * 20-day
mean share volume — the institutional "liquid enough to trade" test.

These pin that:
  * screen_by_price_range excludes a name that clears the SHARE floor but
    is below the DOLLAR floor (the cheap-but-liquid quadrant);
  * a name above the dollar floor still passes;
  * with the floor at 0 the same cheap name passes (proving the floor,
    not some other gate, is what excluded it);
  * the default is the $5M institutional floor.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

import config
import screener


def _bars(close: float, volume: int, n: int = 20) -> pd.DataFrame:
    """A flat n-bar OHLCV frame at a constant price/volume."""
    return pd.DataFrame({"close": [close] * n, "volume": [volume] * n})


# Three names that ALL clear price ($1–$10k) and the 100k SHARE floor,
# but differ on DOLLAR liquidity:
#   LIQ  $50 x 1.0M = $50M ADV  (deep)
#   MID  $10 x 0.6M = $6M  ADV  (above the $5M floor)
#   THIN $2  x 0.5M = $1M  ADV  (cheap-but-liquid — the gap)
_UNIVERSE_BARS = {
    "LIQ": _bars(50.0, 1_000_000),
    "MID": _bars(10.0, 600_000),
    "THIN": _bars(2.0, 500_000),
}


def _run(min_adv):
    with patch("screener._get_bars_for_symbols", return_value=dict(_UNIVERSE_BARS)):
        rows = screener.screen_by_price_range(
            min_price=1.0, max_price=10_000.0, min_volume=100_000,
            min_adv=min_adv, universe=list(_UNIVERSE_BARS), limit=50,
        )
    return {r["symbol"] for r in rows}, rows


def test_default_floor_is_five_million():
    assert config.SCREEN_MIN_ADV == 5_000_000


def test_cheap_but_liquid_name_is_excluded_by_dollar_floor():
    symbols, _ = _run(min_adv=5_000_000)
    assert "THIN" not in symbols, (
        "THIN ($2 x 500k shares = $1M ADV) clears the 100k SHARE floor but "
        "is below the $5M DOLLAR floor — it must be excluded")
    assert {"LIQ", "MID"} <= symbols, (
        "names above the $5M dollar floor must still pass")


def test_floor_off_lets_the_cheap_name_through():
    symbols, _ = _run(min_adv=0)
    assert "THIN" in symbols, (
        "with min_adv=0 the cheap name must pass — proving the dollar floor "
        "(not price/share gating) is what excluded it at $5M")


def test_passing_rows_report_adv_and_carry_the_value():
    _, rows = _run(min_adv=5_000_000)
    liq = next(r for r in rows if r["symbol"] == "LIQ")
    assert liq["adv_dollar"] == 50_000_000
    assert "ADV" in liq["reason"]


def test_adv_uses_twenty_day_mean_not_last_bar():
    # A name whose LAST bar volume is huge but whose 20-day mean is thin
    # must be judged on the mean (a single spike day shouldn't qualify a
    # structurally illiquid name).
    df = pd.DataFrame({
        "close": [4.0] * 20,
        # 19 quiet days + one 10M-share spike: last bar = 10M, but
        # mean ≈ 0.6M -> ADV ≈ $2.4M, below the $5M floor.
        "volume": [100_000] * 19 + [10_000_000],
    })
    with patch("screener._get_bars_for_symbols", return_value={"SPIKE": df}):
        rows = screener.screen_by_price_range(
            min_price=1.0, max_price=10_000.0, min_volume=50_000,
            min_adv=5_000_000, universe=["SPIKE"], limit=50,
        )
    assert rows == [], (
        "ADV must be the 20-day MEAN dollar volume; a single spike day "
        "must not qualify a structurally thin name")
