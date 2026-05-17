"""Per-profile internal-consistency audits.

These check that the virtual journal's OWN numbers are self-consistent —
distinct from `aggregate_audit` which compares virtual journal to the
broker. Together:

    aggregate_audit.py       virtual journal  ↔  broker truth
    integrity_audit.py       virtual journal  ↔  itself

The 2026-05-13 cash-logic bugs survived for weeks because aggregate_audit
only checked share quantities, not dollar amounts. Even after #165
(account_value_parity), there are still classes of bugs that don't show
up cross-account but DO show up as the journal failing its own algebra:

  - FIFO mismatch: realized_pnl column inconsistent with cash flows
  - Hidden cash flow (dividend, fee, manual adjustment) not in trades
  - market_value computation different from unrealized_pl computation

Public API:
  audit_equity_identity(profile_id)         per-profile
  audit_equity_identity_all(profile_ids)    batch
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)


# Equity identity must hold within $1 — anything bigger means the
# journal's own algebra is broken.
_EQUITY_TOLERANCE = 1.00


def audit_equity_identity(profile_id: int) -> Dict[str, Any]:
    """Check the master invariant:

        equity == initial_capital + Σ(realized_pnl) + Σ(unrealized_pnl)

    Realized P&L comes from the `pnl` column on closed trades (populated
    by `journal.reconcile_trade_statuses`'s FIFO matcher). Unrealized
    P&L comes from `get_virtual_positions` on currently-open positions.
    Actual equity comes from `get_virtual_account_info` (cash +
    portfolio_value).

    If these don't match, ONE of the following is wrong:
      - FIFO matcher: pnl column inconsistent with cash flows
      - market_value: differs from unrealized_pl computation
      - Hidden cash flow: deposit, dividend, fee, manual adjustment
        affecting equity without a matching trade row

    Returns:
      {
        'profile_id': int,
        'initial_capital': float,
        'realized_total': float,        # sum of pnl on closed trades
        'unrealized_total': float,      # sum of unrealized_pl on open
        'expected_equity': float,       # init_cap + realized + unrealized
        'actual_equity': float,         # from get_virtual_account_info
        'drift': float,                 # actual - expected
        'has_drift': bool,              # abs(drift) > _EQUITY_TOLERANCE
        'errored': str | None,          # populated if check itself failed
      }
    """
    from models import build_user_context_from_profile
    out: Dict[str, Any] = {
        "profile_id": profile_id,
        "initial_capital": 0.0,
        "realized_total": 0.0,
        "unrealized_total": 0.0,
        "expected_equity": 0.0,
        "actual_equity": 0.0,
        "drift": 0.0,
        "has_drift": False,
        "errored": None,
    }
    try:
        ctx = build_user_context_from_profile(profile_id)
    except Exception as exc:
        out["errored"] = f"build_user_context failed: {type(exc).__name__}: {exc}"
        return out

    initial_capital = float(getattr(ctx, "initial_capital", 0) or 0)
    out["initial_capital"] = initial_capital

    try:
        with sqlite3.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE pnl IS NOT NULL"
            ).fetchone()
            realized_total = float(row[0] or 0)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            realized_total = 0.0
        else:
            out["errored"] = f"realized pnl read failed: {exc}"
            return out
    out["realized_total"] = round(realized_total, 2)

    try:
        from journal import get_virtual_positions
        from client import _make_price_fetcher
        try:
            api = ctx.get_alpaca_api() if hasattr(
                ctx, "get_alpaca_api") else getattr(ctx, "api", None)
            fetcher = _make_price_fetcher(api) if api else None
        except Exception:
            fetcher = None
        positions = get_virtual_positions(
            db_path=ctx.db_path, price_fetcher=fetcher,
        )
        unrealized_total = sum(
            float(p.get("unrealized_pl", 0) or 0) for p in positions
        )
    except Exception as exc:
        out["errored"] = (
            f"unrealized read failed: {type(exc).__name__}: {exc}"
        )
        return out
    out["unrealized_total"] = round(unrealized_total, 2)

    try:
        from journal import get_virtual_account_info
        # Reuse the same price_fetcher so unrealized and equity see
        # identical marks — otherwise a snapshot lag would show up as
        # false drift.
        account = get_virtual_account_info(
            db_path=ctx.db_path,
            initial_capital=initial_capital,
            price_fetcher=fetcher,
        )
        actual_equity = float(account.get("equity", 0) or 0)
    except Exception as exc:
        out["errored"] = (
            f"actual equity read failed: {type(exc).__name__}: {exc}"
        )
        return out
    out["actual_equity"] = round(actual_equity, 2)

    expected_equity = initial_capital + realized_total + unrealized_total
    out["expected_equity"] = round(expected_equity, 2)
    out["drift"] = round(actual_equity - expected_equity, 2)
    out["has_drift"] = abs(out["drift"]) > _EQUITY_TOLERANCE
    return out


def audit_equity_identity_all(profile_ids: Iterable[int]) -> Dict[str, Any]:
    """Batch wrapper. Returns:
      {
        'profiles': [per-profile dict, ...],
        'drift': [profiles where has_drift is True],
        'errored': [profile_ids that errored],
      }
    """
    profiles: List[Dict[str, Any]] = []
    drift: List[Dict[str, Any]] = []
    errored: List[int] = []
    for pid in profile_ids:
        row = audit_equity_identity(pid)
        profiles.append(row)
        if row["errored"]:
            errored.append(pid)
            continue
        if row["has_drift"]:
            drift.append(row)
    return {"profiles": profiles, "drift": drift, "errored": errored}


def format_identity_drift_summary(audit: Dict[str, Any]) -> str:
    drift = audit.get("drift", [])
    if not drift:
        return "equity-identity audit: 0 drift items, every profile's algebra balances"
    lines = [f"equity-identity audit: {len(drift)} drift item(s)"]
    for d in drift:
        lines.append(
            f"  pid={d['profile_id']}: "
            f"init=${d['initial_capital']:>10,.2f}  "
            f"realized=${d['realized_total']:>+10,.2f}  "
            f"unrealized=${d['unrealized_total']:>+10,.2f}  "
            f"expected=${d['expected_equity']:>+12,.2f}  "
            f"actual=${d['actual_equity']:>+12,.2f}  "
            f"drift=${d['drift']:>+10,.2f}"
        )
    return "\n".join(lines)
