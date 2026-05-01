"""Phase F of OPTIONS_PROGRAM_PLAN.md — earnings/event opportunism.

Currently `avoid_earnings_days` blanket-skips stocks with earnings
inside an N-day window. That leaves predictable IV-crush premium on
the table — earnings are the most reliable IV expansion + crush event
in the equity options market.

Pro options programs OPPORTUNISTICALLY trade earnings:

  Pre-earnings + IV high vs realized
    → SELL premium (iron condor capturing IV crush after the print)

  Pre-earnings + IV unexpectedly cheap (rare)
    → BUY straddle (market is mispricing event risk)

  Post-earnings (1-3 days after)
    → time-stop early — premium decays even faster than usual
       post-event as IV normalizes

This module produces strategy recommendations specific to the
earnings window. The blanket avoid-earnings filter still applies for
EQUITY trades; this is the OPTIONS-side opportunistic layer.

Macro events (FOMC, CPI, NFP) follow the same crush-capture logic
with index ETFs (SPY/QQQ); deferred until macro-event tracker exists.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Earnings window definition. "Pre" = within N days BEFORE earnings.
# "Post" = within M days AFTER. Adjustable but conservative defaults
# match how pro programs structure the play.
EARNINGS_PRE_WINDOW_DAYS = 3   # tight pre-window; IV expansion peaks here
EARNINGS_POST_WINDOW_DAYS = 2  # tight post-window; IV crush is rapid

# IV thresholds for the pre-earnings opportunity. Reuses the same
# rank thresholds as the multi-leg advisor for consistency.
EARNINGS_PREMIUM_RICH_RANK = 75.0
EARNINGS_PREMIUM_CHEAP_RANK = 25.0

# Iron condor parameters for pre-earnings premium capture
EARNINGS_CONDOR_INNER_PCT = 6.0    # ±6% OTM short legs
EARNINGS_CONDOR_OUTER_PCT = 12.0   # ±12% wings


def evaluate_earnings_play(symbol: str,
                                days_until_earnings: Optional[int],
                                iv_rank_pct: Optional[float],
                                current_price: float
                                ) -> Optional[Dict[str, Any]]:
    """Decide whether to recommend an earnings options play.

    Args:
        symbol: underlying.
        days_until_earnings: signed integer; positive = pre-earnings,
            zero = day-of, negative = post-earnings. None = no
            earnings in window.
        iv_rank_pct: 0-100 IV rank. None → skip (no IV signal).
        current_price: spot for strike sizing.

    Returns recommendation dict or None when no play applies.

    Recommendation kinds:
      pre_earnings_iron_condor: IV rich + within pre-window. Sell
                                  premium, capture crush.
      pre_earnings_long_straddle: IV cheap + within pre-window.
                                    Rare; buy mispriced premium.
      post_earnings_close: position open + post-window + IV normalizing.
                              (Not currently triggered here — handled
                               by a future close-after-event task.)
    """
    if days_until_earnings is None:
        return None
    if iv_rank_pct is None or current_price <= 0:
        return None

    # Pre-earnings: 0 to EARNINGS_PRE_WINDOW_DAYS
    if 0 <= days_until_earnings <= EARNINGS_PRE_WINDOW_DAYS:
        if iv_rank_pct >= EARNINGS_PREMIUM_RICH_RANK:
            # Iron condor at expanded outer legs to absorb the post-
            # earnings move while still capturing crush
            put_short = round(current_price * (1 - EARNINGS_CONDOR_INNER_PCT / 100))
            put_long = round(current_price * (1 - EARNINGS_CONDOR_OUTER_PCT / 100))
            call_short = round(current_price * (1 + EARNINGS_CONDOR_INNER_PCT / 100))
            call_long = round(current_price * (1 + EARNINGS_CONDOR_OUTER_PCT / 100))
            return {
                "play": "pre_earnings_iron_condor",
                "symbol": symbol,
                "strategy": "iron_condor",
                "strikes": {
                    "put_long": put_long, "put_short": put_short,
                    "call_short": call_short, "call_long": call_long,
                },
                "days_until_earnings": days_until_earnings,
                "rationale": (
                    f"{symbol} reports in {days_until_earnings}d; IV rank "
                    f"{iv_rank_pct:.0f} → premium expanded for the print. "
                    f"Iron condor at ±{EARNINGS_CONDOR_INNER_PCT:.0f}% "
                    f"shorts captures IV crush after the event. Outer "
                    f"wings at ±{EARNINGS_CONDOR_OUTER_PCT:.0f}% absorb "
                    f"a typical earnings move."
                ),
            }
        if iv_rank_pct <= EARNINGS_PREMIUM_CHEAP_RANK:
            # Cheap pre-earnings IV is rare — opportunistic long
            # straddle when market is under-pricing the event
            strike = round(current_price)
            return {
                "play": "pre_earnings_long_straddle",
                "symbol": symbol,
                "strategy": "long_straddle",
                "strikes": {"strike": strike},
                "days_until_earnings": days_until_earnings,
                "rationale": (
                    f"{symbol} reports in {days_until_earnings}d; IV rank "
                    f"{iv_rank_pct:.0f} → premium UNEXPECTEDLY cheap for "
                    f"the event. Long ATM straddle profits if the actual "
                    f"move exceeds what's currently priced in."
                ),
            }

    # Post-earnings: -EARNINGS_POST_WINDOW_DAYS to 0
    # (Currently no recommendation generated here; future commit
    # adds a close-existing-position recommender for held positions
    # within the post-window.)
    return None


def render_earnings_plays_for_prompt(
    candidates: List[Dict[str, Any]],
    earnings_lookup: Callable[[str], Optional[Dict[str, Any]]],
    iv_rank_lookup: Callable[[str], Optional[float]],
    max_lines: int = 5,
) -> str:
    """Build an EARNINGS PLAYS prompt block.

    For each candidate, check if earnings are within the pre-window.
    When they are AND the IV regime supports a play, emit a
    recommendation line.

    Args:
        candidates: screener candidates.
        earnings_lookup: callable(symbol) → earnings dict from
            earnings_calendar.check_earnings (must contain
            "days_until" key) or None.
        iv_rank_lookup: callable(symbol) → IV rank or None.
        max_lines: cap on rendered recs.

    Empty when no candidate has an actionable earnings play.
    """
    if not candidates:
        return ""
    lines: List[str] = []
    for c in candidates:
        sym = c.get("symbol")
        price = float(c.get("price") or 0)
        if not sym or price <= 0:
            continue
        try:
            earn = earnings_lookup(sym)
        except Exception:
            earn = None
        if not earn:
            continue
        days = earn.get("days_until")
        try:
            iv_rank = iv_rank_lookup(sym)
        except Exception:
            iv_rank = None

        rec = evaluate_earnings_play(sym, days, iv_rank, price)
        if not rec:
            continue
        lines.append(
            f"  - {sym} ({rec['days_until_earnings']}d to earnings): "
            f"{rec['play']} → {rec['strategy']}"
        )
        lines.append(f"      {rec['rationale']}")
        if len(lines) >= max_lines * 2:
            break

    if not lines:
        return ""
    return (
        "EARNINGS PLAYS (opportunistic IV-crush capture / cheap-vol "
        "buys around upcoming earnings):\n"
        + "\n".join(lines)
        + "\n  → Propose MULTILEG_OPEN with the suggested strategy + "
          "strikes if you agree with the play."
    )
