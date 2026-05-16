"""Data-source health check — fails LOUDLY when a critical data
source has degraded silently.

The 2026-05-15 incident: master Alpaca key was revoked.
`market_data.get_bars` has a yfinance fallback (line 184) that
fires silently when Alpaca returns nothing — no log, no alert.
For some unknown period of time, every bar fetch system-wide
served yfinance data while paying for Alpaca. The fact that
predictions kept being recorded and trades kept firing masked
the regression entirely.

This module runs ONCE PER SCHEDULER CYCLE and probes each critical
data source against a known-liquid symbol. If a source has degraded:
  1. Emit a WARNING log
  2. Write an activity_log entry (operator-visible)
  3. Optionally page (email via notify_error)

Probes:
  - Alpaca daily bars on SPY (the most-traded ETF in existence — if
    this fails the data API is down or the keys are dead)
  - Alpaca options chain on SPY (separate auth/permission scope)
  - Alpaca news API on SPY
  - earnings_calendar (yfinance — grandfathered, but the most
    common yfinance path; broken here means upstream pipelines fail)
  - sector_classifier (yfinance — grandfathered)

Each probe records pass/fail with the specific failure reason.
The aggregated result is exposed via `current_health()` for the
dashboard.

NOT a Pythonic monitoring tool. Just a deterministic check that
runs in-process; no extra dependencies.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# In-memory cache of last probe results, keyed by source name.
# Dashboard reads this. Cleared on process restart (intentional —
# fresh state per scheduler instance).
_last_health: Dict[str, Dict[str, Any]] = {}


def _record(source: str, ok: bool, detail: str = "") -> None:
    _last_health[source] = {
        "source": source,
        "ok": ok,
        "detail": detail,
        "checked_at": time.time(),
    }


def probe_alpaca_bars() -> bool:
    """Probe Alpaca daily bars endpoint via the live client.

    Returns True if Alpaca returned non-empty bars for SPY. False if
    the fallback would have fired (which is what we silently missed
    for who knows how long)."""
    try:
        from market_data import _fetch_via_alpaca
        df = _fetch_via_alpaca("SPY", 5)
        if df is None or len(df) == 0:
            _record(
                "alpaca_bars", False,
                "Alpaca returned no bars for SPY — keys may be "
                "revoked or data subscription lapsed. yfinance "
                "fallback is firing system-wide.",
            )
            return False
        _record("alpaca_bars", True, f"{len(df)} bars returned for SPY")
        return True
    except Exception as exc:
        _record(
            "alpaca_bars", False,
            f"{type(exc).__name__}: {exc}",
        )
        return False


def probe_alpaca_options() -> bool:
    """Probe Alpaca options chain for SPY (the most-liquid options
    contract in existence). Failure here breaks 3 strategies +
    every options ensemble specialist."""
    try:
        from options_chain_alpaca import fetch_chain_alpaca
        chain = fetch_chain_alpaca("SPY")
        if not chain or "near_term" not in chain:
            _record(
                "alpaca_options", False,
                "Alpaca options endpoint returned None for SPY — "
                "401 or subscription issue. max_pain_pinning, "
                "high_iv_rank_fade, iv_regime_short will not fire.",
            )
            return False
        n_calls = len(chain["near_term"].get("calls", []))
        n_puts = len(chain["near_term"].get("puts", []))
        _record(
            "alpaca_options", True,
            f"chain with {n_calls} calls + {n_puts} puts on near-term expiration",
        )
        return True
    except Exception as exc:
        _record(
            "alpaca_options", False,
            f"{type(exc).__name__}: {exc}",
        )
        return False


def probe_alpaca_news() -> bool:
    """Probe Alpaca News API — feeds news_sentiment + AI prompt
    injection sites."""
    try:
        from news_sentiment import fetch_news_alpaca
        items = fetch_news_alpaca("SPY", limit=3)
        if not items:
            _record(
                "alpaca_news", False,
                "Alpaca news endpoint returned empty for SPY — "
                "401 or rate limit. news_sentiment_spike will not "
                "fire and AI prompt injection loses news context.",
            )
            return False
        _record("alpaca_news", True, f"{len(items)} news items for SPY")
        return True
    except Exception as exc:
        _record(
            "alpaca_news", False,
            f"{type(exc).__name__}: {exc}",
        )
        return False


def probe_earnings_calendar() -> bool:
    """yfinance-backed earnings calendar (grandfathered exception).
    Breaking here disables earnings_drift + earnings_disaster_short
    fallbacks + AI earnings-context injection."""
    try:
        from earnings_calendar import check_earnings
        result = check_earnings("AAPL")
        if result is None:
            _record(
                "earnings_calendar", False,
                "check_earnings returned None for AAPL — yfinance "
                "may be rate-limited or schema-drifted.",
            )
            return False
        _record(
            "earnings_calendar", True,
            f"AAPL earnings date: {result.get('earnings_date')}",
        )
        return True
    except Exception as exc:
        _record(
            "earnings_calendar", False,
            f"{type(exc).__name__}: {exc}",
        )
        return False


def probe_sector_classifier() -> bool:
    """yfinance-backed sector lookup (grandfathered)."""
    try:
        from sector_classifier import get_sector
        sector = get_sector("AAPL")
        if not sector or sector == "unknown":
            _record(
                "sector_classifier", False,
                f"get_sector('AAPL') returned '{sector}' — "
                "yfinance schema drift or rate limit.",
            )
            return False
        _record("sector_classifier", True, f"AAPL → sector='{sector}'")
        return True
    except Exception as exc:
        _record(
            "sector_classifier", False,
            f"{type(exc).__name__}: {exc}",
        )
        return False


# Critical = must work for system integrity. Non-critical = warn only.
_CRITICAL_PROBES = (
    ("alpaca_bars", probe_alpaca_bars),
    ("alpaca_options", probe_alpaca_options),
    ("alpaca_news", probe_alpaca_news),
)
_ADVISORY_PROBES = (
    ("earnings_calendar", probe_earnings_calendar),
    ("sector_classifier", probe_sector_classifier),
)


def run_all_probes() -> Dict[str, Any]:
    """Run every probe. Returns aggregate health dict for the
    dashboard / alerts. ALWAYS runs every probe regardless of
    individual failure (so the dashboard shows the full picture)."""
    critical_failures: List[str] = []
    advisory_failures: List[str] = []

    for name, fn in _CRITICAL_PROBES:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            _record(name, False, f"probe crashed: {type(exc).__name__}: {exc}")
        if not ok:
            critical_failures.append(name)

    for name, fn in _ADVISORY_PROBES:
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            _record(name, False, f"probe crashed: {type(exc).__name__}: {exc}")
        if not ok:
            advisory_failures.append(name)

    return {
        "checked_at": time.time(),
        "all_critical_ok": not critical_failures,
        "critical_failures": critical_failures,
        "advisory_failures": advisory_failures,
        "per_source": dict(_last_health),
    }


def current_health() -> Dict[str, Any]:
    """Read-only accessor for the dashboard. Returns the most
    recent probe results from the in-process cache. Empty dict if
    no probe has run yet."""
    return dict(_last_health)


def alert_on_critical_failure(health: Dict[str, Any],
                               profile_id: int = 0,
                               user_id: int = 1) -> None:
    """If any CRITICAL probe failed, emit an activity_log entry
    and (debounced) email. Idempotent per source per process run
    so we don't spam on every scheduler cycle while a source is
    degraded."""
    if health["all_critical_ok"]:
        return
    failures = health["critical_failures"]
    msg_lines = []
    for name in failures:
        rec = health["per_source"].get(name, {})
        msg_lines.append(f"  - {name}: {rec.get('detail', 'unknown')}")
    detail = (
        "Critical data-source health probe FAILED. The system would "
        "now be silently degraded — yfinance fallback firing OR "
        "data ensemble specialists / strategies that depend on these "
        "sources are returning empty.\n\nFailed probes:\n"
        + "\n".join(msg_lines)
        + "\n\nThis alert exists because the 2026-05-15 incident "
        "had the master Alpaca key revoked and the bar fetcher "
        "silently fell back to yfinance for an unknown period. "
        "The health probe runs every scheduler cycle to ensure "
        "the same regression cannot recur silently."
    )
    logger.warning("DATA SOURCE HEALTH FAILURE: %s", failures)

    # Avoid duplicate alerts — only fire one per source per process.
    fired_key = f"_health_alert_fired_{','.join(sorted(failures))}"
    if fired_key in _last_health:
        return
    _last_health[fired_key] = {"fired_at": time.time()}

    try:
        from notifications import notify_error
        notify_error(
            error_msg=detail,
            context=f"Data-source health failure: {', '.join(failures)}",
        )
    except Exception as exc:
        logger.debug("notify_error skipped: %s", exc)

    if profile_id:
        try:
            from models import log_activity
            log_activity(
                profile_id=profile_id,
                user_id=user_id,
                activity_type="data_source_health_failure",
                title=f"Data source health: {', '.join(failures)} FAILED",
                detail=detail,
            )
        except Exception as exc:
            logger.debug("log_activity skipped: %s", exc)
