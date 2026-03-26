"""Execute trades based on strategy signals."""

from client import get_api, get_account_info, get_positions


def execute_trade(symbol, signal, max_position_pct=0.05):
    """
    Execute a trade based on a strategy signal.

    Args:
        symbol: Stock ticker
        signal: Strategy signal dict with 'signal' key
        max_position_pct: Max % of portfolio to allocate per position (default 5%)
    """
    api = get_api()
    account = get_account_info(api)
    positions = {p["symbol"]: p for p in get_positions(api)}

    action = signal["signal"]
    price = signal.get("price", 0)

    result = {
        "symbol": symbol,
        "action": "NONE",
        "signal": action,
        "reason": signal["reason"],
    }

    if action in ("BUY", "STRONG_BUY", "WEAK_BUY") and symbol not in positions:
        # Calculate position size
        max_dollars = account["equity"] * max_position_pct
        if action == "STRONG_BUY":
            dollars = max_dollars
        elif action == "BUY":
            dollars = max_dollars * 0.75
        else:  # WEAK_BUY
            dollars = max_dollars * 0.5

        if price > 0:
            qty = int(dollars / price)
        else:
            qty = 1

        if qty > 0:
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
        else:
            result["action"] = "SKIP"
            result["reason"] = "Position size too small"

    elif action in ("SELL", "STRONG_SELL", "WEAK_SELL") and symbol in positions:
        position = positions[symbol]
        qty = int(position["qty"])

        if action == "STRONG_SELL":
            sell_qty = qty  # Sell all
        elif action == "SELL":
            sell_qty = max(1, int(qty * 0.75))
        else:  # WEAK_SELL
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

    elif action == "HOLD":
        result["action"] = "HOLD"

    else:
        result["action"] = "SKIP"
        if symbol in positions and "BUY" in action:
            result["reason"] = f"Already holding {symbol}"
        elif symbol not in positions and "SELL" in action:
            result["reason"] = f"No position in {symbol} to sell"

    return result
