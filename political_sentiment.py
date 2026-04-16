"""Political sentiment analysis for market volatility detection.

Sources (all free, no API keys):
- Google News RSS (political + market search)
- CNBC Economy RSS
- Reuters Business RSS
- Yahoo Finance (via yfinance) for SPY/QQQ/DIA news
"""

import json
import logging
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List

import yfinance as yf

import config
from ai_providers import call_ai
from ai_analyst import get_claude_client  # backward compat

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Political keywords for filtering market news
# ---------------------------------------------------------------------------
POLITICAL_KEYWORDS = [
    "tariff", "trade war", "sanctions", "executive order", "policy",
    "regulation", "president", "white house", "congress", "fed",
    "treasury", "ban", "restrict", "tax", "subsidy", "stimulus",
    "shutdown", "debt ceiling", "impeach", "trump", "truth social",
    "trade deal", "china", "retaliation", "duty", "import",
    "executive action", "veto", "bipartisan",
]

# ---------------------------------------------------------------------------
# Free RSS feeds
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    ("Google News: Trump Market", "https://news.google.com/rss/search?q=trump+market+tariff&hl=en-US&gl=US&ceid=US:en"),
    ("Google News: Political Economy", "https://news.google.com/rss/search?q=political+economy+stocks&hl=en-US&gl=US&ceid=US:en"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
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

def _fetch_rss(url: str, source_name: str) -> List[Dict[str, str]]:
    """Fetch headlines from an RSS feed. Returns list of {title, source}."""
    results = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "QuantOpsAI/1.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = resp.read()
        root = ET.fromstring(data)
        for item in root.findall(".//item"):
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                results.append({
                    "title": title_el.text.strip(),
                    "source": source_name,
                })
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", source_name, exc)
    return results


def fetch_political_news(limit: int = 30) -> List[Dict[str, str]]:
    """Fetch recent market-relevant political news from multiple free sources.

    Sources:
    - Google News RSS (political + market searches)
    - CNBC Economy RSS
    - Yahoo Finance (via yfinance) for SPY/QQQ/DIA news

    All headlines are filtered for political keywords and deduplicated.
    Results cached for 30 minutes.
    """
    if _is_cached("political_news"):
        logger.debug("Returning cached political news")
        return _cache["political_news"]

    all_headlines: List[Dict[str, str]] = []
    seen_titles = set()

    def _add(title, source):
        if title and title not in seen_titles:
            title_lower = title.lower()
            if any(kw in title_lower for kw in POLITICAL_KEYWORDS):
                seen_titles.add(title)
                all_headlines.append({"title": title, "source": source})

    # 1. RSS feeds (fast, free, broad coverage)
    for feed_name, feed_url in RSS_FEEDS:
        for item in _fetch_rss(feed_url, feed_name):
            _add(item["title"], item["source"])

    # 2. Yahoo Finance via yfinance (market ETFs)
    for sym in ["SPY", "QQQ", "DIA"]:
        try:
            ticker = yf.Ticker(sym)
            for item in (ticker.news or []):
                _add(item.get("title", ""), item.get("publisher", "Yahoo Finance"))
        except Exception as exc:
            logger.warning("yfinance news failed for %s: %s", sym, exc)

    result = all_headlines[:limit]
    _set_cache("political_news", result)
    logger.info("Fetched %d political headlines from %d sources",
                len(result), len(RSS_FEEDS) + 3)
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
        "Analyze these recent political/market headlines. Assess political "
        "volatility AND identify specific trading opportunities.\n\n"
        f"Headlines:\n{headlines_text}\n\n"
        "Respond ONLY with valid JSON (no markdown fences):\n"
        "{\n"
        '  "political_volatility_level": "high" | "medium" | "low",\n'
        '  "is_panic_driven": true | false,\n'
        '  "expected_duration": "days" | "weeks" | "months",\n'
        '  "affected_sectors": ["sector1", "sector2"],\n'
        '  "sector_impact": {"tech": "negative", "defense": "positive", "energy": "neutral"},\n'
        '  "ticker_mentions": ["AAPL", "BA"],\n'
        '  "summary": "2-3 sentence assessment",\n'
        '  "recommendation": "buy_the_dip" | "stay_cautious" | "normal",\n'
        '  "trade_ideas": [{"symbol": "BA", "direction": "BUY", "reasoning": "Defense spending likely to increase"}]\n'
        "}\n\n"
        "Consider: Is weakness driven by political noise vs fundamentals? "
        "Which specific sectors and stocks benefit or suffer from these headlines? "
        "Political panic selloffs often overcorrect and revert within days."
    )

    try:
        response_text = call_ai(
            prompt,
            provider=ctx.ai_provider if ctx else "anthropic",
            model=ctx.ai_model if ctx else config.CLAUDE_MODEL,
            api_key=ctx.ai_api_key if ctx else config.ANTHROPIC_API_KEY,
            max_tokens=512,
            db_path=getattr(ctx, "db_path", None) if ctx else None,
            purpose="political_context",
        )

        # Track API usage
        if ctx is not None:
            try:
                from models import increment_api_usage
                increment_api_usage(ctx.user_id)
            except Exception:
                pass

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

    # Sector-specific impact
    sector_impact = climate.get("sector_impact", {})
    impact_lines = []
    for sector, impact in sector_impact.items():
        if impact != "neutral":
            impact_lines.append(f"  {sector}: {impact}")
    impact_str = "\n".join(impact_lines) if impact_lines else "  No sector-specific impact"

    # Specific ticker mentions and trade ideas
    tickers = climate.get("ticker_mentions", [])
    trade_ideas = climate.get("trade_ideas", [])
    ideas_str = ""
    if trade_ideas:
        ideas_str = "\nPolitical Trade Ideas: " + "; ".join(
            f"{t.get('symbol', '?')} {t.get('direction', '?')} ({t.get('reasoning', '')})"
            for t in trade_ideas[:3]
        )

    context = (
        f"POLITICAL MARKET CONTEXT (MAGA Mode Active):\n"
        f"Volatility Level: {vol_level}\n"
        f"Assessment: {summary}\n"
        f"Panic-Driven: {'Yes' if is_panic else 'No'} | "
        f"Expected Duration: {duration}\n"
        f"Affected Sectors: {sectors_str}\n"
        f"Sector Impact:\n{impact_str}\n"
        f"Recommendation: {rec_text}"
        f"{ideas_str}"
    )

    return context
