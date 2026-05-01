"""Phase A1 of OPTIONS_PROGRAM_PLAN.md — book-level Greeks aggregator.

Walks every open position (options + underlying stock) and returns
the net portfolio Greeks. This is foundational: every later phase
(exposure gates, multi-leg, hedging) depends on knowing the book's
current delta / gamma / vega / theta.

Per-position contribution:
  Stock:    delta = qty * 1, gamma=vega=theta=0
  Option:   delta = compute_greeks(...).delta * qty * 100  (signed by side)
            gamma = compute_greeks(...).gamma * qty * 100
            vega  = compute_greeks(...).vega * qty * 100
            theta = compute_greeks(...).theta * qty * 100
            (qty is signed: long = +, short = -; the multiplier is per-share)

Exposed for two consumers:
  1. Greeks exposure gates (Phase A2) — block trades that push past mandate
  2. Dashboard panel (Phase A3) — visibility on book risk

The IV used per-contract comes from the options oracle when available,
falling back to a profile-default (0.25 = 25%) so the aggregator never
crashes on missing IV. A separate `iv_source` field per leg flags
whether the value is fresh-from-chain or fallback.
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Fallback IV when the oracle can't provide a fresh quote. 0.25 (25%)
# is roughly the median equity IV; using it keeps the aggregator
# directionally correct even when chain data is stale.
FALLBACK_IV = 0.25


def _is_option_position(pos: Dict[str, Any]) -> bool:
    """True if this position is an option (has occ_symbol or
    21-char OCC-style symbol field)."""
    occ = pos.get("occ_symbol") or pos.get("symbol", "")
    if not isinstance(occ, str):
        return False
    # OCC symbols are exactly 21 chars: 6-char root padded + 6-digit date +
    # 1 right + 8-digit strike. Stock symbols are <=6 chars no spaces.
    if len(occ) == 21 and (occ[12] in ("C", "P", "c", "p")):
        return True
    return False


def _parse_option_position(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the fields needed for Greeks from a position row.

    Accepts both:
      - "rich" position dicts that already have occ_symbol /
        option_strategy / expiry / strike (from our journal layer)
      - "raw" Alpaca positions where the symbol IS the OCC
    """
    from options_trader import parse_occ_symbol
    occ = pos.get("occ_symbol") or pos.get("symbol")
    if not occ:
        return None
    try:
        parsed = parse_occ_symbol(occ)
    except Exception:
        return None
    return {
        "occ_symbol": occ,
        "underlying": parsed["underlying"],
        "expiry": parsed["expiry"],
        "strike": parsed["strike"],
        "right": parsed["right"],
        "qty": float(pos.get("qty") or 0),
    }


def _greek_contribution(
    occ_position: Dict[str, Any],
    spot_price: float,
    iv: float,
    today: Optional[_date] = None,
) -> Optional[Dict[str, Any]]:
    """Compute one option leg's contribution to portfolio Greeks.

    Returns {delta, gamma, vega, theta, rho, price} where each value
    is the signed dollar/share contribution (qty * 100 * per-share-greek).
    None when inputs are invalid.
    """
    from options_trader import compute_greeks
    today = today or _date.today()
    expiry = occ_position["expiry"]
    days = (expiry - today).days
    if days <= 0:
        # Already expired — no contribution. Lifecycle sweep will close.
        return None
    qty = occ_position["qty"]
    if qty == 0:
        return None
    is_call = occ_position["right"] == "C"
    g = compute_greeks(
        spot=spot_price, strike=occ_position["strike"],
        days_to_expiry=days, iv=iv, is_call=is_call,
    )
    if g is None:
        return None

    # Multiplier: 100 shares per contract. qty already signed (long+, short-).
    mult = qty * 100.0
    return {
        "occ_symbol": occ_position["occ_symbol"],
        "underlying": occ_position["underlying"],
        "qty": qty,
        "spot": spot_price,
        "iv": iv,
        "days_to_expiry": days,
        "delta": g["delta"] * mult,
        "gamma": g["gamma"] * mult,
        "vega": g["vega"] * mult,
        "theta": g["theta"] * mult,
        "rho": g["rho"] * mult,
        "price": g["price"],
        "market_value": g["price"] * mult,
    }


def compute_book_greeks(
    positions: List[Dict[str, Any]],
    price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
) -> Dict[str, Any]:
    """Aggregate Greeks across an entire position book.

    Args:
        positions: list of position dicts (mixed stock + options).
        price_lookup: callable(underlying_symbol) → spot. Falls back
            to position's own current_price field if lookup fails.
        iv_lookup: callable(underlying_symbol) → IV (annualized fraction).
            Falls back to FALLBACK_IV if lookup returns None.
        today: override for testing.

    Returns a summary dict:
      {
        "net_delta": float,        # share-equivalent delta
        "net_gamma": float,
        "net_vega": float,         # $ per 1-vol-point move
        "net_theta": float,        # $ per day (negative = decay)
        "net_rho": float,
        "n_options_legs": int,
        "n_stock_positions": int,
        "by_leg": [ ... per-leg contributions ... ],
        "stock_delta": float,      # underlying-stock delta only
        "options_delta": float,    # options-only delta
        "fallback_iv_count": int,  # how many legs used the fallback
        "expired_skipped": int,    # legs already expired
      }
    """
    today = today or _date.today()
    summary: Dict[str, Any] = {
        "net_delta": 0.0, "net_gamma": 0.0, "net_vega": 0.0,
        "net_theta": 0.0, "net_rho": 0.0,
        "n_options_legs": 0, "n_stock_positions": 0,
        "stock_delta": 0.0, "options_delta": 0.0,
        "by_leg": [],
        "fallback_iv_count": 0, "expired_skipped": 0,
    }

    for pos in positions or []:
        sym = pos.get("symbol", "")
        qty = float(pos.get("qty") or 0)
        if qty == 0:
            continue

        if _is_option_position(pos):
            parsed = _parse_option_position(pos)
            if parsed is None:
                continue

            # Get spot for the underlying
            spot = None
            if price_lookup:
                try:
                    spot = price_lookup(parsed["underlying"])
                except Exception as exc:
                    logger.debug("price_lookup(%s) raised: %s",
                                 parsed["underlying"], exc)
            if spot is None or spot <= 0:
                # Last fallback: position's own current_price (Alpaca
                # ships this for option positions)
                cp = pos.get("current_price")
                if cp:
                    spot = float(cp)
            if spot is None or spot <= 0:
                logger.debug("No spot for %s — skipping leg", parsed["underlying"])
                continue

            # Get IV
            iv = None
            if iv_lookup:
                try:
                    iv = iv_lookup(parsed["underlying"])
                except Exception:
                    iv = None
            if iv is None or iv <= 0:
                iv = FALLBACK_IV
                summary["fallback_iv_count"] += 1

            contrib = _greek_contribution(parsed, spot, iv, today=today)
            if contrib is None:
                if (parsed["expiry"] - today).days <= 0:
                    summary["expired_skipped"] += 1
                continue

            summary["n_options_legs"] += 1
            summary["net_delta"] += contrib["delta"]
            summary["net_gamma"] += contrib["gamma"]
            summary["net_vega"] += contrib["vega"]
            summary["net_theta"] += contrib["theta"]
            summary["net_rho"] += contrib["rho"]
            summary["options_delta"] += contrib["delta"]
            summary["by_leg"].append(contrib)
        else:
            # Stock position: delta = qty (signed), other Greeks 0
            summary["n_stock_positions"] += 1
            summary["net_delta"] += qty
            summary["stock_delta"] += qty

    # Round for display sanity (full precision retained internally
    # would surface through float math noise on the dashboard)
    for k in ("net_delta", "net_gamma", "net_vega", "net_theta",
              "net_rho", "stock_delta", "options_delta"):
        summary[k] = round(summary[k], 4)

    return summary


def render_greeks_for_prompt(summary: Dict[str, Any]) -> str:
    """Compact one-block summary for inclusion in the AI batch prompt.

    Returns empty string when the book has no exposure to surface.
    """
    if not summary:
        return ""
    if summary["n_options_legs"] == 0 and summary["n_stock_positions"] == 0:
        return ""
    lines = ["BOOK GREEKS (net exposure across all positions):"]
    lines.append(
        f"  Delta: {summary['net_delta']:+,.0f} share-eq  "
        f"(stock {summary['stock_delta']:+,.0f}, "
        f"options {summary['options_delta']:+,.0f})"
    )
    if summary["n_options_legs"] > 0:
        lines.append(
            f"  Gamma: {summary['net_gamma']:+,.2f}  "
            f"Vega: {summary['net_vega']:+,.0f}/vol-pt  "
            f"Theta: {summary['net_theta']:+,.0f}/day"
        )
        if summary["fallback_iv_count"] > 0:
            lines.append(
                f"  Note: {summary['fallback_iv_count']} leg(s) used "
                f"fallback IV {FALLBACK_IV*100:.0f}% (chain data missing)"
            )
    return "\n".join(lines)
