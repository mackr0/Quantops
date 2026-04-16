"""high_iv_rank_fade — fade extreme price moves when IV rank is elevated.

When implied volatility rank is high (> 80), options premiums are
expensive, and extreme short-term price moves tend to mean-revert as
the rich premiums get sold into the rally. This is effectively a
"sell-the-volatility" proxy expressed through spot price.

Combines `options_oracle` IV rank with RSI extremes to time entries.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "high_iv_rank_fade"
APPLICABLE_MARKETS = ["midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators
    from options_oracle import get_options_oracle

    out = []
    for symbol in universe:
        try:
            oracle = get_options_oracle(symbol) or {}
            iv_rank = oracle.get("iv_rank")
            if iv_rank is None or iv_rank < 80:
                continue

            df = get_bars(symbol, limit=40)
            if df is None or len(df) < 20:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)

            rsi = float(df["rsi"].iloc[-1]) if df["rsi"].iloc[-1] is not None else 50
            price = float(df["close"].iloc[-1])

            # Fade extremes — high IV rank means moves are likely overdone
            if rsi >= 75:
                signal = "SELL"
                reason = f"High IV rank ({iv_rank:.0f}) + overbought RSI {rsi:.0f} — fade the move"
            elif rsi <= 25:
                signal = "BUY"
                reason = f"High IV rank ({iv_rank:.0f}) + oversold RSI {rsi:.0f} — fade the decline"
            else:
                continue

            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {NAME: signal},
                "price": price,
                "reason": reason,
            })
        except Exception:
            continue
    return out
