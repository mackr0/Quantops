# Dynamic Universe Plan

**Goal:** Replace the hand-curated universe lists in `segments.py` and `screener.py` with Alpaca-authoritative dynamic discovery, so delistings self-correct and new IPOs appear automatically — without manual file edits.

**Status:** ✅ COMPLETE 2026-04-27.

| Step | Status | Commit |
|---|---|---|
| Operational symptom fix (delisted-ticker spam in screener) | ✅ Shipped | 2026-04-23 |
| MAGA-scan filter via `get_active_alpaca_symbols` | ✅ Shipped | 2026-04-24 |
| Step 1 — `sector_classifier.py` with SQLite cache | ✅ Shipped | 2026-04-27 |
| Step 2 — `segments_historical.py` (frozen baseline for backtests, fixes survivorship bias) | ✅ Shipped | 2026-04-27 (`f2e6b74`) |
| Step 3 — `get_live_universe()` + `USE_DYNAMIC_UNIVERSE` feature flag | ✅ Shipped | 2026-04-27 |
| Step 4 — screener.py `fallback_universe` filtering | ✅ Already shipped | 2026-04-23 |
| Step 5 — UI universe-size surface | DEFERRED (low priority; not an integrity issue) |
| Step 6 — regression tests | ✅ Shipped (22 across 3 files) | 2026-04-27 |
| Step 7 — CHANGELOG entries | ✅ Shipped | 2026-04-27 |

**§3d (emergency fallback shrinkage to ~100 names):** Decided to keep
the current ~300-name lists in `segments.py` rather than shrink. Rationale:
the survivorship-bias concern is solved via `segments_historical.py` +
auto-augmentation; live-trading dead-ticker leakage is solved by the
2026-04-23 Alpaca-active filter. Shrinking would change live screener
sample composition (fewer canonical names mixed into Alpaca's 500-symbol
random sample) for no integrity gain. Lists stay at full size and are
used only by the screener's curated-universe supplement.

**§3e (rollout):** Feature flag `USE_DYNAMIC_UNIVERSE` shipped, default
OFF (preserves prior behavior). User can flip per-profile in `.env` for
A/B testing if desired.

**Context for future sessions:** Today (2026-04-23) produced ~30 `$SYMBOL: possibly delisted` yfinance errors per scan cycle because `segments.py` and `screener.py` contain tickers like `SQ` (now `XYZ`), `PARA` (now `PSKY`), `X` (acquired, delisted), `CFLT` (taken private). Alpaca's asset API confirms these symbols no longer exist. Yahoo's website still renders them for UX; Yahoo's API returns 404. Root cause is stale hand-curated lists. **All resolved 2026-04-27.**

---

## 1. Current State (what exists)

### 1a. Hardcoded lists
| File | Lists | Purpose |
|---|---|---|
| `segments.py:32-215` | `MICRO_CAP_UNIVERSE`, `SMALL_CAP_UNIVERSE`, `MID_CAP_UNIVERSE`, `LARGE_CAP_UNIVERSE`, `CRYPTO_UNIVERSE` | Referenced by `SEGMENTS[x]["universe"]` |
| `screener.py:39-99` | `SMALL_CAP_UNIVERSE` (duplicate of the one in segments.py) | Returned by `get_small_cap_universe()` |
| `market_data.py:429-442` | `_SECTOR_MAP` in `_guess_sector()` | 7 sectors, ~50 symbols covered |

### 1b. Dynamic discovery already built
`screener.py:538` `screen_dynamic_universe()` — pulls `api.list_assets(status='active')`, filters to US exchanges + tradable + not-ETF, samples 500, filters by Alpaca `get_snapshots` against price/volume, falls back to yfinance only if Alpaca fails, caches 24h on disk in `dynamic_screener_cache.json`.

### 1c. The leak
`screener.py:592-594`:
```python
if fallback_universe:
    # Always include the curated universe
    sample = list(set(sample + list(fallback_universe)))
```
The hardcoded list is *unioned into* the dynamic sample — so dead symbols survive even when dynamic discovery runs. The parameter name `fallback_universe` is misleading; it's used as a *supplement*, not a fallback.

### 1d. Consumers of the hardcoded universe
| File | Usage | Impact of change |
|---|---|---|
| `multi_scheduler.py:321-330` | Passes hardcoded list as `fallback_universe` to dynamic screener every cycle | **Primary leak path — must fix** |
| `multi_scheduler.py:530` | Crypto screen uses hardcoded crypto list | Keep as-is (small fixed list, Alpaca crypto symbols are stable) |
| `rigorous_backtest.py:128` | Historical backtests | Decision needed (see §3a) |
| `backtester.py:443, 740, 853` | Historical backtests + what-if | Decision needed (see §3a) |
| `correlation.py:157` | Correlation matrix computation | Decision needed |
| `views.py:2866, 2909, 2963` | UI (what-if backtesting, universe display) | Can use same source as live scan |
| `strategies/sector_momentum_rotation.py:45` | Uses `_guess_sector()` | Update when sector source changes |

---

## 2. Target State (what we're building)

### 2a. Live-scan universe
- `SEGMENTS[x]["universe"]` becomes a **computed property**, not a literal list.
- At module load (and refreshed every 24h), `segments.py` calls Alpaca's `api.list_assets(status='active', asset_class='us_equity')`, filters to `tradable=True` + exchange in `{NYSE, NASDAQ, ARCA, AMEX}` + not-in-ETF-blacklist.
- Each segment gets its slice by applying its own `min_price` / `max_price` / `min_volume` / sector rules against a fresh Alpaca snapshot.
- Hardcoded lists remain in-file **only** as a last-ditch emergency fallback (Alpaca completely unreachable + no disk cache). Shrunk to ~20 names per segment, all currently-active, purely as a "system must run even if Alpaca is down" safety net.

### 2b. Sector classification
Replace `_guess_sector()`'s hand-typed 50-symbol dict with a cached lookup:
- **Primary:** yfinance `Ticker(sym).info['sector']` → map GICS sectors (e.g., "Technology", "Financial Services") to our 7 internal keys (tech, finance, etc.).
- **Cache:** persistent SQLite table `sector_cache(symbol, sector, fetched_at)` in the master `quantopsai.db`. TTL 7 days (sector rarely changes). Pattern matches how `sec_filings.py` caches were moved to SQLite in the 2026-04-23 fix.
- **Fallback:** a minimal hardcoded map for the top ~100 symbols kept as a last-resort + unit-test fixture. Same role as 2a's emergency fallback.
- **Default:** `"tech"` if both yfinance and fallback miss (current behavior).

### 2c. Backtest universe
This is a real tradeoff — see §3a.

### 2d. Regression tests (new)
- `test_universe_dynamic.py`:
  - Live universe contains zero symbols with `tradable=False` on Alpaca (mock the Alpaca client)
  - Stale cached universe returns on Alpaca outage
  - Emergency fallback returns when cache is also empty
  - Segment-specific filters (price, volume) apply correctly
- `test_sector_classification.py`:
  - Cache hit returns cached value, doesn't call yfinance
  - Cache miss calls yfinance and writes result
  - yfinance failure returns fallback map value, not crash
  - GICS → internal mapping covers all yfinance `info['sector']` possibilities

---

## 3. Open Decisions (need Mack's sign-off before I start)

### 3a. Backtest universe policy — **most important decision**
Backtests depend on a historical universe. Two options:

**Option α (historical fidelity):** Freeze the current hardcoded lists into `segments_historical.py` and let backtesters read from there. New dynamic universe for live trading only.
- *Pro:* backtests reflect what you could have traded then; includes SQ, PARA, etc. that really did trade in 2024-2025.
- *Con:* two universes to maintain; historical list is frozen, not updated.

**Option β (tradeable-today only):** Backtests use the current dynamic universe too. Delisted symbols dropped from all windows.
- *Pro:* simpler; one source of truth.
- *Con:* survivorship bias — backtest winners overrepresent survivors; backtest results may be rosier than reality.

**My recommendation: Option α.** Quant discipline says avoid survivorship bias at all costs — it's the #1 way retail backtests lie to you. The roadmap's non-negotiable principle "Statistical rigor is mandatory" points here. One extra file to maintain is worth correctness.

### 3b. Sector data source
yfinance `info['sector']` is the path of least resistance but:
- Slow (1-2 sec per symbol uncached)
- Inconsistent: some foreign ADRs return `None`
- Yahoo flakiness is the exact problem we're fixing today

Alternatives:
- **FMP free tier** (`financialmodelingprep.com/api/v3/profile/SYMBOL`) — 250 req/day free, clean data, needs an API key
- **Static GICS map sourced once from NASDAQ/SEC** — stale but deterministic
- **Ignore sector entirely** for now — `sector_momentum_rotation.py` and `get_relative_strength_vs_sector()` go dark until resolved

**My recommendation: yfinance primary + static top-200 map as fallback.** Same pattern as the live-universe fix (emergency fallback). Upgrade to FMP later if yfinance causes problems.

### 3c. Renamed-ticker handling
Block (SQ → XYZ), Paramount (PARA → PSKY), Gap (GPS → GAP). Three options:

1. **Silent substitution:** when screener sees `SQ` in its fallback list, translate to `XYZ`.
2. **Replacement at source:** update any remaining static lists to the new tickers, leave historical DB rows alone.
3. **No substitution:** drop the old symbol; let dynamic discovery surface the new one naturally.

**My recommendation: Option 3.** Dynamic discovery will find `XYZ`, `PSKY`, `GAP` on its own via Alpaca. Substitution adds a rename dict nobody will maintain. Historical DB rows that reference `SQ` stay correct as historical records.

### 3d. Hardcoded list size for emergency fallback
Current lists total ~300 unique symbols. Proposed emergency fallback: ~20 per segment = ~100 total, all verified-tradable. Acceptable?

### 3e. Rollout
- **Safe:** put the new code behind a feature flag `USE_DYNAMIC_UNIVERSE` in `.env`, default False. Flip per-profile on the droplet. Roll back by flipping to False.
- **Bold:** deploy directly, trust the tests and stale cache.

**My recommendation: feature flag.** The 2026-04-23 scan crash is a fresh lesson about deploy safety. Flag adds ~20 lines, buys a trivial rollback, and lets you A/B one profile before all 10.

---

## 4. Work Breakdown (once decisions are made)

Steps assume recommended choices above (Option α, yfinance + fallback, no rename substitution, ~100-symbol emergency list, feature flag).

### Step 1: Sector classification module (`sector_classifier.py`, new file)
- `get_sector(symbol)` with SQLite-cached yfinance lookup + fallback map + `"tech"` default
- Migration: new `sector_cache` table in `quantopsai.db` (idempotent `CREATE TABLE IF NOT EXISTS`)
- GICS-to-internal-key mapping
- Tests in `test_sector_classification.py` (~8 tests)
- Update callers: `market_data._guess_sector` (now a thin wrapper calling `sector_classifier.get_sector`), `strategies/sector_momentum_rotation.py`
- **Est:** 2-3 hours

### Step 2: Historical-universe freeze (`segments_historical.py`, new file)
- Move current `MICRO_CAP_UNIVERSE`, `SMALL_CAP_UNIVERSE`, `MID_CAP_UNIVERSE`, `LARGE_CAP_UNIVERSE` lists here verbatim
- Update `backtester.py:443, 740, 853`, `rigorous_backtest.py:128`, `correlation.py:157` to import from `segments_historical` instead of `segments`
- Leave `CRYPTO_UNIVERSE` in `segments.py` (it's fine)
- **Est:** 30 min

### Step 3: Dynamic universe provider in `segments.py`
- New function `get_live_universe(segment_name)` that returns a symbol list filtered against Alpaca snapshots by segment's `min_price` / `max_price` / `min_volume` / sector rules
- Internal 24-hour disk cache (same file as `dynamic_screener_cache.json`, new key)
- Emergency fallback: new `EMERGENCY_FALLBACK_UNIVERSE = {...}` literal with ~100 verified-active symbols
- `SEGMENTS[x]["universe"]` becomes `property` that calls `get_live_universe(segment_name)` — OR we change consumers to call `get_live_universe()` directly (cleaner but touches more files). Decision: call directly, no property magic. Add a compat `seg.get("universe")` accessor in `get_segment()`.
- Feature flag: `USE_DYNAMIC_UNIVERSE=true` in `.env`. When False, fall through to `segments_historical` lists.
- Update `multi_scheduler.py:321-330` to call `get_live_universe` and NOT pass a fallback into `screen_dynamic_universe` anymore (or pass the emergency fallback only)
- **Est:** 3-4 hours

### Step 4: Remove `screener.py` dead weight
- Delete the duplicate `SMALL_CAP_UNIVERSE` at `screener.py:39-99`
- Rewrite `get_small_cap_universe()` to call `segments.get_live_universe('small')`
- Delete `fallback_universe` parameter from `screen_dynamic_universe` (or keep but rename to `emergency_only_fallback` and only use when Alpaca path totally fails)
- Delete `screener.py:594` union line
- **Est:** 30 min

### Step 5: UI updates (`views.py`)
- `views.py:2866, 2909, 2963` — use `segments.get_live_universe(segment_name)` instead of `segment["universe"]`
- Surface in the UI: "Universe: 312 tradable symbols (Alpaca, cached 4h ago)" so it's visible the dynamic system is working
- **Est:** 1 hour

### Step 6: Tests
- `test_universe_dynamic.py` (new, ~12 tests)
- `test_sector_classification.py` (new, ~8 tests)
- Update any existing test that asserts specific symbols in the universe — there shouldn't be many, but grep and audit
- Run full suite before deploying; target: 678 → ~700 passing
- **Est:** 2-3 hours

### Step 7: CHANGELOG.md entry (enforced by pre-commit hook)
- Entry date 2026-04-XX — title "Dynamic universe discovery replaces hardcoded lists"
- Severity: medium
- Problem / root cause / fix / tests — standard format
- **Est:** 15 min

### Step 8: Deploy with feature flag off, verify locally + on droplet
- `./run_tests.sh` → green
- Deploy via `./sync.sh 67.205.155.63`
- Flip `USE_DYNAMIC_UNIVERSE=true` for ONE profile first (recommend Crypto or Large Cap — smallest blast radius) via the droplet `.env`
- Wait one full scan cycle, check logs for clean run + expected universe size
- Flip remaining profiles over 24-48h, watch the dashboard scan-failure banner
- **Est:** 1-2 hours of watching + occasional intervention

---

## 5. Total Estimate

**~10-14 hours of engineering, ~48 hours of watching post-deploy.** Realistic over 2-3 work sessions. Not a one-shot.

---

## 6. Risk / Rollback

**What can go wrong:**
- Alpaca rate-limiting on `list_assets` — mitigated by 24h disk cache
- yfinance sector lookup bottleneck — mitigated by 7-day SQLite cache + fallback map
- Emergency fallback list itself goes stale over months — mitigated by nightly test comparing fallback against Alpaca; failures email `mack@mackenziesmith.com`
- First cycle after cutover: universe includes OTC / thinly-traded names that slip past filters — mitigated by feature flag (roll back instantly), pre-deploy sample inspection
- Backtest behavior differs if someone forgets to switch to `segments_historical` — mitigated by leaving `segments.SMALL_CAP_UNIVERSE` = a sentinel that raises `NotImplementedError` pointing to `segments_historical`

**Rollback:** feature flag to False. Takes 10 seconds. All changes beside the flag are additive (new files, new column, no destructive DB changes).

---

## 7. Out of Scope (explicitly deferred)

- **Multi-exchange expansion** (LSE, TSX, etc.): Alpaca is US-only anyway.
- **Corporate-action awareness** (stock splits, mergers, rename tracking): Alpaca's asset list reflects current state, so we get this implicitly. No dedicated tracking.
- **Crypto dynamic discovery:** `CRYPTO_UNIVERSE` stays hardcoded (small stable set, Alpaca's crypto asset list is itself small).
- **Short-availability tracking:** `a.shortable` from Alpaca is a single bool, useful but a separate concern.

---

## 8. Cross-Session Continuity

**If a future session picks this up:** read this file. Sections §1 and §2 are the contract. §3 open decisions should be resolved in the commit that closes each step. Update §4 work breakdown statuses (add `[x]` checkmarks as steps complete). Link the final CHANGELOG entry from §7 when shipped.
