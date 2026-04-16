"""macd_cross_confirmation — classic MACD cross with multi-factor confirmation.

MACD histogram crossing zero is one of the most-cited technical signals
and one of the most-abused in isolation — the raw signal has no edge
on its own. The edge lives in confirmation: we only take the cross
when RSI is in a trending zone (not extreme) and volume confirms.

Works across equities and crypto because it's pure price/volume.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "macd_cross_confirmation"
APPLICABLE_MARKETS = ["small", "midcap", "largecap", "crypto"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=60)
            if df is None or len(df) < 30:
                continue
            if "macd_histogram" not in df.columns:
                df = add_indicators(df)

            hist_now = df["macd_histogram"].iloc[-1]
            hist_prev = df["macd_histogram"].iloc[-2]
            if hist_now is None or hist_prev is None:
                continue
            hist_now = float(hist_now)
            hist_prev = float(hist_prev)

            # Zero-line cross required
            bull_cross = hist_prev <= 0 and hist_now > 0
            bear_cross = hist_prev >= 0 and hist_now < 0
            if not (bull_cross or bear_cross):
                continue

            rsi = float(df["rsi"].iloc[-1]) if df["rsi"].iloc[-1] is not None else 50
            price = float(df["close"].iloc[-1])
            vol = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0

            # Volume confirmation — ignore weak-volume crosses
            if avg_vol <= 0 or vol < avg_vol * 1.2:
                continue

            if bull_cross:
                # Must not be fighting an overbought trend
                if rsi >= 75 or rsi < 45:
                    continue
                signal = "BUY"
            else:
                if rsi <= 25 or rsi > 55:
                    continue
                signal = "SELL"

            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {NAME: signal},
                "price": price,
                "reason": (
                    f"MACD {'bullish' if bull_cross else 'bearish'} cross "
                    f"with RSI {rsi:.0f} and {vol/avg_vol:.1f}x volume"
                ),
            })
        except Exception:
            continue
    return out
