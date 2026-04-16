"""max_pain_pinning — fade moves that take price away from max pain.

Near monthly options expiration (typically <14 DTE), price tends to
gravitate toward the max-pain strike due to dealer hedging. If price
has moved meaningfully away from max pain, fade the move.

Only triggers on stocks with active options chains and approaching
expiration windows.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "max_pain_pinning"
APPLICABLE_MARKETS = ["midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from options_oracle import get_options_oracle

    out = []
    for symbol in universe:
        try:
            oracle = get_options_oracle(symbol)
            if not oracle.get("has_options"):
                continue
            implied = oracle.get("implied_move", {})
            dte = implied.get("days_to_expiration", 99)
            if dte > 14 or dte < 1:
                continue   # Only fade near expiration

            pain = oracle.get("max_pain", {})
            distance = pain.get("distance_pct", 0)
            current_price = oracle.get("current_price", 0)
            max_pain_strike = pain.get("max_pain_strike", 0)

            if abs(distance) < 3:
                continue   # Already pinned, no edge
            if max_pain_strike <= 0:
                continue

            # Price is above max pain → expect drift down toward strike → SELL
            # Price is below max pain → expect drift up toward strike → BUY
            signal = "SELL" if distance > 0 else "BUY"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {"max_pain_pinning": signal},
                "price": current_price,
                "reason": (
                    f"Max pain pinning: {dte}d to expiration, "
                    f"price {distance:+.1f}% from ${max_pain_strike:.2f} pain strike"
                ),
            })
        except Exception:
            continue
    return out
