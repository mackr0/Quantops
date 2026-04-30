"""Options trading layer.

Item 1a of COMPETITIVE_GAP_PLAN.md. Equity-only profiles leave 30-40%
of obvious P&L on the table — protective puts on big positions
(downside hedge), covered calls on existing longs (income), and IV
mean-reversion (sell rich vol, buy cheap vol). All built on top of
the existing options_oracle (read-only IV/skew/term structure data).

This module provides:
  - Black-Scholes Greeks calculator
  - OCC option symbol formatter (the standard "AAPL250516C00150000" form)
  - Strategy builders that return position specs (defined-risk math)
  - Order submission via Alpaca's options endpoint (single-leg market/limit)

Strategies provided (Phase 1 — single-leg only):
  - long_put          — outright bearish or hedge
  - long_call         — outright bullish, defined risk
  - covered_call      — income on an existing long
  - cash_secured_put  — willing-buyer at lower price + premium

Multi-leg strategies (verticals, iron condors, calendars) are Phase 2;
they require Alpaca's `mleg` order class which differs from single-leg.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, date
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Risk-free rate proxy. Doesn't move enough day-to-day to matter for
# our IV/Greek calcs at the precision we need; revisit when adding
# longer-dated strategies. Pinned to recent 3-month T-bill yield.
DEFAULT_RISK_FREE_RATE = 0.045  # 4.5% annualized


# ---------------------------------------------------------------------------
# Black-Scholes Greeks
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard-normal CDF (no scipy dep)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def compute_greeks(
    spot: float,
    strike: float,
    days_to_expiry: float,
    iv: float,
    is_call: bool = True,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> Optional[Dict[str, float]]:
    """Black-Scholes Greeks + theoretical price.

    Args:
      spot:             underlying price
      strike:           option strike
      days_to_expiry:   calendar days to expiry (T = days/365)
      iv:               implied volatility, annualized fraction (0.25 = 25%)
      is_call:          True for call, False for put
      risk_free_rate:   annualized risk-free rate

    Returns dict with: price, delta, gamma, theta (per day), vega (per 1%
    vol move), rho (per 1% rate move). Returns None on invalid inputs.
    """
    if (spot is None or spot <= 0 or strike is None or strike <= 0
            or days_to_expiry is None or days_to_expiry <= 0
            or iv is None or iv <= 0):
        return None

    T = days_to_expiry / 365.0
    sqrt_T = math.sqrt(T)
    sigma_sqrt_T = iv * sqrt_T

    # Avoid div-by-zero when sigma * sqrt(T) is tiny
    if sigma_sqrt_T <= 1e-9:
        return None

    d1 = (math.log(spot / strike)
          + (risk_free_rate + 0.5 * iv * iv) * T) / sigma_sqrt_T
    d2 = d1 - sigma_sqrt_T

    pdf_d1 = _norm_pdf(d1)
    discount = math.exp(-risk_free_rate * T)

    if is_call:
        cdf_d1 = _norm_cdf(d1)
        cdf_d2 = _norm_cdf(d2)
        price = spot * cdf_d1 - strike * discount * cdf_d2
        delta = cdf_d1
        # Theta has TWO terms; convert from per-year to per-day.
        theta_year = (-spot * pdf_d1 * iv / (2.0 * sqrt_T)
                       - risk_free_rate * strike * discount * cdf_d2)
        rho = strike * T * discount * cdf_d2 / 100.0
    else:
        cdf_neg_d1 = _norm_cdf(-d1)
        cdf_neg_d2 = _norm_cdf(-d2)
        price = strike * discount * cdf_neg_d2 - spot * cdf_neg_d1
        delta = -cdf_neg_d1
        theta_year = (-spot * pdf_d1 * iv / (2.0 * sqrt_T)
                       + risk_free_rate * strike * discount * cdf_neg_d2)
        rho = -strike * T * discount * cdf_neg_d2 / 100.0

    gamma = pdf_d1 / (spot * sigma_sqrt_T)
    # Vega per 1% IV move (industry convention — divide by 100)
    vega = spot * pdf_d1 * sqrt_T / 100.0
    theta_day = theta_year / 365.0

    return {
        "price": round(price, 4),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta_day, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


# ---------------------------------------------------------------------------
# OCC option symbol formatter
# ---------------------------------------------------------------------------

def format_occ_symbol(
    underlying: str,
    expiry: date,
    strike: float,
    right: str,
) -> str:
    """Format an OCC-21 option symbol.

    Format: <root padded to 6><YYMMDD><C|P><strike × 1000 padded to 8>
    Example: AAPL  250516C00150000  → AAPL @ 2025-05-16 call $150

    Right is "C" for call or "P" for put (case-insensitive).
    """
    if not underlying or not expiry or strike is None or strike <= 0:
        raise ValueError("invalid OCC inputs")
    r = right.upper()
    if r not in ("C", "P"):
        raise ValueError(f"right must be 'C' or 'P', got {right!r}")
    # OCC root is 6 chars left-justified, padded with spaces. Some
    # systems strip the spaces — Alpaca accepts both. We use the
    # space-padded canonical form.
    root = underlying.upper().ljust(6)
    yymmdd = expiry.strftime("%y%m%d")
    # Strike × 1000, padded to 8 digits. $150.00 → 00150000.
    strike_int = int(round(float(strike) * 1000))
    strike_str = f"{strike_int:08d}"
    return f"{root}{yymmdd}{r}{strike_str}"


def parse_occ_symbol(occ: str) -> Dict[str, Any]:
    """Inverse of format_occ_symbol — extract underlying, expiry, strike, right."""
    if not occ or len(occ) < 21:
        raise ValueError(f"OCC symbol too short: {occ!r}")
    # 6-char root (may have trailing spaces), 6-char date, 1 char C/P,
    # 8-char strike × 1000.
    root = occ[:6].strip()
    yymmdd = occ[6:12]
    right = occ[12]
    strike_str = occ[13:21]
    expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    strike = int(strike_str) / 1000.0
    return {
        "underlying": root,
        "expiry": expiry,
        "strike": strike,
        "right": right,
    }


# ---------------------------------------------------------------------------
# Strategy primitives — return position specs (caller submits)
# ---------------------------------------------------------------------------

def build_long_put(
    underlying: str,
    expiry: date,
    strike: float,
    qty: int = 1,
    spot_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Outright long put — bearish bet OR downside hedge for an existing
    long stock position.

    Defined risk: max loss = premium paid. Max gain = strike - premium
    (bounded; max if stock goes to zero).
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    occ = format_occ_symbol(underlying, expiry, strike, "P")
    spec = {
        "strategy": "long_put",
        "occ_symbol": occ,
        "underlying": underlying.upper(),
        "expiry": expiry.isoformat(),
        "strike": strike,
        "right": "P",
        "qty": qty,
        "side": "buy",
        "intent": "open",
        "max_loss_per_contract": None,  # = premium paid (computed at fill)
    }
    if spot_price is not None and spot_price > 0:
        spec["moneyness_pct"] = round((strike - spot_price) / spot_price * 100, 2)
    return spec


def build_long_call(
    underlying: str,
    expiry: date,
    strike: float,
    qty: int = 1,
    spot_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Outright long call — bullish bet with defined max loss = premium."""
    if qty <= 0:
        raise ValueError("qty must be positive")
    occ = format_occ_symbol(underlying, expiry, strike, "C")
    spec = {
        "strategy": "long_call",
        "occ_symbol": occ,
        "underlying": underlying.upper(),
        "expiry": expiry.isoformat(),
        "strike": strike,
        "right": "C",
        "qty": qty,
        "side": "buy",
        "intent": "open",
        "max_loss_per_contract": None,
    }
    if spot_price is not None and spot_price > 0:
        spec["moneyness_pct"] = round((strike - spot_price) / spot_price * 100, 2)
    return spec


def build_covered_call(
    underlying: str,
    expiry: date,
    strike: float,
    shares_held: int,
    spot_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Covered call — sell a call against existing 100-share lots.

    qty is derived: 1 contract per 100 shares. Caps upside at strike,
    captures premium income. Use when expecting flat-to-modestly-up
    price action and IV is rich.
    """
    if shares_held < 100:
        raise ValueError(
            f"covered call needs ≥100 shares per contract; got {shares_held}"
        )
    qty = shares_held // 100
    occ = format_occ_symbol(underlying, expiry, strike, "C")
    spec = {
        "strategy": "covered_call",
        "occ_symbol": occ,
        "underlying": underlying.upper(),
        "expiry": expiry.isoformat(),
        "strike": strike,
        "right": "C",
        "qty": qty,
        "side": "sell",
        "intent": "open",
        "shares_covered": qty * 100,
    }
    if spot_price is not None and spot_price > 0:
        spec["moneyness_pct"] = round((strike - spot_price) / spot_price * 100, 2)
        # Capped upside if assigned: shares × (strike - spot) + premium
        spec["max_capped_upside_per_share"] = round(strike - spot_price, 2)
    return spec


def build_cash_secured_put(
    underlying: str,
    expiry: date,
    strike: float,
    qty: int = 1,
    spot_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Cash-secured put — sell a put while holding cash to cover assignment.

    Use when willing to own the stock at strike anyway (acquisition
    price = strike - premium received). Captures premium if not assigned.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    occ = format_occ_symbol(underlying, expiry, strike, "P")
    cash_required_per_contract = strike * 100  # 100 shares × strike
    spec = {
        "strategy": "cash_secured_put",
        "occ_symbol": occ,
        "underlying": underlying.upper(),
        "expiry": expiry.isoformat(),
        "strike": strike,
        "right": "P",
        "qty": qty,
        "side": "sell",
        "intent": "open",
        "cash_required": qty * cash_required_per_contract,
    }
    if spot_price is not None and spot_price > 0:
        spec["moneyness_pct"] = round((strike - spot_price) / spot_price * 100, 2)
    return spec


# ---------------------------------------------------------------------------
# Order submission via Alpaca's REST endpoint
# ---------------------------------------------------------------------------

def submit_option_order(
    api,
    occ_symbol: str,
    side: str,
    qty: int,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
) -> Optional[str]:
    """Submit a single-leg option order via Alpaca's `submit_order`.

    Alpaca's submit_order accepts OCC option symbols on the same path
    as equity orders — just pass the OCC string as the symbol arg.

    side: "buy" or "sell"
    order_type: "market" or "limit"
    limit_price: required when order_type == "limit"

    Returns the broker order_id on success, None on failure (failure
    is logged, not raised — caller decides how to surface).
    """
    if not occ_symbol or qty <= 0 or side not in ("buy", "sell"):
        return None
    if order_type == "limit" and (limit_price is None or limit_price <= 0):
        logger.warning(
            "submit_option_order: limit_price required for limit order"
        )
        return None
    try:
        kwargs = {
            "symbol": occ_symbol,
            "qty": int(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if order_type == "limit":
            kwargs["limit_price"] = round(float(limit_price), 2)
        order = api.submit_order(**kwargs)
        order_id = getattr(order, "id", None)
        if order_id:
            logger.info(
                "Option order placed: %s %d %s %s order_id=%s",
                side, qty, occ_symbol, order_type, order_id,
            )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place option order (%s %d %s): %s",
            side, qty, occ_symbol, exc,
        )
        return None
