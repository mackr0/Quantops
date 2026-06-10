"""Phase B1 of OPTIONS_PROGRAM_PLAN.md — multi-leg strategy primitives.

Single-leg primitives (long_call/long_put/covered_call/protective_put/
cash_secured_put) live in options_trader.py and stay untouched. This
module adds the multi-leg primitives that turn us from a single-leg
dabbler into a real options program:

  Phase B1 (this commit): 4 vertical spreads (the simplest 2-leg
  defined-risk constructions). Each is the foundation for one of
  the four directional / credit-or-debit quadrants:
    - Bull call spread: bullish, debit, defined-risk
    - Bear put spread:  bearish, debit, defined-risk
    - Bull put spread:  bullish, credit, defined-risk
    - Bear call spread: bearish, credit, defined-risk

  Phase B (rest, separate commits): condor, butterfly, straddle,
  strangle, calendar, diagonal.

Each builder returns an `OptionStrategy` dataclass with:
  - legs: list of OptionLeg (one per leg, with full OCC details)
  - max_loss_per_contract / max_gain_per_contract: per-spread P&L
    bounds in DOLLARS (not points). Computed from premiums when
    the caller supplies quotes; otherwise the dataclass stores the
    spread WIDTH so the executor can finalize after fill.
  - breakevens: stock prices at expiry where the spread P&L = 0.
  - net_premium_per_contract: signed dollar amount per spread
    (positive = debit paid, negative = credit collected).
  - thesis: human-readable summary (used in AI prompt + journal).

Sizing: callers pass `qty` (number of spreads). The builder produces
the legs with that contract count on each leg. The execution layer
(B2, separate file) submits all legs as a single combo order so the
spread fills atomically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from options_trader import format_occ_symbol


# ---------------------------------------------------------------------------
# Data model — leg + strategy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionLeg:
    """One leg of a multi-leg option strategy.

    occ_symbol identifies the contract uniquely. side is broker-style
    ('buy' = long, 'sell' = short). qty is contracts; positive integer.
    """
    occ_symbol: str
    underlying: str
    expiry: str        # ISO date string
    strike: float
    right: str         # 'C' or 'P'
    side: str          # 'buy' or 'sell'
    qty: int

    def signed_qty(self) -> int:
        """+qty for long, -qty for short. Used by the Greeks aggregator."""
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class OptionStrategy:
    """A multi-leg options strategy ready to execute.

    `name` identifies the strategy type ('bull_call_spread', etc.).
    `legs` is the ordered list of OptionLeg, where leg ordering follows
    the convention the executor expects (long leg before short leg for
    debit spreads; short leg before long leg for credit spreads).

    Premium/P&L fields are pre-fill estimates when premiums are
    supplied to the builder; otherwise None (executor finalizes
    post-fill). `breakeven_at_expiry` is in stock-price terms.
    """
    name: str
    underlying: str
    expiry: str
    legs: List[OptionLeg]
    qty: int  # number of spreads
    spread_width_points: float  # |upper_strike - lower_strike|
    is_credit: bool  # True for credit spreads, False for debit
    thesis: str

    # Pre-fill estimates (None until quotes provided)
    net_premium_per_contract: Optional[float] = None  # signed: + debit, - credit
    max_loss_per_contract: Optional[float] = None     # in dollars (per spread)
    max_gain_per_contract: Optional[float] = None     # in dollars (per spread)
    breakeven_at_expiry: Optional[float] = None       # stock price

    # Greeks summary (None until aggregator computes)
    net_delta_per_contract: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "qty": self.qty,
            "spread_width_points": self.spread_width_points,
            "is_credit": self.is_credit,
            "thesis": self.thesis,
            "net_premium_per_contract": self.net_premium_per_contract,
            "max_loss_per_contract": self.max_loss_per_contract,
            "max_gain_per_contract": self.max_gain_per_contract,
            "breakeven_at_expiry": self.breakeven_at_expiry,
            "legs": [
                {
                    "occ_symbol": leg.occ_symbol, "underlying": leg.underlying,
                    "expiry": leg.expiry, "strike": leg.strike,
                    "right": leg.right, "side": leg.side, "qty": leg.qty,
                }
                for leg in self.legs
            ],
        }


# ---------------------------------------------------------------------------
# Vertical spread builders
# ---------------------------------------------------------------------------

def _validate_strikes(lower: float, upper: float) -> None:
    if lower <= 0 or upper <= 0:
        raise ValueError("strikes must be positive")
    if upper <= lower:
        raise ValueError(
            f"upper strike ({upper}) must be > lower strike ({lower})"
        )


def _vertical_pl_bounds(width_points: float, net_premium: float,
                          is_credit: bool) -> Dict[str, float]:
    """Compute max_loss/max_gain in DOLLARS per spread.

    `net_premium` is per-share (Black-Scholes / quote convention) —
    the dollar amount per contract is `net_premium * 100`.

    For DEBIT verticals (long lower, short upper for calls;
    long upper, short lower for puts):
      max_loss  = net_premium_paid * 100
      max_gain  = (width_points - net_premium_paid) * 100

    For CREDIT verticals (short higher-IV, long lower-IV):
      max_gain  = net_premium_collected * 100
      max_loss  = (width_points - net_premium_collected) * 100

    All in dollars per spread. Multiply by qty for total exposure.
    """
    premium_dollars = abs(net_premium) * 100
    width_dollars = width_points * 100
    if is_credit:
        return {
            "max_loss_per_contract": width_dollars - premium_dollars,
            "max_gain_per_contract": premium_dollars,
        }
    return {
        "max_loss_per_contract": premium_dollars,
        "max_gain_per_contract": width_dollars - premium_dollars,
    }


def build_bull_call_spread(
    underlying: str,
    expiry: date,
    lower_strike: float,
    upper_strike: float,
    qty: int = 1,
    long_premium: Optional[float] = None,
    short_premium: Optional[float] = None,
) -> OptionStrategy:
    """Bull call spread: long lower-strike call + short upper-strike call.

    Bullish thesis with defined risk. Net debit (you pay premium).

    Max loss  = net_debit * 100 (per spread)
    Max gain  = (upper - lower - net_debit) * 100 (per spread)
    Breakeven = lower_strike + net_debit  (at expiry)

    `long_premium` / `short_premium` are per-share quotes (e.g. mid
    price from chain) used to compute the financial bounds. When
    None, those fields are left None and the executor finalizes
    after fill.
    """
    _validate_strikes(lower_strike, upper_strike)
    if qty <= 0:
        raise ValueError("qty must be positive")

    long_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, lower_strike, "C"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=lower_strike, right="C", side="buy", qty=qty,
    )
    short_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, upper_strike, "C"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=upper_strike, right="C", side="sell", qty=qty,
    )

    spec = OptionStrategy(
        name="bull_call_spread",
        underlying=underlying.upper(),
        expiry=expiry.isoformat(),
        legs=[long_leg, short_leg],
        qty=qty,
        spread_width_points=upper_strike - lower_strike,
        is_credit=False,
        thesis=(
            f"Bullish on {underlying.upper()} between {lower_strike:.2f} "
            f"and {upper_strike:.2f}. Defined-risk bet that pays off if "
            f"price rises through the spread by expiry."
        ),
    )

    if long_premium is not None and short_premium is not None:
        net_debit = long_premium - short_premium
        spec.net_premium_per_contract = net_debit
        bounds = _vertical_pl_bounds(
            spec.spread_width_points, net_debit, is_credit=False)
        spec.max_loss_per_contract = bounds["max_loss_per_contract"]
        spec.max_gain_per_contract = bounds["max_gain_per_contract"]
        spec.breakeven_at_expiry = lower_strike + net_debit

    return spec


def build_bear_put_spread(
    underlying: str,
    expiry: date,
    lower_strike: float,
    upper_strike: float,
    qty: int = 1,
    long_premium: Optional[float] = None,
    short_premium: Optional[float] = None,
) -> OptionStrategy:
    """Bear put spread: long upper-strike put + short lower-strike put.

    Bearish thesis with defined risk. Net debit.

    Max loss  = net_debit * 100
    Max gain  = (upper - lower - net_debit) * 100
    Breakeven = upper_strike - net_debit
    """
    _validate_strikes(lower_strike, upper_strike)
    if qty <= 0:
        raise ValueError("qty must be positive")

    long_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, upper_strike, "P"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=upper_strike, right="P", side="buy", qty=qty,
    )
    short_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, lower_strike, "P"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=lower_strike, right="P", side="sell", qty=qty,
    )

    spec = OptionStrategy(
        name="bear_put_spread",
        underlying=underlying.upper(),
        expiry=expiry.isoformat(),
        legs=[long_leg, short_leg],
        qty=qty,
        spread_width_points=upper_strike - lower_strike,
        is_credit=False,
        thesis=(
            f"Bearish on {underlying.upper()} between {lower_strike:.2f} "
            f"and {upper_strike:.2f}. Defined-risk bet that pays off if "
            f"price falls through the spread by expiry."
        ),
    )

    if long_premium is not None and short_premium is not None:
        net_debit = long_premium - short_premium
        spec.net_premium_per_contract = net_debit
        bounds = _vertical_pl_bounds(
            spec.spread_width_points, net_debit, is_credit=False)
        spec.max_loss_per_contract = bounds["max_loss_per_contract"]
        spec.max_gain_per_contract = bounds["max_gain_per_contract"]
        spec.breakeven_at_expiry = upper_strike - net_debit

    return spec


def build_bull_put_spread(
    underlying: str,
    expiry: date,
    lower_strike: float,
    upper_strike: float,
    qty: int = 1,
    short_premium: Optional[float] = None,
    long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Bull put spread: short higher-strike put + long lower-strike put.

    Bullish thesis (sell premium when you think price stays above
    short strike). Net credit; defined-risk income strategy.

    Max gain  = net_credit * 100
    Max loss  = (upper - lower - net_credit) * 100
    Breakeven = upper_strike - net_credit  (at expiry)

    Args take `short_premium` then `long_premium` ordering matching
    leg dominance (the short leg is the income leg).
    """
    _validate_strikes(lower_strike, upper_strike)
    if qty <= 0:
        raise ValueError("qty must be positive")

    # Convention: short leg listed FIRST in legs[] for credit spreads
    short_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, upper_strike, "P"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=upper_strike, right="P", side="sell", qty=qty,
    )
    long_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, lower_strike, "P"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=lower_strike, right="P", side="buy", qty=qty,
    )

    spec = OptionStrategy(
        name="bull_put_spread",
        underlying=underlying.upper(),
        expiry=expiry.isoformat(),
        legs=[short_leg, long_leg],
        qty=qty,
        spread_width_points=upper_strike - lower_strike,
        is_credit=True,
        thesis=(
            f"Bullish on {underlying.upper()} (price stays above "
            f"{upper_strike:.2f}). Collect premium; max gain is the "
            f"credit, max loss is width minus credit. Time-decay "
            f"works in your favor."
        ),
    )

    if short_premium is not None and long_premium is not None:
        net_credit = short_premium - long_premium  # positive
        spec.net_premium_per_contract = -net_credit  # signed: credits negative
        bounds = _vertical_pl_bounds(
            spec.spread_width_points, net_credit, is_credit=True)
        spec.max_loss_per_contract = bounds["max_loss_per_contract"]
        spec.max_gain_per_contract = bounds["max_gain_per_contract"]
        spec.breakeven_at_expiry = upper_strike - net_credit

    return spec


def build_bear_call_spread(
    underlying: str,
    expiry: date,
    lower_strike: float,
    upper_strike: float,
    qty: int = 1,
    short_premium: Optional[float] = None,
    long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Bear call spread: short lower-strike call + long upper-strike call.

    Bearish thesis (sell premium when you think price stays below
    short strike). Net credit; defined-risk income strategy.

    Max gain  = net_credit * 100
    Max loss  = (upper - lower - net_credit) * 100
    Breakeven = lower_strike + net_credit  (at expiry)
    """
    _validate_strikes(lower_strike, upper_strike)
    if qty <= 0:
        raise ValueError("qty must be positive")

    short_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, lower_strike, "C"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=lower_strike, right="C", side="sell", qty=qty,
    )
    long_leg = OptionLeg(
        occ_symbol=format_occ_symbol(underlying, expiry, upper_strike, "C"),
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        strike=upper_strike, right="C", side="buy", qty=qty,
    )

    spec = OptionStrategy(
        name="bear_call_spread",
        underlying=underlying.upper(),
        expiry=expiry.isoformat(),
        legs=[short_leg, long_leg],
        qty=qty,
        spread_width_points=upper_strike - lower_strike,
        is_credit=True,
        thesis=(
            f"Bearish on {underlying.upper()} (price stays below "
            f"{lower_strike:.2f}). Collect premium; max gain is the "
            f"credit, max loss is width minus credit. Time-decay "
            f"works in your favor."
        ),
    )

    if short_premium is not None and long_premium is not None:
        net_credit = short_premium - long_premium
        spec.net_premium_per_contract = -net_credit
        bounds = _vertical_pl_bounds(
            spec.spread_width_points, net_credit, is_credit=True)
        spec.max_loss_per_contract = bounds["max_loss_per_contract"]
        spec.max_gain_per_contract = bounds["max_gain_per_contract"]
        spec.breakeven_at_expiry = lower_strike + net_credit

    return spec


# Registry — used by the executor and advisor to look up builders by name.
VERTICAL_SPREAD_BUILDERS = {
    "bull_call_spread": build_bull_call_spread,
    "bear_put_spread": build_bear_put_spread,
    "bull_put_spread": build_bull_put_spread,
    "bear_call_spread": build_bear_call_spread,
}


# ---------------------------------------------------------------------------
# Phase B2 — atomic multi-leg execution
# ---------------------------------------------------------------------------

import logging  # noqa: E402

logger = logging.getLogger(__name__)


# position_intent map shared by combo legs, sequential submits, and
# the rollback path. Alpaca async-cancels short option opens that
# arrive without intent (silently, sometimes labeled as "wash
# trade").
_INTENT_OPEN = {"buy": "buy_to_open", "sell": "sell_to_open"}
_INTENT_CLOSE = {"buy": "buy_to_close", "sell": "sell_to_close"}


class _RawOrderResult:
    """Lightweight stand-in for the SDK's Order object so existing
    callers that read `order.id` continue to work."""
    def __init__(self, payload):
        self.id = payload.get("id")
        self.status = payload.get("status")
        self._payload = payload

    def __getattr__(self, name):
        return self._payload.get(name)


def _combo_submit_with_retry(api, payload, max_retries=2,
                             backoff_seconds=(0.5, 1.5)):
    """Wrap `_submit_alpaca_order_raw` with 5xx retry logic for the
    combo MLEG path.

    Alpaca's paper option-combo endpoint returns transient
    `{"code":50010000,"message":"internal server error occurred"}`
    on ~30% of submissions in observed prod traffic (2026-05-08
    sample: CWAN/PCG/BKLN failed; CPRT/FITB/ACHR/RIOT succeeded —
    same code path, same minute). Without retry, every 500 falls
    through to the sequential path, which is non-atomic and can
    leave the AI with naked single-leg positions when one leg
    later expires unfilled.

    Retry policy (precise about what counts as transient — vague
    "retry on any exception" would mask permanent failures like
    "MLEG not supported on this account" and waste real time on
    every multileg):

    - RuntimeError matching `"Alpaca order rejected (5NN)"` →
      retry. The `_submit_alpaca_order_raw` helper raises this exact
      shape on every HTTP error, so the regex is reliable.
    - `requests.exceptions.{ConnectionError, Timeout,
      ChunkedEncodingError}` → retry. Real network transients.
    - 4xx HTTP → re-raise immediately. Bad symbol / missing field
      / permission denied — retry can't help.
    - Anything else (bare `Exception`, `KeyError`, etc.) → re-raise
      immediately. Could be a code bug or permanent account-config
      issue; failing fast lets the caller's outer try/except log
      it and fall through to the sequential path right away.

    Final failure re-raises so the caller's existing fall-through
    behavior is preserved exactly.
    """
    import re
    import time
    import requests as _requests

    transient_network_excs = (
        _requests.exceptions.ConnectionError,
        _requests.exceptions.Timeout,
        _requests.exceptions.ChunkedEncodingError,
    )

    def _is_transient(exc):
        if isinstance(exc, transient_network_excs):
            return True
        if isinstance(exc, RuntimeError):
            m = re.match(r"Alpaca order rejected \((\d+)\)", str(exc))
            return bool(m and 500 <= int(m.group(1)) < 600)
        return False

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return _submit_alpaca_order_raw(api, payload)
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt >= max_retries:
                raise
            delay = backoff_seconds[
                min(attempt, len(backoff_seconds) - 1)
            ]
            logger.info(
                "Combo submit attempt %d/%d failed with %s — "
                "retrying in %.1fs",
                attempt + 1, max_retries + 1, str(exc)[:120], delay,
            )
            time.sleep(delay)
    # Unreachable, but defensive
    if last_exc:
        raise last_exc
    raise RuntimeError("Combo submit retry loop exited without result")


def _submit_alpaca_order_raw(api, payload):
    """POST directly to Alpaca's `/v2/orders` endpoint, bypassing
    the alpaca-trade-api SDK's narrow `submit_order` signature.

    Required because the SDK doesn't expose `position_intent` or
    `legs` (multileg combo orders) as kwargs, but Alpaca's REST
    API supports both. Reads auth from the SDK's `_key_id` /
    `_secret_key` / `_base_url` so behavior matches the SDK call
    site (paper vs live, per-profile credentials).

    Returns a `_RawOrderResult` with `.id`, `.status`, and any
    other field accessible via attribute lookup.

    Raises on HTTP error so the caller's try/except sees a real
    exception (matches SDK behavior).
    """
    import requests
    base = getattr(api, "_base_url", None) or "https://paper-api.alpaca.markets"
    headers = {
        "APCA-API-KEY-ID": getattr(api, "_key_id", "") or "",
        "APCA-API-SECRET-KEY": getattr(api, "_secret_key", "") or "",
        "Content-Type": "application/json",
    }
    # Alpaca expects qty as string and certain fields as nested dicts;
    # serialize defensively.
    serializable = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (int, float)) and k != "legs":
            serializable[k] = str(v)
        else:
            serializable[k] = v
    r = requests.post(
        f"{base}/v2/orders", headers=headers, json=serializable, timeout=15,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"Alpaca order rejected ({r.status_code}): {r.text[:300]}"
        )
    return _RawOrderResult(r.json())


def _alpaca_leg_dict(leg: OptionLeg, ratio: int = 1) -> Dict[str, Any]:
    """Convert an OptionLeg to Alpaca's `option_legs` array shape.

    Alpaca combo orders take legs as:
      {"symbol": OCC, "side": "buy"|"sell", "ratio_qty": int,
       "position_intent": "buy_to_open" / "sell_to_open" / etc.}

    `ratio` is the leg count multiplier. For verticals every leg has
    ratio 1 (1 long + 1 short = 1 spread). For ratio spreads the
    builder would set ratio differently.
    """
    return {
        "symbol": leg.occ_symbol,
        "side": leg.side,
        "ratio_qty": int(ratio),
        "position_intent": _INTENT_OPEN.get(leg.side, "buy_to_open"),
    }


def execute_multileg_strategy(
    api,
    strategy: OptionStrategy,
    ctx,
    log: bool = True,
    use_combo: bool = True,
    limit_price: Optional[float] = None,
    ai_confidence: Optional[int] = None,
    ai_reasoning: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a multi-leg option strategy atomically.

    Two paths:
      1. Combo order (default): submit all legs as a single MLEG order
         via Alpaca's `option_legs` parameter. Atomic — the whole
         spread fills together at the net price or not at all.
      2. Sequential fallback (use_combo=False, or combo unsupported):
         submit each leg in sequence. If leg N fails after legs 1..N-1
         have submitted, attempt rollback by closing the filled legs.
         Logs loud on partial-fill so the operator can reconcile.

    Args:
        api: Alpaca REST client.
        strategy: built OptionStrategy (from a vertical builder).
        ctx: UserContext (db_path used for journal logging).
        log: write trade rows to journal.
        use_combo: prefer combo-order path. False forces sequential.
        limit_price: optional NET limit price (signed: + debit, - credit).
        ai_confidence: AI's per-trade confidence (0-100). Stamped on
            every leg's journal row so the trades-table AI Conf column
            is populated for multileg trades the same way it is for
            single-leg options + stock trades.
        ai_reasoning: AI's per-trade rationale. Stamped on every leg's
            ai_reasoning column so the expanded trade detail shows the
            AI's actual reasoning (not the spread's boilerplate thesis).

    Returns:
        {
            "action": "MULTILEG_OPEN" | "ERROR" | "PARTIAL",
            "strategy_name": str,
            "underlying": str,
            "qty": int,
            "leg_order_ids": List[str],
            "reason": str,
        }
    """
    db_path = getattr(ctx, "db_path", None) if ctx else None
    result: Dict[str, Any] = {
        "action": "ERROR",
        "strategy_name": strategy.name,
        "underlying": strategy.underlying,
        "qty": strategy.qty,
        "leg_order_ids": [],
        "reason": "",
    }

    if not strategy.legs:
        result["reason"] = "Strategy has no legs"
        return result

    # Snap each leg to a listed Alpaca contract before submission.
    # AI proposes target strikes/expiries that may not exist as
    # actual listed contracts (split-strike intervals, non-standard
    # expirations). Without this, every leg with a mismatched OCC
    # symbol fails Alpaca's "asset not found" check and the whole
    # multi-leg rolls back. Snapping silently rounds to the closest
    # listed contract within tight tolerance (5% strike, 30 days
    # expiry). If any leg can't snap within tolerance, refuse the
    # whole strategy with a specific reason.
    try:
        from options_chain_alpaca import (
            list_available_contracts as _list_contracts,
            snap_to_listed_contract as _snap,
        )
        contracts = _list_contracts(strategy.underlying)
        if contracts:
            snapped_legs = []
            for leg in strategy.legs:
                snapped = _snap(
                    strategy.underlying, leg.expiry, float(leg.strike),
                    leg.right, contracts=contracts,
                )
                if snapped is None:
                    result["reason"] = (
                        f"No listed Alpaca contract within tolerance for "
                        f"{strategy.underlying} {leg.right} {leg.strike} "
                        f"exp {leg.expiry} — refusing submission"
                    )
                    return result
                # Rebuild the leg with snapped values
                snapped_leg = OptionLeg(
                    occ_symbol=snapped["symbol"],
                    underlying=leg.underlying,
                    expiry=snapped["expiration_date"],
                    strike=snapped["strike"],
                    right=leg.right,
                    side=leg.side,
                    qty=leg.qty,
                )
                snapped_legs.append(snapped_leg)
            # 2026-06-09 — DUPLICATE-LEG DETECTION (post-snap).
            # The per-leg snapper rounds each strike to the closest
            # listed contract independently. For closely-spaced
            # spreads on chains with sparse strikes, two distinct
            # input strikes can snap to the same OCC contract (e.g.
            # AI proposes bull_put_spread short=11, long=10.5 on a
            # chain spaced $1; both snap to the $11 put). That
            # produces a degenerate zero-width "spread" that Alpaca
            # rejects with `leg.N symbol is duplicated` — caught
            # this morning on NOK and again now on NU260717P00011000.
            #
            # Detect before submission: if any two legs end up at
            # the same OCC symbol, refuse with a clean reason that
            # names the upstream cause (AI proposed strikes whose
            # snapped contracts collided) so the operator sees the
            # source of the collapse instead of a downstream
            # broker rejection.
            seen_occ = {}
            for leg in snapped_legs:
                if leg.occ_symbol in seen_occ:
                    other = seen_occ[leg.occ_symbol]
                    result["reason"] = (
                        f"Strike-snap collision: AI-proposed strikes "
                        f"for {strategy.name} on {strategy.underlying} "
                        f"both snapped to {leg.occ_symbol}. "
                        f"Original strikes (pre-snap) were too close "
                        f"to distinct listed contracts on this chain "
                        f"— spread would be zero-width. Refusing "
                        f"before broker submission."
                    )
                    logger.warning(
                        "Strike-snap collision on %s %s: legs %d and "
                        "%d both at %s — refusing.",
                        strategy.underlying, strategy.name,
                        other, snapped_legs.index(leg), leg.occ_symbol,
                    )
                    return result
                seen_occ[leg.occ_symbol] = snapped_legs.index(leg)
            # Replace strategy.legs with snapped versions. OptionStrategy
            # is a non-frozen dataclass so mutating .legs in place is
            # safe and avoids needing to know every dataclass field.
            strategy.legs = snapped_legs
        # If contracts list empty (Alpaca contracts API failure), submit
        # as-is and let Alpaca reject if any contract is missing —
        # graceful degradation.
    except Exception as exc:
        logger.debug(
            "Multi-leg contract snap failed (continuing with original "
            "legs): %s", exc,
        )

    # Duplicate-position guard: if this profile already has open journal
    # rows for any of the snapped OCC symbols (any side), refuse — the
    # spread is already on. Caught 2026-05-06: profile_10 fired the same
    # ARCC bull_call_spread every cycle for 4 hours because the long leg
    # filled but the short leg didn't, so the strategy never noticed it
    # had an open position and kept re-firing. Result: 13 phantom long
    # call contracts at the broker, no offsetting shorts.
    if db_path and strategy.legs:
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(db_path)
            for leg in strategy.legs:
                occ = getattr(leg, "occ_symbol", None)
                if not occ:
                    continue
                existing = conn.execute(
                    "SELECT id FROM trades WHERE occ_symbol=? "
                    "AND status='open' LIMIT 1",
                    (occ,),
                ).fetchone()
                if existing:
                    conn.close()
                    result["action"] = "SKIP"
                    result["reason"] = (
                        f"Duplicate-position guard: profile already has "
                        f"open journal row for {occ} (id #{existing[0]}). "
                        f"Refusing to duplicate the spread."
                    )
                    logger.warning(
                        "[multileg] %s SKIPPED — %s",
                        strategy.name, result["reason"],
                    )
                    return result
            conn.close()
        except Exception as exc:
            logger.debug(
                "Duplicate-position check failed (continuing): %s", exc,
            )

    combo_id: Optional[str] = None
    if use_combo:
        try:
            combo_kwargs = {
                "qty": strategy.qty,
                "side": "buy",  # required field; combo order side
                "type": "limit" if limit_price is not None else "market",
                "time_in_force": "day",
                "order_class": "mleg",
                "legs": [_alpaca_leg_dict(leg) for leg in strategy.legs],
            }
            if limit_price is not None:
                combo_kwargs["limit_price"] = abs(limit_price)
            # Direct POST: alpaca-trade-api's submit_order doesn't
            # accept the `legs` kwarg, but the underlying REST API
            # does. Bypass the SDK for option orders. Wrapped in a
            # 5xx-retry helper because Alpaca's paper MLEG endpoint
            # returns transient 500s on ~30% of submissions. 4xx
            # client errors bypass retry.
            combo_order = _combo_submit_with_retry(api, combo_kwargs)
            combo_id = getattr(combo_order, "id", None)
            # Combo orders return one parent id; child fills come via
            # api.list_orders(parent=combo_id) once filled.
            #
            # Atomic-placement contract: the per-leg journal writes
            # in `_log_strategy_legs` re-raise on any failure, having
            # already cancelled the broker order and halted the
            # profile. The caller-side except below converts the
            # raised exception into an ERROR result so the trade
            # pipeline records the failure without falling through
            # to the sequential path (which would re-submit to a
            # broker we already rolled back from).
            if log and db_path:
                _log_strategy_legs(
                    strategy, combo_id, ctx, api=api,
                    ai_confidence=ai_confidence,
                    ai_reasoning=ai_reasoning,
                )
            result.update({
                "action": "MULTILEG_OPEN",
                "leg_order_ids": [combo_id],
                "combo_order_id": combo_id,
                "reason": (
                    f"Submitted {strategy.name} on {strategy.underlying} "
                    f"as MLEG combo (parent={combo_id})"
                ),
            })
            return result
        except _AtomicPlacementBreach:
            # Already rolled back at the broker + halted the profile
            # inside _log_strategy_legs; surface ERROR so the trade
            # pipeline records the failure but do NOT fall through
            # to the sequential path.
            result.update({
                "action": "ERROR",
                "combo_order_id": combo_id,
                "reason": (
                    f"Multileg journal-write breach: {strategy.name} "
                    f"on {strategy.underlying} — broker rolled back, "
                    f"profile halted"
                ),
            })
            return result
        except Exception as exc:
            exc_str = str(exc)
            # Some combo failures are structurally unrecoverable —
            # sequential submission will produce a different (wrong)
            # error message that hides the real cause. Refuse fallback
            # so the operator sees the original combo error.
            #
            # "is duplicated" — combo legs collapsed to the same OCC
            # symbol (upstream strike-snapper rounded two strikes to
            # the same listed contract). Sequential would submit leg
            # 0 then have leg 1 net it back to zero (or hit uncovered
            # if Alpaca infers the second leg as a separate position),
            # producing a misleading "uncovered" reason in trade_drops
            # instead of the real "duplicate strike" cause.
            # 2026-06-09 — position-intent mismatch on the combo means
            # at least one leg's OCC has an existing broker position
            # that conflicts. Sequential fallback would hit the same
            # rejection (same legs, same broker state). Classify as
            # SKIP at the combo level to avoid the extra round-trip.
            if "position intent mismatch" in exc_str.lower():
                logger.info(
                    "Combo-order skipped — broker already holds a "
                    "conflicting position on one or more legs of %s "
                    "on %s: %s",
                    strategy.name, strategy.underlying, exc,
                )
                result.update({
                    "action": "SKIP",
                    "reason": (
                        f"Already-positioned at broker on one of "
                        f"{strategy.name}'s legs (Alpaca position-"
                        f"intent mismatch — local journal drifted "
                        f"from broker state). Not a system error."
                    ),
                })
                return result
            if "is duplicated" in exc_str or "duplicate" in exc_str.lower():
                logger.warning(
                    "Combo-order rejected with duplicate-symbol for %s "
                    "on %s: %s. Refusing sequential fallback (it would "
                    "obscure the real cause).",
                    strategy.name, strategy.underlying, exc,
                )
                result.update({
                    "action": "ERROR",
                    "reason": (
                        f"Combo rejected with duplicate-leg symbol: "
                        f"{exc_str[:200]}. Sequential fallback refused "
                        f"— upstream strike picker / snapper collapsed "
                        f"two legs to the same OCC contract."
                    ),
                })
                return result
            logger.warning(
                "Combo-order path failed for %s on %s: %s. "
                "Falling back to sequential submission.",
                strategy.name, strategy.underlying, exc,
            )
            # Fall through to sequential path

    # Sequential fallback — submit each leg, rollback on failure.
    # NOTE: position_intent is required on every option submit_order;
    # without it Alpaca async-cancels short opens (the root cause of
    # the 2026-05-06 ARCC runaway). _INTENT_OPEN maps buy→buy_to_open
    # and sell→sell_to_open for opening legs.
    #
    # 2026-06-09 — leg ordering for sequential submission. Credit-spread
    # builders emit legs in shorts-first convention (so the journal
    # rows reflect the credit-receiving leg first). For an atomic
    # MLEG combo that's fine — Alpaca processes all legs together. In
    # sequential mode submitting a short leg ALONE makes Alpaca see
    # an uncovered short → 403 "account not eligible to trade
    # uncovered option" on accounts approved only for vertical
    # spreads. Sort longs (buy) before shorts (sell) so each short
    # is submitted after its covering long is already open at the
    # broker. Stable sort preserves intra-side ordering so the
    # rollback path still matches the original strategy.legs order.
    sequential_legs = sorted(
        strategy.legs,
        key=lambda lg: 0 if lg.side == "buy" else 1,
    )
    submitted: List[Dict[str, Any]] = []
    for i, leg in enumerate(sequential_legs):
        try:
            # Direct POST so position_intent reaches Alpaca — the
            # SDK's submit_order signature drops the kwarg.
            order = _submit_alpaca_order_raw(api, {
                "symbol": leg.occ_symbol,
                "qty": leg.qty,
                "side": leg.side,
                "type": "market",
                "time_in_force": "day",
                "position_intent": _INTENT_OPEN.get(leg.side, "buy_to_open"),
            })
            submitted.append({
                "leg_index": i, "leg": leg,
                "order_id": getattr(order, "id", None),
            })
        except Exception as exc:
            exc_str = str(exc)
            # 2026-06-09 — position-intent mismatch means the broker
            # already holds a position on this exact OCC that conflicts
            # with our intent (we said sell_to_open; Alpaca inferred
            # sell_to_close because there's an existing long, or vice
            # versa). The duplicate-position guard above (line ~711)
            # checks our LOCAL journal — if journal drifted from broker
            # state we miss this and Alpaca catches it instead. Not a
            # system ERROR; classify as SKIP with a clear reason so the
            # operator sees "already-positioned at broker" rather than
            # a red error badge in the AI Brain.
            if "position intent mismatch" in exc_str.lower():
                logger.info(
                    "Leg %d (%s %s) of %s skipped — broker already "
                    "holds a conflicting position on this OCC: %s",
                    i, leg.side, leg.occ_symbol, strategy.name, exc,
                )
                # Rollback any legs we already opened in this attempt
                # (typically zero, since position-intent issues usually
                # surface on leg 0; but be defensive)
                for sub in submitted:
                    try:
                        rev_side = (
                            "sell" if sub["leg"].side == "buy" else "buy"
                        )
                        _submit_alpaca_order_raw(api, {
                            "symbol": sub["leg"].occ_symbol,
                            "qty": sub["leg"].qty,
                            "side": rev_side,
                            "type": "market",
                            "time_in_force": "day",
                            "position_intent": _INTENT_CLOSE.get(
                                rev_side, "sell_to_close",
                            ),
                        })
                    except Exception as _rb_exc:
                        logger.warning(
                            "Rollback of leg %d failed during position-"
                            "intent skip: %s",
                            sub["leg_index"], _rb_exc,
                        )
                result.update({
                    "action": "SKIP",
                    "leg_order_ids": [s["order_id"] for s in submitted],
                    "reason": (
                        f"Already-positioned at broker on "
                        f"{leg.occ_symbol} (Alpaca position-intent "
                        f"mismatch — local journal drifted from broker "
                        f"state). Not a system error; the spread was "
                        f"declined to avoid stacking a conflicting "
                        f"position."
                    ),
                })
                return result
            logger.error(
                "Leg %d (%s %s) of %s failed: %s. Attempting rollback.",
                i, leg.side, leg.occ_symbol, strategy.name, exc,
            )
            # Rollback: close each successfully-submitted leg with the
            # OPPOSITE intent (_INTENT_CLOSE). A buy_to_open is closed
            # by sell_to_close; a sell_to_open by buy_to_close.
            rollback_results = []
            for sub in submitted:
                try:
                    rev_side = "sell" if sub["leg"].side == "buy" else "buy"
                    rev = _submit_alpaca_order_raw(api, {
                        "symbol": sub["leg"].occ_symbol,
                        "qty": sub["leg"].qty,
                        "side": rev_side,
                        "type": "market",
                        "time_in_force": "day",
                        "position_intent": _INTENT_CLOSE.get(rev_side, "sell_to_close"),
                    })
                    rollback_results.append({
                        "leg_index": sub["leg_index"],
                        "rollback_order_id": getattr(rev, "id", None),
                    })
                except Exception as rb_exc:
                    rollback_results.append({
                        "leg_index": sub["leg_index"],
                        "rollback_error": str(rb_exc),
                    })
            result.update({
                "action": "ERROR",
                "leg_order_ids": [s["order_id"] for s in submitted],
                "rollback": rollback_results,
                "reason": (
                    f"Leg {i} failed: {exc}. Submitted {len(submitted)} "
                    f"leg(s); rollback attempted."
                ),
            })
            return result

    # All legs submitted successfully via sequential path
    leg_order_ids = [s["order_id"] for s in submitted]
    if log and db_path:
        try:
            _log_strategy_legs(
                strategy, None, ctx,
                leg_order_ids=leg_order_ids, api=api,
                ai_confidence=ai_confidence, ai_reasoning=ai_reasoning,
            )
        except _AtomicPlacementBreach:
            result.update({
                "action": "ERROR",
                "leg_order_ids": leg_order_ids,
                "reason": (
                    f"Multileg journal-write breach (sequential): "
                    f"{strategy.name} on {strategy.underlying} — "
                    f"broker rolled back, profile halted"
                ),
            })
            return result
    result.update({
        "action": "MULTILEG_OPEN",
        "leg_order_ids": leg_order_ids,
        "reason": (
            f"Submitted {len(submitted)} legs of {strategy.name} on "
            f"{strategy.underlying} sequentially (combo unavailable)"
        ),
    })
    return result


def build_iron_condor(
    underlying: str,
    expiry: date,
    put_long_strike: float,    # lowest
    put_short_strike: float,   # below the money
    call_short_strike: float,  # above the money
    call_long_strike: float,   # highest
    qty: int = 1,
    put_short_premium: Optional[float] = None,
    put_long_premium: Optional[float] = None,
    call_short_premium: Optional[float] = None,
    call_long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Iron condor: short OTM put spread + short OTM call spread.

    Range-bound thesis: profit if price stays between the two short
    strikes at expiry. Net credit; defined-risk neutral strategy.

    Strikes ordered low → high:
      put_long < put_short < call_short < call_long
    Width must be equal on both wings (typical iron condor).

    Max gain  = total_credit * 100
    Max loss  = (wing_width - total_credit) * 100  (per spread, on
                  whichever wing breaches first; symmetric when wings
                  are equal width)
    Breakevens (at expiry, two of them):
      lower = put_short - total_credit
      upper = call_short + total_credit
    """
    # Validate strike ordering
    strikes = [put_long_strike, put_short_strike,
               call_short_strike, call_long_strike]
    if any(s <= 0 for s in strikes):
        raise ValueError("strikes must be positive")
    if not (put_long_strike < put_short_strike <
            call_short_strike < call_long_strike):
        raise ValueError(
            f"strikes must be ordered low→high: "
            f"{put_long_strike} < {put_short_strike} < "
            f"{call_short_strike} < {call_long_strike}"
        )
    if qty <= 0:
        raise ValueError("qty must be positive")

    put_wing_width = put_short_strike - put_long_strike
    call_wing_width = call_long_strike - call_short_strike
    # We use max of the two for max-loss math (the worse-case wing).
    spread_width = max(put_wing_width, call_wing_width)

    # Build the 4 legs in convention order (shorts first, then longs)
    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            put_short_strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=put_short_strike, right="P", side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            call_short_strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=call_short_strike, right="C", side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            put_long_strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=put_long_strike, right="P", side="buy", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            call_long_strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=call_long_strike, right="C", side="buy", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="iron_condor",
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=spread_width,
        is_credit=True,
        thesis=(
            f"Range-bound on {underlying.upper()} between "
            f"{put_short_strike:.2f} and {call_short_strike:.2f}. "
            f"Collect premium; defined-risk wings at "
            f"{put_long_strike:.2f}/{call_long_strike:.2f}. Profits "
            f"as time passes if price stays in the range."
        ),
    )

    if all(p is not None for p in (put_short_premium, put_long_premium,
                                     call_short_premium, call_long_premium)):
        # Net credit = sum of shorts - sum of longs
        net_credit = (
            (put_short_premium - put_long_premium)
            + (call_short_premium - call_long_premium)
        )
        spec.net_premium_per_contract = -net_credit
        spec.max_gain_per_contract = net_credit * 100
        spec.max_loss_per_contract = (spread_width - net_credit) * 100
        # Two breakevens — store as a list in a custom field via thesis,
        # OR record the lower one (the more relevant for downside risk).
        spec.breakeven_at_expiry = put_short_strike - net_credit

    return spec


def build_iron_butterfly(
    underlying: str,
    expiry: date,
    body_strike: float,        # ATM (short straddle body)
    wing_width: float,         # equal width on both sides
    qty: int = 1,
    put_short_premium: Optional[float] = None,
    put_long_premium: Optional[float] = None,
    call_short_premium: Optional[float] = None,
    call_long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Iron butterfly: short ATM straddle + long OTM wings.

    Pin-risk thesis (price expected to stay AT the body strike).
    Higher max gain than iron condor (collect on both ATM legs) but
    much narrower profit zone. Net credit, defined risk.

    Max gain  = net_credit * 100  (only at exactly body_strike at expiry)
    Max loss  = (wing_width - net_credit) * 100
    Breakevens: body_strike ± net_credit
    """
    if body_strike <= 0 or wing_width <= 0:
        raise ValueError("body_strike and wing_width must be positive")
    if qty <= 0:
        raise ValueError("qty must be positive")

    put_long_strike = body_strike - wing_width
    call_long_strike = body_strike + wing_width

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, body_strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=body_strike, right="P", side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, body_strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=body_strike, right="C", side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            put_long_strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=put_long_strike, right="P", side="buy", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry,
                                            call_long_strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=call_long_strike, right="C", side="buy", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="iron_butterfly",
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=wing_width,
        is_credit=True,
        thesis=(
            f"Pin-risk on {underlying.upper()} at {body_strike:.2f}. "
            f"Collect rich premium; max gain at the pin. Max loss "
            f"capped at wings ${wing_width:.2f} away."
        ),
    )

    if all(p is not None for p in (put_short_premium, put_long_premium,
                                     call_short_premium, call_long_premium)):
        net_credit = (
            (put_short_premium - put_long_premium)
            + (call_short_premium - call_long_premium)
        )
        spec.net_premium_per_contract = -net_credit
        spec.max_gain_per_contract = net_credit * 100
        spec.max_loss_per_contract = (wing_width - net_credit) * 100
        spec.breakeven_at_expiry = body_strike - net_credit  # lower

    return spec


def build_long_straddle(
    underlying: str,
    expiry: date,
    strike: float,
    qty: int = 1,
    call_premium: Optional[float] = None,
    put_premium: Optional[float] = None,
) -> OptionStrategy:
    """Long straddle: long ATM call + long ATM put.

    Long-vol thesis: profit if price moves significantly in EITHER
    direction (or IV expands). Unlimited max gain. Max loss = total
    premium paid.

    Max loss   = (call_premium + put_premium) * 100
    Max gain   = unlimited (technically)
    Breakevens = strike ± (total_premium)
    """
    if strike <= 0:
        raise ValueError("strike must be positive")
    if qty <= 0:
        raise ValueError("qty must be positive")

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=strike, right="C", side="buy", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=strike, right="P", side="buy", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="long_straddle",
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=0,  # not a spread
        is_credit=False,
        thesis=(
            f"Long-vol bet on {underlying.upper()} at {strike:.2f}. "
            f"Profits on a big move in either direction or on IV "
            f"expansion. Time decay works against you."
        ),
    )

    if call_premium is not None and put_premium is not None:
        total_debit = call_premium + put_premium
        spec.net_premium_per_contract = total_debit
        spec.max_loss_per_contract = total_debit * 100
        # Max gain unlimited — leave None to signal that
        spec.breakeven_at_expiry = strike - total_debit  # lower BE

    return spec


def build_short_straddle(
    underlying: str,
    expiry: date,
    strike: float,
    qty: int = 1,
    call_premium: Optional[float] = None,
    put_premium: Optional[float] = None,
) -> OptionStrategy:
    """Short straddle: short ATM call + short ATM put.

    Range-bound thesis with UNCAPPED downside. Real funds use this
    only with a careful risk budget and ideally with a 2nd-tier
    hedge (which makes it an iron butterfly). Included here for
    completeness; expect the advisor to almost never recommend it
    over an iron butterfly.

    Max gain   = (call_premium + put_premium) * 100  (at exactly strike)
    Max loss   = unlimited
    Breakevens = strike ± total_premium
    """
    if strike <= 0:
        raise ValueError("strike must be positive")
    if qty <= 0:
        raise ValueError("qty must be positive")

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=strike, right="C", side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=strike, right="P", side="sell", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="short_straddle",
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=0,
        is_credit=True,
        thesis=(
            f"Pin-risk + short-vol bet on {underlying.upper()} at "
            f"{strike:.2f}. UNCAPPED downside on big moves. Prefer "
            f"iron_butterfly (defined risk equivalent)."
        ),
    )

    if call_premium is not None and put_premium is not None:
        total_credit = call_premium + put_premium
        spec.net_premium_per_contract = -total_credit
        spec.max_gain_per_contract = total_credit * 100
        # Max loss unlimited — leave None
        spec.breakeven_at_expiry = strike + total_credit  # upper BE

    return spec


def build_long_strangle(
    underlying: str,
    expiry: date,
    put_strike: float,    # below ATM
    call_strike: float,   # above ATM
    qty: int = 1,
    call_premium: Optional[float] = None,
    put_premium: Optional[float] = None,
) -> OptionStrategy:
    """Long strangle: long OTM put + long OTM call.

    Long-vol bet, cheaper than a straddle but needs a bigger move
    to profit. Defined-risk on the debit, unlimited gain potential.

    Max loss   = (call_premium + put_premium) * 100
    Max gain   = unlimited
    Breakevens = call_strike + total_debit  (upper)
                 put_strike  - total_debit  (lower)
    """
    if put_strike >= call_strike:
        raise ValueError(
            f"put_strike ({put_strike}) must be < call_strike ({call_strike})"
        )
    if put_strike <= 0 or qty <= 0:
        raise ValueError("strikes and qty must be positive")

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, call_strike, "C"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=call_strike, right="C", side="buy", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, expiry, put_strike, "P"),
            underlying=underlying.upper(), expiry=expiry.isoformat(),
            strike=put_strike, right="P", side="buy", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="long_strangle",
        underlying=underlying.upper(), expiry=expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=call_strike - put_strike,
        is_credit=False,
        thesis=(
            f"Long-vol bet on {underlying.upper()} (cheaper than "
            f"straddle). Profits on a big move past "
            f"{put_strike:.2f} or {call_strike:.2f}. IV expansion "
            f"helps; time decay hurts."
        ),
    )

    if call_premium is not None and put_premium is not None:
        total_debit = call_premium + put_premium
        spec.net_premium_per_contract = total_debit
        spec.max_loss_per_contract = total_debit * 100
        spec.breakeven_at_expiry = put_strike - total_debit

    return spec


def build_calendar_spread(
    underlying: str,
    short_expiry: date,    # near expiry (sell)
    long_expiry: date,     # far expiry (buy)
    strike: float,
    right: str = "C",
    qty: int = 1,
    short_premium: Optional[float] = None,
    long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Calendar spread: short near-expiry option + long far-expiry option
    at the SAME strike.

    Term-structure bet: profits as the front-month decays faster than
    the back-month. Net debit. Best when:
      - IV is reasonably stable (not collapsing on the back)
      - Front-expiry has near-pin behavior likely

    Max loss   = net_debit * 100  (when stock moves far either way)
    Max gain   = roughly difference in time value at front expiry,
                  hard to compute closed-form (depends on path).
    Breakevens: implicit; depends on how vol-surface evolves.
    """
    if strike <= 0 or qty <= 0:
        raise ValueError("strike and qty must be positive")
    if short_expiry >= long_expiry:
        raise ValueError(
            f"short_expiry ({short_expiry}) must be before "
            f"long_expiry ({long_expiry})"
        )
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, short_expiry, strike, right),
            underlying=underlying.upper(), expiry=short_expiry.isoformat(),
            strike=strike, right=right, side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, long_expiry, strike, right),
            underlying=underlying.upper(), expiry=long_expiry.isoformat(),
            strike=strike, right=right, side="buy", qty=qty,
        ),
    ]

    days_to_short = (short_expiry - date.today()).days
    days_to_long = (long_expiry - date.today()).days

    spec = OptionStrategy(
        name="calendar_spread",
        underlying=underlying.upper(),
        expiry=long_expiry.isoformat(),  # use long expiry for tracking
        legs=legs, qty=qty,
        spread_width_points=0,
        is_credit=False,
        thesis=(
            f"Term-structure bet on {underlying.upper()} at "
            f"{strike:.2f}. Sell {days_to_short}d, buy {days_to_long}d. "
            f"Profits as front-month decays faster than back-month."
        ),
    )

    if short_premium is not None and long_premium is not None:
        net_debit = long_premium - short_premium
        spec.net_premium_per_contract = net_debit
        spec.max_loss_per_contract = net_debit * 100

    return spec


def build_diagonal_spread(
    underlying: str,
    short_expiry: date,
    long_expiry: date,
    short_strike: float,    # OTM (typically)
    long_strike: float,     # different strike from short
    right: str = "C",
    qty: int = 1,
    short_premium: Optional[float] = None,
    long_premium: Optional[float] = None,
) -> OptionStrategy:
    """Diagonal spread: short near-expiry + long far-expiry at
    DIFFERENT strikes. Hybrid between vertical and calendar.

    Combines directional view (different strikes = directional bias)
    with term-structure (different expiries = front-decay capture).
    Most flexible primitive; max_loss/max_gain depend on both
    components.
    """
    if short_strike <= 0 or long_strike <= 0 or qty <= 0:
        raise ValueError("strikes and qty must be positive")
    if short_expiry >= long_expiry:
        raise ValueError("short_expiry must be before long_expiry")
    if right not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")

    legs = [
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, short_expiry,
                                            short_strike, right),
            underlying=underlying.upper(), expiry=short_expiry.isoformat(),
            strike=short_strike, right=right, side="sell", qty=qty,
        ),
        OptionLeg(
            occ_symbol=format_occ_symbol(underlying, long_expiry,
                                            long_strike, right),
            underlying=underlying.upper(), expiry=long_expiry.isoformat(),
            strike=long_strike, right=right, side="buy", qty=qty,
        ),
    ]

    spec = OptionStrategy(
        name="diagonal_spread",
        underlying=underlying.upper(),
        expiry=long_expiry.isoformat(),
        legs=legs, qty=qty,
        spread_width_points=abs(long_strike - short_strike),
        is_credit=False,
        thesis=(
            f"Diagonal {right}-spread on {underlying.upper()}: "
            f"directional bias + term structure. "
            f"Sell {short_strike:.2f}/{short_expiry.isoformat()}, "
            f"buy {long_strike:.2f}/{long_expiry.isoformat()}."
        ),
    )

    if short_premium is not None and long_premium is not None:
        net_debit = long_premium - short_premium
        spec.net_premium_per_contract = net_debit
        if net_debit >= 0:
            spec.max_loss_per_contract = net_debit * 100
        else:
            # Net credit: max gain is the credit
            spec.max_gain_per_contract = abs(net_debit) * 100
            spec.is_credit = True

    return spec


# Extended registry with all multi-leg builders
ALL_MULTILEG_BUILDERS = {
    **VERTICAL_SPREAD_BUILDERS,
    "iron_condor": build_iron_condor,
    "iron_butterfly": build_iron_butterfly,
    "long_straddle": build_long_straddle,
    "short_straddle": build_short_straddle,
    "long_strangle": build_long_strangle,
    "calendar_spread": build_calendar_spread,
    "diagonal_spread": build_diagonal_spread,
}


# Which option right each strike key of a vertical spread trades.
_VERTICAL_RIGHT = {
    "bull_put_spread": "P", "bear_put_spread": "P",
    "bull_call_spread": "C", "bear_call_spread": "C",
}


def validate_and_snap_multileg_strikes(
    underlying: str,
    strategy_name: str,
    strikes: Dict[str, Any],
    expiry: str,
    contracts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Tuple[Dict[str, Any], str]]:
    """Parse-layer strike validation for MULTILEG_OPEN proposals.

    2026-06-10 — the single-leg OPTIONS path got parse-layer
    snap-to-listed-contract validation this morning (RC11); multileg
    did not. AI strikes that don't resolve to DISTINCT listed
    contracts only failed at execution (the 06-09 strike-snap
    collision refusal) and badged GATED · ERROR on every profile
    that received the same proposal — AAL bear_call_spread 14/14.5
    on a $1 grid hit 3 profiles on the first post-reset cycle.

    This validates AND repairs at the parse layer so a proposal is
    either executable exactly as validated (snapped strikes baked
    in) or never reaches the executor/brain. Per strategy shape:

      verticals       both strikes snapped as an ordered group on
                      the strategy's right; collisions repaired one
                      grid step outward (snap_strike_group)
      iron_condor     put pair + call pair group-snapped on their
                      rights; full put_long < put_short <
                      call_short < call_long ordering enforced
                      post-snap (the builder raises on violations)
      straddles       body must exist at the SAME strike + expiry
                      on both rights
      long_strangle   pair group-snapped for distinctness/order on
                      the call grid; put leg must exist at the
                      lower strike (builder requires put < call)
      iron_butterfly  body snapped on both rights; wings at
                      body±width must exist and stay symmetric
                      (builder takes a single wing_width)

    Unknown shapes and chain-data outages pass through unchanged —
    the execution-layer snap + collision refusal remains the safety
    belt, same graceful degradation as the single-leg path.

    Returns (snapped_strikes, snapped_expiry), or None when the
    proposal is structurally unplaceable on the listed chain (the
    caller must reject it before it enters the validated list).
    """
    from options_chain_alpaca import (
        list_available_contracts, snap_strike_group,
        snap_to_listed_contract,
    )

    name = (strategy_name or "").lower()
    if contracts is None:
        contracts = list_available_contracts(underlying)
    if not contracts:
        # Chain fetch failed — degrade gracefully rather than reject
        # legitimate proposals on an infrastructure blip.
        return strikes, expiry

    try:
        if name in _VERTICAL_RIGHT:
            lo, hi = sorted(
                (float(strikes["short"]), float(strikes["long"]))
            )
            if lo == hi:
                return None
            group = snap_strike_group(
                underlying, expiry, [lo, hi], _VERTICAL_RIGHT[name],
                contracts=contracts,
            )
            if group is None:
                return None
            new_lo, new_hi = group["strikes"]
            # Preserve the AI's short/long labeling orientation for
            # audit visibility; the dispatcher sorts before building
            # so orientation doesn't affect execution.
            if float(strikes["short"]) <= float(strikes["long"]):
                out = {"short": new_lo, "long": new_hi}
            else:
                out = {"short": new_hi, "long": new_lo}
            return out, group["expiration_date"]

        if name == "iron_condor":
            put_pair = sorted(
                (float(strikes["put_long"]), float(strikes["put_short"]))
            )
            call_pair = sorted(
                (float(strikes["call_short"]), float(strikes["call_long"]))
            )
            if put_pair[0] == put_pair[1] or call_pair[0] == call_pair[1]:
                return None
            gp = snap_strike_group(
                underlying, expiry, put_pair, "P", contracts=contracts,
            )
            gc = snap_strike_group(
                underlying, expiry, call_pair, "C", contracts=contracts,
            )
            if gp is None or gc is None:
                return None
            if gp["expiration_date"] != gc["expiration_date"]:
                return None
            pl, ps = gp["strikes"]
            cs, cl = gc["strikes"]
            if not (pl < ps < cs < cl):
                # Wings overlap the body post-snap — the builder
                # would raise; reject at parse instead.
                return None
            return (
                {"put_long": pl, "put_short": ps,
                 "call_short": cs, "call_long": cl},
                gp["expiration_date"],
            )

        if name in ("long_straddle", "short_straddle"):
            body = float(strikes["strike"])
            sc = snap_to_listed_contract(
                underlying, expiry, body, "C", contracts=contracts,
            )
            sp = snap_to_listed_contract(
                underlying, expiry, body, "P", contracts=contracts,
            )
            if not sc or not sp:
                return None
            if (sc["strike"] != sp["strike"]
                    or sc["expiration_date"] != sp["expiration_date"]):
                return None
            return {"strike": sc["strike"]}, sc["expiration_date"]

        if name == "long_strangle":
            put_s = float(strikes["put"])
            call_s = float(strikes["call"])
            if put_s >= call_s:
                return None
            group = snap_strike_group(
                underlying, expiry, [put_s, call_s], "C",
                contracts=contracts,
            )
            if group is None:
                return None
            new_put, new_call = group["strikes"]
            sp = snap_to_listed_contract(
                underlying, group["expiration_date"], new_put, "P",
                contracts=contracts,
            )
            if (not sp or sp["strike"] != new_put
                    or sp["expiration_date"] != group["expiration_date"]):
                return None
            return (
                {"put": new_put, "call": new_call},
                group["expiration_date"],
            )

        if name == "iron_butterfly":
            body = float(strikes["body"])
            width = float(strikes["wing_width"])
            if body <= 0 or width <= 0:
                return None
            sc = snap_to_listed_contract(
                underlying, expiry, body, "C", contracts=contracts,
            )
            sp = snap_to_listed_contract(
                underlying, expiry, body, "P", contracts=contracts,
            )
            if not sc or not sp:
                return None
            if (sc["strike"] != sp["strike"]
                    or sc["expiration_date"] != sp["expiration_date"]):
                return None
            snapped_body = sc["strike"]
            snapped_exp = sc["expiration_date"]
            cw = snap_to_listed_contract(
                underlying, snapped_exp, snapped_body + width, "C",
                contracts=contracts,
            )
            pw = snap_to_listed_contract(
                underlying, snapped_exp, snapped_body - width, "P",
                contracts=contracts,
            )
            if not cw or not pw:
                return None
            if (cw["expiration_date"] != snapped_exp
                    or pw["expiration_date"] != snapped_exp):
                return None
            up = cw["strike"] - snapped_body
            down = snapped_body - pw["strike"]
            if up <= 0 or down <= 0 or up != down:
                # Wing collapsed onto the body (width below the
                # grid) or asymmetric wings — builder takes a single
                # symmetric wing_width; reject at parse.
                return None
            return (
                {"body": snapped_body, "wing_width": up},
                snapped_exp,
            )
    except (KeyError, TypeError, ValueError):
        # Malformed strikes dict for the declared strategy shape —
        # structurally unbuildable, reject at parse.
        return None

    # Unknown strategy shape (calendar/diagonal etc.) — pass through;
    # the execution-layer snap remains the safety belt.
    return strikes, expiry


class _AtomicPlacementBreach(Exception):
    """Raised by `_log_strategy_legs` when a per-leg journal write
    fails AFTER broker orders were placed. Broker rollback has been
    attempted and the profile halted before this is raised; callers
    use the exception type to distinguish "atomic-placement breach
    with cleanup already done" from a fall-through pre-broker
    failure (e.g., combo path 5xx → fall through to sequential).
    """
    pass


def _rollback_multileg_broker_orders(
    api,
    combo_order_id: Optional[str],
    leg_order_ids: Optional[List[str]],
) -> None:
    """Cancel every broker order produced by a multileg submission so
    a partial / total journal-write failure can't leave the broker
    holding positions that no profile's virtual book reflects (the
    `broker_orphan` class the aggregate audit catches after the fact).

    Combo path: one parent `combo_order_id` whose cancel unwinds all
    legs at the broker. Sequential path: per-leg order_ids — every
    one gets cancelled. Some IDs may be None (failed leg before its
    own submit completed); skip those.

    Caller responsibility: invoke this on ANY exception inside the
    journal-write block. Re-raises the first cancel exception so the
    caller knows the rollback was incomplete and can halt the
    profile.
    """
    if api is None:
        return
    candidates: List[str] = []
    if combo_order_id:
        candidates.append(str(combo_order_id))
    for oid in (leg_order_ids or []):
        if oid and str(oid) not in candidates:
            candidates.append(str(oid))
    first_exc: Optional[Exception] = None
    for oid in candidates:
        try:
            api.cancel_order(oid)
            logger.info(
                "Multileg rollback: cancelled broker order %s", oid,
            )
        except Exception as exc:
            # Treat "already terminal" cancels as success — the
            # broker order is in the desired non-active state.
            msg = str(exc).lower()
            if (
                "already" in msg and (
                    "filled" in msg or "cancel" in msg
                    or "terminal" in msg
                )
            ):
                logger.info(
                    "Multileg rollback: order %s already terminal: %s",
                    oid, exc,
                )
                continue
            logger.error(
                "Multileg rollback: cancel %s FAILED: %s: %s",
                oid, type(exc).__name__, exc,
            )
            if first_exc is None:
                first_exc = exc
    if first_exc is not None:
        raise first_exc


def _mark_legs_canceled(
    db_path: Optional[str],
    journal_row_ids: List[int],
) -> None:
    """Flip status='canceled' on every journal row this same call
    successfully wrote before a later leg's log_trade raised. The
    FIFO position book filters on `status != 'canceled'`, so these
    rows stop affecting any virtual book derivation; the rows
    themselves survive for audit traceability.

    Best-effort: the caller has already initiated broker rollback +
    profile halt before invoking this, so a DB failure here is logged
    but doesn't change the outcome (the halt is what stops further
    trading until manual review).
    """
    if not db_path or not journal_row_ids:
        return
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(db_path)) as conn:
        placeholders = ",".join("?" * len(journal_row_ids))
        conn.execute(
            f"UPDATE trades SET status='canceled' "
            f"WHERE id IN ({placeholders})",
            list(journal_row_ids),
        )
        conn.commit()


def _log_strategy_legs(strategy: OptionStrategy,
                          combo_order_id: Optional[str],
                          ctx,
                          leg_order_ids: Optional[List[str]] = None,
                          api=None,
                          ai_confidence: Optional[int] = None,
                          ai_reasoning: Optional[str] = None) -> None:
    """Write one journal row per leg, tagging them with the strategy
    name so the lifecycle sweep + dashboard can group them together.

    `signal_type=MULTILEG` and `option_strategy=<strategy.name>` make
    the legs queryable as a unit. `reason` includes the combo order id
    when available.

    `ai_confidence` and `ai_reasoning` come from the AI proposal so
    every leg row carries the same per-trade context that single-leg
    options + stock trades carry. Without them, the trades-table AI
    Conf column shows '--' on multileg legs and the expanded reason
    shows the spread's boilerplate thesis instead of the AI's
    per-trade rationale.
    """
    db_path = getattr(ctx, "db_path", None) if ctx else None
    if not db_path:
        return
    try:
        from journal import log_trade
    except Exception:
        return

    # Combo path: every leg shares the SAME parent `combo_order_id`.
    # Calling `api.get_order(combo_id).filled_avg_price` returns the
    # COMBO'S NET PREMIUM (signed), not per-leg prices. For credit
    # spreads that's a NEGATIVE number, which then gets stored as
    # the price on every leg — silently dropping all legs from
    # `get_virtual_positions` (`if price <= 0: continue`) and
    # making them invisible to the AI's portfolio context. Caught
    # 2026-05-11: 10 multileg legs across 4 profiles invisible since
    # May 8.
    #
    # Per-leg fills live on `combo_order.legs[i].filled_avg_price`
    # (positive numbers). Build an OCC→price map up front and use
    # it for any leg whose `order_id == combo_order_id`. Sequential
    # path (each leg has its own order_id) keeps the existing
    # per-leg fetch.
    combo_legs_by_occ = {}
    if combo_order_id and api is not None:
        try:
            combo_o = api.get_order(combo_order_id)
            for cl in (getattr(combo_o, "legs", []) or []):
                cl_fap = getattr(cl, "filled_avg_price", None)
                cl_sym = getattr(cl, "symbol", None)
                if cl_sym and cl_fap is not None:
                    combo_legs_by_occ[cl_sym] = float(cl_fap)
        except Exception as exc:
            logger.debug(
                "Combo legs fetch (%s) failed: %s — _task_update_fills "
                "will backfill per-leg prices on its next cycle",
                combo_order_id, exc,
            )

    leg_order_ids = leg_order_ids or [combo_order_id] * len(strategy.legs)

    # Per atomic-placement contract (`feedback_no_orphan_broker_fills`,
    # `feedback_fix_class_not_instance`): if ANY per-leg journal write
    # raises, every successfully-journaled leg AND every broker order
    # placed by this call MUST be unwound — otherwise the broker
    # holds positions no virtual book reflects (a `broker_orphan`
    # which the aggregate audit surfaces but only after the fact).
    # On rollback failure the profile is halted so the operator sees
    # the breach immediately rather than discovering it via the next
    # audit cycle.
    journaled_leg_ids: List[int] = []
    rollback_failed = False
    for i, leg in enumerate(strategy.legs):
        order_id = (leg_order_ids[i]
                    if i < len(leg_order_ids) else combo_order_id)
        # Per-leg price priority:
        #   (a) combo's per-leg fill (combo path — matched by OCC)
        #   (b) this leg's own order fill (sequential path — order_id
        #       differs from combo_order_id)
        # Never store the combo's signed net price as a leg price.
        leg_price = combo_legs_by_occ.get(leg.occ_symbol)
        if leg_price is None and api is not None and order_id \
                and order_id != combo_order_id:
            try:
                o = api.get_order(order_id)
                fap = getattr(o, "filled_avg_price", None)
                if fap is not None:
                    leg_price = float(fap)
            except Exception as exc:
                # Best-effort: paper-account fills usually need
                # 50-500ms after submit, so this often returns None
                # immediately. _task_update_fills is the reliable
                # catch-up path. Log at debug so an unusual failure
                # mode (auth error, network) is still observable
                # without spamming WARN on every leg log.
                logger.debug(
                    "leg get_order(%s) returned no immediate fill: %s",
                    order_id, exc,
                )
        # Defense-in-depth: never store a non-positive price. If
        # something upstream went wrong, leave the column NULL so
        # _task_update_fills can backfill it later from the broker.
        # `get_virtual_positions` skips price=NULL rows the same way
        # it skips price<=0 rows, so the position remains invisible
        # but a recovery path exists. Log a warning so we know.
        if leg_price is not None and leg_price <= 0:
            logger.warning(
                "Refusing to write non-positive price %s on multileg "
                "leg %s/%s (combo=%s) — leaving NULL for update_fills "
                "to backfill",
                leg_price, leg.occ_symbol,
                strategy.name, combo_order_id,
            )
            leg_price = None
        try:
            # Prefer the AI's per-trade reasoning when present;
            # fall back to the spread's structural thesis so the
            # row always carries SOMETHING explanatory.
            row_reasoning = ai_reasoning or strategy.thesis
            row_id = log_trade(
                symbol=leg.underlying,
                side=leg.side,
                qty=leg.qty,
                price=leg_price,
                order_id=order_id,
                signal_type="MULTILEG",
                strategy=strategy.name,
                reason=(
                    f"{strategy.name} leg {i+1}/{len(strategy.legs)} "
                    f"(combo={combo_order_id or 'sequential'})"
                ),
                ai_reasoning=row_reasoning,
                ai_confidence=ai_confidence,
                fill_price=leg_price,
                occ_symbol=leg.occ_symbol,
                option_strategy=strategy.name,
                expiry=leg.expiry,
                strike=float(leg.strike),
                db_path=db_path,
            )
            if row_id:
                journaled_leg_ids.append(int(row_id))
        except Exception as exc:
            # Atomic-placement breach: the broker has fills this
            # process can no longer journal. Cancel every order
            # this call placed, mark any leg row already written
            # as 'canceled' so the FIFO position book ignores it,
            # halt the profile, then re-raise so the caller sees
            # the failure (vs the prior silent warning + continue).
            logger.error(
                "log_trade FAILED for leg %d of %s (occ=%s, combo=%s): "
                "%s: %s — initiating atomic rollback",
                i, strategy.name, leg.occ_symbol, combo_order_id,
                type(exc).__name__, exc,
            )
            try:
                _rollback_multileg_broker_orders(
                    api, combo_order_id, leg_order_ids,
                )
            except Exception as cancel_exc:
                rollback_failed = True
                logger.error(
                    "Rollback of broker orders FAILED for %s "
                    "(combo=%s): %s: %s — broker may hold orphan "
                    "positions; halting profile",
                    strategy.name, combo_order_id,
                    type(cancel_exc).__name__, cancel_exc,
                )
            # Mark any successfully-journaled leg rows from this
            # same call as canceled so the virtual book treats the
            # whole strategy as never having existed.
            try:
                _mark_legs_canceled(db_path, journaled_leg_ids)
            except Exception as mark_exc:
                logger.error(
                    "Marking journaled legs %s canceled FAILED: "
                    "%s: %s",
                    journaled_leg_ids,
                    type(mark_exc).__name__, mark_exc,
                )
            # Halt the profile so trading stops until the operator
            # acknowledges. Rollback failure is more serious (broker
            # could still hold orphan fills) so distinguish in the
            # alert title.
            try:
                from halt_helpers import halt_and_alert
                profile_id = (
                    getattr(ctx, "profile_id", None) if ctx else None
                )
                if profile_id:
                    title = (
                        "Multileg journal-write breach (rollback FAILED): "
                        + strategy.name
                        if rollback_failed
                        else "Multileg journal-write breach: "
                        + strategy.name
                    )
                    halt_and_alert(
                        profile_id=profile_id,
                        db_path=db_path,
                        alert_type="multileg_atomic_breach",
                        title=title,
                        detail=(
                            f"strategy={strategy.name} "
                            f"underlying={strategy.underlying} "
                            f"combo_order_id={combo_order_id} "
                            f"failing_leg_index={i} "
                            f"failing_leg_occ={leg.occ_symbol} "
                            f"log_trade_exc={type(exc).__name__}: {exc} "
                            f"rollback_failed={rollback_failed}"
                        ),
                    )
            except Exception as halt_exc:
                logger.error(
                    "halt_and_alert FAILED: %s: %s — "
                    "operator must investigate manually",
                    type(halt_exc).__name__, halt_exc,
                )
            # Re-raise as a sentinel so the caller's outer try/except
            # converts the result dict to ERROR (vs the prior silent
            # swallow that left the broker holding fills the journal
            # didn't reflect — the `broker_orphan` class observed on
            # the EXP-A2 NVDA strangle).
            raise _AtomicPlacementBreach(
                f"log_trade failed for leg {i} of {strategy.name}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
