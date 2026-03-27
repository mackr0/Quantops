"""Execute trades using aggressive strategies for small/micro-cap paper trading.

Position sizing is more aggressive than the conservative trader:
  - 10% max position (vs 5% default)
  - STRONG_BUY -> 10%, BUY -> 7.5%
  - Tighter stop-loss at 3% (cut losses fast on volatile names)
  - Take-profit at 10%
"""

from client import get_api, get_account_info, get_positions
from portfolio_manager import check_portfolio_constraints
from journal import init_db, log_trade, log_signal
from aggressive_strategy import aggressive_combined_strategy


# ---------------------------------------------------------------------------
# Aggressive position-sizing constants
# ---------------------------------------------------------------------------
AGGRESSIVE_MAX_POSITION_PCT = 0.10   # 10% of equity
AGGRESSIVE_STOP_LOSS_PCT = 0.03      # 3%
AGGRESSIVE_TAKE_PROFIT_PCT = 0.10    # 10%


# ---------------------------------------------------------------------------
# 1. Execute a single aggressive trade
# ---------------------------------------------------------------------------

def aggressive_execute_trade(symbol, signal, max_position_pct=0.10, log=True):
    """Execute a trade with aggressive position sizing.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict (from any aggressive_strategy function).
        max_position_pct: Max fraction of equity for one position (default 10%).
        log: Whether to write to the journal database.

    Returns:
        Dict describing the action taken.
    """
    api = get_api()
    account = get_account_info(api)
    positions_list = get_positions(api)
    positions = {p["symbol"]: p for p in positions_list}

    equity = account.get("equity", 0)
    cash = account.get("cash", 0)
    action = signal.get("signal", "HOLD")
    price = signal.get("price", 0)

    result = {
        "symbol": symbol,
        "action": "NONE",
        "signal": action,
        "reason": signal.get("reason", ""),
        "strategy": "aggressive",
    }

    if log:
        init_db()

    # ---- BUY logic --------------------------------------------------------
    if action in ("BUY", "STRONG_BUY") and symbol not in positions:
        # Aggressive sizing: STRONG_BUY gets full allocation, BUY gets 75%
        if action == "STRONG_BUY":
            alloc_pct = max_position_pct          # 10%
        else:
            alloc_pct = max_position_pct * 0.75   # 7.5%

        max_dollars = equity * alloc_pct
        dollars = min(max_dollars, cash)

        if price <= 0:
            result["action"] = "SKIP"
            result["reason"] = "Invalid price"
            return result

        qty = int(dollars / price)
        if qty <= 0:
            result["action"] = "SKIP"
            result["reason"] = "Position size too small for price"
            return result

        # Portfolio constraint check (uses the *aggressive* position limit)
        proposed = {"side": "buy", "qty": qty, "price": price}
        allowed, constraint_reason = check_portfolio_constraints(
            symbol, proposed, positions, account
        )

        # If the default constraint blocks us because of the conservative
        # MAX_POSITION_PCT, we still respect other constraints (max positions,
        # cash).  The concentration check may fire — override only that one
        # by re-checking manually.
        trade_value = qty * price
        if not allowed and "exceeds" in constraint_reason and equity > 0:
            # Re-check with our aggressive limit
            if trade_value / equity <= max_position_pct:
                # Also verify cash
                if trade_value <= cash:
                    allowed = True
                    constraint_reason = "Passed aggressive constraints"

        if not allowed:
            result["action"] = "BLOCKED"
            result["reason"] = constraint_reason
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
        result["estimated_cost"] = round(qty * price, 2)
        result["stop_loss_pct"] = AGGRESSIVE_STOP_LOSS_PCT
        result["take_profit_pct"] = AGGRESSIVE_TAKE_PROFIT_PCT

        if log:
            log_trade(
                symbol=symbol,
                side="buy",
                qty=qty,
                price=price,
                order_id=order.id,
                signal_type=action,
                strategy="aggressive",
                reason=signal.get("reason"),
                stop_loss=AGGRESSIVE_STOP_LOSS_PCT,
                take_profit=AGGRESSIVE_TAKE_PROFIT_PCT,
            )

    # ---- SELL logic -------------------------------------------------------
    elif action in ("SELL", "STRONG_SELL") and symbol in positions:
        position = positions[symbol]
        qty = int(position["qty"])

        # Aggressive: STRONG_SELL dumps everything, SELL dumps 75%
        if action == "STRONG_SELL":
            sell_qty = qty
        else:
            sell_qty = max(1, int(qty * 0.75))

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
            pnl = position.get("unrealized_pl")
            if pnl is not None and qty > 0:
                pnl = float(pnl) * (sell_qty / qty)
            log_trade(
                symbol=symbol,
                side="sell",
                qty=sell_qty,
                price=price,
                order_id=order.id,
                signal_type=action,
                strategy="aggressive",
                reason=signal.get("reason"),
                pnl=pnl,
            )

    # ---- HOLD / no-action -------------------------------------------------
    elif action == "HOLD":
        result["action"] = "HOLD"
    else:
        result["action"] = "SKIP"
        if symbol in positions and "BUY" in action:
            result["reason"] = f"Already holding {symbol}"
        elif symbol not in positions and "SELL" in action:
            result["reason"] = f"No position in {symbol} to sell"

    # Log the signal regardless
    if log:
        log_signal(
            symbol=symbol,
            signal=action,
            strategy="aggressive",
            reason=signal.get("reason"),
            price=price,
            indicators={
                k: signal[k]
                for k in ("rsi", "score", "votes", "volume_ratio",
                          "gap_pct", "pct_below_sma")
                if k in signal
            },
            acted_on=result["action"] in ("BUY", "SELL"),
        )

    return result


# ---------------------------------------------------------------------------
# 2. Scan a list of candidates and trade actionable signals
# ---------------------------------------------------------------------------

def run_aggressive_scan_and_trade(candidates, max_position_pct=0.10, log=True):
    """Run aggressive_combined_strategy on every candidate and trade actionable
    signals.

    Args:
        candidates: List of ticker symbols to scan.
        max_position_pct: Per-position size cap as fraction of equity.
        log: Whether to log to the journal.

    Returns:
        Dict with summary stats and per-symbol details.
    """
    actions = []
    errors = []

    for symbol in candidates:
        try:
            signal = aggressive_combined_strategy(symbol)
            trade_result = aggressive_execute_trade(
                symbol, signal, max_position_pct=max_position_pct, log=log,
            )
            actions.append(trade_result)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    buys = [a for a in actions if a["action"] == "BUY"]
    sells = [a for a in actions if a["action"] == "SELL"]
    holds = [a for a in actions if a["action"] == "HOLD"]
    skips = [a for a in actions if a["action"] in ("SKIP", "BLOCKED", "NONE")]

    return {
        "scanned": len(candidates),
        "buys": len(buys),
        "sells": len(sells),
        "holds": len(holds),
        "skips": len(skips),
        "errors": len(errors),
        "actions": actions,
        "error_details": errors,
        "summary": (
            f"Scanned {len(candidates)} symbols: "
            f"{len(buys)} buys, {len(sells)} sells, "
            f"{len(holds)} holds, {len(skips)} skipped, "
            f"{len(errors)} errors"
        ),
    }
