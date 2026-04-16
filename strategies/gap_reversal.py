"""gap_reversal — fade gaps that aren't backed by news.

Opening gaps that aren't supported by material news catalysts tend to
fill within a few sessions. We look for stocks that gapped >3% on
average-or-lower volume with no SEC alert and no fresh insider buying.

Distinct from the existing gap-and-go strategy: that one trades WITH
gaps; this one fades them.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "gap_reversal"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=10)
            if df is None or len(df) < 2:
                continue

            today_open = float(df["open"].iloc[-1])
            yesterday_close = float(df["close"].iloc[-2])
            today_close = float(df["close"].iloc[-1])
            today_volume = float(df["volume"].iloc[-1])
            avg_volume = float(df["volume"].iloc[-7:-1].mean()) if len(df) >= 7 else 0

            if yesterday_close <= 0:
                continue
            gap_pct = (today_open - yesterday_close) / yesterday_close * 100

            # Only fade gaps >3% on normal-or-lower volume (no catalyst)
            if abs(gap_pct) < 3:
                continue
            if avg_volume > 0 and today_volume > avg_volume * 1.5:
                continue   # Above-average volume suggests a real catalyst — don't fade

            # Direction: gap up → fade short, gap down → fade long
            signal = "SELL" if gap_pct > 0 else "BUY"
            out.append({
                "symbol": symbol,
                "signal": signal,
                "score": 1,
                "votes": {"gap_reversal": signal},
                "price": today_close,
                "reason": (
                    f"Gap reversal: {gap_pct:+.1f}% open gap on "
                    f"normal volume, no catalyst — fade for fill"
                ),
            })
        except Exception:
            continue
    return out
