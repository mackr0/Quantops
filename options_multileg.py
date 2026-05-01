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
from typing import Any, Dict, List, Optional

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


def _alpaca_leg_dict(leg: OptionLeg, ratio: int = 1) -> Dict[str, Any]:
    """Convert an OptionLeg to Alpaca's `option_legs` array shape.

    Alpaca combo orders take legs as:
      {"symbol": OCC, "side": "buy"|"sell", "ratio_qty": int,
       "position_intent": "buy_to_open" / "sell_to_open" / etc.}

    `ratio` is the leg count multiplier. For verticals every leg has
    ratio 1 (1 long + 1 short = 1 spread). For ratio spreads the
    builder would set ratio differently.
    """
    intent_map = {
        "buy": "buy_to_open",
        "sell": "sell_to_open",
    }
    return {
        "symbol": leg.occ_symbol,
        "side": leg.side,
        "ratio_qty": int(ratio),
        "position_intent": intent_map.get(leg.side, "buy_to_open"),
    }


def execute_multileg_strategy(
    api,
    strategy: OptionStrategy,
    ctx,
    log: bool = True,
    use_combo: bool = True,
    limit_price: Optional[float] = None,
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
            combo_order = api.submit_order(**combo_kwargs)
            combo_id = getattr(combo_order, "id", None)
            # Combo orders return one parent id; child fills come via
            # api.list_orders(parent=combo_id) once filled.
            result.update({
                "action": "MULTILEG_OPEN",
                "leg_order_ids": [combo_id],
                "combo_order_id": combo_id,
                "reason": (
                    f"Submitted {strategy.name} on {strategy.underlying} "
                    f"as MLEG combo (parent={combo_id})"
                ),
            })
            if log and db_path:
                _log_strategy_legs(strategy, combo_id, ctx)
            return result
        except Exception as exc:
            logger.warning(
                "Combo-order path failed for %s on %s: %s. "
                "Falling back to sequential submission.",
                strategy.name, strategy.underlying, exc,
            )
            # Fall through to sequential path

    # Sequential fallback — submit each leg, rollback on failure
    submitted: List[Dict[str, Any]] = []
    for i, leg in enumerate(strategy.legs):
        try:
            order = api.submit_order(
                symbol=leg.occ_symbol,
                qty=leg.qty,
                side=leg.side,
                type="market",
                time_in_force="day",
            )
            submitted.append({
                "leg_index": i, "leg": leg,
                "order_id": getattr(order, "id", None),
            })
        except Exception as exc:
            logger.error(
                "Leg %d (%s %s) of %s failed: %s. Attempting rollback.",
                i, leg.side, leg.occ_symbol, strategy.name, exc,
            )
            # Rollback: try to close each successfully-submitted leg
            rollback_results = []
            for sub in submitted:
                try:
                    rev_side = "sell" if sub["leg"].side == "buy" else "buy"
                    rev = api.submit_order(
                        symbol=sub["leg"].occ_symbol,
                        qty=sub["leg"].qty,
                        side=rev_side,
                        type="market",
                        time_in_force="day",
                    )
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
    result.update({
        "action": "MULTILEG_OPEN",
        "leg_order_ids": leg_order_ids,
        "reason": (
            f"Submitted {len(submitted)} legs of {strategy.name} on "
            f"{strategy.underlying} sequentially (combo unavailable)"
        ),
    })
    if log and db_path:
        _log_strategy_legs(strategy, None, ctx,
                              leg_order_ids=leg_order_ids)
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


def _log_strategy_legs(strategy: OptionStrategy,
                          combo_order_id: Optional[str],
                          ctx,
                          leg_order_ids: Optional[List[str]] = None) -> None:
    """Write one journal row per leg, tagging them with the strategy
    name so the lifecycle sweep + dashboard can group them together.

    `signal_type=MULTILEG` and `option_strategy=<strategy.name>` make
    the legs queryable as a unit. `reason` includes the combo order id
    when available.
    """
    db_path = getattr(ctx, "db_path", None) if ctx else None
    if not db_path:
        return
    try:
        from journal import log_trade
    except Exception:
        return
    leg_order_ids = leg_order_ids or [combo_order_id] * len(strategy.legs)
    for i, leg in enumerate(strategy.legs):
        order_id = (leg_order_ids[i]
                    if i < len(leg_order_ids) else combo_order_id)
        try:
            log_trade(
                symbol=leg.underlying,
                side=leg.side,
                qty=leg.qty,
                order_id=order_id,
                signal_type="MULTILEG",
                strategy=strategy.name,
                reason=(
                    f"{strategy.name} leg {i+1}/{len(strategy.legs)} "
                    f"(combo={combo_order_id or 'sequential'})"
                ),
                ai_reasoning=strategy.thesis,
                occ_symbol=leg.occ_symbol,
                option_strategy=strategy.name,
                expiry=leg.expiry,
                strike=float(leg.strike),
                db_path=db_path,
            )
        except Exception as exc:
            logger.warning(
                "log_trade failed for leg %d of %s: %s",
                i, strategy.name, exc,
            )
