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
