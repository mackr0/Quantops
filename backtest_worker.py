"""Background backtest worker -- runs backtests in separate threads.

Uses a JSON file for job storage so all gunicorn workers can access
the same job state (in-memory dicts are per-worker).
"""

import threading
import time
import logging
import uuid
import json
import os
from typing import Dict, Any

logger = logging.getLogger(__name__)

_JOBS_FILE = "/tmp/quantopsai_backtest_jobs.json"
_JOB_EXPIRY = 30 * 60  # 30 minutes
_lock = threading.Lock()


def _read_jobs() -> Dict[str, Any]:
    """Read jobs from shared file."""
    try:
        if os.path.exists(_JOBS_FILE):
            with open(_JOBS_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def _write_jobs(jobs: Dict[str, Any]):
    """Write jobs to shared file."""
    try:
        with open(_JOBS_FILE, "w") as f:
            json.dump(jobs, f)
    except IOError as exc:
        logger.warning("Failed to write jobs file: %s", exc)


def _update_job(job_id: str, **kwargs):
    """Update specific fields on a job (thread-safe)."""
    with _lock:
        jobs = _read_jobs()
        if job_id in jobs:
            jobs[job_id].update(kwargs)
            _write_jobs(jobs)


def start_backtest(market_type, current_params, proposed_params, days=90,
                    changes_summary=None, profile_id=None, user_id=None):
    """Start a backtest in a background thread. Returns job_id immediately.

    `profile_id` + `user_id` (2026-06-07) are persisted to
    `backtest_history` so the operator can look back across
    param-tuning cycles after the 30-min in-memory expiry. Both
    are optional to preserve back-compat with any caller that
    doesn't know its profile context.
    """
    job_id = str(uuid.uuid4())[:8]

    with _lock:
        jobs = _read_jobs()
        jobs[job_id] = {
            "status": "running",
            "started_at": time.time(),
            "result": None,
            "error": None,
            "progress": "Starting backtest...",
            "changes": changes_summary or [],
            "profile_id": profile_id,
            "user_id": user_id,
            "market_type": market_type,
        }
        _write_jobs(jobs)

    # Persist the started run immediately so an operator inspecting
    # /backtest-history mid-job sees "running"; the completion side
    # updates the row.
    _persist_history_row(
        job_id=job_id, profile_id=profile_id, user_id=user_id,
        market_type=market_type, status="running",
        current_params=current_params, proposed_params=proposed_params,
        changes=changes_summary or [],
    )

    def _run():
        try:
            from backtester import backtest_comparison

            def _update_progress(msg):
                _update_job(job_id, progress=msg)

            _update_progress("Downloading historical data...")
            result = backtest_comparison(market_type, current_params, proposed_params,
                                         days=days, progress_callback=_update_progress)
            _update_job(job_id, status="complete", result=result)
            _finalize_history_row(
                job_id=job_id, status="complete",
                result=result, error=None,
            )
        except Exception as exc:
            logger.exception("Backtest job %s failed", job_id)
            _update_job(job_id, status="failed", error=str(exc))
            _finalize_history_row(
                job_id=job_id, status="failed",
                result=None, error=str(exc),
            )

        # Clean up old jobs
        _cleanup_old_jobs()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return job_id


def _persist_history_row(
        job_id, profile_id, user_id, market_type, status,
        current_params, proposed_params, changes,
):
    """Insert a backtest_history row at job start. Best-effort —
    history persistence must never block the actual backtest run."""
    try:
        import sqlite3
        from contextlib import closing
        try:
            from config import DB_PATH
        except ImportError:
            DB_PATH = "quantopsai.db"
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO backtest_history "
                "(job_id, profile_id, user_id, market_type, status, "
                "current_params_json, proposed_params_json, changes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, profile_id, user_id, market_type, status,
                    json.dumps(current_params),
                    json.dumps(proposed_params),
                    json.dumps(changes),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "backtest_history insert failed (job=%s): %s: %s",
            job_id, type(exc).__name__, exc,
        )


def _finalize_history_row(job_id, status, result, error):
    """Update the backtest_history row when the job finishes. Best-
    effort — failure here doesn't block the in-memory job update."""
    try:
        import sqlite3
        from contextlib import closing
        try:
            from config import DB_PATH
        except ImportError:
            DB_PATH = "quantopsai.db"
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "UPDATE backtest_history "
                "SET status=?, completed_at=datetime('now'), "
                "    result_json=?, error=? "
                "WHERE job_id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    job_id,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "backtest_history finalize failed (job=%s): %s: %s",
            job_id, type(exc).__name__, exc,
        )


def get_job_status(job_id):
    """Get the status of a backtest job."""
    jobs = _read_jobs()
    if job_id not in jobs:
        return {"status": "not_found", "error": "Backtest job expired or not found."}

    job = jobs[job_id]
    elapsed = int(time.time() - job.get("started_at", time.time()))

    return {
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "progress": job.get("progress", ""),
        "elapsed_seconds": elapsed,
        "changes": job.get("changes", []),
    }


def _cleanup_old_jobs():
    """Remove jobs older than 30 minutes."""
    with _lock:
        jobs = _read_jobs()
        now = time.time()
        expired = [jid for jid, j in jobs.items()
                   if now - j.get("started_at", 0) > _JOB_EXPIRY]
        for jid in expired:
            del jobs[jid]
        if expired:
            _write_jobs(jobs)
