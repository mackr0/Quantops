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
# Constants (defaults — overridden by ctx when available)
# ---------------------------------------------------------------------------
AGGRESSIVE_MAX_POSITION_PCT = 0.10
AGGRESSIVE_STOP_LOSS_PCT = 0.03
AGGRESSIVE_TAKE_PROFIT_PCT = 0.10
AI_MIN_CONFIDENCE = 25  # AI must be at least this confident to allow a buy (lower for paper trading)


# ---------------------------------------------------------------------------
# AI Review
# ---------------------------------------------------------------------------

def ai_review(symbol, technical_signal, ctx=None, political_context=None):
    """Ask Claude to review a proposed trade before execution.

    Records every AI prediction to the tracker for accuracy measurement.
    Returns (approved: bool, ai_result: dict).

    Parameters
    ----------
    ctx : UserContext, optional
        If provided, passes ctx to analyze_symbol and record_prediction
        for credentials and DB path.
    political_context : str, optional
        If provided (from MAGA Mode), passed through to analyze_symbol so
        Claude considers political/macro conditions.
    """
    from ai_analyst import analyze_symbol
    from ai_tracker import record_prediction, init_tracker_db

    db_path = ctx.db_path if ctx is not None else None
    init_tracker_db(db_path)

    print(f"    AI reviewing {symbol}...", end=" ", flush=True)
    ai_result = analyze_symbol(symbol, ctx=ctx, political_context=political_context)

    ai_signal = ai_result.get("signal", "HOLD").upper()
    ai_confidence = ai_result.get("confidence", 0)
    tech_signal = technical_signal.get("signal", "HOLD").upper()
    tech_direction = "BUY" if "BUY" in tech_signal else "SELL" if "SELL" in tech_signal else "HOLD"
    price = technical_signal.get("price", 0)

    # Record every AI prediction for accuracy tracking
    record_prediction(
        symbol=symbol,
        predicted_signal=ai_signal,
        confidence=ai_confidence,
        reasoning=ai_result.get("reasoning", ""),
        price_at_prediction=price,
        price_targets=ai_result.get("price_targets"),
        db_path=db_path,
    )

    # Determine the confidence threshold
    min_confidence = ctx.ai_confidence_threshold if ctx is not None else AI_MIN_CONFIDENCE

    # Approval logic for BUY trades
    if tech_direction == "BUY":
        if ai_signal == "SELL":
            print(f"VETOED (AI says SELL, confidence {ai_confidence})")
            return False, ai_result
        if ai_confidence < min_confidence and ai_signal != "BUY":
            print(f"VETOED (AI confidence {ai_confidence} < {min_confidence})")
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

def aggressive_execute_trade(symbol, signal, ctx=None, ai_result=None,
                             max_position_pct=None, log=True):
    """Execute a trade with aggressive position sizing.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict (from aggressive_combined_strategy).
        ctx: UserContext, optional.  When provided, all API calls, risk
             parameters, and journal logging use the context.
        ai_result: AI analysis dict (from ai_review). If provided, logged
                   with the trade for full audit trail.
        max_position_pct: Max fraction of equity for one position.  Falls back
                          to ctx or module constant.
        log: Whether to write to the journal database.
    """
    # Check exclusion list — symbol is analyzed but never traded
    if ctx is not None:
        from models import is_symbol_excluded
        if is_symbol_excluded(ctx.user_id, symbol):
            return {
                "symbol": symbol,
                "action": "EXCLUDED",
                "signal": signal.get("signal", "HOLD"),
                "price": signal.get("price", 0),
                "reason": f"{symbol} is on your restricted list and cannot be traded",
                "strategy": "aggressive",
            }

    # Resolve parameters from ctx, explicit arg, or module-level constants
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else AGGRESSIVE_MAX_POSITION_PCT
    stop_loss_pct = ctx.stop_loss_pct if ctx is not None else AGGRESSIVE_STOP_LOSS_PCT
    take_profit_pct = ctx.take_profit_pct if ctx is not None else AGGRESSIVE_TAKE_PROFIT_PCT
    db_path = ctx.db_path if ctx is not None else None

    api = get_api(ctx)
    account = get_account_info(api)
    positions_list = get_positions(api)

    # Filter positions to match profile's market type
    if ctx is not None:
        is_crypto = ctx.segment == "crypto"
        positions_list = [p for p in positions_list if ("/" in p["symbol"]) == is_crypto]

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
        "price": price,
        "reason": signal.get("reason", ""),
        "score": signal.get("score"),
        "ai_signal": ai_result.get("signal") if ai_result else None,
        "ai_confidence": ai_confidence,
        "ai_reasoning": ai_reasoning,
        "ai_risk_factors": ai_result.get("risk_factors", []) if ai_result else [],
        "strategy": "aggressive",
    }

    if log:
        init_db(db_path)

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

        # Portfolio constraint check — pass ctx-derived params
        max_total = ctx.max_total_positions if ctx is not None else None
        proposed = {"side": "buy", "qty": qty, "price": price}
        allowed, constraint_reason = check_portfolio_constraints(
            symbol, proposed, positions, account,
            max_position_pct=max_position_pct,
            max_total_positions=max_total,
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
        result["stop_loss_pct"] = stop_loss_pct
        result["take_profit_pct"] = take_profit_pct

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
                stop_loss=stop_loss_pct,
                take_profit=take_profit_pct,
                db_path=db_path,
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
                db_path=db_path,
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
            db_path=db_path,
        )

    return result


# ---------------------------------------------------------------------------
# Scan and trade with AI gate
# ---------------------------------------------------------------------------

def run_aggressive_scan_and_trade(candidates, ctx=None, max_position_pct=None,
                                  log=True):
    """Screen candidates with aggressive strategies, AI-review before trading.

    Pipeline for each candidate:
      1. Run aggressive_combined_strategy -> technical signal
      2. If signal is actionable (BUY/SELL), run AI review
      3. If AI approves, execute the trade
      4. If AI vetoes, skip and log the veto

    Parameters
    ----------
    ctx : UserContext, optional
        Passed through to ai_review and aggressive_execute_trade.
    max_position_pct : float, optional
        Override for position sizing.  Falls back to ctx or module constant.

    Returns summary dict with counts and details.
    """
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else AGGRESSIVE_MAX_POSITION_PCT

    # Fetch political context once for the entire scan if MAGA mode is enabled
    political_context = None
    maga_mode = ctx.maga_mode if ctx is not None else False
    if maga_mode:
        from political_sentiment import get_maga_mode_context
        print("  MAGA Mode active — fetching political context...", flush=True)
        political_context = get_maga_mode_context(ctx=ctx)
        if political_context:
            print(f"  {political_context.splitlines()[0]}")  # Print first line

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
            approved, ai_result = ai_review(symbol, signal, ctx=ctx,
                                            political_context=political_context)

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
                symbol, signal, ctx=ctx, ai_result=ai_result,
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
