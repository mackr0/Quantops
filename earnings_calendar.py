"""Earnings calendar — avoid trading around earnings dates.

Earnings dates are scheduled events that don't change frequently.
Instead of hitting yfinance on every symbol check (which floods Yahoo
with requests and triggers 401 "Invalid Crumb" errors), we fetch
dates once per day per symbol and store them in the main DB.
"""

import logging
import sqlite3
import time
from datetime import datetime, date, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

_DB_PATH = "quantopsai.db"
_REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours


def _ensure_table():
    """Create the earnings_dates table if it doesn't exist."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS earnings_dates (
                symbol TEXT PRIMARY KEY,
                earnings_date TEXT,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


def _fetch_and_store(symbol: str) -> Optional[str]:
    """Fetch earnings date from yfinance and store in DB.
    Returns the earnings_date string or None."""
    import yfinance as yf

    try:
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
            except Exception:
                pass

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
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO earnings_dates (symbol, earnings_date, fetched_at) "
            "VALUES (?, ?, datetime('now'))",
            (symbol, earnings_date),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_cached(symbol: str) -> tuple:
    """Return (earnings_date_str_or_None, is_fresh_bool)."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT earnings_date, fetched_at FROM earnings_dates WHERE symbol=?",
            (symbol,),
        ).fetchone()
        conn.close()
        if row is None:
            return None, False
        fetched_at = row[1]
        try:
            fetched_dt = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S")
            age = (datetime.utcnow() - fetched_dt).total_seconds()
            is_fresh = age < _REFRESH_INTERVAL
        except Exception:
            is_fresh = False
        return row[0], is_fresh
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
    except Exception:
        pass


def check_earnings(symbol: str) -> Optional[Dict]:
    """Check if a symbol has upcoming earnings.

    Returns dict with keys: symbol, earnings_date, days_until.
    Returns None if no upcoming earnings or lookup fails.

    Uses DB cache — only hits yfinance once per 24 hours per symbol.
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
        ed_date = datetime.strptime(cached_date[:10], "%Y-%m-%d").date()
        today = date.today()
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
