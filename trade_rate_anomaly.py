"""Trade-rate anomaly detection — Item 5 of docs/17 Phase 1 (2026-05-18).

Operator-visibility layer on top of the autonomous guardrails. Items
1-4 prevent and unwind over-restriction structurally; Item 5 surfaces
the *symptom* (entry-count drop) so the operator knows when the
autonomous systems are working overtime.

Per `feedback_ai_driven_no_manual_loop`: detection is purely
observational. The tuner is NOT paused; it continues to auto-loosen
(Item 2) and auto-expire (Item 4) regardless of whether the alert
fires. The alert is a "you should look at root cause" signal, not a
gate on the system's autonomous behavior.

Detection rule:
  current_week_entries  = stock entries in [now - 7d, now]
  prior_week_entries    = stock entries in [now - 14d, now - 7d]
  fire iff prior_week_entries >= MIN_PRIOR_WEEK
          AND current_week_entries < prior_week_entries * 0.5

The MIN_PRIOR_WEEK floor (default 5) prevents firing on noise:
1 entry → 0 entries is a 100% drop but isn't actionable.

The alert is written into the master `audit_alerts` table with a
stable per-profile-per-prior-week signature so re-running the check
doesn't create duplicates, and so the alert resolves naturally when
trade rate recovers (the same signature stops being in the current
items set and audit_runner marks it resolved).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Knobs — exposed for tests; production rarely tunes.
WINDOW_DAYS = 7
DROP_THRESHOLD = 0.5         # current < prior * 0.5 → fire
MIN_PRIOR_WEEK_ENTRIES = 5   # noise floor

_ENTRY_SIDES = ("buy", "short")
_ENTRY_SIGNALS = ("BUY", "STRONG_BUY", "SHORT", "STRONG_SELL")


def _count_entries_in_window(profile_db_path: str,
                              window_start: datetime,
                              window_end: datetime) -> int:
    """Count stock-entry trades in `[window_start, window_end)` for
    the given profile DB. Mirrors the volume-floor query so the
    semantics match across the four guardrail layers.

    Fail-soft: returns 0 on any sqlite error (missing table, locked
    DB, etc.) — the anomaly check stays observational and shouldn't
    crash on a transient infrastructure problem.
    """
    if not profile_db_path or not os.path.exists(profile_db_path):
        return 0
    try:
        with closing(sqlite3.connect(profile_db_path)) as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) FROM trades
                    WHERE timestamp >= ?
                      AND timestamp < ?
                      AND side IN ({','.join('?' for _ in _ENTRY_SIDES)})
                      AND signal_type IN ({','.join('?' for _ in _ENTRY_SIGNALS)})""",
                (window_start.isoformat(), window_end.isoformat(),
                 *_ENTRY_SIDES, *_ENTRY_SIGNALS),
            ).fetchone()
            return int(row[0] or 0) if row else 0
    except sqlite3.OperationalError as exc:
        logger.warning(
            "trade_rate_anomaly: count query failed for %s: %s",
            profile_db_path, exc,
        )
        return 0


def detect_anomaly(profile_id: int, profile_db_path: str,
                    *,
                    now: Optional[datetime] = None,
                    drop_threshold: float = DROP_THRESHOLD,
                    min_prior_week: int = MIN_PRIOR_WEEK_ENTRIES,
                    ) -> Optional[Dict[str, Any]]:
    """Compute the two-window comparison. Returns a details dict when
    the anomaly fires, None otherwise.

    The returned dict matches the schema `audit_alerts.details_json`
    expects — it carries every number the operator needs to decide
    whether to investigate.
    """
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)
    current_start = now - timedelta(days=WINDOW_DAYS)
    prior_start = now - timedelta(days=2 * WINDOW_DAYS)

    current_entries = _count_entries_in_window(
        profile_db_path, current_start, now)
    prior_entries = _count_entries_in_window(
        profile_db_path, prior_start, current_start)

    if prior_entries < min_prior_week:
        return None  # noise floor — too few entries to compare

    threshold = prior_entries * drop_threshold
    if current_entries >= threshold:
        return None  # within tolerance

    drop_pct = 100.0 * (1.0 - current_entries / prior_entries)
    return {
        "profile_id": profile_id,
        "prior_week_start": prior_start.date().isoformat(),
        "prior_week_entries": prior_entries,
        "current_week_entries": current_entries,
        "drop_pct": round(drop_pct, 1),
        "threshold_pct": round(100.0 * (1.0 - drop_threshold), 1),
        "detected_at": now.isoformat(),
    }


def _signature(profile_id: int, prior_week_start: str) -> str:
    """Stable per-profile-per-prior-week signature. Re-running the
    check on the same week doesn't create duplicate rows; the same
    week's entries can recover and resolve the alert naturally."""
    return f"trade_rate_anomaly:{profile_id}:{prior_week_start}"


def record_alert(main_db_path: str, details: Dict[str, Any]) -> bool:
    """Insert (or refresh) an audit_alerts row for this anomaly.
    Returns True if a new row was created, False on update of an
    existing one. Fail-soft on sqlite errors.
    """
    sig = _signature(details["profile_id"], details["prior_week_start"])
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(main_db_path)) as conn:
            # audit_runner.py owns the canonical schema. We mirror it
            # here so this module can stand alone — CREATE TABLE IF
            # NOT EXISTS is a no-op when the table already exists.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS audit_alerts ("
                " signature TEXT PRIMARY KEY,"
                " audit_type TEXT NOT NULL,"
                " first_seen TEXT NOT NULL,"
                " last_seen TEXT NOT NULL,"
                " resolved_at TEXT,"
                " details_json TEXT,"
                " alert_sent INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            existing = conn.execute(
                "SELECT signature FROM audit_alerts WHERE signature = ?",
                (sig,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE audit_alerts SET last_seen = ?, "
                    "details_json = ?, resolved_at = NULL "
                    "WHERE signature = ?",
                    (now_iso, json.dumps(details, default=str), sig),
                )
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO audit_alerts "
                "(signature, audit_type, first_seen, last_seen, "
                " resolved_at, details_json, alert_sent) "
                "VALUES (?, 'trade_rate_anomaly', ?, ?, NULL, ?, 0)",
                (sig, now_iso, now_iso,
                 json.dumps(details, default=str)),
            )
            conn.commit()
            return True
    except sqlite3.OperationalError as exc:
        logger.warning(
            "trade_rate_anomaly: alert write failed: %s", exc,
        )
        return False


def resolve_alert_if_recovered(main_db_path: str, profile_id: int,
                                prior_week_start: str) -> bool:
    """Mark the alert for (profile, prior_week) as resolved. Called
    when a subsequent detection run no longer sees the anomaly —
    trade rate has recovered. Idempotent.

    Returns True iff a row was updated (i.e., there was an open
    alert and we just closed it).
    """
    sig = _signature(profile_id, prior_week_start)
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    try:
        with closing(sqlite3.connect(main_db_path)) as conn:
            cur = conn.execute(
                "UPDATE audit_alerts SET resolved_at = ? "
                "WHERE signature = ? AND resolved_at IS NULL",
                (now_iso, sig),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False


def check_and_alert(profile_id: int, profile_db_path: str,
                     main_db_path: str,
                     *,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """End-to-end: detect, write/resolve the audit_alerts row,
    return a small status dict for the scheduler logs.

    Status fields:
      - fired: True iff this run wrote/refreshed an alert
      - resolved: True iff this run cleared a previously-open alert
      - details: detection details when fired, else None
    """
    details = detect_anomaly(profile_id, profile_db_path, now=now)
    if details is None:
        # No anomaly this run. If there was an open alert for the
        # SAME prior-week signature, resolve it. We need to compute
        # prior_week_start to find the candidate signature.
        eff_now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)
        prior_start = (eff_now - timedelta(days=2 * WINDOW_DAYS)).date().isoformat()
        resolved = resolve_alert_if_recovered(
            main_db_path, profile_id, prior_start)
        return {"fired": False, "resolved": resolved, "details": None}

    is_new = record_alert(main_db_path, details)
    return {"fired": True, "is_new": is_new, "resolved": False,
            "details": details}
