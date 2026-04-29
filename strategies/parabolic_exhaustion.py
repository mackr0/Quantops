"""parabolic_exhaustion — extreme overbought + reversal candle = mean reversion.

Names that go vertical eventually pause or reverse. We don't try to
top-tick — the fade is hard and squeezes are real. Instead we wait for
TWO confirmations:

  (1) Run-up itself: +25% or more in the trailing 10 trading days.
  (2) RSI > 80 (overbought).
  (3) The latest candle is a clear reversal pattern:
      bearish engulfing OR shooting star OR a -2% drop from the
      intra-day high on volume above average.

This shifts us from "shorting strength" (suicide) to "shorting the
first crack in strength." Win rate on this pattern is moderate but
when it works the moves are large (15-30% over 5-10 sessions is
typical).

Markets: equities (small/mid). Large-cap parabolics are rare and
crypto fades work on different timescales.
"""

from __future__ import annotations

from typing import Any, Dict, List


NAME = "parabolic_exhaustion"
APPLICABLE_MARKETS = ["small", "midcap"]


def find_candidates(ctx: Any, universe: List[str]) -> List[Dict[str, Any]]:
    from market_data import get_bars, add_indicators

    out = []
    for symbol in universe:
        try:
            df = get_bars(symbol, limit=30)
            if df is None or len(df) < 12:
                continue
            if "rsi" not in df.columns:
                df = add_indicators(df)

            close_now = float(df["close"].iloc[-1])
            close_10ago = float(df["close"].iloc[-11])
            run_up_pct = (close_now - close_10ago) / close_10ago * 100
            if run_up_pct < 25.0:
                continue

            rsi = df["rsi"].iloc[-1]
            if rsi is None:
                continue
            rsi = float(rsi)
            if rsi < 80:
                continue

            # Reversal candle detection on the latest bar
            o = float(df["open"].iloc[-1])
            h = float(df["high"].iloc[-1])
            l = float(df["low"].iloc[-1])
            c = float(df["close"].iloc[-1])
            o_prev = float(df["open"].iloc[-2])
            c_prev = float(df["close"].iloc[-2])
            vol_now = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-11:-1].mean())
            if avg_vol <= 0:
                continue

            range_today = max(h - l, 1e-6)
            body_today = abs(c - o)
            upper_wick = h - max(c, o)
            drop_from_high_pct = (h - c) / h * 100

            bearish_engulfing = (c_prev > o_prev  # prev candle green
                                 and c < o       # today red
                                 and o > c_prev   # opens above prev close
                                 and c < o_prev)  # closes below prev open
            shooting_star = (upper_wick > body_today * 2
                             and (c - l) < range_today * 0.3
                             and c < o)
            sharp_drop = (drop_from_high_pct >= 2.0
                          and vol_now > avg_vol * 1.0
                          and c < o)

            if not (bearish_engulfing or shooting_star or sharp_drop):
                continue

            pattern = (
                "bearish engulfing" if bearish_engulfing
                else "shooting star" if shooting_star
                else f"-{drop_from_high_pct:.1f}% drop from high"
            )

            out.append({
                "symbol": symbol,
                "signal": "SHORT",
                "score": 2,
                "votes": {NAME: "SHORT"},
                "price": close_now,
                "reason": (
                    f"Parabolic exhaustion: +{run_up_pct:.1f}% in 10d, "
                    f"RSI {rsi:.0f}, {pattern} on {vol_now/avg_vol:.1f}× volume"
                ),
            })
        except Exception:
            continue
    return out
