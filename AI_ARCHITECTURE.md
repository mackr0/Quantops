# QuantOpsAI — AI Architecture

Everything the AI does, end to end. This document is exhaustive on
purpose: the system has grown across a lot of waves and "what does
the AI actually do" should never require code-spelunking to answer.

The system is built around three principles:

1. **Decisions, not recommendations.** Wherever the AI or the tuner
   identifies something worth changing, it changes it (within
   bounded safety guards). Recommendation-only paths are forbidden
   except for a tiny allowlist (see §Safety).
2. **Layered overrides at decision time.** Every parameter the
   pipeline reads is resolved through a precedence chain
   (per-symbol → per-regime → per-time-of-day → profile global → caller default).
   The system can express different behavior in different contexts
   without humans maintaining N copies of every profile.
3. **Cost discipline as a first-class concern.** Every spend-affecting
   autonomous action checks a daily cost ceiling. Over-budget changes
   surface as the only allowed `Recommendation: cost-gated` strings.

---

## At a Glance

```
  USER ──────────────► Strategic settings (AI provider, schedule,
                       cost ceiling, opt-in toggles, profile identity)
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  TRADING PIPELINE (per profile, every scan cycle)            │
  │                                                              │
  │  Screener (rule-based) → 16 strategies vote (rule-based) →  │
  │  Specialist Ensemble (4 AI calls) → Batch Trade Selector    │
  │  (1 AI call) → Order Execution → Internal Ledger            │
  │                                                              │
  │  At every read of a tunable parameter:                       │
  │    resolve_for_current_regime(profile, name, symbol=...)     │
  │    → checks per-symbol → per-regime → per-TOD → global       │
  │  Position size further multiplied by capital_scale           │
  │  (Layer 9, opt-in, per-Alpaca-account-conserving)            │
  └─────────────────────────┬───────────────────────────────────┘
                            │  resolved predictions
                            ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  AUTONOMOUS TUNING SYSTEM (daily, per profile)              │
  │                                                              │
  │  Layer 1: 35+ parameters tuned with cooldown/reverse/clamp  │
  │  Layer 2: Per-signal weights (1.0/0.7/0.4/0.0 ladder)       │
  │  Layer 3: Per-regime overrides (bull/bear/sideways/         │
  │           volatile/crisis)                                   │
  │  Layer 4: Per-time-of-day overrides (open/midday/close)     │
  │  Layer 5: Cross-profile insight propagation                 │
  │  Layer 6: Adaptive prompt structure (cost-gated)            │
  │  Layer 7: Per-symbol overrides (most-specific tier)         │
  │  Layer 8: Self-commissioned new strategies (cost-gated)     │
  │  Layer 9: Auto capital allocation (opt-in, per-account)     │
  │                                                              │
  │  + Cost guard wraps every spend-affecting action             │
  │  + 6 anti-regression guardrail tests                         │
  │  + Post-mortem pattern extraction on losing weeks            │
  │  + False-negative analysis on rejected trades                │
  └──────────────────────────────────────────────────────────────┘
```

---

## Part 1 — The Trading Pipeline

### 1a. The 7 AI Agent Types (per scan cycle)

Every scan cycle makes ~13–14 AI calls across 7 distinct agents.
Each agent has a single job; the orchestration is rule-based.

| Agent | Purpose | When | Model | Cost label |
|---|---|---|---|---|
| **Earnings Analyst** | Evaluate fundamentals — recent earnings, revenue trends, guidance. Abstains when no data (rather than guessing). | Per scan, batched 5 candidates | Claude Haiku | `ensemble:earnings_analyst` |
| **Pattern Recognizer** | Read technical chart patterns — price action, volume, S/R, momentum | Per scan, batched 5 | Claude Haiku | `ensemble:pattern_recognizer` |
| **Sentiment & Narrative** | Evaluate the story — news headlines, sector winds, macro | Per scan, batched 5 | Claude Haiku | `ensemble:sentiment_narrative` |
| **Risk Assessor** | The skeptic. Concentration risk, regulatory exposure, event risk, regime sensitivity. Has VETO authority. | Per scan, batched 5 | Claude Haiku | `ensemble:risk_assessor` |
| **Batch Trade Selector** | Final decision-maker. Sees all candidates + ensemble verdicts + portfolio + macro. Picks 0-3 trades, sizes them. | Once per scan | Claude Haiku (configurable) | `batch_select` |
| **Political Context Analyst** | Tariff developments, executive orders, political news. Output injected into Batch Selector context. | Once per scan if MAGA Mode on (skipped for crypto) | Claude Haiku | `political_context` |
| **Strategy Proposer** | Generate new strategy specs as JSON. AI never writes code, only parameters against a closed allowlist. | Weekly (Sundays) + on-demand when tuner detects a coverage gap | Claude Haiku | `strategy_proposal` |

**Supporting AI calls (event-driven, not every cycle):**

| Agent | When | Cost label |
|---|---|---|
| Single-Symbol Analyzer | Event-driven (price shock, SEC filing) | `single_analyze` |
| Consensus Secondary Model | If profile has multi-model consensus enabled | `consensus_secondary` |
| SEC Filing Diff Analyzer | When new 10-K/10-Q/8-K is detected | `sec_diff` |
| Earnings Call Sentiment | When new 8-K press release lands | `transcript_sentiment` |
| Portfolio Review | Periodic holistic review | `portfolio_review` |

### 1b. Decision Flow

```
Screener (no AI)            8,000+ symbols → ~15 candidates
                              based on price/volume/technical filters
        │
        ▼
21-strategy voting           Each strategy independently votes
(no AI)                      BUY/SELL/SHORT/HOLD per candidate.
                              Votes aggregated into a score.
                              Includes 5 dedicated bearish strategies
                              (breakdown_support, distribution_at_highs,
                              failed_breakout, parabolic_exhaustion,
                              relative_weakness_in_strong_sector) —
                              see LONG_SHORT_PLAN.md Phase 1.1.
        │
        ▼
Meta-model pre-gate          For each candidate, the per-profile
(no AI; loads existing      meta-model returns P(this prediction is
 model)                       correct). Candidates with meta_prob below
                              `meta_pregate_threshold` (default 0.5)
                              are dropped BEFORE the ensemble fires.
                              Falls open if no model is trained yet.
                              See COST_AND_QUALITY_LEVERS_PLAN.md
                              Lever 2.
        │
        ▼
4 specialists vote in        Batched in groups of 5 candidates so
parallel (≤4 AI calls)        no candidate is dropped. Each returns
                              verdict + confidence + reasoning.
                              Per-profile `disabled_specialists` skips
                              specialists whose calibration data shows
                              anti-correlation (Lever 3). Hard floor:
                              ≥2 active specialists per profile.
        │
        ▼
Ensemble synthesizer         Confidence-weighted vote across remaining
(no AI; applies              specialists. EACH specialist's confidence
 calibration)                 is mapped through its per-profile
                              Platt-scaling layer (raw 90 → empirical
                              P(correct), e.g., 28% on Small Cap for
                              pattern_recognizer). Risk Assessor's
                              VETO overrides everything.
        │
        ▼
Batch Trade Selector         Sees: candidates, calibrated ensemble
(1 AI call)                   verdicts, portfolio state, market regime,
                              political context, learned patterns,
                              post-mortems, per-stock track records
                              SPLIT BY SIGNAL TYPE
                              (e.g., "VALE: BUY 0W/0L; SHORT 0W/0L;
                              HOLD 13W/0L" — prevents the AI from
                              attributing HOLD outcomes to a SHORT
                              decision narrative).
                              Picks 0-3 trades, sizes them.
        │
        ▼
Meta-model re-weighting      Final per-trade meta_prob check. Trades
(no AI)                      with meta_prob < SUPPRESSION_THRESHOLD
                              (0.3) are dropped. Confidence on
                              survivors blended with meta_prob.
        │
        ▼
Parameter resolution         For each parameter the executor needs
(per symbol)                  (max_position_pct, stop_loss_pct, etc.):
                              resolve_for_current_regime(ctx, name,
                                                          symbol=symbol)
                              → per-symbol > per-regime > per-TOD > global
                              → × capital_scale (Layer 9 multiplier)
        │
        ▼
Order execution              Trades sent to Alpaca. Internal ledger
                              records everything for metrics, tuning,
                              and meta-model training. Order rejections
                              are logged with full traceback (added
                              2026-04-28 after silent-swallow incident).
                              Short covers subtract accrued borrow
                              cost (`short_borrow.py`) before P&L log.
                              MFE per open position updated every
                              Check Exits cycle (long: max price
                              reached; short: min price reached) →
                              feeds the trailing-stop tuner.
```

### 1c. What the AI Sees per Candidate

19 alternative-data signals + 13 technical indicators + market context
+ per-stock memory. Detailed list lives in the AI page's "What the
AI Sees" reference panel; the canonical signal registry is in
`signal_weights.WEIGHTABLE_SIGNALS`. Each signal can be omitted or
discounted per profile via Layer 2 weights (see §Part 2).

**Per-stock memory is split by signal type** (added 2026-04-28). For
each candidate the prompt's `track_record` field reads:

```
13W/0L overall (100%) — BUY 0W/0L (0%); SHORT 0W/0L (0%); HOLD 13W/0L (100%)
```

Without the split, the prompt previously emitted just `13W/0L
(100%)` and the AI would attribute that aggregate record to whatever
signal it was currently considering. (Example incident:
`SHORT VALE` reasoning included "100% personal win rate (13W/0L) on
VALE SHORT signals" when zero SHORTs had ever been resolved on
VALE — all 13 wins were HOLDs.) The signal-split prompt makes that
class of confabulation structurally impossible.

**Days to earnings** (added 2026-04-27). `features_payload[days_to_earnings]`
is captured at prediction time via `earnings_calendar.check_earnings(symbol)`.
Used by the self-tuner's `_optimize_avoid_earnings_days` rule to
bucket resolved predictions by proximity to earnings. Older
predictions (pre-2026-04-27) carry `-1` and are excluded from the
buckets.

**Local-SQLite alt-data signals (added 2026-04-26):** four data
sources are refreshed daily by the standalone projects in
`/opt/quantopsai-altdata/{project}/`. The QuantOpsAI read layer in
`alternative_data.py` queries those SQLite databases at decision time
(6-hour cache). Each helper gracefully no-ops when the DB is missing
or empty.

| Helper | Source | Per-symbol output |
|---|---|---|
| `get_congressional_recent` | `congresstrades` (Senate eFD + House Clerk STOCK Act) | 60-day count of buys/sells, dollar volume, party split, last filing date, net direction |
| `get_13f_institutional` | `edgar13f` (SEC 13F-HR XML) | Latest-quarter total holders, total shares, top holder name, QoQ share-change % |
| `get_biotech_milestones` | `biotechevents` (ClinicalTrials.gov v2 + PDUFA tracker) | Days to next PDUFA, drug name, active phase-3 count, recent phase change |
| `get_stocktwits_sentiment` | `stocktwits` (StockTwits REST API) | 7-day net sentiment, message volume vs 30-day average, currently-trending flag |

The AI prompt builder wraps each new signal in `_weighted_signal_text`
so Layer 2 weights apply (omitted at 0.0, discount-hint decorated at
0.4/0.7). The 4 signals are also flattened into `features_payload`
so the meta-model trains on them.

### 1d. Cost per Cycle

| Agent | Calls | Est. cost |
|---|---|---|
| Earnings Analyst | ≤3 (5-candidate batches; abstains when no candidate has imminent earnings) | ~$0.003 |
| Pattern Recognizer | ≤3 (skipped per-profile if `disabled_specialists` includes it) | ~$0.003 |
| Sentiment & Narrative | ≤3 | ~$0.003 |
| Risk Assessor | ≤3 | ~$0.003 |
| Batch Trade Selector | 1 | ~$0.001 |
| Political Context | 1 (if MAGA on; cached 30 min cross-restart) | ~$0.001 |
| **Total** | **≤13** | **~$0.014** |

**Three cost levers shipped 2026-04-27 (`COST_AND_QUALITY_LEVERS_PLAN.md`):**

1. **Persistent shared cache** (`shared_ai_cache.py`) — ensemble +
   political context cached to SQLite, not just module-level dicts.
   Survives scheduler restarts. Saves ~$0.50/day on deploy-heavy days.
2. **Meta-model pre-gate** — drops candidates with meta_prob < 0.5
   BEFORE the ensemble fires. Cuts specialist call count by ~50%
   on profiles with trained meta-models. Saves ~$0.30-0.40/day.
3. **Per-profile specialist disable** — anti-calibrated specialists
   (e.g., pattern_recognizer on small-cap profiles per the
   2026-04-27 calibrator findings) skip their API call entirely.
   Daily `_task_specialist_health_check` auto-(dis)enables based
   on Platt-scaling slope. Hard floor: ≥2 active per profile.
   Saves ~$0.40/day once auto-disable fires.

**Other recent cost fixes:**

- `transcript_sentiment` cache: 24h → 30 days (earnings transcripts
  are quarterly events; saves ~$0.30/day, fixed 2026-04-27).

**Projected normal-cadence daily AI spend with all levers active:**
$1.50-$2.00 across 10 profiles. Cost guard ceiling defaults to
trailing-7-day-avg × 1.5 (floor $5/day); user-configurable.

---

## Part 2 — The Autonomous Tuning System (12 Layers)

The system continuously adjusts how it trades based on its own
performance. Every layer below is active in production; safety
scaffolding (cooldown, reverse-if-worsened, bound clamping, cost
gate) is shared across all of them.

### Layer 1 — Parameter Coverage

**35+ parameters** auto-tuned with detection rules. Categories:

- **AI behavior**: confidence threshold (band-search), maga_mode (auto-disable when underperforming), enable_short_selling (auto-disable on persistent losses)
- **Sizing & concentration**: max_position_pct, max_total_positions, max_correlation, max_sector_positions, drawdown_pause_pct, drawdown_reduce_pct, min_price/max_price (band tuning)
- **Exits**: stop_loss_pct, take_profit_pct, short_stop_loss_pct, short_take_profit_pct, ATR multipliers (SL/TP/trailing)
- **Entry filters**: min_volume, volume_surge_multiplier, breakout_volume_threshold, gap_pct_threshold, momentum_5d_gain, momentum_20d_gain, rsi_overbought, rsi_oversold
- **Time-of-day**: skip_first_minutes (buckets resolved predictions by minutes-since-open from timestamp; tightens / loosens based on opening-window WR delta)
- **Earnings proximity**: avoid_earnings_days (buckets by `days_to_earnings` from features_json; tightens when in-window predictions underperform, loosens when in-window outperforms — catches post-earnings drift setups)
- **Trailing stop**: trailing_atr_multiplier (uses `max_favorable_excursion` per closed long to compute give-back % — tightens when avg give-back > 50%, loosens when winners exit near peak)
- **Strategy toggles**: 4 legacy + auto-deprecation of all 16+ modular strategies via alpha_decay (auto-restore on Sharpe recovery)

Each parameter has explicit bounds in `param_bounds.PARAM_BOUNDS`. Tuner clamps every change to these bounds. Per-rule cooldown (3-day default, 7 for per-symbol overrides, 14 for prompt rotation). Reverse-if-worsened automatic. Self-tuner adjustments are gated by a 14-day validation window (added 2026-04-27) — proposed changes only apply if they would have helped on recent data.

### Layer 2 — Weighted Signal Intensity

Every signal the AI sees has a per-profile weight on a 4-step ladder (`1.0 → 0.7 → 0.4 → 0.0`). 21 weightable signals to start. Tuner buckets resolved predictions by "was this signal materially present" and nudges the weight up/down based on differential WR.

Prompt builder reads the weight when formatting each signal:
- `1.0`: present as today
- `0.7` / `0.4`: present + `[intensity 0.4]` hint
- `0.0`: omit entirely

### Layer 3 — Per-Regime Overrides

Every parameter can have regime-specific values (bull / bear / sideways / volatile / crisis). Detected via `market_regime.detect_regime()`. Tuner creates per-regime overrides when a parameter behaves materially differently across regimes (≥10 samples per regime, ≥12pt WR divergence).

### Layer 4 — Per-Time-of-Day Overrides

Same pattern as regime, bucketed intraday: open (09:30–10:30), midday (10:30–14:30), close (14:30–16:00) ET. Tuner detects time-of-day-specific patterns and creates per-bucket overrides.

### Layer 5 — Cross-Profile Insight Propagation

When an adjustment turns out to improve a profile's WR (`outcome_after = 'improved'`), the same detection rule runs against every OTHER enabled profile belonging to the same user. Each peer's own data must independently support the change — **no value-copying**. Fleet learns ~10× faster than profiles in isolation, with zero new API spend.

### Layer 6 — Adaptive AI Prompt Structure

The prompt's section verbosity per profile becomes tunable. 4 sections (alt_data, political_context, learned_patterns, portfolio_state); 3 verbosity levels (brief / normal / detailed). Tuner rotates one section's verbosity every 14 days to test which framing improves WR. Cost-gated — moves toward `detailed` (longer prompts → more tokens) checked against the daily ceiling.

### Layer 7 — Per-Symbol Overrides

Most-specific tier. For symbols with ≥20 individual resolved predictions and ≥15pt WR divergence, the tuner creates per-symbol overrides. NVDA's optimal stop-loss is not KO's. 7-day cooldown to prevent over-fitting on small samples.

### Layer 8 — Self-Commissioned New Strategies

Tuner detects coverage gaps — winning AI predictions where no strategy fired — and triggers Phase 7's strategy_proposer with a focused brief describing the gap. Heavily cost-gated. Rate-limited to ≤1 per profile per week. The proposed spec flows through Phase 7's existing pipeline (proposed → validated → shadow → active).

### Layer 9 — Auto Capital Allocation (Opt-In)

User flips `auto_capital_allocation` ON; weekly Sunday task rebalances per-profile `capital_scale` multipliers. **Critical: respects shared Alpaca accounts.** Profiles that share one real account have their scales normalized so the sum within the group equals N (the count) — the underlying real account is never over-committed. Solo profiles always at 1.0. Bounds: per-rebalance ±50%, absolute ∈ [0.25, 2.0]. Score = `recent_sharpe × (1 + win_rate)`.

### The Cost Guard (Cross-Cutting)

`cost_guard.py` defines a per-user daily API spend ceiling (default = trailing-7-day-avg × 1.5, floor $5). Every cost-affecting autonomous action calls `can_afford_action(user_id, est_extra_usd)`. If False, surfaced as `Recommendation: cost-gated — ...` with explicit cost estimate — the only Recommendation prefix allowed by the guardrail tests.

---

## Part 3 — Closed-Loop Learning Surfaces

Beyond the parameter-tuning loop, three other feedback systems operate:

### 3a. Meta-Model

Gradient-boosted classifier (`meta_model.py`) trained daily per profile on accumulated resolved predictions. Learns "given everything the main AI saw, was it right?" Outputs a probability used to re-weight the AI's confidence at decision time. Needs ≥100 resolved predictions to train. Reports AUC + accuracy + feature importance (visible in the AI page Brain tab).

The meta-model and the self-tuner are complementary:
- Meta-model: per-prediction probability adjustment (micro)
- Self-tuner: per-parameter / per-signal / per-regime adjustment (macro)

### 3b. Alpha Decay Monitor (`alpha_decay.py`)

Tracks each strategy's rolling 30-day Sharpe vs lifetime baseline. Auto-deprecates when rolling Sharpe degrades for 30+ consecutive days; auto-restores when rolling Sharpe recovers for 14+ days. The self-tuner integrates: when it identifies a non-toggleable strategy underperforming, it calls `alpha_decay.deprecate_strategy()` directly. Manual restore via Restore button on AI page Strategy tab.

### 3c. Losing-Week Post-Mortems (`post_mortem.py`)

Weekly Sunday task per profile. When the past 7 days underperformed the long-term baseline by ≥10pt, clusters losing predictions by feature signature, identifies the dominant pattern (e.g., "60% of losses had insider_cluster=high AND vwap_position=below"), stores it as a `learned_pattern`. The trade pipeline injects active patterns into the AI prompt's `LEARNED PATTERNS` section so the AI sees the post-mortem learning at decision time.

### 3d. False-Negative Analysis

Tuner rule that scans HOLD predictions resolved as `loss` (price moved enough that we missed an opportunity). When ≥60% of recent HOLD-losses had confidence in the marginal band just below the current threshold, it lowers the threshold by 5. Catches the case where the AI is rejecting trades it should be taking.

### 3e. Specialist Confidence Calibration (`specialist_calibration.py`)

Per-specialist Platt-scaling layer fitted daily from accumulated
`specialist_outcomes` (one row per specialist per resolved
prediction). Maps raw confidence (0-100) to empirical P(correct)
using a 1-feature logistic regression on the last 90 days.

**What it surfaced 2026-04-27:** `pattern_recognizer` is *inversely
calibrated* on every primary equity profile (raw=90 → cal≈28 on
Mid/Small/Small-Shorts). When the specialist is loudly confident,
its empirical hit rate is in the high-20s. The calibration layer
attenuates its vote weight, but the synthesizer math can't flip
the sign — that's why Lever 3 (per-profile disable) was added.

**Daily auto-(dis)enable** via
`multi_scheduler._task_specialist_health_check`. With ≥50 resolved
samples for a specialist:
- DISABLE if calibrator maps raw=90 → cal<35 (clear anti-signal)
- RE-ENABLE a previously-disabled specialist if calibrator
  recovers to raw=90 → cal>50 (slope flipped back)
- Hard floor: never disable below 2 active specialists per profile.

The disable list lives in `trading_profiles.disabled_specialists`
(JSON array). Skipped specialists' API calls don't fire (cost
saving), and their slot in the synthesizer is treated as ABSTAIN
(no synthesizer pollution).

### 3f. Backtest Survivorship-Bias Correction

The `historical_universe_augment.py` daily diff captures symbols
as they fall off Alpaca's active asset list, so backtests over
windows that include a delisted symbol's `last_seen_active` date
include it in the universe. Combined with `segments_historical.py`
(frozen baseline of segments.py as of 2026-04-27), backtests no
longer silently survivorship-bias-up by excluding names that
delisted/merged/renamed since.

The frozen baseline carries everything dead-or-alive as of today;
auto-augmentation accumulates every future death. Backtest universe
= baseline ∪ {additions where last_seen_active >= start_date}.
Live trading paths read `segments.py` (unchanged); structural
guards prevent the augmented universe from leaking into live
code paths.

---

## Part 4 — Long/Short Architecture (Phases 1-4 of LONG_SHORT_PLAN.md)

The decision pipeline runs in long-only mode by default. When a
profile sets `enable_short_selling=1` the pipeline switches to
long/short with parity infrastructure:

### Strategy supply
- 16 bullish strategies + 10 dedicated bearish strategies:
  - **Phase 1 technicals (5):** `breakdown_support`, `distribution_at_highs`, `failed_breakout`, `parabolic_exhaustion`, `relative_weakness_in_strong_sector`
  - **Phase 3 alpha sources (4):** `earnings_disaster_short` (PEAD inverse), `catalyst_filing_short` (going-concern / material-weakness / 8-K), `sector_rotation_short` (bottom-3 sector overlay), `iv_regime_short`
  - **Anti-momentum quant (1):** `relative_weakness_universe` — universe-ranked by 20d return vs SPY; emits bottom 5% (cap 5) with 5%+ RS gap and 20d MA confirmation. Always-on regardless of regime; fills short books in bull markets where textbook bearish technical patterns are rare.

### Candidate ranking (`trade_pipeline._rank_candidates`)
- Reserved slots: top 10 longs + top 5 shorts when shorts enabled.
- Filters applied to SHORT candidates only:
  - **Borrow availability** (Alpaca `shortable` flag)
  - **Squeeze risk** (`squeeze_risk` from `alternative_data.get_short_interest`; HIGH skipped, MED/LOW pass through)
  - **Regime gate** (`_classify_market_regime` reads SPY 200d/50d/20d MAs). In `strong_bull`, only catalyst shorts pass through — UNLESS the profile mandates a substantial short book (`target_short_pct ≥ 0.4`), in which case the gate is bypassed entirely. The user has explicitly accepted regime-side risk by configuring the mandate.
- Candidates carry `_borrow_cost` and `_squeeze_risk` annotations into the AI prompt.

### AI prompt
- Two sections: "LONG CANDIDATES" / "SHORT CANDIDATES" with directive: "BOTH sides are real options. A high-conviction short beats a mediocre long."
- Per-candidate annotations on shorts: borrow cost ("low" ~1%/yr; "high" 5-50%+/yr) and squeeze risk.

### Sizing
- `max_position_pct` for longs.
- `short_max_position_pct` (defaults to half) for shorts. Asymmetric-risk convention.
- Borrow penalty: HTB shorts halved again on top of the asymmetric cap.

### Exits
- Standard stop-loss / take-profit per direction (`stop_loss_pct` / `short_stop_loss_pct`, etc).
- Trailing stops via `trailing_atr_multiplier`.
- Time stop on shorts: cover any short older than `short_max_hold_days` (default 10) regardless of P&L.

### Resolution semantics
- `prediction_type` column distinguishes:
  - `directional_long` (BUY/HOLD predictions — predict price up or range-bound)
  - `directional_short` (SHORT, or SELL on a non-held — predict price down)
  - `exit_long` (SELL on a held long — lock in / exit)
  - `exit_short` (cover on a held short)
- Resolver applies per-type win/loss criteria. Exit quality (did the price stay flat or move favorably after the exit?) is judged separately from directional accuracy (did the price drop after the SHORT?).

### Self-tuning per direction
- Long-side optimizers tune `stop_loss_pct`, `take_profit_pct`, `max_position_pct`, etc. — read aggregate or BUY-side data.
- Short-side optimizers (`_optimize_short_stop_loss`, `_optimize_short_max_position_pct`, `_optimize_short_take_profit`, `_optimize_short_max_hold_days`) read ONLY short-side trades. Adjust short-side parameters independently.
- AI prompt context exposes per-direction win rates: `BY DIRECTION: Longs X%W (n=...) | Shorts Y%W (n=...) | Exits Z%W (n=...)`.

### ML layers
- **Specialist calibrators**: each specialist has separate (long, short) Platt-scaling models. Ensemble loads both per run; per verdict picks calibrator by direction. Falls back to legacy unified calibrator until each direction accumulates ≥30 samples.
- **Meta-model**: `prediction_type` is a categorical one-hot feature. Pregate at inference infers direction from candidate's strategy signal. Once shorts accumulate the model learns short-specific feature weights.

### Strategy evolution
- `strategy_proposer.propose_strategies` accepts `direction_mix` for forced long/short balance. Shorts-enabled profiles alternate BUY/SELL proposals on each commission so the Evolving Strategy Library actually grows in both directions, not 90% bullish.

### Portfolio construction context the AI sees (Phases 2-4)

The batch prompt accumulates these blocks in `_build_batch_prompt` after the candidate list. Each is best-effort: if the data isn't available the block is suppressed rather than rendered with a "n/a".

- **EXPOSURE BREAKDOWN (P2.1, P2.5, P3.6).** Sector breakdown with concentration warnings (≥30% gross flagged). Direction balance (long_pct / short_pct of gross). Factor breakdown — size bands, book/value buckets, beta classes, momentum 12-1m winners/losers. Built from `portfolio_exposure.compute_exposure` over current positions.
- **BOOK-BETA TARGET (P4.1).** When `ctx.target_book_beta` is set: shows target, current gross-weighted book beta, and the delta with directive ("BETA TOO HIGH → DEFENSIVE picks long or LEVERED shorts"). ±0.30 tolerance band before the directive switches from "on target" to a corrective bias.
- **LONG/SHORT BALANCE TARGET (P2.2).** When `target_short_pct > 0`: target vs current short share of gross + directive ("UNDERSHORTED by X% — pick a SHORT this cycle"). Threshold ±10pp before the directive engages.
- **KELLY SIZING (P4.2).** Per-direction recommendation: `LONG: Kelly 11.8% (WR 70%, avg win 2.95%, avg loss 2.23%, n=30)`. Reads only entry signals from `ai_predictions` (HOLD predictions tagged `directional_long` are filtered — they reflect existing-position drift, not new bets, so polluting Kelly with them flips edge negative). Quarter Kelly is the default fractional. Empty when neither direction has ≥30 resolved entry trades with positive edge.
- **DRAWDOWN CAPITAL SCALE (P4.3).** Continuous size modifier 1.0× → 0.25× from `drawdown_scaling.compute_capital_scale`. Linear interpolation between breakpoints (0%→1.00, 5%→0.85, 10%→0.65, 15%→0.45, 20%+→0.25). Suppressed when scale rounds to 1.00. The AI is told to multiply suggested sizes by this factor.
- **RISK-BUDGET (P4.4).** Per-name `weight × annualized_vol` contributions; flags names ≥ 2× or ≤ 0.5× the per-position average. Sizing rule: `size_i ∝ target_vol / realized_vol_i` clamped to [0.40×, 1.60×]. Vols cached 7d via `factor_data.get_realized_vol`. Suppressed when nothing actionable.

Layered together the AI is told: `final_size = base × kelly × drawdown_scale × vol_scale` (each clamped, each defaulting to 1.0× when unknown).

### Validation-time gates (in `_validate_ai_trades`)

After the AI returns trades, these hard gates filter them. Each one logs why a trade was dropped (visible to the user via the AI dashboard's vetoed-trades panel):

- **Balance gate (P2.4).** When the book has drifted >25pp off `target_short_pct`, block new entries on the over-weighted side. Lets natural turnover rebalance vs forcing trims (which would burn transaction costs and cut winners).
- **Asymmetric short cap (P1.6).** Longs sized against `max_position_pct`; shorts capped at `short_max_position_pct` (defaults to half of long).
- **HTB borrow penalty (P1.14).** Hard-to-borrow shorts have their cap halved again on top of the asymmetric one (since multi-day holds eat real money in borrow costs).
- **Market-neutrality enforcement (P4.5).** When `ctx.target_book_beta` is set, the gate computes the projected book beta if the trade went through (`portfolio_exposure.simulate_book_beta_with_entry`) and blocks the trade if `|projected - target| - |current - target| > 0.5`. Symmetric: trades that improve neutrality always pass; trades that worsen it by >0.5 in distance are blocked. Skipped for SELL exits.

---

## Part 5 — Safety

### What Stays Manual (and Why)

The `MANUAL_PARAMETERS` allowlist in `tests/test_every_lever_is_tuned.py` enumerates every column that's intentionally not autonomously tuned, with written rationale. Categories:

- **Identity / metadata**: id, user_id, name, market_type, created_at, enabled
- **Secrets**: alpaca_*_enc, ai_api_key_enc, consensus_api_key_enc
- **Strategic AI choice**: ai_provider, ai_model — opt-in via `ai_model_auto_tune` toggle
- **Architectural**: enable_consensus, consensus_model
- **Schedule**: schedule_type + custom_*
- **Meta**: enable_self_tuning (the tuner can't disable itself)
- **Historical baselines**: initial_capital, is_virtual
- **Conviction-TP-override knobs**: explicit risk-preference choice

The guardrail test fails on (a) any new schema column not on this list and not auto-tuned, and (b) stale entries no longer in the schema.

### Structural Tests Across the AI Path

Beyond the manual-allowlist guard, several AST/source-level tests
sit between any future regression and the source — each catches a
specific class of bug we've been bitten by:

- `test_no_snake_case_in_optimizer_strings.py` — every function in
  `self_tuning.py` is walked; raw `max_position_pct` etc. embedded
  inside user-facing strings fails the build. Plus a separate
  decimal-format guard fails on raw `old_v`/`new_v` interpolation
  for percentage-typed parameters (catches the 2026-04-27 ticker
  leak: "max_position_pct 0.08->0.092").
- `test_no_missing_logging_import.py` — every `.py` file using
  `logging.X` must `import logging`. Catches the 2026-04-28
  `NameError` that silently broke Check Exits for ~24 hours.
- `test_track_record_split_by_signal.py` — the per-symbol track
  record returned by `get_symbol_reputation` must include
  `by_signal` breakdown; the prompt builder must emit the split
  string. Catches the 2026-04-28 confabulation incident.
- `test_trade_execution_logging.py` — `run_trade_cycle`'s
  exception handler must call `logging.error(..., exc_info=True)`;
  non-trade SKIP returns must emit a `logging.warning`. Catches
  the 2026-04-28 silent-swallow of order rejections.
- `test_no_per_symbol_bars_in_web_path.py` — dashboard render
  paths must NOT call per-symbol `get_bars()`; use batched
  `get_snapshots()`. Catches the 2026-04-27 rate-limit storm.
- `test_meta_model_time_ordered_split.py` — the meta-model train
  /test split must be time-ordered (slice the most-recent 20%),
  never random. Catches inflated-AUC data leakage.
- `test_walk_forward_and_oos_disjoint.py` — `walk_forward_analysis`
  + `out_of_sample_degradation` must call `backtest_strategy` with
  disjoint date ranges, never `days=N` (today-anchored).
- `test_self_tuning_validation_window.py` — confidence-threshold
  raises must be confirmed against a 14-day held-out window.
- `test_no_per_symbol_bars_in_web_path.py`, `test_shared_ai_cache.py`,
  `test_meta_pregate_lever.py`, `test_specialist_disable_lever.py`,
  `test_alpha_decay_lifetime_disjoint.py`, `test_specialist_calibration.py`,
  `test_historical_universe_augment.py`, `test_resolve_min_hold_horizon.py`
  — each enforces a specific architectural contract; collectively
  ~100+ structural tests across the AI path.

### Six Anti-Regression Guardrails

1. `test_no_recommendation_only` — every "Recommendation:" string in `self_tuning.py` must be on a written-rationale allowlist
2. `test_no_snake_case_in_optimizer_strings` — optimizer return strings can't embed raw column names
3. `test_self_tune_task_no_change_path` — the no-change branch can't NameError
4. `test_signal_weights_lifecycle` — weight ladder + tuner + prompt builder
5. `test_regime_overrides` / `test_tod_overrides` / `test_symbol_overrides` — chain precedence
6. `test_every_lever_is_tuned` — every schema column is autonomous or explicitly manual

### Idempotency

All weekly/daily-once tasks (snapshot bundle, summary emails, weekly digest, capital rebalance, post-mortem) write file-based markers. Markers are excluded from `rsync --delete` so deploys preserve them. This is what stopped the Apr-25 100-email storm caused by ~10 deploys re-firing the snapshot bundle each time.

---

## Part 6 — User Surfaces

### AI Intelligence Page (`/ai`)

- **Brain tab**: prediction accuracy, win rate trend, confidence calibration, meta-model status (AUC, top features per profile)
- **Strategy tab**: alpha-decay monitoring, currently-deprecated strategies with Restore button
- **Awareness tab**: market intelligence, SEC filing alerts, crisis monitor
- **Operations tab**: Self-Tuning history, AI cost tracking, "Active Autonomy State" card showing all per-profile signal weights / regime / TOD / symbol / prompt-layout overrides + capital scale, "What the AI Sees" reference panel

### Settings Page (`/settings`)

- **Autonomy section**: per-user `auto_capital_allocation` toggle (Layer 9 opt-in)
- **Per-profile**: `enable_self_tuning` toggle (default ON), `ai_model_auto_tune` toggle (default OFF, cost-sensitive)

### Weekly Digest (Email, Fridays after market close)

Single email across all profiles. Includes per-profile P&L, trades, win rate, AI cost; self-tuning changes (applied vs recommended counts); strategy deprecations/restorations; auto-strategy lifecycle transitions; crisis state transitions; trading narrative on top/bottom trades.

### Daily Summary (Email, end-of-day per profile)

File-based idempotency marker prevents re-fire on scheduler restart. Per-profile state snapshot.

---

## Part 7 — Where Each File Fits

| File | Purpose |
|---|---|
| `ai_analyst.py` | Specialists + Batch Trade Selector + prompt builder (reads Layer 2 weights, Layer 6 verbosity) |
| `ai_providers.py` | Provider abstraction (Anthropic / OpenAI / Google) + cost ledger logging |
| `ai_tracker.py` | Records predictions, resolves them (BUY/SELL on price move, HOLD on time + stable price) |
| `meta_model.py` | Gradient-boosted classifier — Phase 1 (re-weights AI confidence at decision time) |
| `alpha_decay.py` | Strategy deprecation / restoration based on rolling Sharpe |
| `self_tuning.py` | All 12 layers' tuner rules + safety scaffolding (cooldown, reverse, clamp) |
| `param_bounds.py` | Declarative PARAM_BOUNDS + clamp() helper |
| `signal_weights.py` | Layer 2 — per-signal intensity ladder + WEIGHTABLE_SIGNALS registry |
| `regime_overrides.py` | Layer 3 — per-regime values + the `resolve_for_current_regime` chain entry |
| `tod_overrides.py` | Layer 4 — per-time-of-day values |
| `symbol_overrides.py` | Layer 7 — per-symbol values |
| `prompt_layout.py` | Layer 6 — per-section verbosity |
| `capital_allocator.py` | Layer 9 — per-Alpaca-account-conserving capital rebalance |
| `cost_guard.py` | Cross-cutting daily-spend ceiling enforcement |
| `insight_propagation.py` | Layer 5 — cross-profile insight fan-out |
| `post_mortem.py` | Losing-week pattern extraction |
| `multi_scheduler.py` | Per-profile cycle orchestration; weekly tasks (digest, capital rebalance, post-mortem); idempotency markers |
| `trade_pipeline.py` | Decision-time parameter resolution; the override chain at every read |
| `strategy_proposer.py` / `strategy_generator.py` / `strategies/` | Phase 7 strategy creation pipeline |
| `post_mortem.py` | Losing-week pattern extraction (Sundays); writes to `learned_patterns` table |
| `/opt/quantopsai-altdata/{congresstrades,edgar13f,biotechevents,stocktwits}/` | Standalone data-collection projects (deployed 2026-04-26). Daily cron at 06:00 UTC. Each project owns its own SQLite at `data/*.db`. QuantOpsAI's `alternative_data.py` helpers query these read-only. |

### Extensive Docs

- `SELF_TUNING.md` — every tuning rule, every signal, every safety guard
- `AUTONOMOUS_TUNING_PLAN.md` — the 13-wave plan with acceptance criteria
- `CHANGELOG.md` — every fix, every feature, every regression and how it was caught
- `ROADMAP.md` — what's next
