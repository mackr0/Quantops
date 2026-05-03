"""OPEN_ITEMS #9 — Macro event tracker (FOMC, CPI, NFP).

Phase F2 of OPTIONS_PROGRAM_PLAN. Same IV-crush capture template as F1
earnings plays, but on index ETFs (SPY/QQQ) around scheduled macro
releases. Pro options programs systematically trade these:

  Pre-event + IV rich
    → SELL premium on SPY/QQQ (iron condor capturing post-print crush)

  Post-event (1-2 days after)
    → time-stop early — IV normalizes faster than usual

Sources:
  - **FOMC**: 8 scheduled meetings/year. Federal Reserve publishes the
    calendar at federalreserve.gov. We hardcode known dates with a
    monthly refresh from the published list.
  - **CPI**: ~13th of each month at 08:30 ET. Bureau of Labor
    Statistics releases on a fixed schedule.
  - **NFP**: First Friday of each month at 08:30 ET. Always.

Approach: hand-curated calendar in `MACRO_EVENT_CALENDAR` that an
operator can extend. The CALENDAR is intentionally minimal — these
dates change rarely (FOMC is announced annually) and a stale entry
just means we miss a play, not that we trade wrong.

Public API:
  - get_upcoming_macro_event(today=None) → next event or None
  - days_until_next_event(today=None) → int or None
  - render_macro_event_for_prompt() → string for AI prompt
  - evaluate_macro_play(iv_rank_pct, days_until) → recommendation or None
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MacroEvent:
    """One scheduled macro event."""
    date: str          # ISO YYYY-MM-DD (always release date in ET)
    event_type: str    # 'FOMC' | 'CPI' | 'NFP' | 'GDP' | 'PPI'
    description: str
    severity: str = "medium"   # 'low' | 'medium' | 'high'


# Schedule for the next ~12 months. Operators extend this list as the
# Fed / BLS publish new calendars. Dates known as of 2026-05-03.
#
# FOMC: 8 meetings / year. Fed publishes the calendar a year out.
# CPI: published on the ~13th of each month at 08:30 ET (varies).
# NFP: first Friday of each month at 08:30 ET.
MACRO_EVENT_CALENDAR: List[MacroEvent] = [
    # FOMC 2026 (full schedule on federalreserve.gov)
    MacroEvent("2026-05-07", "FOMC", "FOMC rate decision",   "high"),
    MacroEvent("2026-06-18", "FOMC", "FOMC rate decision",   "high"),
    MacroEvent("2026-07-30", "FOMC", "FOMC rate decision",   "high"),
    MacroEvent("2026-09-17", "FOMC", "FOMC rate decision",   "high"),
    MacroEvent("2026-11-05", "FOMC", "FOMC rate decision",   "high"),
    MacroEvent("2026-12-17", "FOMC", "FOMC rate decision",   "high"),

    # CPI release ~13th of each month at 08:30 ET (BLS schedule)
    MacroEvent("2026-05-13", "CPI", "CPI release", "high"),
    MacroEvent("2026-06-11", "CPI", "CPI release", "high"),
    MacroEvent("2026-07-15", "CPI", "CPI release", "high"),
    MacroEvent("2026-08-12", "CPI", "CPI release", "high"),
    MacroEvent("2026-09-11", "CPI", "CPI release", "high"),
    MacroEvent("2026-10-15", "CPI", "CPI release", "high"),
    MacroEvent("2026-11-12", "CPI", "CPI release", "high"),
    MacroEvent("2026-12-10", "CPI", "CPI release", "high"),

    # NFP — first Friday of each month
    MacroEvent("2026-05-01", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-06-05", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-07-03", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-08-07", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-09-04", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-10-02", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-11-06", "NFP", "Non-Farm Payrolls",     "high"),
    MacroEvent("2026-12-04", "NFP", "Non-Farm Payrolls",     "high"),
]

# IV thresholds match earnings plays for consistency
MACRO_PREMIUM_RICH_RANK = 75.0
MACRO_PREMIUM_CHEAP_RANK = 25.0

MACRO_PRE_WINDOW_DAYS = 5      # IV expansion typically starts ~5d out
MACRO_POST_WINDOW_DAYS = 2     # IV crushes within 1-2d post-event

# Iron condor parameters for pre-event premium capture on SPY
MACRO_CONDOR_INNER_PCT = 1.5   # ±1.5% OTM short legs (index moves smaller)
MACRO_CONDOR_OUTER_PCT = 3.5   # ±3.5% wings


def get_upcoming_macro_event(today: Optional[_date] = None) -> Optional[MacroEvent]:
    """Return the nearest future MacroEvent (or today's), or None
    if none scheduled."""
    if today is None:
        today = datetime.now().date()
    upcoming = []
    for ev in MACRO_EVENT_CALENDAR:
        try:
            ev_date = _date.fromisoformat(ev.date)
        except ValueError:
            continue
        if ev_date >= today:
            upcoming.append((ev_date, ev))
    if not upcoming:
        return None
    upcoming.sort(key=lambda t: t[0])
    return upcoming[0][1]


def days_until_next_event(today: Optional[_date] = None) -> Optional[int]:
    """Calendar days from today to the next scheduled macro event."""
    if today is None:
        today = datetime.now().date()
    ev = get_upcoming_macro_event(today)
    if ev is None:
        return None
    try:
        return (_date.fromisoformat(ev.date) - today).days
    except ValueError:
        return None


def evaluate_macro_play(
    *,
    iv_rank_pct: Optional[float],
    days_until_event: Optional[int],
    event: Optional[MacroEvent],
    spy_price: float,
) -> Optional[Dict[str, Any]]:
    """Same template as evaluate_earnings_play but for macro events
    on the SPY index. Returns a recommendation dict or None when no
    actionable opportunity.

    - In pre-window with rich IV  → SELL iron condor on SPY
    - In pre-window with cheap IV → BUY straddle on SPY
    - Post-event                  → time-stop existing macro plays
    """
    if event is None or days_until_event is None or iv_rank_pct is None:
        return None
    if spy_price <= 0:
        return None

    # Pre-event window
    if 0 < days_until_event <= MACRO_PRE_WINDOW_DAYS:
        if iv_rank_pct >= MACRO_PREMIUM_RICH_RANK:
            inner = MACRO_CONDOR_INNER_PCT / 100.0
            outer = MACRO_CONDOR_OUTER_PCT / 100.0
            put_long = round(spy_price * (1 - outer))
            put_short = round(spy_price * (1 - inner))
            call_short = round(spy_price * (1 + inner))
            call_long = round(spy_price * (1 + outer))
            return {
                "play": "sell_macro_condor",
                "rationale": (
                    f"{event.event_type} {event.date} in {days_until_event}d, "
                    f"SPY IV rank {iv_rank_pct:.0f} (rich). Sell iron condor "
                    f"to capture post-event IV crush."
                ),
                "underlying": "SPY",
                "event_type": event.event_type,
                "event_date": event.date,
                "days_until_event": days_until_event,
                "iv_rank_pct": iv_rank_pct,
                "structure": {
                    "type": "iron_condor",
                    "put_long_strike":  put_long,
                    "put_short_strike": put_short,
                    "call_short_strike": call_short,
                    "call_long_strike":  call_long,
                    "spot_at_recommendation": spy_price,
                },
            }
        if iv_rank_pct <= MACRO_PREMIUM_CHEAP_RANK:
            return {
                "play": "buy_macro_straddle",
                "rationale": (
                    f"{event.event_type} {event.date} in {days_until_event}d, "
                    f"SPY IV rank {iv_rank_pct:.0f} (cheap). Market is "
                    f"underpricing event risk — long straddle."
                ),
                "underlying": "SPY",
                "event_type": event.event_type,
                "event_date": event.date,
                "days_until_event": days_until_event,
                "iv_rank_pct": iv_rank_pct,
                "structure": {
                    "type": "long_straddle",
                    "strike": round(spy_price),
                    "spot_at_recommendation": spy_price,
                },
            }

    # Post-event window — recommend early close on existing macro plays
    if -MACRO_POST_WINDOW_DAYS <= days_until_event < 0:
        return {
            "play": "time_stop_macro",
            "rationale": (
                f"{event.event_type} was {-days_until_event}d ago. "
                f"IV crush on SPY largely complete; close any open "
                f"macro condors / straddles near max profit."
            ),
            "event_type": event.event_type,
            "event_date": event.date,
            "days_since_event": -days_until_event,
        }

    return None


def render_macro_event_for_prompt() -> str:
    """One-line block for AI prompt MARKET CONTEXT."""
    ev = get_upcoming_macro_event()
    if ev is None:
        return ""
    days = days_until_next_event() or 0
    if days == 0:
        timing = "TODAY"
    elif days == 1:
        timing = "tomorrow"
    else:
        timing = f"in {days}d"
    return (
        f"Next macro event: {ev.event_type} {timing} "
        f"({ev.date}, {ev.severity} severity)"
    )
