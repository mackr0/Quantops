"""Profile halt helpers — the reconciler safety net (2026-05-19).

Per `feedback_no_orphan_broker_fills`: every broker order MUST be
atomically journaled by the submit_order code path. If the
reconciler ever finds a journal-vs-broker discrepancy that would
require SYNTHESIZING a journal row (a `backfill_sell`, `backfill_cover`,
`broker_orphan`, or `journal_phantom`), the system is broken
somewhere — the synthesis would paper over the root cause.

These helpers replace silent synthesis with: HALT the profile
(block new entries via `multi_scheduler`), write a loud `audit_alerts`
row, and let the next reconcile pass auto-clear when the drift is
gone.

Critical contracts:
  - HALT only blocks NEW ENTRIES. Existing exit / monitor / risk-
    snapshot tasks continue. A halted profile is safer than an
    unhalted one — closing positions is always permitted; opening
    new ones requires the system to trust the journal.
  - Auto-clear: when the reconciler's next pass detects no
    synthesis needed, it calls clear_halt unconditionally
    (idempotent — no-op if not halted).
  - Manual override: operator can clear via Settings UI after
    investigating the root cause. The halt itself is automatic;
    the unhalt is automatic OR operator-triggered.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _master_db_path() -> str:
    """Best-effort resolution of the master DB path.

    Resolution order:
      1. `QUANTOPSAI_DB` env var (used by unit tests and overrides)
      2. `config.DB_PATH` (the canonical app-level setting,
         DB_PATH env-aware)
      3. `/opt/quantopsai/quantopsai.db` (prod canonical absolute
         path — covers cron jobs that `cd` into subdirectories
         before invoking Python, which is what produced the
         2026-06-05 /issues flood when cred lookups resolved
         a CWD-local empty DB instead of the real one)
      4. `quantopsai.db` next to this module (dev / repo-local)
    """
    explicit = os.environ.get("QUANTOPSAI_DB")
    if explicit:
        return explicit
    try:
        from config import DB_PATH as _cfg_db
    except Exception:
        _cfg_db = "quantopsai.db"
    if os.path.isabs(_cfg_db) or os.path.exists(_cfg_db):
        return _cfg_db
    for candidate in (
        "/opt/quantopsai/quantopsai.db",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "quantopsai.db",
        ),
    ):
        if os.path.exists(candidate):
            return candidate
    return _cfg_db


def is_halted(profile_id: int, db_path: Optional[str] = None,
              ) -> Tuple[bool, Optional[str]]:
    """Return (halted, reason). Reason is None when not halted.

    Read-only — safe to call from every cycle / request. Never raises;
    on DB error returns (False, None) so a flaky DB doesn't
    accidentally HALT trading.
    """
    db = db_path or _master_db_path()
    try:
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT trading_halted, halt_reason "
                "FROM trading_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
    except Exception as exc:
        logger.warning(
            "is_halted(%s) DB error (treating as NOT halted): %s: %s",
            profile_id, type(exc).__name__, exc,
        )
        return (False, None)
    if not row:
        return (False, None)
    return (bool(row[0]), row[1])


def halt_profile(profile_id: int, reason: str,
                  db_path: Optional[str] = None) -> bool:
    """Set trading_halted=1 + halt_reason + halted_at on a profile.

    Idempotent — calling repeatedly with the same reason refreshes
    halted_at but doesn't double-alert. Returns True if the row
    changed state from unhalted to halted (caller may want to send
    a notification on first transition only); False if already
    halted or update failed.
    """
    db = db_path or _master_db_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(db)) as conn:
            cur = conn.execute(
                "SELECT trading_halted FROM trading_profiles "
                "WHERE id = ?", (profile_id,),
            ).fetchone()
            was_halted = bool(cur[0]) if cur else False
            conn.execute(
                "UPDATE trading_profiles SET trading_halted = 1, "
                "halt_reason = ?, halted_at = ? WHERE id = ?",
                (reason, now_iso, profile_id),
            )
            conn.commit()
        if not was_halted:
            logger.error(
                "TRADING HALTED profile=%s reason=%r",
                profile_id, reason,
            )
        return not was_halted
    except Exception as exc:
        logger.warning(
            "halt_profile(%s) failed: %s: %s",
            profile_id, type(exc).__name__, exc,
        )
        return False


def clear_halt(profile_id: int, source: str = "auto",
                db_path: Optional[str] = None) -> bool:
    """Unhalt a profile. Idempotent — no-op if not halted.

    Returns True if state transitioned from halted to unhalted;
    False otherwise. `source` is logged for audit (e.g., "auto"
    when the reconciler clears after drift resolves, or
    "settings_ui" when the operator manually clears).
    """
    db = db_path or _master_db_path()
    try:
        with closing(sqlite3.connect(db)) as conn:
            cur = conn.execute(
                "SELECT trading_halted, halt_reason FROM trading_profiles "
                "WHERE id = ?", (profile_id,),
            ).fetchone()
            if not cur or not cur[0]:
                return False
            prev_reason = cur[1]
            conn.execute(
                "UPDATE trading_profiles SET trading_halted = 0, "
                "halt_reason = NULL, halted_at = NULL WHERE id = ?",
                (profile_id,),
            )
            conn.commit()
        logger.info(
            "trading halt CLEARED profile=%s source=%s prev_reason=%r",
            profile_id, source, prev_reason,
        )
        return True
    except Exception as exc:
        logger.warning(
            "clear_halt(%s) failed: %s: %s",
            profile_id, type(exc).__name__, exc,
        )
        return False


def _write_audit_alert(db_path: str, alert_type: str, severity: str,
                        title: str, detail: str) -> None:
    """Write an audit_alerts row to a profile DB. Best-effort; never
    raises. Used by reconciler to surface halt events on /issues."""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    title TEXT NOT NULL,
                    detail TEXT,
                    resolved INTEGER NOT NULL DEFAULT 0)
            """)
            conn.execute(
                "INSERT INTO audit_alerts "
                "(alert_type, severity, title, detail) "
                "VALUES (?, ?, ?, ?)",
                (alert_type, severity, title, detail),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "audit_alerts write failed (%s): %s: %s",
            alert_type, type(exc).__name__, exc,
        )


def halt_and_alert(profile_id: int, db_path: str, alert_type: str,
                    title: str, detail: str,
                    master_db: Optional[str] = None) -> bool:
    """Combined: halt the profile + write an audit_alerts row + send
    a notify_error email (on first transition only).

    Used by reconcilers when they detect a synthesis-action gap.
    `db_path` is the per-profile journal DB where the audit_alerts
    row gets written. `master_db` is the master DB where the
    trading_profiles flag lives — defaults to QUANTOPSAI_DB env.

    Returns True if newly halted (first transition); False if
    already halted (refresh).
    """
    first_transition = halt_profile(profile_id, title, db_path=master_db)
    _write_audit_alert(
        db_path, alert_type, "critical", title, detail,
    )
    if first_transition:
        try:
            from notifications import notify_error
            notify_error(
                error_msg=(
                    f"Profile {profile_id} HALTED — reconciler safety net.\n\n"
                    f"{title}\n\n{detail}\n\n"
                    "Trading-pipeline dispatch is blocked for this profile "
                    "until either (a) the next reconcile pass shows no "
                    "drift (auto-clear) OR (b) operator clears manually "
                    "via Settings UI after fixing the root cause "
                    "(check which submit_order call site failed to "
                    "journal atomically)."
                ),
                context=f"reconciler HALT: {alert_type}",
            )
        except (ImportError, AttributeError, OSError) as exc:
            logger.warning(
                "halt notify_error delivery failed: %s: %s",
                type(exc).__name__, exc,
            )
    return first_transition
