"""Earnings calendar — avoid trading around earnings dates.

Earnings dates are scheduled quarterly events that don't change
frequently. We cache them for 7 days per symbol to avoid flooding
Yahoo with requests. The only time we need to re-check sooner is
when a cached date is within 14 days (imminent earnings may get
rescheduled, so we refresh weekly instead of monthly).
"""

import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, date, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

_DB_PATH = "quantopsai.db"
_REFRESH_INTERVAL = 7 * 24 * 60 * 60  # 7 days (was 24 hours — way too aggressive)


def _ensure_table():
    """Create the earnings_dates table if it doesn't exist."""
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS earnings_dates (
                    symbol TEXT PRIMARY KEY,
                    earnings_date TEXT,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _ci_exc:
        # Cache schema init; cache writes that fail leave callers
        # in non-cached path. Surface for follow-up.
        logger.warning(
            "earnings cache schema init failed: %s: %s",
            type(_ci_exc).__name__, _ci_exc,
        )


def _fetch_and_store(symbol: str) -> Optional[str]:
    """Fetch earnings date from yfinance and store in DB.
    Returns the earnings_date string or None.

    Skip yfinance for symbols Alpaca doesn't carry (delisted/acquired)
    — they pollute the logs with "possibly delisted" errors and the
    answer wouldn't be tradable anyway.
    """
    import yfinance as yf
    try:
        from screener import is_alpaca_active
        if not is_alpaca_active(symbol):
            _store(symbol, None)
            return None
    except ImportError:
        pass

    try:
        import yf_lock as _yfl
        with _yfl._lock:
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar

        if cal is None:
            _store(symbol, None)
            return None

        earnings_date = None

        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed is not None:
                if isinstance(ed, list) and len(ed) > 0:
                    earnings_date = ed[0]
                elif hasattr(ed, "date"):
                    earnings_date = ed
        else:
            try:
                if hasattr(cal, "loc") and "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"]
                    if hasattr(ed, "iloc"):
                        earnings_date = ed.iloc[0]
                    else:
                        earnings_date = ed
            except (KeyError, ValueError, AttributeError, TypeError, IndexError) as _yc_exc:
                # yfinance calendar parse fallback; falls through to
                # other date sources below. Surface for follow-up.
                logger.debug(
                    "yfinance calendar parse failed for %s: %s: %s",
                    symbol, type(_yc_exc).__name__, _yc_exc,
                )

        if earnings_date is None:
            _store(symbol, None)
            return None

        if hasattr(earnings_date, "date"):
            ed_str = str(earnings_date.date())
        elif isinstance(earnings_date, str):
            ed_str = earnings_date[:10]
        else:
            ed_str = str(earnings_date)

        _store(symbol, ed_str)
        return ed_str

    except Exception as exc:
        if "Crumb" in str(exc) or "401" in str(exc):
            _reset_yf_crumb()
        logger.warning("Earnings fetch failed for %s: %s", symbol, exc)
        return None


def _store(symbol: str, earnings_date: Optional[str]):
    """Store earnings date in DB."""
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO earnings_dates (symbol, earnings_date, fetched_at) "
                "VALUES (?, ?, datetime('now'))",
                (symbol, earnings_date),
            )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _cw_exc:
        # Cache write fallback; cache miss is acceptable next time.
        # Surface for follow-up.
        logger.debug(
            "earnings cache write failed: %s: %s",
            type(_cw_exc).__name__, _cw_exc,
        )


def _get_cached(symbol: str) -> tuple:
    """Return (earnings_date_str_or_None, is_fresh_bool).

    Cache is considered fresh if:
      - We have a future earnings date (no need to re-check until it passes)
      - OR the fetch happened within _REFRESH_INTERVAL (for symbols with
        no known date, we re-check periodically)
    """
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT earnings_date, fetched_at FROM earnings_dates WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if row is None:
            return None, False

        earnings_date_str = row[0]
        fetched_at = row[1]

        # If we have a future earnings date, no need to refetch until it passes
        if earnings_date_str:
            try:
                from zoneinfo import ZoneInfo
                today_et = datetime.now(ZoneInfo("America/New_York")).date()
                ed = datetime.strptime(earnings_date_str[:10], "%Y-%m-%d").date()
                if ed >= today_et:
                    return earnings_date_str, True
            except (ValueError, TypeError, AttributeError) as _et_exc:
                # ET date parse fallback; falls through to
                # staleness check below. Surface for follow-up.
                logger.debug(
                    "earnings ET date parse failed: %s: %s",
                    type(_et_exc).__name__, _et_exc,
                )

        # No future date — check if the fetch itself is recent enough
        try:
            fetched_dt = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S")
            age = (datetime.utcnow() - fetched_dt).total_seconds()
            is_fresh = age < _REFRESH_INTERVAL
        except Exception:
            is_fresh = False
        return earnings_date_str, is_fresh
    except Exception:
        return None, False


_crumb_reset_at = 0


def _reset_yf_crumb():
    """Reset yfinance's stale cookie cache."""
    global _crumb_reset_at
    now = time.time()
    if now - _crumb_reset_at < 300:
        return
    _crumb_reset_at = now
    try:
        import os
        cache_dir = os.path.expanduser("~/.cache/py-yfinance")
        for fname in ("cookies.db", "tkr-tz.db"):
            path = os.path.join(cache_dir, fname)
            if os.path.exists(path):
                os.remove(path)
    except (OSError, AttributeError) as _cc_exc:
        # yfinance cookie cleanup; failure has no functional
        # impact. Surface for follow-up.
        logger.debug(
            "yfinance cookie cleanup failed: %s: %s",
            type(_cc_exc).__name__, _cc_exc,
        )


def check_earnings(symbol: str) -> Optional[Dict]:
    """Check if a symbol has upcoming earnings.

    Returns dict with keys: symbol, earnings_date, days_until.
    Returns None if no upcoming earnings or lookup fails.

    Cache logic: if a future earnings date is stored, serves from cache
    indefinitely (no refetch needed until that date passes). For symbols
    with no known date, re-checks every 7 days.
    """
    if "/" in symbol:
        return None

    _ensure_table()

    # Check DB cache first
    cached_date, is_fresh = _get_cached(symbol)

    if not is_fresh:
        # Stale or missing — refresh from yfinance
        cached_date = _fetch_and_store(symbol)

    if cached_date is None:
        return None

    try:
        from zoneinfo import ZoneInfo
        ed_date = datetime.strptime(cached_date[:10], "%Y-%m-%d").date()
        today = datetime.now(ZoneInfo("America/New_York")).date()
        days_until = (ed_date - today).days

        if days_until < 0:
            return None

        return {
            "symbol": symbol,
            "earnings_date": str(ed_date),
            "days_until": days_until,
        }
    except Exception:
        return None


def _ensure_past_table():
    """Create the earnings_history table for past earnings dates.

    Separate from the future-dated `earnings_dates` table because the
    semantics differ: future dates are SCHEDULED events that we re-check
    if soon; past dates are HISTORICAL fact and never need re-checking
    once observed.
    """
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS earnings_history (
                    symbol TEXT NOT NULL,
                    earnings_date TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (symbol, earnings_date)
                )
            """)
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _ci_exc:
        logger.warning(
            "earnings_history schema init failed: %s: %s",
            type(_ci_exc).__name__, _ci_exc,
        )


def _store_past_dates(symbol: str, dates: list) -> None:
    """Insert observed past earnings dates. Idempotent via PRIMARY KEY."""
    if not dates:
        return
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO earnings_history "
                "(symbol, earnings_date) VALUES (?, ?)",
                [(symbol, d) for d in dates],
            )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.debug(
            "earnings_history write failed for %s: %s: %s",
            symbol, type(exc).__name__, exc,
        )


def days_since_last_earnings(symbol: str,
                              max_lookback_days: int = 120) -> Optional[int]:
    """Return calendar days since the most-recent past earnings
    announcement for `symbol`, or None if not known and not findable.

    Lookup order:
      1. `earnings_history` cache (this module's persistent store).
      2. yfinance `Ticker(sym).earnings_dates` — the only data source
         we have today for past earnings (Alpaca has no earnings
         calendar endpoint at all). Cached on first observation so
         subsequent scans don't re-hit yfinance.

    Per the project's Alpaca-first → custom-altdata → yfinance-last
    rule, yfinance is grandfathered HERE because no Alpaca alternative
    exists for earnings dates (same status as the existing FUTURE
    earnings cache in this module). All yfinance-knowledge is
    encapsulated inside this function — strategies never import
    yfinance themselves.
    """
    if "/" in symbol:
        return None
    _ensure_past_table()
    today = date.today()

    # 1. Cache lookup.
    try:
        with closing(sqlite3.connect(_DB_PATH)) as conn:
            row = conn.execute(
                "SELECT earnings_date FROM earnings_history "
                "WHERE symbol = ? AND date(earnings_date) <= date('now') "
                "ORDER BY earnings_date DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if row and row[0]:
            ed = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
            days = (today - ed).days
            if 0 <= days <= max_lookback_days:
                return days
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.debug(
            "earnings_history read failed for %s: %s: %s",
            symbol, type(exc).__name__, exc,
        )

    # 2. yfinance fetch (last resort, encapsulated here). Skip if
    # Alpaca doesn't carry the symbol — yfinance "possibly delisted"
    # errors don't help anyone if we can't trade it anyway.
    try:
        from screener import is_alpaca_active
        if not is_alpaca_active(symbol):
            return None
    except ImportError:
        pass
    try:
        import yfinance as yf
        try:
            import yf_lock as _yfl
            with _yfl._lock:
                df = yf.Ticker(symbol).earnings_dates
        except Exception:
            df = yf.Ticker(symbol).earnings_dates
        if df is None or len(df) == 0:
            return None
        past_dates = []
        for d in df.index:
            dt = d.to_pydatetime() if hasattr(d, "to_pydatetime") else d
            if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            if dt.date() <= today:
                past_dates.append(dt.date().isoformat())
        if not past_dates:
            return None
        # Persist for next time.
        _store_past_dates(symbol, past_dates)
        most_recent = max(past_dates)
        ed = datetime.strptime(most_recent, "%Y-%m-%d").date()
        days = (today - ed).days
        if 0 <= days <= max_lookback_days:
            return days
        return None
    except Exception as exc:
        logger.debug(
            "yfinance past-earnings fetch failed for %s: %s: %s",
            symbol, type(exc).__name__, exc,
        )
        return None


def get_earnings_context(symbol: str, avoid_days: int = 2) -> str:
    """Return earnings context string for AI prompt injection."""
    result = check_earnings(symbol)
    if result is None:
        return ""
    days = result["days_until"]
    if days <= avoid_days:
        return (
            f"EARNINGS WARNING: {symbol} reports earnings on "
            f"{result['earnings_date']} ({days} day(s) away). "
            f"Avoid new positions within {avoid_days} days of earnings."
        )
    elif days <= 7:
        return (
            f"Earnings upcoming: {symbol} reports on "
            f"{result['earnings_date']} ({days} days away)."
        )
    return ""
