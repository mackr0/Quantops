"""Execute trades based on strategy signals with risk management and journaling."""

from client import get_api, get_account_info, get_positions
from portfolio_manager import (
    calculate_position_size,
    check_portfolio_constraints,
    check_stop_loss_take_profit,
)
from journal import init_db, log_trade, log_signal


def execute_trade(symbol, signal, strategy_name="combined", log=True):
    """
    Execute a trade based on a strategy signal.

    Uses portfolio_manager for position sizing and constraint checks.
    Logs trades and signals to the journal.
    """
    api = get_api()
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
        init_db()

    if action in ("BUY", "STRONG_BUY", "WEAK_BUY") and symbol not in positions:
        # Use portfolio manager for position sizing
        sizing = calculate_position_size(symbol, signal, account, positions)
        qty = sizing["qty"]

        if qty <= 0:
            result["action"] = "SKIP"
            result["reason"] = sizing["reason"]
        else:
            # Check portfolio constraints before executing
            proposed = {"side": "buy", "qty": qty, "price": price}
            allowed, constraint_reason = check_portfolio_constraints(
                symbol, proposed, positions, account
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
                    )

    elif action in ("SELL", "STRONG_SELL", "WEAK_SELL") and symbol in positions:
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
        )

    return result


def check_exits():
    """Check all positions for stop-loss/take-profit triggers and execute sells."""
    api = get_api()
    positions = get_positions(api)

    if not positions:
        return []

    init_db()
    triggered = check_stop_loss_take_profit(positions)
    results = []

    for trigger_signal in triggered:
        symbol = trigger_signal["symbol"]
        qty = int(trigger_signal["qty"])

        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )

        log_trade(
            symbol=symbol,
            side="sell",
            qty=qty,
            price=trigger_signal["price"],
            order_id=order.id,
            signal_type="SELL",
            strategy=trigger_signal["trigger"],
            reason=trigger_signal["reason"],
        )

        results.append({
            "symbol": symbol,
            "action": "SELL",
            "qty": qty,
            "trigger": trigger_signal["trigger"],
            "reason": trigger_signal["reason"],
            "order_id": order.id,
        })

    return results
