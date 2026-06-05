# 05 — Data Dictionary

**Audience:** quants, engineers, anyone who needs the canonical name and definition of any column / signal / feature / knob.
**Purpose:** the reference open while reading every other doc. If a name is mentioned anywhere else and you need to look it up, look here.
**Last updated:** 2026-05-03.

## Table of contents

1. [Per-profile schema (`trading_profiles`)](#1-per-profile-schema-trading_profiles)
2. [Trade journal (`trades`)](#2-trade-journal-trades)
3. [AI prediction journal (`ai_predictions`)](#3-ai-prediction-journal-ai_predictions)
4. [Other persistent tables](#4-other-persistent-tables)
5. [Meta-model NUMERIC_FEATURES](#5-meta-model-numeric_features)
6. [Meta-model CATEGORICAL_FEATURES](#6-meta-model-categorical_features)
7. [Layer-2 weightable signals](#7-layer-2-weightable-signals)
8. [UserContext fields](#8-usercontext-fields)
9. [Scheduler tasks](#9-scheduler-tasks)
10. [Display name registry](#10-display-name-registry)

---

## 1. Per-profile schema (`trading_profiles`)

Every per-profile setting lives here. Source of truth: `models.py` `init_user_db` + `_migrations` list.

### Identity & metadata

| Column | Type | Default | Description |
|---|---|---|---|
| `id` | INTEGER PK | autoincrement | Profile ID. Used everywhere as the per-profile key. |
| `user_id` | INTEGER | required | Foreign key → `users.id`. Owner. |
| `name` | TEXT | required | User-chosen profile name. |
| `market_type` | TEXT | required | `stocks` / `crypto`. Cap tiers (`largecap` / `midcap` / `small` / `micro`) were removed 2026-05-20 (commit `a49c9d6`); see `docs/archive/2026-06-04-pre-audit/22_UNIFIED_STOCK_UNIVERSE.md` for migration detail. `stocks` profiles trade the unified Alpaca-tradable US equity universe filtered per-profile by `min_price` / `max_price` / `min_volume`. `crypto` profiles use a separate code path (24/7 schedule, crypto data endpoints). The actual instrument-class pipeline split (`stock` vs `option`) lives in `pipelines/dispatch.py`. |
| `enabled` | INTEGER | 1 | Master on/off for this profile. |
| `created_at` | TEXT | now() | Profile creation timestamp. |

### Authentication / API keys (encrypted)

| Column | Type | Description |
|---|---|---|
| `alpaca_api_key_enc` | TEXT | Encrypted Alpaca API key. |
| `alpaca_secret_key_enc` | TEXT | Encrypted Alpaca secret. |
| `alpaca_account_id` | INTEGER | FK → `alpaca_accounts.id`. Which of the 3 paper accounts this profile maps to. |
| `ai_api_key_enc` | TEXT | Encrypted AI provider API key. |
| `consensus_api_key_enc` | TEXT | Encrypted secondary AI provider key (for multi-model consensus). |

### Risk and sizing

| Column | Type | Default | Description |
|---|---|---|---|
| `stop_loss_pct` | REAL | 0.03 | Long stop loss as fraction of entry price. |
| `take_profit_pct` | REAL | 0.10 | Long take profit as fraction. |
| `max_position_pct` | REAL | 0.10 | Max position size for longs as fraction of equity. |
| `max_total_positions` | INTEGER | 10 | Hard cap on concurrent positions. |
| `max_correlation` | REAL | 0.7 | Max 30d return correlation with existing positions. |
| `max_sector_positions` | INTEGER | 5 | Concentration cap per sector. |
| `drawdown_pause_pct` | REAL | 0.20 | Drawdown level at which all trading pauses. |
| `drawdown_reduce_pct` | REAL | 0.10 | Drawdown level at which size scales down. |

### Screener parameters

| Column | Type | Default | Description |
|---|---|---|---|
| `min_price` | REAL | 1.0 | Minimum stock price for inclusion. |
| `max_price` | REAL | 10000.0 | Maximum stock price (widened to 10000 with the 2026-05-20 unified-universe migration so a profile actually sees the full Alpaca pool; prior cap-tier default of 20 was for `microsmall`). |
| `min_volume` | INTEGER | 100000 | Minimum daily volume (lowered to 100K with the unified-universe migration; widen / tighten per profile to dial liquidity). |
| `volume_surge_multiplier` | REAL | 2.0 | Volume-vs-average ratio for surge detection. |
| `rsi_overbought` | REAL | 85.0 | RSI level above which entries are suppressed. |
| `rsi_oversold` | REAL | 25.0 | RSI level below which mean-reversion entries fire. |
| `momentum_5d_gain` | REAL | 3.0 | Min 5d gain (%) for momentum strategies. |
| `momentum_20d_gain` | REAL | 5.0 | Min 20d gain (%). |
| `breakout_volume_threshold` | REAL | 1.0 | Volume threshold for breakout confirmation. |
| `gap_pct_threshold` | REAL | 3.0 | Gap size (%) for gap-and-go strategies. |

### Strategy toggles (legacy — gate dead-code path)

| Column | Type | Default | Description |
|---|---|---|---|
| `strategy_momentum_breakout` | INTEGER | 1 | (Legacy.) Gates `fallback_strategy.py` / `strategy_small.py` momentum_breakout — only invoked via the now-removed cap-tier branch in `strategy_router.py`. Live strategy of the same name lives in `strategies/` as a plugin. |
| `strategy_volume_spike` | INTEGER | 1 | (Legacy — same.) Live volume_spike is in `strategies/`. |
| `strategy_mean_reversion` | INTEGER | 1 | (Legacy — same.) Live mean_reversion is in `strategies/`. |
| `strategy_gap_and_go` | INTEGER | 1 | (Legacy — same.) Live gap_and_go is in `strategies/`. |

> The four columns above remain in the schema for backward compatibility; flipping them off does not disable the corresponding live strategy (it disables the dead-code legacy path). To disable a live strategy, use `signal_weights` (set the strategy's vote weight to 0).

### Custom watchlist

| Column | Type | Default | Description |
|---|---|---|---|
| `custom_watchlist` | TEXT | `'[]'` | JSON list of additional symbols (always traded). |

### Schedule

| Column | Type | Default | Description |
|---|---|---|---|
| `schedule_type` | TEXT | `market_hours` | `market_hours` / `extended` / `custom`. |
| `custom_start` | TEXT | `09:30` | Custom session start (HH:MM ET). |
| `custom_end` | TEXT | `16:00` | Custom session end. |
| `custom_days` | TEXT | `0,1,2,3,4` | Days of week (0=Mon ... 4=Fri). |

### AI provider

| Column | Type | Default | Description |
|---|---|---|---|
| `ai_provider` | TEXT | `google` | `anthropic` / `openai` / `google`. Default flipped from `anthropic` to `google` after the 2026-05-19 silent-fallback gate (CHANGELOG) and to reduce per-cycle cost (~$0.27/day at current `gemini-2.5-flash-lite` rate). |
| `ai_model` | TEXT | `gemini-2.5-flash-lite` | Provider-specific model ID. |
| `ai_confidence_threshold` | INTEGER | 25 | Min AI confidence to act on. |
| `ai_model_auto_tune` | INTEGER | 0 | Allow tuner to A/B-test models within cost guard. |

### Multi-model consensus

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_consensus` | INTEGER | 0 | Run secondary AI for cross-validation. |
| `consensus_model` | TEXT | `''` | Secondary model ID. |

### Long/short construction

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_short_selling` | INTEGER | 1 | Allow opening short positions (default flipped 0→1 on 2026-05-12 for non-crypto profiles). AI-tunable via `_optimize_short_selling_toggle` based on 30-day short-side avg return. |
| `short_stop_loss_pct` | REAL | 0.08 | Short-side stop loss. |
| `short_take_profit_pct` | REAL | 0.08 | Short-side take profit. |
| `short_max_position_pct` | REAL | NULL | Short-side position cap (default = half of `max_position_pct`). |
| `short_max_hold_days` | INTEGER | 10 | Time stop on shorts. |
| `target_short_pct` | REAL | 0.0 | Target short share of gross book. |
| `target_book_beta` | REAL | NULL | Target book beta (NULL = no target). |

### Earnings / time-of-day

| Column | Type | Default | Description |
|---|---|---|---|
| `avoid_earnings_days` | INTEGER | 2 | Skip stocks with earnings in N days. |
| `skip_first_minutes` | INTEGER | 5 | Skip first N minutes after market open (default bumped 0→5 on 2026-05-12). AI-tunable on win-rate AND slippage signals independently. |

### ATR-based stops & limit orders

| Column | Type | Default | Description |
|---|---|---|---|
| `use_atr_stops` | INTEGER | 1 | Use ATR-derived stop distance. |
| `atr_multiplier_sl` | REAL | 2.0 | ATR multiplier for stop. |
| `atr_multiplier_tp` | REAL | 3.0 | ATR multiplier for take profit. |
| `use_trailing_stops` | INTEGER | 1 | Use broker trailing stops. |
| `trailing_atr_multiplier` | REAL | 1.5 | ATR multiplier for trailing distance. |
| `use_limit_orders` | INTEGER | 0 | Limit orders instead of market. |

### Conviction TP override

| Column | Type | Default | Description |
|---|---|---|---|
| `use_conviction_tp_override` | INTEGER | 1 | Skip fixed TP when AI conviction is high (default flipped ON 2026-05-12). AI-tunable via `_optimize_conviction_tp_override` based on MFE capture + stop-to-TP ratio. |
| `option_iv_rich_threshold` | REAL | 55.0 | Above this IV rank, option candidate generator proposes credit-spread strategies (bull_put / bear_call). AI-tunable via `OptionPipeline.tune()`. |
| `option_iv_cheap_threshold` | REAL | 55.0 | Below this IV rank, option candidate generator proposes debit-spread strategies (bull_call / bear_put). Default 55/55 = no dead zone. AI-tunable. |
| `entry_blacklist` | TEXT (JSON) | `'{}'` | Per-symbol entry cool-off after 3+ stop-outs in 30 days. JSON `{"NVDA": "ISO_EXPIRY"}`. Auto-expires on read. Maintained by `_optimize_stop_out_blacklist`. |
| `conviction_tp_min_confidence` | REAL | 70.0 | Min AI confidence to skip TP. |
| `conviction_tp_min_adx` | REAL | 25.0 | Min ADX (trend strength) to skip TP. |

### Self-tuning

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_self_tuning` | INTEGER | 1 | Master on/off for the autonomous self-tuner (12 original layers + 5 deterministic guardrails added 2026-05-18 per `docs/17`). When 0, no `_optimize_*` rule fires for this profile (used by the "No Self-Tuning" ablation in docs/15). |
| `signal_weights` | TEXT | `'{}'` | JSON dict {signal_name: weight ∈ [0.0, 1.0]} (Layer 2). |
| `regime_overrides` | TEXT | `'{}'` | JSON {param: {regime: value}} (Layer 3). |
| `tod_overrides` | TEXT | `'{}'` | JSON {param: {tod_bucket: value}} (Layer 4). |
| `symbol_overrides` | TEXT | `'{}'` | JSON {param: {symbol: value}} (Layer 7). |
| `prompt_layout` | TEXT | `'{}'` | JSON {section: verbosity} (Layer 6). Tunable sections: `alt_data`, `political_context`, `learned_patterns`, `portfolio_state`, `portfolio_risk_scenarios`. Verbosity values: `brief` / `normal` (default) / `detailed`. For `portfolio_risk_scenarios`: brief=0 scenarios, normal=worst-1, detailed=worst-3. |

### Cost levers

| Column | Type | Default | Description |
|---|---|---|---|
| `disabled_specialists` | TEXT | `'[]'` | JSON list of specialist names to skip (Lever 3). |
| `meta_pregate_threshold` | REAL | 0.35 | Min meta_prob to pass pre-gate (Lever 2). Default lowered 0.5→0.35 on 2026-05-13 after audit found 68% of candidates being filtered before AI evaluation. AI-tunable via `_optimize_meta_pregate_threshold` based on 5-day actionable-signal ratio. |

### Capital allocation

| Column | Type | Default | Description |
|---|---|---|---|
| `capital_scale` | REAL | 1.0 | Auto-allocator-recommended size scalar (Layer 9). |

### Virtual account layer

| Column | Type | Default | Description |
|---|---|---|---|
| `is_virtual` | INTEGER | 0 | Profile is virtual (FIFO from trades) vs broker-direct. |
| `initial_capital` | REAL | 100000.0 | Starting capital for virtual P&L. |
| `maga_mode` | INTEGER | 0 | Inject political-context block into AI prompt. |

### Experiment ablation flags (2026-05-17)

Operator-set columns that disable specific subsystems for the
13-profile fresh-start experiment (docs/15). Never auto-tuned —
auto-tuning ablations would defeat the experiment.

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_alt_data` | INTEGER | 1 | When 0, alt-data fetcher in `trade_pipeline._get_universe_context` is skipped; every candidate gets `alt_data=None`. Used by "No Alt-Data" ablation. |
| `enable_meta_model` | INTEGER | 1 | When 0, `_meta_pregate_candidates` falls open (returns all candidates) and the main meta-model load is bypassed (raw AI confidence flows unmodified). Used by "No Meta-Model" ablation. |
| `enable_options` | INTEGER | 1 | When 0, `ai_analyst.build_prompt` short-circuits the `multileg_block` builder so the AI prompt contains no options-strategy section. Used by "No Options" ablation. |
| `enable_stocks` | INTEGER | 1 | When 0, `StockPipeline.applies_to(ctx)` returns False — the stock pipeline does not run for this profile. Used by "No Stocks" / crypto-only profiles. |
| `enable_crypto` | INTEGER | 0 | When 1, `CryptoPipeline` (when implemented) runs. Today only the column exists; the crypto pipeline path is not yet wired (per `OPEN_ITEMS.md`). |
| `strategy_type` | TEXT | `'ai'` | Strategy mode. `'ai'` = full pipeline (current behavior). `'buy_hold'` = buy SPY day 1 and hold (`simple_strategies.run_buy_hold_spy`). `'random'` = pick 5 random stocks/day deterministically with no AI consultation (`simple_strategies.run_random_stock_of_day`). Architectural choice, not tunable. Used by the Account 1 baseline profiles in docs/15. |
| `auto_capital_allocation` | INTEGER | 0 | When 0 (the deliberate baseline-control default), `strategy_capital_allocator` does NOT mutate `capital_scale` for this profile — see `project_capital_allocation` memory. |

### Cutover + shadow

These columns gate the pipeline-dispatch cutover (per `docs/14` Phase 0 → `docs/18` exit criteria). Default OFF; flip per-profile only after shadow soak shows verdict agreement ≥ 95% for 1–2 trading days.

| Column | Type | Default | Description |
|---|---|---|---|
| `use_pipeline_dispatch` | INTEGER | 0 | When 1, `multi_scheduler:957` routes through `pipelines.dispatch.run_via_pipelines` (calls `Pipeline.run_cycle` per pipeline) instead of legacy `trade_pipeline.run_trade_cycle`. Mutually exclusive per cycle. |
| `enable_pipeline_shadow_eval` | INTEGER | 0 | When 1, `pipelines/shadow.py` runs StockPipeline + OptionPipeline in parallel with the legacy dispatcher (read-only — stops before `execute()`) and writes divergence rows to `pipeline_shadow_runs`. Cost: ~$0.01–0.02/cycle per shadow-enabled profile. |
| `scan_interval_minutes` | INTEGER | 15 | Operator-tunable scan cadence (15 / 10 / 5 / 3 / 2 min — added 2026-06-04). Read by `multi_scheduler._scan_interval_seconds()` every loop iteration; UI change takes effect on the next cycle (no restart). Settings → AI Behavior dropdown. |

### Trading halt (operator override)

| Column | Type | Default | Description |
|---|---|---|---|
| `trading_halted` | INTEGER | 0 | When 1, the pre-trade gate `kill_switch.is_halted(profile_id)` returns True and every new entry on this profile is blocked with reason `HALT_TRADING`. Set by `_optimize_options_pnl_cutoff` (per #171 — auto-halts options on −3% × initial_capital threshold) and by manual `/api/halt-trading` POST from the dashboard. |
| `halt_reason` | TEXT | `''` | Human-readable reason populated when `trading_halted=1` (e.g. `"options auto-cutoff: −3.4% over 12 trades"`). |
| `halted_at` | TEXT | NULL | UTC ISO 8601 timestamp set when the halt fires. |

### Item 1c — Long-vol portfolio hedge

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_long_vol_hedge` | INTEGER | 0 | Master toggle. |
| `long_vol_hedge_drawdown_pct` | REAL | 0.05 | Drawdown threshold to trigger hedge. |
| `long_vol_hedge_var_pct` | REAL | 0.03 | VaR threshold to trigger hedge. |
| `long_vol_hedge_premium_pct` | REAL | 0.01 | Premium budget per active hedge. |

### Item 1b — Statistical arbitrage

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_stat_arb_pairs` | INTEGER | 0 | Enable cointegration pair book + scheduled tasks. |

### Item 2a/2b — Risk monitoring

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_intraday_risk_halt` | INTEGER | 1 | Master toggle for intraday auto-halt monitor. |
| `enable_portfolio_risk_snapshot` | INTEGER | 1 | Master toggle for daily Barra-style snapshot. |

### Options programs

| Column | Type | Default | Description |
|---|---|---|---|
| `wheel_symbols` | TEXT | `'[]'` | JSON list of symbols opted into wheel cycle. |
| `options_roll_window_days` | INTEGER | 7 | Days-to-expiry window for roll manager. |
| `options_auto_close_profit_pct` | REAL | 0.80 | Auto-close credit positions at this % of max profit. |
| `options_roll_recommend_profit_pct` | REAL | 0.50 | Recommend roll above this % of max profit. |
| `max_net_options_delta_pct` | REAL | 0.05 | Greek-budget cap: `|options-only delta| / equity`. Tunable by `OptionPipeline.tune`. |
| `max_theta_burn_dollars_per_day` | REAL | 50.0 | Greek-budget cap: max $/day of premium decay before blocking new long-premium trades. Tunable. |
| `max_short_vega_dollars` | REAL | 500.0 | Greek-budget cap: max short vega before blocking new short-premium trades. Tunable. |
| `option_premium_stop_loss_pct` | REAL | -0.50 | LONG single-leg: close when premium drops by this fraction (-0.50 = -50%). Read by `options_exits.check_single_leg_option_exits` via ctx. Tunable. |
| `option_premium_take_profit_pct` | REAL | 1.00 | LONG single-leg: close when premium gains by this fraction (1.00 = +100%). Tunable. |
| `option_dte_exit_threshold_days` | INTEGER | 7 | LONG and SHORT single-leg: close when days-to-expiry ≤ this value. Tunable (looser = lower N). |
| `option_short_premium_take_profit_pct` | REAL | -0.50 | SHORT single-leg: close (win) when premium drops by this fraction. Tunable. |
| `option_short_premium_stop_loss_pct` | REAL | 1.00 | SHORT single-leg: close (loss) when premium expands by this fraction. Tunable. |
| `option_spread_iv_rank_veto_threshold` | REAL | 80.0 | `option_spread_risk` specialist VETOes LONG-premium proposals at iv_rank > this. Surfaced in the LLM prompt. Tunable. |
| `option_spread_gamma_dte_veto_threshold` | INTEGER | 7 | `option_spread_risk` VETOes SHORT-options proposals at DTE < this. Tunable (looser = lower N). |
| `option_spread_credit_ratio_veto_threshold` | REAL | 0.20 | `option_spread_risk` VETOes credit-spread proposals at credit/max-loss < this. Tunable (looser = lower). |

---

## 2. Trade journal (`trades`)

Per-profile DB. Every order submitted lands here.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `timestamp` | TEXT | UTC ISO 8601, default `now()`. |
| `symbol` | TEXT | Underlying ticker. |
| `side` | TEXT | `buy` / `sell` / `sell_short` / `buy_to_cover`. |
| `qty` | REAL | Shares (or contracts × 100 for options). |
| `price` | REAL | Decision-time price. |
| `order_id` | TEXT | Alpaca order ID. |
| `signal_type` | TEXT | `BUY` / `SHORT` / `SELL` / `OPTIONS` / `MULTILEG` / `PAIR_TRADE`. |
| `strategy` | TEXT | Strategy name that produced the vote. |
| `reason` | TEXT | Human-readable reason. |
| `ai_reasoning` | TEXT | LLM rationale verbatim. |
| `ai_confidence` | REAL | AI confidence 0-100. |
| `stop_loss` | REAL | Price level for stop. |
| `take_profit` | REAL | Price level for TP. |
| `status` | TEXT | `open` (entry, position held) / `pending_fill` (close submitted to broker, fill not yet confirmed) / `closed` (broker-confirmed close) / `canceled` (entry never filled at broker, phantom undo). FIFO virtual-position book filters only on `status != 'canceled'`. The `pending_fill` → `closed` transition is driven by `_task_update_fills` once Alpaca returns `filled_avg_price`. |
| `pnl` | REAL | Realized P&L (only on closing rows). |
| `decision_price` | REAL | Price the strategy/AI saw at decision. |
| `fill_price` | REAL | Actual broker fill. Updated by fill updater. |
| `slippage_pct` | REAL | `(fill - decision) / decision * 100`. |
| `predicted_slippage_bps` | REAL | Slippage model prediction at submit (Item 5c). |
| `adv_at_decision` | REAL | 20d ADV at submit time (OPEN_ITEMS #1). |
| `max_favorable_excursion` | REAL | Highest price the position touched (long) / lowest (short). |
| `protective_stop_order_id` | TEXT | Alpaca order ID for broker stop. |
| `protective_tp_order_id` | TEXT | Alpaca order ID for broker TP (legacy). |
| `protective_trailing_order_id` | TEXT | Alpaca order ID for broker trailing stop. |
| `occ_symbol` | TEXT | OCC option contract symbol (options only). |
| `option_strategy` | TEXT | `covered_call` / `protective_put` / etc. |
| `expiry` | TEXT | Option expiry ISO date. |
| `strike` | REAL | Option strike. |

---

## 3. AI prediction journal (`ai_predictions`)

The proprietary asset. Every AI decision writes a row.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | autoincrement. |
| `timestamp` | TEXT | UTC ISO 8601. |
| `symbol` | TEXT | Ticker. |
| `predicted_signal` | TEXT | `STRONG_BUY` / `BUY` / `HOLD` / `SELL` / `STRONG_SELL` / `SHORT` / `STRONG_SHORT`. |
| `confidence` | INTEGER | 0-100. |
| `reasoning` | TEXT | LLM rationale. |
| `prediction_type` | TEXT | `directional_long` / `directional_short` / `exit_long` / `exit_short`. |
| `features_json` | TEXT | Full feature snapshot the AI saw, ~80 fields. |
| `price_at_prediction` | REAL | Price at decision. |
| `price_targets` | TEXT | JSON {stop, take_profit}. |
| `status` | TEXT | `pending` / `resolved`. |
| `actual_outcome` | TEXT | `win` / `loss` / `neutral`. |
| `actual_return_pct` | REAL | Realized **gross** return (price-move only; ignores slippage). |
| `actual_return_pct_net` | REAL | Realized return **net of round-trip slippage** (approximated as 2 × entry slippage from the matched trade row; 0 for option signals, where premium P&L already nets). The learning loop and self-tuner should pivot to this column; `actual_return_pct` is kept for backward compat. Written by `ai_tracker.resolve_predictions`. |
| `rule_votes_json` | TEXT | #185 (2026-05-20). JSON list of deterministic-panel verdicts that fired for this prediction at decision time. Each entry: `{name, severity: VETO\|CAUTION\|CONFIRM, direction: long\|short\|neutral}`. Reasoning text intentionally dropped (reconstructable; bloats row). Joined by the fine-tune dataset builder to `ai_prediction_outcomes` so "rule X fired in direction Y → outcome at horizon Z" is one query. |
| `resolution_price` | REAL | Price at resolution. |
| `days_held` | INTEGER | Days from prediction to resolution. |
| `resolved_at` | TEXT | UTC ISO timestamp of resolution. |

### `ai_prediction_outcomes` (per-profile)

#185 (2026-05-20). Multi-horizon outcome rows, one per `(prediction_id,
horizon_days)` pair. Designed for the future fine-tune dataset
builder (`ai_tracker.build_training_dataset`) — sibling table rather
than wide columns on `ai_predictions` so adding a new horizon is a
one-line constant change with no schema migration. Stock signals only
(option outcomes go through `pipelines/outcomes/option_resolver`).
Written by `ai_tracker.measure_horizon_outcomes` on each scheduler
cycle alongside `resolve_predictions`. Idempotent via UNIQUE.

| Field | Type | Notes |
|-------|------|-------|
| `id` | INTEGER PK | |
| `prediction_id` | INTEGER | FK → ai_predictions(id). |
| `horizon_days` | INTEGER | One of 1, 3, 5, 10, 20. |
| `price_at_horizon` | REAL | Close price at horizon bar. |
| `return_pct` | REAL | Gross return at horizon. Signed by prediction direction (long: (exit-entry)/entry; short: -(exit-entry)/entry). |
| `return_pct_net` | REAL | Cost-adjusted return — `return_pct - 2 × entry_slippage` from the matched trade row. |
| `mfe_pct` | REAL | Max favorable excursion within (entry, horizon] window. Signed by direction so positive MFE always means "the prediction was right at some point" — long: `(max_high - entry)/entry`; short: `(entry - min_low)/entry`. |
| `mae_pct` | REAL | Max adverse excursion within window. Always non-positive when present. Long: `-(entry - min_low)/entry`; short: `-(max_high - entry)/entry`. |
| `outcome_class` | TEXT | Categorical label for cross-entropy training: `big_win` (≥5%), `win` (≥1%), `flat` (>-1%), `loss` (>-5%), `big_loss` (≤-5%). Boundary-strict on the loss side (a -1% return labels as "loss", not "flat"). |
| `measured_at` | TEXT | UTC ISO timestamp when the row was written. |

Unique: `(prediction_id, horizon_days)` — re-running the measurer
silently skips already-filled rows.

---

## 4. Other persistent tables

### `daily_snapshots` (per-profile)
Per-day equity / cash / num_positions / daily_pnl.

### `signals` (per-profile)
Strategy votes per cycle (audit trail).

### `signal_performance_history` (per-profile)
Per-strategy historical win rate / Sharpe / sample count.

### `deprecated_strategies` (per-profile)
Strategies the alpha-decay monitor has auto-deprecated.

### `sec_filings_history` (per-profile)
SEC filings analyzed by `sec_filings.monitor_symbol`. Diff-detected alerts feed the AI prompt.

### `task_runs` (per-profile)
Audit log of scheduler task executions (when, duration, error if any).

### `recently_exited_symbols` (per-profile)
Cooldown table — wash-trade flagged, recently sold, or otherwise blocked from re-entry.

### `ai_cost_ledger` (per-profile)
Per-AI-call cost accounting. Daily roll-up via `ai_cost_ledger.spend_summary`.

### `crisis_state_history` (per-profile)
Transitions of the crisis state machine (level changes, signals at the time).

### `events` (per-profile)
Event-bus dispatcher table — SEC alerts, price shocks, halts, etc.

### `auto_generated_strategies` (per-profile)
Strategies commissioned by `strategy_proposer.propose_strategies`.

### `stat_arb_pairs` (per-profile)
Active cointegrated pairs.

### `intraday_risk_halt` (per-profile)
Single-row table for the active risk-halt state.

### `portfolio_risk_snapshots` (per-profile)
Daily Barra-style risk snapshot. 90-day retention.

### `long_vol_hedges` (per-profile)
Open / closed long-vol hedges with entry/exit/triggers/PnL.

### `app_store_history` (master DB, OPEN_ITEMS #2)
Daily snapshots of best App Store ranks per ticker.

### `pdufa_events` (in `altdata/biotechevents/data/biotechevents.db`)
Scraped FDA PDUFA dates per ticker. Populated by `pdufa_scraper.py` via SEC EDGAR full-text search for "PDUFA date" mentions in 8-K filings (replaced BioPharmCatalyst, which is now Cloudflare-protected). Schema: `id, drug_name NOT NULL, sponsor_company NOT NULL, ticker, pdufa_date NOT NULL, action_type (NDA/BLA/sNDA/sBLA), indication, outcome (pending/approved/crl/withdrawn), outcome_date, source_url, parser_version, fetched_at`. UNIQUE on (drug_name, sponsor_company, pdufa_date). Read by `alternative_data.get_biotech_milestones()` to surface `upcoming_pdufa_date` and `days_to_pdufa` to the AI prompt.

### `adcomm_events` (in `altdata/biotechevents/data/biotechevents.db`)
FDA Advisory Committee meeting dates per ticker — leading indicator that typically precedes a PDUFA decision by 1-3 months and materially moves the stock around the meeting. Populated by `pdufa_scraper.fetch_adcomm_events_from_edgar()` via SEC EDGAR full-text search for "Advisory Committee meeting" mentions in 8-K filings; runs as a side-channel inside the same `_task_pdufa_scrape` daily task. Schema: `id, ticker NOT NULL, sponsor_company NOT NULL, drug_name, adcomm_date NOT NULL, committee_name (ODAC/BPAC/EMDAC/...), outcome, outcome_date, source_url, parser_version, fetched_at`. UNIQUE on (ticker, adcomm_date). Read by `get_biotech_milestones()` to surface `upcoming_adcomm_date`, `days_to_adcomm`, and `adcomm_committee` alongside the PDUFA fields.

### `users` (master DB)
Operator accounts.

### `alpaca_accounts` (master DB)
Configured Alpaca paper accounts (1-3 per user).

---

## 5. Meta-model NUMERIC_FEATURES

Source: `meta_model.NUMERIC_FEATURES`. Every numeric input the meta-model uses.

| Feature | Source | Description |
|---|---|---|
| `score` | strategy votes | Composite candidate score (vote count × strength × historical win rate). |
| `rsi` | technicals | 14-day Relative Strength Index. |
| `volume_ratio` | technicals | Today's volume / 20d avg. |
| `atr` | technicals | 14-day Average True Range, normalized to price. |
| `adx` | technicals | 14-day Average Directional Index (trend strength). |
| `stoch_rsi` | technicals | Stochastic RSI 0-100. |
| `roc_10` | technicals | 10-day rate of change. |
| `pct_from_52w_high` | technicals | Percent below 52-week high. |
| `mfi` | technicals | Money Flow Index. |
| `cmf` | technicals | Chaikin Money Flow. |
| `squeeze` | technicals | Bollinger Band squeeze indicator. |
| `pct_from_vwap` | technicals | Percent from VWAP (intraday). |
| `nearest_fib_dist` | technicals | Distance to nearest Fibonacci retracement level. |
| `gap_pct` | technicals | Today's gap from prior close. |
| `rel_strength_vs_sector` | factor | Relative strength vs sector ETF (5d). |
| `short_pct_float` | alt-data | Short interest as fraction of float. |
| `put_call_ratio` | alt-data | Options put/call ratio. |
| `pe_trailing` | factor | Trailing P/E ratio. |
| `reddit_mentions` | alt-data | r/wallstreetbets + r/stocks 30-day mentions. |
| `reddit_sentiment` | alt-data | Net sentiment score from Reddit. |
| `_market_signal_count` | scaffolding | Total bullish strategies firing across the universe. |
| `finra_short_vol_ratio` | alt-data | FINRA RegSHO short volume ratio. |
| `insider_cluster` | alt-data | Cluster score for recent insider activity. |
| `eps_revision_magnitude` | alt-data | Magnitude of recent analyst revisions. |
| `_yield_spread_10y2y` | macro | 10Y - 2Y treasury yield spread. |
| `_cboe_skew` | macro | CBOE Skew Index. |
| `_unemployment_rate` | macro | Latest BLS unemployment rate. |
| `_cpi_yoy` | macro | Latest CPI year-over-year. |
| `dark_pool_pct` | alt-data | Dark pool % of total volume. |
| `earnings_surprise_streak` | alt-data | Consecutive earnings beats / misses. |
| `google_trends_z` | alt-data | Google search interest z-score (Item 3a). |
| `wikipedia_pageviews_z` | alt-data | Wikipedia page-views z-score (Item 3a). |
| `app_store_grossing_rank` | alt-data | Best grossing rank across ticker's apps (or 999 if none). |
| `app_store_free_rank` | alt-data | Best free rank (or 999). |

---

## 6. Meta-model CATEGORICAL_FEATURES

Source: `meta_model.CATEGORICAL_FEATURES`. Each maps to a list of allowed values; the meta-model one-hot encodes them.

| Feature | Values |
|---|---|
| `signal` | STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL, SHORT, STRONG_SHORT |
| `prediction_type` | directional_long, directional_short, exit_long, exit_short |
| `insider_direction` | buying, selling, neutral |
| `options_signal` | bullish_flow, bearish_flow, neutral |
| `vwap_position` | above, at, below |
| `sector_trend` | inflow, outflow, flat |
| `_regime` | bull, bear, sideways, volatile, unknown |
| `congress_direction` | buying, selling, neutral |
| `eps_revision_direction` | up, down, flat |
| `_curve_status` | normal, flat, inverted |
| `insider_near_earnings` | bullish, bearish, neutral |
| `_rotation_phase` | risk_on, risk_off, mixed |
| `earnings_surprise_direction` | beats, misses, mixed |
| `_market_gex_regime` | pinning, expansion, balanced |
| `google_trends_direction` | rising, flat, falling |

---

## 7. Layer-2 weightable signals

Source: `signal_weights.WEIGHTABLE_SIGNALS`. Each can be down-weighted per profile in [0.0, 1.0]. 0.0 = signal completely omitted from prompt; 0.4 = mention with discount hint; 0.7 = mention without flag; 1.0 = full strength.

The full list of 28 weightable signals and their `is_active` predicates lives in `signal_weights.WEIGHTABLE_SIGNALS`. Surfaced on the AI Operations tab "Tunable Signal Weights" panel.

Categories:

- **Insider/options/short:** `insider_cluster`, `insider_direction`, `short_pct_float`, `finra_short_vol_ratio`, `dark_pool_pct`, `options_signal`, `put_call_ratio`
- **Analyst / earnings:** `eps_revision_direction`, `earnings_surprise_streak`
- **Political / congressional / institutional:** `congress_direction`, `congressional_recent`, `institutional_13f`
- **Biotech:** `biotech_milestones`
- **Sentiment:** `stocktwits_sentiment`, `google_trends`, `wikipedia_pageviews`, `app_store_ranking`
- **Technical:** `rel_strength_vs_sector`, `vwap_position`
- **Macro:** `political_context`
- **Strategy votes:** `vote_momentum_breakout`, `vote_volume_spike`, `vote_mean_reversion`, `vote_gap_and_go`, `vote_insider_cluster`, `vote_short_squeeze_setup`, `vote_earnings_drift`, `vote_news_sentiment_spike`

---

## 8. UserContext fields

Source: `user_context.py`. Built per-cycle from the profile row by `models.build_user_context_from_profile`. Every field consumed by code must be on the dataclass (else silent default); enforced by `test_ctx_field_round_trip`.

The full list mirrors the schema columns above plus computed fields like `db_path`, `segment`, `display_name`. See `user_context.py` for the canonical definition.

---

## 9. Scheduler tasks

Source: `multi_scheduler.py` `_task_*` functions. 47 tasks total. Each is either:

- **Per-profile per-cycle** (gated on enable_X column or on INFRASTRUCTURE_TASKS allowlist with rationale), OR
- **Once-per-day** (idempotent, master-DB marker).

| Task | Cadence | Purpose |
|---|---|---|
| `_task_scan_and_trade` | per cycle | THE trade loop. |
| `_task_check_exits` | per cycle | Polling-based exit detection. |
| `_task_cancel_stale_orders` | per cycle | Cancel orders past TIF. |
| `_task_update_fills` | per cycle | Sync broker fill state. |
| `_task_reconcile_trade_statuses` | per cycle | DB consistency sweep. |
| `_task_options_lifecycle` | per cycle | Detect option expiry / assignment. |
| `_task_options_roll_manager` | per cycle | Auto-close credit positions; flag rolls. |
| `_task_options_delta_hedger` | per cycle | Rebalance long-vol option deltas. |
| `_task_intraday_risk_check` | per cycle (gated) | Drawdown / vol / sector / halt monitor. |
| `_task_manage_long_vol_hedge` | per cycle (gated) | Open/roll/close SPY puts. |
| `_task_crisis_monitor` | per cycle | Cross-asset crisis state machine. |
| `_task_event_tick` | per cycle | Event bus dispatcher. |
| `_task_run_watchdog` | per cycle | Self-healing for stuck tasks. |
| `_task_resolve_predictions` | per cycle | Mark resolved predictions; update online model. |
| `_task_stat_arb_retest` | per cycle (gated) | Daily Engle-Granger retest of active pairs. |
| `_task_stat_arb_universe_scan` | weekly (gated) | New-pair discovery. |
| `_task_daily_snapshot` | once/day | Equity / cash / pnl snapshot. |
| `_task_self_tune` | once/day | 12-layer self-tuner. |
| `_task_retrain_meta_model` | once/day | GBM retrain + SGD bootstrap. |
| `_task_calibrate_specialists` | once/day | Platt-scaling refit. |
| `_task_specialist_health_check` | once/day | Auto-disable / re-enable specialists. |
| `_task_universe_audit` | once/day | Survivorship-bias correction. |
| `_task_alpha_decay` | once/day | Per-strategy decay tracking. |
| `_task_sec_filings` | once/day | SEC EDGAR scan. |
| `_task_db_backup` | once/day | Per-profile DB backup. |
| `_task_cost_check` | once/day | Daily AI spend audit. |
| `_task_cross_account_reconcile` | once/day | Virtual ↔ broker position sync. |
| `_task_virtual_audit` | once/day | Virtual position FIFO consistency. Negative-stock-position warnings are suppressed when a `side='short'` journal entry exists for the symbol (legitimate stock short, not corruption). |
| `_task_post_mortem` | weekly | Sunday losing-week analysis. |
| `_task_auto_strategy_lifecycle` | once/day | Auto-deprecate poor strategies. |
| `_task_auto_strategy_generation` | weekly | Commission new strategies. |
| `_task_capital_rebalance` | once/day | Auto capital allocation (Layer 9). |
| `_task_portfolio_risk_snapshot` | once/day (gated) | Barra-style daily snapshot. |
| `_task_app_store_snapshot` | once/day | App Store rank history (Item 3a). |
| `_task_pdufa_scrape` | once/day | PDUFA event scrape (Item 6). |
| `_task_weekly_digest` | weekly | Sunday performance digest. |
| `_task_daily_summary_email` | once/day | Email summary (no-op if no SMTP). |

---

## 10. Display name registry

Source: `display_names._DISPLAY_NAMES`. Every internal identifier surfaced to a user routes through `display_name(internal)` which maps `snake_case` → human label. Categories:

- **Sector codes:** `tech` → "Technology", `comm_services` → "Communication Services", etc.
- **Factor IDs:** `sector_tech` → "Technology Sector", `Mkt-RF` → "Market Excess Return", `SMB` → "Size (Small minus Big)", etc.
- **Stress scenario IDs:** `2008_lehman` → "2008 Lehman / GFC Peak", etc.
- **Strategy names:** `momentum_breakout` → "Momentum Breakout", etc.
- **Parameter keys:** every entry in `param_bounds.PARAM_BOUNDS` has a display label.
- **Risk decomposition groups:** `sectors` / `styles` / `french` / `idio`.

The complete map is in `display_names.py`. Any new identifier rendered to the user must have an entry here or fall back to the default snake-case-to-Title-Case translator. Rendering raw snake_case in HTML is blocked by `test_no_snake_case_in_user_facing_ids`.

## See also

- `docs/02_AI_SYSTEM.md` — how features feed the AI / ML pipeline.
- `docs/03_TRADING_STRATEGY.md` — how the parameters affect trading.
- `docs/04_TECHNICAL_REFERENCE.md` — schema migrations and module map.
- `docs/06_USER_GUIDE.md` — settings UI references for these columns.
