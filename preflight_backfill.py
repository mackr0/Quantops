"""Pre-flight alt-data backfill (2026-05-17).

Run this BEFORE the experiment's first market-hours cycle so day-1
scans don't pay the cold-cache cost on every new signal.

Three layers:
  1. PERSISTED scrapes that need historical depth — SEC 8-K and
     SEC 13D/G run now so DB has recent filings to query.
  2. MACRO cache warm — get_all_macro_data forces one fetch of
     every macro source (yield curve, FRED, MOVE/OVX/GVZ, USDA,
     EIA, CFTC, sector_flow_diff) so the 10-min TTL covers cycle 1.
  3. PER-SYMBOL cache warm — for each ticker in the corporate-
     mapped lists (GitHub tech tickers, FDA pharma tickers, NHTSA
     auto tickers, SAM.gov defense tickers), make one fetch so the
     24h cache is hot.

Already-populated (no action needed): Form 4 insiders, 13F holdings,
Congressional trades, StockTwits, biotech events (1M+ rows total).
Placeholders (intentional empty): EPA/OSHA, FAA, USPTO, job_postings.
Derived signals: read from existing DBs, no fetch.

Usage:
    cd /opt/quantopsai && /opt/quantopsai/venv/bin/python preflight_backfill.py
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)


def _h(title: str) -> None:
    log.info("=" * 70)
    log.info(title)
    log.info("=" * 70)


def main() -> int:
    t_start = time.time()

    # ── 1. SEC scrapers — historical depth ───────────────────────────
    _h("1. SEC scrapers (historical depth via atom feeds)")
    try:
        from sec_8k_broad import scrape_recent_8k_filings
        r = scrape_recent_8k_filings(max_filings=100)
        log.info("  SEC 8-K: seen=%d new=%d errors=%d",
                 r["seen"], r["new"], r["errors"])
    except Exception as exc:
        log.error("  SEC 8-K failed: %s: %s", type(exc).__name__, exc)
    try:
        from sec_13dg_activist import scrape_recent_13dg_filings
        r = scrape_recent_13dg_filings(max_per_form=100)
        log.info("  SEC 13D/G: seen=%d new=%d errors=%d",
                 r["seen"], r["new"], r["errors"])
    except Exception as exc:
        log.error("  SEC 13D/G failed: %s: %s", type(exc).__name__, exc)

    # ── 2. Macro cache warm ──────────────────────────────────────────
    _h("2. Macro cache warm (yield_curve, FRED, MOVE/OVX/GVZ, "
       "USDA, EIA, CFTC, sector_flow_diff)")
    try:
        from alternative_data import _get_cached_macro
        macro = _get_cached_macro()
        present = [k for k, v in macro.items() if v]
        log.info("  macro sources warmed: %d → %s",
                 len(present), sorted(present))
    except Exception as exc:
        log.error("  macro warm failed: %s: %s", type(exc).__name__, exc)

    # ── 3. Per-symbol cache warm ─────────────────────────────────────
    _h("3. Per-symbol cache warm (corporate-mapped sources)")
    # Pull the actual mapped ticker lists from the source modules
    # so this stays in sync if mappings expand.
    from altdata_tier2_corporate import (
        _TICKER_TO_GITHUB_ORG, _TICKER_TO_FDA_NAME,
        _TICKER_TO_NHTSA, _TICKER_TO_USA_SPENDING_NAME,
        get_github_activity, get_fda_inspections,
        get_nhtsa_recalls, get_sam_gov_contracts,
    )
    # NHTSA map is now (make, [models]) — keep just keys (tickers)
    _TICKER_TO_NHTSA_MAKE = _TICKER_TO_NHTSA
    from altdata_tier3 import (
        get_wikipedia_edits, get_uspto_patents,
        _TICKER_TO_USPTO_ASSIGNEE,
        get_epa_osha_violations, _TICKER_TO_EPA_FACILITY_NAME,
        get_job_postings_count, _TICKER_TO_GREENHOUSE_BOARD,
    )

    counts = {"github": 0, "fda": 0, "nhtsa": 0, "sam": 0,
              "wiki_edits": 0, "uspto": 0, "epa": 0, "jobs": 0}

    for ticker in _TICKER_TO_GITHUB_ORG.keys():
        try:
            d = get_github_activity(ticker)
            if d.get("has_data"):
                counts["github"] += 1
        except Exception as exc:
            log.debug("github warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_FDA_NAME.keys():
        try:
            d = get_fda_inspections(ticker)
            if d.get("has_data"):
                counts["fda"] += 1
        except Exception as exc:
            log.debug("fda warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_NHTSA_MAKE.keys():
        try:
            d = get_nhtsa_recalls(ticker)
            if d.get("has_data"):
                counts["nhtsa"] += 1
        except Exception as exc:
            log.debug("nhtsa warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_USA_SPENDING_NAME.keys():
        try:
            d = get_sam_gov_contracts(ticker)
            if d.get("has_data"):
                counts["sam"] += 1
        except Exception as exc:
            log.debug("sam warm %s failed: %s", ticker, exc)

    # Wikipedia edits — warm for the same union of mapped tickers
    # since they're the most likely to be picked.
    warm_universe = (
        set(_TICKER_TO_GITHUB_ORG.keys())
        | set(_TICKER_TO_FDA_NAME.keys())
        | set(_TICKER_TO_NHTSA_MAKE.keys())
        | set(_TICKER_TO_USA_SPENDING_NAME.keys())
    )
    for ticker in sorted(warm_universe):
        try:
            d = get_wikipedia_edits(ticker)
            if d.get("has_data"):
                counts["wiki_edits"] += 1
        except Exception as exc:
            log.debug("wiki_edits warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_USPTO_ASSIGNEE.keys():
        try:
            d = get_uspto_patents(ticker)
            if d.get("has_data"):
                counts["uspto"] += 1
        except Exception as exc:
            log.debug("uspto warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_EPA_FACILITY_NAME.keys():
        try:
            d = get_epa_osha_violations(ticker)
            if d.get("has_data"):
                counts["epa"] += 1
        except Exception as exc:
            log.debug("epa warm %s failed: %s", ticker, exc)

    for ticker in _TICKER_TO_GREENHOUSE_BOARD.keys():
        try:
            d = get_job_postings_count(ticker)
            if d.get("has_data"):
                counts["jobs"] += 1
        except Exception as exc:
            log.debug("jobs warm %s failed: %s", ticker, exc)

    log.info("  per-symbol cache warm — tickers returning data:")
    for src, n in sorted(counts.items()):
        log.info("    %-12s: %d", src, n)

    _h("DONE in %.1fs" % (time.time() - t_start))
    log.info(
        "First cycle Monday will read from warmed caches on every "
        "Tier-2/3 source. SEC 8-K + 13D/G DBs have recent atom-feed "
        "snapshots. Macro cache TTL = 10 min so it carries through "
        "the first scheduler cycle."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
