"""Background backtest worker -- runs backtests in separate threads."""

import threading
import time
import logging
import uuid
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# In-memory job store (jobs expire after 30 minutes)
_jobs: Dict[str, Dict[str, Any]] = {}
_JOB_EXPIRY = 30 * 60  # 30 minutes


def start_backtest(market_type, current_params, proposed_params, days=90):
    """Start a backtest in a background thread. Returns job_id immediately."""
    job_id = str(uuid.uuid4())[:8]

    _jobs[job_id] = {
        "status": "running",
        "started_at": time.time(),
        "result": None,
        "error": None,
        "progress": "Starting backtest...",
    }

    def _run():
        try:
            from backtester import backtest_comparison

            def _update_progress(msg):
                _jobs[job_id]["progress"] = msg

            _update_progress("Downloading historical data...")
            result = backtest_comparison(market_type, current_params, proposed_params,
                                         days=days, progress_callback=_update_progress)
            _jobs[job_id]["status"] = "complete"
            _jobs[job_id]["result"] = result
        except Exception as exc:
            logger.exception("Backtest job %s failed", job_id)
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)

        # Clean up old jobs
        _cleanup_old_jobs()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return job_id


def get_job_status(job_id):
    """Get the status of a backtest job. Returns dict with status, result, error."""
    if job_id not in _jobs:
        return {"status": "not_found", "error": "Job not found or expired"}

    job = _jobs[job_id]
    elapsed = int(time.time() - job["started_at"])

    return {
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
        "progress": job.get("progress", ""),
        "elapsed_seconds": elapsed,
    }


def _cleanup_old_jobs():
    """Remove jobs older than 30 minutes."""
    now = time.time()
    expired = [jid for jid, j in _jobs.items() if now - j["started_at"] > _JOB_EXPIRY]
    for jid in expired:
        del _jobs[jid]
