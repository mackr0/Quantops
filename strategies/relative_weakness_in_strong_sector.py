"""relative_weakness_in_strong_sector — stock weakening into sector strength.

When a stock can't keep up with its own sector during a sector rally,
that's usually a fundamental problem the market knows about and the
chart is starting to reflect. The sector tailwind isn't enough to
lift it.

This is one of the highest-quality short patterns because the SHORT
isn't betting on a market or sector reversal — it's betting that the
SPECIFIC name has weakness while the broader environment supports it.
When the sector eventually pauses or rotates, this name leads the
decline.

Detection:
  - Stock's sector ETF (XLK, XLF, XLE, etc.) up >=2% over 5 trading days.
  - Stock down or flat (<= +0.5%) over the same 5 days.
  - Relative-strength gap: sector ETF return − stock return >= 3%.
  - Stock is below its 20-day moving average (trend confirmation).

Markets: equities only. The sector ETF mapping doesn't apply to
crypto or micro-caps that lack clean sector classification.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "relative_weakness_in_strong_sector"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


# Sector ETF mapping. Falls back to SPY for unknown sectors so the
# strategy still emits if classification fails.
_SECTOR_ETF = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Materials": "XLB",
}


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    # Cache sector-ETF returns so we only compute each once per scan.
    sector_returns: Dict[str, float] = {}

    def _sector_5d_return(etf: str) -> float:
        if etf in sector_returns:
            return sector_returns[etf]
        try:
            df = get_bars(etf, limit=10)
            if df is None or len(df) < 6:
                sector_returns[etf] = 0.0
                return 0.0
            ret = (float(df["close"].iloc[-1]) - float(df["close"].iloc[-6])) / float(df["close"].iloc[-6]) * 100
            sector_returns[etf] = ret
            return ret
        except Exception:
            sector_returns[etf] = 0.0
            return 0.0

    try:
        from sector_classifier import get_sector
    except Exception:
        get_sector = lambda s: None  # noqa: E731

    out = []
    for symbol in universe:
        try:
            sector = get_sector(symbol) if callable(get_sector) else None
            etf = _SECTOR_ETF.get(sector or "", "SPY")
            sec_ret = _sector_5d_return(etf)
            if sec_ret < 2.0:
                continue  # sector not strong enough — pattern doesn't apply

            df = get_bars(symbol, limit=30)
            if df is None or len(df) < 22:
                continue

            close_now = float(df["close"].iloc[-1])
            close_5ago = float(df["close"].iloc[-6])
            stock_5d_ret = (close_now - close_5ago) / close_5ago * 100

            # Stock should be flat or down (<= +0.5% — anything more is
            # participating, not lagging)
            if stock_5d_ret > 0.5:
                continue

            rs_gap = sec_ret - stock_5d_ret
            if rs_gap < 3.0:
                continue  # gap too small to be a real divergence

            # Trend confirmation — below the 20-day MA
            sma20 = df["close"].iloc[-21:-1].astype(float).mean()
            if close_now >= sma20:
                continue

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Relative weakness vs {etf}: stock {stock_5d_ret:+.1f}% "
                    f"vs sector {sec_ret:+.1f}% over 5d (gap {rs_gap:.1f}%), "
                    f"price below 20d MA"
                ),
            })
        except Exception:
            continue
    return out
