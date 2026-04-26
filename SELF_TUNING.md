# Self-Tuning System

QuantOpsAI's self-tuning system is an autonomous feedback loop that adjusts trading parameters, signal weights, and (in upcoming waves) per-regime / per-time-of-day / per-symbol overrides based on the AI's own prediction accuracy and trade outcomes. It runs daily as part of the snapshot bundle (~3:55 PM ET) per profile and logs every decision it makes — even when it makes none.

**The end goal:** every tactical decision is autonomous. Humans set strategic direction (cost ceilings, AI provider/model choice, secrets, schedule) and the system handles every dial that responds to data.

See `AUTONOMOUS_TUNING_PLAN.md` for the complete 9-layer roadmap.

---

## How It Works

### What Is a "Resolved Prediction"?

Every time the AI evaluates a stock candidate (whether it trades it or not), a **prediction** is recorded with the AI's signal (BUY/SELL/HOLD), confidence score, price, and the full feature context. This is NOT the same as a closed trade — the AI makes predictions on every candidate in the shortlist, not just the ones it buys.

A prediction **resolves** when one of these conditions is met:

| Signal | Win Condition | Loss Condition | Timeout |
|--------|--------------|----------------|---------|
| BUY | Price rises ≥ 2% | Price drops ≥ 2% | 10 trading days |
| SELL | Price drops ≥ 2% | Price rises ≥ 2% | 10 trading days |
| HOLD | Price stays within ±2% for 3 trading days | Price moves > 2% within 3 trading days | 10 trading days |

After 10 trading days with no threshold hit, the prediction resolves as "neutral."

**Why this matters:** A closed trade and a resolved prediction are different things. The AI might predict HOLD on 20 stocks and BUY on 2. All 22 are tracked as predictions, but only the 2 BUYs become trades. The self-tuner learns from ALL predictions — not just the ones that led to trades — because the AI's accuracy on HOLD calls matters just as much (a false HOLD on a stock that jumped 10% is a missed opportunity worth learning from).

### The 20-Prediction Threshold

The self-tuner requires **at least 20 resolved predictions** before it will adjust anything. This prevents premature optimization on tiny sample sizes that could be pure noise.

---

## What Gets Auto-Tuned (Today)

The tuner currently autonomously adjusts these levers. Each rule fires at most once per cycle (one change per run, for clean reversal attribution).

### Disaster prevention (always-on, runs below 35% WR)

| Lever | Trigger | Action |
|-------|---------|--------|
| `ai_confidence_threshold` | Win rate at <60% conf < 35% | Raise to 60 (escalate to 70 if same problem) |
| `max_position_pct` | Overall WR < 30% | Reduce 20% (floor 3%) |
| `short_stop_loss_pct` | 5+ shorts, 0% WR | Widen 50% (cap 20%) |
| `enable_short_selling` | 10+ shorts, <20% WR, negative P&L | **Auto-disable** |

### Upward optimization (runs at WR ≥ 35%)

| Lever | Trigger | Action |
|-------|---------|--------|
| `ai_confidence_threshold` (band-search) | A higher confidence band has 10+ pt better WR than overall | Raise threshold to that band |
| `max_position_pct` (regime-aware) | Current regime WR ±15 pt vs overall | Reduce 25% / raise 15% |
| `max_position_pct` (proven-edge) | 55%+ WR, 30+ predictions, positive avg return | Raise 15% (cap 15%) |
| 4 legacy strategy toggles | Strategy WR < 30% AND 15+ pt below overall | Disable (never the last enabled) |
| Modular strategies | Same trigger; no profile toggle | **Auto-deprecate via `alpha_decay`** (auto-restores when rolling Sharpe recovers) |
| `stop_loss_pct` | 40%+ of losses cluster within 1% of stop | Widen 20% |
| `take_profit_pct` | Avg winner < 50% of TP target | Tighten 20% |

### Wave 1 — Concentration / risk + Timing (newly active)

| Lever | Trigger | Action |
|-------|---------|--------|
| `max_total_positions` | Avg loss < -$200 AND WR < 40% | -1 (concentration risk) |
|                       | WR ≥ 60% AND avg winner > $100 | +1 (capacity) |
| `max_correlation` | ≥40% of weeks have 3+ losing trades (loss clusters) | Tighten 0.05 |
|                   | < 10% loss-cluster weeks AND WR ≥ 55% | Loosen 0.05 |
| `max_sector_positions` | Overall WR < 35% | -1 (avoid sector drawdowns) |
| `drawdown_pause_pct` | WR drift zone (35–45%) | Tighten 0.02 (catch deterioration earlier) |
| `drawdown_reduce_pct` | WR drift zone (35–45%) | Tighten 0.01 |
| `min_price` | Bottom-of-band entries (≤ 1.5× min) WR < 30% | Raise 25% (capped 0.5×–2.0× current) |
| `max_price` | Top-of-band entries (≥ 0.85× max) WR < 30% | Lower to 0.85× current |
| `avoid_earnings_days` | *(active once `days_to_earnings` is logged on each prediction)* | ±1 day |
| `skip_first_minutes` | *(active once intraday entry-time is structured)* | ±5 min |
| `maga_mode` | Predictions with political_context active WR ≥ 10pt below overall (≥20 samples) | **Auto-disable** |

**Total levers auto-tuned today: 35** (8 pre-existing + 15 from Wave 1 + 8 from Wave 2 + 4 from Wave 3 + per-signal weight system from Wave 4 covering 21 signals).

### Wave 5 — Per-Regime Parameter Overrides (Layer 3, newly active)

Each parameter can have **regime-specific values** that override the
profile's global value when the market is in that regime. Recognised
regimes: `bull`, `bear`, `sideways`, `volatile`, `crisis`.

Storage: `regime_overrides` JSON column on `trading_profiles`. Empty by
default — every parameter resolves to its global value until the tuner
detects a per-regime divergence worth correcting for.

**Pipeline integration:** `regime_overrides.resolve_for_current_regime(profile, name, default=...)` is called instead of `getattr(profile, name)` at every decision point that reads a tunable parameter. The helper auto-detects the current regime (cached 5 min) and returns the per-regime override if one exists, else falls back to the profile's global. Today wired into:

- Trade-pipeline confidence threshold (`ai_confidence_threshold`)
- Position sizing (`max_position_pct`)
- Stop-loss / take-profit (`stop_loss_pct`, `take_profit_pct`)
- Concurrent-position cap (`max_total_positions`)

**Tuner detection:** `_optimize_regime_overrides` walks each regime that has ≥10 resolved predictions. If a regime's WR diverges from baseline by ≥12pt, an override is created:
- Underperforming regime → reduce `max_position_pct` 25% for that regime
- Outperforming regime → raise `ai_confidence_threshold` +5 to focus on strongest setups

Same safety scaffolding: cooldown keyed on `regime:<regime>:<param>`, reverse-if-worsened, snap to PARAM_BOUNDS.

### Wave 9 — Auto Capital Allocation (Layer 9, opt-in)

**Per-user opt-in.** Default OFF. When the user enables
`auto_capital_allocation` (Settings → Autonomy), a weekly task
(`_task_capital_rebalance`, runs Sundays) shifts per-profile
`capital_scale` multipliers based on each profile's risk-adjusted
recent returns.

**Critical: respects the per-Alpaca-account constraint.** Profiles are
virtual on top of shared Alpaca paper accounts. Multiple profiles can
share one $1M account. The allocator works **per-Alpaca-account-group**:

- Profiles are grouped by `alpaca_account_id`.
- Within each group, scales are normalized so they sum to N (group
  size). Average stays at 1.0; relative shifts move toward
  higher-scoring profiles.
- Solo profiles (1 per account) always get scale=1.0.
- The underlying real account is never over-committed — if
  scale[A]=1.5, then scale[B]+scale[C]=1.5 in the same group.

**Bounds:** per-rebalance ±50% max move; absolute scale ∈ [0.25, 2.0].
Score formula: `recent_sharpe × (1 + win_rate)` over trailing 30 days.

**Pipeline integration:** `trade_pipeline.execute_trade` multiplies
`max_position_pct` by `capital_scale` after the override-chain
resolution, so the allocator's decisions stack on every other tuning
layer.

### Wave 8 — Self-Commissioned New Strategies (Layer 8, cost-gated)

When the tuner detects a coverage gap — winning AI predictions over
the last 30 days where no strategy fired — it triggers Phase 7's
strategy generator with a focused brief. Heavily cost-gated and
rate-limited.

- **Detection:** ≥5 no-strategy winners in last 30 days.
- **Cost gate:** every commission call wrapped in
  `cost_guard.can_afford_action`. If it would push spend over the
  daily ceiling, surfaces as `Recommendation: cost-gated` instead.
- **Rate limit:** 7-day cooldown per profile.
- The proposed spec flows through the existing Phase 7 lifecycle:
  proposed → validated → shadow → active.

### Wave 7 — Per-Symbol Parameter Overrides (Layer 7, most-specific tier)

Some symbols behave fundamentally differently. The tuner creates
per-symbol overrides for symbols with materially different track
records than the profile baseline.

- **Detection:** ≥20 individual resolved predictions per symbol;
  ≥15pt WR divergence from overall.
- **Cooldown:** 7 days per-symbol per-parameter (vs 3 for other tiers)
  to prevent over-fitting on small samples.
- **Pipeline chain order at decision time:**
  per-symbol → per-regime → per-time-of-day → global.

Underperforming symbols get `max_position_pct` reduced for that
symbol; outperforming symbols get `ai_confidence_threshold` raised.

### Wave 6 — Adaptive AI Prompt Structure (Layer 6, cost-gated)

The structure of the AI's prompt — section verbosity per profile —
becomes a tunable surface. Rotates one section's verbosity every 14
days to test whether different framing improves WR.

- **Sections:** alt_data, political_context, learned_patterns,
  portfolio_state.
- **Verbosity ladder:** brief / normal / detailed.
- **Cost gate:** moves toward `detailed` (longer prompts) checked
  against the daily ceiling. Cost-saving moves (toward `brief`)
  always auto-applied.
- **Cooldown:** 14 days per rotation (vs 3 for parameters) so each
  variant has enough cycles to attribute outcomes.

### Wave 5 — Cross-Profile Insight Propagation (Layer 5)

When the tuner makes a change that turns out to improve a profile's
win rate (review marks `outcome_after = 'improved'`), the same
detection rule runs against every OTHER enabled profile belonging to
the same user. Each peer's own data has to independently support the
change — **no value-copying**.

This means the fleet learns ~10× faster than profiles in isolation,
with zero new API spend. Insights propagate via
`insight_propagation.propagate_insight(source_id, change_type, name)`
which iterates peers, builds a duck-typed context, and re-runs the
appropriate `_optimize_*` function on the peer's own prediction DB.

### Wave 4 — Weighted Signal Intensity (Layer 2, newly active)

Every signal the AI sees has a per-profile weight on a 4-step ladder:
**1.0 → 0.7 → 0.4 → 0.0**. Stored as JSON on `trading_profiles.signal_weights`. Missing keys = default 1.0.

The tuner walks every weightable signal each cycle (alt-data signals like insider clusters, options flow, dark pool, congressional trades, political context, plus the modular strategy votes). For each: bucket recent resolved predictions by whether the signal was materially present, compute differential WR vs absence baseline.

| Signal-present WR vs absent baseline | Action |
|--------------------------------------|--------|
| ≤ -10pt below | Nudge weight DOWN one step |
| ≥ +5pt above (recovery) | Nudge weight UP one step |
| Within band | No change |

The prompt builder reads weights at build time:
- Weight `1.0`: signal presented as today.
- Weight `0.7` / `0.4`: signal still presented, with appended `[intensity 0.4]` hint so the AI knows to discount it.
- Weight `0.0`: signal **omitted entirely** from the prompt.

Same safety scaffolding as Layer 1 — 3-day cooldown per signal (`weight:<sig>`), reverse-if-worsened, snapped to ladder values to prevent absurd drift.

---

## What Stays Manual (and Why)

The tuner cannot change these. Each is on the `MANUAL_PARAMETERS` allowlist enforced by an anti-regression test.

| Parameter | Why |
|-----------|-----|
| `ai_provider`, `ai_model` | Strategic AI choice. Cost vs capability tradeoff. Opt-in toggle `ai_model_auto_tune` (planned) will let the user enable A/B testing within a cost budget. |
| `enable_consensus`, `consensus_model` | Architectural — multi-model setup is intentional. |
| `schedule_type`, `custom_*` | When the user wants trading active. (Future: per-strategy schedules.) |
| `enable_self_tuning` | Meta — the tuner can't disable itself. |
| `*_api_key_enc` | Secrets. |
| `initial_capital` | Historical baseline. |
| `name`, `created_at`, `user_id`, `id` | Identity / metadata. |

---

## Safety Mechanisms

The tuner has several guardrails to prevent runaway adjustments:

### Bounds Clamping

Every parameter has hard min/max bounds in `param_bounds.PARAM_BOUNDS`. The tuner clamps every change to these bounds. Even if a detection rule produces an extreme value, the bounds catch it. Bounds are **absolute** safety floors and ceilings; the tuner's per-rule logic restricts day-to-day movement to small steps.

### 3-Day Cooldown

After any adjustment, that same parameter cannot be changed again for 3 days. This prevents rapid oscillation (e.g., raising and lowering the confidence threshold every day).

### Automatic Reversal

Every adjustment is reviewed after 3 days (with at least 10 new predictions since the change):

- **Improved:** WR went up. Mark as "improved" and keep.
- **Worsened:** WR went down. **Automatically reverse** to the previous value.
- **Neutral:** No meaningful change. Keep the adjustment but don't count it as a success.

The system can undo its own mistakes. If raising the confidence threshold to 70 caused the AI to miss good trades, the tuner sets it back.

### History Check

Before any adjustment, the tuner checks if the same change was tried before and worsened. If yes, it won't try again.

### One Change Per Run

The orchestrator runs optimizers in priority order and stops after the first change. This lets the auto-reversal system attribute any subsequent WR shift to that specific adjustment.

### Anti-Regression Tests

- `tests/test_no_recommendation_only.py` — every "Recommendation:" string in `self_tuning.py` must be on an explicit allowlist with rationale. New "recommendation only" code paths fail this test until the author wires a real action or adds an allowlist entry.
- `tests/test_self_tuning_wave1.py` — covers every Wave 1 rule: triggers correctly, respects bounds, respects cooldown, registered in orchestrator.
- `tests/test_every_lever_is_tuned.py` *(coming in W13)* — walks the `trading_profiles` schema and asserts every column is either tuned or on the manual allowlist.

---

## What Gets Logged

Every self-tuning run produces a record, whether or not it makes changes.

### When Changes Are Made

Stored in the `tuning_history` table (in `quantopsai.db`):

| Field | Description |
|-------|-------------|
| `profile_id` | Which profile was tuned |
| `change_type` | Category (e.g., `confidence_threshold`, `concentration_reduce`, `price_band_min_raise`) |
| `parameter_name` | Exact DB column changed |
| `old_value` | Value before the change |
| `new_value` | Value after the change |
| `reason` | Human-readable explanation |
| `win_rate_at_change` | Overall win rate when the decision was made |
| `predictions_resolved` | How many resolved predictions informed the decision |
| `timestamp` | When it happened |
| `outcome_after` | Filled in 3 days later: `improved`, `worsened`, `unchanged`, or `n/a` |

### When No Changes Are Made

A `tuning_history` row with `change_type = 'evaluation'` is logged with the current WR and prediction count, plus an activity-feed entry visible in the dashboard. This shows the tuner is running even on quiet days.

---

## Viewing Self-Tuning Activity

### AI Intelligence Page → Operations Tab

- **Self-Tuning** card: per-profile readiness pills (Active / Collecting) + the full Self-Tuning History table with parameter, old → new (formatted), reason, and outcome.
- **Alpha Decay Monitoring** card (Strategy tab): currently-deprecated strategies with a manual **Restore** button.

### Weekly Digest Email (Fridays after market close)

- Self-Tuning Changes section with applied vs recommended counts and full per-profile breakdown.

---

## Difference vs. the Meta-Model (Phase 1)

The **self-tuner** adjusts trading parameters based on aggregate win/loss statistics — macro level.

The **meta-model** is a gradient-boosted classifier that predicts "will this specific prediction be correct?" based on the full feature vector — per-prediction level.

They complement:
- Tuner: "stop taking trades below 60% confidence."
- Meta-model: "this specific 75% confidence trade on AAPL in a bear market with RSI 80 is likely wrong."

Meta-model retrains daily from `_task_retrain_meta_model` and outputs an `auc` / `accuracy` / feature-importance ranking that's surfaced in the activity feed.

---

## Coming Next (per `AUTONOMOUS_TUNING_PLAN.md`)

| Wave | Layer | Scope |
|------|-------|-------|
| W2 | Layer 1 Group C | 8 entry-filter parameter rules |
| W3 | Layer 1 Group B | 6 exit parameter rules |
| W4 | Layer 2 — Weighted signal intensity | Per-profile weights for ~16 signals (alt-data + strategies + booleans). Replaces binary toggles where appropriate. |
| W5 | Layer 3 — Per-regime overrides | Bull / bear / sideways / volatile / crisis specific values per parameter |
| W6 | Layer 4 — Per-time-of-day overrides | Open / midday / close specific values |
| W7 | Cost guard | Cross-cutting daily-spend ceiling enforcement |
| W8 | Layer 7 — Per-symbol overrides | Some tickers behave differently |
| W9 | Layer 5 — Cross-profile insight sharing | Improvement on profile A triggers detection on B's own data |
| W10 | Layer 6 — Adaptive AI prompt structure | Tuner reinforces high-WR prompt variants |
| W11 | Layer 8 — Self-commissioned strategies | Tuner identifies coverage gaps → triggers Phase 7 generator |
| W12 | Layer 9 — Auto capital allocation (opt-in) | Weight capital toward proven-edge profiles |
| W13 | Final | Settings UI Autonomy section + `test_every_lever_is_tuned.py` + doc final pass |

End state: ~50 autonomous decision surfaces with cost discipline. The system genuinely earns "makes better, faster, smarter decisions than a person can."

---

## Technical Reference

| File | Purpose |
|------|---------|
| `self_tuning.py` | Core logic: `apply_auto_adjustments()`, `describe_tuning_state()`, `_apply_upward_optimizations()` and all `_optimize_*` rules |
| `param_bounds.py` | `PARAM_BOUNDS` declarative bounds + `clamp(name, value)` |
| `alpha_decay.py` | Strategy deprecation/restoration pipeline; called by tuner for non-toggleable strategies |
| `multi_scheduler.py` | Scheduler integration: `_task_self_tune(ctx)` fires daily at snapshot time |
| `models.py` | `tuning_history` CRUD: `log_tuning_change()`, `review_past_adjustments()`, `get_tuning_history()` |
| `views.py` | Dashboard display + `/ai/profile/<id>/restore-strategy/<name>` endpoint |
| `templates/ai.html` | Self-Tuning + Alpha Decay UI |
| `display_names.py` | Snake_case → human label for every parameter and adjustment_type |
| `tests/test_no_recommendation_only.py` | Guardrail: no new recommendation-only code paths |
| `tests/test_self_tuning_wave1.py` | Wave 1 rule coverage |
| `tests/test_self_tuning_deprecation.py` | Tuner → alpha_decay integration |
