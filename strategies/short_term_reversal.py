"""short_term_reversal — fade 3-day declines on oversold readings.

Jegadeesh (1990) and Lehmann (1990) documented that stocks with sharp
short-term declines tend to mean-revert over the following 1-2 days.
The effect is strongest in liquid names with a clear pullback from a
recent local high.

Academic research shows this anomaly has decayed in large caps but
persists in small/mid cap and in volatile market regimes.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "short_term_reversal"
APPLICABLE_MARKETS = ["micro", "small", "midcap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=20)
            if df is None or len(df) < 10:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)

            # Three-day decline: each of last 3 closes must be lower than
            # the close 1 bar prior
            c = df["close"]
            if not (c.iloc[-1] < c.iloc[-2] < c.iloc[-3] < c.iloc[-4]):
                continue

            rsi = float(df["rsi"].iloc[-1]) if df["rsi"].iloc[-1] is not None else 50
            if rsi >= 35:
                continue

            # Must have pulled back at least 3% from the 5-day high
            recent_high = float(df["high"].iloc[-6:-1].max())
            price = float(c.iloc[-1])
            if recent_high <= 0:
                continue
            pullback_pct = (recent_high - price) / recent_high * 100
            if pullback_pct < 3.0:
                continue

            out.append({
                "symbol": symbol,
                "signal": "BUY",
                "score": 1,
                "votes": {NAME: "BUY"},
                "price": price,
                "reason": (
                    f"Short-term reversal: 3-day decline of {pullback_pct:.1f}% "
                    f"from 5d high, RSI {rsi:.0f} — fade for bounce"
                ),
            })
        except Exception:
            continue
    return out
