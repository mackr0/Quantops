"""AI integration for trading analysis (multi-provider)."""

import json
import logging

import config
from ai_providers import call_ai
from client import get_api
from market_data import get_bars, add_indicators

logger = logging.getLogger(__name__)


def get_claude_client(api_key=None):
    """Return an authenticated Anthropic client (backward compat for CLI).

    Parameters
    ----------
    api_key : str, optional
        Anthropic API key.  Falls back to config.ANTHROPIC_API_KEY when
        not provided.

    Note: New code should use ai_providers.call_ai() instead.
    """
    import anthropic
    key = api_key or config.ANTHROPIC_API_KEY
    if not key:
        raise ValueError(
            "Missing ANTHROPIC_API_KEY. Add it to your .env file."
        )
    return anthropic.Anthropic(api_key=key)


def analyze_symbol(symbol, ctx=None, api=None, political_context=None):
    """
    Fetch market data for *symbol*, add technical indicators, and ask Claude
    for a structured trading recommendation.

    Parameters
    ----------
    symbol : str
        Ticker symbol.
    ctx : UserContext, optional
        If provided, uses ctx for Anthropic client and model name, and
        ctx for the Alpaca API client.
    api : alpaca REST client, optional
        Pre-built API client.  Falls back to get_api(ctx) when not provided.
    political_context : str, optional
        If provided (from MAGA Mode), appended to the prompt so Claude
        factors political/macro context into its recommendation.

    Returns a dict with keys: signal, confidence, reasoning, risk_factors,
    price_targets (entry, stop_loss, take_profit).
    """
    try:
        api = api or get_api(ctx)
        df = get_bars(symbol, limit=100, api=api)
        df = add_indicators(df)
        df = df.dropna()

        if df.empty:
            return {
                "symbol": symbol,
                "signal": "HOLD",
                "confidence": 0,
                "reasoning": "Not enough data to analyze.",
                "risk_factors": [],
                "price_targets": {},
            }

        latest = df.iloc[-1]
        recent = df.tail(10)

        # Build a concise technical summary for the prompt
        tech_summary = {
            "symbol": symbol,
            "current_price": float(latest["close"]),
            "volume": int(latest["volume"]),
            "sma_20": float(latest["sma_20"]),
            "sma_50": float(latest["sma_50"]),
            "ema_12": float(latest["ema_12"]),
            "rsi": float(latest["rsi"]),
            "macd": float(latest["macd"]),
            "macd_signal": float(latest["macd_signal"]),
            "macd_histogram": float(latest["macd_histogram"]),
            "bb_upper": float(latest["bb_upper"]),
            "bb_lower": float(latest["bb_lower"]),
            "bb_middle": float(latest["bb_middle"]),
            "volume_sma_20": float(latest["volume_sma_20"]),
            "recent_closes": [float(row["close"]) for _, row in recent.iterrows()],
            "recent_volumes": [int(row["volume"]) for _, row in recent.iterrows()],
        }

        prompt = (
            "You are a quantitative trading analyst. Analyze the following "
            "technical data and provide a trading recommendation.\n\n"
            f"Technical Data:\n{json.dumps(tech_summary, indent=2)}\n\n"
            "Respond ONLY with valid JSON (no markdown fences) using this exact schema:\n"
            "{\n"
            '  "signal": "BUY" | "SELL" | "HOLD",\n'
            '  "confidence": <integer 0-100>,\n'
            '  "reasoning": "<string explaining the analysis>",\n'
            '  "risk_factors": ["<risk1>", "<risk2>", ...],\n'
            '  "price_targets": {\n'
            '    "entry": <float>,\n'
            '    "stop_loss": <float>,\n'
            '    "take_profit": <float>\n'
            "  }\n"
            "}\n\n"
            "Base your analysis on the indicator values, price action, and "
            "volume trends provided. Be specific and quantitative in your "
            "reasoning."
        )

        # Concise context: market regime, stock history, overall win rate, earnings
        # (replaces verbose build_performance_context for cleaner AI prompts)
        if ctx is not None:
            try:
                from self_tuning import build_concise_context
                concise = build_concise_context(ctx, symbol=symbol)
                if concise:
                    prompt += f"\n\nCONTEXT:\n{concise}"
            except Exception as _ctx_err:
                logger.warning("Failed to build concise context: %s", _ctx_err)

        # Append political/macro context when MAGA Mode is active
        if political_context:
            prompt += (
                "\n\nPOLITICAL/MACRO CONTEXT:\n"
                f"{political_context}\n"
                "If technical weakness looks driven by political noise rather "
                "than fundamentals, factor in mean reversion likelihood."
            )

        # Call AI provider (multi-provider via ai_providers.call_ai)
        response_text = call_ai(
            prompt,
            provider=ctx.ai_provider if ctx else "anthropic",
            model=ctx.ai_model if ctx else config.CLAUDE_MODEL,
            api_key=ctx.ai_api_key if ctx else config.ANTHROPIC_API_KEY,
            db_path=getattr(ctx, "db_path", None) if ctx else None,
            purpose="single_analyze",
        )

        # Track API usage
        if ctx is not None:
            try:
                from models import increment_api_usage
                increment_api_usage(ctx.user_id)
            except Exception as _usage_err:
                logger.warning("Failed to increment API usage: %s", _usage_err)

        result = json.loads(response_text)
        result["symbol"] = symbol
        return result

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Claude response as JSON: %s", exc)
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "confidence": 0,
            "reasoning": f"AI response was not valid JSON: {exc}",
            "risk_factors": ["ai_parse_error"],
            "price_targets": {},
        }
    except Exception as exc:
        logger.error("Error in analyze_symbol for %s: %s", symbol, exc)
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "confidence": 0,
            "reasoning": f"Analysis failed: {exc}",
            "risk_factors": ["analysis_error"],
            "price_targets": {},
        }


def analyze_symbol_consensus(symbol, ctx=None, api=None, political_context=None):
    """Run analysis through primary model, then if STRONG signal, get second opinion.

    Returns same dict as analyze_symbol but with additional keys:
    - consensus: True if both models agree, False if not
    - primary_signal: what the primary model said
    - secondary_signal: what the secondary model said (or None)
    - secondary_model: which model was used for second opinion
    """
    # Step 1: Call analyze_symbol normally with user's chosen model
    result = analyze_symbol(symbol, ctx=ctx, api=api, political_context=political_context)

    signal = result.get("signal", "HOLD").upper()
    result["primary_signal"] = signal
    result["secondary_signal"] = None
    result["secondary_model"] = None
    result["consensus"] = True  # default: agree with self

    # Only seek consensus on actionable signals
    actionable = {"STRONG_BUY", "STRONG_SELL", "BUY", "SELL"}
    if signal not in actionable:
        return result

    # Check if consensus is enabled
    if ctx is None or not getattr(ctx, "enable_consensus", False):
        return result

    # Determine secondary model
    consensus_model = getattr(ctx, "consensus_model", "") or ""
    if not consensus_model:
        logger.info("Consensus enabled but no secondary model configured — skipping")
        return result

    # Determine the API key for the secondary model
    from ai_providers import get_provider_for_model
    secondary_provider = get_provider_for_model(consensus_model)
    if not secondary_provider:
        logger.warning("Could not determine provider for consensus model %s", consensus_model)
        return result

    primary_provider = ctx.ai_provider if ctx else "anthropic"

    # Determine which API key to use for the secondary model
    if secondary_provider == primary_provider:
        # Same provider — use the primary API key
        secondary_api_key = ctx.ai_api_key
    else:
        # Different provider — need a separate key
        secondary_api_key = getattr(ctx, "consensus_api_key", "") or ""
        if not secondary_api_key:
            logger.info(
                "Consensus: secondary model %s is provider %s but no consensus_api_key set — skipping",
                consensus_model, secondary_provider,
            )
            return result

    result["secondary_model"] = consensus_model

    # Step 2: Build the same prompt and call secondary model
    try:
        api_client = api or get_api(ctx)
        df = get_bars(symbol, limit=100, api=api_client)
        df = add_indicators(df)
        df = df.dropna()

        if df.empty:
            return result

        latest = df.iloc[-1]
        recent = df.tail(10)

        tech_summary = {
            "symbol": symbol,
            "current_price": float(latest["close"]),
            "volume": int(latest["volume"]),
            "sma_20": float(latest["sma_20"]),
            "sma_50": float(latest["sma_50"]),
            "ema_12": float(latest["ema_12"]),
            "rsi": float(latest["rsi"]),
            "macd": float(latest["macd"]),
            "macd_signal": float(latest["macd_signal"]),
            "macd_histogram": float(latest["macd_histogram"]),
            "bb_upper": float(latest["bb_upper"]),
            "bb_lower": float(latest["bb_lower"]),
            "bb_middle": float(latest["bb_middle"]),
            "volume_sma_20": float(latest["volume_sma_20"]),
            "recent_closes": [float(row["close"]) for _, row in recent.iterrows()],
            "recent_volumes": [int(row["volume"]) for _, row in recent.iterrows()],
        }

        prompt = (
            "You are a quantitative trading analyst. Analyze the following "
            "technical data and provide a trading recommendation.\n\n"
            f"Technical Data:\n{json.dumps(tech_summary, indent=2)}\n\n"
            "Respond ONLY with valid JSON (no markdown fences) using this exact schema:\n"
            "{\n"
            '  "signal": "BUY" | "SELL" | "HOLD",\n'
            '  "confidence": <integer 0-100>,\n'
            '  "reasoning": "<string explaining the analysis>",\n'
            '  "risk_factors": ["<risk1>", "<risk2>", ...],\n'
            '  "price_targets": {\n'
            '    "entry": <float>,\n'
            '    "stop_loss": <float>,\n'
            '    "take_profit": <float>\n'
            "  }\n"
            "}\n\n"
            "Base your analysis on the indicator values, price action, and "
            "volume trends provided. Be specific and quantitative in your "
            "reasoning."
        )

        if political_context:
            prompt += (
                "\n\nAdditionally, consider the following political/macro "
                "context when making your recommendation:\n"
                f"{political_context}\n\n"
                "If the current technical weakness appears to be driven by "
                "political noise rather than fundamental deterioration, factor "
                "in the likelihood of a mean reversion bounce."
            )

        secondary_text = call_ai(
            prompt,
            provider=secondary_provider,
            model=consensus_model,
            api_key=secondary_api_key,
            db_path=getattr(ctx, "db_path", None) if ctx else None,
            purpose="consensus_secondary",
        )

        # Track secondary API usage
        if ctx is not None:
            try:
                from models import increment_api_usage
                increment_api_usage(ctx.user_id)
            except Exception:
                pass

        secondary_result = json.loads(secondary_text)
        secondary_signal = secondary_result.get("signal", "HOLD").upper()
        result["secondary_signal"] = secondary_signal

        # Determine direction agreement
        primary_direction = "BUY" if "BUY" in signal else "SELL" if "SELL" in signal else "HOLD"
        secondary_direction = "BUY" if "BUY" in secondary_signal else "SELL" if "SELL" in secondary_signal else "HOLD"

        if primary_direction == secondary_direction and primary_direction != "HOLD":
            # Both agree on direction — boost confidence by 10%
            result["consensus"] = True
            original_conf = result.get("confidence", 0)
            result["confidence"] = min(100, int(original_conf * 1.10))
            logger.info(
                "Consensus AGREE on %s for %s: primary=%s, secondary=%s (confidence %d->%d)",
                primary_direction, symbol, signal, secondary_signal,
                original_conf, result["confidence"],
            )
        else:
            # Disagree — downgrade to HOLD
            result["consensus"] = False
            result["signal"] = "HOLD"
            logger.info(
                "Consensus DISAGREE on %s: primary=%s, secondary=%s — downgrading to HOLD",
                symbol, signal, secondary_signal,
            )

    except json.JSONDecodeError as exc:
        logger.warning("Consensus: secondary model returned invalid JSON for %s: %s", symbol, exc)
        # Treat as "no consensus available" — proceed with primary only
        result["secondary_signal"] = "PARSE_ERROR"
    except Exception as exc:
        logger.warning("Consensus: secondary model call failed for %s: %s", symbol, exc)
        # Proceed with primary only
        result["secondary_signal"] = "ERROR"

    return result


def analyze_portfolio_risk(positions, account_info, ctx=None):
    """
    Send the full portfolio and account info to Claude for a holistic risk
    assessment.

    Parameters
    ----------
    positions : list[dict]
        Output of client.get_positions().
    account_info : dict
        Output of client.get_account_info().
    ctx : UserContext, optional
        If provided, uses ctx for Anthropic client and model name.

    Returns a dict with overall_risk_level, warnings, and recommendations.
    """
    try:
        prompt = (
            "You are a portfolio risk manager. Analyze the following portfolio "
            "and account information, then provide a risk assessment.\n\n"
            f"Account Info:\n{json.dumps(account_info, indent=2)}\n\n"
            f"Current Positions:\n{json.dumps(positions, indent=2)}\n\n"
            "Respond ONLY with valid JSON (no markdown fences) using this schema:\n"
            "{\n"
            '  "overall_risk_level": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",\n'
            '  "portfolio_concentration": "<string>",\n'
            '  "total_exposure_pct": <float>,\n'
            '  "warnings": ["<warning1>", ...],\n'
            '  "recommendations": ["<rec1>", ...],\n'
            '  "position_risks": [\n'
            "    {\n"
            '      "symbol": "<str>",\n'
            '      "risk_level": "LOW" | "MEDIUM" | "HIGH",\n'
            '      "note": "<string>"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Consider concentration risk, unrealized P/L, buying power "
            "utilization, and correlation between holdings."
        )

        response_text = call_ai(
            prompt,
            provider=ctx.ai_provider if ctx else "anthropic",
            model=ctx.ai_model if ctx else config.CLAUDE_MODEL,
            api_key=ctx.ai_api_key if ctx else config.ANTHROPIC_API_KEY,
            db_path=getattr(ctx, "db_path", None) if ctx else None,
            purpose="portfolio_review",
        )

        if ctx is not None:
            try:
                from models import increment_api_usage
                increment_api_usage(ctx.user_id)
            except Exception:
                pass

        return json.loads(response_text)

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse portfolio risk response: %s", exc)
        return {
            "overall_risk_level": "UNKNOWN",
            "warnings": [f"AI response was not valid JSON: {exc}"],
            "recommendations": [],
            "position_risks": [],
        }
    except Exception as exc:
        logger.error("Error in analyze_portfolio_risk: %s", exc)
        return {
            "overall_risk_level": "UNKNOWN",
            "warnings": [f"Risk analysis failed: {exc}"],
            "recommendations": [],
            "position_risks": [],
        }


def compare_signals(technical_signal, ai_signal):
    """
    Merge a technical strategy signal with the AI analyst signal and return a
    final recommendation.

    Parameters
    ----------
    technical_signal : dict
        Output of one of the strategy functions (must have 'signal' key).
    ai_signal : dict
        Output of analyze_symbol() (must have 'signal' and 'confidence' keys).

    Returns a dict with the final merged recommendation.
    """
    tech = technical_signal.get("signal", "HOLD").upper()
    ai = ai_signal.get("signal", "HOLD").upper()
    ai_confidence = ai_signal.get("confidence", 0)

    # Normalize strong/weak signals from strategies.py to base direction
    tech_base = tech.replace("STRONG_", "").replace("WEAK_", "")

    # Agreement logic
    if tech_base == ai:
        # Full agreement
        if tech_base == "BUY":
            final_signal = "STRONG_BUY"
        elif tech_base == "SELL":
            final_signal = "STRONG_SELL"
        else:
            final_signal = "HOLD"
        agreement = "full"
    elif tech_base == "HOLD" or ai == "HOLD":
        # One is HOLD, take the directional signal at reduced confidence
        directional = tech_base if tech_base != "HOLD" else ai
        final_signal = f"WEAK_{directional}" if directional != "HOLD" else "HOLD"
        agreement = "partial"
    else:
        # Direct conflict (BUY vs SELL)
        if ai_confidence >= 70:
            final_signal = f"WEAK_{ai}"
        else:
            final_signal = "HOLD"
        agreement = "conflict"

    return {
        "symbol": ai_signal.get("symbol", technical_signal.get("symbol")),
        "final_signal": final_signal,
        "technical_signal": tech,
        "ai_signal": ai,
        "ai_confidence": ai_confidence,
        "agreement": agreement,
        "reasoning": ai_signal.get("reasoning", ""),
        "risk_factors": ai_signal.get("risk_factors", []),
        "price_targets": ai_signal.get("price_targets", {}),
        "technical_reason": technical_signal.get("reason", ""),
    }


# ---------------------------------------------------------------------------
# AI-first batch trade selection
# ---------------------------------------------------------------------------

def ai_select_trades(candidates_data, portfolio_state, market_context, ctx=None):
    """Send a batch of ranked candidates to AI for portfolio-aware trade selection.

    One smart AI call replaces N per-symbol calls.  The AI sees the full
    picture (candidates + portfolio + regime) and picks the best 0-3 trades.

    Returns dict with keys:
        trades: list[dict] — each has symbol, action, size_pct, confidence, reasoning
        portfolio_reasoning: str
        pass_this_cycle: bool
    """
    prompt = _build_batch_prompt(candidates_data, portfolio_state, market_context, ctx)

    provider = getattr(ctx, "ai_provider", "anthropic") if ctx else "anthropic"
    model = getattr(ctx, "ai_model", "claude-haiku-4-5-20251001") if ctx else "claude-haiku-4-5-20251001"
    api_key = getattr(ctx, "ai_api_key", "") if ctx else ""

    try:
        raw = call_ai(prompt, provider=provider, model=model, api_key=api_key,
                       max_tokens=1024,
                       db_path=getattr(ctx, "db_path", None),
                       purpose="batch_select")
        result = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("AI batch call failed: %s", exc)
        return {
            "trades": [],
            "portfolio_reasoning": f"AI call failed: {exc}",
            "pass_this_cycle": True,
        }

    return _validate_ai_trades(result, candidates_data, ctx)


def _build_batch_prompt(candidates_data, portfolio_state, market_context, ctx=None):
    """Construct the prompt for the AI batch trade selector."""

    max_pos_pct = getattr(ctx, "max_position_pct", 0.10) if ctx else 0.10
    max_positions = getattr(ctx, "max_total_positions", 10) if ctx else 10
    enable_shorts = getattr(ctx, "enable_short_selling", False) if ctx else False
    market_type = getattr(ctx, "segment", "unknown") if ctx else "unknown"

    # Layer 2 — per-profile signal weights. The tuner adjusts these based
    # on which signals have been historically reliable for THIS profile.
    # Read once at the top of the prompt build; every signal-emitting
    # block consults this map.
    try:
        from signal_weights import parse_weights
        _sig_weights = parse_weights(getattr(ctx, "signal_weights", None) if ctx else None)
    except Exception:
        _sig_weights = {}

    # Layer 6 — adaptive prompt structure. Per-section verbosity
    # overrides set by the tuner. Default is "normal" (no behavior
    # change) for any section not explicitly tuned.
    try:
        from prompt_layout import get_verbosity as _get_verbosity
        def _verbosity(section_name):
            return _get_verbosity(ctx, section_name) if ctx else "normal"
    except Exception:
        def _verbosity(section_name):
            return "normal"

    def _signal_weight(name):
        """1.0 default; respect per-profile override if set."""
        return _sig_weights.get(name, 1.0)

    def _weighted_signal_text(name, text):
        """Apply weight to a signal's display text. Returns None if the
        signal should be omitted (weight 0.0); appends an intensity hint
        when partially weighted."""
        w = _signal_weight(name)
        if w <= 0.0:
            return None
        if w < 1.0:
            return f"{text} [intensity {w:.1f}]"
        return text

    # --- Portfolio section ---
    positions_text = "  None (all cash)"
    pos_list = portfolio_state.get("positions", [])
    if pos_list:
        lines = []
        for p in pos_list:
            sym = p.get("symbol", "?")
            qty = p.get("qty", 0)
            mv = p.get("market_value", 0)
            upl = p.get("unrealized_pl", 0)
            uplpc = p.get("unrealized_plpc", 0)
            lines.append(f"  {sym}: {qty} shares, ${mv:,.0f} mkt val, P&L ${upl:+,.0f} ({uplpc:+.1f}%)")
        positions_text = "\n".join(lines)

    dd_pct = portfolio_state.get("drawdown_pct", 0)
    dd_action = portfolio_state.get("drawdown_action", "normal")

    # P2.1 of LONG_SHORT_PLAN.md — sector-exposure context. If
    # portfolio_state carries a precomputed exposure dict (built by
    # views.py / the live pipeline), surface it so the AI can avoid
    # stacking Tech longs on top of Tech longs etc.
    exposure_block = ""
    exp = portfolio_state.get("exposure")
    if exp and exp.get("num_positions", 0) > 0:
        try:
            from portfolio_exposure import render_for_prompt
            exposure_block = "\nEXPOSURE BREAKDOWN:\n" + render_for_prompt(exp)
        except Exception:
            pass

    # P2.2 of LONG_SHORT_PLAN.md — long/short balance target. Tell
    # the AI whether we're under-/over-shorted vs the profile target
    # so it can bias the next pick toward the underweight side.
    target_block = ""
    target_short_pct = float(getattr(ctx, "target_short_pct", 0.0) or 0.0) if ctx else 0.0
    if enable_shorts and target_short_pct > 0 and exp and exp.get("gross_pct", 0) > 0:
        gross = float(exp.get("gross_pct") or 0)
        # Compute current short fraction of gross. by_sector totals
        # would also work but we already have aggregate long/short
        # mass in net/gross.
        current_short = sum(
            (b.get("short_pct") or 0) for b in (exp.get("by_sector") or {}).values()
        )
        cur_short_frac = (current_short / gross) if gross > 0 else 0.0
        delta = target_short_pct - cur_short_frac
        target_block = (
            f"\nLONG/SHORT BALANCE TARGET:\n"
            f"  Target short share of gross: {target_short_pct:.0%}\n"
            f"  Current short share of gross: {cur_short_frac:.0%}\n"
        )
        if delta > 0.10:
            target_block += (
                f"  → UNDERSHORTED by {delta:.0%}. Strong preference: "
                f"pick a SHORT this cycle (only if a quality short setup "
                f"exists — don't force it).\n"
            )
        elif delta < -0.10:
            target_block += (
                f"  → OVERSHORTED by {abs(delta):.0%}. Strong preference: "
                f"pick a LONG this cycle (or pass).\n"
            )
        else:
            target_block += "  → Balance is on target; pick on conviction.\n"

    portfolio_section = (
        f"PORTFOLIO STATE:\n"
        f"  Equity: ${portfolio_state.get('equity', 0):,.0f} | "
        f"Cash: ${portfolio_state.get('cash', 0):,.0f}\n"
        f"  Positions ({portfolio_state.get('num_positions', 0)}/{max_positions}):\n"
        f"{positions_text}\n"
        f"  Drawdown: {dd_pct:.1f}% from peak ({dd_action})"
        f"{exposure_block}"
        f"{target_block}"
    )

    # --- Market context section ---
    regime = market_context.get("regime", "unknown")
    vix = market_context.get("vix", 0)
    spy_trend = market_context.get("spy_trend", "unknown")
    political = market_context.get("political_context")
    profile_summary = market_context.get("profile_summary")

    market_section = f"MARKET CONTEXT:\n  Regime: {regime} (VIX {vix:.0f}, SPY trend: {spy_trend})"
    crisis_ctx = market_context.get("crisis_context")
    if crisis_ctx:
        market_section += f"\n  *** {crisis_ctx} ***"
    if political:
        political_w = _signal_weight("political_context")
        if political_w > 0.0:
            # Layer 6 verbosity: brief = first 2 lines only; normal =
            # first 4 (current behavior); detailed = up to 8 lines.
            _v = _verbosity("political_context")
            line_cap = {"brief": 2, "normal": 4, "detailed": 8}.get(_v, 4)
            for pline in political.splitlines()[:line_cap]:
                market_section += f"\n  {pline}"
            if political_w < 1.0:
                market_section += (
                    f"\n  [Note: political-context signal has been historically less "
                    f"reliable for this profile (intensity {political_w:.1f}) — "
                    f"discount its contribution accordingly.]"
                )
    if profile_summary:
        market_section += f"\n  Track record: {profile_summary}"

    learned = market_context.get("learned_patterns", [])
    if learned:
        # Layer 6 verbosity: brief = top 2; normal = top 5 (current);
        # detailed = top 10.
        _v_lp = _verbosity("learned_patterns")
        cap = {"brief": 2, "normal": 5, "detailed": 10}.get(_v_lp, 5)
        market_section += "\n  LEARNED PATTERNS (from your history):"
        for pattern in learned[:cap]:
            market_section += f"\n    - {pattern}"

    # Sector rotation
    _sector_display = {
        "tech": "Tech", "finance": "Financials", "energy": "Energy",
        "healthcare": "Healthcare", "industrial": "Industrials",
        "consumer_disc": "Consumer Disc", "consumer_staples": "Consumer Staples",
        "utilities": "Utilities", "materials": "Materials",
        "real_estate": "Real Estate", "comm_services": "Communications",
    }
    sector_rot = market_context.get("sector_rotation", {})
    if sector_rot:
        inflows = [f"{_sector_display.get(s,s)}({d['return_5d']:+.1f}%)" for s, d in sector_rot.items()
                   if d.get("trend") == "inflow"]
        outflows = [f"{_sector_display.get(s,s)}({d['return_5d']:+.1f}%)" for s, d in sector_rot.items()
                    if d.get("trend") == "outflow"]
        if inflows:
            market_section += f"\n  Sector inflows: {', '.join(inflows)}"
        if outflows:
            market_section += f"\n  Sector outflows: {', '.join(outflows)}"

    # Macro data (yield curve, CBOE skew, ETF flows, economic indicators)
    macro = market_context.get("macro_context", {})
    yc = macro.get("yield_curve", {})
    if yc.get("rate_10y"):
        spread = yc.get("spread_10y_2y", 0)
        status = yc.get("curve_status", "normal").upper()
        yc_line = (f"YIELD CURVE: 2y={yc['rate_2y']:.2f}% 10y={yc['rate_10y']:.2f}% "
                   f"spread={spread:+.2f}% ({status})")
        if status == "INVERTED":
            yc_line += " — recession signal"
        market_section += f"\n  {yc_line}"
    skew = macro.get("cboe_skew", {})
    if skew.get("skew_value"):
        market_section += (f"\n  TAIL RISK: CBOE Skew {skew['skew_value']:.0f} "
                           f"({skew.get('skew_signal', 'normal')})")
    flows = macro.get("etf_flows", {})
    if flows:
        flow_in = [f"{_sector_display.get(s,s)}(${d['estimated_weekly_flow']/1e9:+.1f}B)"
                   for s, d in flows.items() if d.get("flow_direction") == "inflow"
                   and d.get("magnitude") in ("strong", "moderate")]
        flow_out = [f"{_sector_display.get(s,s)}(${d['estimated_weekly_flow']/1e9:+.1f}B)"
                    for s, d in flows.items() if d.get("flow_direction") == "outflow"
                    and d.get("magnitude") in ("strong", "moderate")]
        if flow_in:
            market_section += f"\n  ETF INFLOWS: {', '.join(flow_in)}"
        if flow_out:
            market_section += f"\n  ETF OUTFLOWS: {', '.join(flow_out)}"
    fred = macro.get("fred_macro", {})
    if fred.get("unemployment_rate"):
        market_section += (f"\n  MACRO: Unemployment {fred['unemployment_rate']:.1f}% "
                           f"({fred.get('unemployment_trend', 'stable')}), "
                           f"CPI {fred.get('cpi_yoy', 0):.1f}% YoY, "
                           f"Consumer sentiment {fred.get('consumer_sentiment', 0):.0f} "
                           f"({fred.get('consumer_sentiment_trend', 'stable')})")
    sector_mom = macro.get("sector_momentum", {})
    if sector_mom.get("rankings"):
        top = ", ".join(f"{r['sector']}(#{r['rank']})" for r in sector_mom["rankings"][:3])
        bottom = ", ".join(f"{r['sector']}(#{r['rank']})" for r in sector_mom["rankings"][-3:])
        phase = sector_mom.get("rotation_phase", "mixed").upper().replace("_", " ")
        market_section += f"\n  SECTOR MOMENTUM: Top: {top} | Bottom: {bottom} ({phase})"
    mgex = macro.get("market_gex", {})
    if mgex.get("sample_size", 0) >= 5:
        regime = mgex.get("net_regime", "balanced").upper()
        pct = mgex.get("pct_positive", 0.5)
        market_section += (f"\n  MARKET GEX: {pct:.0%} positive ({regime} — "
                           f"{'mean reversion favored' if regime == 'PINNING' else 'breakouts favored' if regime == 'EXPANSION' else 'no dominant regime'})")

    # --- Candidates section ---
    cand_lines = []
    for i, c in enumerate(candidates_data, 1):
        sym = c.get("symbol", "?")
        price = c.get("price", 0)
        signal = c.get("signal", "?")
        score = c.get("score", 0)
        rsi = c.get("rsi", 0)
        vol_ratio = c.get("volume_ratio", 0)
        reason = c.get("reason", "")[:120]
        votes = c.get("votes", {})

        votes_parts = [f"{k}={v}" for k, v in votes.items() if v != "HOLD"]
        votes_str = ", ".join(votes_parts) if votes_parts else "no strong votes"

        adx = c.get("adx", 0)
        stoch = c.get("stoch_rsi", 50)
        roc = c.get("roc_10", 0)
        pct_52h = c.get("pct_from_52w_high", 0)
        mfi = c.get("mfi", 50)
        cmf = c.get("cmf", 0)
        squeeze = c.get("squeeze", 0)
        vwap_dist = c.get("pct_from_vwap", 0)
        fib_dist = c.get("nearest_fib_dist", 99)
        gap = c.get("gap_pct", 0)

        line = (f"  {i}. {sym} @ ${price:.2f} | {signal} (score {score}/4)\n"
                f"     Votes: {votes_str}\n"
                f"     RSI: {rsi:.0f} | StochRSI: {stoch:.0f} | ADX: {adx:.0f} | "
                f"Vol: {vol_ratio:.1f}x | ROC10: {roc:+.1f}%\n"
                f"     MFI: {mfi:.0f} | CMF: {cmf:+.2f} | "
                f"vs52wH: {pct_52h:+.1f}% | vsVWAP: {vwap_dist:+.1f}%")

        # Conditional flags (only show when meaningful)
        flags = []
        if squeeze:
            flags.append("SQUEEZE (big move imminent)")
        if abs(gap) > 2:
            flags.append(f"GAP {gap:+.1f}%")
        if fib_dist < 2:
            flags.append(f"Near Fib level ({fib_dist:.1f}%)")
        if flags:
            line += f"\n     FLAGS: {' | '.join(flags)}"

        line += f"\n     {reason}"

        # P1.14 — short-side annotations from the candidate filter pass.
        # Borrow cost: 'low' (~1% annual) vs 'high' (5-50%+ annual on
        # HTB names — eats real money over multi-day holds).
        # Squeeze risk: HIGH/MED/LOW based on short interest + float.
        borrow_cost = c.get("_borrow_cost")
        squeeze_risk = c.get("_squeeze_risk")
        short_flags = []
        if borrow_cost:
            short_flags.append(
                f"BORROW: {borrow_cost} cost"
                + (" (eats ~5-15% over a 3-week hold)" if borrow_cost == "high" else "")
            )
        if squeeze_risk and squeeze_risk != "LOW":
            short_flags.append(
                f"SQUEEZE: {squeeze_risk}"
                + (" (high short interest — confirm breakdown before shorting)"
                   if squeeze_risk == "MED" else "")
            )
        if short_flags:
            line += f"\n     SHORT-SIDE: {' | '.join(short_flags)}"

        # Relative strength vs sector
        rs = c.get("rel_strength")
        if rs:
            line += (f"\n     Sector: {rs['sector']} ({rs['sector_trend']}) | "
                     f"Stock 5d: {rs['stock_5d']:+.1f}% vs sector: {rs['sector_5d']:+.1f}% "
                     f"(RS: {rs['relative_strength']:+.1f}%)")

        # Alternative data
        alt = c.get("alt_data", {})
        if alt:
            alt_parts = []
            # EVERY field access below uses .get() with defaults.
            # Direct dict['key'] access is BANNED — it crashes when
            # data sources are disabled or return empty dicts.
            insider = alt.get("insider", {})
            if insider.get("net_direction") and insider.get("net_direction") != "neutral":
                txt = _weighted_signal_text("insider_direction",
                    f"Insiders: {insider.get('net_direction', '')} "
                    f"({insider.get('recent_buys',0)}B/{insider.get('recent_sells',0)}S)")
                if txt: alt_parts.append(txt)
            short = alt.get("short", {})
            if short.get("short_pct_float", 0) > 5:
                txt = _weighted_signal_text("short_pct_float",
                    f"Short: {short.get('short_pct_float', 0):.1f}% float "
                    f"(squeeze risk: {short.get('squeeze_risk','low')})")
                if txt: alt_parts.append(txt)
            opts = alt.get("options", {})
            if opts.get("unusual"):
                txt = _weighted_signal_text("options_signal",
                    f"Options: {opts.get('signal','neutral')} "
                    f"(P/C ratio: {opts.get('put_call_ratio',0):.1f})")
                if txt: alt_parts.append(txt)
            intra = alt.get("intraday", {})
            if intra.get("opening_range_breakout"):
                alt_parts.append("ORB breakout")
            if intra.get("vwap_position") and intra.get("vwap_position") != "at":
                txt = _weighted_signal_text("vwap_position",
                    f"Intraday: {intra.get('vwap_position', '')} VWAP")
                if txt: alt_parts.append(txt)
            fund = alt.get("fundamentals", {})
            if fund.get("pe_trailing", 0) > 0:
                alt_parts.append(f"PE: {fund.get('pe_trailing', 0):.1f}")
            # Congressional (disabled — no free API)
            congress = alt.get("congressional", {})
            if congress.get("net_direction") and congress.get("net_direction") != "neutral":
                txt = _weighted_signal_text("congress_direction",
                    f"Congress: {congress.get('recent_transactions', 0)} members "
                    f"{congress.get('net_direction', '')} "
                    f"(${congress.get('total_value', 0):,.0f})")
                if txt: alt_parts.append(txt)
            finra = alt.get("finra_short_vol", {})
            if finra.get("is_elevated"):
                txt = _weighted_signal_text("finra_short_vol_ratio",
                    f"Short vol: {finra.get('short_volume_ratio', 0):.0%} of daily (elevated)")
                if txt: alt_parts.append(txt)
            cluster = alt.get("insider_cluster", {})
            if cluster.get("is_cluster"):
                txt = _weighted_signal_text("insider_cluster",
                    f"INSIDER CLUSTER: {cluster.get('insider_count', 0)} insiders "
                    f"{cluster.get('cluster_direction', '')} ${cluster.get('total_value', 0):,.0f}")
                if txt: alt_parts.append(txt)
            estimates = alt.get("analyst_estimates", {})
            if estimates.get("eps_revision_direction") and estimates.get("eps_revision_direction") != "flat":
                txt = _weighted_signal_text("eps_revision_direction",
                    f"EPS revised {estimates.get('eps_revision_direction', '').upper()} "
                    f"{abs(estimates.get('revision_magnitude_pct', 0)):.0f}%")
                if txt: alt_parts.append(txt)
            ie = alt.get("insider_earnings", {})
            if ie.get("insider_buying_near_earnings"):
                alt_parts.append(
                    f"Insiders buying {ie.get('days_to_earnings', '?')}d before earnings (bullish)")
            elif ie.get("insider_selling_near_earnings"):
                alt_parts.append(
                    f"Insiders selling {ie.get('days_to_earnings', '?')}d before earnings (bearish)")
            dp = alt.get("dark_pool", {})
            if dp.get("ats_volume", 0) > 0:
                txt = _weighted_signal_text("dark_pool_pct",
                    f"Dark pool: {dp.get('ats_volume', 0):,} shares across "
                    f"{dp.get('num_venues', 0)} ATS venues")
                if txt: alt_parts.append(txt)
            es = alt.get("earnings_surprise", {})
            if es.get("total_quarters", 0) >= 4:
                txt = _weighted_signal_text("earnings_surprise_streak",
                    f"Earnings: {es.get('surprise_direction', 'mixed')} "
                    f"({es.get('beat_count', 0)}/{es.get('total_quarters', 0)} beats, "
                    f"avg surprise {es.get('avg_surprise_pct', 0):+.1f}%)")
                if txt: alt_parts.append(txt)
            transcript = alt.get("transcript_sentiment", {})
            if transcript.get("has_data"):
                phrases = ", ".join(transcript.get("key_phrases", [])[:2])
                alt_parts.append(
                    f"Earnings call: {transcript.get('tone', 'neutral').upper()}"
                    f"{' — ' + phrases if phrases else ''}")
            patents = alt.get("patent_activity", {})
            if patents.get("has_data") and patents.get("recent_filings_365d", 0) > 0:
                alt_parts.append(
                    f"Patents: {patents['recent_filings_90d']} filed last 90d, "
                    f"{patents['recent_filings_365d']} last year "
                    f"({patents['velocity_trend']})")

            # ── 4 local-SQLite alt-data sources (per-profile weighted) ──
            cong = alt.get("congressional_recent") or {}
            if cong.get("trades_60d", 0) > 0:
                direction = cong.get("net_direction", "neutral")
                amt = cong.get("dollar_volume_60d", 0) or 0
                amt_label = (f"${amt/1e6:.1f}M" if amt >= 1_000_000
                              else f"${amt/1000:.0f}k" if amt >= 1000
                              else f"${amt:.0f}")
                txt = _weighted_signal_text("congressional_recent",
                    f"Congress: {cong['trades_60d']} trades / "
                    f"{cong.get('buys_60d',0)}B / {cong.get('sells_60d',0)}S "
                    f"({direction}, {amt_label} 60d)")
                if txt: alt_parts.append(txt)

            inst = alt.get("institutional_13f") or {}
            if inst.get("total_holders", 0) > 0:
                shares_m = (inst.get("total_shares", 0) or 0) / 1_000_000
                top = inst.get("top_holder_name") or ""
                qoq = inst.get("qoq_share_change_pct")
                qoq_str = f", {qoq:+.0f}% QoQ" if qoq is not None else ""
                top_str = f", top: {top}" if top else ""
                txt = _weighted_signal_text("institutional_13f",
                    f"13F: {inst['total_holders']} holders, "
                    f"{shares_m:.1f}M shares{qoq_str}{top_str}")
                if txt: alt_parts.append(txt)

            bio = alt.get("biotech_milestones") or {}
            if bio.get("days_to_pdufa") is not None or bio.get("active_phase3_count", 0) > 0:
                bits = []
                if bio.get("days_to_pdufa") is not None:
                    bits.append(
                        f"PDUFA in {bio['days_to_pdufa']}d "
                        f"({bio.get('drug_name','?')})")
                if bio.get("active_phase3_count", 0) > 0:
                    bits.append(f"{bio['active_phase3_count']} active P3")
                if bio.get("recent_phase_change"):
                    rc = bio["recent_phase_change"]
                    bits.append(
                        f"recent {rc.get('field')} change: "
                        f"{rc.get('from')}→{rc.get('to')}")
                txt = _weighted_signal_text("biotech_milestones",
                    f"Biotech: {' | '.join(bits)}")
                if txt: alt_parts.append(txt)

            twits = alt.get("stocktwits_sentiment") or {}
            if twits.get("message_count_7d", 0) > 0:
                ns = twits.get("net_sentiment_7d")
                ns_label = (
                    f"net {ns:+.2f}" if ns is not None else "")
                trending = (f", trending #{twits['trending_rank']}"
                             if twits.get("is_trending") else "")
                txt = _weighted_signal_text("stocktwits_sentiment",
                    f"StockTwits: {twits['message_count_7d']} msgs/7d "
                    f"({ns_label}){trending}")
                if txt: alt_parts.append(txt)

            if alt_parts:
                # Layer 6 verbosity: brief = show only top 3 signals;
                # normal = show all; detailed = show all + a "(X more)"
                # tail hint when truncated alt-data lines were skipped.
                _v = _verbosity("alt_data")
                if _v == "brief" and len(alt_parts) > 3:
                    line += f"\n     ALT DATA: {' | '.join(alt_parts[:3])} | (+{len(alt_parts) - 3} more, brief mode)"
                else:
                    line += f"\n     ALT DATA: {' | '.join(alt_parts)}"

        # Social sentiment
        social = c.get("social", {})
        if social.get("mentions", 0) > 0:
            sent_label = "bullish" if social["sentiment_score"] > 0.2 else \
                         "bearish" if social["sentiment_score"] < -0.2 else "mixed"
            line += (f"\n     REDDIT: {social['mentions']} mentions ({sent_label}) "
                     f"in r/{', r/'.join(social.get('subreddits_found', []))}")

        track = c.get("track_record")
        if track:
            line += f"\n     Your record: {track}"
        last_pred = c.get("last_prediction")
        if last_pred:
            line += f"\n     {last_pred}"
        earnings = c.get("earnings_warning")
        if earnings:
            line += f"\n     {earnings}"
        # Phase 4: SEC filing alert (material language changes in 10-K/10-Q/8-K)
        sec = c.get("sec_alert")
        if sec:
            line += (f"\n     SEC ALERT [{sec['severity'].upper()}/{sec['signal']}]: "
                     f"{sec['form']} filed {sec['date']} — {sec['summary'][:200]}")
        # Phase 5: Options Chain Oracle — IV skew, term structure, GEX, etc
        opts_sum = c.get("options_oracle_summary")
        if opts_sum:
            line += f"\n     OPTIONS: {opts_sum}"
        # Phase 8: Specialist ensemble summary (earnings, pattern, sentiment, risk)
        ens = c.get("ensemble_summary")
        if ens:
            line += f"\n     {ens}"
        news = c.get("news")
        if news:
            line += f"\n     News: {' | '.join(n[:80] for n in news[:3])}"

        cand_lines.append(line)

    # P1.8 of LONG_SHORT_PLAN.md — when shorts are enabled, surface
    # the long/short split explicitly so the AI considers each side
    # on its own merits instead of defaulting to BUY against a
    # bullish-dominated combined list. The candidate's ranking already
    # comes pre-split from _rank_candidates, but we relabel sections
    # here to make the choice unambiguous.
    if enable_shorts:
        long_lines = []
        short_lines = []
        for i, c in enumerate(candidates_data):
            sig = (c.get("signal") or "").upper()
            line = cand_lines[i]
            if sig in ("SELL", "STRONG_SELL", "SHORT", "STRONG_SHORT"):
                short_lines.append(line)
            else:
                long_lines.append(line)
        sections = []
        if long_lines:
            sections.append("LONG CANDIDATES (ranked by technical score):\n"
                            + "\n".join(long_lines))
        if short_lines:
            sections.append("SHORT CANDIDATES (ranked by technical score):\n"
                            + "\n".join(short_lines))
        else:
            sections.append("SHORT CANDIDATES: (none triggered this scan)")

        # P2.3 of LONG_SHORT_PLAN.md — pair-trade opportunities.
        # Same-sector long+short pairs surfaced separately so the AI
        # can propose them. Isolates the relative-strength signal
        # from market beta — the highest-Sharpe quant funds run
        # heavily on pair trades.
        try:
            from portfolio_exposure import find_pair_opportunities, render_pairs_for_prompt
            pairs = find_pair_opportunities(candidates_data, max_pairs=3)
            pair_block = render_pairs_for_prompt(pairs)
            if pair_block:
                sections.append(pair_block)
        except Exception:
            pass

        candidates_section = "\n\n".join(sections)
    else:
        candidates_section = ("CANDIDATES (ranked by technical score):\n"
                              + "\n".join(cand_lines))

    # --- Actions allowed ---
    actions = "BUY"
    if enable_shorts:
        actions += " | SHORT"

    # --- Assemble prompt ---
    long_short_note = ""
    if enable_shorts:
        long_short_note = (
            "\n- BOTH sides are real options. Don't force a long pick when the "
            "short setups are stronger, or vice versa. A high-conviction short "
            "beats a mediocre long.\n"
            "- Shorts: prefer breakdowns, distribution patterns, failed "
            "breakouts, and relative weakness in strong sectors. Avoid "
            "shorting names with high short interest unless the breakdown "
            "is well-confirmed (squeeze risk).\n"
        )

    prompt = (
        f"You are a portfolio manager for an automated {market_type} trading system. "
        f"You see a batch of candidates our technical screener flagged. "
        f"Your job is to PICK the best 0-3 trades and SIZE them. "
        f"Zero trades is a valid and often correct answer — only trade when conviction is high.\n\n"
        f"{portfolio_section}\n\n"
        f"{market_section}\n\n"
        f"{candidates_section}\n\n"
        f"RULES:\n"
        f"- Select 0-3 trades. Actions allowed: {actions}\n"
        f"- Max position size: {max_pos_pct * 100:.0f}% of equity for longs"
        f"{', halved for shorts (asymmetric risk)' if enable_shorts else ''}\n"
        f"- Consider: portfolio concentration, market regime, drawdown state, "
        f"your track record on each symbol\n"
        f"- If drawdown is elevated ({dd_action}), be conservative\n"
        f"- If at max positions, only recommend exits"
        f"{long_short_note}\n\n"
        f"Respond ONLY with valid JSON (no markdown, no commentary):\n"
        f'{{"trades": [{{"symbol": "TICKER", "action": "BUY", '
        f'"size_pct": 7.5, "confidence": 75, '
        f'"stop_loss_pct": 3.0, "take_profit_pct": 10.0, '
        f'"reasoning": "1-2 sentences"}}], '
        f'"portfolio_reasoning": "Why this combination or why pass", '
        f'"pass_this_cycle": false}}'
    )

    return prompt


def _validate_ai_trades(result, candidates_data, ctx=None):
    """Validate and sanitize the AI batch response."""

    max_pos_pct = getattr(ctx, "max_position_pct", 0.10) if ctx else 0.10
    # P1.6 of LONG_SHORT_PLAN.md — asymmetric sizing for shorts.
    # Unlimited downside on shorts means smaller per-name caps are
    # standard professional convention (half the long size). Falls
    # back to half of long max if not explicitly set.
    short_max_pos_pct = (getattr(ctx, "short_max_position_pct", None)
                         if ctx else None)
    if short_max_pos_pct is None:
        short_max_pos_pct = max_pos_pct / 2
    enable_shorts = getattr(ctx, "enable_short_selling", False) if ctx else False

    # P1.14 — borrow-cost sizing penalty: HTB names eat real money
    # over the typical hold. Halve again on top of the asymmetric cap.
    # Lookup table by symbol from the candidates_data flags set in
    # _rank_candidates.
    borrow_cost_by_sym = {
        c.get("symbol"): c.get("_borrow_cost")
        for c in (candidates_data or [])
    }

    # Ensure structure
    if not isinstance(result, dict):
        return {"trades": [], "portfolio_reasoning": "Invalid response format",
                "pass_this_cycle": True}

    trades = result.get("trades", [])
    if not isinstance(trades, list):
        trades = []

    pass_cycle = result.get("pass_this_cycle", False)
    reasoning = result.get("portfolio_reasoning", "")

    if pass_cycle:
        return {"trades": [], "portfolio_reasoning": reasoning,
                "pass_this_cycle": True}

    # Valid symbols from candidates
    valid_symbols = {c["symbol"] for c in candidates_data}

    validated = []
    for t in trades[:3]:  # Max 3
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol", "")
        if sym not in valid_symbols:
            logger.warning("AI suggested symbol %s not in candidates — skipped", sym)
            continue

        action = t.get("action", "").upper()
        if action == "SHORT" and not enable_shorts:
            logger.warning("AI suggested SHORT on %s but shorts disabled — skipped", sym)
            continue
        if action not in ("BUY", "SELL", "SHORT"):
            continue

        # Cap by direction: longs against max_pos_pct, shorts against
        # the smaller short_max_pos_pct (asymmetric-risk sizing).
        cap_pct = (short_max_pos_pct if action in ("SHORT", "SELL")
                   else max_pos_pct) * 100
        # P1.14 — extra penalty for HTB shorts: the borrow cost eats
        # the upside on a typical 2-3 week hold. Halve again.
        if action in ("SHORT", "SELL") and borrow_cost_by_sym.get(sym) == "high":
            cap_pct = cap_pct / 2
        size_pct = min(float(t.get("size_pct", 5.0)), cap_pct)
        size_pct = max(size_pct, 1.0)

        validated.append({
            "symbol": sym,
            "action": action,
            "size_pct": size_pct,
            "confidence": int(t.get("confidence", 50)),
            "stop_loss_pct": float(t.get("stop_loss_pct", 3.0)),
            "take_profit_pct": float(t.get("take_profit_pct", 10.0)),
            "reasoning": t.get("reasoning", ""),
        })

    return {
        "trades": validated,
        "portfolio_reasoning": reasoning,
        "pass_this_cycle": len(validated) == 0,
    }
