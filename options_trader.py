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
    # OCC root is 6 chars left-justified, padded with spaces (the OSI
    # canonical form). NOTE (2026-06-15): Alpaca's API does NOT accept
    # this padded form — it returns 422 "asset not found" and wants
    # the compact "BMNR260724..." form. Every submit path therefore
    # SNAPS this output to the listed contract's own symbol before
    # sending (single-leg: execute_option_strategy; multileg: the
    # snap loop). This builder stays OSI-canonical so parse_occ_symbol
    # round-trips and so non-broker callers have a stable form; the
    # snap is what makes it broker-resolvable.
    root = underlying.upper().ljust(6)
    yymmdd = expiry.strftime("%y%m%d")
    # Strike × 1000, padded to 8 digits. $150.00 → 00150000.
    strike_int = int(round(float(strike) * 1000))
    strike_str = f"{strike_int:08d}"
    return f"{root}{yymmdd}{r}{strike_str}"


def parse_occ_symbol(occ: str) -> Dict[str, Any]:
    """Inverse of format_occ_symbol — extract underlying, expiry,
    strike, right. FORMAT-AGNOSTIC.

    Handles BOTH the OSI space-padded root (`AAPL  250516C00150000`)
    AND Alpaca's compact form (`BMNR260724C00018000`). The trailing
    structure is fixed — the last 8 chars are strike×1000, the char
    before is C/P, the 6 before that are YYMMDD — so we parse from
    the RIGHT and whatever remains is the root.

    2026-06-15: the old fixed-offset parse assumed a padded 6-char
    root (`occ[6:12]` for the date, etc.). Fed a compact symbol it
    raised ValueError, which every caller catches into a dropped
    result — silently blinding the Greeks gate (`_parse_option_
    position` → None → leg excluded from book delta/theta/vega) and
    the options-exit timing (`_days_to_expiry` → None) to multileg
    legs, which are STORED compact via the snap step. Confirmed live
    on BMNR260724C00018000 (A1 held it; the parser dropped it)."""
    if not occ:
        raise ValueError(f"empty OCC symbol: {occ!r}")
    s = occ.strip()
    # Fixed right-anchored tail: <YYMMDD(6)><C|P(1)><strike(8)>.
    # Minimum viable symbol is root(>=1) + 15 = 16 chars.
    if len(s) < 16:
        raise ValueError(f"OCC symbol too short: {occ!r}")
    strike_str = s[-8:]
    right = s[-9]
    yymmdd = s[-15:-9]
    root = s[:-15].strip()
    if not root:
        raise ValueError(f"OCC symbol missing root: {occ!r}")
    if right not in ("C", "P"):
        raise ValueError(f"OCC right must be 'C'/'P', got {right!r}: {occ!r}")
    if not (strike_str.isdigit() and yymmdd.isdigit()):
        raise ValueError(f"OCC malformed date/strike: {occ!r}")
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

def execute_option_strategy(
    api,
    proposal: Dict[str, Any],
    ctx,
    log: bool = True,
) -> Dict[str, Any]:
    """Execute an AI-proposed option strategy end-to-end.

    `proposal` is a dict from the AI's trades list with shape:
      {
        "action": "OPTIONS",
        "option_strategy": "covered_call" | "protective_put" |
                              "long_call" | "long_put" | "cash_secured_put",
        "symbol": "AAPL",            # underlying
        "strike": 165.00,
        "expiry": "2026-05-16",      # ISO date
        "contracts": 1,
        "limit_price": 2.55,         # optional; market order if absent
        "reasoning": "..."
      }

    Returns a result dict shaped like the equity execute_trade result:
      {action, symbol, qty, order_id, reason}
    Action will be "OPTIONS_OPEN" on success or "SKIP" / "ERROR" on failure.

    Sizing constraints (per strategy):
      - covered_call: contracts must be ≤ shares_held // 100
      - protective_put: same — only hedge what we hold
      - cash_secured_put: cash required ≤ ctx buying power
      - long_call/long_put: total premium ≤ 1% of equity (defined-risk
        hard cap so we don't blow up the account on a wild AI proposal)
    """
    from datetime import date as _date
    result = {"symbol": proposal.get("symbol"), "action": "SKIP", "reason": ""}

    strategy = proposal.get("option_strategy", "").lower()
    underlying = (proposal.get("symbol") or "").upper()
    strike = proposal.get("strike")
    expiry_str = proposal.get("expiry")
    contracts = int(proposal.get("contracts") or 0)
    limit_price = proposal.get("limit_price")

    # Validate the basics. The OPTIONS action is single-leg only;
    # spread strategies (bull_put_spread, bear_call_spread, iron_condor,
    # etc.) must use action='MULTILEG_OPEN' so the multileg builder +
    # bracket-via-MLEG pipeline runs instead of the single-leg path.
    _SINGLE_LEG = ("covered_call", "protective_put", "long_call",
                   "long_put", "cash_secured_put")
    _MULTILEG_NAMES = ("bull_put_spread", "bull_call_spread",
                       "bear_put_spread", "bear_call_spread",
                       "iron_condor", "iron_butterfly",
                       "long_straddle", "short_straddle",
                       "long_strangle")
    if strategy in _MULTILEG_NAMES:
        # The AI proposed a multileg strategy under the single-leg
        # OPTIONS action. Surface the routing mistake so the prompt
        # tuner / AI sees this needs MULTILEG_OPEN, not OPTIONS.
        result["reason"] = (
            f"AI proposed {strategy!r} under action='OPTIONS' which is "
            f"single-leg only. This strategy requires "
            f"action='MULTILEG_OPEN' (the multileg pipeline handles "
            f"strikes dict, atomic combo submit, etc.). Re-propose "
            f"the trade with the correct action class."
        )
        return result
    if strategy not in _SINGLE_LEG:
        result["reason"] = (
            f"Unsupported option_strategy: {strategy!r}. "
            f"Single-leg path supports: {', '.join(_SINGLE_LEG)}. "
            f"Multileg strategies need action='MULTILEG_OPEN'."
        )
        return result
    if not underlying or not strike or not expiry_str or contracts <= 0:
        result["reason"] = (
            f"Missing required option proposal fields "
            f"(symbol/strike/expiry/contracts)"
        )
        return result
    try:
        expiry = _date.fromisoformat(expiry_str)
    except Exception:
        result["reason"] = f"Invalid expiry date: {expiry_str!r}"
        return result
    if expiry <= _date.today():
        result["reason"] = f"Expiry {expiry_str} is not in the future"
        return result

    # Strategy → option right + side
    if strategy == "covered_call":
        right, side = "C", "sell"
    elif strategy == "protective_put":
        right, side = "P", "buy"
    elif strategy == "long_call":
        right, side = "C", "buy"
    elif strategy == "long_put":
        right, side = "P", "buy"
    elif strategy == "cash_secured_put":
        right, side = "P", "sell"

    # Sizing constraints — abort cleanly if the proposal exceeds them.
    # Re-read positions / account from ctx; we need accurate state.
    try:
        from client import get_positions, get_account_info
        positions = get_positions(ctx=ctx) or []
        account = get_account_info(ctx=ctx) or {}
    except Exception as exc:
        result["reason"] = f"Could not read account state: {exc}"
        return result

    pos_for_underlying = next(
        (p for p in positions if p.get("symbol") == underlying), None,
    )

    if strategy in ("covered_call", "protective_put"):
        # Must already hold the underlying; contracts ≤ shares // 100.
        held_qty = int(float(pos_for_underlying.get("qty", 0))
                        if pos_for_underlying else 0)
        if held_qty < 100:
            result["reason"] = (
                f"Need ≥100 shares of {underlying} for {strategy}; "
                f"hold {held_qty}"
            )
            return result
        max_contracts = held_qty // 100
        if contracts > max_contracts:
            logger.info(
                "Capping %s contracts for %s from %d to %d (held=%d)",
                strategy, underlying, contracts, max_contracts, held_qty,
            )
            contracts = max_contracts

    elif strategy == "cash_secured_put":
        cash_required = strike * 100 * contracts
        buying_power = float(account.get("buying_power", 0))
        if cash_required > buying_power:
            result["reason"] = (
                f"CSP needs ${cash_required:,.0f} buying power; "
                f"have ${buying_power:,.0f}"
            )
            return result

    elif strategy in ("long_call", "long_put"):
        # Defined-risk hard cap: total premium ≤ 1% of equity. We don't
        # know the actual fill price yet; use limit_price as estimate
        # if provided, else assume premium = 5% of strike (rough cap).
        equity = float(account.get("equity", 0))
        est_premium = (limit_price if limit_price is not None
                       else 0.05 * strike)
        max_premium_dollars = 0.01 * equity
        total_premium_dollars = est_premium * 100 * contracts
        if total_premium_dollars > max_premium_dollars:
            result["reason"] = (
                f"{strategy} premium ${total_premium_dollars:,.0f} > 1% "
                f"of equity ${max_premium_dollars:,.0f} (hard cap)"
            )
            return result

    # Build OCC + submit
    occ = format_occ_symbol(underlying, expiry, strike, right)

    # 2026-06-15 — SNAP to Alpaca's listed contract symbol, exactly
    # as the multileg path does. format_occ_symbol emits the OSI
    # space-padded root ("BMNR  260724C00018000"); Alpaca's API uses
    # the compact form ("BMNR260724C00018000") and rejects the padded
    # one with 422 "asset not found" — so single-leg options were
    # 100% rejected at submit (confirmed live on BMNR, which A1 held
    # via the multileg path the same cycle). The parse-layer RC11
    # snap only fixed the STRIKE; the executor rebuilt the symbol
    # string here and lost Alpaca's form. Carrying the snapped
    # contract's own symbol forward makes every downstream use (dup
    # guard, Greeks mock, submit, journal) consistent and
    # broker-resolvable. Best-effort: if the chain is unavailable we
    # keep the built symbol (graceful degradation, same as multileg).
    try:
        from options_chain_alpaca import snap_to_listed_contract as _snap
        _snapped = _snap(underlying, expiry.isoformat(), float(strike),
                         right)
        if _snapped and _snapped.get("symbol"):
            occ = _snapped["symbol"]
    except Exception as _snap_exc:
        logger.debug(
            "single-leg OCC snap failed for %s (using built symbol "
            "%s): %s", underlying, occ, _snap_exc,
        )

    # Duplicate-position guard — same shape as the multileg dup guard
    # added 2026-05-06. Without this, a strategy that proposes the
    # same OPTIONS entry on consecutive cycles (e.g., AI repeatedly
    # surfacing the same covered_call recommendation) would re-fire
    # on every cycle, accumulating duplicate positions at the broker.
    db_path = ctx.db_path if ctx else None
    if db_path:
        try:
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT id FROM trades "
                "WHERE occ_symbol = ? "
                "  AND COALESCE(status, 'open') NOT IN ('closed', 'canceled') "
                "LIMIT 1",
                (occ,),
            ).fetchone()
            conn.close()
            if row:
                result["action"] = "SKIP"
                result["reason"] = (
                    f"Open journal row for {occ} already exists "
                    f"(id={row[0]}) — refusing to duplicate "
                    f"{strategy} entry."
                )
                logger.warning(
                    "[options] %s SKIPPED on %s — %s",
                    strategy, underlying, result["reason"],
                )
                return result
        except Exception as exc:
            logger.warning(
                "Duplicate-position check failed for %s (continuing): %s",
                occ, exc,
            )

    # Phase A2 — Greeks exposure gate. Compute the leg's contribution
    # to portfolio Greeks and check it doesn't push the book past
    # any active gate. Skips silently when no chain data available
    # (gate is best-effort; equity-sizing gates upstream still apply).
    try:
        from options_greeks_aggregator import (
            compute_book_greeks, _greek_contribution, _parse_option_position,
            check_greeks_gates, FALLBACK_IV,
        )
        from datetime import date as _date_now
        # Estimate IV: use limit_price if provided to back out an IV;
        # otherwise fall back to FALLBACK_IV. Production callers should
        # plumb a real IV from the options oracle.
        spot_for_gate = None
        try:
            from market_data import get_bars as _gb
            bars_for_gate = _gb(underlying, limit=2)
            if bars_for_gate is not None and len(bars_for_gate) > 0:
                spot_for_gate = float(bars_for_gate["close"].iloc[-1])
        except Exception:
            spot_for_gate = None
        if spot_for_gate is not None and spot_for_gate > 0:
            mock_pos = {"symbol": occ, "occ_symbol": occ, "qty": contracts
                        if side == "buy" else -contracts}
            parsed = _parse_option_position(mock_pos)
            if parsed:
                contribution = _greek_contribution(
                    parsed, spot_for_gate, FALLBACK_IV,
                    today=_date_now.today(),
                )
                # Read current book
                try:
                    from client import get_positions
                    positions_for_gate = get_positions(ctx=ctx) or []
                except Exception:
                    positions_for_gate = []
                book_summary = compute_book_greeks(
                    positions_for_gate,
                    price_lookup=lambda s: spot_for_gate if s == underlying else None,
                    iv_lookup=lambda s: FALLBACK_IV,
                )
                gate_result = check_greeks_gates(
                    book_summary, contribution, ctx=ctx,
                )
                if not gate_result["allowed"]:
                    result["reason"] = (
                        f"Greeks gate(s) blocked: "
                        f"{'; '.join(gate_result['reasons'])}"
                    )
                    return result
    except Exception as exc:
        logger.debug("Greeks gate eval failed (non-blocking): %s", exc)

    order_type = "limit" if limit_price is not None else "market"
    order_id = submit_option_order(
        api, occ, side=side, qty=contracts,
        order_type=order_type, limit_price=limit_price,
    )

    if not order_id:
        # Surface the actual broker rejection (Alpaca error string,
        # invalid args, etc.) instead of the generic message. The
        # error is stashed in submit_option_order's module state
        # because back-compat on Optional[str] return signature.
        last_err = get_last_option_order_error()
        result["action"] = "ERROR"
        result["reason"] = (
            f"Option order rejected for {occ}: {last_err}"
            if last_err else
            f"Broker did not return order_id for {occ}"
        )
        return result

    if log:
        try:
            from journal import log_trade
            log_trade(
                symbol=underlying,
                side=side,
                qty=contracts,
                price=limit_price,
                order_id=order_id,
                signal_type="OPTIONS",
                strategy=strategy,
                reason=proposal.get("reasoning", "")[:500],
                ai_reasoning=proposal.get("reasoning", ""),
                ai_confidence=int(proposal.get("confidence", 0) or 0),
                decision_price=limit_price,
                occ_symbol=occ,
                option_strategy=strategy,
                expiry=expiry.isoformat(),
                strike=float(strike),
                db_path=ctx.db_path if ctx else None,
            )
        except Exception as exc:
            # Atomic-placement contract: the broker holds an order
            # this process can no longer journal. Cancel the broker
            # order and halt the profile so trading stops until the
            # operator acknowledges. Without this, the prior silent
            # warning left the broker in a `broker_orphan` state
            # (no profile's virtual book reflects the position).
            logger.error(
                "Single-leg option order placed (id=%s) but "
                "log_trade failed: %s: %s — initiating atomic rollback",
                order_id, type(exc).__name__, exc,
            )
            rollback_failed = False
            try:
                api.cancel_order(order_id)
                logger.info(
                    "Single-leg rollback: cancelled broker order %s",
                    order_id,
                )
            except Exception as cancel_exc:
                msg = str(cancel_exc).lower()
                already_terminal = (
                    "already" in msg and (
                        "filled" in msg or "cancel" in msg
                        or "terminal" in msg
                    )
                )
                if not already_terminal:
                    rollback_failed = True
                    logger.error(
                        "Cancel of broker order %s FAILED: %s: %s — "
                        "broker may hold orphan position",
                        order_id, type(cancel_exc).__name__,
                        cancel_exc,
                    )
            try:
                from halt_helpers import halt_and_alert
                profile_id = (
                    getattr(ctx, "profile_id", None) if ctx else None
                )
                if profile_id:
                    title = (
                        "Single-leg option journal-write breach "
                        "(rollback FAILED): " + str(occ)
                        if rollback_failed
                        else "Single-leg option journal-write breach: "
                        + str(occ)
                    )
                    halt_and_alert(
                        profile_id=profile_id,
                        db_path=(
                            ctx.db_path if ctx else None
                        ),
                        alert_type="option_atomic_breach",
                        title=title,
                        detail=(
                            f"occ={occ} side={side} qty={contracts} "
                            f"order_id={order_id} "
                            f"log_trade_exc={type(exc).__name__}: {exc} "
                            f"rollback_failed={rollback_failed}"
                        ),
                    )
            except Exception as halt_exc:
                logger.error(
                    "halt_and_alert FAILED: %s: %s",
                    type(halt_exc).__name__, halt_exc,
                )
            result.update({
                "action": "ERROR",
                "occ_symbol": occ,
                "order_id": order_id,
                "reason": (
                    f"Single-leg option journal-write breach on "
                    f"{occ} — broker rolled back, profile halted"
                ),
            })
            return result

    result.update({
        "action": "OPTIONS_OPEN",
        "qty": contracts,
        "order_id": order_id,
        "occ_symbol": occ,
        "option_strategy": strategy,
        "expiry": expiry.isoformat(),
        "strike": float(strike),
        "reason": (proposal.get("reasoning", "") or "")[:200],
    })
    return result


def submit_option_order(
    api,
    occ_symbol: str,
    side: str,
    qty: int,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
    position_intent: Optional[str] = None,
) -> Optional[str]:
    """Submit a single-leg option order via Alpaca's `submit_order`.

    Alpaca's submit_order accepts OCC option symbols on the same path
    as equity orders — just pass the OCC string as the symbol arg.

    side: "buy" or "sell"
    order_type: "market" or "limit"
    limit_price: required when order_type == "limit"
    position_intent: "buy_to_open" / "sell_to_open" / "buy_to_close" /
        "sell_to_close". Required by Alpaca on every option submit; if
        None, defaults to *_to_open by side. Without intent Alpaca
        async-cancels short opens (caught 2026-05-06 ARCC runaway).

    Returns the broker order_id on success, None on failure (failure
    is logged, not raised — caller decides how to surface).
    """
    # 2026-06-10 — Surface the rejection reason. Pre-fix the
    # function only returned Optional[str] for order_id; on
    # failure (early-validation refuse OR broker exception) the
    # caller got None with no detail and reported the generic
    # "Broker did not return order_id for {occ}". Now we also
    # stash the reason on a module-level last-error attribute so
    # callers can surface the actual cause.
    global _LAST_OPTION_ORDER_ERROR
    _LAST_OPTION_ORDER_ERROR = None
    if not occ_symbol or qty <= 0 or side not in ("buy", "sell"):
        _LAST_OPTION_ORDER_ERROR = (
            f"invalid args: occ={occ_symbol!r} qty={qty} side={side!r}"
        )
        return None
    if order_type == "limit" and (limit_price is None or limit_price <= 0):
        _LAST_OPTION_ORDER_ERROR = (
            f"limit_price required for limit order (got {limit_price!r})"
        )
        logger.warning(
            "submit_option_order: limit_price required for limit order"
        )
        return None
    if position_intent is None:
        position_intent = "buy_to_open" if side == "buy" else "sell_to_open"
    try:
        kwargs = {
            "symbol": occ_symbol,
            "qty": int(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
            "position_intent": position_intent,
        }
        if order_type == "limit":
            kwargs["limit_price"] = round(float(limit_price), 2)
        # alpaca-trade-api's submit_order doesn't accept
        # `position_intent`, but Alpaca's REST API does. Bypass the
        # SDK for option orders so the intent reaches the broker.
        from options_multileg import _submit_alpaca_order_raw
        order = _submit_alpaca_order_raw(api, kwargs)
        order_id = getattr(order, "id", None)
        if order_id:
            logger.info(
                "Option order placed: %s %d %s %s order_id=%s",
                side, qty, occ_symbol, order_type, order_id,
            )
            return order_id
        # No id but no exception either — surface what the broker
        # returned so the caller can put it in the drop reason
        # instead of the generic "did not return order_id."
        _LAST_OPTION_ORDER_ERROR = (
            f"broker response missing id; status={getattr(order, 'status', '?')!r} "
            f"client_order_id={getattr(order, 'client_order_id', '?')!r}"
        )
        return None
    except Exception as exc:
        _LAST_OPTION_ORDER_ERROR = (
            f"{type(exc).__name__}: {str(exc)[:240]}"
        )
        logger.warning(
            "Could not place option order (%s %d %s): %s",
            side, qty, occ_symbol, exc,
        )
        return None


# Module-level last-error storage so the simple `Optional[str]`
# return signature stays back-compat while the upstream drop
# reason can still surface the actual broker rejection.
_LAST_OPTION_ORDER_ERROR: Optional[str] = None


def get_last_option_order_error() -> Optional[str]:
    """Return the rejection reason from the most recent
    `submit_option_order` call that returned None. Resets each
    call to submit_option_order. Module-level so this works for
    single-threaded scheduler dispatch; the option pipeline never
    parallelizes across symbols within a single profile."""
    return _LAST_OPTION_ORDER_ERROR
