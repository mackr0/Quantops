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
    prior period and return a directional shift summary.

    Returns:
        dict with keys:
            shift: float  (current_score - prior_score, range ~[-4, 4])
            direction: 'bullish' | 'bearish' | 'flat'
            current_score: float
            prior_score: float
            total_analysts: int  (current period)
        OR None if the symbol has no recommendations data, fewer than 2
        periods of history, or yfinance fails.

    A `bullish` shift (>=+0.10) means the average rating moved at
    least 10% of one rating-grade more positive vs the prior period —
    materially stronger consensus than noise. `bearish` is the
    symmetric case. Otherwise `flat`.
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

    # yfinance returns periods ordered most-recent first (period='0m'
    # at row 0). Some versions return oldest first — handle both by
    # checking column types.
    try:
        rows = [df.iloc[i] for i in range(len(df))]
        # Detect order by which row has period='0m' (most current).
        cur_idx = 0
        for i, r in enumerate(rows):
            if str(r.get("period", "")).strip() == "0m":
                cur_idx = i
                break
        prior_idx = cur_idx + 1 if cur_idx + 1 < len(rows) else cur_idx - 1
        if prior_idx < 0:
            return None
        cur = rows[cur_idx]
        prior = rows[prior_idx]
    except Exception:
        return None

    cur_score = _period_score(cur)
    prior_score = _period_score(prior)
    if cur_score is None or prior_score is None:
        return None

    shift = cur_score - prior_score
    if shift >= 0.10:
        direction = "bullish"
    elif shift <= -0.10:
        direction = "bearish"
    else:
        direction = "flat"

    total = sum(int(cur.get(c, 0) or 0) for c in _RATING_WEIGHTS)
    return {
        "shift": round(shift, 3),
        "direction": direction,
        "current_score": round(cur_score, 3),
        "prior_score": round(prior_score, 3),
        "total_analysts": total,
    }
