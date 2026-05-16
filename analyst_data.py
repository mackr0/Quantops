"""Analyst recommendation data — period-over-period sentiment shift.

Wraps yfinance's `Ticker.recommendations` because no Alpaca / custom
altdata source exists today for analyst rating distributions. yfinance
is grandfathered HERE per the Alpaca-first → custom-altdata → yfinance-
last rule (documented exception in `docs/04_TECHNICAL_REFERENCE.md`).

A future Phase-6 evaluation may swap the underlying source (Polygon
free tier, Finnhub) — the public function signature is intentionally
data-source-agnostic so the swap is one-module.

yfinance schema (as of 2026-05-15):

    period strongBuy buy hold sell strongSell
    0     0m       6   25  15   1   1   ← most recent month
    1    -1m       6   25  15   1   1
    2    -2m       6   25  15   1   1
    3    -3m       5   25  16   1   1

It's NOT individual rating CHANGES — it's the analyst-distribution
SNAPSHOT per period. We detect a "shift" by comparing two consecutive
periods.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Per-rating contribution to a directional score:
#   strongBuy=+2, buy=+1, hold=0, sell=-1, strongSell=-2
#
# Score is the SUM across all analysts in that period — comparing two
# periods' scores tells us whether the consensus shifted bullish or
# bearish. Normalizing by total analyst count handles symbols with
# growing or shrinking coverage.
_RATING_WEIGHTS = {
    "strongBuy":  2.0,
    "buy":        1.0,
    "hold":       0.0,
    "sell":      -1.0,
    "strongSell": -2.0,
}


def _period_score(row) -> Optional[float]:
    """Return the weighted-mean rating for one period row, or None if
    the row has no analysts at all (zero total)."""
    total = 0
    weighted = 0.0
    for col, w in _RATING_WEIGHTS.items():
        try:
            n = int(row.get(col, 0) or 0)
        except (TypeError, ValueError):
            continue
        total += n
        weighted += w * n
    if total <= 0:
        return None
    return weighted / total


def recommendation_shift(symbol: str) -> Optional[dict]:
    """Compare the most-recent analyst-distribution period to the
    OLDEST available period and return a directional shift summary.

    Why oldest, not prior: yfinance's `0m` and `-1m` snapshots are
    typically identical (same data point reported under both labels);
    `-2m` and `-3m` carry the actual historical comparison. Using
    oldest captures the full ~3-month consensus drift, which is what
    "fresh upgrade" means in this aggregate-distribution schema.

    Returns:
        dict with keys:
            shift: float (current_score - oldest_score)
            direction: 'bullish' | 'bearish' | 'flat'
            count_shift: int (current bullish-minus-bearish - oldest)
            current_score: float
            oldest_score: float
            total_analysts: int  (current period)
        OR None if the symbol has no recommendations data, fewer than
        2 periods, or yfinance fails.

    A `bullish` direction is set when EITHER:
      - score shift >= 0.05 (subtle but real consensus move), OR
      - count_shift >= 2 (at least 2 more analysts now bullish vs
        bearish than before — a real shift even if normalized score
        barely moves due to large analyst count).
    `bearish` is the symmetric case. Otherwise `flat`.
    """
    try:
        import yfinance as yf
        try:
            import yf_lock as _yfl
            with _yfl._lock:
                df = yf.Ticker(symbol).recommendations
        except Exception:
            df = yf.Ticker(symbol).recommendations
    except Exception as exc:
        logger.debug(
            "analyst recommendations fetch failed for %s: %s: %s",
            symbol, type(exc).__name__, exc,
        )
        return None

    if df is None or len(df) < 2:
        return None

    try:
        rows = [df.iloc[i] for i in range(len(df))]
        # Find current (period='0m') and oldest (largest -Nm).
        def _period_age(r):
            p = str(r.get("period", "")).strip()
            if p == "0m":
                return 0
            if p.startswith("-") and p.endswith("m"):
                try:
                    return int(p[1:-1])
                except ValueError:
                    return 999
            return 999

        rows_sorted = sorted(rows, key=_period_age)
        cur = rows_sorted[0]
        oldest = rows_sorted[-1]
        if _period_age(cur) == _period_age(oldest):
            return None
    except Exception:
        return None

    cur_score = _period_score(cur)
    oldest_score = _period_score(oldest)
    if cur_score is None or oldest_score is None:
        return None

    score_shift = cur_score - oldest_score

    # Count-based corroboration: bullish - bearish counts, then delta.
    def _bull_minus_bear(r):
        sb = int(r.get("strongBuy", 0) or 0)
        b = int(r.get("buy", 0) or 0)
        s = int(r.get("sell", 0) or 0)
        ss = int(r.get("strongSell", 0) or 0)
        return (sb + b) - (s + ss)

    cur_bmb = _bull_minus_bear(cur)
    oldest_bmb = _bull_minus_bear(oldest)
    count_shift = cur_bmb - oldest_bmb

    bullish = score_shift >= 0.05 or count_shift >= 2
    bearish = score_shift <= -0.05 or count_shift <= -2
    if bullish and not bearish:
        direction = "bullish"
    elif bearish and not bullish:
        direction = "bearish"
    else:
        direction = "flat"

    total = sum(int(cur.get(c, 0) or 0) for c in _RATING_WEIGHTS)
    return {
        "shift": round(score_shift, 3),
        "count_shift": count_shift,
        "direction": direction,
        "current_score": round(cur_score, 3),
        "oldest_score": round(oldest_score, 3),
        "total_analysts": total,
    }
