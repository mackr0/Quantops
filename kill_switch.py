"""Master kill switch — single global flag that blocks all new
trade entries across every profile.

Two ways it can flip:

1. **Manually**, via the admin endpoint or `kill_switch.activate()` —
   for "I don't trust the system right now, stop everything" days.

2. **Automatically**, by the per-cycle daily-loss-floor task. When
   cumulative book-wide day-of P&L drops below the configured floor
   (default -8% of opening equity), this module is flipped ON with
   `reason="auto: book P&L crossed daily-loss floor"`. The next
   cycle's pre-trade gate sees the flag and refuses every new entry.

When the flag is ON:
- New BUY / SHORT entries return `KILL_SWITCH` instead of executing.
- Existing positions and broker stops are NOT touched. Stops still
  fire at the broker; held positions still resolve normally.
- Exit logic still runs — if you want to manually flatten, that
  works.

The flag persists in the `kill_switch_state` table on the master DB
(single-row pattern, primary key=1). It survives restarts. Clearing
it requires either an explicit `deactivate()` call or admin action;
auto-activation does not auto-clear at midnight (a -8% day deserves
human review before resuming).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional, Tuple

import config

logger = logging.getLogger(__name__)


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or config.DB_PATH)


def _ensure_table(db_path: Optional[str] = None) -> None:
    """Create the kill_switch_state table if missing. Single row,
    primary key id=1."""
    conn = _conn(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kill_switch_state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                enabled     INTEGER NOT NULL DEFAULT 0,
                reason      TEXT,
                set_at      TEXT,
                set_by      TEXT
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO kill_switch_state (id, enabled, set_at, set_by)
            VALUES (1, 0, datetime('now'), 'system_init')
        """)
        # History rows for audit
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kill_switch_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT NOT NULL,           -- 'activate' | 'deactivate'
                reason      TEXT,
                set_by      TEXT,
                set_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def is_active(db_path: Optional[str] = None) -> Tuple[bool, str]:
    """Return (enabled, reason). Always succeeds — returns (False, '')
    on any DB error so a kill-switch read failure does NOT auto-block
    trading (we'd rather risk a missed activation than freeze the
    book on transient SQLite locking)."""
    try:
        _ensure_table(db_path)
        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT enabled, reason FROM kill_switch_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return False, ""
            return bool(row[0]), (row[1] or "")
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Kill-switch read failed: %s", exc)
        return False, ""


def activate(reason: str, set_by: str = "manual",
              db_path: Optional[str] = None) -> bool:
    """Turn the kill switch ON. Idempotent — calling on an already-
    active switch refreshes the reason/set_at fields but doesn't
    duplicate history rows for the same reason."""
    _ensure_table(db_path)
    now_iso = datetime.utcnow().isoformat()
    conn = _conn(db_path)
    try:
        already, prev_reason = is_active(db_path)
        conn.execute(
            "UPDATE kill_switch_state SET enabled = 1, reason = ?, "
            "set_at = ?, set_by = ? WHERE id = 1",
            (reason, now_iso, set_by),
        )
        # Only log a history row when transitioning OFF→ON or when the
        # reason changes (avoid spam on every cycle's auto-check).
        if not already or prev_reason != reason:
            conn.execute(
                "INSERT INTO kill_switch_history (action, reason, set_by) "
                "VALUES ('activate', ?, ?)",
                (reason, set_by),
            )
            logger.warning(
                "KILL SWITCH activated by %s: %s", set_by, reason,
            )
        conn.commit()
        return True
    finally:
        conn.close()


def deactivate(set_by: str = "manual",
                db_path: Optional[str] = None) -> bool:
    _ensure_table(db_path)
    conn = _conn(db_path)
    try:
        already, _ = is_active(db_path)
        conn.execute(
            "UPDATE kill_switch_state SET enabled = 0, "
            "set_at = datetime('now'), set_by = ? WHERE id = 1",
            (set_by,),
        )
        if already:
            conn.execute(
                "INSERT INTO kill_switch_history (action, reason, set_by) "
                "VALUES ('deactivate', '', ?)",
                (set_by,),
            )
            logger.warning("KILL SWITCH deactivated by %s", set_by)
        conn.commit()
        return True
    finally:
        conn.close()


def get_history(limit: int = 20, db_path: Optional[str] = None):
    """Return recent kill-switch transitions for the dashboard."""
    _ensure_table(db_path)
    conn = _conn(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT action, reason, set_by, set_at "
            "FROM kill_switch_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily-loss floor — auto-flips the kill switch
# ---------------------------------------------------------------------------

def compute_book_day_pnl_pct(profile_db_paths) -> Optional[float]:
    """Sum today's realized + unrealized P&L across every profile DB,
    divide by the sum of opening-day equities, return as a percentage.

    Returns None when we can't get a clean baseline — which means the
    floor task should NOT activate (no false positives on bad data).

    `profile_db_paths` is an iterable of profile DB paths. We read
    each profile's most recent `daily_snapshots` row from yesterday
    (or earlier) for the baseline equity, plus today's broker equity
    via the per-profile journal (or current-day snapshot if available).
    """
    total_baseline = 0.0
    total_today = 0.0
    rows_seen = 0
    for path in profile_db_paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            # Yesterday-or-earlier snapshot equity = today's baseline
            prev = conn.execute(
                "SELECT equity FROM daily_snapshots "
                "WHERE date < date('now') "
                "ORDER BY date DESC, rowid DESC LIMIT 1"
            ).fetchone()
            today = conn.execute(
                "SELECT equity FROM daily_snapshots "
                "WHERE date = date('now') "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if prev is None or today is None:
                continue
            total_baseline += float(prev["equity"] or 0)
            total_today += float(today["equity"] or 0)
            rows_seen += 1
        except Exception as exc:
            logger.debug("compute_book_day_pnl_pct: %s skipped (%s)",
                          path, exc)
            continue
    if rows_seen == 0 or total_baseline <= 0:
        return None
    return ((total_today - total_baseline) / total_baseline) * 100.0


def check_and_activate_on_loss_floor(
    profile_db_paths,
    floor_pct: float = -8.0,
    db_path: Optional[str] = None,
) -> Optional[float]:
    """Compute book-wide day-P&L and activate the kill switch if it's
    below the floor. Returns the computed percentage (or None if not
    computable). Idempotent: re-activating with the same auto reason
    does not spam history rows."""
    pnl_pct = compute_book_day_pnl_pct(profile_db_paths)
    if pnl_pct is None:
        return None
    if pnl_pct < floor_pct:
        reason = (
            f"auto: book day P&L {pnl_pct:.2f}% breached "
            f"floor {floor_pct:.2f}%"
        )
        activate(reason, set_by="auto_loss_floor", db_path=db_path)
    return pnl_pct
