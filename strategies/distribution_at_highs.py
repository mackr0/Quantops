"""distribution_at_highs — smart-money exit while price hides at highs.

Classic Wyckoff-style distribution pattern. Price flatlines or grinds
sideways near 52-week highs while volume rises on red days and falls
on green days. Big holders are quietly unloading; retail is too
focused on "the high" to notice the rotation.

Detection signals (all must hold):
  - Price within 5% of the 60-day high.
  - Last 10 days' average true range tightening relative to the prior
    20 days (consolidation, not expansion).
  - Down-day volume averages higher than up-day volume across the
    last 10 sessions (the asymmetry is the tell).
  - Net price change over those 10 days is roughly flat (within ±3%).

When the breakdown comes, it usually comes hard — these distribution
tops resolve fast because the buying pool is already exhausted.

Markets: equities only. Crypto's 24/7 cadence breaks the daily
volume-asymmetry signal.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "distribution_at_highs"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=80)
            if df is None or len(df) < 35:
                continue

            close_now = float(df["close"].iloc[-1])
            high_60 = float(df["high"].iloc[-60:].max())
            if high_60 <= 0:
                continue
            distance_from_high_pct = (high_60 - close_now) / high_60 * 100
            if distance_from_high_pct > 5.0:
                continue  # not near highs anymore

            # Net move over last 10 sessions ~ flat
            close_10ago = float(df["close"].iloc[-11])
            net_move_pct = (close_now - close_10ago) / close_10ago * 100
            if abs(net_move_pct) > 3.0:
                continue

            # Range tightening: avg(high-low) last 10 vs prior 20
            range_recent = (df["high"].iloc[-10:] - df["low"].iloc[-10:]).mean()
            range_prior = (df["high"].iloc[-30:-10] - df["low"].iloc[-30:-10]).mean()
            if range_prior <= 0 or range_recent / range_prior > 0.85:
                continue

            # Down-day vs up-day volume asymmetry — the tell
            up_vol = []
            down_vol = []
            for i in range(-10, 0):
                o = float(df["open"].iloc[i])
                c = float(df["close"].iloc[i])
                v = float(df["volume"].iloc[i])
                if c > o:
                    up_vol.append(v)
                elif c < o:
                    down_vol.append(v)
            if not up_vol or not down_vol:
                continue
            avg_up = sum(up_vol) / len(up_vol)
            avg_down = sum(down_vol) / len(down_vol)
            if avg_down <= avg_up * 1.15:
                continue  # need >=15% asymmetry toward down-day volume

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Distribution at highs: {distance_from_high_pct:.1f}% "
                    f"from 60-day high, net {net_move_pct:+.1f}% over 10d, "
                    f"down-day vol {avg_down/avg_up:.2f}× up-day vol"
                ),
            })
        except Exception:
            continue
    return out
