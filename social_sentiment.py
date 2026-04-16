"""Social media sentiment analysis using Reddit (PRAW).

Scans r/wallstreetbets, r/stocks, r/investing, r/options for ticker
mentions and sentiment signals. Requires Reddit API credentials
(free at reddit.com/prefs/apps — create a "script" type app).

Environment variables:
    REDDIT_CLIENT_ID     — Reddit app client ID
    REDDIT_CLIENT_SECRET — Reddit app secret
    REDDIT_USER_AGENT    — e.g., "QuantOpsAI/1.0"

If credentials are not configured, all functions return empty results
(graceful degradation — the system works without Reddit data).
"""

import logging
import os
import re
import time
from collections import Counter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reddit client (lazy-initialized)
# ---------------------------------------------------------------------------

_reddit = None
_reddit_available = None  # None = not checked, True/False = checked


def _get_reddit():
    """Get or create the PRAW Reddit client. Returns None if not configured."""
    global _reddit, _reddit_available

    if _reddit_available is False:
        return None
    if _reddit is not None:
        return _reddit

    client_id = os.getenv("REDDIT_CLIENT_ID", "")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    user_agent = os.getenv("REDDIT_USER_AGENT", "QuantOpsAI/1.0 (by /u/quantopsai)")

    if not client_id or not client_secret:
        logger.info("Reddit credentials not configured (REDDIT_CLIENT_ID / "
                     "REDDIT_CLIENT_SECRET). Social sentiment disabled.")
        _reddit_available = False
        return None

    try:
        import praw
        _reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        )
        _reddit_available = True
        logger.info("Reddit client initialized (read-only)")
        return _reddit
    except Exception as exc:
        logger.warning("Failed to initialize Reddit client: %s", exc)
        _reddit_available = False
        return None


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_mention_cache = {}
_MENTION_TTL = 1800  # 30 minutes

_trending_cache = {}
_TRENDING_TTL = 900  # 15 minutes


# ---------------------------------------------------------------------------
# Ticker mention detection
# ---------------------------------------------------------------------------

# Common words that look like tickers but aren't
_FALSE_TICKERS = {
    "A", "I", "AM", "AN", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "ALL", "ANY", "ARE", "BIG", "CAN", "CEO", "CFO", "CTO", "DD",
    "DIP", "EPS", "ETF", "FOR", "FED", "GDP", "HAS", "HOW", "IMO", "IPO",
    "IRS", "LLC", "NEW", "NOW", "OLD", "ONE", "OTC", "OUT", "OWN", "PE",
    "SEC", "THE", "TOP", "USD", "VIX", "WSB", "YOY", "ATH", "ATL", "FD",
    "OTM", "ITM", "DTE", "IV", "RSI", "SMA", "EMA", "MACD", "PUT", "CALL",
    "YOLO", "FOMO", "HODL", "MOON", "BEAR", "BULL", "PUMP", "DUMP",
    "RIP", "LOL", "WTF", "SMH", "TBH", "IMO", "FYI", "TLDR", "EDIT",
    "LMAO", "ROFL",
}

_TICKER_PATTERN = re.compile(r'\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b')


def _extract_tickers(text):
    """Extract likely stock tickers from text."""
    tickers = []
    for match in _TICKER_PATTERN.finditer(text):
        ticker = match.group(1) or match.group(2)
        if ticker and ticker not in _FALSE_TICKERS and len(ticker) <= 5:
            tickers.append(ticker)
    return tickers


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def get_ticker_mentions(symbol, subreddits=None, limit=100):
    """Count recent mentions of a ticker across trading subreddits.

    Returns dict with:
        mentions: int (total mentions in recent posts/comments)
        sentiment_score: float (-1.0 to +1.0, rough estimate)
        trending: bool (significantly more mentions than normal)
        sample_titles: list[str] (up to 5 post titles mentioning the ticker)
        subreddits_found: list[str] (which subs mentioned it)
    """
    cache_key = f"mentions_{symbol}"
    cached = _mention_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _MENTION_TTL:
        return cached[1]

    result = {
        "mentions": 0, "sentiment_score": 0.0, "trending": False,
        "sample_titles": [], "subreddits_found": [],
    }

    reddit = _get_reddit()
    if reddit is None:
        return result

    if subreddits is None:
        subreddits = ["wallstreetbets", "stocks", "investing", "options"]

    symbol_upper = symbol.upper().replace("/", "")
    positive_words = {"moon", "calls", "bull", "up", "long", "buy", "gains",
                      "profit", "squeeze", "breakout", "undervalued", "rocket"}
    negative_words = {"puts", "bear", "short", "sell", "crash", "dump",
                      "overvalued", "loss", "down", "bag", "dead", "rip"}

    mentions = 0
    pos_score = 0
    neg_score = 0
    titles = []
    subs_found = set()

    for sub_name in subreddits:
        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.hot(limit=limit):
                text = f"{post.title} {post.selftext}".upper()
                if symbol_upper in text or f"${symbol_upper}" in text:
                    mentions += 1
                    subs_found.add(sub_name)
                    if len(titles) < 5:
                        titles.append(post.title[:100])

                    # Rough sentiment from keywords
                    text_lower = f"{post.title} {post.selftext}".lower()
                    for w in positive_words:
                        if w in text_lower:
                            pos_score += 1
                    for w in negative_words:
                        if w in text_lower:
                            neg_score += 1
        except Exception as exc:
            logger.debug("Reddit scan failed for r/%s: %s", sub_name, exc)

    # Calculate sentiment score
    total_sentiment = pos_score + neg_score
    if total_sentiment > 0:
        result["sentiment_score"] = round((pos_score - neg_score) / total_sentiment, 2)

    result["mentions"] = mentions
    result["trending"] = mentions >= 5  # 5+ mentions = trending
    result["sample_titles"] = titles
    result["subreddits_found"] = list(subs_found)

    _mention_cache[cache_key] = (time.time(), result)
    return result


def get_trending_tickers(subreddits=None, limit=100):
    """Find the most-mentioned tickers across trading subreddits.

    Returns list of dicts sorted by mention count:
        [{ticker: str, mentions: int, sentiment: float, subreddits: [str]}]
    """
    cache_key = "trending_all"
    cached = _trending_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _TRENDING_TTL:
        return cached[1]

    reddit = _get_reddit()
    if reddit is None:
        return []

    if subreddits is None:
        subreddits = ["wallstreetbets", "stocks"]

    ticker_counts = Counter()
    ticker_subs = {}

    for sub_name in subreddits:
        try:
            subreddit = reddit.subreddit(sub_name)
            for post in subreddit.hot(limit=limit):
                text = f"{post.title} {post.selftext}"
                tickers = _extract_tickers(text)
                for t in tickers:
                    ticker_counts[t] += 1
                    ticker_subs.setdefault(t, set()).add(sub_name)
        except Exception as exc:
            logger.debug("Reddit trending scan failed for r/%s: %s", sub_name, exc)

    # Build result sorted by mentions
    result = [
        {
            "ticker": ticker,
            "mentions": count,
            "subreddits": list(ticker_subs.get(ticker, [])),
        }
        for ticker, count in ticker_counts.most_common(20)
        if count >= 3  # Minimum 3 mentions
    ]

    _trending_cache[cache_key] = (time.time(), result)
    return result


def is_available():
    """Check if Reddit credentials are configured."""
    return _get_reddit() is not None
