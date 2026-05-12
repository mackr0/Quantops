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


def _signed_delta_dollars_for_position(
    pos: Dict[str, Any],
    spot: Optional[float],
    iv: Optional[float],
    today: _date,
) -> float:
    """Signed delta-equivalent $ contribution for one position.

    Stocks: qty × price (signed by qty).
    Options: contrib['delta'] × spot, where contrib['delta'] is
        already qty-signed (long call positive, short call negative).

    Returns 0.0 for un-pricable inputs.
    """
    qty = float(pos.get("qty") or 0)
    if qty == 0:
        return 0.0

    if not _is_option_position(pos):
        price = pos.get("current_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            price = spot
        if price is None or price <= 0:
            return 0.0
        return qty * price  # signed

    parsed = _parse_option_position(pos)
    if parsed is None or spot is None or spot <= 0:
        return 0.0
    iv_eff = iv if (iv is not None and iv > 0) else FALLBACK_IV
    contrib = _greek_contribution(parsed, spot, iv_eff, today=today)
    if contrib is None:
        return 0.0
    return contrib["delta"] * spot  # signed (delta sign + qty sign)


def _default_iv_lookup_factory() -> Callable[[str], Optional[float]]:
    """Phase 6c (2026-05-12): cached per-call IV lookup using the
    live options oracle. Returns a callable that resolves an
    underlying ticker to its ATM call IV (annualized decimal,
    e.g. 0.35 = 35%).

    The lookup hits `options_oracle.get_options_oracle` which is
    a chain-fetch — caches per-call so multiple positions on the
    same underlying don't re-fetch. Returns None on any failure;
    callers fall back to `FALLBACK_IV=0.25`.

    Without this wiring (the Phase 6b state), every option
    position's delta-adjusted exposure used the 25% fallback,
    regardless of whether the underlying was actually trading
    at 60% IV (overpriced, near earnings) or 15% IV (quiet name).
    """
    _cache: Dict[str, Optional[float]] = {}

    def lookup(underlying: str) -> Optional[float]:
        if not underlying:
            return None
        if underlying in _cache:
            return _cache[underlying]
        try:
            from options_oracle import get_options_oracle
            oracle = get_options_oracle(underlying)
            if not oracle or not oracle.get("has_options"):
                _cache[underlying] = None
                return None
            # Skew dict has call_iv (ATM call IV, annualized
            # decimal). Use that as the effective IV for delta
            # calc — close enough for portfolio aggregation.
            iv = float(oracle.get("skew", {}).get("call_iv") or 0)
            if iv <= 0:
                _cache[underlying] = None
                return None
            _cache[underlying] = iv
            return iv
        except Exception:
            _cache[underlying] = None
            return None

    return lookup


def signed_portfolio_delta_exposure(
    positions: List[Dict[str, Any]],
    price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
) -> Dict[str, float]:
    """Like `portfolio_delta_exposure` but preserves SIGN.

    Used by `effective_positions_for_risk_model` to feed the
    factor-regression model (which needs signed weights so a long
    AAPL stock and a short AAPL call partially offset rather than
    doubling up).
    """
    today = today or _date.today()
    out: Dict[str, float] = {}

    for pos in positions or []:
        qty = float(pos.get("qty") or 0)
        if qty == 0:
            continue

        if _is_option_position(pos):
            parsed = _parse_option_position(pos)
            if parsed is None:
                continue
            underlying = parsed["underlying"]
        else:
            underlying = pos.get("symbol", "")
            if not underlying:
                continue

        spot = None
        if price_lookup:
            try:
                spot = price_lookup(underlying)
            except Exception:
                spot = None
        if spot is None or spot <= 0:
            cp = pos.get("current_price")
            try:
                spot = float(cp) if cp is not None else None
            except (TypeError, ValueError):
                spot = None

        iv = None
        if _is_option_position(pos) and iv_lookup:
            try:
                iv = iv_lookup(underlying)
            except Exception:
                iv = None

        contribution = _signed_delta_dollars_for_position(
            pos, spot=spot, iv=iv, today=today,
        )
        if contribution != 0:
            out[underlying] = out.get(underlying, 0.0) + contribution

    return out


def effective_positions_for_risk_model(
    positions: List[Dict[str, Any]],
    price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
    use_live_iv: bool = True,
) -> List[Dict[str, Any]]:
    """Convert raw positions into synthetic underlying-bucket positions
    suitable for the factor-regression risk model.

    Each output dict has:
      - symbol: the UNDERLYING ticker (option positions roll up here)
      - market_value: signed delta-equivalent $ exposure
      - n_legs: count of underlying legs that contributed (for
        debugging / display)

    The factor-regression model in `portfolio_risk_model.
    compute_portfolio_risk_from_positions` consumes this list in
    place of raw positions — replaces audit-finding-#7's broken
    behavior where option positions either contributed
    premium-based weight (under-counting risk by ~10×) or were
    silently dropped (when their OCC symbol had no bars).
    """
    # Phase 6c (2026-05-12): when caller doesn't provide an explicit
    # iv_lookup and the new use_live_iv flag is True (default),
    # build a per-call cached lookup hitting the options_oracle.
    # Falls back to FALLBACK_IV (0.25) inside the math if the
    # lookup returns None for any underlying.
    if iv_lookup is None and use_live_iv:
        iv_lookup = _default_iv_lookup_factory()
    signed = signed_portfolio_delta_exposure(
        positions, price_lookup=price_lookup,
        iv_lookup=iv_lookup, today=today,
    )
    # Per-underlying leg counts for visibility
    leg_counts: Dict[str, int] = {}
    for pos in positions or []:
        qty = float(pos.get("qty") or 0)
        if qty == 0:
            continue
        if _is_option_position(pos):
            parsed = _parse_option_position(pos)
            underlying = parsed["underlying"] if parsed else None
        else:
            underlying = pos.get("symbol", "")
        if underlying:
            leg_counts[underlying] = leg_counts.get(underlying, 0) + 1
    return [
        {
            "symbol": sym,
            "market_value": mv,
            "n_legs": leg_counts.get(sym, 0),
        }
        for sym, mv in signed.items()
    ]


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
