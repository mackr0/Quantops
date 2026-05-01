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


def _compute_pnl_for_expired(row: Dict[str, Any],
                                broker_position: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute realized P&L for an expired option row.

    Returns {"pnl_dollars", "outcome", "reason"} where outcome is one of:
      "expired_worthless" — option went to zero, full premium realized
      "assigned"          — broker still shows position OR underlying
                              position changed; flagged for review
      "unknown"           — couldn't determine; row marked closed but
                              P&L set to 0 conservatively
    """
    strategy = row.get("option_strategy", "")
    contracts = int(row.get("qty") or 0)
    premium = float(row.get("decision_price") or row.get("price") or 0)
    side = row.get("side", "").lower()

    # If broker still has the position, expiry processing hasn't
    # finished or it was assigned — defer the P&L call.
    if broker_position is not None and broker_position.get("qty", 0) != 0:
        return {
            "pnl_dollars": None,
            "outcome": "assigned",
            "reason": (
                f"Broker still holds {broker_position['qty']} contracts of "
                f"{row.get('occ_symbol')} after expiry — assignment likely. "
                f"Review manually."
            ),
        }

    # Expired worthless — premium fully realized in our favor or against.
    # Long positions: we paid premium, now zero → loss.
    # Short positions: we collected premium, now zero → gain.
    multiplier = contracts * 100  # 100 shares per contract
    if side == "buy":
        # long_call / long_put / protective_put — paid premium, now zero
        pnl = -premium * multiplier
        outcome = "expired_worthless"
        reason = f"Long {strategy} expired worthless: -${abs(pnl):,.2f}"
    elif side == "sell":
        # covered_call / cash_secured_put — collected premium, now zero
        pnl = premium * multiplier
        outcome = "expired_worthless"
        reason = f"Short {strategy} expired worthless: +${pnl:,.2f}"
    else:
        return {
            "pnl_dollars": 0.0,
            "outcome": "unknown",
            "reason": f"Unknown side {side!r} on expired option row",
        }

    return {"pnl_dollars": pnl, "outcome": outcome, "reason": reason}


def sweep_expired_options(api, db_path: str,
                              today: Optional[_date] = None) -> Dict[str, Any]:
    """Sweep expired open option trades and mark them closed.

    Returns summary dict: {
        "expired_found": int,
        "closed_worthless": int,
        "assignment_flagged": int,
        "errors": int,
        "details": [ ... per-row outcomes ... ],
    }
    """
    today = today or _date.today()
    summary = {
        "expired_found": 0, "closed_worthless": 0,
        "assignment_flagged": 0, "errors": 0, "details": [],
    }

    rows = find_expired_open_options(db_path, today=today)
    summary["expired_found"] = len(rows)

    if not rows:
        return summary

    from journal import _get_conn
    conn = _get_conn(db_path)

    for row in rows:
        try:
            broker_pos = _option_position_at_broker(api, row.get("occ_symbol"))
            outcome = _compute_pnl_for_expired(row, broker_pos)

            new_status = ("closed" if outcome["outcome"] == "expired_worthless"
                          else "needs_review")
            conn.execute(
                "UPDATE trades SET status=?, pnl=?, reason=? WHERE id=?",
                (new_status, outcome["pnl_dollars"], outcome["reason"],
                 row["id"]),
            )
            conn.commit()

            if outcome["outcome"] == "expired_worthless":
                summary["closed_worthless"] += 1
            elif outcome["outcome"] == "assigned":
                summary["assignment_flagged"] += 1
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
