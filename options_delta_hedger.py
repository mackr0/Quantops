"""Phase D1 of OPTIONS_PROGRAM_PLAN.md — dynamic delta hedging.

For LONG single-leg option positions whose delta drifts as the
underlying moves (long_call, long_put), continuously rebalance the
underlying-stock hedge so the net (option + hedge) position stays
near target delta.

Why: a long call is a long-delta position. As the stock rises, delta
rises (gamma). Without rebalancing, what was meant to be a 50-delta
"insurance" position becomes 80-delta — a directional bet you didn't
sign up for. The hedge keeps the position tracking VEGA / GAMMA / THETA
exposure cleanly without accumulating directional drift.

Excluded from hedging (already-hedged or defined-risk):
  - covered_call: stock IS the hedge; adding more would double up
  - protective_put: stock + put already paired
  - cash_secured_put: no stock yet; no drift to hedge
  - vertical spreads / iron_condor / iron_butterfly: defined-risk, the
    multi-leg structure self-hedges
  - calendar / diagonal: complex term-structure plays, not simple
    delta hedges

Conservative defaults:
  - target_delta = 0 (delta-neutral)
  - rebalance threshold: |drift| > max(5 shares, 5%) of position
  - hedge applies only to long single-leg options
"""
from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Tunables
HEDGE_TARGET_DELTA = 0.0  # delta-neutral
HEDGE_REBALANCE_MIN_SHARES = 5
HEDGE_REBALANCE_MIN_PCT = 0.05  # 5% drift relative to current

# Strategies that get delta-hedged. Excluded list above.
HEDGEABLE_STRATEGIES = {"long_call", "long_put"}


def _is_hedgeable_option(row: Dict[str, Any]) -> bool:
    """True if a given option position should be delta-hedged."""
    strategy = (row.get("option_strategy") or "").lower()
    side = (row.get("side") or "").lower()
    # Only LONG single-leg long_call/long_put
    if strategy not in HEDGEABLE_STRATEGIES:
        return False
    if side != "buy":
        return False
    return True


def compute_hedge_target(
    positions: List[Dict[str, Any]],
    db_path: str,
    price_lookup: Callable[[str], Optional[float]],
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Compute the target hedge position per underlying.

    Walks the journal for hedgeable LONG option rows, computes their
    aggregate delta per underlying via the Greeks aggregator, and
    returns the share count needed to offset that delta to target.

    Args:
        positions: live position list (stock + options).
        db_path: profile journal DB.
        price_lookup, iv_lookup, today: same shape as Greeks aggregator.

    Returns dict:
      {
        underlying_symbol: {
          "options_delta": float,
          "current_stock_qty": int,
          "target_stock_qty": int,
          "drift_shares": int,
          "rebalance_needed": bool,
        },
        ...
      }
    """
    today = today or _date.today()
    from journal import _get_conn
    conn = _get_conn(db_path)
    cur = conn.execute(
        """SELECT id, symbol, side, qty, occ_symbol, option_strategy,
                  expiry, strike
           FROM trades
           WHERE signal_type='OPTIONS' AND status='open'""",
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()

    # Build hedgeable-option position dicts (compatible with the
    # Greeks aggregator's input shape)
    hedgeable_legs: List[Dict[str, Any]] = []
    for rd in rows:
        if not _is_hedgeable_option(rd):
            continue
        # Greeks aggregator expects "qty" signed by side
        signed_qty = float(rd["qty"]) if rd["side"] == "buy" else -float(rd["qty"])
        hedgeable_legs.append({
            "symbol": rd["occ_symbol"],
            "occ_symbol": rd["occ_symbol"],
            "qty": signed_qty,
            "underlying_for_hedge": rd["symbol"],  # bookkeeping
        })

    if not hedgeable_legs:
        return {}

    # Group legs by underlying
    legs_by_underlying: Dict[str, List[Dict[str, Any]]] = {}
    for leg in hedgeable_legs:
        legs_by_underlying.setdefault(leg["underlying_for_hedge"], []).append(leg)

    # For each underlying, compute aggregate options_delta via the
    # Greeks aggregator on JUST those legs.
    from options_greeks_aggregator import compute_book_greeks

    out: Dict[str, Dict[str, Any]] = {}
    for underlying, legs in legs_by_underlying.items():
        summary = compute_book_greeks(
            legs, price_lookup=price_lookup, iv_lookup=iv_lookup,
            today=today,
        )
        options_delta = float(summary.get("options_delta", 0))

        # Current stock holding
        current_stock = 0
        for p in positions or []:
            if (p.get("symbol", "").upper() == underlying.upper()
                    and not (len(p.get("symbol", "")) == 21
                             and p.get("symbol", "")[12] in "CP")):
                try:
                    current_stock += int(float(p.get("qty") or 0))
                except (TypeError, ValueError):
                    pass

        # Target = -options_delta + HEDGE_TARGET_DELTA
        target_stock = int(round(HEDGE_TARGET_DELTA - options_delta))
        drift = target_stock - current_stock

        # Threshold: bigger of |5 shares| or 5% of |options_delta|
        rel_threshold = max(
            HEDGE_REBALANCE_MIN_SHARES,
            int(abs(options_delta) * HEDGE_REBALANCE_MIN_PCT),
        )
        rebalance_needed = abs(drift) >= rel_threshold

        out[underlying] = {
            "options_delta": round(options_delta, 4),
            "current_stock_qty": current_stock,
            "target_stock_qty": target_stock,
            "drift_shares": drift,
            "rebalance_needed": rebalance_needed,
            "threshold_shares": rel_threshold,
        }
    return out


def rebalance_hedges(
    api,
    db_path: str,
    positions: List[Dict[str, Any]],
    price_lookup: Callable[[str], Optional[float]],
    iv_lookup: Optional[Callable[[str], Optional[float]]] = None,
    today: Optional[_date] = None,
    log: bool = True,
) -> Dict[str, Any]:
    """For each underlying with hedgeable long options, submit any
    stock-side rebalance order needed to bring net delta to target.

    Returns:
      {
        evaluated: int,
        rebalanced: int,
        details: [
          {underlying, action, qty, order_id, drift_before},
          ...
        ],
        errors: int,
      }
    """
    summary = {
        "evaluated": 0, "rebalanced": 0, "errors": 0, "details": [],
    }
    targets = compute_hedge_target(
        positions, db_path,
        price_lookup=price_lookup, iv_lookup=iv_lookup, today=today,
    )
    summary["evaluated"] = len(targets)
    if not targets:
        return summary

    for underlying, info in targets.items():
        if not info["rebalance_needed"]:
            continue
        drift = int(info["drift_shares"])
        if drift == 0:
            continue
        # Drift > 0 → need MORE long stock → BUY |drift| shares
        # Drift < 0 → need to reduce long stock (or go short) → SELL
        side = "buy" if drift > 0 else "sell"
        qty = abs(drift)
        try:
            order = api.submit_order(
                symbol=underlying, qty=qty, side=side,
                type="market", time_in_force="day",
            )
            order_id = getattr(order, "id", None)
        except Exception as exc:
            summary["errors"] += 1
            summary["details"].append({
                "underlying": underlying,
                "error": f"hedge submit failed: {exc}",
            })
            logger.warning("Hedge rebalance failed for %s: %s",
                           underlying, exc)
            continue
        summary["rebalanced"] += 1
        summary["details"].append({
            "underlying": underlying, "action": side, "qty": qty,
            "order_id": order_id,
            "options_delta_before": info["options_delta"],
            "current_stock_before": info["current_stock_qty"],
        })
        if log:
            try:
                from journal import log_trade
                log_trade(
                    symbol=underlying, side=side, qty=qty,
                    order_id=order_id, signal_type="DELTA_HEDGE",
                    strategy="delta_neutralization",
                    reason=(
                        f"Delta-hedge rebalance: options Δ "
                        f"{info['options_delta']:+.0f}, "
                        f"current stock {info['current_stock_qty']}, "
                        f"target {info['target_stock_qty']}. "
                        f"Submit {side} {qty}."
                    ),
                    db_path=db_path,
                )
            except Exception as exc:
                logger.warning("Hedge log_trade failed: %s", exc)
        logger.info(
            "Delta hedge: %s %d %s (Δ %+.1f → target %d, current %d)",
            side, qty, underlying, info["options_delta"],
            info["target_stock_qty"], info["current_stock_qty"],
        )
    return summary
