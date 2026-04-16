"""Crisis-state persistence and transition orchestration (Phase 10).

Separates the "detect state" logic (`crisis_detector.py`) from the
"record and react to transitions" logic. This module is responsible for
writing state transitions to `crisis_state_history`, emitting
`crisis_state_change` events on the event bus, and serving the current
state to the trade pipeline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Optional

from crisis_detector import (
    LEVELS,
    LEVEL_RANK,
    NORMAL,
    SIZE_MULTIPLIERS,
    detect_crisis_state,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def get_current_level(db_path: str) -> Dict[str, Any]:
    """Return the most recent crisis state as stored in history.

    Defaults to NORMAL when no rows exist yet.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM crisis_state_history "
            "ORDER BY transitioned_at DESC, id DESC LIMIT 1",
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {
            "level": NORMAL,
            "size_multiplier": 1.0,
            "transitioned_at": None,
            "signals": [],
            "readings": {},
        }
    d = dict(row)
    try:
        d["signals"] = json.loads(d.get("signals_json") or "[]")
    except Exception:
        d["signals"] = []
    try:
        d["readings"] = json.loads(d.get("readings_json") or "{}")
    except Exception:
        d["readings"] = {}
    d["level"] = d.get("to_level", NORMAL)
    return d


def history(db_path: str, limit: int = 20) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM crisis_state_history "
            "ORDER BY transitioned_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["signals"] = json.loads(d.get("signals_json") or "[]")
        except Exception:
            d["signals"] = []
        try:
            d["readings"] = json.loads(d.get("readings_json") or "{}")
        except Exception:
            d["readings"] = {}
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Tick — run detection, persist transitions, emit events
# ---------------------------------------------------------------------------

def run_crisis_tick(db_path: str) -> Dict[str, Any]:
    """One crisis monitoring pass.

    Runs detection, compares to stored state, writes a new history row
    only if the level changed, and emits a `crisis_state_change` event
    on any transition.

    Returns a dict with keys: `level`, `prior_level`, `changed`.
    """
    detection = detect_crisis_state(db_path=db_path)
    new_level = detection["level"]

    current = get_current_level(db_path)
    prior_level = current["level"]
    changed = new_level != prior_level

    if not changed:
        return {
            "level": new_level,
            "prior_level": prior_level,
            "changed": False,
            "signals": detection["signals"],
            "readings": detection["readings"],
            "size_multiplier": detection["size_multiplier"],
        }

    # Persist the transition
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO crisis_state_history
                 (from_level, to_level, signals_json, readings_json,
                  size_multiplier)
               VALUES (?, ?, ?, ?, ?)""",
            (
                prior_level,
                new_level,
                json.dumps(detection["signals"]),
                json.dumps(detection["readings"]),
                detection["size_multiplier"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Emit bus event — the dashboard will pick it up, and handlers that
    # subscribe to crisis_state_change can react (notifications, etc.).
    try:
        from event_bus import emit
        severity = _severity_for_level(new_level, prior_level)
        # Dedup key includes the transition pair so each distinct
        # upgrade/downgrade within a single day emits once (not just
        # the first transition of the day).
        import datetime as _dt
        today = _dt.datetime.utcnow().strftime("%Y%m%d")
        dedup = f"crisis_state_change:{prior_level}->{new_level}:{today}"
        emit(
            db_path, "crisis_state_change",
            symbol=None,
            severity=severity,
            payload={
                "from": prior_level,
                "to": new_level,
                "size_multiplier": detection["size_multiplier"],
                "signals": detection["signals"],
            },
            dedup_key=dedup,
        )
    except Exception as exc:
        logger.warning("failed to emit crisis_state_change: %s", exc)

    logger.warning(
        "Crisis state transition: %s → %s (size_multiplier=%.2f, signals=%d)",
        prior_level, new_level,
        detection["size_multiplier"], len(detection["signals"]),
    )

    return {
        "level": new_level,
        "prior_level": prior_level,
        "changed": True,
        "signals": detection["signals"],
        "readings": detection["readings"],
        "size_multiplier": detection["size_multiplier"],
    }


def _severity_for_level(new_level: str, prior_level: str) -> str:
    """Map a transition to event-bus severity."""
    if LEVEL_RANK[new_level] > LEVEL_RANK[prior_level]:
        # Upgrade — more severe than downgrade
        return {"severe": "critical", "crisis": "high",
                "elevated": "medium", "normal": "info"}[new_level]
    else:
        # Downgrade (recovery)
        return "info"
