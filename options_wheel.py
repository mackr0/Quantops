"""Phase C3 of OPTIONS_PROGRAM_PLAN.md — wheel strategy automation.

The "wheel" is one of the highest-Sharpe options income strategies:
self-perpetuating cycle on stable names that compounds premium.

State machine per (profile, symbol):

    cash ──CSP_open──> csp_open
                          │
            ┌─────────────┴──────────────┐
            │ otm at expiry              │ assigned (ITM put)
            │                            │
            ▼                            ▼
    cash (back to start)            shares_held
                                          │
                            ┌─────────────┴──────────────┐
                            │ CC_open                    │ no CC yet
                            │                            │
                            ▼                            ▼
                       cc_open                      shares_held
                            │
            ┌───────────────┴────────────────┐
            │ otm at expiry                  │ called away (ITM call)
            │                                │
            ▼                                ▼
       shares_held                          cash

This module derives the current state from the journal + positions and
emits the next-step recommendation. Execution happens via existing
OPTIONS / MULTILEG_OPEN actions — the state machine is observation +
recommendation, not its own executor.

The wheel runs only on profiles that opt in via `wheel_symbols`
(JSON list of underlyings). Default: empty (no wheel).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Wheel states
STATE_CASH = "cash"
STATE_CSP_OPEN = "csp_open"
STATE_SHARES_HELD = "shares_held"
STATE_CC_OPEN = "cc_open"

WHEEL_STATES = {STATE_CASH, STATE_CSP_OPEN,
                STATE_SHARES_HELD, STATE_CC_OPEN}

# Strike % offsets for the recommended next leg of the cycle
WHEEL_CSP_STRIKE_PCT_BELOW = 5.0   # CSP strike ~5% below current
WHEEL_CC_STRIKE_PCT_ABOVE = 5.0    # CC strike ~5% above current
WHEEL_TARGET_DAYS_TO_EXPIRY = 35


def _shares_held_for_symbol(positions: List[Dict[str, Any]],
                                symbol: str) -> int:
    """Total long-stock qty for `symbol` in the position list."""
    for p in positions or []:
        if (p.get("symbol", "").upper() == symbol.upper()
                and p.get("qty") is not None):
            try:
                return int(float(p["qty"]))
            except (TypeError, ValueError):
                return 0
    return 0


def _open_options_for_symbol(db_path: str, symbol: str
                                  ) -> List[Dict[str, Any]]:
    """Return open OPTIONS rows for a given underlying (single-leg only)."""
    from journal import _get_conn
    conn = _get_conn(db_path)
    cur = conn.execute(
        """SELECT id, side, qty, occ_symbol, option_strategy, expiry,
                  strike, decision_price
           FROM trades
           WHERE symbol = ?
             AND signal_type = 'OPTIONS'
             AND status = 'open'
             AND expiry IS NOT NULL""",
        (symbol.upper(),),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def determine_wheel_state(db_path: str,
                              positions: List[Dict[str, Any]],
                              symbol: str) -> Dict[str, Any]:
    """Derive the wheel state for a (profile, symbol) pair.

    Returns:
      {
        "state": cash|csp_open|shares_held|cc_open,
        "shares_held": int,
        "active_options": [...],   # from journal
        "active_csp": dict | None,
        "active_cc": dict | None,
      }
    """
    shares = _shares_held_for_symbol(positions, symbol)
    open_options = _open_options_for_symbol(db_path, symbol)

    csp = next(
        (o for o in open_options
         if (o.get("option_strategy") or "").lower() == "cash_secured_put"),
        None,
    )
    cc = next(
        (o for o in open_options
         if (o.get("option_strategy") or "").lower() == "covered_call"),
        None,
    )

    # State mapping. Note: shares + active CC = cc_open.
    if shares >= 100 and cc:
        state = STATE_CC_OPEN
    elif shares >= 100:
        state = STATE_SHARES_HELD
    elif csp:
        state = STATE_CSP_OPEN
    else:
        state = STATE_CASH

    return {
        "state": state,
        "shares_held": shares,
        "active_options": open_options,
        "active_csp": csp,
        "active_cc": cc,
    }


def _round_strike(price: float) -> float:
    """Standard option strike rounding (matches advisor)."""
    if price < 25:
        return round(price * 2) / 2
    if price < 200:
        return round(price)
    return round(price / 5) * 5


def recommend_next_action(state_summary: Dict[str, Any],
                              symbol: str,
                              current_price: float) -> Optional[Dict[str, Any]]:
    """Given the current wheel state, return the recommended next action.

    cash         → propose CSP (OPTIONS action with cash_secured_put)
    csp_open     → wait (handled by lifecycle / roll-manager)
    shares_held  → propose CC (OPTIONS action with covered_call)
    cc_open      → wait

    Returns dict matching the AI's OPTIONS-action JSON shape so the
    rendered prompt block can paraphrase it directly. None when no
    action is recommended.
    """
    if current_price <= 0:
        return None
    state = state_summary["state"]
    from datetime import date as _d, timedelta as _td
    expiry = _d.today() + _td(days=WHEEL_TARGET_DAYS_TO_EXPIRY)
    # Snap to a Friday
    expiry = expiry + _td(days=(4 - expiry.weekday()) % 7)

    if state == STATE_CASH:
        # Recommend CSP at ~5% below
        strike = _round_strike(
            current_price * (1 - WHEEL_CSP_STRIKE_PCT_BELOW / 100))
        return {
            "step": "open_csp",
            "strategy": "cash_secured_put",
            "symbol": symbol,
            "strike": strike,
            "expiry": expiry.isoformat(),
            "rationale": (
                f"Wheel state=cash. Sell CSP at ${strike:.2f} "
                f"(~{WHEEL_CSP_STRIKE_PCT_BELOW:.0f}% below current "
                f"${current_price:.2f}). If assigned → take shares + "
                f"sell covered call. If expires OTM → keep premium, "
                f"sell another CSP."
            ),
        }

    if state == STATE_SHARES_HELD:
        shares = state_summary["shares_held"]
        contracts = shares // 100
        if contracts < 1:
            return None
        strike = _round_strike(
            current_price * (1 + WHEEL_CC_STRIKE_PCT_ABOVE / 100))
        return {
            "step": "open_cc",
            "strategy": "covered_call",
            "symbol": symbol,
            "strike": strike,
            "expiry": expiry.isoformat(),
            "contracts": contracts,
            "rationale": (
                f"Wheel state=shares_held ({shares} shares). Sell CC at "
                f"${strike:.2f} (~{WHEEL_CC_STRIKE_PCT_ABOVE:.0f}% above "
                f"current ${current_price:.2f}). If called away → take "
                f"the gain, cycle back to cash + new CSP. If expires OTM "
                f"→ keep premium, sell another CC."
            ),
        }

    # csp_open / cc_open: nothing new to propose; lifecycle / roll
    # handle these. Caller can render this state for visibility.
    return None


def render_wheel_block_for_prompt(
    db_path: str,
    positions: List[Dict[str, Any]],
    wheel_symbols: List[str],
    price_lookup,
) -> str:
    """Build the WHEEL STATE prompt block.

    For each opted-in symbol, surface the current wheel state and the
    recommended next action. AI then proposes via OPTIONS action with
    cash_secured_put / covered_call as appropriate.

    Args:
        db_path: profile journal DB.
        positions: live position list (stock + options).
        wheel_symbols: list of underlyings on which the wheel is enabled.
        price_lookup: callable(symbol) -> current price.

    Empty string when wheel_symbols is empty or no actionable state.
    """
    if not wheel_symbols:
        return ""

    lines = []
    for sym in wheel_symbols:
        try:
            state_summary = determine_wheel_state(db_path, positions, sym)
        except Exception as exc:
            logger.debug("wheel state lookup failed for %s: %s", sym, exc)
            continue
        try:
            current_price = price_lookup(sym)
        except Exception:
            current_price = None
        if current_price is None or current_price <= 0:
            continue

        recommendation = recommend_next_action(
            state_summary, sym, current_price,
        )
        # Always show state for visibility; only show recommendation
        # when there's a next-step action.
        line = (
            f"  - {sym}: state={state_summary['state']} "
            f"(shares={state_summary['shares_held']})"
        )
        if recommendation:
            line += (
                f"\n      Next: {recommendation['rationale']}"
            )
        lines.append(line)

    if not lines:
        return ""
    return (
        "WHEEL STRATEGY (auto-cycling premium-income loop):\n"
        + "\n".join(lines)
        + "\n  → Propose OPTIONS action matching the recommended step. "
          "The wheel runs only on opted-in symbols; recommendations "
          "respect current state."
    )
