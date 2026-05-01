"""Options lifecycle management — sweep expired contracts.

Item 1a follow-up of COMPETITIVE_GAP_PLAN.md: when an option contract
expires, the open trade row in the journal becomes stale unless we
sweep it. This module:

  1. Finds option trades (signal_type='OPTIONS') with status='open'
     whose expiry has passed.
  2. Queries the broker for the actual outcome (filled? canceled?
     position still held?) — broker is source of truth for paper
     accounts where Alpaca handles assignment automatically.
  3. Marks the row status='closed' and computes realized P&L based
     on the strategy:
       - long_call/long_put: P&L = (last_underlying_value − premium_paid),
         floor 0. If broker still holds the option position with qty 0,
         it expired worthless → P&L = -premium_paid * 100 * contracts.
       - covered_call/cash_secured_put: short premium. If position is
         flat at expiry, P&L = +premium_collected * 100 * contracts.
         Assignment cases are detected via broker fills and noted.

Phase-1 scope: handle expired-OTM (no assignment). Assignment cases
are flagged for manual review via a `reason` string on the trade row.
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def find_expired_open_options(db_path: str,
                                  today: Optional[_date] = None) -> List[Dict[str, Any]]:
    """Return open option trade rows whose expiry has passed.

    Args:
        db_path: profile journal DB.
        today: override for current date (testing). Defaults to today.

    Returns rows shaped:
      {id, symbol, side, qty, occ_symbol, option_strategy, expiry,
       strike, price, decision_price, ai_confidence}
    """
    today = today or _date.today()
    from journal import _get_conn
    conn = _get_conn(db_path)
    cur = conn.execute(
        """SELECT id, symbol, side, qty, occ_symbol, option_strategy,
                  expiry, strike, price, decision_price, ai_confidence
           FROM trades
           WHERE signal_type='OPTIONS'
             AND status='open'
             AND expiry IS NOT NULL
             AND expiry < ?""",
        (today.isoformat(),),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _option_position_at_broker(api, occ_symbol: str) -> Optional[Dict[str, Any]]:
    """Return broker position for the OCC contract, or None if no
    position exists.

    Alpaca lists option positions alongside equity positions in
    api.list_positions() — the symbol is the OCC string.
    """
    try:
        positions = api.list_positions()
    except Exception as exc:
        logger.warning("Could not list positions for option lookup: %s", exc)
        return None
    for p in positions:
        if getattr(p, "symbol", None) == occ_symbol:
            return {
                "symbol": p.symbol,
                "qty": float(getattr(p, "qty", 0) or 0),
                "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
                "market_value": float(getattr(p, "market_value", 0) or 0),
            }
    return None


def _underlying_close_at_expiry(symbol: str,
                                    expiry: _date) -> Optional[float]:
    """Fetch the underlying's close price at the expiry date so we can
    determine ITM vs OTM. Falls back to last available close.

    Best-effort: returns None on data-fetch failure (caller treats as
    "can't determine ITM/OTM" → marks needs_review).
    """
    try:
        from market_data import get_bars
        bars = get_bars(symbol, limit=120)
        if bars is None or len(bars) == 0:
            return None
        # bars is a DataFrame with tz-aware index; find the close on or
        # closest before expiry. tz-naive .index lookup is fragile, so
        # iterate.
        for ts, row in bars.iterrows():
            try:
                ts_date = ts.date()
            except Exception:
                continue
            if ts_date == expiry:
                return float(row["close"])
        # No exact match — use the last bar before expiry
        before = [ts for ts in bars.index if hasattr(ts, "date")
                  and ts.date() <= expiry]
        if before:
            return float(bars.loc[before[-1], "close"])
        return float(bars["close"].iloc[-1])
    except Exception as exc:
        logger.debug("Could not fetch close for %s @ %s: %s",
                     symbol, expiry, exc)
        return None


def _is_itm_at_expiry(option_row: Dict[str, Any],
                          underlying_close: float) -> Optional[bool]:
    """Decide if an expired option finished ITM.

    Calls ITM if underlying > strike; puts ITM if underlying < strike.
    Returns None when the OCC right can't be determined.
    """
    strike = float(option_row.get("strike") or 0)
    if strike <= 0 or underlying_close is None or underlying_close <= 0:
        return None
    occ = option_row.get("occ_symbol") or ""
    right = ""
    if len(occ) >= 13 and occ[12] in ("C", "P", "c", "p"):
        right = occ[12].upper()
    if not right:
        # Try to infer from option_strategy
        strat = (option_row.get("option_strategy") or "").lower()
        if "call" in strat:
            right = "C"
        elif "put" in strat:
            right = "P"
    if right == "C":
        return underlying_close > strike
    if right == "P":
        return underlying_close < strike
    return None


def _compute_pnl_for_expired(row: Dict[str, Any],
                                broker_position: Optional[Dict[str, Any]],
                                underlying_close: Optional[float] = None
                                ) -> Dict[str, Any]:
    """Compute realized P&L for an expired option row.

    Returns {"pnl_dollars", "outcome", "reason", "synthetic_equity_leg"}
    where outcome is one of:
      "expired_worthless" — option went to zero (OTM at expiry).
      "assigned"          — short option ITM at expiry → counterparty
                              exercised, equity leg created. P&L
                              reflects realized premium received.
      "exercised"         — long option ITM at expiry → we (or broker)
                              exercised, equity leg created. P&L
                              reflects intrinsic value at expiry minus
                              premium paid.
      "needs_review"      — couldn't determine (no underlying close, etc.)
      "unknown"           — bad input data.

    `synthetic_equity_leg` is a dict like:
      {symbol, side, qty, price, reason} when an equity leg should be
      logged downstream; None otherwise.
    """
    strategy = row.get("option_strategy", "")
    contracts = int(row.get("qty") or 0)
    premium = float(row.get("decision_price") or row.get("price") or 0)
    side = row.get("side", "").lower()
    underlying = row.get("symbol", "")
    strike = float(row.get("strike") or 0)
    multiplier = contracts * 100  # 100 shares per contract

    # Prefer ITM/OTM decision over the broker-position check. The
    # broker-position check is a fallback signal because it only tells
    # us whether the option contract is still in the broker's books;
    # ITM/OTM at expiry tells us what definitely happened.
    is_itm = None
    if underlying_close is not None:
        is_itm = _is_itm_at_expiry(row, underlying_close)

    # Path A: clear ITM/OTM call available
    if is_itm is True:
        # ITM at expiry
        if side == "sell":
            # SHORT option assigned. Counterparty exercised against us.
            # Equity-leg created:
            #   short call ITM → called away, we lose 100×qty shares,
            #     receive strike × 100 × qty cash
            #   short put ITM → assigned, we gain 100×qty shares at
            #     cost basis of strike, paying strike × 100 × qty cash
            occ = row.get("occ_symbol") or ""
            right = occ[12].upper() if len(occ) >= 13 else ""
            if right == "C":
                # Called away → equity leg is a SELL
                synthetic = {
                    "symbol": underlying, "side": "sell",
                    "qty": multiplier, "price": strike,
                    "reason": (
                        f"Short call ITM at expiry — called away at "
                        f"${strike:.2f} ({contracts}× contracts)"
                    ),
                }
            elif right == "P":
                # Assigned → equity leg is a BUY
                synthetic = {
                    "symbol": underlying, "side": "buy",
                    "qty": multiplier, "price": strike,
                    "reason": (
                        f"Short put assigned at expiry — bought "
                        f"{multiplier} shares at ${strike:.2f}"
                    ),
                }
            else:
                synthetic = None
            # Premium realized fully in our favor (we keep the credit)
            pnl = premium * multiplier
            return {
                "pnl_dollars": pnl, "outcome": "assigned",
                "reason": (
                    f"Short {strategy} assigned at expiry "
                    f"(close ${underlying_close:.2f} vs strike "
                    f"${strike:.2f}). Premium realized: +${pnl:,.2f}. "
                    f"Equity leg created."
                ),
                "synthetic_equity_leg": synthetic,
            }
        elif side == "buy":
            # LONG option exercised. Auto-exercised by broker if ITM.
            # P&L = intrinsic_value × multiplier - premium_paid × multiplier
            occ = row.get("occ_symbol") or ""
            right = occ[12].upper() if len(occ) >= 13 else ""
            intrinsic = (max(0, underlying_close - strike) if right == "C"
                         else max(0, strike - underlying_close))
            if right == "C":
                # Exercised long call → buy stock at strike
                synthetic = {
                    "symbol": underlying, "side": "buy",
                    "qty": multiplier, "price": strike,
                    "reason": (
                        f"Long call exercised at expiry — bought "
                        f"{multiplier} shares at ${strike:.2f}"
                    ),
                }
            elif right == "P":
                # Exercised long put → sell stock at strike
                synthetic = {
                    "symbol": underlying, "side": "sell",
                    "qty": multiplier, "price": strike,
                    "reason": (
                        f"Long put exercised at expiry — sold "
                        f"{multiplier} shares at ${strike:.2f}"
                    ),
                }
            else:
                synthetic = None
            pnl = (intrinsic - premium) * multiplier
            return {
                "pnl_dollars": pnl, "outcome": "exercised",
                "reason": (
                    f"Long {strategy} exercised at expiry "
                    f"(close ${underlying_close:.2f} vs strike "
                    f"${strike:.2f}). Intrinsic ${intrinsic:.2f}, "
                    f"net P&L ${pnl:+,.2f}."
                ),
                "synthetic_equity_leg": synthetic,
            }

    if is_itm is False:
        # OTM at expiry → expired worthless. Same logic as before.
        if side == "buy":
            pnl = -premium * multiplier
            return {
                "pnl_dollars": pnl, "outcome": "expired_worthless",
                "reason": (
                    f"Long {strategy} expired OTM (close "
                    f"${underlying_close:.2f} vs strike ${strike:.2f}): "
                    f"-${abs(pnl):,.2f}"
                ),
                "synthetic_equity_leg": None,
            }
        elif side == "sell":
            pnl = premium * multiplier
            return {
                "pnl_dollars": pnl, "outcome": "expired_worthless",
                "reason": (
                    f"Short {strategy} expired OTM (close "
                    f"${underlying_close:.2f} vs strike ${strike:.2f}): "
                    f"+${pnl:,.2f}"
                ),
                "synthetic_equity_leg": None,
            }

    # Path B: ITM/OTM unknown. Fall back to broker-position check.
    if broker_position is not None and broker_position.get("qty", 0) != 0:
        return {
            "pnl_dollars": None, "outcome": "needs_review",
            "reason": (
                f"Broker still holds {broker_position['qty']} contracts of "
                f"{row.get('occ_symbol')} after expiry — assignment likely. "
                f"Review manually (no underlying close available for "
                f"automatic determination)."
            ),
            "synthetic_equity_leg": None,
        }

    # No data → conservative worthless treatment with note
    if side == "buy":
        pnl = -premium * multiplier
    elif side == "sell":
        pnl = premium * multiplier
    else:
        return {
            "pnl_dollars": 0.0, "outcome": "unknown",
            "reason": f"Unknown side {side!r} on expired option row",
            "synthetic_equity_leg": None,
        }
    return {
        "pnl_dollars": pnl, "outcome": "expired_worthless",
        "reason": (
            f"{strategy} treated as expired worthless (could not fetch "
            f"underlying close to verify ITM/OTM): "
            f"{'-' if pnl < 0 else '+'}${abs(pnl):,.2f}"
        ),
        "synthetic_equity_leg": None,
    }


def sweep_expired_options(api, db_path: str,
                              today: Optional[_date] = None) -> Dict[str, Any]:
    """Sweep expired open option trades and mark them closed.

    For each expired option:
      1. Fetch the underlying's close at expiry (so we can determine
         ITM/OTM automatically).
      2. Decide outcome:
         - OTM → expired_worthless
         - ITM short → assigned (counterparty exercised) + equity leg
         - ITM long → exercised (we exercised) + equity leg
         - Indeterminate → needs_review
      3. Update the option row's status + pnl + reason.
      4. If a synthetic equity leg is required (assignment / exercise),
         log it as a separate journal row so the virtual ledger
         reconciles correctly. Without this, the journal would think
         we still held the option but the underlying-stock position
         changed without explanation.

    Returns summary: {expired_found, closed_worthless, assigned,
    exercised, needs_review, errors, equity_legs_logged, details}.
    """
    today = today or _date.today()
    summary = {
        "expired_found": 0, "closed_worthless": 0, "assigned": 0,
        "exercised": 0, "needs_review": 0, "errors": 0,
        "equity_legs_logged": 0, "details": [],
    }

    rows = find_expired_open_options(db_path, today=today)
    summary["expired_found"] = len(rows)

    if not rows:
        return summary

    from journal import _get_conn, log_trade
    conn = _get_conn(db_path)

    for row in rows:
        try:
            broker_pos = _option_position_at_broker(api, row.get("occ_symbol"))
            # Fetch underlying close at the option's expiry date
            try:
                expiry_d = _date.fromisoformat(row["expiry"])
            except Exception:
                expiry_d = today
            underlying_close = _underlying_close_at_expiry(
                row.get("symbol", ""), expiry_d,
            )

            outcome = _compute_pnl_for_expired(
                row, broker_pos, underlying_close=underlying_close,
            )

            # Status mapping per outcome
            outcome_status_map = {
                "expired_worthless": "closed",
                "assigned": "closed",
                "exercised": "closed",
                "needs_review": "needs_review",
                "unknown": "needs_review",
            }
            new_status = outcome_status_map.get(
                outcome["outcome"], "needs_review")

            conn.execute(
                "UPDATE trades SET status=?, pnl=?, reason=? WHERE id=?",
                (new_status, outcome["pnl_dollars"], outcome["reason"],
                 row["id"]),
            )
            conn.commit()

            # Log the synthetic equity leg when assignment / exercise
            # happened. The leg sits in the journal as a stock trade so
            # FIFO P&L matching works for the resulting position.
            synthetic = outcome.get("synthetic_equity_leg")
            if synthetic:
                try:
                    log_trade(
                        symbol=synthetic["symbol"],
                        side=synthetic["side"],
                        qty=synthetic["qty"],
                        price=synthetic["price"],
                        signal_type="OPTION_EXERCISE",
                        strategy=row.get("option_strategy", ""),
                        reason=synthetic["reason"],
                        decision_price=synthetic["price"],
                        fill_price=synthetic["price"],
                        db_path=db_path,
                    )
                    summary["equity_legs_logged"] += 1
                except Exception as exc:
                    logger.warning(
                        "Synthetic equity leg log failed for option "
                        "trade %s: %s", row["id"], exc,
                    )

            # Bookkeeping
            if outcome["outcome"] == "expired_worthless":
                summary["closed_worthless"] += 1
            elif outcome["outcome"] == "assigned":
                summary["assigned"] += 1
            elif outcome["outcome"] == "exercised":
                summary["exercised"] += 1
            elif outcome["outcome"] == "needs_review":
                summary["needs_review"] += 1

            summary["details"].append({
                "id": row["id"], "occ": row.get("occ_symbol"),
                **outcome,
            })
            logger.info(
                "Lifecycle: trade %s (%s) → %s (%s)",
                row["id"], row.get("occ_symbol"),
                outcome["outcome"], outcome["reason"],
            )
        except Exception as exc:
            summary["errors"] += 1
            logger.exception(
                "Lifecycle sweep failed for trade %s: %s", row.get("id"), exc,
            )

    return summary
