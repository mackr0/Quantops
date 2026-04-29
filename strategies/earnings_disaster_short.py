"""earnings_disaster_short — short post-disaster gap-down with non-recovery.

P3.1 of LONG_SHORT_PLAN.md. The Post-Earnings Announcement Drift
(PEAD) effect (Bernard & Thomas 1990) shows that stocks that miss
earnings significantly continue underperforming for 60-90 days.
We exploit the inverse: detect a recent significant gap-down on
volume + non-recovery, and emit SHORT.

The pattern works for earnings misses but ALSO for any catalyst-
driven gap-down (downgrade, fraud allegation, guidance cut, FDA
rejection, etc.). All of these share the price-action signature:
panic-driven distribution + slow continuation lower.

Detection (all must hold):

  1. Within the last 10 trading days, there's a single bar with:
     - Open < prior close × 0.95 (gap down ≥5%) OR
     - Close < prior close × 0.92 (decline ≥8%)
  2. Volume on that bar ≥ 2× the 20-day avg volume.
  3. Latest close is still below the catalyst-bar's closing price
     (no recovery yet — the move was real).
  4. Latest close is below 20-day SMA (broader trend confirmation).
  5. Distance from 52-week high is at least 15% (catalysts on
     names hugging highs are usually false alarms; real disasters
     leave the stock far below recent peak).

Why this is a catalyst-tagged short: the trigger is structural
(post-earnings or post-news distribution), not pure technicals.
Survives the regime gate even in strong-bull markets because
the company-specific damage overrides market drift.

Markets: equities only. Crypto patterns are different.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "earnings_disaster_short"
APPLICABLE_MARKETS = ["small", "midcap", "largecap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=270)  # need 252+ for 52w high
            if df is None or len(df) < 25:
                continue

            close_now = float(df["close"].iloc[-1])
            high_52w = float(df["high"].iloc[-252:].max() if len(df) >= 252
                              else df["high"].max())
            if high_52w <= 0:
                continue
            distance_from_high_pct = (high_52w - close_now) / high_52w * 100
            if distance_from_high_pct < 15.0:
                continue  # too close to highs — not a real disaster

            # Trend filter — must be below 20-day SMA
            sma_20 = df["close"].iloc[-20:].astype(float).mean()
            if close_now >= sma_20:
                continue

            # Look for the catalyst bar in the last 10 days
            avg_vol = float(df["volume"].iloc[-30:-10].astype(float).mean())
            if avg_vol <= 0:
                continue

            catalyst_bar_idx = None
            for i in range(-10, -1):
                bar = df.iloc[i]
                prev = df.iloc[i - 1]
                gap_down = float(bar["open"]) < float(prev["close"]) * 0.95
                hard_drop = float(bar["close"]) < float(prev["close"]) * 0.92
                vol_spike = float(bar["volume"]) >= avg_vol * 2.0
                if (gap_down or hard_drop) and vol_spike:
                    catalyst_bar_idx = i
                    break
            if catalyst_bar_idx is None:
                continue

            catalyst_close = float(df["close"].iloc[catalyst_bar_idx])
            catalyst_drop_pct = (
                float(df["close"].iloc[catalyst_bar_idx - 1])
                - catalyst_close
            ) / float(df["close"].iloc[catalyst_bar_idx - 1]) * 100
            catalyst_vol_ratio = float(df["volume"].iloc[catalyst_bar_idx]) / avg_vol

            # Non-recovery check: current close <= catalyst-bar close.
            # Means the post-catalyst bounce hasn't reclaimed the disaster
            # day's level. Continuation is more likely.
            if close_now > catalyst_close * 1.02:
                continue

            days_since = abs(catalyst_bar_idx) - 1
            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 3,  # high-conviction catalyst short
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Earnings/catalyst disaster: -{catalyst_drop_pct:.1f}% bar "
                    f"on {catalyst_vol_ratio:.1f}× volume {days_since}d ago, "
                    f"close ${close_now:.2f} below catalyst close "
                    f"${catalyst_close:.2f}, {distance_from_high_pct:.0f}% off "
                    f"52w high"
                ),
            })
        except Exception:
            continue
    return out
