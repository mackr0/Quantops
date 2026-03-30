"""Claude AI integration for trading analysis."""

import json
import logging

import anthropic

import config
from client import get_api
from market_data import get_bars, add_indicators

logger = logging.getLogger(__name__)


def get_claude_client(api_key=None):
    """Return an authenticated Anthropic client.

    Parameters
    ----------
    api_key : str, optional
        Anthropic API key.  Falls back to config.ANTHROPIC_API_KEY when
        not provided (backward compat for CLI).
    """
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

        # Use ctx for client and model if available, else fall back to config
        if ctx is not None:
            client = ctx.get_anthropic_client()
            model = ctx.claude_model
        else:
            client = get_claude_client()
            model = config.CLAUDE_MODEL

        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        # Strip markdown code fences if present (Haiku sometimes adds them)
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines).strip()
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

        if ctx is not None:
            client = ctx.get_anthropic_client()
            model = ctx.claude_model
        else:
            client = get_claude_client()
            model = config.CLAUDE_MODEL

        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines).strip()
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
