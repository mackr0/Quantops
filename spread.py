"""Multileg Spread class — groups Position objects into the spread
they collectively form, so the dashboard renders per-spread P&L
(capped at structural max loss) instead of the per-leg numbers that
showed -10100% on credit-side legs.

Why this exists
---------------
A bull put spread's max loss is bounded by `(strike_width - net_credit)`.
A bull call spread's max loss is the debit paid. Per-leg P&L on a
short option leg is meaningless when the partner long leg caps the
risk. Mack saw a PCG short call leg displayed as -10,100% (premium
went from $0.01 to $1.02 — a 100x move on the leg's $5 cost basis,
but the actual spread max loss is the debit paid).

This module:
  - `Spread` dataclass: a 2+ leg combo with shared `option_strategy`,
    `underlying`, and entry timestamp window.
  - `group_into_spreads(positions, journal_rows)`: groups a list of
    `Position` objects (option legs) by combo identity using the
    matching journal rows' `option_strategy` + `timestamp` proximity.
  - Per-spread P&L = sum of leg unrealized_pl, capped at the
    structural max loss for known spread types.

Phase 4 of the Position class refactor (2026-05-11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from position import Position


# Structural max-loss formulas per known spread type. Returns the
# absolute dollar max loss given the spread's entry economics.
# Each formula receives (legs: List[Position], strike_width: Optional[float])
# and returns the max-loss-cap dollar amount, OR None if the formula
# can't be applied (in which case display falls back to uncapped sum).
_SPREAD_MAX_LOSS = {
    # Bull call spread (debit): paid net debit, can't lose more than that.
    "bull_call_spread":
        lambda legs, w: _net_debit_dollars(legs),
    # Bear put spread (debit): same — debit paid is the cap.
    "bear_put_spread":
        lambda legs, w: _net_debit_dollars(legs),
    # Bull put spread (credit): max loss = (strike_width - net_credit).
    "bull_put_spread":
        lambda legs, w: _credit_spread_max_loss(legs, w),
    # Bear call spread (credit): same shape.
    "bear_call_spread":
        lambda legs, w: _credit_spread_max_loss(legs, w),
}


def _net_debit_dollars(legs: List[Position]) -> Optional[float]:
    """Sum signed leg costs at entry. Long legs cost (positive),
    short legs receive (negative). Total > 0 means net debit, < 0
    net credit. Returns abs(net) * 100 * qty for option contracts."""
    if not legs:
        return None
    total = 0.0
    for leg in legs:
        if not leg.is_option or leg.avg_entry_price is None:
            return None
        sign = 1 if leg.qty_signed > 0 else -1
        total += sign * leg.avg_entry_price * leg.abs_qty
    # Multiply by 100 (option contract multiplier)
    return abs(total) * 100


def _credit_spread_max_loss(legs: List[Position],
                             strike_width: Optional[float]) -> Optional[float]:
    """Max loss on a credit spread = (strike_width - net_credit) * 100 * qty."""
    if not strike_width or strike_width <= 0 or not legs:
        return None
    # Net credit per spread = sum of leg premiums (short premium - long premium)
    net = 0.0
    for leg in legs:
        if not leg.is_option or leg.avg_entry_price is None:
            return None
        # Short leg contributes positive premium received; long leg
        # contributes negative (premium paid).
        sign = -1 if leg.qty_signed > 0 else 1
        net += sign * leg.avg_entry_price
    if net <= 0:
        # Not a credit spread at entry (something off) — fall back.
        return None
    qty_per_spread = legs[0].abs_qty
    return (strike_width - net) * 100 * qty_per_spread


def _strike_width_from_legs(legs: List[Position]) -> Optional[float]:
    """Pull strike values from OCC strings: chars [-8:] / 1000."""
    strikes = []
    for leg in legs:
        if not leg.occ_symbol:
            return None
        try:
            s = int(leg.occ_symbol[-8:]) / 1000.0
            strikes.append(s)
        except Exception:
            return None
    if len(strikes) < 2:
        return None
    return abs(max(strikes) - min(strikes))


@dataclass
class Spread:
    """A multileg combo — 2+ Position objects that share an
    `option_strategy` name and were entered within a small time
    window."""
    strategy_name: str
    underlying: str
    legs: List[Position]
    # Entry timestamps span; for ordering on the dashboard.
    earliest_entry_ts: Optional[str] = None

    @property
    def qty(self) -> float:
        """Spread count = qty per leg (assumes ratio 1, true for all
        VERTICAL_SPREAD_BUILDERS shapes today)."""
        return self.legs[0].abs_qty if self.legs else 0

    @property
    def strike_width(self) -> Optional[float]:
        return _strike_width_from_legs(self.legs)

    @property
    def per_leg_unrealized_pl_sum(self) -> float:
        """Naive sum — what the macro currently displays as the
        spread total P&L. NOT capped at structural max loss."""
        return sum(leg.unrealized_pl for leg in self.legs)

    @property
    def structural_max_loss(self) -> Optional[float]:
        """Absolute dollar cap on this spread's loss. Returns None
        for unknown strategy names or insufficient data."""
        formula = _SPREAD_MAX_LOSS.get(self.strategy_name)
        if not formula:
            return None
        return formula(self.legs, self.strike_width)

    @property
    def display_unrealized_pl(self) -> float:
        """The number to show the user. Sum of per-leg unrealized
        P&L, but bounded by `-structural_max_loss` on the loss side
        (broker marks on illiquid OTM options can produce per-leg
        losses that exceed the spread's structural max loss; that's
        a fictional number and the display should show the truth)."""
        raw = self.per_leg_unrealized_pl_sum
        cap = self.structural_max_loss
        if cap is None or cap <= 0:
            return raw
        return max(raw, -cap)

    @property
    def display_unrealized_pl_pct(self) -> Optional[float]:
        """Percent of the spread's at-risk capital (the debit paid
        for debit spreads, or max loss for credit spreads)."""
        cap = self.structural_max_loss
        if cap is None or cap <= 0:
            return None
        return self.display_unrealized_pl / cap * 100


def group_into_spreads(positions: List[Position],
                        journal_rows: List[Dict[str, Any]],
                        timestamp_window_seconds: int = 60,
                        ) -> Tuple[List[Spread], List[Position]]:
    """Group option Position legs into the multileg combos they
    belong to.

    Args:
        positions: list of Position objects (mix of stock + options).
        journal_rows: trade rows from get_trade_history_for_profile
            for this profile, used to look up `option_strategy` +
            entry timestamp by OCC.
        timestamp_window_seconds: legs must share an entry window.

    Returns:
        (spreads, ungrouped) — spreads is the list of Spread objects;
        ungrouped is the list of Positions that didn't match any
        multileg group (stocks + single-leg options + orphans).
    """
    # Build OCC -> (option_strategy, timestamp) lookup from journal.
    occ_meta: Dict[str, Tuple[str, str]] = {}
    for r in journal_rows:
        occ = r.get("occ_symbol")
        strat = r.get("option_strategy")
        ts = r.get("timestamp")
        if occ and strat and ts and occ not in occ_meta:
            occ_meta[occ] = (strat, ts)

    # Group option positions by (strategy_name, underlying, ts_bucket)
    groups: Dict[Tuple[str, str, int], List[Position]] = {}
    ungrouped: List[Position] = []
    for pos in positions:
        if not pos.is_option or not pos.occ_symbol:
            ungrouped.append(pos)
            continue
        meta = occ_meta.get(pos.occ_symbol)
        if not meta:
            ungrouped.append(pos)
            continue
        strat, ts = meta
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_bucket = int(ts_dt.timestamp()
                            // timestamp_window_seconds)
        except Exception:
            ungrouped.append(pos)
            continue
        key = (strat, pos.underlying, ts_bucket)
        groups.setdefault(key, []).append(pos)

    spreads: List[Spread] = []
    for (strat, underlying, _bucket), legs in groups.items():
        if len(legs) < 2:
            # Single leg matched a multileg strategy name — orphan
            # (the partner leg was probably closed or expired).
            ungrouped.extend(legs)
            continue
        # Earliest entry timestamp among legs' OCC matches
        earliest = None
        for leg in legs:
            ts = occ_meta.get(leg.occ_symbol, (None, None))[1]
            if ts and (earliest is None or ts < earliest):
                earliest = ts
        spreads.append(Spread(
            strategy_name=strat,
            underlying=underlying,
            legs=legs,
            earliest_entry_ts=earliest,
        ))

    return spreads, ungrouped
