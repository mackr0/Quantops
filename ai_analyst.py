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

        # Inject self-tuning performance context (after technical data,
        # before political context) so the AI learns from past mistakes
        if ctx is not None and getattr(ctx, "enable_self_tuning", True):
            try:
                from self_tuning import build_performance_context
                perf_context = build_performance_context(ctx, symbol=symbol)
                if perf_context:
                    prompt += f"\n\n{perf_context}"
            except Exception as _st_err:
                logger.warning("Failed to build self-tuning context: %s", _st_err)

        # Market regime context
        try:
            from market_regime import get_regime_context
            regime_context = get_regime_context()
            if regime_context:
                prompt += f"\n\n{regime_context}"
        except Exception as _regime_err:
            logger.warning("Failed to get regime context: %s", _regime_err)

        # Earnings calendar context
        try:
            from earnings_calendar import get_earnings_context
            avoid_days = ctx.avoid_earnings_days if ctx else 2
            earnings_ctx = get_earnings_context(symbol, avoid_days=avoid_days)
            if earnings_ctx:
                prompt += f"\n\n{earnings_ctx}"
        except Exception as _earn_err:
            logger.warning("Failed to get earnings context: %s", _earn_err)

        # Current time context (Feature 7: Time-of-Day Patterns)
        try:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo
            now_et = _dt.now(ZoneInfo("America/New_York"))
            time_note = f"Current time: {now_et.strftime('%I:%M %p ET')} ({now_et.strftime('%A')})"
            prompt += f"\n\n{time_note}"
        except Exception as _time_err:
            logger.warning("Failed to add time context: %s", _time_err)

        # Append political/macro context when MAGA Mode is active
        if political_context:
            prompt += (
                "\n\nAdditionally, consider the following political/macro "
                "context when making your recommendation:\n"
                f"{political_context}\n\n"
                "If the current technical weakness appears to be driven by "
                "political noise rather than fundamental deterioration, factor "
                "in the likelihood of a mean reversion bounce."
            )

        # Call AI provider (multi-provider via ai_providers.call_ai)
        response_text = call_ai(
            prompt,
            provider=ctx.ai_provider if ctx else "anthropic",
            model=ctx.ai_model if ctx else config.CLAUDE_MODEL,
            api_key=ctx.ai_api_key if ctx else config.ANTHROPIC_API_KEY,
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
