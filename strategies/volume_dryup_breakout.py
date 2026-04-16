"""volume_dryup_breakout — quiet consolidation, then expansion.

The classic Minervini/O'Neil setup: a stock consolidates with declining
volume for ~5 days (signaling supply exhaustion), then breaks to a new
10-day high on a volume surge. Low-volume consolidations that resolve
up tend to produce clean follow-through because there's no overhead
supply to absorb.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "volume_dryup_breakout"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=30)
            if df is None or len(df) < 15:
                continue

            # Past 5-day window excluding today — volume should be
            # declining (each day at-or-below the prior day's volume)
            past_vols = [float(df["volume"].iloc[-i]) for i in range(6, 1, -1)]
            if not all(past_vols[i] <= past_vols[i-1] * 1.1
                        for i in range(1, len(past_vols))):
                continue

            # Today's volume must be at least 2× the avg of those 5 days
            avg_recent = sum(past_vols) / len(past_vols)
            today_vol = float(df["volume"].iloc[-1])
            if avg_recent <= 0 or today_vol < avg_recent * 2.0:
                continue

            # Price must break above the 10-day high
            price = float(df["close"].iloc[-1])
            prior_high = float(df["high"].iloc[-11:-1].max())
            if prior_high <= 0 or price <= prior_high:
                continue

            out.append({
                "symbol": symbol,
                "signal": "BUY",
                "score": 1,
                "votes": {NAME: "BUY"},
                "price": price,
                "reason": (
                    f"Volume-dryup breakout: 5d quiet consolidation, "
                    f"breakout above 10d high on {today_vol/avg_recent:.1f}x recent volume"
                ),
            })
        except Exception:
            continue
    return out
