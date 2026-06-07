"""Pre-expiry proactive exits for single-leg long options.

`options_lifecycle.py` handles options at EXPIRY — marks worthless
contracts closed, flags assignments. What it doesn't do (and what
this module adds) is exit single-leg long positions BEFORE expiry
when one of two rules fires:

  1. Premium-based stop — close when the current mid premium has
     dropped at least `PREMIUM_STOP_PCT` from the entry premium. A
     long call/put losing 50% of its premium is signalling either
     adverse move or IV crush, both of which compound into expiry.
     Cuts the trade before the position decays to zero.

  2. Time-based exit — close when days-to-expiry drops below
     `TIME_EXIT_DTE_THRESHOLD`. Single-leg long options take their
     largest gamma hit in the final week; the IV drop is mostly
     priced in by then and theta becomes the dominant force.

Scope: SINGLE-LEG LONGS only (`long_call`, `long_put`). Multileg
legs are managed at the spread level via `options_multileg.py`
(structural max loss caps the position), and short-premium strategies
(`covered_call`, `cash_secured_put`) want premium to DROP, not be
stopped on it.

Submission: `sell_to_close` via `options_trader.submit_option_order`.
Order type is LIMIT at the current mid (option bid-ask spreads are
wide; a market order can fill 10-20% below mid on illiquid contracts).
If the limit doesn't fill within one polling cycle, the next sweep
re-evaluates with a fresh mid — no escalation to market here. That's
the operator's choice if a position is genuinely stuck.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Tunable thresholds.
PREMIUM_STOP_PCT = 0.50          # close if mid <= entry * (1 - 0.50)
TIME_EXIT_DTE_THRESHOLD = 7      # close at <= 7 days to expiry

# Strategy whitelist — only single-leg longs are subject to these
# proactive exits. Whitelist (not blacklist) so a new multileg
# strategy added later doesn't accidentally trigger this code.
SINGLE_LEG_LONG_STRATEGIES = ("long_call", "long_put")


def _days_to_expiry(expiry: str, today: Optional[date] = None) -> Optional[int]:
    """Whole days between today and the option expiry (ISO date).
    Returns None if expiry can't be parsed."""
    if not expiry:
        return None
    try:
        exp = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    base = today or datetime.utcnow().date()
    return (exp - base).days


def find_proactive_exit_candidates(
    db_path: str,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Return open single-leg long-option rows that match either
    exit rule. Each dict has the row fields PLUS `_exit_reason`
    naming the rule that fired."""
    if not db_path:
        return []
    qmarks = ",".join("?" for _ in SINGLE_LEG_LONG_STRATEGIES)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, symbol, side, qty, fill_price, price, "
            f"order_id, signal_type, status, occ_symbol, "
            f"option_strategy, expiry, strike "
            f"FROM trades "
            f"WHERE signal_type = 'OPTIONS' "
            f"  AND status = 'open' "
            f"  AND side = 'buy' "
            f"  AND option_strategy IN ({qmarks}) "
            f"  AND occ_symbol IS NOT NULL",
            SINGLE_LEG_LONG_STRATEGIES,
        ).fetchall()
    candidates: List[Dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        # Time-based check first — cheap, no quote needed.
        dte = _days_to_expiry(row.get("expiry"), today)
        if dte is not None and dte <= TIME_EXIT_DTE_THRESHOLD:
            row["_exit_reason"] = (
                f"time_exit: {dte} days to expiry "
                f"(threshold {TIME_EXIT_DTE_THRESHOLD})"
            )
            row["_dte_at_check"] = dte
            candidates.append(row)
            continue
        # Premium-based check requires the current mid — defer to
        # caller (sweep) which has the api handle.
        row["_dte_at_check"] = dte
        candidates.append(row)
    return candidates


def _entry_premium_from_row(row: Dict[str, Any]) -> Optional[float]:
    """Best-available entry premium, preferring the actual fill over
    the decision price."""
    fp = row.get("fill_price")
    if fp is not None and fp > 0:
        return float(fp)
    p = row.get("price")
    if p is not None and p > 0:
        return float(p)
    return None


def should_close_on_premium_stop(
    entry_premium: float,
    current_mid: float,
    stop_pct: float = PREMIUM_STOP_PCT,
) -> bool:
    """True iff the current mid has dropped at least `stop_pct`
    below the entry premium. Pure compute — easy to unit-test."""
    if entry_premium <= 0 or current_mid <= 0:
        return False
    return current_mid <= entry_premium * (1.0 - stop_pct)


def sweep_proactive_option_exits(
    api,
    db_path: str,
    ctx=None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Main sweep entry. Returns a summary dict for the scheduler."""
    summary = {
        "scanned": 0,
        "time_exits_submitted": 0,
        "premium_stops_submitted": 0,
        "skipped_no_quote": 0,
        "errors": 0,
        "closed_rows": [],
    }
    try:
        from client import _fetch_option_premium
        from options_trader import submit_option_order
        from journal import log_trade
    except Exception as exc:
        logger.warning(
            "proactive option exits: import failed (%s: %s); skipping",
            type(exc).__name__, exc,
        )
        return summary

    candidates = find_proactive_exit_candidates(db_path, today=today)
    summary["scanned"] = len(candidates)
    for row in candidates:
        occ = row.get("occ_symbol")
        qty = int(row.get("qty") or 0)
        if not occ or qty <= 0:
            continue
        reason = row.get("_exit_reason")
        if reason is None:
            # No time-exit fired; check the premium stop.
            try:
                current_mid = _fetch_option_premium(api, occ, side="buy")
            except Exception as exc:
                logger.warning(
                    "proactive option exits: quote fetch failed for %s "
                    "(%s: %s); skipping this row",
                    occ, type(exc).__name__, exc,
                )
                summary["skipped_no_quote"] += 1
                continue
            if not current_mid or current_mid <= 0:
                summary["skipped_no_quote"] += 1
                continue
            entry = _entry_premium_from_row(row)
            if entry is None:
                summary["skipped_no_quote"] += 1
                continue
            if not should_close_on_premium_stop(entry, current_mid):
                continue
            reason = (
                f"premium_stop: mid {current_mid:.2f} <= "
                f"entry {entry:.2f} * (1 - {PREMIUM_STOP_PCT}) "
                f"= {entry * (1 - PREMIUM_STOP_PCT):.2f}"
            )
            limit_price = current_mid
        else:
            # Time-exit: fetch a mid to use as the limit price (still
            # better than market on a wide spread).
            try:
                limit_price = _fetch_option_premium(api, occ, side="buy")
            except Exception:
                limit_price = 0.0
        # Submit sell_to_close at the current mid. If the mid isn't
        # available, fall through to market — the position needs to
        # close (time exit or stop trigger) regardless.
        try:
            if limit_price and limit_price > 0:
                order_id = submit_option_order(
                    api, occ, side="sell", qty=qty,
                    order_type="limit", limit_price=limit_price,
                    position_intent="sell_to_close",
                )
            else:
                order_id = submit_option_order(
                    api, occ, side="sell", qty=qty,
                    order_type="market",
                    position_intent="sell_to_close",
                )
        except Exception as exc:
            logger.warning(
                "proactive option exits: order submit failed for %s "
                "(%s: %s)",
                occ, type(exc).__name__, exc,
            )
            summary["errors"] += 1
            continue
        if not order_id:
            summary["errors"] += 1
            continue
        # Journal the close as a SELL row with status=pending_fill —
        # the state machine (multi_scheduler._task_update_fills)
        # transitions to 'closed' when the broker confirms.
        try:
            log_trade(
                symbol=row.get("symbol"),
                side="sell",
                qty=qty,
                price=limit_price if limit_price and limit_price > 0 else None,
                order_id=order_id,
                signal_type="OPTIONS",
                option_strategy=row.get("option_strategy"),
                expiry=row.get("expiry"),
                strike=row.get("strike"),
                occ_symbol=occ,
                status="pending_fill",
                reason=f"proactive_exit: {reason}",
                db_path=db_path,
            )
        except Exception as exc:
            logger.warning(
                "proactive option exits: log_trade failed for %s after "
                "broker order %s placed (%s: %s) — broker order will "
                "still fill; reconciler picks up the orphan next pass",
                occ, order_id, type(exc).__name__, exc,
            )
        summary["closed_rows"].append({
            "occ_symbol": occ,
            "qty": qty,
            "reason": reason,
            "order_id": order_id,
        })
        if reason.startswith("time_exit"):
            summary["time_exits_submitted"] += 1
        else:
            summary["premium_stops_submitted"] += 1
    return summary
