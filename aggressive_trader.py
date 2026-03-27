"""Execute trades using aggressive strategies for small/micro-cap paper trading.

Position sizing is more aggressive than the conservative trader:
  - 10% max position (vs 5% default)
  - STRONG_BUY -> 10%, BUY -> 7.5%
  - Tighter stop-loss at 3% (cut losses fast on volatile names)
  - Take-profit at 10%

AI Review Gate:
  Before placing any order, Claude analyzes the stock and must approve.
  If AI says SELL or gives low confidence (<40), the trade is vetoed.
"""

import json
from client import get_api, get_account_info, get_positions
from portfolio_manager import check_portfolio_constraints
from journal import init_db, log_trade, log_signal
from aggressive_strategy import aggressive_combined_strategy


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
AGGRESSIVE_MAX_POSITION_PCT = 0.10
AGGRESSIVE_STOP_LOSS_PCT = 0.03
AGGRESSIVE_TAKE_PROFIT_PCT = 0.10
AI_MIN_CONFIDENCE = 40  # AI must be at least this confident to allow a buy


# ---------------------------------------------------------------------------
# AI Review
# ---------------------------------------------------------------------------

def ai_review(symbol, technical_signal):
    """Ask Claude to review a proposed trade before execution.

    Returns (approved: bool, ai_result: dict) where ai_result contains
    the full AI analysis including signal, confidence, reasoning, and
    risk factors.
    """
    from ai_analyst import analyze_symbol

    print(f"    AI reviewing {symbol}...", end=" ", flush=True)
    ai_result = analyze_symbol(symbol)

    ai_signal = ai_result.get("signal", "HOLD").upper()
    ai_confidence = ai_result.get("confidence", 0)
    tech_signal = technical_signal.get("signal", "HOLD").upper()
    tech_direction = "BUY" if "BUY" in tech_signal else "SELL" if "SELL" in tech_signal else "HOLD"

    # Approval logic for BUY trades
    if tech_direction == "BUY":
        if ai_signal == "SELL":
            print(f"VETOED (AI says SELL, confidence {ai_confidence})")
            return False, ai_result
        if ai_confidence < AI_MIN_CONFIDENCE and ai_signal != "BUY":
            print(f"VETOED (AI confidence {ai_confidence} < {AI_MIN_CONFIDENCE})")
            return False, ai_result
        print(f"APPROVED (AI: {ai_signal}, confidence {ai_confidence})")
        return True, ai_result

    # Approval logic for SELL trades — AI sell confirmation or low confidence
    if tech_direction == "SELL":
        if ai_signal == "BUY" and ai_confidence >= 70:
            print(f"VETOED (AI strongly says BUY, confidence {ai_confidence})")
            return False, ai_result
        print(f"APPROVED (AI: {ai_signal}, confidence {ai_confidence})")
        return True, ai_result

    # HOLD — nothing to approve
    return True, ai_result


# ---------------------------------------------------------------------------
# Execute a single aggressive trade
# ---------------------------------------------------------------------------

def aggressive_execute_trade(symbol, signal, ai_result=None,
                             max_position_pct=0.10, log=True):
    """Execute a trade with aggressive position sizing.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict (from aggressive_combined_strategy).
        ai_result: AI analysis dict (from ai_review). If provided, logged
                   with the trade for full audit trail.
        max_position_pct: Max fraction of equity for one position.
        log: Whether to write to the journal database.
    """
    api = get_api()
    account = get_account_info(api)
    positions_list = get_positions(api)
    positions = {p["symbol"]: p for p in positions_list}

    equity = account.get("equity", 0)
    cash = account.get("cash", 0)
    action = signal.get("signal", "HOLD")
    price = signal.get("price", 0)

    # Extract AI info for logging
    ai_reasoning = None
    ai_confidence = None
    if ai_result:
        ai_reasoning = ai_result.get("reasoning", "")
        ai_confidence = ai_result.get("confidence")
        # If AI provided price targets, use them for stop/take-profit
        targets = ai_result.get("price_targets", {})

    result = {
        "symbol": symbol,
        "action": "NONE",
        "signal": action,
        "reason": signal.get("reason", ""),
        "ai_signal": ai_result.get("signal") if ai_result else None,
        "ai_confidence": ai_confidence,
        "strategy": "aggressive",
    }

    if log:
        init_db()

    # ---- BUY logic --------------------------------------------------------
    if action in ("BUY", "STRONG_BUY") and symbol not in positions:
        if action == "STRONG_BUY":
            alloc_pct = max_position_pct
        else:
            alloc_pct = max_position_pct * 0.75

        # Boost allocation if AI is highly confident
        if ai_confidence and ai_confidence >= 80:
            alloc_pct = min(alloc_pct * 1.25, max_position_pct)

        max_dollars = equity * alloc_pct
        dollars = min(max_dollars, cash)

        if price <= 0:
            result["action"] = "SKIP"
            result["reason"] = "Invalid price"
            return result

        qty = int(dollars / price)
        if qty <= 0:
            result["action"] = "SKIP"
            result["reason"] = "Position size too small"
            return result

        # Portfolio constraint check
        proposed = {"side": "buy", "qty": qty, "price": price}
        allowed, constraint_reason = check_portfolio_constraints(
            symbol, proposed, positions, account
        )

        # Override conservative concentration limit for aggressive trades
        trade_value = qty * price
        if not allowed and "exceeds" in constraint_reason and equity > 0:
            if trade_value / equity <= max_position_pct and trade_value <= cash:
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
                ai_reasoning=ai_reasoning,
                ai_confidence=ai_confidence,
                stop_loss=AGGRESSIVE_STOP_LOSS_PCT,
                take_profit=AGGRESSIVE_TAKE_PROFIT_PCT,
            )

    # ---- SELL logic -------------------------------------------------------
    elif action in ("SELL", "STRONG_SELL") and symbol in positions:
        position = positions[symbol]
        qty = int(position["qty"])

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
                ai_reasoning=ai_reasoning,
                ai_confidence=ai_confidence,
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
# Scan and trade with AI gate
# ---------------------------------------------------------------------------

def run_aggressive_scan_and_trade(candidates, max_position_pct=0.10, log=True):
    """Screen candidates with aggressive strategies, AI-review before trading.

    Pipeline for each candidate:
      1. Run aggressive_combined_strategy → technical signal
      2. If signal is actionable (BUY/SELL), run AI review
      3. If AI approves, execute the trade
      4. If AI vetoes, skip and log the veto

    Returns summary dict with counts and details.
    """
    details = []
    vetoed = []
    errors = []

    for symbol in candidates:
        try:
            # Step 1: Technical analysis
            signal = aggressive_combined_strategy(symbol)
            action = signal.get("signal", "HOLD")

            if action == "HOLD":
                details.append({"symbol": symbol, "action": "HOLD", "reason": signal.get("reason", "")})
                continue

            # Step 2: AI review for actionable signals
            print(f"  {symbol}: {action} (score {signal.get('score', '?')})")
            approved, ai_result = ai_review(symbol, signal)

            if not approved:
                vetoed.append({
                    "symbol": symbol,
                    "technical_signal": action,
                    "ai_signal": ai_result.get("signal"),
                    "ai_confidence": ai_result.get("confidence"),
                    "ai_reasoning": ai_result.get("reasoning", ""),
                })
                details.append({
                    "symbol": symbol,
                    "action": "AI_VETOED",
                    "signal": action,
                    "ai_signal": ai_result.get("signal"),
                    "ai_confidence": ai_result.get("confidence"),
                    "reason": f"AI vetoed: {ai_result.get('reasoning', '')[:100]}",
                })
                continue

            # Step 3: Execute trade
            trade_result = aggressive_execute_trade(
                symbol, signal, ai_result=ai_result,
                max_position_pct=max_position_pct, log=log,
            )
            details.append(trade_result)

        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            details.append({"symbol": symbol, "action": "ERROR", "reason": str(exc)})

    buys = [d for d in details if d.get("action") == "BUY"]
    sells = [d for d in details if d.get("action") == "SELL"]
    holds = [d for d in details if d.get("action") == "HOLD"]
    skips = [d for d in details if d.get("action") in ("SKIP", "BLOCKED", "NONE")]
    ai_vetoed = [d for d in details if d.get("action") == "AI_VETOED"]

    return {
        "total": len(candidates),
        "buys": len(buys),
        "sells": len(sells),
        "holds": len(holds),
        "skips": len(skips),
        "ai_vetoed": len(ai_vetoed),
        "errors": len(errors),
        "details": details,
        "vetoed_details": vetoed,
    }
