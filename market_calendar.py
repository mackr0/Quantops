"""Holiday-aware market-calendar gating, backed by Alpaca.

Both schedulers (`is_market_open`/`next_market_open`) and the per-profile
schedule check (`UserContext.is_within_schedule`) used to decide "is the
market open?" from weekday + clock time alone. None of them knew about
market holidays (Memorial Day, Thanksgiving, …) or half-days, so the
scheduler ran full scan/trade cycles on closed days and submitted orders
that the broker queued and filled at the *next* session's open — a thesis
priced on Friday's close executing after a 3-day gap.

This module makes Alpaca the source of truth:

  * `is_market_open(now)`   — regular cash session open right now
                              (`GET /clock`'s `is_open`; half-day aware).
  * `is_trading_day(now)`   — is today a trading day at all
                              (`GET /calendar`; used to holiday-guard
                              extended-hours / custom schedules).
  * `is_market_holiday(now)`— a *weekday* the market is closed (so
                              weekend-inclusive custom schedules keep
                              working while still skipping holidays).
  * `next_market_open(now)` — next regular open (clock's `next_open`).

Resilience: every Alpaca call is cached (the scheduler loop ticks every
~30s and the dashboard renders far more often) and wrapped so a network
blip never breaks the trading loop. When Alpaca is unreachable — or when
called with a non-live datetime, e.g. in tests — we fall back to a
weekday + hardcoded-NYSE-holiday + time-window heuristic. The fallback
is holiday-aware but not half-day aware; the live clock handles those.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

_REGULAR_OPEN = (9, 30)    # 9:30 AM ET
_REGULAR_CLOSE = (16, 0)   # 4:00 PM ET

# A caller's `now` is treated as "live" (safe to consult Alpaca, which
# only describes the present) when it's within this many seconds of the
# real wall clock. Historical/arbitrary datetimes use the deterministic
# fallback instead.
_LIVE_TOLERANCE_SEC = 120

# Full-day NYSE closures — FALLBACK ONLY. Alpaca's clock/calendar is
# authoritative when reachable; this keeps us holiday-aware during an
# outage. Half-days are intentionally omitted (the live clock handles
# the early close). Extend each year.
_HARDCODED_HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}

# Clock is a near-instant snapshot; cache briefly so a 30s scheduler loop
# (and far busier dashboard) don't hammer the endpoint. A few seconds of
# staleness at the open/close boundary is harmless.
_CLOCK_TTL = 30.0
_clock_lock = threading.Lock()
_clock_cache = {"ts": 0.0, "val": None}

# Trading-day-ness only changes at the date boundary; cache per ET date.
_CAL_TTL = 3600.0
_cal_lock = threading.Lock()
_cal_cache: dict = {}  # iso-date str -> (epoch, is_trading_day bool)


# ── internals ────────────────────────────────────────────────────────

def _to_et(now=None):
    """Normalize *now* to a tz-aware ET datetime."""
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def _is_live(now_et):
    """True if *now_et* is close enough to the real clock that Alpaca's
    present-tense endpoints describe it."""
    return abs((datetime.now(ET) - now_et).total_seconds()) <= _LIVE_TOLERANCE_SEC


def _api():
    """Best-effort module-level Alpaca client (clock/calendar are
    account-independent). Returns None when credentials are missing or
    the import fails — callers fall back to the heuristic."""
    try:
        import client
        return client.get_api()
    except Exception as exc:
        logger.debug("market_calendar: no Alpaca client (%s); using fallback", exc)
        return None


def _get_clock():
    """Cached Alpaca clock object, or None if unavailable."""
    now = time.time()
    with _clock_lock:
        if (now - _clock_cache["ts"]) < _CLOCK_TTL:
            return _clock_cache["val"]
    api = _api()
    clock = None
    if api is not None:
        try:
            clock = api.get_clock()
        except Exception as exc:
            logger.warning("market_calendar: get_clock failed (%s); using fallback", exc)
            clock = None
    with _clock_lock:
        _clock_cache["ts"] = now
        _clock_cache["val"] = clock
    return clock


def _trading_day_live():
    """Per-ET-date trading-day flag from Alpaca's calendar, or None."""
    key = datetime.now(ET).date().isoformat()
    now = time.time()
    with _cal_lock:
        ent = _cal_cache.get(key)
        if ent and (now - ent[0]) < _CAL_TTL:
            return ent[1]
    api = _api()
    if api is None:
        return None
    try:
        cal = api.get_calendar(start=key, end=key)
        is_td = bool(cal)  # non-empty list => today is a trading day
    except Exception as exc:
        logger.warning("market_calendar: get_calendar failed (%s); using fallback", exc)
        return None
    with _cal_lock:
        _cal_cache[key] = (now, is_td)
    return is_td


def _is_nontrading_day(d: date) -> bool:
    return d.weekday() >= 5 or d in _HARDCODED_HOLIDAYS


def _regular_session_open_fallback(now_et) -> bool:
    if _is_nontrading_day(now_et.date()):
        return False
    o = now_et.replace(hour=_REGULAR_OPEN[0], minute=_REGULAR_OPEN[1],
                       second=0, microsecond=0)
    c = now_et.replace(hour=_REGULAR_CLOSE[0], minute=_REGULAR_CLOSE[1],
                       second=0, microsecond=0)
    return o <= now_et < c


# ── public API ───────────────────────────────────────────────────────

def is_market_open(now=None) -> bool:
    """True if the regular US cash session is open right now.

    Uses Alpaca's clock (holiday- and half-day-aware) for live calls;
    falls back to weekday + hardcoded-holiday + 9:30-16:00 ET otherwise.
    """
    now_et = _to_et(now)
    if _is_live(now_et):
        clock = _get_clock()
        if clock is not None:
            try:
                return bool(clock.is_open)
            except Exception as exc:
                logger.debug("clock.is_open unreadable (%s); using fallback", exc)
    return _regular_session_open_fallback(now_et)


def is_trading_day(now=None) -> bool:
    """True if *now*'s ET date is a trading day (not a weekend/holiday)."""
    now_et = _to_et(now)
    if _is_live(now_et):
        live = _trading_day_live()
        if live is not None:
            return live
    return not _is_nontrading_day(now_et.date())


def is_market_holiday(now=None) -> bool:
    """True if *now* is a weekday the market is closed (a holiday), as
    distinct from a weekend. Lets weekend-inclusive custom schedules run
    while still skipping holidays."""
    now_et = _to_et(now)
    if now_et.weekday() >= 5:
        return False
    return not is_trading_day(now_et)


def next_market_open(now=None):
    """Next regular-session open as a tz-aware ET datetime.

    Prefers Alpaca's `clock.next_open` (knows every holiday); falls back
    to walking forward over weekends + hardcoded holidays.
    """
    now_et = _to_et(now)
    if _is_live(now_et):
        clock = _get_clock()
        if clock is not None:
            try:
                return clock.next_open.astimezone(ET)
            except Exception as exc:
                logger.debug("clock.next_open unreadable (%s); using fallback", exc)
    candidate = now_et.replace(hour=_REGULAR_OPEN[0], minute=_REGULAR_OPEN[1],
                               second=0, microsecond=0)
    if now_et >= candidate or _is_nontrading_day(now_et.date()):
        candidate += timedelta(days=1)
    while _is_nontrading_day(candidate.date()):
        candidate += timedelta(days=1)
    return candidate
