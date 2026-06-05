# Unified Stock Universe — Completing the Cap-Tier Removal

> **Archived 2026-06-04.** Describes state as of 2026-05-20 PM (design); the planned migration LANDED that day in commit `a49c9d6`. All §3 changes (A-N) shipped, the SQL migration ran, and `segments.py:SEGMENTS` now has the two keys (`stocks` + `crypto`) this doc planned. Preserved as the canonical explanation of WHY cap tiers were removed.

**Removes the cap-tier price-bracket grouping (large/mid/small/micro) as runtime dispatch and replaces it with a single `stocks` segment whose effective trading pool is every Alpaca-tradable US equity (~8,000 symbols), gated only by per-profile price/volume thresholds.**

Status: IN PROGRESS — design 2026-05-20 PM.
Owner: TBD.
Triggered by: operator review of 2026-05-19 commits 840293c + 464f1ca during the 2026-05-20 cycle-time incident. Those commits' messages declared cap-tier "informational" and "doesn't affect strategy selection." Runtime audit showed that was true only for the UI label and within-stock strategy filter; universe selection, cache keys, credentials, risk-param defaults, and backtester still routed through `market_type`.
Depends on: nothing (no schema migrations beyond a single `UPDATE` on `trading_profiles`).

---

## 0. TL;DR

The cap-tier concept (`largecap`, `midcap`, `small`, `micro`) was an arbitrary grouping of stocks by price bracket from an earlier iteration of the system. The current architecture is per-profile asset-class checkboxes (`enable_stocks`, `enable_crypto`, `enable_options`) with per-profile risk thresholds (`min_price`, `max_price`, `min_volume`) on the profile row itself.

Yesterday's commits removed the UI label and the within-stock strategy filter. They did **not** remove the seven other places the cap-tier still drives behavior at runtime. This doc completes that work.

**What changes**:
- `segments.py` collapses to two segments: `stocks` (everything Alpaca-tradable) and `crypto` (separate by data source).
- The four `*_CAP_UNIVERSE` lists become one `STOCK_UNIVERSE = union(all four)`, kept solely as the outage fallback for `screen_dynamic_universe`.
- Screener stops sampling 500 random + curated favorites; it screens all ~8,000 Alpaca-tradable US equities every cycle.
- The 13 existing profiles' `market_type` migrates from `largecap` → `stocks`. Their `min_price`/`max_price`/`min_volume` widen to (1.0, 10000.0, 100000) so they actually see the full pool.
- Strategy-filter constants update: `_STOCK_MARKETS = ("stocks",)`, `ALLOWED_MARKETS = {"stocks", "crypto"}`. Every existing strategy's `APPLICABLE_MARKETS` list gets normalized to `["stocks"]`, `["crypto"]`, or `["*"]`.

**What stays**:
- The crypto-vs-equity dispatch (`ctx.segment == "crypto"`) keeps working — `crypto` remains a real segment because Alpaca's symbol format, market hours, and data endpoints genuinely differ.
- Per-profile asset-class checkboxes (`enable_stocks`, `enable_crypto`, `enable_options`) are unchanged. They were the legitimate yesterday-side work.
- Per-profile `min_price`/`max_price`/`min_volume` columns stay. They are now the **only** filter applied to the unified universe.

---

## 1. Why this hasn't already been done

Honestly: it was supposed to be done yesterday. Commit 840293c's message said:

> Drop within-stock strategy filtering. Was: exact match on APPLICABLE_MARKETS, so a strategy listing `["small","midcap"]` was walled off from largecap. Now: any stock-applicable strategy runs on any stock profile.

And 464f1ca:

> market_type is now informational... it's an internal field that doesn't affect strategy selection.

Both statements were aspirational, not literal. The work that actually shipped:
- UI label "Large Cap" → "Stocks + Options" (`models.py:1062-1065`, `views.py`, `templates/`)
- Within-stock strategy applicability check loosened (`strategies/__init__.py:85-121`)
- Added per-profile `enable_stocks` / `enable_crypto` columns

The work that did **not** ship — discovered by source audit during the 2026-05-20 cycle-time incident:
1. `models.py:1523` still assigns `ctx.segment = profile["market_type"]`. Whatever value sits in the column, that's `ctx.segment`.
2. `multi_scheduler.py:1933` still calls `get_live_universe(ctx.segment)` → `SEGMENTS["largecap"]["universe"]` = `LARGE_CAP_UNIVERSE` only.
3. `screener.py:759` samples 500 random + the cap-tier favorites list. The 500-sample cap is a legacy yfinance constraint; today's Alpaca-snapshots path handles ~1000 symbols/call.
4. `trade_pipeline.py:199` keys the AI ensemble cache by `ctx.segment`. With every profile on `largecap` this is benign coincidence, not design.
5. `multi_scheduler.py:829` keys the screener cache by `ctx.segment`. Same.
6. `segments.py:240-307` `SEGMENTS` dict still holds per-cap-tier risk defaults (`min_price`, `max_price`, `min_volume`, `stop_loss_pct`, `take_profit_pct`, `max_position_pct`) — used by `models.py:962-964, 1138-1140` to seed new profiles, frozen forever after creation.
7. `views.py:5258, 5300, 5354` portfolio/watchlist endpoints look up `SEGMENTS.get(market_type)["universe"]` to render UI watchlists.

Plus 26 strategy files declaring `APPLICABLE_MARKETS = ["midcap", "largecap"]` (or some subset) — those tags drive `_strategy_applies_to_market` and don't recognize `"stocks"` as a valid value.

This doc closes the gap.

---

## 2. Goals + non-goals

### Goals
- **One stock universe, one `stocks` segment.** No code path branches behavior based on cap tier.
- **All 13 existing profiles see ~8,000 Alpaca-tradable US equities per cycle** (subject to their per-profile price/volume filters, which widen as part of this change).
- **No regression in cycle time.** Last incident's fix landed Scan & Trade at ~2 min per profile. Universe expansion adds Alpaca snapshot calls (chunks of 200, ~few seconds total) plus more downstream per-candidate work. Acceptable if cycles stay under ~5 min.
- **No regression in crypto handling.** `ctx.segment == "crypto"` dispatch must continue to work.

### Non-goals
- Dropping the `trading_profiles.market_type` column entirely. We're changing its values, not the schema. Column drop is a separate task once we're confident no read site depends on the column itself.
- Killing the legacy single-segment `scheduler.py` + `main.py` CLI screener paths (they use their own duplicate `SMALL_CAP_UNIVERSE` in `screener.py:48`). Multi-scheduler doesn't touch them. Separate cleanup task.
- Re-keying the ensemble lock to be more granular (tracked as task #192). Today's collapse to a single `stocks` segment already eliminates the misleading per-cap-tier cache key — that's enough for now.

---

## 3. Change set (file-by-file)

### A. `segments.py` — collapse universes and SEGMENTS dict

1. Delete `MICRO_CAP_UNIVERSE`, `SMALL_CAP_UNIVERSE`, `MID_CAP_UNIVERSE`. Replace `LARGE_CAP_UNIVERSE` with a new `STOCK_UNIVERSE` that is the deduplicated union of all four. Comment explains its purpose: outage-fallback only, not the primary source of candidates.
2. Delete `_LARGECAP_KEY`, `_LARGECAP_SECRET`, `_MIDCAP_KEY`, `_MIDCAP_SECRET`, `_SMALLCAP_KEY`, `_SMALLCAP_SECRET` env vars. Already dead per feedback memory "No master key — Alpaca creds live in alpaca_accounts only."
3. Collapse the four stock entries in `SEGMENTS` dict into one:
   ```python
   SEGMENTS = {
       "stocks": {
           "name": "Stocks",
           "db_path": "quantopsai_stocks.db",
           "min_price": 1.0,
           "max_price": 10000.0,
           "min_volume": 100_000,
           "max_position_pct": 0.07,
           "stop_loss_pct": 0.05,
           "take_profit_pct": 0.15,
           "universe": STOCK_UNIVERSE,
       },
       "crypto": {...},   # unchanged
   }
   ```
   The defaults are seeds for newly-created profiles; existing profiles' values stay on the profile row.

### B. `screener.py` — stop sampling

`screener.py:759` change:
```python
sample = random.sample(equity_symbols, min(500, len(equity_symbols)))
```
to:
```python
sample = list(equity_symbols)
```
plus update the comment block above. The Alpaca-snapshots path at `screener.py:772-791` already handles 1000+ symbols per call and chunks at 200; total wall-clock for 8,000 symbols is ~8-40 chunked calls = a few seconds.

The `fallback_universe` parameter and the `alive_fallback` dedup at `screener.py:768-770` stay — they're harmless when `sample == equity_symbols` (the union is `equity_symbols`) and they remain the safety net when Alpaca's `list_assets` returns suspiciously few entries (`screener.py:752-753`).

### C. `strategies/__init__.py` — strategy filter
`strategies/__init__.py:85`:
```python
_STOCK_MARKETS = ("micro", "small", "midcap", "largecap")
```
becomes:
```python
_STOCK_MARKETS = ("stocks",)
```

`_strategy_applies_to_market` semantics stay — once every strategy's `APPLICABLE_MARKETS` is normalized to `["stocks"]` (see D), the existing `any(m in applicable for m in _STOCK_MARKETS)` check works correctly.

### D. 26 strategy files — `APPLICABLE_MARKETS`
Each file in `strategies/` currently declares `APPLICABLE_MARKETS = [...]` with one or more of `"micro"`, `"small"`, `"midcap"`, `"largecap"`, `"crypto"`, `"*"`. Normalize to:
- `["stocks"]` if the strategy is stock-only (covers all current stock strategies — within-stock filtering is dead per 840293c)
- `["crypto"]` if crypto-only
- `["*"]` if universal

This is a mechanical rewrite, one line per file.

### E. `strategy_generator.py:72`
```python
ALLOWED_MARKETS = {"micro", "small", "midcap", "largecap", "crypto"}
```
becomes:
```python
ALLOWED_MARKETS = {"stocks", "crypto"}
```

Used for validating auto-generated strategies' `applicable_markets` field. With the existing 26 strategies normalized in D, no current strategy registration breaks.

### F. `models.py` — defaults + iteration tuples
- `models.py:948`: iteration tuple `("micro", "small", "midcap", "largecap", "crypto")` → `("stocks", "crypto")`.
- `models.py:962-964, 1138-1140`: defaults seeded from `SEGMENTS["stocks"]` now (the only stock entry). No code shape change.
- `models.py:1062-1065` `MARKET_TYPE_NAMES`: `{"stocks": "Stocks", "crypto": "Crypto"}`.
- `models.py:1523` (`segment=profile["market_type"]`) **unchanged** — ctx.segment now flows as `"stocks"` or `"crypto"`.

### G. `altdata_warmup.py:66-74`
Updated to import `STOCK_UNIVERSE`. **Superseded same day**: the entire warmup (`altdata_warmup.py` + `premarket_warmup.py` + cron) was retired the same evening once we found the real cycle-time hotspots were elsewhere. See `docs/21_ALTDATA_PREMARKET_WARMUP.md` "Retirement rationale".

### H. `simple_strategies.py`
Replace `LARGE_CAP_UNIVERSE` import with `STOCK_UNIVERSE`.

### I. `views.py` (5258, 5300, 5354)
The watchlist endpoints look up `SEGMENTS.get(market_type)["universe"]`. With one `"stocks"` entry, the lookup still works; the response just always returns the full stock universe for stock profiles.

### J. `multi_scheduler.py` (cache keys, log labels)
- `_get_screener_cache_key(ctx.segment)` and `cache_key = ctx.segment` collapse to one key per segment-class. No code change needed; the cache just naturally has fewer keys.
- `seg_label = ctx.display_name or ctx.segment` log labels render "stocks" instead of "largecap". Cosmetic.

### K. `trade_pipeline.py` (`ctx.segment == "crypto"` sites)
Unchanged. The three sites at 742, 1412, 1699 keep working because `"crypto"` is still a valid segment value.

### L. `scaling_projection.py`
Normalization logic for old segment names — strip out the cap-tier branches; keep only the `"crypto"` and `"stocks"` normalizations.

### M. Tests
~10 tests pin segment-based behavior. Update each to use `"stocks"`:
- `test_user_context.py:89-97` — `test_build_from_segment`, `test_all_segment_types`
- `test_ensemble.py` — crypto-branch tests stay; stock-branch tests update
- `test_simple_strategies_graceful_apierror_2026_05_18.py`
- `test_specialist_disable_lever.py`
- `test_shared_ai_cache.py`
- `test_screener_cache.py`
- `test_altdata_warmup_2026_05_20.py` — universe import changes (test file deleted same evening when warmup was retired; see docs/21 retirement rationale)
- `test_historical_universe_augment.py`
- `test_scaling_projection.py:232`
- `test_database.py:77`

Plus a new test that asserts a profile with `market_type='stocks'` resolves to a universe of >5,000 symbols (the hard floor that catches accidental re-introduction of cap-tier gating).

### N. CHANGELOG + docs
- CHANGELOG entry naming this the **completion** of yesterday's commits 840293c + 464f1ca. Honest about the gap.
- Update `docs/03_TRADING_STRATEGY.md` and `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` where they reference cap tiers.

---

## 4. SQL migration

Single transaction on prod `quantopsai.db`:
```sql
UPDATE trading_profiles
SET market_type='stocks',
    min_price=1.0,
    max_price=10000.0,
    min_volume=100000
WHERE market_type IN ('largecap', 'midcap', 'small', 'micro');
```

Currently affects all 13 rows (all `largecap`). Applied **before** the scheduler restart so the new `_STOCK_MARKETS = ("stocks",)` filter accepts the migrated profiles.

---

## 5. Order of operations

1. Local code changes (A through L above).
2. Run full pytest. Must pass.
3. Update CHANGELOG + docs (N).
4. Commit + push.
5. Apply SQL migration to prod (section 4) — **before** sync.sh.
6. `./sync.sh` — auto-restarts scheduler.
7. Monitor first post-deploy cycle:
   - **Strategies actually run** — confirm "Multi-strategy: 26 strategies ran" log line still appears
   - **Candidate count expands** — current ~30 post-screener → expected to grow significantly (the 8,000-symbol screen will filter down by the per-profile price/volume thresholds; expected post-screener candidate count is in the same order of magnitude as today because the per-profile filters are the actual gate)
   - **Cycle time** — Scan & Trade stays under ~5 min per profile
   - **No `KeyError` / `Unknown segment`** anywhere in the journal
8. If any of step 7 fails: revert the commit, sync.sh again, restore the SQL with `UPDATE ... SET market_type='largecap', min_price=50.0, max_price=500.0, min_volume=1000000`.

---

## 6. Risks I see

- **Cycle time growth.** Removing the 500-sample cap means screening ~8,000 symbols per cycle. Alpaca snapshots can handle the I/O in a few seconds, but every survivor of the price/volume filter feeds into downstream strategy iteration. The recent compute_max_pain vectorization removed the worst hotspot; if other strategies have non-vectorized inner loops, they could now bite. Mitigation: if cycle time grows beyond ~5 min, profile per-strategy time and fix the next hotspot (same approach as today's incident).

- **Cache-key collapse for largecap profile DBs.** The `quantopsai_largecap.db` path in `SEGMENTS["largecap"]["db_path"]` won't have a `quantopsai_stocks.db` equivalent — but no code path actually opens these DBs by segment name today. The per-profile DB is `quantopsai_profile_{profile_id}.db` per `models.py:1518`. Verified before code change.

- **Tests that I miss in the grep.** Best mitigation: run the entire test suite, not just the obvious ones.

---

## 7. Out of scope (separate tasks)

- Drop `trading_profiles.market_type` column entirely (this doc just changes values).
- Kill the legacy single-segment scheduler (`scheduler.py` + `main.py`) and its duplicate `SMALL_CAP_UNIVERSE` in `screener.py:48`.
- Re-key the ensemble lock to be more granular than per-segment (task #192).

---

## 8. Open questions

- The screener cache TTL is 30 min. With 13 profiles in the same `"stocks"` segment, the first profile's screen warms the cache; the other 12 hit it. Is 30 min still right, or should it be tighter now that the universe is bigger and more potentially volatile? Default: keep 30 min. Revisit if data goes stale in a noticeable way.
