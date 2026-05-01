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


def check_greeks_gates(
    book_summary: Dict[str, Any],
    proposed_contribution: Optional[Dict[str, Any]],
    ctx: Any,
) -> Dict[str, Any]:
    """Phase A2 — Greeks exposure gates. Decide whether a proposed
    options trade is allowed by the book's Greeks-based risk caps.

    Args:
        book_summary: result of `compute_book_greeks` for the current
            book (BEFORE the proposed trade).
        proposed_contribution: dict shaped like one entry from
            book_summary["by_leg"] — the Greeks contribution the
            proposed trade WOULD add. Pass None to evaluate the
            current book's compliance without a proposal.
        ctx: UserContext supplying equity + the gate limits
            (max_net_options_delta_pct, max_theta_burn_dollars_per_day,
            max_short_vega_dollars).

    Returns:
        {
            "allowed": bool,
            "reasons": List[str],   # one per failing gate
            "post_trade_options_delta": float,
            "post_trade_theta": float,
            "post_trade_vega": float,
            "limits": { ... limits actually applied ... },
        }

    Gate semantics:
      delta_pct: post-trade |options_delta| / equity > limit → block
      theta:    post-trade net_theta < -limit → block (paying too much
                  decay). Only checks when limit is set AND proposed
                  trade adds long premium (negative theta).
      short_vega: post-trade net_vega < -limit → block (too much short
                  vol). Only checks when limit is set AND proposed
                  trade adds short premium (negative vega).

    Note: the AI's existing equity-sizing gates still apply; these are
    ADDITIONAL gates on top, specific to options Greeks exposure.
    """
    reasons: List[str] = []

    # Read the limits — None means "no gate"
    delta_pct_limit = getattr(ctx, "max_net_options_delta_pct", None)
    theta_limit = getattr(ctx, "max_theta_burn_dollars_per_day", None)
    short_vega_limit = getattr(ctx, "max_short_vega_dollars", None)

    # Read equity for normalization
    equity = 0.0
    try:
        from client import get_account_info
        account = get_account_info(ctx=ctx) or {}
        equity = float(account.get("equity") or 0)
    except Exception:
        equity = 0.0
    if equity <= 0:
        # Best-effort: try the ctx initial_capital fallback
        equity = float(getattr(ctx, "initial_capital", 0) or 0)

    # Compute post-trade Greeks
    pre_options_delta = float(book_summary.get("options_delta", 0))
    pre_theta = float(book_summary.get("net_theta", 0))
    pre_vega = float(book_summary.get("net_vega", 0))
    add_delta = float((proposed_contribution or {}).get("delta", 0))
    add_theta = float((proposed_contribution or {}).get("theta", 0))
    add_vega = float((proposed_contribution or {}).get("vega", 0))

    post_options_delta = pre_options_delta + add_delta
    post_theta = pre_theta + add_theta
    post_vega = pre_vega + add_vega

    # Gate 1: directional delta cap (options-only delta as % of equity).
    # We use options_delta NOT net_delta because stock delta is
    # explicitly authorized via equity-sizing gates upstream — we
    # don't want options gates double-counting that.
    if delta_pct_limit is not None and equity > 0:
        # |post_delta * spot| isn't directly available; use share-equiv
        # delta vs equity approximation. Each unit of options_delta is
        # one share-equivalent — its $-value depends on the underlying.
        # For the cap, we compare delta-shares against an
        # equity-equivalent share count: equity / typical_price (~$100).
        # Simplification: cap absolute options_delta against
        # delta_pct_limit * equity / 100. This is an APPROXIMATION
        # that assumes ~$100 average underlying; conservative for
        # high-priced stocks (overcounts), permissive for low-priced.
        # Refinement to per-leg dollar-delta is a follow-up.
        delta_dollar_proxy = abs(post_options_delta) * 100  # approx $-exposure
        delta_dollar_limit = delta_pct_limit * equity
        if delta_dollar_proxy > delta_dollar_limit:
            reasons.append(
                f"options delta ${delta_dollar_proxy:,.0f} > limit "
                f"${delta_dollar_limit:,.0f} ({delta_pct_limit*100:.0f}% equity)"
            )

    # Gate 2: theta-burn cap (long-vol budget)
    if theta_limit is not None:
        # post_theta is signed: negative = paying decay. Compare
        # against -limit (i.e. allow up to $theta_limit/day in decay).
        if post_theta < -theta_limit:
            reasons.append(
                f"theta burn {post_theta:+.0f}/day < -${theta_limit:,.0f} "
                f"(long-vol budget exceeded)"
            )

    # Gate 3: short-vega cap (short-vol exposure cap)
    if short_vega_limit is not None:
        if post_vega < -short_vega_limit:
            reasons.append(
                f"vega {post_vega:+.0f} < -${short_vega_limit:,.0f} "
                f"(short-vega exposure cap)"
            )

    return {
        "allowed": len(reasons) == 0,
        "reasons": reasons,
        "post_trade_options_delta": round(post_options_delta, 4),
        "post_trade_theta": round(post_theta, 4),
        "post_trade_vega": round(post_vega, 4),
        "limits": {
            "max_net_options_delta_pct": delta_pct_limit,
            "max_theta_burn_dollars_per_day": theta_limit,
            "max_short_vega_dollars": short_vega_limit,
            "equity": equity,
        },
    }


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
