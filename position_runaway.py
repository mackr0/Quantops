"""Position-runaway sentinel.

Catches two failure modes that the daily reconciliation runs miss
intra-day:

1. **Duplicate-submit**: more than one OPEN buy trade for the same
   (profile, symbol).
2. **Excessive single-trade qty**: a fill whose qty is >5× the
   profile's recent median qty. Catches qty-arithmetic bugs where
   the dollar size check passes but the qty is absurd.

Both are alerts, not blocks (already-filled).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List

logger = logging.getLogger(__name__)


EXCESSIVE_QTY_MULT = 5.0
RECENT_TRADE_WINDOW = 50


def find_duplicate_open_buys(db_path: str) -> List[Dict[str, object]]:
    """List symbols with >1 open buy. Empty list if none / on error."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS n, SUM(qty) AS total_qty, "
            "MIN(timestamp) AS oldest, MAX(timestamp) AS newest "
            "FROM trades WHERE side='buy' AND status='open' "
            "GROUP BY UPPER(symbol) HAVING COUNT(*) > 1"
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.debug("find_duplicate_open_buys: %s", exc)
        return []
    return [
        {
            "symbol": r["symbol"], "count": int(r["n"]),
            "total_qty": float(r["total_qty"] or 0),
            "oldest_ts": r["oldest"], "newest_ts": r["newest"],
        }
        for r in rows
    ]


def find_excessive_qty_trades(
    db_path: str, mult: float = EXCESSIVE_QTY_MULT,
    window: int = RECENT_TRADE_WINDOW,
) -> List[Dict[str, object]]:
    """Open trades whose qty is > mult × profile-recent median."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        recent = conn.execute(
            "SELECT qty FROM trades WHERE qty IS NOT NULL AND qty > 0 "
            "ORDER BY id DESC LIMIT ?",
            (window,),
        ).fetchall()
        if not recent or len(recent) < 5:
            conn.close()
            return []
        qtys = sorted(float(r["qty"]) for r in recent)
        mid = qtys[len(qtys) // 2]
        threshold = mid * mult
        flagged = conn.execute(
            "SELECT id, symbol, qty FROM trades "
            "WHERE status='open' AND qty > ? "
            "ORDER BY qty DESC",
            (threshold,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.debug("find_excessive_qty_trades: %s", exc)
        return []
    return [
        {
            "trade_id": int(r["id"]), "symbol": r["symbol"],
            "qty": float(r["qty"]), "median": mid,
            "multiple": (
                round(float(r["qty"]) / mid, 1)
                if mid > 0 else float("inf")
            ),
        }
        for r in flagged
    ]


def runaway_snapshot(db_path: str) -> Dict[str, List]:
    return {
        "duplicate_buys": find_duplicate_open_buys(db_path),
        "excessive_qty": find_excessive_qty_trades(db_path),
    }
