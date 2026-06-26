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


# ─────────────────────────────────────────────────────────────────────
# Reconciler heartbeat (#170, 2026-05-17)
# ─────────────────────────────────────────────────────────────────────
#
# All six integrity audits are useless if the reconciler isn't
# actually running. A silent cron failure (deploy broke crontab,
# scheduler crashed, host went down) would let drift accumulate
# unbounded.
#
# This check scans each profile's task_runs table for the latest
# successful "Reconcile Trade Statuses" run. If older than
# _RECONCILER_MAX_AGE_MINUTES, the reconciler is considered stale.

# Reconciler runs every exit-check cycle (5 min). 60 minutes is
# 12 missed cycles — a real outage, not just a single slow cycle.
_RECONCILER_MAX_AGE_MINUTES = 60
# Task name written by multi_scheduler when scheduling the reconciler.
# Match the literal label string used in run_task().
_RECONCILER_TASK_NAME_FRAGMENTS = ("Reconcile Trade Statuses",)


def audit_reconciler_heartbeat(
    profile_id: int,
    max_age_minutes: int = _RECONCILER_MAX_AGE_MINUTES,
) -> Dict[str, Any]:
    """Verify the per-profile reconciler ran recently. Returns:
      {
        'profile_id': int,
        'latest_run_at': str | None,    # ISO timestamp, None if never ran
        'age_minutes': float | None,
        'max_age_minutes': int,
        'has_drift': bool,              # True if stale
        'errored': str | None,
      }
    """
    from models import build_user_context_from_profile
    out: Dict[str, Any] = {
        "profile_id": profile_id,
        "latest_run_at": None,
        "age_minutes": None,
        "max_age_minutes": max_age_minutes,
        "has_drift": False,
        "expected_running": None,
        "errored": None,
    }
    try:
        ctx = build_user_context_from_profile(profile_id)
    except Exception as exc:
        out["errored"] = f"build_user_context: {type(exc).__name__}: {exc}"
        return out

    # 2026-06-26 — the reconciler runs ONLY while the profile's own schedule is
    # active (market_hours / extended_hours / 24_7 / custom). Off-schedule —
    # nights, weekends, holidays, the freshly-reset hours before the first
    # cycle — it is INTENTIONALLY idle, so a stale heartbeat then is expected,
    # not an outage. Flagging it floods /issues with a false heartbeat per
    # profile every evening and all weekend. Gate staleness on the SAME
    # predicate the scheduler uses to decide whether to run a cycle, so the
    # check is correct for every schedule type. The second call (max_age ago)
    # skips the brief window right after the session opens, before the day's
    # first cycle has had a chance to stamp a fresh run.
    from datetime import (datetime as _dt, timezone as _tz,
                          timedelta as _td)
    from zoneinfo import ZoneInfo as _ZI
    now_et = _dt.now(_ZI("America/New_York"))
    try:
        expected_running = bool(
            ctx.is_within_schedule(now_et)
            and ctx.is_within_schedule(now_et - _td(minutes=max_age_minutes))
        )
    except Exception:
        # Can't determine the schedule → fail toward DETECTION, not silence:
        # a genuine reconciler outage must still surface.
        expected_running = True
    out["expected_running"] = expected_running

    try:
        with sqlite3.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(started_at) FROM task_runs "
                "WHERE task_name LIKE ?",
                (f"%{_RECONCILER_TASK_NAME_FRAGMENTS[0]}%",),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            # Fresh DB, no task has ever run — stale ONLY if it should be
            # running now (else it's the expected post-reset off-hours idle).
            out["has_drift"] = expected_running
            return out
        out["errored"] = f"task_runs read: {exc}"
        return out

    if row is None or row[0] is None:
        out["has_drift"] = expected_running
        return out

    out["latest_run_at"] = row[0]
    try:
        latest = _dt.fromisoformat(row[0].replace("Z", "+00:00"))
        # Handle naive timestamps as UTC
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=_tz.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        out["errored"] = f"timestamp parse: {exc}"
        return out
    age = (_dt.now(tz=_tz.utc) - latest).total_seconds() / 60.0
    out["age_minutes"] = round(age, 1)
    if expected_running and age > max_age_minutes:
        out["has_drift"] = True
    return out


def audit_reconciler_heartbeat_all(
    profile_ids: Iterable[int],
    max_age_minutes: int = _RECONCILER_MAX_AGE_MINUTES,
) -> Dict[str, Any]:
    profiles: List[Dict[str, Any]] = []
    drift: List[Dict[str, Any]] = []
    errored: List[int] = []
    for pid in profile_ids:
        row = audit_reconciler_heartbeat(pid, max_age_minutes)
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
