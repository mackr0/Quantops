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

    Stays on yfinance: Alpaca is a broker — they expose price data
    but NOT fundamentals (book value, shares outstanding). Replacing
    this would require a fundamentals vendor (Polygon Stocks Plus,
    Financial Modeling Prep, etc., all paid). Documented as an
    accepted yfinance use in feedback_alpaca_first_data.md.

    Returns None when fundamentals aren't available. Cached 1 week.
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
    """Return 2-year beta vs SPY computed from daily returns.

    Migrated 2026-05-01 from yfinance.Ticker.info.beta (which used
    Yahoo's 5-year computation) to a local OLS regression on Alpaca
    bars. We pay for Alpaca; computing beta from real bars beats
    leaning on Yahoo's stale field.

    Math: β = cov(symbol_returns, spy_returns) / var(spy_returns)
    over the trailing ~500 trading days (~2 years). Daily log returns,
    aligned by date.

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
        from market_data import get_bars
        import numpy as np
        sym_bars = get_bars(symbol, limit=500)
        spy_bars = get_bars("SPY", limit=500)
        if (sym_bars is None or spy_bars is None
                or len(sym_bars) < 60 or len(spy_bars) < 60):
            _set_cached(symbol, "beta", None)
            return None

        sym_returns = sym_bars["close"].pct_change().dropna()
        spy_returns = spy_bars["close"].pct_change().dropna()

        # Align by date — both are tz-aware DateTimeIndex
        common = sym_returns.index.intersection(spy_returns.index)
        if len(common) < 60:
            _set_cached(symbol, "beta", None)
            return None

        s = sym_returns.loc[common].values
        m = spy_returns.loc[common].values

        var_m = float(np.var(m, ddof=1))
        if var_m <= 0:
            _set_cached(symbol, "beta", None)
            return None
        cov_sm = float(np.cov(s, m, ddof=1)[0, 1])
        beta_val = cov_sm / var_m
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


def get_realized_vol(symbol: str, days: int = 30) -> Optional[float]:
    """Return annualized realized volatility from `days` of daily log
    returns. Returned as a fraction (0.25 = 25% annualized vol — typical
    mid-vol large cap; 0.50+ = high-vol; 0.15 = utility-like).

    Cached 1 week — vol moves daily but 7-day staleness is acceptable
    for sizing-guidance purposes (and matches the rest of factor_data's
    refresh cadence).
    """
    if not symbol or "/" in symbol:
        return None
    cache_key = f"vol_{days}d"
    cached = _get_cached(symbol, cache_key)
    if cached is not None:
        return cached
    try:
        from market_data import get_bars
        # Need `days+1` closes to produce `days` returns. Buffer a bit
        # in case of holidays.
        bars = get_bars(symbol, limit=days + 10)
        if bars is None or len(bars) < days + 1:
            _set_cached(symbol, cache_key, None)
            return None
        import math
        closes = bars["close"].iloc[-(days + 1):].tolist()
        rets = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                rets.append(math.log(closes[i] / closes[i - 1]))
        if len(rets) < 5:
            _set_cached(symbol, cache_key, None)
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        daily_std = math.sqrt(var)
        annualized = daily_std * math.sqrt(252)
        _set_cached(symbol, cache_key, annualized)
        return annualized
    except Exception as exc:
        logger.debug("get_realized_vol(%s) failed: %s", symbol, exc)
    _set_cached(symbol, cache_key, None)
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
