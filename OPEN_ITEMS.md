# QuantOpsAI — Open Items (Master List)

**Date:** 2026-06-04 (last reconciliation)
**Purpose:** Single source of truth for every open / deferred / partial item across every plan file in the repo and every code-level marker (`TODO`, `deferred`, `future enhancement`, `honest limit`). One place to look so nothing stays invisible.

**How to read it:**
- ✅ DONE — fully shipped + verified
- ⚠ PARTIAL — substantially shipped with named gaps
- ⏳ OPEN (free) — buildable now, no paid dependency
- 💰 OPEN (paid) — requires a paid feed / account / vendor
- 🔒 DEFERRED — explicitly out-of-scope for now (real-money phase, etc.)

**Rule for keeping this current:**
When something here moves to ✅, update the entry with the commit + date. When new work surfaces a new gap, add an entry. The CHANGELOG tracks history; this file tracks what's still pending.

---

## 0. THE CONNECTIONS FIX LIST (2026-07-26 exploration → operator-approved work queue)

The 2026-07-26 docs+code exploration found the system's documented
capability materially exceeds its operating capability — mostly broken
CONNECTIONS, not missing components. Operator directive: "make a plan
to fix it all and check off the list one thing at a time." Each item
ships through the full gate (suite green → commit → push →
./droplet-sync.sh → live verify). Behavior-impact tags protect the
running cohort: [none] = no trading change; [inputs] = restores
DESIGNED AI inputs; [exec] = execution change, operator times it.

**Phase 1 — silently broken (all ⏳ free):**
- [x] P1.1 [inputs] Alt-data reconnect — RESOLVED 2026-07-26. Real
      causes (paths were fine): 13F ticker on 1.2% of 965k rows
      (35-CUSIP hand seed) + period_of_report '' on ALL filings
      (scraper read "periodOfReport"; SEC calls it "reportDate") +
      QoQ summed ALL prior quarters (SUM+LIMIT no-op) → every QoQ
      ≈−100%; biotech ticker on 6.4% (hand sponsor map); StockTwits
      watchlist 37 vs ~400-symbol universe, and stocktwits_data_absent
      fired CAUTION off our own coverage gap. Shipped: altdata_enrich.py
      (SEC-registrant name matching; equity-class-only stamping;
      period backfill incl. paginated submissions) wired into
      run-altdata-daily.sh; reportDate key fix; per-filer-latest
      reader fallback; QoQ = immediately-preceding quarter; covered
      flag + specialist guard; universe watchlist. Backfilled live:
      443k holdings rows stamped (47%), 581/581 periods, +1,561
      trials; 13F informative 13/14 universe probe (was ~0);
      stale cache purged (2,256 rows). Pinned:
      test_altdata_reconnect_2026_07_26.py (19).
- [x] P1.2 [inputs] Dark pool — RESOLVED 2026-07-26. Was triple-
      broken: no date filter (FINRA default order = week 2023-11-06
      on 86% of payloads); summed aggregate + per-firm (double count)
      + non-ATS OTC wholesaler rows; pipeline read nonexistent
      `ats_pct_of_total` → feature constant 0. Rewrote reader:
      dateRangeFilters 42d window + client-side freshness guard,
      latest week only, ATS_W_SMBL aggregates for volume, firm rows
      for venue count, real ats_pct_of_total vs yf consolidated
      weekly volume (refuses >100% artifacts → None). Null-safe
      extraction; prompt shows pct + week. Live: AAPL wk 2026-06-29
      45.7M sh / 30 venues / 17.8%; 752 stale cache rows purged.
      Pinned: test_dark_pool_fresh_2026_07_26.py (10).
- [x] P1.3 [none] Slippage calibrator — RESOLVED 2026-07-26. The
      journal never writes status='filled' (real lifecycle:
      pending_* → open → closed/canceled/expired), so the calibrator
      matched zero rows on every profile and K stayed at the 12.0
      default. Predicate now `status IN ('open','closed')` (the only
      statuses that ever carry fill_price); 'cover' side signed as a
      buy in the sample loop; the SAME dead predicate killed three
      views.py analytics (two MC round-trip joins → 'closed' legs;
      predicted-vs-realized series → real-fill whitelist). Live:
      p212 fits 242 samples (230 real-ADV) + 200-sample bootstrap
      bucket. Honest note for P2.1: the interceptless sqrt fit
      absorbs decision-to-fill drift, so fitted K rides the 200
      clamp on paper fills — bootstrap residuals carry the real
      noise. Pinned: test_slippage_calibrator_2026_07_26.py
      (+ repo-wide status='filled' predicate ban).
- [x] P1.4 [inputs] Execution-cost specialists — RESOLVED 2026-07-26.
      wide_spread_caution + slippage_high_caution regexed '%' out of
      a string that says 'bps' — zero fires since the format change.
      Both now read the structured slippage_estimate dict
      (total_bps; thresholds 15-30 / 30+ bps ≡ old 0.15%/0.30%)
      with a bps-string parse fallback. Folded in:
      multi_alt_data_silent was dead the other way — its len(v)>1
      carrier heuristic counted every source as signal because all
      readers return multi-key dicts even when empty
      ({"is_cluster": false}, {"ats_volume": 0}) → never fired; now
      per-source has-signal predicates on the live payload shapes
      (patent_activity dropped — source disabled upstream). All
      three fixture rows re-seeded to real payload shapes (the old
      fixtures mirrored the bugs). Pinned:
      test_execution_cost_specialists_2026_07_26.py.
- [x] P1.5 [none] Wheel config corruption — RESOLVED 2026-07-26.
      Mechanism: DB stores JSON text; the settings template only
      joins LIST values; custom_watchlist is parsed to a list by
      every profile-dict builder but wheel_symbols never was — so
      the textarea displayed the raw text '[]' and the next save
      split it on commas into a "ticker". Fixed at the root: all
      three profile-dict builders parse wheel_symbols
      (custom_watchlist treatment); writer + parser share a
      ticker-shape filter (junk logged + dropped, never traded);
      10 corrupt rows repaired live ('["[]"]' → '[]'). Pinned:
      test_wheel_symbols_integrity_2026_07_26.py (incl. the exact
      corruption round-trip now converging).
- [ ] P1.6 [exec: p216 only] Meta-pregate has no AUC floor — p216's
      AUC-0.397 (anti-predictive) model still trims its candidates.
- [ ] P1.7 [none] Meta retrain isn't gated on enable_meta_model —
      p212 (NoMetaModel ablation) trains a model it never uses.
- [ ] P1.8 [inputs] Structurally-zero sources (finra_short_vol,
      activist_13dg, star_manager, risk_factor_diff,
      insider_track_records) + constant meta features (reddit_*,
      _cpi_yoy, dark_pool_pct) + 999-sentinels fed as real values.
      Fix or honest-disable; sentinels → missing.
      ADDED 2026-07-26 (found during P1.4 live verify): NEITHER
      cache layer refuses to cache empty payloads — a transient
      blip (rate limit / DB lock / circuit breaker, e.g. Google
      Trends 429 → {}) poisons a (symbol, source) for the full TTL
      (up to 24h-7d), and the two layers re-seed each other. AAPL
      served {} for ~20 sources through the bundle while direct
      reads were fine. One-time purge done (2,414 + 139 empty
      rows); the FIX (negative-cache short-TTL or refuse-empty in
      alt_data_cache.cache_set + ad._set_cached) belongs here.
      Also fold in: trade_pipeline reads alt.get("congressional")
      (not a bundle key) → congress_direction constant "neutral";
      multi_alt_data_silent len>1 note RESOLVED with P1.4.

**Phase 2 — cost truth & execution:**
- [ ] P2.1 [none — labels only] actual_return_pct_net nets a CONSTANT
      2×12.5bps estimate; use per-row realized entry/exit slippage
      (decision vs fill, already stored) + short borrow accrual so the
      tuner/meta optimize NET edge. (Not commissions — Alpaca is
      commission-free; the gap is slippage/spread realism.)
- [ ] P2.2 [exec — operator times rollout] Equity path never reads the
      NBBO though it's in the snapshot payload. Step 1: record spread
      at decision (telemetry, no behavior change). Step 2:
      marketable-limit orders + never-market multi-leg options.
- [ ] P2.3 [none] Backtester slippage: drop hardcoded adv=1M /
      constant 12.67bps.

**Phase 3 — breadth:**
- [ ] P3.1 [proposal → operator decision] The 93% HOLD wall: funnel
      telemetry → evidence-driven threshold proposal. IR = skill ×
      √breadth; calibration proves the skill, the funnel starves the
      breadth (1-2 candidates from ~8,000).

**Phase 4 — structural:**
- [ ] P4.1 [none] portfolio_risk_snapshots has NEVER written a row
      (713-line factor model, flag on for all 13) — fix "insufficient
      factor data" so it at least reports.
- [ ] P4.2 [design decision] Binding vol-aware sizing (Kelly/parity are
      computed, rendered as prompt English, and ignored by the
      fixed-fraction formula).
- [ ] P4.3 [gated on corpus ~2 months at current 21k/2.5wk cadence]
      Fine-tune training_runner (archive empty, no trainer, registry
      tables never created, use_finetuned_ai column doesn't exist).
- [ ] P4.4 [docs] Truth pass: "$0.27/day" → real spend (~$2.6
      operational + shadow, <$10/day); "50K rows after 12 months" →
      ~2 months at current cadence; stale reset framing.

---

## 1. COMPETITIVE_GAP_PLAN.md

| Item | Status | Notes |
|---|---|---|
| 1a. Options trading layer | ✅ DONE | Phases A-F + H1-H4 of OPTIONS_PROGRAM_PLAN |
| 1b. Statistical arbitrage at scale | ✅ DONE | `stat_arb_pair_book.py` + scheduler tasks (gated `enable_stat_arb_pairs`) |
| 1c. Volatility strategies | ⚠ PARTIAL | Phases E/F shipped (vol regime, earnings IV crush). Long-vol portfolio hedge ✅ DONE 2026-05-02 (`long_vol_hedge.py`) |
| 2a. Barra-style multi-factor model | ✅ DONE | `portfolio_risk_model.py` + `risk_stress_scenarios.py` |
| 2b. Intraday risk monitoring | ✅ DONE | `intraday_risk_monitor.py`, gated `enable_intraday_risk_halt` |
| 3a. Web-scraped alt data | ⚠ PARTIAL | See §1.1 below |
| 3b. Earnings-call sentiment NLP | ✅ DONE | `sec_filings.get_earnings_call_sentiment` |
| 3c. Paid data feeds | 💰 OPEN | Quiver Quant ($30-100/mo), Polygon ($50/mo), Benzinga Pro ($150/mo) |
| 4a. Futures + FX via IBKR | ⏳ OPEN | ~1 month build; opens cross-asset hedging |
| 4b. Crypto deeper build | 🔒 DEFERRED | Awaiting strategy thesis |
| 5a. Online / continuous learning | ✅ DONE | `online_meta_model.py` (SGD freshness layer) |
| 5b. Adversarial / red-team specialist | ✅ DONE | 5th specialist with VETO authority |
| 5c. Better backtesting infrastructure | ⚠ PARTIAL | See §1.2 below |
| 6a. Real money via IBKR Pro | ⏳ OPEN | Within 4a; ready once Alpaca paper proves out |
| 6b. Capital allocation across strategies | ✅ DONE | `strategy_capital_allocator.py` |

### 1.1 Open inside 3a (web-scraped alt data)

| Sub-item | Status | Effort |
|---|---|---|
| Reddit ticker mentions | ✅ DONE | `social_sentiment.get_ticker_mentions` |
| StockTwits sentiment | ✅ DONE | `alternative_data.get_stocktwits_sentiment` |
| Earnings transcript NLP | ✅ DONE | `sec_filings.get_earnings_call_sentiment` |
| Congressional trades | ✅ DONE | `alternative_data.get_congressional_recent` |
| Institutional 13F holdings | ✅ DONE | `alternative_data.get_13f_institutional` |
| Biotech FDA / PDUFA milestones | ✅ DONE | `alternative_data.get_biotech_milestones` reads from `pdufa_events`; `pdufa_scraper.py` populates via SEC EDGAR full-text search on 8-K filings + hand-curated fallback seed; wired into `_task_pdufa_scrape` daily-idempotent cron (multi_scheduler.py:3669). Prod has 21 events as of 2026-06-07. |
| Google Trends search interest | ✅ DONE | `alternative_data.get_google_trends_signal` |
| Wikipedia page-views | ✅ DONE | `alternative_data.get_wikipedia_pageviews_signal` |
| App Store rankings | ✅ DONE | `alternative_data.get_app_store_ranking` shipped + WoW change wired via `_get_wow_change` at `alternative_data.py:2018-2096` (see §10 row). Earlier partial-status framing superseded by the §10 ✅ DONE marker; this row resolves the internal contradiction. |
| GitHub commit activity | 🔒 DEFERRED | Most S&P doesn't have meaningful public repos; weak signal. |
| Job-postings volume | 🔒 DEFERRED | No clean free source (LinkedIn paid, Indeed TOS-fragile). |
| 10b5-1 insider planned-sale tracking | ✅ DONE 2026-06-07 | Form 4 normalizer captures `is_10b5_1_plan` from footnote text via `_is_10b5_1_footnote` (case-insensitive, dash-variant-tolerant). `insider_txns.is_10b5_1_plan` column + ALTER-ADD migration. `get_recent_insider_activity` splits `discretionary_*` from `planned_10b5_1_*`; `net_direction` and `cluster_count` exclude plan-driven trades. |

### 1.2 Open inside 5c (better backtesting)

| Sub-item | Status |
|---|---|
| Walk-forward + OOS-disjoint splits in `rigorous_backtest` | ✅ DONE |
| Synthetic options backtester (Phase H of options plan) | ✅ DONE |
| Realistic slippage model (`slippage_model.py`) | ✅ DONE |
| Monte Carlo backtest with bootstrap (`mc_backtest.py`) | ✅ DONE |
| Per-strategy MC tiles | ✅ DONE 2026-05-03 |
| **MC bootstrap by-day not by-trade** | ✅ DONE 2026-05-03 | `bootstrap_mode='by_day'` is the default at `mc_backtest.py:128`; samples one slippage realization per day so correlated regimes (full day of wide spreads) are captured. `per_trade` mode kept as a legacy baseline. |
| **ADV-at-trade-time storage** | ✅ DONE | `adv_at_decision` column captured at order submit; calibrator at `slippage_model.py:163-204` uses real participation rate (`qty / adv_shares`) instead of the coarse $50M proxy. Legacy rows pre-dating the column fall back to the proxy. |
| **Slippage model recalibration after real money** | 🔒 DEFERRED | K is currently fitted from paper fills (see `slippage_model.py:42` docstring); rerun after 30+ days live. |

---

## 2. OPTIONS_PROGRAM_PLAN.md

| Item | Status | Notes |
|---|---|---|
| Phase A. Greeks (aggregator + gates + dashboard) | ✅ DONE | A1-A3 |
| Phase B. Multi-leg primitives + atomic execution | ✅ DONE | B1-B4, 11 builders |
| Phase C. Lifecycle (roll + assignment + wheel) | ✅ DONE | C1-C3 |
| Phase D. Dynamic delta hedging | ✅ DONE | D1 |
| Phase E. Vol surface analysis | ✅ DONE | E1-E4 |
| Phase F1. Earnings vol plays | ✅ DONE | |
| Phase F2. Macro event plays (FOMC/CPI/NFP) | ✅ DONE | Tracker shipped 2026-05-03; integration shipped 2026-05-09 (`render_macro_play_recommendation_for_prompt` + trade_pipeline + ai_analyst wiring) |
| Phase G1. Real-time options chain feed | 🔒 DEFERRED | "Defer until real-money phase" (per plan) |
| Phase H1. Synthetic options backtester L1-L4 | ✅ DONE | 31 tests |
| **Phase H L5. Backtester dashboard integration** | ⏳ OPEN | API callable; UI panel not yet wired. Plan flags as "not strictly needed". |
| **`wheel_symbols` populated per profile** | ⏳ OPEN | `options_wheel.py` is built but NO profile has the field set, so wheel never fires. Need: settings UI + per-profile opt-in symbol list. |

---

## 3. ROADMAP.md (10-phase main + Phases 11-13)

| Phase | Status |
|---|---|
| 1. Meta-model on own predictions | ✅ DONE |
| 2. Scientific backtesting infra (10 gates) | ✅ DONE |
| 3. Alpha decay monitoring | ✅ DONE |
| 4. SEC filings semantic analysis | ✅ DONE |
| 5. Options chain oracle | ✅ DONE |
| 6. Multi-strategy parallel execution | ✅ DONE |
| 7. Strategy auto-generation | ✅ DONE |
| 8. Ensemble of specialized AIs | ✅ DONE |
| 9. Event-driven architecture | ✅ DONE |
| 10. Cross-asset crisis detection | ✅ DONE |
| 11. Long/Short parity | ✅ DONE | Phases 1-4 of LONG_SHORT_PLAN |
| 12. Exit execution hardening | ✅ DONE | All 4 stages of INTRADAY_STOPS_PLAN |
| 13. Competitive-gap closure | ⚠ PARTIAL | See §1 above |

---

## 4. LONG_SHORT_PLAN.md

| Phase | Status |
|---|---|
| Phase 1 (1.0 → 1.14) | ✅ DONE |
| Phase 2 (2.1 → 2.5) | ✅ DONE |
| Phase 3 (3.1 → 3.6) | ✅ DONE |
| Phase 4 (4.1 → 4.5) | ✅ DONE |

Nothing open in this plan.

---

## 5. INTRADAY_STOPS_PLAN.md

| Stage | Status | Commit |
|---|---|---|
| Stage 1: Static stop-loss on entry | ✅ DONE | 3d84543 |
| Stage 2: Take-profit (replaced by Stage 3) | ✅ DONE | b024ab8 (superseded) |
| Stage 3: Trailing-stop on entry | ✅ DONE | f34b81f |
| Stage 4: Polling defers to broker | ✅ DONE | 7dbbf88 |

Nothing open in this plan.

---

## 6. COST_AND_QUALITY_LEVERS_PLAN.md

| Lever | Status |
|---|---|
| 1. Persistent disk cache for ensemble + political_context | ✅ DONE |
| 2. Meta-model pre-gate before ensemble | ✅ DONE |
| 3. Per-profile specialist disable list (auto-disable + auto-re-enable) | ✅ DONE |

Nothing open in this plan.

---

## 7. ALTDATA_INTEGRATION_PLAN.md

| Wave | Status |
|---|---|
| W1. Read layer (4 helpers) | ✅ DONE |
| W2. AI integration | ✅ DONE |
| W3. Production deployment (`altdata/` subdirectory after 2026-05-04 merge into main repo; was `/opt/quantopsai-altdata/`) | ✅ DONE |
| W4. UI + docs | ✅ DONE |
| **PDUFA scraper** | ✅ DONE 2026-05-04 (commits `ffe8b9c..41c3b28`). EDGAR full-text search for "PDUFA date" in 8-K filings; populates 10/10 events with real drug names + action types after the regex iteration. |

---

## 8. DYNAMIC_UNIVERSE_PLAN.md

| Step | Status |
|---|---|
| 1. Sector classification module (`sector_classifier.py`) | ✅ DONE |
| 2. Historical-universe freeze (`segments_historical.py`) | ✅ DONE |
| 3. Dynamic universe provider in `segments.py` | ✅ DONE |
| 4. Remove `screener.py` dead weight | ✅ DONE |
| 5. UI updates (`views.py`) | ✅ DONE |
| 6. Tests | ✅ DONE |
| 7. CHANGELOG | ✅ DONE |
| 8. Deploy + verify | ✅ DONE |

Out-of-scope (per plan §7): multi-exchange expansion, corporate-action awareness, crypto dynamic discovery, short-availability tracking — all 🔒 DEFERRED by design.

---

## 9. SCALING_PLAN.md (graduation milestones)

| Stage | Capital | Status |
|---|---|---|
| Stage 1: $3M Paper | $3M virtual ($1M × 3 Alpaca paper accounts) | ✅ ACTIVE | Per docs/15 experiment design; cohort reset 2026-06-04 with new accounts after orphan-class contamination. |
| Stage 2: $10K Real Money | $10K | ⏳ OPEN | Prerequisite: Stage 1 success criteria (30+ days, >45% win rate). Switch Alpaca paper → live. |
| Stage 3: $50K Real Money | $50K | ⏳ OPEN | Prerequisites: Stage 2 profitable 60+ days. Add Polygon real-time data, $5M ADV filter, limit orders by default. |
| Stage 4: $100K-$250K | $100K+ | ⏳ OPEN | WebSocket streaming arch, Level 2 order book, VWAP execution, iceberg orders. |
| Stage 5: $1M+ | $1M+ | ⏳ OPEN | Full execution rebuild, dedicated infra, regulatory compliance. |

---

## 10. Code-level markers (`grep` of `.py` for TODO / deferred / future enhancement)

| File:line | Item | Status |
|---|---|---|
| `ai_analyst.py:640` | Earlier "the AI to propose with action='OPTIONS' (deferred to follow-up)" | ✅ DONE 2026-06-07 — AI proposes OPTIONS natively (prompt vocabulary + parse-layer validation; `tests/test_ai_proposes_options_natively_2026_06_07.py`). Comment rewritten 2026-06-10 when single-leg-ONLY constraint + strike snap landed at the parse layer. |
| `alternative_data.py` (App Store WoW) | Earlier "leave None — future enhancement when daily snapshots persist" | ✅ DONE — WoW logic implemented at `alternative_data.py:2018-2096` (`_get_wow_change`, "Item 2 of OPEN_ITEMS — WoW change vs 7 days ago") |
| `mc_backtest.py` (by-day bootstrap) | Earlier "future enhancement" framing | ✅ DONE 2026-05-03 — `bootstrap_mode='by_day'` is the default (line 128); module docstring rewritten in Issue 10 (commit `47de74d`) |
| `multi_scheduler.py:1257-1284` | Earlier `multi_scheduler.py:1196` "sector_moves + halted_held_symbols deferred" | ✅ DONE 2026-05-09 — comment removed; `_compute_sector_moves` (L1257) + `_compute_halted_held_symbols` (L1284) wired into the intraday risk check; AST guardrail in `tests/test_intraday_risk_full_wiring.py` enforces all kwargs are passed |
| `options_earnings_plays.py:24-26` | Earlier `:25` "with index ETFs (SPY/QQQ); deferred until macro-event tracker exists" | ✅ DONE 2026-05-09 — comment rewritten to point at the macro analog (`macro_event_tracker.render_macro_play_recommendation_for_prompt`) which is wired in trade_pipeline + ai_analyst |
| `options_roll_manager.py:31-34` | Earlier "Roll-window thresholds. Tunable per-profile in a future commit." | ✅ DONE — comment now reads "these are now per-profile tunable knobs (UserContext fields, settings UI). Module constants stay as fallbacks when a function is called without ctx." |
| `slippage_model.py:163-168` | Earlier `:165` "We don't store ADV at trade time, so use a simple proxy" | ✅ DONE 2026-05-10 — comment rewritten in Issue 10 (commit `47de74d`) to describe actual behavior (`adv_at_decision` IS stored and used; legacy rows fall back to the $50M ADV proxy) |
| `slippage_model.py:42` | Earlier `:197` "K is currently fitted from paper fills" — text now lives in module docstring at L42: "fills will deviate; the calibrator should be re-run after going [live]" | 🔒 DEFERRED — recalibrate after real money. Concept unchanged; only the line moved. |
| `short_borrow.py:3` | "DYNAMIC_UNIVERSE_PLAN.md / TECHNICAL_DOCUMENTATION.md §15 deferred" | ✅ DONE — `short_borrow.py` has a 3-tier model: easy_to_borrow=True → DEFAULT_BPS_PER_DAY (general collateral), easy_to_borrow=False → MEDIUM_BORROW_BPS_PER_DAY (~8% annualized), per-symbol overrides in HARD_TO_BORROW_BPS_PER_DAY for known meme/squeeze/HTB names. Live borrow-rate API integration (IBKR SLB feed) remains a paid-feed dependency per §1 row 3c. Docstring updated 2026-06-07. |

---

## 11. Documented honest limits (acknowledged but not fixed)

These are NOT bugs; they're scope constraints surfaced in code comments. They shape future work direction.

| Limit | File | Notes |
|---|---|---|
| Synthetic options backtester ≠ precise P&L | `OPTIONS_PROGRAM_PLAN.md` Phase H | Doesn't capture bid-ask spread, IV term structure, catalyst vol pop. Sufficient for STRATEGY VALIDATION, not PRECISE FORECASTING. |
| Parametric VaR understates tails | `portfolio_risk_model.py` | Assumes normal returns; Monte Carlo helps but inherits factor distribution normality. |
| Stress scenarios miss cross-asset risk | `risk_stress_scenarios.py` | No rates / FX / commodities in factor set yet. 2022-style rate shocks under-report. |
| 1987 / dot-com scenarios use French only | `risk_stress_scenarios.py` | Sector ETFs didn't exist; sector-tilt P&L flagged as "approximation_quality: low" or "medium". |
| Long-vol hedge: SPY puts hedge BETA, not idio | `long_vol_hedge.py` | Concentrated single-name books still bleed even if SPY rallies. |
| Slippage MC IID per trade — RESOLVED via `bootstrap_mode='by_day'` (default) at `mc_backtest.py:128` | — | Kept here as a historical limit note; the per_trade mode is preserved as a legacy baseline. |
| Slippage K calibrated from paper | `slippage_model.py:42` | Real-money fills will deviate. K refit deferred until 30+ days live. |

---

## 12. Recommended next batch — STATUS

All 10 items SHIPPED 2026-05-03. Commits: `91a6f9a` (#1-4), `81d4d95` (#5-10).

1. ✅ **ADV-at-trade-time storage** — `trades.adv_at_decision` captured at submit; slippage calibrator uses real participation_rate.
2. ✅ **App Store WoW snapshot task** — `app_store_history` table, daily-idempotent snapshot, WoW deltas in prompt.
3. ✅ **MC bootstrap by-day** — `bootstrap_mode='by_day'` default; whole-day slippage realizations shared across same-day trades.
4. ✅ **`wheel_symbols` settings UI** — schema column + textarea + parser.
5. ✅ **Options backtester dashboard panel** — `/api/options-backtest` + Run button on Brain tab.
6. ✅ **PDUFA scraper** — `pdufa_scraper.py` BiopharmCatalyst scrape + daily-idempotent task.
7. ✅ **Short borrow rate tracking** — 3-tier rate lookup (HTB / non-GC / GC) + per-candidate annotation.
8. ✅ **AI vocabulary for proposing options trades** — OPTIONS action ungated for any candidate with `options_oracle_summary`.
9. ✅ **Macro event tracker (Phase F2)** — `macro_event_tracker.py` with FOMC/CPI/NFP calendar + evaluate_macro_play.
10. ✅ **Per-profile options roll-window knobs** — 3 schema columns; `evaluate_for_roll` parameterized; settings UI.

---

## 13. NOT pursuing (explicitly not on the open list)

Per `COMPETITIVE_GAP_PLAN.md` §"Explicitly NOT pursuing":
- Latency arbitrage (sub-microsecond + colocation)
- Market making (exchange membership + low-latency infra)
- Block trading capacity
- Index inclusion arbitrage
- Insider-information networks (paid expert networks)

These are real differentiators of billion-dollar funds but the gap is structural, not addressable in software.

---

## How this list is maintained

- **Adding an item:** when a code marker (`TODO`, `deferred`, `future enhancement`) gets shipped, add it under §10 with a status. When a new plan ships with new gaps, add a section.
- **Closing an item:** mark ✅ DONE with the commit + date. Don't delete entries — keeping them visible documents what was completed.
- **Quarterly sweep:** every ~3 months, re-run the `grep` audit (see §10) to catch any new code-level deferrals that snuck in. The pattern matchers: `TODO`, `FIXME`, `HACK`, `XXX`, `deferred`, `defer until`, `future enhancement`, `future:`, `NOT YET`, `not yet built`, `not yet wired`, `future improvement`, `improve later`, `known limit`, `limitation:`, `honest limit`.

---

## INCIDENT FOLLOW-UP 2026-07-23 — ✅ RESOLVED 2026-07-24: the anonymous entry-closer (p213 COST #296)

**Cause (was open; now root-caused + fixed in code).** The 2026-07-23
provenance commit made every `status='closed'` writer sign the reason
column and named the reconciler protective-fill handler as prime suspect.
Live broker forensics **refuted** that suspect: #296's entry
(`740e9736`) filled 5 COST @920.27; both bracket children never filled
(canceled 16:03:10, filled_qty 0); the shares were never sold. The real
writer is `_task_update_fills`'s **FIFO lot-close**, whose scope SELECT
treated every non-canceled sell — including a bracket's own resting
`pending_protective` stop/TP — as a completed exit. #296's own two
resting children (stop 5 + TP 5) FIFO-consumed its 5-share lot to zero
and closed it while the broker still held the shares (acct-56 drift +5;
−4,601 decomposition gap). The 16:03:10 child cancel is downstream (a
'closed' entry's protective sweep cancels its unbacked legs), not cause.

**Fix (2026-07-24, commit on main).** `_fifo_lots_fully_consumed`: a
consumer consumes a lot only when it carries a `fill_price` (resting/
unfilled orders are NULL). Fail-closed — a lot can only be under-closed
(self-heals) never over-closed into a phantom. Pinned in
`test_fifo_resting_sell_no_phantom_close_2026_07_24.py`. Was a LIVE
recurrence (#304, the naked-bracket re-arm, would re-close #296);
deploying the fix defuses it — no data repair needed beyond the
operator's earlier reopen of #296.

**Fleet-wide "bracket has NO live child" (~1,500×/cycle):** root-caused
as the shared-account architecture (N virtual profiles' full-size
protective sells overflow one real position → broker cancels the
excess). The 2026-06-16 naked-bracket fix re-arms a fresh stop each
cycle, so it is a re-arm heartbeat, not a protection gap. Eliminating
the warning is an architectural choice (per-profile synthetic stops vs
broker brackets) left to the operator; protection is not at risk.

## INCIDENT FOLLOW-UP 2026-07-24 — ✅ RESOLVED same day: the NFLX 123-share cross-profile oversell + acct-56 cash drift

**Both repaired from broker truth, same session (see the 2026-07-24
"session close" CHANGELOG entry).** The cash drift was NOT the NFLX
flows: a per-symbol bisect landed it on p212's CVX 180C expiry
auto-liquidation (broker BUY 4 @7.10, order 3b5758a0) that the
pre-07-20 assignment sweep never journaled (−$2,840 missing +
fabricated +416 pnl) — repaired via
`scripts/repair_cvx_assignment_cash_p212_2026_07_24.py`. The NFLX
oversell was enabled by an 8-day-unconfirmed PARTIAL trailing-stop
fill (123/269) leaving p214's believed inventory stale — its 07-17
stop-loss then legally oversold 123 shares delivered from p213's
inventory; repaired via
`scripts/repair_nflx_inventory_transfer_2026_07_24.py` (internal
transfer @66.57, the actual exit price; account aggregates unchanged
by construction). Prevention already shipped: update_fills
qty-truthing (test-pinned) + the 07-22 rate-limit/cache work keep
fill confirmation in budget every cycle. Verified live mid-trading:
position drift 0, cash drift 0, p213 NFLX = 147 = broker backing,
p214 flat. The original P1 text is preserved below for the record.

**(original P1 text)** ⏳ was: the NFLX 123-share cross-profile oversell + acct-56 cash drift (P1)

**Symptom (live audit errors, damage NOT yet repaired):** acct 56 NFLX
journal_open +270 (p213 row #53, real filled buy, order 78992a37) vs
broker +147 — because p214 sold **392** NFLX against only **269** ever
bought (raw rows; every sell broker-filled). Its 123-share oversell
physically sold shares backing p213's live lot during the 07-22 rate-
limit storm. Signed own-fill sums still equal the broker (+270 −123 =
+147), so the integrity gate correctly does not halt — but p213's book
claims inventory the account no longer holds, and p214's book shows no
live short. Related: acct-56 cash parity drift −$2,851.43 (profiles
211–215), not yet root-caused. `reconcile_aggregate_drift` dry-run
REFUSES all rows (all broker-filled — nothing voidable): correct.

**To do:** (a) root-cause how 123 shares of sells passed the guarded-api
oversell door (order_guard own-journal cap) during the 429 storm;
(b) decide + execute the repair (p214's oversell surfacing as its live
short vs. reducing p213's lot — broker truth first, operator visibility
required); (c) tie out the −$2,851.43 cash drift against the same flows.

**~~Also open (design decision)~~ ✅ DONE 2026-07-24 (operator decided:
"make the system reflect" reconciled books):** the audit's parallel
live-only FIFO is retired; `_journal_open_qty_per_symbol` now delegates
to `journal.get_virtual_positions` (`include_unpriced=True` quantity
truth). GOOG/WMT/NFLX all audit at the broker's exact number; only
genuine broker↔books divergence can flag. Pinned in
`test_audit_lens_canonical_2026_07_24.py`.

## INCIDENT FOLLOW-UP 2026-07-22 — ✅ RESOLVED 2026-07-22 (header updated 2026-07-25; the fix shipped same-day but this ledger entry was never flipped): the spread-close bookkeeping bug

**Fixed by the 2026-07-22 "THE CREATOR FIX" commit** (see CHANGELOG):
the roll manager's auto-close journals its own close row (no open-row
clobber, direction-aware pnl via journal.realized_option_close_pnl),
resubmit dedup replaces the in-place flip, and the per-cycle phantom
sweep runs automatically. Pinned in
`test_roll_close_corruption_fix_2026_07_22.py` (7 tests). The original
text below is preserved for the record.

**Symptom (repaired, cause NOT yet fixed):** p212's GOOG 375/380 bear-call
spread open rows (ids 257/258, written 2026-07-15T19:02, sides sell/buy @
10.05/8.35, status pending_fill) were CLOBBERED when the spread's
single-leg closes finally filled on 2026-07-22 13:45 (buy 375C @1.85 order
8a0a2d3b…, sell 380C @1.19 order 4d12d9f9…): something overwrote the rows'
order_ids with the CLOSE order ids and stamped per-leg pnl WITH THE LONG
LEG'S SIGN INVERTED (+2,148 where truth is −2,148), fabricating +4,608
realized pnl → constant decomposition gap → integrity kill switch, fleet
halted a full day. Books repaired via
`scripts/repair_goog_spread_p212_2026_07_22.py` (true pnl +312, broker-
verified). THE WRITER IS STILL IN THE CODE — any future spread close can
corrupt the books the same way.

**Eliminated as the writer** (each checked against the row shapes):
`options_proactive_exits` (writes NEW sell rows only),
`options_lifecycle` expiry sweep (expiry-only; this spread hadn't expired),
`bracket_orders.sync_pending_protective_order_ids` (pending_protective
only), `auto_close_broker_orphans` (writes NEW rows).

**Prime suspects, in order:**
1. `reconcile_journal_to_broker`'s fill-matching over rows with
   `status IN ('closed','pending_fill')` (~line 1554) — matches broker
   fills to journal rows and could stamp order_id + pnl onto the OPEN
   rows by occ/qty match when the real close was never journaled.
2. `journal.recompute_realized_pnl` — per-leg sign convention; verify the
   long-leg (buy-to-open, sell-to-close) direction against a pinned test.

**Definition of fixed:** (a) close fills NEVER mutate open rows' order_id
(closes get their own rows); (b) per-leg pnl sign test-pinned for BOTH leg
directions of a credit AND debit spread; (c) a regression test replaying
this exact incident shape (open pair pending_fill, later single-leg close
fills) ends with a zero decomposition gap.

---

## INCIDENT FOLLOW-UP 2026-07-23 — ✅ SUPERSEDED by the 2026-07-24 entry above

This entry opened the hunt for the anonymous entry-closer (p213 COST
#296) and named the reconciler's protective-fill handler as prime
suspect, pending a signed reason on the next occurrence.

**That suspect was REFUTED on 2026-07-24 by live broker forensics** —
#296's bracket children were CANCELED UNFILLED (filled_qty 0), and the
protective-fill path gates on `order.status=='filled'`, so it cannot
have fired. The real writer was `_task_update_fills`' FIFO lot-close
counting those still-RESTING children as completed exits. The child
cancellation was DOWNSTREAM of the wrong close, not its cause. Root
cause, fix, and the fleet-wide "bracket has NO live child" explanation
are in the ✅ RESOLVED 2026-07-24 section above; this stub is kept only
so the original suspect isn't re-investigated from scratch.
