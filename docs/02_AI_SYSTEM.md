# 02 — AI System

**Audience:** quants, ML researchers, anyone who builds production prediction systems.
**Prerequisites:** familiarity with gradient-boosted trees, online learning, calibration, ensemble methods.
**Last updated:** 2026-05-03.

## Overview

QuantOpsAI's AI architecture is a **stacked decision pipeline** in which a frontier LLM acts as the apex policy, and a portfolio of supporting models (ensemble specialists, meta-model, calibrators, online learner) provide the LLM with calibrated probabilistic context, suppress likely-incorrect candidates before they reach the LLM, and re-weight the LLM's confidence after the fact. Every decision the system makes is captured in a journal, resolved against price action, and used to train every layer.

The pipeline is summarized as:

```
Universe (8,000 symbols) →
    Strategy votes (20+ engines) →
        Pre-rank shortlist (~30) →
            Meta-model pre-gate (drops low-prob candidates) →
                Specialist ensemble (5 calibrated specialists) →
                    LLM batch decision (the apex policy) →
                        Validation gates (hard rules) →
                            Execution →
                                Journal (decision + features) →
                                    Resolution (win/loss labeling) →
                                        Feedback loops (training data for every layer above)
```

Each layer is documented below. Section sequence follows the data flow.

## 1. Universe construction

Source: Alpaca's `/v2/assets` endpoint (US equities + ETFs, ~8,000 active symbols), filtered per-profile by:

- Market type (mid-cap / small-cap / micro-cap / large-cap / crypto / shorts variants), via `segments.py` and `segments_historical.py`.
- Active-status flag (delisted / merged / renamed names are removed from live universe but kept in `historical_universe_additions` for backtest survivorship-bias correction).
- Per-profile custom watchlist (additions) and exclusion list (removals).

The dynamic-universe layer is described in detail in `docs/04_TECHNICAL_REFERENCE.md`.

## 2. Strategy engines

20+ deterministic per-symbol strategies live in `strategies/*.py`. Each is a pure function `run(symbol, market_type, df, params) → {signal, score, ...}` returning a vote. They are organized as:

- **Bullish strategies (16):** momentum_breakout, volume_spike, mean_reversion, gap_and_go, gap_reversal, news_sentiment_spike, short_squeeze_setup (long side), earnings_drift, insider_cluster, fifty_two_week_breakout, macd_cross_confirmation, sector_momentum_rotation, analyst_upgrade_drift, short_term_reversal, volume_dryup_breakout, max_pain_pinning.
- **Bearish strategies (10):** breakdown_support, distribution_at_highs, failed_breakout, parabolic_exhaustion, relative_weakness_in_strong_sector, earnings_disaster_short, catalyst_filing_short, sector_rotation_short, iv_regime_short, relative_weakness_universe, insider_selling_cluster, high_iv_rank_fade, vol_regime.

Strategy votes are deterministic; they're cheap (no LLM cost) and run on every cycle. Each strategy emits one of: STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, SHORT, STRONG_SHORT.

The shortlist is built by aggregating strategy votes per symbol, computing a composite score (defined in `multi_strategy.py`), and retaining the top ~30 candidates with two reserved slots: top-10 longs and top-5 shorts (when shorts are enabled for the profile). This protects short-side opportunities from being crowded out by a long-dominated vote pool.

## 3. Meta-model pre-gate (Lever 2)

Before each candidate reaches the specialist ensemble — which costs API tokens — the meta-model evaluates whether the AI is likely to be **right** on this candidate given the full feature payload. Candidates whose predicted probability of success is below `meta_pregate_threshold` (default 0.5) are dropped silently.

This is a cost-and-quality lever: it cuts specialist API calls roughly in half once the meta-model is trained, and it also functions as a quality filter — candidates the meta-model strongly suspects are noise don't pollute the specialist consensus.

The meta-model itself is described in §6 below.

## 4. Specialist ensemble

The ensemble (`ensemble.py`) consists of five specialist LLMs, each instantiated with the same backend model but a differentiated system prompt and feature subset:

| Specialist | Role | Veto authority |
|---|---|---|
| `earnings_analyst` | Reasons about earnings calendar, surprise streak, transcript sentiment, days-to-PDUFA, days-to-earnings | No |
| `pattern_recognizer` | Reasons about technical patterns, price action, volume confirmation | No |
| `sentiment_narrative` | Reasons about news, Reddit, StockTwits, congressional trades, attention signals | No |
| `risk_assessor` | Reasons about correlation, concentration, regime fit, drawdown context | **Yes** |
| `adversarial_reviewer` | Red-teams the trade thesis: what's the failure mode, what's the bear case, where does this go wrong | **Yes** |

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

After the candidate list has been pre-gated (meta-model), enriched (specialist ensemble), and contextualized (portfolio state + market context + per-stock memory), the system makes **one batched LLM call per scan cycle** via `ai_analyst.ai_select_trades`. The prompt contains:

- **Candidate block** for each of the (typically 5-15) survivors, including: technical indicators, options oracle summary (IV rank, term structure, skew, GEX, max pain, implied move), alternative data (insider, short interest, options flow, intraday patterns, congressional, 13F, biotech, StockTwits, Google Trends, Wikipedia views, App Store ranks), specialist ensemble verdicts, per-stock track record by signal type, last prediction reasoning, earnings warning, SEC alerts, news headlines, slippage estimate, borrow rate (for shorts).
- **Portfolio state block:** equity, cash, current positions, exposure breakdown (sector + factor + direction), book beta (current vs target), Kelly recommendations per direction, drawdown capital scale, risk-budget per-position contributions, MFE capture ratio, sector concentration warnings.
- **Market context block:** regime label + VIX, SPY trend, sector rotation (5-day returns), crisis level (with size multiplier), macro context (yield curve, CBOE skew, FRED indicators, ETF flows), portfolio risk readout (daily σ, 95% VaR, ES, top factor exposures, worst-3 stress scenarios), next macro event (FOMC/CPI/NFP), long-vol hedge state (when active).
- **Long/short balance target** and **book-beta target** with directives ("UNDERSHORTED — pick a SHORT this cycle"; "BETA TOO HIGH — DEFENSIVE picks long or LEVERED shorts").
- **Learned patterns** from prior post-mortems and self-tuner findings.
- **Track record** aggregated and split by signal type to prevent confabulation (e.g., the AI cannot claim "100% win rate on VALE shorts" when all 13 wins were HOLDs).
- **Allowed actions** dynamically scoped: BUY, HOLD; plus SHORT (when enabled), OPTIONS (when any candidate has tradeable options or the advisor surfaced an opportunity), PAIR_TRADE (when stat-arb book has actionable pairs), MULTILEG_OPEN (when multi-leg advisor has surfaced one).

The prompt structure is verbosity-tunable per profile via `prompt_layout.set_verbosity` (Layer 6 of the self-tuning stack — see §9).

The LLM's response is parsed by `_parse_ai_response_strict_json`, which is a defensive parser tolerant of common malformations (markdown fences, single quotes, trailing commas). Strict-JSON parse failure logs the response and skips the cycle without any trades.

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

The 12 layers:

1. **Parameter coverage.** ~35 numeric parameters on `trading_profiles` (stop loss, take profit, max position, RSI thresholds, gap threshold, volume floors, max correlation, max sector positions, drawdown thresholds, etc.) Each has a tuning rule in `self_tuning.py` of the form: "buckets of recent resolved predictions by parameter value range; if win rate is materially worse in one bucket, shift the parameter toward the better bucket." A bounded step size (typically 5-20% per change) prevents oscillation.
2. **Weighted signal intensity.** Each entry in `signal_weights.WEIGHTABLE_SIGNALS` (~28 signals) has a weight in [0.0, 1.0]. The tuner buckets resolved predictions by whether the signal was materially present, computes differential win rate, and nudges the weight up (signal reliable) or down (signal noise). Weight 0.0 = signal completely omitted from prompt; 0.4 = mention with discount hint; 0.7 = mention without flag; 1.0 = full strength. Anti-correlated signals are auto-suppressed at 0.0.
3. **Per-regime overrides.** `regime_overrides.set_override(param, regime, value)` — a parameter can have a different value in `bull` / `bear` / `sideways` / `volatile` regimes. Tuner bucket: prediction outcomes split by regime label.
4. **Per-time-of-day overrides.** Same mechanic but bucketing by `open` (09:30-10:30 ET) / `mid` (10:30-15:00) / `close` (15:00-16:00). Names with poor open-hour win rates get a smaller `max_position_pct` during the open.
5. **Cross-profile insight propagation.** When a tuning rule applied to profile A produces a measurable improvement, the rule is offered to profile B with similar feature distribution. Implemented via `insight_propagation.py`.
6. **Adaptive AI prompt structure.** Per-profile verbosity for each prompt section (`brief` / `normal` / `detailed`). Sections with low information value for this profile (e.g. political_context for crypto profile) get truncated or omitted.
7. **Per-symbol overrides.** `symbol_overrides.set_override(param, symbol, value)` — NVDA might have a 5% stop loss while CSCO has 8%. Tuner kicks in once a symbol has ≥10 resolved predictions.
8. **Self-commissioned new strategies.** `strategy_proposer.propose_strategies` periodically generates new strategy variants by recombining successful signal patterns. New strategies enter a probationary period and run through the rigorous backtest gauntlet before being added to the live engine pool.
9. **Auto capital allocation.** Per-profile recommendation of `capital_scale` ∈ [0.5, 2.0] based on rolling Sharpe. Disabled by default; opt-in.
10. **Cost guard.** Daily AI-spend ceiling enforced cross-cutting all autonomous actions. Spend-affecting changes are surfaced as recommendations rather than auto-applied when over-budget.
11. **Alpha decay monitor.** Tracks per-strategy rolling 30-day Sharpe vs lifetime baseline. Auto-deprecates after 30+ consecutive days of degradation; auto-restores after 14+ days of recovery. Implemented in `alpha_decay.py`.
12. **Losing-week post-mortems.** When the past 7 days underperformed the long-term baseline by ≥10pt, `post_mortem.py` clusters losing predictions by feature signature and stores a learned pattern that gets injected into future prompts under "LEARNED PATTERNS."

The complete tuning rule list is in `self_tuning.py` and described in `docs/03_TRADING_STRATEGY.md`.

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

The system is engineered to operate on a $1.50-2.00/day AI budget across ten profiles. Three levers documented in the (now archived) COST_AND_QUALITY_LEVERS_PLAN:

1. **Persistent shared cache** (`shared_ai_cache.py`) — ensemble + political-context responses cached in SQLite, surviving scheduler restarts.
2. **Meta-model pre-gate** (§3) — drops low-prob candidates before specialist fan-out.
3. **Per-profile specialist disable list** (§4) — anti-calibrated specialists skipped automatically.

The cost guard (`cost_guard.py`) enforces a daily ceiling cross-cutting all autonomous actions.

## 14. AI provider portability

Three providers wired (`ai_providers.py`): Anthropic Claude (Haiku, Sonnet, Opus), OpenAI GPT, Google Gemini. Default model per profile is configurable; the per-profile `ai_model_auto_tune` toggle (off by default) lets the tuner A/B-test alternative models within the daily cost ceiling.

## 15. What's deliberately not in the AI system

- **No reinforcement learning loop.** The system is a stacked prediction-and-decision pipeline, not an RL agent. The "feedback loop" is supervised: resolve labeled predictions, retrain models. This is a deliberate choice; see `docs/10_METHODOLOGY.md`.
- **No prompt-learning / fine-tuning.** The LLM is used as a frozen-weights frontier policy; calibration happens externally via the meta-model and specialist Plat scaling.
- **No latency optimization.** The system runs on a 5-15 minute cycle. Sub-second execution is out of scope.

## See also

- `docs/03_TRADING_STRATEGY.md` — what the system actually trades, in finance terms.
- `docs/05_DATA_DICTIONARY.md` — every feature, signal, column, knob.
- `docs/10_METHODOLOGY.md` — the system's epistemic stance.
- `docs/11_INTEGRATION_GUIDE.md` — adding a new specialist or strategy.
