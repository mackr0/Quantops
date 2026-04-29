"""Broker-managed protective stop orders.

Stage 1 of INTRADAY_STOPS_PLAN.md. Replaces the polling-and-react exit
logic for static stop-losses with broker-side stop orders that fire AT
the stop price when triggered.

Why: polling check_exits runs every 5 minutes. Between cycles, prices
can move significantly past the stop level. The polling logic submits
a market sell at the current price, which is typically far worse than
the intended stop. Real prod data: AMD stop threshold -5%, actual fill
-7.91% — a 60% overshoot.

Submitting a `type='stop'` order with `stop_price = entry × (1 - stop_loss_pct)`
makes the broker fire the moment that price is touched, regardless of
our cycle timing. Fills land at the stop price (or near it on gaps).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def submit_protective_stop(
    api,
    symbol: str,
    qty: int,
    side: str,
    stop_price: float,
) -> Optional[str]:
    """Submit a broker stop order. Returns the order_id on success, None on failure.

    Args:
      api: Alpaca REST client (from client.get_api).
      symbol: Ticker.
      qty: Absolute share count to protect.
      side: "sell" (close a long) or "buy" (cover a short).
      stop_price: Trigger price. Must be below current for sell, above for buy.

    Failure is intentionally non-fatal — if the broker rejects the order
    the polling fallback in check_exits still detects threshold breaches.
    Returns None so the caller knows there's no order_id to track.
    """
    if not symbol or qty <= 0 or stop_price <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="stop",
            stop_price=round(float(stop_price), 2),
            time_in_force="gtc",
        )
        order_id = getattr(order, "id", None)
        if order_id:
            logger.info(
                "Protective stop placed: %s %s qty=%d stop=$%.2f order_id=%s",
                side, symbol, qty, stop_price, order_id,
            )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place protective stop for %s (qty=%d, stop=$%.2f): %s",
            symbol, qty, stop_price, exc,
        )
        return None


def cancel_protective_stop(api, order_id: Optional[str]) -> bool:
    """Cancel an open broker stop order. Returns True if cancelled or already gone.

    No-op when order_id is None or empty. Treats already-filled / already-
    cancelled orders as success (the goal is reached either way).
    """
    if not order_id:
        return True
    try:
        api.cancel_order(order_id)
        logger.info("Cancelled protective stop %s", order_id)
        return True
    except Exception as exc:
        # Already-filled / already-cancelled orders raise on cancel — that's
        # not an error from our perspective; the order is no longer live.
        msg = str(exc).lower()
        if "filled" in msg or "cancel" in msg or "not found" in msg or "404" in msg:
            return True
        logger.warning("Could not cancel protective stop %s: %s", order_id, exc)
        return False


def stop_price_for_entry(
    entry_price: float,
    stop_loss_pct: float,
    is_short: bool,
) -> Optional[float]:
    """Compute the appropriate stop_price given entry and risk parameters.

    Long: entry × (1 - stop_loss_pct). Stop fires when price falls.
    Short: entry × (1 + stop_loss_pct). Stop fires when price rises.

    Returns None on invalid inputs (zero entry, missing pct).
    """
    if not entry_price or entry_price <= 0:
        return None
    if stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    if is_short:
        return entry_price * (1 + stop_loss_pct)
    return entry_price * (1 - stop_loss_pct)


def _is_order_active(api, order_id: str) -> bool:
    """Return True iff the order is still working at the broker. Fail-open
    on lookup errors — we'd rather submit a duplicate than leave a position
    unprotected because the API blipped."""
    if not order_id:
        return False
    try:
        order = api.get_order(order_id)
    except Exception:
        return False
    status = (getattr(order, "status", "") or "").lower()
    return status in ("new", "accepted", "pending_new", "held",
                       "accepted_for_bidding")


def ensure_protective_stops(api, positions, ctx, db_path):
    """Sweep all open positions and place a broker stop order on any
    position lacking an active one.

    Called from trader.check_exits each cycle. Idempotent — verifies
    the stored protective_stop_order_id is still working before
    deciding to submit a new one. Survives restarts (positions
    created before restart get protected on the next sweep) and
    races (entry path's own placement is best-effort).
    """
    import sqlite3
    if not db_path or not positions:
        return
    sl_pct_long = getattr(ctx, "stop_loss_pct", None) if ctx else None
    sl_pct_short = (getattr(ctx, "short_stop_loss_pct", None) or sl_pct_long
                     if ctx else None)
    if not sl_pct_long and not sl_pct_short:
        return

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return

    try:
        for pos in positions:
            symbol = pos.get("symbol")
            qty = float(pos.get("qty", 0))
            entry_price = float(pos.get("avg_entry_price", 0))
            if not symbol or qty == 0 or entry_price <= 0:
                continue

            is_short = qty < 0
            entry_side_in_db = "short" if is_short else "buy"
            row = conn.execute(
                "SELECT id, protective_stop_order_id FROM trades "
                "WHERE symbol = ? AND side = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (symbol, entry_side_in_db),
            ).fetchone()
            if not row:
                continue

            existing_id = row["protective_stop_order_id"]
            if existing_id and _is_order_active(api, existing_id):
                continue  # already protected

            sl_pct = sl_pct_short if is_short else sl_pct_long
            stop_price = stop_price_for_entry(entry_price, sl_pct, is_short)
            if stop_price is None:
                continue

            close_side = "buy" if is_short else "sell"
            order_id = submit_protective_stop(
                api, symbol, abs(int(qty)), close_side, stop_price,
            )
            if order_id:
                try:
                    conn.execute(
                        "UPDATE trades SET protective_stop_order_id = ? "
                        "WHERE id = ?",
                        (order_id, row["id"]),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Stop placed but couldn't store order_id: %s "
                        "(symbol=%s)", exc, symbol,
                    )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_for_symbol(api, db_path: str, symbol: str) -> None:
    """Cancel any active protective stop order for the given symbol.

    Called before a manual exit (AI SELL, polling-triggered exit, etc.)
    so the broker stop doesn't fire AFTER our market sell on a now-flat
    position. The matching trade row's protective_stop_order_id is
    cleared either way (cancel succeeded, or the order is already gone).
    """
    import sqlite3
    if not db_path or not symbol:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, protective_stop_order_id FROM trades "
            "WHERE symbol = ? AND status = 'open' "
            "AND protective_stop_order_id IS NOT NULL",
            (symbol,),
        ).fetchall()
        for r in rows:
            cancel_protective_stop(api, r["protective_stop_order_id"])
            conn.execute(
                "UPDATE trades SET protective_stop_order_id = NULL "
                "WHERE id = ?",
                (r["id"],),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("cancel_for_symbol(%s) skipped: %s", symbol, exc)
