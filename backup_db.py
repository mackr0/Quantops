"""Database backup — safe daily snapshot + rotation of all project SQLite DBs.

Uses SQLite's native `.backup` API (via `conn.backup(dest_conn)`) so backups
are WAL-safe even while the scheduler is actively writing. A plain `cp` of
a WAL-mode database can produce a corrupt copy; this does not.

Layout:
    /var/backups/quantopsai/<name>.<YYYY-MM-DD>.db
    e.g. /var/backups/quantopsai/quantopsai_profile_3.2026-04-14.db

The per-profile DBs hold all proprietary training data (every resolved
prediction + feature vector + outcome). Losing them means losing the
meta-model's training substrate — irreplaceable once overwritten.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List

logger = logging.getLogger(__name__)


DEFAULT_BACKUP_DIR = "/var/backups/quantopsai"
DEFAULT_RETAIN_DAYS = 14


# ---------------------------------------------------------------------------
# Single-DB backup
# ---------------------------------------------------------------------------

def backup_one(src_path: str, dest_path: str) -> bool:
    """Snapshot `src_path` → `dest_path` using SQLite's backup API.

    Returns True on success, False on any error. Never raises.
    """
    if not os.path.exists(src_path):
        logger.warning("backup: source missing %s", src_path)
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    # Write to a .tmp file first, then atomically rename — avoids leaving
    # a half-written file at the dest path if the process is interrupted.
    tmp_path = dest_path + ".tmp"
    try:
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        os.replace(tmp_path, dest_path)
        return True
    except Exception as exc:
        logger.warning("backup failed for %s: %s", src_path, exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Multi-DB backup + rotation
# ---------------------------------------------------------------------------

def backup_all(
    project_dir: str,
    backup_dir: str = DEFAULT_BACKUP_DIR,
    retain_days: int = DEFAULT_RETAIN_DAYS,
) -> Dict[str, int]:
    """Snapshot every *.db file in `project_dir`, then prune stale backups.

    Returns a summary dict:
        {"backed_up": int, "pruned": int, "failed": int, "files": [...]}
    """
    summary: Dict[str, int] = {
        "backed_up": 0, "pruned": 0, "failed": 0, "files": []
    }
    today = datetime.utcnow().strftime("%Y-%m-%d")

    try:
        files = [f for f in os.listdir(project_dir)
                 if f.endswith(".db") and not f.endswith("-shm")
                 and not f.endswith("-wal")]
    except OSError as exc:
        logger.warning("backup: cannot list %s — %s", project_dir, exc)
        return summary

    for fname in files:
        src = os.path.join(project_dir, fname)
        base = fname[:-3]  # strip .db
        dest = os.path.join(backup_dir, f"{base}.{today}.db")
        ok = backup_one(src, dest)
        if ok:
            summary["backed_up"] += 1
            summary["files"].append(dest)
        else:
            summary["failed"] += 1

    summary["pruned"] = prune_old_backups(backup_dir, retain_days)
    return summary


# ---------------------------------------------------------------------------
# Rotation — remove backups older than retain_days
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\.(\d{4}-\d{2}-\d{2})\.db$")


def prune_old_backups(backup_dir: str, retain_days: int) -> int:
    """Remove backups with a date stamp older than `retain_days`. Returns count."""
    if not os.path.isdir(backup_dir):
        return 0
    cutoff = datetime.utcnow().date() - timedelta(days=retain_days)
    removed = 0
    for fname in os.listdir(backup_dir):
        m = _DATE_RE.search(fname)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.remove(os.path.join(backup_dir, fname))
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# List backups — exposed for the dashboard
# ---------------------------------------------------------------------------

def list_backups(backup_dir: str = DEFAULT_BACKUP_DIR) -> List[Dict]:
    """Return metadata for each backup file, newest first."""
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for fname in os.listdir(backup_dir):
        m = _DATE_RE.search(fname)
        if not m:
            continue
        path = os.path.join(backup_dir, fname)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out.append({
            "name": fname,
            "date": m.group(1),
            "size_bytes": size,
            "size_mb": round(size / (1024 * 1024), 2),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out
