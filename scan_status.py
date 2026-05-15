"""Lightweight scan-step status for dashboard display.

Writes a small JSON file per profile that the dashboard polls
to show what step the scanner is on instead of just "Scanning..."
"""

import json
import time
import logging

logger = logging.getLogger(__name__)


def update_status(profile_id, step, detail=""):
    """Write the current scan step to a status file."""
    try:
        path = "scan_status_%d.json" % profile_id
        with open(path, "w") as f:
            json.dump({
                "step": step,
                "detail": detail,
                "timestamp": time.time(),
            }, f)
    except (OSError, TypeError, ValueError) as _sw_exc:
        # Scan-status file write; status display is informational,
        # never blocks scan. Surface for follow-up.
        logger.debug(
            "scan_status file write failed: %s: %s",
            type(_sw_exc).__name__, _sw_exc,
        )


def get_status(profile_id):
    """Read the current scan status. Returns None if not scanning."""
    try:
        path = "scan_status_%d.json" % profile_id
        with open(path) as f:
            data = json.load(f)
        # Stale after 5 minutes = not actively scanning
        if time.time() - data.get("timestamp", 0) > 300:
            return None
        return data
    except Exception:
        return None


def clear_status(profile_id):
    """Clear status after scan completes."""
    try:
        import os
        os.remove("scan_status_%d.json" % profile_id)
    except (OSError, FileNotFoundError) as _sr_exc:
        # Scan-status file removal; missing file is the desired
        # post-condition anyway. Surface for follow-up.
        logger.debug(
            "scan_status file removal failed: %s: %s",
            type(_sr_exc).__name__, _sr_exc,
        )
