"""fifty_two_week_breakout — new 52-week highs on confirmed volume.

One of the oldest and most robust anomalies in the literature: stocks
making new 52-week highs on above-average volume exhibit persistent
positive drift over the following 1–4 weeks (George & Hwang 2004).
The volume filter is critical — breakouts on thin volume are failure-
prone retail-driven moves.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "fifty_two_week_breakout"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            # 252 trading days ≈ 52 weeks
            df = get_bars(symbol, limit=260)
            if df is None or len(df) < 100:
                continue

            high = float(df["high"].iloc[-1])
            price = float(df["close"].iloc[-1])
            prior_high = float(df["high"].iloc[:-1].max())
            if prior_high <= 0:
                continue

            # Must actually break (not just equal) the prior 52w high
            if high <= prior_high:
                continue

            # Volume confirmation: today's vol >= 1.5× 20-day avg
            vol = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0
            if avg_vol <= 0 or vol < avg_vol * 1.5:
                continue

            # Avoid over-extended blow-offs: today's move > 15% is
            # probably a news-driven spike that's already crowded
            prev_close = float(df["close"].iloc[-2])
            if prev_close > 0 and (price - prev_close) / prev_close * 100 > 15:
                continue

            out.append({
                "symbol": symbol,
                "signal": "BUY",
                "score": 2,
                "votes": {NAME: "BUY"},
                "price": price,
                "reason": (
                    f"52-week breakout: new high at ${high:.2f} on "
                    f"{vol/avg_vol:.1f}x avg volume"
                ),
            })
        except Exception:
            continue
    return out
