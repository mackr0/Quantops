"""Political sentiment analysis for market volatility detection."""

import json
import logging
import time
from typing import Optional, Dict, Any, List

import yfinance as yf

import config
from ai_analyst import get_claude_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Political keywords for filtering market news
# ---------------------------------------------------------------------------
POLITICAL_KEYWORDS = [
    "tariff", "trade war", "sanctions", "executive order", "policy",
    "regulation", "president", "white house", "congress", "fed",
    "treasury", "ban", "restrict", "tax", "subsidy", "stimulus",
    "shutdown", "debt ceiling", "impeach",
]

# ---------------------------------------------------------------------------
# In-memory cache (30-minute TTL)
# ---------------------------------------------------------------------------
_cache: Dict[str, Any] = {
    "political_news": None,
    "political_news_ts": 0,
    "political_climate": None,
    "political_climate_ts": 0,
}
_CACHE_TTL = 30 * 60  # 30 minutes in seconds


def _is_cached(key: str) -> bool:
    """Return True if the cache entry is still fresh."""
    ts_key = f"{key}_ts"
    return (
        _cache.get(key) is not None
        and (time.time() - _cache.get(ts_key, 0)) < _CACHE_TTL
    )


def _set_cache(key: str, value: Any) -> None:
    """Store a value in the cache with the current timestamp."""
    _cache[key] = value
    _cache[f"{key}_ts"] = time.time()


# ---------------------------------------------------------------------------
# Fetch political news
# ---------------------------------------------------------------------------

def fetch_political_news(limit: int = 20) -> List[Dict[str, str]]:
    """Fetch recent market-relevant political news.

    Uses yfinance news for broad market ETFs (SPY, QQQ, DIA) and filters
    headlines that contain political keywords.

    Returns a list of headline dicts with 'title' and 'source' keys.
    """
    if _is_cached("political_news"):
        logger.debug("Returning cached political news")
        return _cache["political_news"]

    market_symbols = ["SPY", "QQQ", "DIA"]
    all_headlines: List[Dict[str, str]] = []
    seen_titles = set()

    for sym in market_symbols:
        try:
            ticker = yf.Ticker(sym)
            news = ticker.news or []
            for item in news:
                title = item.get("title", "")
                if not title or title in seen_titles:
                    continue
                # Check if headline contains any political keyword
                title_lower = title.lower()
                if any(kw in title_lower for kw in POLITICAL_KEYWORDS):
                    seen_titles.add(title)
                    all_headlines.append({
                        "title": title,
                        "source": item.get("publisher", "Unknown"),
                    })
        except Exception as exc:
            logger.warning("Failed to fetch news for %s: %s", sym, exc)

    # Trim to requested limit
    result = all_headlines[:limit]
    _set_cache("political_news", result)
    logger.info("Fetched %d political headlines (from %d total market news)",
                len(result), len(seen_titles))
    return result


# ---------------------------------------------------------------------------
# Analyze political climate via Claude
# ---------------------------------------------------------------------------

def analyze_political_climate(ctx=None) -> Dict[str, Any]:
    """Send recent political headlines to Claude for a volatility assessment.

    Returns a dict with:
        political_volatility_level: "high" | "medium" | "low"
        is_panic_driven: bool
        expected_duration: "days" | "weeks" | "months"
        affected_sectors: list of sector strings
        summary: 2-3 sentence plain English assessment
        recommendation: "buy_the_dip" | "stay_cautious" | "normal"
    """
    if _is_cached("political_climate"):
        logger.debug("Returning cached political climate analysis")
        return _cache["political_climate"]

    headlines = fetch_political_news()

    if not headlines:
        result = {
            "political_volatility_level": "low",
            "is_panic_driven": False,
            "expected_duration": "days",
            "affected_sectors": [],
            "summary": "No politically-charged market news detected.",
            "recommendation": "normal",
        }
        _set_cache("political_climate", result)
        return result

    headlines_text = "\n".join(
        f"- [{h['source']}] {h['title']}" for h in headlines
    )

    prompt = (
        "Analyze these recent political/market headlines and assess the "
        "current political volatility environment for US equity markets.\n\n"
        f"Headlines:\n{headlines_text}\n\n"
        "Respond ONLY with valid JSON (no markdown fences) using this schema:\n"
        "{\n"
        '  "political_volatility_level": "high" | "medium" | "low",\n'
        '  "is_panic_driven": true | false,\n'
        '  "expected_duration": "days" | "weeks" | "months",\n'
        '  "affected_sectors": ["sector1", "sector2"],\n'
        '  "summary": "2-3 sentence assessment",\n'
        '  "recommendation": "buy_the_dip" | "stay_cautious" | "normal"\n'
        "}\n\n"
        "Consider: Is current market weakness driven by political noise "
        "(tariffs, tweets, policy threats) vs fundamental deterioration? "
        "Political panic selloffs often overcorrect and revert within days."
    )

    try:
        if ctx is not None:
            client = ctx.get_anthropic_client()
            model = ctx.claude_model
        else:
            client = get_claude_client()
            model = config.CLAUDE_MODEL

        message = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        result = json.loads(response_text)
        _set_cache("political_climate", result)
        return result

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse political climate response: %s", exc)
        return {
            "political_volatility_level": "low",
            "is_panic_driven": False,
            "expected_duration": "days",
            "affected_sectors": [],
            "summary": f"Analysis failed (bad JSON): {exc}",
            "recommendation": "normal",
        }
    except Exception as exc:
        logger.error("Error in analyze_political_climate: %s", exc)
        return {
            "political_volatility_level": "low",
            "is_panic_driven": False,
            "expected_duration": "days",
            "affected_sectors": [],
            "summary": f"Analysis failed: {exc}",
            "recommendation": "normal",
        }


# ---------------------------------------------------------------------------
# Convenience function for injection into AI analyst prompt
# ---------------------------------------------------------------------------

def get_maga_mode_context(ctx=None) -> str:
    """Fetch political news, analyze the climate, and return a formatted
    string suitable for injection into the AI analyst prompt.

    This is the main entry point for the MAGA Mode feature.
    """
    try:
        climate = analyze_political_climate(ctx=ctx)
    except Exception as exc:
        logger.error("MAGA Mode context generation failed: %s", exc)
        return ""

    vol_level = climate.get("political_volatility_level", "low").upper()
    summary = climate.get("summary", "No assessment available.")
    sectors = climate.get("affected_sectors", [])
    recommendation = climate.get("recommendation", "normal")
    is_panic = climate.get("is_panic_driven", False)
    duration = climate.get("expected_duration", "unknown")

    # Map recommendation to human-readable advice
    rec_text = {
        "buy_the_dip": "Mean reversion opportunities likely — political panic tends to overcorrect.",
        "stay_cautious": "Caution warranted — political uncertainty may persist.",
        "normal": "No significant political headwinds detected.",
    }.get(recommendation, "Normal market conditions.")

    sectors_str = ", ".join(sectors) if sectors else "Broad market"

    context = (
        f"POLITICAL MARKET CONTEXT (MAGA Mode Active):\n"
        f"Volatility Level: {vol_level}\n"
        f"Assessment: {summary}\n"
        f"Panic-Driven: {'Yes' if is_panic else 'No'} | "
        f"Expected Duration: {duration}\n"
        f"Affected Sectors: {sectors_str}\n"
        f"Recommendation: {rec_text}"
    )

    return context
