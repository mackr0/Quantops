"""short_squeeze_setup — high short interest + breakout = squeeze fuel.

When a stock with >15% short interest breaks above its 20-day high on
elevated volume, the short book is under pressure. Forced covering
amplifies the move. This is the classic squeeze setup — it's short-
lived (days, not weeks) but produces outsized returns when it fires.

Crypto is excluded (no short-interest data equivalent).
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "short_squeeze_setup"
APPLICABLE_MARKETS = ["micro", "small", "midcap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from alternative_data import get_short_interest
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            short = get_short_interest(symbol) or {}
            pct_float = float(short.get("short_pct_float", 0) or 0)
            if pct_float < 15.0:
                continue

            df = get_bars(symbol, limit=25)
            if df is None or len(df) < 21:
                continue

            price = float(df["close"].iloc[-1])
            prior_high = float(df["high"].iloc[-21:-1].max())
            if prior_high <= 0 or price <= prior_high:
                continue

            # Volume confirmation
            vol = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean())
            if avg_vol <= 0 or vol < avg_vol * 1.5:
                continue

            # Days-to-cover is the acceleration factor
            days_to_cover = float(short.get("days_to_cover", 0) or 0)
            score = 2 if days_to_cover >= 5 else 1

            out.append({
                "symbol": symbol,
                "signal": "BUY",
                "score": score,
                "votes": {NAME: "BUY"},
                "price": price,
                "reason": (
                    f"Short squeeze setup: {pct_float:.1f}% SI, "
                    f"{days_to_cover:.1f} days to cover, "
                    f"breakout above 20d high on {vol/avg_vol:.1f}x volume"
                ),
            })
        except Exception:
            continue
    return out
