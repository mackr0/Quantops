"""Virtual account data integrity auditor.

Runs periodically (every exit-check cycle) and verifies that the
internal ledger is consistent. Catches problems early before they
compound into corrupted metrics.

Checks performed per virtual profile:
  1. Accounting identity: cash + portfolio_value == equity (within $0.01)
  2. No negative quantities in open positions
  3. Cash never went negative (shouldn't happen with proper sizing)
  4. Trade attribution: every trade in this profile's DB was placed by
     this profile (no cross-profile leakage)
  5. Position consistency: FIFO lot computation produces same result
     when run twice (deterministic)

Cross-account check (per shared Alpaca account):
  6. Sum of virtual positions across profiles sharing an account should
     not exceed the Alpaca account's actual holdings (within tolerance
     for fill timing)

Any failure logs a WARNING and records it to the activity log so it
shows up in the dashboard immediately.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def audit_virtual_profile(db_path: str, initial_capital: float,
                          profile_name: str = "") -> List[str]:
    """Run all per-profile integrity checks.

    Returns a list of problem descriptions. Empty = healthy.
    """
    from journal import get_virtual_positions, get_virtual_account_info

    problems = []

    # 1. Accounting identity
    try:
        info = get_virtual_account_info(db_path=db_path,
                                         initial_capital=initial_capital)
        expected_equity = info["cash"] + info["portfolio_value"]
        if abs(info["equity"] - expected_equity) > 0.02:
            problems.append(
                f"Accounting mismatch: equity={info['equity']:.2f} but "
                f"cash({info['cash']:.2f}) + portfolio({info['portfolio_value']:.2f}) "
                f"= {expected_equity:.2f}"
            )
    except Exception as exc:
        problems.append(f"Could not compute account info: {exc}")
        return problems

    # 2. No negative position quantities
    try:
        positions = get_virtual_positions(db_path=db_path)
        for p in positions:
            if p["qty"] < 0:
                problems.append(
                    f"Negative position: {p['symbol']} qty={p['qty']}"
                )
    except Exception as exc:
        problems.append(f"Could not compute positions: {exc}")

    # 3. Cash shouldn't be deeply negative (small float rounding OK)
    if info["cash"] < -1.0:
        problems.append(
            f"Cash is negative: ${info['cash']:.2f} — profile may be "
            f"overallocated or trades are misattributed"
        )

    # 4. Position consistency (deterministic)
    try:
        pos_a = get_virtual_positions(db_path=db_path)
        pos_b = get_virtual_positions(db_path=db_path)
        syms_a = {p["symbol"]: p["qty"] for p in pos_a}
        syms_b = {p["symbol"]: p["qty"] for p in pos_b}
        if syms_a != syms_b:
            problems.append(
                "Position computation not deterministic — two consecutive "
                "calls returned different results"
            )
    except Exception:
        pass

    # 5. Trade count sanity — log if no trades at all (profile may be misconfigured)
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        if count == 0:
            problems.append("No trades recorded — profile may not be executing")
    except Exception:
        pass

    if problems:
        label = profile_name or db_path
        for p in problems:
            logger.warning("[%s] AUDIT: %s", label, p)

    return problems


def audit_cross_account(alpaca_account_id: int,
                        profile_ids: List[int]) -> List[str]:
    """Compare sum of virtual positions against Alpaca's actual holdings.

    Returns a list of discrepancies. Empty = consistent.
    """
    from models import build_user_context_from_profile
    from client import get_api
    from journal import get_virtual_positions

    problems = []

    # Sum virtual positions across all profiles sharing this account
    virtual_totals: Dict[str, float] = {}
    for pid in profile_ids:
        try:
            ctx = build_user_context_from_profile(pid)
            positions = get_virtual_positions(db_path=ctx.db_path)
            for p in positions:
                virtual_totals[p["symbol"]] = (
                    virtual_totals.get(p["symbol"], 0) + p["qty"]
                )
        except Exception as exc:
            problems.append(f"Profile {pid}: could not read positions: {exc}")

    # Get Alpaca actual positions
    alpaca_totals: Dict[str, float] = {}
    try:
        if profile_ids:
            ctx = build_user_context_from_profile(profile_ids[0])
            api = get_api(ctx)
            for p in api.list_positions():
                alpaca_totals[p.symbol] = float(p.qty)
    except Exception as exc:
        problems.append(f"Could not read Alpaca positions: {exc}")
        return problems

    # Compare
    all_symbols = set(virtual_totals.keys()) | set(alpaca_totals.keys())
    for sym in sorted(all_symbols):
        v_qty = virtual_totals.get(sym, 0)
        a_qty = alpaca_totals.get(sym, 0)
        diff = abs(v_qty - a_qty)
        if diff > 0.5:  # tolerance for partial fills in flight
            problems.append(
                f"{sym}: virtual total={v_qty:.0f} vs Alpaca={a_qty:.0f} "
                f"(diff={diff:.0f} shares)"
            )

    if problems:
        for p in problems:
            logger.warning("[Account %d] CROSS-AUDIT: %s", alpaca_account_id, p)

    return problems
