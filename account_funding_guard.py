"""Broker-funding guard — halt within ONE cycle when an execution
account's money is gone.

Born 2026-06-12. The 6-12 paper accounts were verified at $1M each
at 03:15 UTC; by the 13:30 open they were $0 at the broker (zero
fills — the funding itself vanished at Alpaca's dashboard level,
which the trading API can neither cause nor prevent). The system
then ate SIX HOURS of 'insufficient buying power' rejections in
silence: warnings in logs, ERROR badges in the brain, nothing on
the dashboard, no halt. The operator found out after the close.

This guard makes that day structurally impossible to repeat:
every scan cycle compares the broker account's live equity against
the combined initial capital of the enabled profiles that trade
through it. A material shortfall (default: below 50%, which P&L
swings can't plausibly produce but vanished funding always does)
HALTS the profile via halt_and_alert — which feeds the dashboard
TRADING HALTED banner — and the halt self-clears on the first
cycle after funding is restored.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import closing
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

HALT_REASON_PREFIX = "Broker account funding missing:"
SHORTFALL_FRACTION = 0.50   # halt when equity < 50% of expected
_CACHE_TTL_SECONDS = 60     # one broker GET per account per minute

_cache_lock = threading.Lock()
_equity_cache: dict = {}    # alpaca_account_id -> (epoch, equity)


def _expected_capital(account_id) -> float:
    """Combined initial capital of enabled profiles on this account."""
    import config
    with closing(sqlite3.connect(config.DB_PATH)) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(initial_capital), 0) "
            "FROM trading_profiles "
            "WHERE enabled = 1 AND alpaca_account_id = ?",
            (account_id,),
        ).fetchone()
    return float(row[0] or 0)


def _broker_equity(ctx) -> Optional[float]:
    """Live account equity from the broker, cached per account."""
    account_id = getattr(ctx, "alpaca_account_id", None)
    now = time.time()
    with _cache_lock:
        hit = _equity_cache.get(account_id)
        if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]
    try:
        api = ctx.get_alpaca_api()
        eq = float(api.get_account().equity)
    except Exception as exc:
        logger.warning(
            "funding guard: broker equity fetch failed for account "
            "%s (%s) — not blocking this cycle; broker_health owns "
            "disconnect handling", account_id, exc,
        )
        return None
    with _cache_lock:
        _equity_cache[account_id] = (now, eq)
    return eq


def funding_status(ctx) -> Tuple[bool, str]:
    """(funded, detail). funded=True also on broker-unreachable —
    refusing to trade on a transient API blip is broker_health's
    call, not ours."""
    account_id = getattr(ctx, "alpaca_account_id", None)
    if account_id is None:
        return True, "no alpaca_account_id on ctx — guard skipped"
    expected = _expected_capital(account_id)
    if expected <= 0:
        return True, "no expected capital recorded — guard skipped"
    equity = _broker_equity(ctx)
    if equity is None:
        return True, "broker unreachable — deferred to broker_health"
    if equity < expected * SHORTFALL_FRACTION:
        return False, (
            f"broker equity ${equity:,.0f} vs ${expected:,.0f} "
            f"combined profile capital on account {account_id} — "
            f"funding is missing at the broker"
        )
    return True, f"broker equity ${equity:,.0f} / expected ${expected:,.0f}"


def enforce_funding(ctx) -> bool:
    """Called at the top of every scan cycle. Returns True to
    proceed. On missing funding: halts the profile (dashboard
    banner + audit alert) and returns False. Self-heals: clears
    its own halt on the first funded cycle."""
    from halt_helpers import is_halted, clear_halt, halt_and_alert
    pid = getattr(ctx, "profile_id", None)
    funded, detail = funding_status(ctx)
    if funded:
        if pid is not None:
            try:
                halted, reason = is_halted(pid)
                if halted and reason and reason.startswith(
                        HALT_REASON_PREFIX):
                    clear_halt(pid, source="funding_restored")
                    logger.info(
                        "funding guard: funding restored for pid %s "
                        "(%s) — halt cleared", pid, detail,
                    )
            except Exception as exc:
                logger.warning(
                    "funding guard: auto-clear check failed for pid "
                    "%s: %s", pid, exc,
                )
        return True
    logger.error(
        "funding guard: HALTING pid %s — %s", pid, detail,
    )
    if pid is not None:
        try:
            halt_and_alert(
                profile_id=pid,
                db_path=getattr(ctx, "db_path", None),
                alert_type="broker_funding_missing",
                title=f"{HALT_REASON_PREFIX} profile halted",
                detail=(
                    f"{detail}\n\nEvery order would be rejected "
                    "'insufficient buying power' (the 2026-06-12 "
                    "silent dead-day class). Restore funding at the "
                    "Alpaca dashboard; the halt clears itself on the "
                    "next funded cycle."
                ),
            )
        except Exception as exc:
            logger.error(
                "funding guard: halt_and_alert failed for pid %s: %s "
                "— trading still blocked by the False return",
                pid, exc,
            )
    return False
