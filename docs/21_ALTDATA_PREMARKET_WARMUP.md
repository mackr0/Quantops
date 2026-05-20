# Pre-Market Alt-Data Warmup + Cache Layer

**Eliminates the cold-start tax at market open by pre-fetching the 25 daily-cadence alt-data sources at 04:00 ET. Only the 3 truly-intraday sources get live-fetched per cycle.**

Status: IN PROGRESS — design + initial implementation 2026-05-20.
Owner: TBD.
Triggered by: operator's repeated observation that the system "seems stuck" at market open because cold-start cycles take 10+ minutes before the first AI call.
Depends on: nothing (no external dependencies; ships as a self-contained refactor).

---

## 0. TL;DR

At today's market open (2026-05-20 13:30 UTC), 9 AI profiles entered their first cycle. **Wall-clock from screener-done to first AI call: ~9 minutes per profile.** Cause: each candidate (~30 post-screener) triggers ~28 alt-data fetches; most of those are network calls to per-symbol APIs (yfinance, FINRA, SEC, StockTwits, Google Trends, etc.). With a 3-worker thread pool processing 13 profiles, the fleet takes ~40-60 minutes to complete its first cycle of the day.

**Of the 28 alt-data sources, only 3 are genuinely intraday.** The other 25 update at daily-or-slower cadence — insider Form 4s, 13F filings, earnings calendar, FDA inspections, congressional trades, etc. We are re-fetching the same data we had at 16:00 ET yesterday, every morning, in parallel across 13 profiles, against rate-limited public APIs.

The fix: a new SQLite cache that gets populated at 04:00 ET by a single pre-warmer iterating the universe. The per-candidate path checks the cache first and falls back to live fetch only on miss. Result: cycle cold-start drops from ~10min to ~30-60sec; first AI predictions land within ~2min of market open instead of ~10min.

---

## 1. Why this hasn't been built yet

Honestly: the operator has asked about it multiple times. The codebase has *partial* infrastructure for it — several sources already have local SQLite stores (`altdata/edgar13f/`, `altdata/biotechevents/`, `altdata/stocktwits/`) populated by separate scraper jobs. But the per-candidate query path doesn't go through a unified cache: each source's getter has its own caching policy (some, none) and falls back to live fetch in different ways.

The work to unify it is well-scoped but spans ~25 source-getter files. Nobody has done the chore.

This doc is the chore + the plan to ship it.

---

## 2. Goals + non-goals

### Goals
- **First AI predictions land ≤2 min after market open** at next trading day open
- **Per-profile cold-start cycle wall-clock ≤60 sec** (vs ~10 min today)
- **Eliminate Google Trends rate-limit-at-minute-9** — pre-warm against the universe at 1 req/sec spread over 30 min instead of slamming 30 symbols × 13 profiles in parallel at 09:30 ET
- **Zero behavior change to AI prompts** — pre-warmed data must be functionally identical to live-fetched data for the AI to see the same inputs

### Non-goals
- **Not** building a generic cache framework for all DB queries
- **Not** changing the alt-data source contracts (signatures, return shapes)
- **Not** trying to cache the 3 truly-intraday sources (intraday patterns, recent_8k_events, options chain) — those need fresh data per cycle
- **Not** building a separate cache server — SQLite-backed, in-process, same pattern as everything else
- **Not** restructuring the existing `altdata/*/store.py` per-source SQLite stores — those keep their current role; the new unified cache sits ON TOP as a coalescing layer

---

## 3. Source-by-source classification

The exhaustive list of `get_all_alternative_data(symbol)` calls and their time-sensitivity:

| Source | Time-sensitivity | TTL setting | Pre-warm? |
|---|---|---|---|
| `intraday` (`get_intraday_patterns`) | Intraday — last 5 min matters | None (cycle-fresh) | ❌ |
| `recent_8k_events` | 8:30am ET filings matter | None | ❌ |
| `options` (`get_options_unusual`) | Intraday IV/UOA | 5 min | Cycle-fresh OK; cache 5min |
| `macro` (yield curve, FRED, etc.) | Daily macro feeds | Already cached once/cycle | ✅ once-per-cycle |
| `insider` | Form 4 has 2-day filing lag | 24h | ✅ |
| `insider_cluster` | Derived from insider | 24h | ✅ |
| `insider_earnings` | Derived | 24h | ✅ |
| `short` | Daily FINRA reporting | 24h | ✅ |
| `finra_short_vol` | Daily | 24h | ✅ |
| `dark_pool` | Daily ATS | 24h | ✅ |
| `fundamentals` | Quarterly | 7d | ✅ weekly |
| `analyst_estimates` | Daily revisions max | 24h | ✅ |
| `earnings_surprise` | Event-based, post-event | 24h | ✅ |
| `congressional_recent` | 45-day filing window | 24h | ✅ |
| `institutional_13f` | Quarterly | 7d | ✅ weekly |
| `biotech_milestones` | Calendar-based, PDUFA | 24h | ✅ |
| `stocktwits_sentiment` | Rate-limited; near-real-time | 30 min | Cache 30min |
| `google_trends` | Rate-limited, daily ish | 24h | ✅ daily; rate-limited |
| `wikipedia_pageviews` | 24h lag built into source | 24h | ✅ |
| `app_store_ranking` | Daily snapshots | 24h | ✅ |
| `activist_13dg` | 10-day filing window | 24h | ✅ |
| `github_activity` | Daily counts | 24h | ✅ |
| `fda_inspections` | Event-based | 24h | ✅ |
| `nhtsa_recalls` | Event-based | 24h | ✅ |
| `sam_gov_contracts` | Event-based | 24h | ✅ |
| `epa_osha_violations` | Event-based | 24h | ✅ |
| `risk_factor_diff` | Quarterly 10-K/10-Q | 7d | ✅ weekly |
| `bls_jobless_claims` | Weekly Thursday release | 7d | ✅ weekly |
| `wikipedia_edits` | Daily | 24h | ✅ |
| `uspto_patents` | Daily, very stable | 24h | ✅ |
| `job_postings` | Daily | 24h | ✅ |
| `insider_track_records` | Quarterly calibration | 7d | ✅ weekly |
| `star_manager_holdings` | Quarterly 13F | 7d | ✅ weekly |

**Tally:** 25 pre-warmable + 3 truly intraday + a few partial (options chain cached 5min; stocktwits cached 30min).

---

## 4. Architecture

```
   ┌─────────────────────────────────────────────────────────────────────┐
   │                  Pre-market window (04:00-08:00 ET)                 │
   │                                                                     │
   │   ┌──────────────────────────┐                                      │
   │   │ _task_premarket_         │                                      │
   │   │   altdata_warmup         │                                      │
   │   │  • iterate universe      │                                      │
   │   │    (~1500 symbols)       │                                      │
   │   │  • rate-limit batched    │                                      │
   │   │  • populate cache        │                                      │
   │   └────────────┬─────────────┘                                      │
   │                ↓                                                    │
   │   ┌──────────────────────────┐                                      │
   │   │ altdata/cache/static.db  │  ← SQLite WAL                        │
   │   │  (symbol, source, json,  │                                      │
   │   │   fetched_at, expires_at)│                                      │
   │   └──────────────────────────┘                                      │
   └─────────────────────────────────────────────────────────────────────┘
                                  │
                                  │ at market open (13:30 UTC), per cycle:
                                  ↓
   ┌─────────────────────────────────────────────────────────────────────┐
   │                       Per-candidate flow                            │
   │                                                                     │
   │  _build_candidates_data → get_all_alternative_data(symbol)          │
   │                                       │                             │
   │              ┌────────────────────────┼────────────────────┐        │
   │              ↓                        ↓                    ↓        │
   │         cache.get(sym,           cache.get(sym,        live fetch   │
   │           "insider")               "13f")             (intraday +   │
   │           HIT (fresh)              HIT (fresh)         recent_8k)   │
   │              │                        │                    │        │
   │              └────────────────────────┴────────────────────┘        │
   │                                  ↓                                  │
   │                         merged alt_data dict                        │
   │                         (functionally identical to                  │
   │                          current behavior)                          │
   └─────────────────────────────────────────────────────────────────────┘
```

Two data paths, same surface:

1. **Pre-warm path** (04:00 ET, single sweep across the universe):
   - For each symbol in the active universe:
     - For each pre-warmable source:
       - Live-fetch with proper rate limiting
       - Write to cache with appropriate TTL
   - Coordinates across sources: Google Trends gets a 1-req-per-second pacer; SEC sources get the existing local-store path
2. **Read path** (per-cycle, per-candidate):
   - Each source getter wrapped with `cache_or_fetch(source_name, symbol, fetcher_fn, ttl_seconds)`
   - Cache hit fresh: return cached value (microseconds)
   - Cache hit stale: discard, live-fetch, write back
   - Cache miss: live-fetch, write back

---

## 5. Cache schema

`altdata/cache/static_altdata.db` (new file):

```sql
CREATE TABLE altdata_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,            -- 'insider', '13f', 'google_trends', etc.
    payload_json TEXT NOT NULL,      -- the source's return dict, JSON-encoded
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    ttl_seconds INTEGER NOT NULL,
    expires_at TEXT NOT NULL,        -- precomputed for fast SELECT
    UNIQUE(symbol, source)
);
CREATE INDEX idx_altdata_cache_expires ON altdata_cache(expires_at);
CREATE INDEX idx_altdata_cache_symbol_source ON altdata_cache(symbol, source);
```

`UNIQUE(symbol, source)` enforces one row per (symbol, source); upsert pattern via `INSERT OR REPLACE`. Total expected size: 1500 symbols × 25 sources × ~2KB avg payload = ~75MB. Reasonable on a $5/mo droplet.

Pruning: lazy-on-read (we skip rows where `expires_at < now`); periodic vacuum via a daily task.

---

## 6. Code components

### 6.1 `alt_data_cache.py` (NEW)

```python
def cache_get(symbol: str, source: str) -> Optional[Dict]:
    """Return cached payload for (symbol, source) if fresh; else None.
    Defensive: returns None on any DB error so a flaky cache can't
    block the live-fetch fallback."""

def cache_set(symbol: str, source: str, payload: Dict, ttl_seconds: int) -> None:
    """Upsert payload with TTL. Best-effort; logs on failure but never raises."""

def cache_or_fetch(source: str, symbol: str,
                    fetcher_fn, ttl_seconds: int) -> Dict:
    """The wrapper every source getter uses. Pseudocode:
        cached = cache_get(symbol, source)
        if cached is not None:
            return cached
        result = fetcher_fn(symbol)
        cache_set(symbol, source, result, ttl_seconds)
        return result"""

def evict_stale() -> int:
    """DELETE FROM altdata_cache WHERE expires_at < now. Returns count.
    Called periodically by a daily task."""

def cache_stats() -> Dict:
    """Return aggregate stats: total rows, fresh rows, stale rows,
    per-source breakdown. Surfaced on the /altdata page."""
```

### 6.2 Per-source TTL config (single source of truth)

```python
# In alt_data_cache.py
SOURCE_TTL_SECONDS = {
    "insider": 86400,                # 24h
    "insider_cluster": 86400,
    "insider_earnings": 86400,
    "short": 86400,
    "finra_short_vol": 86400,
    "dark_pool": 86400,
    "fundamentals": 86400 * 7,       # 7d (quarterly cadence)
    "analyst_estimates": 86400,
    "earnings_surprise": 86400,
    "congressional_recent": 86400,
    "institutional_13f": 86400 * 7,  # quarterly
    "biotech_milestones": 86400,
    "stocktwits_sentiment": 1800,    # 30 min
    "google_trends": 86400,
    "wikipedia_pageviews": 86400,
    "app_store_ranking": 86400,
    "activist_13dg": 86400,
    "github_activity": 86400,
    "fda_inspections": 86400,
    "nhtsa_recalls": 86400,
    "sam_gov_contracts": 86400,
    "epa_osha_violations": 86400,
    "risk_factor_diff": 86400 * 7,
    "bls_jobless_claims": 86400 * 7,
    "wikipedia_edits": 86400,
    "uspto_patents": 86400 * 7,
    "job_postings": 86400,
    "insider_track_records": 86400 * 7,
    "star_manager_holdings": 86400 * 7,
    # Cycle-fresh sources: NOT cached (or 5min cache)
    "options": 300,                  # 5 min (rapid options-flow change)
    # NOT in this dict:
    # - intraday (cycle-fresh, no cache at all)
    # - recent_8k_events (8:30am morning matters)
    # - macro (already cached once/cycle elsewhere)
}
```

### 6.3 `_task_premarket_altdata_warmup` (NEW in `multi_scheduler.py`)

```python
def _task_premarket_altdata_warmup():
    """Run at 04:00 ET Mon-Fri. Iterates the universe, populates
    altdata cache for every pre-warmable source. Rate-limit-aware:
    Google Trends gets a 1-req/sec pacer; others go faster.

    Runs concurrently with a thread pool sized to a per-source
    concurrency cap (so we don't fan out 1500 simultaneous requests
    to one upstream).

    Total wall-clock: ~30-45 min at our universe size. Finishes
    well before market open at 13:30 UTC.
    """
    from alt_data_cache import cache_or_fetch, SOURCE_TTL_SECONDS
    from universe import get_active_universe  # or similar
    symbols = get_active_universe()
    # ... per-source parallel fetch with rate limits
```

### 6.4 Source-by-source rewiring

Each pre-warmable source getter gets a one-line wrapper:

```python
# Before:
def get_insider_activity(symbol):
    # ... live fetch logic
    return result

# After:
def get_insider_activity(symbol):
    from alt_data_cache import cache_or_fetch, SOURCE_TTL_SECONDS
    return cache_or_fetch(
        source="insider", symbol=symbol,
        fetcher_fn=_get_insider_activity_live,
        ttl_seconds=SOURCE_TTL_SECONDS["insider"],
    )

def _get_insider_activity_live(symbol):
    # ... the original live fetch logic, now private
    return result
```

This pattern is repeated for each of the ~25 pre-warmable sources. Mechanical change, ~5-10 lines per source.

---

## 7. Failure modes

| Failure | Detection | Response |
|---|---|---|
| Pre-warm task crashes mid-sweep | `_task_premarket_altdata_warmup` logs exception | Partial cache populated; per-cycle path falls back to live fetch for un-cached symbols. Audit alert emitted. Next day's run picks up. |
| Cache DB locked / corrupt | `cache_get` returns None | Per-cycle path falls back to live fetch (current behavior). No degradation vs today. |
| One source's upstream is down at 04:00 ET | Per-source fetcher exception in pre-warm | Skip that source for that symbol; log + continue. Per-cycle live fetch will retry. |
| Rate-limited at pre-warm | HTTP 429 from upstream | Honor `Retry-After` header; back off; resume. Worst case: that source is incomplete for that day. |
| Universe changes intraday (new symbol appears) | Cache miss on first request | Live fetch happens (current behavior); cached for subsequent. |
| Pre-warm doesn't run (cron skipped, scheduler down) | `cache_stats()` shows stale rows; `/altdata` dashboard alarms when fresh-row count < threshold | Audit alert. Per-cycle live fetch covers it (slow cycle but not broken). |
| Cache returns stale data (TTL expired) | `cache_get` filters by `expires_at < now` | Cache miss → live fetch → write fresh entry. |
| Disk full | `cache_set` raises | Logged as warning; live fetch continues. Daily `evict_stale` should keep size bounded. |

---

## 8. Risks + mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cached data diverges from live: AI prompt sees stale info, makes wrong call | Medium | Medium | TTLs tuned per-source based on real change cadence; pre-warm task runs daily so worst-case staleness is 24h. The 3 truly-intraday sources never go through cache. |
| Pre-warm crashes silently and operator doesn't notice | Low | Medium-high (back to slow cold-start) | Daily activity log row + `/altdata` dashboard surface fresh-row count + alarm when count drops below universe×source threshold |
| Cache schema needs to change later → migration headache | Medium | Low | Versioned schema column; lazy migration on first read |
| One bad source pollutes cache with garbage payload → AI prompt corrupted | Low | High | Each cached payload includes a `schema_version` field; pre-warm validates against expected schema before persisting; corrupted payloads are rejected |
| Pre-warm hits API rate limits at 04:00 ET and never completes | Low | Medium | Per-source concurrency caps + Retry-After honored; can extend the warmup window (04:00 → 09:30 if needed) |
| Symbol list at 04:00 ET differs from active universe at 09:30 ET → cache misses for new symbols | Medium | Low | Cache miss falls back to live fetch (current behavior); new symbols get cached on first request and benefit from cache on subsequent cycles |

---

## 9. Rollout plan

### Phase A — internal verify (day 1)
- Ship the cache module + the pre-warm task + rewire 5 of the most expensive sources (insider, 13F, Google Trends, fundamentals, congressional)
- Deploy
- Manually trigger pre-warm task once
- Verify cache populated, per-cycle fetches hit cache, no behavioral changes vs unwrapped path

### Phase B — wider source rewiring (day 2)
- Rewire the remaining ~20 sources (mechanical change, ~5-10 lines each)
- Tests for each that the cache-first wrapper preserves behavior
- Deploy

### Phase C — pre-market scheduled run (next trading day)
- Cron the pre-warm task at 04:00 ET
- Observe at next 09:30 ET market open: cycle cold-start time
- Target: ≤ 60sec per profile (vs ~10min today)

### Phase D — observability + tuning
- Add `/altdata` dashboard showing cache freshness, source-by-source last-fetched timestamps, pre-warm run history
- Operator can manually flush the cache or force-refresh a single source if it goes bad

### Kill switches
- Feature flag `ALTDATA_CACHE_ENABLED` (env var or DB-stored). When OFF, `cache_or_fetch` short-circuits to direct live-fetch — instant revert without code deploy.
- Per-source flag: `_DISABLED_SOURCES = {"problematic_source"}` env-driven list; listed sources bypass cache and always live-fetch.

---

## 10. Test plan

### Unit tests (`tests/test_alt_data_cache_2026_05_20.py`)
- `cache_get` returns None when no row exists
- `cache_set` upserts (second write replaces first; only one row per symbol+source)
- `cache_get` returns None when entry is past `expires_at`
- `cache_get` returns the payload when fresh
- `cache_or_fetch` calls fetcher on cache miss and writes to cache
- `cache_or_fetch` returns cached value on cache hit (fetcher NOT called)
- `cache_or_fetch` doesn't raise if cache layer fails — falls back to live
- `evict_stale` removes only stale rows; returns correct count
- Concurrent writes to same (symbol, source) don't corrupt the row

### Integration tests
- Pre-warm task runs against a 10-symbol mock universe; populates cache; subsequent `get_all_alternative_data` calls hit cache for each pre-warmable source
- A source's live-fetch is exercised exactly ONCE when called repeatedly within the TTL window
- A source's live-fetch is exercised again after the TTL elapses

### Behavioral pin
- For each rewired source: `tests/test_altdata_cache_wrapper_preserves_shape.py` asserts that `get_X(symbol)` returns the SAME dict (modulo timestamps) whether cache is hit or fresh-fetched. The wrapper must be transparent.

### Operational dry-run
- `_task_premarket_altdata_warmup --dry-run` mode: prints what it would fetch + cost estimate (number of upstream API calls × per-source budget) without actually fetching. Operator sanity-check before first deploy.

---

## 11. File list

```
alt_data_cache.py                                   # NEW — core cache module
docs/21_ALTDATA_PREMARKET_WARMUP.md                 # this doc

tests/
├── test_alt_data_cache_2026_05_20.py               # cache module unit tests
└── test_altdata_premarket_warmup_2026_05_20.py     # pre-warm task tests
```

### Files to modify
- `multi_scheduler.py` — add `_task_premarket_altdata_warmup` task + wire into daily-snapshot block at 04:00 ET
- `alternative_data.py` — wrap each pre-warmable source getter with `cache_or_fetch`; rename original implementations to `_*_live` private helpers
- `altdata_tier3.py` — same pattern for the tier-3 sources
- `docs/04_TECHNICAL_REFERENCE.md` — add `alt_data_cache.py` to the modules table
- `CHANGELOG.md` — full entry

---

## 12. Open decisions

1. **Cache file location** — `altdata/cache/static_altdata.db` (new directory) vs `quantopsai.db` (master DB). Recommend new directory: keeps the cache separate from operational data; easier to wipe/rebuild without touching master state.
2. **Pre-warm cadence** — daily (04:00 ET) recommended; could be hourly for the rate-limited sources that benefit from more frequent refresh.
3. **Universe definition** — `get_active_universe()` returns what? Probably the union of every profile's watchlist + S&P 1500. Need to confirm the universe size; if it's >2000 symbols, the per-source rate limits become tighter.
4. **TTL on tightest source (stocktwits at 30min)** — does StockTwits sentiment within a 30-min window provide enough signal? Could tighten to 15min if signal is too stale.
5. **Concurrent pre-warm tasks per source** — recommend 5 concurrent for non-rate-limited; 1 sequential for Google Trends.

---

## 13. Activation criteria

Pre-warm task ships as soon as the cache module + at least 5 source wrappers are in place + tests pass. Cron starts the next trading day. No external dependencies, no operator approval required for the initial ship — it's an additive performance optimization with a kill switch.

After the first week of operation:
- If cycle cold-start time hasn't dropped meaningfully → audit cache hit rate; identify which sources are missing wrappers
- If AI prediction quality regresses → audit cache TTL settings; tighten any that are too loose
