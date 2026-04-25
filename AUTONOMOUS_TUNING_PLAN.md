# Autonomous Tuning — Comprehensive Plan

**Goal:** QuantOpsAI makes better, faster, smarter decisions than a
person can — autonomously. Every parameter, signal, regime context,
and prompt structure is on a feedback loop. Humans set strategic
direction (cost ceilings, AI model choice, secrets, identity); the
system handles every tactical decision.

**Cost discipline is a first-class concern.** Every autonomous action
is cost-aware. The tuner respects a daily API spend ceiling and
surfaces only the rare cost-exceeding changes for human approval.

---

## Architecture Overview — 9 Layers + Cost Guard

| Layer | Surface | Status |
|------|---------|--------|
| 1 | Parameter coverage (every column) | Designed below |
| 2 | Weighted signal intensity (per-signal weights) | Designed below |
| 3 | Per-regime overrides | Designed below |
| 4 | Per-time-of-day overrides | Designed below |
| 5 | Cross-profile insight sharing | Designed below |
| 6 | Adaptive AI prompt structure | Designed below |
| 7 | Per-symbol overrides | Designed below |
| 8 | Self-commissioned new strategies | Designed below |
| 9 | Automatic capital allocation | Designed below |
| ✱ | Cost guard (cross-cutting) | Designed below |

---

## Manual Allowlist (the only things humans still control)

| Parameter | Why Manual |
|-----------|-----------|
| `ai_provider`, `ai_model` | Strategic AI choice. **Opt-in toggle**: `ai_model_auto_tune` (default OFF, per-profile). When user flips it on, tuner can A/B test models within the cost guard. |
| `enable_consensus`, `consensus_model` | Architectural choice — multi-model setup is intentional. |
| `schedule_type`, `custom_start`, `custom_end`, `custom_days` | When the user wants trading active. (Future: per-strategy schedules.) |
| `enable_self_tuning` | Meta — tuner can't disable itself. |
| `alpaca_api_key_enc`, `alpaca_secret_key_enc`, `ai_api_key_enc`, `consensus_api_key_enc` | Secrets. |
| `initial_capital` | Historical baseline, not tunable. |
| `name`, `created_at`, `user_id`, `id` | Identity / metadata. |

**Everything else is autonomous.** A guardrail test enforces this — see "Anti-Regression Tests" section.

---

## Layer 1 — Parameter Coverage

Every parameter in `trading_profiles` not on the manual allowlist gets
a tuning rule. All rules share the same scaffolding: minimum data
threshold (≥20 resolved predictions or rule-specific minimum), 3-day
per-parameter cooldown, reverse-if-worsened guard, bound clamping,
logged to `tuning_history`, surfaced via `display_name`.

### Already tuned (8)

`ai_confidence_threshold`, `max_position_pct`, `stop_loss_pct`,
`take_profit_pct`, `short_stop_loss_pct`, `enable_short_selling`
(auto-disable), 4 legacy strategy toggles (auto-disable), modular
strategies via alpha_decay (auto-deprecate + auto-restore).

### Group A — Concentration & Risk (7 parameters)

| Parameter | Detection | Direction & Magnitude | Bounds |
|-----------|-----------|------------------------|--------|
| `max_total_positions` | Avg loss large + cap hit often | -1 / +1 | 3–25 |
| `max_correlation` | >50% of losing trades within 7d are correlated >0.6 | -0.05 / +0.05 | 0.30–0.95 |
| `max_sector_positions` | Sector-concentrated losing days | -1 / +1 | 1–10 |
| `drawdown_pause_pct` | Pause threshold hit + got worse | -0.02 / +0.02 | 0.10–0.30 |
| `drawdown_reduce_pct` | Reduce-trigger fired + further drawdown | -0.01 / +0.01 | 0.05–0.15 |
| `min_price` | Bottom-of-band entries (within 1.5×) <30% WR | +25% / -10% (clamped) | 0.5× to 2× of current, abs floor $0.50 |
| `max_price` | Top-of-band entries (within 0.85×) <30% WR | -10% / +25% | 0.5× to 2× of current, abs ceiling $1000 |

### Group B — Exits & Order Behavior (6 parameters; the boolean execution toggles roll into Layer 2)

| Parameter | Detection | Direction & Magnitude | Bounds |
|-----------|-----------|------------------------|--------|
| `short_take_profit_pct` | Shorts hit TP then reversed / shorts ran past TP | ±20% | 0.03–0.20 |
| `atr_multiplier_sl` | ATR stops hit on >40% of losers within 0.2× ATR | ±0.25 | 1.0–4.0 |
| `atr_multiplier_tp` | Avg winner < 50% of ATR target distance | ±0.25 | 1.0–5.0 |
| `trailing_atr_multiplier` | Avg give-back from peak >40% / stopped before continuation | ±0.25 | 0.5–3.0 |
| `use_atr_stops` (becomes weight 0.0/0.5/1.0) | Compare ATR-on vs ATR-off period Sharpe | weight nudge | 0.0–1.0 |
| `use_trailing_stops` (becomes weight) | Same shape | weight nudge | 0.0–1.0 |
| `use_limit_orders` (becomes weight) | Slippage as market vs limit fill rate | weight nudge | 0.0–1.0 |

### Group C — Entry Filters (8 parameters)

Same shape: bucket recent resolved predictions by which side of the
threshold they fell on; tighten if marginal bucket underperforms;
loosen if too many would-have-winners are filtered out.

| Parameter | Direction & Magnitude | Bounds |
|-----------|------------------------|--------|
| `min_volume` | +50% / -25% | 100K–5M |
| `volume_surge_multiplier` | ±0.25 | 1.0–5.0 |
| `breakout_volume_threshold` | ±0.25 | 0.5–3.0 |
| `gap_pct_threshold` | ±0.5 | 1.0–10.0 |
| `momentum_5d_gain` | ±0.5 | 1.0–15.0 |
| `momentum_20d_gain` | ±0.5 | 1.0–15.0 |
| `rsi_overbought` | ±2 | 70–95 |
| `rsi_oversold` | ±2 | 5–30 |

### Group D — Timing (3 parameters)

| Parameter | Direction & Magnitude | Bounds |
|-----------|------------------------|--------|
| `avoid_earnings_days` | ±1 day | 0–7 |
| `skip_first_minutes` | ±5 min | 0–30 |
| `maga_mode` (becomes weight) | weight nudge based on with-vs-without WR | 0.0–1.0 |

**Layer 1 net new tunings: 24 parameters.**

---

## Layer 2 — Weighted Signal Intensity

Every signal the AI sees gets a per-profile weight on a 4-step ladder:
**`1.0 → 0.7 → 0.4 → 0.0`**. Weight `1.0` is full strength (default,
omitted from storage); `0.0` means omit the signal entirely from the
prompt.

Storage: new column `signal_weights TEXT NOT NULL DEFAULT '{}'` on
`trading_profiles`. JSON dict `{signal_name: weight}`. Helper module
`signal_weights.py` provides get/set/list with the 1.0 default
fallback.

**Weightable signals (initial set):**
- Strategy weights: all 16+ strategies including the 4 legacy ones (consistency — replace binary toggles)
- Alt-data: insider_cluster, insider_selling_cluster, options_flow, options_chain_oracle, dark_pool, sec_filing_alerts, earnings_call_sentiment, analyst_estimate_revisions, short_interest, finra_short_volume, intraday_patterns, fundamentals
- AI behavior: maga_mode (political_context), enable_short_selling, use_atr_stops, use_trailing_stops, use_limit_orders

**Tuner detection:** for each weightable signal, bucket recent
resolved predictions by "signal materially present vs absent" (per
signal-specific `is_active` predicate) and compute differential WR. If
signal-present consistently underperforms, nudge weight down. If
recovers (signal-present WR ≥ overall + 5pts for 14+ days), nudge up.

**Prompt builder integration:**
- Weight 1.0: present signal as today
- Weight 0.7 / 0.4: include signal + inject hint *"Note: {signal} has been historically weak for this profile (intensity: 0.4) — weight its contribution accordingly"*
- Weight 0.0: omit signal entirely from prompt
- For execution toggles (use_atr_stops etc.), weight 0.5 means use 50% of the time at random (rotational A/B), enabling continued data collection

**Cooldown / reversal**: same as Layer 1, keyed on `f"weight:{signal_name}"`.

---

## Layer 3 — Per-Regime Parameter Overrides

Each parameter can have regime-specific values. Regimes:
`bull`, `bear`, `sideways`, `volatile`, `crisis`.

Storage: new column `regime_overrides TEXT NOT NULL DEFAULT '{}'` on
`trading_profiles`. JSON dict
`{parameter_name: {regime: value}}`. Empty / missing = use global value.

**Pipeline integration:** at decision time, before reading any
parameter, the pipeline calls
`resolve_param(profile, param_name, regime)` which returns the
regime-specific value if it exists AND has sufficient sample size
(≥10 resolved predictions in that regime), else falls back to global.

**Tuner detection:** for each Layer 1 parameter the tuner adjusts, it
also bucket-analyzes per regime. If a parameter's optimal value
differs materially by regime (e.g., `stop_loss_pct = 0.04` works best
in `volatile`, `0.03` in `sideways`), the tuner creates per-regime
overrides instead of (or in addition to) a global change.

Same cooldown / reversal infrastructure, keyed on
`f"regime:{regime}:{param_name}"`.

---

## Layer 4 — Per-Time-of-Day Overrides

Three intraday buckets (US ET):
- **Open:** 09:30–10:30 (high vol, gap-dependent)
- **Midday:** 10:30–14:30 (lower vol, mean-reverting)
- **Close:** 14:30–16:00 (vol picks up, MOC/LOC orders)

Storage: new column `tod_overrides TEXT NOT NULL DEFAULT '{}'` on
`trading_profiles`. Same JSON shape as `regime_overrides`.

`resolve_param` consults regime overrides first (more specific), then
TOD overrides, then global value.

Tuner detects time-of-day signal the same way as regime.

---

## Layer 5 — Cross-Profile Insight Sharing

When one profile makes a successful adjustment (`outcome_after =
"improved"` after the 3-day review window), the tuner runs the same
detection rule against every other enabled profile's own data.
Profiles with the same pattern get the same change considered (their
own data triggers it; no value-copying).

Mechanism: new function `propagate_insight(source_profile_id,
adjustment_type, parameter_name)` — called when the reversal-review
finds an improvement. It iterates other profiles, runs the
parameter's detection function with their data, applies if the
detection triggers.

This means the fleet learns ~10× faster than profiles in isolation.
**No additional API cost** — all analysis is from existing resolved
predictions.

---

## Layer 6 — Adaptive AI Prompt Structure

The prompt has structure: section order, section headers, section
verbosity, signal grouping. Each of these is a tunable surface.

Storage: new column `prompt_layout TEXT NOT NULL DEFAULT '{}'` on
`trading_profiles`. Stores `{section_name: {order: int, verbosity:
"brief"|"normal"|"detailed", included: bool}}`.

Default layout matches today's prompt (no behavior change at start).

**Tuner runs implicit A/B tests:** rotates layout variants across
cycles (different ordering, different verbosity per section) and
correlates with subsequent prediction outcomes. Layouts producing
materially better WR get reinforced; underperforming ones get pruned.

**Cost guard:** verbosity changes that would push prompts past the
cost ceiling are blocked. Tuner prefers shorter layouts when WR is
equivalent.

---

## Layer 7 — Per-Symbol Parameter Overrides

Some symbols behave differently (e.g., NVDA has different optimal
stop-loss than KO). Storage: new column `symbol_overrides TEXT NOT
NULL DEFAULT '{}'` on `trading_profiles`. JSON dict
`{symbol: {parameter_name: value}}`.

`resolve_param` consults per-symbol first (most specific), then
regime, then TOD, then global.

Tuner detection: for symbols with ≥20 resolved predictions
individually, bucket-analyze and create per-symbol overrides for
parameters that differ materially from the profile global.

This is the most fine-grained layer. Cooldown 7 days
(per-symbol, per-parameter) to prevent over-fitting on small symbol
samples.

---

## Layer 8 — Self-Commissioned New Strategies

Phase 7 (auto-strategy generation via LLM) already exists. This layer
adds a feedback loop on top: the tuner identifies *gaps* in current
strategy coverage (e.g., "we have no strategy for high-IV-rank
breakouts" or "no strategy for post-earnings drift") and triggers
the strategy generator with a focused brief.

Detection: analyze the universe of resolved predictions to find
patterns where the existing strategies didn't fire but the AI made
correct calls anyway. These represent untapped edges.

**Cost-gated:** strategy generation costs LLM tokens. The tuner
checks the cost guard before triggering and limits to ≤1 generation
per profile per week.

---

## Layer 9 — Automatic Capital Allocation

Across profiles, capital should flow toward the ones with proven
edge. Storage: new table `capital_allocation_history` tracking weekly
rebalances.

Mechanism: weekly task computes per-profile risk-adjusted return over
trailing 30/60/90 days. Allocates capital proportionally to a
`capital_score = recent_sharpe × (1 + win_rate)`, clamped so no
profile drops below 25% or rises above 200% of its baseline
allocation per rebalance.

Re-allocation is a recommendation only by default — flipped to
auto-action when a per-user toggle `auto_capital_allocation` is
enabled (default OFF, similar to `ai_model_auto_tune`).

---

## Cost Guard (Cross-Cutting)

New module `cost_guard.py`:
- `daily_ceiling_usd(user_id) -> float` — configurable, defaults to user's trailing-7-day-avg × 1.5
- `projected_daily_spend(user_id) -> float` — current spend + projected remaining cycles
- `can_afford_action(user_id, estimated_extra_cost_usd) -> bool`

Every autonomous action that could increase API cost (Layer 6 prompt
verbosity changes, Layer 8 strategy generation, Layer 2 weight changes
that re-include omitted signals) calls `can_afford_action` first. If
False, the action is queued as a recommendation surfacing the cost
estimate.

This is the **only** legitimate use of "Recommendation:" — the
guardrail test allowlist is updated to include
`"Recommendation: cost-gated"` as a valid prefix for these.

---

## Anti-Regression Tests

### Existing (already added)
- `tests/test_no_recommendation_only.py` — every "Recommendation:" string in `self_tuning.py` must be on the allowlist with rationale.

### New
- `tests/test_every_lever_is_tuned.py` — walks `trading_profiles` schema, asserts every column is either:
  - On the `MANUAL_PARAMETERS` allowlist (with rationale string), OR
  - Has a tuning rule (`update_trading_profile(... <param> ...)` called somewhere in `self_tuning.py`), OR
  - Is in `signal_weights` / `regime_overrides` / `tod_overrides` / `symbol_overrides` JSON dicts (handled by Layer 2/3/4/7)
  
- `tests/test_signal_weights_lifecycle.py` — round-trip on storage; tuner nudge down / up / cooldown / reversal.

- `tests/test_regime_overrides.py` — `resolve_param` falls back correctly; tuner creates per-regime overrides only with sufficient sample.

- `tests/test_tod_overrides.py` — same as regime.

- `tests/test_cost_guard.py` — actions blocked when projected spend > ceiling; allowed otherwise; recommendation surfaces with cost estimate.

- `tests/test_cross_profile_propagation.py` — improvement on profile A triggers detection (not value-copy) on profile B.

- `tests/test_per_symbol_overrides.py` — `resolve_param` precedence; tuner creates per-symbol only with ≥20 samples.

---

## UI / Documentation Updates

1. **`SELF_TUNING.md`** — full rewrite. Replace "4 parameters" / "Future Parameters Planned Late May 2026" with the layered architecture above.
2. **`AI_ARCHITECTURE.md`** — add the 9-layer autonomy diagram.
3. **`README.md`** — one paragraph summary of full autonomy.
4. **`EXECUTIVE_OVERVIEW.md`** — mention the cost guard prominently.
5. **Settings page** — add an "Autonomy" section showing every parameter, current value, current regime/TOD overrides, and "Auto-tuned by: {layer}" badge. Per-profile opt-in toggles for `ai_model_auto_tune` and `auto_capital_allocation`.
6. **AI Operations tab** — add cards for: Active Signal Weights, Active Regime Overrides, Active TOD Overrides, Per-Symbol Overrides, Cost Guard Status.
7. **Tuning history rows** — every layer's adjustments use namespaced parameter names (`weight:insider_cluster`, `regime:volatile:stop_loss_pct`, `tod:open:max_position_pct`, `symbol:NVDA:stop_loss_pct`) so the existing display_name fallback renders them cleanly.

---

## Implementation Waves

Each wave is a self-contained commit + deploy.

| Wave | Layers | Scope |
|------|--------|-------|
| W1 | Layer 1 Group A + Group D | 10 parameters + bounds clamping infra |
| W2 | Layer 1 Group C | 8 entry filter parameters |
| W3 | Layer 1 Group B | 6 exit parameters (excluding the 3 booleans rolling into Layer 2) |
| W4 | Layer 2 | Weighted signal intensity infra + tuner + prompt builder + 16+ initial weights |
| W5 | Layer 3 | Per-regime overrides (`resolve_param`, schema, tuner, UI) |
| W6 | Layer 4 | Per-time-of-day overrides |
| W7 | Cost guard | Module + integration into all spend-touching layers |
| W8 | Layer 7 | Per-symbol overrides |
| W9 | Layer 5 | Cross-profile insight sharing |
| W10 | Layer 6 | Adaptive AI prompt structure (cost-gated) |
| W11 | Layer 8 | Self-commissioned new strategies (cost-gated) |
| W12 | Layer 9 | Automatic capital allocation (opt-in) |
| W13 | Anti-regression tests + docs + Settings UI Autonomy section | Final pass |

Each wave passes the full test suite, gets a CHANGELOG entry, commits,
and deploys.

---

## Acceptance Criteria

- ✅ Every column in `trading_profiles` is autonomous OR on the manual allowlist with rationale
- ✅ `signal_weights`, `regime_overrides`, `tod_overrides`, `symbol_overrides`, `prompt_layout` columns exist
- ✅ `resolve_param(profile, name, regime, tod, symbol)` is the single source of truth for parameter access
- ✅ Cost guard wraps every cost-affecting autonomous action
- ✅ All anti-regression tests pass
- ✅ `SELF_TUNING.md` accurate; `AI_ARCHITECTURE.md` updated; CHANGELOG documents the full change
- ✅ Settings page Autonomy section + per-profile opt-in toggles deployed
- ✅ Cross-profile insight sharing demonstrably triggers (test verifies)
- ✅ Production deployed
