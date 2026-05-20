"""Pre-market alt-data warmup (docs/21, 2026-05-20).

Iterates the active trading universe and pre-fetches the 25
daily-cadence alt-data sources, populating `alt_data_cache`.
Eliminates the ~10-min cold-start tax at market open by moving
the expensive per-candidate network calls out of the trading
window.

Designed to run as a daily cron at 04:00 ET (08:00 UTC during
EDT). Idempotent — repeated runs just refresh the cache.
Rate-limit-aware: Google Trends is paced at 1 req/sec (the
constraint that bit us at 09:30 ET); other sources go faster.

Run manually:
    cd /opt/quantopsai && venv/bin/python3 premarket_warmup.py

Cron (recommended):
    0 8 * * 1-5 cd /opt/quantopsai && /opt/quantopsai/venv/bin/python3 \\
        premarket_warmup.py >> logs/warmup.log 2>&1

Or from another module:
    from altdata_warmup import run_warmup
    summary = run_warmup()
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def _get_universe() -> List[str]:
    """Return the set of symbols we want to pre-warm. Pulled from
    every active profile's recent watchlist + the screener's
    permanent universe so we cover everything a cycle might
    evaluate.

    Returns deduplicated, uppercased ticker list. Crypto symbols
    (with '/') are excluded — they don't use these alt-data
    sources per `get_all_alternative_data`'s early-return.
    """
    symbols: Set[str] = set()

    # 2026-05-20 — universe is now the UNION of:
    #   (a) all 4 cap segments (LARGE/MID/SMALL/MICRO from segments.py)
    #       = 524 unique symbols total, covering everything the
    #         screener could surface for any profile
    #   (b) symbols appearing in any profile's recent cycle_data
    #       shortlist (catches mid-cycle additions / experiment-
    #       specific watchlists)
    #
    # Previously this returned ~31 symbols (just (b) thinned by
    # post-reset shortlist size), which meant first-day cycles
    # cache-missed on most candidates the screener picked. The
    # 524-symbol union covers the screener's canonical universe;
    # cache hit rate should approach 100% on the first cycle that
    # follows a warmup run.
    try:
        from segments import (
            LARGE_CAP_UNIVERSE, MID_CAP_UNIVERSE,
            SMALL_CAP_UNIVERSE, MICRO_CAP_UNIVERSE,
        )
        for u in (LARGE_CAP_UNIVERSE, MID_CAP_UNIVERSE,
                   SMALL_CAP_UNIVERSE, MICRO_CAP_UNIVERSE):
            for sym in u:
                if sym and "/" not in sym:
                    symbols.add(sym.upper())
    except Exception as exc:
        logger.warning(
            "warmup universe: cap-segment import failed (%s); "
            "falling back to cycle_data + seed only",
            exc,
        )

    # Augment with symbols from any profile's most-recent shortlist
    # so we catch experiment-specific additions the cap segments
    # might miss.
    import glob
    import json
    for path in glob.glob("cycle_data_*.json"):
        try:
            with open(path) as f:
                cycle_data = json.load(f)
            for c in cycle_data.get("shortlist", []):
                sym = c.get("symbol")
                if sym and "/" not in sym:
                    symbols.add(sym.upper())
        except Exception as exc:
            logger.debug("warmup universe: skipped %s: %s", path, exc)

    # Last-resort fallback: if both segments AND cycle_data failed,
    # use a static seed list so the warmup still has something to do
    # rather than no-op'ing.
    if not symbols:
        symbols.update([
            "SPY", "QQQ", "IWM", "DIA",
            "AAPL", "MSFT", "GOOG", "AMZN", "META", "TSLA", "NVDA",
            "BRK.B", "JPM", "V", "JNJ", "WMT", "PG", "UNH", "HD",
            "MA", "DIS", "BAC", "PFE", "KO", "PEP", "XOM", "CVX",
            "T", "VZ", "MRK", "INTC", "CSCO", "ADBE", "CRM",
        ])
        logger.warning(
            "warmup universe: every primary source failed — using "
            "%d-symbol static seed as last resort",
            len(symbols),
        )

    logger.info(
        "warmup universe: %d unique symbols (cap-segments + cycle_data)",
        len(symbols),
    )
    return sorted(symbols)


# ---------------------------------------------------------------------------
# Per-source warmup
# ---------------------------------------------------------------------------

# Tuples of (source_name, fetcher_callable, optional_rate_limit_seconds)
# Rate limit is the minimum wait BETWEEN consecutive calls to this
# source. Default 0 (no per-call delay; rely on thread pool sizing).
# Google Trends gets 1.0s because that's what its server cooperates
# with — we'd rather warm 1500 symbols slowly than 30 symbols 13x
# during market open.
_WARMUP_SOURCES: List = []  # populated lazily on first call to avoid import cycles


def _build_warmup_sources():
    """Lazy import + build the warmup-source registry. Done lazily
    so the module imports without forcing alternative_data to load
    (alternative_data has heavy transitive deps)."""
    global _WARMUP_SOURCES
    if _WARMUP_SOURCES:
        return _WARMUP_SOURCES
    from alternative_data import (
        get_insider_activity, get_short_interest, get_fundamentals,
        get_options_unusual, get_finra_short_volume,
        get_insider_cluster, get_analyst_estimates,
        get_insider_earnings_signal, get_dark_pool_volume,
        get_earnings_surprise, get_congressional_recent,
        get_13f_institutional, get_biotech_milestones,
        get_stocktwits_sentiment, get_google_trends_signal,
        get_wikipedia_pageviews_signal, get_app_store_ranking,
        _get_recent_13dg_safe,
    )
    _WARMUP_SOURCES = [
        # source_name, fetcher, rate_limit_seconds
        ("insider", get_insider_activity, 0.0),
        ("short", get_short_interest, 0.0),
        ("fundamentals", get_fundamentals, 0.1),
        ("options", get_options_unusual, 0.05),
        ("finra_short_vol", get_finra_short_volume, 0.0),
        ("insider_cluster", get_insider_cluster, 0.0),
        ("analyst_estimates", get_analyst_estimates, 0.1),
        ("insider_earnings", get_insider_earnings_signal, 0.0),
        ("dark_pool", get_dark_pool_volume, 0.0),
        ("earnings_surprise", get_earnings_surprise, 0.1),
        ("congressional_recent", get_congressional_recent, 0.0),
        ("institutional_13f", get_13f_institutional, 0.0),
        ("biotech_milestones", get_biotech_milestones, 0.0),
        ("stocktwits_sentiment", get_stocktwits_sentiment, 0.5),
        # Google Trends is heavily rate-limited; pace at 1s/req.
        # At 1500 symbols this is 25 min — fits inside the pre-market
        # window (04:00-09:30 ET = 5.5h available).
        ("google_trends", get_google_trends_signal, 1.0),
        ("wikipedia_pageviews", get_wikipedia_pageviews_signal, 0.1),
        ("app_store_ranking", get_app_store_ranking, 0.1),
        ("activist_13dg", _get_recent_13dg_safe, 0.0),
    ]
    return _WARMUP_SOURCES


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_warmup(symbols: Optional[List[str]] = None,
                limit: Optional[int] = None,
                ) -> Dict[str, Dict]:
    """Pre-warm the alt-data cache for every (symbol, source) pair.

    Args:
      symbols: list of tickers to warm. Defaults to _get_universe().
      limit: optional cap on # of symbols (for dry-run / test).

    Returns a per-source summary:
        {source_name: {"fetched": int, "errors": int, "elapsed_s": float}}

    Per `feedback_no_silent_failures`, every per-source exception
    is logged but doesn't abort the whole warmup. A single broken
    source doesn't take down the others.
    """
    from alt_data_cache import cache_set, SOURCE_TTL_SECONDS, is_enabled
    if not is_enabled():
        logger.warning(
            "warmup: ALTDATA_CACHE_ENABLED=0 — cache disabled; "
            "warmup is a no-op until re-enabled"
        )
        return {}

    symbols = symbols or _get_universe()
    if limit:
        symbols = symbols[:limit]
    sources = _build_warmup_sources()

    logger.info(
        "warmup: %d symbols × %d sources = %d (symbol, source) pairs",
        len(symbols), len(sources), len(symbols) * len(sources),
    )

    summary: Dict[str, Dict] = {}
    for source_name, fetcher, rate_limit_s in sources:
        fetched = 0
        errors = 0
        start = time.time()
        ttl_seconds = SOURCE_TTL_SECONDS.get(source_name)
        if ttl_seconds is None:
            logger.warning(
                "warmup: source %s missing from SOURCE_TTL_SECONDS — skip",
                source_name,
            )
            continue
        for symbol in symbols:
            try:
                payload = fetcher(symbol)
                if payload is not None:
                    cache_set(symbol, source_name, payload, ttl_seconds)
                    fetched += 1
            except Exception as exc:
                errors += 1
                logger.debug(
                    "warmup: %s for %s failed: %s: %s",
                    source_name, symbol, type(exc).__name__, exc,
                )
            if rate_limit_s > 0:
                time.sleep(rate_limit_s)
        elapsed = time.time() - start
        summary[source_name] = {
            "fetched": fetched, "errors": errors,
            "elapsed_s": round(elapsed, 1),
        }
        logger.info(
            "warmup: %s done — %d fetched / %d errors / %.1fs",
            source_name, fetched, errors, elapsed,
        )
    return summary


def main():
    """CLI entry point. Logs to stderr; returns 0 on success."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    start = time.time()
    summary = run_warmup()
    total_fetched = sum(s["fetched"] for s in summary.values())
    total_errors = sum(s["errors"] for s in summary.values())
    elapsed = time.time() - start
    logger.info(
        "warmup COMPLETE: %d fetched, %d errors, %.0fs total",
        total_fetched, total_errors, elapsed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
