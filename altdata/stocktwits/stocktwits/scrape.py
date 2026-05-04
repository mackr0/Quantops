"""StockTwits REST API client.

Endpoints used:
  GET /api/2/streams/symbol/{TICKER}.json     last ~30 messages for a ticker
  GET /api/2/trending/symbols.json             top trending tickers right now

Rate limit: documented as 200 req/hour for unauthenticated, 400/hour
with a registered app token. We default to 200/hour (free, no key).
That gives ~1 req every 18 seconds — we cap our throttle at 20s.

Design choice: poll a watchlist (not the firehose). Pulling messages
for 50 tickers every 30 minutes consumes ~100 requests/hour, well
under the limit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .store import (
    finish_run,
    insert_message,
    insert_raw_response,
    insert_trending_snapshot,
    start_run,
    upsert_daily_sentiment,
)

logger = logging.getLogger(__name__)


BASE = "https://api.stocktwits.com/api/2"
USER_AGENT = "stocktwits Research Tool mack@mackenziesmith.com"

# Politeness: 20s between requests = 180 req/hour, just under the 200/hour
# unauthenticated cap. With ~50 tickers polled, we cycle through them
# every ~17 minutes — fast enough for sentiment, gentle on the API.
REQUEST_DELAY_SEC = 20.0

PARSER_VERSION = "stocktwits-rest-v1"


class RateLimitedError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    time.sleep(REQUEST_DELAY_SEC)
    r = requests.get(
        BASE + path, params=params, timeout=30,
        headers={"User-Agent": USER_AGENT},
    )
    if r.status_code in (429, 403):
        raise RateLimitedError(
            f"StockTwits returned HTTP {r.status_code}. "
            f"Re-run after waiting — cached rows preserved."
        )
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Pure parser — easy to test, no I/O
# ---------------------------------------------------------------------------

def parse_messages_response(json_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract message dicts from a /streams/symbol response.

    Each message has the shape we need for insert_message.
    """
    out = []
    for msg in (json_data or {}).get("messages") or []:
        # Sentiment lives in entities.sentiment.basic ('Bullish' | 'Bearish')
        sent = None
        ent = (msg.get("entities") or {}).get("sentiment")
        if isinstance(ent, dict):
            basic = (ent.get("basic") or "").strip().lower()
            if basic in ("bullish", "bearish"):
                sent = basic
        user = msg.get("user") or {}
        out.append({
            "msg_id": msg.get("id"),
            "body": msg.get("body") or "",
            "created_at": msg.get("created_at") or "",
            "user_id": user.get("id"),
            "user_name": user.get("username"),
            "sentiment": sent,
            "like_count": (msg.get("likes") or {}).get("total", 0),
        })
    return out


def parse_trending_response(json_data: Dict[str, Any]) -> List[str]:
    """Extract ticker symbols (in rank order) from /trending/symbols."""
    out = []
    for sym in (json_data or {}).get("symbols") or []:
        symbol = sym.get("symbol")
        if symbol:
            out.append(symbol.upper())
    return out


# ---------------------------------------------------------------------------
# Top-level fetchers
# ---------------------------------------------------------------------------

def fetch_messages_for_ticker(
    db_conn: sqlite3.Connection,
    ticker: str,
) -> Dict[str, int]:
    """Pull recent messages for one ticker, persist, update daily aggregate."""
    stats = {"messages_seen": 0, "messages_new": 0}

    try:
        r = _get(f"/streams/symbol/{ticker.upper()}.json")
    except RateLimitedError:
        raise
    except Exception as exc:
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return stats

    insert_raw_response(
        db_conn, endpoint="streams/symbol",
        request_params={"symbol": ticker},
        payload=r.text,
    )

    try:
        data = r.json()
    except Exception as exc:
        logger.warning("StockTwits JSON parse failed for %s: %s", ticker, exc)
        return stats

    msgs = parse_messages_response(data)
    seen_dates = set()
    for m in msgs:
        stats["messages_seen"] += 1
        if not m["msg_id"] or not m["created_at"]:
            continue
        if insert_message(
            db_conn, msg_id=m["msg_id"], ticker=ticker,
            body=m["body"], created_at=m["created_at"],
            user_id=m["user_id"], user_name=m["user_name"],
            sentiment=m["sentiment"], like_count=m["like_count"],
            parser_version=PARSER_VERSION,
        ):
            stats["messages_new"] += 1
        seen_dates.add(m["created_at"][:10])  # YYYY-MM-DD

    # Recompute daily aggregates for any date we touched
    for date in seen_dates:
        upsert_daily_sentiment(db_conn, ticker, date)

    db_conn.commit()
    return stats


def fetch_trending(db_conn: sqlite3.Connection) -> List[str]:
    """Pull current trending tickers, save snapshot."""
    try:
        r = _get("/trending/symbols.json")
    except Exception as exc:
        logger.warning("Trending fetch failed: %s", exc)
        return []

    insert_raw_response(db_conn, endpoint="trending", payload=r.text)
    try:
        data = r.json()
    except Exception:
        return []

    tickers = parse_trending_response(data)
    if tickers:
        snapshot_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        insert_trending_snapshot(db_conn, snapshot_at, tickers)
        db_conn.commit()
    return tickers


def fetch_watchlist(
    db_conn: sqlite3.Connection,
    tickers: Iterable[str],
) -> Dict[str, int]:
    """Pull messages for every ticker in the watchlist sequentially.

    With our 20s delay, 50 tickers takes ~17 minutes. Reasonable for a
    daily refresh that runs in the background.
    """
    run_id = start_run(db_conn, "stocktwits:watchlist")
    overall = {"tickers_seen": 0, "messages_new": 0, "errors": 0}

    try:
        for t in tickers:
            overall["tickers_seen"] += 1
            try:
                stats = fetch_messages_for_ticker(db_conn, t)
                overall["messages_new"] += stats["messages_new"]
            except RateLimitedError:
                raise
            except Exception as exc:
                overall["errors"] += 1
                logger.warning("Ticker %s failed: %s", t, exc)

        finish_run(db_conn, run_id, status="ok",
                   rows_inserted=overall["messages_new"],
                   rows_seen=overall["tickers_seen"])
    except RateLimitedError as exc:
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=overall["messages_new"],
                   rows_seen=overall["tickers_seen"],
                   error=f"rate limited: {exc}")
        raise
    except Exception as exc:
        finish_run(db_conn, run_id, status="failed",
                   rows_inserted=overall["messages_new"],
                   rows_seen=overall["tickers_seen"],
                   error=str(exc))
        raise

    return overall
