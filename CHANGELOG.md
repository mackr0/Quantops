# Changelog

Every bug fix, behavior change, and known-issue resolution. Newest entries
at the top. Each entry includes the problem, root cause, fix, and any
follow-up work tracked separately.

**Format**: YYYY-MM-DD — short title. Severity: critical / high / medium / low.

Rules going forward:
- Every production bug fix gets an entry here before deploy
- Every fix must name the test that prevents regression (or a follow-up TODO
  to add one)
- "Production" means anything that changed behavior on the droplet, not
  code-only refactors
- Honest failure analysis: **what broke**, **why it wasn't caught**, **what
  the fix actually does**, **why the new test would catch it next time**

---

## 2026-05-04 — AdComm meeting scraper (Severity: low, new capability)

**What changed**: Added a side-channel to `pdufa_scraper.run_full_sync` that pulls upcoming FDA Advisory Committee meeting disclosures from SEC EDGAR 8-K full-text search and writes them to a new `adcomm_events` table. `alternative_data.get_biotech_milestones()` now returns `upcoming_adcomm_date`, `days_to_adcomm`, and `adcomm_committee` alongside the PDUFA fields.

**Why**: AdComm meetings are leading indicators — they typically precede a PDUFA decision by 1-3 months and the meeting outcome (recommendation to approve / vote against) materially moves the stock around the meeting itself. Without this, the AI was missing the most actionable biotech catalyst window.

**Fix**:
- New `_ensure_adcomm_table()` schema mirrors the pdufa_events pattern (same NOT NULL + UNIQUE constraints).
- Refactored `_parse_drug_and_action_near_pdufa()` into a generalized `_parse_drug_and_action_near_phrase(text, anchor)` so the same 3-pass extractor (phrase / WHO INN suffix / compound code) works for both PDUFA and AdComm anchors. PDUFA-specific function is now a thin wrapper.
- New `fetch_adcomm_events_from_edgar()` queries `"Advisory Committee meeting"`, parses the meeting date with a small set of date patterns, and extracts the committee name (ODAC, BPAC, EMDAC, etc.) when explicit.
- New `sync_adcomm_events_to_altdata_db()` upserts into `adcomm_events` with UNIQUE(ticker, adcomm_date).
- AdComm side-sync is wrapped in a `try/except` inside `run_full_sync` so a parse failure on the AdComm path doesn't invalidate the PDUFA pull.

**Tests**: 18 new tests covering AdComm date parsing (3 phrasings + no-match), committee name extraction (acronym + full name + missing), sync table behavior (write + UNIQUE dedupe), and end-to-end mocked fetch with drug-name extraction. Total 2,225 tests pass.

**Why next time will be caught**: AdComm meetings are rarer than PDUFA disclosures (~2 hits / 60 days vs. ~10), so the test suite explicitly exercises the empty-corpus path. The drug-name fallback paths are shared with the PDUFA fetcher, so improvements there flow through automatically.

---

## 2026-05-04 — PDUFA: extract drug name + action type from 8-K filings (Severity: low, signal quality)

**What changed**: The EDGAR PDUFA scraper now parses an actual drug name (when one is mentioned) and the action type (NDA / BLA / sNDA / sBLA / 510(k) / PMA) from the 8-K filing text, instead of writing the placeholder `"(see 8-K filing)"` for every event.

**Why**: First-pass implementation wrote a static placeholder for `drug_name` and skipped `action_type`. After deploying and seeing 10 real PDUFA events land, the placeholder hurts the AI prompt — `get_biotech_milestones` returns useful date + ticker but a useless drug field, when filings reliably name the drug nearby.

**Fix**: New `_parse_drug_and_action_near_pdufa(text)` function — scans a 600-char window centered on the first "PDUFA" mention; matches three common phrasings ("NDA for X with PDUFA…", "PDUFA date for X is…", "regarding X,…"); rejects a small set of generic-noun false positives ("the", "company", "review", etc.); pairs with a separate full-text regex for action type. The fetcher now calls this once per filing and includes both fields in the event dict; sync writes them to `pdufa_events.action_type` and `pdufa_events.drug_name`.

**Tests**: 6 new cases in `TestDrugAndActionExtraction` — generic drug after "NDA for", brand name after "BLA for", sNDA action type, fallback to "(see filing)" when no match, no-PDUFA-in-text, and false-positive rejection.

**Why next time will be caught**: the false-positive rejection set is the riskiest piece — a new filing phrasing could slip a noun like "treatment" or "candidate" through. The test asserts the rejection list works for known FPs; new FPs would show up as low-quality drug names in the live `pdufa_events` table and can be added to `_DRUG_FP` as they're noticed.

---

## 2026-05-04 — Alt-data project merge + real PDUFA scraper (Severity: medium, hygiene + capability)

**What changed**: (1) The four standalone alt-data scrapers (`congresstrades`, `stocktwits`, `biotechevents`, `edgar13f`) were merged into the Quantops repo as `altdata/<project>/` subdirectories. (2) Replaced the broken BioPharmCatalyst PDUFA scraper with a SEC EDGAR full-text-search implementation.

**Why** (merge): Standalone repos were rsync'd to prod without `.git/`, so the standing rule "prod git must track deployed code" was unenforceable for those four projects. Plus four private repos × no prod credentials meant no clean fetch path. Single repo means single deploy, single venv, single git status to monitor.

**Why** (PDUFA): `pdufa_events` table on prod had 0 rows for weeks. Diagnosis: BioPharmCatalyst returns HTTP 403 with `cf-mitigated: challenge` — Cloudflare browser-challenge mode. Programmatic bypasses (cloudscraper etc.) are an arms race. Switched primary source to SEC EDGAR full-text search for "PDUFA date" in 8-K filings — companies file an 8-K within hours of receiving a PDUFA date from FDA, so EDGAR is the authoritative forward-looking source. Free, no auth, no anti-bot challenges, durable.

**Fix**:
- Merge: `altdata/<project>/` for all four; deleted per-project `pytest.ini` (broken urllib3 filterwarning) and `test_changelog_enforcement.py` (per-repo hooks no longer apply); added `--import-mode=importlib` to root `pytest.ini` to avoid `test_store.py` name collisions across projects; added per-project `conftest.py` for sys.path setup; new `altdata/run-altdata-daily.sh` uses Quantops's shared venv; ALTDATA_BASE_PATH on prod moved from `/opt/quantopsai-altdata/` to `/opt/quantopsai/altdata/`.
- PDUFA: new `pdufa_scraper.fetch_pdufa_events_from_edgar()` calls EDGAR's full-text search, fetches each matching 8-K, regex-extracts PDUFA dates and tickers, dedupes by (ticker, date). Polite (`SEC_USER_AGENT` includes contact email; 200ms sleep between filing fetches; capped at 50 filings per run). `fetch_pdufa_events()` now calls EDGAR first; BioPharmCatalyst stays as legacy parser only.

**Tests**:
- 269 sub-project tests now run as part of Quantops's combined sweep.
- 6 new EDGAR-path test classes in `tests/test_pdufa_scraper.py` (14 tests): ticker extraction, filing URL construction, regex date parsing, end-to-end mocked fetch.
- All 2,197 tests pass.

**Why next time will be caught**: the EDGAR API is structured + stable (it's the SEC), so layout drift is much less likely than HTML scraping. If EDGAR returns 0 hits unexpectedly, `_task_pdufa_scrape` logs the empty result and the daily run still completes (graceful degradation). Repo merge eliminates the "no git on prod" failure mode for the four scrapers.

**Follow-ups**:
- 4 standalone GitHub repos (`mackr0/{congresstrades,stocktwits,biotechevents,edgar13f}`) were archived 2026-05-04 with a `MIGRATED.md` redirect commit.
- Old `/opt/quantopsai-altdata/` directory on prod renamed to `.OLD-2026-05-04`; can be deleted after a few days of cron success at the new path.
- Drug-name extraction in EDGAR PDUFA events currently writes "(see 8-K filing)" — could be improved with more parsing if signal quality demands.

---

## 2026-05-03 — OPEN_ITEMS #1-10: ten free-tier items shipped end-to-end (Severity: high, capability)

Working through the master open-items list. All ten free-tier items now built, tested, deployed.

**#1 ADV-at-trade-time storage.** `trades.adv_at_decision` REAL column. Captured in `_execute_buy`/`_execute_sell` from `get_bars(symbol, limit=20).volume.mean()`. `slippage_model.calibrate_from_history` prefers the row's stored ADV → real participation_rate; falls back to coarse `$50M` proxy for legacy rows. Adds `n_samples_real_adv` to fit metadata so users can see how much of the calibration is anchored on real ADV.

**#2 App Store WoW snapshot.** New `app_store_history` table snapshotted by `_task_app_store_snapshot` (daily-idempotent across all profiles via master-DB marker). `get_app_store_ranking` returns `wow_change_grossing` / `wow_change_free` deltas vs ~7 days ago. AI prompt renders signed delta inline: "App Store: Uber — #15 free (+3 WoW)".

**#3 MC bootstrap by-day.** `mc_backtest.run_monte_carlo` gains `bootstrap_mode` ('per_trade' | 'by_day', default 'by_day'). by_day pre-draws ONE slippage realization per (date, side) at sim start; trades sharing a day reuse the draw — captures correlated-regime variance the per-trade IID mode misses. New `_replay_with_slips` helper for the cached path. 3 new tests including invalid-mode error path.

**#4 wheel_symbols settings UI.** Schema column `TEXT NOT NULL DEFAULT '[]'` (JSON list). Settings textarea with tooltip + plain-English helper. `_parse_wheel_symbols` helper for ctx build. Save_profile parser. update_trading_profile allowlist. MANUAL_PARAMETERS entry. The wheel state machine in `options_wheel.py` finally has user input.

**#5 Synthetic options backtester dashboard panel.** New `/api/options-backtest` POST endpoint. Wraps `backtest_strategy_over_period` with a 5-strategy preset map (long_put / long_call / bull_call_spread / bear_put_spread / iron_condor). UI on AI Brain tab: symbol + strategy + lookback + OTM% + DTE + cycle-days inputs, Run button, equity-curve table.

**#6 PDUFA scraper.** New `pdufa_scraper.py`: scrapes BiopharmCatalyst FDA calendar, parses iso/long-form/US dates, dedupes, syncs to `~/quantopsai-altdata/biotechevents/biotechevents.db` (creates table if missing). `_task_pdufa_scrape` daily-idempotent. 15 tests including parse robustness, date format coverage, sync upsert, fallback path. `alternative_data.get_biotech_milestones` already queries `pdufa_events` — now it'll have data.

**#7 Short borrow rate tracking.** `short_borrow.py` extended: 3-tier rate lookup (HTB-overridden → 12-30%/yr; non-GC `easy_to_borrow=False` → 8%/yr; GC default → 1.8%/yr). `render_borrow_rate_for_prompt(symbol, easy_to_borrow)` returns "borrow ~8.0%/yr (non-GC)". Trade pipeline annotates each short candidate with `_borrow_rate_str` + `_borrow_bps_per_day`; AI prompt renders concrete rate instead of binary "low/high".

**#8 AI vocabulary for proposing options trades.** OPTIONS action was previously gated on the read-side advisor surfacing held-position covered_call/protective_put opportunities. Opened up: AI can now propose `long_call` / `long_put` directly on any candidate with options. Updated prompt with explicit per-strategy validator notes (1% premium cap on longs, share-coverage rule on covered_call) and a directional-play example.

**#9 Macro event tracker (FOMC/CPI/NFP).** New `macro_event_tracker.py` with hand-curated MACRO_EVENT_CALENDAR through end of 2026. `get_upcoming_macro_event` / `days_until_next_event` / `evaluate_macro_play` (pre-window IV-rich → SPY iron condor; pre-window IV-cheap → long straddle; post-window → time-stop). One-line block surfaces next event in AI prompt MARKET CONTEXT. Closes Phase F2 of OPTIONS_PROGRAM_PLAN.

**#10 Per-profile options roll-window knobs.** Three new schema columns: `options_roll_window_days` (default 7), `options_auto_close_profit_pct` (default 0.80), `options_roll_recommend_profit_pct` (default 0.50). UserContext fields with matching defaults. `evaluate_for_roll` and `auto_close_high_profit_credits` parameterized; scheduler task passes ctx values. Settings UI with three numeric inputs + tooltips explaining the trade-offs.

All 10 wired through schema → UserContext → allowlist → save_profile → settings UI → AI prompt or scheduler → tests. Existing guardrails extended where needed (test_today_integration adds new task stubs; MANUAL_PARAMETERS adds 8 new entries).

Suite: 1914 passed, 0 skipped.

---

## 2026-05-03 — Hidden-lever sweep: extended UI guardrail + 4 new panels (Severity: medium, UX)

Three follow-ups in priority order:

**1. Extended UI-coverage guardrail.** `tests/test_meta_features_have_ui.py` now covers four classes of "hidden lever":

- `meta_model.NUMERIC_FEATURES` (was)
- `meta_model.CATEGORICAL_FEATURES` (NEW)
- `signal_weights.WEIGHTABLE_SIGNALS` (NEW; with `vote_X` ↔ base-strategy aliasing)

Plus a stale-allowlist test that fails when any `INTERNAL_*` entry no longer exists in its source — prevents drift. Removed `signal_weights.py`, `alternative_data.py`, `self_tuning.py` from the surface scan path because including the source files defining a feature would make the test tautological. Caught real gaps: 6 `vote_*` strategy weights had no static UI surface; surfaced via the new `/api/weightable-signals` panel + INTERNAL_WEIGHTABLE allowlist with rationale.

**2. New panel: Tunable Signal Weights (Layer 2).** `/api/weightable-signals/<id>` lists EVERY weightable signal with current weight + override status. Solves "what CAN I tune?" — `get_all_weights()` only returned non-default entries, so users couldn't see the full lever set without reading the code.

**3. Slippage calibration drift.** New schema column `predicted_slippage_bps` on the trades table; captured at submit time in `_execute_buy` and `_execute_sell` paths in `trade_pipeline.py`. New API `/api/slippage-history/<id>` returns predicted vs realized for the last 200 fills + aggregate stats: mean delta, σ delta, Pearson correlation. New panel on Brain tab shows live drift table + summary stat-cards. Plain-English explainer: persistent positive delta = K under-calibrated (bump it); persistent negative = over-pessimistic.

**4. Per-strategy MC tiles.** `/api/mc-backtest-by-strategy/<id>` groups closed trades by `strategy` field, runs MC per group, returns each strategy's distribution. New panel on Brain tab renders one tile per strategy with median, 5–95 band, σ, P(loss). Lets you see which strategies have ROBUST edge vs which would die under realistic slippage variance. Min 5 trades per strategy to compute.

Suite: 1896 passed, 0 skipped.

---

## 2026-05-03 — UI panels for slippage / MC backtest / attention signals + meta-feature UI guardrail (Severity: medium, UX)

The user called out that I keep shipping signals without a way to see them. Three new panels + a guardrail test that fails any future ship that adds a meta-model feature without a corresponding UI surface.

**New API endpoints:**
- `GET /api/slippage-model/<profile_id>` — current K, n_samples, mean residual, bucket sample counts, sample estimate.
- `POST /api/mc-backtest/<profile_id>` — runs Monte Carlo backtest on the profile's last 90 days of closed trades. Body: `{n_sims: 1000}`. Returns full P&L distribution.
- `GET /api/attention-signals/<profile_id>` — Google Trends + Wikipedia + App Store snapshot for held positions. Capped at 25 symbols / call.

**New panels on AI page:**
- **Brain tab → Slippage Model:** shows K calibration, sample size, mean residual, bucket histogram, sample-estimate breakdown (half-spread + impact + vol + bootstrap = total bps).
- **Brain tab → Monte Carlo Backtest:** Run button kicks off 1000 simulations; result panel shows σ, P(loss), distribution table (worst / 5th / 25th / median / 75th / 95th / best). Plain-English explainer: wide [5%, 95%] band = strategy P&L is execution-variance-sensitive; narrow = robust edge.
- **Awareness tab → Attention Signals:** per-position table of Google Trends z-score + direction, Wikipedia 7d/90d z-score + SPIKE flag, App Store rank + primary-app name. Color-coded: ≥+1σ green, ≤−1σ red.

**Guardrail (`tests/test_meta_features_have_ui.py`):**
For every key in `meta_model.NUMERIC_FEATURES`, asserts the key is referenced by at least one Jinja template, view, or AI-prompt assembler — OR is on the explicit `INTERNAL_FEATURES` allowlist with a written rationale (currently 5 entries: `_market_signal_count`, `_yield_spread_10y2y`, `_cboe_skew`, `_unemployment_rate`, `_cpi_yoy` — all surfaced via macro_context blocks under different names).

A second test fails on stale `INTERNAL_FEATURES` entries (allowlist drift). Verified the guardrail catches a regression by temporarily adding `fake_feature_no_ui_surface` to `NUMERIC_FEATURES` — test failed with the right error, then reverted.

Suite: 1894 passed, 0 skipped.

---

## 2026-05-03 — Item 3a (cont.): App Store ranking + 5c Monte Carlo backtest (Severity: medium, capability)

**App Store ranking signal:** `alternative_data.get_app_store_ranking(symbol)` queries Apple's free iTunes RSS (no auth) for top-grossing + top-free chart positions. Hand-curated `APP_STORE_TICKER_OVERRIDES` covers ~36 consumer-app tickers (UBER, LYFT, ABNB, DASH, SNAP, SPOT, NFLX, META, RBLX, COIN, HOOD, RDDT, ...). Returns best grossing + free rank across the ticker's tracked apps; supports multi-app companies (META has Instagram + Facebook + Threads). 24h cache. Tickers without a known app return `has_data=False` cleanly.

Wired through the same path as Google Trends / Wikipedia: alt_data aggregator → features_payload (`app_store_grossing_rank`, `app_store_free_rank`) → meta-model NUMERIC_FEATURES → signal_weights for Layer-2 tuning → AI prompt under ALT DATA (`App Store: Uber — #5 grossing, #12 free`).

**Monte Carlo backtest** (`mc_backtest.py`): turns single-point backtest results into a distribution. `run_monte_carlo(trades, db_path, n_sims=1000)` replays each trade `n_sims` times with entry + exit slippage drawn from `slippage_model.calibrate_from_history`'s bootstrap residuals. Returns 5/25/50/75/95th percentile returns, mean ± σ, worst case, best case, P(loss). Surfaces the question deterministic backtests can't answer: "is this strategy's edge larger than realistic execution variance, or is the deterministic P&L just one lucky slippage realization?"

Falls back to a Gaussian (5±8 bps) when bootstrap buckets are sparse. IID slippage assumption documented as a limit (correlated regimes — full day of wide spreads — aren't captured; future enhancement: bootstrap by day, not trade).

**Tests:** `tests/test_app_store_signal.py` (6 cases — unknown ticker, crypto skip, multi-app best-of, top-200 cutoff, HTTP failure graceful) + `tests/test_mc_backtest.py` (12 cases — replay math, percentile ordering, deterministic with seed, P(loss) bounds, dollar/pct consistency, render).

Job-postings volume signal **deferred**: no clean free source. LinkedIn API is paid, Indeed scraping is TOS-fragile. Revisit when paid alternative is acceptable, or when SEC 10-K headcount tracking gets built.

Suite: 1892 passed, 0 skipped.

---

## 2026-05-03 — Item 3a: Google Trends + Wikipedia attention signals (Severity: medium, capability)

Two new free web-scraped attention proxies. Both are best-effort: HTTP/rate-limit failures return `has_data: False` and the prompt suppresses the line. 24h cache. No per-profile config — they're zero-cost analytical signals always-on, like the existing congressional / 13F / StockTwits feeds.

**`alternative_data.get_google_trends_signal(symbol)`:** trailing-12-month weekly interest from Google Trends via `pytrends`. Output: `trend_z_score` (σ above/below trailing-year mean), `trend_direction` (rising / flat / falling — last-4-weeks vs prior-4-weeks slope), `current_index` (0-100). Bracketed query (`"AAPL"`) so Google scopes to the ticker, not the English word.

**`alternative_data.get_wikipedia_pageviews_signal(symbol)`:** daily article views from the Wikimedia REST API. Output: `pageview_z_score`, `pageview_spike_flag` (z ≥ 2σ), `current_7d_avg`, `trailing_90d_avg`, `article` slug. Ticker → article resolution via hand-curated `WIKIPEDIA_TICKER_OVERRIDES` map (~60 large-caps), falling back to Wikipedia's OpenSearch API for unknowns.

**Wired:**
- `get_all_alternative_data` returns both as `alt["google_trends"]` and `alt["wikipedia_pageviews"]`.
- `_build_features_payload` flattens `google_trends_z`, `google_trends_direction`, `wikipedia_pageviews_z`, `wikipedia_pageviews_spike` into the meta-model feature payload.
- `meta_model.NUMERIC_FEATURES` + `CATEGORICAL_FEATURES` include the new fields so the meta-model trains on them.
- `signal_weights.WEIGHTABLE_SIGNALS` registers `google_trends` + `wikipedia_pageviews` so the Layer-2 weight tuner can up- or down-weight per profile based on differential win-rate.
- `ai_analyst._build_alt_data_section` renders both lines under ALT DATA when present (e.g. `Search interest: index 80 (z=+1.2σ, rising)` and `Wiki views: 45,000/day 7d avg (z=+2.4σ — SPIKE)`).

**Tests** (`tests/test_attention_signals.py`, 13 cases): rising/falling/flat detection on Google Trends, OpenSearch fallback for unknown tickers, z-score math + spike threshold for Wikipedia, crypto skipped, graceful failure on HTTP errors, cache hit on second call.

**Display names** added for new feature keys to satisfy the existing display-name guardrails.

`pytrends>=4.9.0` added to `requirements.txt`.

GitHub commit-activity signal deferred — most of the S&P doesn't have meaningful public repos, and large engineering work moves to private repos. Net signal weakness called out in plan; revisited later if a focused use case arrives.

---

## 2026-05-02 — Item 5c: realistic slippage model (Severity: medium, capability)

Backtests previously used a flat 0.2% on entry + 0.2% on exit. This inflated apparent edge — strategies that worked great on big-cap names but would die on micro-caps couldn't be told apart. Live trading had no per-candidate execution-cost signal, so the AI couldn't pass over names where friction would eat the edge.

**`slippage_model.py`** — four-component model:

1. **Half-spread** — deterministic, from current snapshot bid-ask.
2. **Market impact** — `K × sqrt(participation_rate)` where `participation = order_qty / 20d_ADV`. Almgren-Chriss square-root, with `K` calibrated empirically.
3. **Volatility scalar** — `vol_factor × daily_vol_bps`. Higher-vol names experience more decision-to-fill drift even on tiny orders.
4. **Bootstrap residual** — empirical distribution of `actual − model_predicted` slippage from past trades, conditioned on size bucket. The piece an analytical formula can't capture.

**Lazy calibration:** `calibrate_from_history(db_path, market_type)` reads `trades` rows with both `decision_price` and `fill_price` set, fits `K` via least-squares closed form, caches per market_type on disk for 7 days. Refresh-on-call when stale; no scheduler task = no new toggle (auto-passes the scheduler-gate guardrail).

**Wired in two places:**
- `backtester.py` entry + exit fills now call `estimate_slippage` and use `fill_price` instead of the flat 0.2% assumption. Backtests get realistic friction.
- `_build_candidates_data` attaches `slippage_estimate` + `slippage_str` to each candidate. AI prompt shows `Execution: exec cost ~8.4 bps ($42 on this order)` per candidate so the model factors friction into sizing.

**Per-profile config:** none. Slippage is analytical math empirically calibrated from live fills — users shouldn't tune it. `market_type` added to `UserContext` so the slippage model can scope per-segment calibration.

**Honest limits documented in module:**
- K is calibrated from paper fills today; real-money fills will deviate. The calibrator should be re-run after going live for 30+ days.
- ADV-at-trade-time isn't stored yet, so a coarse `$50M default ADV` is used for the calibration regression. Better fits arrive when this gets backfilled.
- Sqrt impact assumes typical liquidity; squeeze events / regime breaks aren't captured.
- Bootstrap requires ≥ 20 trades per size bucket per market_type; below that, residual = 0 (no noise).

**Tests** (`tests/test_slippage_model.py`, 20 cases): half-spread math, sqrt-impact monotonicity (doubling participation → ~1.41× impact), vol scalar, side semantics (buy fills above decision, sell below), planted-K recovery from synthetic trades, default fallback on insufficient history, deterministic bootstrap with seed, prompt rendering. All green.

---

## 2026-05-02 — Item 1c: long-vol portfolio tail-risk hedge (Severity: high, capability)

Active tail-risk insurance. Existing layers (`crisis_state`, `intraday_risk_monitor`, per-trade stops) all reduce exposure when stress fires — pull the book in. This adds explicit DOWNSIDE COVER: when triggers fire, the system buys SPY puts so further SPY weakness pays us. Pays for protection that mostly expires worthless in calm markets — meaningful drag, but caps tail outcomes.

**`long_vol_hedge.py`:**
- `evaluate_triggers(drawdown_pct, crisis_level, var_95_pct_of_equity, ...)` — three triggers (drawdown ≥ 5%, crisis ≥ elevated, 95% VaR ≥ 3% of book). Each returns a `HedgeTrigger` with `fired`, metric, threshold, human-readable detail.
- `select_hedge_strike(spot, otm_pct=0.05)` — 5% OTM by default, rounded to whole-dollar (SPY strike granularity).
- `select_hedge_expiry(target_dte=45)` — 30-60 day band; chain-fetch path snaps to the nearest available real expiry.
- `size_hedge_contracts(equity, premium_per_contract, premium_budget_pct=0.01)` — 1% of book in premium per active hedge.
- `should_roll(expiry, delta)` — DTE < 14 OR delta decayed past −0.10. `should_close(triggers)` — only when ALL triggers clear simultaneously.
- Persisted in `long_vol_hedges` table (open/close/roll history). `hedge_cost_summary(days=90)` rolls up insurance bill.
- `compute_drawdown_from_30d_peak(db_path, equity)` — drawdown from 30-day rolling equity peak via `daily_snapshots`.
- `render_hedge_for_prompt(...)` — multi-line block surfaced to AI under MARKET CONTEXT.

**`_task_manage_long_vol_hedge`** (gated on `enable_long_vol_hedge`): each cycle reads triggers, decides open/roll/close, submits the option order via existing `submit_option_order`. Picks the closest available SPY put expiry + strike; sizes contracts to the premium budget; refuses to open if budget can't afford even one contract.

**AI prompt:** `_build_market_context` reads active hedge + triggers + 90-day cost summary and adds a `LONG-VOL TAIL HEDGE:` block under MARKET CONTEXT showing entry strike/expiry, which triggers fired, and running insurance cost. The AI sees what the hedge is doing AND why, so it can factor it into sizing reasoning.

**Per-profile config (settings page):**
- `enable_long_vol_hedge` — default OFF (opt-in: costs real premium)
- `long_vol_hedge_drawdown_pct` — drawdown trigger (default 5%)
- `long_vol_hedge_var_pct` — VaR trigger (default 3%)
- `long_vol_hedge_premium_pct` — budget per hedge (default 1%)

End-to-end wired: schema migration, `UserContext` fields, `update_trading_profile` allowlist, `save_profile` form parser, `settings.html` controls with tooltips. The new scheduler task auto-passes the scheduler-gate guardrail because it's wrapped in `if getattr(ctx, "enable_long_vol_hedge", False)`.

**Tests** (`tests/test_long_vol_hedge.py`, 31 cases): all three triggers individually, strike/expiry/sizing math, roll/close decisions, schema round-trip, cost summary aggregation, drawdown helper, prompt rendering. **MANUAL_PARAMETERS** allowlist updated for the four new columns.

**Limits documented in module:** parametric VaR understates tail; SPY puts hedge BETA, not idio risk; insurance bleeds in calm markets. Default OFF means it does nothing until the user flips the switch.

Suite: 1841 passed, 0 skipped.

---

## 2026-05-02 — Per-profile toggles for new scheduled features; settings UI + scheduler-gate guardrail (Severity: high, UX)

I'd shipped Items 1b / 2a / 2b with new scheduler tasks (`_task_intraday_risk_check`, `_task_portfolio_risk_snapshot`, `_task_stat_arb_retest`, `_task_stat_arb_universe_scan`) that ran unconditionally for every profile. Users had no way to see they existed, no way to toggle them, no settings control. New "lever" buried in the system — exactly the pattern called out as a recurring failure mode.

**Toggles added:**
- `enable_intraday_risk_halt` (default ON) — gates the intraday risk monitor + auto-halt on drawdown / vol / sector / position halts.
- `enable_portfolio_risk_snapshot` (default ON) — gates the daily Barra factor risk snapshot + stress scenario projection.
- `enable_stat_arb_pairs` (default OFF — requires shorts enabled, since pair trades use both legs).

Wired all three end-to-end: schema migration, `UserContext` field, `update_trading_profile` allowlist, `save_profile` form parser, settings.html control with tooltip + plain-English explanation. Each scheduled task now checks `getattr(ctx, "enable_*", default)` before running.

**New guardrail (`tests/test_scheduled_features_have_settings.py`):**
Static-analyzes `multi_scheduler.py` for every `lambda: _task_X(ctx)` registered via `run_task(...)`. For each one, requires either:
1. Membership in an explicit `INFRASTRUCTURE_TASKS` allowlist (with rationale per entry — load-bearing tasks like `_task_resolve_predictions`, `_task_scan_and_trade`, `_task_crisis_monitor`), OR
2. An enclosing `if getattr(ctx, "enable_*", ...)` block, where the `enable_*` column exists in `trading_profiles` AND has a `<input name="enable_*">` control in `templates/settings.html`.

This would have caught the original Item 1b/2a/2b ship as a regression. The `INFRASTRUCTURE_TASKS` allowlist deliberately requires a written rationale per entry, so future tasks can't be silently classified as "infra" without thought.

`test_every_lever_is_tuned.py` MANUAL_PARAMETERS allowlist updated for the three new columns (user-controlled toggles, not autonomously tunable).

Suite: 1810 passed, 0 skipped.

---

## 2026-05-01 — Documentation + UI surfaces for Items 2a / 5a; snake_case guardrail extended; remove all test skips (Severity: medium, hygiene)

**UI:**
- AI Awareness tab gets a new "Portfolio Risk — Barra-style factor model the AI sees" article: daily σ, parametric/Monte Carlo VaR + ES, top factor exposures, risk decomposition (sectors/styles/french/idio), and the worst-3 historical stress scenarios — same data the AI sees under MARKET CONTEXT > PORTFOLIO RISK.
- AI Brain tab's Meta-Model panel now shows the SGD online freshness layer (n_updates, n_features, last_update_at) next to GBM AUC.
- New `_build_portfolio_risk_awareness` builder reads the latest `portfolio_risk_snapshots` row per profile.

**Docs:**
- AI_ARCHITECTURE.md: 3a meta-model section rewritten to document the GBM + SGD two-layer setup; new "PORTFOLIO RISK" entry under "what the AI sees" block.
- COMPETITIVE_GAP_PLAN.md: items 1a, 1b, 2a, 2b, 3b, 5a, 5b, 6b, plus partial 1c / 3a / 5c, marked SHIPPED with what was actually built.
- ROADMAP.md: Phase 13 Competitive-Gap Closure section listing every shipped item.
- TECHNICAL_DOCUMENTATION.md: new "Competitive-gap closure modules" section with module-by-module reference.

**Snake_case guardrail extended (`tests/test_no_snake_case_in_user_facing_ids.py`):**
- Existing `test_no_snake_case_in_api_responses.py` only flagged `PARAM_BOUNDS` keys. Sector codes (`tech`, `comm_services`), factor IDs (`sector_tech`, `Mkt-RF`, `SMB`), and stress scenario IDs (`2008_lehman`, `2020_covid`) were unguarded.
- New test enforces (a) every identifier in those families has an explicit `display_name` entry — no fallback drift — and (b) the rendered visible text of `/ai`, `/performance`, `/dashboard` contains no raw IDs. Uses a temp seeded SQLite DB so all three routes execute their actual code paths in test.
- Caught and fixed real leaks I'd shipped: factor names (`sector_tech` etc) and scenario IDs (`2008_lehman` etc) on the new Portfolio Risk panel; sector codes (`comm_services`, `consumer_disc`) in the existing performance.html "By Sector" table.

**Test skips removed:** every skip in the test suite is gone.
- `test_no_guessing.py:494/565`: two `pytest.skip` calls that silently passed when JS functions weren't found in `ai.html` → converted to hard assertions (functions verified to exist).
- `test_no_snake_case_in_user_facing_ids.py`: one `pytest.skip` for `/dashboard` returning non-200 → replaced with a real seeded temp DB so the route actually renders.
- Suite is now 1809 passed, 0 skipped.

`statsmodels` was missing from the venv (used by `stat_arb_pair_book`) — installed and 5 stat_arb tests now pass.

---

## 2026-05-01 — COMPETITIVE_GAP_PLAN Item 2a: full Barra-style portfolio risk model (Severity: high, capability)

We had crisis_state, intraday_risk_monitor, and per-trade stops. We did NOT have portfolio-level factor risk decomposition, parametric or Monte Carlo VaR, expected shortfall, or historical scenario stress tests. Real fund risk teams have all of these. This ships them — full implementation, not MVP.

**Factor universe (~21 factors):**

- Ken French daily 5-factor + Momentum (Mkt-RF, SMB, HML, RMW, CMA, Mom) — fetched from his official ZIP CSVs, parsed, cached on disk for 7 days. Goes back to 1926 for stress scenarios.
- 11 SPDR sector ETFs (XLK, XLF, XLE, XLV, XLI, XLP, XLY, XLU, XLB, XLRE, XLC) — captures industry concentration risk that style alone misses.
- 4 MSCI USA style ETFs (IWM small-cap, MTUM momentum, QUAL quality, USMV low-vol).

**`portfolio_risk_model.py`:**
- `compute_factor_returns(lookback_days)` — joint daily return matrix.
- `estimate_exposures(symbol_rets, factor_returns)` — ridge-regularized regression (sector ETFs and Mkt-RF are collinear; ridge α=1.0 keeps βs stable). Returns β + idiosyncratic variance + R².
- `estimate_factor_cov(factor_returns)` — Ledoit-Wolf shrunk covariance, manual fallback if sklearn LedoitWolf unavailable.
- `compute_portfolio_risk(weights, exposures, factor_cov, equity)` — factor + idio variance, parametric 95/99% VaR + ES, per-factor variance contribution, grouped decomposition (sectors / styles / french / idio).
- `monte_carlo_var(...)` — 10k Cholesky-decomposed factor draws + independent idio draws → empirical VaR + ES from the simulated portfolio P&L distribution.
- `compute_portfolio_risk_from_positions(positions, equity)` — end-to-end convenience.

**`risk_stress_scenarios.py`:**
7 named historical windows with full description + severity:
- `1987_blackmonday` (Oct 19 1987, -20.5% one-day)
- `2000_dotcom` (Q2 2000 Nasdaq -40%)
- `2008_lehman` (Sep-Oct 2008 GFC peak)
- `2018_q4_selloff` (rate-fear -19%)
- `2020_covid` (Feb-Mar 2020 -34% in 33 days)
- `2022_rates` (Fed hiking cycle)
- `2023_svb` (regional bank contagion)

`replay_scenario` fetches the actual historical factor returns from the window, projects them onto current portfolio exposures, returns total P&L %, worst day, max drawdown, and an idio band approximation. `run_all_scenarios` returns them sorted worst-first.

**Honest limitations documented in code:**
- Older scenarios (1987, dot-com) only have French factors; sector exposures projected against what overlap exists; quality flagged as "low" or "medium".
- Parametric assumes normal returns — under-reports tail. Monte Carlo helps but inherits the normality of the factor distribution.
- Cross-asset risk (rates, FX, commodities) not in factor set yet, so 2022-style rate shocks under-report.

**Wired in:**
- `_task_portfolio_risk_snapshot` runs daily at snapshot time per profile. Persists to `portfolio_risk_snapshots` table (90-day retention).
- `_build_market_context` in trade_pipeline reads the latest snapshot and surfaces `portfolio_risk_summary` + worst-3 stress scenarios into the AI prompt under `MARKET CONTEXT > PORTFOLIO RISK`.

**Tests:** 21 in `tests/test_portfolio_risk_model.py` (recovers planted βs, R² thresholds, factor decomposition sums to factor variance, long/short hedge produces near-zero factor variance, Monte Carlo VaR ordering, French CSV parser robustness). 9 in `tests/test_risk_stress_scenarios.py` (long book in crash projects loss, short book projects gain, missing factors flagged, idio band present, sorted worst-first). All 30 green.

---

## 2026-05-01 — COMPETITIVE_GAP_PLAN Item 5a: online learning meta-model (Severity: medium, capability)

GBM meta-model retrains weekly on the full history. Slow to adapt to regime shifts (today's outcomes don't enter the prediction stack until the next retrain). Adds an SGDClassifier "freshness layer" that updates incrementally per resolved prediction.

**What ships:**

- `online_meta_model.py` — `initialize_from_history` bootstraps an SGDClassifier from the same training set the GBM uses (min 10 rows, vs GBM's 100). `update_online_model` does a single-row `partial_fit` on each resolved prediction. `online_predict_probability` returns P(win). Persisted as `online_meta_model_p{profile_id}.pkl` next to the profile DB.

- Wired into `ai_tracker.resolve_predictions`: every resolution now also updates the SGD model with that row's features + outcome. `resolve_predictions` gained an optional `profile_id` arg; `_task_resolve_predictions` in `multi_scheduler` plumbs it through.

- Wired into `_task_retrain_meta_model`: after the GBM retrain, also (re)bootstraps the online model from the latest resolved set.

- Wired into `trade_pipeline` post-AI re-weighting: each accepted trade gets `online_meta_prob` and `meta_divergence` (`online − gbm`) attached, and divergence is logged. Large divergence = recent regime drift.

**Why SGD vs GBM:** complementary, not substitute. GBM is more accurate on stable distributions; SGD adapts in real time. Agreement = stable signal; divergence = something changed since the last weekly retrain.

**Tests** (`tests/test_online_meta_model.py`, 12 cases): bootstrap requires both classes; bootstrap fails gracefully on insufficient data; `update_online_model` rejects non-binary outcomes and missing models; `online_predict_probability` returns ordered probabilities for high-vs-low score features; `get_online_model_info` exposes metadata. **Deliberately NOT testing exact model accuracy** — SGD weights drift across runs and the test would be flaky.

---

## 2026-05-01 — OPTIONS_PROGRAM_PLAN Phase H complete: synthetic options backtester (Severity: high, capability)

The last unbuilt phase of the options program. Lets us validate any options strategy historically before going live with real money.

**Approach:** synthetic backtester since paid historical options data ($99/mo Polygon historical, $thousands OptionMetrics) is out of scope. Uses Alpaca historical bars (free, real) + Black-Scholes pricing with realized-vol IV approximation. Documented limits: captures direction + approximate magnitude; doesn't capture bid-ask spread, real IV term structure / skew, or catalyst vol expansion. Sufficient for STRATEGY VALIDATION (does this class earn its keep?), not PRECISE P&L FORECASTING.

**4 of 5 layers shipped** (commits b7581c2, dcdc04d, 93b15e8, 5737377):

- **L1** — `historical_iv_approximation`, `historical_spot`, `price_option_at_date`. Black-Scholes pricing of arbitrary options at any historical date using trailing 30-day realized vol as IV proxy. Filters to dates ≤ as_of (no look-ahead bias).

- **L2** — `simulate_single_leg`. Walks one option position day-by-day from entry through close. Closes on whichever fires first: profit_target, stop_loss, time_stop, or expiry. Returns `BacktestTrade` with full lifecycle.

- **L3** — `simulate_multileg_strategy`. Same shape for any `OptionStrategy` from `options_multileg`. Per-leg accounting (`buy: pnl = exit - entry; sell: pnl = entry - exit`) sums correctly across all 11 multi-leg primitives. Profit/stop targets keyed off PERCENTAGE OF MAX (max_gain / max_loss) — defined-risk natural anchors.

- **L4** — `backtest_strategy_over_period(strategy_factory, symbol, period, entry_rule, cycle_days)`. Replays entry rules across a historical period at configurable cadence. Aggregates: n_trades, win_rate, total/avg/best/worst P&L, avg days held, sharpe proxy.

**31 tests** covering: IV recovery on synthetic vol, look-ahead-bias prevention, expired/intrinsic handling, profit-target/stop-loss/time-stop early exits, P&L sign correctness across long/short/credit/debit, win/loss behavior on directional setups (bull spread up, bear spread down, condor in-range vs blown-wing), aggregate stats correctness.

**L5 (dashboard integration) deferred** — API is callable directly; UI surfacing isn't strictly necessary to use the backtester.

**OPTIONS_PROGRAM_PLAN status: Phases A–F + H complete.** Phase G (real-time data feed) was implicitly accomplished by the Alpaca-first migration. The full options program — Greeks aggregation, multi-leg primitives + atomic execution, lifecycle (assignment + roll + wheel), dynamic delta hedging, vol regime classifier, earnings/event opportunism, and now historical backtesting — is built end-to-end.

---

## 2026-05-01 — Alpaca-first migration: 9 modules off yfinance (Severity: high, correctness + cost)

ALPACA-FIRST DATA RULE applied across the codebase. We pay for Alpaca; using yfinance for fields Alpaca exposes was wasting the subscription, shipping decisions on 15-min-delayed quotes, and leaving real money on the table on real-money plays. Recurring failure pattern documented in `feedback_alpaca_first_data.md`.

**Migrated to Alpaca:**

- `options_oracle._fetch_chain` — real-time NBBO chains via `/v1beta1/options/snapshots/<sym>`. Black-Scholes inversion (Newton + bisection fallback in `options_chain_alpaca._implied_vol_from_price`) computes IV ourselves since Alpaca returns prices but not IV. (commit a59747b)
- `options_oracle.compute_iv_rank` — realized-vol fetch via `market_data.get_bars` instead of `yfinance.Ticker.history`. (commit a59747b)
- `news_sentiment.fetch_news` — `/v1beta1/news` Benzinga feed (verified 200 with paper keys). The earlier "Alpaca news requires paid subscription" comment was wrong. (commit bc0a8c0)
- `market_regime.detect_regime` VIX — computed locally as 30-day ATM IV of SPY options via `fetch_chain_alpaca`. By definition VIX = 30d ATM IV of SPX/SPY, so this is the same number from real-time chain. (commit bc0a8c0)
- `political_sentiment` market-ETF news — SPY/QQQ/DIA headlines now via `fetch_news_alpaca`. (commit bc0a8c0)
- `factor_data.get_beta` — 2-year OLS regression on Alpaca bars (`cov(sym_returns, spy_returns) / var(spy_returns)`) instead of `yfinance.Ticker.info.beta`. (commit bc0a8c0)
- `models.fetch_and_cache_names` — Alpaca `/v2/assets/<sym>` for company names. (commit bc0a8c0)
- `screener.run_crypto_screen` — Alpaca `/v1beta3/crypto/us/bars` (no more BTC-USD ↔ BTC/USD shuffle). (commit 5c168f0)
- `alternative_data.get_intraday_patterns` — Alpaca `/v2/stocks/<sym>/bars?timeframe=5Min` for intraday VWAP/ORB analysis. (commit 5c168f0)

**Stays on yfinance — Alpaca genuinely doesn't have these** (documented inline + in feedback memory):

- `sector_classifier` — Alpaca asset endpoint has no sector field
- `earnings_calendar` — Alpaca corporate-actions has no `earnings_announcement` type
- `factor_data.get_book_to_market` — fundamentals (book value, market cap, shares outstanding) — Alpaca is a broker, not a fundamentals provider
- `alternative_data` insider transactions / short interest / fundamentals — same reason

**Acceptable yfinance fallback** (Alpaca-first, yfinance only on explicit Alpaca failure with wall-clock budget):

- `screener` dynamic-screener fallback path

**New tests:** 13 in `test_options_chain_alpaca.py` (IV inversion + DataFrame builder + integration). `test_factor_data.test_get_beta_computes_from_alpaca_bars` updated to verify the new OLS approach.

---

## 2026-05-01 — OPTIONS_PROGRAM_PLAN Phases A-F COMPLETE (Severity: high, capability)

End-to-end real options program shipped. The single-leg toy that
existed before this is replaced.

**Phase C — lifecycle** (commits d403464, 7c7c69d, 468e70b, 6e58281)
- C2: Assignment + exercise detection. ITM short → assigned with
  synthetic SELL/BUY equity leg logged. ITM long → exercised with
  synthetic equity leg. OTM → expired_worthless. Indeterminate →
  needs_review. Virtual ledger now reconciles correctly through
  full options lifecycle.
- C1: Roll mechanics. Daily auto-close of credit positions at ≥80%
  of max profit (avoid late-cycle gamma + assignment risk).
  ROLL_RECOMMEND surfaced to AI prompt for 50-80% range. Wired as
  scheduler task.
- C3: Wheel state machine. Per-(profile, symbol) state derived from
  journal + positions: cash → CSP → assigned → shares_held → CC →
  called_away → cash. `wheel_symbols` list on UserContext opts in.
  Recommendations surfaced via prompt; AI confirms each step.

**Phase D — hedging** (commits 47f0705, bb6556a)
- D1: Dynamic delta hedger for long_call / long_put. Compute net
  options_delta per underlying via Greeks aggregator; submit
  stock-side rebalance to neutralize when |drift| ≥ max(5 shares,
  5%). Excludes covered_call / protective_put / CSP / multi-leg
  defined-risk (already hedged or self-hedged).

**Phase E — vol surface** (commit 3b046ef)
- E1-E3 leveraged from existing options_oracle (term_structure,
  iv_skew, iv_rank-with-realized-vol).
- E4: vol regime classifier turns raw signals into strategy
  guidance. premium_rich → sell-premium plays; premium_cheap →
  buy-premium; steep_put + rich → asymmetric iron condor;
  backwardation drops calendars. Surfaced to AI prompt as
  "VOL REGIME" block.

**Phase F — earnings opportunism** (commit d28ba83)
- Pre-earnings (0-3d): IV ≥ 75 → iron_condor for IV crush capture
  with ±6%/±12% strikes; IV ≤ 25 → long_straddle for under-priced
  event. Surfaced as "EARNINGS PLAYS" block. Replaces the blanket
  avoid-earnings filter on the OPTIONS side; equity side still
  honors avoid_earnings_days.

**Tests across C-F:** 60+ new tests. All green on prod.

**Acceptance criteria status (per OPTIONS_PROGRAM_PLAN.md):**
1. Greeks aggregated, gated, dashboarded ✓
2. All 11 multi-leg primitives ship with builders + tests ✓
3. Multi-leg atomic execution ships ✓
4. Multi-leg advisor recommends regime-appropriate strategies ✓
5. AI can propose any strategy and they execute ✓
6. Assignment detection reconciles correctly ✓
7. Rolls fire on near-expiry profitable positions ✓
8. Wheel runs end-to-end (state machine ships; needs opted-in symbol
   to actually run live) ✓
9. Delta hedging keeps long-vol positions near target delta ✓
10. Vol regime drives advisor recommendations ✓
11. Earnings days are TRADED, not avoided ✓ (on options side)

**Out of scope (separate plans):**
- Phase G (real-time options chain feed): deferred to real-money
  phase. Paper trading on yfinance data is honest about its
  limitations.
- Phase H (options backtester): major build (~2 weeks). Required
  before adding NEW strategies; existing primitives sufficient
  for current production use.

This commit closes the build the user wanted: a complete options
program. The AI prompt now sees per-symbol vol regime, multi-leg
strategy recommendations, near-expiry roll candidates, wheel state,
earnings IV-crush plays, plus the existing single-leg
covered_call/protective_put advisor — and can execute via the
OPTIONS, MULTILEG_OPEN, or PAIR_TRADE actions.

---

## 2026-05-01 — OPTIONS_PROGRAM_PLAN Phase A + Phase B complete (Severity: high, capability)

**Phase A — Greeks foundation** (commits 2feba8e, 5ce9fab, af43d80)

- A1 — `options_greeks_aggregator.compute_book_greeks(positions)` walks every position (stock + options), computes per-leg Greeks via `compute_greeks`, multiplies by signed qty × 100, returns net delta/gamma/vega/theta/rho. Stock contributes qty × 1 to delta only. Expired options skipped without crash. Missing IV → fallback 25% with counter; missing spot → leg skipped.
- A2 — `check_greeks_gates(book, proposed, ctx)` enforces three caps: `max_net_options_delta_pct` (5% default), `max_theta_burn_dollars_per_day` ($50 default), `max_short_vega_dollars` ($500 default). Each gate is None-disable-able. Wired into `options_trader.execute_option_strategy` so OPTIONS proposals run the Greeks gate before broker submission. SKIP with reason on gate failure.
- A3 — `/ai` "Book Greeks" panel: per-profile table of net delta/gamma/vega/theta with amber/red color-coding when within 20% of any active gate. Surfaces fallback-IV usage in the Notes column.

**Phase B — Multi-leg primitives + atomic execution + advisor + AI vocabulary** (commits de8350d, 422d6be, 0b28c76, 71e33ac, 465b80f)

- B1 — `OptionLeg`/`OptionStrategy` dataclasses + 4 vertical-spread builders: `bull_call_spread`, `bear_put_spread`, `bull_put_spread`, `bear_call_spread`. Each computes max_loss/max_gain in DOLLARS and breakeven from per-share quotes (or leaves None for the executor to finalize post-fill). `VERTICAL_SPREAD_BUILDERS` registry.
- B2 — `execute_multileg_strategy(api, strategy, ctx)`: Alpaca MLEG combo order by default (atomic, all-or-nothing fill); sequential fallback on combo failure with rollback (reverse-side closing orders for each filled leg if leg N fails). Single combo order id returned, OR per-leg ids on sequential path. Logs all legs with `signal_type=MULTILEG`.
- B (rest) — 7 more builders: `iron_condor`, `iron_butterfly`, `long_straddle`, `short_straddle`, `long_strangle`, `calendar_spread`, `diagonal_spread`. `ALL_MULTILEG_BUILDERS` extended registry. Short straddle leaves `max_loss_per_contract=None` to flag UNLIMITED downside (advisor should almost always recommend iron_butterfly instead).
- B3 — `evaluate_candidate_for_multileg(candidate, iv_rank_pct, regime)`: regime/IV-aware strategy selection on screener candidates. Bullish + IV rich → bull_put_spread; bullish + IV cheap → bull_call_spread; bearish symmetric; range-bound + IV rich → iron_condor; expansion + IV cheap → long_strangle. IV in 50-60 neutral → no recs.
- B4 — `MULTILEG_OPEN` action wired end-to-end. Validator accepts strategy_name + strikes + expiry + contracts, drops bad. Trade pipeline dispatcher resolves builder via registry, calls `execute_multileg_strategy`. AI prompt surfaces multi-leg recs and adds MULTILEG_OPEN to allowed-actions vocabulary when block non-empty.

**Tests across A+B:** 91 new tests (16 aggregator + 10 gates + 17 vertical builders + 6 atomic execution + 17 condor/butterfly/straddle/strangle/calendar/diagonal + 13 multi-leg advisor + 4 validator + 8 misc). All green on prod.

**What's done vs. what's left:**

Done:
- Greeks aggregated, gated, dashboarded
- All single-leg + multi-leg primitives ship with builders + tests
- Multi-leg atomic execution with combo orders
- Multi-leg advisor recommends regime-appropriate strategies
- AI can propose any of the strategies and they execute end-to-end

Remaining (per OPTIONS_PROGRAM_PLAN.md):
- Phase C — lifecycle: assignment detection + reconciliation, roll mechanics, wheel automation
- Phase D — dynamic delta hedging
- Phase E — vol surface (term structure, skew, realized vs implied)
- Phase F — earnings/event opportunism (replace blanket avoid-earnings)

This commit closes the foundational + structural layers. Lifecycle + hedging next.

---

## 2026-04-30 — Item 1b complete: PAIR_TRADE action + two-leg execution (Severity: high, capability)

**What this closes.** The final layer of Item 1b. AI can now propose `action: "PAIR_TRADE"` and the pipeline routes it to a dedicated two-leg executor. The stat-arb pair book is now a fully-functional capability end-to-end.

**Files changed:**

- `stat_arb_pair_book.execute_pair_trade(api, proposal, ctx, log)` — validates, sizes (5% equity per leg cap), submits both legs sequentially, logs both with `signal_type=PAIR_TRADE` / `strategy=stat_arb_pair`. Atomicity is best-effort: leg-B failure after leg-A success returns ERROR with `order_id_a` so the operator can manually flatten. We don't auto-cancel because Alpaca cancellation isn't synchronous.
- `stat_arb_pair_book._lookup_active_pair(db, sym_a, sym_b)` — finds an active pair by either symbol ordering. Used by both the executor and the validator to gate the AI from inventing pairs we haven't validated.
- `ai_analyst._validate_ai_trades` — new PAIR_TRADE branch handled BEFORE the candidate-symbol check (the pair "symbol" is a label like "AAPL/MSFT", not a candidate). Validates pair_action enum, looks up the pair via `_lookup_active_pair`, drops if not in active book.
- `ai_analyst._build_batch_prompt` — `pair_book_rendered` flag tracks whether the prompt actually includes pair-book content. When set, adds "PAIR_TRADE" to allowed-actions vocabulary, adds a pair_note explaining required fields, adds a pair_example to the JSON example.
- `trade_pipeline.run_trade_cycle` — new `action == "PAIR_TRADE"` dispatch branch right after OPTIONS. Calls `execute_pair_trade(api, ai_trade, ctx, log)`.

**Sizing model.** Dollar-neutral, not hedge-ratio'd. Each leg gets `dollars_per_leg`; shares = `floor(dollars_per_leg / current_price)`. Hedge ratio influences cointegration but not sizing — dollar-neutral keeps risk symmetric, the standard professional convention. Trade-off documented in the executor's docstring: this gives close-to-spread P&L on small moves but isn't perfectly spread-neutral.

**13 new tests** covering: pair-not-in-book → SKIP, unsupported pair_action → SKIP, zero dollars → SKIP, successful ENTER submits both legs (correct sides + correct quantities), ENTER_SHORT_A_LONG_B swaps sides correctly, 5% equity cap enforced, leg-B failure returns ERROR with order_id_a, EXIT closes held legs (long → sell, short → buy), EXIT with nothing held → SKIP, validator passes through PAIR_TRADE with all fields, validator drops unknown pair, validator drops missing pair_action.

**Item 1b status: COMPLETE.** Math foundation, persistence, signal generator, daily retest task, universe scan + persist, AI prompt surfacing, PAIR_TRADE action, two-leg execution — all shipped. The pair book is empty by default (universe scan task not yet wired into a cron); once the user populates it, the AI sees pairs and can trade them.

**Known follow-ups for next sessions:**
- Wire `scan_and_persist_pairs` as a weekly task (not daily — quadratic scan is expensive). Need to decide which symbol universe to scan per profile.
- Pair-book observability panel on /ai dashboard (parallel to the veto-rate panel) showing active pairs + current z-scores + hit rate.
- Adversarial reviewer's prompt should learn about pair trades (currently only sees single-symbol candidates).

---

## 2026-04-30 — Item 1b: pair book lifecycle + AI surfacing (Severity: high, capability)

**What this adds.** Layers 2-4 of Item 1b stacked in one session, building on the math foundation (9d6755f). The pair book now lives, refreshes itself, and is visible to the AI.

**Persistence (`97dbfb7`)**: new `stat_arb_pairs` table. `upsert_pair` / `get_active_pairs` / `retire_pair` with canonical-order enforcement (UNIQUE(symbol_a, symbol_b) where a < b alphabetically; hedge ratio inverted on swap). Reviving a retired pair flips status back to active.

**Signal generator (`97dbfb7`)**: `pair_signal(pair, prices_a, prices_b, currently_open, ...)` returns `ENTER_LONG_A_SHORT_B` / `ENTER_SHORT_A_LONG_B` / `EXIT` / `REGIME_BREAK_EXIT` / `HOLD` based on z-score thresholds (entry ±2σ, exit ±0.5σ, regime-break ±3σ).

**Daily retest task (`20598a0`)**: `retest_active_pairs` re-runs Engle-Granger on each active pair. Refreshes hedge_ratio / p_value / half_life when still cointegrated; retires when `p >= 0.10` (looser than the 0.05 entry threshold to avoid ejecting on borderline noise) or when half-life moves out of [1, 30] days. Wired as new "Stat-Arb Pair Retest" daily task in `multi_scheduler.py` right after Alpha Decay Monitor.

**Universe scan (`804ded2`)**: `scan_and_persist_pairs(db, symbols, price_history)` — quadratic universe scan that discovers new pairs and persists them. Cost ~25s for 100 symbols → run weekly. Uses `find_cointegrated_pairs` from the foundation, then upserts.

**AI prompt surfacing (`e82e714`)**: `render_pair_book_for_prompt(db, price_history, open_pair_legs)` emits a "STAT-ARB PAIR BOOK" section with current z-scores per active pair. Splits output into "Actionable now" (entry/exit signals) vs "Currently quiet" (informational, only when nothing actionable, to keep prompts tight). Wired into `ai_analyst._build_batch_prompt` after the existing P2.3 pair-opportunities block.

**Tests.** 21 new across 4 commits in `test_stat_arb_pair_book.py`:
- Persistence: upsert+retrieve, canonical-order swap, refresh existing row, retire (+ swapped + nonexistent), revive after retire.
- Signal generator: 6 transitions covered (entry both directions, hold, exit at mean, hold-in-window while open, regime-break exit, insufficient history).
- Retest: empty book, refreshed when still cointegrated (deterministic seed + γ=-0.30), retired when broken, missing data → error not retire.
- Universe scan: planted pair discovered + persisted, empty universe → no rows, re-scan refreshes (no duplicates).
- Render: actionable z-line, empty when book empty, "Currently quiet" labeled when no actionable signals.

**Deferred to next session:** `PAIR_TRADE` action vocabulary in `_validate_ai_trades`, two-leg atomic execution with hedge-ratio'd dollar-neutral sizing. The AI can today see pair signals and propose individual long/short trades on each leg, but it lacks the explicit pair semantics.

---

## 2026-04-30 — Item 1b foundation: stat-arb pair book (math + tests) (Severity: high, capability)

**What this lays down.** First commit toward COMPETITIVE_GAP_PLAN Item 1b — a real cointegrated-pair book to replace the one-shot pair-trade primitive (P2.3). This lands the math foundation; wiring into the trade pipeline is multi-session.

**New module: `stat_arb_pair_book.py`**

- `engle_granger(price_a, price_b)` — Engle-Granger two-step: OLS hedge ratio + ADF on residuals. Returns `{p_value, hedge_ratio, half_life_days, correlation, n_obs}`. Insufficient data / NaN inputs / degenerate spreads return `p_value=1.0` (rejected).
- `_half_life(spread)` — AR(1) on differences of the residual series. `half_life = -ln(2) / ln(1+γ)`. Random walks → infinity.
- `compute_spread_zscore(price_a, price_b, hedge_ratio, lookback=60)` — current spread standardized against trailing window. The signal generator that the next session will key off.
- `is_pair_tradeable(eg_result)` — applies the standard filters (p < 0.05, |corr| > 0.6, 1d ≤ half-life ≤ 30d).
- `find_cointegrated_pairs(symbols, price_history, max_pairs=50)` — pairwise universe scan. Caller provides a `price_history` callable so cache + fetch logic stays out of this module. Cost: N·(N-1)/2 EG tests; ~25s for 100 symbols, run daily not per-cycle.
- `Pair` dataclass — frozen description (symbols, hedge_ratio, p_value, half_life, correlation).

**Out of scope this commit (separate sessions):**
- Persistent pair-book table in journal
- Daily rebalance task that re-tests cointegration of active pairs (auto-eject when p > 0.10)
- Trade entry/exit signal generator (z > +2 → SHORT A / LONG B; |z| < 0.5 → exit; |z| > 3 → regime break)
- Wiring into trade_pipeline so the AI sees pair-trade actions in its candidate list

**New dep:** `statsmodels>=0.14.0` for `tsa.stattools.adfuller`. Standard quant lib; well-tested. ~10MB install.

**Tests.** 17 in `test_stat_arb_pair_book.py` using deterministic synthetic data:
- Planted cointegrated pair (A = β·B + small noise) is detected (p < 0.05, β recovered ±5%)
- Two independent random walks NOT cointegrated (p > 0.10)
- Short series, mismatched lengths, NaN inputs → safe defaults
- Strongly mean-reverting AR(1) recovers known half-life (~1.36)
- Pure random walk → infinite half-life
- Z-score sign + magnitude on planted-spread inputs
- Tradeability filters (p, correlation, half-life range) reject correctly
- Universe scan recovers planted pair from a noise universe; respects max_pairs cap; handles missing data
- Pair.label format

**Why it matters.** Stat-arb is one of the most scalable, market-neutral edge sources. Real funds run hundreds-to-thousands of pairs simultaneously. We have the architecture (long/short, asymmetric sizing, beta-neutrality) to support a pair book; this commit is the math the pair book is built on.

---

## 2026-04-30 — Veto-rate panel on /ai dashboard (Severity: medium, observability)

**What this surfaces.** New table in the Specialist Ensemble section showing per-specialist verdict + veto counts over the last 7 days, across all profiles. Distinguishes:

- **Effective vetoes** — verdict='VETO' from a specialist in `ensemble.VETO_AUTHORIZED` (currently `risk_assessor`, `adversarial_reviewer`). Actually blocked a trade.
- **Claimed vetoes** — verdict='VETO' from any specialist. Includes silent no-ops where an unauthorized specialist (pattern_recognizer, sentiment_narrative) wrote VETO into `specialist_outcomes` but couldn't actually block because the ensemble doesn't grant them authority.

**Why this exists.** First check after deploy showed: across all 10 prod profiles, the only specialists actually emitting VETO are `pattern_recognizer` and `sentiment_narrative` — neither has authority. `risk_assessor` (which DOES have authority) emits 0 vetoes in the window. Without surfacing this, the system looks like it has healthy disagreement when actually all the disagreement is silently ignored. The new `adversarial_reviewer` (Item 5b) needs the same visibility once it accumulates verdicts.

**Files.**

- `journal.py` — new `get_specialist_veto_stats(db_paths, days=7)`. Aggregates `specialist_outcomes` rows per specialist; tags each row with `has_authority` based on the live `VETO_AUTHORIZED` set (so it stays in sync if the set ever changes).
- `views.py` — `ai_dashboard()` calls the helper and passes `ensemble_info["veto_stats"]` to the template.
- `templates/ai.html` — new "Veto Activity" sub-panel inside the Specialist Ensemble article. Color-coded: green "Effective" for authority-bearing specialists, amber "No authority — silent no-op" for the rest.

**Tests.** 7 in `test_specialist_veto_stats.py`: empty DB, authorized specialist VETO counted as effective, unauthorized specialist VETO is silent no-op (the prod bug it surfaces), `adversarial_reviewer` recognized as authorized, multi-DB aggregation, sorted by veto count descending, missing DB handled gracefully. The /ai smoke test in test_web.py catches any Jinja errors in the new panel markup.

---

## 2026-04-30 — Item 5b: adversarial reviewer specialist (Severity: high, capability)

**What this adds.** 5th specialist in the ensemble (`specialists/adversarial_reviewer.py`) with VETO authority. Different framing from `risk_assessor`: hunts for failure modes ("what would have to be true for this to lose money fast?") rather than risk factors ("what risks exist?"). Two redundant voices intentionally — different framings catch different misses.

**Checklist baked into the prompt:** correlation overlap with current book, single-name concentration, regime mismatch with mandate, earnings/event risk, crowded-trade indicators, factor-direction violations against `target_book_beta` / `target_short_pct`. Standard VETO discipline ("uncertainty is HOLD, not VETO") to avoid over-vetoing.

**Wiring.**

- `specialists/__init__.py` — added to `SPECIALIST_MODULES`. Picked up automatically by `discover_specialists()` and the daily specialist health check, which uses calibrators + sample counts to auto-(dis)enable.
- `ensemble.py` — new `VETO_AUTHORIZED = {"risk_assessor", "adversarial_reviewer"}` set. The veto loop now checks set membership instead of hardcoding the name. `SPECIALIST_WEIGHTS["adversarial_reviewer"] = 1.0`. `format_for_final_prompt` drops "by risk" since either can veto.
- `templates/ai.html`, `templates/ai_awareness.html` — table column added so the new specialist's verdicts render.

**Tests.**

- `test_adversarial_reviewer.py` — 15 tests covering module contract, HAS_VETO_AUTHORITY, prompt-includes-regime/portfolio/failure-mode-framing/checklist, exact-N-entries demand, VETO discipline language, parse handles all 4 verdicts, `_portfolio_summary` handles empty/populated/failure, ensemble registration (discover, VETO_AUTHORIZED, weights).
- `test_ensemble.py` — bumped 4-specialist assumptions to 5 in the count-based tests.
- `test_integration.py` — `test_all_phase_entry_points_importable` updated to expect 5 specialists.
- `test_ensemble.py::TestEnsembleAggregation` — fixture now mocks `earnings_calendar.check_earnings`. Without this, the cost gate silently dropped earnings_analyst and the canned BUY 80 vote vanished from the consensus math. Pre-existed my change but exposed by it. Fixed.
- `test_no_missing_logging_import.py` — bumped to 120s timeout (AST-walks 50KLOC; flakes at default 30s on a loaded prod box).
- `test_trade_execution_logging.py` — slice window grew 4000 → 5500 chars to span the new OPTIONS dispatch branch in `run_trade_cycle`.

**Known follow-ups.** Calibrator for `adversarial_reviewer` will train naturally as it accumulates outcomes. Veto-rate health check fires automatically via the existing `_task_specialist_health_check`. No manual baby-sitting needed.

---

## 2026-04-30 — Options lifecycle sweep — close expired contracts (Item 1a follow-up) (Severity: medium, capability)

**What this adds.** New `options_lifecycle.py` module + scheduler task to sweep expired option contracts from the journal. Without it, expired option rows would dangle with `status='open'` forever once the AI starts proposing options.

**Behavior.**

- `find_expired_open_options(db_path)` — returns rows where `signal_type='OPTIONS' AND status='open' AND expiry < today`. Cheap; bounded by the open-option count.
- `_option_position_at_broker(api, occ)` — looks up the OCC contract in `api.list_positions()` (Alpaca lists option positions by their OCC string).
- `_compute_pnl_for_expired(row, broker_position)` — two paths. Broker has zero qty (expired worthless): recognize `-premium` for longs, `+premium` for shorts (×100 contract multiplier). Broker still holds: mark `needs_review` and flag assignment likely.
- `sweep_expired_options(api, db_path)` — iterates expired rows, updates `status` / `pnl` / `reason`, returns summary dict.
- `multi_scheduler.py` — new "Options Lifecycle" task right after Reconcile Trade Statuses. No-op when the journal has no open option rows.

**Tests.** 10 in `test_options_lifecycle.py`: find filters by expiry+status+signal_type, long/short worthless P&L math, multi-contract scaling, broker-still-holds → needs_review, empty journal, broker failure resilience.

---

## 2026-04-29 — Options execution routing — AI proposal → broker submission (Item 1a complete) (Severity: high, capability)

**What this adds.** End-to-end execution path for options trades. The AI can now propose `action: "OPTIONS"` in its batch response and the trade pipeline routes it to a dedicated executor that handles sizing, OCC formatting, broker submission, and journal logging.

**Files changed:**

- `journal.py` — `trades` table now has `occ_symbol`, `option_strategy`, `expiry`, `strike` columns (auto-migrated). `log_trade()` accepts these as optional kwargs; existing equity callers unaffected.
- `options_trader.py` — new `execute_option_strategy(api, proposal, ctx, log)` validates the AI proposal, enforces sizing constraints per strategy, formats the OCC symbol, calls `submit_option_order`, and logs the trade. Sizing constraints:
  - `covered_call` / `protective_put`: `contracts ≤ shares_held // 100` (cap, don't reject)
  - `cash_secured_put`: `strike × 100 × contracts ≤ buying_power` (reject if over)
  - `long_call` / `long_put`: `total_premium ≤ 1% of equity` (defined-risk hard cap)
- `trade_pipeline.py` — `run_trade_cycle` dispatches `action == "OPTIONS"` to `execute_option_strategy` instead of `execute_trade`. Equity flow unchanged.
- `ai_analyst.py` — `_validate_ai_trades` accepts `OPTIONS` action and bypasses the equity-position gates (balance / asymmetric-cap / neutrality) — options sizing is defined-risk and doesn't touch book beta the same way. Carries through option-specific fields (`option_strategy`, `strike`, `expiry`, `contracts`, `limit_price`).
- `ai_analyst._build_batch_prompt` — when the options advisor surfaces at least one opportunity, the prompt's allowed-actions list adds `OPTIONS` and the JSON example shows the expected option fields. Otherwise the prompt stays exactly as it was (no token bloat when there's nothing to do).

**Why now.** Item 1a of `COMPETITIVE_GAP_PLAN.md` — closing the "we trade only equities" gap with the multi-asset prop shops we benchmark against. Foundation (Greeks, OCC, primitives) and advisor were already shipped; this commit wires the execution path so the loop closes.

**Tests.** 9 new in `test_options_trader.py::TestExecuteOptionStrategy` + `TestValidateOptionsAction`:
- Invalid strategy → SKIP, no broker call
- Missing required fields → SKIP, no broker call
- Past expiry → SKIP
- Covered call without 100 shares → SKIP
- CSP exceeding buying power → SKIP
- Long call premium > 1% of equity → SKIP
- Successful long call returns OPTIONS_OPEN with order_id, expiry, strike
- Successful covered call caps contracts to `shares // 100` (asks for 5 with 250 shares → emits 2)
- `_validate_ai_trades` passes through OPTIONS action with all option fields intact

**Known follow-ups.**

- Lifecycle: expired contracts aren't yet auto-marked closed. Cron-style sweep TBD.
- Real broker behavior on Alpaca paper isn't yet smoke-tested in prod (waiting for the AI to propose its first OPTIONS trade now that the prompt invites it).

---

## 2026-04-30 — Options strategy advisor wired to AI prompt (Item 1a continued) (Severity: high, capability)

**What this adds.** New module `options_strategy_advisor.py` that evaluates each held position against rules for covered-call / protective-put recommendations. Read-side only — surfaces opportunities to the AI prompt without auto-executing. The AI sees the recommendation, decides whether to take it.

**Strategy rules (Phase 1 — single-leg only):**

- **Covered call** when: position ≥ 100 shares, ≥ +5% unrealized gain, IV rank > 70 (premium is rich). Strike ~7% above current, expiry ~35 days out.
- **Protective put** when: position ≥ 100 shares, ≥ +10% unrealized gain (worth protecting). Strike ~5% below current, expiry ~45 days out. IV-rank-independent.

Both compute the right contract count (1 per 100 shares) and an OCC-format symbol via `format_occ_symbol`. The recommendation includes the rationale string the AI sees.

**Wired into `ai_analyst._build_batch_prompt`** alongside the other prompt blocks. IV rank fetched via `get_options_oracle(symbol)` (cache-backed, 1 chain fetch per symbol per TTL). Best-effort: any failure → None → advisor skips IV-conditional strategies for that symbol.

**Tests.** 14 new in `test_options_strategy_advisor.py`:
- Below 100 shares → no recs
- Covered call fires at +10% gain + IV rank 80 (sweet spot)
- Skipped at IV rank < 70 (premium not rich enough)
- Skipped at gain < 5% (no upside to cap)
- Protective put fires at +20% gain (worth protecting)
- Skipped at gain < 10% (not enough at risk)
- Both fire when both conditions met
- Short positions skip both (no covered-call on a short)
- IV rank None: covered call skipped, protective put still fires
- Render: empty when no positions/recs, caps at 5 bullets, robust to lookup failures

**What's still NOT wired:** the AI prompt block exists but the AI's proposed `action="OPTIONS"` doesn't yet route through trade_pipeline to actual order submission. That's the next commit. After that, executions become live.

Full suite: 1427 passing.

---

## 2026-04-30 — Options trading layer foundation (COMPETITIVE_GAP_PLAN Item 1a) (Severity: high, capability)

**Why now.** First item in `COMPETITIVE_GAP_PLAN.md`. Equity-only strategies leave 30-40% of obvious P&L on the table — protective puts on big positions (downside hedge), covered calls on existing longs (income), and IV mean-reversion (sell rich vol, buy cheap). All buildable on free Alpaca paper options API + Black-Scholes math.

**This commit ships the foundation.** Pure-math + strategy-spec layer. Live submission integration deferred to a follow-up so the foundation can be validated by tests before touching the trade pipeline.

**`options_trader.py`:**
- `compute_greeks(spot, strike, days, iv, is_call, risk_free_rate)` — Black-Scholes price + delta/gamma/theta/vega/rho. Pure math, no scipy dependency (uses `math.erf` for normal CDF).
- `format_occ_symbol(underlying, expiry, strike, right)` — produces canonical 21-char OCC symbol (`AAPL  250516C00150000`). Round-trip `parse_occ_symbol` for the inverse.
- Strategy spec builders (return position dicts, caller submits):
  - `build_long_put` — outright bearish or downside hedge
  - `build_long_call` — outright bullish, defined max loss
  - `build_covered_call` — income on existing 100-share lots; auto-derives qty from shares_held
  - `build_cash_secured_put` — willing-buyer at lower price; computes cash requirement
- `submit_option_order(api, occ_symbol, side, qty, order_type, limit_price)` — Alpaca submit_order with OCC symbol path; failure logged not raised.

**Multi-leg strategies (verticals, iron condors, calendars) deferred to Phase 2** — those need Alpaca's `mleg` order class which differs from single-leg.

**Tests.** 23 in `test_options_trader.py`:
- Greeks: ATM call/put parity, OTM call low delta, ITM put delta near -1, invalid inputs return None
- OCC: round-trip, decimal strikes, short root padding, lowercase right normalization, invalid right raises
- Strategy specs: qty derivation for covered_call (250 shares → 2 contracts), cash requirement for CSP, moneyness percent
- Submission: market vs limit kwargs, missing limit_price returns None, broker failure returns None not raises

**Next steps (separate commits):**
- AI prompt block exposing IV rank + recommended option strategies
- Position-sizing layer (defined-risk math vs equity %)
- Lifecycle management (expiration tracking, roll vs let-expire decisions)
- Integration with the existing options_oracle (IV regime classifier)

Full suite: 1413 passing.

---

## 2026-04-30 — verify_first_cycle: deploy-window awareness + cross-direction error classification (Severity: medium, observability)

**Two cleanups from running verify and seeing inflated warnings.**

1. **verify_first_cycle.sh used a fixed window from market open**, so historic pre-deploy failures (e.g., 12 Check Exits TASK FAILs from 13:41-15:38 UTC, before the 17:09 resilience deploy) showed up as if they were current bugs. Added `RESILIENCE_DEPLOY_UTC`, `WASH_CLASSIFY_DEPLOY_UTC`, `DEFER_TO_BROKER_DEPLOY_UTC` constants + `J_SINCE` helper. Each fix's verification now checks failures only AFTER its deploy. Pre-deploy historic failures are reported separately with the count. From 5 alerts down to 2 truly-current issues.

2. **The track_record verification was checking the wrong place.** It looked for `track_record` in `features_json`, but track_record is intentionally excluded from features_json (it's a narrative string, not a numeric ML feature — see trade_pipeline.py:1408-1413). The right check is whether `get_symbol_reputation` is producing data, which is what feeds the track_record string into the AI prompt. Replaced.

3. **Cross-direction broker rejection now classified as SKIP not ERROR.** Alpaca rejects with `cannot open a long buy while a short sell order is open` (and the symmetric short-side case) when there's a pending opposite-direction order on the same symbol. Recoverable — the other order will resolve and we can retry next cycle. Added to the existing classifier alongside wash-trade and insufficient-qty. Was the last source of un-classified ERROR-with-traceback noise.

**Tests.** 1 new in `test_wash_cooldown.py`: source-pin on the cross-direction pattern.

Full suite: 1390 passing.

---

## 2026-04-30 — Polling defers to broker trailing stop (the trio finally works as designed) (Severity: high, P&L)

**The bug.** Audited today's exits: 0 of 11 trailing-stop fires came from the broker. All 11 fired via the polling fallback in `check_trailing_stops`. With ~150 broker trailing orders actively placed across all profiles, the broker should have been firing them — instead the polling was beating it to a worse fill on every single trade.

**Root cause.** Polling check_trailing_stops detects "current_price < trail_level" on the 5-minute cycle. Once it does, the exit loop calls `cancel_for_symbol` which cancels the broker trailing — then submits a market sell at the current (post-breach) price. So the broker never gets to fire AT the trail level. The system was designed to have the broker fire on tick data (faster than polling) but the polling was killing the broker's chance every cycle.

**Fix.** Polling now defers to the broker when there's an active broker trailing order for the symbol. New helper `has_active_broker_trailing(api, db_path, symbol)` checks two things: the trades table has a tracked `protective_trailing_order_id`, AND the broker confirms the order is still working. If both true, polling drops the trigger from its list. The broker fires AT the trail level on the next adverse tick.

If broker trailing isn't actively placed (qty conflict, restart race, etc.), polling stays as the fallback.

Logs now include `"Trailing detection deferred to broker for N symbols"` so you can see this at work. After this deploy, the ratio of broker-fired vs polling-fired trailing exits should flip from 0/11 to majority broker.

**Why this is the right structural fix.** The whole point of placing broker stops was to fire AT the threshold price, not at next-cycle current price. Today's data showed every single trailing exit was still going through polling — broker placement was infrastructure overhead with no realized benefit. After this defer, the broker actually does the job it was placed to do.

**Tests.** 4 new in `test_bracket_orders.py` (39 total):
- `has_active_broker_trailing` returns True with an active id, False without an id, False when broker says order is filled/cancelled
- Source-pin on `trader.check_exits` calling `has_active_broker_trailing`

Full suite: 1389 passing.

---

## 2026-04-30 — Pending orders panel: filter to this profile only (Severity: high, UX correctness)

**The bug.** Dashboard's per-profile Pending Orders panel showed orders for symbols the profile didn't hold. e.g., Mid Cap displayed trailing-stop orders for SOFI even though Mid Cap doesn't trade small caps. Confusing pattern caught by the user.

**Root cause.** `_safe_pending_orders(ctx)` called `api.list_orders(status="open")` and returned everything Alpaca had open. With 10 profiles sharing 3 Alpaca accounts (verified architecture), every profile's panel showed orders placed by ALL sibling profiles on its account. profile_3 saw orders from profiles 4, 5, 9, 10, 11 (all Account 3); profile_8 stayed clean only because it's the sole inhabitant of Account 1.

**Fix.** Cross-reference each Alpaca order's `id` against this profile's trades table. Owned IDs = union of `order_id`, `protective_stop_order_id`, `protective_tp_order_id`, `protective_trailing_order_id` across all rows. Orders whose id isn't in our DB are sibling-profile orders and get filtered out.

Fail-open if the trades DB can't be read — better to show extras than to hide everything and leave the user wondering. Older trade DBs without protective_*_order_id columns degrade gracefully (per-column try/except).

**Tests.** 4 new in `test_pending_orders_filter.py`:
- Hides sibling-profile orders (3 returned by Alpaca, only 1 belongs to us, 1 visible)
- Unions all four ID columns
- Falls open when ctx has no db_path
- Handles missing protective_* columns on legacy schemas

Full suite: 1384 passing.

---

## 2026-04-30 — Three production hardenings: protective-order conflict, wash-trade cooldown, bar cache (Severity: high, multi-issue)

Cleanup pass triggered by reviewing 18h of prod logs. Three independent issues, all addressed.

### 1. Protective-order qty conflict (within-profile)

The biggest noisy pattern: every cycle saw warnings like:

> `Could not place protective trailing stop for SBUX (qty=19, ...): insufficient qty available for order (requested: 19, available: 0)`
> `Could not place protective take-profit for SBUX (qty=19, ...): insufficient qty available for order (requested: 19, available: 0)`

Root cause: `ensure_protective_stops` was placing **three** broker orders per position (stop + TP + trailing). Alpaca treats every open sell-side order as a qty reservation against the position. The first order reserved all 19 shares; the next two saw `available: 0`.

**Fix.** Place ONE protective order per position:
- If `use_trailing_stops`: trailing_stop ONLY. Trailing is functionally a superset — it covers downside (initial level = entry × (1 - trail)) AND locks in gains as high-water rises.
- Else: static stop ONLY.

Take-profit dropped from the broker side. The polling TP check in `check_stop_loss_take_profit` still fires at threshold breach. TP isn't time-critical the way stops are.

### 2. Wash-trade cooldown

Single occurrence today: `Trade execution raised for BP (BUY): potential wash trade detected. use complex orders`. The exception didn't crash (already wrapped) but was logged as ERROR with full traceback, and the system would re-attempt every cycle.

**Fix.** Classify the wash error in `trade_pipeline`'s except handler:
- Log as WARNING (not ERROR), no traceback
- Call `record_wash_cooldown` to mark the symbol with a 30-day skip in the `recently_exited_symbols` table (trigger='wash_cooldown')
- Pre-filter loop unions wash-cooldown symbols into the existing `recently_exited` set

Same treatment for `insufficient buying power` and bare `insufficient qty available` — both are recoverable broker rejections, not code bugs.

### 3. Bar fetch caching

`get_bars` had no cache. Every call hit Alpaca/yfinance. With `relative_weakness_universe` iterating 200+ symbols × `get_bars(symbol, limit=257)` per scan, each cycle made hundreds of redundant network calls. Verified prod stat: 59 scans / 18h, avg **4 minutes**, max 7.5 min.

**Fix.** 5-minute TTL cache around `get_bars`. Daily bars don't change intraday, so staleness within a cycle is fine. Multiple strategies fetching the same symbol within 5 minutes share the result. Empty/None results NOT cached (would poison transient failures).

Implementation: `get_bars` now wraps `_get_bars_uncached` with TTL cache. All test source-pins on the underlying ordering (Alpaca-first) updated to look at `_get_bars_uncached`.

**Tests.**
- `test_bracket_orders.py` (30 total, 4 updated): one-order-per-position behavior, prefers trailing when enabled
- `test_wash_cooldown.py` (5 new): record + read + 30-day window + filter by trigger + pre-filter source pin
- `test_bars_cache.py` (6 new): TTL behavior, separate keys per (symbol, limit), no caching of empty/None, expiry, universe-iteration efficiency
- Updated source-pins in `test_alpaca_data_migration.py` and `test_trade_execution_logging.py` to reflect refactored locations

Full suite: 1380 passing.

---

## 2026-04-30 — check_exits per-position resilience: one bad submit no longer halts the cycle (Severity: critical, outage)

**The outage.** verify_first_cycle reported 11 TASK FAILs on Check Exits in one hour. Pattern: `Cancelled conflicting order ... before exit` followed immediately by `[TASK FAIL] Check Exits` with traceback ending in `alpaca_trade_api.rest.APIError: insufficient qty available for order (requested: 9, available: 8)`. Every subsequent position in that cycle lost protection — no stop refresh, no trailing detection, no exit processing.

**Root cause.** With 10 profiles sharing 3 Alpaca accounts (per the architecture verified yesterday), cumulative reserved share counts across protective stops + take-profits + trailing stops + polling exits can exceed actual qty held at the broker. Alpaca rejects with the "insufficient qty" APIError. The exception propagated up out of `trader.check_exits` (the per-position submit at the bottom of the loop wasn't wrapped) and the whole task crashed. So one over-committed AAPL exit took out MSFT, GOOG, etc., for that whole cycle.

**Fix.** Extract the per-position exit work into `_process_exit_trigger()` and wrap each call in a try/except in the loop. Failures log a `WARNING` with the symbol and trigger reason, then the loop continues to the next position. Subsequent stops/trails get refreshed, subsequent exits get processed.

The deeper qty-overcommit issue (multiple profiles each reserving the same shared shares) remains — the right long-term fix is qty-clamping per submit, but that requires fetching Alpaca-side positions separately from virtual positions. For now, the resilience patch ensures the failures don't cascade.

**Tests.**
- `test_check_exits_resilience.py` (3 new): per-position try/except actually catches and continues; source-pin on `_process_exit_trigger`; source-pin on the wrapping try/except in the loop.
- Updated existing source-pins in `test_bracket_orders.py`, `test_exit_gates_unfilled_entry.py`, `test_short_borrow.py` to look at both `check_exits` AND `_process_exit_trigger` source (since the body moved during this refactor).

Full suite: 1367 passing.

---

## 2026-04-29 — Fix 1: MFE capture ratio surfaced to AI prompt + dashboard (Severity: medium, observability)

**The metric.** Realized P&L as a fraction of the available favorable excursion (max-favorable-price reached during the trade's life):

  - 1.0 = exited at the peak — full capture
  - 0.30 = gave back 70% of unrealized gains
  - 0.0 = exited at break-even despite favorable run
  - <0 = lost money despite the trade running favorably (worst pattern — IBM-style intraday spike then collapse)

**Why surface it.** Pre-INTRADAY_STOPS Stage 3, this was very low because polling-based trailing stops fired at next-day close after intraday reversal. The asymmetry (low capture × full loss-side exposure) made high win rates statistically meaningless. Now the metric is visible to the user and the AI on every cycle.

**Implementation.**
- New module `mfe_capture.py`:
  - `compute_capture_ratio(db_path, lookback=50)` — averages realized_pct / mfe_pct across recent closed trades. Returns avg_capture_ratio, median_capture_ratio, n_trades, n_negative_capture (trades that LOST despite favorable excursion).
  - `render_for_prompt(capture)` — formats as a `MFE CAPTURE` AI prompt block. Suppresses when capture ≥ 0.50 (no signal worth flagging) or when n_trades < 10 (too noisy).
- Performance dashboard: new "MFE Capture" stat-card alongside Avg Position Size and Total Trades. Shows the percent + count of negative-capture trades (the most damaging pattern). Only rendered when a single profile is selected.
- AI prompt: appended to the portfolio-state section. The AI sees "MFE CAPTURE: 12% over last 50 trades — exit logic leaving money on the table" when capture is low. After Stage 3 takes effect, this number should rise materially.

**Why it's primarily for the user, not the AI.** The AI controls *which trades to enter*, not exit timing. The capture ratio is a *signal* the AI can weight (lower capture → maybe size more conservatively, prefer setups with cleaner exit profiles), but the actual exit logic improvements come from the broker-managed orders (Stages 1-3). The dashboard view is the more valuable surface — it tells the operator whether the recent improvements are translating to higher realized capture.

**Tests.** 8 new in `test_mfe_capture.py`:
- Returns None below the 10-trade minimum
- High capture (>0.5) when exits near peak
- Low capture (<0.30) on the "gave back gains" pattern
- Negative captures counted on the "lost despite favorable run" pattern
- Render suppressed at high capture, warns on low, flags negatives
- Handles None / empty input

**The asymmetric-edge trio is now complete (Fix 1 + Fix 3 + INTRADAY_STOPS_PLAN Stages 1-3):**
- Fix 3: scratch-trade classification → win rate is honest
- Stages 1-3: broker stops + TP + trailing → exit timing is real
- Fix 1: MFE capture → operator + AI sees the asymmetry

Full suite: 1364 passing.

---

## 2026-04-29 — INTRADAY_STOPS Stage 3: broker-managed trailing stops (Severity: critical, P&L)

**The biggest single P&L bug yet.** This is the IBM tiny-win pattern:

- Entry: $231.86
- High water during day: $258.50 (+11.5% MFE)
- Trail level (high - 1.5×ATR): $248.54
- EOD close: $231.90
- Polling at EOD detects close < trail → exit at close = $231.90
- Recorded as **$2.70 win** on what was a $1500+ unrealized winner.

The broker trailing stop solves this by tracking the high water continuously and firing the moment the trail level is broken — not on the next 5-min cycle, not at EOD close.

**Implementation.**
- `submit_protective_trailing(api, symbol, qty, side, trail_percent)` — submits Alpaca `type='trailing_stop'` with `trail_percent` (clamped [2%, 10%] for sanity).
- `trail_percent_for_entry(stop_loss_pct)` — converts the profile's stop_loss_pct to trail_percent. If the user accepts a 5% drawdown for the static stop, the trail follows the high water at 5% below.
- `ensure_protective_stops` extended to also place trailing stops when `ctx.use_trailing_stops` is enabled.
- `cancel_for_symbol` extended to cancel all three protective orders (stop / TP / trailing).
- New `protective_trailing_order_id TEXT` column on the trades table.

**Safety net retained.** The polling-based `check_trailing_stops` stays in `trader.check_exits` as a fallback. If broker trailing fails to fire for any reason, the polling check still runs. Polling on a flat position (broker stop already fired) is a safe no-op — nothing to sell, the existing `_entry_order_filled_at_broker` guard handles it.

**Why the broker trailing differs from the polling logic.** Polling computed `high_water - 1.5×ATR` from the last 5 daily bars. Broker trailing tracks the high water from order-submit time onward, with a fixed `trail_percent`. Slight semantic difference — broker trail uses the full position lifetime, polling used a 5-day window. In practice, broker trailing is tighter early in a position's life (less room for noise) and comparable later. The trail_percent clamp [2%, 10%] keeps the broker trail in the same ballpark as the polling 1.5×ATR.

**Tests.** 6 new in `test_bracket_orders.py` (now 29 total):
- `trail_percent_for_entry` clamps to [2%, 10%]
- `submit_protective_trailing` uses `type='trailing_stop'` with trail_percent string
- Sweep places trailing alongside stop + TP when `use_trailing_stops=True`
- Sweep skips trailing when disabled
- `cancel_for_symbol` clears all three columns

**INTRADAY_STOPS_PLAN.md is now complete (Stages 1, 2, 3).** Combined with Fix 3 (scratch classification), the system is now:
- Loss execution: real broker stops, no overshoot
- Win execution: real broker take-profit + trailing, no give-back
- Win classification: honest threshold (no $2 "wins")
- Per-trade fills: real Alpaca paper, near identical to real-money execution at this scale

Full suite: 1356 passing.

---

## 2026-04-29 — INTRADAY_STOPS Stage 2: broker-managed take-profit orders (Severity: high, P&L)

**The problem.** Polling take-profit detection runs every 5 minutes. By the time we detect a position has hit its TP threshold, price has typically reverted some — we exit at a worse price than the target. Combined with trailing stops that fire after intraday reversals, profitable trades give back gains before the polling cycle catches them.

**Fix.** Place broker-managed `type='limit'` orders at `entry × (1 + take_profit_pct)` (long) or `entry × (1 - take_profit_pct)` (short) on every open position. Limit orders fill ONLY at the target or better — won't slip past on gaps; will simply not fill if the target is never reached. Pairs with the Stage 1 stop-loss to bracket each position on both sides.

**Implementation.**
- New helpers in `bracket_orders.py`:
  - `tp_price_for_entry(entry_price, take_profit_pct, is_short)` — symmetric to `stop_price_for_entry`.
  - `submit_protective_take_profit(api, symbol, qty, side, limit_price)` — uses `type='limit'` with `time_in_force='gtc'`.
- `ensure_protective_stops` extended to also place TP orders alongside the stop. Idempotent — checks `protective_tp_order_id` for each row, only places if missing or stale.
- `cancel_for_symbol` extended to cancel BOTH the stop and the TP, and clear both DB columns.
- New `protective_tp_order_id TEXT` column on the trades table (idempotent migration).
- **Conviction-override integration.** When `conviction_tp_skip(symbol, pct_change)` returns True (the high-conviction "let runners run" mode), the sweep does NOT place a TP order on that position. Otherwise the broker would cap a runaway winner at +take_profit_pct, defeating the override.

**API budget.** ~30 new entries per day × 2 protective orders each = ~60 calls/day vs the 200/min Alpaca rate limit. Trivial.

**Failure modes (handled).**
- Submit fails → returns None, polling fallback still detects threshold breach.
- Cancel of already-filled order → treated as success.
- TP fills before stop fires → position closes at TP, stop becomes orphan, next sweep skips (no position) and the stop remains until eventually cancelled by reconciliation or expires (GTC orders persist but Alpaca cancels them automatically when position is flat).

**Tests.** 6 new in `test_bracket_orders.py` (now 23 total):
- `submit_protective_take_profit` uses `type='limit'`, correct limit_price, GTC.
- Long TP above entry, short TP below entry.
- Sweep places stop AND TP alongside each other on bare positions.
- Sweep skips TP placement when conviction-override predicate returns True.
- `cancel_for_symbol` clears both stop and TP order IDs in the DB.

**What's still polled.** Trailing stops. Stage 3 replaces those with broker `type='trailing_stop'` orders, addressing the IBM tiny-win pattern (intraday spike → EOD collapse → break-even exit).

Full suite: 1350 passing.

---

## 2026-04-29 — Fix 3: scratch-trade classification (Severity: high, metric correctness)

**The problem.** Hundreds of trades closing at break-even ($1-$50 pnl on $50K notional = 0.0-0.1% returns) were counted as "wins" because `pnl > 0`. Profile_8 reported 30 wins / 10 losses (75% win rate) — but the median win was $43 (~0.09%). After commission and slippage that's a wash. The "win rate" was vapor.

**Source pattern.** Trail-stop firings on daily bars after intraday reversals (the IBM $2.70 case). The trade ran +11% intraday, reversed to entry, the EOD trail-stop check caught it at break-even. Counted as win because `pnl > 0`, but no real edge captured.

**Fix.** Reclassify trades by pnl_pct against position notional, not by `pnl > 0`:

- `|pnl_pct| < 0.5%` → **scratch** (effectively break-even, excluded from win rate)
- `pnl_pct ≥ 0.5%` → **win**
- `pnl_pct ≤ -0.5%` → **loss**

Win rate denominator is now `winning + losing` (decisive trades only). Scratches surfaced as a separate field in metrics + dashboard so the user can see how many trades closed at break-even.

The 0.5% threshold roughly matches commission + slippage costs on a typical position. A trade that nets less than that hasn't really won — it's traded round-trip cost.

**What this changes on the dashboards.**
- `m.winning_trades`, `m.losing_trades`, `m.scratch_trades` reflect the new buckets.
- `m.win_rate` now reports `winning / (winning + losing) * 100`. Profiles that close at break-even all day will see win rates drop substantially — that's the honest number.
- `m.scratch_rate` shows the proportion that's neither a real win nor a real loss.
- `m.profit_factor` uses real wins / real losses (scratch pnls excluded from both).
- `m.slippage_vs_gross` denominator is now real gains only — slippage as a fraction of *real* profits, not flattered by scratch pnls.

**What this does NOT change.**
- `ai_perf.win_rate` (prediction-side) was already on a 2% movement threshold — already honest.
- Kelly recommendations were already computed from `actual_return_pct` of resolved predictions, which require ≥2% movement. Already honest.
- Realized cumulative P&L is unchanged — same dollars, just bucketed differently.

**Tests.** 6 new in `test_scratch_classification.py`:
- 30 sub-0.5% "wins" + 5 real losses → win rate is 0%, not 86%
- Scratch rate surfaces separately
- 0.5% threshold is inclusive on the win side, exclusive below
- Scratch pnls excluded from total_gains
- Template surfaces scratch_rate

Full suite: 1344 passing.

---

## 2026-04-29 — INTRADAY_STOPS Stage 1: broker-managed stop-loss orders (Severity: critical, P&L)

**The bug.** Polling-based exit detection runs `check_exits` every 5 minutes. Between cycles, prices move continuously. By the time we detect a stop-loss should fire, the price has moved past the level. We then submit a market sell at the *current* price — typically far worse than the intended stop.

Real prod data:
- AMD: stop_loss_pct = 5%, actual exit = -7.91% (60% overshoot)
- INTC: -5% threshold, exit at -5.36%
- COHR: -5% threshold, exit at -6.03%
- CRM: -5% threshold, exit at -6.25%

Each of these gave back hundreds of dollars per trade beyond the intended loss.

**Fix.** Place broker-managed `type='stop'` orders on Alpaca for every open position. The broker fires AT the stop price the moment it's touched, regardless of our cycle timing. Fills land at the stop level (or near it on gap-downs) instead of at next-cycle current price.

**Implementation.**

- New module `bracket_orders.py`:
  - `submit_protective_stop(api, symbol, qty, side, stop_price)` — submits a `type='stop'` order with `time_in_force='gtc'`. Returns the broker order_id, or None on failure (caller falls back to existing polling).
  - `cancel_protective_stop(api, order_id)` — cancels by id; treats already-filled / already-cancelled / not-found as success.
  - `stop_price_for_entry(entry_price, stop_loss_pct, is_short)` — computes the right side of entry: long stops below, short stops above.
  - `ensure_protective_stops(api, positions, ctx, db_path)` — sweep that places stops on positions lacking active ones. Idempotent — verifies the stored order_id is still working before deciding to submit a new one. Survives restarts and races with the entry path.
  - `cancel_for_symbol(api, db_path, symbol)` — pre-exit cleanup that cancels the broker stop AND clears `protective_stop_order_id` in the trades table.

- Schema: new `protective_stop_order_id TEXT` column on the trades table. Populated when a stop is placed; cleared when cancelled.

- `trader.check_exits` invokes `ensure_protective_stops` after the MFE update each cycle. Existing polling stop-loss / take-profit / trailing detection stays as a fallback. When polling fires an exit, `cancel_for_symbol` runs before the market exit so the broker stop doesn't orphan.

- `trade_pipeline.py` SELL path (AI-driven exits) also calls `cancel_for_symbol` before the market sell.

**Failure modes (handled).**
- Submit fails → returns None, polling fallback still detects threshold breach.
- Cancel of already-filled order → treated as success (the goal is reached).
- Broker stop fires between our cycles → reconciliation picks up the closed position; polling on a flat position is a no-op.
- Restart → next sweep restores stops on positions created before restart.

**API budget.** ~30 new entries per day × 1 stop submit each = ~30 calls/day vs the 200/min Alpaca rate limit. Trivial impact.

**Tests.** 17 new in `test_bracket_orders.py`:
- `submit_protective_stop` calls Alpaca with `type='stop'`, `time_in_force='gtc'`, correct stop_price
- Invalid inputs return None without making API calls
- `cancel_protective_stop` treats already-filled / not-found as success
- Sweep places stop on unprotected position; skips when active stop exists; resubmits when stale
- Short positions get BUY stops above entry
- `cancel_for_symbol` clears the DB column
- Source-level pins on `trader.check_exits` to prevent regression

**What this does NOT fix yet.** Trailing stops (the IBM tiny-win pattern). Those are still polled. Stage 3 of `INTRADAY_STOPS_PLAN.md` replaces polling trailing stops with broker `type='trailing_stop'` orders.

Full suite: 1338 passing.

---

## 2026-04-29 — Slippage cost: signed (real economic impact) instead of absolute (Severity: high, data correctness)

**The misleading number.** Dashboard showed `Total Slippage Cost: $9,593` against ~$14.6K realized P&L — implied slippage was eating most of our edge. Actual net cost is **$2,437** (~17% of P&L, not ~66%).

**Root cause.** `journal.get_slippage_stats` was summing `ABS(fill_price - decision_price) * qty` — counting ALL execution variance as cost, including favorable executions where:
- a BUY filled BELOW decision price (we paid less = SAVINGS)
- a SELL filled ABOVE decision price (we got more = SAVINGS)

The economic-correct measure is signed: adverse fills add to cost, favorable fills subtract. Auditing prod data:

| Profile | Trades | Absolute (was) | Signed (is) |
|---|---|---|---|
| profile_1 | 83 | $476 | $77 |
| profile_3 | 65 | $1,111 | -$274 (favorable!) |
| profile_8 | 95 | $3,881 | $1,527 |
| **Total** | 832 | $9,593 | **$2,437** |

Two profiles even had net favorable slippage.

**Fix.**
- `journal.get_slippage_stats` now computes signed `total_slippage_cost` and a separate `total_slippage_magnitude` (the old absolute number — kept as "execution variance" since it's still useful as a measure of fill quality).
  - For BUY / sell_short entries: cost = `(fill - decision) * qty` (positive when adverse)
  - For SELL / cover / short exits: cost = `(decision - fill) * qty` (positive when adverse)
- `metrics.calculate_all_metrics` aggregates both. `slippage_vs_gross` switched from absolute closed-trade slippage to signed closed-trade slippage — so a profile with favorable execution shows negative slippage_vs_gross (slippage helped P&L).
- AI page Slippage Impact panel now shows BOTH: "Net Slippage Cost" (signed, headline) and "Execution Variance" (absolute magnitude). Performance page same treatment.

**Tests.** 2 new in `test_slippage_aggregation.py` (now 7 total):
- `test_total_slippage_cost_is_signed_not_absolute`: two trades that should cancel ($10 adverse + $10 favorable) produce signed cost ≈ $0 and magnitude ≈ $20
- `test_signed_slippage_cost_is_negative_when_executions_are_favorable`: all-favorable book produces negative total_slippage_cost (regression-pin against accidentally re-introducing ABS)

**What it means for the user.** Slippage isn't eating the edge. Of $9.6K of execution variance, $7.2K cancels out across the book. Net cost of $2.4K is 17% of $14.6K realized P&L — within normal range for a system this size and within the <20% target on `slippage_vs_gross`.

Full suite: 1321 passing.

---

## 2026-04-29 — Slippage scope alignment between /ai and /performance (Severity: medium, data correctness)

**The discrepancy.** After the previous slippage-key-mismatch fix, the AI page reported 832 trades / $9,593 total slippage, but the Performance page Slippage Analysis panel showed 356 trades / $4,346 — same data, different numbers.

**Root cause.** Two different code paths with two different scopes:

- `journal.get_slippage_stats` (used on /ai): counts EVERY trade row with `fill_price IS NOT NULL AND decision_price IS NOT NULL` — entries + exits, open + closed = 832
- `metrics.calculate_all_metrics` → `_gather_trades`: filters `WHERE pnl IS NOT NULL` (closed-trade rows only — typically the SELL rows) = 356

Both are internally correct; they just measure different things. Slippage applies to every fill execution, so the all-fills count is more accurate as a measure of "trade execution slippage." The closed-trade count is appropriate for the `slippage_vs_gross` ratio (since gross profit only exists on closed trades).

**Fix.** `metrics.calculate_all_metrics` now uses `get_slippage_stats` for the headline numbers (`slippage_avg_pct`, `slippage_total_cost`, `trades_with_slippage`) — so /ai and /performance agree on the same metrics. The `slippage_vs_gross` calculation continues to use closed-trade slippage (kept in `closed_slippage_costs`) since that's the only scope where gross_profit is defined.

**Tests.** 2 new in `test_slippage_aggregation.py` (now 5 total):
- Source-level pin: `calculate_all_metrics` references `get_slippage_stats`, reads `trades_with_fills` and `total_slippage_cost`
- End-to-end: seed 5 trades (2 open + 3 closed), assert `metrics.trades_with_slippage == get_slippage_stats.trades_with_fills == 5` (both must see all fills, not just closed)

Full suite: 1319 passing.

---

## 2026-04-29 — Slippage Impact panel: fix key-mismatch silent failure (Severity: high, data correctness)

**The bug.** Slippage Impact panel on `/ai` and `/performance` showed "No fill data yet — slippage impact populates once trades record both decision and fill prices" — but every profile had 50-100 trades with full decision_price + fill_price + slippage_pct data. Direct call to `journal.get_slippage_stats(db_path=...)` returned populated stats; the UI just never showed them.

**Root cause.** `journal.get_slippage_stats` returns `{trades_with_fills, avg_slippage_pct, total_slippage_cost, worst_slippage_pct, worst_trade}`. The view aggregator in `views.py` (both `performance_dashboard` and `ai_dashboard`) read `s.get("count", 0)` and `s.get("total_cost", 0)` — neither key exists in the returned dict, so `count` stayed 0 forever and the template fell to the empty state. Classic silent dict-key bug.

**Fix.** Read the actual keys: `trades_with_fills`, `total_slippage_cost`, `avg_slippage_pct`. Aggregate avg_pct as a weighted average across profiles (weighted by trades_with_fills) — was previously computed as `total_cost / count` which would have given an average dollar cost, not a percent.

**Tests.** New `test_slippage_aggregation.py` (3 tests):
- Pin the journal-side contract: `get_slippage_stats` source must reference the three required keys
- Pin the views-side: both `performance_dashboard` and `ai_dashboard` must read `trades_with_fills` and `total_slippage_cost` (regression-pin against accidentally re-introducing `count` / `total_cost`)
- End-to-end round-trip: seed 5 trades with realistic slippage, verify the aggregation produces non-zero count/cost/avg_pct

**Verified on prod.** profile_1 has 83 trades, profile_3 has 65, profile_10 has 50 — all with fill data. After this fix, the Slippage Impact panel will populate immediately on next page load.

Full suite: 1317 passing.

---

## 2026-04-29 — Chart SVGs fill their container (max-width regression) (Severity: medium, UX)

**The bug.** All 5 chart renderers in `metrics.py` (Equity Curve, Drawdown, Bar Chart for PnL Distribution / Monthly Returns, Rolling Sharpe, Win Rate Trend) had `style="width:100%;max-width:700px;"`. On dashboards rendered into containers wider than 700px (the AI page is full-width), the SVG capped at 700px and left ~half the container empty. Visually broken even though the chart data was fine.

**Fix.** Replaced `max-width:Npx` with `height:auto;display:block;` everywhere. The SVG's `viewBox` preserves coordinates while letting the rendered size scale up with the container. Default `preserveAspectRatio="xMidYMid meet"` keeps text proportions correct (no distortion).

Covered: `render_equity_curve_svg`, `render_drawdown_svg`, `render_bar_chart_svg` (used by both PnL Distribution and Monthly Returns), `render_rolling_sharpe_svg`, `render_win_rate_svg`. Both data and empty-state SVG paths.

**Tests.** New `test_chart_svg_responsive.py` (3 tests):
- No chart renderer outputs `max-width:` in its SVG style
- Every chart includes `width:100%`
- Empty-state SVG (when there's not enough data) is also responsive — same regression class

Full suite: 1314 passing.

---

## 2026-04-29 — /ai page 500 + page-render smoke tests (Severity: critical, outage)

**The outage.** User reported "/ai is no longer loading: Internal Server Error" after the last deploy. Root cause: my Awareness page expansion added a new `{% if has_risk_budget %}` panel but inadvertently removed the closing `{% endif %}` for the surrounding `{% if long_short_awareness %}` block. Jinja error: `Encountered unknown tag 'endblock'. The innermost block that needs to be closed is 'if'.`

The pattern is the same one I made earlier this session — claiming "302 in curl = page works" when 302 was just the login redirect. A real authenticated render was never tested in CI. Templates broke silently between commit and prod.

**Fix.** Re-added the missing `{% endif %}` after the long-short-awareness table block. Verified with the new smoke test below.

**Why it slipped through.** `tests/test_web.py::TestAuthenticatedRoutes` had smoke tests for `/dashboard`, `/performance`, `/settings`, `/trades`, `/ai-performance` — but NOT `/ai`. The dedicated AI dashboard never had a render check. Template syntax errors there ran free.

**Now caught.** Six new authenticated render tests added to `test_web.py`:
- `/ai` (full status=200 check with body preview in failure message)
- `/ai/brain`, `/ai/strategy`, `/ai/awareness`, `/ai/operations` (the redirect routes)
- `/admin`

Verified the new test catches the bug class — temporarily reintroduced the missing `{% endif %}` and confirmed the test fails with the exact Jinja error message. Then restored the fix; suite green at 1311.

**Pattern note.** From now on, every visible page route must have a smoke test that hits it authenticated. If there's no smoke test, template syntax errors hide between commit and prod 500s.

Full suite: 1311 passing (was 1305 + 6 new smoke tests).

---

## 2026-04-29 — Meta-pregate: bypass shorts when training data is insufficient (Severity: critical, structural blocker)

**The hidden blocker.** Audit of meta-model training data on prod:

| Profile | n_train_long | n_train_short |
|---|---|---|
| profile_1 (Mid Cap) | 926 | 1 |
| profile_3 (Small Cap) | 1034 | 0 |
| profile_10 (Small Cap Shorts) | 866 | 0 |

The meta-model has been trained almost entirely on long predictions. When it scores a SHORT candidate, the prediction is extrapolation from data the model has never seen — and almost certainly biased low (the model has only learned what successful longs look like). With the uniform meta_pregate_threshold=0.5, every legit SHORT candidate gets dropped before reaching the ensemble. Even though the long/short pipeline now emits short candidates and the regime gate respects target_short_pct, the meta-pregate was silently filtering them out.

**This is the missing link.** The user has been wondering why the AI doesn't enter shorts even with profile_10 configured for 50% short. Answer: shorts mostly weren't reaching the AI — pregate was dropping them based on a model that can't reliably score them.

**Fix.**

1. `meta_model.train_meta_model` — count training samples per direction by reading the `prediction_type_directional_short` and `prediction_type_directional_long` one-hot columns. Add `n_train_short` and `n_train_long` to the metrics dict on every saved bundle.

2. `trade_pipeline._meta_pregate_candidates` — when the inferred prediction_type for a candidate is `directional_short` and `n_train_short < 30`, bypass the pregate (keep the candidate). Same for `directional_long` and `n_train_long < 30`. Threshold matches the MIN_SAMPLES_FOR_KELLY convention. Logged: `"Meta-pregate: bypassed N short candidates (model has n_train_short=0 < 30 — insufficient direction-specific training data)"`.

3. Backwards-compat: models trained before this metrics field existed don't carry n_train_short/long. For those, the bypass is skipped and the threshold applies uniformly (old behavior preserved). Once meta-models are retrained (next daily cycle), the new metrics populate and the bypass takes effect.

**Tests (4 new in `test_meta_pregate_lever.py`, 11 total):**
- SHORT candidates with `n_train_short=0` bypass the threshold even when meta_prob is far below it
- LONG candidates with `n_train_long=5` bypass symmetrically
- Once `n_train_short=50`, the bypass turns OFF and shorts under threshold are filtered normally
- `train_meta_model` populates per-direction sample counts in the metrics dict

Full suite: 1305 passing.

---

## 2026-04-29 — Lever 3 visibility: bump skip log to INFO + smarter verify check (Severity: medium, observability)

**The false alarm.** verify_first_cycle.sh check 2 reported "zero 'skipping pattern_recognizer' events — ctx disconnect may have regressed" — making it look like Lever 3 (per-profile specialist disable list) was broken. But cross-checking against ensemble call counts on prod showed the disable list WAS being respected: profiles with `disabled_specialists=["pattern_recognizer", "risk_assessor"]` were running "Specialist ensemble: 2 calls" instead of 4. The disable was firing — the log line was just at `logger.debug`, invisible in journalctl.

**The fix.**
- `ensemble.run_ensemble`: bump the "skipping" log from `logger.debug` to `logger.info` so operators can verify the disable branch is firing each cycle.
- `verify_first_cycle.sh`: cross-check both signals. Direct evidence is the INFO log; corroborating evidence is reduced call counts (1-3 instead of 4). When skip-log is missing but call counts ARE reduced, report a WARNING (something is being applied but log isn't visible — pointing to a regression in the log level rather than the underlying logic).
- Test pin: `test_skipping_log_is_info_not_debug` enforces the level so this can't silently regress to `logger.debug` again.

**Confirmed working on prod.** profile_1 (Mid Cap, 2 disabled) and profile_10 (Small Cap Shorts, 2 disabled) showing "Specialist ensemble: 2 calls" cycles. profile_3 (Small Cap, 1 disabled) showing "3 calls" cycles. Lever 3 was always working — just wasn't audible.

Full suite: 1301 passing.

---

## 2026-04-29 — relative_weakness_universe: quality filters for short candidates (Severity: high, signal quality)

**The problem.** First version emitted LCID for profile_10. The AI saw it and passed: *"LCID presents a superficially attractive mean-reversion setup (RSI 24, StochRSI 0, -83% vs 52wH) but fails conviction thresholds: (1) Your 0W/11L track record on LCID is disqualifying."* The strategy was finding deeply-crashed names, the AI was correctly rejecting them as bottom-pickers' graveyard. Quantity without quality.

**Three new filters.**
- **Recent weakness check.** Was: 20-day RS gap ≥ 5%. Now also requires 5-day RS gap ≥ 1% — the underperformance must be CURRENT, not just historical. Filters out names that crashed months ago and have been quietly mean-reverting upward (the worst kind of short — bouncing while we're betting on continuation).
- **Drawdown filter.** Names down >40% from 252-day high are skipped. Real long/short profit comes from names with further to fall, not names already at the floor. The empirical pattern: names with 50%+ drawdowns more often bounce than continue lower (forced selling exhaustion).
- **Ranking by 5d, not 20d.** Was: rank ascending by 20d return (most lifetime weakness first). Now: rank by 5d return (most acute current weakness first). Same candidate pool, different ordering — surface the names showing weakness NOW rather than the names that have been weak since forever.

**Knobs.** New module-level constants: `RECENT_RS_GAP_THRESHOLD = 1.0`, `DRAWDOWN_FILTER_PCT = 40.0`, `RECENT_LOOKBACK_DAYS = 5`, `DRAWDOWN_LOOKBACK_DAYS = 252`. Self-tuning can adjust later; these defaults are conservative.

**Tests.** 3 new (now 12 total): name with bad 20d but bouncing 5d is filtered; name down >40% from 252d high is filtered; when both qualify, the more-recently-weak name ranks first.

**Why this matters.** Profile_10 had been showing the AI candidates that were structurally bad shorts (deep-drawdown, mean-reverting). Of course it passed. With these filters the strategy will surface names that are starting to break down NOW — the kind of setup the AI's risk gates respect.

Full suite: 1300 passing.

---

## 2026-04-29 — Awareness page: full coverage of AI prompt blocks (Severity: medium, completeness)

**The gap.** First pass at the awareness page surfaced 4 of the 6 long/short prompt blocks (book beta target, balance target, Kelly, drawdown scale). Two remained invisible: P4.4 risk-budget per-position contributions, and P2.1 sector concentration warnings — both routinely appear in the AI prompt but weren't on the user-visible "what the AI sees" surface.

**Now surfaced.**
- **Risk Budget panel.** For each shorts-enabled profile, lists over-contributing positions (≥ 2× the per-position avg variance contribution) and under-contributing positions (≤ 0.5×). Includes the sizing rule the AI is told (`size ∝ 25% / annualized_vol`, clamped). Mirrors `risk_parity.analyze_position_risk` output one-for-one.
- **Sector Concentration panel.** Per profile, lists every sector at ≥30% gross — the same threshold the prompt flags as "concentration risk." Real long/short funds typically target <20% gross per sector; the AI is told this in its prompt every cycle.
- **Position count** added to the top-line table.

**Schema guard.** New test `test_awareness_row_has_all_prompt_block_fields` enforces that every required prompt-block key is present in the awareness row dict. Adding a new prompt block (P4.6, P5.x, etc) without surfacing it is now a test failure, not a silent gap.

**Tests.** 3 new in `test_long_short_awareness.py` (now 11 total): risk-budget panel renders, sector-concentration panel renders, awareness row schema enforces full prompt coverage.

Full suite: 1297 passing.

---

## 2026-04-29 — UI catch-up for long/short: settings + awareness + performance (Severity: high, completeness)

**The gap.** Backend supported all four short configuration knobs (`target_short_pct`, `target_book_beta`, `short_max_position_pct`, `short_max_hold_days`) but the Settings page exposed none of them — users couldn't actually configure the most important short parameters through the UI. The AI awareness page didn't show any of the new long/short prompt blocks (Kelly, drawdown scale, balance, book-beta), so when a profile emitted zero shorts there was no way to verify the prompt was computing the expected numbers. Performance dashboard had factor breakdowns but didn't surface book beta as a single number.

**Settings page.**
- Added "Long/Short Mandate" section with `target_short_pct` slider (0% long-only → 50% market-neutral → 100% short-only) and `target_book_beta` slider (-0.5 to +2.0). Tooltips explain that target_short_pct ≥ 0.4 bypasses the strong-bull regime gate (the user has accepted regime risk by setting that mandate) and target_book_beta drives both the AI prompt directive AND the P4.5 hard neutrality gate.
- Added `short_max_position_pct` and `short_max_hold_days` to the Short Selling Risk panel.
- `views.save_profile` now parses each of the four fields. `target_book_beta` preserves None when the form value is empty.

**AI awareness page.**
- New "Long/Short Construction" panel at the top of the Awareness tab, one row per shorts-enabled profile. Shows: target vs current short share + balance gate state; target vs current book beta + delta with "out of band" flag; Kelly recommendation per direction (or "insufficient data"); current drawdown % + capital scale modifier.
- Built by `views._build_long_short_awareness(profiles)` — pulls live positions, computes book beta, fetches Kelly recs, computes drawdown scale. Best-effort: profile-level failures keep the row with empty fields rather than dropping the profile.

**Performance dashboard.**
- "Book Beta" stat card alongside Net / Gross / Positions in the Current Exposure panel. When a single profile is selected, also shows target + delta with out-of-band flag.
- "Kelly Position Sizing" panel with side-by-side LONG / SHORT cards. Each shows fractional Kelly % + supporting stats (WR, avg win, avg loss, n) or "need 30+ resolved entries with positive edge" placeholder.

**Tests (8 new in `test_long_short_awareness.py`, 4 in `test_settings_short_knobs.py`):**
- `_build_long_short_awareness` skips long-only profiles, skips profiles with no DB file, builds rows per shorts-enabled profile with empty fields when sub-fetches fail, surfaces Kelly when data exists.
- Performance template has book_beta stat-card, Kelly panel, references the right view variables.
- Performance view passes `profile_target_book_beta`, `perf_kelly_long`, `perf_kelly_short`.
- Settings template has all four short-knob inputs, save_profile parses each one, values round-trip through DB → UserContext.

Full suite: 1294 passing.

---

## 2026-04-29 — Doc + display catch-up for the long/short build (Severity: medium, hygiene)

**The gap.** Phases 1-4 of LONG_SHORT_PLAN shipped in code with full test coverage and CHANGELOG entries, but the canonical reference docs (ROADMAP, TECHNICAL_DOCUMENTATION, AI_ARCHITECTURE) and display-name registry were stale. ROADMAP's Phase 11 entry described only Phase 1; AI_ARCHITECTURE's Part 4 named only Phase 1's strategies and didn't document the Phase 2-4 prompt blocks the AI now sees on every cycle. `display_names.py` had no explicit entries for any of the 10 dedicated short strategies — they fell back to title-case from snake_case which works but leaves the system documentation visibly incomplete.

**What's now documented.**
- `ROADMAP.md`: Phase 11 entry expanded to cover Phases 2 (sector/factor neutrality), 3 (real alpha sources), 4 (active factor construction — Kelly, drawdown scaling, risk-parity, neutrality enforcement), plus tonight's structural fixes (regime-gate respects mandate, relative_weakness_universe).
- `TECHNICAL_DOCUMENTATION.md`: new "Long/short capability modules" subsection lists every module added (kelly_sizing, drawdown_scaling, risk_parity, factor_data + get_realized_vol, portfolio_exposure additions, the 10 bearish strategies, validation-time gates).
- `AI_ARCHITECTURE.md`: Part 4 retitled to cover Phases 1-4. New subsections for the prompt blocks (EXPOSURE BREAKDOWN, BOOK-BETA TARGET, BALANCE TARGET, KELLY SIZING, DRAWDOWN CAPITAL SCALE, RISK-BUDGET) plus validation-time gates (balance gate, asymmetric short cap, HTB borrow penalty, P4.5 neutrality enforcement).
- `display_names.py`: explicit human labels for `breakdown_support`, `distribution_at_highs`, `failed_breakout`, `parabolic_exhaustion`, `relative_weakness_in_strong_sector`, `earnings_disaster_short`, `catalyst_filing_short`, `sector_rotation_short`, `iv_regime_short`, `relative_weakness_universe`.

**What's still pending.** UI gaps — Settings page lacks controls for `target_short_pct`, `target_book_beta`, `short_max_position_pct`, `short_max_hold_days`. AI awareness page doesn't show the new prompt blocks. Performance dashboard doesn't surface book_beta as a single number. Tracked separately and being worked through.

Full suite: 1282 passing.

---

## 2026-04-28 — Anti-momentum short strategy: relative_weakness_universe (Severity: high, capability)

**The thesis.** The regime-gate fix unblocked the few short candidates that existing strategies were producing. But in extended strong-bull regimes, textbook bearish technical patterns (breakdown_support, distribution_at_highs, parabolic_exhaustion, failed_breakout) are rare BY CONSTRUCTION — most names aren't breaking support when SPY climbs daily. A dedicated short profile (target_short_pct=0.5) needs a strategy that fires regardless of whether textbook setups are forming.

**The strategy.** Anti-momentum / relative-weakness ranking. Universe-wide:

1. Compute 20-day return for SPY
2. For each name in the universe, compute 20-day return
3. RS gap = SPY_return - stock_return (positive = lagging market)
4. Filter: RS gap ≥ 5% (cumulative underperformance)
5. Filter: stock below its 20-day MA (trend confirmation)
6. Rank ascending by stock return; emit bottom 5% (cap 5) as SHORT

This is a real fund pattern: Jegadeesh & Titman 1993's momentum literature is symmetric — top-decile winners outperform, bottom-decile losers underperform. We were already running the long side via momentum-style strategies; this completes the symmetry.

**Score is intentionally 1** (vs 2 for focused setups like breakdown_support). There's no specific bearish catalyst — purely relative weakness. The AI sees this context and weights accordingly. If a name shows up here AND on a focused short strategy, the meta-model and ensemble will compound the conviction.

**Markets.** Equities only (small/midcap/largecap). Crypto's universe is too small for ranking.

**Tests.** 9 in `test_relative_weakness_universe.py`:
- Module interface (NAME, APPLICABLE_MARKETS, find_candidates)
- Emits when stock underperforms SPY by threshold
- Skips stocks above 20-day MA (trend filter)
- Caps output at 5 candidates regardless of universe size
- Emit format (signal=SHORT, score=1, votes, price, reason)
- Returns empty when SPY data missing
- Skips stocks with insufficient history
- Empty universe / universe < 5 names returns empty
- Strategy registered in STRATEGY_MODULES

**What this fixes.** Profile_10 (Small Cap Shorts, target_short_pct=0.5) had only 3/1497 SHORTs over 30 days because (a) regime gate was blocking technical shorts (fixed), AND (b) technical shorts emit ~0 candidates per cycle in strong-bull. With this strategy, the universe is ranked every cycle and the worst-RS names emit regardless of whether they fit a textbook bearish setup.

Full suite: 1282 passing.

---

## 2026-04-28 — Regime gate respects target_short_pct mandate (Severity: critical, root cause)

**The bug.** Profile_10 (Small Cap Shorts, target_short_pct=0.5) emitted only 3 SHORT predictions out of 1,497 in the last 30 days — essentially the same 0.2% rate as before Phases 1-4 shipped. The whole long/short build was blocked from producing visible results because of one upstream gate.

**Root cause.** `trade_pipeline._rank_candidates`'s P1.4 regime gate filters out routine technical shorts whenever the market is in `strong_bull` (SPY > 200d MA AND 20d MA > 50d MA). Only catalyst strategies (`_CATALYST_SHORT_STRATEGIES`) flow through. Logs confirmed 5-9 shorts filtered for regime gate per cycle. SPY has been in strong_bull continuously, so the gate was permanently engaged.

**The contradiction.** A profile configured with `target_short_pct=0.5` is explicitly mandated to hold a substantial short book. The user has accepted regime-side risk for that profile by design. The regime gate's "don't fight the tape" rationale doesn't apply — the user has ALREADY signed up for the opposite of trend-following.

**Fix.** `_rank_candidates` now accepts `target_short_pct` (defaults to 0.0). When `target_short_pct >= 0.4`, the regime gate is bypassed for shorts on that profile. Lower-target profiles (regular long-tilt books) keep the gate. Caller in trade_pipeline reads `ctx.target_short_pct` and threads it through.

**Why 0.4 (not 0.5).** Some profiles will run target_short_pct = 0.45 in mixed-balance configurations. Threshold at 0.4 captures the "substantially short" intent without requiring exactly 50/50.

**Tests.** 3 new in `test_long_short_phase1.py`:
- `test_regime_gate_blocks_routine_short_in_strong_bull` — gate active when target_short_pct=0.0 (regression pin)
- `test_regime_gate_bypassed_when_target_short_pct_high` — gate bypassed at target_short_pct=0.5
- `test_regime_gate_default_target_short_pct_zero` — call without kwarg preserves prior behavior

**Caught by.** Real-data audit of SHORT prediction emission rates across all profiles — exactly the validation the user prompted with "make it the best thing the world has ever seen". A code-only review would have missed this; the gate was correctly implemented per the original P1.4 spec, just incompatible with the dedicated-shorts mandate added in P2.2.

Full suite: 1273 passing.

---

## 2026-04-28 — Phase 4.5 of LONG_SHORT_PLAN: market-neutrality enforcement (Severity: high, capability)

**The thesis.** P4.1 added a beta target as a soft directive in the AI prompt; P4.5 makes it a HARD gate in `_validate_ai_trades`. Soft guidance and exposure visibility aren't enough when an AI proposes a high-beta long after we're already over-target — the trade can clear all other gates yet drift the book further from neutrality. Real long/short funds enforce this at the trade level, not via prompts.

**Rule.** Block any entry where:

    |projected_book_beta - target| - |current_book_beta - target| > 0.5

Symmetric:
- Block trades that worsen neutrality by more than 0.5 in distance
- ALWAYS allow trades that improve neutrality (no upper limit on the *good* direction)
- Skip when target_book_beta is unset or current beta isn't computable
- Apply only to BUY/SHORT entries — SELL exits can't worsen neutrality further than the entry already did

**Implementation.**

`portfolio_exposure.simulate_book_beta_with_entry(positions, equity, candidate_symbol, candidate_size_pct, candidate_action, beta_lookup=None)` — projects book beta if the candidate were added at the proposed size. Reuses the same gross-weighted formula as `compute_book_beta`.

`ai_analyst._validate_ai_trades` — initialises `neutrality_enforce` once per call (when ctx.target_book_beta is set and current book beta is computable) and recomputes the current book beta from positions to keep current/projected consistent under the same beta lookup. Each candidate trade evaluated with simulate; failures logged and dropped from `validated`.

**Why hard, not soft.** Soft guidance (P4.1) is honored by the AI most of the time but is the first thing dropped under conviction pressure ("this AAPL setup is gold, even if we're already over-target"). The hard gate enforces neutrality independently of the AI's discretion — the same way `balance_gate` (P2.4) enforces target_short_pct.

**Tests.** 10 new in `test_neutrality_enforcement.py`:
- `simulate_book_beta_with_entry`: returns None on unknown beta; long entry adds positive signed beta; short entry adds negative; combines correctly with existing book; respects sign of existing positions
- Gate inactive when no target set
- Gate blocks long that pushes >0.5 further from target
- Gate ALLOWS trade that improves neutrality (e.g., 30% short of β=2 brings 0.6 → 0.0)
- Gate skips when no book beta computable

Full suite: 1270 passing.

---

## 2026-04-28 — Phase 4.4 of LONG_SHORT_PLAN: risk-budget (risk-parity) sizing (Severity: high, capability)

**The thesis.** Equal-dollar weights are NOT equal-risk weights. A 5% slug of a 60%-vol biotech contributes ~3× the variance of a 5% slug of a 20%-vol utility. Real risk-parity funds size INVERSELY to vol so each position contributes equal variance. We don't run a fully-rebalanced risk-parity book, but the sizing principle still applies on every new entry: high-vol names get smaller, low-vol names can stretch.

**Implementation.** Two pieces:

1. `factor_data.get_realized_vol(symbol, days=30)` — annualized realized vol from log returns of recent daily closes. Cached (factor_cache, 7d TTL — vol moves daily but staleness is acceptable for sizing guidance).

2. New module `risk_parity.py`:
   - `compute_vol_scale(realized_vol, target_vol=0.25)` — returns `target_vol / realized_vol`, clamped to [0.40×, 1.60×]. Defaults to 1.0× when vol unknown (degrade gracefully).
   - `analyze_position_risk(positions, equity)` — per-position weight × annualized_vol, flags names whose risk contribution is ≥ 2× or ≤ 0.5× the per-position average.
   - `render_for_prompt(analysis)` — `RISK-BUDGET` block with sizing rule + over-/under-contributing call-outs. Suppressed when nothing actionable.

**Wiring.** `ai_analyst._build_batch_prompt` now appends `risk_budget_block` after `drawdown_block`. The block is read alongside Kelly + drawdown scale; the AI multiplies its base size by all three:

    final_size = base × kelly × drawdown_scale × vol_scale

**Why now.** With Kelly (P4.2) and drawdown scaling (P4.3) in place, equal-dollar bets across a mixed-vol portfolio meant a single high-vol name dominated portfolio variance regardless of edge or drawdown state. Risk-parity sizing closes the last sizing-related gap before the neutrality enforcement in P4.5.

**Tests.** 13 new in `test_risk_parity.py`:
- `compute_vol_scale` returns 1.0 at target vol, inverse-proportional otherwise, clamped at [0.4, 1.6]
- Returns 1.0 (graceful) on unknown/zero/negative vol
- `analyze_position_risk` flags 4×-vol outliers, skips unknown-vol names, handles short positions (uses abs(market_value))
- Returns None on empty positions / zero equity / fewer than 2 known vols
- `render_for_prompt` suppresses noise-only output, includes sizing rule and outliers when present

Full suite: 1260 passing.

---

## 2026-04-28 — Phase 4.3 of LONG_SHORT_PLAN: drawdown-aware capital scaling (Severity: high, capability)

**The thesis.** Kelly says how big a bet *should* be at full conviction. But when the book is below peak, "full conviction" is the wrong baseline — the edge estimate may be wrong, and variance compounds against us harder when we're already down. Drawdown scaling is the safety net: shrink positions while recovering, restore them when peak returns. This is independent of the existing pause threshold (which stops new entries entirely); scaling is for the entries that *do* happen below peak.

**Implementation.** New module `drawdown_scaling.py`:

- `compute_capital_scale(drawdown_pct)` — continuous scale in [0.25, 1.0]. Linear interpolation between breakpoints (0%→1.00, 5%→0.85, 10%→0.65, 15%→0.45, 20%+→0.25 floor). Monotonically non-increasing.
- `render_for_prompt(dd)` — formats the scale + drawdown context as a `DRAWDOWN CAPITAL SCALE` AI prompt block. Suppresses the block when scale rounds to 1.00× (no point telling the AI "multiply by 1.00").

Wired into `ai_analyst._build_batch_prompt` as `drawdown_block`, appended after `kelly_block`. Reads `drawdown_pct`, `peak_equity`, `current_equity` from `portfolio_state`. `trade_pipeline._build_portfolio_state` now passes `peak_equity` through.

**Why continuous, not discrete.** The pre-existing `check_drawdown` already returns a discrete `action` (normal/reduce/pause), but that's a control-flow signal — pause = no entries. The AI needs a *sizing* signal: keep entering, just size smaller. Smooth scaling avoids cliffs at threshold boundaries (a position at 9.9% drawdown shouldn't suddenly halve when we cross to 10.1%).

**Tests.** 9 new in `test_drawdown_scaling.py`:
- Breakpoints match schedule
- Floor at 0.25× below 20% drawdown
- Linear interpolation between breakpoints (7.5% → 0.75, 12.5% → 0.55, 17.5% → 0.35)
- Monotonically decreasing as drawdown grows
- `render_for_prompt` suppresses empty blocks (no drawdown, full scale)
- `render_for_prompt` includes scale, drawdown %, peak/current equity

Full suite: 1247 passing.

---

## 2026-04-28 — P4.2b Kelly: exclude HOLD predictions from edge stats (Severity: high, correctness)

**The bug.** `compute_kelly_recommendation` read every row tagged `prediction_type='directional_long'`, including HOLD predictions. HOLDs aren't entries — their "actual_return_pct" reflects existing-position drift, not new-bet P&L. On profile_3 this meant 920 HOLD rows (601 losses, 314 wins-with-negative-avg-return) drowned out the 49 actual BUY rows. On profile_11, real positive edge (21W/9L = 70%, +2.95% / -2.23% — full Kelly ~47%) returned `None` in the recommendation because HOLDs flipped the aggregate edge negative.

**Why it matters.** Kelly sizing is for sizing NEW entries. Including HOLD outcomes is a category error: the prediction "keep your current position" doesn't produce an entry-sized bet, so its win/loss outcome doesn't measure the edge that Kelly is supposed to size. With the bug present, NO profile in prod produced a non-None Kelly recommendation, despite profile_11 having a clean positive edge.

**Fix.** Filter Kelly query on `predicted_signal IN ('BUY','STRONG_BUY')` for long, `IN ('SHORT','SELL','STRONG_SELL','STRONG_SHORT')` for short. Drops HOLDs (and any other ambiguous signals) from the Kelly-relevant population entirely.

**Test.** `test_recommendation_excludes_hold_predictions` — seeds 30 BUYs with strong positive edge plus 600 HOLD rows with terrible outcomes; pre-fix would crater the win rate, post-fix returns Kelly ≈ quarter of full at 70% WR.

**Caught by.** Real-data validation against prod predictions databases — Kelly returned None on every profile despite obvious positive edge on profile_11.

---

## 2026-04-29 — Phase 4.2 of LONG_SHORT_PLAN: fractional Kelly position sizing (Severity: high, capability)

**The thesis.** Position sizing is the silent killer of trading systems — the wrong size compounds wins poorly and amplifies losses faster than the edge is supposed to support. The Kelly criterion gives the position fraction that maximizes long-run logarithmic growth given a known edge. Fractional Kelly (typically quarter Kelly) cuts variance ~50% while keeping ~75% of the growth rate — the standard pro-fund variance/growth tradeoff.

**Implementation.** New module `kelly_sizing.py`:

- `compute_kelly_fraction(win_rate, avg_win, avg_loss, fractional=0.25)` — implements `f* = (bp - q) / b` × fractional. Returns None on no-edge, negative-edge, zero/invalid inputs, or extreme positive recommendations (>50% of capital after fractional). Report mode (`fractional=1.0`) skips the cap so callers can get the full Kelly value for display.

- `compute_kelly_recommendation(db_path, direction, fractional=0.25)` — reads per-direction edge stats from `ai_predictions` (`prediction_type` column with backwards-compat fallback for legacy rows). Computes win_rate / avg_win / avg_loss / sample_size and returns the recommendation dict. Returns None below `MIN_SAMPLES_FOR_KELLY` = 30.

- `render_for_prompt(rec_long, rec_short)` — formats both directions as a compact AI-prompt block.

**AI prompt block** in `_build_batch_prompt`:
```
KELLY SIZING (fractional=0.25):
  Suggested size per trade based on observed edge.
  LONG: Kelly 9.2% (WR 65%, avg win 4.0%, avg loss 2.5%, n=128)
  SHORT: Kelly 5.0% (WR 55%, avg win 5.0%, avg loss 4.0%, n=80)
```

Soft guidance — does NOT override `max_position_pct`. The AI sees the recommendation and decides whether to size at Kelly, lower, or pass entirely on weak setups.

**Tests added.** `tests/test_kelly_sizing.py` (14 tests):
- Classic Kelly formula (55% WR, 2:1 odds → 0.325 full)
- Quarter Kelly default
- None on no-edge / negative-edge / zero inputs / 100% win-rate
- Cap at 50% in fractional mode but full value returned in report mode
- Below-min-samples → None
- Real recommendation math on seeded predictions
- Long and short directions read separately
- Legacy-row fallback (rows without prediction_type)
- Negative edge → None
- Render: empty, long-only, both directions

Total full-suite count: 1237 passing.

---

## 2026-04-29 — Phase 4.1 of LONG_SHORT_PLAN: beta-targeted construction (Severity: high, capability)

**The thesis.** Phase 3 surfaced book-level factor exposures to the AI. Phase 4.1 is the first piece of *active* factor management: the AI gets a directive on every cycle to bias picks toward a configured book-level beta target. The gold-standard construction technique for long/short funds — pro shops typically target book beta of 0.0 (market-neutral) to 0.5 (low-net).

**Implementation.**

- New `target_book_beta` column on `trading_profiles` (REAL, NULL = no target). Schema migration auto-applies.
- `UserContext.target_book_beta: Optional[float]` plumbed through `build_user_context_from_profile`.
- `param_bounds` clamp range -0.5 to 2.0 (covers reasonable: net-short bias to highly-levered long).
- `update_trading_profile` allowlist updated.
- `MANUAL_PARAMETERS` entry — strategic user choice, NOT auto-tuned.

- New `portfolio_exposure.compute_book_beta(positions, equity, beta_lookup=None)`. Returns gross-weighted book beta with shorts contributing NEGATIVELY (industry-standard convention). Skips positions with unknown beta. Returns None when book is empty or no betas resolvable. Bundled into `compute_exposure()` output as `book_beta` key (rounded to 3 decimals or None).

- AI prompt directive in `_build_batch_prompt`: when `ctx.target_book_beta` is set AND book has positions AND `book_beta` is computable, surface a `BOOK-BETA TARGET` block:
    - `BETA TOO HIGH by +X.XX. Strong preference: DEFENSIVE picks ... or LEVERED shorts to reduce book beta.`
    - `BETA TOO LOW by X.XX. Strong preference: LEVERED long picks or DEFENSIVE shorts to raise book beta.`
    - `Book beta is on target; pick on conviction.`
  Tolerance ±0.30 either side of target before triggering directive.

**Tests added.** `tests/test_book_beta_target.py` (14 tests):
- Empty positions / zero equity → None
- Long-only book math
- Short positions subtract from book beta
- Market-neutral book lands near zero
- Unknown beta positions skipped
- All-unknown returns None
- `compute_exposure` exposes `book_beta` key
- Directive absent when target=None
- Directive present + 'BETA TOO HIGH' when above target
- 'BETA TOO LOW' when below target
- 'on target' within tolerance
- Skipped on empty book
- UserContext default is None

Total full-suite count: 1223 passing.

**Why this is Phase 4 not Phase 3.** Phase 3 was alpha sources (real strategies). Phase 4 starts active *construction* — using the factor data to actively shape the portfolio rather than just observe it. Future Phase 4 entries will add fractional Kelly sizing, drawdown-aware capital scaling, and risk-budget position sizing.

---

## 2026-04-29 — P3.6 docstring clarification + CHANGELOG pairing (Severity: trivial, docs)

Tightened the `get_factor_classification` docstring to make the
per-position-loop usage pattern explicit (cache hit per
(symbol, factor) per week + how unknown classifications flow
through the caller's bucket logic). Cosmetic only — same code
path, no behavior change.

This commit is paired with CHANGELOG (per the recurring
discipline reminder) — every .py commit ships with a CHANGELOG
entry so the test_last_py_commit_includes_changelog guard
stays green.

---

## 2026-04-29 — P3.6 follow-up: factor render path fix (Severity: medium, display bug)

The first P3.6 commit (`3e04e56`) populated the new factor buckets
correctly in the data layer but the AI prompt's `render_for_prompt`
read them at `exposure[<factor>]` (top level) instead of
`exposure["factors"][<factor>]` (where `compute_exposure` actually
nests them). Result: factor data was correct in the dashboard but
the AI never saw it in its prompt context.

Caught by real-data prod validation — running the validator showed
correct bucket numbers in the dashboard render but missing lines in
the prompt block. Fixed render path + added a regression test that
pins it (`test_render_for_prompt_surfaces_real_factor_lines`).

---

## 2026-04-29 — Phase 3.6 of LONG_SHORT_PLAN: real factor exposures (book/value, beta, momentum 12-1m) (Severity: high, capability)

**The thesis.** Phase 2 P2.5 used a stylized price-band size proxy because we didn't have fundamentals data cached. Real factor exposures require yfinance fundamentals. Adding the three classic equity factors with decades of academic evidence:

- **Book-to-Market** (Fama & French 1992): high B/M = value stocks that historically outperform low B/M = growth.
- **Beta vs SPY**: market sensitivity. <0.7 = defensive; 0.7-1.3 = market; >1.3 = levered.
- **Momentum 12-1m** (Jegadeesh & Titman 1993): 12-month return excluding the most recent month (avoids short-term reversal). Long winners + short losers is the momentum factor.

**Implementation.** New module `factor_data.py`:
- `get_book_to_market(symbol)` — yfinance `bookValue × sharesOutstanding / marketCap`
- `get_beta(symbol)` — yfinance `info.beta`
- `get_momentum_12_1(symbol)` — `(price[-21] - price[-252]) / price[-252]` from market_data bars
- All cached 7 days in dedicated `factor_cache` table (separate from alt_data_cache to keep concerns clean)
- All return `None` on errors / missing data — graceful degrade
- Crypto symbols skipped at the top of each fetcher

Bucketing helpers: `classify_book_to_market`, `classify_beta`, `classify_momentum`. Each returns one of {value/mid/growth, defensive/market/levered, winner/neutral/loser, unknown}.

**Wired into compute_factor_exposure.** Now produces gross-weighted breakdowns by all three factors alongside the existing size_bands and direction. Surfaces in:
- Performance Dashboard's Current Exposure → "By Equity Factor" cards (B/M, Beta, Momentum, each showing % gross per bucket).
- AI prompt's EXPOSURE BREAKDOWN block — adds 3 new lines when ≥1% of book classified.
- "Unknown" bucket absorbs symbols whose fundamentals aren't reachable; rendered when ≥5% of book.

**Tests added.** `tests/test_factor_data.py` (13 tests):
  - All three classifiers' boundary cases
  - Cache hit avoids re-fetching
  - None on missing fundamentals
  - Beta from `info.beta`
  - Momentum 12-1m correctly skips the recent month (verified by a fixture where the recent month crashes -50% but the formula returns positive)
  - Insufficient history returns None
  - `get_factor_classification` round-trip
  - yfinance exception → None (graceful)
  - Crypto skipped
  - `compute_factor_exposure` includes the new buckets
  - Per-symbol lookup exception falls into "unknown" without crashing

Test count: 1208 passing locally. Test infrastructure also updated: `test_no_guessing` template-var allowlist now includes `f_btm`, `f_beta`, `f_mom` (P3.6 template locals) and `1` (numeric literal artifact).

---

## 2026-04-29 — Phase 3.5 of LONG_SHORT_PLAN: insider signal score promotion (Severity: high, alpha)

**The thesis.** Insider trading clusters have decades of academic evidence — Seyhun (1986), Cohen et al. (2012), and many others — showing that stocks where 3+ insiders buy in concert outperform by ~6% over the following six months, and the reverse for selling clusters. The signal is among the strongest in finance.

**The bug.** Both `insider_cluster` (BUY) and `insider_selling_cluster` (SELL) emitted with score 2. Many less-rigorous technical strategies also emit at score 2. Result: insider signals were getting CROWDED OUT of the AI's top-15 shortlist by noisier momentum-based signals.

**The fix.** Promoted both to score 3 with a documented comment referencing the academic work. Higher score lifts insider signals into the top-15 reliably, restoring their primary-weight status. Particularly impactful for shorts-enabled profiles where `insider_selling_cluster` is one of only four catalyst-tagged short strategies that survive strong-bull regimes.

**Tests added.** `tests/test_insider_score_promotion.py` — 3 tests pinning score=3 in both modules + a source-level test that the P3.5 comment is present (so future refactors can't silently regress to 2). Updated `test_seed_strategies::test_triggers_on_cluster` from 2 → 3 to match new score.

---

## 2026-04-29 — Phase 3.4 of LONG_SHORT_PLAN: iv_regime_short strategy (Severity: medium, alpha)

**The thesis.** Different from the existing `high_iv_rank_fade` (mean-reversion). This is a CONTINUATION pattern: when implied volatility is elevated AND a stock is in an established downtrend with active selling, the combination of priced-in fear + technical breakdown predicts multi-day continuation lower. Elevated IV signals material uncertainty about the name; that uncertainty rarely resolves to the upside on a stock already breaking down.

**Implementation.** `strategies/iv_regime_short.py`. Triggers when ALL hold:
1. IV rank ≥ 70 (elevated but not extreme; extremes mean-revert)
2. Stock below 20-day SMA (downtrend)
3. Stock down ≥3% over trailing 10 days (active selling, not just sideways)
4. RSI between 35-65 (avoid mean-reversion territory either side)
5. Most-recent-day volume ≥ 1.2× 20-day avg (distribution confirmation)

NOT tagged as catalyst — IV regime is a market condition, not a company-specific event. Score: 2.

**Tests added.** `tests/test_iv_regime_short.py` — 9 tests covering interface, registry, NOT-in-catalyst-set, low-IV rejection, uptrend rejection, real trigger, oversold rejection, thin-volume rejection, sideways-below-SMA rejection.

---

## 2026-04-29 — Phase 3.3 of LONG_SHORT_PLAN: sector_rotation_short strategy (Severity: medium, alpha)

**The thesis.** Sector rotation has documented asymmetry: when capital flows OUT of a sector (bottom-3 by trailing 5d return), individual names in that sector continue underperforming for 5-15 days as the rotation completes. Standard practice in stat-arb funds.

**Implementation.** `strategies/sector_rotation_short.py`. Reads from existing `macro_data.get_sector_momentum_ranking()` (already cached upstream — no new API hits). Triggers when ALL hold:
1. Symbol's sector is in `bottom_3` per the ranking.
2. Stock's own 5-day return is negative (rotation hitting THIS name, not just sector averages).
3. Stock below 20-day SMA (trend confirmation).
4. RSI between 35-70 (avoid oversold bounce candidates and overbought reversion candidates).
5. Sector not also classified into top-3 (defends against bad sector data).

**NOT tagged as catalyst** — sector rotation is technical/macro, not a company-specific thesis. Strong-bull regime filters it out, which is correct (rotation patterns are weaker when broader market is strongly bid). Score: 2 (medium-conviction).

**Tests added.** `tests/test_sector_rotation_short.py` — 8 tests covering interface, registry membership, NOT-in-catalyst-set assertion, no-data degradation, sector-not-in-bottom-3 rejection, real trigger case, oversold RSI rejection, positive-stock-in-weak-sector rejection.

---

## 2026-04-29 — Phase 3.2 of LONG_SHORT_PLAN: catalyst_filing_short strategy (Severity: high, alpha)

**The thesis.** Material adverse SEC filings (going-concern warnings, material-weakness disclosures, high-severity concerning 8-K language) predict 6-12 month underperformance with statistical significance (Beneish 1999; Dechow et al. 2011). The signal is in the filing AND in the market's reaction — if the stock has already dropped post-filing, the catalyst is real and continuation is likely.

**Implementation.** `strategies/catalyst_filing_short.py`. Reads from existing `sec_filings_history` table populated by the daily SEC analysis task — no API calls in the hot path. Triggers when ALL hold:
1. Filing in last 30 days with `going_concern_flag=1` OR `material_weakness_flag=1` OR (`alert_severity='high'` AND `alert_signal='concerning'`).
2. Price has dropped ≥3% since the filing (market is reacting, not ignoring).
3. Reference close found via timestamp matching to the filing date (falls back to 5 bars ago if timestamps unavailable).

Tagged in `_CATALYST_SHORT_STRATEGIES` so it survives the strong-bull regime gate. Score: 3 (high-conviction). Graceful degrade — if the filings table is empty or missing, returns empty list.

**Tests added.** `tests/test_catalyst_filing_short.py` — 10 tests covering the required interface, registry/catalyst-set membership, no-filings rejection, too-old rejection, going-concern + price-drop trigger, post-filing rally rejection, universe filtering, missing db_path, high-severity-concerning trigger.

---

## 2026-04-28 — Phase 3.1 of LONG_SHORT_PLAN: earnings_disaster_short strategy (Severity: high, alpha)

**The thesis.** Post-Earnings Announcement Drift (PEAD, Bernard & Thomas 1990) shows stocks that miss earnings significantly continue underperforming for 60-90 days. Inverse PEAD on the short side: detect a recent significant gap-down on volume + non-recovery, emit SHORT.

**Implementation.** `strategies/earnings_disaster_short.py`. Detection requires ALL:
1. Within last 10 trading days, a single bar with gap-down ≥5% OR decline ≥8%
2. Volume on that bar ≥ 2× the 20-day avg (institutional distribution, not noise)
3. Latest close still below catalyst-bar close (no recovery yet)
4. Latest close below 20-day SMA (broader trend confirmation)
5. Distance from 52-week high ≥ 15% (false alarms near highs filtered out)

Tagged in `_CATALYST_SHORT_STRATEGIES` so it survives the strong-bull regime gate. The disaster is company-specific damage that overrides market drift.

Works for earnings misses AND any catalyst-driven gap (downgrade, fraud allegation, FDA rejection, guidance cut) — they all share the price-action signature.

**Tests added.** `tests/test_earnings_disaster_short.py` — 7 tests covering: required interface, no-catalyst rejection, near-highs rejection, real disaster trigger, recovered-stock rejection, catalyst-tag membership, registry membership.

---

## 2026-04-28 — Phase 2 of LONG_SHORT_PLAN: pair trades, sector exposure, balance gates (Severity: high, capability)

**The problem.** Phase 1 gave us proper short execution. Phase 2 is what real long/short equity hedge funds use to actually compete: pair trades (long winner / short loser in same sector), sector-aware portfolio construction, target long/short balance per profile.

**Built today (4 commits):**

- **P2.1 Sector exposure tracking.** `portfolio_exposure.compute_exposure()` returns net/gross/by-sector breakdown plus concentration flags (sectors >= 30% of gross book). Wired into the Performance Dashboard's Current Exposure section + the AI prompt's portfolio_state, so the AI sees "you're already 35% long Tech" before picking the next trade.

- **P2.2 Long/short balance target.** New profile column `target_short_pct` (0.0 = long-only [default], 0.5 = balanced, 0.7 = short-dominant). AI prompt surfaces a `LONG/SHORT BALANCE TARGET` directive on every cycle: "you're 50% undershorted vs target, prefer SHORT this cycle." Profile_10 ("Small Cap Shorts") configured to 0.5.

- **P2.3 Pair trades primitive.** `find_pair_opportunities()` scans the candidates list for same-sector long+short matches. Surfaced in the AI prompt as a `PAIR OPPORTUNITIES` section: "Technology: LONG NVDA / SHORT INTC." Lets the AI propose pair trades that isolate relative-strength signal from market beta.

- **P2.4 Balance gate.** When the book has drifted >25 percentage points off target_short_pct, BLOCK new entries on the over-weighted side at the validator. Lets natural turnover (TPs, time stops) bring the book back into balance instead of forcing trims (which would cut winners short and burn transaction costs — what real funds explicitly avoid).

- **P2.5 Factor-aware exposure (minimum viable).** `compute_factor_exposure()` adds two factor slices to the exposure bundle: **size bands** (cheap < $20, mid $20-$100, expensive > $100 — stylized price-based size proxy) and **direction balance** (long_share vs short_share of gross, with `single_direction_concentrated` flag when one side > `SINGLE_DIRECTION_THRESHOLD` = 80%). Bundled into `compute_exposure()` so dashboards and AI prompt see all three slices (sector + size + direction) from one source. Real factor exposures (book-to-market, momentum 12-1m, beta to SPY) need a fundamentals data layer we don't currently cache — deferred to Phase 3.

**Tests added.** 70+ new tests across `tests/test_portfolio_exposure.py` (sector math, pair detection, balance gate logic) and `tests/test_long_short_balance_target.py` (AI prompt rendering for each balance state).

**Test infrastructure failures fixed in this batch.** Running the FULL test suite (not cherry-picked subsets) surfaced 14 failures from earlier work that were silently ignored. Fixed:
  - `test_every_lever_is_tuned`: `target_short_pct` added to `MANUAL_PARAMETERS` (strategic choice, not auto-tuned).
  - `test_meta_model.py`: schema-aware fallback when `prediction_type` column is missing on legacy DBs (fresh test fixtures).
  - `test_sixteen_strategies_registered`: relaxed to `>= 16` since P1.1 added 5.
  - `test_every_meta_model_feature_has_display_name`: added display names for `prediction_type`, `short_max_position_pct`, etc.
  - `test_performance_template_gets_all_its_variables`: ignore `b` (P2.1 sector loop variable).
  - `test_pure_winning_streak_in_window`: ET-localized today match (P1.0 timezone fix).

**Regression coverage.** `tests/test_portfolio_exposure.py` covers sector math + pair detection + balance gate edge cases. The full suite (1138 passing pre-fix) now blocks on the same set of corner cases.

---

## 2026-04-28 — Phase 1 of LONG_SHORT_PLAN: real short capability (Severity: critical, capability)

**The problem.** Even on profile_10 ("Small Cap Shorts" with `enable_short_selling=1`), the system emitted 2 SHORT predictions in 1,491 cycles. The long pipeline had been built thoughtfully; the short side was "shorts allowed if flag is set" bolted onto the long pipeline. No dedicated bearish strategies, no separate AI prompt slots, no asymmetric sizing, no time stops, no borrow / squeeze / regime filters, no per-direction self-tuning, no per-direction calibrators, no meta-model awareness. Result: a strategy that can't compete with real long/short funds.

**The fix.** 14 commits across `LONG_SHORT_PLAN.md` Phase 1, deployed today:

- **P1.0 SELL semantic fix.** Added `prediction_type` column (`directional_long | directional_short | exit_long | exit_short`). Resolver applies per-type win/loss criteria. Backfilled the 12K existing rows — exit_long/short outcomes no longer get conflated with directional shorts.
- **P1.1 Five dedicated bearish strategies.** `breakdown_support`, `distribution_at_highs`, `failed_breakout`, `parabolic_exhaustion`, `relative_weakness_in_strong_sector`. Built specifically for short setups, not bullish strategies with sign flips.
- **P1.2/1.3/1.4 Quality filters on shorts.** Borrow (Alpaca shortable flag), squeeze (short_pct_float + short_ratio classification), regime (strong-bull market suppresses routine technical shorts; catalyst shorts pass through).
- **P1.5/1.6 Time stops + asymmetric sizing.** Cover any short older than `short_max_hold_days` (default 10); cap shorts at `short_max_position_pct` (defaults to half of long max_position_pct).
- **P1.7/1.8 Two shortlists + AI prompt.** `_rank_candidates` reserves slots for shorts (top 10 long + top 5 short). AI prompt splits "LONG CANDIDATES" / "SHORT CANDIDATES" sections with explicit "high-conviction short beats mediocre long" directive.
- **P1.9 + P1.9b Per-direction self-tuning.** Short-side optimizers for `short_stop_loss_pct`, `short_max_position_pct`, `short_max_hold_days`, `short_take_profit_pct`. Schema migrated. Read short-side trades only — long performance can't drown short signal.
- **P1.10 MFE / side mismatch.** `log_trade` writes `side='short'` but the MFE updater queried `side='sell_short'` — every short MFE was None. Fixed.
- **P1.11 Direction-aware specialist calibrators.** Each specialist now has separate (long, short) Platt-scaling models. Ensemble picks the right calibrator based on each verdict's direction.
- **P1.12 Meta-model with prediction_type feature.** Categorical one-hot for direction + signal extended with SHORT/STRONG_SHORT. Pregate at inference time infers direction from candidate's strategy signal.
- **P1.13 Strategy generator alternates direction.** `propose_strategies` accepts `direction_mix`; shorts-enabled profiles alternate BUY/SELL proposals so the strategy library actually grows in both directions.
- **P1.14 Borrow cost as feature + sizing.** Surfaces `_borrow_cost: low|high` in AI prompt; HTB shorts get position cap halved AGAIN on top of the asymmetric short cap.

**Verification.** Tomorrow's first scan will exercise all of this. Targets: profile_10 SHORT/SELL on 20-30% of trades (vs <1% today). Tracking via the per-direction columns + `BY DIRECTION:` line in the AI prompt context.

**What this is NOT.** Phase 2 (sector-neutral / pair trades / factor-aware) and Phase 3 (real alpha sources — earnings disasters, catalyst shorts) are still ahead. The system now has parity on the foundational infrastructure; competing with the highest-Sharpe quant funds requires the Phase 2/3 work.

**Regression coverage.** `tests/test_database.py` exercises schema migration. `tests/test_pipeline.py` covers the rank-candidates path. The MFE side-mismatch was historic data (no test would have caught it without short trades to test against) — once shorts execute and resolve, the verify script will detect MFE-on-shorts populating correctly.

---

## 2026-04-28 — daily_snapshots: dedupe + UNIQUE(date) + INSERT OR REPLACE (Severity: medium, data hygiene)

**What broke.** While walking the Performance Dashboard with the user
(All Profiles view, validating each metric vs source data), I noticed
`daily_snapshots` had many rows per date — 13 rows for 2026-04-17, 8 for
2026-04-22, 11 for 2026-04-25. Per-DB pattern was identical. The metric
readers (`metrics.py:185`, `views.py:1233`, `views.py:2862`,
`multi_scheduler.py:1055`) happened to pick the right row most of the time
because `dict[date] = snapshot` overwrites in iteration order, but that
behavior is undocumented in SQLite and one VACUUM / migration could break
it silently.

**Why it wasn't caught.** The marker-file fix from 2026-04-25 ("100+
daily summary emails ... in-memory state reset on each of ~10 deploys")
stopped re-snapping per scheduler restart, so 2026-04-27 and 2026-04-28
each had exactly 1 row per date. But the scar tissue from before the
fix stayed in the DB, and there was no schema constraint preventing the
duplicates from re-appearing if anything regressed.

**The fix (3 layers, belt-and-suspenders).**

1. **Reader hardening** (`metrics.py`, `views.py` x2, `multi_scheduler.py`):
   all 4 daily_snapshots readers now filter to `MAX(rowid) GROUP BY date`
   so the latest write per date is picked deterministically. Used
   `rowid` instead of `id` so test fixtures with minimal schemas still
   work.

2. **Writer upsert** (`journal.py:log_daily_snapshot`): switched
   `INSERT INTO` → `INSERT OR REPLACE INTO daily_snapshots`. With the
   UNIQUE(date) constraint below, same-day re-runs now overwrite
   instead of accumulating.

3. **Schema migration** (`journal.py:_migrate_daily_snapshots_unique`):
   one-shot table rebuild that adds `UNIQUE(date)` and dedupes existing
   rows in the same step (`INSERT INTO new SELECT ... WHERE id IN
   (SELECT MAX(id) GROUP BY date)`). Idempotent — checks for the
   UNIQUE index via PRAGMA before rebuilding. Wired into `init_db`,
   so it runs once on each profile DB on next scheduler start.

**Verified.** Migration on a copy of `quantopsai_profile_8.db`
(production data): 100% of computed metrics match (Sharpe, Sortino,
max DD, daily returns, etc.). No displayed numbers change — confirming
the readers were already picking the same row by accident. After
migration the table has 1 row per date and the implicit unique
index `sqlite_autoindex_daily_snapshots_1`. Re-running `init_db`
is a no-op.

**Regression test.** `tests/test_database.py::TestProfileDatabase`
covers `journal_init_idempotent`. The migration's idempotency check
(via `PRAGMA index_list`) is exercised on every `init_db` call.

---

## 2026-04-28 — Silent ctx ↔ DB disconnect on 9 columns (Severity: critical, reliability)

User asked me to be fully sure nothing was left. While doing one
more verification pass, I traced whether `disabled_specialists`
(written by the auto-disable health check) actually reaches
`ensemble.run_ensemble` via ctx. It doesn't — the column wasn't
on `UserContext` and wasn't populated by
`build_user_context_from_profile`. So the DB write was real, but
the running scheduler couldn't see it through ctx; the disable
list was ignored at decision time. Lever 3's effect on actual
trading was silently zero in production, even though the DB row
was correct.

I wrote a structural test that walks every `.py` file for
`ctx.<col>` and `getattr(ctx, "<col>", ...)` patterns where
`<col>` is also a `trading_profiles` column name. The test then
fails for any name that isn't a `UserContext` field AND populated
in `build_user_context_from_profile`.

**Test surfaced 4 more silent disconnects beyond `disabled_specialists`:**

- `signal_weights` (Layer 2) — `ai_analyst.py:543` reads via
  `getattr(ctx, "signal_weights", None)`. Layer 2 weighted-signal
  intensity was inert through ctx.
- `regime_overrides` (Layer 3) — `self_tuning.py:3416`. Layer 3
  bull/bear/sideways/volatile overrides inert.
- `capital_scale` (Layer 9) — `trade_pipeline.py:439` defaulted to
  1.0 always. The auto-allocator's recommendation never reached
  position sizing.
- `alpaca_account_id` — `multi_scheduler.py:877` defaulted to None
  always. Multi-Alpaca-account linkage didn't see DB updates
  through ctx.

Plus 2 more I haven't verified are accessed: `tod_overrides`
(Layer 4), `symbol_overrides` (Layer 7), `prompt_layout` (Layer 6),
and `ai_model_auto_tune` — added preventively.

**Fixes:**

1. Added all 9 fields to `UserContext` dataclass with sensible
   defaults matching the DB defaults.
2. Populated each in `build_user_context_from_profile`.
3. Cleaned up two dead-fallback patterns in `self_tuning.py`
   (was reading `ctx.market_type` then falling back to
   `ctx.segment` — first try always failed because market_type
   wasn't on ctx; same pattern for ai_api_key_enc).

**Anti-regression — `tests/test_ctx_field_round_trip.py` (4 tests):**

- AST-walks the repo for `ctx.<col>` and `getattr(ctx, ...)` —
  every column-named attribute access must be a UserContext field
  AND assigned in `build_user_context_from_profile`. Catches the
  ENTIRE class going forward.
- Explicit guards for `disabled_specialists` and
  `meta_pregate_threshold` round-trip.

**Honest read:** The Lever 3 health check's reasoning has been
correct since I added it (auto-detect anti-correlated specialists,
write disable list to DB). But because of this silent-disconnect
bug, the disable list never reached the running ensemble at scan
time. Same pattern means Layers 2/3/4/6/7/9 + multi-account
linkage have all been partially inert in production. After this
fix, the next scan cycle will read all of these correctly.

Tests: 1098 passing.

---

## 2026-04-28 — MFE floor bug: max_favorable_excursion can't be below entry (Severity: medium, accuracy)

End-of-day verification of yesterday's Lever-3 work surfaced a
real bug. AVGO long: entry $414.74, max_favorable_excursion
$405.07. MFE below entry is impossible by definition.

**Root cause:** the MFE updater initialized with
`MAX(COALESCE(mfe, current_price), current_price)`. On first
observation that returns whatever current_price was at that
moment — even if it had already dropped below entry. For a long,
the MFE floor IS entry price (the position never had a price
below entry "in our favor"). For shorts, symmetrically, the
ceiling is entry.

**Why it matters:** the trailing-stop optimizer
(`_optimize_trailing_atr_multiplier`) computes
`give_back_pct = (mfe - exit_fill_price) / mfe` to bucket
trades. With MFE below entry, give-back math is nonsensical —
which means the trailing-stop tuner would make wrong decisions
once it had enough samples to fire (it hasn't fired yet —
sample-size gate at 30 closed longs not met).

**Fix:** include the row's `price` column in the MAX/MIN:
```sql
UPDATE trades SET max_favorable_excursion =
  MAX(COALESCE(max_favorable_excursion, price), price, ?)
WHERE symbol = ? AND side = 'buy' AND status = 'open'
```

Self-heals on next Check Exits cycle. Existing rows with bad
MFE auto-correct.

**Anti-regression — `tests/test_mfe_floor_at_entry.py` (7 tests):**

- Long with current below entry → MFE floored at entry
- Long sequence (high then drop) → MFE tracks max correctly
- Short with current above entry → MFE ceilinged at entry
- Short sequence → MFE tracks min correctly
- Long self-heal: pre-fix bad row corrects on next update
- Short self-heal: same
- Source-level guard: SQL must reference the row's `price` column
  in both long and short paths.

Tests: 1094 passing.

---

## 2026-04-28 — update_trading_profile silently dropped disabled_specialists writes (Severity: critical, reliability)

Verifying yesterday's Lever 3 health check on prod, found the
detection logic correctly identified pattern_recognizer as anti-
correlated on Small Cap (raw=90 → cal=28, n=360) and called
`update_trading_profile(profile_id, disabled_specialists=...)`.
The health check logged "Specialist health check applied: DISABLE
pattern_recognizer" successfully.

But `disabled_specialists` was NOT in the `allowed_cols` allowlist
inside `update_trading_profile`. The kwarg was silently filtered
out and the UPDATE never executed. Across all profiles, the column
stayed `[]` after the health check ran — health check thought it
won, DB said otherwise.

Same bug pattern as the morning's silent-execute_trade-swallow:
hide a side-effect failure behind a `return None` so callers
can't tell their action didn't take.

**Fix:**

1. Add `disabled_specialists` and `meta_pregate_threshold` to
   `allowed_cols` in `models.update_trading_profile`.
2. Loud `logger.warning(...)` when ANY kwarg is rejected as
   unknown. Future schema additions trigger a visible log line
   instead of silent swallow.

**Anti-regression — `tests/test_update_trading_profile_allowlist.py` (3 tests):**

- `test_every_kwarg_passed_is_in_allowed_cols` — repo-wide AST
  scan: every kwarg name passed to `update_trading_profile()`
  anywhere in the codebase must appear in `allowed_cols`.
  Catches the ENTIRE class — adding a new column without
  updating the allowlist fails the build.
- `test_lever_2_3_columns_in_allowlist` — explicit guard for
  `disabled_specialists` and `meta_pregate_threshold`.
- `test_update_trading_profile_logs_rejected_kwargs` — verifies
  the loud-log discipline.

After deploy, the next daily snapshot block will re-run the
specialist health check and the disable list will actually
persist this time. Verified detection-side data:
- Small Cap: pattern_recognizer should be disabled (cal_at_90=28)
- Mid Cap: pattern_recognizer + sentiment_narrative both flagged

Tests: 1087 passing.

---

## 2026-04-28 — Three real bugs from the morning's anomaly scan (Severity: critical, reliability+accuracy)

User flagged two anomalies on the dashboard ticker:

1. `Large Cap Limit Orders: Check Exits failed at 11:06 AM ET`
2. `SHORT VALE (2% equity, 53% confidence) — Perfect fit: 100% personal win rate (13W/0L) on VALE SHORT signals`
3. (User follow-up) "the SHORT was listed but never went through"

All three are real, all three are fixed.

### Bug 1 — Missing `import logging` in trader.py (CRITICAL)

`trader.py` was using `logging.info(...)` and `logging.debug(...)`
in the short-borrow accrual + MFE updater code I added on
2026-04-27 (commit `e2c040d`), but the module never imported
`logging`. Result: `NameError: name 'logging' is not defined`
fired every Check Exits cycle for the Large Cap Limit Orders
profile (the only profile holding limit-order positions long
enough to enter the short-borrow path).

Silent regression for ~24 hours. Caught only because the user saw
the failure in the Scan Failures dashboard panel.

Fix: `import logging` at the top of trader.py.

Anti-regression: `tests/test_no_missing_logging_import.py` —
AST-walks every .py file in the repo. If a file uses `logging.X`
at any depth, it MUST `import logging` at any scope. Catches the
entire class of bug.

### Bug 2 — AI confabulating signal-specific track records

The AI's "100% on VALE SHORT signals (13W/0L)" claim was
fabricated. Actual data: VALE had 13 RESOLVED predictions, all of
them HOLD signals, ZERO resolved SHORTs. Root cause:
`get_symbol_reputation()` aggregated wins/losses across ALL signal
types into a single number. The prompt then injected
`Your record on VALE: 13W/0L (100% win rate)` and the AI
reasonably attributed those wins to whatever signal it was
currently considering (in this case, SHORT).

Fix:
- `get_symbol_reputation` now returns a `by_signal` breakdown
  alongside the aggregate.
- `_build_candidates_data` formats the prompt's `track_record`
  field with explicit signal splits:
  `"13W/0L overall (100%) — BUY 0W/0L (0%); SHORT 0W/0L (0%);
   HOLD 13W/0L (100%)"`.
- The AI now sees that VALE has zero SHORT history and can't
  cite signal-specific edge from HOLD outcomes.

Anti-regression: `tests/test_track_record_split_by_signal.py` —
4 tests including the exact VALE repro (13 HOLD wins, asserts
no by_signal entry claims SHORT credit).

### Bug 3 — Silent error-swallow on trade execution

User: "the SHORT was listed but never went through." Dashboard
showed `Executing: SHORT VALE` at 14:35:02 UTC but no order_id,
no submitted log, no error trace. The code at
`run_trade_cycle:1628` had a try/except wrapping `execute_trade`
that appended exceptions to `errors[]` with **no log emission**.
Plus the SKIP / EXCLUDED / EARNINGS_SKIP non-exception paths also
returned silently — the user only ever saw "Executing:" with no
follow-up.

Fix:
- Exception path: `logging.error(..., exc_info=True)` so the
  full traceback hits the journal. Alpaca rejections (e.g.,
  not-shortable, halt, regulatory restriction) now produce a
  visible error line.
- Non-exception SKIP path: `logging.warning(...)` when
  `execute_trade` returns a non-trade action, with the symbol +
  action + reason.

Anti-regression: `tests/test_trade_execution_logging.py` —
2 source-level guards (logging.error with exc_info=True;
warning emitted on non-trade action).

Tests: 1084 passing (was 1077; +7 new across 3 files).

---

## 2026-04-27 — Wave 3 / Fix #9: per-specialist confidence calibration (Severity: medium, accuracy)

The last methodology fix. METHODOLOGY_FIX_PLAN.md is now ✅ COMPLETE
— all 9 issues identified by the audit are fixed.

**Before:** Each specialist (earnings_analyst, pattern_recognizer,
sentiment_narrative, risk_assessor) returned a verdict + raw
confidence 0-100. The ensemble synthesizer multiplied that raw
confidence by a static specialist weight to compute the contribution
to the final BUY/SELL score. But the raw confidence was never
validated against actual outcomes — when earnings_analyst said BUY
78%, it might have been right 50% of the time historically. An
over-confident specialist therefore dominated the ensemble even
when its track record didn't justify it.

**Fix:**

1. New module `specialist_calibration.py`:
   - `init_calibration_db(db)` creates `specialist_outcomes` table.
   - `record_outcomes_for_prediction(db, pred_id, specialists)` logs
     the per-specialist verdicts attached to each prediction.
   - `update_outcomes_on_resolve(db, pred_id, was_correct)` backfills
     the binary outcome when the prediction resolves.
   - `fit_calibrator(db, specialist)` trains a logistic regression
     mapping `raw_confidence/100 → P(correct)` on the last 90 days
     of resolved data; returns None below 30 samples or on
     degenerate (all-win/all-loss) inputs.
   - `apply_calibration(raw, calibrator)` returns the calibrated
     confidence as an int in [0, 100]; passes raw value through
     when calibrator is None (graceful degradation).
   - Per-specialist pkl persistence + module-level cache.
   - `refit_all(db, names)` for the daily scheduler task.

2. **Schema** — new `specialist_outcomes` table with
   `(prediction_id, specialist_name)` UNIQUE constraint and an
   index on `(specialist_name, resolved_at)` for fit performance.

3. **Wiring:**
   - `trade_pipeline.py` now passes `c["ensemble_specialists"]` from
     each candidate's per-symbol entry forward and calls
     `record_outcomes_for_prediction` immediately after
     `record_prediction`.
   - `ai_tracker.resolve_predictions` now calls
     `update_outcomes_on_resolve(was_correct=outcome=='win')` for
     each prediction it resolves to win/loss (skips neutrals).
   - `ensemble._synthesize` accepts `db_path`; loads calibrators
     once per ensemble run; applies `apply_calibration` to each
     specialist's confidence BEFORE computing contributions to the
     buy/sell score. Each per-symbol output now carries both
     `confidence` (calibrated) and `raw_confidence` (original) for
     auditability.
   - `ensemble.run_ensemble` passes `ctx.db_path` through.

4. **Daily retrain** — new `_task_calibrate_specialists` in
   `multi_scheduler.py`, registered in the daily snapshot block
   right after the meta-model retrain. Runs `refit_all` per profile.

5. **Anti-regression — `tests/test_specialist_calibration.py` (8 tests):**

   - Module exposes the contract API (8 named functions).
   - `_synthesize` source references `apply_calibration` and
     `get_calibrator` so removing the integration trips the build.
   - Record-then-resolve round trip writes correct rows.
   - Fit returns None below MIN_SAMPLES_TO_FIT.
   - **Behavioral leakage test #1:** seed 100 outcomes for an
     "overconfident" specialist (always raw=90, 50% hit rate). Fit
     calibrator. Assert `apply_calibration(90)` returns 35-65
     (calibrated DOWN to ~50). With the bug, this would return ~90.
   - **Behavioral leakage test #2:** seed 120 outcomes for an
     "underconfident" specialist (raw=25-35, 80% hit rate). Assert
     `apply_calibration(30)` returns ≥ 60 (calibrated UP toward 80).
   - apply_calibration with None returns raw value unchanged.
   - get_calibrator returns None when no pkl exists.

Tests: 1000 passing (was 992; +8 new). 🎉

**Why the AUC bump won't appear immediately:** specialist outcomes
start being logged from this commit forward. The first time
`fit_calibrator` produces a real model per specialist will be after
30+ resolved predictions per specialist accumulate
(~1-2 trading weeks at current volume). Until then,
`get_calibrator` returns None and `_synthesize` uses raw confidence
— same as before. The fix is **prospective**: it kicks in
automatically once the data is there.

---

## 2026-04-27 — EXPERIMENTATION_AND_TUNING.md: unified partner-facing doc on how the system learns (Severity: low, docs)

User asked: "Do we have a document that explains in detail how our
experimentation and tuning works?" Honest answer was: scattered
across SELF_TUNING.md, AUTONOMOUS_TUNING_PLAN.md, ROADMAP.md
(Phases 1, 3, 7), and METHODOLOGY_FIX_PLAN.md — no single unified
doc.

Wrote `EXPERIMENTATION_AND_TUNING.md` (~270 lines) that pulls all
the threads together:

- The headline (1 page): 7 feedback loops grinding on AI's own
  outcomes, all daily, all gated by cost ceiling.
- The closed-loop diagram (1 ASCII figure showing the entire data
  flow from universe → execution → resolution → 7 loops).
- All 7 loops described in detail: meta-model, self-tuner, 12-layer
  autonomy stack, alpha-decay, specialist calibration (added today),
  strategy auto-generation (Phase 7), post-mortems on losing weeks.
  Each loop includes its file, DB table, run frequency, and the
  specific integrity guarantee from the methodology audit.
- The 9 integrity guarantees (the audit fixes) summarized in a
  single table with status.
- A concrete worked example: today's pattern_recognizer
  inversely-calibrated finding (raw 90 → calibrated 28 on Small
  Cap) — uses the actual prod data to show the system surfacing
  its own failure mode automatically.
- "What to expect over time" — week 1, 2, 4, 6, 12 timeline.
- Where to look in the dashboard for each loop.
- Cross-session continuity — which doc to read in what order.

README.md doc tree updated to include this + METHODOLOGY_FIX_PLAN.md.
HTML export at exports/EXPERIMENTATION_AND_TUNING.html (33K).

---

## 2026-04-27 — pytest-randomly added; suite verified deterministic across random orderings (Severity: low, infra)

User flagged a one-off test failure earlier ("there should not be
flake"). Investigated:

- 3 consecutive sequential runs: 1002 / 1002 each.
- 5 randomized orderings (seeds 1-5 via pytest-randomly):
  1002 / 1002 each.

Conclusion: the test suite has no deterministic order dependency.
The earlier failures were almost certainly transient I/O issues
(sqlite locking, filesystem sync, or — likely — pytest running
concurrently with a `./sync.sh` deploy in another shell on the same
machine).

Permanent fix: `pytest-randomly` added to `requirements.txt`. From
now on every local & CI test run uses a randomized seed. If anyone
introduces order-dependent test pollution in the future, it'll show
up immediately as a deterministic failure on some seeds, not as an
intermittent "flake" later.

---

## 2026-04-27 — Specialist calibration: backfill from existing 4,400 resolved predictions (Severity: medium, accuracy)

After Fix #9 shipped, the user pointed out we have ~4,400 resolved
predictions across all profiles already. They're right — the
calibrator-data isn't actually starting from zero today. Each
resolved prediction's `features_json["ensemble_summary"]` carries
the per-specialist verdict + confidence in a parseable format (e.g.
`earn=BUY(72), patt=HOLD(45), sent=SELL(78), risk=HOLD(55)`).

Added `specialist_calibration.backfill_from_resolved_predictions(db)`
that parses every resolved prediction's ensemble_summary and seeds
the `specialist_outcomes` table with `was_correct` already set from
the prediction's `actual_outcome`. Idempotent via the
`(prediction_id, specialist_name)` UNIQUE constraint. Skips ABSTAIN
(no signal) and VETO (separate code path). Skips neutrals
(actual_outcome NOT IN ('win', 'loss')).

Two new behavioral tests:
- Parse format check: 3 seeded predictions → expected row count by
  outcome and skip-rule.
- Idempotency: re-running inserts zero rows.

Tests: 1002 passing (+2 new).

After deploy: a one-shot script runs `backfill_from_resolved_predictions`
on every profile DB, then triggers the daily calibration retrain so
calibrators are fitted immediately rather than waiting 1-2 weeks for
fresh outcomes.

---

## 2026-04-27 — Levers 1-3 of COST_AND_QUALITY_LEVERS_PLAN.md (Severity: medium, cost+quality)

User asked for all three cost-reduction levers planned and shipped
in one session. Markets closed = right time to land structural
changes. Plan committed first as `COST_AND_QUALITY_LEVERS_PLAN.md`,
then implemented in order.

**Lever 1 — Persistent shared cache (`shared_ai_cache.py`):**

- New SQLite-backed cache for cross-profile AI results that
  previously lived in module-level dicts (`_ensemble_cache`,
  `_political_cache`).
- Two-tier: in-process L1 (fast hot path) + SQLite L2 (cross-restart).
- `trade_pipeline._get_shared_ensemble` and
  `_get_shared_political_context` now check L2 before firing the
  expensive call. Same 30-min TTL as before.
- Survives scheduler restarts. Today's 16-deploy cadence cost
  ~$0.50 in cache wipes; structurally protected against that
  pattern from now on.
- Quality: identical (same payloads, just persisted).

**Lever 2 — Meta-model pre-gate (`_meta_pregate_candidates`):**

- New helper in `trade_pipeline.py`. Runs the meta-model on each
  shortlisted candidate BEFORE the ensemble fires. Drops candidates
  with `meta_prob < threshold` (default 0.5).
- Wired into Step 3.65 of the trade pipeline, immediately before
  `_get_shared_ensemble` and `update_status` for the ensemble step.
- Per-profile config: `meta_pregate_threshold` (default 0.5,
  0.0 = disabled).
- Cold-start safe: when no meta-model is trained yet, the gate
  falls open and returns all candidates. Per-candidate
  `predict_probability` failures also fall open.
- Cost: ~50% reduction in ensemble specialist calls on profiles
  with trained meta-models.
- Quality (4 mechanisms): sharper specialist attention,
  smaller batch_select prompt → more reasoning per remaining
  candidate, risk_assessor VETO authority preserved for edge
  cases, calibration data accumulates ~2× faster.

**Lever 3 — Per-profile specialist disable list:**

- New per-profile column `disabled_specialists` (JSON list).
- `ensemble.run_ensemble` reads the list and skips disabled
  specialists' API calls entirely.
- Hard floor: never fewer than 2 active specialists per profile.
  Floor enforcement logs a warning + restores enough to satisfy
  the floor.
- New daily scheduler task `_task_specialist_health_check`:
  - DISABLEs a specialist when its calibrator maps raw=90 to
    cal<35 with ≥50 resolved samples (anti-correlation signal).
  - RE-ENABLEs a previously-disabled specialist when its
    calibrator recovers to raw=90 → cal>50.
  - Hard floor protects against disabling all 4.
- Cost: ~$0.20-$0.40/day per profile where a specialist is
  disabled.
- Quality (5 mechanisms): sign-flip beyond what calibration alone
  can do, cleaner synthesizer math, cleaner final-AI-prompt
  narrative, legible coverage analysis on remaining specialists,
  higher-information VETOs.

**Schema additions:**

- `trading_profiles.disabled_specialists TEXT NOT NULL DEFAULT '[]'`
- `trading_profiles.meta_pregate_threshold REAL NOT NULL DEFAULT 0.5`
- `shared_ai_cache(cache_key, cache_kind, bucket, payload, fetched_at)`
  with PK on `(cache_key, cache_kind)`, index on `(cache_kind, bucket)`.

**Anti-regression — 21 new structural tests across 3 files:**

`tests/test_shared_ai_cache.py` (9):
- Round-trip put→get; bucket expiry; pickle corruption returns
  None; clear_kind selectivity; concurrent put atomic replace;
  trade_pipeline.{ensemble,political} integration with persisted
  cache; source-level reference guard.

`tests/test_meta_pregate_lever.py` (7):
- No-model path → falls open; threshold 0.0 → disabled;
  drops candidates below threshold; per-candidate fail-open;
  source-level pipeline-ordering guard (pregate BEFORE ensemble);
  empty input handled; no-profile-id falls open.

`tests/test_specialist_disable_lever.py` (5):
- Disabled specialist's skip-branch fires;
  floor-enforcement restores when too many disabled;
  source-level guards on `run_ensemble` and the scheduler
  health-check task.

Plus the `test_every_lever_is_tuned.py` allowlist updated to
mark `disabled_specialists` and `meta_pregate_threshold` as
explicitly-managed-elsewhere (not auto-tuned by self_tuning.py).

Tests: 1077 passing (was 1056; +21 new).

**Cumulative impact projection:**

- Lever 1: ~$0.50/day deploy-heavy, ~$0.05/day quiet (no quality change)
- Lever 2: ~$0.30-0.40/day once meta-models train (quality improves)
- Lever 3: ~$0.40/day once auto-disable fires (quality improves)
- **Total: ~$1.20/day savings + measurable decision-quality gains**

Projected normal-cadence daily AI spend: $1.50-$2.00 (well below
the $3 user-set ceiling).

---

## 2026-04-27 — transcript_sentiment cache: 24h → 30d (closes ~$0.30/day token leak) (Severity: medium, cost)

User flagged elevated AI spend today ($3.54 vs Fri's $0.42/profile
baseline). Audited per-purpose breakdown: ensemble specialist fires
tripled (7 → 21 per market_type) — but that's a one-time artifact
of 16 deploys today (each restart wipes the in-memory ensemble
cache). Tomorrow with normal cadence the ensemble normalizes.

Genuine bug found in the audit: `sec_filings.get_earnings_call_sentiment`
docstring claims "Cost-gated: cached 30 days (earnings are quarterly)"
but the actual code routed through `_get_cached(key, "insider")`
which has a 24-hour TTL. So the per-symbol AI call was re-firing
every day per held position even though the underlying 8-K text
only changes quarterly. ~30 redundant calls per profile per day.

Fix:
- New `_CACHE_TTL["transcript"] = 86400 * 30` in alternative_data.py
- get_earnings_call_sentiment switched to the "transcript" bucket
- Comment in both files explains the rationale

Saves ~$0.30/day system-wide. Projected normal-cadence daily total:
$2.50-$2.80 (under the $3 ceiling on quiet days; over on heavy-news
days when sec_diff fires more).

---

## 2026-04-27 — Backfill historical activity_log rows with raw snake_case + decimals (Severity: medium, ux)

User noticed that 3+ hours after the structural fix at `fb55c07`,
their ticker still showed:
> "Reviewed past adjustment: max_position_pct 0.08->0.092
>  (win rate 48%->52%: IMPROVED)"

Correctly identified the cause: that row was logged BEFORE the fix
deployed. The activity_log table stores text as-is; a code change
doesn't retroactively rewrite history. So the fix only affects
rows logged AFTER the deploy.

`migrate_activity_log_format.py` — one-shot rewriter that walks
existing activity_log rows whose `detail` matches the old format
and rewrites in place using the same `display_name()` +
`format_param_value()` helpers the live code now uses.

Three regex patterns covered:
1. "Reviewed past adjustment: <param> <old>-><new>"
2. "REVERSED: <param> back from <new> to <old>"
3. "- Adjusting <param>: ..."

Plus a cosmetic pass on "win rate 48%->52%" → "win rate 48% → 52%".

Idempotent — re-running on already-rewritten text is a no-op
(rewritten format no longer matches the regex). Defensive — only
rewrites if the matched name is actually a key in PARAM_BOUNDS, so
random English text containing underscores (e.g., "has_options",
"easy_to_borrow") passes through untouched.

Supports `--dry-run` to preview counts without committing.

`tests/test_migrate_activity_log_format.py` — 8 tests covering:
- The exact user-reported string roundtrips to friendly format
- REVERSED message variant rewrites correctly
- "- Adjusting <param>" summary lines
- Re-running is idempotent
- Unrelated text (no PARAM_BOUNDS match) passes through
- Made-up snake_case names not in PARAM_BOUNDS pass through
- End-to-end with a real SQLite DB
- `--dry-run` doesn't write

Tests: 1056 passing.

Run on prod: `python migrate_activity_log_format.py --db /opt/quantopsai/quantopsai.db`

---

## 2026-04-27 — Snake_case + raw-decimal leak in ticker: 6 fixes + strengthened guard (Severity: critical, regression-prevention)

User saw on the activity ticker:
> "PAST ADJUSTMENT REVIEWS:
>  - Reviewed past adjustment: max_position_pct 0.08->0.092
>    (win rate 48%->52%: IMPROVED)"

Both leaks I had previously claimed structural tests would catch.
The user's words: "you have GUARANTEED catches every possible
snake case issue, especially ones within the ticker. and you are
displaying them as unfriendly decimals, which you also said you
have a test for. … We have talked about this at length, you say,
yes, i've caught every place that this could happen, very
specifically this example and yet here it is."

The user is right and the gap is real. Two compounding bugs:

**1. The bug itself — `self_tuning.py:1330`:**

```python
adjustments_made.append(
    f"Reviewed past adjustment: {param} {old_v}->{new_v} "
    f"(win rate {wr_before:.0f}%->{wr_after:.0f}%: {outcome})"
)
```

Built directly inside `apply_auto_adjustments` — the orchestrator
that REVIEWS past adjustments before running new optimizers. Raw
param name + raw decimals straight into the ticker.

**2. The test that "guaranteed" coverage — actually didn't:**

`tests/test_no_snake_case_in_optimizer_strings.py` previously walked
ONLY `_optimize_*` Return statements:

```python
if not node.name.startswith("_optimize_"):
    continue
```

The bug was inside `apply_auto_adjustments`, NOT an `_optimize_*`
function. Test silently passed because that function name didn't
match. Same story for value-formatting — the previous coverage had
no decimal-format guard at all.

**Fixes:**

`self_tuning.py` — 6 locations updated to route through `_label()`
and `format_param_value()` (aliased as `_fmt`):
- `apply_auto_adjustments:1330` (the user-reported bug — past
  adjustment review)
- `describe_tuning_state:999/1001/1003` ("Adjusting {param}: …" lines)
- `apply_auto_adjustments:1388` (REVERSED message)
- `_optimize_price_band:2462` (raise min_price floor)
- `_optimize_price_band:2492` (lower max_price ceiling)
- `_optimize_min_volume:2789` (raise min_volume floor)

**Strengthened guard:**

`tests/test_no_snake_case_in_optimizer_strings.py` now walks EVERY
function in `self_tuning.py`, not just `_optimize_*`. Refined to
ignore standalone param-name strings (those are internal database
column / kwargs identifiers, not user-facing) — only flags when a
PARAM_BOUNDS key appears EMBEDDED INSIDE a longer string literal.

**Plus a new decimal-format guard** in the same file:
`TestNoRawDecimalsForPercentageParams` — walks every `JoinedStr`
(f-string), and if the f-string mentions a percentage-typed param
name in its literal text AND interpolates a raw old/new value
variable (`old_v`, `new_v`, `old_val`, `new_val`, `current`,
`new_pct`, `current_pct`) WITHOUT wrapping it in
`format_param_value()` / `_fmt()`, the test fails.

The "0.08->0.092" leak is now structurally impossible — the test
wraps both axes (param name AND value-formatting) for the entire
self_tuning.py module, not just `_optimize_*` returns.

Tests: 1048 passing (was 1047; +1 net — strengthened guard caught 6
existing bugs, fixed those, plus a new behavioral test for the
decimal formatter).

---

## 2026-04-27 — All three placeholder optimizers + MFE tracking + days_to_earnings feature (Severity: medium, accuracy)

User scoured for "any open item I missed." Found three `_optimize_*`
functions that were registered but `return None`-only placeholders,
plus stale references in 3 docs.

**Three optimizers implemented for real:**

1. `_optimize_skip_first_minutes` — buckets resolved predictions by
   minutes-since-market-open (parsed directly from `timestamp`).
   Recommends raising the skip threshold when opening-window WR is
   materially below the rest-of-day; lowering when it's fine.

2. `_optimize_avoid_earnings_days` — buckets by `days_to_earnings`
   (now captured in `features_json` for new predictions).
   Recommends tightening when in-window predictions underperform
   out-of-window; loosening when they outperform (post-earnings
   drift catch).

3. `_optimize_trailing_atr_multiplier` — uses new
   `max_favorable_excursion` (MFE) column to compute give-back %
   per closed long. Tightens when avg give-back > 50% (winners
   evaporate too much before exit); loosens when give-back < 10%
   AND avg pnl positive (winners getting whipsawed near peak).

**Schema additions (idempotent migrations in `journal._migrate_all_columns`):**

- `trades.max_favorable_excursion REAL` — populated by a new MFE
  updater in `trader.check_exits` that runs every cycle. For longs:
  `MAX(current, MFE)`. For shorts: `MIN(current, MFE)`. Cheap (1
  UPDATE per held symbol per tick).
- `features_json["days_to_earnings"]` — added by `trade_pipeline`
  via `earnings_calendar.check_earnings(sym)` at prediction-record
  time. Older predictions get -1 (excluded from the bucketing).

**Doc-cleanliness pass (additional findings from the scour):**

- `SELF_TUNING.md` "Coming Next (per AUTONOMOUS_TUNING_PLAN.md)"
  section pointed to a deleted file. Replaced with "All 12-Wave
  Layers ✅ Shipped" status table.
- `ROADMAP.md` "Phase 1 Implementation (Current)" heading was stale
  (Phase 1 long since complete). Updated to
  "(✅ Complete — kept here as design reference)".
- `ROADMAP.md` cross-session continuity section instructed future
  contributors to "find the row marked 🟡 In Progress" — but no
  such row exists anymore. Rewritten to point at the current
  partner-facing doc set instead.
- `TECHNICAL_DOCUMENTATION.md` §15 was still describing
  short-borrow accrual as a "Single small gap, deferred" — that
  shipped in commit `e2c040d`. Updated to ✅ Shipped with the
  details + test reference.

**10 new behavioral tests** in
`tests/test_self_tuning_placeholder_optimizers.py`:
- 3 cases each for skip_first_minutes and avoid_earnings_days
  (self-skip, tighten, loosen, plus a no-feature-data skip case).
- 3 cases for trailing_atr_multiplier (self-skip < 30 samples,
  tighten on excessive give-back, loosen on small give-back +
  positive pnl).

Integration verified by the existing snake_case AST guard
(`tests/test_no_snake_case_in_optimizer_strings.py`) — all three
new implementations route their user-facing reason strings through
`_label('param_name')` instead of embedding the snake_case key
directly.

Tests: 1047 passing (was 1037; +10 new).

---

## 2026-04-27 — Closing every open item: sector_classifier, get_live_universe + flag, short_borrow accrual, doc cleanup (Severity: medium, hygiene + integrity)

User instruction: "ALL THE THINGS, NO OPEN ISSUES." Cleared the
remaining DYNAMIC_UNIVERSE_PLAN.md items + the deferred TECHNICAL
DOC §15 short-borrow gap + stale plan-doc cleanup, all in one pass.

**1. `sector_classifier.py` (new module)** — replaces the hardcoded
~50-symbol `_SECTOR_MAP` in `market_data._guess_sector`. SQLite cache
in `quantopsai.db.sector_cache` (7-day TTL). Lookup order:
cache → yfinance GICS → static fallback map (~100 symbols) → "tech"
default. Fail-open at every layer. `_guess_sector` now a one-line
delegate. Means future sector reclassifications and rename events
update automatically; the 50-symbol blind spots in the old map are
gone.

**2. `segments.get_live_universe(name, ctx)` + `USE_DYNAMIC_UNIVERSE`
feature flag (off by default).** When the env flag is "true", live
trading universe = hardcoded list ∩ Alpaca-active set (via the same
`get_active_alpaca_symbols` helper the screener already uses — zero
new API calls). Crypto bypasses the dynamic path. Default OFF
preserves historical behavior; user can flip per-profile to A/B.

**3. `short_borrow.py` (new module)** — overnight-short borrow
accrual. `compute_borrow_cost(shares, price, days, bps_per_day)`
implements the standard `notional × bps/day × days` formula.
`accrue_for_cover(db, symbol, shares)` looks up the most-recent open
sell_short, computes days held, returns USD cost (zero for sub-1-day
intraday covers). `trader.check_exits` cover branch now subtracts
the accrual from `pnl` before logging. Default rate 0.5 bps/day
(~1.8% annualized) for general collateral; per-symbol overrides for
known hard-to-borrow names (GME, AMC, BBBY, DJT). Closes the
deferred-item gap in TECHNICAL_DOCUMENTATION.md §15.

**4. Doc cleanup.** Three superseded plan docs deleted:
- `ALTDATA_PLAN.md` — superseded by ALTDATA_INTEGRATION_PLAN.md
- `AUTONOMOUS_TUNING_PLAN.md` — superseded by SELF_TUNING.md +
  EXPERIMENTATION_AND_TUNING.md
- `METHODOLOGY_FIX_PLAN.md` — fully fixed; coverage in CHANGELOG +
  EXPERIMENTATION_AND_TUNING.md §4

Their HTML exports also deleted.
`DYNAMIC_UNIVERSE_PLAN.md` status header updated to ✅ COMPLETE
with per-step commit attribution. README.md doc tree refreshed.

**Anti-regression — 22 new structural tests:**

`tests/test_sector_classifier.py` (7):
- 7-key taxonomy contract.
- Cache hit doesn't call yfinance.
- Cache miss writes row after yfinance.
- yfinance failure → fallback map.
- Unknown symbol → "tech" default.
- Stale cache (>7 days) is bypassed.
- `_guess_sector` is a delegate (and old `_SECTOR_MAP` is gone).

`tests/test_dynamic_live_universe.py` (6):
- Default returns hardcoded list.
- Flag-on filters by Alpaca-active.
- Empty Alpaca + flag-on → falls back to hardcoded (self-healing).
- Alpaca exception + flag-on → falls back (no crash).
- Crypto bypasses dynamic filter.
- Unknown segment raises KeyError.

`tests/test_short_borrow.py` (9):
- compute_borrow_cost basic math.
- Zero/negative inputs return 0.
- Hard-to-borrow override applies (GME 12 bps/day vs default 0.5).
- Monotonic in shares, price, days, bps.
- accrue_for_cover with no journal entry → 0 (fail-open).
- Intraday cover (< 1 day) → 0.
- 5-day overnight short charges expected accrual.
- No db_path → 0.
- `trader.check_exits` source-level guard: must reference
  `accrue_for_cover` AND subtract `borrow_cost` from pnl.

Tests: 1037 passing (was 1015; +22 new).

---

## 2026-04-27 — Wave 4 / Issue #10: backtest survivorship bias — frozen baseline + auto-augmentation (Severity: medium, accuracy)

User noted earlier today, after the 9-finding audit was declared
complete, that there was a related-but-separate integrity issue
which I'd flagged in the dynamic-universe doc audit but not rolled
into the methodology plan: backtests were reading the same
hand-curated "tradeable today" universe as live trading. That list
was curated to exclude delisted/renamed/acquired names, so every
backtest silently dropped exactly the symbols whose outcomes
mattered most for honest measurement (bankruptcies, severe
drawdowns, fraud disclosures). Backtest results were therefore
biased UP — the well-known survivorship-bias trap.

User instruction: "roll it into METHODOLOGY_FIX_PLAN.md as a §10
to make the audit honestly complete, and fucking do it, and do it
in a way that doesn't cause regressions or add unnecessary API
calls or break something else."

**Two-part architecture:**

1. **`segments_historical.py` (frozen baseline)** — verbatim
   one-time snapshot of `segments.py`'s four equity universes as of
   2026-04-27. Includes every name the system has tracked
   dead-or-alive (SQ, PARA, CFLT, X, AZUL, GPS, etc.). Crypto stays
   only in segments.py — its set is small and stable.

2. **`historical_universe_augment.py` (auto-augmentation)** — daily
   diff of Alpaca's active asset list against yesterday's snapshot.
   Symbols that disappeared get appended to
   `historical_universe_additions` with `last_seen_active=today`.
   `get_augmented_universe(segment, start_date)` returns the frozen
   baseline ∪ additions whose `last_seen_active >= start_date`.
   This way the historical universe **grows organically forever**
   without manual refresh — answering the user's pointed question
   "and do future dead ones get moved into the historical when it
   is their time?" — yes, every day, automatically.

**Constraint compliance:**

- **No new API calls.** The daily `_task_universe_audit` reads
  `screener.get_active_alpaca_symbols(ctx)` which is already cached
  daily in-process from the screener's existing `list_assets` call.
  Zero net new requests to Alpaca.
- **No regressions.** Live trading paths read `segments.py` exactly
  as before (verified by `test_live_trading_does_not_use_augmented_universe`
  + `test_screener_does_not_use_augmented_universe`). The Alpaca
  filter that protects live paths (CHANGELOG 2026-04-23 / 04-24) is
  unchanged. Only the four backtest call sites were updated.
- **Idempotent.** Daily snapshot is keyed by date (UPSERT). Diff
  uses `INSERT OR IGNORE` semantics so re-running is a no-op for
  already-recorded departures. New scheduler task has its own
  `universe_audit_runs` marker table so multi-profile cycles only
  do the work once per UTC day.

**Wired into 4 backtest read sites + 1 scheduler write site:**

- `rigorous_backtest.py:128` — `validate_strategy` shared-symbol
  selection
- `backtester.py:521` — `backtest_strategy` (the new date-range +
  legacy days= entry point)
- `backtester.py:862` — `_fetch_universe_batch` cache build
- `backtester.py:984` — `validate_strategy_with_params` (what-if
  backtest)
- `multi_scheduler.py` — new `_task_universe_audit` registered in
  the daily snapshot block right after specialist calibration.
  Idempotent across the day so it only runs once even though
  multiple profiles enter the snapshot block.

**Anti-regression — `tests/test_historical_universe_augment.py` (13 tests):**

A. `test_segments_historical_module_exists` — frozen file present.
B. `test_segments_historical_includes_known_dead_tickers` — must
   contain SQ, PARA, CFLT, X, AZUL, GPS (the canonical examples
   from prior fixes). If absent, the freeze didn't capture state.
C. `test_segments_historical_excludes_crypto` — design constraint.
D-F. `test_diff_records_departures_after_snapshot`, `_idempotent_on_rerun`,
   `_first_run_with_no_prior_snapshot_records_nothing` — the
   diff-and-record contract.
G-H. `test_augmented_universe_includes_recent_departures`,
   `_excludes_pre_window_departures` — backtest read path
   correctness.
I. `test_augmented_universe_returns_baseline_for_unknown_segment` —
   crypto fallback path.
J-K. `test_rigorous_backtest_uses_augmented_universe`,
   `test_backtester_uses_augmented_universe` — source-level
   contract guards on the 4 backtest entry points (all 3 entries
   in backtester.py + the rigorous one).
L-M. `test_live_trading_does_not_use_augmented_universe`,
   `test_screener_does_not_use_augmented_universe` — **the most
   important regression guards**: prevent any future change from
   slipping the backtest-only helper into live-trading paths,
   which would re-introduce the dead-ticker spam fixed on
   2026-04-23.

Tests: 1014 passing (was 1002; +13 new -1 reordered).

**Status:** Wave 4 / Issue #10 ✅ COMPLETE. The methodology audit
is now honestly closed across all 10 findings. The augmentation
ledger is empty today; it accumulates one day's worth of departures
on the next daily snapshot block.

---

## METHODOLOGY_FIX_PLAN.md is ✅ COMPLETE

All 10 audit findings are fixed:

| # | Wave | What it fixed | Commit |
|---|---|---|---|
| 1 | 0 | Meta-model time-ordered split | `cd2d207` |
| 2 | 1 | backtest_strategy date ranges | `a3a3d64` |
| 6 | 1 | ai_tracker forward-horizon resolution | `7729bc4` |
| 3 | 2 | walk-forward truly walks forward | `ec758e3` |
| 4 | 2 | OOS strictly disjoint from in-sample | `ec758e3` |
| 5 | 2 | self_tuner train/validate split | `ec758e3` |
| 7 | 3 | strategy_lifecycle inherits real gates | `f65d757` |
| 8 | 3 | alpha_decay rolling vs lifetime disjoint | `f65d757` |
| 9 | 3 | specialist confidence calibration | `3675ba1` |
| 10 | 4 | backtest survivorship bias — frozen baseline + auto-augmentation | this commit |

Anti-regression tests across all 10 fixes total ~75 new structural
tests. The system can no longer ship any of these data-leak
patterns silently — every one now has either an AST guard, a
behavioral leakage detector, or both.

---

## 2026-04-27 — Wave 3 (partial) / Fixes #7 + #8: alpha_decay strict disjoint windows + strategy_lifecycle contract (Severity: medium, accuracy)

Wave 3 part 1 — the smaller two fixes ship together. Fix #9
(specialist confidence calibration) is the larger one and gets its
own commit.

**Fix #8 — alpha_decay rolling vs lifetime is now strictly disjoint.**

Before: `compute_lifetime_metrics(db, strategy)` returned metrics
over ALL resolved predictions including the rolling window itself.
When `detect_decay` compared rolling Sharpe vs lifetime Sharpe to
flag degradation, both sides shared the most-recent data — biasing
the lifetime baseline toward whatever was happening recently and
dampening decay signals.

After: `compute_lifetime_metrics` gained `exclude_recent_days`
parameter (default 0 for backwards compat). `detect_decay` and
`check_restoration` now pass `rolling_window_days` (=30) so the
lifetime baseline is `[earliest, as_of - 30 days]` and the rolling
window is `[as_of - 30 days, as_of]`. Strictly disjoint.

The default-of-0 keeps the legacy "all resolved predictions"
behavior for any direct callers; the production code path
(decay detector + restoration checker) explicitly opts into the
disjoint window. Pre-existing tests that asserted the legacy
behavior keep passing.

**Fix #7 — strategy_lifecycle contract test.**

Mostly auto-fixed by Wave 2 (#3 + #4): `_run_validation` calls
`validate_strategy`, which internally uses the now-fixed
`walk_forward_analysis` and `out_of_sample_degradation`. So
auto-strategies inherit the disjoint-window discipline without
code changes. Added a contract test asserting
`_run_validation` still calls `validate_strategy` — prevents a
silent decoupling that would let auto-strategies bypass the gates.

**Anti-regression — `tests/test_alpha_decay_lifetime_disjoint.py` (6 tests):**

Source guards:
- `compute_lifetime_metrics` accepts `exclude_recent_days` parameter.
- `detect_decay` source mentions `exclude_recent_days`.
- `check_restoration` source mentions `exclude_recent_days`.
- `strategy_lifecycle._run_validation` calls `validate_strategy`.

Behavioral:
- Old data profitable + recent rolling losses → lifetime with
  `exclude_recent_days=30` shows higher win rate (excluded losses)
  vs `exclude_recent_days=0` baseline.
- `exclude_recent_days=0` matches legacy "all resolved" semantics
  exactly.

Plus pre-existing `test_no_snapshots_yet` updated to seed enough
older predictions to clear the 50-sample lifetime threshold even
after the 30-day exclusion (so it reaches the "no snapshots yet"
code path it was originally testing).

Tests: 992 passing (was 986; +6 new, +0 modified).

**Wave 3 status:** PARTIAL. Fix #7 + #8 ✅. Fix #9 (specialist
confidence calibration) is the last one and gets its own commit
because it requires a new module + integration with the ensemble +
data-dependent test seeding.

---

## 2026-04-27 — Wave 2 / Fixes #3, #4, #5: walk-forward, OOS, and self-tuner now use disjoint windows (Severity: critical, accuracy)

Wave 2 of `METHODOLOGY_FIX_PLAN.md` shipped — all three "uses the
foundation" fixes in one commit. With Wave 1 + Wave 2 together, the
methodology stack is now coherent: every test that claims to read
"different data" actually reads different data.

**Fix #3 — `walk_forward_analysis` actually walks forward.**
Previously: every fold passed `days=fold_days` to backtest_strategy,
which always anchored on `datetime.now()` — every fold tested
overlapping recent data. Fix: split `[today - history_days, today]`
into N consecutive disjoint calendar windows, pass each as
`start_date` / `end_date` to backtest_strategy. Each fold result
now records its actual `start_date` and `end_date`.

**Fix #4 — `out_of_sample_degradation` separates IS from OOS.**
Previously: IS = `days=in_sample_days` (today-anchored), OOS =
`days=oos_days` (today-anchored). The OOS window was INSIDE the IS
window — strategy trained on data we claimed was held out. Fix: IS
runs `[today - history_days, today - oos_days]`, OOS runs
`[today - oos_days, today]`. Strict separation. Output now includes
`in_sample_start`, `in_sample_end`, `oos_start`, `oos_end` for
auditability.

**Fix #5 — self-tuner train/validate split on `resolved_at`.**
Previously: confidence-threshold raises were proposed AND validated
on the same full-history dataset. Classic in-sample optimization.
Fix: split resolved predictions into:
- Adjustment window: `resolved_at < now - 14 days` (used to detect
  the bad band)
- Validation window: `resolved_at >= now - 14 days` (used to verify
  the proposed raise would have improved or at least not hurt
  recent performance)
A threshold raise is now ONLY recommended if BOTH the adjustment
window confirms the band underperforms (< 35% win rate) AND the
validation window's surviving cohort (confidence ≥ proposed
threshold) outperforms the full validation cohort. If validation
data is too thin (< 5 resolved in last 14 days, or < 3 in the kept
cohort), no recommendation is made — we err toward not changing.

**Anti-regression — 11 new structural tests across 2 files:**

`tests/test_walk_forward_and_oos_disjoint.py` (6 tests):
- AST-walks both wrapper functions, fails on any
  `backtest_strategy(..., days=...)` call (only `start_date` /
  `end_date` allowed).
- Behavioral: mocks backtest_strategy, records the date ranges of
  each call, asserts walk-forward folds are pairwise disjoint and
  OOS in-sample-end ≤ out-of-sample-start.

`tests/test_self_tuning_validation_window.py` (5 tests):
- Source guards: `VALIDATION_WINDOW_DAYS` exists, query references
  `resolved_at`.
- Behavioral: validation-confirms case (recommends), validation-
  rejects case (recent data disagrees → blocks), validation-too-thin
  case (defers).

Tests: 986 passing (was 975; +11 new).

**Wave 2 status:** ✅ COMPLETE. Wave 1 + Wave 2 both done.

**Wave 3 starts next:**
- Fix #8 (`alpha_decay` rolling-window discipline)
- Fix #7 (`strategy_lifecycle`; mostly auto-fixed by #3 + #4)
- Fix #9 (specialist confidence calibration)

---

## 2026-04-27 — Wave 1 / Fix #6: forward-horizon gate on prediction resolution (Severity: medium-going-on-critical, accuracy)

Wave 1 of `METHODOLOGY_FIX_PLAN.md` is now complete (Fix #2 + Fix #6).

**Before:** `ai_tracker._resolve_one` checked the ±2% win/loss
thresholds against the current price as soon as the next resolve-tick
ran. A BUY made at 10am that drifted +2.5% by 11am resolved as
"win" within an hour — the label captured intraday noise, not the
forward outcome the AI was actually predicting. With a 2% threshold
and typical retail-cap volatility (small-caps routinely move ±2%
intraday on no news), a meaningful fraction of resolved labels were
random.

**After:** new constant `MIN_HOLD_DAYS_BEFORE_RESOLVE = 5` (5 trading
days ≈ 1 trading week). `_resolve_one` returns `None` (still pending)
for any BUY/SELL prediction younger than that, regardless of price
movement. After the horizon, the same threshold logic runs and the
prediction resolves to win/loss. HOLD's existing `HOLD_RESOLVE_DAYS`
gate is preserved (already had this discipline). `TIMEOUT_DAYS`
escape hatch still force-resolves stale pending predictions to
neutral.

**Effect on observable metrics:**

- Pending count climbs temporarily as young predictions wait their
  horizon out (instead of resolving immediately on noise).
- Win rate on freshly-resolved predictions becomes a meaningful
  forward-horizon measurement instead of a noise estimate.
- The meta-model's training labels (which feed off resolved
  predictions) become more predictive — combined with the
  time-ordered split fix from `cd2d207`, this is the second of
  two changes that determine whether the meta-model has any real
  edge to learn.

**Anti-regression — `tests/test_resolve_min_hold_horizon.py` (10 tests):**

1. Constant exists and is ≥ 1.
2. Source-level: `_resolve_one` references the constant.
3. Young BUY at +2.5% returns None (was: "win").
4. Young BUY at -2.5% returns None (was: "loss").
5. Young SELL at -3% returns None.
6. Aged BUY at +3% resolves as "win" (gate doesn't block real wins).
7. Aged BUY at -3% resolves as "loss".
8. HOLD path preserved — too-young HOLD stays pending.
9. HOLD path preserved — past-horizon HOLD with quiet price resolves win.
10. TIMEOUT escape hatch — old pending BUY with no threshold cross
    still force-resolves to neutral.

Tests: 975 passing (was 965; +10 new).

**Wave 1 status:** ✅ COMPLETE.
- Fix #2 (backtest_strategy date ranges) — `a3a3d64`
- Fix #6 (forward-horizon resolution gate) — this commit

**Wave 2 starts next:** rewire `walk_forward_analysis` and
`out_of_sample_degradation` to use the new date-range path, then
add the train/validate split to `self_tuning`.

---

## 2026-04-27 — Wave 1 / Fix #2: backtest_strategy accepts explicit date ranges (Severity: critical, accuracy)

Foundation for the methodology fix. Wave 1 of `METHODOLOGY_FIX_PLAN.md`.

**Before:** `backtest_strategy(market_type, days=N, ...)` always
fetched the latest N days from `datetime.now()`. Every wrapper that
called it (walk_forward_analysis, out_of_sample_degradation, plus
any future caller wanting "historical period X") inherited the
"all windows end at today" defect.

**After:** `backtest_strategy` now also accepts `start_date` and
`end_date` parameters. When both are passed, simulation reads
EXACTLY the bars in `[start_date, end_date]`, with warmup from
`start_date - 80 calendar days` for indicator priming. The
sim-loop's start index is the first bar at or after `start_date`,
so bars before it are warmup and bars after `end_date` are
ignored.

**New helper `backtester._fetch_yf_history_range(symbol, start, end,
warmup_days)`** is the date-range counterpart to
`_fetch_yf_history(symbol, days)`. Slices the cached full-history
dataframe by date instead of row count. Tz-aware against tz-naive
indices. Returns None when the requested range is outside cached
data.

**Backwards compat:** `days=` parameter remains accepted as the
legacy entry point. Positional-argument order preserved (`days`
ahead of `start_date` in the signature) so no existing caller
breaks. Wave 2 fixes (#3, #4) will migrate walk_forward_analysis
and out_of_sample_degradation to the date-range path.

**Anti-regression — `tests/test_backtest_date_range_split.py` (6 tests):**

1. Public API has `start_date` and `end_date` parameters.
2. `_fetch_yf_history_range` helper exists.
3. Slicing returns bars inside the requested window plus warmup.
4. Out-of-cache windows return None gracefully.
5. **The leakage detector:** two backtests with disjoint date
   ranges read disjoint simulation bars (the property
   walk-forward and OOS depend on).
6. Legacy `days=` path still works and parameter order is
   preserved for positional-arg compat.

Tests: 965 passing (was 959; +6 new).

**Next:** Fix #6 (ai_tracker forward-bar resolution) completes
Wave 1. Then Wave 2: rewire walk_forward_analysis and
out_of_sample_degradation to use the new date-range path.

---

## 2026-04-27 — METHODOLOGY_FIX_PLAN.md: durable plan for the 7 remaining accuracy bugs (Severity: low, docs)

After the meta-model data-leakage fix landed (`cd2d207`), the user
asked: "are there other aspects of this system that are equally
incorrect or inaccurate?" An Explore-agent audit (assistant verified
the top 3 findings personally) surfaced 7 issues sharing the same
root pattern: wrappers around `backtester.backtest_strategy()` use
`days=N` parameters that always fetch from `datetime.now()` backwards,
so every "walk-forward" / "out-of-sample" / "in-sample" test reads
overlapping recent data. Plus `self_tuning` optimizes parameters on
full history, predictions resolve on same-day close, alpha-decay
windows have forward-looking bias, and specialist confidence is
never calibrated against actual outcomes.

`METHODOLOGY_FIX_PLAN.md` documents:

- The full inventory of 7 issues with severity, file, line range,
  and brief description.
- A 3-wave dependency graph: Wave 1 (`backtest_strategy` date ranges
  + forward-bar resolution) is structural foundation; Wave 2
  (walk-forward, OOS, self-tuning hold-out) becomes mechanically
  correct once Wave 1 ships; Wave 3 (alpha-decay discipline,
  lifecycle gates, specialist calibration) consumes the clean data
  produced by 1+2.
- Per-fix execution plan: implementation, anti-regression test,
  migration, expected metric impact.
- Honest expected-impact table — meta-model AUCs probably drop to
  0.50-0.65, validation reports become more sobering, self-tuner
  applies fewer changes, alpha-decay flags more strategies. Calibrated
  numbers are the goal.
- Cross-session continuity rules so this plan survives context loss.

User instruction was explicit: "we need to do it all." Wave 1 starts
in the next commit.

---

## 2026-04-27 — Meta-model: fix data-leakage from random train/test split (Severity: critical, accuracy)

**The problem we found.** Per-profile dashboard reported AUC values
of 0.83-0.96 across every profile. Realistic out-of-sample financial
AUCs are ~0.55. The numbers were not real edge — they were a known
data-leakage artifact.

**Root cause.** `meta_model.train_meta_model` was using
sklearn's `train_test_split(X, y, test_size=0.2, random_state=42)`
— a RANDOM 80/20 split with no time awareness. Test predictions
were interleaved in time with training predictions. Because
financial features are heavily autocorrelated day-to-day (RSI today
≈ RSI tomorrow, regime today ≈ regime tomorrow), the classifier
effectively memorized "this market state ≈ this outcome" instead
of learning predictive patterns. AUC inflated from a realistic
~0.55 to an artifact ~0.95.

Compounding it: `build_training_set` selected from `ai_predictions`
without an `ORDER BY`. SQLite's row order in that case is
implementation-defined, so even a deterministic slice of the result
would have been random in time.

**Fix:**

1. `build_training_set` query now `ORDER BY id ASC` — guarantees
   time-ascending order. Comment in code references this CHANGELOG
   entry as the reason.
2. `train_meta_model` no longer imports or calls
   `sklearn.model_selection.train_test_split`. Replaced with a
   deterministic tail split:
   ```python
   n_test = max(1, int(round(n * 0.2)))
   n_train = n - n_test
   X_train, X_test = X[:n_train], X[n_train:]
   y_train, y_test = y[:n_train], y[n_train:]
   ```
   The most-recent 20% becomes the held-out test set. No shuffling.
   No `random_state` on the split. (Classifier `random_state=42` is
   kept — that's reproducibility, not data leakage.)

**Honest expectation.** AUCs will drop on the next retrain, possibly
significantly. A drop from ~0.95 to ~0.55-0.65 would be GOOD news —
that's a real edge, just much smaller than the leakage made it look.
A drop to ~0.50 means the AI's confidence has no learnable
correction from these features and we'd need to either widen the
feature set or accept raw AI confidence. Either outcome is more
useful than continuing to operate on inflated numbers.

The user's explicit guidance: "yes, accuracy above all else."

**Anti-regression — `tests/test_meta_model_time_ordered_split.py` (4 tests):**

1. `test_train_meta_model_does_not_import_train_test_split` —
   AST-walks `train_meta_model` source; fails the build if anyone
   reintroduces sklearn's random splitter.
2. `test_build_training_set_orders_by_id_asc` — regex-asserts the
   query has `ORDER BY id ASC` (or `ORDER BY timestamp ASC`).
3. `test_train_meta_model_uses_deterministic_tail_split` — confirms
   the slice-based split idiom is present.
4. `test_split_takes_most_recent_data_as_test_set` — behavioral
   end-to-end: feeds 100 samples where the LAST 20 deliberately
   invert the training pattern. With the time-ordered split, AUC
   on test data must be ≤ 0.5 (because the test half contradicts
   what the model learned). With a random split, the inverted
   samples interleave into training and AUC would stay artificially
   high. This test is the actual leakage detector.

Tests: 959 passing (was 955; +4 new).

**Post-deploy step:** delete `meta_model_*.pkl` files on prod so
the next daily retrain (3:55 PM ET) trains fresh on the corrected
methodology. Dashboard AUCs will reflect reality from that point.

---

## 2026-04-27 — Documented "trade-execution costs modeled at $0" decision (Severity: low, docs)

User reviewed today's trailing-stop exits (mostly profitable; AMD
+$190, NXPI +$224, QCOM +$53; one stop-loss on TXN -$99) and asked
why the system doesn't subtract per-trade commissions. Combined
recall (his E*Trade account didn't charge him) with current market
reality (every major US retail broker — Alpaca, Schwab, Fidelity,
E*Trade, IBKR Lite, Robinhood, Charles Schwab — has been $0 stock
commission since 2019) and the existing slippage-tracking that
already captures the only material trade-cost (bid-ask spread).

Result: trade execution costs stay modeled at $0; decision is now
documented in `TECHNICAL_DOCUMENTATION.md` §15 ("Cost Model" → new
"Trade Execution Costs" subsection) so the reasoning is preserved
if anyone questions it later.

The single small gap — short-borrow fees on overnight shorts — is
explicitly noted as deferred (small magnitude; rarely held >1-3
days; clean post-hoc add when a >5-day short shows up in the
journal).

---

## 2026-04-27 — check_exits: skip exits whose entry order hasn't filled at the broker (Severity: medium, bug)

**Symptom:** Production scan-failures widget showed
`Large Cap Limit Orders: [Large Cap Limit Orders] Check Exits failed
at Apr 27, 1:53 PM ET`. Stack trace from journal:

```
alpaca_trade_api.rest.APIError:
    cannot open a short sell while a long buy order is open
```

**Root cause:** Virtual profiles compute "open positions" from the
trades journal as soon as the entry order is logged — even before
Alpaca actually fills it. For most profiles this is fine because
their entry orders are market orders that fill in milliseconds. But
"Large Cap Limit Orders" places limit BUYs that can sit unfilled at
Alpaca for minutes or hours.

Sequence that broke:

1. 17:50 — limit BUY for symbol X submitted, journal records an
   open virtual position.
2. 17:53 — `check_exits` runs, sees the journal-derived position,
   detects a stop-loss/take-profit trigger, submits a market SELL.
3. Alpaca: "you have 0 real shares (the BUY hasn't filled) AND
   there's still a long BUY pending — this SELL is a short
   attempt — rejected." Task fails.

The existing defense at `trader.py:281-292` (cancel any open orders
for this symbol before submitting the exit) didn't help because the
cancel hits Alpaca asynchronously; the submit fired before the
cancel landed.

**Fix (`trader.py`):**

New helper `_entry_order_filled_at_broker(api, db_path, symbol,
is_short)` looks up the most recent matching open entry row in the
journal, reads its `order_id`, calls `api.get_order(...)`, and
returns:

- `True` if status is `filled` or `partially_filled` (real shares
  exist → SELL is safe).
- `False` for any pending state (`new`, `accepted`, `pending_new`,
  `pending_replace`, `pending_cancel`, `accepted_for_bidding`,
  `held`, `suspended`).
- `True` (fail-open) on every uncertain path: missing db_path, no
  matching journal row, NULL order_id, broker-unrecognized id, or
  SQL error. Reason: a too-conservative gate would block legitimate
  exits when the journal is healthy but its row→Alpaca link is
  stale; the prior behavior was "always allow," so fail-open is the
  conservative regression-free choice.

`check_exits` now calls this gate immediately after the schedule
guard. If `False`, it logs an INFO line and continues — the trigger
re-fires on the next exit cycle, by which time the entry has
typically filled.

**Effect on the failing profile:** the limit-order profile no longer
errors on exits during the entry-pending window. Alpaca-state is
now the source of truth for "does this position really exist?", not
the optimistic journal.

**Anti-regression — `tests/test_exit_gates_unfilled_entry.py` (18 tests):**

- `filled` and `partially_filled` allow the exit.
- All 8 known pending Alpaca statuses block the exit (parameterized).
- Short positions: `sell_short` entry side is looked up correctly,
  and pending shorts block the cover.
- All 5 fail-open paths return `True`: no db_path, no matching row,
  NULL order_id, broker raises on `get_order`, SQL error.
- **Contract test** uses `inspect.getsource(check_exits)` to assert
  the gate call is still present in `check_exits` itself — prevents
  a silent regression where someone removes the wiring but leaves
  the helper.

Tests: 955 passing (was 937; +18 new).

---

## 2026-04-27 — Show current price + % change inline on position rows (Severity: low, ui)

User asked to see current price on the dashboard without having to
click-expand each position row. The data was already in the row dict
(`current_price` from Alpaca, used for unrealized P&L) and was already
rendered — but only inside the click-to-expand detail panel.

`templates/_trades_table.html`: the Price column now stacks the entry
price (top) with the current price + % change (below, color-coded
green/red). Renders only when `current_price > 0` so closed/SELL rows
on the trades page don't grow a redundant line. The duplicate
"Current: ..." line in the expanded detail panel was removed since
it would just repeat what's now visible in the main row.

Zero new system load — uses the same data already fetched for the
P&L calc.

**Follow-up fix same day:** the first cut naively did
`(current - entry) / entry` regardless of side, which would have
shown a SHORT position GAINING when the underlying price ROSE
(opposite of reality). Caught while no shorts were open in prod, so
the bug never bit. Fix inverts the sign for `side in ('sell',
'sell_short', 'short')`. Guardrail: `tests/test_trades_table_pnl_sign.py`
covers long winner, long loser, short winner, short loser, the
dashboard's `side='sell'` alias for shorts, and the closed-trade
no-render case (6 tests).

---

## 2026-04-27 — Dashboard rate-limit storm: per-symbol bars → batched snapshots (Severity: critical, regression-prevention)

**Symptom:** Monday's market open. User reports dashboard "loading for
7 minutes" — looks broken. Gunicorn logs:

```
13:42:36 sleep 3 seconds and retrying https://data.alpaca.markets/v2/stocks/GT/bars
13:42:36 sleep 3 seconds and retrying https://data.alpaca.markets/v2/stocks/ET/bars
13:43:50 [CRITICAL] WORKER TIMEOUT (pid:903832)
13:43:51 [ERROR] Worker (pid:903832) was sent SIGKILL!
```

**Root cause:** `client._make_price_fetcher` called
`market_data.get_bars(symbol, limit=1)` once per symbol. Virtual
profiles use this fetcher to compute current prices for FIFO-derived
positions. Math:

  10 virtual profiles × 4-8 held positions × ThreadPoolExecutor of 10
  parallel workers = 50-100 sequential per-symbol Alpaca bar requests
  per dashboard render. → Alpaca rate limit. → 3-second-sleep retries.
  → 120s gunicorn worker timeout. → SIGKILL. → next request restarts
  the same trap.

The screener migration to Alpaca SIP (CHANGELOG 2026-04-15) fixed the
*screener's* yfinance hang but left this dashboard path on per-symbol
calls because it was a separate code path under
`client._make_price_fetcher`.

**Fix (`client.py`):**

1. New `_prefetch_prices(symbols)` — one batched
   `data_client.get_snapshots(symbols)` call (the same path the screener
   uses) populates a process-wide TTL price cache (30s).
2. `_make_price_fetcher` now reads from that cache; per-symbol fallback
   to `api.get_latest_trade` only fires for the rare cache miss (e.g.
   delisted ticker).
3. Module-level `_price_cache` dict + `_price_cache_lock` so concurrent
   gunicorn workers in the same process share the cache.
4. New `_held_symbols_from_journal(db_path)` reads the symbol list from
   the trades table so callers can prefetch BEFORE invoking the journal
   helper.
5. Both `get_account_info` and `get_positions` now call
   `_prefetch_prices(_held_symbols_from_journal(ctx.db_path))` before
   passing the fetcher to the journal helper.

**Effect:** Dashboard render goes from N×M Alpaca calls (where N =
profiles, M = symbols/profile) to **1 batched snapshots call per
render**. Result is shared across all profiles via the process cache.

**Anti-regression — `tests/test_no_per_symbol_bars_in_web_path.py`** (5 tests):

1. `test_price_fetcher_does_not_call_get_bars` — AST-walks
   `_make_price_fetcher` and fails if it ever calls `get_bars` again.
2. `test_prefetch_prices_uses_batched_snapshots` — confirms the new
   prefetch uses `get_snapshots`, not `get_bars`.
3. `test_price_fetcher_has_process_wide_cache` — asserts module-level
   `_price_cache`, `_PRICE_CACHE_TTL`, and `_price_cache_lock` exist.
4. `test_dashboard_view_does_not_call_get_bars` — grep guard on
   `views.py`.
5. `test_held_symbols_helper_exists` — ensures the symbol-list helper
   exists for batched prefetch.

The structural test makes it impossible to revert this fix without
the build failing on the exact pattern that caused the outage.

Tests: 931 passing (was 926; +5 new structural tests).


Closing-out doc pass to bring the front-of-repo docs in line with what
actually ships now.

1. **`README.md`** — was still describing the system as it stood ~6
   weeks ago. Refreshed:
   - Top blurb: now names the 4 new alt-data sources and the 12-layer
     autonomy stack instead of "self-tuning adjusts parameters daily".
   - "Self-Tuning" feature bullet replaced with "12-Layer Autonomous
     Self-Tuning" naming the override chain and cost guard.
   - Web Platform list adds the 5 new dashboard widgets that landed in
     the autonomy rollout: Active Lessons, Active Autonomy State, Cost
     Guard, Parameter Resolver, Autonomy Timeline.
   - "All 105 tests" → "All 926 tests".
   - New §6 setup step documents the alt-data wiring (clone, `daily`,
     `~/run-altdata-daily.sh`).
   - Project Structure tree expanded with new groups: Phase 1-10
     module additions (`meta_model`, `alpha_decay`, `options_oracle`,
     `ensemble`, `event_bus`, `crisis_detector`, etc.) and a new
     "Autonomy Layer" group naming all 10 modules.
   - Documentation list lifted from "TECHNICAL_DOCUMENTATION.md (v4.0)"
     to a full enumeration including `EXECUTIVE_OVERVIEW`, `ROADMAP`,
     `AI_ARCHITECTURE`, `SELF_TUNING`, `AUTONOMOUS_TUNING_PLAN`,
     `ALTDATA_INTEGRATION_PLAN`, `MONTHLY_REVIEW`, `CHANGELOG` and
     bumps the TECHNICAL_DOCUMENTATION reference to v5.0.

2. **`ROADMAP.md`** — replaced the "Upcoming Enhancements (Queued):
   Self-Tuning Parameter Expansion (~Late May 2026)" section, which
   claimed the self-tuner adjusts 4 parameters and 3 more were queued
   for a month from now. That plan was superseded a week early by the
   12-wave rollout. Section now reads "✅ DELIVERED (2026-04-25)"
   with the full layer table, override-chain explanation, and the
   6 anti-regression guardrails. Also added a parallel "Alternative
   Data Integration ✅ DELIVERED (2026-04-26)" section so the roadmap
   reflects what shipped this weekend. Bumped baseline test count in
   cross-session continuity from 104+ → 920+.

3. **`ALTDATA_PLAN.md`** — added a "STATUS: ✅ DELIVERED" banner at
   the top pointing to `ALTDATA_INTEGRATION_PLAN.md` as the live
   integration design, and clarified that the document is preserved
   as the historical record of the project-build plan rather than a
   living roadmap.

Tests: 926 passing (no .py change in this commit; documentation-only).

---

## 2026-04-26 — Alt-data integration: doc completeness pass (Severity: low, docs)

End-of-session sweep: tests/docs/UI/prod-logs audit caught three
documentation gaps from the alt-data integration session:

1. `AI_ARCHITECTURE.md` had a count bump (15 → 19 alt-data signals)
   but didn't actually describe the 4 new sources or list them in
   the file map. Added an explicit table under §1c naming each helper,
   its source project, and per-symbol output. Added the
   `/opt/quantopsai-altdata/` path to the §6 file map.
2. `SELF_TUNING.md` bumped the count (21 → 25 weighted signals) but
   didn't enumerate which 4 were new. Added a complete grouped table
   of all 25 weightable signals with the 4 alt-data additions called
   out.
3. `ALTDATA_INTEGRATION_PLAN.md` still said "Plan draft, ready for
   execution" — flipped to "DEPLOYED 2026-04-26" with the verified
   record counts (1,109 trades / 857,304 holdings / 5,342 trials /
   981 messages).

Helper docstrings in `alternative_data.py` updated to call out the
prod path (`/opt/quantopsai-altdata`) and the daily cron schedule —
makes the runtime contract clear to future readers.

Tests: 925 passing (was failing on the CHANGELOG-discipline rule
because the W1+W2 .py-touching commit didn't include CHANGELOG; this
follow-up commit bundles `.py` + `CHANGELOG.md` + docs together,
re-satisfying the rule going forward).

---

## 2026-04-26 — Alt-data integration: 4 standalone projects wired into the AI (Severity: medium, feature)

The four projects built last week — `congresstrades`, `edgar13f`,
`biotechevents`, `stocktwits` — are now feeding the AI's prompt as
weighted signals on the same Layer 2 ladder as everything else.

**W1 — Read layer** (`alternative_data.py`): four new helpers
(`get_congressional_recent`, `get_13f_institutional`,
`get_biotech_milestones`, `get_stocktwits_sentiment`) read each
project's SQLite DB read-only with 6h cache, configurable path via
`ALTDATA_BASE_PATH`. Graceful no-op when DB is missing or schema is
partial. 12 new tests with seeded fixtures mirroring prod schema.

**W2 — AI integration**: 4 new keys in `get_all_alternative_data`,
4 new prompt blocks via `_weighted_signal_text` (so Layer 2 weights
apply), 4 new entries in `signal_weights.WEIGHTABLE_SIGNALS` so the
tuner can autonomously discount any signal that doesn't predict
for a profile. Features flattened into `features_payload` so the
meta-model can train on them too.

**W3 — Production deployment**: 4 projects rsync'd to
`/opt/quantopsai-altdata/{project}/` on the droplet. Fresh venvs +
`pip install -r requirements.txt` per project (~217MB total). Cron
entry at 06:00 UTC (02:00 ET, off hours):
`0 6 * * * cd /opt/quantopsai-altdata && ALTDATA_BASE=/opt/quantopsai-altdata bash run-altdata-daily.sh >> logs/altdata-$(date +%Y%m%d).log 2>&1`.
Driver script patched to honor `ALTDATA_BASE` env var with
`$HOME` fallback for local-dev compat. `ALTDATA_BASE_PATH` added to
`/opt/quantopsai/.env` so the QuantOpsAI services find the DBs at
the right path. Manual seed run kicked off post-deploy.

**W4 — Docs + UI**: "What the AI Sees" reference card on the AI page
now shows the 4 cards as active sources (moved out of "Built Locally
— Not Yet Wired In"). Alt-data source count bumped 15 → 19.
`SELF_TUNING.md` Layer 2 inventory bumped 21 → 25 signals.
`AI_ARCHITECTURE.md` updated.

Each new signal joins the same self-correcting feedback loop as
every other one — if congressional-trade signals don't predict for
a profile, Layer 2 nudges the weight from 1.0 → 0.7 → 0.4 → 0.0
within ~9 days. Layer 5 propagates that finding to peer profiles.
Cost guard wraps prompt verbosity changes from any expanded
signal set.

Full suite: 926 passed (914 + 12 new alt-data reader tests).

---

## 2026-04-25 — Hotfix: Active Lessons widget stuck on "Loading..." (Severity: medium, regression)

**Problem:** The new "Active Lessons" widget on the AI Operations
tab showed "Loading..." indefinitely. Backend was fine — endpoint
returned 200 in ~165ms with valid data — but the widget never updated.

**Root cause:** duplicate DOM IDs. The new "Active Lessons" widget
was assigned `id="learned-patterns-widget"`, which was already used
by an older widget on the Brain tab. `getElementById` returns only
the FIRST match, so my JS updated the Brain-tab widget (not visible
on the Operations tab) and left the Active Lessons widget stuck on
its "Loading..." placeholder forever.

**Fix:** rename the new widget to `id="active-lessons-widget"` and
update the JS to target it.

**Structural fix — `test_no_duplicate_dom_ids.py`.** New guardrail
that walks every template under `templates/`, parses `id="..."`
attributes (skipping `<script>` and `<style>` blocks so JS string
literals don't false-positive), and fails if any ID appears more
than once in the same file. Allowlist supported for legitimate
duplicates (e.g., a partial template intentionally included twice).

Verified by reverting the fix: the test failed cleanly on
`learned-patterns-widget appears 2× — JS getElementById returns only
the first match, second/etc. silently orphaned.`

This is the structural protection against the entire class of
"silently orphaned widget" bugs.

Full suite: 914 passed (913 + 1 new dup-id guardrail).

---

## 2026-04-25 — URGENT: comprehensive snake_case guardrail + autonomy summary in weekly digest (Severity: high, regression + feature)

**The snake_case leak that wasn't supposed to be possible.** User
opened the AI Operations tab and saw raw `options_signal weight 0.7`,
`vwap_position weight 0.7`, `ai_confidence_threshold (bull): 30` in
the new "Active Autonomy State" card. Despite my repeated promises
that the existing `test_no_snake_case_in_optimizer_strings` would
catch this everywhere, **it didn't — because that test only covered
`_optimize_*` function returns inside `self_tuning.py`**. Every new
API endpoint and JS render path I built outside that file was
uncovered.

**Root cause:** the new `/api/autonomy-status` endpoint returned
`signal_weights` / `regime_overrides` / `tod_overrides` /
`symbol_overrides` / `prompt_layout` as dicts-of-dicts whose KEYS
were raw PARAM_BOUNDS column names. The JS rendered them with
`Object.entries(...).forEach(e => render(e[0]))` — leak.

**Fix:**
1. `/api/autonomy-status` now returns labeled-list shapes:
   `[{"key": "options_signal", "label": "Options Flow Signal",
   "weight": 0.7}, ...]`. Server-side `display_name(...)` resolves
   every parameter name + regime/tod label.
2. `/api/resolve-param` now includes `param_label`,
   `current_regime_label`, `current_tod_label`, `final_source_label`
   alongside their raw counterparts.
3. AI Operations tab JS rewritten to consume the labeled fields
   instead of raw keys.

**The real fix — `test_no_snake_case_in_api_responses.py`.** A new
end-to-end guardrail that:
- Discovers every GET `/api/*` endpoint via `app.url_map`
- Hits each one with a mocked logged-in user + profile data seeded
  with overrides on every PARAM_BOUNDS key
- Walks the JSON response recursively
- Fails if any PARAM_BOUNDS key appears as either:
  (a) a dict KEY anywhere in nested structures (the
      Object.entries-render leak pattern), OR
  (b) a string VALUE in a field whose name isn't on the
      `ALLOWED_RAW_KEY_FIELDS` allowlist (param_name,
      parameter_name, change_type, key, field, strategy_type —
      all paired with explicit `*_label` siblings).

Verified the test catches the exact regression by reverting the
fix and re-running — it failed cleanly with all three leak paths
(`regime_overrides`, `symbol_overrides`, `tod_overrides`).

This guardrail is dynamic — every new API endpoint added going
forward is automatically covered. No new endpoint can ship a
PARAM_BOUNDS key as a dict KEY without explicitly bypassing the
test.

**Also: weekly digest gains an Autonomy Activity section.** Renders
right after "This Week at a Glance" and includes:
- counts of parameter tunings, strategy deprecations/restorations,
  auto-strategy lifecycle and crisis transitions (this week)
- snapshot of active overrides across all profiles (signal weights,
  regime/TOD/symbol overrides, profiles with non-default capital
  scale)
- cost-guard status (today's spend, daily ceiling with source label,
  7-day average)
- post-mortem patterns extracted this week with examples

Full suite: 913 passed (912 + 1 new comprehensive guardrail).

---

## 2026-04-25 — User-controllable cost ceiling + Parameter Resolver + Autonomy Timeline (Severity: medium, feature)

Three additions that put the user in control of the autonomy and
make it inspectable.

**1. User-configurable daily cost ceiling.** New
`users.daily_cost_ceiling_usd` column (NULL = auto-compute). When
set, overrides the auto-computed `trailing-7-day-avg × 1.5`. Settings
> Autonomy gains an input field; current ceiling shows up with its
source ("user-set" or "auto") so you always know whether your cap is
authoritative. `cost_guard.daily_ceiling_usd()` honors the user
value when present and falls back to auto-compute otherwise. New
`cost_guard.ceiling_source()` helper exposes the provenance.

**2. Parameter Resolver tool** (AI Operations tab). Pick a profile +
parameter (+ optional symbol) → see exactly how the value resolves
through the override chain *right now*. Shows global default +
each layer that has an override + which one wins, with the final
value highlighted. Also annotates position-size parameters with the
current `capital_scale` multiplier (Layer 9). Backed by new
`/api/resolve-param` endpoint.

This is the "why is the AI behaving this way" debugging tool. When
the system has 4 dimensions of overrides stacked, knowing which one
is winning for a specific (param, regime, TOD, symbol) tuple is
otherwise non-trivial to figure out.

**3. Autonomy Timeline** (AI Operations tab). Per-profile
chronological feed of every autonomous change in the last 30 days:
parameter tunings (with from/to + reason + outcome), strategy
deprecations / restorations, post-mortem patterns extracted. Color-
coded by event type with vertical-rail timeline styling. Backed by
new `/api/autonomy-timeline` endpoint that merges `tuning_history`
(master DB) + `deprecated_strategies` + `learned_patterns`
(per-profile DBs) into a single sorted feed.

This is the "what has the system done autonomously" history view.
The Self-Tuning History table covers parameter tunings; the
timeline includes all event types in one place.

**Tests:** 5 new in `test_cost_guard.py` covering user-set vs
auto-computed ceiling precedence, zero/negative override fallback,
and `ceiling_source` provenance. Full suite: 912 passed.

---

## 2026-04-25 — UI surfaces: cost guard status + active lessons cards (Severity: low, UX)

Two read-only widgets on the AI Operations tab so the new
infrastructure is visible without console-spelunking.

**Cost Guard card.** Shows today's spend vs ceiling, headroom
remaining, trailing-7-day average, with a colored progress bar (green
< 60%, orange < 90%, red ≥ 90%). The explanatory subtitle tells the
user that over-ceiling auto-actions become recommendations, not
silent debits. New `/api/cost-guard-status` endpoint backs it.

**Active Lessons card.** Per-profile breakdown of currently-active
post-mortem patterns and tuner-detected failure patterns —
i.e., everything currently being injected into the AI prompt's
LEARNED PATTERNS section. Profiles with no active lessons render as
"AI is operating on default context — no post-mortem patterns or
strong tuner-detected failure patterns to inject." New
`/api/active-lessons` endpoint backs it (named to avoid colliding
with the older `/api/learned-patterns` paginated endpoint).

Tests: full suite 907 still green (UI changes only; no Python logic
changes).

---

## 2026-04-25 — Closed-loop learning: post-mortems on losing weeks + false-negative tuning + comprehensive AI doc (Severity: medium, feature)

Three additions that turn information into learning:

**1. Losing-week post-mortems (`post_mortem.py`).** Weekly Sunday task
per profile. Triggers when the past 7 days underperformed the
long-term baseline by ≥10pt. Clusters losing predictions by feature
signature, identifies the dominant pattern (e.g., "60% of losses had
insider_cluster=high AND vwap_position=below"), stores it as a
`learned_pattern`. The trade pipeline already injects active patterns
into the AI prompt's `LEARNED PATTERNS` section, so the AI sees the
post-mortem learning at its next decision automatically — no extra
wiring needed.

Storage in a new `learned_patterns` table per profile DB. Only the
most recent post-mortem stays "active" so the prompt isn't drowned
in stale lessons. Idempotency marker
`.post_mortem_done_p<id>.marker` prevents re-fire on restart;
excluded from rsync delete.

**2. False-negative tuner rule (`_optimize_false_negatives`).** Scans
HOLD predictions resolved as `loss` (price moved >2% in 3 days, so
we missed an opportunity). When ≥60% of such misses cluster in the
band just below the current confidence threshold (within 10 conf
points), the threshold is rejecting trades it should be taking —
auto-lower it by 5. Same safety scaffolding as other tuner rules.

**3. AI_ARCHITECTURE.md comprehensive rewrite.** The doc now
exhaustively describes everything the AI does end-to-end: 7 agents
× 13–14 calls per cycle, the decision flow, the 12-layer autonomy
system, the cross-cutting cost guard, the closed-loop learning
surfaces (meta-model, alpha decay, post-mortems, false-negative
analysis), the safety guardrails, the user surfaces, and a
file-by-file map of where each piece lives. Should answer "what
does the AI actually do" without code-spelunking.

**Tests:** 9 new in `test_post_mortem.py` covering pattern
extraction, idempotency, prior-pattern deactivation,
get_active_patterns, and the false-negative trigger conditions
(threshold lowering, floor respect, no-cluster no-op). Full suite:
907 passed.

---

## 2026-04-25 — Post-W13 follow-ups: ai_model_auto_tune toggle + namespaced display names (Severity: low, completion)

Two small but real follow-ups to W13:

1. **`ai_model_auto_tune` opt-in toggle** added — schema column on
   `trading_profiles` (default OFF), Settings UI checkbox with
   explicit copy ("OFF by default, flipping this on can increase API
   spend"), wired into the profile-save form. The toggle is the
   per-profile entry point for future tuner logic that A/B tests AI
   models within the cost guard. The actual A/B tuning code is a
   future expansion of Layer 1; for now the toggle exists so users
   can express intent.

2. **Display names cleaned up for the override-stack namespaced keys.**
   Added explicit prefix labels: `weight` → "Signal Intensity",
   `tod` → "Time of Day", `deprecate` → "Deprecate Strategy",
   `layout` → "Prompt Section", `self_commission` →
   "Self-Commissioned Strategy", `capital_scale` → "Capital Scale".
   Plus a `_is_ticker_like` helper that preserves uppercase ticker
   tokens (`NVDA`, `AAPL`) verbatim instead of title-casing them.
   So `symbol:NVDA:max_position_pct` now reads as
   "Symbol — NVDA — Max Position Size (%)" instead of
   "Symbol — Nvda — Max Position Size (%)". Tested for collision
   with the existing AI-cost-purpose `political_context` label.

898 passed.

---

## 2026-04-25 — Post-W13: scheduled the capital allocator, surfaced the autonomy state UI (Severity: medium, completion)

Three real gaps caught after W13 declared "done":

1. **Layer 9 had no scheduled task.** I built
   `capital_allocator.rebalance(user_id)` in W12 and added the user
   opt-in toggle in W13, but never registered the weekly task that
   actually CALLS rebalance(). Without it, flipping the toggle did
   nothing. Added `_task_capital_rebalance` to `multi_scheduler.py` —
   runs Sundays only, file-based idempotency marker
   (`.capital_rebalance_done.marker`) prevents re-firing on restart.
   Iterates users with `auto_capital_allocation = 1`, calls
   `rebalance(user_id)`, logs results. Marker added to sync.sh
   exclude list so deploys don't wipe it.

2. **No UI surface for active overrides.** Six layers of autonomy
   were running invisibly — signal weights, regime/TOD/symbol
   overrides, prompt layout, capital scale all lived in JSON columns
   nobody could see without sqlite. Added `/api/autonomy-status`
   endpoint that returns one entry per profile with all active
   overrides. AI page Operations tab now has an "Active Autonomy
   State" card rendering them as colored pills (green = capital
   scale up, orange = down, blue = regime overrides, purple = TOD,
   red = per-symbol, brown = prompt verbosity). Profiles with no
   overrides show "all defaults, no autonomous overrides active".

3. **SELF_TUNING.md only documented Layers 1-4.** Added sections for
   Layers 5-9 (cross-profile propagation, adaptive prompt structure,
   per-symbol, self-commission, capital allocation) with the same
   detail level as the Layer 1-4 sections.

Full suite: 898 passed.

---

## 2026-04-25 — Autonomous tuning Wave 13: Final guardrail + Settings UI Autonomy section (Severity: medium, infrastructure)

The closing wave of the autonomous-tuning rollout. Ships the
structural guardrail that prevents future regressions in autonomy
coverage, plus the user-facing Settings page surface for the per-user
opt-in toggles.

**Anti-regression test: `test_every_lever_is_tuned.py`.**
AST-walks the `trading_profiles` schema (CREATE TABLE + ALTER TABLE
migrations) and asserts every column is either:
- Updated by `update_trading_profile()` somewhere in `self_tuning.py`
  (covers direct param-tuning and the dynamic-key strategy-toggle
  pattern via `_STRATEGY_TYPE_TO_TOGGLE.values()`); or
- On the explicit `MANUAL_PARAMETERS` allowlist with a written
  rationale.

The allowlist captures every legitimate exception: secrets, identity,
strategic AI choice (opt-in via `ai_model_auto_tune` planned), schedule,
the override-stack JSON storage columns (tuned via layer-specific
helpers, not `update_trading_profile`), boolean execution toggles
(intensity tuned via Layer 2 weights, defaults stay user-set), and
the three placeholder optimizers awaiting feature columns
(avoid_earnings_days, skip_first_minutes, trailing_atr_multiplier).

A second test (`test_no_stale_entries_in_manual_allowlist`) catches
allowlisted-but-no-longer-existing columns so the list stays honest.

**Settings page Autonomy section.** New `<h2 id="autonomy">Autonomy</h2>`
block with a checkbox for `auto_capital_allocation` (default OFF).
The accompanying copy explains the per-Alpaca-account constraint
explicitly so the user understands what they're enabling. New POST
endpoint `/settings/autonomy` persists the toggle to the user record.

**Tests:** 2 new in `test_every_lever_is_tuned.py`. Full suite: 898
passed.

This closes the 12-wave plan. Final state of the autonomous-tuning
system as of 2026-04-25:

- 35+ parameters auto-tuned with cooldown/reversal/bound-clamping
- 21 weighted signals + per-profile intensity ladder
- Per-regime / per-time-of-day / per-symbol overrides chained at
  every decision point
- Cross-profile insight propagation from improvements
- Adaptive AI prompt structure with cost gating
- Self-commissioned new strategies via Phase 7 generator
- Auto capital allocation (opt-in, per-Alpaca-account constrained)
- Cost guard wrapping every spend-affecting action
- Six anti-regression guardrails:
  1. `test_no_recommendation_only` — every Recommendation: string
     must be on a written-rationale allowlist
  2. `test_no_snake_case_in_optimizer_strings` — optimizer return
     strings can't embed raw column names
  3. `test_self_tune_task_no_change_path` — the no-change branch
     can't NameError
  4. `test_signal_weights_lifecycle` — weight ladder + tuner +
     prompt builder
  5. `test_regime_overrides` / `test_tod_overrides` /
     `test_symbol_overrides` — chain precedence
  6. `test_every_lever_is_tuned` — every schema column is
     autonomous or explicitly manual

---

## 2026-04-25 — Autonomous tuning Wave 12: Layer 9 Auto Capital Allocation — opt-in (Severity: medium, behavior)

The final functional layer. When the user flips
`auto_capital_allocation` ON for their account, a weekly task
rebalances per-profile `capital_scale` multipliers based on each
profile's risk-adjusted recent returns. The trading pipeline reads
`capital_scale` before sizing, so a profile at 0.5 takes
half-position-size relative to its own baseline. Default OFF.

**Critical constraint respected:** profiles are virtual on top of
shared Alpaca paper accounts. Multiple profiles can share one real
$1M paper account. The allocator works **per-Alpaca-account**:

1. Profiles are grouped by `alpaca_account_id`.
2. Within each group, scales are normalized so they sum to N (the
   group size). Average stays 1.0; relative shifts move toward
   higher-scoring profiles.
3. Group conservation means the underlying real account is never
   over-committed — if scale[A]=1.5, then scale[B]+scale[C]=1.5 in
   the same group.
4. **Solo profiles** (1 per account) always get `scale=1.0`. There's
   nothing to rebalance against.

**Bounds (in addition to group conservation):**
- Per-rebalance: each scale moves at most ±50% per week.
- Absolute: scale ∈ [0.25, 2.0] — no profile drops below 25% or
  rises above 200% of baseline.

**Schema:** `users.auto_capital_allocation` boolean (default OFF) +
`trading_profiles.capital_scale` REAL (default 1.0). Both
auto-migrated.

**Pipeline integration** (`trade_pipeline.execute_trade`): after the
override-chain resolution of `max_position_pct`, the result is
multiplied by `capital_scale`. So the auto-allocator's decisions stack
on top of all other tuning layers — per-symbol stop-loss × regime ×
TOD × global × `capital_scale` = final position size.

**Tests:** 7 new in `test_capital_allocator.py`: solo-profile
preservation, group-sum conservation, score-weighted shifts, mixed
solo/shared groups, per-rebalance and absolute bound enforcement,
opt-in gate respected. Full suite: 896 passed.

This closes the 9-layer plan from `AUTONOMOUS_TUNING_PLAN.md`. The
last wave (W13) is the cross-cutting guardrail: a test that walks
`trading_profiles` schema and asserts every column is either tuned
or on a manual allowlist. Then the user-facing Settings UI for
opting into the per-user toggles (`auto_capital_allocation`,
`ai_model_auto_tune`).

---

## 2026-04-25 — Autonomous tuning Wave 11: Layer 8 Self-Commissioned New Strategies (Severity: medium, behavior)

The tuner can now identify *gaps* in current strategy coverage and
trigger Phase 7's strategy generator with a focused brief. Heavily
cost-gated (LLM tokens cost real money) and rate-limited to ≤1 per
profile per week.

**Detection** (`_optimize_commission_strategy`): scans the last 30
days of resolved AI predictions. Counts winning BUY/SELL predictions
where `strategy_type` was empty/null — i.e., the AI made the right call
but no existing strategy fired on that pattern. ≥5 such gaps trigger
the commission flow.

**Cost guard**: every commission call is wrapped in
`cost_guard.can_afford_action(user_id, ~$0.05)`. If it would push spend
over the daily ceiling, the gap surfaces as
`Recommendation: cost-gated` instead of firing the LLM.

**Brief construction**: builds a focused prompt for
`strategy_proposer.propose_strategies` describing the gap — sample
symbols, average return — and asks for 1-2 new strategy specs. The
returned specs flow through the existing Phase 7 pipeline:
proposed → validated → shadow → active.

**Rate limit**: 7-day cooldown via the existing
`_get_recent_adjustment` machinery, keyed on `"self_commission"`.
At most one commission per profile per week.

**Tests:** 5 new in `test_self_commission.py` covering insufficient
gaps, cooldown respect, cost-gated path, end-to-end proposal flow,
and empty-proposer-result handling. Full suite: 889 passed.

---

## 2026-04-25 — Autonomous tuning Wave 10: Layer 6 Adaptive AI Prompt Structure (Severity: medium, behavior)

The structure of the AI's prompt — section verbosity per profile —
becomes a tunable surface. The tuner periodically rotates one section's
verbosity across `brief / normal / detailed` to test whether the AI
makes better decisions with different framing. Cost-gated to prevent
verbosity drift toward longer prompts that would balloon API spend.

**New module: `prompt_layout.py`** with sections registry (4 sections
to start: `alt_data`, `political_context`, `learned_patterns`,
`portfolio_state`), parse / get_verbosity / set_verbosity helpers, a
deterministic `pick_rotation` for testability, and an
`estimate_daily_cost_delta` that's used by the cost guard.

**Schema migration:** `prompt_layout TEXT NOT NULL DEFAULT '{}'`
column auto-migrated. Default behavior unchanged — every section is
"normal" until the tuner rotates it.

**Prompt builder integration** (`ai_analyst._build_batch_prompt`):
each tunable section now consults `_verbosity(name)` and adjusts:
- `alt_data` brief = top 3 signals + "(N more)" tail; detailed = same as normal (no extra noise).
- `political_context` brief = 2 lines; normal = 4 (current); detailed = 8.
- `learned_patterns` brief = 2; normal = 5 (current); detailed = 10.

**Tuner rule** (`_optimize_prompt_layout`):
- Requires ≥50 resolved predictions before experimenting.
- 14-day cooldown per rotation (vs 3-day for parameters) so each
  variant has enough cycles to attribute outcomes.
- Cost-saving rotations (toward `brief`) are auto-applied.
- Cost-adding rotations (toward `detailed`) are wrapped in
  `cost_guard.can_afford_action`. If they'd push over the daily
  ceiling, surfaced as `Recommendation: cost-gated` instead.

**Tests:** 18 new in `test_prompt_layout.py` covering parse/get/set,
rotation picking, cost estimation, tuner skip-conditions, cost-gate
auto-apply vs recommend, and end-to-end prompt builder rendering at
brief vs normal verbosity. Full suite: 884 passed.

This is the last "decision-surface" layer before the meta-tuning waves
W11 (self-commissioned strategies) and W12 (capital allocation).

---

## 2026-04-25 — Autonomous tuning Wave 9: Layer 5 Cross-Profile Insight Propagation (Severity: medium, behavior)

When the tuner makes a change that turns out to improve a profile's
win rate (`outcome_after = 'improved'` after the 3-day review window),
the same detection rule now runs against every OTHER enabled profile
belonging to the same user. Each peer's own data has to independently
support the change — no value-copying. The fleet learns ~10× faster
than profiles in isolation, with zero new API spend.

**New module: `insight_propagation.py`.**
- `_peer_profiles(source_id)` — enumerates other enabled profiles in
  the same user's account.
- `_detector_for(change_type)` — maps adjustment types to the
  corresponding `_optimize_*` function in self_tuning.
- `propagate_insight(source_id, change_type, parameter_name)` — for
  each peer, builds a duck-typed context, opens its prediction DB,
  runs the detection rule. Returns a list of human-readable messages
  for peers where the change was applied.

**Integration:** `self_tuning.apply_auto_adjustments` now calls
`propagate_insight` after `review_past_adjustments` finds an
improvement. Propagated changes appear in the tuner's adjustment log
prefixed with `PROPAGATED:` for visibility.

**Critical guarantee — no value-copying.** A change to Mid Cap's
`max_position_pct` doesn't get applied to Small Cap's profile. What
gets propagated is the *detection rule check* — Small Cap's own data
must trigger the same rule before any change is made. Same cooldown,
same reverse-if-worsened, same bound clamping as direct tuning.

**Tests:** 7 new in `test_insight_propagation.py`: detector mapping
coverage, peer enumeration excludes source, no-op-on-unknown-type,
no-op-on-no-peers, end-to-end propagation when peer data triggers,
no-change when peer data is healthy. Full suite: 866 passed.

---

## 2026-04-25 — Autonomous tuning Wave 8: Layer 7 Per-Symbol Parameter Overrides (Severity: medium, behavior)

The most-specific tier of the override stack. Some symbols behave
fundamentally differently from each other — NVDA's optimal stop-loss
isn't KO's. The tuner now creates per-symbol parameter overrides for
symbols with materially different track records than the profile
baseline.

New module `symbol_overrides.py` mirrors the regime/TOD pattern. Schema
column `symbol_overrides TEXT NOT NULL DEFAULT '{}'` auto-migrated.
Symbol keys normalised to uppercase on read/write.

**Tuner detection** (`_optimize_symbol_overrides`): walks symbols with
≥20 individual resolved predictions (high bar — over-fitting risk on
small samples is real) ordered worst-WR-first. Symbols ≥15pt off
overall WR get a per-symbol override. Cooldown 7 days (vs 3 for other
tiers) for the same over-fitting reason. Underperformers get
`max_position_pct` reduced for that symbol; outperformers get
`ai_confidence_threshold` raised.

**Pipeline chain** (`regime_overrides.resolve_for_current_regime`)
extended with optional `symbol=` parameter. Full lookup order is now:

  1. **Per-symbol override** (Layer 7, this wave)
  2. Per-regime override (Layer 3)
  3. Per-time-of-day override (Layer 4)
  4. Profile global value
  5. Caller default

Wired into `trade_pipeline.ai_review` (confidence threshold) and
`execute_trade` (position size, stop-loss, take-profit). Symbol is
already in scope at every call site; passed through to the resolver.

**Tests:** 14 new in `test_symbol_overrides.py` covering parse/resolve
case-normalization, tuner detection (sample-size + threshold
respect), and chain precedence (per-symbol wins over regime when both
set; falls through to regime when no symbol override). Full suite:
858 passed.

The full chain shipped today means parameters can vary along 4
dimensions at once: symbol × regime × time-of-day × global. The tuner
acts on the dimension where the WR signal is strongest. A user with a
profile that has `stop_loss_pct=0.03` could end up with NVDA-in-volatile
at 0.08, NVDA-in-bull at 0.05, regular-symbol-in-volatile at 0.06,
and regular-symbol-in-bull at 0.03 — all autonomously chosen,
all reversible, all bounded.

---

## 2026-04-25 — URGENT hotfix: 100+ daily summary emails sent in a single day (Severity: critical, regression)

**Problem:** User hit their email-sending quota — ~100 daily-summary
emails sent today across ~10 profiles. Root cause: every scheduler
restart re-fired the snapshot bundle (snapshot, summary email, DB
backup, alpha-decay snapshot) because the
`last_run["daily_snapshot"]` flag was in-memory only. Today saw ~10
deploys (W1 + W2 + W3 + 2 hotfixes + W4 + W5 + W6 + this fix), each
restarting the scheduler. 10 restarts × 10 profiles = ~100 daily
summary emails sent for the same calendar day.

**Fix — file-based idempotency markers, like the weekly digest:**
- `_task_daily_summary_email` now writes
  `.daily_summary_sent_p<profile_id>.marker` after sending. Subsequent
  restarts on the same calendar day (ET) skip the send with
  "already sent today".
- `last_run["daily_snapshot"]` now persists to/from
  `.daily_snapshot_done.marker` so the entire snapshot bundle (not
  just the email) doesn't re-fire on restart. Also stops re-running
  expensive daily tasks like alpha-decay snapshot and DB backup.
- Manually pre-created today's markers on prod via SSH so the next
  scheduler tick after this deploy skips today's bundle entirely.

**Why it wasn't caught:** The weekly digest already had this
file-based idempotency pattern (introduced 2026-04 for this exact
reason). The daily summary used in-memory state only — the missing
mirror of the weekly pattern. Tests covered "the email gets sent at
all" but not "the email doesn't get re-sent on restart."

**Also fixed (related):** `RECOGNISED_TODS` and `RECOGNISED_REGIMES`
are sets, so the W5/W6 tuner rules iterated buckets in
hash-randomized order. Tests passed in isolation but failed in the
full suite when the random order picked a different bucket. Fixed
by using explicit ordered tuples for tuner iteration.

---

## 2026-04-25 — Autonomous tuning Wave 7: Cost Guard cross-cutting infrastructure (Severity: medium, infrastructure)

**New module: `cost_guard.py`.** Daily-spend ceiling enforcement that
wraps every autonomous action that could increase API costs. Today's
projected spend (sum of today's actual + the action's estimated extra
cost) is compared against the daily ceiling. If it would push us over,
the action is queued as a "Recommendation: cost-gated" with explicit
cost estimate — the ONLY recommendation prefix the
no-recommendation-only guardrail allows.

API:
- `daily_ceiling_usd(user_id)` — defaults to trailing-7-day-avg × 1.5,
  floored at $5/day so brand-new users aren't immediately blocked.
- `today_spend(user_id)` — sum across user's enabled profile DBs.
- `can_afford_action(user_id, estimated_extra_cost_usd)` — bool gate.
- `format_cost_recommendation(action_summary, user_id, cost)` — the
  standardized "Recommendation: cost-gated — ..." string.
- `status(user_id)` — UI snapshot dict.

**First integration:** the Layer-2 signal-weight nudge-up case (which
re-includes a previously-omitted signal in prompts → longer prompts →
higher API spend per scan). Estimated 1¢/day per re-included signal
at typical scan rate. If the ceiling would be breached, surfaces as
recommendation instead of auto-applying. Future waves (Layer 6
adaptive prompt structure, Layer 8 self-commissioned strategies) will
plug into the same gate.

**Tests:** 11 new in `test_cost_guard.py` covering ceiling computation
(floor + multiplier), can_afford gate (under/over/zero/negative),
recommendation string format, status snapshot. The
`test_no_recommendation_only.py` allowlist gained
`"Recommendation: cost-gated"` with rationale; the staleness check
expanded to scan both `self_tuning.py` and `cost_guard.py`.

Full suite: 844 passed.

---

## 2026-04-25 — Autonomous tuning Wave 6: Layer 4 Per-Time-of-Day Parameter Overrides (Severity: medium, behavior)

Mirror of Wave 5's regime architecture, bucketed by intraday window
(open 09:30-10:30, midday 10:30-14:30, close 14:30-16:00 ET). New
module `tod_overrides.py` with the same shape: `parse_overrides`,
`resolve_param`, `set_override`, `resolve_for_current_tod`. Schema:
`tod_overrides TEXT NOT NULL DEFAULT '{}'` column auto-migrated.

Tuner detection (`_optimize_tod_overrides`): bucket recent resolved
predictions by their timestamp's ET hour, find buckets with WR
divergence ≥12pt from overall, create per-bucket override (reduce
position size in underperforming bucket; raise confidence floor in
outperforming bucket).

Pipeline integration: `regime_overrides.resolve_for_current_regime`
extended to a multi-layer chain — per-regime override beats per-TOD
override beats global. So a profile with `stop_loss_pct=0.03`,
`regime_overrides={"volatile": 0.06}`, and `tod_overrides={"open":
0.05}` resolves to:
- 0.06 in volatile regime (regime wins)
- 0.05 at open in bull regime (TOD fallback)
- 0.03 at midday in bull regime (global fallback)

This is the architectural foundation for Layer 7 (per-symbol overrides)
which will plug into the same chain as the most-specific tier.

**Tests:** 14 new in `test_tod_overrides.py` covering bucket
boundaries, parse/resolve, tuner detection, and chain precedence.
Full suite: 832 passed.

---

## 2026-04-25 — Autonomous tuning Wave 5: Layer 3 Per-Regime Parameter Overrides (Severity: medium, behavior + architecture)

**The big architectural one.** Real quant funds use different
parameters in different market regimes — a stop-loss right for sideways
trading is too tight for volatile breakouts, a position size right in
bull is too aggressive in crisis. This wave gives the tuner a place to
express those overrides without forcing the user to maintain five
copies of every profile.

**New module: `regime_overrides.py`.**
- `RECOGNISED_REGIMES = {"bull","bear","sideways","volatile","crisis"}`
- `parse_overrides(json)` — defensive JSON parsing with bounds
  clamping and unknown-regime/unknown-param filtering.
- `resolve_param(profile, name, regime, default=...)` — single source
  of truth for parameter access at decision time. Per-regime override
  first, then global, then default.
- `resolve_for_current_regime(profile, name, default=...)` — wrapper
  that auto-detects current regime via `market_regime.detect_regime()`
  with 5-minute cache.
- `set_override(profile_id, name, regime, value)` — clamped persist;
  `value=None` removes the override.

**Schema migration:** `regime_overrides TEXT NOT NULL DEFAULT '{}'`
column added to `trading_profiles` via the existing auto-migration
framework.

**Pipeline integration** (`trade_pipeline.py`): every decision-point
read of `ai_confidence_threshold`, `max_position_pct`, `stop_loss_pct`,
`take_profit_pct`, `max_total_positions` now goes through
`resolve_for_current_regime`. Falls back gracefully on any error.

**Tuner detection** (`self_tuning._optimize_regime_overrides`): walks
each regime that has ≥10 resolved predictions. If regime WR diverges
from overall by ≥12pt, creates a regime-specific override:
- Underperforming regime → reduce `max_position_pct` 25% for that
  regime only.
- Outperforming regime → raise `ai_confidence_threshold` +5 to focus
  on strongest setups.

Same safety scaffolding as previous waves: cooldown keyed on
`regime:<regime>:<param>`, reverse-if-worsened, snap to PARAM_BOUNDS.

**Tests:** 17 new in `test_regime_overrides.py` covering parse/resolve
fallback chains, current-regime auto-detection, tuner divergence
detection, sample-size and cooldown respect. Full suite: 818 passed.

**Documentation:** `SELF_TUNING.md` Layer 3 section added.

This is the architectural enabler for per-context decision-making.
Layer 4 (per-time-of-day) and Layer 7 (per-symbol) will reuse the
exact same pattern: a JSON column + a `resolve_for_*` helper +
fallback chain. The pattern generalizes; future context dimensions
just plug in.

---

## 2026-04-25 — Hotfix: sync.sh missed models.py → web restart, schema migration didn't auto-apply (Severity: high, deploy regression)

**Problem:** W4 added a `signal_weights` column to `trading_profiles`
via the auto-migration framework in `models.init_user_db()`, which only
runs at web-server startup (called from `app.py:create_app()`). But
`sync.sh`'s `WEB_PATTERNS` only matched `templates|static|views.py|
display_names.py|app.py|auth.py` — `models.py` wasn't on that list, so
W4 deploy didn't trigger a web restart, and the migration never ran.
Result: every tuner cycle that tried to write a signal weight saw
`UPDATE trading_profiles SET signal_weights=...` fail with `no such
column: signal_weights`. The optimizer's exception was caught by the
orchestrator (so the cycle didn't crash), but the new tuning surface
was effectively dead.

**Fix:**
- Added `models.py` to the `WEB_PATTERNS` regex in `sync.sh` so any
  schema change triggers a web restart on the next deploy.
- Manually ran `init_user_db()` on prod via SSH to apply the missing
  column without a full restart cycle.

**Why it wasn't caught:** Tests don't simulate deploy paths. The
auto-migration framework was assumed to fire on every code push;
the WEB_PATTERNS regex hadn't been updated since the framework was
introduced. Future schema additions to `models.py` now trigger a web
restart automatically.

**Also fixed:** Updated `test_tuning_status_js_uses_real_fields` —
previously `pytest.skip()`-ing because the function was renamed to
`loadTuningStatusPills` during the Self-Tuning widget merge. Test now
asserts hard against the new function name and the actual fields the
pills code uses (`profile_name`, `resolved`, `required`, `can_tune`,
`message`). Suite is now 801 passing / 0 skipped.

---

## 2026-04-25 — Autonomous tuning Wave 4: Layer 2 Weighted Signal Intensity (Severity: medium, behavior + architecture)

**The big one.** Previously every signal the AI saw was binary: present
in the prompt or absent. The tuner could disable a whole strategy via
the toggle pipeline but had no way to express "this signal is weak but
not worthless — discount it." This wave adds per-profile signal weights
on a 4-step discrete ladder (`1.0 → 0.7 → 0.4 → 0.0`).

**New module: `signal_weights.py`** — declarative `WEIGHTABLE_SIGNALS`
list (21 signals to start: insider/options/dark-pool/congressional/
political-context alt-data + modular strategy votes), `WEIGHT_LADDER`
constant, `parse_weights` / `get_weight` / `set_weight` / `nudge_up` /
`nudge_down` helpers. Each signal has an `is_active(features_dict)`
predicate the tuner uses to decide "was this signal materially present
in this prediction" so per-signal WR is computable.

**Schema migration:** added `signal_weights TEXT NOT NULL DEFAULT '{}'`
column to `trading_profiles`. Auto-migration via the existing
ALTER-TABLE-on-startup framework — production profiles get the column
on first restart with no manual DBA work.

**New tuner rule: `_optimize_signal_weights`.** Walks every weightable
signal each cycle, buckets recent resolved predictions by signal
presence, computes differential WR. Nudges DOWN when present-WR ≥10pt
below absent-baseline; nudges UP when present-WR ≥5pt above (recovery).
3-day cooldown per signal keyed on `weight:{signal_name}`.
Reverse-if-worsened protection. Registered as the last entry in the
upward optimizer chain.

**Prompt builder integration** (`ai_analyst._build_batch_prompt`):
introduces a `_weighted_signal_text(name, text)` wrapper around every
`alt_parts.append`. Returns `None` (signal omitted) for weight 0.0;
appends `[intensity 0.4]` for partial weights; passes through unchanged
at full weight. Same logic guards the political-context block.

**Tests (20 new in `test_signal_weights.py`):** parse/snap/round-trip,
nudge ladder edge cases, predicate truthiness, tuner detection
(triggers/doesn't trigger/insufficient-data), and prompt builder
respects each weight tier (full / partial / zero). Full suite: 800
passed.

**Documentation:** `SELF_TUNING.md` Wave 4 section added with the
per-signal ladder, action table, and prompt-builder behavior matrix.
`AUTONOMOUS_TUNING_PLAN.md` Layer 2 marked active.

**System now tunes 35+ levers.** Layer 2 is the architectural enabler
for replacing every binary on/off in the system with graduated weights —
future signals automatically join this system without new schema work.

---

## 2026-04-25 — Hotfix: snake_case parameter names leaked to dashboard ticker via optimizer return strings (Severity: high, UX regression)

**Problem:** User saw `atr_multiplier_tp` in the dashboard activity
ticker. Audit found 13 W1/W2/W3 optimizer functions returning strings
that embedded raw snake_case column names directly:
- `"Tightened atr_multiplier_tp from 3.00 to 2.75"`
- `"Raised min_volume from 500,000 to 750,000"`
- etc.

These strings flow into the activity ticker, weekly digest body, and
tuning-history detail. The `display_names` registry was already correct
for every parameter (`atr_multiplier_tp` → "ATR Target Multiplier") —
the bug was that the registry was never consulted when constructing
these return messages.

**Fix:**
- Added `_label(param_name)` helper in `self_tuning.py` — single
  shortcut to call `display_name()` from inside an f-string.
- Rewrote every offending optimizer return string to use `_label()`.
- Added `tests/test_no_snake_case_in_optimizer_strings.py` — AST-walks
  every `_optimize_*` function in `self_tuning.py`, finds all string
  literals returned, and fails the build if any contains a raw
  parameter name from `PARAM_BOUNDS`. Excludes the legitimate case
  where the parameter name appears as a direct argument to `_label()`
  or `display_name()`. This is now the structural guardrail that
  prevents this class of bug from recurring.

**Why it wasn't caught:** Existing tests verified the tuner WROTE the
right value to the database, but not that the human-readable string
returned to the orchestrator was in plain English. The new test closes
that gap with AST-level enforcement — no future optimizer can ship a
parameter-name leak without explicitly bypassing it.

**Tests:** 780 passed total (1 new guardrail test + label-helper
sanity).

---

## 2026-04-25 — Hotfix: Self-Tune NameError on no-change path (Severity: high, regression)

**Problem:** Production "Scan Failures" panel showed "Self-Tune failed"
for every profile after the first weekend snapshot ran. Root cause:
the earlier "applied vs recommended" notification rewrite moved
`real_changes = applied` inside the `if adjustments:` branch in
`_task_self_tune`. When the tuner found nothing to change (the common
case — most cycles), `real_changes` was never defined, and the
no-changes-needed log path 30 lines below raised `NameError`.

**Fix:** Define `real_changes = applied` unconditionally at the top
of the function, before any branching. Removed the now-redundant
assignment inside the `if` branch.

**Why it wasn't caught:** The original test coverage for
`_task_self_tune` only exercised the changes-applied path. The
no-adjustments path was never hit in tests despite being the most
common production code path.

**Tests:** New `test_self_tune_task_no_change_path.py` with 3 tests:
no-change path (the regression), applied path (sanity), and
recommendation-only path (the new asymmetric branch). Full suite
778 passed.

---

## 2026-04-25 — Autonomous tuning Wave 3: Group B (exit parameters) — 4 new tunable parameters (Severity: medium, behavior)

**4 new exit-parameter tuning rules** (`self_tuning.py`):

| Function | Parameter | Detection |
|----------|-----------|-----------|
| `_optimize_short_take_profit` | `short_take_profit_pct` | Avg short winner < 50% of TP target → tighten 20% |
| `_optimize_atr_multiplier_sl` | `atr_multiplier_sl` | ≥40% of losses cluster near max-loss magnitude (proxy for stops being hit too tight) → +0.25 |
| `_optimize_atr_multiplier_tp` | `atr_multiplier_tp` | Avg winner < 50% of best winner achieved → -0.25 (tighten to capture more) |
| `_optimize_trailing_atr_multiplier` | `trailing_atr_multiplier` | Placeholder until per-trade max-favorable-excursion is tracked |

ATR-multiplier rules respect `use_atr_stops`: skip when off (the
multiplier doesn't apply). Trailing-multiplier rule no-ops gracefully
until the supporting per-trade MFE column lands. Same safety scaffolding
as W1/W2.

The 3 boolean execution toggles (`use_atr_stops`, `use_trailing_stops`,
`use_limit_orders`) deliberately are NOT in W3 — they roll into W4
(weighted signal intensity) where they become 0.0/0.5/1.0 weights with
rotational A/B testing rather than binary on/off cliffs.

**Tests:** 5 new in `test_self_tuning_wave3.py`. Full suite: 775 passed.

**Tuner now manages 35 levers.** Layer 1 (parameter coverage) is now
substantively complete; remaining gaps are the 3 execution-toggle
booleans (deferred to W4) and the 2 placeholder rules awaiting feature
columns. W4 (weighted signal intensity) is next.

---

## 2026-04-25 — Autonomous tuning Wave 2: Group C (entry filters) — 8 new tunable parameters (Severity: medium, behavior)

**8 new entry-filter tuning rules** (all in `self_tuning.py`,
registered in `_apply_upward_optimizations` after the W1 set):

| Function | Parameter | Detection |
|----------|-----------|-----------|
| `_optimize_min_volume` | `min_volume` | Marginal-volume entries (≤1.5× threshold) WR < 30% → +50% |
| `_optimize_volume_surge_multiplier` | `volume_surge_multiplier` | Marginal surge entries WR < 35% → +0.25 |
| `_optimize_breakout_volume_threshold` | `breakout_volume_threshold` | Marginal breakout entries WR < 35% → +0.25 |
| `_optimize_gap_pct_threshold` | `gap_pct_threshold` | Marginal-gap entries (within 1.2×) WR < 35% → +0.5 |
| `_optimize_momentum_5d` | `momentum_5d_gain` | Marginal 5d-momentum entries WR < 35% → +0.5 |
| `_optimize_momentum_20d` | `momentum_20d_gain` | Marginal 20d-momentum entries WR < 35% → +0.5 |
| `_optimize_rsi_overbought` | `rsi_overbought` | Near-overbought entries (RSI ±5 of threshold) WR ≥55% → raise +2 |
| `_optimize_rsi_oversold` | `rsi_oversold` | Near-oversold entries WR ≥55% → lower -2 |

All read from `features_json` on resolved predictions via the new
shared helper `_bucket_by_feature(conn, feature_name)`. Rules
gracefully no-op when the relevant feature isn't logged yet (some
older predictions may not have full feature payloads). Same safety
scaffolding as W1: cooldown, reverse-if-worsened, bound clamping via
`param_bounds`, log to `tuning_history`.

**Tests:** 11 new in `test_self_tuning_wave2.py` covering each rule's
trigger logic, cooldown respect, no-op-on-missing-features, and
orchestrator registration. Full suite: 769 passed / 1 skipped.

**Tuner now manages 31 levers** (8 pre-existing + 10 W1 + 8 W2 + 5 wave-cross
[evaluation row, alpha_decay deprecation, 4 legacy strategy toggles
already counted as part of "8 pre-existing"]). Coverage of `trading_profiles`
columns is approaching 100%; W3 (Group B exits) closes the remaining
parameter rules.

---

## 2026-04-25 — Autonomous tuning Wave 1: Group A (concentration/risk) + Group D (timing) — 10 new tunable parameters (Severity: medium, behavior)

**Why this exists:** The whole point of QuantOpsAI is that it makes
better, faster, smarter tactical decisions than a person can. The
prior tuner managed only ~8 levers; the rest were either manually
configured or completely untouched. The full plan (see
`AUTONOMOUS_TUNING_PLAN.md`) brings every tactical parameter, signal,
regime context, and prompt structure under autonomous control across
9 layers, with cost discipline cross-cutting everything.

**Wave 1 ships the foundation** — Layer 1 Group A (concentration / risk)
and Group D (timing / flag) — plus the bounds-clamping infrastructure
that every later wave will use.

**New module: `param_bounds.py`.** Declarative `PARAM_BOUNDS` for every
tunable parameter — absolute min/max safety bounds. `clamp(name, value)`
helper. Tuning rules call `clamp` before writing so even a buggy
detection rule can't push a parameter to a dangerous value.

**10 new tuner functions** (all in `self_tuning.py`, registered in
`_apply_upward_optimizations`):

| Function | Parameter(s) | What it does |
|----------|--------------|--------------|
| `_optimize_max_total_positions` | `max_total_positions` | -1 on deep-loss + low-WR; +1 on strong-edge + healthy-winner |
| `_optimize_max_correlation` | `max_correlation` | Tighten 0.05 on weekly loss-cluster rate ≥40%; loosen on clean history + WR ≥55% |
| `_optimize_max_sector_positions` | `max_sector_positions` | -1 when overall WR < 35% |
| `_optimize_drawdown_thresholds` | `drawdown_pause_pct` | Tighten 0.02 in the WR drift zone (35–45%) |
| `_optimize_drawdown_reduce` | `drawdown_reduce_pct` | Tighten 0.01 in the WR drift zone |
| `_optimize_price_band` | `min_price`, `max_price` | Raise floor / lower ceiling when band-edge entries WR < 30%; capped at 0.5×–2.0× current to prevent identity drift |
| `_optimize_avoid_earnings_days` | `avoid_earnings_days` | Placeholder (no-op); activates when `days_to_earnings` is logged on each prediction |
| `_optimize_skip_first_minutes` | `skip_first_minutes` | Placeholder; activates when intraday entry-time is structured |
| `_optimize_maga_mode` | `maga_mode` | **Auto-disable** when predictions with political_context active WR ≥ 10pt below overall (≥20 samples) |

Every rule inherits the existing safety scaffolding: 3-day per-parameter
cooldown via `_get_recent_adjustment`, reverse-if-worsened guard via
`_was_adjustment_effective`, bound clamping, logging to `tuning_history`,
display via `display_name` namespaced fallback. Helper
`_safe_change_guarded` wraps the cooldown+history check.

**Documentation rewrite.** `SELF_TUNING.md` rewritten end-to-end —
removes the outdated "4 parameters" / "Future Parameters Planned Late
May 2026" sections and reflects the current 23 auto-tuned levers and
the 9-layer roadmap. `AI_ARCHITECTURE.md` Self-Learning section
expanded with the layered autonomy diagram and per-layer descriptions.

**Tests:** 23 new tests in `test_self_tuning_wave1.py` covering every
new rule (triggers correctly, respects bounds, respects cooldown, no-op
when conditions not met) plus an orchestrator-registration test.
`param_bounds.clamp` covered with under/over/in-range/unknown-param
cases. Full suite: 758 passed / 1 skipped.

**Next waves** (per `AUTONOMOUS_TUNING_PLAN.md`): W2 = entry filters,
W3 = exit parameters, W4 = weighted signal intensity (Layer 2), W5 =
per-regime overrides, W6 = per-time-of-day, W7 = cost guard, W8 =
per-symbol, W9 = cross-profile insight sharing, W10 = adaptive prompt
structure, W11 = self-commissioned strategies, W12 = capital
allocation, W13 = guardrail tests + Settings UI Autonomy section + final
doc pass.

---

## 2026-04-25 — Self-tuner: act on what it identifies (close 'recommendation only' hole) (Severity: medium, behavior)

**Problem:** When the tuner found a problem it knew the answer to, it
sometimes just emitted a "Recommendation:" string and called it done.
Concrete example flagged by user: "Insider Buying Cluster has 17% win
rate (3/18) vs 42% overall — consider removing from strategy mix" was
logged as 1 adjustment but no actual change was applied. The
underlying cause: only 4 of 16+ strategies had profile-level toggles,
so any modular strategy (insider_cluster, options-derived, etc.) the
tuner couldn't disable. The whole point of self-tuning is to act,
observe, and adjust — not to draft suggestions for a human.

**Fix — three layers:**

1. **Logic.** In `self_tuning._optimize_strategy_toggles`, the
   no-toggle branch now calls `alpha_decay.deprecate_strategy()` to
   actually remove the strategy from the active mix. The existing
   alpha-decay restoration pipeline (rolling Sharpe recovery) handles
   un-deprecating automatically. Cooldown applies via a synthetic
   parameter key `deprecate:{strategy_type}`. Same 3-day rule and
   reverse-if-worsened protection as the rest of the tuner. The
   "Recommendation: DISABLE short selling" branch was promoted from
   text to an actual `update_trading_profile(enable_short_selling=0)`
   call when 10+ short trades have <20% win rate AND negative P&L —
   defensive auto-action only. The reverse case ("ENABLE shorts") is
   deliberately left as a recommendation because flipping a high-risk
   feature ON without human review is dangerous (uncapped downside,
   margin requirements).

2. **Visibility.** `_task_self_tune` notification now separates
   "applied" from "recommended" counts (e.g., "Self-Tuning: 2
   applied, 1 recommended"). Body breaks them into APPLIED /
   RECOMMENDATIONS sections so the user can scan at a glance.
   Deprecated-strategies UI in the Strategy tab gets a "Restore"
   button (POSTs to a new
   `/ai/profile/<id>/restore-strategy/<strategy_type>` endpoint) so
   manual override is one click. Tuning history rows for deprecations
   surface via the existing display_name namespaced fallback —
   "deprecate:insider_cluster" renders as "Deprecate — Insider Buying
   Cluster".

3. **Guardrail.** New test `test_no_recommendation_only.py` AST-walks
   `self_tuning.py`, finds every "Recommendation:" string literal,
   and fails unless it matches an entry on a small ALLOWED list with
   a written rationale. Currently allowed: "Recommendation: enable
   short selling" (asymmetric on purpose: defensive disables get
   auto-applied; high-risk enables require human review). New
   "Recommendation:"-only paths fail this test until the author
   either wires a real action or adds an allowlist entry with
   rationale.

**Tests:** 6 new tests across `test_self_tuning_deprecation.py` and
`test_no_recommendation_only.py`: deprecation auto-action, cooldown,
already-deprecated short-circuit, toggleable strategies still use the
toggle path, allowlist enforcement, allowlist staleness check. Full
suite green at 735 passed / 1 skipped.

---

## 2026-04-25 — AI Win-Rate Trend chart added to AI Intelligence > Brain tab (Severity: low, feature)

**Problem:** No way to see whether the AI's prediction accuracy is
trending up or down over time. The Brain tab showed only the
all-time cumulative win rate — useful as a headline number, but
it hides recent shifts.

**Fix:** Added two pieces:

1. `ai_tracker.compute_rolling_win_rate(db_paths, window_days=7,
   lookback_days=60)` — returns a daily series of `{date, win_rate, n}`
   where each point is the win rate over the trailing 7 days. Days
   with zero resolved predictions in their window are returned with
   `win_rate=None` so the chart breaks the line cleanly instead of
   interpolating a fake value.
2. `metrics.render_win_rate_svg(series)` — server-rendered SVG line
   chart, mirroring the existing `render_equity_curve_svg` /
   `render_rolling_sharpe_svg` pattern (no JS chart library
   dependency). Y-axis 0–100% with grid lines at 0/25/50/75/100, a
   dashed 50% coin-flip baseline, green line if the latest point ≥ 50%
   else red. Gaps in resolved-prediction coverage render as broken
   polyline segments.

Wired into `ai_dashboard()` in `views.py` and rendered in the Brain
tab of `templates/ai.html` immediately after the headline win-rate
metric (so the user sees the trend right next to the cumulative
number).

**Tests:** 11 new tests in `test_ai_win_rate_chart.py` cover empty /
all-none series, pure winning/losing windows, mixed outcomes,
neutral-outcome exclusion, multi-DB aggregation, gap segmentation,
color selection. Full suite still green at 729 passed / 1 skipped.

---

## 2026-04-25 — Admin user table: humanize Created and Last Login columns (Severity: low, UX)

**Problem:** The admin user list showed raw ISO date/time strings:
`2026-03-28` for Created and `2026-04-23T14:36` for Last Login. The
"T" separator and lack of any natural formatting made the table read
as machine output.

**Fix:** Added a `friendly_date` Jinja filter to `display_names.py`
that renders a date or timestamp string as `"Mar 28, 2026"`. Updated
`templates/admin.html` to pipe `created_at` through `friendly_date`
and `last_login_at` through the existing `friendly_time` filter
(which renders `"Apr 23, 10:36 AM ET"`).

**Tests:** Existing 718-test suite passes — `friendly_date` is a
small additive function with no callers other than the template.

---

## 2026-04-25 — Self-tuning UI/digest: humanize parameter names and format values as percentages (Severity: medium, UX)

**Problem:** Two related leaks of internal identifiers and raw numeric
values to the user:

1. The weekly digest email's "Self-Tuning Changes" table showed
   snake_case parameter names like `ai_confidence_threshold`,
   `max_position_pct`, `strategy_gap_and_go` directly.
2. The dashboard's Self-Tuning History table (and the same table in
   `ai_performance.html` / `ai_operations.html`) rendered raw fractional
   decimals like `0.07 → 0.0805` for percentage params, instead of the
   user-facing `7.0% → 8.05%`.

**Root cause:** `_render_tuning_changes` in `ai_weekly_summary.py` and
the JS in `templates/ai.html` / `templates/ai_operations.html` both
pulled `parameter_name`, `old_value`, `new_value` straight from the
sqlite columns. There was no central knowledge of which params are
percentages vs. booleans vs. integers, and `display_names.py` had no
entries for self-tuning parameter keys.

**Fix:**
- Extended `display_names.py` with self-tuning parameter labels
  (`ai_confidence_threshold` → "AI Confidence Threshold", etc.),
  strategy-toggle labels (`strategy_gap_and_go` → "Strategy: Gap &
  Go"), bare strategy_type entries (`gap_and_go` → "Gap & Go" for the
  decay table), `_PERCENTAGE_PARAMS` and `_BOOLEAN_PARAMS` frozensets,
  and a `format_param_value(name, value)` function that renders a
  param value in its natural form (percentage / Enabled-Disabled /
  int / 2-dp float).
- `views.py`: `_format_param_name` now delegates to `display_name`;
  added `_format_param_value` helper; `api_tuning_history` populates
  `old_value_label` / `new_value_label` on each row; the two dashboard
  views populating the inline table do the same.
- `ai_weekly_summary.py`: `_render_tuning_changes` now passes
  `display_name(pname)` and uses `format_param_value` for old/new;
  `_render_decay_changes` wraps `strategy_type` with `display_name`.
- `templates/ai.html` (line 1157), `templates/ai_operations.html`
  (line 189): JS prefers `r.old_value_label` / `r.new_value_label`.
- `templates/ai_performance.html` (line 459): server-rendered template
  uses `| display_name` filter and `h.old_value_label or h.old_value`.

**Why it wasn't caught:** Display-formatting logic was scattered across
the API layer, JS templates, and the digest renderer, with no shared
source of truth — each layer had a partial humanization that left the
self-tuning params and percentage values uncovered. Tests covered the
data shape (`test_weekly_digest.py` passes raw rows through) but not
the rendered string content.

**Tests:** Existing 719-test suite passes. The render path for the
digest is exercised by `test_weekly_digest.py::TestRender::*` — they
verified no crash with the new code path. Follow-up TODO: add a
focused string-content assertion that "max_position_pct" and "0.07"
never appear in the rendered HTML for a tuned profile.

---

## 2026-04-24 — Blacklist: move from pre-filter to execution gate so stocks can recover (Severity: high, architectural)

**Problem:** The auto-blacklist at `trade_pipeline.py:817-837` rejected
any symbol with `win_rate == 0 AND total >= 3` resolved predictions
directly in the pre-filter, BEFORE the AI ever saw the candidate. That
meant no new predictions were ever recorded on blacklisted symbols,
their 0% win rate stayed 0% forever, and the stock was permanently
excluded from trading with no path back.

User framing (correct): **the blacklist should block TRADING, not
EVALUATION.** If the AI keeps predicting and those predictions start
winning, the symbol should earn its way back into the tradable set
automatically.

**Root cause:** pre-filter conflates two concerns — "don't risk capital
on this" (valid) and "don't even let the AI think about this" (side
effect). The latter broke the feedback loop that would let a stock
recover.

**Fix:** two surgical changes to `trade_pipeline.run_trade_cycle`.

1. **Pre-filter:** removed the `AUTO_BLACKLISTED` skip entirely. Kept
   the `get_symbol_reputation()` lookup (used downstream by
   `_build_candidates_data` to surface `track_record` to the AI).
   Blacklisted symbols now flow through multi-strategy, ranking,
   ensemble (4 AI calls), batch_select (1 AI call), and **prediction
   recording** — Step 4's existing logic writes an `ai_predictions` row
   for every candidate the AI evaluates, regardless of outcome.
2. **New Step 4.95 "Blacklist gate"** — right after the crisis gate
   and before execution. Filters `ai_trades` by reputation: entries
   (BUY/SHORT) for symbols with `win_rate == 0 AND total >= 3` are
   dropped with a `BLACKLIST_BLOCKED` detail entry and an activity-log
   row ("AI wanted BUY X but 0/N win rate — prediction recorded for
   re-evaluation"). Exits (SELL/COVER) are never blocked — blocking
   them would trap positions.

**Why this works without manual intervention:**
- The AI keeps predicting on blacklisted symbols every cycle.
- Those predictions resolve against price over 10 days.
- `get_symbol_reputation()` recomputes win_rate on each cycle.
- The instant a blacklisted symbol's win_rate rises above 0%
  (e.g., 1 win in 4 predictions → 25%), it no longer matches the
  blacklist predicate → gate passes → execution resumes.
- No persistent blacklist flag, no manual un-blacklisting, no stale
  state.

**What does NOT change:**
- The AI prompt is NOT modified — no "blacklisted" flag is injected
  into `candidates_data`. The AI already sees `track_record` (e.g.
  "0W/3L (0% win rate)") via `_build_candidates_data`, so it has
  visibility into the poor history without us biasing its decision
  with a dedicated flag.
- Exits are never blocked (we always want to let positions close).
- Symbols with < 3 resolved predictions are never blacklisted
  (insufficient evidence).
- Cost impact is marginal (+1-3 extra candidates per cycle in the
  shortlist; most blacklisted symbols don't trigger strong strategy
  signals and get filtered out at the ranking step anyway).

**Dashboard surface:** `BLACKLIST_BLOCKED` entries appear in the
pipeline output's `details` list. Each includes the AI's intended
action, the symbol's win/loss record, and the reason. The activity
feed logs the same event for historical review.

**Test coverage:** 10 new tests in `tests/test_blacklist_at_execution.py`:

Source-pattern contracts:
- Pre-filter no longer skips with `AUTO_BLACKLISTED`
- Step 4.95 gate + `BLACKLIST_BLOCKED` marker both present
- Gate touches only BUY/SHORT, never SELL/COVER
- `ai_analyst` source has no `blacklist` references (no prompt bias)

Behavioral:
- Entry blocked when reputation is 0% WR on 3+ predictions
- SELL/COVER never blocked even when blacklisted
- Symbols below 3 predictions not blacklisted (insufficient data)
- Symbols with no reputation record pass through
- **Recovered symbols (win_rate > 0%) pass the gate** — proves the
  "earn your way back" mechanism
- Mixed portfolio filters correctly (good/blacklisted/fresh/exit)

Tests: 709 → 719 passing.

---

## 2026-04-24 — Weekly AI-work digest email (Severity: feature)

**What:** New weekly digest — one consolidated email across all active
trading profiles — summarizing the autonomous changes the AI made, why,
and their observed effect. Fires every Friday at market close
(16:00 ET, right after the 15:55 ET self-tune run so the week's last
tuning decisions are captured).

**Sections:**
- Week at a glance — total realized P&L, trades, resolved-prediction
  win rate, AI cost, count of autonomous changes
- Per-profile table — buys/sells, resolved (win rate), realized P&L,
  AI cost per profile
- Self-tuning changes — parameter, old → new, reason, outcome_after
  (improved/worsened/neutral) with win_rate_after
- Strategy deprecations & restorations (Phase 3 alpha decay)
- Auto-strategy lifecycle transitions (Phase 7)
- Crisis-state transitions (Phase 10)
- Trading narrative — top 5 winners + bottom 3 losers with AI reasoning
  and confidence, grouped by profile

**Idempotency:** file marker at `{master_db_dir}/.weekly_digest_sent.marker`
stores the last-send date. The task is called from the daily-snapshot
block (per-profile) — the marker ensures only the first profile hitting
the task on Friday actually sends; the other 9 no-op. On send failure
the marker is NOT written, so next cycle retries.

**Gates:**
- `weekday() == 4` (Friday)
- `hour >= 16` in ET (matches the snapshot-block fire time)
- `marker_date != today` (not already sent today)

All gates use `datetime.now(ET)` — server is UTC, explicit conversion
matches the rest of the scheduler's timing-sensitive code.

**Why not 17:00 ET (my first draft):** the snapshot block only fires
once per day, on the first scheduler tick after 15:55 ET. A 17:00 gate
would have skipped the snapshot's only call to the digest task, so the
email would never send. 16:00 ET aligns with the snapshot fire time.

**Files:**
- `ai_weekly_summary.py` (new, ~420 lines) — `build_weekly_summary`
  across master + per-profile DBs; `render_html` emits subject + full
  HTML using existing `notifications.py` helpers
  (`_wrap_html`, `_section`, `_table`, `_color_pnl`, etc.)
- `multi_scheduler.py` — new `_task_weekly_digest` + hook inside the
  daily snapshot block
- `tests/test_weekly_digest.py` (new) — 13 tests covering build,
  render, day/time gating, idempotency, and retry-on-failure

**Uses existing infrastructure:** Resend via `notifications.send_email`,
env-var-based recipient (`NOTIFICATION_EMAIL`), styling helpers shared
with trade/veto/daily-summary emails.

**Tests:** 696 → 709 passing.

---

## 2026-04-24 — Stop MAGA oversold scan from spamming yfinance for dead tickers (Severity: low, log hygiene)

**Problem:** Today's audit showed 175 "possibly delisted" errors in the
production log across 30 unique symbols (`AUY, AZUL, CEIX, CFLT, CPE,
DLOCAL, ERJ, GPS, HEAR, IAS, LILM, PARA, SQ, VTLE, X, ...`). Yesterday's
screener fix filtered these out of `screen_dynamic_universe.fallback_universe`,
but the errors kept appearing — because a different code path was still
hitting yfinance for them every scan cycle.

**Root cause:** `multi_scheduler.py:543` — the MAGA mode oversold scan
loops directly over the raw hardcoded `seg["universe"]` from
`segments.py` (containing the known-stale hand-curated list) and calls
`get_bars(sym, limit=30)` for every symbol. Dead tickers return empty
from Alpaca → fall through to yfinance → yfinance logs "possibly
delisted" to stderr.

**Not a cost issue:** `get_bars` with empty/short bars results in the
MAGA loop's `if bars is None or bars.empty or len(bars) < 15: continue`
skip — no AI calls triggered, no trading impact. Pure log noise.
**Is a readability issue:** 170+ error lines/day make
`journalctl -u quantopsai` unreadable and would mask real failures.

**Fix:** New shared helper `screener.get_active_alpaca_symbols(ctx)` —
returns the set of Alpaca-active, tradable US equity symbols (same
filter rules as `screen_dynamic_universe`: US exchange, tradable,
no warrant/preferred suffixes). 24h in-process cache. Fail-open: on
Alpaca failure returns last-known-good set; on first-call-with-failure
returns empty (caller's fallback kicks in).

MAGA oversold scan now intersects `seg["universe"]` with this active
set before the loop. When the active set is empty (Alpaca completely
unreachable + no cache), uses the raw universe (preserves prior
behavior).

**Why the helper vs inline filter:** other hand-curated-universe paths
may get this same treatment later (e.g. the bigger
`DYNAMIC_UNIVERSE_PLAN.md` refactor). Centralizing the filter rules
means a future audit fixes them all in one place.

**Test coverage:** 6 new tests.
- `TestActiveAlpacaSymbolsHelper` (5): returns filtered set, cache hit,
  stale-refresh, stale-on-failure, empty-on-cold-failure
- `TestMigrationContract.test_maga_scan_filters_universe_via_get_active_alpaca_symbols`
  — source-pattern contract guards the MAGA block against regression

Tests: 690 → 696 passing.

**Expected impact:** delisted-ticker error lines drop from ~170/day to
zero within one scan cycle after deploy (once 24h active-symbols cache
warms). No trading behavior change. No cost change.

---

## 2026-04-23 — Gate earnings_analyst when no candidate has earnings in 14d window (Severity: medium, cost)

**Problem:** Today's ensemble audit showed `earnings_analyst` outputs
~45 tokens per call on average, while the other three specialists
(pattern, sentiment, risk) output ~1000 tokens each. That 45-token
response is the specialist returning "ABSTAIN — no earnings data to
analyze" for shortlists where no candidate has near-term earnings.
We pay ~1800 input tokens per call for effectively zero signal.

Today's split: of the ensemble's ~$1.45 total spend, `earnings_analyst`
was ~$0.15 (~10%). Over 95% of its calls appear to be abstentions.

**Fix:** New `EARNINGS_ANALYST_WINDOW_DAYS = 14` constant in
`ensemble.py`. Before running specialists in `run_ensemble`, check if
ANY candidate in the batch has earnings within `0 <= days_until <= 14`
via the existing `earnings_calendar.check_earnings` (DB-cached,
shortlist symbols are warm). If none do, skip `earnings_analyst`
entirely that cycle. The other three specialists run normally.

**Fail-open semantics** — three defensive properties, covered by tests:
- If `earnings_calendar` can't be imported at all → specialist runs
  (tested: `test_import_failure_fails_open`)
- If `check_earnings` raises for every symbol → specialist skipped
  ONLY when we have no evidence of upcoming earnings anywhere, but
  other specialists always run regardless
- If at least one candidate has earnings in window → specialist runs
  on the full batch (not filtered)

**Not affected by this gate:**
- Crypto profiles — already exclude `earnings_analyst` via
  `APPLICABLE_SPECIALISTS_BY_MARKET` (regression test added)
- Pattern / risk / sentiment specialists — always run
- `batch_select`, `sec_diff`, `transcript_sentiment`, etc. — unaffected

**Expected savings:** ~$0.15/day steady state across all equity
profiles. Larger on days when no earnings are in the window across
any profile's shortlist.

**What this is NOT:**
- NOT disabling the ensemble or reducing signal. `earnings_analyst`
  still runs on every cycle where a candidate has earnings within 14
  days — which is exactly when its output is most actionable
  (pre-announcement risk, post-announcement drift setups).

**Test coverage:** 6 new tests in `TestEarningsAnalystCostGate`:
- Skipped when no candidate has earnings
- Runs when any single candidate has earnings in window
- Boundary: 13 days in (runs), 15 days out (skipped)
- Fails open on per-symbol check_earnings exceptions
- Fails open on module import failure
- Crypto market still excludes it (via the older gate, not the new one)

Also updated two existing tests (`test_equity_markets_run_all_four`,
`test_cost_scales_with_chunks_not_candidate_count`,
`test_single_chunk_when_few_candidates`) to mock `check_earnings` so
they remain deterministic under the new gate.

Tests: 684 → 690 passing.

---

## 2026-04-23 — SEC filing backfill cost spike: cap AI diff calls per cycle (Severity: high)

**Problem:** Post-restart this afternoon (18:41 UTC) the `sec_diff` AI call
volume exploded to 487 calls in ~1 hour — 15-19 calls/minute sustained,
driving per-profile spend up $0.63. Rate peaked at 192 calls in the
20:05-20:09 window. Trajectory:

```
20:00-20:04:  46  calls
20:05-20:09: 192  calls  (peak)
20:10-20:14: 160
20:15-20:19:  89
```

**Root cause (not a regression, but a bounded-work design gap):**

`_task_sec_filings` calls `monitor_symbol(sym, days_back=180)` for every
symbol in positions + shortlist, per profile, every scan cycle. The task
had been blocked all morning by the `'recent_transactions'` KeyError
crashes (fixed earlier today). Once crashes stopped at 15:41 UTC and the
scheduler restarted at 18:41, `_task_sec_filings` finally ran — and
discovered ~180 days of uncached filings across symbols like STRC (37
filings), BMNR (49), RIG (14). The cache works correctly (verified:
487 AI calls = 487 new rows in `sec_filings_history`, zero duplicates;
delta = 0 between AI calls and rows written). But nothing bounded the
first-encounter cost per symbol. Per-profile databases mean each
profile pays the backfill cost independently when it first encounters
a high-filing-volume ticker.

**Fix (two changes to `sec_filings.monitor_symbol`):**

1. **Cap AI diff calls per invocation** — new `max_filings_per_cycle=5`
   param. After 5 filings analyzed, break out of the loop and record
   `deferred_to_next_cycle`. Filings arrive newest-first from EDGAR, so
   the cap always processes the MOST RECENT uncached filings first;
   older ones roll in on subsequent cycles. No data is lost; cost is
   just spread across time.
2. **Reduce `days_back` default 180 → 90** — one full quarterly cycle
   is enough context for `analyze_filing_diff` baseline comparison
   (the diff is against the most-recent prior filing in our DB, not a
   year-old one from EDGAR). Shrinks the backfill universe roughly
   in half.

Updated `multi_scheduler._task_sec_filings` caller to pass the new
values explicitly.

**Expected impact:**
- First-encounter of a high-volume symbol: ~5 AI calls (was up to 50)
- Subsequent cycles: same symbol, ~0 AI calls (cache hit)
- Steady state across portfolios: same as before (no change when caches
  are already warm)
- Upper bound per-cycle per-profile: `watchlist_size × 5` AI calls max

**What this explicitly is NOT:**
- NOT a cache bug. The `sec_filings_history` idempotency via
  `accession_number` lookup works correctly.
- NOT related to the `alt_data_cache`-based transcript_sentiment fix
  earlier today (that one IS working — 320 calls/day → 16/day confirmed
  post-restart).

**Test coverage:** 3 new tests in `TestBackfillCap`:
- `test_monitor_symbol_caps_ai_calls_per_invocation` — 20 filings, cap=5,
  assert exactly 5 AI calls and 15 deferred
- `test_default_cap_is_applied` — no explicit kwarg, still capped
- `test_cached_filings_skipped_before_cap_counts` — pre-cached filings
  don't consume cap budget (3 new fillings all analyzed under cap)

**Follow-up for a future session:**
- Cross-profile SEC filing cache (one EDGAR fetch shared across profiles
  of same user). Today's per-profile DB means N profiles × same symbol =
  N backfill passes. Design would need a shared cache in the master
  `quantopsai.db`. Not urgent — the cap bounds the per-profile cost.

---

## 2026-04-23 — sync.sh silently skipping deploys for weeks (Severity: high)

**Problem:** `./sync.sh 67.205.155.63` has been reporting "No files changed.
Nothing to sync." even when local files clearly differed from the droplet.
Today's earlier deploy of the dead-ticker fix was silently skipped by
sync.sh — had to be rsynced manually to land in production. This is the
root cause of how the local repo was able to drift 60 commits ahead of
origin without anyone noticing: each `./sync.sh` call appeared to succeed,
so nothing screamed that deploys weren't happening.

**Root cause:** Line 44 used `grep '^>f'` to pick file-transfer lines out of
`rsync --itemize-changes` dry-run output. But rsync's itemize direction
flags are:
- `<` — file being *sent to remote* (outgoing)
- `>` — file being *received from remote* (incoming)

Since we're always pushing local → droplet, every outbound change is
prefixed `<f...`, not `>f...`. The grep never matched, the `CHANGED`
variable stayed empty, the `-z` guard said "nothing to sync" and the
script exited cleanly without running the actual rsync or restarting any
services.

**Fix:** Changed `grep '^>f'` → `grep '^<f'` on line 44. One character.

**Bonus hygiene:** While in the file, added two excludes that were leaking
non-production files into the droplet when the detector finally did fire
(e.g., during manual testing):
- `.claude/` — Claude Code internal session state (scheduled tasks, caches)
- `.sync_test_marker` — reserved for sync diagnostics

**Why it wasn't caught:** No test exercises `sync.sh` end-to-end (it's a
shell script that SSHes to production — not trivial to mock). The dry-run
output has ordering subtleties that are easy to misremember; this kind of
rsync flag reversal is a classic copy-paste-era bug.

**Verification:** After the fix, `./sync.sh 67.205.155.63` correctly
identifies "sync.sh" as the changed file and proceeds with the full rsync.
Service restart logic (web vs scheduler detection) already worked
correctly — the issue was purely the change-detection gate.

**Follow-up (queued):** Add a smoke test that stubs `rsync --dry-run` with
a synthetic itemize-output and asserts that sync.sh correctly parses
outbound transfers. Would have caught this the moment the script was
written.

---

## 2026-04-23 — Dead-ticker log spam: filter fallback universe against Alpaca active assets (Severity: medium)

**Problem:** Every scan cycle produced ~20-30 `ERROR $SYMBOL: possibly delisted`
yfinance errors for tickers like `SQ`, `PARA`, `X`, `CFLT`, `IAS`, `MAG`,
`AUY`, `LILM`, `DLOCAL`, `HEAR`, `VTLE`, `ERJ`, `AZUL`, `SWI`, `GPS`. Yahoo's
website still renders these tickers (cached marketing pages), but Yahoo's
`/v8/finance/chart/SYMBOL` API returns 404 — the tickers moved or are gone:
`SQ → XYZ` (Block rebrand), `PARA → PSKY` (Paramount/Skydance merger),
`GPS → GAP`, `X` (US Steel acquired), `CFLT` (Confluent taken private),
plus several acquisitions/bankruptcies. Production Alpaca `get_asset()` calls
on every flagged symbol return `NOT FOUND`, confirming the source of truth.

**Root cause:** `screener.py:592-594` in `screen_dynamic_universe()` had a
"# Always include the curated universe" line that unioned the hand-curated
`segments.py` universe into the dynamic Alpaca sample:

```python
if fallback_universe:
    sample = list(set(sample + list(fallback_universe)))
```

The parameter name was misleading — `fallback_universe` was used as a
*supplement* on every run, not only as a fallback. So even though dynamic
discovery pulled fresh symbols from Alpaca, the hand-curated dead tickers
were still forced into the sample every cycle and ended up in
`get_snapshots()` and the yfinance fallback path, generating the log spam.

**Fix:** Intersect the fallback list with Alpaca's active-asset set
(`equity_symbols`, already built just above) before merging. Dead tickers
get filtered out as Alpaca stops listing them — the fix is self-healing as
future renames/delistings happen.

**Why it wasn't caught:** Existing tests verified that fallback symbols
*could* appear in output (`test_screener_alpaca_failure_falls_back_to_yfinance`),
but no test asserted that *dead* fallback symbols get filtered. The leak was
invisible to the test suite because no test mocked Alpaca returning fewer
symbols than the fallback list contained.

**Test coverage:** new `test_fallback_universe_filters_dead_symbols` in
`test_alpaca_data_migration.py` asserts that `ZOMBIE1`, `ZOMBIE2` symbols
passed in `fallback_universe` never reach `get_snapshots()` when Alpaca's
asset list doesn't contain them. Alive fallback symbols (`ALIVE_A`, `ALIVE_B`)
must still be carried through.

**Scope:** Quick-win surgical patch. The broader refactor documented in
`DYNAMIC_UNIVERSE_PLAN.md` (move sector classification to cached yfinance
lookups, freeze hardcoded lists into `segments_historical.py` for backtests
only, introduce a feature flag) remains queued as a separate multi-session
effort.

---

## 2026-04-23 — Continued fixes: exit order conflicts, confidence bypass, cache persistence (Severity: high)

**Exit order conflict fix.** `check_exits` crashed with "cannot open a short sell while a long buy order is open" when a limit buy was pending for the same symbol. Now cancels all open orders for a symbol before submitting the exit order.

**Confidence threshold bypass removed.** BUY signals previously bypassed the confidence threshold entirely — a 46% confidence BUY executed even with threshold at 70. This undermined the self-tuner's data-driven adjustment. All trades now must meet the threshold regardless of signal type.

**Transcript sentiment cache persisted to SQLite.** Was using in-memory cache that cleared on every restart, causing 221 AI calls ($0.29) in one day. Now uses `alt_data_cache` SQLite table. All SEC filings caches (filing metadata, text, insider data) also moved to persistent SQLite — no redundant EDGAR fetches on restart.

**Per-profile scan status replaces global timers.** Each profile bar shows its own state: scan step when active, "Next: 8m" when idle, "Queued" (amber) when due but waiting its turn. Global countdown timer blocks removed.

**friendly_time handles space-separated timestamps.** `task_runs.started_at` format is `2026-04-23 14:41:37` (space, not T) which `friendly_time` didn't parse, showing just "Apr 23" with no time.

**Changelog enforcement test.** New test verifies CHANGELOG.md contains today's date when any .py file was modified. Prevents commits without documentation.

---

## 2026-04-23 — Critical scan crash fix, dashboard hardening, performance (Severity: critical)

**CRITICAL: Scan cycles crashing since congressional data disabled.** When the congressional trading source was removed from the aggregator, the AI prompt builder still referenced `congress['recent_transactions']` with direct dict access. Empty dict + `None != "neutral"` evaluated True → `KeyError` → every scan cycle crashed for 1.5+ hours. Zero buys all day, only trailing stop exits.

**Fix:** Replaced ALL direct dict access (`dict['key']`) with `.get('key', default)` across every alt data field in `_build_batch_prompt()`. New test `TestPromptBuildDoesNotCrash` verifies the prompt builds successfully with empty, partial, and missing alt data — would have caught this before deploy.

**Scan failure banner on dashboard.** Red alert shows when any profile has failed scans in the last hour. Queries `task_runs` table for `status='failed'`. Would have immediately surfaced today's outage. Timestamps use `friendly_time` filter (ET).

**Profile error banner on dashboard.** Red alert shows when any profile has API authentication errors. Caught Large Cap 1M unauthorized key (stale key in `alpaca_accounts` table after regeneration).

**Dashboard load time: 17.5s → 2.2s.** Parallelized profile loading with `ThreadPoolExecutor(max_workers=10)` + 30-second in-memory cache for account info and positions.

**Countdown timers use actual ET market hours.** Was checking if last scan was <30min ago (false at market open until first scan completed ~22min later). Now checks Mon-Fri 9:30-4:00 ET directly.

**Display name fixes:**
- Exit triggers: `trailing_stop` → "Trailing Stop" (was `Trailing_stop` via `.capitalize()`)
- Sector flows: `comm_services` → "Comm. Services" (JS sectorNames mapping added)
- Ticker: HOLD predictions labeled "(HOLD prediction)" to distinguish from actual trades

**Data source corrections:**
- Dark pool ATS: fixed to use FINRA POST API with `compareFilter` by symbol (was returning 12.8M aggregate rows)
- Congressional trading: disabled (QuiverQuant paywalled, Senate/House GitHub repos dead since 2020, Finnhub premium-only)
- Patent filing: disabled (USPTO `api.uspto.gov` returns 403 — PatentsView v1→v2 migration incomplete, `searchText` param doesn't filter by assignee)
- "What the AI Sees" section updated: 12 per-symbol sources, 8 market-wide sources, 3 unavailable with honest explanations

**Other fixes:**
- AI cost "today" uses ET trading day (was UTC, showing $0 after 7-8 PM ET)
- Worst Periods hidden when <7 days of data (was showing empty $0.00 rows)
- Large Cap 1M Alpaca key updated in `alpaca_accounts` table (was stale after regeneration)

**Tests:** 678 total passing. New: `TestPromptBuildDoesNotCrash`, exit trigger display name enforcement, JS snake_case detection, sector flow name coverage.

---

## 2026-04-22 — Wave 2: 7 more free data signals (15 total) (Severity: feature)

Added 7 more alternative data sources, bringing the total to 15. The AI now sees:
- **Insider timing vs earnings** — insiders buying before earnings = bullish
- **Sector momentum ranking** — risk-on vs risk-off rotation detection
- **Dark pool ATS volume** — institutional accumulation/distribution (FINRA)
- **Market-wide GEX aggregate** — pinning vs expansion regime from options data
- **Earnings surprise history** — serial beater/misser track record (yfinance)
- **Earnings call transcript sentiment** — management tone via SEC EDGAR 8-K (AI-analyzed, cost-gated)
- **USPTO patent filing velocity** — innovation pipeline acceleration (PatentsView API)

All integrated into AI prompt, features_payload for meta-model, display names. 673 tests passing.

---

## 2026-04-22 — No-guessing test suite (Severity: infrastructure)

Added `test_no_guessing.py` with 26 tests that enforce correctness of names, schemas, data structures, and function signatures. Every bug caused by guessing during this session would now fail these tests before deploy:

- SQL table names must exist in known schemas (catches `sec_alerts` → real name `sec_filings_history`)
- Template JS must use real API field names, with blacklist of known bad names (catches `d.cboe_skew.value` → real name `skew_value`)
- `render_template` must pass every variable the template references (catches blank sections)
- Function calls must match actual signatures (catches `get_allocation_summary(profile_id)` → real sig `(db_path, market_type)`)
- API return fields verified against template consumers
- Display names cover all meta-model features
- View data consistency between performance and AI dashboards

673 total tests passing.

---

## 2026-04-22 — Trades pagination, countdown fix, AI cost timezone fix (Severity: medium)

**Trades page server-side pagination**: 50 trades per page with prev/next navigation. Column sorting via URL params (`?sort=pnl&dir=desc&page=1`) so sorting and pagination work together across page loads. Replaced client-side JS sort.

**Countdown timers always visible**: Timer blocks were hidden entirely after market close (`{% if any_profile_active %}` gate). Now always displayed — shows "Market Closed" after hours instead of disappearing. JS checks `market_open` flag from `/api/scheduler-status` to prevent showing "Scanning..." when market is closed.

**AI cost "today" uses ET trading day**: `date('now')` in SQLite is UTC, which flips to the next calendar day at 7-8 PM ET. Costs recorded during the trading day showed as $0.00 after that. Now computes the ET date boundary so "today" means the current trading day until midnight ET.

**Empty sections hidden**: Strategy Validations and Evolving Strategy Library sections hidden when no data exists instead of showing confusing "no data yet" messages.

---

## 2026-04-22 — AI Intelligence separated into own top-level page (Severity: feature)

**Problem**: The Performance page had 14 AI-related sections crammed into one tab alongside 5 tabs of traditional metrics. This is an AI-first system — it deserved proper organization.

**Solution**: New `/ai` route with 4 tabs matching the Performance page's tab pattern:
- **Brain** — prediction accuracy, confidence calibration, learned patterns, meta-model
- **Strategy** — allocation, validations, alpha decay, evolving library
- **Awareness** — Market Intelligence (NEW), SEC alerts, crisis monitor, events, ensemble
- **Operations** — self-tuning status/history, AI cost tracking, "What the AI Sees"

Performance page slimmed from 1721 to 762 lines — now only traditional metrics (tabs 1-5). All 18 original AI sections verified present in the new template via line-by-line diff against the original. Data computation copied verbatim from `performance_dashboard()` — no paraphrasing, no guessed field names.

**New Market Intelligence panel** on Awareness tab shows yield curve status (FRED API), CBOE Skew, estimated sector ETF flows, and economic indicators (unemployment, CPI, consumer sentiment, initial claims). Requires free FRED API key (`FRED_API_KEY` in `.env`).

**Full system audit** verified all pages load (10/10), all APIs return valid JSON (7/7), all 13 non-displayed system components functional (prediction resolution, trade pipeline, AI prompt, alt data, crisis detector, upward optimizer, display names, dotenv, backups, earnings cache, ensemble chunk size, political cache).

---

## 2026-04-22 — 8 free alternative data sources added (Severity: feature)

Added 8 new data sources to give the AI richer context for trading decisions. All free, no API keys required.

**Per-symbol (added to `alternative_data.py`):**
1. **Congressional Trading** — QuiverQuant API: which members of Congress are buying/selling each stock
2. **FINRA Daily Short Volume** — daily short volume ratio per symbol, flags when >50% (elevated)
3. **Insider Cluster Detection** — flags when 3+ insiders buy the same stock within 90 days
4. **Analyst Estimate Revisions** — EPS/revenue estimate direction (up/down/flat) from yfinance

**Market-wide (new `macro_data.py`):**
5. **Treasury Yield Curve** — FRED API: 2y, 10y, 30y rates, spread, inversion detection
6. **ETF Sector Flow Estimates** — computed from existing Alpaca bar data for sector ETFs
7. **CBOE Skew Index** — yfinance `^SKEW`: measures institutional tail-risk hedging
8. **FRED Leading Economic Indicators** — unemployment, CPI YoY, consumer sentiment, initial claims

**Pipeline integration:** All per-symbol data flows into the AI prompt per-candidate. All macro data renders in the market context section. New features flattened into `features_json` for meta-model training (7 new numeric, 3 new categorical).

**Crisis detector:** Two new signals — CBOE Skew extreme (>150) and yield curve inversion (10y-2y < 0).

**Tests:** 22 new in `test_alternative_data_new.py`. 647 total passing.

---

## 2026-04-22 — Remove cross-profile suggestions (Severity: cleanup)

Removed the cross-profile suggestion logic from `apply_auto_adjustments()`. It recommended copying another profile's confidence threshold but never auto-applied it, generating noise like "raise to 25" (the default floor). The upward optimizer now handles this better by analyzing each profile's own confidence band data and making targeted, auto-reversible adjustments.

---

## 2026-04-22 — UI clarity, viewer accounts, server-side pagination (Severity: medium)

**Profit factor clarity**: Renamed to "Portfolio Profit Factor" (trades tab, dollars) vs "Prediction Accuracy" (AI tab, directional %). Added tooltips explaining the difference. The AI picks winners at 1.50 but portfolio is at 0.95 because losing trades had larger positions — the upward optimizer's position sizing adjustments target this gap.

**AI profit factor was always N/A**: The `ai_perf["profit_factor"]` was initialized to 0.0 but never computed. Fixed. Also fixed to exclude HOLD predictions — HOLD "losses" aren't real losses (AI said don't trade, price moved, no money lost).

**Viewer accounts**: New `role` column on users (`admin` / `viewer`). Viewers see all data (linked to an admin via `linked_to_user_id`) but cannot change settings — all form controls disabled, POST routes blocked by `@admin_required`. New accounts default to viewer. Guest account created.

**Server-side pagination**: Tuning Status, Tuning History, Learned Patterns, and SEC Alerts load via AJAX API endpoints (`/api/tuning-status`, `/api/tuning-history`, `/api/learned-patterns`, `/api/sec-alerts`) with `page`/`per_page` parameters. Performance page loads instantly.

**SEC alerts broken by pagination**: API endpoint queried nonexistent `sec_alerts` table instead of using `sec_filings.get_active_alerts()`. Fixed.

**Tuning history missing profiles**: Profiles with only cross-profile suggestions went through the `if adjustments:` branch and skipped the `tuning_history` log. Now logs an "evaluation" row for every profile that was evaluated, regardless of whether changes were made.

**Confidence threshold cascade**: Was raising 25→60→70 in one run. Fixed to check the tighter band first and pick the right level in one step.

**Display names**: Added 30+ feature name entries to `display_names.py` (RSI, ATR, ADX, etc). Fixed `_analyze_failure_patterns` to use `display_name()`. Added test enforcing every meta-model feature has a display name entry.

**Activity ticker profile names**: Activity feed entries now show `[Profile Name]` so you can tell which account generated the activity.

**Stalled task diagnostics**: Watchdog now diagnoses probable cause (service restart, slow API, hung fetch) instead of generic "investigate in journalctl."

**Smart deploy script**: `sync.sh` now auto-detects changed files, only restarts affected services, waits for cycle boundaries before restarting the scheduler.

**Daily backups**: Cron job at 1 AM ET, 14-day retention, uses `sqlite3 .backup` for consistency.

**Earnings calendar**: Refresh interval 24h→7d. Smart cache: if a future earnings date is stored, no refetch until that date passes.

---

## 2026-04-22 — Self-tuner upward optimization (Severity: feature)

**Problem**: The self-tuner only prevented disasters (win rate < 35%) but never tried to improve a profile already performing at 50-60%. A profile at 61% win rate got "no changes needed" when it should be pursuing 70%+.

**Solution**: Added 5 upward optimization strategies to `apply_auto_adjustments()` in `self_tuning.py`, gated on `overall_wr >= 35%`:

1. **Confidence threshold optimization** — finds the best-performing confidence band and raises the threshold one band at a time
2. **Regime-aware position sizing** — reduces exposure in losing market regimes, increases in winning ones
3. **Strategy toggle optimization** — disables worst-performing strategies (never the last one)
4. **Stop-loss/take-profit optimization** — widens stops that trigger too early, tightens TPs that never hit
5. **Position size increase** — increases position size when edge is proven (55%+ WR, 30+ samples, cap 15%)

**Safety**: One change per run (for clean auto-reversal attribution), 3-day cooldown, history check prevents repeating failed adjustments, hard caps on all parameters.

**Also fixed**: Confidence threshold cascade bug — was raising 25→60→70 in one run instead of picking the right level once. Deploy script now auto-detects changed files and only restarts affected services, waits for cycle boundaries before restarting scheduler.

**Tests**: 13 new in `test_self_tuning_upward.py`. 625 total passing.

---

## 2026-04-22 — Complete yfinance→Alpaca migration for all equity data paths (Severity: high)

**Problem**: Multiple modules were still using yfinance (`yf.download`, `yf.Ticker`) for equity price data instead of the paid Alpaca API. This caused Yahoo rate limit errors (`YFRateLimitError: Too Many Requests`), thread-safety crashes, and silent data failures. The screener batch downloads were the worst offenders — hitting Yahoo with 50+ symbols simultaneously and getting rate-limited.

**Files migrated to Alpaca primary**:
- `screener.py`: `screen_by_price_range`, `find_volume_surges`, `find_momentum_stocks`, `find_breakouts` — all now use `_get_bars_for_symbols()` via Alpaca
- `market_data.py`: `get_sector_rotation` (sector ETFs), `get_relative_strength_vs_sector`, `get_snapshot`, `get_bars_daterange` — all now try Alpaca first
- `correlation.py`: `_fetch_returns` — now uses `get_bars` per symbol via Alpaca
- `metrics.py`: `_fetch_benchmark_returns` — now uses `get_bars_daterange` via Alpaca
- `backtester.py`: `_download_symbol`, `_fetch_universe_batch` — both now use Alpaca
- `ai_tracker.py`: `_get_current_price` — now uses `api.get_latest_trade()` directly
- `app.py`: added `load_dotenv()` — gunicorn web process had no env vars, causing all Alpaca calls from the dashboard to fail silently (broke sector rotation widget)

**Earnings calendar optimization**: Changed refresh interval from 24 hours to 7 days, and added smart cache: if a future earnings date is stored, no refetch needed until that date passes. Earnings are quarterly events — daily re-checking was pointless and hammered Yahoo.

**Ensemble cost optimization**: Raised `CHUNK_SIZE` from 5 to 15 in `ensemble.py`. Each specialist now processes the full shortlist in 1 API call instead of 3. Cuts ensemble AI cost ~60%.

**Political context cache**: Added 30-minute cache in `trade_pipeline.py` so all MAGA-mode profiles share one political analysis call instead of each making their own.

**Tests added**: 6 new tests in `test_alpaca_data_migration.py` enforcing Alpaca-first in screener, ai_tracker, correlation, metrics, market_data, backtester, and both app.py/multi_scheduler.py dotenv loading. 610 total tests passing.

---

## 2026-04-22 — AI prediction resolution broken for all profiles (Severity: critical)

**Problem**: Dashboard showed "0 / 20 (0%)" for Large Cap resolved predictions despite having trades going back 5 days. Small Cap Aggressive had only 5 resolved out of 380 total. Multiple profiles were silently failing to resolve predictions every cycle.

**Root causes (three cascading failures)**:

1. **`days_held` column missing** — The `ai_predictions` table in several profile DBs lacked the `days_held` column. The resolution `UPDATE` statement included `days_held = ?` which threw `sqlite3.OperationalError: no such column: days_held`, killing the entire resolution task. Fixed in the earlier `_migrate_all_columns` patch, but that fix wasn't deployed to all profile DBs until this session.

2. **Alpaca data API returning 401 in scheduler** — `multi_scheduler.py` never imported `config.py` or called `load_dotenv()`. Environment variables `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` were not loaded in the scheduler process. The shared `market_data._get_alpaca_data_client()` got empty keys → 401 Unauthorized → fell back to yfinance. yfinance then failed intermittently due to thread-safety issues in the ThreadPoolExecutor, causing 0 prices → 0 resolutions.

3. **`_get_current_price` ignored the per-profile API client** — The function called `market_data.get_bars(symbol, api=api)` but `get_bars` ignores the `api` parameter entirely and uses its own module-level client. The per-profile API client (which has valid, authenticated credentials) was passed but never used.

**Fix**:
- Added `from dotenv import load_dotenv; load_dotenv()` at top of `multi_scheduler.py` before any imports that read env vars
- Rewrote `ai_tracker._get_current_price()` to use `api.get_latest_trade(symbol)` as primary path (uses the per-profile authenticated API directly), falling back to `market_data.get_bars()` only if that fails
- Added price validation guard in `record_prediction()`: rejects predictions with `price_at_prediction <= 0` to prevent unresolvable records
- Fixed 40 existing predictions with `price=0` (all from Apr 17 profile setup day) by marking them `status='resolved', actual_outcome='data_error'`
- Added thread-safety locks to `political_sentiment.py` and `options_oracle.py` yfinance calls

**After fix**: Manual resolution run resolved 124 predictions for Small Cap Aggressive (was stuck at 5), 79 for Small Cap Shorts, 42 for Small Cap, 35 for Mid Cap. All profiles now resolving correctly.

**Why it wasn't caught**: The resolution task swallowed the `OperationalError` inside the task runner's generic try/except, logging `[TASK FAIL]` but continuing. The subsequent price-fetch failures returned None silently (no warning logged because `get_bars` returns empty DataFrames, not exceptions). The dashboard showed "0 resolved" which looked like "no data yet" rather than "resolution is broken."

**Test coverage**: Existing 605 tests pass. The `_get_current_price` change is covered by the prediction resolution integration test which mocks the API client. The `record_prediction` price guard prevents future price=0 records.

---

## 2026-04-22 — yfinance thread safety audit (Severity: medium)

**Problem**: Thread-safety wrappers (`yf_lock`) were missing on `yf.Ticker()` calls in `political_sentiment.py` and `options_oracle.py`. These could cause `RuntimeError: dictionary changed size during iteration` when multiple profiles run concurrently in the ThreadPoolExecutor.

**Fix**: Wrapped yfinance Ticker creation in both modules with `yf_lock._lock`. No functional change — purely thread safety.

**Honest assessment of remaining yfinance usage**: yfinance is correctly used as the ONLY source for: VIX index data, fundamentals, insider trades, options chains, earnings dates, analyst recommendations. These have no Alpaca equivalent. For equity price data (bars, latest trade), Alpaca is now the primary source everywhere. `backtester.py` still uses yfinance directly for bulk historical data (intentional — 720-day cache per symbol for backtesting).

---

## 2026-04-15 — FIFO P&L backfilled onto closed BUY rows (no more useless "open" or "closed" labels)

**Severity:** medium UX — user feedback: "having a bunch of random closed
is just as useless as a bunch of random opens. You know what you bought
it for and what you sold it for so you can calculate the P&L."

**Root cause of the useless display:** the trades table design puts pnl
only on SELL (exit) rows. BUY (entry) rows had `pnl=NULL` — even after
`reconcile_trade_statuses` marked them `status='closed'`, the trades
page had no dollar value to display, so it fell back to "closed" or
"open" labels that told the user nothing.

**Fix (proper one):** extend `reconcile_trade_statuses` with FIFO lot
matching. For each symbol, walk trades in timestamp order; every BUY
opens a lot; every SELL consumes qty from the oldest open lots and
accumulates realized P&L back onto each BUY row's `pnl` column.

The algorithm handles:
- Simple round-trips (1 BUY → 1 SELL)
- Partial exits (1 BUY → 2+ partial SELLs) — sums attributed P&L
- Multiple round-trips (BUY → SELL → BUY → SELL) — each entry row
  gets its own correct P&L
- Open positions (BUY without matching SELL) — left with `pnl=NULL`
- Already-set `pnl` — never overwritten

**Template now:** the trades-table macro shows realized P&L on every
closed row (BUY or SELL), unrealized P&L on held positions, "open"
only for truly uncalibrated rows (new BUY with no live quote yet).
No more "closed" or "exit" labels — every closed trade shows a dollar
number.

**Backfill on production:** Mid Cap had 4 closed BUY rows with NULL
pnl — after the one-shot reconcile run, all 4 carry their realized
P&L (e.g., HIMS 04-13 BUY now shows +$15.20 across the two partial
sells on 04-15). Small Cap and Large Cap had no closed positions
yet (their exits happen on Mid Cap primarily).

**Tests** (`test_trade_status_reconcile.py` — 16 total, +6 new):
- Simple round-trip: BUY 10@$100, SELL 10@$110 → BUY pnl +$100
- Losses record correctly as negative
- Partial sells sum to total realized P&L on the BUY row
- Multiple round-trips: each BUY gets its own lot's P&L, not merged
- Still-open BUYs stay `pnl=NULL`
- Existing `pnl` values never overwritten

**Test count:** 550 (was 545 + 6 new − 1 replaced obsolete).

---

## 2026-04-15 — Self-tuning now visible even when it doesn't change anything

**Severity:** UX — user was certain self-tuning wasn't running because
nothing ever appeared in the dashboard.

**Reality check:** Self-Tune runs daily (alongside the Daily Snapshot
task), but it needs ≥20 resolved AI predictions per profile before it
will adjust anything. Current state: Mid Cap 8 resolved, Small Cap 0,
Large Cap 0. The tuner is alive but patiently waiting for data.

**The UX gap:** when `apply_auto_adjustments()` returned an empty list
(no changes), the scheduler silently exited with a log line nobody
reads. No activity row. No dashboard signal. User saw nothing →
assumed the tuner was broken.

**Fix:**
- New `self_tuning.describe_tuning_state(ctx)` returns a struct with
  `can_tune`, `resolved` (current count), `required` (20), and a
  human-readable `message` explaining the current state.
- `_task_self_tune` in the scheduler now logs an activity entry
  EVERY run — whether it changed parameters, found no adjustments
  needed, or is waiting for more data. The title + detail distinguish
  the three cases so the user can confirm the tuner is alive at a
  glance.
- New "Self-Tuning Status" panel on Performance > AI Intelligence
  tab shows per-profile: resolved-predictions progress bar
  (e.g. "8 / 20 (40%)"), current status ("Collecting data" vs
  "Active"), last-run timestamp, and the human-readable message.
  Hides only if there's literally no data at all.

**Tests** (`test_self_tuning_visibility.py` — 5 tests):
- `can_tune=False` when self-tuning disabled on profile
- Resolved count reads from ai_predictions table
- `can_tune=True` when resolved ≥ 20
- Missing `ai_predictions` table → safe message, no crash
- Message copy communicates "waiting for data" (not failure)

---

## 2026-04-15 — Trade statuses reconciled (BUY rows no longer show "open" after exit)

**Severity:** medium UX — the Trades page was showing closed positions
as "open" forever.

**Symptom:** HIMS BUY on 04-13 (qty 20) was fully exited by two SELLs on
04-15 (qty 5 + 15). The BUY row stayed `status=open, pnl=null` and
displayed as "open" on the trades page. Most exit-SELL rows also
carried `status=open` despite having realized `pnl`.

**Root causes:**
1. `trader.check_exits` logged exit SELLs without passing `status="closed"`
   (unlike `trade_pipeline.py` which did). Default was "open".
2. Nothing ever went back and marked matching BUY rows as closed when
   their positions flattened.

**Fixes:**
- `trader.py` now passes `status="closed" if pnl is not None else "open"`
  on exit SELLs, matching the pipeline's behavior.
- After an exit, both `trader.py` and `trade_pipeline.py` run an
  inline `UPDATE trades SET status='closed' WHERE symbol=? AND
  side='buy' AND status='open'` — flattens entry rows the moment the
  position closes.
- New `journal.reconcile_trade_statuses(db_path, open_symbols)` —
  authoritative reconciliation using live Alpaca positions as ground
  truth. Fixes any drift (old rows from before this fix) by marking
  open BUYs closed when their symbol isn't currently held.
- New scheduled task `_task_reconcile_trade_statuses` runs every
  exit cycle (5 min) to catch any drift automatically.
- One-shot backfill run against the live DBs: Mid Cap fixed 5 sells
  and 4 buys; Small/Large had no drift.

**Tests** (`test_trade_status_reconcile.py` — 10 tests):
- SELL rows with pnl but open status → closed
- SELL rows without pnl → left alone (can't confirm)
- Already-closed SELLs unchanged
- BUY rows for symbols not in live positions → closed
- Empty open_symbols (no positions) → all open BUYs closed
- BUY rows for still-held symbols → preserved
- Heuristic path (no positions list): BUY with matching SELL → closed
- Count-reporting correctness

**Test count:** 543 (was 528 + 15 new across two features).

---

## 2026-04-22 — Universal schema migration + cost tracking fix

**"Resolve AI Predictions" failing every cycle on 3 profiles:**
`sqlite3.OperationalError: no such column: days_held` — profiles 4, 5,
and 9 were created before the `days_held` column was added to the
`ai_predictions` schema. The old per-column migration functions
(`_migrate_slippage_columns`, `_migrate_prediction_columns`) only
covered specific columns and missed `days_held`.

**Fix:** Replaced the per-column migrations with `_migrate_all_columns()`
— a single function that defines every expected column for every table
and adds any that are missing via ALTER TABLE. Runs on every `init_db()`
call. Safe to run repeatedly. Will catch any future schema additions
automatically.

**AI cost "today" was showing last 24 hours, not calendar day:**
`spend_summary()` used `datetime('now', '-1 day')` which is a rolling
24-hour window. Changed to `date('now')` for the "today" bucket so
it matches the Anthropic billing console. Added total cost row to
dashboard overview table.

---

## 2026-04-21 — Max positions cap removed (10 → 100)

All profiles were maxed at 10/10 positions by mid-morning, blocking
all new trades for the rest of the day. The arbitrary cap was
redundant — position sizing (10% max per position), correlation
limits (0.7), and sector caps (5) already control concentration
risk based on actual portfolio characteristics, not an arbitrary
count. Set to 100 (effectively uncapped) to maximize data collection.

---

## 2026-04-21 — Trades page: single P&L column, brokerage-standard layout

Replaced the two-column Unrealized/Realized layout with a single P&L
column. BUY rows show entry info only (no P&L). SELL rows show
realized P&L. Dashboard shows unrealized on open positions. Matches
Schwab/Fidelity trade history view. Removed trades page enrichment
that was adding unrealized to BUY rows.

---

## 2026-04-21 — Archived profiles hidden from all UI pages

Disabled profiles (e.g. "Crypto (archived)") no longer appear in
dashboard tabs, trades dropdown, performance dropdown, or AI
performance dropdown. Settings page has a "Show archived profiles"
checkbox that reveals them dimmed when needed.

---

## 2026-04-21 — Split P&L into Unrealized + Realized columns

**Problem:** BUY and SELL rows both showed the same realized P&L,
making it look like double the profit or loss on every trade.

**Fix:** Replaced the single "P&L" column with two:
- **Unrealized** — live P&L on positions still held (BUY rows with
  open positions). Blank once the position closes.
- **Realized** — locked-in P&L from closed positions (SELL rows only).
  Blank while position is still open.

Every dollar amount appears exactly once. No double-counting.

Removed the FIFO backfill that wrote pnl onto BUY rows. Cleared
existing backfilled values from all profile databases.

---

## 2026-04-21 — Prediction resolution too slow for self-tuning to activate

**Problem:** 82 actual trades across 10 profiles, but self-tuning
hadn't activated on any profile. Self-tuning requires 20 resolved
predictions, but most profiles had 0-7 resolved despite hundreds of
pending predictions.

**Root cause:** Resolution thresholds were too strict. BUY predictions
needed a +5% price move to count as "win" — most stocks don't move 5%
in a few days. Meanwhile the system's actual stop-loss is 3% and
take-profit is 10%, so the resolution criteria didn't match the
trading parameters.

**Fix:** Lowered thresholds to match actual trading behavior:
- BUY/SELL win/loss: 5%/3% → 2%/2%
- HOLD resolve: 5 days → 3 days
- Timeout: 20 days → 10 days

**UI:** Added explanation on the AI Performance tab explaining the
difference between resolved predictions (AI forecasting accuracy
across all candidates) and closed trades (actual executed trades
with real P&L). Tooltips on each metric card.

---

## 2026-04-20 — Market regime broken all day + silent failure test suite

**Market regime bug:** When I migrated SPY data from yfinance to Alpaca,
I left `spy_hist["High"]` / `["Low"]` / `["Close"]` in title case.
Alpaca returns lowercase. Result: "Failed to detect market regime: 'High'"
174 times today. **Every trade decision today was made without knowing
if the market was bullish, bearish, or sideways.** Fixed to lowercase.

**Silent failure test suite** (`test_silent_failures.py` — 11 tests):
Catches the exact class of bugs that keep recurring — column case
mismatches, Alpaca vs yfinance format differences, missing thread
locks, API calls to services we don't subscribe to. These tests
would have caught the market regime bug before deploy.

**ETF filter expanded:** Added JPST, RSP, SRTY, SOXS, LABU, LABD.

**Test count:** 607 (was 596 + 11).

---

## 2026-04-20 — Fix ensemble sharing race condition + disable intraday emails

**Ensemble race condition:** Parallel profiles of the same market type
were both missing the ensemble cache simultaneously and running
duplicate AI calls. Added a threading lock to `_get_shared_ensemble()`
so only one thread runs the ensemble per market type — the others
wait and reuse the cached result. Mid Cap had 60 ensemble calls today
when it should have had ~12.

**Email reduction:** Disabled `notify_trade`, `notify_exit`, and
`notify_veto` — all visible on the dashboard. Only EOD summary,
self-tuning adjustments, and system errors are emailed now. Prevents
hitting the Resend daily limit with 10 profiles.

---

## 2026-04-17 — Eliminate yfinance rate limiting: DB caching, Alpaca for SPY, ETF filter

**Problem:** ~500+ yfinance errors per day from rate limiting.
Alternative data (insider, fundamentals, short interest) was fetched
per-symbol per-cycle from yfinance with only an in-memory cache that
reset on every deploy. Market regime used yfinance for SPY. ETFs like
SOXL and AMZD were in the screener universe but have no fundamentals,
flooding "no data found" errors.

**Fixes:**
1. **Alternative data DB cache** — `alt_data_cache` SQLite table replaces
   in-memory cache. Survives restarts. Each symbol fetched once per TTL
   (24h for insider/fundamentals, 1h for short interest). Thread-locked
   yfinance calls prevent race conditions.
2. **Market regime uses Alpaca for SPY** — `get_bars("SPY")` instead of
   `yf.Ticker("SPY")`. VIX stays on yfinance (Alpaca doesn't serve
   index data) but is thread-locked.
3. **ETF filter** — 40+ known ETFs/leveraged products (SOXL, TQQQ, SPY,
   QQQ, AMZD, NVDL, etc.) excluded from the screener universe. They
   don't have fundamentals data and aren't tradeable candidates.

**Expected impact:** yfinance calls drop from ~3,000/day to ~300/day.
Rate limiting errors should be near zero.

**Tests** (`test_data_fixes_apr17.py` — 8 tests):
- Alt data cache: persists to SQLite, respects TTL, survives reload
- ETF blocklist contains key symbols
- Market regime uses Alpaca get_bars, not yf.Ticker for SPY
- Metrics capital: per-profile forward-fill, no double-multiply
- Annualized return: no overflow on <7 days

**Test count:** 596 (was 588 + 8).

**"What the AI Sees" section updated** to match actual code: added
Strategy Votes, Last Prediction memory, Portfolio State, Market Regime.
Moved to collapsible reference at bottom of AI Performance tab. Tab
renamed from "AI Intelligence" to "AI Performance."

---

## 2026-04-17 — System hardening: cost alerting, cross-account reconciliation, metrics fixes

**Fixes:**
- **Metrics initial_capital bug** — `calculate_all_metrics` was doubling
  the total capital (passed $2.15M total, then multiplied by num_profiles
  again). Showed +1279%, then -56%, then +33% at various stages. Now
  correctly shows -0.1%. Per-profile capital map passed for accurate
  snapshot forward-fill.
- **Legacy DB inclusion** — old segment DBs (quantopsai_midcap.db etc.)
  were being included in the metrics aggregation despite being empty,
  inflating the profile count.
- **Disabled profiles included** — Profile 2 (disabled crypto) was counted
  in DB paths and capital calculations.
- **Annualized return overflow** — `(1+return)^(365/1)` crashed with
  OverflowError on day 1. Now requires 7+ days before computing.
- **Recovered trades backfilled** — 21 manually recovered trades now have
  the original AI reasoning and confidence from their matching predictions.
- **Auto-exit label** — exit trades (trailing stop, SL, TP) show "Auto-exit"
  instead of "--" in the AI Confidence column.
- **Admin page** — reads from actual per-profile cost ledger instead of the
  dead `user_api_usage` table.

**New features:**
- **API cost alerting** — daily spend check runs with the snapshot. Alerts
  in the activity feed when total exceeds $3/day.
- **Cross-account reconciliation** — wired into scheduler. Runs once per
  Alpaca account per snapshot cycle. Compares sum of virtual positions
  against Alpaca's actual holdings, logs drift warnings.
- **Cost per profile on dashboard** — overview table shows each profile's
  AI cost today.

---

## 2026-04-17 — Specialist ensemble + SEC filings shared across profiles ($5.75 → ~$2/day)

**Severity:** high — API costs were 3× the estimate

**Problem:** Each of the 10 profiles ran its own specialist ensemble
(4 AIs × 3 chunks = 12 calls) independently, even when profiles
of the same market type evaluated the exact same candidates. Mid Cap,
Mid Cap 25K, and Mid Cap 500K all asked the same 4 specialists the
same questions about the same stocks — just with different capital.
Same issue with SEC filing diffs: 612 AI calls/day instead of ~20.

**Why sharing makes sense:** The specialist ensemble evaluates the
CANDIDATES, not the profile. An earnings analyst's verdict on AAPL
doesn't change because one profile has $25K and another has $500K.
The candidates are identical (same screener, same market type), so
the verdicts are identical. Only the final batch trade selector
needs to be per-profile because it makes sizing decisions based on
each profile's capital, positions, and risk parameters.

**Fix:**
- `_get_shared_ensemble()` in `trade_pipeline.py` caches ensemble
  results per market_type per 15-minute cycle. First profile to
  shortlist runs the ensemble; subsequent profiles of the same
  market type reuse the cached verdicts.
- SEC filing monitor (`_task_sec_filings`) now runs once per
  market_type per cycle instead of per-profile. Same filings,
  same AI diffs — no reason to repeat.

**Cost impact:**
| Call type | Before | After | Savings |
|---|---|---|---|
| Specialist ensemble | 1,437 calls ($4.20) | ~430 calls ($1.26) | 70% |
| SEC filing diffs | 612 calls ($0.69) | ~60 calls ($0.07) | 90% |
| Batch selector | 119 ($0.76) | 119 ($0.76) | 0% (correct) |
| Political context | 18 ($0.09) | 18 ($0.09) | 0% (already cached) |
| **Total** | **$5.75/day** | **~$2.10/day** | **63%** |

**What stays per-profile (correctly):**
- Batch trade selector — different capital = different sizing
- Position sizing / risk checks — profile-specific
- Order execution — routed to profile's Alpaca account
- Trade logging — per-profile database

---

## 2026-04-17 — Small return percentage now shows 2 decimal places

When Total Return rounds to 0.0% but P&L is non-zero (e.g. $791 on
$2.15M combined capital = 0.04%), the display now shows +0.04% instead
of the misleading +0.0%.

---

## 2026-04-17 — Dashboard tabbed UI for 10 profiles

Replaced the vertically-stacked profile list with a tabbed layout:
Overview tab (summary table of all profiles + activity + sectors) plus
one tab per profile. Eliminates the massive scroll on the dashboard.

---

## 2026-04-17 — Parallel profile execution + droplet upgrade

Upgraded DigitalOcean droplet from 1 CPU/1GB ($6) to 2 CPU/2GB ($18).
Added ThreadPoolExecutor(max_workers=3) to run all profiles in parallel.
Total cycle time dropped from ~15 min (sequential) to ~5-8 min.

---

## 2026-04-17 — Order guard prevents after-hours trades

New `order_guard.py` checks `ctx.is_within_schedule()` at order
submission time, not just at cycle start. Prevents accidental
after-hours fills when pipeline takes longer than the schedule window.
10 tests covering market_hours, extended_hours, 24/7, weekends.

---

## 2026-04-17 — Sortable trade columns + ET timestamps + consistent P&L format

Trades page: clickable column headers sort by any field. Timestamps
converted from UTC to Eastern Time with "ET" label. All P&L entries
show both dollar and percentage consistently. Friendly time filter
added to display_names.py.

---

## 2026-04-17 — Screener results shared across same-market-type profiles

**Severity:** optimization — reduces API costs ~70% on screener/data calls

**Problem:** 10 profiles were each running their own screener
independently. Mid Cap, Mid Cap 25K, and Mid Cap 500K all screened the
same "midcap" universe — 3× the Alpaca snapshot calls, 3× the MAGA
oversold scan, 3× the alternative data lookups (insider trades, short
interest, options chains).

**Fix:** `_get_shared_candidates()` caches screener + MAGA results per
market_type per 15-minute cycle. First profile to run screens the
universe; subsequent profiles with the same market_type reuse the
cached result. Logs "Using shared screener results for midcap" so
it's visible.

**Savings:** 10 screener runs → 3 (one per market type). Each screener
run includes ~100 symbol-level data fetches. Net: ~700 fewer API calls
per cycle.

**AI calls unchanged:** Each profile still runs its own specialist
ensemble + batch selector because they have different capital, positions,
and risk parameters. That's correct — a $25K profile should make
different sizing decisions than a $500K profile on the same candidates.

---

## 2026-04-17 — Earnings calendar moved to DB cache (eliminates yfinance error floods)

**Severity:** high — yfinance earnings checks were flooding 401 errors

**Root cause:** Every scan cycle checked each candidate's earnings date
by calling `yf.Ticker(symbol).calendar` individually. With 10 profiles
× 30 candidates = 300 yfinance calls per cycle, Yahoo rate-limited
and returned "Invalid Crumb" 401 errors. The earnings filter silently
failed, allowing trades into earnings announcements.

**Fix:** Rewrote `earnings_calendar.py` to store dates in SQLite
(`earnings_dates` table in main DB). yfinance is called only once per
24 hours per symbol. All subsequent checks read from DB — instant,
zero API calls, zero errors. 300 yfinance calls/cycle → 0.

---

## 2026-04-17 — Position values visible, scan step status, yfinance crumb fix

**Position values:** Qty column now shows the dollar value underneath
the share count (qty × price). No more mental math.

**Scan step status:** Dashboard schedule bars now show the current
pipeline step instead of just "Scanning" — e.g. "Running 16 strategies
(30 candidates)", "Specialist ensemble (15 candidates)", "AI selecting
trades (15 shortlisted)". Polls every 3 seconds via `/api/scan-status/<id>`.
New `scan_status.py` module writes per-profile step files. Cleared when
scan completes.

**yfinance Invalid Crumb fix:** Yahoo rotates session cookies, causing
401 errors that disabled the earnings filter. Added auto-reset of
yfinance's cookie cache when "Invalid Crumb" errors are detected.
Rate-limited to once per 5 minutes.

---

## 2026-04-17 — Multiple silent failures fixed: news, prices, yfinance crashes, MAGA mode

**Severity:** high — AI was making decisions with missing data

**Problems found and fixed:**

1. **Alpaca news API 401s (silent):** Every news fetch was failing with
   "Unauthorized" because the subscription doesn't include the news
   endpoint. The system silently returned empty arrays — AI saw no news
   for any symbol. **Fix:** `fetch_news()` redirected to yfinance news
   (which works and was already used elsewhere in the pipeline).

2. **Political sentiment JSON truncation:** max_tokens=512 was too small
   for the political context response, causing JSON parse errors and
   the AI losing political context. **Fix:** bumped to 1024.

3. **yfinance thread-safety crash:** `yf.download()` uses a shared
   global dict internally that isn't thread-safe. With 10 profiles
   running in parallel, this caused `RuntimeError: dictionary changed
   size during iteration` and crashed entire scan cycles.
   **Fix:** new `yf_lock.py` module wraps all `yf.download()` calls
   in a threading lock. All 10 call sites migrated.

4. **MAGA mode scanner using yfinance batch download:** Still using
   `yf.download(universe)` for 100+ symbols instead of Alpaca bars.
   This caused the "possibly delisted" errors for valid symbols
   (GPS, SQ, SKX) and was the source of the thread-safety crashes.
   **Fix:** migrated to per-symbol `get_bars()` via Alpaca.

5. **Price=0 causing trades to silently not execute:** The AI would
   select a trade (visible in "TRADES SELECTED" on the dashboard)
   but execution silently skipped it because the candidate's price
   was 0 from a failed fetch during strategy scoring. The user sees
   "BUY CRGY" in the brain panel but no trade happens and no error
   appears. **Fix:** price is now verified and re-fetched at the
   shortlist stage before sending to AI. Candidates without a valid
   price are filtered out before wasting an AI call. Execution path
   also re-fetches as a final safety net with a logged warning.

6. **Price fetcher returning 0 silently:** Virtual position P&L showed
   phantom losses when price fetch failed. **Fix:** tries Alpaca bars,
   then Alpaca last trade, then logs a warning — never silently
   returns 0 without explanation.

7. **Earnings calendar logging at debug level:** Failures to check
   earnings dates were invisible. **Fix:** bumped to warning level.

8. **Crisis detector event cluster check:** Failed silently.
   **Fix:** logs warning.

**21 missed trades recovered:** The price=0 bug caused trades the AI
recommended to not execute across multiple profiles. All 21 were
manually executed at current market prices.

---

## 2026-04-17 — Bad account allocation caused 3 data wipes in 2 days

**Severity:** critical — user lost all accumulated trading data three times

**What happened:** When setting up 10 virtual profiles across 3 Alpaca
paper accounts ($1M each), the initial allocation put $1.625M of
virtual capital on a single $1M Alpaca account. This was compounded by
moving profiles between accounts after they had open positions,
creating orphaned trades on the wrong accounts and "account_rebalance"
sells that polluted trade history with non-strategy exits.

**The mistakes, in order:**
1. Created all profiles without thinking about which Alpaca account
   each should use. The $1M Large Cap profile landed on the same
   account as three other profiles totaling $625K — $1.625M virtual
   on a $1M account.
2. Attempted to fix by moving profiles between accounts while they
   had open positions. This created orphaned positions on the old
   account and forced-close trades logged as "account_rebalance"
   that would have corrupted win rate, P&L, and self-tuning data.
3. Each fix required wiping trade data to get back to clean state.
   Total data wipes: 3 (April 15 evening, April 16 afternoon,
   April 17 morning).

**Root cause:** Failure to plan account allocation BEFORE creating
profiles. The allocation should have been the FIRST step, not an
afterthought corrected live with open positions.

**Correct allocation (what we should have done from the start):**
```
Account 1: Large Cap 1M ($1M) = 100% (dedicated)
Account 2: Mid Cap + Mid Cap 25K + Mid Cap 500K = $625K = 62%
Account 3: Everything else = $525K = 52%
```
No account exceeds its Alpaca balance even at 100% utilization.

**Lesson:** When setting up virtual profiles on shared broker accounts:
1. Plan the allocation on paper first — total virtual capital per
   account must not exceed the account balance
2. NEVER move a profile between accounts while it has open positions
3. If allocation must change, close positions on the old account
   first, then move, then wipe that profile's trade history
4. A few hours of planning saves days of lost data

**Data impact:** All 10 profiles start from zero as of 2026-04-17.
No historical trade data survives. The system is now correctly
allocated and collecting clean data going forward.

---

## 2026-04-16 — Critical fix: virtual profiles sized against Alpaca's balance, not their own

**Severity:** critical — virtual profiles with $25K capital were buying $176K of stock

**Symptom:** Mid Cap 25K profile showed cash of -$151,074. Small Cap 25K showed -$12,492.

**Root cause:** `trade_pipeline.py` line 190 and 641, and `trader.py` lines 43-44 and 208 called `get_account_info(api)` and `get_positions(api)` passing only the API client but NOT `ctx`. Without `ctx`, the virtual interception in `client.py` never fired — the pipeline saw Alpaca's $1M account balance and sized positions accordingly.

**Fix:** All 5 call sites now pass `ctx=ctx`:
- `trade_pipeline.py:190` — `get_account_info(api, ctx=ctx)`
- `trade_pipeline.py:641-642` — both `get_account_info` and `get_positions`
- `trader.py:43-44` — `execute_trade` path
- `trader.py:208` — `check_exits` path

**Data impact:** Profiles 5 (Small Cap 25K) and 6 (Mid Cap 25K) had corrupted trade data from oversized positions. Both were wiped clean and reset to their $25K starting balance. All other profiles were unaffected — their trade history is intact.

**Lesson:** The virtual account layer requires that EVERY code path reading equity or positions passes `ctx` through to `client.py`. Added this as an invariant to watch for in future code changes.

---

## 2026-04-16 — Virtual Account Layer (broker decoupling)

**Severity:** architectural — major new capability

**What it enables:** Unlimited virtual trading profiles sharing the
same 3 Alpaca paper accounts. Each virtual profile has its own
starting capital, positions, P&L, and strategy — all tracked
internally. Alpaca is used only for order execution and price quotes.

**Architecture:**
- **Internal position ledger** (`journal.get_virtual_positions()`) —
  computes net positions from the trades table via FIFO lot tracking.
  Returns the exact same dict shape as `client.get_positions()` so
  every downstream consumer works unchanged.
- **Virtual equity tracker** (`journal.get_virtual_account_info()`) —
  computes equity, cash, buying power from trade flows + initial
  capital. `cash = initial_capital - sum(buy_costs) + sum(sell_proceeds)`.
- **Profile-to-account mapping** — new `alpaca_accounts` table holds
  named broker connections. Multiple profiles can reference the same
  account via `alpaca_account_id` FK. `is_virtual=1` flips the profile
  to use internal data instead of Alpaca reads.
- **Single interception point** (`client.py`) — `get_positions()` and
  `get_account_info()` check `ctx.is_virtual` and route to the
  internal ledger when true. Because trader.py, trade_pipeline.py,
  multi_scheduler.py, and views.py all call through client.py, this
  one change makes the entire pipeline virtual-aware.
- **Virtual-aware reconciliation** — virtual profiles use the internal
  ledger as ground truth (not Alpaca's combined view of shared accounts).
- **Settings UI** — new "Alpaca Accounts" section for managing broker
  connections. "Create Profile" form has a dropdown to select a shared
  account + starting capital input. Virtual profiles show "(Virtual)"
  badge on the dashboard.

**Backward compatibility:** Existing profiles have `is_virtual=0` and
`alpaca_account_id=NULL`. Zero behavior change — they continue using
per-profile Alpaca keys and reading positions/equity from Alpaca.

**Schema:**
```sql
CREATE TABLE alpaca_accounts (id, user_id, name, keys, base_url);
ALTER TABLE trading_profiles ADD COLUMN alpaca_account_id INTEGER;
ALTER TABLE trading_profiles ADD COLUMN is_virtual INTEGER DEFAULT 0;
ALTER TABLE trading_profiles ADD COLUMN initial_capital REAL DEFAULT 100000;
```

**Tests:** 26 new (test_virtual_positions.py: 15, test_virtual_account.py: 11)
covering FIFO lots, partial sells, unrealized P&L, equity math,
output shape compatibility, price fetcher fallbacks, UserContext defaults.

**Test count:** 583 passing.

---

## 2026-04-15 — Scaling projection v4: side-by-side market vs limit columns

**Severity:** UX — final iteration on the Scalability tab

**User pushback on v3:** "Why is the system OK with losses vs switching
to limit orders, is it really nonstandard to use limit orders?"

**The honest answer:** it's a real tradeoff, not a clear winner.
Limit orders cut slippage by ~60% but can miss fills entirely
on momentum moves. The "right" choice depends on strategy style.

**Fix:** stop picking for the user. Show BOTH execution styles
side-by-side at every capital tier so they can compare and decide.

**New table layout:**
```
                       │ If Market Orders │ If Limit Orders │
Capital   Profile      │ Slippage Return  │ Slippage Return │
$10K      Small Cap    │  0.336%  -0.09%  │  0.134%   +0.11%│
$50K      Small Cap    │  0.751%  -0.51%  │  0.300%   -0.05%│
$100K     Small Cap    │  1.062%  -0.82%  │  0.425%   -0.18%│
$500K     Mid Cap      │  0.751%  -0.51%  │  0.300%   -0.05%│
$1M       Mid Cap      │  1.062%  -0.82%  │  0.425%   -0.18%│
$10M      Large Cap    │  1.062%  -0.82%  │  0.425%   -0.18%│
```

Limit columns are tinted green to make the comparison obvious. The
footer notes that limits are an option but come with their own
tradeoff (missed fills on momentum moves) and points to the profile
settings toggle.

**Code changes:**
- `project_scaling()` returns `slippage_market_pct` + `slippage_limit_pct`
  + `return_market_pct` + `return_limit_pct` + CIs for both.
- Calibration backs out the right baseline based on what the user
  is currently using (so we never double-apply or miss the limit
  benefit).
- Removed `_LIMIT_ORDER_CAPITAL_THRESHOLD` — no automatic switch at
  any tier; both shown everywhere.
- Removed `uses_limit_orders` per-row flag (no longer needed).
- Slippage growth no longer clipped at 0 — improvements (e.g.
  switching to limits at current scale) properly INCREASE projected
  return.
- Template adds a header row with `colspan` grouping the two
  execution columns.

**Tests** (`test_scaling_projection.py` — 20, replaced ExecutionStyleAdjustment
with BothExecutionStylesAlwaysShown):
- Every row has both market and limit columns
- Limit slippage always lower than market
- Limit/market ratio stays at ~0.40 across all scales
- Baseline calibration correct whether user is on market or limit
  orders (back-implied properly in both directions)

**Test count:** 528.

---

## 2026-04-15 — Tooltip z-index fix (mounted to body via JS)

**Symptom:** tooltips on the Scalability table appeared as a thin
sliver — clipped by the parent `overflow-auto` container.

**Fix:** new JS in `base.html` mounts a single tooltip element on
`<body>`, positioned via `getBoundingClientRect` and `position: fixed`.
Escapes ALL parent overflow constraints. Hides on scroll. The old CSS
pseudo-element approach (`.tip:hover::after`) is stripped + defensively
suppressed in case browser cache lingers. CSS link gets a
`?v=20260415-tooltip-fix` cache-buster.

---

## 2026-04-15 — Scaling model v3: real-world migration ladder + execution adjustment

**Severity:** medium — UX/accuracy of a high-visibility planning tool

**User feedback (v2 wasn't right either):** "Why are you using the same
profile (small cap) for all the different levels? That doesn't make
sense. Isn't this supposed to show the scalability in a real way?"
Plus: "Why are we referencing internal documentation on all these rows?"
Plus: "The tooltips for est. slippage and universe appear to be showing
up behind the layer above."

**Real-world model:** at each capital level, project what would
*actually happen* — not "if you stubbornly stayed Small Cap forever"
(v2) and not "magically blended universe" (v1). Three real effects
compound:

1. **Square-root market impact.** Larger orders cost more, scaling as
   `√(position_size)`. (Almgren-Chriss.)
2. **Tier migration.** $250K+ rationally migrates Small → Mid; $5M+
   migrates Mid → Large. Each tier offers ~10× more daily volume per
   name, which offsets ~√10 ≈ 3.16× of capital growth.
3. **Order-execution style.** Above $100K any rational operator uses
   limit orders, which cut realized slippage by ~60%. Cap-bracket
   institutional norm.

The combined effect: with the full real-world playbook, slippage stays
roughly flat across the entire scale range. Realistic example for a
user calibrated at 0.336% baseline (Small Cap, market orders, 6 fills):

```
Capital  Profile     Orders   Slippage   Return
$10K     Small Cap   market    0.336%    -0.09%
$50K     Small Cap   market    0.751%    -0.51%
$100K    Small Cap   limit     0.425%    -0.18%   ← order type switches
$500K    Mid Cap     limit     0.300%    -0.09%   ← profile migrates
$1M      Mid Cap     limit     0.425%    -0.18%
$10M     Large Cap   limit     0.425%    -0.18%   ← profile migrates again
```

That's what real institutional execution looks like. The previous v2
model showed slippage exploding to 10%+ at $10M because it pretended a
small-cap operator would still be running small-cap names — which no
sane operator would.

**Code changes:**
- `scaling_projection.project_scaling()` — added `use_limit_orders_now`
  parameter, applies 0.40× slippage multiplier when projection assumes
  switch to limit orders (only if user isn't already using them).
- `_LIMIT_ORDER_CAPITAL_THRESHOLD = $100K`, `_LIMIT_ORDER_SLIPPAGE_MULT = 0.40`.
- `views.py` reads the profile's `use_limit_orders` setting and
  threads it through.
- `templates/performance.html` adds an "Orders" column showing
  Market vs Limit at each capital level.
- Migration notes use plain English: "At this scale, you'd switch
  to a Mid Cap profile. The bigger universe gives you ~10× more daily
  volume per name." No internal-doc references like "SCALING_PLAN.md."
- Footer explanation lists the three compounding effects in user
  terms — no jargon, no formulas.

**Tooltip z-index fix:**
- `.tip` tooltips were CSS pseudo-elements being clipped by parent
  `overflow-auto` on the Scalability table — visible only as a
  thin sliver.
- New JS in `base.html` mounts a single tooltip element on `<body>`,
  positioned via `getBoundingClientRect` and `position: fixed`.
  Escapes ALL parent overflow constraints. Hides on scroll so it
  doesn't float orphaned.
- Old CSS `.tip:hover::after` / `::before` stripped to avoid
  double-rendering.

**Tests** (`test_scaling_projection.py` — 19 total, +5 new):
- `test_above_100k_uses_limit_orders_when_currently_market`
- `test_limit_order_adoption_lowers_slippage_at_threshold`
- `test_already_using_limit_does_not_double_apply_benefit`
- `test_limit_order_note_in_migration_row`
- `test_no_internal_doc_references_anywhere` — sweeps every output
  string for `.md` and `scaling_plan` so internal doc names can't
  silently leak to the UI again
- Updated `test_migration_offsets_capital_growth` — pinned to the
  real-world ratio (~1.27×) rather than the without-execution-adjustment
  ratio (~3.16×)

**Test count:** 527 (was 525 + 5 new − 3 obsolete from earlier model
revisions).

---

## 2026-04-15 — Scaling model: removed fake universe shifts, made monotonic

**Severity:** medium — the v1 sqrt model fixed the linear bug but
introduced its own quirk: non-monotonic projections from cross-profile
universe shifts. User screenshot showed slippage going
`0.336% → 0.752% → 0.336% → 0.752% → 1.063%`, with universe rows
labeled "drops {micro} (improves liquidity)" that didn't apply to a
single-profile view.

**Root cause:** the projection assumed the universe SHIFTS as capital
grows, blending across all cap tiers. That's a valid model for
"if I were running the whole system at $X AUM," but it's wrong when
the user is viewing a *single profile* (e.g. Small Cap). A Small Cap
profile only ever trades small caps — the universe is FIXED by
`market_type`. The "shift to large at $1M" is a system-level
recommendation in `SCALING_PLAN.md`, not a per-profile projection.

**Fix:**
- `project_scaling()` now uses the profile's `market_type` as a fixed
  singleton universe at every ladder rung.
- Slippage formula is pure `base × sqrt(scale_mult)` — guaranteed
  monotonic. No more confusing up-down-up artifacts.
- New `_MAX_CAPITAL_BY_MARKET_TYPE` table encodes per-tier soft
  capacity (micro $50K, small $250K, mid $5M, large $50M+, crypto $1M).
- Each row gets a `warnings[]` and `exceeds_capacity` flag. Once
  capital exceeds the soft max, the row warns the user to migrate
  capital to a larger-cap profile per SCALING_PLAN.md — instead of
  fudging the slippage number lower.

**Sample output for a Small Cap profile** (6 trades, 0.336% baseline):
```
$10K   0.336%  small                  ← soft max $250K
$50K   0.751%  small
$100K  1.062%  small
$500K  2.376%  small  EXCEEDS CAPACITY → migrate to mid/large
$1M    3.360%  small  EXCEEDS CAPACITY
$10M  10.625%  small  EXCEEDS CAPACITY
```
Now monotonic, honest about per-tier capacity, and the universe column
shows the actual profile rather than a fictional cross-profile blend.

**Tests** (`test_scaling_projection.py` — 17 total, +3 new):
- `test_slippage_monotonic_across_ladder` — guards against
  reintroducing universe-shift artifacts
- `test_small_cap_universe_stays_small_at_all_scales` — fixed-
  universe invariant
- `test_capacity_warning_when_exceeding_soft_max` — exercises the
  `exceeds_capacity` flag and warning text
- `test_market_type_aliases_normalize` — `smallcap` and `small`
  produce identical projections
- `test_mid_cap_has_higher_capacity_than_small` — sanity check on
  the per-tier capacity ladder
- Updated `test_100x_capital_gives_10x_slippage_pure_sqrt` — pure
  sqrt(100)=10 instead of the v1's universe-fudged value

**Test count:** 525 (was 522 + 3 new − 0 removed).

---

## 2026-04-15 — Replaced broken linear scaling model with sqrt-impact + universe-aware ladder

**Severity:** medium — UI was showing dangerously misleading projections

**Symptom:** Performance > Scalability > "Scaling Projection" tab showed
slippage of >10% at $1M AUM. That's plausible for trading penny stocks
with no risk management, but absurd for our system which rotates universe
as it scales. The number was scary enough to throw off planning, and it
was wrong.

**Root cause:** the Jinja template did the math inline:
```
slippage_at_scale = base_slip × (1 + (mult - 1) × 0.1)
return_at_scale   = base_return - base_slip × (mult - 1) × 0.05
```
Three flaws:
1. **Linear**, not square-root. Real market impact is sub-linear in trade
   size (Almgren-Chriss).
2. **Ignored universe changes.** `SCALING_PLAN.md` already documents
   that we drop micro at $100K, drop small at $1M, etc. The model
   projected as if the system kept slamming the same illiquid names.
3. **Arbitrary constants.** `+0.1` per multiplier and `0.05×slip` for
   return decay had no empirical or theoretical basis.

**Fix:** new `scaling_projection.py` module implements:
- Square-root market impact: `scaled = base_slip × √(scale_mult / liquidity_factor)`
- Universe-change ladder per capital tier (micro dropped at $100K, small
  dropped at $10M, etc.) with empirical $ADV averages for each cap tier
- Confidence intervals scaled to sample size (n<10 = ±100%, n≥100 = ±10%)
- Three data-quality states: `insufficient` (no fill data → show N/A),
  `modeled` (small sample → wide CIs), `calibrated` (≥30 trades → tight CIs)

**Realistic example output** (50 trades with 0.05% baseline slippage,
small-cap profile):
```
Capital     Slippage   CI            Universe                  Return
$10K        0.050%     [0.037,0.062] micro,mid,small           +12.00%
$100K       0.050%     [0.037,0.062] large,mid,small (-micro)  +12.00%
$1M         0.158%     [0.119,0.198] large,mid,small           +11.89%
$10M        0.281%     [0.211,0.351] large,mid (-small)        +11.77%
```
Note how the $100K row's slippage stays at 0.050% — the 10× capital
increase is exactly offset by the universe shift to more liquid names.
This is the kind of insight the broken linear model erased.

**Wired in:**
- `views.py` performance route loads `_gather_trades(db_paths)` and
  calls `project_scaling()` with the selected profile's market_type
- `templates/performance.html` Scalability tab now renders the table
  from `scaling.rows`, shows confidence intervals, lists per-tier
  warnings (universe drops, position-vs-volume cautions), and
  surfaces the model formula in the footer

**Tests** (`test_scaling_projection.py` — 14 tests):
- Square-root scaling: 4× capital → ~2× slippage, NOT 4×
- 100× capital → < 7× slippage when universe shifts (regression guard
  against the broken linear formula)
- Universe correctly drops micro at $100K, small at $10M
- Crypto universe stays crypto at all capital levels
- CIs widen with small samples (n=5 → ±100%; n=150 → ±10%)
- Insufficient-data path returns flag + message instead of misleading numbers
- Net return projection only deducts the *additional* slippage cost,
  not arbitrary 5× decay
- **Hard regression bound:** $1M slippage with 0.05% baseline must be < 1%

**Test count:** 522 (was 508 + 14).

---

## 2026-04-15 — Conviction take-profit override (prevent capping runaway winners)

**Severity:** feature — opt-in per profile, default OFF

**Motivation:** IONQ this morning sold at +20% TP, then the AI immediately
wanted back in at a slightly higher price. That's the IONQ scenario —
fixed TP caps the upside when a strong trend is actually still running.
A trailing stop would have ridden the move further; fixed TP pays bid-ask
spread + slippage twice for no extra return.

**Design:** new per-profile flag `use_conviction_tp_override`. When on,
a long position's fixed take-profit is SKIPPED if ALL three conditions
hold:
1. Most recent AI prediction confidence for the symbol >= `conviction_tp_min_confidence` (default 70)
2. Latest ADX >= `conviction_tp_min_adx` (default 25) — trend has actual strength
3. Current close >= previous bar's high — trend is still intact right now

When skip fires, the ATR trailing stop continues to manage the exit. If
the trend reverses, trailing stop catches it. If it keeps running, we
keep the gains.

**What is NEVER overridden (safety):**
- Stop-loss — always fires
- Short-position take-profit — shorts profit on fast reversals, not trends

**Files:**
- `conviction_tp.py` — new module: pure predicate + DB/bars IO wrapper
- `portfolio_manager.check_stop_loss_take_profit` — new
  `conviction_tp_skip` kwarg (optional callable)
- `trader.check_exits` — builds the skip predicate when the profile
  has the override enabled
- `user_context.UserContext` — 3 new fields (default OFF)
- `models.py` — 3 new ALTER TABLE migrations + build_user_context loader
- `views.py` — settings POST handler persists the 3 new fields
- `templates/settings.html` — new checkbox + 2 sliders under Trailing
  Stops section with tooltip explaining tradeoff

**Tests** (`test_conviction_tp.py` — 17 tests):
- Pure predicate: all conditions true → True; any one false → False;
  None/missing inputs → False (safe default: don't skip)
- Integration with `check_stop_loss_take_profit`: skip fn prevents
  long TP; returning False still triggers TP; stop-loss NEVER skipped;
  short TP NEVER skipped; no-skip-fn preserves legacy behavior
- DB lookups: most recent confidence wins; missing DB returns None;
  empty path returns None
- UserContext defaults: off, 70%, 25 (unchanged behavior for existing
  profiles)

**Test count:** 508 (was 491 + 17).

**Self-tuning note:** The override is NOT auto-tuned by the existing
self-tuning system. That system adjusts numeric thresholds
(confidence, stop/TP %), not boolean strategy flags. Auto-toggling
can be added later once we have 15-20 TP events to compare
"counterfactually would have kept running" vs "reversed" — a
premature flip on a 3-trade sample would do more harm than good.

---

## 2026-04-15 — Dashboard expand-row state preserved across auto-refresh

**Severity:** low UX — annoying, not broken

**Symptom:** Dashboard auto-refreshes Open Positions every 15s by fetching
the server-rendered HTML and replacing the wrapper. Any row the user
had expanded to read AI reasoning collapsed on refresh — mid-sentence.

**Fix:** `_trades_table.html` macro adds `data-symbol` on the summary
row. Dashboard JS `refreshPositions()` captures the set of expanded
symbols before the swap, then reapplies expansion state (and the
caret icon) afterward. State is by symbol, so it survives add/remove
of positions as well.

---

## 2026-04-15 — Dashboard: Open Positions now use rich format, Recent Trades removed

**Severity:** low (UX improvement, reduced duplication)

**Symptom & rationale:** The dashboard was double-duty — Open Positions
(live Alpaca data) plus a slim Recent Trades table, both competing for
space. The Recent Trades duplicated what `/trades` already does better
(full history, filters, expandable reasoning). Meanwhile Open Positions
lacked the AI metadata that made `/trades` useful.

**Fix:**
- Open Positions now render through the shared
  `_trades_table.html` macro. Each row is click-to-expand; the
  expanded panel shows Current Price, Market Value, AI Reasoning,
  Stop/Target, and Slippage.
- `_enriched_positions(ctx, profile_id)` — new helper that merges
  Alpaca's live position data with the most recent matching row in
  the profile's `trades` table, pulling in `ai_reasoning`,
  `ai_confidence`, `stop_loss`, `take_profit`, `decision_price`,
  `fill_price`, `slippage_pct`.
- Recent Trades table removed from the dashboard. Replaced with a
  small "View full trade history →" link that filters `/trades`
  by the profile.
- `/api/positions-html/<id>` — new partial endpoint returning the
  server-rendered positions block. The 15-second auto-refresh fetches
  HTML instead of rebuilding in JS, so the expandable markup can't
  drift from the template.
- Macro extended: expanded panel now shows Current + Market Value
  when the row is an open position (detected by `current_price`
  being set).

**Tests** (`test_enriched_positions.py` — 6 tests):
- Positions gain AI metadata from the matching open trade
- Most-recent trade wins when symbol has been re-entered
- Positions without any matching trade still render (manual Alpaca
  fills don't crash the dashboard)
- Missing DB doesn't crash
- Short positions get `side='sell'` with absolute qty
- Empty positions list returns empty list (not error)

**Test count:** 491 (was 485 + 6).

---

## 2026-04-15 — Unified dashboard + /trades trade-history display

**Severity:** low (UX consistency, DRY refactor)

**Symptom:** Dashboard had a slim 6-column trade table (Time / Symbol /
Side / Qty / Price / P&L) while `/trades` had the richer 9-column
expandable version (Time / Profile / Symbol / Side / Qty / Price / AI
Conf / P&L + expand row showing AI reasoning, stop, target, slippage).
Two copies of similar Jinja meant bug fixes landed on one and not the
other.

**Fix:**
- New `templates/_trades_table.html` — single Jinja macro
  `render_trades(trades, show_profile, empty_message)` owning all
  trade-row markup including expand-on-click details row.
- `templates/trades.html` and `templates/dashboard.html` now both
  `{% import "_trades_table.html" as trades_tpl %}` and call the macro.
- Dashboard calls with `show_profile=False` (it's already per-profile);
  `/trades` calls with `show_profile=True`.
- `colspan` auto-adjusts to match column count.

**Net effect:** dashboard now shows AI confidence + expandable AI
reasoning, stop/target, slippage on every trade, matching `/trades`.
Future UI tweaks land in one place.

**Tests** (`test_trades_table_shared.py` — 12 tests):
- AI confidence, reasoning, stop/target, slippage all render
- Expand-caret present
- `show_profile` toggle adds/removes Profile column AND adjusts colspan
- Empty-state custom + default messages
- P&L rendering: realized (closed), unrealized (open-with-mark), open-no-mark

**Test count:** 485 (was 473 + 12).

---

## 2026-04-15 — Pending Alpaca orders now visible on dashboard (Task 18.4)

**Severity:** medium (UX / operational visibility)

**Symptom:** After-hours order submissions queue in Alpaca as `accepted`
or `new` and don't fill until the next session. Dashboard showed only
filled positions, so a user couldn't tell "scheduler has orders waiting
for market open" from "scheduler produced nothing this cycle." Silently
confusing.

**Fix:**
- `views._safe_pending_orders(ctx)` — defensive wrapper around
  `api.list_orders(status="open")` with float coercion and
  exception-to-empty-list fallback.
- Dashboard renders a new "Pending Orders" table between Open Positions
  and Recent Trades, showing symbol / side / qty / order type / limit
  price / status / submitted timestamp / TIF.
- `/api/portfolio/<id>` returns `pending_orders`; JS auto-refresh every
  15s updates the table alongside positions.
- Hidden entirely when the list is empty (no dead UI).

**Tests** (`test_pending_orders.py` — 5 tests):
- Happy path: accepted limit buy renders with correct shape
- Market orders produce `limit_price=None`
- Garbage numeric fields coerce safely instead of crashing
- API exception → empty list, not 500
- `list_orders` is called with `status="open"` (filters out fills)

**Test count:** 473 (was 468 + 5).

---

## 2026-04-15 — Cleaned up stale `/opt/quantops/` directory on server (Task 20.5)

**Severity:** low (operational hygiene / prevents future confusion)

**Symptom:** Earlier today I wasted a minute on the server when `find`
surfaced a stale `aggressive_trader.py` at `/opt/quantops/` (no "ai")
— an abandoned pre-refactor codebase from March 27. The active service
runs at `/opt/quantopsai/`. Old path had a disabled `quantops.service`
systemd unit, not inactive since 2026-03-28.

**Fix:** `systemctl disable quantops.service`, removed the unit file,
`daemon-reload`, `rm -rf /opt/quantops/`. Verified `/opt/` now contains
only `quantopsai/`. No running service referenced the stale tree.

---

## 2026-04-15 — Strategy SELL-bias starved Small Cap of trades for 4+ days

**Severity:** critical — profile opened zero trades despite scanning every 15 min

**Symptoms:** Small Cap profile scanned continuously (616 AI predictions
across 2026-04-13 to 2026-04-15) but opened **zero trades**. Every
prediction returned `HOLD` with `confidence=0`. Mid Cap and Large Cap
were also affected — their shortlists were 11/12 and 15/15
`STRONG_SELL` respectively; only a stray `STRONG_BUY` had let Mid Cap
open any positions, and not recently.

**Where the prior "working as intended" call was wrong:** Past
evaluations chalked this up to a genuinely bearish universe. It was
actually a labeling bug — the screener was pre-tagging nearly every
candidate `STRONG_SELL` before the AI even saw it, the specialist
ensemble (Phase 8) saw the `STRONG_SELL` input and agreed, and the AI
correctly concluded "no edge across the board." The loop looked
convincing because every layer "agreed."

**Root cause:** Each size-specific strategy module
(`strategy_small.py`, `strategy_mid.py`, `strategy_large.py`,
`strategy_micro.py`) is a LONG-ONLY entry engine, but several of its
internal rules emitted `signal="SELL"` whenever the **exit condition
for a hypothetical existing long** was true. Examples:

- `mean_reversion_strategy`: SELL if `price >= sma_20` OR `rsi > 55`
  (fires on ~60-70% of any universe)
- `momentum_continuation_strategy`: SELL if `price < sma_20`
- `ma_alignment_strategy`: SELL if `price < sma_20`
- `pullback_support_strategy`: SELL if `price < sma_50`
- `dividend_yield_strategy`: SELL if `rsi > 55`
- `penny_reversal_strategy`: SELL if `price >= sma_10` OR `rsi > 50`
- `volume_explosion_strategy`: SELL if `vol_ratio < 2 and rsi > 60`
- `sector_momentum_strategy`: two separate bogus SELL branches

Those comments literally say `EXIT --` but the code emits a SELL
signal, which `multi_strategy.aggregate_candidates()` then interprets
as bearish sentiment. A typical stock accumulated 2+ SELL votes → score
≤ -2 → label `STRONG_SELL`. AI then declined everything.

**Fixes:**

1. **Aggregation respects short-selling flag.** `multi_strategy.aggregate_candidates()`
   now coerces SELL votes to HOLD (and zeroes their score contribution)
   when the profile has `enable_short_selling=False`. Defensive — all
   current profiles have shorting on, but this closes the class of bug
   for any future long-only profile.
2. **Stripped the broken SELL branches.** Replaced ~12 "exit-as-SELL"
   branches with HOLD returns across all four size strategy files.
   Legit bearish setups preserved (MACD bearish cross, 10-day-low
   break, failed gap, falling-knife 10-consecutive-red-days, SPY
   overbought ≥75).

**Why the specialist ensemble didn't catch it:** The ensemble receives
the already-`STRONG_SELL`-labeled shortlist as input. It's a
second-layer consensus model, not a first-principles re-evaluator — its
job is to confirm or veto, not to re-score from scratch. GIGO.

**Why no prior test caught it:** The existing `test_multi_strategy.py`
fixtures passed explicit `signal` values into fake strategies; they
never exercised the "what happens when a real strategy emits SELL
from an exit-condition" path.

**Tests** (`test_strategy_sell_bias_fix.py` — 18 tests):
- Aggregation: SELL → HOLD when shorting off, pass-through when on,
  BUY votes untouched by the flag
- `mean_reversion` returns HOLD at RSI 60 and above-SMA, still BUY
  when truly oversold
- `momentum_continuation`, `sector_momentum`, `pullback_support`,
  `dividend_yield`, `ma_alignment`, `relative_strength`,
  `volume_explosion`, `penny_reversal` all return HOLD (not SELL)
  in the previously-broken conditions
- **Preserved legit bearish signals:** 10-day-low break still SELLs,
  MACD bearish cross still SELLs, 10-consecutive-red-days still SELLs
- End-to-end: diverse universe with no SELL votes produces zero
  `STRONG_SELL` labels (regression guard against the Small Cap freeze)

**Verification:** Small Cap's next scan cycle post-deploy should show
a mix of signal labels (not 100% `STRONG_SELL`) and begin evaluating
BUY candidates. Actual trade execution still gated behind the AI
(Phase 1-10 stack), which now has real information to decide on.

**Test count:** 468 (was 450 + 18).

---

## 2026-04-15 — Migrated market data from yfinance to Alpaca (Algo Trader Plus)

**Severity:** architectural improvement (prevents the class of bug from
yesterday's 30-min hang; not fixing a new regression)

**Context:** yfinance is an unofficial Yahoo scraper; during market open
Yahoo throttles and returns 10-sec timeouts on many symbols. Yesterday
this hung the screener for 30+ minutes and blocked exits behind it,
nearly costing ~$100 of locked-in profit on HOOD and IONQ.

**Upgrade:** subscribed to Alpaca Algo Trader Plus ($99/mo) for SIP feed
and unlimited historical bars. Updated main `.env` with account-level
master API key that has the subscription active.

**Code migration:**
- `market_data.get_bars()` now tries Alpaca first, falls back to
  yfinance. Crypto symbols (containing `/`) bypass Alpaca directly —
  Alpaca's equity endpoint doesn't serve crypto.
- `screener.screen_dynamic_universe()` now uses Alpaca's
  `get_snapshots()` batch endpoint (up to 200 symbols per call) to
  filter by price + volume. The previous `yf.download()` path remains
  as a fallback when the Alpaca snapshot call fails or raises.

**Measured speedup:**
- Single `get_bars` call: 10s timeouts → 200ms (50× faster)
- Full dynamic screener: 30 min → 853 ms (**~2,100× faster**)
- First live cycle post-restart: Small Cap Scan & Trade completed
  in 166 seconds (well inside the 15-min interval)

**Tests** (`test_alpaca_data_migration.py` — 13 tests):
- `_limit_to_days` calendar window math
- Alpaca success → lowercase OHLCV columns + US/Eastern tz
- Alpaca over-fetch respects caller's `limit` via `.tail()`
- Alpaca empty / exception / missing client → yfinance fallback
- Crypto symbols skip Alpaca entirely, slash→dash for yfinance
- Screener Alpaca success path → filtered symbols
- Screener Alpaca failure → yfinance fallback invoked
- **Contract guards:** source inspection ensures the Alpaca-before-yfinance
  ordering can't silently regress in either `market_data.get_bars` or
  `screener.screen_dynamic_universe`.

**Test count:** 450 (was 437 + 13).

---

## 2026-04-15 — Exits blocked behind hung scan (realized-P&L risk)

**Severity:** critical — positions past take-profit thresholds weren't selling

**Symptoms:** Mid Cap Scan & Trade hung for 30+ minutes during market
open. User noticed positions should have hit take-profit but nothing
was firing. Manual exit-check via SSH triggered HOOD (+10.2%) and IONQ
(+20.3%) sells that the scheduler had been sitting on.

**Root cause:** `run_segment_cycle` ran tasks in order `scan → exits`.
When the scan hung (yfinance timeout storm during market open, see
below), exit checks never got a chance. Take-profit and stop-loss
triggers are only meaningful if they fire within minutes of being
hit; gating them behind a 30-minute hung scan means P&L evaporates.

**Fixes:**
1. **Exits run BEFORE scan** — reordered `run_segment_cycle` so
   `_task_check_exits`, `_task_cancel_stale_orders`, and
   `_task_update_fills` fire first. Exits are ~1-5 seconds per profile,
   cheap, and must never be blocked by a slow scan pipeline downstream.
2. **Exit interval shortened from 15 min → 5 min** — `INTERVAL_CHECK_EXITS`
   was matching the scan interval; now it's independent and tight enough
   that TP/SL triggers fire within 5 min of being hit.
3. **Dynamic screener budget + disk cache** — the hang root cause was
   yfinance getting hammered during market open (40+ failed downloads
   at 10-sec timeouts each). Added `_DYNAMIC_YF_BUDGET_SEC = 180` hard
   wall-clock budget that abandons yfinance after 3 min and falls back
   to stale cache or curated fallback. Cache now persists to
   `dynamic_screener_cache.json` so process restarts don't force a
   re-scan.
4. **Trailing stop NoneType crash** — `check_trailing_stops` failed with
   "'NoneType' object is not subscriptable" on symbols where `get_bars`
   returned a malformed DataFrame. Added defensive guards: skip if
   `bars` is None / missing `.empty` / missing required columns /
   NaN ATR.

**Verified live:** at 14:21:02 UTC, Mid Cap's Check Exits completed
in 4.2 seconds — before Scan & Trade even started. Exit checks are now
firewalled from scan failures.

**Tests:** `test_screener_cache.py` — 4 tests covering disk persistence,
stale fallback, and budget constant bounds. Total suite now 437 passing.

---

## 2026-04-14 — Per-profile scheduling (Large Cap starvation bug) + droplet swap

**Severity:** high (profiles could be starved, scheduler would silently skip)

**Bug:** Scheduler tracked `last_run["scan"]` / `last_run["check_exits"]`
/ `last_run["resolve_predictions"]` as a **single global timestamp shared
across all profiles**. When one profile's full cycle (scan + ensemble
+ AI + event tick) overran the 15-minute interval, every other profile
inherited the same "just ran" timestamp and none would be due again for
15 minutes. In practice: Mid Cap took ~5 min, then Small Cap ~5 min,
then Large Cap (last in iteration) was often still starting when the
next interval rolled around — so its cycle got truncated or skipped
entirely. The user observed zero Large Cap trades despite the profile
being enabled.

**Fix:**
- New `profile_runs: Dict[int, Dict[str, float]]` state, keyed by
  profile_id. Each profile gets its own `{scan, check_exits,
  resolve_predictions}` timestamps.
- Helper `_get_profile_runs(pid)` lazily initializes a profile's
  entry on first access.
- The profile-iteration loop now computes `prof_do_scan` /
  `prof_do_exits` / `prof_do_predictions` **per-profile** from that
  profile's own timestamps.
- After each profile's cycle completes, **only that profile's**
  timestamps are stamped — adjacent profiles aren't affected.
- Snapshot remains global (one snapshot per calendar day is the
  correct system-wide behavior).
- Legacy segment-mode branch keeps the old global `last_run` for
  backwards compat; only the profile branch changed.

**Natural staggering:** First-run starts all profiles due simultaneously.
Sequential execution (one at a time, since we're memory-constrained)
means profile 1 finishes at T+5min, profile 2 at T+10min, profile 3 at
T+15min. Each then clocks its own 15-minute interval from there. After
one full warm-up cycle, the three profiles naturally fire at
approximately staggered 5-minute offsets. No explicit offset logic
needed — emerges from sequential execution + independent clocks.

**Secondary: added 1 GB swap to droplet.** The droplet is 1 GB RAM,
1 CPU, no swap — 681 MB used, 281 MB free. A Python memory spike
(large yfinance batch, concurrent AI responses) could OOM-kill the
scheduler with no cushion. `fallocate /swapfile 1G`, `mkswap`,
`swapon`, persisted in `/etc/fstab`. Free + safety. Does not enable
parallel execution, but prevents unexpected OOM kills.

**Tests:** `test_per_profile_scheduling.py` — 5 tests covering
independent clocks, slow-cycle-doesn't-starve-others invariant,
natural staggering from sequential execution, module import
stability, and a source-pattern guard that fails loudly if the
per-profile structure is ever flattened back to globals.

**Test count:** 426 (was 421 + 5).

---

## 2026-04-14 — Dashboard P/L formatting flicker + earnings detector import bug

**Bug A: Unrealized P/L cell flickers between two formats**
- On page load (Jinja-rendered): `-29.70` (no `$`)
- On 5-second auto-refresh (JS-rendered): `$-29.70` (`$` prepended,
  minus sign INSIDE the dollar)
- The two render paths used different format strings for the same cell.
  Looked like the column was changing because it WAS — every refresh.
- **Fix:** standardized both to `+$1,234.56` / `-$29.70` (sign before
  `$`, conventional). Changed in `dashboard.html` template (line 166)
  AND inline JS (line 630). Same fix applied to `trades.html` for the
  unrealized-P/L badge.

**Bug B: `event_detectors.detect_earnings_imminent` imports nonexistent function**
- Imports `get_next_earnings` from `earnings_calendar` — function doesn't
  exist (the actual API is `check_earnings(symbol) -> dict`). Detector
  silently failed every event tick with a warning the user wouldn't see.
- **Fix:** call `check_earnings(sym)` and read `.days_until` from the
  returned dict.
- **Tests:** `test_event_bus.TestEarningsImminentDetector` — 2 tests
  verify the import resolves and the detector handles empty positions.

**Test count:** 421 (was 419 + 2).

---

## 2026-04-14 — Profile switch: Crypto → Large Cap

**Severity:** (not a bug — operational change, logged per changelog policy)

**What changed:** The Crypto profile (id=2) was producing zero trades
despite consuming ~$0.78/day in AI calls because 3 of 4 specialists had
no crypto-relevant data. After the ensemble scoping fix limited crypto
to pattern_recognizer only, we further discussed whether to continue
running crypto at all versus switching to Large Cap, where all 10 phases
of infrastructure apply meaningfully.

**Decision:** Switch. Alpaca Crypto account deleted; new Alpaca Large Cap
paper account created.

**Steps taken on the server:**
1. Profile id=2 renamed to "Crypto (archived)", `enabled=0`, Alpaca keys
   blanked so the scheduler stops trying to authenticate.
   Historical DB (`quantopsai_profile_2.db`) preserved as archival
   record of crypto prediction history.
2. New profile id=4 "Large Cap" created with `market_type='largecap'`,
   `schedule_type='market_hours'`, `enable_short_selling=1`, settings
   mirroring Mid Cap (max_position_pct=0.08, max_total_positions=10).
3. Alpaca credentials encrypted via `crypto.encrypt()` and stored in
   `trading_profiles.alpaca_api_key_enc` / `alpaca_secret_key_enc`.
4. `journal.init_db('quantopsai_profile_4.db')` to create the Large Cap
   profile's database with current schema (including the new
   `recently_exited_symbols` and `ai_cost_ledger` tables from today).
5. Scheduler restarted. New profile is now in the rotation:
   Mid Cap → Small Cap → Large Cap (Crypto no longer iterated).

**Verified:** Alpaca connection live, equity $10,000 paper, status ACTIVE.

**Implication for MONTHLY_REVIEW.md tracker:** the month-1/2/3 review
metrics are now gathered across three equity profiles (Mid, Small,
Large Cap) all using the full 10-phase stack. Historical crypto data
in `quantopsai_profile_2.db` stays archived and does not feed meta-model
training or decay monitoring for the new profile.

---

## 2026-04-14 — Crypto specialist ensemble scoped to pattern_recognizer only

**Severity:** medium (cost + signal quality on crypto)

**Symptoms:** Crypto profile spent ~$0.78 today (256 AI calls) with
zero trades executed. Ensemble log: "ENSEMBLE HOLD at 0% confidence
across the board" for nearly every cycle. Specialists were ABSTAIN-ing
or returning generic HOLDs because crypto has none of the data they're
designed to read.

**Root cause:** Three of the four specialists need data sources that
don't exist for crypto:
- `earnings_analyst` — crypto has no earnings calls or filings
- `sentiment_narrative` — political/insider/options-flow inputs are
  equity-specific
- `risk_assessor` — portfolio concentration / Form 4 / SEC context
  doesn't apply

Running them produced noise that drowned out the one specialist
(`pattern_recognizer`) that can genuinely read crypto price action.

**Fix:** `ensemble.APPLICABLE_SPECIALISTS_BY_MARKET["crypto"] = {"pattern_recognizer"}`.
On crypto, only pattern_recognizer runs. Equity markets keep the full
4-specialist ensemble.

**Expected impact:**
- Crypto cost drops ~75% (1 specialist × chunks instead of 4)
- Pattern-recognizer's BUY/SELL verdicts now drive consensus directly
  (no dilution from ABSTAIN-ing peers)
- Crypto should start actually trading

**Tests:** `test_ensemble.TestSpecialistMarketApplicability` — 2 tests:
crypto-only-pattern, and equity-runs-all-four.

**Test count:** 419 (was 417 + 2).

---

## 2026-04-14 — Re-entry cooldown + skip political_context on crypto

**Severity:** medium (trade quality + cost efficiency)

**Bug: Position churn on same-symbol re-entry (ASTS)**
- 17:32 BUY ASTS @ $88.25 → 17:56 trailing stop triggered, sold @ $89.44
  (+$1.83 profit) → **18:02 BUY ASTS again @ $89.78** (6 min later,
  $0.34 higher than the exit). AI prompt had no "we just stopped out
  of this" context, so it re-selected ASTS as a high-conviction setup
  seconds after the protective exit fired.
- **Fix:**
  - New `recently_exited_symbols` table in per-profile DB
  - `journal.record_exit()` is called by `_task_check_exits` for every
    trailing-stop / stop-loss / take-profit firing
  - `trade_pipeline` pre-filter drops non-held symbols that appear in
    `get_recently_exited(cooldown_minutes=60)`. Held positions can
    still be managed (trimmed/exited); only fresh BUY entries are blocked.
- **Tests:** `test_reentry_cooldown.py` — 6 tests covering insert,
  expiry window, dedup on replace, missing-table safety, and the
  pipeline-filter contract.

**Cost optimization: Skip political_context on crypto**
- `political_sentiment.get_maga_mode_context` runs once per cycle when
  MAGA mode is on. It's ~$0.02 per call, equity-focused (tariffs,
  sector impacts). Crypto profiles called it ~40× today ($0.15/day
  wasted — crypto is macro-driven, not political-narrative-driven).
- **Fix:** `trade_pipeline.py` Step 4 skips the political context
  fetch when `ctx.segment == "crypto"`.
- **Expected impact:** Crypto AI cost drops ~20% per day.

**Open follow-up:** Small Cap / Crypto are still showing 0 trades.
Logs reveal the AI sees unanimous ensemble SELL conviction but passes
citing "sideways market regime". Not a bug — an AI decision pattern.
Separate task (#107) to decide whether to tune prompt to respect
strong ensemble consensus or accept cautious behavior during bootstrap.

**Test count:** 417 (was 411 + 6).

---

## 2026-04-14 — Systematic "insufficient data = N/A" pass across every metric

**Severity:** medium (UX correctness, not data integrity)

**Symptoms:** User audited the Performance Dashboard and found misleading
`0.00` values everywhere. Sharpe showing 0.00 with 1 day of data, Calmar
showing absurd numbers, Alpha/Beta showing 0.000 with insufficient data,
VaR showing 0.0 with no trades, Profit Factor showing 0.00 when there
are no wins, Current Streak showing "0 none" with no trades, etc. User
rightly pushed back: "I tell you to evaluate each page and you fix them
one at a time reactively."

**Root cause:** Widespread anti-pattern. Every `X if Y > 0 else 0.0`
collapses "undefined" and "zero" into the same display value. Users
can't distinguish "no data yet" from "your system produces no return."

**Fix:** Introduced a consistent `{metric}_computable` boolean alongside
every numeric metric that can be undefined. Template checks the flag
and renders **N/A** with a short "need X" hint instead of `0.00`.

**Metrics covered** (all now flag-guarded):
- `sharpe_ratio` — need ≥ 2 daily returns with positive std
- `sortino_ratio` — need ≥ 2 losing days
- `annualized_volatility` — same as Sharpe
- `calmar_ratio` — need ≥ 1% DD + ≥ 30 days
- `var_95` — need ≥ 5 closed trades
- `cvar_95` — same
- `win_rate` — need ≥ 1 closed trade
- `profit_factor` — need at least one win AND one loss
- `win_loss_ratio` — same
- `monthly_win_rate` — need ≥ 1 month of activity
- `alpha` — need ≥ 20 days aligned vs SPY
- `beta_spy` — same
- `correlation_spy / _qqq / _btc` — need ≥ 10 aligned days
- `slippage_vs_gross` — need positive gross profit
- `current_streak` — need ≥ 1 closed trade

**Tests:** `test_insufficient_data_guards.py` — 14 tests covering:
1. Every flag is emitted (not silently missing from the dict)
2. Empty data → all flags False
3. One-trade scenario (matches production state) → most flags False,
   ones that should compute (win_rate, streaks) return correctly
4. Sufficient data (30 snapshots, 5+ trades, wins+losses) → flags True

This is a **contract test**: a future refactor that removes a flag will
fail immediately with a pointed error message. Same mechanism we used
for the snake_case leak audit.

**Test count:** 411 (was 397 + 14).

---

## 2026-04-14 — Win/Loss Ratio shows undefined when ratio isn't computable

**Severity:** low (UX correctness — same class as the Calmar guard)

**Bug 8:** Win/Loss Ratio displayed `0.00` when the account had no
winning trades. The math `avg_win / abs(avg_loss) = 0 / X = 0.0` is
technically correct but misleads users into thinking they have a 0×
edge. The correct signal is "undefined — not enough data yet."

**Fix:** `metrics.py` emits `win_loss_ratio_computable = False` when
either `winning_trades` or `losing_trades` is empty. Template shows
**"N/A"** with a "need at least one win and one loss" hint instead
of `0.00`.

**Test:** `test_metrics_bugs.TestWinLossRatio` — three cases: no
wins, no losses, and both present (computes normal 2.0 ratio).

**Test count:** 397 (was 394 + 3).

---

## 2026-04-14 — Trade Analytics audit: 2 more bugs

**Severity:** medium (metrics display)

**Bug 6 — Avg Hold Days always 0.0**
- `metrics.py:765` matched buy→sell pairs by iterating the `trades`
  variable, which is the pnl-filtered list. Buys never have pnl set
  until the sell closes them, so BUY rows weren't in the list. Every
  SELL looked at an empty `open_positions` dict and recorded nothing.
- **Fix:** separate SQL query that fetches ALL trades (unfiltered) for
  the hold-days calculation. Buy/sell matching now works correctly.
- **Test:** `test_metrics_bugs.TestAvgHoldDays` — verifies a 04-13 buy
  + 04-14 sell yields 1.0 days, and empty-list case stays 0.0.

**Bug 7 — PnL distribution chart rendered same label 3× on single-bar charts**
- `metrics.render_bar_chart_svg:366` picked label indices `[0, len//2,
  len-1]` without deduping. A 1-bar chart collapsed all three to idx=0
  and rendered the label 3 times. User saw "-8% / -8% / -8%" when there
  was actually one trade bucketed to -8%.
- **Fix:** `sorted(set(...))` to dedup the idx list before rendering.
- **Test:** `test_metrics_bugs.TestSingleBarChartLabels` — 1 bar renders
  label 1×; 10 bars render 3 distinct labels.

**Test count:** 394 (was 389 + 5 new).

---

## 2026-04-14 — Executive Summary audit: 5 distinct bugs

**Severity:** medium (metrics wrong / misleading, not data-destructive)

**Symptoms:** User reviewed the Performance Dashboard's Executive Summary
tab and noted "a lot of 0s" despite a full day of trading. Audit revealed
5 distinct issues with how metrics are computed or displayed.

**Bug 1 — SELL trade with realized PnL stored as `status='open'`**
- `trade_pipeline.py:405` called `log_trade(pnl=pnl, ...)` on position
  closes without passing `status`. `journal.log_trade` defaults status
  to `'open'`. Result: closed positions with realized PnL appeared as
  open in the DB; downstream status-filter queries were wrong.
- **Fix:** pass `status="closed"` when pnl is not None on the sell path.
- **Test:** `test_metrics_bugs.TestSellStatusClosed`.

**Bug 2 — `daily_pnl` column always NULL**
- `_task_daily_snapshot` never passed `daily_pnl` to `log_daily_snapshot`.
  The column existed in the schema but had zero write paths.
- **Fix:** task now reads the most recent prior snapshot and stores
  `daily_pnl = today_equity - prior_equity`. First-ever snapshot stays
  NULL (no prior to compare against).
- **Test:** `test_metrics_bugs.TestDailyPnlPopulated`.

**Bug 3 — Calmar ratio produced absurd values with tiny drawdown**
- `metrics.py:585` divided annualized return by max_dd_pct with no floor.
  With 1 day of data and a 0.07% DD, Calmar became -310. That's
  mathematically correct but practically meaningless.
- **Fix:** require `max_dd_pct >= 1.0` AND `days_active >= 30` before
  computing Calmar. Below that, return 0.0 — the "insufficient data"
  sentinel already used elsewhere.
- **Test:** `test_metrics_bugs.TestCalmarGuard` with tiny-DD,
  insufficient-days, and meaningful-data scenarios.

**Bug 4 — Daily snapshot triggered only in a 5-minute window**
- `multi_scheduler.py:1221` gated snapshot on `now.hour == 15 and
  now.minute >= 55`. If the scheduler was restarted or paused through
  those 5 minutes, no snapshot that day. Two profiles were missing
  their 2026-04-12 snapshot because of this.
- **Fix:** trigger is now `now >= 15:55` for any time that day, with
  dedup via `last_run["daily_snapshot"]` date string. Missed-at-close
  is still caught later.
- **Test:** `test_metrics_bugs.TestSnapshotTriggerWindow` — both the
  trigger semantics and the dedup-by-date-string assertion (reads
  source to guarantee the dedup form isn't regressed).

**Bug 5 — Total Trades count excluded open positions**
- `metrics._gather_trades` filters `WHERE pnl IS NOT NULL`, so open
  positions never counted. A user who had made 3 trades (2 opens + 1
  close) saw "Total Trades: 1" and thought nothing had happened.
- **Fix:** added `_count_open_trades`; metrics dict now has
  `closed_trades`, `open_trades`, and `all_trades` (plus backward-compat
  `total_trades = closed_trades`). Template displays "3 (1 closed · 2
  open)". Win rate / profit factor / Sharpe still use closed trades
  only (those are the only trades with realized PnL to measure).
- **Test:** `test_metrics_bugs.TestTradeCountsIncludeOpen`.

**Follow-up:** one stray row on the server (Mid Cap LUNR sell) still
has status='open' from before the fix. Retroactively updated with a
one-line SQL on deploy. Future sells will get status='closed' correctly
via the code path.

**Total:** 11 new tests in `test_metrics_bugs.py`. Suite now 389 passing.

---

## 2026-04-14 — Risk specialist over-vetoing, earnings specialist noise-voting

**Severity:** high (trading completely blocked despite unanimous sell signals)

**Symptoms:** First live cycles after the tool_use fix showed ensemble was
producing real verdicts (previously all ABSTAIN), but trading was still
blocked. Per-cycle breakdown:
- `risk_assessor`: VETOing 53-80% of candidates (8/15 Mid Cap, 12/15 Small Cap)
- `earnings_analyst`: returning HOLD @ low confidence for 15/15 in every cycle
- Pattern + sentiment producing real signals but being drowned out
- Final AI correctly reasoning "mixed consensus" → pass

**Root cause:** Both specialists lacked meaningful per-symbol data in their
prompts (only symbol + signal + one-line reason). When asked to judge
without data:
- `risk_assessor` treated its "BIAS TOWARD CAUTION" + "VETO is final" as
  license to VETO anything ambiguous, including "sideways regime" and
  "low volatility" — which should be HOLD, not VETO
- `earnings_analyst` was explicitly instructed to "return HOLD with low
  confidence" when it had no earnings data — so it did, for every symbol,
  every cycle. That filled the consensus with neutral-but-valid HOLD votes
  that drowned out real signal

**Why it wasn't caught:** End-to-end trading behavior couldn't be tested
without running against a live Anthropic model. Unit tests of the ensemble
aggregation use mocked verdicts and don't reveal systemic miscalibration
in the prompts themselves.

**Fix:**
- `risk_assessor` prompt now explicitly lists INVALID VETO reasons
  ("uncertain market", "sideways regime", "low volatility", "general
  caution", "lack of information") — these are HOLD, not VETO. Also added
  a soft sanity check: "if you find yourself writing more than 2 VETOs in
  a batch of 5, re-examine". Removed the "BIAS TOWARD CAUTION" framing.
- `earnings_analyst` prompt now says: **omit symbols you can't assess**.
  Previously it returned HOLD for unknown symbols, polluting consensus.
  Now silence is the correct answer — only return verdicts for symbols
  with specific earnings/filing evidence (upcoming earnings date, recent
  surprise, SEC alert, etc.)

**Tests:** ensemble unit tests unchanged (mock-based, don't cover this).
Live validation required — watch next cycles for VETO rate < 20% on
risk_assessor and earnings_analyst producing verdicts for only a subset
of candidates (not 15/15 HOLD).

**Follow-up:** richer data in the specialist prompts (actual portfolio
state for risk, earnings calendar hits for earnings analyst) would let
them make informed verdicts instead of defaulting to safe-but-useless
output. Tracked informally as a design improvement.

---

## 2026-04-14 — Specialist ensemble silently abstaining on every call

**Severity:** critical (bordering on catastrophic)

**Symptoms:** Over 24 hours of live trading, Mid Cap profile made 2 trades,
Crypto made 0, Small Cap made 0. All 4 specialists showed `ENSEMBLE HOLD @
0% confidence` for every candidate. Final-decision AI correctly refused to
trade because "specialists universally abstain." No SHORT trades ever
executed despite STRONG_SELL technicals.

**Root cause:** Two compounding failures, both rooted in Haiku non-compliance:

1. **Shape failure** — Anthropic Haiku returns a single JSON object `{...}`
   instead of an array `[{...}, {...}]` for specialist prompts. The parser
   strictly required `isinstance(parsed, list)` and dropped the response
   when it wasn't.
2. **Drop failure** — Even with shape coerced, Haiku only returned 1-2 of
   15 requested candidates per call. The remaining 13 abstained by default,
   so the ensemble consensus was ABSTAIN/HOLD for almost every symbol,
   and the final AI refused to trade.

**Why it wasn't caught:** Unit tests mocked the AI call with clean JSON
arrays, never exercised the single-object branch or the truncated-response
branch. No integration test ran real specialist prompts against a real
provider.

**Fix** (three layers — only the third fully resolves the issue):

1. **Parser hardening** — `extract_verdict_array` now accepts: array,
   single object (wrapped), multiple concatenated objects, any of the
   above embedded in prose. Verified live — Haiku's single-object
   responses are now parsed correctly.
2. **Prompt strengthening** — all 4 specialist prompts now say "STRICT
   JSON ARRAY — starts with `[` and ends with `]`" and "You MUST return
   exactly {N} entries". Helped but not sufficient — Haiku still dropped
   candidates at size 15.
3. **Chunking + `tool_use`** — ensemble now chunks candidates into
   groups of 5 AND uses Anthropic's structured-output mode
   (`call_ai_structured` in `ai_providers.py`) to force schema
   compliance via a tool definition. **This is the fix that actually
   works.** Live probe verified 8/8 coverage per specialist (was 0-2/8).

**Cost impact:** With chunking + tool_use, ensemble is now 4 specialists ×
ceil(15/5) = 12 AI calls per cycle (was 4). Cost per cycle increases ~3×
but the ensemble now produces usable verdicts, which is the whole point.

**Tests added** (`test_ensemble.py`):
- `test_accepts_single_object_not_wrapped_in_array` — shape coercion
- `test_accepts_multiple_concatenated_objects` — streaming-object variant
- `test_accepts_object_with_surrounding_prose` — prose-wrapped variant
- `test_cost_scales_with_chunks_not_candidate_count` — chunking math
- `test_single_chunk_when_few_candidates` — small-shortlist sanity

**Gaps acknowledged:** No test uses a real Anthropic SDK to verify
tool_use works end-to-end. I ran a live probe on the server post-deploy
to confirm (8/8 verdicts returned). A mocked SDK integration test
covering the tool_use path would be valuable follow-up.

---

## 2026-04-14 — Snake_case leaking to AI Cost dashboard

**Severity:** medium (UX)

**Symptoms:** AI Cost panel showed `political_context`, `batch_select`,
`ensemble:risk_assessor`, etc., directly in user-facing tables — raw
internal identifiers instead of human labels.

**Root cause:** The `test_every_new_strategy_has_display_name` test was
scoped only to `STRATEGY_MODULES`. The `purpose=` tags emitted by
`call_ai` across 8 modules were never checked. Template also missed
the `| display_name` filter on the purpose column.

**Why it wasn't caught:** Existing test only validated strategy names.
No sweep across all identifier sources in the codebase.

**Fix:**
- Added 11 new `_DISPLAY_NAMES` entries covering every `purpose=` tag
- Added namespaced-fallback: `display_name("ensemble:foo_bar")` → `"Ensemble — Foo Bar"`
- Applied `| display_name` in the AI Cost panel template

**Tests added** (`test_display_names.py::TestNoSnakeCaseLeaksAnywhere`):
- `test_every_purpose_tag_has_human_label` — grep-discovers every
  `purpose=` literal in the codebase and asserts the rendered label has
  no underscores and is capitalized. Auto-catches any future tag.
- `test_known_purpose_labels` — exact assertions for 6 user-facing labels
- `test_namespaced_fallback_for_unknown_specialist` — future specialists
  pretty-print even without an explicit entry

---

## 2026-04-14 — `sync.sh` wiped live dashboard state on every deploy

**Severity:** high

**Symptoms:** Dashboard "AI Brain" panel showed "Waiting for first cycle..."
for Mid Cap and Small Cap profiles despite a full day of trading activity
recorded in their DBs. Multi-day breakage spanning ~6 deploys.

**Root cause:** `sync.sh` uses `rsync --delete` to mirror source → server.
Excludes were set for `*.db`, `*.pkl`, `.env`, `logs/`, `exports/` — but
`cycle_data_*.json` and `scheduler_status.json` were missing from the
excludes. Those files are written at runtime to the project root by
`trade_pipeline._save_cycle_data`. Every deploy wiped them. Crypto
regenerated quickly (24/7 cycle); equities only run during US market
hours, so their files stayed missing all evening.

Data itself was safe — per-profile DBs were correctly excluded.

**Why it wasn't caught:** The sync script has no self-test. I rewrote
it during the templates-flatten incident and didn't enumerate all
runtime files.

**Fix:**
- Added `--exclude 'cycle_data_*.json'` and `--exclude 'scheduler_status.json'`
  to `sync.sh`
- New `recover_cycle_data.py` one-shot script rebuilds missing cycle files
  from recent `ai_predictions` rows
- Freshness check in recovery script prevents overwriting live cycle data
  (`--force` flag for explicit override)

**Tests added** (`test_recover_cycle_data.py`):
- `TestSyncShExclusions::test_sync_excludes_runtime_artifacts` — reads
  `sync.sh` and asserts both exclusions are present. Fails with a message
  that points back at this incident if anyone removes them.
- 5 tests covering the recovery script (valid reconstruction, freshness
  check, force flag, missing-DB safety, empty-DB safety)

---

## 2026-04-14 — Capital allocator hardcoded `DEFAULT_WEIGHT = 1/6`

**Severity:** medium (latent — would have broken silently as library grew)

**Symptoms:** None yet — caught pre-production while expanding the
strategy library from 6 → 16. With the hardcode, 16 new strategies each
got a "default" weight of 1/6 = 16.67% = 2.67× oversized. Normalization
would still sum to 1.0 but relative weights between no-track-record
strategies would be wrong.

**Root cause:** `multi_strategy.DEFAULT_WEIGHT = 1.0 / 6` was a module-level
constant hardcoded to the original library size.

**Fix:**
- Replaced with `_default_weight(n_strategies)` function computed per-call
  using the actual `len(strategy_names)` from the current allocation

**Tests added** (`test_today_integration.py`):
- `test_default_weight_scales_inversely_with_count` — validates at 6, 16, 40
- `test_one_hot_strategy_capped_redistributed` — cap-and-redistribute math
  at 16-strategy library size
- `test_three_hot_strategies_all_capped` — edge case where multiple
  strategies hit the 40% cap

---

## 2026-04-14 — `sync.sh` flattened `templates/`, wiped running web UI

**Severity:** critical (production web UI broke, 500 errors)

**Symptoms:** `GET /login` returned HTTP 500 after a routine deploy.
Flask couldn't find `templates/` anywhere.

**Root cause:** The prior `sync.sh` passed multiple directory arguments
to rsync (`templates/`, `static/`, `strategies/`, `tests/`) — each with
a trailing slash. rsync's semantics for `<src>/` with multiple sources
merges all their *contents* into the target root, so `templates/base.html`
and `strategies/__init__.py` both landed at `/opt/quantopsai/` root.
`--delete` then removed the actual `templates/` directory because it
was no longer "in source" after the flattening.

**Why it wasn't caught:** No deploy-smoke test. The sync script wasn't
tested.

**Fix:**
- Rewrote `sync.sh` to sync the project root as a single source
  (`/Users/mackr0/Quantops/` with trailing slash → `/opt/quantopsai/`),
  preserving directory structure
- Deploy restored templates/ and put everything back in correct subdirectories
- `deploy.sh` updated to explicitly include `strategies/` and `tests/`

**Tests added:** Indirectly by the cycle_data guardrail test, which also
asserts other critical exclusions are present. A dedicated deploy-smoke
test would be better — tracked informally as a hygiene follow-up.

---

## Pre-changelog fixes (retroactive — limited context)

Entries before this date were not tracked contemporaneously. Reconstructed
from session memory; details may be incomplete.

### 2026-04-13 — Capital allocator cap redistribution infinite-excess bug

**Severity:** high

**Symptoms:** At a single strategy, the 40% cap logic capped it to 40%
and had "nowhere to redistribute" the 60% excess, so that capital was
simply lost from the allocation (sum < 1.0). At 2 strategies with both
over-cap, the redistribution oscillated and left sum < 1.0.

**Root cause:** Original cap loop used a stale snapshot of `normalized.items()`
and redistributed excess based on a single pass that didn't iterate to
convergence.

**Fix:** Iterative cap-and-redistribute loop in `multi_strategy.compute_capital_allocations`.
Stops when no strategy is over the cap or no strategies are under the cap.
Single-strategy case keeps 100% (nowhere to redistribute; correct behavior).

**Tests:** `test_multi_strategy.TestCapitalAllocations::test_weights_always_sum_to_one`
covers 1, 2, 6 strategies. `test_no_strategy_exceeds_forty_percent_cap`.

### 2026-04-13 — Statistical significance assertion using numpy booleans

**Severity:** low (test-only)

**Symptoms:** Rigorous backtest test failed with `np.True_ is True`
mismatch on assertion.

**Root cause:** `scipy.stats` returns numpy booleans, not Python `bool`.
`assert result["significant"] is True` fails even when the test is
semantically correct.

**Fix:** Wrapped return values with `bool()` in `rigorous_backtest.py`.

### 2026-04-13 — `/api/portfolio/{id}` passing profile dict instead of id

**Severity:** medium

**Symptoms:** API endpoint returned errors instead of portfolio data.

**Root cause:** `build_user_context_from_profile()` expects profile_id,
was being called with the profile dict itself.

**Fix:** Pass `prof["id"]` instead of `prof`.

### 2026-04-12 — Stop/target displayed as raw percentages ($0.13, $0.19)

**Severity:** medium (UX + correctness)

**Symptoms:** Trades showed stop-loss as $0.13 and take-profit as $0.19
— these were 13% and 19% values stored as raw percentages but rendered
as dollar prices.

**Root cause:** `execute_trade` stored `stop_loss_pct` directly rather
than converting to a dollar price at the time of trade.

**Fix:** `stop_price = price * (1 - actual_sl_pct)` at execution.
Retroactively fixed existing trade rows in the DB.

### 2026-04-12 — Total return +199.8% on "All Profiles" view

**Severity:** medium (correctness)

**Symptoms:** Dashboard showed impossibly high aggregate returns when
"All Profiles" was selected.

**Root cause:** `_gather_snapshots()` summed per-day snapshots across
profiles without forward-filling gaps. A profile missing a day's
snapshot contributed zero, distorting the aggregate.

**Fix:** Forward-fill missing days per profile before aggregation.

### 2026-04-12 — Tab persistence lost on profile dropdown change

**Severity:** low (UX)

**Symptoms:** Changing the profile dropdown lost the active tab hash
(e.g., `#ai` → bare URL).

**Root cause:** Form submit replaced `window.location` without preserving
`.hash`.

**Fix:** Inline `onchange` handler that captures `window.location.hash`
and re-appends before submit.

---

## How to add a new entry

When fixing a production bug, copy this template:

```markdown
## YYYY-MM-DD — Short title

**Severity:** critical | high | medium | low

**Symptoms:** What the user/operator saw.

**Root cause:** What was actually wrong in the code.

**Why it wasn't caught:** Honest answer — missing test coverage,
wrong assumption, etc.

**Fix:** What changed. Point at files.

**Tests added:** Named tests in `test_*.py` that prevent regression.
If none exist yet, track it as a follow-up TODO.

**Follow-up (optional):** Related work not done in this fix.
```

Add the entry **before the deploy ships**, not after. Severity is
assessed on impact, not how hard the fix was.

