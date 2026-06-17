"""Experiment-tradable gate — keep the universe aligned with the
institutional benchmark we're competing against.

A name is experiment-tradable iff the broker reports it
``easy_to_borrow`` (which, per the 2026-06-17 asset audit, coincides
exactly with ``shortable``) AND ``tradable`` AND ``active``.
Hard-to-borrow names (``easy_to_borrow=False`` — e.g. ICCM, SUGP, NEOV,
SOUN, TSLG) are precisely the class that:

  * the broker restricts to DAY orders, so our GTC protective brackets
    are REJECTED and the long rides NAKED (the 2026-06-17 ICCM incident:
    "only day orders are allowed for hard-to-borrow asset" — stops
    un-placeable, bracket entries rejected, reconciler safety-net halts);
  * cannot be shorted at all (one leg of long/short is gone);
  * systematic institutional strategies screen OUT (borrow cost, locate,
    capacity) — the funds this experiment benchmarks against do not
    trade them.

This is a SPECIFIC broker-defined class, not a vague liquidity sweep:
the flag is per-asset and queryable. The easy-to-borrow set is pulled
ONCE from Alpaca's ``list_assets`` and cached (asset flags are
~daily-stable), so the per-entry gate is a set lookup, not an API call.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE = {"set": None, "ts": 0.0}      # broker-wide; shared across profiles
_TTL_SECONDS = 12 * 3600


def _etb_set(api) -> Optional[set]:
    """Cached set of easy-to-borrow, tradable, active US-equity symbols.

    Returns the stale cache if a refresh fails, or ``None`` if the broker
    fetch fails AND nothing was ever cached — callers fail OPEN in that
    case so a broker blip never blocks all trading (the screener filter
    is the primary defense; this gate is the backstop).
    """
    now = time.time()
    with _LOCK:
        s = _CACHE["set"]
        if s is not None and (now - _CACHE["ts"]) < _TTL_SECONDS:
            return s
    try:
        assets = api.list_assets(status="active", asset_class="us_equity")
        fresh = {
            (a.symbol or "").upper()
            for a in (assets or [])
            if getattr(a, "easy_to_borrow", False)
            and getattr(a, "tradable", False)
        }
        if not fresh:
            raise ValueError("empty easy_to_borrow set")
        with _LOCK:
            _CACHE["set"] = fresh
            _CACHE["ts"] = now
        return fresh
    except Exception as exc:
        with _LOCK:
            cached = _CACHE["set"]
        logger.warning(
            "tradability: easy_to_borrow asset fetch failed (%s: %s); "
            "using %s", type(exc).__name__, exc,
            "stale cache" if cached is not None else "fail-open (allow)")
        return cached  # stale set if we have one, else None


def is_experiment_tradable(api, symbol: str) -> bool:
    """True iff ``symbol`` is easy-to-borrow / tradable at the broker.

    Fail-open (True) when the broker set can't be built, so a transient
    API error never halts all entries.
    """
    if not symbol:
        return True
    s = _etb_set(api)
    if s is None:
        return True
    return symbol.upper() in s


def filter_tradable(api, symbols: Iterable[str]) -> List[str]:
    """Subset of ``symbols`` that are experiment-tradable, order
    preserved. Fail-open returns the input list unchanged."""
    s = _etb_set(api)
    syms = list(symbols)
    if s is None:
        return syms
    return [x for x in syms if (x or "").upper() in s]
