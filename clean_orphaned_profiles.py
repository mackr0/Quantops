"""Delete orphaned trading_profiles + their per-profile DB files.

Context (2026-05-17): the user deleted the old Alpaca paper accounts
from the UI before creating new ones for the 13-profile fresh-start
experiment (docs/15). Profiles whose `alpaca_account_id` references a
deleted alpaca_accounts row are now broken:
  - dashboard can't connect to broker (key/secret rows gone)
  - per-profile DB file still exists on disk holding stale trade data
  - the AI pipeline would still try to scan/trade through them

This script:
  1. Scans `quantopsai.db` for trading_profiles whose
     `alpaca_account_id` is NOT NULL but points to a row that no
     longer exists in `alpaca_accounts`.
  2. For each orphan: backs up its per-profile DB
     (`quantopsai_profile_<id>.db`) to a timestamped folder, then
     deletes the DB file and removes the trading_profiles row.

PRESERVED:
  - All other trading_profiles (still-active broker links)
  - alpaca_accounts table (you manage that via the UI)
  - Altdata DBs (insider, congresstrades, etc.) — world data
  - The backups (recoverable for 30 days minimum)

OPTIONAL FLAGS:
  --apply               Actually perform the wipe (default: dry-run)
  --user-id             Which user's profiles to scan (default 1)
  --clear-audit-alerts  After cleanup, TRUNCATE the audit_alerts table.
                        Use this during the fresh-start reset so the
                        /issues page doesn't show stale drift items
                        from the now-deleted profiles. Default OFF —
                        re-running this script on a healthy system
                        should NOT wipe legitimate accumulated alerts.
  --remove-all-alpaca-accounts
                        Treat EVERY trading_profile as an orphan AND
                        remove every alpaca_accounts row for the user.
                        Use when the user has deleted their Alpaca
                        accounts at Alpaca.com but the QuantOps
                        alpaca_accounts rows still exist with stale
                        API keys — the fresh-start case where the
                        default orphan detector counts zero because
                        nothing is technically "orphaned" yet.

Run on prod:
    cd /opt/quantopsai && source .env
    # Dry-run first — confirms which profiles + DB files are orphaned
    /opt/quantopsai/venv/bin/python clean_orphaned_profiles.py
    # If the list is what you expect:
    /opt/quantopsai/venv/bin/python clean_orphaned_profiles.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


_BACKUP_ROOT = "/opt/quantopsai/backups/pre-orphan-cleanup"
_MAIN_DB_CANDIDATES = (
    "/opt/quantopsai/quantopsai.db",
    "quantopsai.db",
)


def _resolve_main_db() -> str:
    for p in _MAIN_DB_CANDIDATES:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "quantopsai.db not found at any expected path: %s" % (_MAIN_DB_CANDIDATES,)
    )


def _per_profile_db_path(profile_id: int) -> str:
    """Production path with a local-dev fallback (matches the
    convention in reset_for_clean_experiment.py)."""
    p = f"/opt/quantopsai/quantopsai_profile_{profile_id}.db"
    if os.path.exists(p):
        return p
    return f"quantopsai_profile_{profile_id}.db"


def _find_orphans(main_db: str, user_id: int,
                  remove_all: bool = False) -> List[Dict]:
    """Return profiles to remove.

    Default behavior: only profiles whose alpaca_account_id is set
    but doesn't resolve in alpaca_accounts.

    When `remove_all=True` (--remove-all-alpaca-accounts): every
    trading_profile for the user, regardless of alpaca_account_id
    state. Used during the fresh-start reset where the user has
    deleted their Alpaca accounts at Alpaca.com but the QuantOps
    alpaca_accounts rows still exist with stale API keys.
    """
    conn = sqlite3.connect(main_db)
    conn.row_factory = sqlite3.Row
    try:
        live_accounts = {
            r["id"] for r in conn.execute(
                "SELECT id FROM alpaca_accounts WHERE user_id = ?",
                (user_id,),
            )
        }
        profiles = conn.execute(
            "SELECT id, name, alpaca_account_id, enabled "
            "FROM trading_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    orphans = []
    for p in profiles:
        ac = p["alpaca_account_id"]
        if remove_all:
            # Take everything — fresh-start mode.
            orphans.append({
                "id": p["id"], "name": p["name"],
                "alpaca_account_id": ac,
                "enabled": p["enabled"],
                "db_path": _per_profile_db_path(p["id"]),
            })
            continue
        # Default-mode: skip null-fallback + still-valid pointers.
        if ac is None:
            continue
        if ac in live_accounts:
            continue
        orphans.append({
            "id": p["id"],
            "name": p["name"],
            "alpaca_account_id": ac,
            "enabled": p["enabled"],
            "db_path": _per_profile_db_path(p["id"]),
        })
    return orphans


def _remove_all_alpaca_accounts(main_db: str, user_id: int,
                                apply: bool) -> int:
    """Wipe every alpaca_accounts row for the user. Returns rowcount.

    Backed by the script's broader --apply gate; pre-flight count
    is shown on dry-run."""
    with sqlite3.connect(main_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM alpaca_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        if apply and count > 0:
            conn.execute(
                "DELETE FROM alpaca_accounts WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            log.info("    removed %d alpaca_accounts row(s)", count)
        elif not apply:
            log.info(
                "    DRY: would DELETE %d alpaca_accounts row(s) "
                "for user %d", count, user_id,
            )
        else:
            log.info("    no alpaca_accounts rows for user %d", user_id)
    return int(count or 0)


def _backup_db_file(db_path: str, backup_dir: str) -> str:
    """Copy db_path to backup_dir via SQLite online backup (WAL-safe)."""
    os.makedirs(backup_dir, exist_ok=True)
    fname = os.path.basename(db_path)
    dest = os.path.join(backup_dir, fname)
    with sqlite3.connect(db_path) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    log.info("    backup → %s (%d bytes)", dest, os.path.getsize(dest))
    return dest


def _delete_orphan(main_db: str, orphan: Dict, backup_dir: str,
                   apply: bool) -> Dict:
    """Backup + remove DB file + delete profile row."""
    result = {"id": orphan["id"], "backup": None,
              "file_removed": False, "row_removed": False}
    pid = orphan["id"]
    db_path = orphan["db_path"]
    log.info("  pid=%d name=%s acct=%s db=%s", pid, orphan["name"],
             orphan["alpaca_account_id"], db_path)

    if os.path.exists(db_path):
        if apply:
            result["backup"] = _backup_db_file(db_path, backup_dir)
            os.remove(db_path)
            result["file_removed"] = True
            log.info("    db file removed")
        else:
            log.info("    DRY: would back up + remove %s", db_path)
    else:
        log.info("    no db file on disk (already gone)")

    if apply:
        with sqlite3.connect(main_db) as conn:
            cur = conn.execute(
                "DELETE FROM trading_profiles WHERE id = ?", (pid,),
            )
            if cur.rowcount:
                result["row_removed"] = True
                log.info("    profile row removed from quantopsai.db")
            conn.commit()
    else:
        log.info("    DRY: would DELETE FROM trading_profiles WHERE id=%d", pid)
    return result


def _clear_audit_alerts(main_db: str, apply: bool) -> int:
    """TRUNCATE the audit_alerts table so /issues starts truly clean
    after the fresh-start reset. Returns rowcount removed."""
    try:
        with sqlite3.connect(main_db) as conn:
            # Pre-count for the dry-run preview AND the apply summary.
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_alerts"
            ).fetchone()
            count = int(row[0] or 0) if row else 0
            if apply and count > 0:
                conn.execute("DELETE FROM audit_alerts")
                conn.commit()
                log.info("    cleared %d audit_alerts row(s)", count)
            elif not apply:
                log.info(
                    "    DRY: would DELETE %d audit_alerts row(s)",
                    count,
                )
            else:
                log.info("    audit_alerts already empty")
            return count
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            log.info("    audit_alerts table doesn't exist yet "
                     "(audit_runner hasn't run) — nothing to clear")
            return 0
        log.error("    audit_alerts wipe failed: %s", exc)
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the cleanup (default: dry-run)")
    ap.add_argument("--user-id", type=int, default=1)
    ap.add_argument(
        "--clear-audit-alerts", action="store_true",
        help="After cleanup, TRUNCATE the audit_alerts table so "
             "stale drift items from deleted profiles don't show "
             "on /issues. Default OFF.",
    )
    ap.add_argument(
        "--remove-all-alpaca-accounts", action="store_true",
        help="Treat EVERY trading_profile as an orphan AND remove "
             "every alpaca_accounts row for the user. Fresh-start "
             "mode. Default OFF.",
    )
    args = ap.parse_args()

    log.info("=" * 70)
    log.info(
        "ORPHAN CLEANUP (apply=%s, user=%d, clear_audit_alerts=%s, "
        "remove_all_alpaca_accounts=%s)",
        args.apply, args.user_id, args.clear_audit_alerts,
        args.remove_all_alpaca_accounts,
    )
    log.info("=" * 70)

    main_db = _resolve_main_db()
    log.info("main db: %s", main_db)
    orphans = _find_orphans(
        main_db, args.user_id,
        remove_all=args.remove_all_alpaca_accounts,
    )
    if (not orphans
            and not args.clear_audit_alerts
            and not args.remove_all_alpaca_accounts):
        log.info("No orphaned profiles found and no other flags set "
                 "— nothing to do.")
        return 0
    log.info("Profiles to remove: %d", len(orphans))

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = (
        f"{_BACKUP_ROOT}-{ts}" if args.apply
        else "(dry-run, no backup dir)"
    )
    log.info("backup dir: %s", backup_dir)

    removed_files = 0
    removed_rows = 0
    for o in orphans:
        r = _delete_orphan(main_db, o, backup_dir, args.apply)
        if r["file_removed"]:
            removed_files += 1
        if r["row_removed"]:
            removed_rows += 1

    # Order matters: remove profiles FIRST (so foreign references
    # are gone), THEN remove alpaca_accounts. Otherwise SQLite
    # foreign-key constraints (if enabled) would block the wipe.
    removed_accounts = 0
    if args.remove_all_alpaca_accounts:
        log.info("removing alpaca_accounts for user %d...", args.user_id)
        removed_accounts = _remove_all_alpaca_accounts(
            main_db, args.user_id, args.apply,
        )

    cleared_alerts = 0
    if args.clear_audit_alerts:
        log.info("clearing audit_alerts table...")
        cleared_alerts = _clear_audit_alerts(main_db, args.apply)

    log.info("=" * 70)
    if args.apply:
        log.info(
            "DONE: removed %d db file(s), %d profile row(s)%s%s",
            removed_files, removed_rows,
            (f", {removed_accounts} alpaca_accounts row(s)"
             if args.remove_all_alpaca_accounts else ""),
            (f", cleared {cleared_alerts} audit_alerts row(s)"
             if args.clear_audit_alerts else ""),
        )
        log.info(
            "Next steps:\n"
            "  - Restart services so caches drop: systemctl restart "
            "quantopsai quantopsai-web\n"
            "  - Verify /issues page is empty\n"
            "  - Create the 13 experiment profiles: "
            "python3 create_experiment_profiles.py --apply\n"
            "  - Then create fresh Alpaca accounts in the UI and "
            "wire alpaca_account_id on each profile"
        )
    else:
        log.info("DRY-RUN preview. Re-run with --apply to execute.")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
