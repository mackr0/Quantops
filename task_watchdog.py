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

2026-05-15 — orphan-restart elimination. The single largest source
of false-positive "stalled task" alerts was scheduler restart: a
deploy mid-cycle kills the process while task_runs rows are still
`status='running'`. The new process started fresh; the next watchdog
pass would mis-diagnose those zombie rows as "API hang." Fix:
`mark_orphaned_at_startup()` is called by the scheduler on boot
and bulk-marks every still-`running` row as `status='orphaned_restart'`
with a deterministic note. `check_stalled_runs` only considers
`status='running'` rows, so orphaned rows never reach the false-
positive pipeline. For Scan & Trade tasks specifically, the
caller can re-fire the cycle immediately (make-up scan) so the
data the killed cycle would have produced is recovered.

`diagnose_stalled_run()` replaces the old hard-coded if/elif
guesswork with evidence from `ai_cost_ledger`, `activity_log`, and
`ai_predictions` — when we DO surface a true stall, the diagnosis
points at the actual stuck step, not a fabricated "Alpaca slow."
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing, contextmanager
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
        with track_run(db_path, "scan_and_trade:stocks"):
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
        with closing(sqlite3.connect(db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO task_runs (task_name) VALUES (?)",
                (task_name,),
            )
            conn.commit()
            rid = int(cur.lastrowid)
        return rid
    except Exception as exc:
        logger.debug("track_run start failed: %s", exc)
        return None


def _mark_completed(db_path: str, run_id: Optional[int],
                    duration: float) -> None:
    if run_id is None:
        return
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE task_runs SET completed_at = datetime('now'), "
                "duration_seconds = ?, status = 'completed' WHERE id = ?",
                (round(duration, 2), run_id),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("track_run complete failed: %s", exc)


def _mark_failed(db_path: str, run_id: Optional[int],
                 duration: float, error: str) -> None:
    if run_id is None:
        return
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE task_runs SET completed_at = datetime('now'), "
                "duration_seconds = ?, status = 'failed', "
                "error_message = ? WHERE id = ?",
                (round(duration, 2), error, run_id),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("track_run fail mark failed: %s", exc)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def mark_orphaned_at_startup(db_path: str) -> List[Dict[str, Any]]:
    """Bulk-mark every `status='running'` row as `status='orphaned_restart'`.

    Called by the scheduler at boot. After a clean systemd restart the
    new process starts fresh — any task_runs row still labeled `running`
    in the DB is by definition a zombie (its parent process is gone).
    Marking them upfront prevents `check_stalled_runs` from later
    reporting them as "stalled, likely API hang" — which was the
    single largest source of false-positive stall alerts and was
    responsible for the misleading "Alpaca slow" diagnoses operators
    were seeing.

    Returns the list of orphaned rows so the caller can decide on
    remediation (e.g. immediately re-fire a make-up Scan & Trade
    cycle for orphaned scan tasks).
    """
    orphaned: List[Dict[str, Any]] = []
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, task_name, started_at,
                          (julianday('now') - julianday(started_at)) * 24 * 60
                          AS minutes_elapsed
                   FROM task_runs
                   WHERE completed_at IS NULL
                     AND status = 'running'""",
            ).fetchall()
            for row in rows:
                orphaned.append(dict(row))
                conn.execute(
                    "UPDATE task_runs SET status = 'orphaned_restart', "
                    "completed_at = datetime('now'), "
                    "error_message = ? WHERE id = ?",
                    (
                        "Killed by scheduler restart — task was "
                        "in-flight when the parent process exited.",
                        row["id"],
                    ),
                )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.warning(
            "mark_orphaned_at_startup failed for %s: %s: %s",
            db_path, type(exc).__name__, exc,
        )
    return orphaned


def diagnose_stalled_run(db_path: str, task_name: str,
                          started_at: str,
                          minutes_elapsed: float) -> str:
    """Real evidence-based diagnosis of a stalled task.

    Replaces the previous hard-coded if/elif text that guessed at
    causes. Reads three signals from the profile DB:

      - `ai_cost_ledger` last entry: if AI was responding within the
        stall window, the AI is not the stuck step.
      - `activity_log` last entry: shows what the task was last
        observed doing before it went silent.
      - `ai_predictions` last entry: shows whether prediction
        recording happened (one of the last steps in a Scan & Trade
        cycle).

    Returns a single-line string describing what the evidence shows.
    NEVER fabricates a culprit — if no evidence is available, says
    "no recent activity in any subsystem; cause indeterminate."
    """
    findings: List[str] = []
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT timestamp, purpose, model FROM ai_cost_ledger "
                    "WHERE timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (started_at,),
                ).fetchone()
                if row:
                    findings.append(
                        f"AI was responding ({row['purpose']} call at "
                        f"{row['timestamp']} on {row['model']})"
                    )
                else:
                    findings.append(
                        "no AI calls completed since task started"
                    )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass

            try:
                row = conn.execute(
                    "SELECT timestamp, title FROM activity_log "
                    "WHERE timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (started_at,),
                ).fetchone()
                if row:
                    findings.append(
                        f"last activity_log: '{row['title']}' at "
                        f"{row['timestamp']}"
                    )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass

            try:
                row = conn.execute(
                    "SELECT timestamp, symbol FROM ai_predictions "
                    "WHERE timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (started_at,),
                ).fetchone()
                if row:
                    findings.append(
                        f"last prediction recorded for {row['symbol']} "
                        f"at {row['timestamp']}"
                    )
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.debug(
            "diagnose_stalled_run signal lookup failed: %s: %s",
            type(exc).__name__, exc,
        )

    if not findings:
        return (
            f"Task running {minutes_elapsed:.0f} min with no recent "
            f"activity in ai_cost_ledger, activity_log, or "
            f"ai_predictions; cause indeterminate."
        )

    return (
        f"Task running {minutes_elapsed:.0f} min. Evidence: "
        + "; ".join(findings) + "."
    )


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
        with closing(sqlite3.connect(db_path)) as conn:
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
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM task_runs "
                "WHERE started_at >= datetime('now', ?) "
                "ORDER BY started_at DESC LIMIT ?",
                (f"-{int(hours)} hours", limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def summary(db_path: str, hours: int = 24) -> Dict[str, int]:
    """Counts by status over the last N hours."""
    counts = {"completed": 0, "failed": 0, "stalled": 0, "running": 0}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM task_runs "
                "WHERE started_at >= datetime('now', ?) GROUP BY status",
                (f"-{int(hours)} hours",),
            ).fetchall()
        for status, n in rows:
            counts[status] = int(n)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _ts_exc:
        # Task-status counts read; counts dict stays empty (caller
        # treats as no data). Surface for follow-up.
        logger.debug(
            "task_watchdog status counts read failed: %s: %s",
            type(_ts_exc).__name__, _ts_exc,
        )
    return counts
