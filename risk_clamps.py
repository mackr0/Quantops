"""ATR-derived stop/TP percentage clamps.

Background (investigation 2026-06-09):

Both `stock_strategy_advisor.evaluate_candidate_for_stock_action`
and `trade_pipeline.execute_trade`'s ATR path compute stop/TP
distances as `(ATR × multiplier) / price`. ATR is in dollars; price
is in dollars. For high-priced stable stocks the ratio is small
(AMZN ~$246 with ~$6.91 ATR → 2.8%). For low-priced volatile
stocks the ratio explodes:

    RGNT $3.36, ATR $0.94 (28% of price) → TP = ATR × 3 / price = 84%
    NEXR $1.28, ATR $0.40 (31% of price) → SL = ATR × 2 / price = 63%

The 84%-TP price level is never reached by the underlying — market
moves don't scale linearly with ATR/price ratio for low-priced
small-caps. Result: 0 of 45 closed trades had MFE reach their TP
(2026-06-09 measurement on pid 42, 30-day window). The TPs were
aspirational; the stops were so wide they only fired on full
collapse rather than meaningful risk management.

Conversely, when ATR is mis-fed as ~0 (stale value or screener
race), the formula produces near-zero stops (RGNT entry $3.36 stop
$3.35 = 0.3% — fires on first tick of noise).

Both pathologies fixed by clamping to a sensible band:

  - TP min 4%: protects against ATR≈0 producing a TP that fires
    immediately on the first uptick.
  - TP max 12%: anchored to historical p75 MFE (6.9%). 12% is
    slightly above p75 so the cap doesn't chop the top quartile;
    it just kills the unreachable 80%+ targets.
  - SL min 3%: protects against ATR≈0 producing a meaningless
    sub-1% stop.
  - SL max 7%: a 7% stop is the wider end of acceptable risk on
    a single trade. Kills the 50%+ stops that defeat their own
    purpose. The 5% profile-default fallback sits inside this band.

These are decimal fractions (0.05 = 5%), matching the existing
fraction convention used by `actual_sl_pct` / `actual_tp_pct` in
`trade_pipeline.execute_trade`.
"""
from __future__ import annotations

ATR_TP_PCT_MIN = 0.04
ATR_TP_PCT_MAX = 0.12
ATR_SL_PCT_MIN = 0.03
ATR_SL_PCT_MAX = 0.07


def clamp_tp_pct(raw_pct: float) -> float:
    """Clamp a TP distance (fraction of price) to the sensible band.

    Args:
        raw_pct: raw ATR-derived TP distance as a fraction (0.15 = 15%).

    Returns:
        Clamped TP fraction in [ATR_TP_PCT_MIN, ATR_TP_PCT_MAX].
    """
    return max(ATR_TP_PCT_MIN, min(ATR_TP_PCT_MAX, float(raw_pct)))


def clamp_sl_pct(raw_pct: float) -> float:
    """Clamp an SL distance (fraction of price) to the sensible band."""
    return max(ATR_SL_PCT_MIN, min(ATR_SL_PCT_MAX, float(raw_pct)))
