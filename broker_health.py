"""Broker (Alpaca) health tracker.

If Alpaca is down or unreachable, we can't get_account / list_positions
/ submit orders. Local state diverges from broker state. Stop orders
already submitted to the broker still trigger — but new exits we want
to make queue up. No alert until reconciliation runs daily.

This module gives the trade pipeline a quick "is the broker reachable
right now?" signal so it can short-circuit new entries during an
outage instead of failing one ticker at a time.

State machine (per-process, like provider_circuit):
  HEALTHY    — calls succeed, business as usual
  DEGRADED   — 1-2 recent failures, warn but still attempt
  DISCONNECTED — `BROKER_DOWN_THRESHOLD` consecutive failures.
                 Pre-trade gate refuses new entries with reason
                 "broker disconnected". Auto-clears on first success.

We keep a small in-memory ring of the last N call outcomes so the
threshold is consecutive *recent* failures, not lifetime failures
since process start.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Optional

logger = logging.getLogger(__name__)


# Tunables
BROKER_DOWN_THRESHOLD = 3      # N consecutive recent failures = DISCONNECTED
RECENT_WINDOW = 10             # Keep this many recent outcomes


class _BrokerState:
    __slots__ = ("recent", "lock", "first_failure_at", "last_alert_at")

    def __init__(self):
        # Each entry is True (success) or False (failure)
        self.recent: Deque[bool] = deque(maxlen=RECENT_WINDOW)
        self.lock = threading.Lock()
        self.first_failure_at: Optional[float] = None
        self.last_alert_at: Optional[float] = None


_state = _BrokerState()


def _consecutive_failures(state: _BrokerState) -> int:
    """How many of the most-recent calls failed in a row?"""
    n = 0
    for outcome in reversed(state.recent):
        if outcome:
            break
        n += 1
    return n


def is_disconnected() -> bool:
    """True when the pre-trade gate should refuse new entries."""
    with _state.lock:
        return _consecutive_failures(_state) >= BROKER_DOWN_THRESHOLD


def status() -> dict:
    """Snapshot for dashboard / verify_first_cycle."""
    with _state.lock:
        n = _consecutive_failures(_state)
        if n == 0:
            label = "healthy"
        elif n < BROKER_DOWN_THRESHOLD:
            label = "degraded"
        else:
            label = "disconnected"
        return {
            "status": label,
            "consecutive_failures": n,
            "recent_outcomes": list(_state.recent),
            "first_failure_at": _state.first_failure_at,
        }


def record_success() -> None:
    with _state.lock:
        was_disconnected = (
            _consecutive_failures(_state) >= BROKER_DOWN_THRESHOLD
        )
        _state.recent.append(True)
        _state.first_failure_at = None
    if was_disconnected:
        logger.warning("Broker reconnected — accepting new entries again")


def record_failure(exc: BaseException) -> None:
    """Record a failed broker call. Calls that look like
    auth/permission errors (401/403) are ALSO failures since we
    can't trade without auth — they're not transient but they're
    just as fatal."""
    with _state.lock:
        now = time.time()
        if _state.first_failure_at is None:
            _state.first_failure_at = now
        was_disconnected = (
            _consecutive_failures(_state) >= BROKER_DOWN_THRESHOLD
        )
        _state.recent.append(False)
        n_now = _consecutive_failures(_state)
        becoming_disconnected = (
            not was_disconnected and n_now >= BROKER_DOWN_THRESHOLD
        )
    if becoming_disconnected:
        logger.error(
            "Broker DISCONNECTED — %d consecutive failures. "
            "Pre-trade gate will refuse new entries until next success. "
            "Last error: %s", n_now, exc,
        )


def reset() -> None:
    """Test helper: clear state."""
    global _state
    _state = _BrokerState()


def call_with_health_tracking(fn, *args, **kwargs):
    """Wrap an Alpaca-touching call so its success/failure updates the
    health tracker. Re-raises the original exception so callers see
    the same behavior as before. Use sparingly — only on calls that
    are good signals of broker reachability (account, positions,
    submit_order). Don't wrap data-only calls (get_bars, snapshots)
    since those go to a different Alpaca surface (data API) that
    can be up while the trading API is down."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        record_failure(exc)
        raise
    record_success()
    return result
