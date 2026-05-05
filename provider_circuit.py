"""Per-process circuit breaker for AI provider calls.

Today's 529s from Anthropic were transient — the SDK retried, calls
eventually succeeded. But if Anthropic is down for tens of minutes
(or hours), every profile's scan stalls because there's no
auto-failover to OpenAI / Google. This module gives `call_ai` a
circuit-breaker so:

  - Three consecutive failures (5xx, 529, timeout, connection error)
    on a provider OPEN its circuit.
  - Open circuits skip that provider for `OPEN_COOLDOWN_SECONDS`
    (default 300s = 5 min). During that time, callers fall back to
    the next configured provider.
  - After cool-down a circuit moves to HALF_OPEN — one call gets
    through; success closes it, failure re-opens with extended
    cool-down (exponential backoff up to 30 min).

State is per-process — restarting the scheduler resets all circuits.
That's the right behavior: a deploy / restart is a clean slate.

Failures we count:
  - Any exception whose message contains "529" or "overloaded"
  - Any exception whose message contains "503", "504", "502"
  - urllib / network timeouts
  - Anthropic/OpenAI/Google SDK error classes (we sniff by name)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# Tunable thresholds
FAIL_THRESHOLD = 3            # N consecutive failures opens the circuit
OPEN_COOLDOWN_SECONDS = 300   # 5 min before HALF_OPEN
MAX_COOLDOWN_SECONDS = 1800   # 30 min cap on exponential backoff


class _ProviderState:
    __slots__ = (
        "consecutive_failures", "opened_at", "current_cooldown",
        "lock",
    )

    def __init__(self):
        self.consecutive_failures = 0
        self.opened_at: Optional[float] = None
        self.current_cooldown = OPEN_COOLDOWN_SECONDS
        self.lock = threading.Lock()


_states: Dict[str, _ProviderState] = {}
_global_lock = threading.Lock()


def _state(provider: str) -> _ProviderState:
    with _global_lock:
        if provider not in _states:
            _states[provider] = _ProviderState()
        return _states[provider]


def is_open(provider: str) -> bool:
    """Return True when callers should SKIP this provider and try
    fallback. Half-open state returns False (one call gets through)."""
    s = _state(provider)
    with s.lock:
        if s.opened_at is None:
            return False
        elapsed = time.time() - s.opened_at
        if elapsed >= s.current_cooldown:
            # Cool-down expired — circuit is HALF_OPEN. Let one
            # caller through. The caller's success/failure will be
            # recorded via record_success/record_failure.
            return False
        return True


def record_success(provider: str) -> None:
    """Successful call resets the circuit."""
    s = _state(provider)
    with s.lock:
        if s.opened_at is not None:
            logger.info(
                "AI provider circuit CLOSED for %s (success after %.0fs)",
                provider, time.time() - s.opened_at,
            )
        s.consecutive_failures = 0
        s.opened_at = None
        s.current_cooldown = OPEN_COOLDOWN_SECONDS


def record_failure(provider: str, exc: BaseException) -> None:
    """Failed call. Increment counter; if threshold hit, open circuit."""
    s = _state(provider)
    with s.lock:
        s.consecutive_failures += 1
        # If circuit was already open and we're in HALF_OPEN, this
        # failure re-opens it with exponential backoff.
        if s.opened_at is not None:
            now = time.time()
            elapsed = now - s.opened_at
            if elapsed >= s.current_cooldown:
                # We were HALF_OPEN; bump cooldown and re-open.
                s.current_cooldown = min(
                    s.current_cooldown * 2, MAX_COOLDOWN_SECONDS,
                )
                s.opened_at = now
                logger.warning(
                    "AI provider circuit RE-OPENED for %s "
                    "(half-open call failed; new cooldown %ds): %s",
                    provider, s.current_cooldown, exc,
                )
            return
        if s.consecutive_failures >= FAIL_THRESHOLD:
            s.opened_at = time.time()
            s.current_cooldown = OPEN_COOLDOWN_SECONDS
            logger.warning(
                "AI provider circuit OPEN for %s after %d failures "
                "(cooldown %ds): %s",
                provider, s.consecutive_failures, s.current_cooldown,
                exc,
            )


def reset(provider: Optional[str] = None) -> None:
    """Test helper: reset state for one provider or all."""
    with _global_lock:
        if provider is None:
            _states.clear()
        else:
            _states.pop(provider, None)


def status() -> Dict[str, Dict]:
    """Snapshot of current circuit state for the dashboard."""
    out = {}
    with _global_lock:
        snapshot = list(_states.items())
    for prov, s in snapshot:
        with s.lock:
            if s.opened_at is None:
                out[prov] = {
                    "state": "closed",
                    "consecutive_failures": s.consecutive_failures,
                }
            else:
                elapsed = time.time() - s.opened_at
                if elapsed >= s.current_cooldown:
                    state = "half_open"
                else:
                    state = "open"
                out[prov] = {
                    "state": state,
                    "consecutive_failures": s.consecutive_failures,
                    "opened_at_seconds_ago": round(elapsed, 1),
                    "cooldown_seconds": s.current_cooldown,
                    "seconds_until_half_open": max(
                        0, round(s.current_cooldown - elapsed, 1),
                    ),
                }
    return out
