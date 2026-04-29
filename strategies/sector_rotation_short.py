"""sector_rotation_short — short names in the worst-performing sectors.

P3.3 of LONG_SHORT_PLAN.md. Sector rotation has predictable
asymmetry: when capital flows OUT of a sector (bottom-3 by
trailing 5d return), individual names in that sector continue
underperforming for 5-15 days as the rotation completes.
Documented in academic factor literature and standard practice
in stat-arb funds.

Detection (all must hold):

  1. Symbol's sector is in the bottom-3 by 5-day return per
     `macro_data.get_sector_momentum_ranking()`.
  2. Stock's 5-day return is also negative (confirming the
     rotation is hitting THIS specific name, not just sector
     averages dragged down by others).
  3. Stock is below its 20-day SMA (trend confirmation).
  4. RSI is between 35 and 70 (NOT oversold — oversold names
     bounce; we want continuation candidates).
  5. Stock isn't in the SAME sector as the top-3 (defends
     against false-positive sector classification).

Score: 2 (medium-conviction; sector signal is broader than a
specific catalyst).

NOT tagged in `_CATALYST_SHORT_STRATEGIES` because rotation is
a technical/macro pattern, not a company-specific thesis.
Strong-bull regimes will filter this strategy out — appropriate,
because rotation patterns are weaker when the broader market
is strongly bid.

Markets: equities only. Crypto sectors don't behave the same way.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "sector_rotation_short"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators

    # Single sector-momentum read per scan — cached upstream
    try:
        from macro_data import get_sector_momentum_ranking
        ranking = get_sector_momentum_ranking() or {}
    except Exception:
        return []

    bottom_set = set(ranking.get("bottom_3") or [])
    top_set = set(ranking.get("top_3") or [])
    if not bottom_set:
        return []

    try:
        from sector_classifier import get_sector
    except Exception:
        return []

    out = []
    for symbol in universe:
        try:
            sector = (get_sector(symbol) or "").lower()
            if not sector or sector not in bottom_set:
                continue
            if sector in top_set:
                # Pathological case — sector classified into both top
                # and bottom. Skip rather than emit confused signals.
                continue

            df = get_bars(symbol, limit=30)
            if df is None or len(df) < 22:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)

            close_now = float(df["close"].iloc[-1])
            close_5ago = float(df["close"].iloc[-6])
            stock_5d_ret = (close_now - close_5ago) / close_5ago * 100
            if stock_5d_ret >= 0:
                continue  # name itself is positive — rotation hitting peers, not it

            sma_20 = float(df["close"].iloc[-21:-1].astype(float).mean())
            if close_now >= sma_20:
                continue

            rsi = df["rsi"].iloc[-1]
            if rsi is None:
                continue
            rsi = float(rsi)
            if rsi < 35 or rsi > 70:
                continue  # oversold (bounce risk) or overbought (mean revert)

            # Match sector by name in the rankings list to extract its return
            rotation_phase = ranking.get("rotation_phase", "mixed")
            sector_ret_5d = next(
                (r["return_5d"] for r in (ranking.get("rankings") or [])
                 if r.get("sector") == sector),
                None,
            )

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Sector rotation: {sector} in bottom-3 "
                    f"({sector_ret_5d:+.1f}% 5d sector return; "
                    f"phase {rotation_phase}); stock {stock_5d_ret:+.1f}% "
                    f"5d, below 20d SMA, RSI {rsi:.0f}"
                ),
            })
        except Exception:
            continue
    return out
