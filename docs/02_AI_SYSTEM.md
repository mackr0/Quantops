# 02 — AI System

**Audience:** quants, ML researchers, anyone who builds production prediction systems.
**Prerequisites:** familiarity with gradient-boosted trees, online learning, calibration, ensemble methods.
**Last updated:** 2026-06-04 (audit reconciliation — see `docs/AUDIT_2026_06_04_DOC_RECONCILIATION.md`).

## Overview

QuantOpsAI's AI architecture is a **stacked decision pipeline** in which a frontier LLM acts as the apex policy, and a portfolio of supporting models (ensemble specialists, meta-model, calibrators, online learner) provide the LLM with calibrated probabilistic context, suppress likely-incorrect candidates before they reach the LLM, and re-weight the LLM's confidence after the fact. Every decision the system makes is captured in a journal, resolved against price action, and used to train every layer.

The pipeline is summarized as:

```
Universe (~8,000 Alpaca-tradable US equities) →
    Strategy votes (25 plugin strategies) →
        Pre-rank shortlist (top 30) →
            Meta-model pre-gate (drops candidates below meta_pregate_threshold, default 0.35) →
                Two-layer specialist ensemble (179 deterministic rules + 8 LLM specialists) →
                    LLM batch decision (the apex policy) →
                        Validation gates (hard rules) →
                            Execution →
                                Journal (decision + features) →
                                    Resolution (win/loss labeling) →
                                        Feedback loops (training data for every layer above)
```

**This is the value-prop story.** The system scales the AI's accuracy without scaling its cost by putting hundreds of *deterministic* rule-checkers in front of the *narrative* LLM call. The 179 rule modules each cost zero API tokens — they're pure-Python pattern matchers — and they catch the structurally-checkable patterns (RSI overbought, insider clusters, gap into resistance, etc.) so the LLM only spends tokens on the synthesis work it's uniquely good at. Most decisions short-circuit cleanly through the rule layer; only the genuinely-contested candidates exercise the apex LLM. Result: ~$0.27/day of AI spend across the 13-profile fleet at the current `gemini-2.5-flash-lite` rate.

> **Full enumeration** of all 187 specialists (8 LLM + 179 deterministic) with per-rule purpose + severity + direction lives in `docs/24_SPECIALIST_CATALOG.md`. The catalog is auto-derivable from the source — each rule's `NAME`, `DESCRIPTION`, and `APPLIES_TO_SIGNALS` are read directly out of `deterministic_specialists/*.py` — so it stays in sync.

Each layer is documented below. Section sequence follows the data flow.

## 1. Universe construction

Source: Alpaca's `/v2/assets` endpoint (US equities + ETFs, ~8,000 active symbols), filtered per-profile by:

- Market type (`stocks` / `crypto`), via `segments.py` and `segments_historical.py`. The `stocks` segment is the unified Alpaca-tradable US equity universe; `crypto` is a separate 24/7 data path. Per-profile `min_price` / `max_price` / `min_volume` thresholds gate which of the ~8,000 active symbols actually reach the strategy layer; short selling is gated by `enable_short_selling`. The genuine instrument-class split (`stock` vs `option`) lives in `pipelines/dispatch.py`, not here.
- Active-status flag (delisted / merged / renamed names are removed from live universe but kept in `historical_universe_additions` for backtest survivorship-bias correction).
- Per-profile custom watchlist (additions) and exclusion list (removals).

The dynamic-universe layer is described in detail in `docs/04_TECHNICAL_REFERENCE.md`.

## 2. Strategy engines

The platform ships **25 plugin strategies** in `strategies/`. Each is a pure function returning a vote (signal + score). Cost: zero per cycle (pure code, no API). The canonical registry lives at `strategies/__init__.py`.

- **Bullish (12):** gap_reversal, news_sentiment_spike, short_squeeze_setup (long side), earnings_drift, insider_cluster, fifty_two_week_breakout, macd_cross_confirmation, sector_momentum_rotation, analyst_upgrade_drift, short_term_reversal, volume_dryup_breakout, max_pain_pinning.
- **Bearish (13):** breakdown_support, distribution_at_highs, failed_breakout, parabolic_exhaustion, relative_weakness_in_strong_sector, earnings_disaster_short, catalyst_filing_short, sector_rotation_short, iv_regime_short, relative_weakness_universe, insider_selling_cluster, high_iv_rank_fade, vol_regime.

(The same four strategy names — `momentum_breakout`, `volume_spike`, `mean_reversion`, `gap_and_go` — also exist as standalone modules in `fallback_strategy.py` / `strategy_small.py`, gated by per-profile `strategy_*` toggle columns. Those files are dead-code paths that no live cycle invokes; the live versions are part of the 25 plugins above. The toggle columns remain in the schema for backward compatibility only.)

Strategy votes are deterministic; they're cheap (no LLM cost) and run on every cycle. Each strategy emits one of: STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, SHORT, STRONG_SHORT.

The shortlist is built by aggregating strategy votes per symbol, computing a composite score (defined in `multi_strategy.py`), and retaining the top ~30 candidates with two reserved slots: top-10 longs and top-5 shorts (when shorts are enabled for the profile). This protects short-side opportunities from being crowded out by a long-dominated vote pool.

## 3. Meta-model pre-gate (Lever 2)

Before each candidate reaches the specialist ensemble — which costs API tokens — the meta-model evaluates whether the AI is likely to be **right** on this candidate given the full feature payload. Candidates whose predicted probability of success is below `meta_pregate_threshold` (schema default 0.35) are dropped silently. The threshold is per-profile and AI-tunable via `_optimize_meta_pregate_threshold`: it's the dial that trades off cost against opportunity-set breadth — tighter values save AI spend but filter more candidates before the apex call sees them.

This is a cost-and-quality lever: it cuts specialist API calls roughly in half once the meta-model is trained, and it also functions as a quality filter — candidates the meta-model strongly suspects are noise don't pollute the specialist consensus.

The meta-model itself is described in §6 below.

## 4. Two-layer specialist ensemble

The ensemble has two parallel layers that surface into the apex LLM prompt:

### 4a. Deterministic specialist library (`deterministic_specialists/`)

179 pure-Python rule checkers. Each rule is a function `(candidate, ctx) → Optional[{severity, reasoning}]` with severities `VETO` / `CAUTION` / `CONFIRM`. Zero per-rule API cost. Each rule fires only when its specific data pattern is present; in practice 5-15 rules fire per candidate. The fired verdicts render into two surfaces:

  - **Apex prompt panel** — `deterministic_specialists.build_panel_block(candidate, ctx)` is appended to each candidate's section of the batched LLM prompt as a `DETERMINISTIC RULE PANEL` block.
  - **Compact rules-suffix on LLM specialist candidate renders** — when the LLM specialists call `candidates_block(candidates, specialist_name=..., ctx=ctx)`, each rendered candidate carries a `RULES: [V]name [C]name ...` suffix so the LLM specialists synthesize from the rule layer rather than re-deriving facts.

Rule categories include: late-stage / extended warnings (`rsi_overbought_late_stage`, `parabolic_blow_off`, `gap_into_resistance`), breakout / momentum quality (`volume_dry_breakout`, `low_atr_breakout`, `weak_adx_breakout`), smart-money + flow (`insider_cluster_buying`, `dark_pool_accumulation`, `activist_13d_filed`, `congressional_buying`), earnings momentum (`positive/negative_earnings_revisions`, `earnings_surprise/miss_streak`), regulatory events (`recent_8k_negative_event`, `fda_inspection_warning`, `nhtsa_recall_active`), trend confirms (`strong_adx_trend_confirm`, `bollinger_walk_up/down`, `near_fib_support`), short-side specific (`borrow_cost_high_short`, `squeeze_risk_short`), macro / volatility regime (`yield_curve_inverted`, `cboe_skew_extreme`, `macro_oil/treasury/gold_vol_high`), execution friction (`slippage_high_caution`, `wide_spread_caution`), calendar / time-of-day (`turn_of_month_strength`, `monday_morning_open`, `last_30_min_session`), oscillator confluence (`triple_overbought`, `triple_oversold`), and catalyst stacking (`multiple_negative_catalysts`, `multiple_positive_catalysts`).

Adding a rule: drop a module under `deterministic_specialists/<name>.py` exposing `NAME`, `DESCRIPTION`, `APPLIES_TO_SIGNALS`, and `evaluate(candidate, ctx)`. Add the import path to `RULE_MODULES`. The structural test pins one positive fixture per rule.

Per-rule exception isolation: one bad rule logs at DEBUG and is skipped; the rest of the panel continues.

**Routing — stock signals and options/multileg.** `APPLIES_TO_SIGNALS` enumerates the stock-side actions a rule applies to (typically `("BUY", "STRONG_BUY", "WEAK_BUY")` for long-only checks or `("SELL", "STRONG_SELL", "WEAK_SELL", "SHORT")` for short-only). The router (`run_panel` in `deterministic_specialists/__init__.py`) supports two match modes:

  - **Direct match** (stock candidates): the candidate's signal must appear in the rule's tuple. Long-only rules don't fire on SHORT candidates and vice versa.
  - **Directional match** (options / multileg candidates): the router calls `signal_direction(candidate)` to classify the candidate by `(signal, option_strategy)` as `bullish` / `bearish` / `neutral`. A rule then fires if its `APPLIES_TO_SIGNALS` overlaps the same-direction stock-action set. So a `long_call` / `bull_call_spread` / `cash_secured_put` / `covered_call` / `bull_put_spread` fires the same long-only rules as a `BUY` would; `long_put` / `bear_call_spread` / `bear_put_spread` / `protective_put` fires the same short-only rules as a `SHORT` would.

Non-directional strategies (`iron_condor`, `iron_butterfly`, `straddle`, `strangle`, `calendar_spread`) don't trigger directional rules — they're covered by the option-specific LLM specialists (`gamma_pin_specialist`, `iv_skew_specialist`, `option_spread_risk`). Unknown option strategies on an `OPTIONS` / `MULTILEG_OPEN` candidate fire no directional rules (avoid mis-attribution). The directional rule layer carries no per-rule option-side code: every rule's existing `APPLIES_TO_SIGNALS` tuple already encodes its direction, and the router uses that to dispatch correctly.

### 4b. LLM specialist ensemble (`specialists/`)

Eight LLM-narrative specialists, each instantiated with the same backend model but a differentiated system prompt and feature subset. Six of the eight read the deterministic panel's verdicts and **synthesize** a coherent narrative on top of them rather than re-derive the underlying facts; the other two cover territory the rule library structurally can't subsume and read raw candidate features directly. This division — facts on rails, narrative on judgment — is what keeps per-cycle cost flat as the rule library grows.

**Re-scoped specialists** (consume the rules-suffix in the candidate render and pivot to synthesis):

| Specialist | Re-scoped role | Veto authority |
|---|---|---|
| `pattern_recognizer` | Synthesizes a coherent technical thesis from the deterministic technical rules' verdicts | No |
| `risk_assessor` | Synthesizes a worst-plausible-outcome scenario from the risk-cluster rule verdicts | **Yes** |
| `sentiment_narrative` | Synthesizes the coherent narrative — who is positioning and why — from the smart-money + sentiment rules | No |
| `earnings_analyst` | Synthesizes the earnings trajectory from the earnings-cluster rules (beat-and-raise vs deteriorating vs event-priced) | No |
| `adversarial_reviewer` | Hunts failure modes the rule library can't encode (book-level correlation, mandate violations, novel scenarios) | **Yes** |
| `iv_skew_specialist` | Reads put/call IV skew for premium-side bias; tweaked to consume the options-rule verdicts | No |

**Untouched specialists** (deterministic library can't subsume these):

| Specialist | Role | Veto authority |
|---|---|---|
| `gamma_pin_specialist` | Reads dealer GEX + max-pain strike for pinning (stability) vs negative-gamma (instability) regimes | No |
| `option_spread_risk` | Option-aware gatekeeper for IV crush, gamma exposure, max-loss budget violations | **Yes** |

All eight share the same canonical interface — each specialist file in `specialists/` exposes `NAME`, `DESCRIPTION`, `HAS_VETO_AUTHORITY`, `APPLIES_TO_PIPELINES`, and `build_prompt(candidates, ctx)`. `specialists/__init__.py` auto-discovers them at load time. The re-scoped specialists pass `ctx=ctx` into `candidates_block(...)` so the deterministic panel verdicts appear in each candidate's render; this is enforced by `tests/test_specialist_rescope_2026_05_18.py`.

The architectural split between the two layers:
- **Deterministic** wins for "did X happen?" — pattern matching on facts. 100% accuracy, zero cost, instant.
- **LLM** wins for "given all these facts, what's the coherent thesis?" — synthesis and narrative reasoning that pure code structurally can't do.

This split is the core architecture decision: facts on rails, narrative on judgment. Before the Phase 3 re-scope, the LLM specialists were duplicating the cheap work and skipping the expensive work; now each LLM call shifts from "look at the data" to "interpret what the rule layer concluded."

### 4c. Case-file RAG (`case_file_rag.py`)

The LLM's weights are frozen at the model provider's training cutoff. To compensate, every new decision retrieves the most-similar past *resolved* cases from THIS profile's own history and injects them into the prompt as concrete cases-to-reason-from. The system gains experience without retraining the model.

Implementation:
- `case_file_rag.build_case_file_text(prediction)` renders an `ai_predictions` row as a structured token stream — symbol, signal, regime, strategy_type, confidence bucket, indicator bands (RSI / momentum / volume / gap / ATR), outcome, return bucket. Numeric indicators are bucketed (`rsi_70_80`, `volume_ratio_1.5_2.5`) so TF-IDF treats them as discrete tokens instead of unique per-row floats.
- `case_file_rag.retrieve_similar(profile_db_path, candidate, top_n=3, min_similarity=0.15)` fits sklearn `TfidfVectorizer` on the rolling 2000-case corpus + the candidate text and returns top-N cosine-similarity matches above the floor. Returns BOTH wins and losses (filtering to only "warnings" would bias the AI away from action — pinned by test).
- `case_file_rag.build_prompt_block(...)` is the one-call wrapper used by `ai_analyst._build_batch_prompt` per candidate. Output is a `SIMILAR PAST CASES FOR <SYMBOL>` block with one line per case: `[date] SIGNAL SYMBOL in regime → OUTCOME (return in days, sim=X)`.
- No schema migration — all retrieval inputs derive from existing `ai_predictions` columns. Backfill is automatic; every already-resolved row immediately becomes available to the corpus.

Backend choice: TF-IDF (sklearn — already installed). Sentence-transformers were considered and rejected because (1) case-file text is highly structured key=value tokens, not natural-language paraphrasing, and (2) the dependency would add ~1GB of PyTorch + model weights. Architecture supports a later upgrade if quality measurably lags.

Fail-soft: any retrieval error (missing DB, sklearn unavailable, malformed corpus) yields an empty block — the existing prompt still works, the AI just doesn't get the extra context. Logged at DEBUG so silent quality regressions are visible.

Each specialist returns a verdict (BUY / SELL / HOLD / SHORT / VETO) and a 0-100 confidence. The synthesizer in `ensemble.run_ensemble`:

1. **Calibrates each specialist's raw confidence** using its Platt-scaling layer (§5). The calibrator maps raw confidence to empirical P(correct) so that "raw=80" from a poorly-calibrated specialist doesn't dominate.
2. **Applies veto authority.** If `risk_assessor` or `adversarial_reviewer` returns VETO, the candidate is dropped regardless of the other three.
3. **Aggregates remaining verdicts** via confidence-weighted voting. Ties break toward HOLD (more conservative).
4. **Suppresses entries** when consensus is below a per-profile threshold.

Specialists are gated by `disabled_specialists` (Lever 3) — a per-profile list of specialists whose API call is skipped. The auto-disable mechanism in `_task_specialist_health_check` removes specialists whose calibration slope flips inverse for ≥30 days, re-enables them when the slope recovers, and enforces a hard floor of two active specialists.

## 5. Specialist Platt-scaling calibration

Each specialist has its own Platt-scaling logistic-regression layer fitted from `specialist_outcomes` rows — one row per (specialist, prediction_id) pair, written when a prediction resolves. The calibrator maps `raw_confidence ∈ [0, 100]` to `empirical_P(correct) ∈ [0, 1]`.

Implementation:

- `specialist_calibration.fit_platt_scaler(rows)` — fits per specialist, per direction (long/short separately), per market type. Falls back to legacy unified calibrator until each direction accumulates ≥30 samples.
- Refitted nightly via `_task_calibrate_specialists`.
- The 2026-04-27 backfill run produced ~9,692 specialist outcomes from existing predictions and surfaced the `pattern_recognizer` anti-calibration finding (raw=90 → empirical=28 on small-cap profiles), which motivated Lever 3.

A specialist with a well-calibrated layer has its raw confidence shifted toward its true hit rate. A specialist with anti-calibration is detected by the daily health check and disabled until its slope recovers.

## 6. Meta-model — two-layer (GBM batch + SGD freshness)

The meta-model answers: "given everything the AI saw at decision time, what is the probability the AI was right?" It is a **per-prediction probability adjustment** that re-weights the AI's stated confidence at execution time.

### 6a. GBM batch model

- **Architecture:** scikit-learn `GradientBoostingClassifier`, default 100 trees, max_depth=3.
- **Training:** retrained nightly via `_task_retrain_meta_model`. Requires ≥100 resolved predictions with `features_json` populated. Time-ordered train/test split (last 20% holdout). Reports AUC, accuracy, top feature importance.
- **Features:** `meta_model.NUMERIC_FEATURES` (technicals, alt-data, attention signals, macro) and `CATEGORICAL_FEATURES` (one-hot encoded: signal type, regime, prediction type, insider direction, options signal, VWAP position, sector trend, congress direction, EPS revision direction, curve status, insider near earnings, rotation phase, earnings surprise direction, market GEX regime, Google Trends direction).
- **Output usage:** `meta_prob` per candidate, used both at the pre-gate (§3) and post-AI re-weighting.
- **Adjustment formula at re-weight:** `new_confidence = original_confidence × (0.5 + meta_prob × 0.5)`. So `meta_prob=0.5` → 0.75× confidence; `meta_prob=1.0` → 1.0×; `meta_prob=0.0` → 0.5×.
- **Suppression:** trades with `meta_prob < 0.4` are dropped entirely.

### 6b. SGD online freshness layer

A second model alongside the GBM that updates **incrementally** on every resolved prediction.

- **Architecture:** `SGDClassifier(loss='log_loss', alpha=0.01, learning_rate='optimal', max_iter=1000)`, with a `StandardScaler` in front (raw features have mixed scales — RSI 0-100, ATR ~0.02, reddit_mentions ~1000 — and unscaled SGD saturates the sigmoid).
- **Bootstrap:** initialized from the same training set as the GBM (minimum 10 rows; bypasses the GBM's 100-row threshold). Re-bootstrapped nightly after the GBM retrain.
- **Update path:** `update_online_model(profile_id, features_dict, outcome_label)` is called from `ai_tracker.resolve_predictions` immediately after a prediction is labeled. This runs `partial_fit([x_row], [outcome_label])` — a single epoch of SGD on the new sample.
- **Persistence:** pickled per-profile as `online_meta_model_p{profile_id}.pkl`.

### 6c. Why two layers

The GBM is more accurate on stable distributions because tree-based methods naturally capture interaction effects in a feature set with ~80 dimensions. The SGD model is faster to adapt to regime shifts because it weights recent samples more heavily and updates per resolution, not per nightly retrain.

The trade pipeline computes both probabilities on every accepted trade, attaches `meta_prob`, `online_meta_prob`, and `meta_divergence = online_meta_prob - meta_prob` to the trade dict, and logs the divergence. **Large divergence is itself a signal**: it indicates that the GBM's view (informed by months of history) and the SGD's view (informed by the last few weeks) disagree, which usually means recent regime drift the batch model hasn't seen yet.

The drift can be inspected on the AI Brain tab; the slippage panel exposes calibration drift, but the meta-model divergence is currently log-only (surfaced to AI prompt under future work).

## 7. The apex LLM call

After the candidate list has been pre-gated (meta-model), enriched (specialist ensemble), and contextualized (portfolio state + market context + per-stock memory), the system makes **one batched LLM call per scan cycle** via `ai_analyst.ai_select_trades`.

### 7.1 Core directive

The prompt opens with two non-negotiable principles:

1. **No fixed cap on the number of trades.** "Propose every trade where conviction is genuine. There is no fixed cap — propose as many high-conviction setups as you see, and zero is acceptable when none qualify." A fixed numeric cap would leave real opportunity on the table when many candidates qualify, and would force forced choices when none do.
2. **Stocks and options are equal opportunities, not competing alternatives.** "When you see a candidate, evaluate it on its own merits — directional stock entry (BUY/SHORT), defined-risk options structure (MULTILEG_OPEN), or single-leg option (OPTIONS) — and pick the action with the best risk/reward for THAT setup." Stocks and options flow into the prompt as parallel pre-built recommendations (see §7.3) — same level of detail on both sides — so the AI's choice is driven by quality of setup, not by which side has more pre-built analysis to read.

### 7.2 Prompt sections (in order)

- **Header**: directives 1 & 2 above + role framing ("portfolio manager for an automated {market_type} trading system").
- **Portfolio state block:** equity, cash, current positions, exposure breakdown (sector + factor + direction), book beta (current vs target), Kelly recommendations per direction, drawdown capital scale, risk-budget per-position contributions, MFE capture ratio, sector concentration warnings.
- **STOCK ACTION RECOMMENDATIONS** (new 2026-05-14): one entry per directional candidate with action (BUY/SHORT), size_pct (conviction-scaled), stop_loss_pct (ATR-based), take_profit_pct (ATR-based), confidence, rationale. Built by `stock_strategy_advisor.render_stock_recs_for_prompt`.
- **MULTI-LEG OPTIONS STRATEGIES**: defined-risk options structures with strategy name, strikes, expiry, rationale. Built by `options_strategy_advisor.render_multileg_recs_for_prompt`. Gated by IV dead zone (no rec when IV rank is in the 45-60 neutral band).
- **Market context block:** regime label + VIX, SPY trend, sector rotation (5-day returns), crisis level (with size multiplier), macro context (yield curve, CBOE skew, FRED indicators, ETF flows), portfolio risk readout (daily σ, 95% VaR, ES, top factor exposures, worst-3 stress scenarios), next macro event (FOMC/CPI/NFP), long-vol hedge state (when active).
- **Candidate block** for each of the (typically 5-15) survivors, including: technical indicators, options oracle summary (IV rank, term structure, skew, GEX, max pain, implied move), alternative data (insider, short interest, options flow, intraday patterns, congressional, 13F, biotech, StockTwits, Google Trends, Wikipedia views, App Store ranks), LLM specialist ensemble verdicts, the **deterministic rule panel** (§4a — typically 5-15 fired verdicts per candidate), the **RAG case-file block** (§4c — top-3 most-similar resolved past trades for this profile), per-stock track record by signal type, last prediction reasoning, earnings warning, SEC alerts, news headlines, slippage estimate, borrow rate (for shorts).
- **Long/short balance target** and **book-beta target** with directives ("UNDERSHORTED — pick a SHORT this cycle"; "BETA TOO HIGH — DEFENSIVE picks long or LEVERED shorts").
- **Learned patterns** from prior post-mortems and self-tuner findings.
- **Track record** aggregated and split by signal type to prevent confabulation (e.g., the AI cannot claim "100% win rate on VALE shorts" when all 13 wins were HOLDs).
- **RULES section**: max position size (longs and asymmetric shorts), independent stock/options evaluation, drawdown-aware sizing without artificial trade-count cap, and per-action notes (`stock_recs_note`, `options_note`, `pair_note`, `multileg_note`) describing required fields and how to use the pre-built recommendations. Each action type has parallel guidance — no implicit-default action that biases the AI.
- **Allowed actions** dynamically scoped: BUY, HOLD; plus SHORT (when enabled), OPTIONS (when any candidate has tradeable options or the advisor surfaced an opportunity), PAIR_TRADE (when stat-arb book has actionable pairs), MULTILEG_OPEN (when multi-leg advisor has surfaced one).

### 7.3 Symmetric pre-computed recommendations

Both stock and options recommendations are pre-computed before the LLM sees them, and presented with the same level of detail:

| Aspect | Stock rec (`stock_strategy_advisor`) | Options rec (`options_strategy_advisor`) |
|---|---|---|
| Action | BUY / SHORT | bull_put_spread / bear_call_spread / iron_condor / etc. |
| Sizing | `size_pct` (conviction-scaled, asymmetric for shorts) | `contracts` (defined-risk by structure) |
| Risk control | `stop_loss_pct` (ATR-based) | implicit by spread width |
| Profit target | `take_profit_pct` (ATR-based) | implicit by spread credit |
| Rationale | technicals summary + sizing rationale | direction + IV regime + structure rationale |
| Confidence | strategy-ensemble conviction | rule-evaluator confidence |
| Cap per prompt | 8 entries (mirrors options) | 8 entries |

This symmetry is enforced by `tests/test_stocks_and_options_equal_in_prompt.py` (7 checks covering field parity, prompt insertion, both blocks present).

### 7.4 Verbosity tuning

The prompt structure is verbosity-tunable per profile via `prompt_layout.set_verbosity` (Layer 6 of the self-tuning stack — see §9).

### 7.5 Response parsing

The LLM's response is parsed by `_parse_ai_response_tolerant` (in `ai_analyst.py`), a defensive parser tolerant of common malformations (markdown fences, single quotes, trailing commas, prose preambles). Parse failure logs the raw response and skips the cycle without any trades. The apex call also explicitly sets `response_mime_type: application/json` on Gemini providers so the provider returns JSON natively rather than prose that has to be coaxed.

### 7.6 Display-safe rendering of LLM-emitted text

The LLM routinely echoes back the snake_case / UPPER_SNAKE_CASE identifiers from its prompt — `STRONG_BUY`, `bull_put_spread`, `max_position_pct`, etc. — in its `ai_reasoning` string and other free-text fields. The architectural contract is that the AI can keep emitting whatever it emits; the **display layer** sanitizes before the user sees anything. Every dynamic-content render goes through `display_names.humanize` (the `| humanize` Jinja filter, or the server-side `humanize(...)` call in `views.py`).

The filter looks up canonical labels in `_DISPLAY_NAMES` and falls back to Title-Case for anything unknown — so a future identifier like `quantum_thresher_signal` renders as "Quantum Thresher Signal" without any code change. See `docs/13_QUALITY_RELIABILITY.md` §3.3 for the full contract and the structural test that enforces it.

## 8. Validation gates

After the LLM returns trades, hard rules in `_validate_ai_trades` filter them. Each gate logs a reason; vetoed trades surface on the AI dashboard.

- **Balance gate (long/short profiles):** when book has drifted >25pp off `target_short_pct`, block new entries on the over-weighted side.
- **Asymmetric short cap:** longs sized against `max_position_pct`; shorts capped at `short_max_position_pct` (defaults to half of long).
- **HTB borrow penalty:** hard-to-borrow shorts have their cap halved again on top of the asymmetric one.
- **Market-neutrality enforcement:** when `target_book_beta` is set, blocks entries that push `|projected - target| - |current - target| > 0.5`. Symmetric — entries that improve neutrality always pass.
- **Crisis gate:** `crisis` and `severe` levels block new long entries entirely; `elevated` scales position sizes 1.0× → 0.85× → 0.65× → 0.45× → 0.25×.
- **Intraday risk halt:** when an active halt is recorded by `_task_intraday_risk_check`, new entries are blocked until the 60-minute auto-clear or manual override.
- **Cost guard:** if today's projected AI spend exceeds the daily ceiling, AI-cost-affecting actions (re-runs, model upgrades) are deferred.
- **Duplicate / wash-trade guard:** orders blocked when Alpaca's wash-trade rule would trigger (30-day cooldown table).
- **Cross-direction guard:** "cannot open a long buy while a short sell order is open" — recoverable, not error.

## 9. Self-tuning stack (12 layers)

The self-tuner runs nightly per profile (`_task_self_tune`) and is the largest single source of long-term improvement. It is a rule-based system, not learned: each rule is a small, auditable piece of code that adjusts one parameter, signal weight, override, or enable/disable bit based on its own track record.

### 9.0 Architectural principle: bias toward confident trading

The self-tuner exists to make the system trade better, not to make it trade less. The architectural principle, enforced by all 12 layers, is that the system should drift toward CONFIDENT TRADING, not stasis. This is not optional — it is the contract every tuning rule honors:

- **Sample-size minimum.** Every tightening decision (deprecate strategy, raise confidence threshold, narrow regime sizing, disable shorts, widen short stop) requires at least **30 resolved predictions** of evidence. Below 30 is statistical noise. Enforced by `tests/test_self_tuner_minimum_sample_sizes.py` — any new tightening rule with a sample-size check below 30 fails CI unless explicitly annotated `# DISPLAY_ONLY:` (analytics) or `# LOOSEN_OK:` (loosening can fire on smaller samples by design).
- **Trade-volume floor signal.** When a profile produces fewer than 3 stock-entry trades in the last 7 days, `apply_auto_adjustments` sets `ctx._runtime_under_volume_floor = True`. This is a SIGNAL not a kill switch: tightening sample-size requirements double to ≥60, and the optimizer registry reorders to put loosening rules (`_optimize_false_negatives`) FIRST. Tightening on truly catastrophic patterns (≥60 samples + clear evidence) remains available.
- **TTL-based auto-restoration.** Every strategy deprecation auto-restores after 14 days unless the existing Sharpe-recovery check has already restored it. Without TTL, deprecations were effectively permanent (a deprecated strategy emits no signals → can never recover its Sharpe → stays deprecated forever). Re-deprecation requires fresh ≥30-sample evidence. Implemented in `alpha_decay.restore_expired_deprecations`.
- **No manual rescue scripts.** A revert script needed to "rescue" the system from over-restriction is evidence of architectural failure, not a feature. Every restriction must have a path back to action.

These guarantees protect the system from a compounding-restriction failure mode: many small parameter tightenings, each individually reasonable, can stack into a state where the entry filter rejects everything. The five guardrails below formalize the principle as deterministic code rather than convention so the failure can't recur structurally.

**Phase 1 hard-rule layer (see `docs/17_SELF_TUNER_GUARDRAILS_AND_RAG.md`).** Five guardrails:

1. **Per-cycle delta cap.** Every numeric parameter write routes through `_apply_param_change` which clamps the change to ±25% of the current value. Composes with #3 so no single cycle can cascade past the cap.
2. **Trade-count floor auto-loosen.** When stock entries fall below 3 in a 7-day window, `_optimize_trade_count_auto_loosen` (tagged LOOSEN — fires FIRST in the registry) picks the most-restrictive entry-filter parameter and forces it to loosen by 25%. Encodes "drift toward trading" as a deterministic action, not just a soft bias.
3. **Reference-window invariant.** `param_references` table snapshots the day-1 value for each (profile, param) on first observation; subsequent writes are clamped to ±50% from that reference. With #1 composed, the 14-day compounding cascade lands at the reference floor (e.g., 0.05) instead of spiraling toward zero — pinned by `test_14_cycles_held_to_reference_floor`.
4. **Auto-expiry on tightenings.** `expired_at` column on `tuning_history`; `_optimize_auto_expire_old_tightenings` (tagged LOOSEN) picks the oldest unexpired tightening older than 14 days whose `outcome_after` is not `'improved'` and walks the parameter one cap-bounded step back toward `old_value`. Stale restrictions can't sit forever without evidence.
5. **Trade-rate anomaly alert.** `trade_rate_anomaly.py` + daily scheduler task `_task_trade_rate_anomaly_check` write an `audit_alerts` row when weekly entry count drops >50% week-over-week. Observability only — does NOT pause the tuner (per `feedback_ai_driven_no_manual_loop`), surfaces in `/issues`.

### 9.1 The 12 layers

1. **Parameter coverage.** ~35 numeric parameters on `trading_profiles` (stop loss, take profit, max position, RSI thresholds, gap threshold, volume floors, max correlation, max sector positions, drawdown thresholds, etc.) Each has a tuning rule in `self_tuning.py` of the form: "buckets of recent resolved predictions by parameter value range (≥30 samples per bucket); if win rate is materially worse in one bucket, shift the parameter toward the better bucket." A bounded step size (typically 5-20% per change) prevents oscillation.
2. **Weighted signal intensity.** Each entry in `signal_weights.WEIGHTABLE_SIGNALS` (~28 signals) has a weight in [0.0, 1.0]. The tuner buckets resolved predictions by whether the signal was materially present, computes differential win rate, and nudges the weight up (signal reliable) or down (signal noise). Weight 0.0 = signal completely omitted from prompt; 0.4 = mention with discount hint; 0.7 = mention without flag; 1.0 = full strength. Anti-correlated signals are auto-suppressed at 0.0.
3. **Per-regime overrides.** `regime_overrides.set_override(param, regime, value)` — a parameter can have a different value in `bull` / `bear` / `sideways` / `volatile` regimes. Tuner bucket: prediction outcomes split by regime label.
4. **Per-time-of-day overrides.** Same mechanic but bucketing by `open` (09:30-10:30 ET) / `mid` (10:30-15:00) / `close` (15:00-16:00). Names with poor open-hour win rates get a smaller `max_position_pct` during the open.
5. **Cross-profile insight propagation.** When a tuning rule applied to profile A produces a measurable improvement, the rule is offered to profile B with similar feature distribution. Implemented via `insight_propagation.py`.
6. **Adaptive AI prompt structure.** Per-profile verbosity for each prompt section (`brief` / `normal` / `detailed`). Sections with low information value for this profile (e.g. political_context for crypto profile) get truncated or omitted.
7. **Per-symbol overrides.** `symbol_overrides.set_override(param, symbol, value)` — NVDA might have a 5% stop loss while CSCO has 8%. Tuner kicks in once a symbol has ≥10 resolved predictions.
8. **Self-commissioned new strategies.** `strategy_proposer.propose_strategies` periodically generates new strategy variants by recombining successful signal patterns. New strategies enter a probationary period and run through the rigorous backtest gauntlet before being added to the live engine pool.
9. **Auto capital allocation.** Per-profile recommendation of `capital_scale` ∈ [0.5, 2.0] based on rolling Sharpe. Disabled by default; opt-in.
10. **Cost guard.** Daily AI-spend ceiling enforced cross-cutting all autonomous actions. Spend-affecting changes are surfaced as recommendations rather than auto-applied when over-budget.
11. **Alpha decay monitor.** Tracks per-strategy rolling 30-day Sharpe vs lifetime baseline. Auto-deprecates after 30+ consecutive days of degradation. Two complementary restoration paths (both implemented in `alpha_decay.py`): (a) Sharpe-based restoration when rolling Sharpe recovers to within X% of lifetime for Y consecutive days; (b) TTL-based auto-restoration after 14 days regardless of signal availability. The TTL path closes the death-spiral gap where a deprecated strategy emits no signals → can never recover its Sharpe → stays deprecated forever.
12. **Losing-week post-mortems.** When the past 7 days underperformed the long-term baseline by ≥10pt, `post_mortem.py` clusters losing predictions by feature signature and stores a learned pattern that gets injected into future prompts under "LEARNED PATTERNS."

The complete tuning rule list is in `self_tuning.py` and described in `docs/03_TRADING_STRATEGY.md`.

### Option-pipeline tuner (`OptionPipeline.tune`)

The instrument-class pipeline migration moved option-specific tuning into `pipelines/option.py:OptionPipeline.tune` so option outcomes can never pool with stock outcomes (audit finding #3). The tuner adjusts 11 parameters based on option win rate over resolved option predictions:

- **3 Greek-budget caps** — `max_net_options_delta_pct`, `max_theta_burn_dollars_per_day`, `max_short_vega_dollars` (directional / theta / vega exposure ceilings).
- **5 single-leg exit thresholds** — `option_premium_stop_loss_pct`, `option_premium_take_profit_pct`, `option_dte_exit_threshold_days`, `option_short_premium_take_profit_pct`, `option_short_premium_stop_loss_pct`. Read by `options_exits.check_single_leg_option_exits(positions, db_path, ctx=...)` on every trade-pipeline pass; module-constant defaults apply only when `ctx` is None.
- **3 `option_spread_risk` VETO thresholds** — `option_spread_iv_rank_veto_threshold`, `option_spread_gamma_dte_veto_threshold`, `option_spread_credit_ratio_veto_threshold`. Formatted into the specialist's LLM prompt so vetoes track per-profile policy, not training-time numbers.

Rule: ≥60% win rate with ≥20 samples loosens by 5%, ≤40% tightens by 5%, otherwise no change. Per-param direction is explicit — most caps loosen by going UP, but DTE-based exits and gamma-DTE/credit-ratio vetoes loosen by going DOWN (close less aggressively / veto less often). Floors and ceilings prevent runaway. Integer-stored params (DTE counts) are rounded.

### Stop-to-TP ratio rebalancer (`_optimize_stop_to_tp_ratio`)

Reads the exit-strategy distribution on closed sell rows in the last 30 days. Acceptable band: 0.5 ≤ stops/tps ≤ 2.5. When the ratio is outside, the AI auto-adjusts both ATR multipliers in one pass:
- ratio > 2.5: stops fire too often → widen `atr_multiplier_sl` (+15%), tighten `atr_multiplier_tp` (-10%)
- ratio < 0.5: TPs fire too easily → tighten `atr_multiplier_sl` (-10%), loosen `atr_multiplier_tp` (+10%)
data_quality-tagged rows are excluded so phantom-stop incidents don't pollute the asymmetry calc. Needs ≥30 attributed exits to fire.

### Per-trade TP/SL price polling

`portfolio_manager.check_stop_loss_take_profit` reads per-trade target prices (`take_profit_price`, `stop_loss_price`) propagated by `get_virtual_positions` from the entry row. Fires the moment `current_price` crosses the AI's per-trade target, bypassing the profile-level percentage. Falls back to the profile percentage when no per-trade price was set. Conviction-TP override still applies to runaway winners.

## 10. Backtesting infrastructure

Three independent layers, each serving a different question:

### 10a. Rigorous backtest gauntlet (`rigorous_backtest.py`)

Ten gates a strategy must pass before it's considered live-ready:

1. Minimum trades (≥30).
2. Win rate ≥ baseline + epsilon.
3. Sharpe ≥ baseline + epsilon.
4. Max drawdown within tolerance.
5. Profit factor ≥ 1.2.
6. Walk-forward stability (out-of-sample Sharpe ≥ 70% of in-sample).
7. Out-of-sample / in-sample disjoint splits.
8. Slippage sensitivity (P&L survives 5× slippage stress).
9. Universe survivorship correction (re-run on `historical_universe_augment` universe).
10. Regime stratification (positive in ≥3 of 4 regimes).

### 10b. Synthetic options backtester (`options_backtester.py`)

Four-layer build (Phase H1-H4 of OPTIONS_PROGRAM_PLAN):

- L1: `historical_iv_approximation` (trailing-30-day realized vol as IV proxy), `historical_spot`, `price_option_at_date` (Black-Scholes).
- L2: `simulate_single_leg` — walks one position day-by-day; closes on profit_target / stop_loss / time_stop / expiry.
- L3: `simulate_multileg_strategy` — per-leg P&L accounting (buy: exit-entry; sell: entry-exit), aggregates to position P&L.
- L4: `backtest_strategy_over_period` — replays entry rules across a historical window; returns `BacktestSummary` with per-trade detail and aggregate stats.

UI panel on the AI Brain tab runs five preset strategies (long_put, long_call, bull_call_spread, bear_put_spread, iron_condor) with parameterized OTM%, target DTE, cycle days, lookback. Equity curve rendered per trade.

### 10c. Monte Carlo backtest (`mc_backtest.py`)

Replays a list of closed trades N times (default 1,000) drawing entry + exit slippage from the bootstrap residual distribution fitted by `slippage_model.calibrate_from_history`. Two modes:

- `per_trade`: IID slippage per trade. Captures per-fill variance.
- `by_day` (default): pre-draws ONE slippage realization per `(date, side)` at sim start; trades sharing a day reuse the draw. Captures correlated-regime variance.

Output: P&L distribution (5/25/50/75/95th percentile, mean, σ, P(loss), worst case, best case). Surfaced on AI Brain tab via Run button.

### 10d. Slippage model (`slippage_model.py`)

Four-component cost model:

```
total_slippage_bps = half_spread_bps
                   + K × √participation_rate     # market impact (Almgren-Chriss)
                   + vol_factor × daily_vol_bps  # volatility scalar
                   + bootstrap_residual_bps      # empirical noise, optional
```

K is calibrated weekly per `market_type` from `trades.fill_price - trades.decision_price` pairs. Real ADV is used when `trades.adv_at_decision` is populated (post 2026-05-03); falls back to a coarse `$50M` ADV proxy for legacy rows. Bootstrap residuals are stored per size bucket for sampling at backtest time.

The slippage model is wired into:
- `backtester.py` entry/exit fills (replaces flat 0.2%).
- `mc_backtest.py` for variance estimation.
- AI prompt per-candidate `Execution: ~X bps ($Y)` annotation so the LLM sees friction cost before sizing.
- `slippage_history` API endpoint for predicted-vs-realized calibration drift tracking.

## 11. Portfolio risk model

`portfolio_risk_model.py` is a Barra-style multi-factor portfolio risk system with 21 factors:

- **Ken French daily 5-factor + Momentum** (Mkt-RF, SMB, HML, RMW, CMA, Mom). Source: free CSV from Dartmouth, cached 7 days. Reaches back to 1926.
- **11 SPDR sector ETFs** (XLK, XLF, XLE, XLV, XLI, XLP, XLY, XLU, XLB, XLRE, XLC).
- **4 MSCI USA style ETFs** (IWM, MTUM, QUAL, USMV).

Pipeline:

1. `compute_factor_returns(lookback_days=252)` — joint daily return matrix.
2. `estimate_exposures(symbol_returns, factor_returns)` — ridge-regularized OLS (α=1.0, handles ETF/Mkt-RF collinearity). Returns β vector + idiosyncratic variance + R².
3. `estimate_factor_cov` — Ledoit-Wolf shrunk covariance, manual fallback if sklearn unavailable.
4. `compute_portfolio_risk(weights, exposures, factor_cov, equity)` — factor + idio variance, parametric 95/99% VaR + ES, per-factor decomposition, grouped by sectors / styles / french / idio.
5. `monte_carlo_var(...)` — 10k Cholesky-decomposed factor draws + idio draws, empirical VaR + ES.

Daily snapshot persisted to `portfolio_risk_snapshots` (90-day retention) by `_task_portfolio_risk_snapshot`. Surfaced in AI prompt under MARKET CONTEXT > PORTFOLIO RISK.

### Stress scenarios (`risk_stress_scenarios.py`)

Seven historical windows: 1987 Black Monday, 2000 dot-com Q2, 2008 Lehman peak, 2018 Q4 selloff, 2020 COVID crash, 2022 Fed hiking cycle, 2023 SVB. `replay_scenario` projects current portfolio exposures onto historical factor returns; returns total P&L %, worst day, max drawdown, and an idio-band approximation. ETF inception dates respected (no spurious projections for ETFs that didn't exist yet).

Honest limits documented in code:
- Older scenarios (1987, dot-com) only have French factors; sector exposures projected against what overlap exists; quality flagged as "low" or "medium."
- No rates / FX / commodities in the factor set yet; 2022-style rate shocks under-report.
- Parametric VaR assumes normal returns (Monte Carlo helps but inherits factor distribution normality).

## 12. The journal

`ai_predictions` is the central asset. Every AI decision writes:

- `symbol`, `predicted_signal` (BUY / SELL / HOLD / SHORT), `confidence` (0-100), `reasoning` (LLM rationale)
- `features_json` — full feature snapshot the AI saw, ~80 fields
- `prediction_type` (`directional_long` / `directional_short` / `exit_long` / `exit_short`)
- `price_at_prediction`, `price_targets` (stop, take_profit)
- `created_at`, `status` (pending / resolved)

When resolved (`ai_tracker.resolve_predictions`), the row gets:

- `actual_outcome` (`win` / `loss` / `neutral`)
- `actual_return_pct`, `resolution_price`, `days_held`, `resolved_at`

Resolution rules are per-direction (`_resolve_one`): a `directional_long` prediction is a win if price exceeded its take-profit target before its stop loss; a `directional_short` is the inverse; `exit_long` (a SELL on a held long) is a win if the post-exit return was favorable for the seller; etc.

The journal is the single source of truth for everything downstream: meta-model training, specialist calibration, self-tuner buckets, post-mortem clustering, learned patterns, alpha decay tracking. **It is also the proprietary asset competitors cannot replicate.**

## 13. Cost discipline

The system is engineered to operate on a **per-user daily AI ceiling** that defaults to `max($5, trailing_7d_avg × 1.5)` via `cost_guard.py` and is operator-overridable in Settings. Observed steady-state spend at the current `gemini-2.5-flash-lite` default model across the 13-profile experiment fleet runs at roughly $0.30/day. Three quality levers keep this number low:

1. **Persistent shared cache** (`shared_ai_cache.py`) — ensemble + political-context responses cached in SQLite, surviving scheduler restarts.
2. **Meta-model pre-gate** (§3) — drops low-prob candidates before specialist fan-out.
3. **Per-profile specialist disable list** (§4) — anti-calibrated specialists skipped automatically.

The cost guard (`cost_guard.py`) enforces a per-user daily AI-spend ceiling. Hard block at the `ai_providers.call_ai` / `call_ai_structured` boundary — every AI call is gated against a worst-case cost estimate before the provider is invoked; over-budget calls raise `CostCapExceeded` and the cycle skips the AI step. Three self-tuner sites also gate advisorily (over-budget tuner actions become `Recommendation: cost-gated` strings instead of auto-applying). Ceiling = user's `daily_cost_ceiling_usd` override or auto-computed `max($5, trailing_7d_avg × 1.5)`.

## 14. AI provider portability

Three providers wired (`ai_providers.py`): Anthropic Claude (Haiku, Sonnet, Opus), OpenAI GPT, Google Gemini. Default model per profile is configurable; the per-profile `ai_model_auto_tune` toggle (off by default) lets the tuner A/B-test alternative models within the daily cost ceiling.

**Structured-output enforcement.** Every provider call is wrapped so the model is forced to return parseable JSON, not free-form prose:
- Anthropic: tool-use schema in `call_ai_structured` (see §`_call_anthropic`).
- OpenAI: `response_format={"type": "json_object"}` is the default.
- Google Gemini: `config={"response_mime_type": "application/json", ...}` in `_call_google`. Required on `gemini-2.5-flash-lite` or the model intermittently emits markdown preamble ("Here's an evaluation…"), which downstream parsers reject with `JSONDecodeError`, triggering the provider retry chain and inflating cycle time. See CHANGELOG 2026-05-20 PM.

## 15. What's deliberately not in the AI system

- **No reinforcement learning loop.** The system is a stacked prediction-and-decision pipeline, not an RL agent. The "feedback loop" is supervised: resolve labeled predictions, retrain models. This is a deliberate choice; see `docs/10_METHODOLOGY.md`.
- **No prompt-learning / fine-tuning.** The LLM is used as a frozen-weights frontier policy; calibration happens externally via the meta-model and specialist Plat scaling.
- **No latency optimization.** The system runs on an operator-tunable cycle (default 15 min; selectable 15 / 10 / 5 / 3 / 2 min via Settings → AI Behavior, persisted to `users.scan_interval_minutes`). Sub-second execution is out of scope; tighter cadence is for operator preference, not latency arbitrage.

## See also

- `docs/03_TRADING_STRATEGY.md` — what the system actually trades, in finance terms.
- `docs/05_DATA_DICTIONARY.md` — every feature, signal, column, knob.
- `docs/10_METHODOLOGY.md` — the system's epistemic stance.
- `docs/11_INTEGRATION_GUIDE.md` — adding a new specialist or strategy.
