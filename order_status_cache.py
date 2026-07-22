"""Process-wide broker order-status cache + 429 circuit breaker.

Born 2026-07-22, the rate-limit death spiral: the protective sweep
re-polled every dead bracket parent (get_order nested) every cycle for
every profile, the fill-updater re-polled every unfilled row, and the
combined volume blew Alpaca's rate limit. The 429s then rejected
LEGITIMATE orders (option closes, covers) and — the vicious part —
starved `_task_update_fills`, the very machinery that voids
journaled-but-never-filled phantom rows. Phantoms accumulated, the
account decomposition broke, and the integrity gate (correctly)
halted the fleet.

Two mechanisms, one module:

1. ORDER-STATUS CACHE — `get_order_cached(api, order_id, nested=...)`.
   A TERMINAL order (filled/canceled/expired/rejected) whose legs are
   all terminal-or-absent can never change again: cached FOREVER, so a
   dead bracket costs ONE api call in the process's lifetime instead
   of one per cycle. Non-terminal orders (and terminal parents with a
   LIVE leg — a bracket child can still be canceled later, so the leg
   set is not immutable) are cached with a short TTL that dedups the
   many same-cycle lookups (sweep + fills + reconciler often poll the
   same order within seconds) while staying fresh across cycles.

2. 429 CIRCUIT BREAKER — any rate-limit error seen through this module
   opens a cooldown (`rate_limited()` returns True). During cooldown:
   cached results (even stale) are served; uncached lookups raise
   immediately WITHOUT hitting the API (callers already treat lookup
   failure as "can't verify" and skip fail-safe); and order-PLACING
   sweeps consult `rate_limited()` to stand down for the cycle rather
   than submit into guaranteed rejections. The breaker sheds the
   optional load so the essential calls (submits, fills healing)
   get the budget.

Thread-safe; all state is process-local (a restart starts clean,
which is correct — cached terminality is re-provable at any time).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Broker statuses that can never transition again.
TERMINAL_STATUSES = frozenset({
    "filled", "canceled", "expired", "rejected", "done_for_day",
})

#: TTL for non-terminal (or not-provably-immutable) cache entries.
NONTERMINAL_TTL_S = 120.0

#: How long the 429 breaker stays open after a rate-limit sighting.
RATE_LIMIT_COOLDOWN_S = 90.0

_lock = threading.Lock()
_cache: dict = {}          # (order_id, nested) -> (fetched_at, order, forever)
_cooldown_until = 0.0


class RateLimitCooldown(RuntimeError):
    """Raised for an uncached lookup while the 429 breaker is open —
    the API is not consulted. Callers already treat any get_order
    failure as 'cannot verify' and skip fail-safe."""


def rate_limited() -> bool:
    """True while the 429 circuit breaker is open."""
    return time.time() < _cooldown_until


def note_rate_limit(source: str = "?") -> None:
    """Open (or extend) the cooldown after a 429 sighting anywhere."""
    global _cooldown_until
    with _lock:
        _cooldown_until = time.time() + RATE_LIMIT_COOLDOWN_S
    logger.warning(
        "429 circuit breaker OPEN for %.0fs (seen at %s) — optional "
        "order polling served from cache / skipped; placing sweeps "
        "stand down this cycle.", RATE_LIMIT_COOLDOWN_S, source,
    )


def _looks_like_rate_limit(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


def _is_immutable(order: Any) -> bool:
    """True iff this order object can never change at the broker again:
    its own status is terminal AND every leg (bracket child) is
    terminal too. A terminal parent with a LIVE leg is NOT immutable —
    the child can still fill or be canceled later."""
    status = (getattr(order, "status", "") or "").lower()
    if status not in TERMINAL_STATUSES:
        return False
    for leg in (getattr(order, "legs", None) or []):
        leg_status = (getattr(leg, "status", "") or "").lower()
        if leg_status not in TERMINAL_STATUSES:
            return False
    return True


def get_order_cached(api, order_id: str, nested: bool = False,
                     ttl: float = NONTERMINAL_TTL_S) -> Optional[Any]:
    """Cached `api.get_order(order_id[, nested=True])`.

    Immutable orders are cached forever; everything else for `ttl`
    seconds. While the 429 breaker is open, serves whatever is cached
    (stale allowed) and raises RateLimitCooldown for cache misses
    instead of touching the API. A genuine 429 from the API opens the
    breaker and re-raises for the caller's existing error handling."""
    key = (order_id, bool(nested))
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        fetched_at, order, forever = hit
        if forever or (now - fetched_at) < ttl:
            return order
        if rate_limited():
            # Stale but the breaker is open — stale beats a 429.
            return order
    elif rate_limited():
        raise RateLimitCooldown(
            "order %s not cached and 429 cooldown active" % order_id)
    try:
        if nested:
            order = api.get_order(order_id, nested=True)
        else:
            order = api.get_order(order_id)
    except Exception as exc:
        if _looks_like_rate_limit(exc):
            note_rate_limit("get_order(%s)" % order_id)
        raise
    if order is not None:
        with _lock:
            _cache[key] = (now, order, _is_immutable(order))
    return order


def clear_cache() -> None:
    """Test hook / manual reset."""
    global _cooldown_until
    with _lock:
        _cache.clear()
        _cooldown_until = 0.0
