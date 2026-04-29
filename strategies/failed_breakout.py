"""failed_breakout — long trap. Breakout reverses back below resistance.

Most powerful short setup in technical analysis. A stock breaks above
a well-defined resistance level (20-day high typical), drawing in
breakout buyers, then closes back below the level within 1-3 days.
Those new longs are now underwater AND watching support; the typical
move is 5-12% lower over the next 3-10 sessions as the trap unwinds.

Detection:
  - Price broke above the 20-day high in the last 5 trading days.
  - Latest close is BACK below that prior 20-day high.
  - Volume on the failure day >= 1.2× the 20-day average (real
    distribution, not noise).
  - The breakout high itself was meaningful (>=1.5% above the level).

Markets: equities only. Crypto resistance levels are too noisy and
24/7 cadence breaks the "breakout day" framing.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "failed_breakout"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=40)
            if df is None or len(df) < 25:
                continue

            close_now = float(df["close"].iloc[-1])
            vol_now = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean())
            if avg_vol <= 0:
                continue

            # Find the 20-day resistance as it stood BEFORE the recent
            # breakout window. Anchor 6 days back.
            resistance = float(df["high"].iloc[-26:-6].max())

            # Did we break above resistance in the last 5 days?
            highs_recent = df["high"].iloc[-6:-1].astype(float)
            breakout_excursion_pct = (highs_recent.max() - resistance) / resistance * 100
            broke_above_recently = breakout_excursion_pct >= 1.5

            # Are we now back BELOW that resistance?
            failed_back = close_now < resistance

            if not (broke_above_recently and failed_back):
                continue

            # Volume confirmation on the failure
            if vol_now < avg_vol * 1.2:
                continue

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Failed breakout: pierced ${resistance:.2f} resistance by "
                    f"{breakout_excursion_pct:.1f}%, closed back at ${close_now:.2f} "
                    f"on {vol_now/avg_vol:.1f}× volume"
                ),
            })
        except Exception:
            continue
    return out
