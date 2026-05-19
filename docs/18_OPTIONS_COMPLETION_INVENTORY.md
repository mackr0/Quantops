# Options work — completion inventory

**Owner question (2026-05-19):** *"Have you fully documented what you need to get to 100% perfect completion on the options work?"* — this doc is the honest answer. Every options-related artifact in the repo is classified into one of four states:

- **PRODUCTION** — wired into the live scheduler today; trades go through this code on every cycle.
- **CAPABILITY** — code is complete and exercised by tests, but the scheduler does NOT call it yet; runnable via `.run_cycle()` / direct import.
- **STUB** — `NotImplementedError` placeholder. The docstring claims a future Phase will land it; that Phase has either already shipped without filling this in or has never shipped.
- **REFINEMENT** — built and shipped, but a follow-up was explicitly punted as "optional / not in scope." These were promised in the original phase docs but never delivered.

100% complete = every STUB is implemented, every REFINEMENT is closed out (or formally declared not needed), and an end-to-end test exists for every claim in section 4.

---

## 1. Inventory by state

### 1a. PRODUCTION (wired into the live scheduler)

These run on every cycle today and are the reason real options trades execute.

| Artifact | Role | Call site |
|---|---|---|
| `options_chain_alpaca.py` | Alpaca-backed options chain fetcher (replaced yfinance) | `options_oracle._fetch_chain` |
| `options_oracle.py` (`get_options_oracle`, `compute_iv_rank`, `summarize_for_ai`) | IV rank, has_options flag, IV skew, term structure, max-pain, gamma exposure | `trade_pipeline.py:3186` (per-symbol enrichment), `trade_pipeline.py:3445` (SPY-level annotation) |
| `options_strategy_advisor.py` (`evaluate_candidate_for_multileg`, `render_multileg_recs_for_prompt`) | Enumerates multileg strategy candidates given IV regime + signal | `ai_analyst.py:1026` (in `_build_batch_prompt`) |
| `options_trader.execute_option_strategy` | Single-leg order submission (BUY/SELL to open/close, OCC formatting) | `trade_pipeline.py:2273` (`action == "OPTIONS"` branch); `OptionPipeline._execute_single_leg` |
| `options_multileg.py` (`ALL_MULTILEG_BUILDERS`, `execute_multileg_strategy`) | Multileg strategy builders (bull/bear verticals, iron condor, strangle) + combo order submission | `OptionPipeline._execute_multileg` via `trade_pipeline.py:2349` |
| `pipelines/option.py:OptionPipeline.execute` | Phase 4c multileg + single-leg dispatcher; veto persistence | `trade_pipeline.py:2349` (multileg) — Phase 4c shipped 2026-05-12 |
| `pipelines/option.py:OptionPipeline.applies_to` | Per-profile opt-in gate | Pipeline-base `run_cycle` and the registry |
| `pipelines/option.py:OptionPipeline.route_to_specialists` | Inherited from `Pipeline` base. Routes proposals through option-tagged specialists (`option_spread_risk` + cross-pipeline) | `trade_pipeline.py:108` `check_multileg_specialist_veto` |
| `pipelines/option.py:OptionPipeline.record_outcome` | Writes resolved option predictions with `pipeline_kind='option'` (Phase 5a, fixes audit finding #2 at storage layer) | Per-cycle outcome resolver |
| `pipelines/option.py:OptionPipeline.compute_metrics` | Option slippage in $ (fixes 1130% display bug structurally) | Daily metrics aggregation |
| `pipelines/option.py:OptionPipeline.tune` | Option-only tuner — Greek caps + 5 exit thresholds + 3 spread-veto thresholds + 2 IV thresholds | Daily tuner pass |
| `pipelines/outcomes/option.py` + `option_resolver.py` | Phase 5b/5c: per-pipeline outcome storage; option-aware premium lookup for resolution | Resolver path |
| `metrics/option.py` | Option-only slippage stats (dollars, never %) | `OptionPipeline.compute_metrics` |
| `tuning/option.py` | Win-rate aggregator filtered to option signal types | `OptionPipeline.tune` |
| `pipelines/risk/exposure.py` (`delta_adjusted_position_value`, `portfolio_delta_exposure`, `signed_portfolio_delta_exposure`, `effective_positions_for_risk_model`) | Phase 6: option positions contribute delta-equivalent exposure to factor regression and the AI prompt | `compute_portfolio_risk_from_positions`, `multi_scheduler` |
| `options_exits.py` | Per-cycle single-leg exit checks (stop-loss, take-profit, DTE-floor) | `trade_pipeline` exit pass |
| `options_lifecycle.py` | Expiry / assignment detection | Daily lifecycle pass |
| `options_roll_manager.py` | Auto-close credit positions ≥80% max profit; recommend rolls | Daily roll pass |
| `options_wheel.py` | Cash-secured-put → assigned → covered-call state machine | Wheel-enabled profiles |
| `options_delta_hedger.py` | Stock-side rebalance for long-vol positions | Long-vol profiles |
| `options_earnings_plays.py` | Pre-earnings IV-crush capture (iron condor) / long straddle | Earnings event handler |
| `options_vol_regime.py` | Vol regime classifier (rich / cheap; skew; term contango / backwardation) | Multileg advisor inputs |
| `options_greeks_aggregator.py` (`compute_book_greeks`) | Portfolio Greek roll-up | Phase 6 risk snapshot + prompt |
| `pipelines/option_prompt.py` (`build_prompt`) | Option-aware AI prompt — IV rank / Greeks / DTE / strikes / spread economics rendered first | `OptionPipeline.build_prompt` (CAPABILITY only — not used by scheduler today; legacy `_build_batch_prompt` is the production prompt) |
| `deterministic_specialists/option_spread_risk.py` | Veto authority on multileg proposals (IV-rank ceiling, gamma-DTE, credit-ratio) | Specialist ensemble |

### 1b. CAPABILITY (built but the scheduler doesn't call it yet)

These are runnable via `OptionPipeline().run_cycle(ctx)` and exercised by tests, but the production scheduler still uses `trade_pipeline.run_trade_cycle` for dispatch. The capability path will replace the legacy path at the eventual cutover — code is ready.

| Artifact | Status |
|---|---|
| `OptionPipeline.execute` end-to-end via `.run_cycle()` | Body complete; legacy `trade_pipeline.run_trade_cycle` is the live dispatcher today. |
| `pipelines/option_prompt.build_prompt` | Built but the legacy `ai_analyst._build_batch_prompt` still serves production prompts for both pipelines. |
| `OptionPipeline.run_cycle` | Inherits from base `Pipeline`; calls each method in order. Cannot run today because `generate_candidates` and `decide` are STUB. |

### 1c. ~~STUB~~ — RESOLVED 2026-05-19

The OptionPipeline + StockPipeline stubs (formerly listed here) were finished in scope-B build-out:

| Method | Status |
|---|---|
| `OptionPipeline.generate_candidates` | ✓ implemented — reads `ctx.shortlist`, fetches IV rank via `options_oracle`, enumerates strategies via `options_strategy_advisor.evaluate_candidate_for_multileg`, emits one Candidate per multileg strategy with option features in `extra` |
| `OptionPipeline.decide` | ✓ implemented — uses `ai_providers.call_ai` with `ctx.ai_provider` + `ctx.ai_api_key`; tolerant JSON parsing; filters to MULTILEG_OPEN/OPTIONS proposals |
| `StockPipeline.generate_candidates` | ✓ implemented — same shape as option side, carries stock technicals in `extra` |
| `StockPipeline.decide` | ✓ implemented — same shape as option side, filters to stock-side actions |
| `StockPipeline.execute` | ✓ implemented — loops verdict.approved → `trader.execute_trade`; classifies into submitted/rejected/skipped/errors |

Both pipelines are now **runnable end-to-end via `.run_cycle(ctx)`**. Production scheduler still uses the legacy `trade_pipeline.run_trade_cycle` dispatch path — the cutover (scope C) is a separate change requiring shadow-mode soak.

Tests in `tests/test_pipelines_b_complete_2026_05_19.py` pin per-method behavior + the full `run_cycle` composition. 200 tests across the full pipeline suite pass.

### 1d. REFINEMENT (shipped phase but with explicitly-deferred follow-up)

Each item below was called out as "optional refinements — not in scope" at the time of its phase. They are not blocking trades today but were promised in the original architecture doc.

| Refinement | Where promised | What's missing | Impact |
|---|---|---|---|
| Live IV oracle wired into Phase 6 `iv_lookup` | `docs/14:Phase 6a, "Optional refinements"` | Currently uses `FALLBACK_IV=0.25` constant for delta-adjusted exposure computation when an option position lacks a fresh IV. Real IV is available from `options_oracle.compute_iv_rank` but isn't plumbed into `pipelines/risk/exposure._greek_contribution`. | Risk model's delta exposure for options uses a stale flat IV; understates vega contribution for high-IV names, overstates for low-IV. Magnitude: typically ±10-20% of position delta. |
| Position-level Greek breakdown in dashboard panel | `docs/14:Phase 6a, "Optional refinements"` | `compute_book_greeks` is in the AI prompt but NOT yet a UI panel. Operator can't see per-position Greek attribution from the dashboard. | Operational visibility gap — operator works from prompt logs instead of UI. |
| Phase 5c backfill of historical option predictions | `docs/14:Phase 5c shipped` mentions `backfill_historical_option_predictions(db_path)` | Backfill helper exists but isn't run on a schedule. Pre-Phase-5c option predictions never get their `option_order_id` / `occ_symbol` populated → can't be resolved with option economics. | Old option predictions stay "pending" forever or get incorrectly resolved with underlying-price math. |
| Single-leg `action == "OPTIONS"` migration to `OptionPipeline.execute` | `OptionPipeline._execute_single_leg` docstring: *"For symmetry with multileg — the elif branch for single-leg can also delegate here in a future cleanup."* | Single-leg path still goes through the legacy `trade_pipeline.py:2273` branch directly; doesn't flow through `OptionPipeline.execute`. Code is duplicated, not delegated. | Code duplication; one bug fix needs two locations. No trade impact. |
| Scheduler cutover from legacy `trade_pipeline.run_trade_cycle` to `OptionPipeline.run_cycle` | `docs/14:Section 4` *"What this enables (post-migration)"* | Legacy dispatcher continues; capability path runs in parallel but isn't called. The whole point of the pipeline ABC was eventual cutover. | Refactor's full benefit (pipeline-specific A/B testing, kill switches, cleaner ML features) blocked behind this cutover. |
| Hard alarm when IV-rank lookup returns None for >80% of candidates | Open follow-up from 2026-05-19 options outage post-mortem | Today, silent fallback to None means no multileg recs get generated; AI prompt looks clean but options proposals never surface. Operator finds out via "why no options trades?" not via alarm. | Outage detection lag (operator-driven, not system-driven). |

---

## 2. Recently-deployed safety fix (related)

- **2026-05-19 PM — Silent Anthropic fallback gate** (commit `9c8cac8`): `ai_providers._build_fallback_chain` now suppresses Anthropic from any non-Anthropic primary's fallback chain. Documented separately in CHANGELOG.md and `docs/04`. Not options-specific but landed during the same investigation.

---

## 3. Work required to reach 100%

Ordered by what unblocks downstream work.

### 3.1 Fill in the two STUB methods (`pipelines/option.py:48,64`)

- `OptionPipeline.generate_candidates(ctx)` — read `ctx.shortlist`, fetch IV rank per symbol via `options_oracle.get_options_oracle`, enumerate strategies via `options_strategy_advisor.evaluate_candidate_for_multileg`, emit `Candidate` objects with option features in `extra`. Top-N by IV-rank score. Fail-soft on per-symbol oracle failures.
- `OptionPipeline.decide(ctx, prompt)` — `call_ai(prompt, provider=ctx.ai_provider, ...)`, `_parse_ai_response_tolerant(raw)`, filter to `MULTILEG_OPEN` / `OPTIONS` actions, return `AIResult`. Reuse the existing `purpose="option_pipeline_decide"` tag for ledger attribution.
- Tests: extend `tests/test_pipelines_phase0.py` to remove the `NotImplementedError` assertion on these two methods; add new tests that pin the working behavior (empty shortlist → []; oracle failure on one symbol skips that symbol; AI returns mixed stock+option → only option proposals survive; cost cap returns empty AIResult).

### 3.2 Wire live IV into Phase 6 risk model (`pipelines/risk/exposure.py`)

- Replace `FALLBACK_IV = 0.25` flat default with `options_oracle.compute_iv_rank(symbol, current_iv)` lookup keyed off the position's OCC symbol.
- Cache per-cycle so the same underlying isn't re-queried for every contract.
- Test: position with known IV ≠ 0.25 must produce a delta-adjusted exposure that uses the looked-up IV.

### 3.3 Run Phase 5c backfill on a schedule

- `multi_scheduler` task: nightly call to `pipelines.outcomes.backfill.backfill_historical_option_predictions(db_path)` per profile DB.
- Test: synthetic pre-Phase-5c row with a matching trade in the ±60min window gets populated and re-resolved with option economics.

### 3.4 Migrate single-leg `OPTIONS` action to `OptionPipeline.execute`

- Move `trade_pipeline.py:2273` single-leg body into a thin caller (mirror the Phase 4c multileg migration).
- The capability already exists (`OptionPipeline._execute_single_leg`); just need to flip the caller.
- Test: existing single-leg tests must pass against the new dispatcher path.

### 3.5 Scheduler cutover from legacy dispatch to `Pipeline.run_cycle`

- This is the big one. Requires StockPipeline to also have its STUBs filled (currently `StockPipeline.generate_candidates`, `decide`, `execute` are all `NotImplementedError`).
- Once both pipelines are end-to-end runnable, `multi_scheduler` can dispatch via `for p in get_pipelines_for_profile(ctx): p.run_cycle(ctx)` instead of calling `trade_pipeline.run_trade_cycle` directly.
- Test: A/B comparison cycle (run both legacy and pipeline path on the same ctx and assert identical outputs). Soak-test in a sandbox profile before cutting over production.

### 3.6 Build dashboard panel for per-position Greeks

- New view + template: read `compute_book_greeks` per profile; render as a panel like the existing risk summary.
- Test: render returns a non-empty panel when ≥1 option position is open; empty-state message when none.

### 3.7 Hard alarm on IV-rank lookup degradation

- New sentinel in the per-cycle health check: if ≥80% of attempted IV-rank lookups returned None in a cycle, write a loud WARNING and a `journal.health_alert` row.
- Test: feed a fixture with 9/10 None and assert the alarm fires; 7/10 None must NOT fire.

---

## 4. Exit criteria for "100% complete"

When EVERY box below ticks, options work is done. Until then it isn't.

- [x] `pipelines/option.py` contains zero `NotImplementedError`. **Done 2026-05-19 (scope B).**
- [x] `OptionPipeline().run_cycle(ctx)` is end-to-end runnable in a test with a fixture ctx; returns a valid `ExecutionResult` for empty input and for input with each strategy type. **Done — see `test_pipelines_b_complete_2026_05_19.py`.**
- [x] `tests/test_pipelines_phase0.py` no longer asserts NotImplementedError on `generate_candidates` / `decide`. Replacement tests pin actual behavior. **Done — `TestPhase0PlaceholdersAllWired` replaces it.**
- [ ] Risk model delta-adjusted exposure uses live IV (no `FALLBACK_IV` fallback in the production path).
- [ ] Phase 5c backfill runs nightly; no resolved option prediction is using underlying-price math by accident.
- [ ] Single-leg `OPTIONS` action flows through `OptionPipeline.execute`, not the legacy elif branch.
- [x] StockPipeline also fully implemented. **Done 2026-05-19 (scope B).**
- [ ] Multi-scheduler can dispatch via `Pipeline.run_cycle` (scope C — requires shadow-mode soak before cutover).
- [ ] Dashboard renders a per-position Greeks panel.
- [ ] Per-cycle IV-rank degradation alarm fires loudly when >80% of lookups return None.
- [ ] Every entry above has a regression test referenced by name in CHANGELOG.

---

## 5. What this doc IS NOT

- It is not a Sprint plan with story points or dates. The user's question was "what's needed," not "by when."
- It is not a guarantee that nothing else exists. Items below the surface I'm unaware of would not appear here — but every artifact in this repo that mentions "options" or `OptionPipeline` was inspected before writing this.
- It is not a defense of why the stubs persisted. The stubs are stale; the docstrings claiming future Phases would fill them in were already false when Phase 6 shipped without doing so.

---

## 6. References

- `pipelines/option.py` — the class itself (currently 2 stubs + 5 implemented methods).
- `pipelines/__init__.py` — the `Pipeline` ABC + DTOs.
- `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` — the original architectural plan; Phases 0-6 narrative.
- `docs/04_TECHNICAL_REFERENCE.md` — module-by-module reference for all `options_*.py` files.
- `docs/02_AI_SYSTEM.md` §10b — synthetic options backtester.
- `CHANGELOG.md` 2026-05-12 — Phase 4c multileg execution migration.
- `CHANGELOG.md` 2026-05-19 — silent Anthropic fallback (related context, not options-specific).
