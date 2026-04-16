"""In-process event bus — Phase 9 of the Quant Fund Evolution roadmap.

Real funds react to events within seconds: earnings announcements, SEC
filings, price shocks, insider filings, macro shocks. Our previous
architecture scanned on a 15-minute timer — which means a material
8-K filed at 9:31 didn't influence decisions until 9:45. This module
closes that window.

Design:
  * `emit(conn, type, symbol, severity, payload, dedup_key)` inserts
    a new row in the `events` table. Duplicate dedup_keys are silently
    dropped (UNIQUE constraint).
  * `subscribe(handler, event_types)` registers an in-process callback
    for one or more event types. Subscriptions are held in module state
    — each scheduler process builds its own subscription set at startup.
  * `dispatch_pending(conn, ctx, limit)` pulls every unhandled event
    (handled_at IS NULL), calls each subscribed handler, records the
    handler results, and marks the event handled.

The bus is deliberately in-process: no Redis / Kafka / external broker.
SQLite with WAL mode is fast enough for our scale (hundreds of events
per hour) and keeps deployment simple. A future migration to an external
broker would only change the persistence layer — handler and detector
APIs stay the same.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module state — subscription registry.
# ---------------------------------------------------------------------------

# Map of event_type -> list of handler callables.
# Each handler: `handler(event_row: dict, ctx) -> dict` (result payload)
_SUBSCRIPTIONS: Dict[str, List[Callable]] = {}


def subscribe(handler: Callable, event_types: Iterable[str]) -> None:
    """Register `handler` to run for every emitted event of the given types."""
    for t in event_types:
        _SUBSCRIPTIONS.setdefault(t, []).append(handler)


def clear_subscriptions() -> None:
    """Wipe all subscriptions — used by tests and at scheduler startup."""
    _SUBSCRIPTIONS.clear()


def handlers_for(event_type: str) -> List[Callable]:
    return list(_SUBSCRIPTIONS.get(event_type, []))


# ---------------------------------------------------------------------------
# Emit / dispatch
# ---------------------------------------------------------------------------

VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")


def emit(
    db_path: str,
    type_: str,
    symbol: Optional[str] = None,
    severity: str = "info",
    payload: Optional[Dict[str, Any]] = None,
    dedup_key: Optional[str] = None,
) -> Optional[int]:
    """Insert a new event. Returns the row id or None if dedupe blocked it."""
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"invalid severity {severity!r}")
    # Default dedup: (type, symbol, day). Caller can override for finer grain.
    if dedup_key is None:
        import datetime as _dt
        today = _dt.datetime.utcnow().strftime("%Y%m%d")
        dedup_key = f"{type_}:{symbol or '-'}:{today}"

    conn = sqlite3.connect(db_path)
    try:
        try:
            cur = conn.execute(
                """INSERT INTO events (type, symbol, severity, payload_json, dedup_key)
                   VALUES (?, ?, ?, ?, ?)""",
                (type_, symbol, severity,
                 json.dumps(payload or {}), dedup_key),
            )
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # Dedup key collision — event already emitted
            return None
    finally:
        conn.close()


def dispatch_pending(db_path: str, ctx: Any, limit: int = 20) -> Dict[str, Any]:
    """Call every handler on every unhandled event, up to `limit`.

    Returns a summary: {dispatched, handler_errors, by_type}.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary = {"dispatched": 0, "handler_errors": 0, "by_type": {}}
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE handled_at IS NULL "
            "ORDER BY detected_at ASC LIMIT ?",
            (limit,),
        ).fetchall()

        for row in rows:
            ev = dict(row)
            try:
                ev["payload"] = json.loads(ev.get("payload_json") or "{}")
            except Exception:
                ev["payload"] = {}

            results: List[Dict[str, Any]] = []
            for handler in handlers_for(ev["type"]):
                try:
                    result = handler(ev, ctx) or {}
                except Exception as exc:
                    summary["handler_errors"] += 1
                    logger.warning(
                        "event handler %s failed on %s:%s — %s",
                        getattr(handler, "__name__", "?"),
                        ev["type"], ev.get("symbol"), exc,
                    )
                    result = {"error": str(exc)}
                results.append({
                    "handler": getattr(handler, "__name__", "?"),
                    "result": result,
                })

            conn.execute(
                "UPDATE events SET handled_at = datetime('now'), "
                "handler_results_json = ? WHERE id = ?",
                (json.dumps(results), ev["id"]),
            )
            conn.commit()
            summary["dispatched"] += 1
            summary["by_type"][ev["type"]] = summary["by_type"].get(ev["type"], 0) + 1
    finally:
        conn.close()

    return summary


def recent_events(db_path: str, hours: int = 24,
                  limit: int = 100) -> List[Dict[str, Any]]:
    """Fetch recent events for dashboard/debug use."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM events "
            "WHERE detected_at >= datetime('now', ?) "
            "ORDER BY detected_at DESC LIMIT ?",
            (f"-{hours} hours", limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            try:
                d["handler_results"] = json.loads(d.get("handler_results_json") or "[]")
            except Exception:
                d["handler_results"] = []
            out.append(d)
        return out
    finally:
        conn.close()
