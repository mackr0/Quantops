"""News fetching and AI-powered sentiment analysis.

2026-05-19 — `analyze_sentiment` migrated from a hardcoded Anthropic
client (`get_claude_client()` + Claude-only) to the provider-agnostic
`ai_providers.call_ai`. Resolution order for the LLM provider/key:
  1. Explicit `ctx` argument (per-profile provider + key)
  2. Explicit `user_id` argument → loads `users.llm_provider` +
     decrypted fallback key via `get_user_llm_settings`
  3. Neither → returns a NEUTRAL sentiment dict with an explanatory
     error field (no crash, no silent fallback to a process-level key)

The trade pipeline doesn't invoke `analyze_sentiment` today — it
uses `fetch_news_alpaca` for headlines only — but CLI tools
(`main.py sentiment AAPL`, `main.py ai-scan`) and any future
caller now respect the user's chosen LLM instead of secretly
calling Anthropic.
"""

import json
import logging
import time

from client import get_api

logger = logging.getLogger(__name__)


def fetch_news(symbol, limit=10, api=None):
    """
    Fetch recent news articles for *symbol*.

    Migrated 2026-05-01 from yfinance to Alpaca News API. The earlier
    note about "Alpaca news requires paid subscription" was wrong:
    `data.alpaca.markets/v1beta1/news` works with our existing keys
    (verified status 200 returning Benzinga headlines). The historical
    401 the old comment mentioned was probably a different endpoint or
    a stale credentials issue. yfinance fallback retained inside
    `fetch_news_alpaca` for the rare case Alpaca returns nothing.

    Returns a list of headline strings (the AI's sentiment analyzer
    only needs the headline; full article retrieval costs tokens
    without proportional signal gain).
    """
    return fetch_news_alpaca(symbol, limit=min(limit, 5))


def analyze_sentiment(symbol, news_items, ctx=None, user_id=None):
    """
    Score news headlines via the user's configured LLM and return
    per-item sentiment plus an aggregate score.

    Parameters
    ----------
    symbol : str
        The ticker symbol the news relates to.
    news_items : list[dict]
        Output of fetch_news().
    ctx : UserContext, optional
        When provided, the AI call uses `ctx.ai_provider`,
        `ctx.ai_model`, `ctx.ai_api_key` — i.e. the per-profile LLM.
    user_id : int, optional
        When `ctx` is not provided, falls back to the user's
        `Settings → Fallback LLM Key` configuration
        (`users.llm_provider` + `users.anthropic_api_key_enc`).
    Without either argument: returns a NEUTRAL sentiment with an
    "error" field — no silent .env fallback (removed 2026-05-19).

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

    # Resolve provider / key — ctx wins; user_id fallback; otherwise
    # return a NEUTRAL sentiment with the lack-of-key explained.
    provider = None
    model = None
    api_key = None
    db_path = None
    if ctx is not None:
        provider = getattr(ctx, "ai_provider", None)
        model = getattr(ctx, "ai_model", None)
        api_key = getattr(ctx, "ai_api_key", None)
        db_path = getattr(ctx, "db_path", None)
    elif user_id is not None:
        try:
            from models import get_user_llm_settings
            settings = get_user_llm_settings(user_id)
            provider = settings.get("provider")
            api_key = settings.get("api_key")
            # Pick a sane default model per provider when none is set
            # at user level (UI doesn't surface model — keep simple).
            from ai_providers import _DEFAULT_MODELS
            model = _DEFAULT_MODELS.get(provider) if provider else None
        except Exception as exc:
            logger.warning(
                "analyze_sentiment: failed to load user LLM settings "
                "for user_id=%s: %s", user_id, exc,
            )

    if not provider or not api_key:
        return {
            "symbol": symbol,
            "overall_score": 0.0,
            "label": "NEUTRAL",
            "items": [],
            "error": (
                "No LLM key available for sentiment scoring. Pass "
                "ctx= for per-profile usage, or user_id= to fall back "
                "to Settings → Fallback LLM Key."
            ),
        }

    try:
        from ai_providers import call_ai
        response_text = call_ai(
            prompt,
            provider=provider, model=model, api_key=api_key,
            max_tokens=1024,
            db_path=db_path,
            purpose="news_sentiment",
        )
        # Use the tolerant parser shared with ai_analyst — handles
        # markdown fences (```json ... ```), trailing prose, and
        # truncated arrays. The strict json.loads here was the source
        # of "Failed to parse sentiment response" warnings on every
        # symbol with news, leaving every consumer (news_sentiment_spike
        # strategy + AI prompt-injection sites) with empty sentiment.
        from ai_analyst import _parse_ai_response_tolerant
        result = _parse_ai_response_tolerant(response_text)
        if not isinstance(result, dict) or "overall_score" not in result:
            raise json.JSONDecodeError(
                "tolerant parser returned non-dict or missing overall_score",
                response_text, 0,
            )
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


def get_sentiment_signal(symbol, ctx=None, user_id=None):
    """
    Convenience function: fetch news, analyze sentiment, and convert to a
    trading signal dict.

    `ctx` / `user_id` flow through to `analyze_sentiment` so callers
    can choose between per-profile and per-user-fallback LLMs.

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

    sentiment = analyze_sentiment(symbol, news_items, ctx=ctx,
                                    user_id=user_id)
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

_news_cache = {}
_NEWS_TTL = 1800  # 30 minutes


def fetch_news_alpaca(symbol, limit=3):
    """Fetch recent headlines via Alpaca News API.

    Alpaca's `/v1beta1/news` endpoint serves the Benzinga feed and
    works with our existing paper-account API keys. Real-time, free
    with our subscription.

    Returns list of dicts with keys {source, headline, summary,
    created_at}. The dict shape matches what `analyze_sentiment`
    expects — pre-2026-05-15 this returned plain strings, which
    crashed `analyze_sentiment` with TypeError on `item['source']`
    every time a symbol had news. Caught by strategies' broad
    except blocks at debug level → entire `news_sentiment_spike`
    strategy silently never fired. Cached for 30 minutes per symbol.
    Uses `market_data._resolve_alpaca_credentials` which sources
    Alpaca creds from the `alpaca_accounts` DB table (the env-level
    "master key" path was removed 2026-05-19).
    """
    now = time.time()
    cached = _news_cache.get(symbol)
    if cached and (now - cached[0]) < _NEWS_TTL:
        return cached[1]

    if "/" in symbol:
        # Crypto: Alpaca news doesn't cover crypto pairs reliably;
        # skip rather than fall through.
        _news_cache[symbol] = (now, [])
        return []

    try:
        import requests
        from market_data import _resolve_alpaca_credentials
        key, secret, _ = _resolve_alpaca_credentials()
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
            },
            params={
                "symbols": symbol.upper(),
                "limit": max(limit, 3),
            },
            timeout=10,
        )
        if r.status_code != 200:
            logger.debug("Alpaca news %s: %s %s",
                         symbol, r.status_code, r.text[:200])
            _news_cache[symbol] = (now, [])
            return []
        data = r.json()
        items = []
        for item in (data.get("news") or [])[:limit]:
            h = item.get("headline", "")
            if not h:
                continue
            items.append({
                "headline": h,
                "summary": item.get("summary", ""),
                "source": item.get("source", "alpaca"),
                "created_at": item.get("created_at", ""),
            })
        _news_cache[symbol] = (now, items)
        return items
    except Exception as exc:
        logger.debug("Alpaca news fetch failed for %s: %s", symbol, exc)
        _news_cache[symbol] = (now, [])
        return []


