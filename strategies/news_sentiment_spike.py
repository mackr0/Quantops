"""news_sentiment_spike — trade decisive news-driven sentiment shifts.

When fresh news delivers a clearly directional sentiment signal that
the price has begun to confirm, the drift has 1–3 trading days of
follow-through on average (Tetlock 2007, Garcia 2013). We only trigger
when sentiment is strongly directional AND price action agrees —
without the price filter, we'd be fading noisy headlines.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "news_sentiment_spike"
APPLICABLE_MARKETS = ["small", "midcap", "largecap", "crypto"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars
    from news_sentiment import get_sentiment_signal

    out = []
    for symbol in universe:
        try:
            sent = get_sentiment_signal(symbol) or {}
            # Expected fields: direction ('bullish'|'bearish'|'neutral'), score (0-100)
            direction = (sent.get("direction") or "").lower()
            score = float(sent.get("score", 0) or 0)
            if direction not in ("bullish", "bearish") or score < 70:
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 2:
                continue
            price = float(df["close"].iloc[-1])
            prev_close = float(df["close"].iloc[-2])
            move_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0

            # Price must confirm the sentiment direction
            if direction == "bullish" and move_pct < 1.0:
                continue
            if direction == "bearish" and move_pct > -1.0:
                continue

            signal = "BUY" if direction == "bullish" else "SELL"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {NAME: signal},
                "price": price,
                "reason": (
                    f"News sentiment spike: {direction} signal (score {score:.0f}) "
                    f"+ price confirming ({move_pct:+.1f}%)"
                ),
            })
        except Exception:
            continue
    return out
