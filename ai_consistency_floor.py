"""AI consistency floor.

Different signal than capital loss: if the AI's win rate on
recently-resolved predictions drops below floor (default 30%) for N
consecutive checks, flag for review. Captures "model is broken"
before the daily-loss floor catches "book is bleeding."

Consecutive-check logic prevents a single bad-day blip from tripping.
A persistent regression (5+ checks in a row below floor) signals
something has broken in the model layer (regime shift, bad meta-model,
broken signal pipeline).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# In-memory state per profile — counts consecutive sub-floor checks
_consecutive_breach: Dict[str, int] = {}


def recent_win_rate(db_path: str, window: int = 100) -> Optional[Dict[str, object]]:
    """Win rate over the most recent `window` resolved predictions
    of types BUY / STRONG_SELL / SHORT (directional only — HOLDs
    have their own metric and don't reflect AI accuracy on actions).

    Returns {win_rate_pct, n_resolved, n_wins, n_losses} or None
    when insufficient history."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT actual_outcome FROM ai_predictions "
            "WHERE status='resolved' "
            "AND UPPER(predicted_signal) IN "
            "('BUY','STRONG_SELL','SHORT','SELL') "
            "AND actual_outcome IN ('win', 'loss') "
            "ORDER BY id DESC LIMIT ?",
            (window,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.debug("recent_win_rate: %s", exc)
        return None
    if not rows or len(rows) < 10:
        return None
    n = len(rows)
    n_wins = sum(1 for r in rows if r["actual_outcome"] == "win")
    n_losses = n - n_wins
    return {
        "win_rate_pct": round(100.0 * n_wins / n, 1),
        "n_resolved": n,
        "n_wins": n_wins,
        "n_losses": n_losses,
    }


def check_floor(
    db_path: str, profile_label: str,
    floor_pct: float = 30.0,
    consecutive_required: int = 5,
) -> Dict[str, object]:
    """Run one check. Returns:
      {win_rate_pct, n_resolved, breached, consecutive, alert_now}
    `alert_now` is True only on the SAME cycle the threshold is met
    (so callers don't repeat-spam alerts every cycle while the model
    is recovering). Reset on first non-breach check."""
    info = recent_win_rate(db_path)
    if info is None:
        return {
            "win_rate_pct": None, "n_resolved": 0,
            "breached": False, "consecutive": 0,
            "alert_now": False,
        }
    breached = info["win_rate_pct"] < floor_pct
    if breached:
        prev = _consecutive_breach.get(profile_label, 0)
        _consecutive_breach[profile_label] = prev + 1
        consec = _consecutive_breach[profile_label]
        alert_now = (consec == consecutive_required)
    else:
        _consecutive_breach[profile_label] = 0
        consec = 0
        alert_now = False
    return {
        "win_rate_pct": info["win_rate_pct"],
        "n_resolved": info["n_resolved"],
        "n_wins": info["n_wins"],
        "n_losses": info["n_losses"],
        "breached": breached,
        "consecutive": consec,
        "alert_now": alert_now,
        "floor_pct": floor_pct,
    }


def reset_state() -> None:
    """Test helper."""
    _consecutive_breach.clear()
