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
from typing import Any, Dict, List, Tuple
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

    Lever 1 of COST_AND_QUALITY_LEVERS_PLAN.md (2026-04-27): cache
    is now persistent in SQLite (`shared_ai_cache` table) so a
    scheduler restart doesn't force a fresh fetch for the rest of
    the 30-min window. The previous in-memory dict is kept as an
    L1 cache to avoid even hitting SQLite when the process is hot.
    """
    global _political_cache, _political_cache_cycle
    import time as _t

    with _political_lock:
        now_bucket = int(_t.time() / 1800)
        if now_bucket != _political_cache_cycle:
            _political_cache = {}
            _political_cache_cycle = now_bucket

        # L1: in-process cache (hot path)
        if "context" in _political_cache:
            logging.info("Using cached political context")
            return _political_cache["context"]

        # L2: persistent SQLite cache (survives restarts)
        try:
            from shared_ai_cache import get as _cache_get, put as _cache_put
            persisted = _cache_get("political", "global",
                                   bucket_seconds=1800)
            if persisted is not None:
                _political_cache["context"] = persisted
                logging.info("Using persisted political context from disk")
                return persisted
        except Exception:
            _cache_get = _cache_put = None

        from political_sentiment import get_maga_mode_context
        print("  MAGA Mode active — fetching political context...", flush=True)
        result = get_maga_mode_context(ctx=ctx)
        _political_cache["context"] = result
        if _cache_put is not None:
            try:
                _cache_put("political", "global", result,
                           bucket_seconds=1800)
            except Exception:
                pass
        return result


def _get_shared_ensemble(candidates_data, ctx):
    """Return ensemble result, cached per market_type per cycle.

    Lever 1 of COST_AND_QUALITY_LEVERS_PLAN.md (2026-04-27): cache
    is now persistent in SQLite so deploy-restarts don't wipe a
    valid cached result. Two-tier: in-memory L1 (process-local) +
    SQLite L2 (cross-restart). Same 30-min TTL as before.
    """
    global _ensemble_cache, _ensemble_cache_cycle
    import time as _t

    with _ensemble_lock:
        now_bucket = int(_t.time() / 1800)
        if now_bucket != _ensemble_cache_cycle:
            _ensemble_cache = {}
            _ensemble_cache_cycle = now_bucket

        cache_key = ctx.segment

        # L1: in-process cache
        if cache_key in _ensemble_cache:
            logging.info("Using shared ensemble results for %s", cache_key)
            return _ensemble_cache[cache_key]

        # L2: persistent SQLite cache
        try:
            from shared_ai_cache import get as _cache_get, put as _cache_put
            persisted = _cache_get("ensemble", cache_key,
                                   bucket_seconds=1800)
            if persisted is not None:
                _ensemble_cache[cache_key] = persisted
                logging.info(
                    "Using persisted ensemble results for %s (from disk)",
                    cache_key,
                )
                return persisted
        except Exception:
            _cache_get = _cache_put = None

        from ensemble import run_ensemble
        result = run_ensemble(
            candidates_data, ctx,
            ai_provider=ctx.ai_provider,
            ai_model=ctx.ai_model,
            ai_api_key=ctx.ai_api_key,
        )
        _ensemble_cache[cache_key] = result
        if _cache_put is not None:
            try:
                _cache_put("ensemble", cache_key, result,
                           bucket_seconds=1800)
            except Exception:
                pass
        return result


# ---------------------------------------------------------------------------
# Lever 2 of COST_AND_QUALITY_LEVERS_PLAN.md — meta-model pre-gate.
# ---------------------------------------------------------------------------

def _meta_pregate_candidates(candidates: List[Dict[str, Any]],
                              ctx: Any) -> List[Dict[str, Any]]:
    """Drop candidates the meta-model deems likely-wrong before the
    ensemble runs. Returns the filtered list (in original order).

    Behavior:
    - If the profile has no trained meta-model yet → fall open, return
      all candidates (preserves current cold-start behavior).
    - If `meta_pregate_threshold = 0.0` (disabled) → fall open.
    - Otherwise: build a feature vector per candidate using the same
      shape as the live prediction-recording path, run the meta-model,
      keep candidates with `meta_prob >= threshold`.

    Quality mechanisms:
    - Specialists analyze a sharper cohort → relative confidence
      spreads carry more signal.
    - Final batch_select prompt has fewer-better candidates → more AI
      attention per remaining candidate.
    - Risk_assessor's VETO authority isn't wasted on already-doomed
      candidates.
    - Calibration data labels accumulate faster (more specialists'
      verdicts attach to candidates that actually became trades).

    Cost savings: roughly halves ensemble specialist calls on profiles
    with trained meta-models (since ~50% of shortlisted candidates
    typically map to meta_prob < 0.5 once a model is trained).
    """
    if not candidates:
        return candidates
    threshold = float(getattr(ctx, "meta_pregate_threshold", 0.5) or 0.0)
    if threshold <= 0:
        return candidates  # disabled — caller opted out
    profile_id = getattr(ctx, "profile_id", 0)
    if not profile_id:
        return candidates
    try:
        import meta_model
        meta_path = meta_model.model_path_for_profile(profile_id)
        meta_bundle = meta_model.load_model(meta_path)
        if meta_bundle is None:
            # No model yet — gate falls open during cold-start data
            # accumulation. Behavior identical to pre-Lever-2.
            return candidates
    except Exception:
        return candidates

    # Per-direction confidence: when the model was trained on too few
    # samples of a given direction, its predictions for that direction
    # are extrapolations (long-trained model scoring SHORT candidates)
    # and shouldn't be used to filter. Threshold = 30 matches the
    # MIN_SAMPLES_FOR_KELLY convention — below that, edge estimates
    # are too noisy to act on.
    #
    # Backwards-compat: models trained before this metrics field was
    # added don't carry n_train_short/n_train_long. For those, skip
    # the bypass and apply the threshold uniformly (old behavior).
    DIRECTION_CONFIDENCE_THRESHOLD = 30
    metrics = (meta_bundle or {}).get("metrics") or {}
    n_train_short = metrics.get("n_train_short")
    n_train_long = metrics.get("n_train_long")
    has_direction_counts = (n_train_short is not None
                             and n_train_long is not None)

    kept = []
    dropped_count = 0
    short_bypass_count = 0
    for c in candidates:
        try:
            # Build a partial feature dict from the candidate's
            # shortlist record. The full features_payload (with alt
            # data, sector context, etc.) is built later for storage,
            # but the meta-model's main inputs (signal, score, RSI,
            # technicals) are already on the candidate by the time
            # the shortlist is ranked.
            sig = c.get("signal", "HOLD")
            # P1.12 — derive likely prediction_type from the candidate's
            # strategy signal so the meta-model can apply direction-
            # specific feature weights even at pregate time (before the
            # AI has actually picked a direction).
            sig_upper = sig.upper()
            if sig_upper in ("SHORT", "STRONG_SHORT", "SELL", "STRONG_SELL"):
                inferred_ptype = "directional_short"
            else:
                inferred_ptype = "directional_long"
            # Insufficient-data bypass: when the model has < 30 samples
            # of this candidate's direction, pregate falls open for it.
            # Critical for profiles in the cold-start of building short
            # track record — without this, every short candidate gets
            # filtered by a long-trained model that has no idea how to
            # score them, defeating the whole long/short capability.
            if has_direction_counts:
                if (inferred_ptype == "directional_short"
                        and int(n_train_short) < DIRECTION_CONFIDENCE_THRESHOLD):
                    kept.append(c)
                    short_bypass_count += 1
                    continue
                if (inferred_ptype == "directional_long"
                        and int(n_train_long) < DIRECTION_CONFIDENCE_THRESHOLD):
                    kept.append(c)
                    continue
            features = {
                "symbol": c.get("symbol", ""),
                "signal": sig,
                "prediction_type": inferred_ptype,
                "score": c.get("score", 0),
                "rsi": c.get("rsi", 0),
                "volume_ratio": c.get("volume_ratio", 0),
                "atr": c.get("atr", 0),
                "adx": c.get("adx", 0),
                "stoch_rsi": c.get("stoch_rsi", 0),
                "roc_10": c.get("roc_10", 0),
                "pct_from_52w_high": c.get("pct_from_52w_high", 0),
                "mfi": c.get("mfi", 0),
                "cmf": c.get("cmf", 0),
                "gap_pct": c.get("gap_pct", 0),
                "pct_from_vwap": c.get("pct_from_vwap", 0),
            }
            meta_prob = meta_model.predict_probability(meta_bundle, features)
            if meta_prob is None:
                # Couldn't score this candidate — keep it (fail-open
                # at the per-candidate level too).
                kept.append(c)
                continue
            if meta_prob >= threshold:
                kept.append(c)
            else:
                dropped_count += 1
        except Exception:
            kept.append(c)

    if dropped_count > 0:
        logging.info(
            "Meta-pregate: dropped %d/%d candidates with meta_prob < %.2f "
            "before ensemble (saves %d specialist calls; sharpens cohort)",
            dropped_count, len(candidates), threshold, dropped_count * 4,
        )
    if short_bypass_count > 0:
        logging.info(
            "Meta-pregate: bypassed %d short candidates (model has "
            "n_train_short=%s < %d — insufficient direction-specific "
            "training data to score reliably)",
            short_bypass_count, n_train_short, DIRECTION_CONFIDENCE_THRESHOLD,
        )
    return kept


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

    # Determine the confidence threshold via the full override chain
    # (per-symbol > per-regime > per-TOD > global). The tuner's
    # per-symbol overrides take effect transparently when this symbol
    # has a meaningful track record.
    if ctx is not None:
        try:
            from regime_overrides import resolve_for_current_regime
            min_confidence = resolve_for_current_regime(
                ctx, "ai_confidence_threshold",
                default=ctx.ai_confidence_threshold,
                symbol=symbol)
        except Exception:
            min_confidence = ctx.ai_confidence_threshold
    else:
        min_confidence = AI_MIN_CONFIDENCE

    # Approval logic for BUY trades — threshold applies to ALL signals,
    # no bypass for BUY (removed 2026-04-23, was undermining self-tuner)
    if tech_direction == "BUY":
        if ai_signal == "SELL":
            print(f"VETOED (AI says SELL, confidence {ai_confidence})")
            return False, ai_result
        if ai_confidence < min_confidence:
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

    # Resolve parameters via the full override chain (per-symbol >
    # per-regime > per-TOD > global). A tuner-set per-symbol stop-loss
    # for NVDA will override the regime-specific stop-loss for volatile.
    if ctx is not None:
        try:
            from regime_overrides import resolve_for_current_regime
            if max_position_pct is None:
                max_position_pct = resolve_for_current_regime(
                    ctx, "max_position_pct",
                    default=ctx.max_position_pct, symbol=symbol)
            stop_loss_pct = resolve_for_current_regime(
                ctx, "stop_loss_pct",
                default=ctx.stop_loss_pct, symbol=symbol)
            take_profit_pct = resolve_for_current_regime(
                ctx, "take_profit_pct",
                default=ctx.take_profit_pct, symbol=symbol)
        except Exception:
            if max_position_pct is None:
                max_position_pct = ctx.max_position_pct
            stop_loss_pct = ctx.stop_loss_pct
            take_profit_pct = ctx.take_profit_pct

        # Layer 9 — apply the auto-allocator's capital_scale multiplier
        # to position sizing. capital_scale is normalized within each
        # Alpaca-account group so siblings sharing real capital sum to N.
        # Default 1.0 = unchanged; 0.5 = half-size; 2.0 = double-size.
        try:
            cap_scale = float(getattr(ctx, "capital_scale", 1.0) or 1.0)
            if cap_scale != 1.0 and max_position_pct is not None:
                max_position_pct = max_position_pct * cap_scale
        except Exception:
            pass
    else:
        if max_position_pct is None:
            max_position_pct = DEFAULT_MAX_POSITION_PCT
        stop_loss_pct = DEFAULT_STOP_LOSS_PCT
        take_profit_pct = DEFAULT_TAKE_PROFIT_PCT
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

        # Portfolio constraint check — Layer 3 regime-aware lookup
        try:
            from regime_overrides import resolve_for_current_regime
            max_total = (resolve_for_current_regime(
                ctx, "max_total_positions",
                default=ctx.max_total_positions)
                if ctx is not None else None)
        except Exception:
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

        # INTRADAY_STOPS_PLAN Stage 1 — cancel any broker stop attached
        # to this position so it doesn't fire after our market sell on
        # what's now a flat position.
        try:
            from bracket_orders import cancel_for_symbol
            cancel_for_symbol(api, db_path, symbol)
        except Exception:
            pass

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
            # Asymmetric short sizing (P1.6 of LONG_SHORT_PLAN.md):
            # cap shorts at short_max_position_pct, defaulting to half
            # the long cap when not explicitly set on the profile.
            short_cap_pct = (getattr(ctx, "short_max_position_pct", None)
                             if ctx else None)
            if short_cap_pct is None:
                short_cap_pct = max_position_pct / 2

            if action == "STRONG_SELL":
                alloc_pct = short_cap_pct
            else:
                alloc_pct = short_cap_pct * 0.75

            # Boost if AI confident
            if ai_confidence and ai_confidence >= 80:
                alloc_pct = min(alloc_pct * 1.25, short_cap_pct)

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
        if ctx is not None:
            try:
                from regime_overrides import resolve_for_current_regime
                max_position_pct = resolve_for_current_regime(
                    ctx, "max_position_pct",
                    default=ctx.max_position_pct)
            except Exception:
                max_position_pct = ctx.max_position_pct
        else:
            max_position_pct = DEFAULT_MAX_POSITION_PCT

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
    if ctx is not None:
        try:
            from regime_overrides import resolve_for_current_regime
            max_positions = resolve_for_current_regime(
                ctx, "max_total_positions", default=ctx.max_total_positions)
        except Exception:
            max_positions = ctx.max_total_positions
    else:
        max_positions = 10
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
            from journal import get_recently_exited, get_wash_cooldown_symbols
            cooldown_min = int(getattr(ctx, "reentry_cooldown_minutes", 60))
            recently_exited = get_recently_exited(ctx.db_path, cooldown_min)
            # Union with the longer (30-day) wash-trade cooldown so we
            # don't re-attempt buys Alpaca already rejected as wash.
            recently_exited |= get_wash_cooldown_symbols(ctx.db_path, 30)
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

        # Blacklist evaluation — note: symbols with 0% win rate across 3+
        # resolved predictions are NOT skipped here anymore. They flow
        # through full AI evaluation so new predictions keep getting
        # recorded. The execution-time blacklist gate (Step 4.95) is the
        # one that prevents capital from going into them. This inversion
        # is deliberate: pre-filtering blocked new predictions, which
        # meant a blacklisted stock's 0% win rate stayed 0% forever with
        # no path back to tradable. Now the AI keeps testing its call on
        # these stocks, and win_rate recovers organically — if the AI
        # starts predicting correctly, the blacklist check at execution
        # naturally releases the stock.
        # Note: the AI already sees `track_record` (e.g., "0W/3L (0% win
        # rate)") in candidates_data, so it has visibility into the poor
        # history without us injecting a dedicated "blacklisted" flag.

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
    # Note: blacklisted symbols flow through (they're blocked at the Step
    # 4.95 execution gate, not here). This keeps the AI's prediction
    # feedback loop alive on poorly-performing stocks so they can earn
    # their way back to tradable.
    logging.info(f"Pipeline: {len(candidates)} candidates -> {len(filtered_candidates)} after pre-filter "
                 f"({len(pre_filter_skips)} removed: "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'EARNINGS_SKIP')} earnings, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'SKIP')} max-positions, "
                 f"{sum(1 for s in pre_filter_skips if s['action'] == 'COOLDOWN')} cooldown)")

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

    target_short_pct_for_rank = float(
        getattr(ctx, "target_short_pct", 0.0) or 0.0
    ) if ctx else 0.0
    shortlist = _rank_candidates(strategy_results, held_symbols, enable_shorts,
                                  deprecated_strategies=deprecated_types,
                                  target_short_pct=target_short_pct_for_rank)

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

    # ── STEP 3.65: Meta-model pre-gate (Lever 2 of COST_AND_QUALITY_LEVERS_PLAN.md)
    # ── Drop candidates the meta-model thinks are likely-wrong BEFORE
    # ── the ensemble runs. Saves ~50% of specialist API calls (cost),
    # ── sharpens the surviving cohort the specialists analyze (quality),
    # ── and makes the calibration data accumulate ~2x faster (because
    # ── more of each specialist's verdicts get a labeled outcome).
    # ──
    # ── Falls open when the meta-model isn't trained yet — `predict_probability`
    # ── returns None on cold-start, so the gate passes all candidates.
    # ── Per-profile threshold via `meta_pregate_threshold` (default 0.5,
    # ── 0.0 = disabled).
    candidates_data = _meta_pregate_candidates(candidates_data, ctx)

    update_status(_pid, "Specialist ensemble", "%d candidates" % len(candidates_data))
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
                    # Carry the per-specialist verdicts forward so we
                    # can persist them with the prediction (Wave 3 /
                    # Fix #9 — specialist confidence calibration).
                    c["ensemble_specialists"] = verdict.get("specialists", [])
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

            # Classify the prediction so the resolver applies the right
            # win/loss criteria (see LONG_SHORT_PLAN.md §1.0). SELL on a
            # held LONG = exit-quality question; SELL on something we don't
            # hold = directional-bearish question. Lumping them together
            # made 'Avg Move on SELLs' uninterpretable.
            sig_upper = (pred_signal or "").upper()
            held_pos = positions_dict.get(sym)
            held_qty = float(held_pos.get("qty", 0)) if held_pos else 0.0
            if sig_upper == "BUY":
                pred_type = "directional_long"
            elif sig_upper == "SHORT":
                pred_type = "directional_short"
            elif sig_upper == "SELL":
                if held_qty > 0:
                    pred_type = "exit_long"
                elif held_qty < 0:
                    pred_type = "exit_short"
                else:
                    # AI hallucinated SELL on something we don't hold —
                    # treat as directional_short.
                    pred_type = "directional_short"
            else:
                # HOLD or unknown — keep neutral classification
                pred_type = "directional_long"

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
            # Capture days-to-earnings so the self-tuner's
            # _optimize_avoid_earnings_days rule can bucket resolved
            # predictions by proximity to earnings. Negative values
            # mean earnings already past or unknown — store as -1.
            try:
                from earnings_calendar import check_earnings as _ck_earn
                _ec = _ck_earn(sym)
                features_payload["days_to_earnings"] = (
                    int(_ec["days_until"]) if _ec and _ec.get("days_until") is not None
                    else -1
                )
            except Exception:
                features_payload["days_to_earnings"] = -1
            alt = c.get("alt_data") or {}
            if alt:
                features_payload["insider_direction"] = alt.get("insider", {}).get("net_direction", "neutral")
                features_payload["short_pct_float"] = alt.get("short", {}).get("short_pct_float", 0)
                features_payload["options_signal"] = alt.get("options", {}).get("signal", "neutral")
                features_payload["put_call_ratio"] = alt.get("options", {}).get("put_call_ratio", 0)
                features_payload["vwap_position"] = alt.get("intraday", {}).get("vwap_position", "at")
                features_payload["pe_trailing"] = alt.get("fundamentals", {}).get("pe_trailing", 0)
                # New per-symbol alt data
                features_payload["congress_direction"] = alt.get("congressional", {}).get("net_direction", "neutral")
                features_payload["finra_short_vol_ratio"] = alt.get("finra_short_vol", {}).get("short_volume_ratio", 0)
                features_payload["insider_cluster"] = 1 if alt.get("insider_cluster", {}).get("is_cluster") else 0
                features_payload["eps_revision_direction"] = alt.get("analyst_estimates", {}).get("eps_revision_direction", "flat")
                features_payload["eps_revision_magnitude"] = alt.get("analyst_estimates", {}).get("revision_magnitude_pct", 0)
                features_payload["insider_near_earnings"] = alt.get("insider_earnings", {}).get("insider_direction_near_earnings", "neutral")
                features_payload["dark_pool_pct"] = alt.get("dark_pool", {}).get("ats_pct_of_total", 0)
                features_payload["earnings_surprise_streak"] = alt.get("earnings_surprise", {}).get("streak", 0)
                features_payload["earnings_surprise_direction"] = alt.get("earnings_surprise", {}).get("surprise_direction", "mixed")
                # 4 local-SQLite alt-data sources — flatten into
                # features_payload so the meta-model can train on them
                # AND the Layer 2 weight tuner's `is_active` predicates
                # can look them up at decision time.
                cong = alt.get("congressional_recent") or {}
                features_payload["congressional_trades_60d"] = cong.get("trades_60d", 0)
                features_payload["congressional_dollar_volume_60d"] = cong.get("dollar_volume_60d", 0)
                features_payload["congressional_net_direction"] = cong.get("net_direction", "neutral")
                inst = alt.get("institutional_13f") or {}
                features_payload["institutional_13f_holders"] = inst.get("total_holders", 0)
                features_payload["institutional_13f_qoq_pct"] = inst.get("qoq_share_change_pct") or 0
                bio = alt.get("biotech_milestones") or {}
                features_payload["biotech_days_to_pdufa"] = bio.get("days_to_pdufa")
                features_payload["biotech_phase3_count"] = bio.get("active_phase3_count", 0)
                twits = alt.get("stocktwits_sentiment") or {}
                features_payload["stocktwits_message_count_7d"] = twits.get("message_count_7d", 0)
                features_payload["stocktwits_net_sentiment_7d"] = twits.get("net_sentiment_7d")
                features_payload["stocktwits_is_trending"] = 1 if twits.get("is_trending") else 0
            social = c.get("social") or {}
            if social:
                features_payload["reddit_mentions"] = social.get("mentions", 0)
                features_payload["reddit_sentiment"] = social.get("sentiment_score", 0)
            # Market context
            features_payload["_regime"] = current_regime
            features_payload["_market_signal_count"] = len([v for v in votes.values() if v != "HOLD"])
            # Market-wide macro features
            _macro = market_ctx.get("macro_context", {})
            _yc = _macro.get("yield_curve", {})
            features_payload["_yield_spread_10y2y"] = _yc.get("spread_10y_2y", 0)
            features_payload["_curve_status"] = _yc.get("curve_status", "normal")
            features_payload["_cboe_skew"] = _macro.get("cboe_skew", {}).get("skew_value", 0)
            _fm = _macro.get("fred_macro", {})
            features_payload["_unemployment_rate"] = _fm.get("unemployment_rate", 0)
            features_payload["_cpi_yoy"] = _fm.get("cpi_yoy", 0)
            features_payload["_rotation_phase"] = _macro.get("sector_momentum", {}).get("rotation_phase", "mixed")
            features_payload["_market_gex_regime"] = _macro.get("market_gex", {}).get("net_regime", "balanced")

            pred_id = record_prediction(
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
                prediction_type=pred_type,
            )
            # Wave 3 / Fix #9 — log the per-specialist verdicts that
            # contributed to this prediction so the calibrators can
            # learn from each specialist's track record.
            if pred_id and pred_id > 0 and ctx and getattr(ctx, "db_path", None):
                specialists_for_pred = c.get("ensemble_specialists", [])
                if specialists_for_pred:
                    try:
                        from specialist_calibration import record_outcomes_for_prediction
                        record_outcomes_for_prediction(
                            ctx.db_path, pred_id, specialists_for_pred,
                        )
                    except Exception as _exc:
                        logging.warning(
                            "Failed to record specialist outcomes "
                            "for prediction %s: %s", pred_id, _exc,
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

    # ── STEP 4.95: Blacklist gate (capital protection) ────────────────
    # Symbols with 0% win rate across 3+ resolved AI predictions are
    # blocked from NEW ENTRIES (BUY/SHORT). The prediction record was
    # already written in Step 4 so the AI keeps building data on these
    # stocks — if its predictions start winning, win_rate rises above 0
    # and the stock falls off the blacklist automatically without any
    # manual intervention. Exits (SELL/COVER) are never blocked — we
    # always want to let positions close.
    blacklist_blocked = []
    if symbol_reputation and ai_trades:
        filtered = []
        for t in ai_trades:
            sym = t.get("symbol", "")
            action = (t.get("action") or "").upper()
            is_entry = action in ("BUY", "SHORT")
            rep = symbol_reputation.get(sym)
            if is_entry and rep and rep.get("win_rate", 1) == 0 and rep.get("total", 0) >= 3:
                blacklist_blocked.append({
                    "symbol": sym,
                    "action": action,
                    "losses": rep.get("total", 0),
                    "ai_confidence": t.get("confidence"),
                })
                logging.info(
                    f"  Blacklist gate BLOCKED {action} {sym}: "
                    f"0/{rep['total']} win rate — keeping prediction recorded "
                    f"for re-evaluation."
                )
                if ctx is not None:
                    try:
                        from models import log_activity
                        log_activity(
                            ctx.profile_id, ctx.user_id, "blacklist_block",
                            f"Blacklist blocked {action} {sym}",
                            f"AI wanted to trade but symbol has 0/{rep['total']} "
                            f"win rate on resolved predictions. Prediction "
                            f"recorded; stock re-evaluates automatically as "
                            f"new predictions resolve.",
                            symbol=sym,
                        )
                    except Exception:
                        pass
                continue
            filtered.append(t)
        if blacklist_blocked:
            print(
                f"  Blacklist gate: blocked {len(blacklist_blocked)} entries "
                f"({', '.join(b['symbol'] for b in blacklist_blocked)})"
            )
            # Surface to the pipeline output so the dashboard shows these
            for b in blacklist_blocked:
                details.append({
                    "symbol": b["symbol"],
                    "action": "BLACKLIST_BLOCKED",
                    "reason": (
                        f"AI wanted {b['action']} but 0/{b['losses']} win "
                        f"rate on resolved predictions. Prediction still "
                        f"recorded for re-evaluation."
                    ),
                })
        ai_trades = filtered

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
            # Visibility: when the trade dict says SKIP / EXCLUDED /
            # ERROR / etc., surface it. The previous behavior was
            # silent — the user only saw "Executing: SHORT X" and
            # never knew the order was rejected. Caused 2026-04-28
            # confusion when SHORT VALE printed "Executing" but no
            # order was actually submitted.
            ta = (trade_result or {}).get("action") if isinstance(trade_result, dict) else None
            if ta and ta not in ("BUY", "SELL", "SHORT", "COVER"):
                logging.warning(
                    "Trade NOT submitted for %s (%s): action=%s reason=%s",
                    symbol, action, ta,
                    (trade_result or {}).get("reason", "no reason given"),
                )
        except Exception as exc:
            # Classify known Alpaca rejections that aren't really errors
            # (the system did the right thing; the broker just won't
            # let us). These get logged as WARNING + SKIP, no traceback,
            # and a per-symbol cooldown so we don't re-attempt every
            # cycle.
            msg_lower = str(exc).lower()
            if "wash trade" in msg_lower:
                try:
                    from journal import record_wash_cooldown
                    record_wash_cooldown(ctx.db_path if ctx else None, symbol)
                except Exception:
                    pass
                details.append({
                    "symbol": symbol, "action": "SKIP",
                    "reason": "Alpaca rejected: potential wash trade — "
                              "deferring re-attempt for 30 days",
                })
                logging.warning(
                    "Wash-trade detected on %s — recording cooldown, "
                    "will retry after 30 days. (%s)",
                    symbol, exc,
                )
            elif "insufficient qty" in msg_lower or "insufficient buying power" in msg_lower:
                # Recoverable broker rejection — not a code bug.
                details.append({
                    "symbol": symbol, "action": "SKIP",
                    "reason": f"Alpaca rejected: {exc}",
                })
                logging.warning(
                    "Broker rejected order for %s (%s): %s",
                    symbol, action, exc,
                )
            else:
                # Genuine error — keep the noisy traceback for
                # diagnosis.
                errors.append({"symbol": symbol, "error": str(exc)})
                details.append({"symbol": symbol, "action": "ERROR", "reason": str(exc)})
                logging.error(
                    "Trade execution raised for %s (%s): %s",
                    symbol, action, exc, exc_info=True,
                )

    # Build summary
    buys = [d for d in details if d.get("action") == "BUY"]
    sells = [d for d in details if d.get("action") == "SELL"]
    shorts = [d for d in details if d.get("action") == "SHORT"]
    holds_count = len([s for s in strategy_results if s.get("signal") == "HOLD"])
    skips = [d for d in details if d.get("action") in ("SKIP", "BLOCKED", "NONE",
                                                         "DRAWDOWN_PAUSE", "EXCLUDED",
                                                         "BLACKLIST_BLOCKED", "EARNINGS_SKIP",
                                                         "COOLDOWN")]

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

# P1.4 of LONG_SHORT_PLAN.md — market-regime classifier for short gating.
# When the market is in a strong-bull regime (SPY above 200d MA AND 20d
# MA above 50d MA), routine technical shorts get filtered out — the
# secular drift overpowers most short setups. Only catalyst / fundamental
# shorts continue. In neutral or bear regimes, the full short slate flows.
_REGIME_CACHE: Dict[str, Tuple[float, str]] = {}
_REGIME_CACHE_TTL = 1800  # 30 minutes; regime doesn't flip intra-cycle

# Strategies whose short signal carries an explicit fundamental thesis,
# allowed to flow through even in strong-bull regimes.
_CATALYST_SHORT_STRATEGIES = {
    "insider_selling_cluster",
    "distribution_at_highs",
    "earnings_drift",            # post-earnings disappointment
    "analyst_upgrade_drift",     # downgrade-after-upgrade thesis
    "earnings_disaster_short",   # P3.1 — gap-down + non-recovery pattern
    "catalyst_filing_short",     # P3.2 — adverse SEC filing + price weakness
}


def _classify_market_regime() -> str:
    """Return 'strong_bull' | 'neutral' | 'bear' from SPY trend.

    Best-effort: any data failure returns 'neutral' which keeps the
    short slate flowing. Cached 30 minutes.
    """
    import time
    cached = _REGIME_CACHE.get("market")
    if cached and (time.time() - cached[0]) < _REGIME_CACHE_TTL:
        return cached[1]
    regime = "neutral"
    try:
        from market_data import get_bars as _get_bars_for_regime
        spy = _get_bars_for_regime("SPY", limit=210)
        if spy is not None and len(spy) >= 200:
            close_now = float(spy["close"].iloc[-1])
            sma_20 = float(spy["close"].iloc[-20:].mean())
            sma_50 = float(spy["close"].iloc[-50:].mean())
            sma_200 = float(spy["close"].iloc[-200:].mean())
            if close_now > sma_200 and sma_20 > sma_50:
                regime = "strong_bull"
            elif close_now < sma_200 and sma_20 < sma_50:
                regime = "bear"
    except Exception:
        pass
    _REGIME_CACHE["market"] = (time.time(), regime)
    return regime


# P1.3 of LONG_SHORT_PLAN.md — squeeze risk filter.
# High short interest + low float = squeeze risk. One squeeze can
# wipe out months of short gains. We filter HIGH risk completely
# and pass MED through (with the understanding that AI sees the
# context and can still skip).
_SQUEEZE_CACHE: Dict[str, Tuple[float, str]] = {}
_SQUEEZE_CACHE_TTL = 86400  # 24h


def _squeeze_risk(symbol: str) -> str:
    """Return 'HIGH' | 'MED' | 'LOW'. Wraps alternative_data.get_short_interest
    which already classifies risk based on short_pct_float + short_ratio.
    Conservative on errors (returns 'LOW' so we don't accidentally block
    legitimate setups when data is missing).
    """
    import time
    cached = _SQUEEZE_CACHE.get(symbol.upper())
    if cached and (time.time() - cached[0]) < _SQUEEZE_CACHE_TTL:
        return cached[1]
    risk = "LOW"
    try:
        from alternative_data import get_short_interest
        info = get_short_interest(symbol) or {}
        risk = (info.get("squeeze_risk") or "low").upper()
        if risk == "MEDIUM":
            risk = "MED"
    except Exception:
        pass
    _SQUEEZE_CACHE[symbol.upper()] = (time.time(), risk)
    return risk


def _rank_candidates(strategy_results, held_symbols, enable_shorts,
                      deprecated_strategies=None,
                      target_short_pct=0.0):
    """Rank strategy results into a shortlist for AI batch review.

    When shorts are disabled: returns top ~15 long candidates by abs(score),
    and SELL/SHORT signals from non-held symbols are filtered out (existing
    behavior).

    target_short_pct (0.0-1.0): when ≥ 0.4, the profile is configured for
    a substantial short book and the user has accepted the regime risk.
    The market regime gate is bypassed so technical shorts can flow even
    in strong_bull — without this override, dedicated short profiles
    (target_short_pct=0.5) cannot emit shorts in extended bull regimes.

    When shorts are enabled: reserves dedicated slots for shorts
    (P1.7 of LONG_SHORT_PLAN.md). Top 10 longs + top 5 shorts. This
    prevents bearish candidates from being crowded out of the top-15
    when most strategies emit bullish signals — the previous
    abs(score)-only ranking sent ~0-1 short candidates to the AI even
    on shorts-enabled profiles, which is the whole reason
    profile_10 'Small Cap Shorts' has emitted only 2 SHORT predictions
    in 1,491 cycles.

    Filters HOLD, BUYs on held symbols, and primary-strategy
    deprecation in either path.
    """
    deprecated_strategies = deprecated_strategies or set()

    def _is_long_action(a):
        return a in ("BUY", "STRONG_BUY")

    def _is_short_action(a):
        return a in ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT")

    long_eligible = []
    short_eligible = []
    short_skips = {"borrow": 0, "squeeze": 0, "regime": 0}
    market_regime = _classify_market_regime() if enable_shorts else "neutral"

    for signal in strategy_results:
        symbol = signal.get("symbol", "")
        action = signal.get("signal", "HOLD")

        if action == "HOLD":
            continue
        if _is_short_action(action) and symbol not in held_symbols and not enable_shorts:
            continue
        if _is_long_action(action) and symbol in held_symbols:
            continue

        # Phase 3: skip candidates whose primary voting strategy is deprecated.
        if deprecated_strategies:
            votes = signal.get("votes", {})
            primary = next((k for k, v in votes.items() if v != "HOLD"), None)
            if primary and primary in deprecated_strategies:
                continue

        # P1.2 / P1.3 / P1.4 / P1.14 — quality filters on SHORT candidates.
        # Long candidates pass through unchanged.
        if _is_short_action(action) and symbol not in held_symbols:
            # 1.2 Borrow availability — Alpaca asset endpoint
            from client import get_borrow_info
            borrow = get_borrow_info(symbol)
            if not borrow.get("shortable", True):
                short_skips["borrow"] += 1
                continue
            # P1.14 — annotate the candidate with borrow cost so the AI
            # can see it and the sizer can adjust. Alpaca's asset
            # endpoint doesn't return a numeric borrow rate (that's
            # paid 3rd-party data), but the easy_to_borrow flag is a
            # reliable proxy: True ≈ ~1% annual; False ≈ 5-50%+ annual
            # (HTB names cost real money to short over multi-day holds).
            signal["_borrow_cost"] = (
                "low" if borrow.get("easy_to_borrow") else "high"
            )
            # 1.3 Squeeze risk — high short interest + low float
            risk = _squeeze_risk(symbol)
            if risk == "HIGH":
                short_skips["squeeze"] += 1
                continue
            signal["_squeeze_risk"] = risk
            # 1.4 Regime gate — strong-bull market suppresses routine
            # technical shorts; catalyst shorts pass through. Skipped
            # when the profile mandates a substantial short book
            # (target_short_pct ≥ 0.4) — the user has explicitly
            # accepted regime-side risk for that profile, and gating
            # would prevent the mandate from ever being filled.
            if market_regime == "strong_bull" and target_short_pct < 0.4:
                primary = next(
                    (k for k, v in (signal.get("votes") or {}).items()
                     if v != "HOLD"), None
                )
                if primary not in _CATALYST_SHORT_STRATEGIES:
                    short_skips["regime"] += 1
                    continue

        if _is_short_action(action):
            short_eligible.append(signal)
        else:
            long_eligible.append(signal)

    if enable_shorts and any(short_skips.values()):
        logging.info(
            "Short candidate filters (regime=%s): %d filtered for borrow, "
            "%d for squeeze risk, %d for regime gate",
            market_regime, short_skips["borrow"],
            short_skips["squeeze"], short_skips["regime"],
        )

    sort_key = lambda s: (abs(s.get("score", 0)),
                          abs(s.get("rsi", 50) - 50))
    long_eligible.sort(key=sort_key, reverse=True)
    short_eligible.sort(key=sort_key, reverse=True)

    if enable_shorts:
        # Reserve slots: top 10 longs + top 5 shorts. If short bench is
        # short, fill remaining slots with more longs (don't waste capacity).
        shorts = short_eligible[:5]
        long_slots = max(10, 15 - len(shorts))
        longs = long_eligible[:long_slots]
        return longs + shorts
    else:
        return long_eligible[:15]


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

        # Per-stock track record + last prediction reasoning.
        #
        # The string is split by signal type (BUY / SHORT / HOLD)
        # so the AI can cite signal-specific edge instead of lumping
        # HOLD outcomes into a SHORT decision narrative. See the
        # 2026-04-28 confabulation incident: AI claimed "100% on VALE
        # SHORTs" when all 13 wins were HOLDs and zero SHORTs had
        # resolved. Aggregate is shown alongside the per-signal lines
        # so the AI sees both views.
        if symbol in symbol_reputation:
            rep = symbol_reputation[symbol]
            by_sig = rep.get("by_signal", {})
            parts = []
            for sig in ("BUY", "SHORT", "SELL", "HOLD"):
                s = by_sig.get(sig)
                if s and s["total"] > 0:
                    parts.append(
                        f"{sig} {s['wins']}W/{s['losses']}L "
                        f"({s['win_rate']:.0f}%)"
                    )
            sig_breakdown = "; ".join(parts) if parts else "no resolved signals"
            entry["track_record"] = (
                f"{rep['wins']}W/{rep['losses']}L overall "
                f"({rep['win_rate']:.0f}%) — {sig_breakdown}"
            )

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
                # Add earnings transcript sentiment (requires ctx for AI call)
                try:
                    from sec_filings import get_earnings_call_sentiment
                    alt["transcript_sentiment"] = get_earnings_call_sentiment(symbol, ctx=ctx)
                except Exception:
                    pass
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
    # P2.1 of LONG_SHORT_PLAN.md — compute sector-exposure breakdown
    # and pass it through so the AI prompt can surface concentration
    # warnings ("you're already 35% long Tech, don't stack another").
    exposure = None
    try:
        from portfolio_exposure import compute_exposure
        equity = float(account.get("equity", 0) or 0)
        if equity > 0 and positions_list:
            exposure = compute_exposure(positions_list, equity)
    except Exception:
        pass

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
        "peak_equity": dd.get("peak_equity", 0),
        "exposure": exposure,
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
        # Post-mortem patterns from losing-week analysis. Prepended so
        # the most recent post-mortem reads first in the AI prompt.
        try:
            from post_mortem import get_active_patterns
            pm_patterns = get_active_patterns(ctx.db_path)
            if pm_patterns:
                learned_patterns = pm_patterns + list(learned_patterns)
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

    # Macro data (yield curve, CBOE skew, economic indicators, ETF flows)
    macro_context = {}
    try:
        from macro_data import get_all_macro_data
        macro_context = get_all_macro_data()
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
        "macro_context": macro_context,
    }
