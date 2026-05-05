"""Stop-order coverage monitor.

Every open long should have a broker-side protective stop
(`protective_stop_order_id` or `protective_trailing_order_id`).
When coverage drops below the floor (default 80%), surface it.
Silent stop-coverage rot was a known failure mode without this.
"""
from __future__ import annotations

import glob
import logging
import os
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _profile_db_paths(repo_root: Optional[str] = None) -> List[str]:
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    return sorted(glob.glob(os.path.join(repo_root, "quantopsai_profile_*.db")))


def coverage_snapshot(db_paths: Optional[List[str]] = None) -> Dict[str, object]:
    """Return current stop-order coverage across the book.

    Counts only LONG positions (side='buy', status='open'). Returns
    {total_longs, covered, naked, coverage_pct, naked_symbols}."""
    paths = db_paths if db_paths is not None else _profile_db_paths()
    total = 0
    covered = 0
    naked: List = []
    for path in paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, protective_stop_order_id, "
                "protective_trailing_order_id "
                "FROM trades WHERE side='buy' AND status='open'"
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("coverage_snapshot: %s skipped (%s)", path, exc)
            continue
        try:
            pid = os.path.basename(path).split("_")[-1].split(".")[0]
        except Exception:
            pid = "?"
        for r in rows:
            total += 1
            has_stop = bool(
                (r["protective_stop_order_id"] or "").strip()
                or (r["protective_trailing_order_id"] or "").strip()
            )
            if has_stop:
                covered += 1
            else:
                naked.append((pid, r["symbol"]))
    pct = 100.0 if total == 0 else (covered / total) * 100.0
    return {
        "total_longs": total,
        "covered": covered,
        "naked": total - covered,
        "coverage_pct": round(pct, 1),
        "naked_symbols": naked,
    }


def check_coverage_floor(
    floor_pct: float = 80.0, db_paths: Optional[List[str]] = None,
) -> Dict[str, object]:
    snap = coverage_snapshot(db_paths=db_paths)
    snap["floor_pct"] = floor_pct
    snap["breached"] = (
        snap["total_longs"] > 0 and snap["coverage_pct"] < floor_pct
    )
    return snap
