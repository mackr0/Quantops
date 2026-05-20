"""news_sentiment_spike — trade decisive news-driven sentiment shifts.

When fresh news delivers a clearly directional sentiment signal that
the price has begun to confirm, the drift has 1–3 trading days of
follow-through on average (Tetlock 2007, Garcia 2013). We only trigger
when sentiment is strongly directional AND price action agrees —
without the price filter, we'd be fading noisy headlines.
"""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)

from typing import Any, Dict, List


NAME = "news_sentiment_spike"
APPLICABLE_MARKETS = ["stocks", "crypto"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars
    from news_sentiment import get_sentiment_signal

    out = []
    for symbol in universe:
        try:
            sent = get_sentiment_signal(symbol) or {}
            # Real contract from news_sentiment.get_sentiment_signal:
            #   signal: 'BUY' | 'SELL' | 'HOLD'
            #   sentiment_score: float in roughly [-1, +1]
            #   label: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
            #   news_count: int
            sig_signal = (sent.get("signal") or "").upper()
            score = float(sent.get("sentiment_score", 0) or 0)
            news_count = int(sent.get("news_count", 0) or 0)

            # Need a real signal backed by enough news to be decisive.
            if sig_signal not in ("BUY", "SELL"):
                continue
            # Magnitude floor: |score| >= 0.5 means the analyzer was
            # decisive (the underlying call already requires |score|>0.3
            # to emit BUY/SELL — we're stricter here to filter noise).
            if abs(score) < 0.5:
                continue
            if news_count < 2:
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 2:
                continue
            price = float(df["close"].iloc[-1])
            prev_close = float(df["close"].iloc[-2])
            move_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0

            # Price must confirm the sentiment direction.
            if sig_signal == "BUY" and move_pct < 1.0:
                continue
            if sig_signal == "SELL" and move_pct > -1.0:
                continue

            out.append({
                "symbol": symbol,
                "signal": sig_signal,
                "score": 1,
                "votes": {NAME: sig_signal},
                "price": price,
                "reason": (
                    f"News sentiment spike: {sig_signal} (sentiment "
                    f"{score:+.2f}, {news_count} headlines) + price "
                    f"confirming ({move_pct:+.1f}%)"
                ),
            })
        except (KeyError, ValueError, AttributeError, TypeError,
                IndexError, ZeroDivisionError, OSError) as _ss_exc:
            # Per-symbol strategy scoring loop; one bad symbol
            # shouldn't kill the strategy loop. Surface for follow-up.
            logger.debug(
                "%s scoring failed for %s: %s: %s",
                NAME, symbol, type(_ss_exc).__name__, _ss_exc,
            )
            continue
    return out
