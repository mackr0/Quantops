"""vol_regime — trade volatility expansion/contraction regimes.

Uses options oracle GEX (gamma exposure) regime detection. When dealers
are net short gamma, moves amplify — we want directional exposure that
matches the prevailing trend. When dealers are long gamma, moves dampen
and price pins to high-OI strikes.

This strategy fires only on stocks where the options chain reveals a
clear regime (GEX_sign != neutral).
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "vol_regime"
APPLICABLE_MARKETS = ["midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from options_oracle import get_options_oracle
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            oracle = get_options_oracle(symbol)
            if not oracle.get("has_options"):
                continue

            gex = oracle.get("gex", {})
            regime = gex.get("regime", "")
            term = oracle.get("term_structure", {})
            skew = oracle.get("skew", {})

            if regime != "volatility_expansion":
                continue   # Only trade vol expansion (where moves amplify)

            # Decide direction from skew + recent price action
            df = get_bars(symbol, limit=20)
            if df is None or len(df) < 6:
                continue
            week_ago = float(df["close"].iloc[-6])
            current = float(df["close"].iloc[-1])
            if week_ago <= 0:
                continue
            week_move = (current - week_ago) / week_ago * 100

            # If skew is fear-laden but price is rising, contrarian short.
            # If skew is greed-laden but price is falling, contrarian long.
            # Otherwise trade direction in line with the recent move.
            skew_sig = skew.get("signal", "neutral")
            if skew_sig == "fear" and week_move > 3:
                signal = "SELL"
                reason = "Vol expansion + fear skew + recent rally — contrarian short"
            elif skew_sig == "greed" and week_move < -3:
                signal = "BUY"
                reason = "Vol expansion + greed skew + recent dip — contrarian long"
            elif week_move > 2:
                signal = "BUY"
                reason = f"Vol expansion + uptrend ({week_move:+.1f}% 5d)"
            elif week_move < -2:
                signal = "SELL"
                reason = f"Vol expansion + downtrend ({week_move:+.1f}% 5d)"
            else:
                continue

            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {"vol_regime": signal},
                "price": current,
                "reason": reason,
            })
        except Exception:
            continue
    return out
