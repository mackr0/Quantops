"""Per-profile catastrophic single-trade gate.

The per-profile `max_position_pct` (default ~5% of equity) catches
absurd dollar SIZES. But if the input price is wrong (split day,
stale quote) the dollar check can pass while the QTY is absurd —
e.g. a $5 stock priced as $0.50 would let qty 10x what it should
be at the same dollar amount. This is the safety net for that
class of bug.

Compares a proposed trade's $ value against the profile's recent
average position size. If >5× the average, reject.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


CATASTROPHIC_MULT = 5.0
RECENT_WINDOW = 50


def recent_avg_position_value(
    db_path: str, window: int = RECENT_WINDOW,
) -> Optional[float]:
    """Average $ value of the profile's last `window` STOCK trades.
    Returns None when insufficient history (we can't gate against
    zero base).

    2026-05-15 — exclude option-leg trades AND data_quality-tagged
    rows from the baseline. The guard's job is to catch a stock
    trade that's wildly oversized vs typical STOCK trades. Including
    option legs (per-leg premiums of $1-$3) drags the average down
    to options-premium dollars, which makes any normal $5-10k stock
    BUY look "5× recent average" and triggers a false rejection.
    Observed 2026-05-15: pid 11 BUYs (JPM, NOC, DHR) all blocked
    because the baseline was poisoned by SBUX/LRCX/ANET/WMT
    multileg legs.
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT qty, price FROM trades "
                "WHERE qty IS NOT NULL AND price IS NOT NULL "
                "AND qty > 0 AND price > 0 "
                "AND occ_symbol IS NULL "
                "AND COALESCE(data_quality, '') != 'polluted' "
                "AND COALESCE(signal_type, '') NOT IN "
                "    ('MULTILEG', 'OPTIONS', 'reconcile_xprof') "
                "ORDER BY id DESC LIMIT ?",
                (window,),
            ).fetchall()
    except Exception as exc:
        logger.debug("recent_avg_position_value: %s", exc)
        return None
    if not rows or len(rows) < 5:
        return None
    values = [float(r["qty"]) * float(r["price"]) for r in rows]
    return sum(values) / len(values)


def is_catastrophic(
    proposed_value: float, db_path: str,
    mult: float = CATASTROPHIC_MULT,
    max_position_dollars: Optional[float] = None,
) -> Tuple[bool, str, dict]:
    """Return (is_catastrophic, reason, detail).

    `max_position_dollars` (2026-06-09): operator-configured
    per-trade ceiling. The threshold is floored at this value so the
    gate never tightens BELOW what the position cap already allows.
    Without this floor, a young profile with a few small trades
    establishes a tiny `recent_avg`, the 5× threshold sits below
    `max_position_pct × equity`, and every within-position-cap trade
    gets blocked — a death spiral where the cap can never grow
    because the cap suppresses larger trades.

    With the floor: `threshold = max(5 × recent_avg, max_position_$)`.
    - Young profile (small avg): threshold = max_position_$ →
      catastrophic gate ≡ position cap. No death spiral.
    - Mature profile (large avg): threshold = 5 × avg → catches the
      genuine 5× anomalies the gate exists for (qty bugs, AI
      hallucination, etc.).

    detail = {avg_recent_value, threshold, multiple, sample_size,
              floor_applied}
    """
    avg = recent_avg_position_value(db_path)
    if avg is None:
        return False, "no baseline", {
            "avg_recent_value": None, "threshold": None,
            "multiple": None, "sample_size": 0,
            "floor_applied": False,
        }
    raw_threshold = avg * mult
    floor = max_position_dollars or 0
    threshold = max(raw_threshold, floor)
    floor_applied = bool(floor and floor > raw_threshold)
    multiple = proposed_value / avg if avg > 0 else float("inf")
    detail = {
        "avg_recent_value": round(avg, 2),
        "threshold": round(threshold, 2),
        "multiple": round(multiple, 1),
        "sample_size": RECENT_WINDOW,
        "floor_applied": floor_applied,
    }
    if proposed_value > threshold:
        if floor_applied:
            reason = (
                f"Catastrophic single-trade: ${proposed_value:,.0f} "
                f"is above the floor ${threshold:,.0f} "
                f"(max_position_pct × equity; recent avg ${avg:,.0f} "
                f"× {mult}× = ${raw_threshold:,.0f} would have allowed "
                f"more)"
            )
        else:
            reason = (
                f"Catastrophic single-trade: ${proposed_value:,.0f} "
                f"is {multiple:.1f}× recent avg ${avg:,.0f} "
                f"(cap {mult}×)"
            )
        return True, reason, detail
    return False, "within mult", detail
