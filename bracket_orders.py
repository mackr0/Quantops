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


def tp_price_for_entry(
    entry_price: float,
    take_profit_pct: float,
    is_short: bool,
) -> Optional[float]:
    """Compute the take-profit limit price.

    Long: entry × (1 + take_profit_pct). Limit fires when price hits target.
    Short: entry × (1 - take_profit_pct).
    """
    if not entry_price or entry_price <= 0:
        return None
    if take_profit_pct is None or take_profit_pct <= 0:
        return None
    if is_short:
        return entry_price * (1 - take_profit_pct)
    return entry_price * (1 + take_profit_pct)


def submit_protective_take_profit(
    api,
    symbol: str,
    qty: int,
    side: str,
    limit_price: float,
) -> Optional[str]:
    """Submit a broker limit order to lock in profit at a target level.

    Use type='limit' (not stop) — fills only when price meets or beats
    the target. Won't slip past the limit on gaps; will simply not fill
    if the target is never reached. Pairs with the protective stop on
    the downside.
    """
    if not symbol or qty <= 0 or limit_price <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="limit",
            limit_price=round(float(limit_price), 2),
            time_in_force="gtc",
        )
        order_id = getattr(order, "id", None)
        if order_id:
            logger.info(
                "Protective take-profit placed: %s %s qty=%d limit=$%.2f order_id=%s",
                side, symbol, qty, limit_price, order_id,
            )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place protective take-profit for %s (qty=%d, limit=$%.2f): %s",
            symbol, qty, limit_price, exc,
        )
        return None


# Bounds on trail percent to avoid stops that are too tight (whipsaw
# on normal volatility) or too loose (defeats the purpose).
TRAIL_PERCENT_MIN = 2.0
TRAIL_PERCENT_MAX = 10.0


def trail_percent_for_entry(stop_loss_pct: float) -> Optional[float]:
    """Convert the profile's stop_loss_pct to an Alpaca trail_percent.

    Uses the same percent the user accepts for the static stop. If
    stop_loss_pct=0.05, the trail follows the high water at 5% below.
    Clamped to [2%, 10%] so we don't get tight-stop whipsaws on
    high-vol names or worthless 20% trails on low-vol names.

    Returns None on invalid inputs.
    """
    if stop_loss_pct is None or stop_loss_pct <= 0:
        return None
    pct = stop_loss_pct * 100.0
    return max(TRAIL_PERCENT_MIN, min(TRAIL_PERCENT_MAX, pct))


def submit_protective_trailing(
    api,
    symbol: str,
    qty: int,
    side: str,
    trail_percent: float,
) -> Optional[str]:
    """Submit a broker trailing-stop order.

    Alpaca tracks the high water continuously and adjusts the stop level
    to (high - trail_percent% × high). When price falls through the
    level, fires a market order. This eliminates the polling lag that
    caused IBM-style "intraday spike then EOD collapse" giveback.

    side='sell' for long position close, 'buy' for short cover.
    """
    if not symbol or qty <= 0 or trail_percent <= 0 or side not in ("sell", "buy"):
        return None
    try:
        order = api.submit_order(
            symbol=symbol,
            qty=int(qty),
            side=side,
            type="trailing_stop",
            trail_percent=str(round(float(trail_percent), 2)),
            time_in_force="gtc",
        )
        order_id = getattr(order, "id", None)
        if order_id:
            logger.info(
                "Protective trailing stop placed: %s %s qty=%d trail=%.2f%% order_id=%s",
                side, symbol, qty, trail_percent, order_id,
            )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place protective trailing stop for %s "
            "(qty=%d, trail=%.2f%%): %s",
            symbol, qty, trail_percent, exc,
        )
        return None


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


def ensure_protective_stops(api, positions, ctx, db_path,
                              conviction_tp_skip=None):
    """Sweep all open positions and place broker protective orders on
    any position lacking active ones.

    Called from trader.check_exits each cycle. Idempotent — verifies
    the stored protective_stop_order_id / protective_tp_order_id is
    still working before deciding to submit a new one. Survives
    restarts (positions created before restart get protected on the
    next sweep) and races (entry path's own placement is best-effort).

    conviction_tp_skip: optional callable(symbol, pct_change) -> bool.
    When truthy for a position, skip placing the take-profit order so
    runaway winners aren't capped (matches the existing polling
    semantics for take_profit_pct override).
    """
    import sqlite3
    if not db_path or not positions:
        return
    sl_pct_long = getattr(ctx, "stop_loss_pct", None) if ctx else None
    sl_pct_short = (getattr(ctx, "short_stop_loss_pct", None) or sl_pct_long
                     if ctx else None)
    tp_pct_long = getattr(ctx, "take_profit_pct", None) if ctx else None
    tp_pct_short = (getattr(ctx, "short_take_profit_pct", None) or tp_pct_long
                     if ctx else None)
    if not sl_pct_long and not sl_pct_short and not tp_pct_long and not tp_pct_short:
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
                "SELECT id, protective_stop_order_id, protective_tp_order_id, "
                "protective_trailing_order_id "
                "FROM trades "
                "WHERE symbol = ? AND side = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (symbol, entry_side_in_db),
            ).fetchone()
            if not row:
                continue

            close_side = "buy" if is_short else "sell"
            abs_qty = abs(int(qty))

            # ---- Stop-loss ----
            existing_stop_id = row["protective_stop_order_id"]
            if not (existing_stop_id and _is_order_active(api, existing_stop_id)):
                sl_pct = sl_pct_short if is_short else sl_pct_long
                stop_price = stop_price_for_entry(entry_price, sl_pct, is_short)
                if stop_price is not None:
                    order_id = submit_protective_stop(
                        api, symbol, abs_qty, close_side, stop_price,
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

            # ---- Take-profit ----
            # Skip TP placement when conviction-override would skip the
            # exit anyway. Without this, runaway winners would still be
            # capped at +take_profit_pct, defeating the override.
            if conviction_tp_skip is not None:
                try:
                    cur_price = float(pos.get("current_price") or 0)
                    pct_change = ((cur_price - entry_price) / entry_price
                                   if entry_price > 0 and cur_price > 0 else 0)
                    if conviction_tp_skip(symbol, pct_change):
                        continue
                except Exception:
                    pass

            existing_tp_id = row["protective_tp_order_id"]
            if existing_tp_id and _is_order_active(api, existing_tp_id):
                continue  # TP already in place

            tp_pct = tp_pct_short if is_short else tp_pct_long
            limit_price = tp_price_for_entry(entry_price, tp_pct, is_short)
            if limit_price is None:
                continue

            order_id = submit_protective_take_profit(
                api, symbol, abs_qty, close_side, limit_price,
            )
            if order_id:
                try:
                    conn.execute(
                        "UPDATE trades SET protective_tp_order_id = ? "
                        "WHERE id = ?",
                        (order_id, row["id"]),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Take-profit placed but couldn't store order_id: %s "
                        "(symbol=%s)", exc, symbol,
                    )

            # ---- Trailing stop ----
            # Only place when the profile uses trailing stops. Skipped
            # if conviction-override fires (let runners run, no trail
            # cap either — same logic as TP).
            use_trailing = (getattr(ctx, "use_trailing_stops", False)
                             if ctx else False)
            if not use_trailing:
                continue

            existing_trail_id = row["protective_trailing_order_id"]
            if existing_trail_id and _is_order_active(api, existing_trail_id):
                continue

            sl_pct_for_trail = sl_pct_short if is_short else sl_pct_long
            trail_pct = trail_percent_for_entry(sl_pct_for_trail)
            if trail_pct is None:
                continue

            order_id = submit_protective_trailing(
                api, symbol, abs_qty, close_side, trail_pct,
            )
            if order_id:
                try:
                    conn.execute(
                        "UPDATE trades SET protective_trailing_order_id = ? "
                        "WHERE id = ?",
                        (order_id, row["id"]),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "Trailing stop placed but couldn't store order_id: %s "
                        "(symbol=%s)", exc, symbol,
                    )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cancel_for_symbol(api, db_path: str, symbol: str) -> None:
    """Cancel any active protective stop / take-profit / trailing-stop
    orders for the given symbol.

    Called before a manual exit (AI SELL, polling-triggered exit, etc.)
    so the broker orders don't fire AFTER our market sell on a now-flat
    position. The matching trade row's protective_*_order_id columns
    are cleared either way (cancel succeeded, or the order is already gone).
    """
    import sqlite3
    if not db_path or not symbol:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, protective_stop_order_id, protective_tp_order_id, "
            "protective_trailing_order_id "
            "FROM trades "
            "WHERE symbol = ? AND status = 'open' "
            "AND (protective_stop_order_id IS NOT NULL "
            "     OR protective_tp_order_id IS NOT NULL "
            "     OR protective_trailing_order_id IS NOT NULL)",
            (symbol,),
        ).fetchall()
        for r in rows:
            if r["protective_stop_order_id"]:
                cancel_protective_stop(api, r["protective_stop_order_id"])
            if r["protective_tp_order_id"]:
                cancel_protective_stop(api, r["protective_tp_order_id"])
            if r["protective_trailing_order_id"]:
                cancel_protective_stop(api, r["protective_trailing_order_id"])
            conn.execute(
                "UPDATE trades SET protective_stop_order_id = NULL, "
                "protective_tp_order_id = NULL, "
                "protective_trailing_order_id = NULL "
                "WHERE id = ?",
                (r["id"],),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("cancel_for_symbol(%s) skipped: %s", symbol, exc)
