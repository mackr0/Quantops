# Self-Tuner Guardrails + Case-File RAG + Specialist Library

This document covers three components of the learning loop that compound the system's edge over time without scaling the apex AI's cost:

1. **Self-tuner guardrails** — five deterministic rules that protect the autonomous parameter tuner from compounding-restriction failure modes.
2. **Case-file RAG** — pre-decision retrieval of relevant past resolved trades, injected into the AI prompt so the apex LLM sees concrete examples of how similar setups resolved before.
3. **Specialist library** — 8 LLM-narrative specialists plus 179 deterministic rule checkers. Adding to the deterministic layer is cost-free per cycle.

The self-tuner itself (12 parameter-family optimizers that adjust per-profile knobs from resolved-prediction outcomes) lives in `self_tuning.py` and is described in `docs/02_AI_SYSTEM.md` §9. This document describes the guardrails that protect it, the RAG layer that gives the apex LLM in-context memory, and the specialist library architecture.

---

## 1. Self-tuner guardrails

The autonomous tuner adjusts per-profile parameters nightly based on resolved-prediction outcomes. Without guardrails, a sequence of individually-reasonable tightenings can stack into a state where the entry filter rejects everything — many small steps in the same direction with no compensating force. The five guardrails formalize the bias-toward-confident-trading principle as deterministic code so the failure can't recur structurally.

| # | Guardrail | What it does |
|---|---|---|
| 1 | **Per-cycle delta cap** | No single cycle tightens any parameter by more than 25% of its current value. Wrapped around every numeric-parameter optimizer call site via `_apply_param_change` in `self_tuning.py`. Stops the cascade directly. |
| 2 | **Trade-count auto-loosen** | If trade count over the last 7 days falls below a floor (default: 3 stock entries), the most-restrictive parameter is FORCED to loosen by 25%. Fires before all other tuners in the registry so the loosen lands first. Implementation: `_optimize_trade_count_auto_loosen`. Tagged LOOSEN. |
| 3 | **Reference window invariant** | No parameter drifts more than ±50% from its day-1 value without operator override. The `param_references` table records the day-1 value on first observation; subsequent tuner changes are clamped to that window. Helpers: `get_param_reference`, `record_param_reference_if_absent`, `clear_param_references` in `models.py`. |
| 4 | **Auto-expiry on tightenings** | Every tuner tightening has a TTL (default 14 days). After the TTL the tightening auto-reverts in cap-bounded steps back toward the pre-tightening value, unless re-justified by recent loss evidence. Schema column: `tuning_history.expired_at`. Optimizer: `_optimize_auto_expire_old_tightenings`, tagged LOOSEN. |
| 5 | **Trade-rate anomaly alert** | If weekly trade count drops more than 50% week-over-week, an `audit_alerts` row fires for `/issues`. Pure observability — the tuner is never paused programmatically; remediation is the responsibility of the other four guardrails. Module: `trade_rate_anomaly.py`, daily scheduler task `_task_trade_rate_anomaly_check`. |

The architectural ordering: guardrail #1 prevents big single-step damage, #2 encodes "drift toward trading" as a hard rule rather than a hope, #3 puts an upper bound on cumulative drift, #4 cleans up the slow accumulation of stale restrictions, #5 surfaces the symptom when something is off. Together they make the over-restriction failure mode structurally impossible.

---

## 2. Case-file RAG over resolved trades

The apex LLM's weights are frozen at the model provider's training cutoff. To compensate, every new decision retrieves the most-similar past *resolved* cases from this profile's own history and injects them into the prompt as concrete cases-to-reason-from. The model gains experience without being retrained.

| Component | How it works |
|---|---|
| **Embedding generation** | Derived on demand from existing `ai_predictions` columns (`symbol`, `predicted_signal`, `regime_at_prediction`, `strategy_type`, `confidence`, `features_json`, `actual_outcome`, `actual_return_pct`). No schema migration; no persisted vectors. Numeric indicators (RSI, momentum, volume ratio, gap, ATR) are bucketed into stable bands so TF-IDF treats them as discrete tokens rather than unique per-row floats. |
| **Retrieval at decision time** | `case_file_rag.retrieve_similar(profile_db_path, candidate, top_n=3, min_similarity=0.15)` fits sklearn `TfidfVectorizer` on a rolling 2000-case corpus + the candidate text and returns the top-N cosine-similarity matches above the floor. Same-profile only. Returns BOTH wins and losses — filtering to warnings alone would bias the AI away from action. |
| **Prompt injection** | `ai_analyst._build_batch_prompt` calls `case_file_rag.build_prompt_block` per candidate. Output is a `SIMILAR PAST CASES FOR <SYMBOL>` block: one line per case in the form `[date] SIGNAL SYMBOL in regime → OUTCOME (return in days, sim=X)` plus an indicator-key=value sub-line. Empty corpus or missing DB yields no block; existing prompt still works. |
| **Backend choice** | TF-IDF (sklearn — already installed). Chosen over sentence-transformers because case-file text is highly-structured key=value tokens (not natural-language paraphrasing), and sentence-transformers would add ~1GB of PyTorch + model weights to the droplet. The architecture supports a later upgrade if quality measurably lags. |

The implementation lives in `case_file_rag.py` (~270 lines) and is wired into `ai_analyst._build_batch_prompt`. Per-rule exception isolation: any retrieval error (missing DB, sklearn unavailable, malformed corpus) yields an empty block and the existing prompt still works; logged at DEBUG.

---

## 3. Specialist library

The ensemble has two layers. The full per-rule catalog with each specialist's purpose lives in `docs/24_SPECIALIST_CATALOG.md`; this section describes the architecture and the cost story.

### 3.1 The two layers

| Layer | Count | What lives there |
|---|---|---|
| **LLM-narrative** (`specialists/`) | 8 | Each makes a per-cycle LLM call. Six are scoped to *synthesize* a narrative thesis from the deterministic panel's verdicts; the other two cover territory the rule library structurally can't subsume (`gamma_pin_specialist` for dealer GEX, `option_spread_risk` for option-specific risk budget enforcement). |
| **Deterministic** (`deterministic_specialists/`) | 179 | Pure-Python rule checkers. Each is a function `(candidate, ctx) → Optional[{severity, reasoning}]` with severity VETO / CAUTION / CONFIRM. Zero API cost per rule. Per-rule exception isolation prevents one bad rule from silencing the panel. |

### 3.2 The cost story

A typical industry library of deterministic signal / veto checkers runs 100–300 entries. Each entry captures a narrow pattern (e.g., "if RSI > 80 AND volume > 3× avg AND insider sold in last 30 days, veto LONG"). These rules are cheap to run (pure code, no API calls), easy to A/B test, and the library compounds — once written, a rule applies forever (assuming the signal it encodes stays real).

The split between the layers is the key cost-control choice. If all 187 specialists were LLM-narrative, the per-cycle API cost would multiply with the library size. By keeping the cheap-pattern-matching work in deterministic code and reserving LLM calls for synthesis, the per-cycle cost stays flat as the rule library grows. Steady-state observed AI spend across the 13-profile experiment fleet runs at roughly $0.30/day.

### 3.3 Routing

A deterministic rule fires only when its `APPLIES_TO_SIGNALS` tuple overlaps the candidate's signal. For stock candidates the match is direct (`BUY` → long-only rules, `SHORT` → short-only). For options the router classifies the candidate's direction via `signal_direction(candidate)` — a bullish multi-leg structure (`bull_call_spread`, `bull_put_spread`, `cash_secured_put`, `covered_call`, `long_call`) fires the same long-only rules a `BUY` would; a bearish structure (`bear_call_spread`, `bear_put_spread`, `long_put`, `protective_put`) fires the short-only set. Non-directional structures (`iron_condor`, `iron_butterfly`, `straddle`, `strangle`, `calendar_spread`) skip directional rules and are covered by the option-specific LLM specialists.

### 3.4 Coverage gaps

A handful of additional specialists would round out the library but require new upstream data feeds not currently in the pipeline:

- **Ex-dividend calendar effects** — needs a dividend-cycle calendar feed.
- **Institutional vs retail ETF flow attribution** — needs ETF flow data at a granularity the current macro feed doesn't provide.
- **Tick / quote microstructure signals** — needs the same data extension that a future microstructure model would consume (see §4.3).

These are documented as known gaps, not blockers.

---

## 4. Deferred future work

Three workstreams are planned but not yet active. Each is scoped here so that picking it up doesn't require re-deriving the plan.

### 4.1 Prompt engineering — systematic A/B testing of prompt structure

**What exists today.** Layer 6 of the self-tuner (`prompt_layout.get_verbosity`) lets the tuner set per-section verbosity to `brief` / `normal` / `detailed`. This affects per-section length only — not section ordering, not section presence, not framing wording. The tuner's adjustment space is small.

**What 4a builds.** A prompt-variant registry (`prompt_variants.py`) defining named variants of each section (e.g., `portfolio.v1_terse` vs `portfolio.v2_with_factor_table`); a per-profile A/B assignment table; outcome tracking that ties each resolved prediction back to which variants were active at decision time; a nightly learning loop that aggregates per-variant outcomes and shifts profile assignments toward winning variants. Guardrails: variants only swap when a clear lift is demonstrated over ≥50 resolved predictions; thrashing is prevented via a per-variant min-hold window.

**Why deferred.** Prompt engineering is a refinement of an already-functioning prompt. The bigger lifts come from the deterministic + RAG architecture continuing to compound. Worth doing once a month or two of post-stabilization outcome data has accumulated so the A/B tests have signal.

### 4.2 Fine-tune — train a model variant on the system's own resolved trades

**What exists today.** The apex LLM is a stock provider model. Every cycle pays the standard per-token rate; the model has no specific knowledge of this profile's history beyond what's injected via prompt context (RAG + specialist panel + track record).

**What 4b builds.** A fine-tuning dataset builder that converts resolved `ai_predictions` into training examples; a fine-tuning pipeline against an open-vendor fine-tune API; a model-version manager so production vs candidate fine-tune run in parallel with promotion gated on measured outperformance; a retraining cadence (likely weekly or monthly per profile prediction volume). Full scoping detail in `docs/20_FINETUNE_PHASE_4B1_INCREMENTAL.md`.

**Why deferred-but-active.** The dataset-builder and model-registry foundation is in place; the training loop runs once enough post-stabilization data has accumulated. The RAG layer (§2) gets a meaningful fraction of the lift fine-tuning would, at zero incremental cost; fine-tuning is the next marginal step after RAG quality is measurable.

### 4.3 Quant-ML — additional learned models beyond the meta-model

**What exists today.** `meta_model.py` is a scikit-learn `GradientBoostingClassifier` predicting P(AI was right) per candidate at decision time. `online_meta_model.py` is an `SGDClassifier` that updates incrementally on every resolved prediction. Both drive the pre-gate and the post-AI confidence re-weighting.

**What 4c would add** (candidate models, each independently scoped):

- **Regime classifier** — a learned model consuming macro features (VIX, term structure, breadth, sector dispersion, yield curve) and emitting a regime label with calibrated probabilities. Replaces the current rule-based regime tagger in `market_regime.py`. Better at regime transitions because it sees the full feature vector rather than tripping on individual thresholds.
- **Learned ranker on top of strategy votes** — trains a model on `(strategy_vote_vector, realized_outcome)` pairs to learn the optimal weighting per regime, replacing the linear composite-score formula in `multi_strategy.rank_candidates`. Captures empirical strategy interaction effects rather than equal weighting.
- **Entry-quality model** — a gradient-boosted model predicting the realized return distribution for a candidate given its full feature payload. Output drives sizing: high-confidence high-expected-return = full size, marginal = quarter size.
- **Exit-quality model** — same idea for the exit side; predicts whether the current position's exit conditions are likely to be met by the planned stop/TP, or whether the trade should be closed early. Augments `trader.check_exits`.
- **Order-flow / microstructure model** — uses tick / quote data (currently not in the pipeline; would require an upstream data extension) to detect HFT pressure, dark-pool footprints, and short-term liquidity events that affect execution.

**Why deferred.** Each candidate model is a multi-week build with its own training pipeline, calibration loop, validation, and integration plumbing. The existing meta-model layer captures a lot of what these would capture; the deterministic-specialist library provides much of the regime + ranking benefit at zero training cost. Adding models increases system complexity / failure surface — better to ship them one at a time with measured production lift, not all at once.

**Pick-up order if approved:** regime classifier first (clearest existing pain — the rule-based regime tagger occasionally mislabels transitions), then the learned ranker (highest expected per-cycle lift), then entry-quality, then exit-quality, then microstructure (gated on the upstream data extension).

### 4.4 Trigger conditions

The deferred workstreams have explicit trigger conditions — the data should argue for picking each one up rather than picking them up on speculation:

| Condition observed | Workstream to start |
|---|---|
| RAG retrieval consistently returns the same generic cases (low signal from the corpus) | 4b — fine-tune to internalize patterns RAG can't surface |
| Deterministic+LLM ensemble's CONFIRMs vs CAUTIONs are tied and the LLM consistently picks HOLD | 4a — better prompt structure to break ties |
| Profile consistently mislabels regime transitions (e.g., bull→bear caught days late) | 4c regime classifier |
| Strategy vote composite score is poorly calibrated against realized outcomes | 4c learned ranker |
| Execution slippage is materially eroding per-trade edge | 4c microstructure model + extended data pipeline |

None of these conditions are observed today.
