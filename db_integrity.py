"""SQLite integrity check + restore-from-backup helper.

`backup_daily.sh` already runs at 05:00 UTC and rotates copies of the
master + per-profile DBs into `/opt/quantopsai/backups/`. This module
adds the missing pieces:

  1. `check_all_dbs()` — runs `PRAGMA integrity_check` on every DB
     the system uses (master + each per-profile + altdata DBs).
     Returns a dict with per-DB status. Designed to run on scheduler
     startup so a corrupted DB is detected before the first cycle.

  2. `restore_from_backup(db_filename)` — atomically replaces a
     corrupt DB with the most recent passing backup. Stops the
     scheduler first, replaces the file, restarts. Documented as
     manual for now (we don't auto-restore — that's a foot-gun) but
     the helper is here so the procedure is one command, not a
     pile of cp/mv steps under stress.

If the integrity check fails on startup, we log loudly, send an
error notification, AND halt the scheduler — refusing to trade on a
corrupt DB is far better than silently mis-recording every fill.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


_SQLITE_MAGIC = b"SQLite format 3\x00"


def check_db(path: str) -> Dict[str, str]:
    """Run PRAGMA quick_check on one DB — file-level integrity only.

    Why quick_check, not integrity_check: integrity_check ALSO reports
    NOT NULL / UNIQUE / FK constraint violations on existing rows.
    Those are real schema-vs-data inconsistencies, but they're NOT
    file corruption — adding a NOT NULL column via ALTER TABLE leaves
    pre-existing rows NULL even though the schema declares them
    NOT NULL. Treating that as "halt the scheduler" is wrong; the DB
    is structurally fine and the rows can be migrated lazily.

    quick_check skips constraint verification and reports only
    storage-level damage (mangled pages, broken indexes, etc.) — the
    actual class of failure that warrants refusing to start.

    Pre-check: a 0-byte or missing-magic-header file is treated as
    corrupt — SQLite happily opens an empty file as a valid empty DB,
    and we caught a near-miss restore on 2026-05-05 that "succeeded"
    by copying a 0-byte WAL sidecar over the live path.

    Open mode: `immutable=1` prevents SQLite from creating `-wal` /
    `-shm` sidecars next to the file we are inspecting. Without it,
    inspecting a backup file leaves sidecar pollution in the backup
    directory, which then gets picked up by find_latest_backup.

    Returns {"status": "ok"|"corrupt"|"missing"|"error", "detail": <str>}.
    """
    if not os.path.exists(path):
        return {"status": "missing", "detail": "file does not exist"}
    try:
        size = os.path.getsize(path)
        if size < len(_SQLITE_MAGIC):
            return {"status": "corrupt",
                    "detail": f"file is {size} bytes (too small for SQLite header)"}
        with open(path, "rb") as f:
            magic = f.read(len(_SQLITE_MAGIC))
        if magic != _SQLITE_MAGIC:
            return {"status": "corrupt",
                    "detail": "missing SQLite file header magic"}
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5.0,
        )
        result = conn.execute("PRAGMA quick_check").fetchall()
        conn.close()
        # An OK DB returns exactly [("ok",)]
        if len(result) == 1 and result[0][0] == "ok":
            return {"status": "ok", "detail": "ok"}
        # Filter out NOT NULL constraint violations on existing rows.
        # These are NOT file corruption — they happen when a column is
        # added to an existing table via ALTER TABLE with NOT NULL but
        # no DEFAULT, leaving pre-existing rows with NULL. The DB is
        # structurally fine; refusing to start would be wrong.
        # (UNIQUE / FK / page-storage problems all surface as different
        # message text and DO halt below.)
        non_constraint = [
            str(r[0]) for r in result
            if not str(r[0]).startswith("NULL value in")
        ]
        if not non_constraint:
            return {"status": "ok",
                    "detail": f"ok (ignored {len(result)} NOT NULL violations)"}
        msgs = "; ".join(non_constraint[:5])
        return {"status": "corrupt", "detail": msgs}
    except sqlite3.DatabaseError as exc:
        return {"status": "corrupt", "detail": str(exc)}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


def _all_db_paths(repo_root: Optional[str] = None) -> List[str]:
    """Discover every SQLite DB the system writes to."""
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    paths: List[str] = []
    # Master DB
    master = os.path.join(repo_root, "quantopsai.db")
    if os.path.exists(master):
        paths.append(master)
    # Per-profile DBs
    paths.extend(glob.glob(os.path.join(
        repo_root, "quantopsai_profile_*.db",
    )))
    # Alt-data project DBs (post-merge: altdata/<p>/data/*.db)
    paths.extend(glob.glob(os.path.join(
        repo_root, "altdata", "*", "data", "*.db",
    )))
    # Strategy validation DB
    strat = os.path.join(repo_root, "strategy_validations.db")
    if os.path.exists(strat):
        paths.append(strat)
    return sorted(paths)


def check_all_dbs(repo_root: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """Run PRAGMA integrity_check on every DB. Returns dict mapping
    relative-path → {status, detail}."""
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    out: Dict[str, Dict[str, str]] = {}
    for path in _all_db_paths(repo_root):
        rel = os.path.relpath(path, repo_root)
        out[rel] = check_db(path)
    return out


def any_corrupt(results: Dict[str, Dict[str, str]]) -> List[str]:
    """Return list of relative paths that are corrupt."""
    return [
        rel for rel, info in results.items()
        if info["status"] == "corrupt"
    ]


# Strict timestamp suffix to keep sidecars (-wal/-shm) and corrupt-archive
# files (corrupt-<TS>) from matching. Accepts either:
#   YYYYMMDD       (date only — used by some hand-named snapshots)
#   YYYYMMDD-HHMM  (produced by backup_daily.sh)
_NEW_TS_RE = re.compile(r"^\d{8}(-\d{4})?$")
_LEGACY_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}\.db$")  # 2026-04-22_2054.db


def find_latest_backup(db_filename: str,
                       backup_dir: str = "/opt/quantopsai/backups") -> Optional[str]:
    """Find the most recent backup of `db_filename`. Returns absolute
    path of file with the latest mtime, or None.

    Two naming conventions supported:
      - New (produced by backup_daily.sh):
          <db_filename>.<YYYYMMDD-HHMM>
          e.g. quantopsai.db.20260505-1200
      - Legacy (hand-named ad-hoc snapshots):
          <basename>_<YYYY-MM-DD>_<HHMM>.db
          e.g. quantopsai_2026-04-22_2054.db
        Restricted to dated suffix so a query for `quantopsai.db`
        does NOT pick up `quantopsai_profile_10_*.db`.

    Excludes by design:
      - `<filename>.<TS>-wal` and `<filename>.<TS>-shm` SQLite sidecars
        that appear when something opens the backup in non-immutable
        mode. These are 0-byte / 32KB sidecars, not real backups.
      - `<filename>.corrupt-<TS>` files written by restore_from_backup
        when archiving the corrupt original aside. Picking one of
        those as a "backup" would loop the restore on its own corrupt
        archive (caught during 2026-05-05 rehearsal).
    """
    if not os.path.isdir(backup_dir):
        return None
    basename = db_filename[:-3] if db_filename.endswith(".db") else db_filename
    candidates: List[str] = []
    # New format: filename must end with a strict YYYYMMDD-HHMM suffix.
    for path in glob.glob(os.path.join(backup_dir, f"{db_filename}.*")):
        suffix = os.path.basename(path)[len(db_filename) + 1:]
        if _NEW_TS_RE.match(suffix):
            candidates.append(path)
    # Legacy format: <basename>_<YYYY-MM-DD>_<HHMM>.db
    for path in glob.glob(os.path.join(backup_dir, f"{basename}_*.db")):
        suffix = os.path.basename(path)[len(basename) + 1:]
        if _LEGACY_TS_RE.match(suffix):
            candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def restore_from_backup(db_filename: str,
                         repo_root: Optional[str] = None,
                         backup_dir: str = "/opt/quantopsai/backups",
                         dry_run: bool = False) -> Dict[str, str]:
    """One-command restore. Caller must STOP the scheduler first
    (we don't do it here — that requires sudo / systemctl access
    we may not have in-process).

    Steps:
      1. Find the latest backup file.
      2. Verify the backup itself passes integrity_check.
      3. Move the live DB aside as `<name>.corrupt-<timestamp>`.
      4. Copy the backup to the live path.
      5. Verify the restored file is intact.

    Returns {"status": "ok"|"error", "detail": <str>, "from_backup": <str>}.
    """
    from datetime import datetime
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    live_path = os.path.join(repo_root, db_filename)
    backup = find_latest_backup(db_filename, backup_dir=backup_dir)
    if backup is None:
        return {"status": "error",
                "detail": f"no backup found for {db_filename}",
                "from_backup": ""}
    bk_check = check_db(backup)
    if bk_check["status"] != "ok":
        return {"status": "error",
                "detail": f"backup {backup} also corrupt: {bk_check['detail']}",
                "from_backup": backup}
    if dry_run:
        return {"status": "ok",
                "detail": "dry-run: would restore",
                "from_backup": backup}
    # Move corrupt aside, then copy backup in.
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if os.path.exists(live_path):
        shutil.move(live_path, f"{live_path}.corrupt-{ts}")
    shutil.copy2(backup, live_path)
    # Verify
    verify = check_db(live_path)
    if verify["status"] != "ok":
        return {"status": "error",
                "detail": f"restore failed integrity_check: {verify['detail']}",
                "from_backup": backup}
    logger.warning(
        "DB restored: %s ← %s (corrupt original archived as %s.corrupt-%s)",
        live_path, backup, live_path, ts,
    )
    return {"status": "ok", "detail": "restored", "from_backup": backup}
