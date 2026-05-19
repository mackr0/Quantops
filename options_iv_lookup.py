"""Shared per-cycle IV lookup factory.

Built around `options_oracle.get_options_oracle`. Returns a callable
that resolves an underlying ticker → ATM call IV (annualized
decimal, e.g. 0.35 = 35%). The factory caches per-call so multiple
positions on the same underlying don't re-fetch the chain.

Lives outside `pipelines/risk/exposure.py` to break a circular dep:
`options_greeks_aggregator` is imported BY `pipelines.risk.exposure`,
so anything `options_greeks_aggregator` wants to import from this
module must NOT come back through exposure.

The 2026-05-19 wiring: every IV-consuming code path (compute_book_greeks,
portfolio_delta_exposure, effective_positions_for_risk_model) defaults
to this factory when no caller-provided iv_lookup is passed. Before
this change, only effective_positions_for_risk_model used it; the
others silently fell back to FALLBACK_IV=0.25, which understates
delta-adjusted exposure on high-IV underlyings and overstates it on
low-IV underlyings.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


def default_iv_lookup_factory() -> Callable[[str], Optional[float]]:
    """Return a per-call cached callable: `underlying → ATM IV`.

    Cache is closure-scoped so a NEW cache is built per factory
    invocation; callers should call once at the top of a cycle and
    reuse the returned callable for every position lookup in that
    cycle. Reusing across cycles is safe but stale data may persist;
    the cache is small (one entry per underlying) so cost is
    negligible.

    Failure modes (each returns None and caches None to avoid
    re-querying within the cycle):
      - options_oracle import fails
      - get_options_oracle returns no oracle
      - oracle reports has_options=False (symbol has no listed options)
      - skew.call_iv is missing or non-positive
    """
    _cache: Dict[str, Optional[float]] = {}

    def lookup(underlying: str) -> Optional[float]:
        if not underlying:
            return None
        if underlying in _cache:
            return _cache[underlying]
        try:
            from options_oracle import get_options_oracle
            oracle = get_options_oracle(underlying)
            if not oracle or not oracle.get("has_options"):
                _cache[underlying] = None
                return None
            iv = float(oracle.get("skew", {}).get("call_iv") or 0)
            if iv <= 0:
                _cache[underlying] = None
                return None
            _cache[underlying] = iv
            return iv
        except Exception as exc:
            logger.debug("IV lookup for %s failed: %s: %s",
                          underlying, type(exc).__name__, exc)
            _cache[underlying] = None
            return None

    return lookup
