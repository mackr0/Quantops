"""Unified alt-data cache (docs/21, 2026-05-20).

Eliminates the cold-start tax at market open by caching the 25
daily-cadence alt-data sources at known TTLs. Pre-warmed by a
04:00 ET daily task; queried by every per-candidate alt-data
fetch in the per-cycle path.

The cache is intentionally SIMPLE — one SQLite table, upsert
on write, lazy TTL check on read. No locking concerns since
SQLite WAL handles single-writer-many-reader; the worst case
under concurrent writers is INSERT OR REPLACE racing, which
produces one definite row (no corruption).

CRITICAL CONTRACTS (load-bearing):
1. Every read returns the same DICT SHAPE as the underlying
   live-fetch would. Wrappers must be transparent — the AI
   prompt builder must see identical data whether it came
   from cache or live.
2. Cache failures NEVER block the live fetch. A missing/locked/
   corrupted DB means cache_get returns None and the wrapper
   falls through to live fetch — same behavior as today
   without the cache.
3. The cache stores RECENT data only. TTL enforcement is at
   read time (we don't return stale rows); pruning at write
   time (we replace expired rows on next fetch).

See docs/21_ALTDATA_PREMARKET_WARMUP.md for the full scoping
including per-source TTL rationale + failure modes + the
phased rollout plan.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache file location
# ---------------------------------------------------------------------------

_CACHE_DIR = os.path.join("altdata", "cache")
_CACHE_DB = os.path.join(_CACHE_DIR, "static_altdata.db")


def _ensure_cache_db() -> str:
    """Make sure the cache directory + table exist. Returns the path."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with closing(sqlite3.connect(_CACHE_DB)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS altdata_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                ttl_seconds INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(symbol, source)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_altdata_cache_expires "
            "ON altdata_cache(expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_altdata_cache_symbol_source "
            "ON altdata_cache(symbol, source)"
        )
        conn.commit()
    return _CACHE_DB


# ---------------------------------------------------------------------------
# Per-source TTL — single source of truth
# ---------------------------------------------------------------------------

SOURCE_TTL_SECONDS = {
    # Insider / FINRA / dark pool — daily reporting cadence
    "insider": 86_400,
    "insider_cluster": 86_400,
    "insider_earnings": 86_400,
    "short": 86_400,
    "finra_short_vol": 86_400,
    "dark_pool": 86_400,
    # Analyst / earnings
    "analyst_estimates": 86_400,
    "earnings_surprise": 86_400,
    # Fundamentals — quarterly cadence; weekly refresh enough
    "fundamentals": 86_400 * 7,
    # Congressional + 13F + 13D/G filings
    "congressional_recent": 86_400,
    "institutional_13f": 86_400 * 7,    # quarterly
    "activist_13dg": 86_400,
    # Biotech / event-based
    "biotech_milestones": 86_400,
    "fda_inspections": 86_400,
    "nhtsa_recalls": 86_400,
    "sam_gov_contracts": 86_400,
    "epa_osha_violations": 86_400,
    # Web-scraped attention signals
    "stocktwits_sentiment": 1_800,      # 30 min (near-real-time but cacheable)
    "google_trends": 86_400,             # daily; rate-limited
    "wikipedia_pageviews": 86_400,
    "wikipedia_edits": 86_400,
    "app_store_ranking": 86_400,
    "github_activity": 86_400,
    # Tier-3 sources
    "risk_factor_diff": 86_400 * 7,     # quarterly 10-K/10-Q
    "bls_jobless_claims": 86_400 * 7,   # weekly Thursday release
    "uspto_patents": 86_400 * 7,
    "job_postings": 86_400,
    "insider_track_records": 86_400 * 7,
    "star_manager_holdings": 86_400 * 7,
    # Options — partial caching; rapidly changes intraday
    "options": 300,                      # 5 min
    # Sources NOT in this dict (NOT cached):
    #   - intraday (cycle-fresh — last 5 min matters)
    #   - recent_8k_events (8:30am morning filings matter)
    #   - macro (already cached once-per-cycle elsewhere)
}


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def cache_get(symbol: str, source: str) -> Optional[Dict[str, Any]]:
    """Return the cached payload for (symbol, source) if it exists
    AND is not expired. Return None otherwise.

    Defensive: any DB error returns None so the caller can fall
    through to live fetch. We NEVER want a cache problem to block
    a real data request.
    """
    if not symbol or not source:
        return None
    try:
        _ensure_cache_db()
        with closing(sqlite3.connect(_CACHE_DB)) as conn:
            row = conn.execute(
                "SELECT payload_json, expires_at FROM altdata_cache "
                "WHERE symbol = ? AND source = ?",
                (symbol.upper(), source),
            ).fetchone()
        if not row:
            return None
        payload_json, expires_at = row
        # Lazy TTL check — even if a stale row sits in the DB,
        # we don't return it. Pruning happens lazily on the next
        # write OR via the daily evict_stale task.
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                return None
        except Exception:
            # Malformed expires_at: treat as stale
            return None
        try:
            return json.loads(payload_json)
        except json.JSONDecodeError:
            logger.warning(
                "alt_data_cache: corrupt payload for (%s, %s) — "
                "treating as cache miss; will re-fetch live",
                symbol, source,
            )
            return None
    except Exception as exc:
        # Cache failure must NEVER block the live fetch. Per
        # `feedback_no_silent_failures`, log but continue.
        logger.warning(
            "alt_data_cache.cache_get(%s, %s) failed: %s: %s — "
            "falling through to live fetch",
            symbol, source, type(exc).__name__, exc,
        )
        return None


def cache_set(symbol: str, source: str, payload: Dict[str, Any],
              ttl_seconds: Optional[int] = None) -> bool:
    """Upsert payload for (symbol, source). TTL defaults to
    `SOURCE_TTL_SECONDS[source]`. Returns True on success, False
    on any failure (failure is non-fatal; caller continues).

    Idempotent: writing the same (symbol, source) twice replaces
    the prior row via INSERT OR REPLACE.
    """
    if not symbol or not source:
        return False
    if ttl_seconds is None:
        ttl_seconds = SOURCE_TTL_SECONDS.get(source)
        if ttl_seconds is None:
            # Source not in the canonical TTL config — refuse to
            # cache (better to live-fetch than to guess at TTL).
            logger.debug(
                "alt_data_cache: source %r not in SOURCE_TTL_SECONDS; "
                "skipping cache write",
                source,
            )
            return False
    try:
        payload_json = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "alt_data_cache: payload for (%s, %s) not JSON-serializable: "
            "%s: %s — skipping cache",
            symbol, source, type(exc).__name__, exc,
        )
        return False
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    try:
        _ensure_cache_db()
        with closing(sqlite3.connect(_CACHE_DB)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO altdata_cache "
                "(symbol, source, payload_json, fetched_at, "
                " ttl_seconds, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol.upper(), source, payload_json,
                 now.isoformat(), ttl_seconds, expires_at),
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning(
            "alt_data_cache.cache_set(%s, %s) failed: %s: %s — "
            "data not cached (no operational impact)",
            symbol, source, type(exc).__name__, exc,
        )
        return False


def cache_or_fetch(source: str, symbol: str,
                    fetcher_fn: Callable[[str], Dict[str, Any]],
                    ttl_seconds: Optional[int] = None,
                    ) -> Dict[str, Any]:
    """The wrapper every cached source getter uses.

    Returns cached payload if fresh; otherwise calls `fetcher_fn(symbol)`,
    writes the result to cache, and returns it.

    Guarantees:
    - Identical return shape to `fetcher_fn` (the cache is transparent)
    - Cache failures don't block fetching (always returns SOMETHING
      sane — either cached data or fresh data)
    - Fetcher exceptions propagate — callers should already wrap their
      alt-data calls in try/except where appropriate
    """
    cached = cache_get(symbol, source)
    if cached is not None:
        return cached
    result = fetcher_fn(symbol)
    if result is not None:
        cache_set(symbol, source, result, ttl_seconds=ttl_seconds)
    return result


def evict_stale() -> int:
    """DELETE rows whose `expires_at` is in the past. Returns the
    number deleted. Called by a daily task to bound cache size.

    Lazy TTL on read already prevents stale rows from being returned;
    eviction is purely a disk-space concern.
    """
    try:
        _ensure_cache_db()
        with closing(sqlite3.connect(_CACHE_DB)) as conn:
            cur = conn.execute(
                "DELETE FROM altdata_cache "
                "WHERE expires_at < ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception as exc:
        logger.warning(
            "alt_data_cache.evict_stale failed: %s: %s",
            type(exc).__name__, exc,
        )
        return 0


def cache_stats() -> Dict[str, Any]:
    """Aggregate cache stats for /altdata dashboard. Returns:
        {
          "total_rows": int,
          "fresh_rows": int,
          "stale_rows": int,
          "per_source": {source: {"fresh": int, "stale": int}, ...},
          "oldest_fresh_fetched_at": str | None,
          "newest_fresh_fetched_at": str | None,
        }
    Defensive: any DB error returns the empty-stats shape so the
    dashboard renders without crashing.
    """
    out = {
        "total_rows": 0, "fresh_rows": 0, "stale_rows": 0,
        "per_source": {}, "oldest_fresh_fetched_at": None,
        "newest_fresh_fetched_at": None,
    }
    try:
        _ensure_cache_db()
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(_CACHE_DB)) as conn:
            out["total_rows"] = conn.execute(
                "SELECT COUNT(*) FROM altdata_cache"
            ).fetchone()[0]
            out["fresh_rows"] = conn.execute(
                "SELECT COUNT(*) FROM altdata_cache "
                "WHERE expires_at >= ?", (now_iso,),
            ).fetchone()[0]
            out["stale_rows"] = out["total_rows"] - out["fresh_rows"]
            for source, fresh in conn.execute(
                "SELECT source, COUNT(*) FROM altdata_cache "
                "WHERE expires_at >= ? GROUP BY source",
                (now_iso,),
            ).fetchall():
                out["per_source"].setdefault(
                    source, {"fresh": 0, "stale": 0},
                )["fresh"] = fresh
            for source, stale in conn.execute(
                "SELECT source, COUNT(*) FROM altdata_cache "
                "WHERE expires_at < ? GROUP BY source",
                (now_iso,),
            ).fetchall():
                out["per_source"].setdefault(
                    source, {"fresh": 0, "stale": 0},
                )["stale"] = stale
            row = conn.execute(
                "SELECT MIN(fetched_at), MAX(fetched_at) "
                "FROM altdata_cache WHERE expires_at >= ?",
                (now_iso,),
            ).fetchone()
            if row and row[0]:
                out["oldest_fresh_fetched_at"] = row[0]
                out["newest_fresh_fetched_at"] = row[1]
    except Exception as exc:
        logger.warning(
            "alt_data_cache.cache_stats failed: %s: %s",
            type(exc).__name__, exc,
        )
    return out


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """Master kill-switch for the cache. When OFF, cache_or_fetch
    short-circuits to direct live-fetch — instant revert without
    code deploy. Set via env var `ALTDATA_CACHE_ENABLED=0` to
    disable.
    """
    return os.environ.get("ALTDATA_CACHE_ENABLED", "1") != "0"
