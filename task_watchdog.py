"""Run-completion watchdog — detect stalled tasks and alert.

Every scheduled task that could matter for correctness (scan, ensemble,
exit check, etc.) is wrapped by `track_run()` which records a row in
`task_runs` with `started_at` on entry and `completed_at` on exit. A
separate watchdog task (`check_stalled_runs`) scans for rows older
than the configured timeout with `completed_at IS NULL` and:

  1. Marks them `status='stalled'` so they're not re-alerted every minute
  2. Logs a WARNING with the task name and elapsed time
  3. Emits a `task_stalled` event on the Phase 9 event bus
  4. Sends an email alert via the existing notifications module

Default stall threshold is 30 minutes. Tune per-task if needed.

The watchdog is idempotent — repeat runs of `check_stalled_runs` will
not re-alert on the same row (the status transition gates it).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default: a task that's been running more than this long is presumed stuck.
# Tune upward for known long-running tasks (backtests, retraining).
DEFAULT_STALL_MINUTES = 30


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

@contextmanager
def track_run(db_path: str, task_name: str):
    """Context manager that records start/end of a task run.

    Usage:
        with track_run(db_path, "scan_and_trade:midcap"):
            ... do the work ...

    On normal exit: row is marked `status='completed'` with duration.
    On exception: row is marked `status='failed'` with error message.
    Never masks the caller's exception — just records it and re-raises.
    """
    run_id = _insert_start(db_path, task_name)
    import time
    t0 = time.time()
    try:
        yield
        duration = time.time() - t0
        _mark_completed(db_path, run_id, duration)
    except Exception as exc:
        duration = time.time() - t0
        _mark_failed(db_path, run_id, duration, str(exc)[:500])
        raise


def _insert_start(db_path: str, task_name: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO task_runs (task_name) VALUES (?)",
            (task_name,),
        )
        conn.commit()
        rid = int(cur.lastrowid)
        conn.close()
        return rid
    except Exception as exc:
        logger.debug("track_run start failed: %s", exc)
        return None


def _mark_completed(db_path: str, run_id: Optional[int],
                    duration: float) -> None:
    if run_id is None:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE task_runs SET completed_at = datetime('now'), "
            "duration_seconds = ?, status = 'completed' WHERE id = ?",
            (round(duration, 2), run_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("track_run complete failed: %s", exc)


def _mark_failed(db_path: str, run_id: Optional[int],
                 duration: float, error: str) -> None:
    if run_id is None:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE task_runs SET completed_at = datetime('now'), "
            "duration_seconds = ?, status = 'failed', "
            "error_message = ? WHERE id = ?",
            (round(duration, 2), error, run_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("track_run fail mark failed: %s", exc)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def check_stalled_runs(db_path: str,
                       stall_minutes: int = DEFAULT_STALL_MINUTES
                       ) -> List[Dict[str, Any]]:
    """Find task runs that started but never completed within threshold.

    Marks each stalled run with status='stalled' so we don't re-alert
    on the next watchdog pass. Returns the list of stalled rows so
    the caller can emit alerts, events, and emails.
    """
    stalled: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, task_name, started_at,
                      (julianday('now') - julianday(started_at)) * 24 * 60
                      AS minutes_elapsed
               FROM task_runs
               WHERE completed_at IS NULL
                 AND status = 'running'
                 AND started_at <= datetime('now', ?)""",
            (f"-{int(stall_minutes)} minutes",),
        ).fetchall()

        for row in rows:
            stalled.append(dict(row))
            conn.execute(
                "UPDATE task_runs SET status = 'stalled' WHERE id = ?",
                (row["id"],),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("watchdog scan failed: %s", exc)

    return stalled


# ---------------------------------------------------------------------------
# Read-side helpers for the dashboard
# ---------------------------------------------------------------------------

def recent_runs(db_path: str, hours: int = 24,
                limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent task runs for dashboard display."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM task_runs "
            "WHERE started_at >= datetime('now', ?) "
            "ORDER BY started_at DESC LIMIT ?",
            (f"-{int(hours)} hours", limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def summary(db_path: str, hours: int = 24) -> Dict[str, int]:
    """Counts by status over the last N hours."""
    counts = {"completed": 0, "failed": 0, "stalled": 0, "running": 0}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM task_runs "
            "WHERE started_at >= datetime('now', ?) GROUP BY status",
            (f"-{int(hours)} hours",),
        ).fetchall()
        conn.close()
        for status, n in rows:
            counts[status] = int(n)
    except Exception:
        pass
    return counts
