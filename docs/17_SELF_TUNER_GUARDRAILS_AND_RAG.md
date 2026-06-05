# Self-Tuner Guardrails + RAG over Resolved Trades

Plan for closing two known weaknesses in the learning loop:

1. **Self-tuner over-restriction failure mode** (compounded tightening → trade-rate collapse)
2. **LLM doesn't learn from outcomes** (Claude's weights are frozen — we need an in-context retrieval workaround)

## Why this matters

Per `feedback_self_tuner_must_drift_toward_trading`: default bias must be LOOSEN, restrictions need auto-expiry, "rescue scripts" indicate architectural failure. The 2026-05-14 incident (`project_self_tuner_overcorrection_2026_05_14`) showed 14 days of compounding tightening killed stock entries entirely.

Per the deep-system analysis 2026-05-18 PM: the LLM portion of the pipeline doesn't learn. Only the calibration layers (per-symbol track record, meta-model, self-tuner, learned_patterns) compound over time. The biggest single unlock for "the LLM gets smarter" is **in-context retrieval over its own resolved trades** — the AI sees specific relevant cases on each decision rather than relying on a frozen training cutoff.

## Build order

### Phase 1 — Self-tuner guardrails (defensive: close the over-restriction failure mode) — **COMPLETE 2026-05-18**

All five layers shipped in a single day (2026-05-18) atop the existing `tuning_auto_expiry.py` infrastructure. The four autonomous layers (1, 2, 3, 4) prevent and unwind over-restriction structurally; the fifth (5) surfaces the symptom so the operator knows when the autonomous systems are actively working. The tuner is never paused — remediation is entirely deterministic per `feedback_ai_driven_no_manual_loop`.

| # | Guardrail | What it prevents | Status |
|---|---|---|---|
| 1 | **Per-cycle delta cap** | Single cycle can't tighten any parameter by more than X% | **Landed 2026-05-18** — `_apply_param_change` wrapper in `self_tuning.py:136` with ±25% per-cycle cap (`_MAX_PCT_PER_CYCLE`). All ~30 numeric-parameter optimizer call sites routed through it. Helpers `_clamp_delta` and `_within_reference_window` ready for item #3 once day-1 reference persistence is added. |
| 2 | **Trade-count floor with auto-loosen** | If trade count drops below N over 7 days, the most-restrictive parameter is FORCED to loosen by Y% | **Landed 2026-05-18** — `_optimize_trade_count_auto_loosen` in `self_tuning.py`. Trigger: `<3` stock entries in last 7 days. Action: picks the entry-filter parameter with the highest restriction score from PARAM_BOUNDS, loosens it 25% (matches the Item 1 cap so it passes without further clamping), routes through `_apply_param_change` so the change appears in `tuning_history`. Tagged LOOSEN — fires FIRST in the registry. 24 new tests. |
| 3 | **Reference window invariant** | No parameter can drift more than ±50% from its day-1 value without operator override | **Landed 2026-05-18** — `param_references` table + `get_param_reference` / `record_param_reference_if_absent` / `clear_param_references` helpers in `models.py`. `_apply_param_change` now records `old_value` as the day-1 reference on first observation and consults it via the existing `_within_reference_window` helper. Both `full_reset_2026_05_18.py` and `clean_orphaned_profiles.py` wired to wipe references. 17 new tests including the original 14-cycle cascade scenario (stops at 0.05 floor vs 0.00178 without). |
| 4 | **Auto-expiry on restrictions** | Every tightening has a TTL (default 14 days). After TTL it auto-reverts unless re-justified by recent loss evidence | **Landed 2026-05-18** — `expired_at` column added to `tuning_history`; `get_expirable_tightenings` + `mark_tuning_event_expired` helpers in `models.py`; `_optimize_auto_expire_old_tightenings` in `self_tuning.py` (tagged LOOSEN). Picks the oldest unexpired tightening >14d old whose outcome isn't 'improved' and walks the parameter one cap-bounded step back toward the pre-tightening value. Marks the row expired once the value reaches the target. 28 new tests. |
| 5 | **Trade-rate anomaly alert** | If weekly trade count drops >50%, fire `/issues` alert (observability only — the tuner is NOT paused, per `feedback_ai_driven_no_manual_loop`) | **Landed 2026-05-18** — new `trade_rate_anomaly.py` module with `detect_anomaly` / `record_alert` / `resolve_alert_if_recovered` / `check_and_alert`. Wired as daily scheduler task `_task_trade_rate_anomaly_check` in `multi_scheduler.py`. Writes a stable per-profile-per-prior-week signature into the existing `audit_alerts` table so `/issues` picks it up; resolves automatically when trade rate recovers. Structural test pins that the module never mutates `enable_self_tuning` or calls `update_trading_profile` — pure observability. 17 new tests. |

Each is a small deterministic check added to the existing `self_tuning.py` decision rules. Order chosen to maximize early payoff:
- #1 stops the cascade *directly* (single biggest fix)
- #2 encodes "drift toward trading" as a *hard rule*, not a hope
- #3 is the safety belt on top of #1+#2
- #4 cleans up the accumulation of stale restrictions
- #5 gives the operator visibility when something is off

### Phase 2 — RAG over resolved trades / post-mortems — **COMPLETE 2026-05-18**

Pre-decision case-file injection into the AI prompt. The LLM doesn't learn weights but it sees specific relevant past cases on every call — effectively few-shot learning over the system's own history.

| Component | Approach (as shipped) |
|---|---|
| **Embedding generation** | Derived ON DEMAND from existing `ai_predictions` columns (`symbol`, `predicted_signal`, `regime_at_prediction`, `strategy_type`, `confidence`, `features_json`, `actual_outcome`, `actual_return_pct`). No schema migration needed; no persisted vectors. Numeric features (RSI, momentum, volume ratio, gap, ATR) bucketed into stable bands so TF-IDF treats them as discrete tokens. |
| **Retrieval at decision time** | `case_file_rag.retrieve_similar` fits TF-IDF on the rolling-window corpus + candidate text (sklearn — already installed; no new deps), returns top-N above a 0.15 cosine-similarity floor. Same-profile only by default. Returns BOTH wins and losses per `feedback_self_tuner_must_drift_toward_trading` (filtering to warnings would bias away from action). |
| **Prompt injection** | `_build_batch_prompt` in `ai_analyst.py` calls `build_prompt_block` per candidate. Outputs a "SIMILAR PAST CASES" block: each line is `[date] SIGNAL SYMBOL in regime → OUTCOME (return in days, sim=X)` plus an indicator-key=value sub-line. Fail-soft — empty corpus or missing DB yields no block, the existing prompt still works. |
| **Embedding backend** | TF-IDF (sklearn). Chosen over sentence-transformers because: (1) case files are highly structured token sequences, not natural language paraphrasing; (2) no PyTorch / no 1GB+ disk cost on the droplet; (3) deterministic + fast (no model load). Can be upgraded to sentence-transformers later if quality measurably lags. |

Implementation: `case_file_rag.py` (270 lines), wired into `ai_analyst.py:_build_batch_prompt`. 22 new tests covering the text builder, retrieval ranking + thresholds, win/loss balance, format rendering, and the prompt-builder integration.

### Phase 3 (IN PROGRESS) — Specialist library expansion: 8 → 200

**2026-05-18 update.** First Phase-3 batch landed. Original framing was "wait for post-mortems to surface patterns" — that's the CALIBRATION mechanism, not the discovery mechanism. Most quant patterns are well-documented in the literature and don't need a losing trade to teach us. Discovery is now aggressive.

**Architecture decision.** The 192 new specialists are NOT LLM-narrative specialists (that would 25× the per-cycle AI cost). They're **deterministic code-only rule checkers** in a new `deterministic_specialists/` directory:

  - Each rule = pure function `(candidate, ctx) → Optional[{severity, reasoning}]`
  - Severities: `VETO` (high-confidence stop), `CAUTION` (yellow flag), `CONFIRM` (pattern supports the signal)
  - Zero per-rule API cost — runs as a panel block injected into the prompt, weighed by the LLM
  - Registered in `RULE_MODULES`; each gated by `APPLIES_TO_SIGNALS`. Routing: stock candidates direct-match by signal; OPTIONS/MULTILEG_OPEN candidates route to same-direction rules via `signal_direction(candidate)` (added 2026-05-19) — a bullish option strategy (`long_call`, `bull_call_spread`, `bull_put_spread`, `cash_secured_put`, `covered_call`) fires the same long-only rules a `BUY` would, and bearish strategies fire the short-only set. Neutral strategies (`iron_condor`, etc.) skip directional rules — they're covered by `gamma_pin_specialist` / `iv_skew_specialist` / `option_spread_risk` in the LLM layer.

**Status after second batch (2026-05-18, same day):**
- **179 deterministic specialists** in `deterministic_specialists/`
- Plus the **8 LLM-narrative specialists** from `specialists/` = **187 total specialists** in the live ensemble
- Up from 8 at session start. The original "Year 1: 150-200" projection achieved in a single day — because most quant patterns are documented in literature; "wait for losses" was only the *calibration* mechanism, never the *discovery* one.

Categories shipped in the first batch:
- Late-stage / extended pattern warnings (RSI overbought + 52w high, parabolic blow-off, gap-into-resistance, bearish divergence, VWAP extension, MFI overbought, CMF distribution)
- Breakout / momentum quality (volume-dry breakout, low-ATR breakout, weak-ADX breakout)
- Smart-money + crowding (insider sold, high SI, crowded long, StockTwits euphoria, FINRA short vol)
- Smart-money + flow confirms (insider cluster buying, 13D activist, dark-pool accumulation, congressional buying, UOA aligned, StockTwits capitulation)
- Earnings / analyst momentum (EPS up-revisions, down-revisions, beat streak, miss streak, in-window earnings)
- Regulatory / corporate-event (8-K Items 1.03/4.02/2.06, 8-K Item 5.02, risk-factor diff additions, FDA citations, NHTSA recalls, SEC HIGH/CRITICAL alerts)
- Trend / pattern confirms (strong ADX, RSI oversold in uptrend, 3×+ volume confirm, sector RS, sector weakness, sector downtrend long, CMF accumulation, MFI oversold, near Fib support, TTM-squeeze release, ORB breakout)
- Short-side specific (extended below VWAP, high borrow cost, HIGH squeeze risk)
- Macro / volatility regime (IV extreme high, cross-asset vol high, yield curve inverted, CBOE SKEW extreme)
- Execution / friction (high slippage, news cluster without parsed SEC catalyst)

| Phase | Specialist count | Source of new specialists |
|---|---|---|
| Session start (2026-05-18) | 8 | Initial LLM ensemble |
| First batch (2026-05-18) | 60 | Phase 3 framework + 44 deterministic rules |
| Second batch (2026-05-18) | 109 | +49 rules — trend/momentum, gap, microstructure, attention, smart-money, fundamentals, options, macro, 8-K, calendar |
| Third batch (2026-05-18) | 155 | +46 rules — factor signals (momentum/quality/low-vol), oscillator confluence, Bollinger walks, round-number psychology, sentiment depth, macro detail (oil/treasury/gold vol), short-side complements, options flow detail, catalyst stacking, intraday flow, wash-cycle |
| PM audit cleanup (2026-05-18) | 151 | −4 noisy wall-clock CAUTIONs (`monday_morning_open`, `last_30_min_session`, `first_5_min_session`, `friday_close_caution`) dropped + 5 CAUTIONs tightened thresholds to fix structural anti-action bias caught by user audit. Severity mix now 9V/67C/67C (balanced). |
| Candlestick batch (2026-05-18 PM) | 167 | +16 candlestick-pattern rules (`candle_*`). Uses OHLC of the last 3 bars surfaced via `trade_pipeline._get_latest_indicators` → `candidate["candle"]`. Zero new API calls. |
| Market-context + portfolio batch (2026-05-18 PM) | **187** | +20 rules consuming `candidate["_market_context"]` (regime, vix, spy_trend, sector_rotation, crisis_context, macro_event_block) + `candidate["_portfolio"]` (positions, drawdown_pct) — both stashed by `ai_analyst._build_batch_prompt` before the panel runs. Categories: regime alignment, VIX bands, SPY trend, crisis state, macro events, sector rotation, portfolio concentration / drawdown. |
| Final stretch | 200 | Remaining ~13 require dedicated new data feeds — ex-div calendar (dividend-cycle effects), ETF flow data (institutional-vs-retail flows), tick / quote microstructure (HFT pressure, dark-pool footprints — same data extension Phase 4c microstructure model would need). Not single-session work. |

**Current state (2026-05-18 EOD)**: **187 specialists** in the live ensemble.

| Layer | Count | Notes |
|---|---|---|
| LLM-narrative (`specialists/`) | 8 | 6 re-scoped 2026-05-18 PM to synthesize from the deterministic panel rather than re-derive facts. `gamma_pin_specialist` + `option_spread_risk` kept as-is (unique territory the rule library can't subsume). |
| Deterministic (`deterministic_specialists/`) | 179 | Pure-Python rule checkers. Severities: VETO / CAUTION / CONFIRM. Mix: 10 VETO / ~88 CAUTION / ~81 CONFIRM. Each rule gated by `APPLIES_TO_SIGNALS`. Per-rule exception isolation prevents one bad rule from silencing the panel. Rules can read market context + portfolio via `candidate["_market_context"]` / `candidate["_portfolio"]` stashed by `ai_analyst._build_batch_prompt`. |

**Target state**: 200 specialists.

**Why 200 not 50**: quant funds typically run libraries of 100-300 deterministic signal/veto checkers. Each one captures a narrow pattern (e.g., "if RSI > 80 AND volume > 3× avg AND insider sold in last 30 days, veto LONG"). They're cheap to run (pure code, no API calls), easy to A/B test, and the library compounds — once written, a specialist works forever (assuming the signal it captures is real). 200 gives enough coverage of failure modes that the AI's narrative-reasoning layer rarely flies blind.

**Growth path**:

| Phase | Specialist count | Source of new specialists |
|---|---|---|
| Today | 8 | Initial build |
| Month 1 | 15-20 | Patterns surfacing from first month's resolved trades (each significant losing pattern → 1 specialist) |
| Month 3 | 40-60 | Add specialists for: each major sector regime, each event-type (earnings, M&A, restatements), each volatility regime, each macro context |
| Month 6 | 100-120 | Cross-asset specialists (bond yields → equity sectors), seasonality, cross-listing arbitrage, options-vs-equity divergence, etc. |
| Year 1 | 150-200 | Pattern library complete; ongoing maintenance + decay-replacement |

**Cadence**: ~1 specialist per day of focused work, but realistically batched: 5-10 specialists per week as patterns from resolved-trade post-mortems accumulate. Significant losing-trade patterns are the primary feed — each post-mortem that identifies a recurring trap becomes a candidate specialist.

**Operational consequence**: as the library grows, the AI's role shifts from "decider" to "tie-breaker." With 200 specialists, most candidates will be unambiguous (clear majority pattern). The LLM only resolves the genuinely-contested cases. Cost per cycle DROPS because most decisions short-circuit before the LLM call.

### Phase 4 (4a + 4c deferred; 4b STARTED 2026-05-21)

Three workstreams originally deferred past 2026-05-18's build. **Phase 4b (fine-tune)** has since started — the dataset_builder + model_registry foundation shipped 2026-05-21 and the corpus clock reset 2026-06-04 (per `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md`). First training run gated on data accumulation (~early-to-mid August 2026 per `docs/20` §17). **Phases 4a and 4c remain deferred.**

Each sub-section below retains its full scope description so the next session can pick up cleanly.

#### 4a. Prompt engineering — systematic A/B testing of prompt structure

**What we have today.** Layer 6 of the self-tuner (`prompt_layout.get_verbosity`) lets the tuner set per-section verbosity to `brief` / `normal` / `detailed`. This is COARSE — only affects per-section length, not section ordering, not section presence, not framing wording. The tuner adjusts based on post-mortem outcomes, but the adjustment space is small.

**What Phase 4a builds.**
- A prompt-variant registry (`prompt_variants.py`) defining named variants of each section (e.g., `portfolio.v1_terse` vs `portfolio.v2_with_factor_table`).
- A per-profile A/B assignment table — which variants this profile is currently using.
- Outcome tracking that ties each resolved prediction back to which variants were active at decision time.
- A learning loop (probably nightly) that aggregates per-variant outcomes and shifts profile assignments toward winning variants. Same shape as the existing meta-model retraining cadence.
- Guardrails: variants only swap when a clear lift is demonstrated over a horizon of ≥50 resolved predictions; thrashing is prevented via a min-hold window per variant assignment.

**Why deferred.** Today's CHANGELOG-level moves (Phase 1+2+3) are bigger expected lift. Prompt engineering is a refinement of an already-functioning prompt; worth doing after the deterministic+RAG architecture has accumulated a month+ of outcome data so the A/B tests have signal.

**Scope estimate.** Substantial — ~1 week of focused work including the variants library, the assignment table, the outcome attribution, the learning loop, and the tests. Not a single-session task.

#### 4b. Fine-tune — train a model variant on the system's own resolved trades

**What we have today.** The LLM is a stock Anthropic model (`claude-haiku-4-5-20251001` by default). Every cycle pays the standard per-token rate; the model has no specific knowledge of this profile's history beyond what we inject via prompt context (RAG + specialist panel + track record).

**What Phase 4b builds.**
- A fine-tuning dataset builder that converts resolved `ai_predictions` into training examples (input = candidate context at decision time, output = chosen action, label = realized outcome).
- A fine-tuning pipeline (Anthropic's fine-tune API or comparable). Real per-cycle inference cost goes UP (fine-tuned model rates are higher) while quality should go UP more.
- A model-version manager — current production model vs candidate fine-tune in parallel — so promotion is gated on measured outperformance.
- A retraining cadence (probably weekly or monthly depending on per-profile prediction volume).

**Why deferred.** (1) Real money commitment — fine-tuning API time + higher per-token rates. (2) Requires sufficient training data per profile — at typical AI-call volumes (~20-50 decisions/day) it takes weeks-to-month to accumulate enough resolved cases for a meaningful train. (3) The RAG layer (Phase 2) gets ~70% of the benefit at zero incremental cost; fine-tuning is the marginal next step *after* RAG is producing measurable lift.

**Scope estimate.** Substantial — ~2-3 weeks of focused work for the dataset pipeline + fine-tune integration + version management. Plus ongoing operational cost.

#### 4c. Quant-ML — additional learned models beyond the meta-model

**Clarification (per Mack's question 2026-05-18):** Quant-ML is NOT a product to buy. In this context it means us building additional learned models that complement or extend the AI ensemble — same architectural shape as the GBM + SGD layers we already run in `meta_model.py` and `online_meta_model.py`, just more of them.

**What we have today.**
- `meta_model.py` — scikit-learn `GradientBoostingClassifier` predicting P(AI was right) per candidate at decision time. Trained nightly on resolved predictions. Drives the pre-gate (drops sub-0.5 candidates) and re-weights AI confidence at execution.
- `online_meta_model.py` — `SGDClassifier` that updates incrementally on every resolved prediction. Catches regime drift faster than the nightly GBM. Bootstrapped from the GBM's training set.

**What Phase 4c would add (candidate models, each independently scoped).**
- **Regime classifier** — a learned model that consumes macro features (VIX, term structure, breadth, sector dispersion, yield curve) and emits a regime label with calibrated probabilities. Replaces the current rule-based regime tagger in `market_regime.py`. Better at handling regime transitions because it sees the full feature vector rather than tripping on individual thresholds.
- **Learned ranker on top of strategy votes** — instead of the linear composite-score formula in `multi_strategy.rank_candidates`, train a model on `(strategy_vote_vector, realized_outcome)` pairs to learn the optimal weighting per regime. Captures empirical strategy interaction effects rather than equal weighting.
- **Entry-quality model** — a gradient-boosted model predicting the realized return distribution for a candidate given its full feature payload. Output drives sizing: high-confidence high-expected-return = full size, marginal = quarter size. Replaces the conviction-based sizing heuristic.
- **Exit-quality model** — same idea but for the exit side: predicts whether the current position's exit conditions are likely to be met by the planned stop/TP, or whether the trade should be closed early. Augments `trader.check_exits`.
- **Order-flow / microstructure model** — uses tick / quote data (currently not in the pipeline; would require an upstream data extension) to detect HFT pressure, dark-pool footprints, and short-term liquidity events that affect execution. Drives venue selection and order timing.

**Why deferred.** (1) Each candidate model is itself a multi-week build with its own training pipeline, calibration loop, validation, and integration plumbing. (2) The existing meta-model layer captures a lot of what these would capture — the marginal lift per new model decreases. (3) The deterministic-specialist library (Phase 3) provides much of the regime + ranking benefit at zero training cost. (4) Adding models increases system complexity / failure surface — better to ship them one at a time with measured production lift, not all at once.

**Recommended pick-up order if/when Phase 4c is approved:** start with the regime classifier (clearest existing pain — the rule-based regime tagger occasionally mislabels transitions), then the learned ranker (highest expected per-cycle lift), then entry-quality, then exit-quality, then microstructure (requires data-pipeline extension that's a project of its own).

**Scope estimate.** Each candidate model is ~2-3 weeks. The full Phase 4c is a multi-month effort. **Not a product to buy** — pure internal model development using the same scikit-learn / pandas / numpy stack already in production.

---

### Phase 4 — go/no-go decision criteria

The trigger conditions for picking up Phase 4 work, ordered by likely first-needed:

| Condition | If observed → kick off |
|---|---|
| RAG retrieval consistently returns the same generic cases (low signal from the corpus) | Phase 4b — fine-tune to internalize patterns RAG can't surface |
| Deterministic+LLM ensemble's CONFIRMs vs CAUTIONs are tied and the LLM is consistently picking HOLD | Phase 4a — better prompt structure to break ties |
| Profile is consistently mislabeling regime transitions (e.g., bull→bear catches days late) | Phase 4c (regime classifier first) |
| Strategy vote composite score is poorly calibrated against realized outcomes | Phase 4c (learned ranker) |
| Execution slippage is materially eroding per-trade edge | Phase 4c (microstructure model + extended data pipeline) |

None of these conditions are observed today. Phase 4 stays parked until the data argues for it.

## Test plan per phase

- Phase 1 fixes: unit tests for the cap / auto-loosen / reference-window logic against synthetic tuning_history sequences; one integration test that simulates 14 days of compounding tightening and asserts the cascade is broken
- Phase 2 RAG: unit test that retrieval returns top-N matches; one integration test that AI prompt includes case files; before/after measurement of decision quality is a longer-horizon evaluation, not a deploy-time test

## Deploy + ops

- Every commit: `./sync.sh` + full test suite (4,561+ passing as of 2026-06-04)
- After phase 1 lands: monitor `tuning_history` table for restriction events; verify auto-loosen fires when synthetic conditions are met
- After phase 2 lands: monitor `cycle_data_*.json` shortlist entries for the new case-file field
