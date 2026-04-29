"""Real factor exposures: book-to-market, beta, momentum 12-1m.

P3.6 of LONG_SHORT_PLAN.md. Phase 3's previous P2.5 used a price-band
size proxy because we didn't have fundamentals data cached. This
module fills that gap with the three classic equity factors that
have decades of academic evidence:

  - Book-to-Market (Fama & French 1992): high B/M = "value" stocks
    that historically outperform low B/M = "growth" stocks.
  - Beta vs SPY: stylized market sensitivity. Book-level beta
    near 1.0 = market-correlated; near 0 = market-neutral; >1 =
    levered to market.
  - Momentum 12-1m (Jegadeesh & Titman 1993): 12-month return
    excluding the last month (to avoid short-term reversal bias).
    Long winners + short losers is the "momentum" factor.

Pattern follows alternative_data.py — yfinance fetch + SQLite cache.
Refresh weekly because these factors don't change rapidly. All
functions degrade gracefully on errors (return None) so the caller
can decide how to handle missing data.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)

_DB_PATH = config.DB_PATH
_FACTOR_TTL_SECONDS = 7 * 86400  # 1 week

_table_ensured = False


def _ensure_cache_table() -> None:
    global _table_ensured
    if _table_ensured:
        return
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS factor_cache (
                symbol TEXT NOT NULL,
                factor TEXT NOT NULL,
                value REAL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (symbol, factor)
            )
        """)
        conn.commit()
        conn.close()
        _table_ensured = True
    except Exception as exc:
        logger.debug("factor_cache table ensure failed: %s", exc)


def _get_cached(symbol: str, factor: str) -> Optional[float]:
    _ensure_cache_table()
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT value, fetched_at FROM factor_cache "
            "WHERE symbol = ? AND factor = ?",
            (symbol.upper(), factor),
        ).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < _FACTOR_TTL_SECONDS:
            return row[0]
    except Exception:
        pass
    return None


def _set_cached(symbol: str, factor: str, value: Optional[float]) -> None:
    _ensure_cache_table()
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO factor_cache "
            "(symbol, factor, value, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (symbol.upper(), factor, value, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-factor fetchers
# ---------------------------------------------------------------------------

def get_book_to_market(symbol: str) -> Optional[float]:
    """Return book-to-market ratio (book_value / market_cap).

    > 1.0  = value stock (book exceeds market cap)
    0.3-1  = mid (typical mature company)
    < 0.3  = growth stock (intangibles + future earnings priced in)

    Returns None when fundamentals aren't available (yfinance miss).
    Cached 1 week.
    """
    if not symbol or "/" in symbol:
        return None
    cached = _get_cached(symbol, "btm")
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        book_value_per_share = info.get("bookValue")
        market_cap = info.get("marketCap")
        shares = info.get("sharesOutstanding")
        if book_value_per_share and shares and market_cap:
            book = float(book_value_per_share) * float(shares)
            mcap = float(market_cap)
            if mcap > 0:
                btm = book / mcap
                _set_cached(symbol, "btm", btm)
                return btm
    except Exception as exc:
        logger.debug("get_book_to_market(%s) failed: %s", symbol, exc)
    _set_cached(symbol, "btm", None)
    return None


def get_beta(symbol: str) -> Optional[float]:
    """Return 5-year beta vs S&P 500 from yfinance.info.beta.

    < 0.7  = defensive (utilities, staples)
    0.7-1.3 = market-correlated
    > 1.3  = levered to market (high-vol tech, financials)

    Cached 1 week.
    """
    if not symbol or "/" in symbol:
        return None
    cached = _get_cached(symbol, "beta")
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        beta = info.get("beta")
        if beta is not None:
            beta_val = float(beta)
            _set_cached(symbol, "beta", beta_val)
            return beta_val
    except Exception as exc:
        logger.debug("get_beta(%s) failed: %s", symbol, exc)
    _set_cached(symbol, "beta", None)
    return None


def get_momentum_12_1(symbol: str) -> Optional[float]:
    """Return 12-1 month momentum: total return from 12 months ago to
    1 month ago. Skipping the last month avoids the well-documented
    short-term reversal effect (Jegadeesh & Titman 1993; Asness 1994).

    Returned as a fraction (0.15 = +15% over the window).
    Requires ≥ 252 daily bars. Returns None on insufficient data.
    Cached 1 week — recomputed weekly is sufficient since the formula
    is anchored to month boundaries that drift slowly.
    """
    if not symbol or "/" in symbol:
        return None
    cached = _get_cached(symbol, "mom_12_1")
    if cached is not None:
        return cached
    try:
        from market_data import get_bars
        bars = get_bars(symbol, limit=300)  # buffer over 252 in case of holidays
        if bars is None or len(bars) < 252:
            _set_cached(symbol, "mom_12_1", None)
            return None
        # Index by trading-day position. 252 ≈ 1 calendar year of trading days,
        # 21 ≈ 1 month.
        price_252_ago = float(bars["close"].iloc[-252])
        price_21_ago = float(bars["close"].iloc[-21])
        if price_252_ago <= 0:
            _set_cached(symbol, "mom_12_1", None)
            return None
        mom = (price_21_ago - price_252_ago) / price_252_ago
        _set_cached(symbol, "mom_12_1", mom)
        return mom
    except Exception as exc:
        logger.debug("get_momentum_12_1(%s) failed: %s", symbol, exc)
    _set_cached(symbol, "mom_12_1", None)
    return None


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

def classify_book_to_market(btm: Optional[float]) -> str:
    """value | mid | growth | unknown."""
    if btm is None:
        return "unknown"
    if btm >= 1.0:
        return "value"
    if btm >= 0.3:
        return "mid"
    return "growth"


def classify_beta(beta: Optional[float]) -> str:
    """defensive | market | levered | unknown."""
    if beta is None:
        return "unknown"
    if beta < 0.7:
        return "defensive"
    if beta <= 1.3:
        return "market"
    return "levered"


def classify_momentum(mom: Optional[float]) -> str:
    """winner | neutral | loser | unknown.

    > +10% over 12-1m = winner
    -10% to +10% = neutral
    < -10% = loser
    """
    if mom is None:
        return "unknown"
    if mom > 0.10:
        return "winner"
    if mom < -0.10:
        return "loser"
    return "neutral"


def get_factor_classification(symbol: str) -> Dict[str, str]:
    """Return all three factor classifications for a symbol.
    Each value is one of the bucket strings (or 'unknown').

    Single round-trip — calls each fetcher (which independently caches).
    Designed to be safe to call inside a per-position loop:
    the cache layer ensures we hit yfinance once per (symbol, factor)
    per week. Crypto symbols and any symbol whose fundamentals
    aren't reachable end up with all-'unknown' classifications,
    which the caller's bucket logic absorbs into the unknown column.
    """
    return {
        "btm": classify_book_to_market(get_book_to_market(symbol)),
        "beta": classify_beta(get_beta(symbol)),
        "momentum": classify_momentum(get_momentum_12_1(symbol)),
    }
