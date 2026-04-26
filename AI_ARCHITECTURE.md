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
16-strategy voting           Each strategy independently votes
(no AI)                      BUY/SELL/HOLD per candidate.
                              Votes aggregated into a score.
        │
        ▼
4 specialists vote in        Batched in groups of 5 candidates so
parallel (4 AI calls)         no candidate is dropped. Each returns
                              verdict + confidence + reasoning.
        │
        ▼
Ensemble synthesizer         Confidence-weighted vote across the 4.
(no AI)                      Risk Assessor's VETO overrides everything.
        │
        ▼
Batch Trade Selector         Sees: candidates, ensemble verdicts,
(1 AI call)                   portfolio state, market regime, political
                              context, learned patterns, post-mortems,
                              per-stock track records.
                              Picks 0-3 trades, sizes them.
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
                              and meta-model training.
```

### 1c. What the AI Sees per Candidate

19 alternative-data signals + 13 technical indicators + market context
+ per-stock memory. Detailed list lives in the AI page's "What the
AI Sees" reference panel; the canonical signal registry is in
`signal_weights.WEIGHTABLE_SIGNALS`. Each signal can be omitted or
discounted per profile via Layer 2 weights (see §Part 2).

### 1d. Cost per Cycle

| Agent | Calls | Est. cost |
|---|---|---|
| Earnings Analyst | 3 (5-candidate batches) | ~$0.003 |
| Pattern Recognizer | 3 | ~$0.003 |
| Sentiment & Narrative | 3 | ~$0.003 |
| Risk Assessor | 3 | ~$0.003 |
| Batch Trade Selector | 1 | ~$0.001 |
| Political Context | 1 (if MAGA on) | ~$0.001 |
| **Total** | **~13–14** | **~$0.014** |

At 4 cycles/hour × 6.5 market hours × 10 profiles → **~$3.64/day** at full load.
Cost guard ceiling defaults to trailing-7-day-avg × 1.5 (floor $5/day).

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
- **Strategy toggles**: 4 legacy + auto-deprecation of all 16+ modular strategies via alpha_decay (auto-restore on Sharpe recovery)

Each parameter has explicit bounds in `param_bounds.PARAM_BOUNDS`. Tuner clamps every change to these bounds. Per-rule cooldown (3-day default, 7 for per-symbol overrides, 14 for prompt rotation). Reverse-if-worsened automatic.

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

---

## Part 4 — Safety

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

## Part 5 — User Surfaces

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

## Part 6 — Where Each File Fits

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

### Extensive Docs

- `SELF_TUNING.md` — every tuning rule, every signal, every safety guard
- `AUTONOMOUS_TUNING_PLAN.md` — the 13-wave plan with acceptance criteria
- `CHANGELOG.md` — every fix, every feature, every regression and how it was caught
- `ROADMAP.md` — what's next
