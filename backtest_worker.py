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
                    changes_summary=None):
    """Start a backtest in a background thread. Returns job_id immediately."""
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
        }
        _write_jobs(jobs)

    def _run():
        try:
            from backtester import backtest_comparison

            def _update_progress(msg):
                _update_job(job_id, progress=msg)

            _update_progress("Downloading historical data...")
            result = backtest_comparison(market_type, current_params, proposed_params,
                                         days=days, progress_callback=_update_progress)
            _update_job(job_id, status="complete", result=result)
        except Exception as exc:
            logger.exception("Backtest job %s failed", job_id)
            _update_job(job_id, status="failed", error=str(exc))

        # Clean up old jobs
        _cleanup_old_jobs()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return job_id


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
