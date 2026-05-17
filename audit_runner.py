"""Cross-profile audit scheduler + first-detection alerter (#169).

The five integrity audits (qty, value, cash, basis, equity-identity)
each only ran when something pulled `/issues` — drift could exist for
days before someone happened to look. This module:

1. Runs every audit on a regular cron.
2. Computes a stable signature for each drift item.
3. Persists the set of signatures we've already alerted on.
4. Emails (or warns) only on the FIRST appearance of any new
   signature, so a long-standing drift item doesn't spam every cycle.
5. Logs (without emailing) when an old signature stops appearing
   ("resolved") so the operator sees the recovery path.

Storage: a tiny `audit_alerts` table in quantopsai.db (main DB, not
per-profile — these signatures are cross-profile by design).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


_MAIN_DB_CANDIDATES = (
    "/opt/quantopsai/quantopsai.db",
    "quantopsai.db",
)

# Per audit type, which fields of the drift dict identify the
# specific item. Used to build a stable signature for dedup.
_SIG_KEYS: Dict[str, Tuple[str, ...]] = {
    "qty_parity":     ("account", "symbol"),
    "value_parity":   ("account",),
    "cash_parity":    ("account",),
    "basis_parity":   ("account", "symbol"),
    "equity_identity": ("profile_id",),
    "reconciler_heartbeat": ("profile_id",),
}


def _resolve_main_db(main_db_override: Optional[str] = None) -> Optional[str]:
    """Return a writable main-DB path, or None.

    Explicit overrides ARE existence-checked — passing a nonexistent
    path returns None so the audit can safe-skip rather than crash on
    `sqlite3.connect` of an unwritable filesystem (e.g. a missing
    parent directory)."""
    import os
    if main_db_override and main_db_override != "auto":
        if os.path.exists(main_db_override):
            return main_db_override
        # Path doesn't exist — try to create it ONLY if its parent
        # directory exists (otherwise return None for safe-skip).
        parent = os.path.dirname(main_db_override) or "."
        if os.path.isdir(parent):
            return main_db_override
        return None
    for p in _MAIN_DB_CANDIDATES:
        try:
            if os.path.exists(p):
                return p
        # SILENT_OK: loop's purpose is to skip unreachable candidates (perm-denied, broken mount). Caller gets None on full-list miss → safe-skip in detect_and_alert_new_drift.
        except Exception:
            continue
    return None


def _ensure_audit_alerts_table(conn: sqlite3.Connection) -> None:
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


def _signature(audit_type: str, drift_dict: Dict[str, Any]) -> str:
    parts = [audit_type]
    for k in _SIG_KEYS.get(audit_type, ()):
        parts.append(str(drift_dict.get(k, "?")))
    return ":".join(parts)


def run_all_audits(profile_ids: Iterable[int]) -> List[Dict[str, Any]]:
    """Run all five integrity audits. Returns a flat list of drift
    items each tagged with audit_type + signature. Errored audits
    are logged but don't break the others."""
    pids = list(profile_ids)
    items: List[Dict[str, Any]] = []

    from aggregate_audit import (
        audit_aggregate_drift,
        audit_account_value_parity,
        audit_account_cash_parity,
        audit_account_basis_parity,
    )
    from integrity_audit import audit_equity_identity_all

    for name, fn in (
        ("qty_parity",     audit_aggregate_drift),
        ("value_parity",   audit_account_value_parity),
        ("cash_parity",    audit_account_cash_parity),
        ("basis_parity",   audit_account_basis_parity),
    ):
        try:
            audit = fn(profile_ids=pids)
        except Exception as exc:
            logger.error(
                "audit_runner: %s raised: %s: %s",
                name, type(exc).__name__, exc,
            )
            continue
        for d in audit.get("drift", []):
            items.append({
                "audit_type": name,
                "signature": _signature(name, d),
                "details": d,
            })

    try:
        identity = audit_equity_identity_all(profile_ids=pids)
        for d in identity.get("drift", []):
            items.append({
                "audit_type": "equity_identity",
                "signature": _signature("equity_identity", d),
                "details": d,
            })
    except Exception as exc:
        logger.error(
            "audit_runner: equity_identity raised: %s: %s",
            type(exc).__name__, exc,
        )

    # Reconciler heartbeat (#170): the reconciler is what KEEPS the
    # five integrity checks meaningful. If it stopped running, all
    # the audits are reading stale state. Drift here = silent cron
    # failure, scheduler crash, host outage.
    try:
        from integrity_audit import audit_reconciler_heartbeat_all
        hb = audit_reconciler_heartbeat_all(profile_ids=pids)
        for d in hb.get("drift", []):
            items.append({
                "audit_type": "reconciler_heartbeat",
                "signature": _signature("reconciler_heartbeat", d),
                "details": d,
            })
    except Exception as exc:
        logger.error(
            "audit_runner: reconciler_heartbeat raised: %s: %s",
            type(exc).__name__, exc,
        )

    return items


def detect_and_alert_new_drift(
    profile_ids: Iterable[int] = range(1, 12),
    notify_fn=None,
    main_db: Optional[str] = None,
) -> Dict[str, Any]:
    """Run all audits, persist signatures, alert on first detection.

    notify_fn(subject, body) is called for new drift items. Defaults
    to `notifications.send_email` with the prod operator address.

    Returns: {'total': int, 'new': int, 'resolved': int,
              'new_items': [...], 'resolved_signatures': [...]}
    """
    db_path = _resolve_main_db(main_db)
    if db_path is None:
        logger.warning(
            "audit_runner: no main DB found at %s — skipping run",
            _MAIN_DB_CANDIDATES,
        )
        return {"total": 0, "new": 0, "resolved": 0,
                "new_items": [], "resolved_signatures": []}

    items = run_all_audits(profile_ids)
    current_by_sig = {it["signature"]: it for it in items}
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    with sqlite3.connect(db_path) as conn:
        _ensure_audit_alerts_table(conn)
        known = {
            r[0]: {"resolved_at": r[1], "alert_sent": r[2]}
            for r in conn.execute(
                "SELECT signature, resolved_at, alert_sent "
                "FROM audit_alerts"
            ).fetchall()
        }

        # New drift items: not in known set OR previously resolved
        # (drift came back).
        new_items: List[Dict[str, Any]] = []
        for sig, it in current_by_sig.items():
            entry = known.get(sig)
            is_new = entry is None
            is_reappeared = entry is not None and entry["resolved_at"] is not None
            if is_new or is_reappeared:
                new_items.append(it)
                conn.execute(
                    "INSERT INTO audit_alerts "
                    "(signature, audit_type, first_seen, last_seen, "
                    " resolved_at, details_json, alert_sent) "
                    "VALUES (?, ?, ?, ?, NULL, ?, 0) "
                    "ON CONFLICT(signature) DO UPDATE SET "
                    " last_seen = excluded.last_seen, "
                    " resolved_at = NULL, "
                    " details_json = excluded.details_json",
                    (sig, it["audit_type"], now_iso, now_iso,
                     json.dumps(it["details"], default=str)),
                )
            else:
                # Same signature still here — just bump last_seen.
                conn.execute(
                    "UPDATE audit_alerts SET last_seen = ? "
                    "WHERE signature = ?",
                    (now_iso, sig),
                )

        # Resolved: in known set but not in current
        resolved_sigs: List[str] = []
        for sig, entry in known.items():
            if sig in current_by_sig:
                continue
            if entry["resolved_at"] is not None:
                continue  # already marked resolved
            resolved_sigs.append(sig)
            conn.execute(
                "UPDATE audit_alerts SET resolved_at = ? "
                "WHERE signature = ?",
                (now_iso, sig),
            )

        conn.commit()

    # Alert on new items (after DB write so a notify_fn raise doesn't
    # cause us to re-alert next cycle)
    if new_items:
        _alert_new_drift(new_items, notify_fn=notify_fn, main_db=db_path)

    if resolved_sigs:
        logger.info(
            "audit_runner: %d drift item(s) resolved: %s",
            len(resolved_sigs), ", ".join(resolved_sigs),
        )

    return {
        "total": len(items),
        "new": len(new_items),
        "resolved": len(resolved_sigs),
        "new_items": new_items,
        "resolved_signatures": resolved_sigs,
    }


def _alert_new_drift(new_items: List[Dict[str, Any]],
                     notify_fn=None,
                     main_db: Optional[str] = None) -> None:
    """Send an email (or call notify_fn) listing the new drift items.
    Marks each as alert_sent=1 after a successful send."""
    if notify_fn is None:
        try:
            from notifications import send_email
            notify_fn = lambda subj, body: send_email(subj, body)
        except Exception as exc:
            logger.warning(
                "audit_runner: send_email unavailable, falling back "
                "to log-only alert: %s", exc,
            )
            notify_fn = None

    subject = f"[QuantOpsAI] {len(new_items)} new audit drift item(s)"
    lines = [
        "<h2>New integrity drift detected</h2>",
        "<p>The following items are new since the last audit run.</p>",
        "<ul>",
    ]
    for it in new_items:
        d = it["details"]
        lines.append(
            f"<li><b>{it['audit_type']}</b> — sig=<code>{it['signature']}</code>"
            f"<br>details: <code>{json.dumps(d, default=str)}</code></li>"
        )
    lines.append("</ul>")
    body = "\n".join(lines)

    sent_ok = False
    if notify_fn is not None:
        try:
            notify_fn(subject, body)
            sent_ok = True
        except Exception as exc:
            logger.error(
                "audit_runner: notify_fn raised: %s: %s",
                type(exc).__name__, exc,
            )

    if not sent_ok:
        logger.warning(
            "audit_runner: NEW DRIFT (no email delivered): %s",
            [it["signature"] for it in new_items],
        )
        return

    # Mark alerted only if delivered
    db_path = main_db or _resolve_main_db()
    if db_path is None:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            for it in new_items:
                conn.execute(
                    "UPDATE audit_alerts SET alert_sent = 1 "
                    "WHERE signature = ?",
                    (it["signature"],),
                )
            conn.commit()
    except sqlite3.OperationalError as exc:
        logger.error(
            "audit_runner: failed to mark alert_sent: %s", exc,
        )
