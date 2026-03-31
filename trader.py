"""Execute trades based on strategy signals with risk management and journaling."""

from client import get_api, get_account_info, get_positions
from portfolio_manager import (
    calculate_position_size,
    check_portfolio_constraints,
    check_stop_loss_take_profit,
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
    account = get_account_info(api)
    positions_list = get_positions(api)
    positions = {p["symbol"]: p for p in positions_list}

    action = signal["signal"]
    price = signal.get("price", 0)

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
                        db_path=db_path,
                    )

    elif action in ("SELL", "STRONG_SELL", "WEAK_SELL") and symbol in positions and int(positions[symbol]["qty"]) > 0:
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

    api = get_api(ctx)
    positions = get_positions(api)

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
    triggered = check_stop_loss_take_profit(
        positions,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )
    results = []

    # Build a lookup for unrealized P&L from positions
    pnl_by_symbol = {p["symbol"]: float(p.get("unrealized_pl", 0)) for p in positions}

    for trigger_signal in triggered:
        symbol = trigger_signal["symbol"]
        qty = int(trigger_signal["qty"])
        is_short = trigger_signal.get("is_short", False)

        if is_short:
            # Close short position by buying to cover
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
            # Close long position by selling
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
            db_path=db_path,
        )

        results.append({
            "symbol": symbol,
            "action": action_label,
            "qty": qty,
            "trigger": trigger_signal["trigger"],
            "reason": trigger_signal["reason"],
            "order_id": order.id,
        })

    return results
