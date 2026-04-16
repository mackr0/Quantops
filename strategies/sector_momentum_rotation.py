"""sector_momentum_rotation — ride sectors with the strongest 5-day returns.

Rotate capital into the top 2 sectors by rolling 5-day return, avoiding
the bottom 2. The relative-strength effect across sectors is one of the
most persistent anomalies in academic finance (Moskowitz, Asness).

A symbol's sector is looked up via `_guess_sector` in market_data. If
the sector is in the current leader set, the stock is a candidate; if
in the laggard set, it's a short-side candidate.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "sector_momentum_rotation"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_sector_rotation, _guess_sector, get_bars

    try:
        rotation = get_sector_rotation() or {}
    except Exception:
        return []
    if not rotation:
        return []

    # Rank sectors by 5-day return
    sectors = sorted(
        rotation.items(),
        key=lambda kv: kv[1].get("return_5d", 0) or 0,
        reverse=True,
    )
    if len(sectors) < 4:
        return []
    top2 = {s for s, _ in sectors[:2]}
    bottom2 = {s for s, _ in sectors[-2:]}

    out = []
    for symbol in universe:
        try:
            sector = _guess_sector(symbol)
            if sector not in top2 and sector not in bottom2:
                continue

            df = get_bars(symbol, limit=5)
            if df is None or len(df) < 1:
                continue
            price = float(df["close"].iloc[-1])

            if sector in top2:
                signal = "BUY"
                score = 1
                reason = f"Sector rotation: {sector} is a top-2 sector (5d leader)"
            else:
                signal = "SELL"
                score = 1
                reason = f"Sector rotation: {sector} is a bottom-2 sector (5d laggard)"

            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": score,
                "votes": {NAME: signal},
                "price": price,
                "reason": reason,
            })
        except Exception:
            continue
    return out
