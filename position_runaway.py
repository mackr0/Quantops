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
from contextlib import closing
from typing import Dict, List

logger = logging.getLogger(__name__)


EXCESSIVE_QTY_MULT = 5.0
RECENT_TRADE_WINDOW = 50


def find_duplicate_open_buys(db_path: str) -> List[Dict[str, object]]:
    """List symbols with >1 open buy. Empty list if none / on error."""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, COUNT(*) AS n, SUM(qty) AS total_qty, "
                "MIN(timestamp) AS oldest, MAX(timestamp) AS newest "
                "FROM trades WHERE side='buy' AND status='open' "
                "GROUP BY UPPER(symbol) HAVING COUNT(*) > 1"
            ).fetchall()
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
    """Open trades whose qty is > mult × profile-recent median,
    computed PER INSTRUMENT CLASS.

    2026-05-21 — stock share-counts (100s-1000s) and option
    contract-counts (1-4) live in the same `qty` column. Pooling
    them into one median made the median ~1.0 for options-heavy
    profiles, so EVERY open stock position read as 100-1000× median
    and got flagged as a runaway. Now we compute a separate median
    for stock rows (occ_symbol NULL) vs option rows (occ_symbol set)
    and check each open position against its OWN class's median.
    Falls back to a single pooled median on minimal schemas without
    the occ_symbol column (older test fixtures).
    """
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            has_occ = bool(conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('trades') "
                "WHERE name = 'occ_symbol'"
            ).fetchone()[0])

            def _median(where_extra: str) -> float:
                recent = conn.execute(
                    f"SELECT qty FROM trades "
                    f"WHERE qty IS NOT NULL AND qty > 0{where_extra} "
                    f"ORDER BY id DESC LIMIT ?",
                    (window,),
                ).fetchall()
                if not recent or len(recent) < 5:
                    return 0.0  # insufficient history → no flagging
                qtys = sorted(float(r["qty"]) for r in recent)
                return qtys[len(qtys) // 2]

            if has_occ:
                stock_median = _median(
                    " AND (occ_symbol IS NULL OR occ_symbol = '')")
                option_median = _median(
                    " AND occ_symbol IS NOT NULL AND occ_symbol != ''")
                open_rows = conn.execute(
                    "SELECT id, symbol, qty, occ_symbol FROM trades "
                    "WHERE status='open' AND qty IS NOT NULL AND qty > 0 "
                    "ORDER BY qty DESC",
                ).fetchall()
            else:
                pooled = _median("")
                stock_median = option_median = pooled
                open_rows = conn.execute(
                    "SELECT id, symbol, qty, NULL AS occ_symbol FROM trades "
                    "WHERE status='open' AND qty IS NOT NULL AND qty > 0 "
                    "ORDER BY qty DESC",
                ).fetchall()
    except Exception as exc:
        logger.debug("find_excessive_qty_trades: %s", exc)
        return []

    flagged_out = []
    for r in open_rows:
        is_option = bool(r["occ_symbol"])
        mid = option_median if is_option else stock_median
        if mid <= 0:
            # Insufficient same-class history → can't judge; skip.
            continue
        if float(r["qty"]) > mid * mult:
            flagged_out.append({
                "trade_id": int(r["id"]), "symbol": r["symbol"],
                "qty": float(r["qty"]), "median": mid,
                "multiple": round(float(r["qty"]) / mid, 1),
            })
    return flagged_out


def runaway_snapshot(db_path: str) -> Dict[str, List]:
    return {
        "duplicate_buys": find_duplicate_open_buys(db_path),
        "excessive_qty": find_excessive_qty_trades(db_path),
    }
