"""earnings_drift — post-earnings announcement drift (PEAD).

A robust academic anomaly: stocks with strong earnings reactions tend to
continue drifting in the same direction for 1-3 months. We trigger when
a stock has just had earnings (within the last 5 trading days) AND price
moved decisively (>5%) on the announcement day.

Avoids the day-of earnings event itself — that's covered by avoid_earnings_days.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "earnings_drift"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from earnings_calendar import check_earnings
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            earn = check_earnings(symbol)
            if not earn:
                continue
            days_since = earn.get("days_since_last", 999)
            if days_since is None or days_since > 5 or days_since < 1:
                continue

            df = get_bars(symbol, limit=10)
            if df is None or len(df) < days_since + 1:
                continue

            # Compare close N days ago vs prior close
            anchor_close = float(df["close"].iloc[-(days_since + 1)])
            announce_close = float(df["close"].iloc[-days_since])
            current = float(df["close"].iloc[-1])

            if anchor_close <= 0:
                continue
            announce_move = (announce_close - anchor_close) / anchor_close * 100
            if abs(announce_move) < 5:
                continue

            signal = "BUY" if announce_move > 0 else "SELL"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 2 if abs(announce_move) > 8 else 1,
                "votes": {"earnings_drift": signal},
                "price": current,
                "reason": (
                    f"PEAD: earnings {days_since}d ago, "
                    f"announce-day move {announce_move:+.1f}%"
                ),
            })
        except Exception:
            continue
    return out
