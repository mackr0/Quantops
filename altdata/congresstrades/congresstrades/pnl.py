"""P&L estimation from STOCK Act disclosures.

Disclosures report transaction amounts as RANGES, not exact values
($1,001-$15,000, $15,001-$50,000, etc.). You can never get exact dollar
P&L from the disclosure alone — the uncertainty bar is the range width.

What this module CAN compute:

  1. FIFO lot matching — per (member, ticker), walk trades in time
     order and match buys to sells. Identifies closed round-trips
     (both sides disclosed) vs still-open positions.
  2. Realized P&L (closed round-trips) — using range midpoints for
     position size + actual ticker price movement between buy/sell
     dates → estimated dollar P&L with a confidence band.
  3. Unrealized P&L (open positions) — using midpoint × (current
     price / buy-date price - 1). Current = last known close.
  4. Member performance summary — realized + unrealized + vs-SPY
     comparison over the analysis window.

Uncertainty model:
  - The true dollar P&L for a single trade lies in a BAND defined by
    range_low × return_pct to range_high × return_pct.
  - We report (low_bound, midpoint_estimate, high_bound) everywhere.
  - Aggregating across many trades narrows the band in relative terms
    (noise averages out) but widens in absolute — we expose both.

This module has ZERO network I/O. The price_series parameter on
compute_pnl_for_member() is a plain dict of {ticker: pandas.Series of
daily closes}, supplied by the caller. Keeps tests hermetic.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Lot model — mutable FIFO queue entries
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    """A single buy-side position that can be partially or fully sold."""
    symbol: str
    member: str
    buy_date: str
    amount_low: int
    amount_high: int       # both are remaining amounts after fills
    buy_price: Optional[float] = None   # ticker price on buy_date
    original_low: int = 0                # track original for ratio math
    original_high: int = 0

    def midpoint_remaining(self) -> float:
        return (self.amount_low + self.amount_high) / 2.0

    def is_open(self) -> bool:
        return self.amount_high > 0


@dataclass
class Roundtrip:
    """A closed buy→sell matched pair. Amounts are still ranges."""
    symbol: str
    member: str
    buy_date: str
    sell_date: str
    # Amounts on THIS matched slice (may be less than original lot size
    # if the sell only consumed part of the buy)
    slice_low: int
    slice_high: int
    buy_price: Optional[float]
    sell_price: Optional[float]
    return_pct: Optional[float] = None   # (sell_price/buy_price - 1), None if prices unknown

    @property
    def midpoint_amount(self) -> float:
        return (self.slice_low + self.slice_high) / 2.0

    def estimated_pnl(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Return (low_bound, midpoint, high_bound) dollar P&L for this slice.

        Math: P&L ≈ position_size × return_pct. Position size is in
        [slice_low, slice_high]; return_pct is observed from prices.
        None if we couldn't fetch prices for either side.
        """
        if self.return_pct is None:
            return (None, None, None)
        low = self.slice_low * self.return_pct
        mid = self.midpoint_amount * self.return_pct
        high = self.slice_high * self.return_pct
        if self.return_pct < 0:
            # When losing, the HIGHER position size means a BIGGER loss —
            # bounds flip in dollar terms (more negative is worse-bound).
            return (high, mid, low)
        return (low, mid, high)


@dataclass
class OpenPosition:
    """A still-held buy — estimates unrealized P&L against current price."""
    symbol: str
    member: str
    buy_date: str
    slice_low: int
    slice_high: int
    buy_price: Optional[float]
    current_price: Optional[float]

    @property
    def return_pct(self) -> Optional[float]:
        if self.buy_price is None or self.current_price is None:
            return None
        if self.buy_price <= 0:
            return None
        return self.current_price / self.buy_price - 1.0

    def estimated_pnl(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        r = self.return_pct
        if r is None:
            return (None, None, None)
        low = self.slice_low * r
        mid = ((self.slice_low + self.slice_high) / 2.0) * r
        high = self.slice_high * r
        if r < 0:
            return (high, mid, low)
        return (low, mid, high)


@dataclass
class MemberPerformance:
    """Aggregate P&L summary for one member over an analysis window."""
    member: str
    n_buys: int = 0
    n_sells: int = 0
    closed_roundtrips: List[Roundtrip] = field(default_factory=list)
    open_positions: List[OpenPosition] = field(default_factory=list)
    skipped_untracked: int = 0   # trades we couldn't resolve (no price, no match)

    def realized_bounds(self) -> Tuple[float, float, float]:
        """Sum of closed-roundtrip (low, mid, high) P&L estimates."""
        low = mid = high = 0.0
        for rt in self.closed_roundtrips:
            l, m, h = rt.estimated_pnl()
            if l is None:
                continue
            low += l
            mid += m
            high += h
        return (low, mid, high)

    def unrealized_bounds(self) -> Tuple[float, float, float]:
        low = mid = high = 0.0
        for op in self.open_positions:
            l, m, h = op.estimated_pnl()
            if l is None:
                continue
            low += l
            mid += m
            high += h
        return (low, mid, high)

    def total_bounds(self) -> Tuple[float, float, float]:
        rl, rm, rh = self.realized_bounds()
        ul, um, uh = self.unrealized_bounds()
        return (rl + ul, rm + um, rh + uh)

    def closed_win_rate(self) -> Optional[float]:
        """Fraction of closed round-trips with positive midpoint P&L."""
        with_prices = [rt for rt in self.closed_roundtrips
                       if rt.return_pct is not None]
        if not with_prices:
            return None
        wins = sum(1 for rt in with_prices if rt.return_pct > 0)
        return wins / len(with_prices)


# ---------------------------------------------------------------------------
# FIFO lot matcher — the core algorithm
# ---------------------------------------------------------------------------

def match_fifo_lots(
    trades: Iterable[Dict[str, Any]],
    price_at_date,   # callable (ticker, iso_date) -> Optional[float]
    current_price,   # callable (ticker) -> Optional[float]
) -> MemberPerformance:
    """Walk a single member's trades in time order; match buys→sells FIFO.

    `trades` must be sorted by transaction_date ascending, one member.
    Each trade dict needs: symbol, transaction_type, transaction_date,
    amount_low, amount_high.

    Returns MemberPerformance with closed_roundtrips + open_positions.
    """
    # Per-ticker FIFO queue of open lots
    lots: Dict[str, List[Lot]] = defaultdict(list)

    perf: MemberPerformance = MemberPerformance(member="")

    for t in trades:
        if not perf.member:
            perf.member = t.get("member_name") or ""
        sym = t.get("ticker")
        tx_type = (t.get("transaction_type") or "").lower()
        date = t.get("transaction_date")
        lo = t.get("amount_low") or 0
        hi = t.get("amount_high") or lo
        if not sym or not date or not (lo or hi):
            perf.skipped_untracked += 1
            continue

        if tx_type == "buy":
            perf.n_buys += 1
            lots[sym].append(Lot(
                symbol=sym, member=perf.member,
                buy_date=date,
                amount_low=int(lo), amount_high=int(hi),
                original_low=int(lo), original_high=int(hi),
                buy_price=price_at_date(sym, date),
            ))
        elif tx_type in ("sell", "partial_sale"):
            perf.n_sells += 1
            remaining_lo = int(lo)
            remaining_hi = int(hi)
            queue = lots[sym]
            sell_price = price_at_date(sym, date)
            while remaining_hi > 0 and queue:
                lot = queue[0]
                # Slice size is bounded by both sides — consume whichever
                # runs out first. Work in midpoint to decide consumption,
                # but carry the range bounds through.
                lot_mid = lot.midpoint_remaining()
                sell_mid = (remaining_lo + remaining_hi) / 2.0
                take_mid = min(lot_mid, sell_mid)
                if take_mid <= 0:
                    break
                # Proportional take on each bound
                take_ratio_lot = take_mid / lot_mid if lot_mid > 0 else 1.0
                slice_lot_low = int(lot.amount_low * take_ratio_lot)
                slice_lot_high = int(lot.amount_high * take_ratio_lot)
                # Build roundtrip slice (use lot side for amount bounds —
                # we're realizing ON the lot's cost basis)
                ret_pct = None
                if lot.buy_price and sell_price and lot.buy_price > 0:
                    ret_pct = sell_price / lot.buy_price - 1.0
                perf.closed_roundtrips.append(Roundtrip(
                    symbol=sym, member=perf.member,
                    buy_date=lot.buy_date, sell_date=date,
                    slice_low=slice_lot_low, slice_high=slice_lot_high,
                    buy_price=lot.buy_price, sell_price=sell_price,
                    return_pct=ret_pct,
                ))
                # Decrement
                lot.amount_low -= slice_lot_low
                lot.amount_high -= slice_lot_high
                if lot.amount_high <= 0:
                    queue.pop(0)
                # Sell side decrement (proportional)
                take_ratio_sell = take_mid / sell_mid if sell_mid > 0 else 1.0
                remaining_lo -= int(lo * take_ratio_sell)
                remaining_hi -= int(hi * take_ratio_sell)
                lo = remaining_lo
                hi = remaining_hi
        # Skip exchanges + others for now — they're rare and require
        # special handling we don't have yet
        else:
            perf.skipped_untracked += 1

    # After all trades, whatever's left in lots is still-open positions
    for sym, queue in lots.items():
        cur = current_price(sym)
        for lot in queue:
            if lot.amount_high > 0:
                perf.open_positions.append(OpenPosition(
                    symbol=sym, member=perf.member,
                    buy_date=lot.buy_date,
                    slice_low=lot.amount_low, slice_high=lot.amount_high,
                    buy_price=lot.buy_price,
                    current_price=cur,
                ))

    return perf


# ---------------------------------------------------------------------------
# Data loader — pulls trades from the sqlite store
# ---------------------------------------------------------------------------

def load_trades_for_member(
    conn: sqlite3.Connection,
    member_name: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    chamber: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Pull trades for one member, time-sorted, ready for FIFO matching."""
    clauses = ["member_name = ?"]
    args: List[Any] = [member_name]
    if chamber:
        clauses.append("chamber = ?")
        args.append(chamber)
    if start_date:
        clauses.append("transaction_date >= ?")
        args.append(start_date)
    if end_date:
        clauses.append("transaction_date <= ?")
        args.append(end_date)
    clauses.append("transaction_date IS NOT NULL")
    clauses.append("ticker IS NOT NULL")

    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT * FROM trades WHERE {where} "
        f"ORDER BY transaction_date ASC, id ASC",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def list_all_members(
    conn: sqlite3.Connection,
    chamber: Optional[str] = None,
    start_date: Optional[str] = None,
    min_trades: int = 3,
) -> List[str]:
    """Return members who have at least `min_trades` in the window.
    Members with tiny samples aren't worth analyzing individually."""
    clauses = []
    args: List[Any] = []
    if chamber:
        clauses.append("chamber = ?")
        args.append(chamber)
    if start_date:
        clauses.append("transaction_date >= ?")
        args.append(start_date)
    clauses.append("ticker IS NOT NULL")
    clauses.append("transaction_date IS NOT NULL")
    where = " AND ".join(clauses) if clauses else "1=1"
    args.append(min_trades)
    rows = conn.execute(
        f"SELECT member_name FROM trades WHERE {where} "
        f"GROUP BY member_name HAVING COUNT(*) >= ? "
        f"ORDER BY COUNT(*) DESC",
        args,
    ).fetchall()
    return [r["member_name"] for r in rows]
