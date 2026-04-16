"""Event detectors — poll state and emit events when triggers fire.

Each detector is a pure function of `ctx` + database state; it emits one
or more events via `event_bus.emit`. Dedup keys keep a single firing
trigger from emitting on every poll.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types — the closed set handlers can subscribe to.
# ---------------------------------------------------------------------------

EVENT_SEC_FILING = "sec_filing_detected"
EVENT_EARNINGS_IMMINENT = "earnings_imminent"
EVENT_PRICE_SHOCK = "price_shock"
EVENT_BIG_WINNER = "prediction_big_winner"
EVENT_BIG_LOSER = "prediction_big_loser"
EVENT_STRATEGY_DEPRECATED = "strategy_deprecated"

ALL_EVENT_TYPES = (
    EVENT_SEC_FILING,
    EVENT_EARNINGS_IMMINENT,
    EVENT_PRICE_SHOCK,
    EVENT_BIG_WINNER,
    EVENT_BIG_LOSER,
    EVENT_STRATEGY_DEPRECATED,
)


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

def detect_sec_filings(ctx: Any) -> int:
    """Emit an event for each high-severity SEC filing alert detected since last poll.

    Reuses Phase 4's `sec_filings_history` table — a filing alert with
    severity in {high, medium} that was analyzed in the last 6 hours is
    treated as fresh.
    """
    from event_bus import emit

    emitted = 0
    conn = sqlite3.connect(ctx.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT symbol, form_type, filed_date, alert_severity,
                      alert_signal, alert_summary, accession_number
               FROM sec_filings_history
               WHERE analyzed_at >= datetime('now', '-6 hours')
                 AND alert_severity IN ('high', 'medium')""",
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        dedup = f"{EVENT_SEC_FILING}:{r['symbol']}:{r['accession_number']}"
        eid = emit(
            ctx.db_path, EVENT_SEC_FILING,
            symbol=r["symbol"],
            severity=r["alert_severity"] or "medium",
            payload={
                "form_type": r["form_type"],
                "filed_date": r["filed_date"],
                "signal": r["alert_signal"],
                "summary": (r["alert_summary"] or "")[:500],
            },
            dedup_key=dedup,
        )
        if eid is not None:
            emitted += 1
    return emitted


def detect_earnings_imminent(ctx: Any) -> int:
    """Emit an event for each held position with earnings in the next 24h."""
    from event_bus import emit
    from earnings_calendar import check_earnings

    emitted = 0
    try:
        from client import get_positions
        positions = get_positions(ctx=ctx) or []
    except Exception:
        positions = []

    today = _dt.datetime.utcnow().date()
    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym or "/" in sym:  # skip crypto
            continue
        try:
            info = check_earnings(sym) or {}
            days = info.get("days_until")
        except Exception:
            continue
        if days is None or days < 0 or days > 1:
            continue
        dedup = f"{EVENT_EARNINGS_IMMINENT}:{sym}:{today}"
        eid = emit(
            ctx.db_path, EVENT_EARNINGS_IMMINENT,
            symbol=sym, severity="medium",
            payload={"days_until": int(days)},
            dedup_key=dedup,
        )
        if eid is not None:
            emitted += 1
    return emitted


def detect_price_shocks(ctx: Any, threshold_pct: float = 5.0,
                        volume_mult: float = 2.0) -> int:
    """Emit an event for held symbols that moved >threshold on elevated volume."""
    from event_bus import emit
    from market_data import get_bars

    emitted = 0
    try:
        from client import get_positions
        positions = get_positions(ctx=ctx) or []
    except Exception:
        positions = []

    today = _dt.datetime.utcnow().strftime("%Y%m%d")
    for pos in positions:
        sym = pos.get("symbol", "")
        if not sym:
            continue
        try:
            df = get_bars(sym, limit=25)
            if df is None or len(df) < 10:
                continue
            today_close = float(df["close"].iloc[-1])
            yday_close = float(df["close"].iloc[-2])
            if yday_close <= 0:
                continue
            move_pct = (today_close - yday_close) / yday_close * 100
            vol = float(df["volume"].iloc[-1])
            avg_vol = float(df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else 0
        except Exception:
            continue

        if abs(move_pct) < threshold_pct:
            continue
        if avg_vol > 0 and vol < avg_vol * volume_mult:
            continue

        severity = "high" if abs(move_pct) >= 10 else "medium"
        dedup = f"{EVENT_PRICE_SHOCK}:{sym}:{today}"
        eid = emit(
            ctx.db_path, EVENT_PRICE_SHOCK,
            symbol=sym, severity=severity,
            payload={
                "move_pct": round(move_pct, 2),
                "volume": vol,
                "avg_volume": avg_vol,
                "volume_ratio": round(vol / avg_vol, 2) if avg_vol > 0 else None,
            },
            dedup_key=dedup,
        )
        if eid is not None:
            emitted += 1
    return emitted


def detect_big_resolved_predictions(ctx: Any,
                                     win_threshold_pct: float = 15.0,
                                     loss_threshold_pct: float = -15.0) -> int:
    """Emit winners/losers among predictions resolved in the last 24h."""
    from event_bus import emit

    emitted = 0
    conn = sqlite3.connect(ctx.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, symbol, predicted_signal, actual_return_pct,
                      strategy_type, resolved_at
               FROM ai_predictions
               WHERE resolved_at >= datetime('now', '-24 hours')
                 AND actual_return_pct IS NOT NULL""",
        ).fetchall()
    finally:
        conn.close()

    for r in rows:
        ret = float(r["actual_return_pct"] or 0)
        if ret >= win_threshold_pct:
            ev_type = EVENT_BIG_WINNER
            severity = "high" if ret >= 25 else "medium"
        elif ret <= loss_threshold_pct:
            ev_type = EVENT_BIG_LOSER
            severity = "high" if ret <= -25 else "medium"
        else:
            continue
        dedup = f"{ev_type}:{r['symbol']}:{r['id']}"
        eid = emit(
            ctx.db_path, ev_type,
            symbol=r["symbol"], severity=severity,
            payload={
                "return_pct": round(ret, 2),
                "strategy": r["strategy_type"],
                "signal": r["predicted_signal"],
                "resolved_at": r["resolved_at"],
                "prediction_id": r["id"],
            },
            dedup_key=dedup,
        )
        if eid is not None:
            emitted += 1
    return emitted


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def run_all_detectors(ctx: Any) -> Dict[str, int]:
    """Run every detector and return per-detector emission counts."""
    counts: Dict[str, int] = {}
    for name, fn in (
        ("sec_filings", detect_sec_filings),
        ("earnings_imminent", detect_earnings_imminent),
        ("price_shocks", detect_price_shocks),
        ("big_resolved_predictions", detect_big_resolved_predictions),
    ):
        try:
            counts[name] = fn(ctx)
        except Exception as exc:
            logger.warning("detector %s failed: %s", name, exc)
            counts[name] = -1
    return counts
