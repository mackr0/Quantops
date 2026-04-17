"""News fetching and AI-powered sentiment analysis."""

import json
import logging

from config import CLAUDE_MODEL
from client import get_api
from ai_analyst import get_claude_client

logger = logging.getLogger(__name__)


def fetch_news(symbol, limit=10, api=None):
    """
    Fetch recent news articles for *symbol*.

    Uses yfinance (free) instead of Alpaca's news API which requires
    a separate paid subscription we don't have. The Alpaca path was
    silently failing with 401 Unauthorized on every call and flooding
    the logs with errors.

    Returns a list of dicts with headline, source, and link.
    """
    return fetch_news_yfinance(symbol, limit=min(limit, 5))


def analyze_sentiment(symbol, news_items):
    """
    Send news headlines/summaries to Claude and get sentiment scores.

    Parameters
    ----------
    symbol : str
        The ticker symbol the news relates to.
    news_items : list[dict]
        Output of fetch_news().

    Returns a dict with per-item scores and an overall sentiment score
    ranging from -1.0 (very bearish) to +1.0 (very bullish).
    """
    if not news_items:
        return {
            "symbol": symbol,
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "items": [],
        }

    # Build a compact representation of the headlines for the prompt
    headlines_text = "\n".join(
        f"{i+1}. [{item['source']}] {item['headline']}"
        + (f" — {item['summary'][:200]}" if item.get("summary") else "")
        for i, item in enumerate(news_items)
    )

    prompt = (
        "You are a financial news sentiment analyst. Analyze the following "
        f"news items for {symbol} and score each one.\n\n"
        f"News Items:\n{headlines_text}\n\n"
        "Respond ONLY with valid JSON (no markdown fences) using this schema:\n"
        "{\n"
        '  "overall_score": <float from -1.0 to 1.0>,\n'
        '  "label": "VERY_BEARISH" | "BEARISH" | "NEUTRAL" | "BULLISH" | "VERY_BULLISH",\n'
        '  "items": [\n'
        "    {\n"
        '      "index": <int>,\n'
        '      "headline": "<str>",\n'
        '      "score": <float from -1.0 to 1.0>,\n'
        '      "reasoning": "<brief explanation>"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Score interpretation: -1.0 = extremely bearish, 0.0 = neutral, "
        "+1.0 = extremely bullish. Consider the impact on the stock price."
    )

    try:
        client = get_claude_client()
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()
        result = json.loads(response_text)
        result["symbol"] = symbol
        return result

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse sentiment response: %s", exc)
        return {
            "symbol": symbol,
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "items": [],
            "error": f"AI response was not valid JSON: {exc}",
        }
    except Exception as exc:
        logger.error("Error in analyze_sentiment for %s: %s", symbol, exc)
        return {
            "symbol": symbol,
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "items": [],
            "error": str(exc),
        }


def get_sentiment_signal(symbol):
    """
    Convenience function: fetch news, analyze sentiment, and convert to a
    trading signal dict.

    Signal thresholds:
        overall_score > 0.3  -> BUY
        overall_score < -0.3 -> SELL
        otherwise            -> HOLD
    """
    news_items = fetch_news(symbol)

    if not news_items:
        return {
            "symbol": symbol,
            "signal": "HOLD",
            "sentiment_score": 0.0,
            "label": "NEUTRAL",
            "reason": "No recent news available.",
            "news_count": 0,
        }

    sentiment = analyze_sentiment(symbol, news_items)
    score = sentiment.get("overall_score", 0.0)
    label = sentiment.get("label", "NEUTRAL")

    if score > 0.3:
        signal = "BUY"
        reason = f"Positive news sentiment ({score:+.2f}): {label}"
    elif score < -0.3:
        signal = "SELL"
        reason = f"Negative news sentiment ({score:+.2f}): {label}"
    else:
        signal = "HOLD"
        reason = f"Neutral news sentiment ({score:+.2f}): {label}"

    return {
        "symbol": symbol,
        "signal": signal,
        "sentiment_score": score,
        "label": label,
        "reason": reason,
        "news_count": len(news_items),
        "items": sentiment.get("items", []),
    }


# ---------------------------------------------------------------------------
# Free yfinance news (no Alpaca key needed)
# ---------------------------------------------------------------------------

_yf_news_cache = {}
_YF_NEWS_TTL = 1800  # 30 minutes

def fetch_news_yfinance(symbol, limit=3):
    """Fetch recent headlines for a symbol from yfinance (free, no API key).

    Returns list of headline strings. Cached for 30 minutes per symbol.
    """
    import time
    now = time.time()
    cached = _yf_news_cache.get(symbol)
    if cached and (now - cached[0]) < _YF_NEWS_TTL:
        return cached[1]

    try:
        import yfinance as yf
        yf_symbol = symbol.replace("/", "-") if "/" in symbol else symbol
        ticker = yf.Ticker(yf_symbol)
        news = ticker.news or []
        headlines = []
        for item in news[:limit]:
            title = item.get("title", "")
            if title:
                headlines.append(title)
        _yf_news_cache[symbol] = (now, headlines)
        return headlines
    except Exception as exc:
        logger.debug("Failed to fetch yfinance news for %s: %s", symbol, exc)
        _yf_news_cache[symbol] = (now, [])
        return []
