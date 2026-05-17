"""Capture non-trade Alpaca account activities into the journal.

Order fills (FILL/PFILL) reach the journal via `order_guard` +
`log_trade` at trade-submission time. But the broker also generates
events the journal misses by default:

  DIV    dividend cash credit
  OPEXP  option expiration (close at $0 if OTM)
  OPASN  option assignment (short option taken)
  OPXRC  option exercise (we exercised a long option)

If those events aren't reflected, broker_cash and broker_value
silently drift from journal_cash and journal_value — exactly the
class of "hidden loss/gain" the user surfaced after the 2026-05-13
cash-logic incident.

This module pulls the activity stream from Alpaca and writes
matching journal rows. Idempotency: the Alpaca activity `id` is
written as `trades.order_id` so re-running the capture never
double-writes the same event.

Schedule: hourly per-profile via multi_scheduler. Mid-day capture
catches dividends and assignments before the close-of-day audits
run, so the existing five integrity audits won't false-flag them.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# Activity types we handle. FILL/PFILL deliberately omitted — those
# are written at submit time via log_trade.
_HANDLED_TYPES = ("DIV", "OPEXP", "OPASN", "OPXRC")


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _has_activity(db_path: str, activity_id: str) -> bool:
    """Idempotency check: have we already written a journal row for
    this Alpaca activity?"""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE order_id = ? LIMIT 1",
                (activity_id,),
            ).fetchone()
            return row is not None
    except sqlite3.OperationalError as exc:
        logger.warning(
            "activities_capture: dedup check failed for %s in %s: %s",
            activity_id, db_path, exc,
        )
        return False


def _write_dividend(ctx, activity: Any) -> bool:
    """Dividend cash credit → trades row with side='dividend',
    qty=1, price=dividend_amount. Adds 'dividend' to the credit
    branch of get_virtual_account_info (see journal.py)."""
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        logger.warning("activities_capture: DIV without id, skipping")
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    symbol = (getattr(activity, "symbol", "") or "").upper()
    try:
        amount = float(getattr(activity, "net_amount", None) or
                       getattr(activity, "amount", 0) or 0)
    except (ValueError, TypeError):
        logger.warning(
            "activities_capture: DIV %s has unparseable amount", activity_id,
        )
        return False
    if amount == 0:
        return False
    ts = getattr(activity, "date", None) or _utcnow_iso()
    try:
        log_trade(
            symbol=symbol or "CASH",
            side="dividend",
            qty=1,
            price=abs(amount),
            order_id=activity_id,
            signal_type="DIVIDEND",
            strategy="alpaca_activity",
            reason=f"dividend credit {symbol or '?'} ${amount:.2f}",
            status="closed",
            pnl=amount,
            decision_price=abs(amount),
            db_path=ctx.db_path,
        )
        logger.info(
            "[%s] DIV captured: %s $%.2f (activity=%s)",
            ctx.display_name if hasattr(ctx, "display_name") else "?",
            symbol, amount, activity_id,
        )
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error(
            "activities_capture: DIV %s log_trade failed: %s",
            activity_id, exc,
        )
        return False


def _write_option_expiry_or_exercise(ctx, activity: Any) -> bool:
    """OPEXP/OPASN/OPXRC: close the option position by writing a
    SELL row at the intrinsic value (or $0 for OTM expiry).

    The resulting STOCK position from assignment/exercise arrives
    as a separate FILL activity — Alpaca splits the events. We
    handle the option-leg close here; the stock-leg fill goes
    through the normal order path via FILL capture (or via the
    aggregate_audit cycle picking up the new broker position).
    """
    from journal import log_trade
    activity_id = str(getattr(activity, "id", ""))
    if not activity_id:
        return False
    if _has_activity(ctx.db_path, activity_id):
        return False
    activity_type = getattr(activity, "activity_type", "?")
    occ_symbol = (getattr(activity, "symbol", "") or "").upper()
    try:
        qty = float(getattr(activity, "qty", 0) or 0)
        price = float(getattr(activity, "price", 0) or 0)
    except (ValueError, TypeError):
        logger.warning(
            "activities_capture: %s %s has unparseable qty/price",
            activity_type, activity_id,
        )
        return False
    if qty <= 0:
        return False
    # Extract underlying from OCC symbol (matches the de4fbed fix).
    import re
    m = re.search(r"(\d{6}[CP]\d{8})$", occ_symbol)
    if m:
        underlying = occ_symbol[:m.start()]
    else:
        underlying = occ_symbol
    try:
        log_trade(
            symbol=underlying or "?",
            side="sell",
            qty=qty,
            price=price,
            order_id=activity_id,
            signal_type=activity_type,
            strategy="alpaca_activity",
            reason=(
                f"{activity_type} {occ_symbol}: closed at ${price:.4f} "
                f"x{qty:.0f} contracts (broker-initiated)"
            ),
            occ_symbol=occ_symbol if m else None,
            status="closed",
            decision_price=price,
            db_path=ctx.db_path,
        )
        logger.info(
            "[%s] %s captured: %s @ $%.4f x%.0f (activity=%s)",
            getattr(ctx, "display_name", "?"),
            activity_type, occ_symbol, price, qty, activity_id,
        )
        return True
    except (sqlite3.OperationalError, ValueError) as exc:
        logger.error(
            "activities_capture: %s %s log_trade failed: %s",
            activity_type, activity_id, exc,
        )
        return False


def capture_activities_for_profile(ctx,
                                   since: Optional[datetime] = None,
                                   ) -> Dict[str, int]:
    """Pull non-trade activities from Alpaca since `since` (default:
    last 7 days) and write matching journal rows.

    Returns {activity_type: count_written}.
    """
    summary = {t: 0 for t in _HANDLED_TYPES}
    if since is None:
        since = datetime.now(tz=timezone.utc) - timedelta(days=7)
    try:
        api = ctx.get_alpaca_api() if hasattr(
            ctx, "get_alpaca_api") else getattr(ctx, "api", None)
        if api is None:
            from client import get_api
            api = get_api(ctx)
    except Exception as exc:
        logger.error(
            "activities_capture: get_api failed for profile %s: %s",
            getattr(ctx, "profile_id", "?"), exc,
        )
        return summary
    try:
        activities = api.get_activities(
            activity_types=",".join(_HANDLED_TYPES),
            after=since.isoformat(),
        )
    except Exception as exc:
        logger.warning(
            "activities_capture: get_activities failed for profile %s: %s",
            getattr(ctx, "profile_id", "?"), exc,
        )
        return summary

    for a in activities:
        a_type = getattr(a, "activity_type", "")
        if a_type == "DIV":
            if _write_dividend(ctx, a):
                summary["DIV"] += 1
        elif a_type in ("OPEXP", "OPASN", "OPXRC"):
            if _write_option_expiry_or_exercise(ctx, a):
                summary[a_type] += 1
        else:
            logger.debug(
                "activities_capture: ignoring unhandled type %s", a_type,
            )
    return summary


def capture_activities_for_all_profiles(profile_ids: Iterable[int],
                                        ) -> Dict[int, Dict[str, int]]:
    """Batch: capture activities for every active profile."""
    from models import build_user_context_from_profile
    out: Dict[int, Dict[str, int]] = {}
    for pid in profile_ids:
        try:
            ctx = build_user_context_from_profile(pid)
        except Exception as exc:
            logger.warning(
                "activities_capture: build_user_context_from_profile "
                "failed for %s: %s", pid, exc,
            )
            continue
        out[pid] = capture_activities_for_profile(ctx)
    return out
