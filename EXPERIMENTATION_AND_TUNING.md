# Experimentation & Tuning — How the System Learns From Itself

**Audience:** technical partners, operators, future contributors.
**Goal:** explain end-to-end how QuantOpsAI auto-experiments with strategies and parameters and how those experiments turn into permanent improvements — without any human in the loop.

This is the single document for "what makes this system special."

---

## 1. The headline

QuantOpsAI is not just an AI that picks trades. It is an AI **plus seven feedback loops grinding on its own outcomes**, all running daily, all gated by an explicit cost ceiling, all anti-regressed by structural tests so a future change cannot silently break the integrity of what's measured.

Concretely: every prediction the AI makes is labeled with the full feature snapshot the AI saw at decision time. Every prediction that resolves is labeled with what actually happened. Seven loops feed off that growing dataset:

| # | Loop | What it learns | Frequency |
|---|---|---|---|
| 1 | Meta-model | "When is the AI likely to be correct, given the features it saw?" | Daily retrain |
| 2 | Self-tuner | "Should we raise the confidence threshold? Tighten stops?" | Daily |
| 3 | 12-layer autonomy stack | 35+ parameters + signal weights + regime/ToD/symbol overrides + AI model + capital scale | Daily |
| 4 | Alpha-decay detector | "Has any strategy's rolling Sharpe degraded vs lifetime?" | Daily |
| 5 | Specialist calibration | "What does an `earnings_analyst BUY 78%` actually mean for THIS profile?" | Daily |
| 6 | Strategy auto-generation | AI proposes a new strategy variant → validate → shadow → promote | Weekly |
| 7 | Post-mortems on losing weeks | "What patterns are common in last week's losers?" | Weekly |

All seven feed back into the next decision the AI makes. The system in 6 months is not the same system that's running today — by design.

---

## 2. The closed loop, in one diagram

```
       ┌────────────────────────────────────────────────────────┐
       │              QuantOpsAI — Closed Learning Loop          │
       └────────────────────────────────────────────────────────┘

   Universe (8,000+)              ┌─→  Meta-model (re-weights AI conf)
        │                         │
        ▼                         │    ┌─→ Self-tuner (4 params/day)
   Strategy library (16 + auto-N) │    │
        │                         │    ├─→ 12-layer autonomy (35+ params,
        ▼                         │    │   weights, regime/ToD/symbol)
   Specialist Ensemble (4 AIs) ───┤    │
        │                         │    ├─→ Alpha-decay deprecator
        ▼                         │    │
   Final AI batch decision ───────┤    ├─→ Specialist calibrators
        │                         │    │
        ▼                         │    ├─→ Auto-strategy proposer
   Execution (ATR stops, trail)   │    │   (weekly, AI-driven)
        │                         │    │
        ▼                         │    └─→ Post-mortems on losing weeks
   Position resolves ─────────────┘
        │
        ▼
   ai_predictions.actual_outcome ─────► (feeds 7 loops above)

   Cross-cutting: Cost Guard caps daily AI spend; methodology guards
   ensure every loop reads only out-of-sample data.
```

Outputs of the loops re-shape inputs of the AI's next decision: parameter bounds, signal weights, prompt structure, the candidate shortlist, the meta-model's veto threshold, capital allocated per profile, even the ensemble specialists' effective confidence.

---

## 3. The seven loops in detail

### Loop 1 — Meta-model (Phase 1 of the roadmap)

**File:** `meta_model.py` · **DB:** `ai_predictions.features_json` · **Retrain:** daily, `_task_retrain_meta_model`

**What it does.** Trains a gradient-boosted classifier on every resolved prediction's feature vector. Label = was that prediction correct? Inference: at the moment a candidate clears the AI batch decision and is about to execute, the meta-model says "P(this prediction is correct given what the AI saw)." The prediction's confidence is re-weighted as `final_conf = ai_conf × (0.5 + meta_prob × 0.5)`. If `meta_prob < 0.3`, the trade is suppressed entirely.

**Why this works.** Our predictions are proprietary data nobody else has. The meta-model isn't predicting the market — it's predicting our AI's systematic blind spots. "AI overconfident on low-volume mid-caps in sideways markets, RSI 45-55 band" is the kind of pattern a tree learns from labeled history.

**Integrity guarantee.** Train/test split is **time-ordered** (`X[:n_train], X[n_train:]`), not random. Without this guard, financial features are autocorrelated enough that a random split inflates AUC from a realistic ~0.55 to an artifact ~0.95. See `tests/test_meta_model_time_ordered_split.py` — AST + behavioral guards prevent regression.

### Loop 2 — Self-tuner (the original 4 parameters)

**File:** `self_tuning.py` · **Tunes:** `confidence_threshold`, `stop_loss_pct`, `take_profit_pct`, `position_size_pct`

**What it does.** Daily, looks at win-rate-by-confidence-band on resolved predictions. If predictions with `confidence < 60` are winning <35% of the time, recommend raising the threshold to 60. Same logic for stops and sizing.

**Integrity guarantee.** **Train/validate split.** Adjustment window = predictions resolved more than 14 days ago. Validation window = the last 14 days. A threshold raise is only applied if BOTH:
- The adjustment window confirms the band underperforms.
- The proposed change would have IMPROVED (or at least not hurt) recent performance on the validation window.

If validation-window data is too thin (<5 resolved or <3 surviving), the change is deferred. See `tests/test_self_tuning_validation_window.py`.

### Loop 3 — 12-layer autonomy stack

**Files:** `param_bounds.py`, `signal_weights.py`, `regime_overrides.py`, `tod_overrides.py`, `symbol_overrides.py`, `prompt_layout.py`, `insight_propagation.py`, `capital_allocator.py`, `post_mortem.py`, `cost_guard.py` · **Plan:** `AUTONOMOUS_TUNING_PLAN.md`

**What it does.** Goes far beyond the 4 parameters. Layer-by-layer:

| Layer | What it tunes | Sample effect |
|---|---|---|
| 1 | 35+ scalar parameters with PARAM_BOUNDS clamp | RSI thresholds, ATR multipliers, squeeze tolerance, momentum windows |
| 2 | Per-signal weight ladder (1.0 / 0.7 / 0.4 / 0.0) for 25 signals | "StockTwits sentiment" gets weight 0.4 on Mid Cap, 0.0 on Crypto |
| 3 | Per-regime parameter overlays | Bull regime uses tighter stops; volatile regime widens them |
| 4 | Per-time-of-day overlays | Opening 30-min uses larger position sizing for momentum names |
| 5 | Per-symbol overlays | SPY-specific take-profit different from XOM-specific |
| 6 | AI prompt section ordering + presence | If "earnings memory" stops correlating with outcomes, drop the section |
| 7 | Lessons-learned propagation | Patterns from post-mortems injected directly into next AI prompt |
| 8 | AI model auto-selection (gated by user toggle) | Off by default; user can opt-in |
| 9 | Per-Alpaca-account-conserving capital allocation | Profiles sharing one Alpaca paper account collectively can't exceed that account's $1M cap |

**Resolution chain at runtime:**
```
per-symbol → per-regime → per-time-of-day → profile-global → caller-default,
then × capital_scale, then clamped by PARAM_BOUNDS.
```

**Cross-cutting Cost Guard.** User-configurable daily AI-spend ceiling. When projected daily spend would exceed it, the system gracefully degrades (skip non-essential AI calls, route to cheaper model) instead of blowing the budget.

### Loop 4 — Alpha-decay detector (Phase 3)

**File:** `alpha_decay.py` · **DB:** `signal_performance_history` (daily snapshots), `deprecated_strategies` · **Run:** daily, `_task_alpha_decay`

**What it does.** Every day, write a snapshot of each `strategy_type`'s rolling-30-day metrics (win rate, Sharpe, profit factor). Compare rolling Sharpe to lifetime baseline. If `rolling ≤ lifetime × 0.7` for 30 consecutive snapshot days → auto-deprecate. Deprecated strategies are excluded from the candidate ranker. If a deprecated strategy recovers (rolling within 15% of lifetime for 14 consecutive days) → auto-restore.

**Integrity guarantee.** Lifetime baseline EXCLUDES the rolling window. Otherwise the rolling-window data is INSIDE the lifetime baseline, biasing the comparison toward recent performance and dampening decay signals. `compute_lifetime_metrics(...)` accepts `exclude_recent_days` and the production callers (`detect_decay`, `check_restoration`) explicitly pass `rolling_window_days`. See `tests/test_alpha_decay_lifetime_disjoint.py`.

### Loop 5 — Specialist calibration (added 2026-04-27)

**File:** `specialist_calibration.py` · **DB:** `specialist_outcomes` table · **Refit:** daily, `_task_calibrate_specialists`

**What it does.** Every prediction the ensemble produces stores per-specialist verdicts: `[earnings_analyst BUY 72, pattern_recognizer HOLD 45, sentiment_narrative BUY 78, risk_assessor HOLD 65]`. When the prediction resolves, the specialists' rows get backfilled with `was_correct`. A daily Platt-scaling layer per specialist learns: "when this specialist says raw=78, what is its empirical P(correct)?" The ensemble's contribution math now uses CALIBRATED confidence.

**Concrete finding from the 2026-04-27 backfill.** With 9,692 specialist outcomes seeded from existing predictions:

| Profile | Specialist | raw=90 confidence | calibrated to |
|---|---|---|---|
| Mid Cap | pattern_recognizer | 90 | **29** |
| Small Cap | pattern_recognizer | 90 | **28** |
| Small Cap Shorts | pattern_recognizer | 90 | **24** |
| Mid Cap | risk_assessor | 90 | 43 |
| Large Cap | sentiment_narrative | 90 | 37 |

Translation: when `pattern_recognizer` screamed BUY at 90% confidence on Small Cap, it was historically right 28% of the time. The previous ensemble math gave its vote weight `0.90 × specialist_weight`. Now it weights `0.28 × specialist_weight` — much closer to "ignore." Bad signals attenuated automatically; good ones (e.g., `risk_assessor` on Mid Cap, which calibrates UP from raw 30 to 32 and from raw 90 to 43) keep their weight.

**Integrity guarantee.** Outcomes table is keyed by `(prediction_id, specialist_name) UNIQUE`, so the backfill is idempotent and the daily resolution write doesn't double-count. ABSTAINs and VETOs are excluded from calibration data — ABSTAIN means "no opinion" (no signal) and VETO is a hard-block on a separate code path. See `tests/test_specialist_calibration.py` — behavioral leakage detector seeds an over-confident specialist (raw=90, 50% hit rate) and asserts the calibrator maps it down to 35-65.

### Loop 6 — Strategy auto-generation (Phase 7)

**Files:** `strategy_generator.py`, `strategy_proposer.py`, `strategy_lifecycle.py` · **Run:** weekly (Sundays), `_task_auto_strategy_generation` + `_task_auto_strategy_lifecycle`

**What it does.** Sundays the AI proposes 3 new strategy specs in strict JSON. Each spec is a structured object: `{name, market, direction, conditions: [{indicator, op, value}, ...]}`. The generator validates the spec against a closed allowlist (no arbitrary code can be injected), renders it into Python from a fixed template, drops it as `strategies/auto_NAME.py`. Each new module gets the full Phase-2 validation gauntlet (walk-forward, OOS, regime consistency, Monte Carlo, capacity, statistical significance — 10 gates total). Specs that PASS are promoted to "validated," then to "shadow" trading where they generate predictions for tracking but don't drive real capital. Once a shadow has 50+ resolved predictions with rolling Sharpe ≥ 0.8, it's promoted to "active." Strategies that fail validation or sit in shadow >60 days without earning their slot are retired.

**Five-state lifecycle:** `proposed → validated → shadow → active → retired`.

**Active cap:** 5 auto-strategies per profile so the library doesn't bloat.

**Integrity guarantee.** Phase 2's gauntlet was the methodology-audit's nightmare zone — the original walk-forward and OOS implementations read overlapping recent data, so a strategy could "PASS" without ever being tested on data it hadn't seen. As of 2026-04-27, walk-forward folds use disjoint date ranges (`start_date + k×W` to `start_date + (k+1)×W`) and OOS strictly separates `[history_start, train_end]` from `[train_end, today]`. See `tests/test_walk_forward_and_oos_disjoint.py` — AST guards reject any `backtest_strategy(..., days=...)` call from these wrappers, plus a behavioral test that mocks the data fetcher and asserts the date ranges are pairwise disjoint. Auto-strategies inherit this discipline mechanically.

### Loop 7 — Post-mortems on losing weeks

**File:** `post_mortem.py` · **DB:** `learned_patterns` (with `still_active` flag) · **Run:** weekly

**What it does.** When a profile has a losing week, the system loads ALL the resolved predictions from that week, identifies the worst N, and asks the AI: "What pattern do these losers share that the system missed?" The output is a structured "lesson" stored in `learned_patterns`. Lessons are then injected into the next batch-decision prompt under "Active Lessons." Lessons get a `still_active` flag so the same pattern doesn't get re-discovered every week.

**False-negative mining.** A symmetric mechanism analyzes HOLDS that should have been BUY/SELL based on subsequent price action. Patterns like "AI held when StochRSI was oversold AND insider buying was present, but the price ran" get registered as lessons too, biasing the next decision toward acting in similar setups.

**Why it matters.** Post-mortems close the loop on **direction** of error. The meta-model handles "is the AI's confidence calibrated to truth?" Post-mortems handle "is the AI systematically missing setups?" Different kind of edge.

---

## 4. Integrity guarantees (the 2026-04-27 methodology audit)

A system that auto-tunes itself is only valuable if its measurements are honest. Today's audit + fix sweep eliminated nine specific data-leak patterns that would have made the system look like it was learning when it wasn't. Each fix is locked down by structural tests so future changes can't silently regress.

| # | Issue | Status |
|---|---|---|
| 1 | Meta-model used a RANDOM train/test split — leaked future state | ✅ Time-ordered split + AST guard |
| 2 | `backtest_strategy` only accepted today-relative `days=N` | ✅ Date-range params |
| 3 | "Walk-forward" actually re-tested overlapping recent windows | ✅ Disjoint folds + behavioral test |
| 4 | OOS window was contained INSIDE in-sample window | ✅ Strict separation + behavioral test |
| 5 | Self-tuner optimized on full history, no hold-out | ✅ 14-day validation window + 3 behavioral tests |
| 6 | Predictions resolved on intraday noise (2% threshold, no min-hold) | ✅ 5-day forward-horizon gate |
| 7 | Auto-strategy lifecycle inherited the broken Phase 2 gates | ✅ Auto-fixed by #3 + #4; contract test |
| 8 | Alpha-decay lifetime baseline included the rolling window | ✅ `exclude_recent_days` parameter |
| 9 | Specialist confidence never validated against outcomes | ✅ Per-specialist Platt calibration |

**The system can no longer ship any of these patterns silently.** ~62 new structural tests sit between any future change and the source.

---

## 5. Concrete example: today's pattern_recognizer finding

To make this real: as soon as the calibration backfill ran (9,692 outcomes, 30 fitted calibrators), it surfaced an immediately actionable insight nobody had noticed.

**Finding.** The `pattern_recognizer` specialist is **inversely calibrated** on every primary equity profile:

```
Profile          raw=30  raw=50  raw=70  raw=90
Mid Cap            47     41     35     29
Small Cap          47     40     34     28
Small Cap Shorts   35     31     27     24
Large Cap          54     53     52     51   (mostly random)
```

Read: when pattern_recognizer says BUY at 90% confidence on Small Cap, the empirical hit rate is **28%**. When it shyly says 30%, it's right **47%** of the time. The signal is anti-correlated with success on small/mid caps — the more confident the specialist, the worse the outcome.

**Implication.** The previous ensemble math weighted contributions linearly by raw confidence. Pattern_recognizer's "strong BUYs" were therefore dominating the ensemble verdict on exactly the trades where it was wrong most of the time. Some non-trivial fraction of Small Cap's -19% cumulative-return underperformance was the system acting on the loudest-but-most-wrong specialist.

**Auto-fix.** The calibration layer now multiplies pattern_recognizer's vote by 0.28 instead of 0.90 in those cases. **No human had to discover this and override it.** The system surfaced its own failure mode with one daily-fitted logistic regression per specialist.

This is the texture of "auto-experimenting and tuning" working as advertised. Not "the AI is so smart it figures everything out" — but "the AI logs honest data, the system reads it honestly, and miscalibrations correct themselves."

---

## 6. What to expect over time

| Horizon | Expected change |
|---|---|
| **Tonight (3:55 PM ET)** | Meta-model retrain produces the first honest AUC reading. Likely drop from artifact 0.83-0.96 to real 0.50-0.65. |
| **1-3 trading days** | Trade volume drops on Small/Mid Cap profiles where pattern_recognizer was inversely calibrated. Large Cap volume holds (specialists were near-random there anyway). |
| **1 trading week** | Per-profile win rates start to converge — Small/Mid should rise as the system stops acting on bad specialist signals; Large Cap might dip slightly. |
| **2 trading weeks** | Self-tuner starts emitting validation-confirmed adjustments. Expect FEWER changes than before — many proposed raises will be rejected at validation. |
| **30 days** | Alpha-decay can detect its first deprecation candidate. Expect 1-3 strategies to be flagged for retirement on at least one profile. |
| **45 days** | Strategy auto-generation has run 6-7 weekly cycles. Some auto-strategies should be approaching shadow→active promotion. |
| **90 days** | First real read on whether the AI has alpha. With every loop measuring honestly, in-sample-vs-out-of-sample performance is comparable, and the system either is or isn't beating SPY net of slippage. |

---

## 7. Where to look in the dashboard

| Tab | Widget | What it tells you |
|---|---|---|
| `/performance#ai` | Meta-Model | Per-profile AUC, accuracy, top features, sample count |
| `/performance#ai` | Specialist Ensemble | Per-symbol breakdown including calibrated vs raw confidence |
| `/performance#ai` | Active Lessons | Patterns from post-mortems currently injected into AI prompt |
| `/performance#ai` | Active Autonomy State | Snapshot of every layer's currently-applied adjustments |
| `/performance#ai` | Autonomy Timeline | Chronological audit trail of every autonomous change |
| `/performance#ai` | Cost Guard | Per-profile + cross-profile daily AI spend vs ceiling |
| `/performance#ai` | Alpha Decay Monitoring | Per-strategy rolling vs lifetime Sharpe + active deprecations |
| `/performance#ai` | Evolving Strategy Library | Auto-strategy lineage: proposed → validated → shadow → active → retired |
| `/performance#scalability` | Strategy Allocation | Capital allocator weights per strategy per profile |
| `/performance#executive` | Win Rate / Sharpe / Drawdown | The honest topline numbers |
| Dashboard cards | Per-profile equity / positions / today's trades | Ground truth |
| `/api/resolve-param` | Parameter Resolver | Inspect: for THIS profile, THIS parameter, RIGHT NOW, what value would the override chain return? |

---

## 8. The two numbers that matter

**Meta-model AUC > 0.55 per profile.** If the meta-model can learn the AI's blind spots better than chance, the AI's confidence is calibratable and the second-order edge exists. If it sits at 0.50, the AI is acting as if its features matter when they don't, and the response is to widen the feature set or simplify the strategy library.

**Cumulative win rate vs SPY over 90 days, per profile.** If at least 2 of the 3 primary profiles beat SPY over a quarter, the system has alpha. If all 3 trail SPY by >5%, the methodology-honest finding is the strategies don't generalize beyond their training context — and the next move is a deeper audit of the strategy library, not more autonomy layers on top.

The system is now finally honest enough for those numbers to mean something.

---

## 9. Cross-session continuity

If a future contributor (or future-you) picks this up cold:

1. Read `EXECUTIVE_OVERVIEW.md` for the top-down summary.
2. Read this document for the experimentation/tuning surface.
3. `METHODOLOGY_FIX_PLAN.md` is the audit-and-fix log of how we know the measurements are honest.
4. `AUTONOMOUS_TUNING_PLAN.md` is the architectural design rationale for the 12-layer stack.
5. `ROADMAP.md` is the 10-phase quant-fund evolution context.
6. `TECHNICAL_DOCUMENTATION.md` is the full reference (database schemas, API endpoints, scheduler tasks).
7. `CHANGELOG.md` is every fix in chronological order — search for "(Severity: critical, accuracy)" entries to see methodology-related changes.

Every one of these is up to date as of 2026-04-27.
