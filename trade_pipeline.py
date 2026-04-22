"""Core trade pipeline — the AI-first decision engine.

This module orchestrates the end-to-end trade flow:
  1. Pre-filter candidates (blacklist, earnings, max positions, drawdown)
  2. Run market-specific strategy engines on each candidate (free, no AI cost)
  3. Rank top candidates and build a shortlist
  4. Single AI batch call: AI sees full portfolio + indicators + alt data +
     sector context + patterns + past performance, then picks 0-3 trades
  5. Meta-model (Phase 1 of ROADMAP): re-weights confidence, suppresses
     low-probability trades based on our own prediction history
  6. Execute selected trades with ATR stops, trailing stops, correlation
     checks, and slippage tracking

All per-profile position sizing and risk parameters come from UserContext.
See ROADMAP.md for the broader quant fund evolution context.
"""

import json
import logging
from typing import Any, Dict, List
from client import get_api, get_account_info, get_positions
from portfolio_manager import check_portfolio_constraints, check_drawdown, calculate_atr_stops
from journal import init_db, log_trade, log_signal
from strategy_router import run_strategy


# ---------------------------------------------------------------------------
# Shared ensemble cache — same candidates get the same specialist verdicts
# regardless of which profile is evaluating them. Keyed by market_type,
# expires every 15-minute cycle. Saves ~$3/day in AI calls.
# ---------------------------------------------------------------------------
_ensemble_cache = {}
_ensemble_cache_cycle = 0
_ensemble_lock = __import__("threading").Lock()

# Political context cache — same climate for all MAGA-mode profiles
# within a 30-minute window. One AI call instead of one per profile.
_political_cache = {}
_political_cache_cycle = 0
_political_lock = __import__("threading").Lock()


def _get_shared_political_context(ctx):
    """Return MAGA political context, cached for 30 minutes.
    All profiles see the same political climate — no need to re-analyze."""
    global _political_cache, _political_cache_cycle
    import time as _t

    with _political_lock:
        now_bucket = int(_t.time() / 1800)
        if now_bucket != _political_cache_cycle:
            _political_cache = {}
            _political_cache_cycle = now_bucket

        if "context" in _political_cache:
            logging.info("Using cached political context")
            return _political_cache["context"]

        from political_sentiment import get_maga_mode_context
        print("  MAGA Mode active — fetching political context...", flush=True)
        result = get_maga_mode_context(ctx=ctx)
        _political_cache["context"] = result
        return result


def _get_shared_ensemble(candidates_data, ctx):
    """Return ensemble result, cached per market_type per cycle.
    Thread-locked to prevent parallel profiles from both missing
    the cache and running duplicate ensemble calls."""
    global _ensemble_cache, _ensemble_cache_cycle
    import time as _t

    with _ensemble_lock:
        now_bucket = int(_t.time() / 1800)  # 30-min cache (was 15)
        if now_bucket != _ensemble_cache_cycle:
            _ensemble_cache = {}
            _ensemble_cache_cycle = now_bucket

        cache_key = ctx.segment
        if cache_key in _ensemble_cache:
            logging.info("Using shared ensemble results for %s", cache_key)
            return _ensemble_cache[cache_key]

        from ensemble import run_ensemble
        result = run_ensemble(
            candidates_data, ctx,
            ai_provider=ctx.ai_provider,
            ai_model=ctx.ai_model,
            ai_api_key=ctx.ai_api_key,
        )
        _ensemble_cache[cache_key] = result
        return result


# ---------------------------------------------------------------------------
# Default sizing / risk parameters — overridden by UserContext per-profile
# ---------------------------------------------------------------------------
DEFAULT_MAX_POSITION_PCT = 0.10
DEFAULT_STOP_LOSS_PCT = 0.03
DEFAULT_TAKE_PROFIT_PCT = 0.10
AI_MIN_CONFIDENCE = 25


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
# Execute a single trade
# ---------------------------------------------------------------------------

def execute_trade(symbol, signal, ctx=None, ai_result=None,
                             max_position_pct=None, log=True,
                             _account=None, _positions_list=None, _dd=None):
    """Execute a trade with profile-specific position sizing.

    Args:
        symbol: Ticker string.
        signal: Strategy signal dict (from strategy_router.run_strategy).
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
                "strategy": ctx.segment if ctx else "unknown",
            }

    # Earnings calendar check — skip stocks reporting earnings soon
    # (When called from run_trade_cycle, this is already
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
                        "strategy": ctx.segment if ctx else "unknown",
                    }
        except Exception as _earn_exc:
            # Never block a trade due to earnings lookup failure
            pass

    # Resolve parameters from ctx, explicit arg, or module-level constants
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else DEFAULT_MAX_POSITION_PCT
    stop_loss_pct = ctx.stop_loss_pct if ctx is not None else DEFAULT_STOP_LOSS_PCT
    take_profit_pct = ctx.take_profit_pct if ctx is not None else DEFAULT_TAKE_PROFIT_PCT
    db_path = ctx.db_path if ctx is not None else None

    api = get_api(ctx)

    # Use pre-fetched data if available, otherwise fetch fresh
    account = _account if _account is not None else get_account_info(api, ctx=ctx)

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
                "strategy": ctx.segment if ctx else "unknown",
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

    # If the signal has no price (screener race condition or failed fetch),
    # get it now before we attempt execution. Without a valid price the
    # trade silently skips, which is the bug that caused CRGY to show as
    # "TRADES SELECTED" in the AI brain but never execute.
    if price <= 0 and action in ("BUY", "STRONG_BUY", "SELL", "STRONG_SELL"):
        try:
            from market_data import get_bars
            bars = get_bars(symbol, limit=1)
            if bars is not None and not bars.empty:
                price = float(bars.iloc[-1]["close"])
                signal["price"] = price
                logging.info("Re-fetched price for %s: $%.2f (was missing from signal)", symbol, price)
        except Exception:
            pass

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
        "strategy": ctx.segment if ctx else "unknown",
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
            logging.warning("Trade SKIPPED for %s: price is 0 after all fetch attempts", symbol)
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

        # Use profile-specific concentration limit
        trade_value = qty * price
        if not allowed and "exceeds" in constraint_reason and equity > 0:
            if trade_value / equity <= max_position_pct and trade_value <= cash:
                allowed = True
                constraint_reason = "Passed risk constraints"

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

        # Schedule guard: reject if pipeline overran the trading window
        from order_guard import check_can_submit
        if not check_can_submit(ctx, symbol, "buy"):
            result["action"] = "SKIP"
            result["reason"] = f"Order blocked: outside {ctx.schedule_type} window"
            return result

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
            # Convert percentages to actual dollar prices for storage
            stop_price = round(price * (1 - actual_sl_pct), 4)
            target_price = round(price * (1 + actual_tp_pct), 4)
            log_trade(
                symbol=symbol,
                side="buy",
                qty=qty,
                price=price,
                order_id=order.id,
                signal_type=action,
                strategy=ctx.segment if ctx else "unknown",
                reason=signal.get("reason"),
                ai_reasoning=ai_reasoning,
                ai_confidence=ai_confidence,
                stop_loss=stop_price,
                take_profit=target_price,
                decision_price=price,
                db_path=db_path,
            )

    # ---- SELL logic (close existing long position) ---------------------------
    elif action in ("SELL", "STRONG_SELL") and symbol in positions and int(positions[symbol]["qty"]) > 0:
        # Schedule guard: reject if pipeline overran the trading window
        from order_guard import check_can_submit
        if not check_can_submit(ctx, symbol, "sell"):
            result["action"] = "SKIP"
            result["reason"] = f"Order blocked: outside {ctx.schedule_type} window"
            return result

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
            # Closing a position produces realized P&L — the row must be
            # 'closed', not 'open'. Without this, downstream reporting
            # filters by status get wrong counts.
            log_trade(
                symbol=symbol,
                side="sell",
                qty=sell_qty,
                price=price,
                order_id=order.id,
                signal_type=action,
                strategy=ctx.segment if ctx else "unknown",
                reason=signal.get("reason"),
                ai_reasoning=ai_reasoning,
                ai_confidence=ai_confidence,
                pnl=pnl,
                status="closed" if pnl is not None else "open",
                decision_price=price,
                db_path=db_path,
            )
            # Mark matching open BUY rows as closed so the trades page
            # doesn't show them as open forever after the position exits.
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
            except Exception:
                pass

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
                                symbol=symbol, signal=action, strategy=ctx.segment if ctx else "unknown",
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
                logging.warning("Short SKIPPED for %s: price is 0 after all fetch attempts", symbol)
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
                    short_sl = getattr(ctx, "short_stop_loss_pct", 0.08) if ctx is not None else DEFAULT_STOP_LOSS_PCT
                    short_tp = getattr(ctx, "short_take_profit_pct", 0.08) if ctx is not None else DEFAULT_TAKE_PROFIT_PCT

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

                    # Schedule guard
                    from order_guard import check_can_submit
                    if not check_can_submit(ctx, symbol, "sell"):
                        result["action"] = "SKIP"
                        result["reason"] = f"Order blocked: outside {ctx.schedule_type} window"
                        return result

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
                        # Shorts: stop is ABOVE entry, target is BELOW
                        stop_price = round(price * (1 + short_sl), 4)
                        target_price = round(price * (1 - short_tp), 4)
                        log_trade(
                            symbol=symbol,
                            side="short",
                            qty=qty,
                            price=price,
                            order_id=order.id,
                            signal_type=action,
                            strategy=ctx.segment if ctx else "unknown",
                            reason=signal.get("reason"),
                            ai_reasoning=ai_reasoning,
                            ai_confidence=ai_confidence,
                            stop_loss=stop_price,
                            take_profit=target_price,
                            decision_price=price,
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
            strategy=ctx.segment if ctx else "unknown",
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

def run_trade_cycle(candidates, ctx=None, max_position_pct=None,
                                  log=True):
    """Pipeline: pre-filter -> strategy -> AI review -> execute.

    AI is ONLY called on candidates that can realistically result in a trade.
    Portfolio state is fetched ONCE at the top and reused throughout.

    Parameters
    ----------
    candidates : list[str]
        Ticker symbols to evaluate.
    ctx : UserContext, optional
        Passed through to ai_review and execute_trade.
    max_position_pct : float, optional
        Override for position sizing.  Falls back to ctx or module constant.
    log : bool
        Whether to write to the journal database.

    Returns summary dict with counts and details.
    """
    if max_position_pct is None:
        max_position_pct = ctx.max_position_pct if ctx is not None else DEFAULT_MAX_POSITION_PCT

    # ── STEP 0: Portfolio state (fetched ONCE) ──────────────────────
    from scan_status import update_status, clear_status
    _pid = getattr(ctx, "profile_id", 0) if ctx else 0
    update_status(_pid, "Loading portfolio", "%d candidates" % len(candidates))

    api = get_api(ctx)
    account = get_account_info(api, ctx=ctx)
    positions_list = get_positions(api, ctx=ctx)

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

    update_status(_pid, "Pre-filtering", "%d candidates" % len(candidates))
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

    # Symbols exited within the last hour — avoid immediate re-entry churn.
    # Held positions can still be ADDED to / exited; we only block fresh
    # BUY entries on symbols we just stopped out of.
    recently_exited: set = set()
    if ctx is not None:
        try:
            from journal import get_recently_exited
            cooldown_min = int(getattr(ctx, "reentry_cooldown_minutes", 60))
            recently_exited = get_recently_exited(ctx.db_path, cooldown_min)
        except Exception:
            recently_exited = set()

    for symbol in candidates:
        # Recent-exit cooldown (only applies to non-held positions — we
        # can still manage a position we already hold, but we won't
        # open a fresh one on a symbol we just exited.)
        if symbol in recently_exited and symbol not in held_symbols:
            pre_filter_skips.append({
                "symbol": symbol, "action": "COOLDOWN",
                "reason": "Recently exited — cooldown to avoid churn",
            })
            continue

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

    # MAGA Mode: defer political context fetch until we know there are
    # candidates worth sending to AI.  This avoids an AI call every cycle
    # when all signals are weak/filtered.
    political_context = None
    maga_mode = ctx.maga_mode if ctx is not None else False

    market_type = ctx.segment if ctx is not None else "small"

    update_status(_pid, "Running 16 strategies", "%d candidates" % len(filtered_candidates))
    # ── STEP 3: Run strategy on ALL filtered candidates (free, no AI) ──
    logging.info(f"Pipeline: {len(candidates)} candidates -> {len(filtered_candidates)} after pre-filter "
                 f"({len(pre_filter_skips)} removed: "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'AUTO_BLACKLISTED')} blacklisted, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'EARNINGS_SKIP')} earnings, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'SKIP')} max-positions)")

    details = list(pre_filter_skips)
    errors = []

    # Phase 6: multi-strategy aggregation. Instead of one market-specific
    # strategy producing all candidates, the registry runs every active
    # (non-deprecated) strategy applicable to this market type. Candidates
    # flagged by multiple strategies get higher conviction scores.
    multi_summary = {"per_strategy_counts": {}, "active_strategies": []}
    strategy_results = []
    if ctx is not None:
        try:
            from multi_strategy import aggregate_candidates
            multi_summary = aggregate_candidates(ctx, filtered_candidates, db_path=ctx.db_path)
            strategy_results = multi_summary["candidates"]
            logging.info(
                f"Multi-strategy: {len(multi_summary['active_strategies'])} strategies ran, "
                f"counts={multi_summary['per_strategy_counts']}, "
                f"merged={len(strategy_results)} unique candidates"
            )
        except Exception as exc:
            logging.warning(f"Multi-strategy aggregation failed, falling back: {exc}")
            # Fallback to legacy single-engine behavior
            from strategies.market_engine import find_candidates as _legacy
            try:
                strategy_results = _legacy(ctx, filtered_candidates)
            except Exception:
                strategy_results = []

    # ── STEP 3.5: Rank and shortlist for AI batch ────────────────────
    # Load deprecated strategies (Phase 3: alpha decay monitoring). Strategies
    # whose rolling Sharpe has dropped meaningfully below lifetime for 30+
    # consecutive days are auto-deprecated by alpha_decay.run_decay_cycle().
    # The strategy registry already filters deprecated strategies upstream,
    # but we keep this guard for any candidates flagged by an older path.
    deprecated_types = set()
    if ctx is not None:
        try:
            from alpha_decay import list_deprecated
            deprecated_types = {d["strategy_type"] for d in list_deprecated(ctx.db_path)}
            if deprecated_types:
                logging.info(f"Alpha decay: skipping deprecated strategies: {deprecated_types}")
        except Exception as exc:
            logging.debug(f"alpha_decay unavailable: {exc}")

    shortlist = _rank_candidates(strategy_results, held_symbols, enable_shorts,
                                  deprecated_strategies=deprecated_types)

    # Ensure every shortlisted candidate has a valid price. If the
    # strategy's get_bars call failed during scoring, the candidate
    # arrives with price=0. Fetch it now — a candidate without a price
    # cannot be sized or executed, and the AI wastes a call evaluating
    # something we can't trade.
    from market_data import get_bars as _get_bars_for_price
    for c in shortlist:
        if not c.get("price") or c["price"] <= 0:
            try:
                _bars = _get_bars_for_price(c["symbol"], limit=1)
                if _bars is not None and not _bars.empty:
                    c["price"] = float(_bars.iloc[-1]["close"])
            except Exception:
                pass
    shortlist = [c for c in shortlist if c.get("price", 0) > 0]

    if not shortlist:
        clear_status(_pid)
        logging.info(f"Pipeline complete: {len(candidates)} candidates -> "
                     f"{len(filtered_candidates)} post-filter -> 0 shortlisted -> "
                     f"0 sent to AI -> 0 buys, 0 sells, 0 shorts")
        return {
            "total": len(candidates), "buys": 0, "sells": 0, "shorts": 0,
            "holds": len(strategy_results) - len(shortlist),
            "skips": len(pre_filter_skips), "ai_vetoed": 0, "errors": len(errors),
            "pre_filtered": len(pre_filter_skips), "sent_to_ai": 0,
            "details": details, "vetoed_details": [],
        }

    update_status(_pid, "AI selecting trades", "%d shortlisted" % len(shortlist))
    # ── STEP 4: AI batch selection (ONE call) ────────────────────────
    # Lazy-fetch MAGA political context only when we have candidates —
    # AND only for equity profiles. Political / tariff narrative has
    # minimal bearing on crypto, and the call costs ~$0.02 × many cycles.
    is_crypto = ctx is not None and ctx.segment == "crypto"
    if maga_mode and political_context is None and not is_crypto:
        political_context = _get_shared_political_context(ctx)
        if political_context:
            print(f"  {political_context.splitlines()[0]}")

    # Build batch context
    candidates_data = _build_candidates_data(shortlist, ctx, symbol_reputation)
    portfolio_state = _build_portfolio_state(account, positions_list, dd, ctx)
    market_ctx = _build_market_context(regime_info, political_context, ctx)

    update_status(_pid, "Specialist ensemble", "%d candidates" % len(shortlist))
    # ── STEP 3.7: Specialist ensemble (Phase 8) ──────────────────────
    # Four specialist AIs (earnings, pattern, sentiment, risk) each see
    # the full shortlist in one batch call, returning per-symbol verdicts.
    # The ensemble synthesizes them into a single verdict per candidate
    # that the final trade-selection AI sees alongside raw data. Risk
    # VETOs drop a candidate from the shortlist entirely.
    ensemble_result = None
    if getattr(ctx, "enable_specialist_ensemble", True):
        try:
            from ensemble import run_ensemble, format_for_final_prompt
            # Share ensemble results across profiles with the same
            # market_type. The specialist verdicts depend on the
            # candidates (same for all profiles of the same type),
            # not on the profile's capital or positions.
            ensemble_result = _get_shared_ensemble(
                candidates_data, ctx,
            )
            per_symbol = ensemble_result.get("per_symbol", {})
            # Inject the ensemble summary into each candidate so the final
            # AI prompt sees it, and filter out risk-vetoed symbols.
            kept: List[Dict[str, Any]] = []
            vetoed_syms: List[str] = []
            for c in candidates_data:
                sym = c.get("symbol", "")
                verdict = per_symbol.get(sym)
                if verdict and verdict.get("vetoed"):
                    vetoed_syms.append(sym)
                    continue
                if verdict:
                    c["ensemble_summary"] = format_for_final_prompt(per_symbol, sym)
                    c["ensemble_verdict"] = verdict["verdict"]
                    c["ensemble_confidence"] = verdict["confidence"]
                kept.append(c)
            candidates_data = kept
            if vetoed_syms:
                logging.info(f"Specialist ensemble vetoed: {vetoed_syms}")
                print(f"  Risk specialist VETOed: {', '.join(vetoed_syms)}")
            print(f"  Specialist ensemble: {ensemble_result.get('cost_calls', 0)} calls, "
                  f"{len(vetoed_syms)} vetoed, {len(candidates_data)} kept")
        except Exception as exc:
            logging.warning(f"Specialist ensemble failed (continuing without): {exc}")

    print(f"  AI batch: {len(candidates_data)} candidates -> selecting trades...", flush=True)
    from ai_analyst import ai_select_trades
    ai_response = ai_select_trades(candidates_data, portfolio_state, market_ctx, ctx=ctx)

    ai_trades = ai_response.get("trades", [])
    portfolio_reasoning = ai_response.get("portfolio_reasoning", "")
    logging.info(f"AI selected {len(ai_trades)} trades: {portfolio_reasoning[:200]}")

    if ai_response.get("pass_this_cycle"):
        print(f"  AI passed this cycle: {portfolio_reasoning[:100]}")

    # Record a prediction for EVERY candidate the AI analyzed.
    # The prediction system tracks whether the AI's analysis was correct
    # (did the price move in the predicted direction?) — NOT whether a
    # trade was executed. This feeds the self-tuning feedback loop.
    try:
        from ai_tracker import record_prediction
        current_regime = (regime_info or {}).get("regime")
        for c in candidates_data:
            sym = c.get("symbol", "")
            votes = c.get("votes", {})
            strategy = next((k for k, v in votes.items() if v != "HOLD"), "batch_ai")

            # Was this symbol selected for a trade by the AI?
            selected = next((t for t in ai_trades if t.get("symbol") == sym), None)

            if selected:
                pred_signal = selected["action"]
                pred_confidence = selected.get("confidence", 50)
                pred_reasoning = selected.get("reasoning", "")
                price_targets = {
                    "stop_loss": selected.get("stop_loss_pct"),
                    "take_profit": selected.get("take_profit_pct"),
                }
            else:
                # AI saw this candidate but passed — record as HOLD
                # so the resolver can check if passing was the right call
                pred_signal = "HOLD"
                pred_confidence = 0
                pred_reasoning = portfolio_reasoning[:300]
                price_targets = None

            # Build feature payload for meta-model training (Phase 1).
            # Strip non-numeric/non-scalar fields — store only what a feature
            # extractor can reliably use (see meta_model.extract_features).
            features_payload = {
                k: v for k, v in c.items()
                if k not in ("reason", "news", "votes", "rel_strength",
                              "alt_data", "social", "last_prediction",
                              "earnings_warning", "track_record")
            }
            # Include meta-signals separately (flattened)
            votes = c.get("votes", {})
            for strat_name, vote in votes.items():
                features_payload[f"vote_{strat_name}"] = vote
            rs = c.get("rel_strength") or {}
            if rs:
                features_payload["rel_strength_vs_sector"] = rs.get("relative_strength", 0)
                features_payload["sector_trend"] = rs.get("sector_trend", "flat")
            alt = c.get("alt_data") or {}
            if alt:
                features_payload["insider_direction"] = alt.get("insider", {}).get("net_direction", "neutral")
                features_payload["short_pct_float"] = alt.get("short", {}).get("short_pct_float", 0)
                features_payload["options_signal"] = alt.get("options", {}).get("signal", "neutral")
                features_payload["put_call_ratio"] = alt.get("options", {}).get("put_call_ratio", 0)
                features_payload["vwap_position"] = alt.get("intraday", {}).get("vwap_position", "at")
                features_payload["pe_trailing"] = alt.get("fundamentals", {}).get("pe_trailing", 0)
            social = c.get("social") or {}
            if social:
                features_payload["reddit_mentions"] = social.get("mentions", 0)
                features_payload["reddit_sentiment"] = social.get("sentiment_score", 0)
            # Market context
            features_payload["_regime"] = current_regime
            features_payload["_market_signal_count"] = len([v for v in votes.values() if v != "HOLD"])

            record_prediction(
                symbol=sym,
                predicted_signal=pred_signal,
                confidence=pred_confidence,
                reasoning=pred_reasoning,
                price_at_prediction=c.get("price", 0),
                price_targets=price_targets,
                db_path=ctx.db_path if ctx else None,
                regime=current_regime,
                strategy_type=strategy,
                features=features_payload,
            )
    except Exception as exc:
        logging.warning(f"Failed to record batch predictions: {exc}")

    # ── STEP 4.5: Meta-model re-weighting (Phase 1) ─────────────────
    # Load the profile's trained meta-model (if available) and re-weight each
    # AI-selected trade's confidence. Trades with meta-probability below the
    # suppression threshold are dropped. See ROADMAP.md Phase 1.
    meta_stats = {"loaded": False, "suppressed": 0, "adjusted": 0}
    meta_bundle = None
    if ctx is not None and ai_trades:
        try:
            import meta_model
            profile_id = getattr(ctx, "profile_id", 0)
            meta_path = meta_model.model_path_for_profile(profile_id)
            meta_bundle = meta_model.load_model(meta_path)
            if meta_bundle:
                meta_stats["loaded"] = True
                filtered_trades = []
                for t in ai_trades:
                    sym = t.get("symbol", "")
                    cand = next((c for c in candidates_data if c.get("symbol") == sym), {})
                    # Rebuild the same features_payload we stored in the prediction
                    fp = {k: v for k, v in cand.items()
                          if k not in ("reason", "news", "votes", "rel_strength",
                                       "alt_data", "social", "last_prediction",
                                       "earnings_warning", "track_record")}
                    votes = cand.get("votes", {}) or {}
                    for strat_name, vote in votes.items():
                        fp[f"vote_{strat_name}"] = vote
                    rs = cand.get("rel_strength") or {}
                    if rs:
                        fp["rel_strength_vs_sector"] = rs.get("relative_strength", 0)
                        fp["sector_trend"] = rs.get("sector_trend", "flat")
                    alt = cand.get("alt_data") or {}
                    if alt:
                        fp["insider_direction"] = alt.get("insider", {}).get("net_direction", "neutral")
                        fp["short_pct_float"] = alt.get("short", {}).get("short_pct_float", 0)
                        fp["options_signal"] = alt.get("options", {}).get("signal", "neutral")
                        fp["put_call_ratio"] = alt.get("options", {}).get("put_call_ratio", 0)
                        fp["vwap_position"] = alt.get("intraday", {}).get("vwap_position", "at")
                        fp["pe_trailing"] = alt.get("fundamentals", {}).get("pe_trailing", 0)
                    social = cand.get("social") or {}
                    if social:
                        fp["reddit_mentions"] = social.get("mentions", 0)
                        fp["reddit_sentiment"] = social.get("sentiment_score", 0)
                    fp["_regime"] = (regime_info or {}).get("regime", "unknown")
                    fp["_market_signal_count"] = len([v for v in votes.values() if v != "HOLD"])

                    meta_prob = meta_model.predict_probability(meta_bundle, fp)
                    t["meta_prob"] = round(meta_prob, 4)

                    if meta_prob < meta_model.SUPPRESSION_THRESHOLD:
                        meta_stats["suppressed"] += 1
                        logging.info(f"  Meta-model SUPPRESS {sym}: meta_prob={meta_prob:.3f} < "
                                     f"{meta_model.SUPPRESSION_THRESHOLD}")
                        continue
                    # Blend confidences
                    original_conf = t.get("confidence", 50)
                    new_conf = meta_model.adjust_confidence(original_conf, meta_prob)
                    t["original_confidence"] = original_conf
                    t["confidence"] = new_conf
                    meta_stats["adjusted"] += 1
                    logging.info(f"  Meta-model {sym}: meta_prob={meta_prob:.3f}, "
                                 f"confidence {original_conf}->{new_conf}")
                    filtered_trades.append(t)
                ai_trades = filtered_trades
        except Exception as exc:
            logging.warning(f"Meta-model integration failed: {exc}")

    # ── STEP 4.9: Crisis gate (Phase 10) ─────────────────────────────
    # Capital preservation override. At `crisis` or `severe` levels, new
    # long entries are blocked entirely. At `elevated`, position sizes
    # are scaled down. This runs AFTER the AI has chosen so the AI still
    # sees its own reasoning in logs, but before execution.
    crisis_size_multiplier = 1.0
    crisis_level = "normal"
    try:
        from crisis_state import get_current_level
        crisis = get_current_level(ctx.db_path)
        crisis_level = crisis["level"]
        crisis_size_multiplier = crisis["size_multiplier"]
        if crisis_level != "normal":
            pre = len(ai_trades)
            if crisis_size_multiplier <= 0:
                # Crisis / severe: block new long entries entirely.
                # Allow SELL/SHORT (exits / capital preservation).
                ai_trades = [t for t in ai_trades
                             if t.get("action", "").upper() in ("SELL", "SHORT")]
                logging.warning(
                    f"Crisis gate BLOCKED {pre - len(ai_trades)} new longs "
                    f"(level={crisis_level})"
                )
                print(f"  Crisis gate [{crisis_level.upper()}]: "
                      f"blocked {pre - len(ai_trades)} new longs")
            else:
                # Elevated: scale down size_pct
                for t in ai_trades:
                    old = t.get("size_pct", 0)
                    t["size_pct"] = round(old * crisis_size_multiplier, 2)
                logging.info(
                    f"Crisis gate scaled down sizes by {crisis_size_multiplier:.2f} "
                    f"(level={crisis_level})"
                )
                print(f"  Crisis gate [{crisis_level.upper()}]: "
                      f"sizes x{crisis_size_multiplier:.2f}")
    except Exception as exc:
        logging.warning(f"Crisis gate skipped: {exc}")

    update_status(_pid, "Executing trades", "%d selected" % len(ai_trades))
    # ── STEP 5: Execute AI-selected trades ───────────────────────────
    for ai_trade in ai_trades:
        symbol = ai_trade["symbol"]
        action = ai_trade["action"]
        size_pct = ai_trade["size_pct"] / 100.0  # Convert 7.5 -> 0.075

        # Build a signal dict that execute_trade expects
        # Find the original strategy signal for this symbol
        orig_signal = next((s for s in strategy_results if s["symbol"] == symbol), {})
        signal = {
            "symbol": symbol,
            "signal": action if action != "SHORT" else "STRONG_SELL",
            "reason": ai_trade.get("reasoning", ""),
            "price": orig_signal.get("price", 0),
            "score": orig_signal.get("score", 0),
            "votes": orig_signal.get("votes", {}),
        }

        ai_result = {
            "signal": action,
            "confidence": ai_trade.get("confidence", 50),
            "reasoning": ai_trade.get("reasoning", ""),
            "risk_factors": [],
            "price_targets": {
                "stop_loss": ai_trade.get("stop_loss_pct", 3.0),
                "take_profit": ai_trade.get("take_profit_pct", 10.0),
            },
        }

        try:
            print(f"  Executing: {action} {symbol} ({size_pct*100:.1f}% equity, "
                  f"confidence {ai_trade.get('confidence', '?')})")
            trade_result = execute_trade(
                symbol, signal, ctx=ctx, ai_result=ai_result,
                max_position_pct=size_pct, log=log,
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
    holds_count = len([s for s in strategy_results if s.get("signal") == "HOLD"])
    skips = [d for d in details if d.get("action") in ("SKIP", "BLOCKED", "NONE",
                                                         "DRAWDOWN_PAUSE", "EXCLUDED",
                                                         "AUTO_BLACKLISTED", "EARNINGS_SKIP")]

    clear_status(_pid)
    logging.info(f"Pipeline complete: {len(candidates)} candidates -> "
                 f"{len(filtered_candidates)} post-filter -> {len(shortlist)} shortlisted -> "
                 f"1 AI call -> {len(buys)} buys, {len(sells)} sells, {len(shorts)} shorts")

    result = {
        "total": len(candidates),
        "buys": len(buys),
        "sells": len(sells),
        "shorts": len(shorts),
        "holds": holds_count,
        "skips": len(skips),
        "ai_vetoed": 0,
        "errors": len(errors),
        "pre_filtered": len(pre_filter_skips),
        "sent_to_ai": 1 if shortlist else 0,
        "details": details,
        "vetoed_details": [],
        "ai_reasoning": portfolio_reasoning,
    }

    # Save cycle data for the web dashboard to display
    _save_cycle_data(ctx, candidates_data, shortlist, ai_trades,
                     portfolio_reasoning, market_ctx, regime_info,
                     meta_stats=meta_stats, ensemble_result=ensemble_result)

    return result


def _ensemble_summary_for_cycle(ensemble_result):
    """Compact ensemble breakdown for persistence in cycle_data JSON."""
    if not ensemble_result:
        return {"enabled": False}
    per_symbol = ensemble_result.get("per_symbol", {})
    rows = []
    vetoed = []
    for sym, entry in per_symbol.items():
        if entry.get("vetoed"):
            vetoed.append({"symbol": sym,
                           "reason": entry.get("veto_reason", "")})
        rows.append({
            "symbol": sym,
            "verdict": entry["verdict"],
            "confidence": entry["confidence"],
            "vetoed": entry.get("vetoed", False),
            "specialists": [
                {"name": s["specialist"], "verdict": s["verdict"],
                 "confidence": int(s["confidence"]),
                 "reasoning": s["reasoning"][:200]}
                for s in entry.get("specialists", [])
            ],
        })
    return {
        "enabled": True,
        "cost_calls": ensemble_result.get("cost_calls", 0),
        "vetoed": vetoed,
        "rows": rows,
    }


def _save_cycle_data(ctx, candidates_data, shortlist, ai_trades,
                     portfolio_reasoning, market_ctx, regime_info,
                     meta_stats=None, ensemble_result=None):
    """Save the last cycle's AI decisions to a JSON file for the dashboard."""
    import json as _json
    import time as _time

    if ctx is None:
        return

    profile_id = getattr(ctx, "profile_id", 0)
    try:
        cycle_data = {
            "profile_id": profile_id,
            "profile_name": getattr(ctx, "display_name", ""),
            "timestamp": _time.time(),
            "ai_reasoning": portfolio_reasoning or "No candidates shortlisted",
            "trades_selected": [
                {
                    "symbol": t.get("symbol"),
                    "action": t.get("action"),
                    "size_pct": t.get("size_pct"),
                    "confidence": t.get("confidence"),
                    "reasoning": t.get("reasoning", ""),
                }
                for t in (ai_trades or [])
            ],
            "shortlist": [
                {
                    "symbol": c.get("symbol"),
                    "signal": c.get("signal"),
                    "score": c.get("score"),
                    "rsi": c.get("rsi"),
                    "adx": c.get("adx"),
                    "mfi": c.get("mfi"),
                    "volume_ratio": c.get("volume_ratio"),
                    "pct_from_52w_high": c.get("pct_from_52w_high"),
                    "squeeze": c.get("squeeze"),
                    "track_record": c.get("track_record"),
                    "news": c.get("news", [])[:2],
                    "insider": (c.get("alt_data", {}).get("insider", {})
                                .get("net_direction", "neutral")),
                    "short_pct": (c.get("alt_data", {}).get("short", {})
                                  .get("short_pct_float", 0)),
                    "options_signal": (c.get("alt_data", {}).get("options", {})
                                       .get("signal", "neutral")),
                    "reddit_mentions": c.get("social", {}).get("mentions", 0),
                    "options_oracle_summary": c.get("options_oracle_summary"),
                    "sec_alert_severity": (c.get("sec_alert") or {}).get("severity"),
                }
                for c in (candidates_data or [])
            ],
            "regime": (regime_info or {}).get("regime", "unknown"),
            "vix": (regime_info or {}).get("vix", 0),
            "sector_rotation": market_ctx.get("sector_rotation", {}),
            "learned_patterns": market_ctx.get("learned_patterns", []),
            "meta_model": meta_stats or {"loaded": False, "suppressed": 0, "adjusted": 0},
            "ensemble": _ensemble_summary_for_cycle(ensemble_result),
        }

        # Write per-profile cycle file
        path = f"cycle_data_{profile_id}.json"
        with open(path, "w") as f:
            _json.dump(cycle_data, f)

    except Exception as exc:
        logging.debug(f"Failed to save cycle data: {exc}")


# ---------------------------------------------------------------------------
# Helpers for AI-first batch pipeline
# ---------------------------------------------------------------------------

def _rank_candidates(strategy_results, held_symbols, enable_shorts,
                      deprecated_strategies=None):
    """Rank strategy results into a shortlist for AI batch review.

    Returns top ~15 candidates sorted by abs(score) desc.
    Filters out HOLD, SELLs with no position + no shorts, BUYs on held symbols,
    and signals whose primary strategy_type is in deprecated_strategies (Phase 3).
    """
    deprecated_strategies = deprecated_strategies or set()
    eligible = []
    for signal in strategy_results:
        symbol = signal.get("symbol", "")
        action = signal.get("signal", "HOLD")
        score = signal.get("score", 0)

        if action == "HOLD":
            continue
        if action in ("SELL", "STRONG_SELL") and symbol not in held_symbols and not enable_shorts:
            continue
        if action in ("BUY", "STRONG_BUY") and symbol in held_symbols:
            continue

        # Phase 3: skip candidates whose primary voting strategy is deprecated.
        # Primary = the first strategy that cast a non-HOLD vote (matches the
        # strategy_type stored in ai_predictions).
        if deprecated_strategies:
            votes = signal.get("votes", {})
            primary = next((k for k, v in votes.items() if v != "HOLD"), None)
            if primary and primary in deprecated_strategies:
                continue

        eligible.append(signal)

    eligible.sort(key=lambda s: (abs(s.get("score", 0)),
                                  abs(s.get("rsi", 50) - 50)),
                  reverse=True)
    return eligible[:15]


def _extract_indicator(signal, key, default=0):
    """Extract an indicator value from a strategy signal dict.

    Checks top level, then digs into strategy_results sub-dicts.
    strategy_results can be a dict {name: result_dict} or a list.
    """
    if key in signal:
        return signal[key]
    sr = signal.get("strategy_results", {})
    # strategy_results is a dict of {strategy_name: result_dict}
    if isinstance(sr, dict):
        for result in sr.values():
            if isinstance(result, dict) and key in result:
                return result[key]
    elif isinstance(sr, list):
        for result in sr:
            if isinstance(result, dict) and key in result:
                return result[key]
    return default


def _get_latest_indicators(symbol):
    """Fetch the latest indicator values directly from market data.

    The strategy result dicts only contain indicators each strategy
    explicitly returns. This function gets ALL indicators from the
    DataFrame for the AI prompt.
    """
    try:
        from market_data import get_bars, add_indicators
        df = get_bars(symbol, limit=200)
        if df.empty or len(df) < 20:
            return {}
        df = add_indicators(df)
        latest = df.iloc[-1]
        return {
            "rsi": float(latest.get("rsi", 50)),
            "stoch_rsi": float(latest.get("stoch_rsi", 50)),
            "adx": float(latest.get("adx", 0)),
            "mfi": float(latest.get("mfi", 50)),
            "cmf": float(latest.get("cmf", 0)),
            "atr_14": float(latest.get("atr_14", 0)),
            "roc_10": float(latest.get("roc_10", 0)),
            "pct_from_52w_high": float(latest.get("pct_from_52w_high", 0)),
            "pct_from_52w_low": float(latest.get("pct_from_52w_low", 0)),
            "pct_from_vwap": float(latest.get("pct_from_vwap", 0)),
            "nearest_fib_dist": float(latest.get("nearest_fib_dist", 99)),
            "squeeze": int(latest.get("squeeze", 0)),
            "gap_pct": float(latest.get("gap_pct", 0)),
            "obv": float(latest.get("obv", 0)),
            "volume_ratio": float(latest.get("volume", 0) / latest.get("volume_sma_20", 1))
                           if latest.get("volume_sma_20", 0) > 0 else 1.0,
        }
    except Exception:
        return {}


def _build_candidates_data(shortlist, ctx, symbol_reputation):
    """Convert strategy signals into the format ai_select_trades expects."""
    from earnings_calendar import check_earnings as _check_earnings
    from news_sentiment import fetch_news_yfinance
    from market_data import get_relative_strength_vs_sector
    from alternative_data import get_all_alternative_data
    from social_sentiment import get_ticker_mentions

    # Phase 4: pull recent SEC filing alerts for shortlist symbols in one
    # DB query, then attach to each candidate. Zero extra network cost —
    # the filings were already analyzed by the daily SEC monitor task.
    sec_alerts_by_symbol = {}
    if ctx is not None:
        try:
            from sec_filings import get_active_alerts
            shortlist_symbols = [s.get("symbol") for s in shortlist if s.get("symbol")]
            alerts = get_active_alerts(ctx.db_path, symbols=shortlist_symbols,
                                        min_severity="medium")
            for a in alerts:
                sym = a["symbol"]
                # Take the most recent alert per symbol (rows come back newest first)
                if sym not in sec_alerts_by_symbol:
                    sec_alerts_by_symbol[sym] = {
                        "form": a["form_type"],
                        "date": a["filed_date"],
                        "severity": a["alert_severity"],
                        "signal": a["alert_signal"],
                        "summary": a["alert_summary"],
                    }
        except Exception:
            pass

    candidates = []
    for signal in shortlist:
        symbol = signal.get("symbol", "?")
        # Get ALL indicators directly from market data (not from strategy
        # result dicts which only contain the few fields each strategy returns)
        indicators = _get_latest_indicators(symbol)

        entry = {
            "symbol": symbol,
            "price": signal.get("price", 0),
            "signal": signal.get("signal", "HOLD"),
            "score": signal.get("score", 0),
            "votes": signal.get("votes", {}),
            "rsi": indicators.get("rsi", 50),
            "volume_ratio": indicators.get("volume_ratio", 1.0),
            "atr": indicators.get("atr_14", 0),
            "adx": indicators.get("adx", 0),
            "stoch_rsi": indicators.get("stoch_rsi", 50),
            "roc_10": indicators.get("roc_10", 0),
            "pct_from_52w_high": indicators.get("pct_from_52w_high", 0),
            "mfi": indicators.get("mfi", 50),
            "cmf": indicators.get("cmf", 0),
            "squeeze": indicators.get("squeeze", 0),
            "pct_from_vwap": indicators.get("pct_from_vwap", 0),
            "nearest_fib_dist": indicators.get("nearest_fib_dist", 99),
            "gap_pct": indicators.get("gap_pct", 0),
            "reason": signal.get("reason", "")[:120],
        }

        # Per-stock track record + last prediction reasoning
        if symbol in symbol_reputation:
            rep = symbol_reputation[symbol]
            entry["track_record"] = (f"{rep['wins']}W/{rep['losses']}L "
                                     f"({rep['win_rate']:.0f}% win rate)")

        # Fetch last prediction reasoning for this symbol so AI remembers
        # WHY it made its previous call
        if ctx and ctx.db_path:
            try:
                import sqlite3 as _sq
                _conn = _sq.connect(ctx.db_path)
                last_pred = _conn.execute(
                    "SELECT predicted_signal, confidence, reasoning, actual_outcome "
                    "FROM ai_predictions WHERE symbol = ? AND confidence > 0 "
                    "ORDER BY id DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
                _conn.close()
                if last_pred:
                    outcome = last_pred[3] or "pending"
                    entry["last_prediction"] = (
                        f"Last call: {last_pred[0]} ({last_pred[1]}% conf, "
                        f"outcome: {outcome}). "
                        f"Reasoning: {str(last_pred[2])[:100]}"
                    )
            except Exception:
                pass

        # Earnings warning
        try:
            e = _check_earnings(symbol)
            if e and e.get("days_until", 999) <= 5:
                entry["earnings_warning"] = f"EARNINGS in {e['days_until']} days"
        except Exception:
            pass

        # Phase 4: SEC filing alerts for this symbol (if any active alerts exist)
        if symbol in sec_alerts_by_symbol:
            entry["sec_alert"] = sec_alerts_by_symbol[symbol]

        # Phase 5: Options Chain Oracle — IV skew, term structure, GEX, max pain,
        # implied move, IV rank. Free from yfinance. Returns None for crypto.
        try:
            from options_oracle import get_options_oracle, summarize_for_ai
            oracle = get_options_oracle(symbol)
            if oracle and oracle.get("has_options"):
                entry["options_oracle"] = oracle
                summary = summarize_for_ai(oracle)
                if summary:
                    entry["options_oracle_summary"] = summary
        except Exception:
            pass

        # Recent news headlines (free from yfinance, no AI cost)
        try:
            headlines = fetch_news_yfinance(symbol, limit=3)
            if headlines:
                entry["news"] = headlines
        except Exception:
            pass

        # Relative strength vs sector (free)
        try:
            rs = get_relative_strength_vs_sector(symbol)
            if rs:
                entry["rel_strength"] = rs
        except Exception:
            pass

        # Alternative data: insider, short interest, options, fundamentals, intraday
        try:
            alt = get_all_alternative_data(symbol)
            if alt and not alt.get("is_crypto"):
                entry["alt_data"] = alt
        except Exception:
            pass

        # Social sentiment from Reddit (if configured)
        try:
            social = get_ticker_mentions(symbol)
            if social and social.get("mentions", 0) > 0:
                entry["social"] = social
        except Exception:
            pass

        candidates.append(entry)
    return candidates


def _build_portfolio_state(account, positions_list, dd, ctx):
    """Bundle portfolio info for the AI batch prompt."""
    return {
        "equity": account.get("equity", 0),
        "cash": account.get("cash", 0),
        "positions": [
            {
                "symbol": p.get("symbol", "?"),
                "qty": p.get("qty", 0),
                "market_value": p.get("market_value", 0),
                "unrealized_pl": p.get("unrealized_pl", 0),
                "unrealized_plpc": p.get("unrealized_plpc", 0),
            }
            for p in positions_list
        ],
        "num_positions": len(positions_list),
        "drawdown_pct": dd.get("drawdown_pct", 0),
        "drawdown_action": dd.get("action", "normal"),
    }


def _build_market_context(regime_info, political_context, ctx):
    """Bundle market context for the AI batch prompt."""
    regime = regime_info or {}

    # Get performance summary + learned patterns from self-tuning
    profile_summary = None
    learned_patterns = []
    if ctx is not None:
        try:
            from self_tuning import get_batch_context_data
            batch_ctx = get_batch_context_data(ctx)
            profile_summary = batch_ctx.get("profile_summary")
            learned_patterns = batch_ctx.get("learned_patterns", [])
        except Exception:
            pass

    # Sector rotation (free, cached 30min)
    sector_rotation = {}
    try:
        from market_data import get_sector_rotation
        sector_rotation = get_sector_rotation()
    except Exception:
        pass

    # Crisis level (Phase 10). If we're in an elevated/crisis state,
    # the AI should know and factor it into its reasoning.
    crisis_ctx = None
    if ctx is not None:
        try:
            from crisis_state import get_current_level
            cs = get_current_level(ctx.db_path)
            if cs.get("level", "normal") != "normal":
                signals = ", ".join(s.get("name", "?")
                                    for s in cs.get("signals", []))
                crisis_ctx = (
                    f"CRISIS STATE: {cs['level'].upper()} "
                    f"(size x{cs.get('size_multiplier', 1.0):.2f}). "
                    f"Signals: {signals}. "
                    f"Bias toward capital preservation; tighter stops; "
                    f"prefer exits over entries."
                )
        except Exception:
            pass

    return {
        "regime": regime.get("regime", "unknown"),
        "vix": regime.get("vix", 0),
        "spy_trend": regime.get("spy_trend", "unknown"),
        "political_context": political_context,
        "profile_summary": profile_summary,
        "learned_patterns": learned_patterns,
        "sector_rotation": sector_rotation,
        "crisis_context": crisis_ctx,
    }
