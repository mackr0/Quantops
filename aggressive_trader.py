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
import logging
from client import get_api, get_account_info, get_positions
from portfolio_manager import check_portfolio_constraints, check_drawdown, calculate_atr_stops
from journal import init_db, log_trade, log_signal
from aggressive_strategy import aggressive_combined_strategy
from strategy_router import run_strategy


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
    from ai_analyst import analyze_symbol, analyze_symbol_consensus
    from ai_tracker import record_prediction, init_tracker_db

    db_path = ctx.db_path if ctx is not None else None
    init_tracker_db(db_path)

    print(f"    AI reviewing {symbol}...", end=" ", flush=True)

    # Use consensus analysis if enabled
    use_consensus = ctx is not None and getattr(ctx, "enable_consensus", False)
    if use_consensus:
        ai_result = analyze_symbol_consensus(symbol, ctx=ctx, political_context=political_context)
    else:
        ai_result = analyze_symbol(symbol, ctx=ctx, political_context=political_context)

    ai_signal = ai_result.get("signal", "HOLD").upper()
    ai_confidence = ai_result.get("confidence", 0)
    tech_signal = technical_signal.get("signal", "HOLD").upper()
    tech_direction = "BUY" if "BUY" in tech_signal else "SELL" if "SELL" in tech_signal else "HOLD"
    price = technical_signal.get("price", 0)

    # Consensus veto: if consensus was sought and models disagree, veto the trade
    if use_consensus and ai_result.get("consensus") is False:
        primary = ai_result.get("primary_signal", "?")
        secondary = ai_result.get("secondary_signal", "?")
        secondary_model = ai_result.get("secondary_model", "unknown")
        print(f"VETOED (No consensus — primary says {primary}, "
              f"secondary ({secondary_model}) says {secondary})")
        return False, ai_result

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
                             max_position_pct=None, log=True,
                             _account=None, _positions_list=None, _dd=None):
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
        _account: Pre-fetched account info dict. If None, fetched fresh.
        _positions_list: Pre-fetched positions list. If None, fetched fresh.
        _dd: Pre-fetched drawdown dict. If None, computed fresh.
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

    # Earnings calendar check — skip stocks reporting earnings soon
    # (When called from run_aggressive_scan_and_trade, this is already
    # handled by pre-filtering. This check remains for direct callers.)
    if ctx is not None and _positions_list is None:
        try:
            avoid_days = getattr(ctx, "avoid_earnings_days", 2)
            if avoid_days > 0:
                from earnings_calendar import check_earnings
                earnings = check_earnings(symbol)
                if earnings and earnings["days_until"] <= avoid_days:
                    return {
                        "symbol": symbol,
                        "action": "EARNINGS_SKIP",
                        "signal": signal.get("signal", "HOLD"),
                        "price": signal.get("price", 0),
                        "reason": f"Skipping {symbol}: earnings in {earnings['days_until']} day(s) (on {earnings['earnings_date']})",
                        "strategy": "aggressive",
                    }
        except Exception as _earn_exc:
            # Never block a trade due to earnings lookup failure
            pass

    # Resolve parameters from ctx, explicit arg, or module-level constants
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else AGGRESSIVE_MAX_POSITION_PCT
    stop_loss_pct = ctx.stop_loss_pct if ctx is not None else AGGRESSIVE_STOP_LOSS_PCT
    take_profit_pct = ctx.take_profit_pct if ctx is not None else AGGRESSIVE_TAKE_PROFIT_PCT
    db_path = ctx.db_path if ctx is not None else None

    api = get_api(ctx)

    # Use pre-fetched data if available, otherwise fetch fresh
    account = _account if _account is not None else get_account_info(api)

    # --- Drawdown protection ---
    dd = _dd if _dd is not None else {"action": "normal", "drawdown_pct": 0.0, "peak_equity": 0, "current_equity": 0}
    if ctx is not None and _dd is None:
        dd = check_drawdown(ctx, account, db_path=db_path)
        print(f"    Drawdown: {dd['drawdown_pct']:.1f}% (peak ${dd['peak_equity']:,.0f}, current ${dd['current_equity']:,.0f}) -> {dd['action']}")
        if dd["action"] == "pause":
            return {
                "symbol": symbol,
                "action": "DRAWDOWN_PAUSE",
                "signal": signal.get("signal", "HOLD"),
                "price": signal.get("price", 0),
                "reason": f"Trading paused: {dd['drawdown_pct']:.1f}% drawdown exceeds {ctx.drawdown_pause_pct*100:.0f}% threshold",
                "strategy": "aggressive",
            }

    if _positions_list is not None:
        positions_list = _positions_list
    else:
        positions_list = get_positions(api)
        # Filter positions to match profile's market type
        if ctx is not None:
            is_crypto = ctx.segment == "crypto"
            positions_list = [p for p in positions_list if ("/" in p["symbol"]) == is_crypto]

    # --- Correlation check ---
    correlation_reduce = False
    if ctx is not None and hasattr(ctx, "max_correlation"):
        try:
            from correlation import check_correlation
            corr_result = check_correlation(
                symbol, positions_list,
                max_correlation=ctx.max_correlation,
            )
            if not corr_result.get("allowed", True):
                correlation_reduce = True
                print(f"    Correlation warning: {corr_result.get('reason', 'too correlated')} — reducing position size 50%")
        except Exception as _corr_exc:
            # Never block a trade due to correlation check failure
            pass

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

        # Drawdown reduce: halve position size when in drawdown
        if ctx is not None and dd["action"] == "reduce":
            qty = max(1, int(qty * 0.5))
            print(f"    Drawdown reduce: halved qty to {qty}")

        # Correlation reduce: halve position size when correlated
        if correlation_reduce:
            qty = max(1, int(qty * 0.5))
            print(f"    Correlation reduce: halved qty to {qty}")

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

        # ATR-based stops: calculate volatility-adapted stop/TP levels
        actual_sl_pct = stop_loss_pct
        actual_tp_pct = take_profit_pct
        if ctx is not None and getattr(ctx, "use_atr_stops", False) and price > 0:
            atr_sl_mult = getattr(ctx, "atr_multiplier_sl", 2.0)
            atr_tp_mult = getattr(ctx, "atr_multiplier_tp", 3.0)
            atr_stop, atr_tp, atr_val = calculate_atr_stops(
                symbol, price, atr_sl_mult, atr_tp_mult)
            if atr_stop is not None:
                # Convert ATR prices to percentage equivalents
                actual_sl_pct = round((price - atr_stop) / price, 4)
                actual_tp_pct = round((atr_tp - price) / price, 4)
                print(f"    ATR stops for {symbol}: SL ${atr_stop:.2f} ({actual_sl_pct:.1%}), "
                      f"TP ${atr_tp:.2f} ({actual_tp_pct:.1%}), ATR ${atr_val:.2f}")

        # Limit orders: use limit price at current price for better fills
        use_limit = ctx is not None and getattr(ctx, "use_limit_orders", False)
        order_type = "limit" if use_limit else "market"
        order_kwargs = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy",
            "type": order_type,
            "time_in_force": "day",
        }
        if use_limit:
            order_kwargs["limit_price"] = str(round(price, 2))
        order = api.submit_order(**order_kwargs)

        result["action"] = "BUY"
        result["qty"] = qty
        result["order_id"] = order.id
        result["estimated_cost"] = round(qty * price, 2)
        result["stop_loss_pct"] = actual_sl_pct
        result["take_profit_pct"] = actual_tp_pct

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
                stop_loss=actual_sl_pct,
                take_profit=actual_tp_pct,
                db_path=db_path,
            )

    # ---- SELL logic (close existing long position) ---------------------------
    elif action in ("SELL", "STRONG_SELL") and symbol in positions and int(positions[symbol]["qty"]) > 0:
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

    # ---- SHORT SELL logic (open new short position) -------------------------
    elif action in ("SELL", "STRONG_SELL") and symbol not in positions:
        # Only if short selling is enabled for this profile
        enable_shorts = ctx.enable_short_selling if ctx is not None else False
        if not enable_shorts:
            result["action"] = "SKIP"
            result["reason"] = f"SELL signal on {symbol} but short selling is disabled"
        else:
            # Only short on bounce days (stock is up intraday) — don't short into weakness
            try:
                from market_data import get_bars
                bars = get_bars(symbol, limit=5)
                if not bars.empty:
                    latest = bars.iloc[-1]
                    day_change = (float(latest["close"]) - float(latest["open"])) / float(latest["open"]) * 100
                    if day_change < 0:
                        result["action"] = "SKIP"
                        result["reason"] = f"Short skipped: {symbol} is down {day_change:.1f}% today (only short on bounce days)"
                        if log:
                            init_db(db_path)
                            log_signal(
                                symbol=symbol, signal=action, strategy="aggressive",
                                reason=result["reason"], price=price,
                                indicators={k: signal[k] for k in ("rsi", "score", "votes", "volume_ratio", "gap_pct", "pct_below_sma") if k in signal},
                                acted_on=False, db_path=db_path,
                            )
                        return result
            except Exception:
                pass  # If we can't check, proceed
            # Position sizing same as BUY but for short
            if action == "STRONG_SELL":
                alloc_pct = max_position_pct
            else:
                alloc_pct = max_position_pct * 0.75

            # Boost if AI confident
            if ai_confidence and ai_confidence >= 80:
                alloc_pct = min(alloc_pct * 1.25, max_position_pct)

            max_dollars = equity * alloc_pct
            dollars = min(max_dollars, cash)

            if price <= 0:
                result["action"] = "SKIP"
                result["reason"] = "Invalid price"
            else:
                qty = int(dollars / price)

                # Drawdown reduce: halve position size when in drawdown
                if ctx is not None and dd["action"] == "reduce":
                    qty = max(1, int(qty * 0.5))
                    print(f"    Drawdown reduce: halved short qty to {qty}")

                # Correlation reduce: halve position size when correlated
                if correlation_reduce:
                    qty = max(1, int(qty * 0.5))
                    print(f"    Correlation reduce: halved short qty to {qty}")

                if qty <= 0:
                    result["action"] = "SKIP"
                    result["reason"] = "Position size too small"
                else:
                    # Use short-specific stop-loss/take-profit (wider stops for shorts)
                    short_sl = getattr(ctx, "short_stop_loss_pct", 0.08) if ctx is not None else AGGRESSIVE_STOP_LOSS_PCT
                    short_tp = getattr(ctx, "short_take_profit_pct", 0.08) if ctx is not None else AGGRESSIVE_TAKE_PROFIT_PCT

                    # ATR-based stops for shorts
                    if ctx is not None and getattr(ctx, "use_atr_stops", False) and price > 0:
                        atr_sl_mult = getattr(ctx, "atr_multiplier_sl", 2.0)
                        atr_tp_mult = getattr(ctx, "atr_multiplier_tp", 3.0)
                        atr_stop, atr_tp, atr_val = calculate_atr_stops(
                            symbol, price, atr_sl_mult, atr_tp_mult)
                        if atr_stop is not None:
                            # For shorts: stop is above entry, TP is below
                            short_sl = round((atr_tp - price) / price, 4)  # distance above
                            short_tp = round((price - atr_stop) / price, 4)  # distance below
                            print(f"    ATR stops for {symbol} (short): "
                                  f"SL +{short_sl:.1%}, TP -{short_tp:.1%}, ATR ${atr_val:.2f}")

                    # Limit orders for short entries
                    use_limit = ctx is not None and getattr(ctx, "use_limit_orders", False)
                    order_type = "limit" if use_limit else "market"
                    order_kwargs = {
                        "symbol": symbol,
                        "qty": qty,
                        "side": "sell",  # sell without owning = short
                        "type": order_type,
                        "time_in_force": "day",
                    }
                    if use_limit:
                        order_kwargs["limit_price"] = str(round(price, 2))
                    order = api.submit_order(**order_kwargs)

                    result["action"] = "SHORT"
                    result["qty"] = qty
                    result["order_id"] = order.id
                    result["estimated_proceeds"] = round(qty * price, 2)
                    result["stop_loss_pct"] = short_sl
                    result["take_profit_pct"] = short_tp

                    if log:
                        log_trade(
                            symbol=symbol,
                            side="short",
                            qty=qty,
                            price=price,
                            order_id=order.id,
                            signal_type=action,
                            strategy="aggressive",
                            reason=signal.get("reason"),
                            ai_reasoning=ai_reasoning,
                            ai_confidence=ai_confidence,
                            stop_loss=short_sl,
                            take_profit=short_tp,
                            db_path=db_path,
                        )

    # ---- HOLD / no-action -------------------------------------------------
    elif action == "HOLD":
        result["action"] = "HOLD"
    else:
        result["action"] = "SKIP"
        if symbol in positions and "BUY" in action:
            result["reason"] = f"Already holding {symbol}"
        elif symbol in positions and "SELL" in action and int(positions[symbol]["qty"]) < 0:
            result["reason"] = f"Already short {symbol}"

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
            acted_on=result["action"] in ("BUY", "SELL", "SHORT"),
            db_path=db_path,
        )

    return result


# ---------------------------------------------------------------------------
# Scan and trade with AI gate
# ---------------------------------------------------------------------------

def run_aggressive_scan_and_trade(candidates, ctx=None, max_position_pct=None,
                                  log=True):
    """Pipeline: pre-filter -> strategy -> AI review -> execute.

    AI is ONLY called on candidates that can realistically result in a trade.
    Portfolio state is fetched ONCE at the top and reused throughout.

    Parameters
    ----------
    candidates : list[str]
        Ticker symbols to evaluate.
    ctx : UserContext, optional
        Passed through to ai_review and aggressive_execute_trade.
    max_position_pct : float, optional
        Override for position sizing.  Falls back to ctx or module constant.
    log : bool
        Whether to write to the journal database.

    Returns summary dict with counts and details.
    """
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else AGGRESSIVE_MAX_POSITION_PCT

    # ── STEP 0: Portfolio state (fetched ONCE) ──────────────────────
    api = get_api(ctx)
    account = get_account_info(api)
    positions_list = get_positions(api)

    # Filter positions by market type (crypto vs equity)
    if ctx is not None:
        is_crypto = ctx.segment == "crypto"
        positions_list = [p for p in positions_list if ("/" in p["symbol"]) == is_crypto]

    held_symbols = {p["symbol"] for p in positions_list}
    positions_dict = {p["symbol"]: p for p in positions_list}

    # Drawdown check — ONCE at the top
    drawdown_action = "normal"
    dd = {"action": "normal", "drawdown_pct": 0.0, "peak_equity": 0, "current_equity": 0}
    if ctx is not None:
        dd = check_drawdown(ctx, account, db_path=ctx.db_path)
        drawdown_action = dd.get("action", "normal")
        logging.info(f"Drawdown: {dd['drawdown_pct']:.1f}% (peak ${dd['peak_equity']:,.0f}, "
                     f"current ${dd['current_equity']:,.0f}) -> {drawdown_action}")
        if drawdown_action == "pause":
            logging.info(f"Drawdown pause: {dd['drawdown_pct']:.1f}% — skipping all trades")
            return {
                "total": len(candidates), "buys": 0, "sells": 0, "shorts": 0,
                "holds": 0, "skips": len(candidates), "ai_vetoed": 0, "errors": 0,
                "pre_filtered": len(candidates), "sent_to_ai": 0,
                "details": [{"symbol": s, "action": "DRAWDOWN_PAUSE"} for s in candidates],
                "vetoed_details": [],
            }

    enable_shorts = ctx.enable_short_selling if ctx is not None else False
    num_positions = len(positions_list)
    max_positions = ctx.max_total_positions if ctx is not None else 10
    at_max_positions = num_positions >= max_positions

    # ── STEP 1: Pre-filter (NO AI calls, NO strategy calls) ────────
    # Load auto-blacklist
    symbol_reputation = {}
    if ctx is not None:
        try:
            from self_tuning import get_symbol_reputation
            symbol_reputation = get_symbol_reputation(ctx.db_path)
        except Exception as exc:
            logging.warning(f"Could not load symbol reputation: {exc}")

    # Load earnings calendar
    earnings_blocklist = set()
    if ctx is not None:
        avoid_days = getattr(ctx, "avoid_earnings_days", 2)
        if avoid_days > 0:
            from earnings_calendar import check_earnings as _check_earnings
            for sym in candidates:
                try:
                    e = _check_earnings(sym)
                    if e and e["days_until"] <= avoid_days:
                        earnings_blocklist.add(sym)
                except Exception:
                    pass

    filtered_candidates = []
    pre_filter_skips = []

    for symbol in candidates:
        # Auto-blacklisted?
        if symbol in symbol_reputation:
            rep = symbol_reputation[symbol]
            if rep["win_rate"] == 0 and rep["total"] >= 3:
                logging.info(f"  Auto-blacklisted {symbol}: 0/{rep['total']} wins")
                pre_filter_skips.append({
                    "symbol": symbol, "action": "AUTO_BLACKLISTED",
                    "reason": f"0% win rate on {rep['total']} predictions",
                })
                # Log to activity feed
                if ctx is not None:
                    try:
                        from models import log_activity
                        log_activity(
                            ctx.profile_id, ctx.user_id, "auto_blacklist",
                            f"Auto-blacklisted {symbol}",
                            f"0/{rep['total']} wins — skipping until track record improves",
                            symbol=symbol,
                        )
                    except Exception:
                        pass
                continue

        # Earnings block?
        if symbol in earnings_blocklist:
            pre_filter_skips.append({
                "symbol": symbol, "action": "EARNINGS_SKIP",
                "reason": f"Earnings within {getattr(ctx, 'avoid_earnings_days', 2)} days",
            })
            continue

        # At max positions and don't hold this stock? Can only close existing.
        if at_max_positions and symbol not in held_symbols:
            pre_filter_skips.append({
                "symbol": symbol, "action": "SKIP",
                "reason": "At max positions, can only close existing",
            })
            continue

        filtered_candidates.append(symbol)

    # ── STEP 2: Fetch regime and political context ONCE ─────────────
    regime_info = None
    try:
        from market_regime import detect_regime
        regime_info = detect_regime()
        if regime_info and regime_info.get("regime") != "unknown":
            regime_label = regime_info["regime"].upper()
            vix_val = regime_info.get("vix", 0)
            print(f"  Market regime: {regime_label} (VIX {vix_val:.1f})")
            if ctx is not None:
                try:
                    from models import log_activity
                    log_activity(
                        getattr(ctx, "profile_id", 0), ctx.user_id,
                        "market_regime",
                        f"Market regime: {regime_label} (VIX {vix_val:.1f})",
                        regime_info.get("summary", ""),
                    )
                except Exception:
                    pass
    except Exception as _regime_exc:
        logging.warning(f"Could not detect market regime: {_regime_exc}")

    political_context = None
    maga_mode = ctx.maga_mode if ctx is not None else False
    if maga_mode:
        from political_sentiment import get_maga_mode_context
        print("  MAGA Mode active — fetching political context...", flush=True)
        political_context = get_maga_mode_context(ctx=ctx)
        if political_context:
            print(f"  {political_context.splitlines()[0]}")

    market_type = ctx.segment if ctx is not None else "small"

    # ── STEP 3: Run strategy on filtered candidates only ────────────
    logging.info(f"Pipeline: {len(candidates)} candidates -> {len(filtered_candidates)} after pre-filter "
                 f"({len(pre_filter_skips)} removed: "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'AUTO_BLACKLISTED')} blacklisted, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'EARNINGS_SKIP')} earnings, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'SKIP')} max-positions)")

    details = list(pre_filter_skips)  # Include skips in details
    vetoed = []
    errors = []
    sent_to_ai = 0

    for symbol in filtered_candidates:
        try:
            # Build strategy params from user context
            strategy_params = {
                "rsi_oversold": ctx.rsi_oversold,
                "rsi_overbought": ctx.rsi_overbought,
                "volume_surge_multiplier": ctx.volume_surge_multiplier,
                "breakout_volume_threshold": ctx.breakout_volume_threshold,
                "momentum_5d_gain": ctx.momentum_5d_gain,
                "momentum_20d_gain": ctx.momentum_20d_gain,
                "gap_pct_threshold": ctx.gap_pct_threshold,
                "strategy_momentum_breakout": ctx.strategy_momentum_breakout,
                "strategy_volume_spike": ctx.strategy_volume_spike,
                "strategy_mean_reversion": ctx.strategy_mean_reversion,
                "strategy_gap_and_go": ctx.strategy_gap_and_go,
            } if ctx else None
            # Run strategy
            signal = run_strategy(symbol, market_type, ctx=ctx, params=strategy_params)
            action = signal.get("signal", "HOLD")
            score = signal.get("score", 0)
            votes = signal.get("votes", {})

            # MAGA mode override for mean reversion
            has_mr_buy = (votes.get("mean_reversion") == "BUY"
                          or votes.get("extreme_oversold") == "BUY"
                          or votes.get("penny_reversal") == "BUY")
            if action == "HOLD" and maga_mode and has_mr_buy:
                action = "BUY"
                signal["signal"] = "BUY"
                signal["reason"] = (f"MAGA Mode override: {signal.get('reason', '')} "
                                    f"| Mean reversion BUY active despite conflicting signals")

            # HOLD -> skip AI (no cost)
            if action == "HOLD":
                details.append({"symbol": symbol, "action": "HOLD",
                                "reason": signal.get("reason", "")})
                continue

            # SELL with no position and no shorts -> skip AI (no cost)
            if action in ("SELL", "STRONG_SELL"):
                if symbol not in held_symbols and not enable_shorts:
                    details.append({"symbol": symbol, "action": "SKIP",
                                    "reason": "SELL signal, no position, shorts disabled"})
                    continue

            # BUY when already holding -> skip AI (no cost)
            if action in ("BUY", "STRONG_BUY") and symbol in held_symbols:
                details.append({"symbol": symbol, "action": "SKIP",
                                "reason": f"Already holding {symbol}"})
                continue

            # ── STEP 4: AI review (ONLY for trades that can actually execute) ──
            sent_to_ai += 1
            print(f"  {symbol}: {action} (score {score})")
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
                    "symbol": symbol, "action": "AI_VETOED", "signal": action,
                    "ai_signal": ai_result.get("signal"),
                    "ai_confidence": ai_result.get("confidence"),
                    "reason": f"AI vetoed: {ai_result.get('reasoning', '')[:100]}",
                })
                continue

            # ── STEP 5: Execute ────────────────────────────────────────
            trade_result = aggressive_execute_trade(
                symbol, signal, ctx=ctx, ai_result=ai_result,
                max_position_pct=max_position_pct, log=log,
                _account=account, _positions_list=positions_list,
                _dd=dd,
            )
            details.append(trade_result)

        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            details.append({"symbol": symbol, "action": "ERROR", "reason": str(exc)})

    # Build summary
    buys = [d for d in details if d.get("action") == "BUY"]
    sells = [d for d in details if d.get("action") == "SELL"]
    shorts = [d for d in details if d.get("action") == "SHORT"]
    holds = [d for d in details if d.get("action") == "HOLD"]
    skips = [d for d in details if d.get("action") in ("SKIP", "BLOCKED", "NONE",
                                                         "DRAWDOWN_PAUSE", "EXCLUDED",
                                                         "AUTO_BLACKLISTED", "EARNINGS_SKIP")]
    ai_vetoed_list = [d for d in details if d.get("action") == "AI_VETOED"]

    logging.info(f"Pipeline complete: {len(candidates)} candidates -> "
                 f"{len(filtered_candidates)} post-filter -> {sent_to_ai} sent to AI -> "
                 f"{len(buys)} buys, {len(sells)} sells, {len(shorts)} shorts")

    return {
        "total": len(candidates),
        "buys": len(buys),
        "sells": len(sells),
        "shorts": len(shorts),
        "holds": len(holds),
        "skips": len(skips),
        "ai_vetoed": len(ai_vetoed_list),
        "errors": len(errors),
        "pre_filtered": len(pre_filter_skips),
        "sent_to_ai": sent_to_ai,
        "details": details,
        "vetoed_details": vetoed,
    }
