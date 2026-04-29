"""breakdown_support — symbol breaks below recent swing-low support on volume.

Classic short setup. The 20-day or 50-day swing low is a level lots of
participants are watching. When price closes below it on above-average
volume, that's "support broken" — typical follow-through is 5-10%
lower over the next 5-15 trading days.

Why this works (when it works): the marginal long buyer at "I'll
support this name at $X" no longer wants to be there once $X breaks.
Stop-loss orders cluster just below support, get triggered on the
break, accelerate the move. Algorithmic shorts pile in.

Why it sometimes fails: low-volume breaks often reverse (the trapped
shorts then have to cover). We require volume >= 1.3× the 20-day
average to filter weak breaks.

Markets: equities only. Crypto support levels are noisier and the
24/7 nature changes the dynamics.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "breakdown_support"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=60)
            if df is None or len(df) < 25:
                continue

            close_now = float(df["close"].iloc[-1])
            low_now = float(df["low"].iloc[-1])
            close_prev = float(df["close"].iloc[-2])
            vol_now = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean())
            if avg_vol <= 0:
                continue

            # Two-tier support: nearer (20-day) and stronger (50-day if available).
            # We trigger on either, but stronger conviction when both are broken.
            swing_low_20 = float(df["low"].iloc[-21:-1].min())
            swing_low_50 = (float(df["low"].iloc[-51:-1].min())
                            if len(df) >= 51 else swing_low_20)

            broke_20 = close_now < swing_low_20 and close_prev >= swing_low_20
            broke_50 = close_now < swing_low_50 and close_prev >= swing_low_50

            if not (broke_20 or broke_50):
                continue

            # Volume confirmation — kills the no-conviction noise breaks
            if vol_now < avg_vol * 1.3:
                continue

            score = 2 if broke_50 else 1  # 50-day break is stronger
            level_label = "50-day swing low" if broke_50 else "20-day swing low"
            level_value = swing_low_50 if broke_50 else swing_low_20

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": score,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Breakdown of {level_label} at ${level_value:.2f}: "
                    f"close ${close_now:.2f} on {vol_now/avg_vol:.1f}× volume"
                ),
            })
        except Exception:
            continue
    return out
