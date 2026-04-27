"""Execute trades based on strategy signals with risk management and journaling."""

from client import get_api, get_account_info, get_positions
from portfolio_manager import (
    calculate_position_size,
    check_portfolio_constraints,
    check_stop_loss_take_profit,
    check_trailing_stops,
)
from journal import init_db, log_trade, log_signal


def execute_trade(symbol, signal, ctx=None, strategy_name="combined", log=True):
    """
    Execute a trade based on a strategy signal.

    Uses portfolio_manager for position sizing and constraint checks.
    Logs trades and signals to the journal.

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, uses ctx for API credentials, risk parameters,
        and journal DB path.
    """
    # Check exclusion list
    if ctx is not None:
        from models import is_symbol_excluded
        if is_symbol_excluded(ctx.user_id, symbol):
            return {
                "symbol": symbol,
                "action": "EXCLUDED",
                "signal": signal.get("signal", "HOLD"),
                "reason": f"{symbol} is on your restricted list and cannot be traded",
            }

    # Resolve ctx-derived parameters
    db_path = ctx.db_path if ctx is not None else None
    max_position_pct = ctx.max_position_pct if ctx is not None else None
    max_total_positions = ctx.max_total_positions if ctx is not None else None

    api = get_api(ctx)
    account = get_account_info(api, ctx=ctx)
    positions_list = get_positions(api, ctx=ctx)
    positions = {p["symbol"]: p for p in positions_list}

    action = signal["signal"]
    price = signal.get("price", 0)

    if price <= 0 and action in ("BUY", "STRONG_BUY", "WEAK_BUY", "SELL", "STRONG_SELL"):
        try:
            from market_data import get_bars
            bars = get_bars(symbol, limit=1)
            if bars is not None and not bars.empty:
                price = float(bars.iloc[-1]["close"])
                signal["price"] = price
        except Exception:
            pass

    result = {
        "symbol": symbol,
        "action": "NONE",
        "signal": action,
        "reason": signal.get("reason", ""),
    }

    if log:
        init_db(db_path)

    if action in ("BUY", "STRONG_BUY", "WEAK_BUY") and symbol not in positions:
        # Use portfolio manager for position sizing
        sizing = calculate_position_size(
            symbol, signal, account, positions,
            max_position_pct=max_position_pct,
        )
        qty = sizing["qty"]

        if qty <= 0:
            result["action"] = "SKIP"
            result["reason"] = sizing["reason"]
        else:
            # Check portfolio constraints before executing
            proposed = {"side": "buy", "qty": qty, "price": price}
            allowed, constraint_reason = check_portfolio_constraints(
                symbol, proposed, positions, account,
                max_position_pct=max_position_pct,
                max_total_positions=max_total_positions,
            )

            if not allowed:
                result["action"] = "BLOCKED"
                result["reason"] = constraint_reason
            else:
                from order_guard import check_can_submit
                if not check_can_submit(ctx, symbol, "buy"):
                    result["action"] = "SKIP"
                    result["reason"] = "Order blocked: outside trading window"
                    return result
                order = api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day",
                )
                result["action"] = "BUY"
                result["qty"] = qty
                result["order_id"] = order.id
                result["estimated_cost"] = qty * price
                result["sizing"] = sizing["reason"]

                if log:
                    log_trade(
                        symbol=symbol,
                        side="buy",
                        qty=qty,
                        price=price,
                        order_id=order.id,
                        signal_type=action,
                        strategy=strategy_name,
                        reason=signal.get("reason"),
                        ai_reasoning=signal.get("ai_raw_reasoning"),
                        ai_confidence=signal.get("confidence"),
                        decision_price=price,
                        db_path=db_path,
                    )

    elif action in ("SELL", "STRONG_SELL", "WEAK_SELL") and symbol in positions and int(positions[symbol]["qty"]) > 0:
        from order_guard import check_can_submit
        if not check_can_submit(ctx, symbol, "sell"):
            result["action"] = "SKIP"
            result["reason"] = "Order blocked: outside trading window"
            return result

        position = positions[symbol]
        qty = int(position["qty"])

        if action == "STRONG_SELL":
            sell_qty = qty
        elif action == "SELL":
            sell_qty = max(1, int(qty * 0.75))
        else:
            sell_qty = max(1, int(qty * 0.5))

        order = api.submit_order(
            symbol=symbol,
            qty=sell_qty,
            side="sell",
            type="market",
            time_in_force="day",
        )
        result["action"] = "SELL"
        result["qty"] = sell_qty
        result["order_id"] = order.id

        if log:
            pnl = position["unrealized_pl"] * (sell_qty / qty) if qty > 0 else None
            log_trade(
                symbol=symbol,
                side="sell",
                qty=sell_qty,
                price=price,
                order_id=order.id,
                signal_type=action,
                strategy=strategy_name,
                reason=signal.get("reason"),
                pnl=pnl,
                decision_price=price,
                db_path=db_path,
            )

    elif action == "HOLD":
        result["action"] = "HOLD"

    else:
        result["action"] = "SKIP"
        if symbol in positions and "BUY" in action:
            result["reason"] = f"Already holding {symbol}"
        elif symbol not in positions and "SELL" in action:
            result["reason"] = f"No position in {symbol} to sell"

    # Log the signal regardless of action
    if log:
        log_signal(
            symbol=symbol,
            signal=action,
            strategy=strategy_name,
            reason=signal.get("reason"),
            price=price,
            indicators={
                k: signal[k] for k in ("rsi", "sma_short", "sma_long", "confidence")
                if k in signal
            },
            acted_on=result["action"] in ("BUY", "SELL"),
            db_path=db_path,
        )

    return result


def _entry_order_filled_at_broker(api, db_path, symbol, is_short):
    """Return True iff the most-recent matching entry order for this
    symbol has actually filled at Alpaca (i.e. shares exist).

    Why this exists: virtual profiles using LIMIT entry orders compute
    "open positions" from the journal as soon as the order is logged,
    even before Alpaca fills it. If `check_exits` then submits a SELL
    against zero real shares, Alpaca interprets the SELL as a short and
    rejects it because the long BUY is still open
    ("cannot open a short sell while a long buy order is open" — see
    CHANGELOG 2026-04-27). This helper is the gate that makes
    check_exits Alpaca-state-aware.

    Returns True (allow exit) on any uncertain path — journal lookup
    failure, missing order_id, broker doesn't recognize the id —
    because being too-conservative here would block legitimate exits
    on positions whose entry rows lost their order_id link. Only an
    explicit "this entry is still pending at the broker" answer
    blocks the exit.
    """
    if not db_path:
        return True
    entry_side = "sell_short" if is_short else "buy"
    try:
        import sqlite3 as _sqlite
        conn = _sqlite.connect(db_path)
        row = conn.execute(
            "SELECT order_id FROM trades "
            "WHERE symbol=? AND side=? AND status='open' "
            "ORDER BY id DESC LIMIT 1",
            (symbol, entry_side),
        ).fetchone()
        conn.close()
    except Exception:
        return True
    if not row or not row[0]:
        return True
    try:
        order = api.get_order(row[0])
    except Exception:
        # Alpaca doesn't recognize the id (already canceled/replaced
        # /cleaned up). Don't gate the exit on a stale lookup.
        return True
    status = (getattr(order, "status", "") or "").lower()
    # Alpaca order states that mean shares actually exist:
    #   "filled", "partially_filled" (some shares present)
    # Everything else (new/accepted/pending_*/held/canceled/etc.)
    # means no shares — the SELL would be interpreted as a short.
    return status in ("filled", "partially_filled")


def check_exits(ctx=None):
    """Check all positions for stop-loss/take-profit triggers and execute sells.

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, uses ctx for API credentials, risk parameters,
        and journal DB path.
    """
    # Resolve ctx-derived parameters
    db_path = ctx.db_path if ctx is not None else None
    stop_loss_pct = ctx.stop_loss_pct if ctx is not None else None
    take_profit_pct = ctx.take_profit_pct if ctx is not None else None
    short_stop_loss_pct = getattr(ctx, "short_stop_loss_pct", None) if ctx is not None else None
    short_take_profit_pct = getattr(ctx, "short_take_profit_pct", None) if ctx is not None else None

    api = get_api(ctx)
    positions = get_positions(api, ctx=ctx)

    if not positions:
        return []

    # Filter positions to match the profile's market type
    # Crypto profiles only manage crypto positions (symbol contains '/')
    # Equity profiles only manage stock positions (no '/')
    if ctx is not None:
        is_crypto = ctx.segment == "crypto"
        positions = [
            p for p in positions
            if ("/" in p["symbol"]) == is_crypto
        ]

    if not positions:
        return []

    init_db(db_path)

    # Update max-favorable-excursion (MFE) on every open position
    # using the current Alpaca-reported price. The trailing-stop
    # tuner reads MFE on closed trades to compute "give-back" =
    # MFE - exit_price; tighter trailing on names that consistently
    # give back too much. Cheap (1 UPDATE per held symbol). Any
    # failure is swallowed so MFE tracking can never break exits.
    if db_path and positions:
        try:
            import sqlite3 as _sqlite_mfe
            mfe_conn = _sqlite_mfe.connect(db_path)
            for p in positions:
                sym = p.get("symbol")
                cur_price = float(p.get("current_price") or 0)
                if not sym or cur_price <= 0:
                    continue
                # Long: MFE = highest price seen. Short: MFE = lowest.
                if float(p.get("qty", 0)) < 0:
                    mfe_conn.execute(
                        "UPDATE trades SET max_favorable_excursion = "
                        "MIN(COALESCE(max_favorable_excursion, ?), ?) "
                        "WHERE symbol = ? AND side = 'sell_short' "
                        "AND status = 'open'",
                        (cur_price, cur_price, sym),
                    )
                else:
                    mfe_conn.execute(
                        "UPDATE trades SET max_favorable_excursion = "
                        "MAX(COALESCE(max_favorable_excursion, ?), ?) "
                        "WHERE symbol = ? AND side = 'buy' "
                        "AND status = 'open'",
                        (cur_price, cur_price, sym),
                    )
            mfe_conn.commit()
            mfe_conn.close()
        except Exception as _exc:
            logging.debug("MFE update skipped (non-fatal): %s", _exc)

    # Conviction-based take-profit override: build the skip predicate if
    # the profile has it enabled. Runaway winners (IONQ-style) keep running
    # while the trailing stop manages the exit, instead of being capped.
    conviction_tp_skip = None
    if ctx is not None and getattr(ctx, "use_conviction_tp_override", False):
        from conviction_tp import build_conviction_skip
        conviction_tp_skip = build_conviction_skip(ctx, db_path)

    triggered = check_stop_loss_take_profit(
        positions,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        short_stop_loss_pct=short_stop_loss_pct,
        short_take_profit_pct=short_take_profit_pct,
        conviction_tp_skip=conviction_tp_skip,
    )

    # Trailing stops: check profitable positions for trailing stop triggers
    if ctx is not None and getattr(ctx, "use_trailing_stops", False):
        # Don't trail symbols already triggered by regular stop/TP
        already_triggered = {t["symbol"] for t in triggered}
        trailing_candidates = [p for p in positions if p["symbol"] not in already_triggered]
        trailing_triggered = check_trailing_stops(trailing_candidates, ctx)
        triggered.extend(trailing_triggered)

    results = []

    # Build a lookup for unrealized P&L from positions
    pnl_by_symbol = {p["symbol"]: float(p.get("unrealized_pl", 0)) for p in positions}

    for trigger_signal in triggered:
        symbol = trigger_signal["symbol"]
        qty = int(trigger_signal["qty"])
        is_short = trigger_signal.get("is_short", False)

        # Schedule guard: don't submit exit orders outside the profile's
        # trading window. Stop-loss/take-profit triggers will re-fire on
        # the next check cycle within schedule.
        from order_guard import check_can_submit
        exit_side = "buy" if is_short else "sell"
        if not check_can_submit(ctx, symbol, exit_side):
            continue

        # Broker-state guard: skip the exit if the underlying entry
        # order is still pending at Alpaca (e.g. an unfilled limit
        # buy). Submitting a SELL against zero real shares makes Alpaca
        # treat it as a short attempt and reject with "cannot open a
        # short sell while a long buy order is open."
        if not _entry_order_filled_at_broker(api, db_path, symbol, is_short):
            logging.info(
                "Deferring exit for %s: entry order has not filled at "
                "the broker yet. Will retry on next exit cycle.",
                symbol,
            )
            continue

        # Cancel any open orders for this symbol before submitting the exit.
        # Alpaca rejects sells when a buy limit order is still open.
        try:
            open_orders = api.list_orders(status="open", symbols=[symbol])
            for oo in open_orders:
                try:
                    api.cancel_order(oo.id)
                    logging.info(f"Cancelled conflicting order {oo.id} for {symbol} before exit")
                except Exception:
                    pass
        except Exception:
            pass

        if is_short:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                type="market",
                time_in_force="day",
            )
            side_label = "cover"
            action_label = "COVER"
        else:
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                type="market",
                time_in_force="day",
            )
            side_label = "sell"
            action_label = "SELL"

        pnl = pnl_by_symbol.get(symbol)
        # Subtract accrued short-borrow cost on covers (overnight
        # shorts pay a daily borrow fee that Alpaca's unrealized_pl
        # doesn't reflect). Sub-1-day shorts get 0.0 — same-day cover
        # has no overnight borrow charge.
        if is_short and pnl is not None:
            try:
                from short_borrow import accrue_for_cover
                borrow_cost = accrue_for_cover(db_path, symbol, qty)
                if borrow_cost > 0:
                    pnl = pnl - borrow_cost
                    logging.info(
                        "Short borrow cost on %s: $%.4f (subtracted from "
                        "cover pnl)", symbol, borrow_cost,
                    )
            except Exception as _exc:
                logging.debug("Borrow accrual failed for %s: %s", symbol, _exc)

        log_trade(
            symbol=symbol,
            side=side_label,
            qty=qty,
            price=trigger_signal["price"],
            order_id=order.id,
            signal_type="SELL",
            strategy=trigger_signal["trigger"],
            reason=trigger_signal["reason"],
            pnl=pnl,
            # Exit-fired orders realize P&L → the row is closed, not open.
            # Matching BUY rows get reconciled below.
            status="closed" if pnl is not None else "open",
            decision_price=trigger_signal["price"],
            db_path=db_path,
        )

        # Mark any still-open BUY rows for this symbol as closed — the
        # exit has flattened the position. Without this the trades page
        # shows the old entry as "open" forever.
        try:
            import sqlite3 as _sqlite3
            _c = _sqlite3.connect(db_path) if db_path else _sqlite3.connect("journal.db")
            _c.execute(
                "UPDATE trades SET status='closed' "
                "WHERE symbol=? AND side='buy' AND status='open'",
                (symbol,),
            )
            _c.commit()
            _c.close()
        except Exception as _exc:
            # Reconciliation is best-effort — never block the exit path.
            pass

        results.append({
            "symbol": symbol,
            "action": action_label,
            "qty": qty,
            "trigger": trigger_signal["trigger"],
            "reason": trigger_signal["reason"],
            "order_id": order.id,
        })

    return results
