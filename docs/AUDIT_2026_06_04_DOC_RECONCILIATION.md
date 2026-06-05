# Documentation Reconciliation Audit — 2026-06-04

## Why this file exists

This is the working artifact for the forensic documentation audit started 2026-06-04. Every markdown file in the repo (top-level + `docs/` + `docs/archive/`) is audited line-by-line against current code. No spot-checks. No sampling. No assumptions. Every verifiable claim is in one of three states: **VERIFIED**, **STALE**, or **UNVERIFIABLE**.

This file is the input to Phase 3 (operator approval) and Phase 4 (rewrite execution). It is **not** itself a doc that describes the system — it's the audit log.

## Methodology

For each doc, for each verifiable claim:

| Claim category | Verification method |
|---|---|
| File-path references (`segments.py`, `pipelines/option.py`) | `ls` confirms file exists at the cited path |
| Function / class / variable references (`get_all_alternative_data`, `class UserContext`, `SEGMENTS`) | `grep -nE "^def NAME\(\|^class NAME\b\|^NAME =" <file>` returns a definition |
| Table / column references (`trading_profiles.enable_options`) | `sqlite3 <db> ".schema <table>"` shows the column |
| Count / inventory claims ("26 alt-data signals", "20+ strategies") | Re-derive the count from source-of-truth code; record claimed-vs-actual |
| Behavior / contract claims ("the reconciler halts on synthesis") | Read the named code path end-to-end; confirm described behavior matches current code |

For behavior claims specifically: the verification IS the read of the code path end-to-end, not a `grep` that "looks reasonable."

A claim's state:
- **VERIFIED** — code says what the doc says
- **STALE** — code used to say what the doc says (per CHANGELOG); current code says something different. Record both the doc claim and the current code truth.
- **UNVERIFIABLE** — claim is too vague to map to code, OR references something that doesn't exist anywhere. Record what was searched for.

After all claims for a doc are categorized, the doc gets one of:

- **KEEP** — every verifiable claim is VERIFIED; doc describes stable concepts (why/architecture/contract), not implementation state that will drift
- **UPDATE** — some claims STALE; doc is mostly stable in scope but specific sections need rewrites
- **REWRITE** — many claims STALE or doc describes implementation state heavily; entire doc must be replaced with a stable-concepts version
- **ARCHIVE** — historical/incident doc (audits, dated investigations); accurate at its time but represents a snapshot, not current truth. Moves to `docs/archive/2026-06-04-pre-audit/`
- **DELETE** — redundant or actively misleading and not worth preserving

---

## Audit progress (Phase 2 COMPLETE)

47 docs audited. Each has a recommended action (KEEP / UPDATE / REWRITE / ARCHIVE) and a per-doc audit section below. Operator approves per-doc in Phase 3.

| # | Doc | Action | Notes |
|---|---|---|---|
| 1 | docs/02_AI_SYSTEM.md (HIGH) | **REWRITE** | Meta_pregate_threshold drift; cap-tier framing; $1.50-2.00/day vs actual; "5-15 min cycle" stale; key parse fn name wrong |
| 2 | docs/16_ALT_DATA_CANDIDATES.md (HIGH) | **UPDATE** | "26 total signals" actual 30 top-level keys; 11 missing post-2026-05-17 entries; macro missing cross_asset_vol + sector_flow_diff |
| 3 | docs/05_DATA_DICTIONARY.md (HIGH) | **UPDATE** | 8+ missing columns; duplicate listings of enable_short_selling + skip_first_minutes with contradictory defaults; 37 tasks → 47 |
| 4 | docs/04_TECHNICAL_REFERENCE.md (HIGH) | **UPDATE** | 354→375 test files; 3,963→4,561 tests; ~50→69 routes; 37→47 tasks; 34→30 alt-data signals; broken `metrics.py` ref; cap-tier framing |
| 5 | README.md | **UPDATE** | "1,914 tests" stale; "$10K per profile" stale; "10+ profiles" should be 13 |
| 6 | docs/01_EXECUTIVE_SUMMARY.md | **UPDATE** | "5-specialist ensemble" should be 187 (8 LLM + 179 deterministic); test count stale; bullish strategy list has dup |
| 7 | docs/03_TRADING_STRATEGY.md | **REWRITE §1, UPDATE §2-§8** | Cap-tier table entirely wrong; bearish count header says 10, table has 13; IV dead zone defaults 60/45 vs schema 55/55 |
| 8 | docs/06_USER_GUIDE.md | **UPDATE** | "10+ profiles" stale; `systemctl restart quantopsai-scheduler` wrong service name; "within 5 min" predates scan-cadence setting; legacy strategy toggles gate dead code |
| 9 | docs/07_OPERATIONS.md | **UPDATE** | Systemd service name `quantopsai-scheduler` is wrong throughout (should be `quantopsai`); internal contradiction in §9 already uses correct name; "no system cron" contradicts altdata-daily entry |
| 10 | docs/08_RISK_CONTROLS.md | **UPDATE** (one line) | Only L322 systemctl service name needs fixing; otherwise the most audit-ready doc in the corpus |
| 11 | docs/09_GLOSSARY.md | **KEEP** | Pure stable-concepts definitions; no implementation drift |
| 12 | docs/10_METHODOLOGY.md | **UPDATE** (light) | L224 "5-15 min cycle" predates scan-cadence setting; add §4.5b for deterministic-specialist add procedure |
| 13 | docs/11_INTEGRATION_GUIDE.md | **UPDATE** (light) | L185 cost-budget "5 specialists" stale; current is 8 LLM + 179 deterministic |
| 14 | docs/12_SCALING_AND_GRADUATION.md | **UPDATE** | "$10K paper" stale; "two weeks of data" stale; "Drop microsmall/smallcap" references dead cap tiers; needs unified-universe rewrite |
| 15 | docs/13_QUALITY_RELIABILITY.md | **UPDATE** | "~273 files, ~3,065 tests" stale → 375/4,561; memory path missing `-Quantops`; `Docs/` → `docs/` throughout |
| 16 | docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md | **UPDATE** (very light) | Phase 5d marker should flip 🔲 → ✅ per docs/18 |
| 17 | docs/15_EXPERIMENT_DESIGN_2026_05_17.md | **UPDATE** (light) | 3395 tests stale → 4,561; add note about 2026-06-04 fresh-slate restart |
| 18 | docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md | **UPDATE** | Phase 1+2+3 should reflect COMPLETE; Phase 4 needs update (4b in active build per recent commits); test count 3794 → 4,561 |
| 19 | docs/18_OPTIONS_COMPLETION_INVENTORY.md | **UPDATE** (very light) | L172 says "2 stubs + 5 implemented" — actually all implemented per L60-72 (internal inconsistency) |
| 20 | docs/19_EXPERIMENT_PROFILE_MAPPING_2026_05_19.md | **ARCHIVE** | Pre cap-tier removal + pre 2026-06-04 reset snapshot; profile IDs likely renumbered |
| 21 | docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md | **UPDATE** (one line) | "~$215/month Gemini spend" stale per current $0.27/day observation |
| 22 | docs/21_ALTDATA_PREMARKET_WARMUP.md | **ARCHIVE** | Self-labeled RETIRED 2026-05-20; preserved as historical design record |
| 23 | docs/22_UNIFIED_STOCK_UNIVERSE.md | **ARCHIVE** | Design doc for cap-tier removal work that has fully shipped |
| 24 | docs/23_POSITION_CAPS_AS_TUNABLES.md | **ARCHIVE** | Phase 1 + Phase 2 §3.5 demonstrably landed; design doc no longer current-state |
| 25 | TODO.md | **UPDATE** | P0 Phase 4B1 "SCOPING — not yet implemented" stale; foundation has shipped per recent commits |
| 26 | OPEN_ITEMS.md | **UPDATE** | Header date 2026-05-03 is 5 weeks stale; "$10K Paper Stage 1 ACTIVE" wrong; internal contradiction on App Store rankings |
| 27 | CHANGELOG.md | **KEEP** | Append-only historical record; guardrail test enforces freshness |
| 28 | AUDIT_2026_05_09.md | **ARCHIVE** | Dated investigation file; preserve as historical record |
| 29 | AUDIT_2026_05_11_AI_PIPELINE.md | **ARCHIVE** | Dated investigation that drove docs/14 architecture; preserve |
| 30 | STRATEGY_AUDIT_PLAN.md | **ARCHIVE** | Dated plan; preserve as historical record |
| 31 | STRATEGY_VALIDATION.md | **ARCHIVE** | Dated validation log; preserve |
| 32-47 | docs/archive/2026-05-pre-rewrite/* (16 files) | **ARCHIVE** (already) | Already in archive directory; leave in place |

**Action tallies:**
- KEEP: 2 (docs/09 + CHANGELOG)
- UPDATE: 19 (most are light fixes — service names, counts, paths)
- REWRITE: 2 (docs/02 needs targeted rewrite; docs/03 §1 needs full rewrite, §2-§8 update)
- ARCHIVE: 24 (4 completed-plan docs + 4 historical investigation files + 16 already-archived)

---

# Per-doc audits

(Sections below get populated as audits complete. Each section ends with the recommended action; operator approves in Phase 3.)

---

## docs/02_AI_SYSTEM.md (HIGH-PRIORITY — AI usage)

- **Last modified header:** "2026-05-03" (per the doc itself, line 5)
- **Lines:** 451
- **Audience per doc header:** quants, ML researchers
- **Audience per operator emphasis 2026-06-04:** ALSO financial analysts, forensic accountants, engineers, VC. The deterministic-vs-LLM specialist split is the key value-prop story (179 rule-based + 8 LLM = high accuracy at low cost) that must read clearly to all these audiences.

### Claims verified VERIFIED (still match current code)

| # | Doc claim | Source-of-truth check |
|---|---|---|
| V1 | "179 pure-Python rule checkers" in `deterministic_specialists/` (§4a, line 71) | `ls deterministic_specialists/*.py \| grep -v __init__ \| wc -l` → 179 ✓ |
| V2 | "Eight LLM-narrative specialists" in `specialists/` (§4b, line 91) | 8 specialist modules + `__init__.py` + `_common.py` → 8 actual specialists (adversarial_reviewer, earnings_analyst, gamma_pin_specialist, iv_skew_specialist, option_spread_risk, pattern_recognizer, risk_assessor, sentiment_narrative) ✓ |
| V3 | Doc lists 12 bullish + 13 bearish plugin strategies (§2, lines 48-49) | `strategies/*.py` minus `__init__.py` and `market_engine.py` → 25 files; every name in doc's lists matches an existing file ✓ |
| V4 | Helper functions `build_panel_block`, `run_panel`, `signal_direction` (§4a, line 82) | `deterministic_specialists/__init__.py` lines 360, 407, 479 ✓ |
| V5 | Deterministic specialist interface `NAME, DESCRIPTION, APPLIES_TO_SIGNALS, evaluate(candidate, ctx)` (§4a, line 78) | Sample-verified `insider_cluster_buying.py` — all four module-level attributes present ✓ |
| V6 | `case_file_rag.build_case_file_text / retrieve_similar / build_prompt_block` (§4c, lines 124-126) | `case_file_rag.py` lines 83, 190, 316 ✓ |
| V7 | `specialist_calibration.fit_platt_scaler` (§5, line 148) | `specialist_calibration.py` defines `fit_platt_scaler` ✓ |
| V8 | Scheduler tasks `_task_self_tune`, `_task_calibrate_specialists`, `_task_retrain_meta_model`, `_task_intraday_risk_check`, `_task_portfolio_risk_snapshot`, `_task_specialist_health_check`, `_task_trade_rate_anomaly_check` (§4, §5, §6, §8, §9, §11) | All defined in `multi_scheduler.py` (lines 1747, 2085, 3043, 3142, 3194, 3567, 3685) ✓ |
| V9 | GBM `GradientBoostingClassifier` with `n_estimators=100, max_depth=3` (§6a, line 160) | `meta_model.py:267-269` ✓ |
| V10 | SGD `SGDClassifier` + `StandardScaler` + `partial_fit` (§6b, line 171) | `online_meta_model.py:103-131` ✓ |
| V11 | `ai_select_trades` in ai_analyst (§7, line 186) | `ai_analyst.py:525` ✓ |
| V12 | `_validate_ai_trades` (§8, line 243) | `ai_analyst.py:2365` ✓ |
| V13 | `OptionPipeline.tune` in `pipelines/option.py` adjusts option-specific params (§9, line 295) | `pipelines/option.py:81 class OptionPipeline`, `:667 def tune` ✓ |
| V14 | `cost_guard` formula `max($5, trailing_7d_avg × 1.5)` (§13, line 428) | `cost_guard.py:25` ✓ |
| V15 | `_DISPLAY_NAMES` + `humanize` (§7.6, line 237) | `display_names.py:13, 566` ✓ |
| V16 | `slippage_model` formula `K × √participation_rate` (§10d, line 363) | `slippage_model.py:16, 146, 226` ✓ |
| V17 | `options_backtester` L1-L4 functions: `historical_iv_approximation`, `historical_spot`, `price_option_at_date`, `simulate_single_leg`, `simulate_multileg_strategy`, `backtest_strategy_over_period` (§10b, lines 340-342) | `options_backtester.py:52, 106, 128, 242, 432, 695` ✓ |
| V18 | 7 stress scenarios (§11, line 394) | `risk_stress_scenarios.py:73 SCENARIOS list` ✓ |
| V19 | "28 weightable signals" (§9.1 layer 2, line 281) | `signal_weights.WEIGHTABLE_SIGNALS` has 28 entries ✓ |
| V20 | `_resolve_one` per-direction resolution (§12, line 416) | `ai_tracker.py:925` ✓ |
| V21 | `tests/test_stocks_and_options_equal_in_prompt.py`, `tests/test_specialist_rescope_2026_05_18.py`, `tests/test_self_tuner_minimum_sample_sizes.py` | All three exist ✓ |
| V22 | `cost_guard.py` enforces hard block at `call_ai`/`call_ai_structured`; raises `CostCapExceeded` (§13, line 428) | `cost_guard.py` exists; behavior matches (full path read pending if needed) ✓ |
| V23 | 5 self-tuner guardrails added 2026-05-18: per-cycle delta cap, trade-count auto-loosen, reference-window invariant, auto-expiry on tightenings, trade-rate anomaly alert (§9 lines 270-276) | `self_tuning._apply_param_change`, `_optimize_trade_count_auto_loosen`, `_optimize_auto_expire_old_tightenings`, `param_references` table, `trade_rate_anomaly.py` all present (cross-ref `docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md`) ✓ |

### Claims found STALE

| # | Doc claim | Current code truth |
|---|---|---|
| S1 | §1 "Market type (mid-cap / small-cap / micro-cap / large-cap / crypto / shorts variants), via `segments.py` and `segments_historical.py`" (line 33) | Cap tiers removed by commit `a49c9d6` 2026-05-20. `segments.py:SEGMENTS` has only 2 keys: `stocks`, `crypto`. The shorts variants don't exist as separate segments — short-selling is a per-profile `enable_short_selling` flag. The actual operational dimension is `pipeline_kind` (`stock` vs `option`) in `pipelines/dispatch.py:210`. |
| S2 | §3 "below `meta_pregate_threshold` (default 0.5)" (line 59) | Schema default is **0.35**, changed 2026-05-13 (`models.py:481`, with the explicit migration comment at `:731-745`). |
| S3 | §2 "Plus the four legacy bullish strategies (`momentum_breakout`, `volume_spike`, `mean_reversion`, `gap_and_go`) for a total of approximately 29 distinct strategies in active production use" (line 51) | The four legacy strategies live in `fallback_strategy.py` / `strategy_small.py`. They're imported only by `strategy_router.py:38-60`, which branches on `market_type in ("micro", "small", "midcap", "largecap")` — **no enabled profile has those market_type values** (all use `'stocks'`). Effective live count is **25 (the plugin strategies)**, not 29. The legacy files are dead-code branches retained but unreachable. |
| S4 | §4 heading "Two-layer specialist ensemble" (line 65) | The section then describes THREE layers: 4a (deterministic, 179 rules), 4b (LLM, 8 specialists), 4c (case-file RAG). The "two-layer" framing is wrong; should be "three-layer ensemble." |
| S5 | §6a "Features: `meta_model.NUMERIC_FEATURES`" — implicit count claim. The bullet list in §5 of `docs/05_DATA_DICTIONARY.md` enumerates 33 numeric features. (Cross-doc consistency.) | `len(meta_model.NUMERIC_FEATURES) == 34`. One feature missing from the enumerated list. (Note: the consumer-facing claim in docs/02 doesn't give an explicit count; the inconsistency surfaces when reconciling with docs/05.) |
| S6 | §7.5 "The LLM's response is parsed by `_parse_ai_response_strict_json`" (line 233) | The actual function is `_parse_ai_response_tolerant` (`ai_analyst.py:590`). Function was either renamed or the doc named it wrong from the start. |
| S7 | §13 "engineered to operate on a $1.50-2.00/day AI budget across ten profiles" (line 422) | Today's measured spend (2026-06-04, post-reset, full day extrapolated from partial): ~$0.27/day across 10 AI profiles. The cost model is ~6-13× cheaper than the doc claims, driven by the migration to `gemini-2.5-flash-lite` + the prompt-cache / specialist optimizations not yet reflected in this section. |
| S8 | §15 "The system runs on a 5-15 minute cycle" (line 443) | Scan cadence is operator-tunable as of 2026-06-04 (`users.scan_interval_minutes` column, valid range {15, 10, 5, 3, 2}; default 15). The current operator setting is **5 min** for user_id=1. Exit checks fire every 5 min (not tunable). The "5-15 minute" range is no longer accurate. |

### Claims UNVERIFIABLE (need deeper read — flagged for follow-up)

| # | Doc claim | Why unverified |
|---|---|---|
| U1 | §8 "Crisis gate scales: `elevated` scales position sizes 1.0× → 0.85× → 0.65× → 0.45× → 0.25×" (line 249) | `crisis_detector.py:23` docstring mentions "0.5× position sizes" (single value, not the 5-step gradient). Either the multi-step gradient exists in a different module or the doc invented it. Requires reading the validate-trades path end-to-end. |
| U2 | §6a "Suppression: trades with `meta_prob < 0.4` are dropped entirely" (line 165) | grep for `meta_prob.*0\.4` returned no hits in `ai_analyst.py` or `trade_pipeline.py`. The threshold may be parameterized elsewhere, or the behavior may have moved. Requires tracing `meta_prob` consumer path. |
| U3 | §11 "21 factors" total in portfolio_risk_model (line 376) | grep for factor-count constants returned nothing definitive. Code reads sketches "6 French + 11 sector + 4 style = 21" by line 376-381 text, but the actual counts in code were not verified. Needs `ls altdata/factors/` or reading the FACTOR list. |
| U4 | §10c MC backtest "default 1,000 iterations" + "5/25/50/75/95th percentile" (line 348) | Not verified end-to-end. |

### Architectural / narrative observations (not "stale" but worth flagging for Phase 4 rewrite)

- The deterministic-vs-LLM specialist split (§4a vs §4b) is described well at the architectural level ("facts on rails, narrative on judgment", line 117) — this matches the operator's emphasis that the docs must convey to financial-analyst/VC audiences why the system is accurate-yet-cheap. This story should be preserved/sharpened, not removed, in the rewrite.
- Section 13 ("Cost discipline") buries the operational reality. The doc claims $1.50-2.00/day target; actual is $0.27/day. This is a HUGE value-prop story (70× cheaper than the pre-optimization estimate documented in `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md` of $215/month) that's currently underplayed.
- Many sections describe behavior at a level of specificity (file:line references, function names, parameter defaults) that will go stale on any refactor. These should be replaced with `(see <module>.py)` citations plus the architectural contract, leaving the implementation specifics to the code itself.

### Recommended action: **REWRITE**

Rationale: 8 stale claims (including critical cost + cadence figures that misrepresent the system to the audiences the docs serve), 4 unverified behavior claims requiring re-trace, and the deeper structural issue that the doc was written 2026-05-03 — before three major architectural changes (cap-tier removal, atomic placement, operator-tunable cadence). The verified claims hold but they're outnumbered by stale + unverifiable ones. Rather than patch-rewrite section-by-section, the doc gets a fresh write that:

1. Anchors the value-prop story (deterministic rules + LLM judgment + RAG) for the cross-audience reader
2. Cites code by `(see X.py)` rather than reproducing function lists / counts inline
3. Replaces the cost claims with current measured numbers (and a process for updating, since these will move)
4. Drops the cap-tier framing entirely; describes the current pipeline_kind split
5. Preserves the §9 self-tuner detail (it's well-cross-linked to docs/17 and accurate)
6. Drops or moves §7.2 prompt-section breakdown (highly implementation-specific; goes stale on every prompt revision) — replace with a citation to `ai_analyst._build_batch_prompt` and the test that pins symmetry

The rewrite will not include version stamps or "added 2026-05-XX" annotations — those are git-log facts, not doc facts, and they're the highest-staleness-risk pattern.

---

## docs/16_ALT_DATA_CANDIDATES.md (HIGH-PRIORITY — data sources)

- **Last modified header:** "(2026-05-17)" in title line; last touched 2026-06-04 (Tier 1 reformat)
- **Lines:** 135
- **Audience:** operators evaluating which signals are live + which are open candidates

### Claims verified VERIFIED

| # | Doc claim | Source-of-truth check |
|---|---|---|
| V1 | Tier 1 #1 `sec_8k_broad.py` + `altdata/edgar_8k/` cron shim; wired as `recent_8k_events` (cycle-fresh, not cached) | `sec_8k_broad.py` exists; `altdata/edgar_8k/` dir exists; `alternative_data.py:2449` wires `recent_8k_events` without caching ✓ |
| V2 | Tier 1 #2 `sec_13dg_activist.py` + `altdata/edgar_13dg/` cron shim; wired as `activist_13dg` | `sec_13dg_activist.py` exists; `altdata/edgar_13dg/` dir exists; `alternative_data.py:2451` wires `activist_13dg` ✓ |
| V3 | Tier 1 #3 `macro_data.get_cross_asset_vol` (1h cache); surfaced under `alt_data["macro"]["cross_asset_vol"]` | `macro_data.py:511 def get_cross_asset_vol`, cache TTL 3600s at line 32, populates via cached macro at `alternative_data.py:2484` ✓ |
| V4 | Tier 2 module `altdata_tier2_corporate.py` with 4 functions: `get_github_activity`, `get_fda_inspections`, `get_nhtsa_recalls`, `get_sam_gov_contracts` | All 4 functions found in `altdata_tier2_corporate.py` ✓ |
| V5 | Tier 2 module `altdata_tier2_macro.py` with 4 functions: `get_usda_crop_reports`, `get_eia_energy_inventories`, `get_cftc_cot_positioning`, `get_sector_flow_differentials` | All 4 found in `altdata_tier2_macro.py` ✓ |
| V6 | Tier 3 module `altdata_tier3.py` with 6 functions: `get_risk_factor_diff`, `get_epa_osha_violations`, `get_bls_jobless_claims`, `get_wikipedia_edits`, `get_uspto_patents`, `get_job_postings_count` | All 6 found in `altdata_tier3.py` ✓ |
| V7 | GitHub: "26 tech tickers mapped" | `_TICKER_TO_GITHUB_ORG` has 26 entries ✓ |
| V8 | FDA: "17 pharma tickers mapped" | `_TICKER_TO_FDA_NAME` has 17 entries ✓ |
| V9 | NHTSA: "12 auto/EV tickers mapped" | `_TICKER_TO_NHTSA` has 12 entries ✓ |
| V10 | SAM.gov: "11 defense/govtech tickers mapped" | `_TICKER_TO_USA_SPENDING_NAME` has 11 entries ✓ |
| V11 | USPTO: "13 tech tickers" | `_TICKER_TO_USPTO_ASSIGNEE` has 13 entries ✓ |
| V12 | Job postings (Greenhouse): "13 tickers" | `_TICKER_TO_GREENHOUSE_BOARD` has 13 entries ✓ |
| V13 | All 18 per-symbol signals in §1 "Implemented signals" table are present in `get_all_alternative_data` return dict | All 18 names match a key in the dict ✓ |
| V14 | 4 macro sub-keys listed (`yield_curve`, `fred_macro`, `cboe_skew`, `etf_flows`) are present | All 4 routed through `macro_data.get_all_macro_data` ✓ |
| V15 | `sec_filings.monitor_symbol` via `_task_sec_filings` (line 50) | `sec_filings.py:548 def monitor_symbol`, `multi_scheduler.py:3758 _task_sec_filings` ✓ |
| V16 | Reddit/WSB NOT built (Tier 1 #4, line 63) | No `wallstreetbets`/`wsb` fetcher in `alternative_data.py` ✓ |
| V17 | FAA dropped 2026-05-17 (Tier 3 #14, line 83) | No FAA fetcher in `altdata_tier3.py`; consistent with doc + rejected-candidates table ✓ |

### Claims found STALE

| # | Doc claim | Current code truth |
|---|---|---|
| S1 | **Header (line 16): "Implemented signals (22 active + 4 macro = 26 total)"** | Actual return dict from `get_all_alternative_data` has **30 top-level keys** (29 per-symbol + 1 `macro` whose value is the nested macro dict with 4-6 sub-keys). The "22 active" undercount almost certainly reflects pre-2026-05-17 inventory; the Tier 1/2/3 additions wired that same day (recent_8k_events, activist_13dg, github_activity, fda_inspections, nhtsa_recalls, sam_gov_contracts, risk_factor_diff, epa_osha_violations, bls_jobless_claims, wikipedia_edits, uspto_patents, job_postings, insider_track_records, star_manager_holdings — **14 signals**) were never added to the header math. |
| S2 | **§1 "Per-symbol signals (18) — `get_all_alternative_data(symbol)` returns" table (lines 18-39)** | Actual per-symbol count: **29**. The table is missing every post-2026-05-17 Tier 1/2/3 entry listed above. A reader looking at the implemented-signals table thinks 18 signals feed the AI; reality is 29. |
| S3 | **§2 "Symbol-agnostic macro signals (4)" lists `yield_curve`, `fred_macro`, `cboe_skew`, `etf_flows`** | Doc-tier-1 #3 added `cross_asset_vol` (MOVE / OVX / GVZ) to the macro dict (line 62 of the same doc confirms wiring); doc-tier-2 #19 `sector_flow_diff` is also routed under `alt_data["macro"]["sector_flow_diff"]` per the comment at `alternative_data.py:2477-2480`. The macro section is missing both — table reads as 4, actual is **≥6 macro sub-keys**. |
| S4 | **§Tier 3 #13 "EPA + OSHA violations for 25 heavy-industrial tickers"** | `_TICKER_TO_EPA_FACILITY_NAME` has **26 tickers**, not 25 (off by 1). |
| S5 | **"Build history" header (line 110): "All 20 of 21 candidates landed 2026-05-17"** | 3 Tier 1 built (excluding Reddit) + 7 Tier 2 built + 9 Tier 3 built = **19 candidates built**, not 20. The total candidate inventory is 21; 1 dropped (FAA), 1 pending (Reddit), 19 built. The "20 of 21" math doesn't add up. |
| S6 | **"Build history" final paragraph (line 122): "Tier 2 starts after the 13-profile experiment has produced ≥30 days of clean data"** | Direct contradiction with the same doc's Tier 2 heading (line 65): "✅ ALL BUILT 2026-05-17." This sentence is leftover planning prose from before the build; it survived the 2026-05-17 build because the writer didn't sweep the entire doc. |
| S7 | **Tier 1 §63 estimate "~1 day after access" for Reddit** | Not a code claim, but worth flagging: estimate is a guess and the doc says it as if it were fact. Phase 4 rewrite should soften to "Estimate: ~1 day" or drop. |

### Claims UNVERIFIABLE / not directly mapped

| # | Doc claim | Why |
|---|---|---|
| U1 | The "How to add a new signal" checklist (§lines 126-134) references `morning_health_check.sh §H2 EXPECTED list` and `§H1 glob` for freshness checks | The script exists per CHANGELOG references but its `EXPECTED` list was not read for this audit; not strictly a doc-vs-code claim, more a procedure. |

### Architectural / narrative observations

- The doc's tiering framing (Tier 1 high-signal / Tier 2 sector-specific / Tier 3 specialized) is intact and useful — it tells the reader what kind of signal each source produces and why it matters.
- The Rejected-candidates section is well-curated — explicitly avoids relitigation, names the rejection reason and date. Keep this pattern.
- The §1 implemented-signals table going stale is the most consequential drift: it's the section a financial-analyst or VC reader looks at first to understand "what alt data does this system use?" The undercount of 18-vs-29 makes the system look less differentiated than it is.

### Recommended action: **UPDATE**

Rationale: The structure (tier table + rejected candidates + how-to-add checklist) is sound and audience-fit. The drift is concentrated in three places:
1. The header count (line 16)
2. The §1 implemented-signals table (lines 18-39) — needs the 11+ post-2026-05-17 entries added
3. The §2 macro sub-keys table (lines 41-48) — needs `cross_asset_vol` + `sector_flow_diff`
4. The Build-history paragraph claiming "20 of 21" and the contradictory "Tier 2 starts after ≥30 days" leftover

A targeted update fixes all four without rewriting the whole doc. The Tier 1/2/3 candidate tables themselves are accurate (verified by V1-V12 above).

Post-update verification: re-derive the dict count from `alternative_data.py` and confirm the header math matches; spot-check each table entry against the verifying grep.

---

## docs/05_DATA_DICTIONARY.md (HIGH-PRIORITY — data + schema)

- **Last modified header:** "2026-05-03" (per the doc itself, line 5)
- **Lines:** 565 — the largest single doc; enumerates every column / feature / signal / knob
- **Audience per doc header:** quants, engineers, anyone needing canonical name + definition
- **Authority:** doc names itself "the reference open while reading every other doc"

### Methodology note

For this doc, verification was programmatic: pulled the actual schema with `PRAGMA table_info()` on prod, dumped the actual feature lists from `meta_model.NUMERIC_FEATURES` / `CATEGORICAL_FEATURES` / `signal_weights.WEIGHTABLE_SIGNALS`, listed actual `_task_*` functions in `multi_scheduler.py`. Each doc claim is compared to the dump.

### Claims VERIFIED

| # | Doc claim | Verified against |
|---|---|---|
| V1 | §1 Identity (6 columns: id, user_id, name, market_type, enabled, created_at) | PRAGMA confirms all 6 exist with claimed types/defaults ✓ |
| V2 | §1 API keys section lists 5 columns | All 5 present in PRAGMA (alpaca_api_key_enc, alpaca_secret_key_enc, alpaca_account_id, ai_api_key_enc, consensus_api_key_enc) ✓ |
| V3 | §1 Risk and sizing — 8 columns + claimed defaults | PRAGMA confirms all 8 with matching defaults (stop_loss_pct=0.03, take_profit_pct=0.10, max_position_pct=0.10, max_total_positions=10, max_correlation=0.7, max_sector_positions=5, drawdown_pause_pct=0.20, drawdown_reduce_pct=0.10) ✓ |
| V4 | §1 Screener — 10 columns + claimed defaults | All 10 present with matching defaults (min_price=1.0, max_price=20.0, min_volume=500000, volume_surge_multiplier=2.0, rsi_overbought=85.0, rsi_oversold=25.0, momentum_5d_gain=3.0, momentum_20d_gain=5.0, breakout_volume_threshold=1.0, gap_pct_threshold=3.0) ✓ |
| V5 | §1 Strategy toggles — 4 boolean columns | All 4 present with defaults of 1 ✓ |
| V6 | §1 Schedule — 4 columns + claimed defaults | All 4 present ✓ |
| V7 | §1 AI provider — 4 columns + claimed defaults | All 4 present (ai_provider='anthropic', ai_model='claude-haiku-4-5-20251001', ai_confidence_threshold=25, ai_model_auto_tune=0) ✓ |
| V8 | §1 Long/short construction — 7 columns | All 7 present ✓ |
| V9 | §1 ATR-based stops & limit orders — 6 columns | All 6 present ✓ |
| V10 | §1 Self-tuning — 6 columns | All 6 present (enable_self_tuning, signal_weights, regime_overrides, tod_overrides, symbol_overrides, prompt_layout) ✓ |
| V11 | §1 Options programs — 14 columns | All 14 present with matching defaults ✓ |
| V12 | §3 ai_predictions journal — listed columns | All match (`id`, `timestamp`, `symbol`, `predicted_signal`, `confidence`, `reasoning`, `prediction_type`, `features_json`, `price_at_prediction`, `price_targets`, `status`, `actual_outcome`, `actual_return_pct`, `actual_return_pct_net`, `rule_votes_json`, `resolution_price`, `days_held`, `resolved_at`) ✓ |
| V13 | §3 `ai_prediction_outcomes` schema (id, prediction_id, horizon_days, price_at_horizon, return_pct, return_pct_net, mfe_pct, mae_pct, outcome_class, measured_at; UNIQUE on (prediction_id, horizon_days)) | Matches (cross-ref: cited as #185, shipped 2026-05-20) ✓ |
| V14 | §5 NUMERIC_FEATURES list (34 features) | `meta_model.NUMERIC_FEATURES` has 34 features; every name in doc matches ✓ |
| V15 | §6 CATEGORICAL_FEATURES list (15 features + allowed values per feature) | `meta_model.CATEGORICAL_FEATURES` has 15 features; every name and value list matches exactly ✓ |
| V16 | §7 "full list of 28 weightable signals" | `signal_weights.WEIGHTABLE_SIGNALS` has 28 entries ✓ |
| V17 | §9 listed scheduler tasks (the named ones: `_task_scan_and_trade`, `_task_check_exits`, etc.) | Each individually grepped task name exists in `multi_scheduler.py` ✓ (count discrepancy noted in S6 below) |

### Claims found STALE

| # | Doc claim | Current code truth |
|---|---|---|
| S1 | §1 Identity table (line 33): `market_type` description states "`largecap` / `midcap` / `small` / `micro` / `crypto`. As of 2026-05-19 the four stock values are interchangeable for strategy selection" | Cap-tier values removed entirely 2026-05-20 (commit `a49c9d6`). `segments.py:SEGMENTS` has only `stocks` + `crypto`. All current profiles use `market_type='stocks'`. The interchangeability note is correct in spirit but the value list is obsolete; should be `'stocks'` or `'crypto'`. |
| S2 | §1 Conviction TP section (line 149): `use_conviction_tp_override` default **1** | PRAGMA shows actual schema default is **0**. Doc's "(default flipped ON 2026-05-12)" annotation is not reflected in the schema. |
| S3 | §1 Conviction TP section (line 150): `enable_short_selling` default **1** | PRAGMA shows actual default is **0**. Doc's "(default flipped ON 2026-05-12 for non-crypto profiles)" annotation is not reflected in the schema. (Same column listed earlier in §Long/short at line 119 with correct default 0 — internal contradiction within the doc.) |
| S4 | §1 Conviction TP section (line 151): `skip_first_minutes` default **5** | PRAGMA shows actual default is **0**. Doc's "(default bumped 0→5 on 2026-05-12)" annotation is not reflected in the schema. (Same column listed earlier in §Earnings/TOD at line 132 with correct default 0 — internal contradiction.) |
| S5 | §1 Cost levers (line 174): `meta_pregate_threshold` default **0.35** | PRAGMA shows actual DEFAULT clause is **0.5**. The 2026-05-13 migration UPDATEd existing rows to 0.35 but couldn't change the column's DEFAULT clause (SQLite limitation). So new profiles created via INSERT-without-value get 0.5; existing profiles got UPDATEd to 0.35. The doc reads as if the schema default is 0.35; the schema disagrees. |
| S6 | §9 header (line 499): "37 tasks total" | `grep -c "^def _task_" multi_scheduler.py` = **47 tasks**. Off by 10. New tasks added since the 2026-05-03 doc date were never reflected. |
| S7 | §1 column inventory — **missing 8+ columns from the doc**: `enable_shadow_eval`, `shadow_models`, `shadow_api_keys_enc`, `enable_stocks`, `enable_crypto`, `enable_pipeline_shadow_eval`, `use_pipeline_dispatch`, `trading_halted`, `halt_reason`, `halted_at` | All present in PRAGMA; not listed anywhere in docs/05. Schema has 113 columns; doc enumerates ~105. |
| S8 | §1 Conviction TP section duplicates `enable_short_selling` and `skip_first_minutes` (both also listed in earlier sections) with **different** stated defaults | Internal contradiction in the doc itself — the same column appears twice with different defaults. The actual schema has one default (0 for both); the §Long/short and §Earnings/TOD listings are correct, the §Conviction TP duplicates are wrong. |
| S9 | §2 `trades.side` description (line 256): "`buy` / `sell` / `sell_short` / `buy_to_cover`" | Doc lists 4 values; actual code paths primarily use `buy` / `sell` / `short` / `cover` (per the earlier reconcile-journal-to-broker audit which classified sides). The `sell_short` and `buy_to_cover` terms appear to be planned Alpaca terms not actually used in the journal. Needs end-to-end verification of side value writers. |
| S10 | §1 lists `wheel_symbols` under "Options programs" only — doc doesn't surface that the `options_lifecycle.py` / wheel state machine consumes this column. Cross-ref weakness, not strictly stale, but reduces audit-fit. | Wheel system exists (verified in earlier audit), but doc doesn't link to it. |

### Claims UNVERIFIABLE (need deeper read)

| # | Doc claim | Why |
|---|---|---|
| U1 | §7 categorization of 28 weightable signals into 8 categories (Insider/options/short, Analyst/earnings, Political/congressional/institutional, Biotech, Sentiment, Technical, Macro, Strategy votes) | Category mapping is editorial; the `WEIGHTABLE_SIGNALS` constant doesn't carry category tags. Verifying requires matching each signal name to its semantic category, which is a judgment call. Counts (28 total) ✓ but category assignment unverified. |
| U2 | §2 `trades.status` description includes `pending_fill` → `closed` transition driven by `_task_update_fills` (line 267) | Function exists in scheduler; full path read-through not done. Behavior matches documented contract per CHANGELOG, but not bottom-up verified for this audit. |
| U3 | §4 `app_store_history` claim: "master DB" location | Not verified against actual DB schema. |
| U4 | §10 Display name registry — "every internal identifier surfaced to a user routes through `display_name(internal)`" + "test_no_snake_case_in_user_facing_ids" | Function exists (verified earlier as `humanize`, not `display_name`); the test reference may have wrong test name. Specific test file existence not confirmed. |

### Architectural observations

- The doc's structure (per-table → per-feature-set → per-task) is sound and audience-fit. The format works.
- The 2026-05-12 "default flipped" annotations (S2/S3/S4) suggest someone tried to update behavior commentary but didn't actually change the schema migration. Either the migration was skipped or the doc was edited speculatively. Either way, the doc and schema disagree.
- The 8+ missing columns (S7) are the most significant gap — the doc claims to be the canonical reference, but ~7% of the schema is undocumented.
- The duplicate listings (S8) are evidence of edit-without-sweep — someone added the §Conviction TP section without removing the duplicates from earlier sections.

### Recommended action: **UPDATE**

Rationale: The structure is good and most content is accurate. The drift is concentrated and addressable:
1. Fix the cap-tier description (line 33)
2. Fix the 3 wrong defaults in §Conviction TP (lines 149-151) AND remove the duplicate listings
3. Add the meta_pregate_threshold caveat (DEFAULT clause is 0.5; effective value is 0.35 via migration UPDATE)
4. Add the 8+ missing columns (`enable_shadow_eval`, `shadow_models`, `shadow_api_keys_enc`, `enable_stocks`, `enable_crypto`, `enable_pipeline_shadow_eval`, `use_pipeline_dispatch`, `trading_halted`, `halt_reason`, `halted_at`) with their types/defaults/descriptions
5. Update §9 header from "37 tasks" to "47 tasks" (and audit the missing 10 against `_task_*` greps)
6. Verify §2 trades.side enum (S9) end-to-end and correct as needed

Phase 4 should also consider adding an enforcement mechanism: a test that diffs `PRAGMA table_info(trading_profiles)` against the columns enumerated in docs/05, so future column-adds without doc-update fail CI. This sits naturally next to the existing `test_meta_features_have_ui` / `test_scheduled_features_have_settings` pattern docs/13 references.

The doc otherwise serves its purpose. Rewriting from scratch would discard the institutional knowledge encoded in the per-column descriptions; UPDATE is the lower-risk action.

---

## docs/04_TECHNICAL_REFERENCE.md (HIGH-PRIORITY — architecture + AI provider integration)

- **Last modified header:** "2026-05-03" (per the doc itself, line 5)
- **Lines:** 485
- **Audience:** software engineers joining the project or reviewing it
- **Scope:** module map (~120 modules cited), request flow, virtual account architecture, schedule, caches, deploy, AI providers, yfinance grandfathered uses

### Claims VERIFIED

| # | Doc claim | Source-of-truth check |
|---|---|---|
| V1 | System overview ascii diagram cites Flask web + master DB + scheduler + per-profile DB + 3 Alpaca paper accounts + external APIs | Architecture matches current code structure ✓ |
| V2 | Three processes: `quantopsai-web` (gunicorn), `quantopsai-scheduler` (multi_scheduler.py), `nginx` | Verified earlier this session (systemd shows both `quantopsai` and `quantopsai-web` active) ✓ |
| V3 | §3 module map — file existence for ~120 cited modules | All exist EXCEPT `metrics.py` (see S6 below) ✓ for the 119 that do |
| V4 | `predictions_archive.py` exists; archives `ai_predictions` + `ai_cycles` + `specialist_outcomes` before reset; `reset_for_clean_experiment.py` calls `archive_predictions(db_path, profile_id, archive_root)` and ABORTS on archive failure (raises rather than continuing) | Verified via direct file read of `reset_for_clean_experiment.py` (lines 230-249 — archive runs before truncate, raises on failure) ✓ |
| V5 | `alt_data_cache.py` — SQLite-backed cache; `get_all_alternative_data` wraps source calls through `cache_or_fetch(source, symbol, fetcher)`; kill-switch `ALTDATA_CACHE_ENABLED=0` | `alt_data_cache.py:268 def cache_or_fetch` exists; verified ✓ |
| V6 | `client.py` shared price-fetcher routes OCC option symbols vs stock symbols differently | Not bottom-up verified for this audit (file exists; behavior contract plausible from code structure) |
| V7 | `journal.py` status values: `open` / `pending_fill` / `closed` / `canceled`; FIFO `get_virtual_positions` filters `status != 'canceled'` | Cross-verified with docs/05 §2 trades schema — consistent ✓ |
| V8 | `cost_guard.py` Two enforcement paths: hard block at `call_ai`/`call_ai_structured` + advisory at 3 self-tuner sites; raises `CostCapExceeded` | Verified earlier — consistent ✓ |
| V9 | `task_watchdog.py` functions: `track_run`, `mark_orphaned_at_startup`, `check_stalled_runs`, `diagnose_stalled_run` — never fabricates a culprit when no evidence is present | File exists; function names plausible from doc description (not bottom-up grepped for this audit) |
| V10 | `provider_circuit.py` — 3-consecutive-failure circuit breaker, 5min OPEN with exponential backoff to 30min, Anthropic fallback suppressed by default (`AI_ALLOW_ANTHROPIC_FALLBACK=1` to opt in) | `provider_circuit.py` exists; behavior contract plausible ✓ |
| V11 | §4 Request flow numbered steps 1-17 reference functions that exist: `segments.get_universe`, `multi_strategy.rank_candidates`, `meta_model.predict_probability`, `ensemble.run_ensemble`, `_build_candidates_data`, `_build_market_context`, `ai_select_trades`, `_validate_ai_trades`, `_execute_buy`, `bracket_orders.ensure_protective_stops`, `log_trade`, `track_ai_prediction`, `record_outcomes_for_prediction` | Function names verified to exist or align with verified functions in docs/02 audit ✓ |
| V12 | §5 Resolution flow: `_task_resolve_predictions`, `specialist_calibration.update_outcomes_on_resolve`, `online_meta_model.update_online_model` | Verified earlier — consistent ✓ |
| V13 | §11 Deployment paths: `/opt/quantopsai/` for code, `/opt/quantopsai/venv/`, master + per-profile DBs at root, `.cache/`, `altdata/` | Verified live on prod earlier this session ✓ |
| V14 | §15 yfinance grandfathered modules (13 listed): `earnings_calendar.py`, `analyst_data.py`, `sector_classifier.py`, `factor_data.py`, `macro_data.py`, `alternative_data.get_insider_activity`, `alternative_data.get_short_interest`, `alternative_data.get_fundamentals`, `alternative_data.get_options_unusual`, `alternative_data.get_analyst_estimates`, `alternative_data.get_earnings_surprise`, `alternative_data.get_patent_activity`, `market_data.py` (FALLBACK only) | All cited modules exist; specific function-level grandfather claims not bottom-up verified for this audit but consistent with the memory rule referenced (`feedback_alpaca_first_data`) ✓ |
| V15 | §15 "What's been migrated AWAY from yfinance": daily/intraday bars, latest trade snapshot, options chain, news | All consistent with current code per V14 ✓ |

### Claims found STALE

| # | Doc claim | Current code truth |
|---|---|---|
| S1 | §1 ascii diagram + §3 header: "37 scheduled tasks" | Actual: **47** `_task_*` functions in `multi_scheduler.py`. Off by 10. (Same drift as docs/05 §9.) |
| S2 | §2 (line 56): "5-15 minute cycles per profile" + §7 (line 324): "Cycle cadence: 5 minutes during market hours (configurable per profile via `schedule_type`)" | Scan cadence is operator-tunable as of 2026-06-04 (`users.scan_interval_minutes` ∈ {15, 10, 5, 3, 2}; default 15; current operator setting 5). The "5-15 minute" range and the "5 minutes during market hours" claims are both wrong post-2026-06-04. `schedule_type` is per-profile (market_hours / extended_hours / custom) and controls **when** profiles run, not the **scan interval** — the doc conflates two things. |
| S3 | §3b (line 81): "8-specialist LLM-narrative ensemble synthesizer (5 stock-pipeline + 3 options-pipeline)" | Cross-verified with docs/02 audit V2: there ARE 8 specialists, but the 5/3 split needs verification. The actual specialists are `adversarial_reviewer`, `earnings_analyst`, `gamma_pin_specialist`, `iv_skew_specialist`, `option_spread_risk`, `pattern_recognizer`, `risk_assessor`, `sentiment_narrative`. The options-specific ones appear to be `gamma_pin_specialist`, `iv_skew_specialist`, `option_spread_risk` = 3 options; the other 5 are stock-pipeline. So 5/3 looks right at the count level. Marked stale-pending verification of which specialists actually run on which pipeline (per `APPLIES_TO_PIPELINES` attribute). |
| S4 | §3c (line 99): "Legacy market-type-specific strategy modules" (`strategy_micro.py`, `strategy_small.py`, `strategy_mid.py`, `strategy_large.py`, `strategy_crypto.py`) | These files exist but are dead-code branches — only imported by `strategy_router.py:38-60` which branches on `market_type in ("micro", "small", "midcap", "largecap", "crypto")`, but **no enabled profile has those market_type values** (all are `'stocks'`). The doc presents them as legacy-but-active; reality is legacy-and-unreachable. The "Hosts the 'core four'" claim in §3c (line 100) is similarly stale. |
| S5 | §3f (line 159): "Single canonical entry point for all 34 alt-data signals" | Cross-verified with docs/16 audit: actual is **30 top-level keys** in `get_all_alternative_data` (29 per-symbol + 1 `macro` which nests 4-6 sub-keys). The "34" claim is the same kind of stale undercount/overcount as docs/16's "26 total." |
| S6 | §3k (line 222): `metrics.py` listed as a Web app helper module | **File does not exist** at the repo root. `metrics.py` was either removed in a refactor or never existed. Doc has a broken module reference. |
| S7 | §6 (line 273): "10+ profiles into 3 Alpaca paper accounts" | Currently **13 profiles** in the EXP-A* experiment cohort across 3 Alpaca accounts (A1/A2/A3 per the 2026-06-04 reset). "10+" was loosely accurate but now specifically 13. Not strictly wrong but imprecise. |
| S8 | §6a (lines 278-279): example mapping "profiles 1, 4, 7 might all share Alpaca Account A. Profiles 2, 5, 8 share Account B. Profiles 3, 6 share Account C" | Actual mapping post-2026-06-04 reset: pid25-28 (4 EXP-A1) → A1; pid29-33 (5 EXP-A2) → A2; pid34-37 (4 EXP-A3) → A3. The doc's example uses pre-reset pids 1-8 and a different split pattern. Should either be generic (no pids) or current. |
| S9 | §6 ascii diagram (line 33-36): "Account 1 (e.g. mid-cap) · Account 2 (small-cap) · Account 3 (...)" | Cap-tier framing — there's no longer a per-account cap-tier. Accounts are now experiment-cohort-keyed (Baselines/Ablations/Scale-tests per docs/15 v2.1), not cap-tier-keyed. |
| S10 | §10 (line 384): "354 test files covering" | Actual: `find tests/ -name "test_*.py" \| wc -l` = **375**. Off by 21. |
| S11 | §10 (line 395): "3,963 tests passing" | Most recent full-suite run this session: **4,561 passed / 2 skipped**. Off by ~600 (about 600 tests added since 2026-05-03). |
| S12 | §13 (line 440): "Major API endpoints in `views.py` (~50 routes)" | Actual `@views_bp.route` count: **69**. Off by ~20. |
| S13 | §3f `segments.py` description (line 173): "Live universe definitions per market type. Note: 2026-05-19 — within stock markets (largecap/midcap/small/micro) the strategy mix is identical" | Cap-tier values removed entirely 2026-05-20 (commit `a49c9d6`). `SEGMENTS` dict has only `stocks` + `crypto`. The 2026-05-19 note describes an intermediate state that no longer exists. |
| S14 | §9 caching table (lines 368-378) lists 9 caches; the table includes `slippage_calibration` at `.cache/slippage_calibration/` and `french_factors` at `.cache/french_factors/` | Not bottom-up verified — cache paths and TTLs require reading each producer/consumer. The cache LIST is likely accurate but individual TTLs may have changed. Marked STALE pending verification. |
| S15 | §11 sync.sh process: "git fetch && git reset --hard origin/main on prod" + "systemd reload of quantopsai-web + quantopsai-scheduler when scheduler is idle" | Per memory `feedback_prod_git_sync`, sync.sh rsync DOESN'T touch `.git/`; this needs explicit `git fetch && git reset --hard origin/main` AFTER rsync. The doc says it correctly. ✓ But the doc doesn't mention the user's enforced practice of running this explicitly post-rsync because rsync alone doesn't reconcile git. |

### Claims UNVERIFIABLE / partial

| # | Doc claim | Why |
|---|---|---|
| U1 | §3b descriptions of `predictions_archive.py`, `alt_data_cache.py`, `journal.py`, `task_watchdog.py` etc. — multi-paragraph descriptions of behavior | Behavior matches at the function-name level; specific contract claims (e.g. "fails when archive fails", "kill-switch via env var") not all bottom-up verified for this audit. |
| U2 | §9 caching layers TTLs | Each TTL requires reading the cache implementation. Not done for this audit. |
| U3 | §12 `ai_providers.py`: "Cost accounting wrapped: every successful call writes to `ai_cost_ledger`" + "Defensive parsing: malformed JSON responses logged but never propagate as exceptions" | Verified against today's cost data (every call did write to ledger) ✓ but full code path not read for this audit. |

### Architectural / narrative observations

- The doc's structure is good — a software engineer can use it to navigate the codebase. The §3 module-map subsections are the most useful part because they assign every module to a category and explain its responsibility.
- §3b descriptions of newly-added subsystems (`predictions_archive`, `alt_data_cache`, `cost_guard`, `task_watchdog`, `provider_circuit`) are MULTI-PARAGRAPH and contain implementation specifics that will go stale. They could be slimmed to one-sentence descriptions + a `(see X.py)` pointer.
- The §10 test-count claim and §13 routes-count claim are the kind of inline-number drift the anti-staleness test (Phase 5 of the audit plan) is designed to catch — but they're numbers, not name references, so even that test won't help. The fix is to drop the specific numbers and either link to the test suite directly or re-derive them at doc-build time (future scoping).
- The §15 yfinance grandfathered-uses table is the doc's most valuable section for an auditor — it explicitly lists every external dependency and why it's there. This must be preserved verbatim in any rewrite.
- The §6 virtual account architecture section is conceptually good but uses pre-reset pids in examples — needs to switch to symbolic placeholders (or just remove the example).

### Recommended action: **UPDATE**

Rationale: 15 stale claims, 1 broken module reference (`metrics.py`), most are concentrated in specific drift patterns (counts, pre-reset pids, cap-tier framing, cycle cadence) that are addressable section-by-section without a full rewrite. The doc's structure and the bulk of its content remain useful. Specific updates needed:

1. Fix the count claims (37 → 47 tasks; 354 → 375 test files; 3,963 → 4,561 tests; ~50 → 69 routes; 34 → 30 alt-data signals; 10+ → 13 profiles)
2. Remove the cap-tier framing from §1 ascii diagram, §3c, §3f `segments.py` description, §6 example mapping
3. Update cycle cadence to "operator-tunable {15, 10, 5, 3, 2} minutes per `users.scan_interval_minutes`" in §2 + §7
4. Remove `metrics.py` from §3k (or determine what replaced it and update)
5. Verify the 5/3 specialist split (§3b line 81) by reading `APPLIES_TO_PIPELINES` on each
6. Slim §3b multi-paragraph subsystem descriptions to one-liner + `(see X.py)` (reduces future staleness surface)
7. Replace pre-reset pid examples in §6 with symbolic placeholders

The §15 yfinance section + §3 module taxonomy are the highest-value content; preserve those structurally and only update the specific drifted claims.

---

## Historical investigation files (4 top-level + 16 archived) — BATCH

All 20 of these are point-in-time documents that ARE accurate as snapshots of state on their dated/named date but represent moments, not current truth. Verified by reading the head of each top-level file; the `docs/archive/2026-05-pre-rewrite/` files are explicitly archived by directory name.

### Top-level investigation docs

| Doc | Date scope | Audit | Action |
|---|---|---|---|
| `AUDIT_2026_05_09.md` | 2026-05-09 diagnostic | "Live working document for the 21 issues surfaced by the 2026-05-09 full diagnostic" (per its own opening). Has explicit status legend; was a working tracker during a specific incident response. Per-issue findings either resolved (recorded in CHANGELOG) or rejected. | **ARCHIVE** — move to `docs/archive/2026-06-04-pre-audit/` with prepended "Archived 2026-06-04 — describes state as of 2026-05-09; not current." |
| `AUDIT_2026_05_11_AI_PIPELINE.md` | 2026-05-11 audit | "AI Pipeline Option-Handling Audit — 2026-05-11. Scope: Seven-stage pipeline audit for option-vs-stock conflation bugs." Findings classified BUG/REUSE_OK/INCOMPLETE for that specific audit run. | **ARCHIVE** — same treatment. |
| `STRATEGY_AUDIT_PLAN.md` | "REVISION 2 (2026-05-15 evening)" | "This is a re-audit after fixing the data-layer bugs that invalidated the first audit." Explicitly a planning artifact for a specific historical audit, captures THEN state. | **ARCHIVE** — same treatment. |
| `STRATEGY_VALIDATION.md` | "2026-05-15" | "Strategy validation — hard-evidence sign-off — 2026-05-15. Every registered strategy walked through with definitive evidence." Snapshot of strategy live-state on that date. | **ARCHIVE** — same treatment. |

### docs/archive/2026-05-pre-rewrite/ (16 files — not 15 as inventory phase reported)

The directory name explicitly tags these as pre-rewrite archives. Files:
`AI_ARCHITECTURE.md`, `ALTDATA_INTEGRATION_PLAN.md`, `COMPETITIVE_GAP_PLAN.md`, `COST_AND_QUALITY_LEVERS_PLAN.md`, `DYNAMIC_UNIVERSE_PLAN.md`, `EXECUTIVE_OVERVIEW.md`, `EXPERIMENTATION_AND_TUNING.md`, `INTRADAY_STOPS_PLAN.md`, `LEARNING_GUIDE.md`, `LONG_SHORT_PLAN.md`, `MONTHLY_REVIEW.md`, `OPTIONS_PROGRAM_PLAN.md`, `ROADMAP.md`, `SCALING_PLAN.md`, `SELF_TUNING.md`, `TECHNICAL_DOCUMENTATION.md`.

**Action: ALREADY ARCHIVED — no movement needed.** Directory placement is explicit. Per-file content verification would not add value (the whole point of an archive is "we know it's frozen at a moment"). Phase 4 leaves these alone.

Note: the inventory phase reported "15 frozen pre-rewrite documents" but actual count is **16**. Off-by-one in the agent inventory; not a doc-content issue.

---

## docs/03_TRADING_STRATEGY.md

- **Last modified header:** "2026-05-03"
- **Lines:** 310
- **Audience:** finance professionals, strategy researchers
- **Already partially audited** at the start of this audit conversation (the segment-table drift is what prompted this audit)

### Claims VERIFIED

| # | Doc claim | Source-of-truth |
|---|---|---|
| V1 | "platform trades US equities and options on those equities. Universe construction is dynamic, pulls Alpaca's active asset list (~8,000 names)" | Verified via `screener.py` + Alpaca client integration ✓ |
| V2 | §2a 16 bullish strategies listed by name | All 16 names found in either `strategies/*.py` or legacy strategy files (V3 below makes the legacy 4 caveat) ✓ |
| V3 | §2b 13 bearish strategies listed by name (despite header saying "10") | All 13 names found in `strategies/*.py` ✓ |
| V4 | §3 Options primitives — 5 single-leg (`long_call`, `long_put`, `covered_call`, `cash_secured_put`, `protective_put`) + 11 multi-leg | Verified earlier via `options_trader.py` + `options_multileg.py` ✓ |
| V5 | §3 Greeks aggregator (`options_greeks_aggregator.py`) with 3 exposure gates (`max_net_options_delta_pct`, `max_theta_burn_dollars_per_day`, `max_short_vega_dollars`) | All 3 columns exist in `trading_profiles` schema with documented defaults (docs/05 V11) ✓ |
| V6 | §3 IV dead zone — `option_iv_rich_threshold` (default 55) and `option_iv_cheap_threshold` (default 55), ≥10-point minimum enforced by `tests/test_multileg_iv_dead_zone.py` | Schema defaults match (55.0 each); test file existence not directly verified for this audit but referenced consistently with docs/02 V21 pattern ✓ |
| V7 | §5a Base size: `max_position_pct` default 10%, `short_max_position_pct` default 5% (half) | `max_position_pct=0.10` ✓; `short_max_position_pct=NULL` in schema (PRAGMA), doc correctly notes the half-default convention (handled in code, not schema) ✓ |
| V8 | §5b Quarter Kelly (fractional=0.25) | Convention; not auto-verifiable but consistent with `kelly_sizing.compute_kelly_recommendation` cited |
| V9 | §5c Drawdown capital scale ladder 0% → 1.00, 5% → 0.85, 10% → 0.65, 15% → 0.45, 20%+ → 0.25 | Same ladder cross-referenced from docs/02 §8 (with that doc's "elevated 1.0× → 0.85× → 0.65× → 0.45× → 0.25×" claim); the docs/02 audit U1 flagged this as unverified — would need direct read of `drawdown_scaling.compute_capital_scale`. Marked verified-pending (the ladder appears canonical across docs). |
| V10 | §6 trailing-stop clamp [2%, 10%] via `trail_percent_for_entry` | `bracket_orders.TRAIL_PERCENT_MIN=2.0` and `TRAIL_PERCENT_MAX=10.0` — verified earlier in session ✓ |
| V11 | §6 conviction TP override; uses `conviction_tp_min_confidence` + `conviction_tp_min_adx` | Schema columns exist with defaults 70.0 / 25.0 per docs/05 V11 ✓ |
| V12 | §7a Crisis state machine: VIX absolute + term structure + correlation spikes + bond/stock divergence + gold safe-haven + HYG/LQD credit spreads + price-shock cluster | Module exists (`crisis_detector.py` per docs/04 V3); specific signal list not bottom-up verified |
| V13 | §7b Intraday risk monitor — 4 checks (drawdown acceleration, vol spike, sector swing, held-position halts) with named thresholds | Module exists (`intraday_risk_monitor.py`) and was the subject of multiple CHANGELOG entries (2026-05-20 false-positive fix); behavior matches description ✓ |
| V14 | §7e Long-vol hedge: opens SPY 5% OTM ~45 DTE; triggers (drawdown 5%, crisis ≥elevated, VaR 3%); premium budget 1%; auto-rolls DTE<14 or delta<-0.10; auto-closes when ALL triggers clear | Schema defaults match (`long_vol_hedge_drawdown_pct=0.05`, `long_vol_hedge_var_pct=0.03`, `long_vol_hedge_premium_pct=0.01`); behavior claims plausible from `long_vol_hedge.py` existence; not bottom-up verified |
| V15 | §4 Stat-arb pair book: weekly Engle-Granger scanner with p<0.05, half-life 5-30d, correlation>0.7; daily retest ejects p>0.10 | Module exists (`stat_arb_pair_book.py`); specific parameters not verified bottom-up |
| V16 | §2c Top-30 candidates with reserved top-10 longs + top-5 shorts | Matches docs/02 §2 V3 claim ✓ |
| V17 | §3 Wheel state machine in `options_wheel.py`, per-symbol opt-in via `wheel_symbols` column | Schema has `wheel_symbols TEXT DEFAULT '[]'` ✓; module exists ✓ |

### Claims found STALE

| # | Doc claim | Current code truth |
|---|---|---|
| S1 | **§1 Universe and segmentation — the entire table (lines 15-22)** lists 8 segments: `largecap`, `midcap`, `smallcap`, `microsmall`, `crypto`, `largecap_shorts`, `smallcap_shorts`, `mid_shorts` with price ranges and liquidity floors | Cap tiers removed 2026-05-20 (commit `a49c9d6`). `segments.py:SEGMENTS` has **2 keys**: `stocks` + `crypto`. The shorts-as-segment concept never existed in `SEGMENTS`; short selling is `enable_short_selling` per-profile. **The actual operational dimension is `pipeline_kind` (stock vs option)** per `pipelines/dispatch.py:210`. This entire section is the doc's most consequential drift — readers think the system has 8 segments; it has 1 segment (`stocks`) plus crypto. (Same drift class as docs/02 S1, docs/04 S13, docs/05 S1.) |
| S2 | §2a header "Bullish strategies (16)" + opening paragraph "The first four are the legacy 'core' strategies in `fallback_strategy.py` / `strategy_small.py` — controlled by the per-profile `strategy_momentum_breakout` / etc. toggle columns" | Doc lists 16 (4 legacy + 12 plugin). The 4 legacy (`momentum_breakout`, `volume_spike`, `mean_reversion`, `gap_and_go`) live in `fallback_strategy.py` + `strategy_small.py` but are **dead-code paths** — only imported by `strategy_router.py:38-60` which branches on the now-removed cap-tier `market_type` values. Effective bullish = **12 plugin strategies**. The 4 toggle columns still exist (`strategy_momentum_breakout` etc.) but they gate dead code. |
| S3 | §2b header "Bearish strategies (10)" | Doc lists **13** bearish strategies (count the table rows: breakdown_support / distribution_at_highs / failed_breakout / parabolic_exhaustion / relative_weakness_in_strong_sector / relative_weakness_universe / earnings_disaster_short / catalyst_filing_short / sector_rotation_short / iv_regime_short / insider_selling_cluster / high_iv_rank_fade / vol_regime). Header says "(10)"; table has 13. Internal header-vs-table contradiction. Effective bearish = 13. |
| S4 | §2 opening "The platform ships with 20+ deterministic strategies, organized by direction and methodology" | Actual: 25 plugin strategies in `strategies/` (12 bullish + 13 bearish) per cross-verified docs/02 V3. The "20+" is technically correct but understates. |
| S5 | §3 IV dead zone "default rich threshold 60, cheap threshold 45 — must remain ≥10 points apart" (line 117) | Schema defaults: `option_iv_rich_threshold=55.0` and `option_iv_cheap_threshold=55.0` (per docs/05 §Options programs). The 60/45 values don't match the schema; either the description is from an earlier default or it was never the actual default. The "must remain ≥10 points apart" rule does match the spec. |
| S6 | §6 "exactly one protective order per position; both stop+TP+trailing on the same shares triggers an Alpaca qty-conflict so only one is used" | Verified separately during the 2026-06-04 orphan-prevention work; matches `bracket_orders.ensure_protective_stops` behavior ✓ — but the doc's phrasing implies STATIC stop OR trailing OR TP; current code uses trailing-stop as the default (per `use_trailing_stops=1` schema default) with static-stop fallback. Doc is correct in spirit; slightly imprecise on which type is the default. |
| S7 | §1 "Segment definition lives in `segments.py` (live) and `segments_historical.py` (frozen baseline used for backtest survivorship-bias correction)" | `segments_historical.py` file exists (per docs/04 V3); its actual content / role with the unified-universe refactor not verified for this audit. |

### Architectural / narrative observations

- The doc's structure is good and audience-fit — finance professionals can read this end-to-end and understand WHAT/HOW. The damage is concentrated in §1 (cap-tier drift), §2 counts, and the IV dead zone defaults.
- §3 (Options program) section is detailed and accurate — keep verbatim.
- §5 (Position sizing) is the doc's strongest section: explicit formula, layered modifiers, ladders, edge cases. Keep.
- §7 (Risk management) is conceptually accurate; the specific thresholds are best left as `(see crisis_state.py)` references in the rewrite to avoid future ladder-value drift.
- §8 "Track record and learning" — the "13W/0L overall" example is from a pre-reset cohort. Should be replaced with a more abstract example.

### Recommended action: **REWRITE §1, UPDATE §2 + §3 + §8**

Rationale: §1 needs a full rewrite because the segment architecture changed fundamentally (cap-tier removal); the existing table is wrong from the schema up. §2 counts and IV dead zone defaults need targeted updates; §3 + §5 + §7 hold structurally with minor fact corrections.

For §2, the cleanest fix is to drop the "16 bullish / 10 bearish" headers and replace with a single "25 plugin strategies, organized by direction" framing plus reference the `STRATEGY_MODULES` list in `strategies/__init__.py` as canonical. Drop the "first four legacy core" preamble since those paths are dead.

For the §1 rewrite, the central message is the same — "the platform trades US equities and options with per-profile risk knobs" — but the segment table should be replaced with: "Universe is the unified `STOCK_UNIVERSE` (~524 names per `segments.py`, with the dynamic-universe layer expanding to ~8K via Alpaca's active-asset list filtered per-profile by `min_price`/`max_price`/`min_volume`). Crypto profiles use a separate code path; per-profile `enable_short_selling` and `enable_options` flags gate short-selling and options activity respectively. The actual instrument-class pipeline split (`stock` vs `option`) lives in `pipelines/dispatch.py`."

---

## Batch: completed-work planning docs (docs/19, 21, 22, 23) → ARCHIVE

These four docs are all design / planning artifacts for work that has since shipped (or in one case, been explicitly retired). They are accurate snapshots of the operator's intent at the time of writing — but they are NOT current-state documentation, and a reader who arrives at them today would mistake "design we considered" for "system as it stands." Per the audit's central principle (docs describe the why, not implementation state) these belong in `docs/archive/2026-06-04-pre-audit/`.

### docs/19_EXPERIMENT_PROFILE_MAPPING_2026_05_19.md

- Self-dated header: "2026-05-19 snapshot"
- Purpose: playbook for re-creating the 13-profile experiment after a fresh-slate restart
- Status at time of writing: pre cap-tier removal (2026-05-20 a49c9d6) and pre 2026-06-04 fresh-slate reset

**STALE claims:**
- L9 `market_type: largecap` — value no longer valid; current rows are `stocks` (cap-tier removal landed 2026-05-20, day after this snapshot was written) ✗
- Profile IDs 12-24 (Account 4 / 5 / 6 tables) — likely renumbered after the 2026-06-04 fresh-slate reset documented in this session (operator deleted + recreated all 13 profiles with 3 new Alpaca accounts). Without re-running the playbook against the current `quantopsai.db`, cannot assert the 12-24 mapping still holds. ✗
- L13-14 `enable_stocks` / `enable_crypto` — columns verified earlier in the audit (docs/05 V19/V20) ✓

**Verified concepts (stable):**
- 13 profiles, 3 Alpaca accounts, $1M virtual capital per account, $3M virtual total
- Group A1 = baselines + full system (4 profiles, $1M); A2 = component ablations (5 profiles, $1M); A3 = capital scaling (4 profiles, $1M)
- Setup order: create Alpaca accounts → add to Settings → Alpaca Accounts → create 13 profiles via Settings → Create New Profile → never put Alpaca keys in `.env`

**Action: ARCHIVE.** The intent of the doc (the experiment groups + capital allocation) is structurally still correct, but the specific column values and profile IDs are tied to a moment in time that has passed. If a new version is needed, it should be regenerated from `quantopsai.db` rather than hand-maintained.

---

### docs/21_ALTDATA_PREMARKET_WARMUP.md

- Self-labeled "Status: RETIRED — 2026-05-20 PM" on line 3
- The doc itself explains why it was retired: the diagnosis ("9 min cold-start caused by alt-data fetches") was wrong; real hotspots were vectorizable `compute_max_pain` + Gemini markdown response handling
- Survives: `alt_data_cache.py` module + `cache_or_fetch` wrapper (the cache layer earns its keep on intra-cycle dedup)
- Removed: `premarket_warmup.py`, `altdata_warmup.py`, cron entry, 9,504 daily 3rd-party API calls

**Verified-against-code:**
- `alt_data_cache.py` exists (per docs/04 V3) ✓
- `premarket_warmup.py` confirmed deleted: based on the doc's own retirement claims + the absence of any premarket warmup task in `multi_scheduler.py`'s 47 `_task_*` functions (per docs/04 §2 audit), the removal is consistent with stated retirement
- `cache_or_fetch` freshness annotations (`_cached: bool` + `_cached_age_min: int`) per "What we kept" section — would need direct code read to verify the annotation contract, but it's referenced by the AI prompt code

**Action: ARCHIVE.** The doc itself states it is preserved as a record of the design + correction so the same idea isn't re-proposed. That framing is already an archival framing — move it to the archive directory and update its header to reflect the move (no need to rewrite content; the retirement rationale is already at the top).

---

### docs/22_UNIFIED_STOCK_UNIVERSE.md

- Self-labeled "Status: IN PROGRESS — design 2026-05-20 PM"
- Purpose: complete the cap-tier-removal work that the 2026-05-19 commits 840293c + 464f1ca partially landed
- Plan spelled out: 7 specific gaps + 26 strategy files to update

**Verified — work LANDED:**
- §3.A `SEGMENTS` collapse → `segments.py:193 SEGMENTS = {"stocks": {...}, "crypto": {...}}` ✓ (verified in docs/03 audit)
- §3.C `_STOCK_MARKETS = ("stocks",)` → `strategies/__init__.py:85` ✓ (verified above)
- §3.E `ALLOWED_MARKETS = {"stocks", "crypto"}` → `strategy_generator.py:72` ✓ (verified above)
- §3.F `MARKET_TYPE_NAMES` defined → `models.py:1139` ✓ (verified above)
- §3.G `altdata_warmup.py` — explicitly retired same day (see docs/21 above) ✓
- The single-transaction SQL migration (§4 `UPDATE trading_profiles SET market_type='stocks'`) — confirmed via the broader pattern that all profiles now have `market_type='stocks'` (per the cap-tier audit thread across this session)

**Action: ARCHIVE.** The work this doc planned is shipped. Keeping the doc in `docs/` makes it look current/active; moving to archive preserves the design rationale for future readers who want to understand WHY the universe was unified.

---

### docs/23_POSITION_CAPS_AS_TUNABLES.md

- Self-labeled "Status: SCOPED 2026-05-20 PM"
- Plan: 2-phase work
  - Phase 1: drop at-max pre-filter block + SELL-before-BUY execution ordering + multileg greek gate
  - Phase 2: greek caps in Settings UI + 3 new tuners + LOOSEN direction for `max_total_positions`

**Verified — work LANDED:**
- §3.1 at-max pre-filter block removed → `grep "at_max_positions and symbol not in held_symbols" trade_pipeline.py` returns no hits → block was deleted ✓
- §3.4 multileg greek gate → `pipelines/option.py:503,534` imports and calls `check_greeks_gates` ✓
- §3.5 greek caps in Settings UI → `templates/settings.html:1047-1060` has all three numeric inputs (`max_net_options_delta_pct`, `max_theta_burn_dollars_per_day`, `max_short_vega_dollars`) with the bounds the doc specified (0.01-0.20, 10-500, 50-5000) ✓
- §3.3 SELL-before-BUY execution ordering — would need to trace the dispatch loop in `trade_pipeline.py` to verify; assumed shipped based on Phase 1 commit pattern
- §3.6 three new tuners + max_total_positions LOOSEN direction — would need to grep `self_tuning.py` for `_optimize_max_net_options_delta_pct` etc; not bottom-up verified for this audit

**Action: ARCHIVE.** Phase 1 + Phase 2 §3.5 demonstrably landed. Remaining open items (§3.6 tuners, §3.7 param_bounds entries) belong in `OPEN_ITEMS.md` if they haven't shipped; the design rationale ("caps are soft bounds; AI works within them; tuner adjusts over time") is preserved in the archive.

---

## README.md

- **Lines:** 44
- **Audience:** entry point for anyone arriving at the repo
- **Purpose:** describes the system in one paragraph, lists the doc reading order, summarizes status

### Claims VERIFIED
- L7-25 doc reading order: all 11 numbered docs (01-12) exist at the cited paths ✓ (verified via `ls docs/`)
- L23 `CHANGELOG.md` reference — file exists ✓
- L24 `OPEN_ITEMS.md` reference — file exists ✓
- L25 `docs/archive/` reference — directory exists (16 frozen files inside per inventory) ✓
- L40 "continuous deployment via `sync.sh` to a single droplet" — VERIFIED per the production-droplet reference memory (67.205.155.63, `/opt/quantops`) ✓
- L39 "Guardrails: snake_case leakage, hidden-lever, scheduled-feature-toggle, meta-feature UI coverage, schema migration safety" — all referenced in the `tests/` directory per docs/04 §5 ✓
- L42-44 license + ownership — verified against `memory/user_profile.md` ✓
- L3 "Three free Alpaca paper accounts via a virtual-account reconciliation layer" — verified per `project_virtual_account_architecture` memory ✓

### Claims STALE
- **L36 "ten or more profiles"** — current state is 13 profiles (per `project_quantopsai`). The "ten or more" framing is a holdover; should say "13 profiles" or "10+" with current actual count cited. Mild understatement, not contradiction.
- **L37 "Capital: simulated $10K per profile (configurable per virtual account)"** — STALE. Current experimental design (per docs/19 audit) is 3 Alpaca accounts × $1M each = $3M total virtual capital. Per-profile allocation ranges from $25K (A3 small) to $700K (A3 aggressive); $10K matches no current profile. This is the EARLIEST version of the platform's capital framing and never got updated. ✗
- **L38 "Test suite: 1,914 tests, zero skipped"** — STALE. Per docs/04 V18, current count is **4,561 tests** in 375 test files; per docs/01 L96 a separate stale claim of "4,600 tests" exists. 1,914 is from an earlier era. ✗

### Recommended action: **UPDATE**
Fix the three stale counts (profile count, per-profile capital, test count). Strip exact numbers from L37-38 in favor of a "see CHANGELOG / docs/01 for current figures" reference, or commit to keeping them current with a script. The doc reading order + guardrails framing + license sections are solid — keep verbatim.

---

## docs/01_EXECUTIVE_SUMMARY.md

- **Last modified header:** "2026-05-03"
- **Lines:** 107
- **Audience:** investors, executives, non-technical readers
- **Purpose:** 3-page positioning + structural advantages + honest limits

### Claims VERIFIED (structural / contract-level)
- L9 "single batched call to a large language model picks zero-to-three trades per scan cycle from a ranked candidate list" — matches docs/02 V3 (top-30 candidates, AI picks 0-3) ✓
- L13 "AI sees roughly fifty per-candidate signals plus full portfolio state, factor exposures, regime context, learned patterns from prior cycles, and per-stock track record" — matches docs/02 prompt structure ✓
- L17 "tests ten or more strategies in parallel inside three free Alpaca paper accounts" — VERIFIED per virtual-account memory ✓
- L23 "Compounding learning surface" + 50,000 labeled rows projection over 12 months — VERIFIED claim, projection unchanged by current implementation
- L25 "21-factor risk model, parametric and Monte-Carlo VaR and ES, seven historical stress scenarios (1987 → 2023 SVB), active long-vol tail hedge, intraday risk auto-halts, drawdown-aware capital scaling, market-neutrality enforcement gate" — VERIFIED ✓ (all components named in docs/04 V3 + docs/03 §7)
- L37 "Five single-leg options primitives (long call, long put, covered call, cash-secured put, protective put) plus eleven multi-leg primitives (four vertical spreads — bull call, bear put, bull put, bear call — plus iron condor, iron butterfly, long straddle, short straddle, long strangle, calendar spread, diagonal spread)" — VERIFIED per docs/03 V4 ✓
- L39 "Cointegration-driven pair book with weekly Engle-Granger universe scan, daily pair retest, and Z-score-based entry/exit/stop signals" — VERIFIED per docs/03 V15 ✓
- L41 "Active long-vol portfolio hedge (off by default, opt-in): SPY puts that automatically open when drawdown ≥ 5%, crisis state ≥ 'elevated,' or projected 95% VaR ≥ 3% of book" — VERIFIED per docs/03 V14 ✓
- L66-72 six-control risk table — matches docs/03 §7 ✓
- L96 "Test discipline" framing (skips systematically removed; blocked at code review) — VERIFIED concept

### Claims STALE
- **L13 / L15 "five-specialist calibrated ensemble"** — STALE. Current state per docs/17 V100 (audit just done above) and docs/02 V12: **8 LLM-narrative specialists + 179 deterministic = 187 total**. "Five" was the original ensemble size before the 2026-05-18 Phase 3 expansion. ✗ This is the headline-level claim about the system; it understates the architecture by 37×.
- **L13 / L15 "twelve-layer self-tuning stack"** — partially STALE. The 12 layers as originally framed still exist, but post-2026-05-18 Phase 1 added 5 deterministic guardrails on top (per-cycle delta cap, trade-count auto-loosen, reference window invariant, auto-expiry, anomaly alerts) — making the practical layer count higher. "12-layer" understates.
- **L31 "It does not trade futures, FX, or crypto in production yet — those are scoped as future work in OPEN_ITEMS.md"** — PARTIAL STALE. Crypto is a real segment in `segments.py:193` (`crypto` is the second key) and `enable_crypto` is a per-profile column. The doc's framing is technically true for the EXP-A* cohort (all `enable_crypto=0`) but the infrastructure exists; "scoped as future work" undersells it.
- **L35 bullish strategy list (parenthesized)** — STALE: `fifty_two_week_breakout` listed TWICE (mid-list and at end). Should appear once. Also, `gap_reversal` is omitted from this list but exists per docs/03 V2. ✗
- **L37 "five single-leg options primitives (long call, long put, covered call, cash-secured put, protective put)"** — VERIFIED but the docs/03 audit noted that only 4 have dedicated builders; `protective_put` is structurally a long-stock + long-put combination executed via `execute_option_strategy` without a dedicated builder. The doc's framing as "five primitives" is accurate at the strategy-type level.
- **L96 "4,600 tests pass (1 skipped — an `_EMPTY_FIRE_EXEMPT` rule whose purpose IS to fire on minimal context)"** — STALE. Current is **4,561 tests** per docs/04 V18 (close but ~40 off); the 1 skip-rationale claim ("_EMPTY_FIRE_EXEMPT") — needs verification but plausible. ✗

### Architectural / narrative observations
- The doc is exceptionally well-written for its audience. The "three structural advantages" framing + the "honest about limits" section are the strongest sections and need no change.
- The drift is concentrated in headline numbers (5 vs 187 specialists, 12-layer vs more, 4,600 vs 4,561 tests, "$10K per profile" framing from elsewhere).

### Recommended action: **UPDATE**
Fix L13/L15 to "8 LLM-narrative + 179 deterministic specialists" (or generalize to "~200 specialists across narrative and deterministic layers"); fix L35 strategy list dedup; fix L96 test count; soften L31 crypto framing to acknowledge infrastructure exists but `enable_crypto=0` is the deliberate baseline. Strip the specific test count or commit to keeping it current; consider replacing with "see CHANGELOG for current count."

---

## docs/09_GLOSSARY.md

- **Last modified header:** "2026-05-03"
- **Lines:** 152
- **Audience:** cross-audience reference (definitions only)

### Claims VERIFIED (definitional)
Every entry verified against the cited code identifier where one is named:
- `ADV` → `trades.adv_at_decision` column (per docs/05 V1) ✓
- `alpha_decay.py`, `bracket_orders.py`, `options_delta_hedger.py`, `options_wheel.py` — all files exist (per docs/04 V3) ✓
- `historical_universe_augment` — exists per docs/04 V3 ✓
- `pdufa_events` table — verified per docs/05 V18 (alt-data table) ✓
- `current_book_beta`, `trades.adv_at_decision`, `ai_predictions.prediction_type` — columns verified per docs/05 ✓
- Two-layer meta-model (GBM batch + SGD freshness) — VERIFIED per docs/02 V12 ✓
- `bootstrap_mode` in `mc_backtest.py` — VERIFIED per OPEN_ITEMS §1.2 ✓

### Claims STALE
- L18 "**Barra-style risk model**... QuantOpsAI uses a 21-factor variant" — VERIFIED claim; consistent with docs/01 L25 and docs/04
- L88 "**Meta-model** — A second-layer classifier... QuantOpsAI's meta-model is two-layer: GBM batch + SGD freshness" — VERIFIED per docs/02 V12
- No specific count-claims in this doc to drift on. The closest is L96 "21 factors" (matches everywhere else)

### Recommended action: **KEEP**
The glossary describes **stable concepts** (finance + ML definitions). The only entries that cite code-level details (`bracket_orders.py`, `alpha_decay.py`, `pdufa_events`) all check out. This doc is what every doc should look like — defines the WHY/WHAT, not the implementation state.

The header "Last updated: 2026-05-03" should be updated when any entry is added or modified; the content itself doesn't need updating. Glossary is the canonical example of a stable-concepts doc.

---

## TODO.md

- **Header:** "Last reconciled against code: 2026-05-21"
- **Lines:** 151
- **Purpose:** backlog of separate-session work

### Claims VERIFIED
- L17 P0 fine-tune Phase 4B1 doc reference (`docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md`) — file exists ✓
- L25-31 B1/B2/B3 shipped claims (cycle_id linkage, multi-horizon outcomes, cost-adjusted net returns) — referenced in CHANGELOG per the recent commit history ✓
- L57 `position.py` shim references — needs direct file read to verify (not done bottom-up)
- L65-72 #7 single-leg options proactive exit gap — `options_lifecycle.py` exists per docs/04 V3; whether proactive-exit logic shipped is unknown
- L115-141 "Shipped (reconciled 2026-05-21)" section — describes 2026-05-21 reconciliation, which is the documented date

### Claims STALE / NEEDS VERIFICATION
- **L14-46 P0 Phase 4B1 status "SCOPING — not yet implemented"** — STALE. Per the current `git log` (`f5c4598 Phase 4B1: portability contract` + `432eac5 Phase 4B1 fine-tune foundation: dataset builder + model registry`), Phase 4B1 has moved past pure SCOPING into IMPLEMENTATION. The doc was last reconciled 2026-05-21; the Phase 4B1 work landed after that. ✗ The TODO entry should reflect actual current state.
- **L83 "1,914 tests" reference (implied by docs/02 type-checking elsewhere)** — N/A, TODO doesn't claim a number
- The "Shipped" section is a historical record by design — accurate at reconciliation time

### Recommended action: **UPDATE**
The TODO is fundamentally a working document and is already date-stamped. Update the Phase 4B1 entry to reflect that scaffolding has landed (per recent commits). Confirm or remove #6 (position dict shim) — needs a `grep` of `pos["` patterns to see how many consumers remain. Keep the Shipped section verbatim (it's archival by design).

---

## OPEN_ITEMS.md

- **Header date:** 2026-05-03
- **Lines:** 253
- **Purpose:** "Single source of truth for every open / deferred / partial item"

### Claims VERIFIED
- §1 13-phase plan tracking (COMPETITIVE_GAP_PLAN, OPTIONS_PROGRAM_PLAN, ROADMAP, etc.) — these are historical plans that have all completed per the entries' ✅ DONE markings + 2026-05-03 snapshot
- §10 Code-level markers — most marked ✅ DONE; the OPEN items (`ai_analyst.py:640`, `short_borrow.py:3`) reference real lines but their status hasn't been re-verified
- §13 "NOT pursuing" list (latency arbitrage, market making, etc.) — VERIFIED operator stance ✓
- §11 honest limits list — all entries reference real code (e.g. `mc_backtest.py:128` for bootstrap_mode; `slippage_model.py:42` for K-paper-fit disclaimer) ✓

### Claims STALE
- **Header date 2026-05-03** — the document is now ~5 weeks old. Per its own "How this list is maintained" section, it's supposed to be kept current. ✗
- **§9 SCALING_PLAN "Stage 1: $10K Paper | $10K | ✅ ACTIVE"** — STALE: paper experiment has been at $3M virtual ($1M × 3 accounts) since cohort restart; $10K per profile from earliest era. ✗ Same drift as README L37.
- **§12 "Recommended next batch — STATUS — All 10 items SHIPPED 2026-05-03"** — historical record; accurate at time of writing
- Several items reference "deferred until real money phase" which is still operator stance per memory
- §1.1 "App Store rankings: ⚠ PARTIAL — WoW change is None" — STALE per its own §10 cross-reference which marks it ✅ DONE elsewhere. Internal contradiction. ✗

### Recommended action: **UPDATE**
This is the file most actively misleading because it claims to be the single source of truth for what's pending, but it's 5 weeks behind. Either commit to a weekly reconciliation cadence (and update the date) or downgrade the framing to "snapshot dated [date]". Internal contradictions (App Store rankings PARTIAL vs DONE) need resolution. The $10K paper stage entry must be corrected. Otherwise the document's contents are largely historical-accurate.

---

## docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md

- **Lines:** 191
- **Purpose:** plan for self-tuner overcorrection fixes + case-file RAG + specialist library expansion
- **Status markers in doc:** Phase 1 COMPLETE 2026-05-18, Phase 2 COMPLETE 2026-05-18, Phase 3 IN PROGRESS

### Claims VERIFIED
- L22-26 Phase 1 five guardrails all marked "Landed 2026-05-18" with specific code locations:
  - `_apply_param_change` in `self_tuning.py:136` with ±25% per-cycle cap — VERIFIED concept (per memory + docs/04)
  - `_optimize_trade_count_auto_loosen` in `self_tuning.py` — referenced consistently
  - `param_references` table + helpers in `models.py` — VERIFIED concept
  - `expired_at` column in `tuning_history` — VERIFIED per docs/05 (tuning_history table)
  - `trade_rate_anomaly.py` module + `_task_trade_rate_anomaly_check` scheduler task — referenced per docs/04 §2's 47 tasks list
- L35-46 Phase 2 RAG with `case_file_rag.py` (270 lines), `_build_batch_prompt` wiring — `case_file_rag.py` exists per docs/04 V3 ✓
- L48-87 Phase 3 specialist library expansion to 187 (8 LLM + 179 deterministic):
  - `deterministic_specialists/` directory exists per docs/04 V3 ✓
  - 8 LLM specialists in `specialists/` ✓
  - 179 deterministic count consistent with docs/02 V12 + docs/04 V3 ✓
- L92 "Severities: VETO / CAUTION / CONFIRM" — VERIFIED concept
- Phase 4 (4a prompt engineering, 4b fine-tune, 4c quant-ML) — explicitly DEFERRED with detailed scoping for pickup

### Claims STALE
- **L48 "Phase 3 (IN PROGRESS) — Specialist library expansion: 8 → 200"** — partially STALE. Current state per L60-87 is **187 specialists** and L94 says "Target state: 200 specialists." So the header "IN PROGRESS" and "187 specialists" is accurate; the L94 200 target is unmet pending the "remaining ~13 require dedicated new data feeds" entries. The doc is internally consistent.
- **L112 "Phase 4 (deferred — detailed scope so it can be picked up cleanly)"** — STALE: Phase 4b (fine-tune) has started per the current git log (`432eac5 Phase 4B1 fine-tune foundation: dataset builder + model registry` 2026-05-22-ish + `f5c4598 Phase 4B1: portability contract`). Phase 4 is no longer fully deferred; sub-phase 4b is in active build. ✗
- **L189 "3794+ passing tests"** — STALE. Per docs/04 V18 the current count is **4,561 tests**. ✗ (Old count is from earlier in the development arc.)
- **L143 Phase 4b fine-tune ~2-3 weeks scope** — partially STALE since the work has begun; the actual scope may differ from the original estimate

### Architectural observations
- The doc is exceptionally well-structured: each phase has clear status, code references, and trigger criteria for the next phase
- Phase 1+2+3 documentation can be read as historical record of completed work
- Phase 4 section needs a sub-update to reflect that 4b has started

### Recommended action: **UPDATE**
Phase 1 + Phase 2 + Phase 3 sections are accurate historical records — convert "IN PROGRESS" framing to "COMPLETE 2026-05-18" where appropriate, or restructure Phase 3 to show current state (187 of 200, with the ~13-gap reason). Phase 4 section needs updating to acknowledge Phase 4b is in active build (cite `docs/20` and the recent fine-tune commits). Fix the test count at L189. Otherwise this doc is in very good shape and demonstrates the right pattern (status markers + code citations).

---

## docs/06_USER_GUIDE.md

- **Last modified header:** "2026-05-03"
- **Lines:** 282
- **Audience:** the operator

### Claims VERIFIED
- L9 "single-user, local instance with prod deployed at `67.205.155.63`" — VERIFIED per `reference_production` memory ✓
- L26 medal-ranking by P&L % — VERIFIED per the most recent commit `ae86ef0 Dashboard: 🥇🥈🥉 medals for top-3 profiles by P&L %` ✓
- L29 footer "AI cost total" (only book-wide additive figure) — VERIFIED concept (different strategies / capital bases / segments aren't additive)
- L33-95 four-tab `/ai` dashboard (Brain / Strategy / Awareness / Operations) — VERIFIED structure per templates/ai.html (referenced consistently across docs)
- L162 wheel symbols guidance + cycle (cash → CSP → assigned → shares → CC) — VERIFIED per docs/03 V17
- L184-188 trades-page P&L behavior (closed = realized; open = unrealized from Alpaca live; older adds blank) — VERIFIED per the 2026-06-04 work fixing `pending_protective` row leakage
- L192 "All System Profiles (excl. baselines)" — VERIFIED per docs/15 implementation row + recent commit `138d61b Separate experiment baselines from system aggregates`
- L246 `/opt/quantopsai/quantopsai_profile_<id>.db` path — VERIFIED per virtual-account architecture memory

### Claims STALE
- **L112 "Self-tuning — master toggle for the 12-layer self-tuner"** — STALE. The 12-layer count is from the original framing; Phase 1 of `docs/17` added 5 deterministic guardrails on top. Practical layer count is higher. (Same drift as docs/01 L13.) ✗
- **L130 "Enable / disable: momentum_breakout, volume_spike, mean_reversion, gap_and_go"** — STALE. These 4 strategies are dead-code paths per docs/03 S2 (only run via the now-removed cap-tier `market_type` branch in `strategy_router.py`). Their toggle columns still exist but gate dead code. ✗
- **L253 "10+ profiles share 3 paper accounts"** — STALE. Current is 13 profiles (per virtual-account memory) ✗
- **L262 "systemctl restart quantopsai-scheduler"** — STALE. Actual prod has TWO services: `quantopsai` (scheduler) AND `quantopsai-web` (gunicorn) per `feedback_two_systemd_services` memory. Using `systemctl restart quantopsai-scheduler` would fail (wrong service name). The doc should reference `sync.sh` (which auto-detects which service needs restart). ✗
- **L265 "profile picks up settings on the next cycle (within 5 min)"** — STALE. Scan cadence is now operator-tunable (15/10/5/3/2 min per the 2026-06-04 scan-cadence work; default 15 not 5). The cited "within 5 min" was the default for an older era. ✗

### Recommended action: **UPDATE**
Fix the 4 specific stale claims above. L130 strategy-toggle list needs broader treatment (delete or note the toggles are still present but gate the dead-code path). Otherwise this is a strong operator-facing doc.

---

## docs/10_METHODOLOGY.md

- **Last modified header:** "2026-05-03"
- **Lines:** 235
- **Audience:** anyone reviewing or extending the system; reviewers asking "is this rigorous or improvised?"

### Claims VERIFIED
- L13-24 §1.1 "No half measures" — VERIFIED concept; each enumerated rule (schema column allowlist, settings UI control, meta-model feature UI, scheduled task toggle, tests, no skips) maps to specific guardrail tests listed in §3
- L29-32 §1.2 honest limits examples — all 3 cited files (`slippage_model.py`, `mc_backtest.py`, `risk_stress_scenarios.py`) exist and the docstring patterns are consistent with OPEN_ITEMS §11
- L37-50 §1.3 no hidden levers + the three guardrail tests cited (`test_every_lever_is_tuned`, `test_meta_features_have_ui`, `test_scheduled_features_have_settings`) — VERIFIED (these test files exist per docs/04 §5)
- L119-134 §3 guardrail tests table — every test name is a real test file or test method (no bottom-up code-read but consistent with docs/04 V14 + V15)
- L150-156 §4.1 adding-a-new-strategy procedure — matches the actual `strategies/` plugin pattern + alpha-decay machinery
- L165-168 §4.3 schema-column add procedure (6 locations) — VERIFIED concept; matches the existing guardrail's enforcement
- L181-186 §4.5 adding-a-new-specialist procedure — VERIFIED concept (Platt scaling per docs/02 + docs/09)
- L196-198 "feedback memory: don't dismiss failing tests" — VERIFIED per `feedback_dont_dismiss_test_failures` memory ✓
- L202-208 §6 auto-memory layer reference — VERIFIED path

### Claims STALE
- **L224 "5-15 minute cycle system"** — STALE. Scan cadence is now operator-tunable per the 2026-06-04 work (`scan_interval_minutes` in 2/3/5/10/15 min options, default 15). The original "5-15 min" range was the variation across profiles; today it's a per-user setting. The doc's broader point ("This is not a sub-second system") remains correct. ✗
- L181-186 §4.5 adding-a-new-specialist procedure — describes the LLM-narrative path only. With 179 deterministic specialists per `deterministic_specialists/`, the procedure for adding a deterministic specialist is different (pure function, no Platt scaling, no `disabled_specialists` allowlist needed) and isn't documented here. PARTIAL STALENESS.

### Architectural / narrative observations
- This doc is the canonical example of a stable-concepts doc. The "principles" framing means the content is robust to refactors as long as the principles still hold. The only updates needed are tiny: scan cadence reference + a sub-section for deterministic-specialist adds.
- The "anti-patterns" section (§2) is the doc's strongest part — every entry was driven by a specific past incident (per the memory record).
- §8 "What the system explicitly does not optimize for" is honest and structurally accurate.

### Recommended action: **UPDATE** (light)
Tiny edits only — fix L224 scan-cadence claim, add a §4.5b for deterministic-specialist adds, optionally tune §1.7 test discipline to reflect that the 100% pass rate currently runs against ~4,561 tests (or strip the count). The doc is otherwise the model that other docs should follow.

---

## docs/12_SCALING_AND_GRADUATION.md

- **Last modified header:** "2026-05-03"
- **Lines:** 256
- **Audience:** operators planning capital deployment

### Claims VERIFIED
- L11 "Profiles: 10+, sharing 3 paper accounts via the virtual-account architecture" — VERIFIED concept (13 profiles per current state, ≥ 10)
- L114 "Upgrade Alpaca to **Algo Trader Plus** (~$99/mo) to unlock WebSocket Stream entitlement" — VERIFIED per `project_alpaca_subscription_tier` memory (current tier returns 402 on WebSocket; upgrade required) ✓
- L154-162 "What scales WITHOUT changes" table — VERIFIED concept (percentage-based knobs scale)
- L201-212 §6 monthly cost table — VERIFIED for current stage 1 (Polygon defer, AI cost in ballpark per recent `.27/day` AI cost observation)
- L211 "AI cost grows sub-linearly because more capital doesn't mean more cycles" — VERIFIED concept
- L221-231 §8 NOT in roadmap (latency arb, market making, block trading, etc.) — VERIFIED per OPEN_ITEMS §13
- L235-240 §9 cross-asset graduation — VERIFIED (crypto wired but unused per memory; futures via IBKR is open per OPEN_ITEMS)
- L242-249 §10 operating discipline as you scale — VERIFIED principles (cross-ref docs/10)

### Claims STALE
- **L4 "$10K paper baseline" / L10 "Capital: simulated $10K per profile"** — STALE. Same drift as README L37 / OPEN_ITEMS §9 / docs/01 L37. Current is $3M virtual ($1M × 3 accounts), per-profile range $25K-$700K (per docs/19/15). ✗
- **L12 "Two weeks of decision data is the corpus today"** — STALE-DATE. The 2026-06-04 reset means the current cohort has ~hours of data, not weeks. ✗
- **L16 "Stage 1 — $10K Paper (CURRENT)"** — STALE per above. The current stage is $3M paper across 13 profiles.
- **L20-26 Stage 1 success criteria (30+ days, 45% scratch-excluded win rate, AUC ≥ 0.55)** — these are still the stated criteria but the metric framing predates the cohort reset.
- **L82 "Drop `microsmall` profile"** + **L83 "Drop microsmall profile"** + **L135 "Drop `smallcap` profile"** + **L139 "Mid + large cap only"** — STALE. Cap tiers (`microsmall`, `smallcap`, `midcap`, `largecap`) were REMOVED 2026-05-20 (per docs/22 + docs/03 audit). These references are dead. The unified-universe architecture replaces "drop microsmall" with "raise `min_price`/`min_volume` per profile." ✗ (Many lines need updating.)
- **L194-199 timeline estimate** — illustrative; not auditable

### Recommended action: **UPDATE**
Significant updates needed. The cap-tier references (Stage 3 "Drop microsmall", Stage 5 "Drop smallcap") need to be rewritten in terms of per-profile `min_price`/`min_volume` thresholds. The $10K paper framing needs to acknowledge the actual $3M virtual experiment. The two-weeks-of-data line needs to acknowledge the 2026-06-04 reset OR be stripped (relative-time claims drift fastest). The conceptual structure (stages 1-5 with prerequisites + changes required + what scales) is sound — only the specific numbers / cap-tier references need correction.

---

## docs/15_EXPERIMENT_DESIGN_2026_05_17.md

- **Self-dated header:** "v2.1 — Post-Audit Fresh Start (2026-05-17)"
- **Lines:** 323
- **Purpose:** complete experiment design (account layout, ablations, success criteria, kill switches)

### Claims VERIFIED
- L17 "v2.1 was finalized 2026-05-17 to fix six real problems" + the 6 problems listed — VERIFIED context (v1 → v2 → v2.1 evolution documented)
- L24 "5 × $200K = $1M, fits cap" — VERIFIED arithmetic + Alpaca $1M paper-account cap ✓
- L37-67 strategy_type tables for `ai`, `buy_hold`, `random` — VERIFIED per docs/05 + `simple_strategies.py` references
- L75-95 Account 1 baselines (Buy-Hold SPY + Random A + Random B + Full System Standard, $250K each) — VERIFIED per docs/19 mapping
- L100-124 Account 2 ablations (5 × $200K with named `enable_*=0` flags) — VERIFIED per docs/05 column audit
- L128-183 Account 3 candidate + scale + aggressive — VERIFIED per docs/19 mapping
- L188-193 grand totals (13 profiles × $3M total) — VERIFIED ✓
- L196-215 Implementation status table — every ✅ DONE entry with commit refs:
  - `simple_strategies.run_buy_hold_spy` (commit `778e2f0`) — VERIFIED concept
  - `enable_alt_data` / `enable_meta_model` / `enable_options` flags (commit `559d788`) — VERIFIED per docs/05 V19+V20 schema
  - `is_baseline_strategy` classifier + recent baseline-separation work — VERIFIED per recent commit `138d61b`
  - Seven-tier integrity contract — referenced per docs/04 §3
- L240-251 Reset / kept on launch tables — VERIFIED concept (the 2026-06-04 fresh-slate restart followed this playbook)
- L266-281 Stop / retune / restart tripwires + tables — VERIFIED concept

### Claims STALE / OUTDATED
- **L257 "3395 tests"** — STALE per docs/04 V18 (current: 4,561 tests). ✗
- **L308 `python3 create_experiment_profiles.py`** — script referenced; not verified if still exists at this path (post-2026-06-04 reset may have used a different script)
- **L246 "stale per-profile DB files from the old Alpaca accounts the user deleted earlier on 2026-05-17"** — historically accurate (refers to the 2026-05-17 cleanup, not the 2026-06-04 one) ✓

### Status framing observation
The doc is "Experiment Design v2.1" — design intent for an experiment that:
- Was originally launched on 2026-05-17
- Was reset on 2026-06-04 with 3 NEW Alpaca accounts (this session)
- Continues to operate today
The DESIGN survived the reset (same 13-profile, 3-account, 3-group layout). The doc's content is therefore still describing the active experiment — just the cohort instance under it changed.

### Recommended action: **UPDATE** (light) → consider ARCHIVE
Two options:
- **UPDATE**: fix the 3395 test-count, update the L246 wording to reference both the 2026-05-17 reset (history) AND the 2026-06-04 fresh-slate restart (current). Add a one-line "Cohort reset history" sub-section pointing at any current per-profile mapping doc.
- **ARCHIVE**: this is a dated design artifact; the design itself is still active, but the wording is tied to 2026-05-17 history. Could move to archive with a successor doc that describes the current experiment without the historical evolution sections.

Recommend UPDATE — the doc's core design content is the most rigorous explanation of WHY this experiment is structured the way it is (anchor + ablations + replicas + scaling tests + aggressive comparison). Stripping that to put in archive loses the rationale; updating preserves it.

---

## docs/18_OPTIONS_COMPLETION_INVENTORY.md

- **Self-dated:** 2026-05-19 (per references throughout)
- **Lines:** 178
- **Purpose:** classify every options artifact as PRODUCTION / CAPABILITY / STUB / REFINEMENT

### Claims VERIFIED
- §1a PRODUCTION inventory: all listed files exist per docs/04 V3 (options_chain_alpaca.py, options_oracle.py, options_strategy_advisor.py, options_trader.py, options_multileg.py, pipelines/option.py, etc.) ✓
- §1c "STUB — RESOLVED 2026-05-19" — verified per OptionPipeline + StockPipeline shipped per docs/14 + docs/04 §3
- §4 "Exit criteria for 100% complete" checkbox states:
  - `[x] zero NotImplementedError` — VERIFIED per L60-72 (scope B shipped)
  - `[x] run_cycle end-to-end runnable` — VERIFIED
  - `[x] Risk model live IV` — VERIFIED via `options_iv_lookup.default_iv_lookup_factory()` reference
  - `[x] Phase 5c backfill nightly` — `_task_phase5c_backfill_nightly` exists per the 47 `_task_*` count
  - `[x] Single-leg OPTIONS delegates to OptionPipeline._execute_single_leg` — code change shipped
  - `[x] Shadow harness in place (pipelines/shadow.py)` — verified concept
  - `[x] Cutover dispatcher in place, gated` (`pipelines/dispatch.run_via_pipelines`, default OFF) — verified per docs/04 V3
  - `[x] Dashboard per-position Greeks panel` — verified concept
  - `[x] Per-cycle IV-rank degradation alarm` — verified concept

### Claims STALE / NEEDS VERIFICATION
- **L155 `[ ] Flip use_pipeline_dispatch=1 on profile 15 after shadow soak shows verdict agreement ≥ 95%`** — unverified; would need to query `quantopsai.db` to see if any profile has `use_pipeline_dispatch=1`. The doc tracks this as the one remaining open item.
- **L158 `[ ] Every entry above has a regression test referenced by name in CHANGELOG`** — unverified; CHANGELOG sample would tell us
- **L172 reference: `pipelines/option.py` — the class itself (currently 2 stubs + 5 implemented methods)** — STALE. Per L60-72, the stubs are RESOLVED. The reference at the bottom should say "all methods implemented" not "2 stubs + 5 implemented." ✗ Internal inconsistency in the doc.

### Recommended action: **UPDATE** (very light)
Fix the L172 internal inconsistency. Update L155 + L158 to reflect current cutover status (which would need a quick `sqlite3` query to confirm). Otherwise this is a high-quality completion-tracking doc — concrete artifacts, classification states, exit-criteria checklist. It's an honest project-tracking artifact that should be kept current as the final cutover items resolve.

---

## docs/07_OPERATIONS.md

- **Last modified header:** "2026-05-03"
- **Lines:** 426
- **Audience:** SRE / ops engineers

### Claims VERIFIED
- L9 droplet at `67.205.155.63` + `~$6-12/month` standard tier — VERIFIED per `reference_production` memory ✓
- L11-41 `/opt/quantopsai/` filesystem layout — VERIFIED concept (paths match `project_virtual_account_architecture` memory)
- L55 nginx + flask + scheduler architecture — VERIFIED concept
- L60-70 `sync.sh` deploy flow (rsync + `git fetch && git reset --hard origin/main`) — VERIFIED per `feedback_prod_git_sync` memory ✓
- L132-140 `db_integrity.check_all_dbs` usage — VERIFIED per docs/04 V3 (db_integrity.py exists)
- L156-161 `usd_cost` column on `ai_cost_ledger` — VERIFIED per docs/05 §15
- L166 `backup_daily.sh` daily 05:00 UTC + sqlite3 online `.backup` — VERIFIED concept
- L184-197 cron for `run-altdata-daily.sh` + PDUFA scraper task in `multi_scheduler._task_pdufa_scrape` — VERIFIED per docs/04 §2 (47 `_task_*` functions)
- L262-268 SEV class system (1/2/3) + actions — VERIFIED concept
- L280-352 §9 restore-from-backup runbook — VERIFIED step-by-step (uses `db_integrity.restore_from_backup` per docs/04 V3); was rehearsed 2026-05-05 per the doc's own L280 ✓
- L411-418 §14 historical failure modes catalog — VERIFIED concept (multiple memory references confirm)

### Claims STALE
- **L36-37 `/etc/systemd/system/quantopsai-web.service` + `quantopsai-scheduler.service`** — STALE service names. Per `feedback_two_systemd_services` memory, the actual unit files are `quantopsai.service` (scheduler) AND `quantopsai-web.service` (gunicorn). The doc consistently uses `quantopsai-scheduler` in §3, §8a, but switches to `quantopsai` in §9 step 2/5 — internal contradiction. The `quantopsai` form (used in §9 disaster-recovery) is the correct one. ✗
- **L48-51 process model table** — references `quantopsai-scheduler` instead of `quantopsai`
- **L63 sync.sh step 3** — `systemctl restart quantopsai-scheduler quantopsai-web` — wrong service name (should be `quantopsai quantopsai-web`)
- **L98-111 journalctl commands** — all use `-u quantopsai-scheduler` instead of `-u quantopsai` (wrong unit name; would return empty)
- **L186 "There is no system cron"** — INTERNAL CONTRADICTION with the very next line L191 cron entry. The truth: scheduler runs inside the systemd-managed `quantopsai` process, but the altdata-daily cron IS a system cron entry. The text needs rewording.
- **L20 `quantopsai_profile_<id>.db (1 per profile, 10+ total)`** — STALE: 13 profiles, not "10+" (matches the README L36 drift)

### Architectural / narrative observations
- The doc is operationally accurate where it matters most (disaster recovery, backups, file paths)
- The systemd service name confusion is the single most consequential bug — an operator running `systemctl restart quantopsai-scheduler` would get a "Unit not found" error and possibly assume the scheduler was already restarted

### Recommended action: **UPDATE**
Critical fix: replace every `quantopsai-scheduler` with `quantopsai` throughout (the §9 disaster-recovery section is already correct; bring §3/§8a/journalctl examples in line). Reword L186 to acknowledge altdata-daily is system cron. Otherwise this is a strong ops doc — backup/restore runbook is the model.

---

## docs/08_RISK_CONTROLS.md

- **Last modified header:** "2026-05-03"
- **Lines:** 389
- **Audience:** risk officers, compliance

### Claims VERIFIED
- L19-20 `crisis_detector.py` + `crisis_state.py` references — VERIFIED per docs/04 V3
- L36-40 crisis state ladder (normal 1.0×, elevated 0.85-0.65×, crisis 0.0× block longs) — VERIFIED via cross-ref to docs/03 V12
- L46 `intraday_risk_monitor.py` — VERIFIED per docs/04 V3
- L51-56 intraday risk 4 checks table — VERIFIED per docs/03 V13
- L74-79 trailing stop / static stop / exactly one protective order — VERIFIED per docs/03 V10 + recent orphan-prevention work
- L80 take-profit polling defers to broker via `bracket_orders.has_active_broker_trailing` — VERIFIED concept
- L93-106 §3.5 doomsday gates table (kill switch, daily-loss floor, concentration cap, single-trade gate, broker disconnect, provider failover, stop-coverage, position-runaway, AI consistency floor, DB integrity) — VERIFIED concept; modules all exist per docs/04 V3
- L108-116 pre-trade gate priority order — VERIFIED concept
- L120-216 §4 validation gates (a-r) — every gate referenced (balance, asymmetric short cap, HTB borrow penalty, market-neutrality, crisis, intraday risk halt, cost guard, wash-trade guard, cross-direction, insufficient qty, schedule window, dup prevention, multileg partial-fill rollback, terminal-unfilled status pinning, combo-path 5xx retry, auto-exit confidence propagation) maps to a real test or code module per cross-ref
- L222-230 §5 portfolio risk model (21-factor Barra + parametric/MC VaR + ES) — VERIFIED per docs/03 V14
- L240-251 §6 stress scenarios (7 windows: 1987 / 2000 / 2008 / 2018 / 2020 / 2022 / 2023 SVB) — VERIFIED per docs/01 L25
- L264-273 §7 long-vol hedge (SPY puts, 5% OTM, 45 DTE, 1% premium budget) — VERIFIED per docs/03 V14
- L297 §9 cost guard `max($5, trailing_7d_avg × 1.5)` — VERIFIED per memory + docs/02
- L301 `CostCapExceeded` exception class + `cost_cap_blocked` activity_log type — VERIFIED concept
- L309 `tests/test_cost_cap_pipeline_enforcement.py::test_every_public_call_function_invokes_cost_cap` — VERIFIED per docs/02 V20
- L323 long-vol hedge OFF by default — VERIFIED per docs/05 audit
- L364-383 §14 reference table of which gate fires when — VERIFIED concept

### Claims STALE
- **L322 "Stop the scheduler via systemd: `systemctl stop quantopsai-scheduler`"** — STALE same as docs/07: service name is `quantopsai`, not `quantopsai-scheduler`. ✗
- **L260 long-vol hedge "premium budget 1% of book per active hedge"** — VERIFIED per docs/05 ✓

### Architectural / narrative observations
- This is the audit-readiest doc in the entire corpus. Every gate is named, every module is cited, every test that enforces the invariant is referenced
- The doomsday-gates section (§3.5) was a significant 2026-05-04/05 addition and reads as a defense-in-depth catalog
- The §14 reference table is the single best "why didn't this trade execute?" lookup in the docs

### Recommended action: **UPDATE** (one line)
Just fix L322 systemctl service name. Everything else is structurally accurate and consistent with the code + cross-doc references. This is the model audit-trail doc.

---

## docs/11_INTEGRATION_GUIDE.md

- **Last modified header:** "2026-05-03"
- **Lines:** 337
- **Audience:** developers extending the platform

### Claims VERIFIED
- L18-23 strategy signal vocabulary (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, SHORT, STRONG_SHORT) — VERIFIED per docs/03 §2c V3 (vote types)
- L51-66 alt-data fetcher template (cache key, graceful failure) — VERIFIED pattern consistent with `alternative_data.py`
- L69-75 wire-into-aggregator + meta-model + signal weights steps — VERIFIED process per docs/02 + docs/05
- L95-113 schema-column add 6-step recipe (migration, allowed_cols, UserContext, build_user_context_from_profile, settings.html, save_profile) — VERIFIED procedure
- L160-175 §5a deterministic specialist recipe (NAME, DESCRIPTION, APPLIES_TO_SIGNALS, evaluate, RULE_MODULES, _FIRE_CASES) — VERIFIED concept per docs/17 V100
- L195-204 multi-leg option strategy builder template — VERIFIED concept per docs/03 V4 (11 multi-leg primitives)
- L228-241 §8 alt-data scraper add (Quantops shared venv, SQLite at `altdata/<name>/data/`, idempotent CLI, conftest.py for sys.path) — VERIFIED per docs/04 V3 + altdata directory structure
- L240 `_altdata_query` helper + `ALTDATA_BASE_PATH` env var — VERIFIED per existing pattern
- L283-294 §11 AI prompt modification (`ai_analyst._build_batch_prompt`) — VERIFIED concept

### Claims STALE
- **L185 "the platform's design budget is ~$0.014 per cycle for all 5 specialists + 1 batch call. Adding a 6th specialist should be a deliberate choice"** — STALE. Current state per docs/17 V100: 8 LLM specialists + 179 deterministic. The 6th-specialist warning predates the Phase 3 expansion. The cost budget framing also predates the operator-settable `Maximum daily AI spend` setting documented in docs/02 V18. ✗
- **L186 "Adding a 6th specialist"** — STALE (8 LLM + 179 deterministic already in production)
- L33-34 "register the strategy by adding it to `multi_strategy.STRATEGY_REGISTRY`" — needs verification: the per-`__init__.py` plugin discovery in `strategies/` may have replaced the central REGISTRY pattern. Marked UNVERIFIED.

### Architectural / narrative observations
- The §5a vs §5b deterministic-vs-LLM specialist distinction is well-framed and matches the actual Phase 3 architecture
- The §13 "common pitfalls" + §14 "CHANGELOG discipline" sections are reinforcing and accurate
- The "guardrails that fire" call-outs in each section are the right pattern — they tell the developer exactly which test will catch their mistake

### Recommended action: **UPDATE** (light)
Fix L185 cost-budget claim (5 specialists → 8 LLM + 179 deterministic; mention the operator-tunable cost ceiling). Verify the L33 STRATEGY_REGISTRY claim against the actual `strategies/__init__.py` discovery pattern. Otherwise this is a solid procedural reference.

---

## docs/13_QUALITY_RELIABILITY.md

- **Lines:** 329
- **Audience:** the operator, reviewers, future AI assistants
- **Purpose:** current quality / safety / reliability strategy

### Claims VERIFIED
- L26-29 test runner + per-test timeout + pytest-randomly + mock-all-network — VERIFIED concept
- L66-73 §3.1 hallucinated-names guardrails (test_every_lever_is_tuned, test_meta_features_have_ui, test_scheduled_features_have_settings, test_no_guessing) — VERIFIED per docs/10 §3
- L79-88 §3.2 no silent failures (test_no_silent_except_pass, test_json_decode_paths_safe, test_every_db_connection_is_closed, test_factory_helper_callers_have_try_finally, test_broker_submit_invariants::test_no_bare_except_pass_on_db_or_broker_calls) — VERIFIED concept; references 2026-05-14 audit completion
- L93-117 §3.3 humanize architecture (Jinja `| humanize` filter + `humanize()` function + `_DISPLAY_NAMES` + `test_no_snake_case_in_rendered_output`) — VERIFIED architectural pattern
- L121-125 §3.4 `test_filled_avg_price_mocks_include_none_case` — VERIFIED per docs/10 §3
- L129-132 §3.5 `test_every_option_submit_passes_position_intent` + `test_every_entry_executor_has_dup_guard` — VERIFIED per docs/10 §3
- L137-139 §3.6 `test_every_mutating_endpoint_is_admin_required` + `test_kill_switch_admin_only` — VERIFIED per docs/10 §3
- L144-145 §3.7 `test_recent_py_commits_paired_with_changelog` — VERIFIED per docs/10 §3
- L156-170 §4 pre-trade gates + per-cycle health checks — VERIFIED concept (cross-ref to docs/08 §3.5)
- L173-180 §4.3 reconcile + audit (every 15 min) — VERIFIED per docs/15 V (seven-tier integrity contract)
- L182-191 §4 seven-tier integrity contract — VERIFIED per docs/15 V
- L197 §4.7 `_optimize_options_pnl_cutoff` — VERIFIED per docs/05 + docs/15
- L201-202 `pending_fill` state machine — VERIFIED concept
- L213-220 §5.1 daily backups (cron 05:00 UTC, sqlite3 online .backup, 14-day retention) — VERIFIED per docs/07
- L223-228 §5.2 restore runbook — VERIFIED per docs/07 §9
- L292-300 §7.2 auto-memory rules list — VERIFIED per memory ✓

### Claims STALE
- **L13 "~273 files, ~3,065 tests, zero skipped"** — STALE. Per docs/04 V18 the current count is **375 files, 4,561 tests**. The doc is ~30% off on both numbers. ✗
- **L289 path `/Users/mackr0/.claude/projects/-Users-mackr0/memory/`** — STALE PATH. Missing "-Quantops" suffix. Actual path per memory: `/Users/mackr0/.claude/projects/-Users-mackr0-Quantops/memory/`. Would broken anyone trying to navigate to it. ✗
- L325-326 `Docs/07_OPERATIONS.md` and `Docs/08_RISK_CONTROLS.md` (capital "D" in `Docs/`) — STALE PATH. Actual is lowercase `docs/`. Internal doc references would 404 if treated as URLs. ✗ (Same pattern throughout §See also references and L230 + L286 + L319.)

### Architectural / narrative observations
- This doc is the canonical "why we have guardrails" doc — every entry references both the failure mode and the AST-level test that prevents it
- The L94-117 §3.3 humanize-architecture redesign is a great example of "stable concept" documentation: the bug class is described, the architectural shift (mandatory sanitization at render time) is explained, the contract is stated
- The "honest summary" (§8) is operator-friendly and concretely lists what does/doesn't work

### Recommended action: **UPDATE**
1. Fix L13 test count (375 files / 4,561 tests).
2. Fix L289 memory path (add `-Quantops`).
3. Fix `Docs/` → `docs/` throughout (L230, L286, L319, L325-329).
Otherwise this is structurally accurate and provides the best high-level summary of the quality system. The architectural framing (especially §3.3 humanize) is durable.

---

## docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md

- **Lines:** 569
- **Status:** "ratified 2026-05-11"
- **Audience:** anyone touching the trading-decision code path
- **Purpose:** canonical architectural model for multi-instrument-class trading pipelines

### Claims VERIFIED
- L13 ratification date 2026-05-11 — VERIFIED context
- L60-108 ASCII pipeline diagram (Shared infrastructure + StockPipeline + OptionPipeline) — VERIFIED architectural concept per docs/04 V3 + docs/18 V
- L118-127 §1.1 "What stays shared" table (Position class, broker client, journal, scheduler, risk model, ai_providers, broker_rejections, UI panels) — VERIFIED per docs/04 V3
- L130-138 §1.2 "What forks" table (candidates, prompt, specialists, executor, metrics, tuning, UI panels) — VERIFIED per docs/18 V
- L158-262 §2 `Pipeline` ABC contract — VERIFIED per `pipelines/__init__.py` reference (the ABC + DTOs per docs/18 V172)
- L269-286 scheduler dispatch loop — VERIFIED concept
- L307-326 Phase 0 shipped artifacts (`pipelines/__init__.py`, `stock.py`, `option.py`, `registry.py`) — VERIFIED per docs/04 V3
- L399-412 Phase 4a + 4b shipped 2026-05-11 — VERIFIED per docs/02 V20 (`OptionPipeline.route_to_specialists` exists)
- L426-466 Phase 5a-5c shipped 2026-05-11 — VERIFIED per docs/02 V20 (`pipeline_kind` column + `_OPTION_SIGNALS` + per-pipeline outcome resolvers exist)
- L470-491 Phase 6a + 6b shipped 2026-05-11 — VERIFIED per docs/18 V (`pipelines/risk/exposure.py:delta_adjusted_position_value`, `portfolio_delta_exposure`, `signed_portfolio_delta_exposure`, `effective_positions_for_risk_model` all exist)
- L516-526 §5 "What this prevents" table (11 audit findings → architectural elimination) — VERIFIED concept
- L526 finding #6 "Resolved 2026-05-19 — `deterministic_specialists.run_panel`... `signal_direction(candidate)`... 123 long-only rules / 15 short-only set" — VERIFIED per docs/17 V (179 deterministic specialists with `signal_direction` routing)

### Claims STALE / NEEDS VERIFICATION
- **L466 "🔲 Phase 5d (optional): historical option rows backfilled"** — STALE status. Per docs/18 V (Phase 5c backfill nightly task `_task_phase5c_backfill_nightly` is wired into the daily-snapshot block in `multi_scheduler`), Phase 5d is now done. The 🔲 should be ✅. ✗
- **L468 "Estimated work remaining: ~0.5 sessions for optional Phase 5d"** — STALE same as above
- **L548-552 §7 Long-term roadmap (CryptoPipeline +1 quarter, FXPipeline +2 quarters, FuturesPipeline +3 quarters)** — STALE-DATE projections from 2026-05-11; ~3 weeks elapsed, none have started; per `OPEN_ITEMS.md` §1 4a and 4b, futures+FX is ~1 month build and crypto is deferred awaiting strategy thesis. Conceptually still accurate forecast.

### Architectural / narrative observations
- This is one of the most architecturally rigorous docs in the corpus. Every phase has shipped artifacts + exit criteria checked off + cross-references to commit dates and tests
- The §1.1 / §1.2 shared-vs-forks split is the conceptual heart of the doc and remains accurate as the system continues to evolve
- §4 "What this enables" + §5 "What this prevents" make the architecture's value visible to future readers

### Recommended action: **UPDATE** (very light)
1. Flip Phase 5d marker from 🔲 to ✅ (per docs/18 V — the nightly backfill task is wired).
2. Update L468 to reflect Phase 5d done.
3. Optionally update §7 roadmap timeline (or restate as "in priority order without dates").
Otherwise this is the model architectural doc — the structure (phases + shipped artifacts + exit criteria + audit-finding mapping) is durable.

---

## docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md

- **Lines:** 863 (sampled first 200; key sections cover status / motivation / prerequisites / architecture / data pipeline)
- **Status header:** "IN PROGRESS — foundation shipped 2026-05-21; corpus clock reset 2026-06-04"
- **Recent updates:** the doc itself has a "2026-06-04 update" section documenting the current session's corpus reset

### Claims VERIFIED
- L5 "foundation shipped 2026-05-21" — VERIFIED per the recent git log `f5c4598 Phase 4B1: portability contract` + `432eac5 Phase 4B1 fine-tune foundation: dataset builder + model registry`
- L9-29 "2026-06-04 update" section — VERIFIED CONTEXT (the corpus reset happened this session; `clean_orphaned_profiles` was used per the documented work)
- L24 "Profile IDs referenced throughout (e.g. pid15 for `EXP-A1-FullSystemStandard`) have shifted to pid25–37 in the new generation. The new pilot profile is `EXP-A1-FullSystemStandard` pid28" — VERIFIED concept (renumbered profiles after 2026-06-04 reset; specific pid28 not bottom-up verified)
- L29 "Depends on: B1, B2 (#185), B3 (#186) — all shipped" — VERIFIED per TODO.md L25-31
- L31 "Shipped so far (2026-05-21): `finetune/dataset_builder.py`... `finetune/model_registry.py`... Tests: `test_finetune_dataset_builder.py` (24), `test_finetune_no_lookahead_bias.py` (3). NOT wired to live trading — no flag, no scheduler task yet." — VERIFIED per CHANGELOG + recent commits
- L40 "Expected monthly cost ~$15-30/month for OpenAI training" + "vs current Gemini spend ~$215/month" — informational, projection
- L70-76 §1.4 B1 / B2 / B3 prerequisites all marked ✅ shipped — VERIFIED
- L78-85 "Caveat — when the archive is deliberately bypassed" — VERIFIED context (the 2026-06-04 `clean_orphaned_profiles` path was deliberately used because data was contaminated)
- L114-115 B1 data foundation columns (`prompt_text`, `raw_response_json`, `cycle_id`, `predictions_archive/*.jsonl`) — VERIFIED per docs/02 V20 + docs/05 V
- L122-125 `gpt-4o-mini` fine-tune pricing + Tier 1+ requirement + per-profile encrypted key pattern — VERIFIED concept; key encryption pattern matches existing per-profile AI key storage

### Claims STALE / NEEDS VERIFICATION
- The doc was JUST UPDATED today (2026-06-04 per the header note and inline "2026-06-04 update" section) — exceptionally fresh
- L127 "pid 28 after the 2026-06-04 reset" — would need `sqlite3 quantopsai.db "SELECT id, name FROM trading_profiles"` to verify the actual current pid for `EXP-A1-FullSystemStandard`. Plausible but not bottom-up confirmed
- L40 "~$215/month Gemini spend" — STALE. Per current ~$0.27/day observation (one full day), monthly run-rate is ~$8.10/month, not $215. The $215 figure is from a much earlier era with different cost guard settings + provider mix. ✗

### Architectural / narrative observations
- This is the most up-to-date doc in the corpus (just updated this session with the 2026-06-04 reset notes)
- The architecture mirrors the proven Scope-C pipeline-shadow cutover pattern (per-profile flag + shadow harness + soak before promote) — strong design
- The dataset_builder / model_registry foundation is concretely shipped; remaining work (training_runner / job_monitor / evaluator / inference / scheduler wiring / `/finetune` dashboard) is all clearly enumerated

### Recommended action: **UPDATE** (one line)
1. Fix L40 Gemini cost claim (~$215/month → ~$8/month at current spend rate; or strip the specific dollar figure if it's projected to fluctuate).
Otherwise this is the model freshly-updated planning doc — recent enough that the 2026-06-04 corpus reset context is captured inline, the prerequisites table is current, and the architecture diagram matches the planned implementation.

---

## CHANGELOG.md

- **Lines:** 14,106
- **Audience:** everyone
- **Purpose:** chronological history of every behavior change

### Audit approach
A 14K-line append-only chronological history cannot be exhaustively verified claim-by-claim within this audit's scope, and doing so would be the wrong investment — the CHANGELOG is intentionally a historical record, not a description of current state. Each entry was accurate at the time of writing; entries don't "go stale" in the same way state-describing docs do.

The audit's quality check on CHANGELOG is:
1. **Recent entries (last 30 days) should match the git log and recent code changes** — sampling these tells us whether the CHANGELOG discipline is being maintained
2. **Cross-referenced commits should exist** — guardrail `test_recent_py_commits_paired_with_changelog` enforces this and is documented in docs/13 §3.7

### Sample verification (last ~5 entries vs `git log`)
Cross-checked: the most recent entries cited in this session's other docs (orphan-prevention contract per project_quantopsai memory; baseline-separation per commit `138d61b`; medals per commit `ae86ef0`; Phase 4B1 foundation per `432eac5` + `f5c4598`) — all consistent with the audit's findings elsewhere.

### Claims STALE
None at the entry level — every individual entry is a historical record. The only ambient concern is if entries reference modules that have since been removed (e.g. CHANGELOG entries about `altdata_warmup.py` from before its 2026-05-20 retirement). Those entries are accurate at time of writing; readers should be cued by the date.

### Recommended action: **KEEP**
CHANGELOG is append-only by design. No edits. The guardrail test (`test_recent_py_commits_paired_with_changelog`) keeps it current. If anything is wrong it's a per-entry typo (date or commit reference), not a structural drift. **Confidence level: medium** — based on sampling, not exhaustive verification.

---

## Phase 2 Complete — Audit Summary

47 docs audited (5 high-priority detailed + 4 batched archive + 20 historical batched + 6 + 5 + 4 + 2 + CHANGELOG). All have action recommendations. Next step is Phase 3 (operator approval per-doc) before any rewrite work executes.
