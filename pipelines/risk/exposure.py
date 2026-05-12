"""Delta-adjusted portfolio exposure (Phase 6 of pipeline refactor).

The bug this closes — audit finding #7:

  Today the portfolio risk model treats every position as
  "weight = qty × price / portfolio_value". That's correct for
  stocks (a 100-share AAPL position at $150 IS $15,000 of AAPL
  exposure). It's WRONG for options. A long call worth $200 in
  premium, with delta = 0.4 on a $50 underlying with qty=1
  contract, has the directional risk of:

    delta-equivalent shares = 0.4 × 1 × 100 = 40 shares
    delta-equivalent $      = 40 × $50      = $2,000

  Treating that position as $200 of exposure under-states its
  contribution to portfolio factor regressions by 10×. Conversely,
  for short premium positions, the bug HIDES the structural
  short-vol exposure that's the actual risk.

Phase 6a (this commit) provides the pure functions. Phase 6b wires
them into `portfolio_risk_model.compute_portfolio_risk` so the
factor regressions see delta-equivalent weights.

Functions:
  - `delta_adjusted_position_value(pos, spot, iv, today)`
        Pure: one position's effective $ exposure.
  - `portfolio_delta_exposure(positions, price_lookup, iv_lookup)`
        Sums per-position values into a {symbol: $exposure} dict
        keyed by underlying (option positions roll up under their
        underlying — a long AAPL call sits in the same bucket as
        an AAPL stock position).
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Callable, Dict, List, Optional


# Reuse the OCC-symbol detection + Greeks contribution from the
# existing aggregator. This module is the per-pipeline namespace
# wrapper, not a reimplementation.
from options_greeks_aggregator import (
    _greek_contribution,
    _is_option_position,
    _parse_option_position,
    FALLBACK_IV,
)


def delta_adjusted_position_value(
    pos: Dict[str, Any],
    spot: Optional[float],
    iv: Optional[float] = None,
    today: Optional[_date] = None,
) -> float:
    """Return one position's delta-equivalent dollar exposure.

    Stocks: qty × price.
    Options: |delta × qty × 100 × spot|. We take absolute value
        because the SIGN of the exposure is captured separately
        in the Greeks aggregation (net_delta, etc.); for the
        `weights` input to a factor-regression model, what matters
        is the magnitude of the position relative to the book.

    Returns 0.0 for any input the function can't price (missing
    spot, expired option, malformed OCC symbol). Never raises.
    """
    qty = float(pos.get("qty") or 0)
    if qty == 0:
        return 0.0

    if not _is_option_position(pos):
        # Stock: straightforward market value.
        # Prefer current_price (broker-shipped); fall back to spot.
        price = pos.get("current_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            price = spot
        if price is None or price <= 0:
            return 0.0
        return abs(qty * price)

    # Option position: need delta × spot × |qty| × 100.
    parsed = _parse_option_position(pos)
    if parsed is None or spot is None or spot <= 0:
        return 0.0
    today = today or _date.today()
    iv_eff = iv if (iv is not None and iv > 0) else FALLBACK_IV
    contrib = _greek_contribution(parsed, spot, iv_eff, today=today)
    if contrib is None:
        return 0.0
    # contrib["delta"] is already qty × 100 × per-share-delta (signed).
    # Effective $ exposure is |delta_shares × spot|.
    delta_shares = contrib["delta"]
    return abs(delta_shares * spot)


def portfolio_delta_exposure(
    positions: List[Dict[str, Any]],
    price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
) -> Dict[str, float]:
    """Aggregate delta-adjusted exposure across the book.

    Returns a `{underlying_symbol: total_$exposure}` dict — option
    positions roll up under their UNDERLYING ticker so a long AAPL
    call shares a bucket with an AAPL stock position. This matches
    how the factor model wants weights (factor exposures are
    per-underlying, not per-contract).

    No external calls without lookups; safe to use in tests with
    `price_lookup=lambda s: prices[s]`.
    """
    today = today or _date.today()
    out: Dict[str, float] = {}

    for pos in positions or []:
        qty = float(pos.get("qty") or 0)
        if qty == 0:
            continue

        # Determine the underlying ticker for bucket aggregation.
        if _is_option_position(pos):
            parsed = _parse_option_position(pos)
            if parsed is None:
                continue
            underlying = parsed["underlying"]
        else:
            underlying = pos.get("symbol", "")
            if not underlying:
                continue

        # Get spot for the underlying.
        spot = None
        if price_lookup:
            try:
                spot = price_lookup(underlying)
            except Exception:
                spot = None
        if spot is None or spot <= 0:
            # Stock fallback: use the position's own current_price
            cp = pos.get("current_price")
            try:
                spot = float(cp) if cp is not None else None
            except (TypeError, ValueError):
                spot = None
        # spot may still be None for an option position with no
        # underlying lookup — delta_adjusted_position_value handles
        # that by returning 0.0.

        # Get IV for option positions only.
        iv = None
        if _is_option_position(pos) and iv_lookup:
            try:
                iv = iv_lookup(underlying)
            except Exception:
                iv = None

        contribution = delta_adjusted_position_value(
            pos, spot=spot, iv=iv, today=today,
        )
        if contribution > 0:
            out[underlying] = out.get(underlying, 0.0) + contribution

    return out
