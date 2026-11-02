"""Marketable-limit pricing for entries (P2.2 step 2, 2026-11-01).

Entries went to the broker as MARKET orders. On 2,305 stock entries
since the 2026-08-24 reset the median fill was fine (+1.6 bps adverse
against a 3.9 bps median quoted spread) but the tail was not: p90
+26 bps, 47% of fills worse than the far touch quoted at decision,
$43,025 of gross slippage in ten weeks across twelve $250K books —
market orders filling through thin books in fast moments, not size
(91% of fills were under 0.1% of ADV). A marketable limit — the far
touch plus a small buffer — fills the same way in the normal case and
caps the tail: the order cannot pay more than the limit, and one that
does not fill in five minutes is canceled by the stale-order task
(the next cycle re-decides from fresh data).

Scope: stock ENTRIES (buy / short brackets) and multi-leg option
spreads. Exits, covers, protective stops and rollback reversals stay
MARKET — an unfilled exit is exposure, an unfilled entry is a missed
trade.

Fail-closed rule: when no two-sided NBBO is available the caller
falls back to a market order and says so in the journal row's reason
— a quote outage must not stop trading; it merely loses the cap for
that order. Multi-leg spreads never go to market: with no net price to
derive a limit from, the spread is skipped.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

#: Buffer beyond the far touch, in basis points. Small enough that the
#: typical fill is unchanged (median adverse slippage was +1.6 bps),
#: large enough to absorb a tick of movement between quote and arrival.
MARKETABLE_BUFFER_BPS = 10.0

#: Concession toward fillability for a multi-leg net limit: a fraction
#: of the mid net premium, never less than two cents per share.
MULTILEG_CONCESSION_PCT = 0.05
MULTILEG_MIN_CONCESSION = 0.02


def _round_price(px: float) -> float:
    """Alpaca accepts sub-penny limits only under $1."""
    return round(px, 4) if px < 1.0 else round(px, 2)


def marketable_limit_price(side: str, bid: float, ask: float,
                           buffer_bps: float = MARKETABLE_BUFFER_BPS) -> float:
    """Far touch plus buffer: buys pay up to ask×(1+b), sells / shorts
    accept down to bid×(1−b). Pure arithmetic; callers validate the
    quote (two-sided, uncrossed) first."""
    b = max(0.0, float(buffer_bps)) / 10_000.0
    if str(side).lower() in ("buy", "cover", "buy_to_open", "buy_to_close"):
        return _round_price(float(ask) * (1.0 + b))
    return _round_price(float(bid) * (1.0 - b))


def marketable_entry(symbol: str, side: str, decision_price: float,
                     nbbo: Optional[Dict[str, Any]] = None,
                     ) -> Optional[Dict[str, Any]]:
    """The order shape for a stock entry: {order_type, limit_price,
    bid, ask, spread_bps, buffer_bps} when a two-sided NBBO exists,
    else None (caller sends a market order and records why). `nbbo`
    may be passed in (one fetch per entry serves the limit price, the
    slippage estimator and the journal's spread stamp)."""
    if nbbo is None:
        try:
            from market_data import get_nbbo_spread_bps
            nbbo = get_nbbo_spread_bps(symbol)
        except Exception:
            nbbo = None
    if not nbbo:
        return None
    try:
        bid, ask = float(nbbo["bid"]), float(nbbo["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return {
        "order_type": "limit",
        "limit_price": marketable_limit_price(side, bid, ask),
        "bid": bid, "ask": ask,
        "spread_bps": float(nbbo.get("spread_bps") or
                            (ask - bid) / ((ask + bid) / 2) * 10_000),
        "buffer_bps": MARKETABLE_BUFFER_BPS,
    }


def multileg_limit_from_net(net_premium_per_contract: Optional[float],
                            concession_pct: float = MULTILEG_CONCESSION_PCT,
                            min_concession: float = MULTILEG_MIN_CONCESSION,
                            ) -> Optional[float]:
    """Signed net limit for a spread from its mid net premium (+ debit,
    − credit): pay a little more than mid for a debit, accept a little
    less than mid for a credit. None when there is no net price — the
    caller then SKIPS rather than sending the spread to market."""
    if net_premium_per_contract is None:
        return None
    try:
        net = float(net_premium_per_contract)
    except (TypeError, ValueError):
        return None
    if net == 0:
        return None
    concession = max(min_concession, abs(net) * concession_pct)
    # Toward fillability in both cases: a debit grows, a credit shrinks
    # in magnitude (moves toward zero).
    limit = net + concession
    if net < 0 and limit >= 0:
        limit = -min_concession   # never flip a credit spread into a debit
    return round(limit, 2)
