"""Earnings calendar — avoid trading around earnings dates."""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Dict

import yfinance as yf

logger = logging.getLogger(__name__)

# Cache per symbol, 24 hours
_cache: Dict[str, Dict] = {}
_CACHE_TTL = 24 * 60 * 60


def check_earnings(symbol: str) -> Optional[Dict]:
    """Check if a symbol has upcoming earnings.

    Returns dict with keys: symbol, earnings_date, days_until.
    Returns None if no upcoming earnings, if the lookup fails,
    or if the symbol is a crypto pair (contains '/').

    Cached per symbol for 24 hours.
    """
    # Skip crypto symbols
    if "/" in symbol:
        return None

    # Check cache
    cache_key = f"earnings_{symbol}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        if (time.time() - cached.get("_cached_at", 0)) < _CACHE_TTL:
            return cached.get("result")

    result = None
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar

        if cal is None:
            _cache[cache_key] = {"result": None, "_cached_at": time.time()}
            return None

        # yfinance calendar can be a dict or DataFrame depending on version
        earnings_date = None

        if isinstance(cal, dict):
            # Newer yfinance versions return a dict
            ed = cal.get("Earnings Date")
            if ed is not None:
                if isinstance(ed, list) and len(ed) > 0:
                    earnings_date = ed[0]
                elif hasattr(ed, "date"):
                    earnings_date = ed
        else:
            # Older versions may return a DataFrame
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
            _cache[cache_key] = {"result": None, "_cached_at": time.time()}
            return None

        # Convert to date
        if hasattr(earnings_date, "date"):
            ed_date = earnings_date.date()
        elif isinstance(earnings_date, str):
            ed_date = datetime.strptime(earnings_date[:10], "%Y-%m-%d").date()
        else:
            ed_date = earnings_date

        today = datetime.now().date()
        days_until = (ed_date - today).days

        # Only return if earnings are in the future (or today)
        if days_until < 0:
            _cache[cache_key] = {"result": None, "_cached_at": time.time()}
            return None

        result = {
            "symbol": symbol,
            "earnings_date": str(ed_date),
            "days_until": days_until,
        }

    except Exception as exc:
        logger.debug("Earnings check failed for %s: %s", symbol, exc)
        result = None

    _cache[cache_key] = {"result": result, "_cached_at": time.time()}
    return result


def get_earnings_context(symbol: str, avoid_days: int = 2) -> str:
    """Return earnings context string for AI prompt injection.

    Returns:
    - Warning string if earnings within avoid_days
    - Notice string if earnings within 5 days
    - Empty string otherwise
    """
    if "/" in symbol:
        return ""

    try:
        earnings = check_earnings(symbol)
        if earnings is None:
            return ""

        days = earnings["days_until"]
        date_str = earnings["earnings_date"]

        if days <= avoid_days:
            return (
                f"EARNINGS WARNING: {symbol} reports earnings on {date_str} "
                f"({days} day{'s' if days != 1 else ''} away). "
                f"High uncertainty — consider avoiding."
            )
        elif days <= 5:
            return (
                f"EARNINGS NOTICE: {symbol} reports earnings on {date_str} "
                f"({days} day{'s' if days != 1 else ''} away)."
            )
        else:
            return ""

    except Exception as exc:
        logger.debug("Failed to get earnings context for %s: %s", symbol, exc)
        return ""
