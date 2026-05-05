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
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


CATASTROPHIC_MULT = 5.0
RECENT_WINDOW = 50


def recent_avg_position_value(
    db_path: str, window: int = RECENT_WINDOW,
) -> Optional[float]:
    """Average $ value of the profile's last `window` trades. Returns
    None when insufficient history (we can't gate against zero base)."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT qty, price FROM trades "
            "WHERE qty IS NOT NULL AND price IS NOT NULL "
            "AND qty > 0 AND price > 0 "
            "ORDER BY id DESC LIMIT ?",
            (window,),
        ).fetchall()
        conn.close()
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
) -> Tuple[bool, str, dict]:
    """Return (is_catastrophic, reason, detail).

    detail = {avg_recent_value, threshold, multiple, sample_size}
    """
    avg = recent_avg_position_value(db_path)
    if avg is None:
        return False, "no baseline", {
            "avg_recent_value": None, "threshold": None,
            "multiple": None, "sample_size": 0,
        }
    threshold = avg * mult
    multiple = proposed_value / avg if avg > 0 else float("inf")
    detail = {
        "avg_recent_value": round(avg, 2),
        "threshold": round(threshold, 2),
        "multiple": round(multiple, 1),
        "sample_size": RECENT_WINDOW,
    }
    if proposed_value > threshold:
        reason = (
            f"Catastrophic single-trade: ${proposed_value:,.0f} is "
            f"{multiple:.1f}× recent avg ${avg:,.0f} (cap {mult}×)"
        )
        return True, reason, detail
    return False, "within mult", detail
