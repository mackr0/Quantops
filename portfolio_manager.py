"""Portfolio-level risk management and position sizing."""

import config


def calculate_position_size(symbol, signal, account_info, current_positions):
    """Calculate the number of shares to trade based on signal strength and risk limits.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict with keys 'signal', 'price', and optionally
                'ai_confidence' (0-1).
        account_info: Dict with at least 'equity' and 'cash'.
        current_positions: Dict mapping symbol -> position dict (with 'market_value').

    Returns:
        dict with 'qty', 'dollars', 'reason'.
    """
    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)
    price = signal.get("price", 0)
    action = signal.get("signal", "HOLD")
    ai_confidence = signal.get("ai_confidence")

    if price <= 0:
        return {"qty": 0, "dollars": 0, "reason": "Invalid price"}

    # Base allocation as fraction of max position
    max_dollars = equity * config.MAX_POSITION_PCT

    signal_multipliers = {
        "STRONG_BUY": 1.0,
        "BUY": 0.75,
        "WEAK_BUY": 0.5,
    }

    multiplier = signal_multipliers.get(action, 0)
    if multiplier == 0:
        return {"qty": 0, "dollars": 0, "reason": f"Signal '{action}' is not a buy signal"}

    # Scale by AI confidence if available
    if ai_confidence is not None and 0 < ai_confidence <= 1:
        multiplier *= ai_confidence

    dollars = max_dollars * multiplier

    # Don't exceed available cash
    dollars = min(dollars, cash)

    qty = int(dollars / price)
    actual_dollars = qty * price

    if qty <= 0:
        return {"qty": 0, "dollars": 0, "reason": "Position size too small for price"}

    return {
        "qty": qty,
        "dollars": actual_dollars,
        "reason": f"{action} -> {multiplier:.0%} of max position (${max_dollars:,.0f})",
    }


def check_portfolio_constraints(symbol, proposed_trade, current_positions, account_info):
    """Check whether a proposed trade is allowed under portfolio risk rules.

    Args:
        symbol: Ticker string.
        proposed_trade: Dict with 'side', 'qty', 'price'.
        current_positions: Dict mapping symbol -> position dict.
        account_info: Dict with 'equity', 'cash'.

    Returns:
        (allowed: bool, reason: str)
    """
    side = proposed_trade.get("side", "").lower()
    qty = proposed_trade.get("qty", 0)
    price = proposed_trade.get("price", 0)
    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)

    # Sells are always allowed (reduces risk)
    if side == "sell":
        return True, "Sell orders are always permitted"

    if qty <= 0:
        return False, "Quantity must be positive"

    # Check max total positions
    num_current = len(current_positions)
    if symbol not in current_positions and num_current >= config.MAX_TOTAL_POSITIONS:
        return False, (
            f"Already at max positions ({config.MAX_TOTAL_POSITIONS}). "
            f"Close a position before opening a new one."
        )

    # Check single-position concentration
    trade_value = qty * price
    if equity > 0 and trade_value / equity > config.MAX_POSITION_PCT:
        max_allowed = equity * config.MAX_POSITION_PCT
        return False, (
            f"Trade value ${trade_value:,.2f} exceeds {config.MAX_POSITION_PCT:.0%} "
            f"of equity (max ${max_allowed:,.2f})"
        )

    # Check if adding to an existing position would breach the limit
    if symbol in current_positions:
        existing_value = abs(float(current_positions[symbol].get("market_value", 0)))
        combined = existing_value + trade_value
        if equity > 0 and combined / equity > config.MAX_POSITION_PCT:
            return False, (
                f"Combined position ${combined:,.2f} would exceed "
                f"{config.MAX_POSITION_PCT:.0%} of equity"
            )

    # Check sufficient cash
    if trade_value > cash:
        return False, (
            f"Insufficient cash: need ${trade_value:,.2f}, have ${cash:,.2f}"
        )

    return True, "Trade passes all portfolio constraints"


def check_stop_loss_take_profit(positions):
    """Check all open positions against stop-loss and take-profit thresholds.

    Args:
        positions: List of position dicts, each with at least:
            'symbol', 'current_price', 'avg_entry_price', 'qty',
            and optionally 'stop_loss', 'take_profit'.

    Returns:
        List of sell signal dicts for positions that have triggered, each with:
        'symbol', 'signal', 'reason', 'price', 'qty', 'trigger'.
    """
    triggered = []

    for pos in positions:
        symbol = pos.get("symbol")
        current_price = float(pos.get("current_price", 0))
        entry_price = float(pos.get("avg_entry_price", 0))
        qty = pos.get("qty", 0)

        if entry_price <= 0 or current_price <= 0:
            continue

        pct_change = (current_price - entry_price) / entry_price

        # Use per-position thresholds if set, otherwise use config defaults
        stop_loss_pct = pos.get("stop_loss") or config.DEFAULT_STOP_LOSS_PCT
        take_profit_pct = pos.get("take_profit") or config.DEFAULT_TAKE_PROFIT_PCT

        if pct_change <= -stop_loss_pct:
            triggered.append({
                "symbol": symbol,
                "signal": "SELL",
                "reason": (
                    f"Stop-loss triggered: {pct_change:+.2%} "
                    f"(threshold -{stop_loss_pct:.0%})"
                ),
                "price": current_price,
                "qty": qty,
                "trigger": "stop_loss",
            })
        elif pct_change >= take_profit_pct:
            triggered.append({
                "symbol": symbol,
                "signal": "SELL",
                "reason": (
                    f"Take-profit triggered: {pct_change:+.2%} "
                    f"(threshold +{take_profit_pct:.0%})"
                ),
                "price": current_price,
                "qty": qty,
                "trigger": "take_profit",
            })

    return triggered


def get_risk_summary(account_info, positions):
    """Compute portfolio-level risk metrics.

    Args:
        account_info: Dict with 'equity', 'cash'.
        positions: List of position dicts with 'symbol', 'market_value',
                   'unrealized_pl', 'qty', 'current_price', 'avg_entry_price'.

    Returns:
        Dict with concentration and risk figures.
    """
    equity = account_info.get("equity", 0)
    cash = account_info.get("cash", 0)
    num_positions = len(positions)

    total_invested = sum(abs(float(p.get("market_value", 0))) for p in positions)
    total_unrealized_pnl = sum(float(p.get("unrealized_pl", 0)) for p in positions)

    cash_pct = (cash / equity * 100) if equity > 0 else 0
    invested_pct = (total_invested / equity * 100) if equity > 0 else 0

    # Per-position breakdown
    position_weights = {}
    largest_position = {"symbol": None, "weight": 0}
    for p in positions:
        symbol = p.get("symbol")
        mv = abs(float(p.get("market_value", 0)))
        weight = (mv / equity * 100) if equity > 0 else 0
        position_weights[symbol] = weight
        if weight > largest_position["weight"]:
            largest_position = {"symbol": symbol, "weight": weight}

    # Slots remaining
    available_slots = max(0, config.MAX_TOTAL_POSITIONS - num_positions)

    return {
        "equity": equity,
        "cash": cash,
        "cash_pct": round(cash_pct, 2),
        "total_invested": round(total_invested, 2),
        "invested_pct": round(invested_pct, 2),
        "num_positions": num_positions,
        "max_positions": config.MAX_TOTAL_POSITIONS,
        "available_slots": available_slots,
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "position_weights": {k: round(v, 2) for k, v in position_weights.items()},
        "largest_position": largest_position,
        "max_position_pct": config.MAX_POSITION_PCT * 100,
    }
