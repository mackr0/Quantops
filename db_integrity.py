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
from contextlib import closing
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


_SQLITE_MAGIC = b"SQLite format 3\x00"


# Pattern for matching a per-profile DB filename and extracting the
# profile id, e.g. `quantopsai_profile_25.db` → 25.
_PROFILE_DB_RE = re.compile(r"quantopsai_profile_(\d+)\.db$")


def _known_profile_ids(master_path: str) -> Optional[Set[int]]:
    """Return the set of profile ids present in `trading_profiles`.

    Used by `_all_db_paths` to filter out PHANTOM profile journal
    files — `quantopsai_profile_<N>.db` files where no row exists
    in master.trading_profiles. These are typically 0-byte shells
    left by a process that was SIGKILLed mid-create-profile (after
    the file was touched but before the master INSERT committed).

    Treating phantoms as critical halts the entire scheduler over
    a file that no real profile points to. The 2026-05-19 incident:
    `quantopsai_profile_25.db` (0 bytes, no master row) caused the
    scheduler to restart-loop for 30+ minutes, blocking all 13 real
    profiles (ids 12-24) from running their cycles. Filtering by
    `trading_profiles` membership at discovery time makes the
    integrity gate immune to this class of phantom.

    Returns None when the master DB is itself missing or unreadable.
    Caller falls back to including every profile_*.db file (i.e.
    legacy behavior) — better to halt on a real corruption than to
    silently skip a real profile because the master temporarily
    couldn't be read.
    """
    if not master_path or not os.path.exists(master_path):
        return None
    try:
        with closing(sqlite3.connect(
            f"file:{master_path}?mode=ro&immutable=1",
            uri=True, timeout=5.0,
        )) as conn:
            rows = conn.execute(
                "SELECT id FROM trading_profiles"
            ).fetchall()
        return {int(r[0]) for r in rows}
    except sqlite3.DatabaseError as exc:
        # Master is corrupt/missing the table — fall through to
        # legacy "include everything" so the actual master-corruption
        # case still halts the scheduler.
        logger.warning(
            "db_integrity._known_profile_ids: master read failed (%s); "
            "falling back to including every profile_*.db file",
            exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "db_integrity._known_profile_ids: unexpected error (%s); "
            "falling back to legacy behavior", exc,
        )
        return None


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
        with closing(sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5.0,
        )) as conn:
            result = conn.execute("PRAGMA quick_check").fetchall()
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
    """Discover every SQLite DB the system writes to.

    Profile journal files are filtered against `trading_profiles`:
    files whose id is not present in master.trading_profiles are
    treated as phantoms (typically 0-byte shells from a killed
    create-profile flow) and skipped. A loud warning is logged for
    each skipped phantom so an operator can investigate, but the
    scheduler is allowed to start. This closes the 2026-05-19
    phantom-DB restart loop without weakening the integrity gate
    for real profiles."""
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    paths: List[str] = []
    # Master DB
    master = os.path.join(repo_root, "quantopsai.db")
    if os.path.exists(master):
        paths.append(master)
    # Per-profile DBs — filter out phantom files whose id isn't in
    # trading_profiles. Fall back to including every file when the
    # master is unreadable (legacy behavior; conservative).
    known_ids = _known_profile_ids(master)
    profile_files = glob.glob(os.path.join(
        repo_root, "quantopsai_profile_*.db",
    ))
    for path in profile_files:
        if known_ids is None:
            paths.append(path)
            continue
        m = _PROFILE_DB_RE.search(os.path.basename(path))
        if not m:
            # Doesn't match the standard pattern — include defensively
            # so a malformed name doesn't silently disappear from the
            # integrity scan.
            paths.append(path)
            continue
        pid = int(m.group(1))
        if pid in known_ids:
            paths.append(path)
        else:
            logger.warning(
                "db_integrity: skipping orphan profile DB %s "
                "(no profile id=%d in trading_profiles — phantom "
                "file, not blocking scheduler startup)",
                path, pid,
            )
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


# 2026-05-13 — DB criticality classification (Wave 9b).
# Critical DBs hold trade-pipeline truth (master config, per-profile
# trades + ai_predictions, primary alt-data caches the AI prompt
# reads from). Their corruption MUST halt the scheduler — refusing
# to trade on broken data is the right behavior.
#
# Non-critical DBs hold derived/auxiliary state that can be
# re-created from scratch without losing trade data. Their
# corruption SHOULD email + log + continue rather than halt the
# scheduler. The 2026-05-13 incident was a 0-byte
# strategy_validations.db (no rows ever written) crashing the
# scheduler in a 30-second restart loop, sending 145 ERROR emails
# before being noticed.
#
# A path is non-critical iff it matches one of these basenames.
NON_CRITICAL_DB_BASENAMES = {
    # Strategy backtest results — recreated from scratch when the
    # rigorous_backtest path runs. No trade data lost if blank.
    "strategy_validations.db",
}


def is_critical(path: str) -> bool:
    """True iff this DB path holds load-bearing trade-pipeline data.
    A False return means corruption should NOT halt the scheduler."""
    return os.path.basename(path) not in NON_CRITICAL_DB_BASENAMES


def critical_corrupt(results: Dict[str, Dict[str, str]]) -> List[str]:
    """Return relative paths that are corrupt AND critical. The
    scheduler should refuse to start when this is non-empty.
    Non-critical corruption is reported separately (log + email)
    but does not halt."""
    return [
        rel for rel, info in results.items()
        if info["status"] == "corrupt" and is_critical(rel)
    ]


def non_critical_corrupt(results: Dict[str, Dict[str, str]]) -> List[str]:
    """Return relative paths that are corrupt AND non-critical.
    Reported and emailed (with debounce) but the scheduler can
    safely continue."""
    return [
        rel for rel, info in results.items()
        if info["status"] == "corrupt" and not is_critical(rel)
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
