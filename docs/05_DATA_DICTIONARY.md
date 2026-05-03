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
| `market_type` | TEXT | required | `largecap` / `midcap` / `smallcap` / `microsmall` / `crypto` / `*_shorts` variants. Defines universe + strategy emphasis. |
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
| `max_price` | REAL | 20.0 | Maximum stock price. |
| `min_volume` | INTEGER | 500000 | Minimum daily volume. |
| `volume_surge_multiplier` | REAL | 2.0 | Volume-vs-average ratio for surge detection. |
| `rsi_overbought` | REAL | 85.0 | RSI level above which entries are suppressed. |
| `rsi_oversold` | REAL | 25.0 | RSI level below which mean-reversion entries fire. |
| `momentum_5d_gain` | REAL | 3.0 | Min 5d gain (%) for momentum strategies. |
| `momentum_20d_gain` | REAL | 5.0 | Min 20d gain (%). |
| `breakout_volume_threshold` | REAL | 1.0 | Volume threshold for breakout confirmation. |
| `gap_pct_threshold` | REAL | 3.0 | Gap size (%) for gap-and-go strategies. |

### Strategy toggles

| Column | Type | Default | Description |
|---|---|---|---|
| `strategy_momentum_breakout` | INTEGER | 1 | Enable momentum_breakout strategy. |
| `strategy_volume_spike` | INTEGER | 1 | Enable volume_spike. |
| `strategy_mean_reversion` | INTEGER | 1 | Enable mean_reversion. |
| `strategy_gap_and_go` | INTEGER | 1 | Enable gap_and_go. |

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
| `ai_provider` | TEXT | `anthropic` | `anthropic` / `openai` / `google`. |
| `ai_model` | TEXT | `claude-haiku-4-5-20251001` | Provider-specific model ID. |
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
| `enable_short_selling` | INTEGER | 0 | Allow shorts. |
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
| `skip_first_minutes` | INTEGER | 0 | Skip first N minutes of session. |

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
| `use_conviction_tp_override` | INTEGER | 0 | Skip fixed TP when AI conviction is high. |
| `conviction_tp_min_confidence` | REAL | 70.0 | Min AI confidence to skip TP. |
| `conviction_tp_min_adx` | REAL | 25.0 | Min ADX (trend strength) to skip TP. |

### Self-tuning

| Column | Type | Default | Description |
|---|---|---|---|
| `enable_self_tuning` | INTEGER | 1 | Master on/off for the 12-layer self-tuner. |
| `signal_weights` | TEXT | `'{}'` | JSON dict {signal_name: weight ∈ [0.0, 1.0]} (Layer 2). |
| `regime_overrides` | TEXT | `'{}'` | JSON {param: {regime: value}} (Layer 3). |
| `tod_overrides` | TEXT | `'{}'` | JSON {param: {tod_bucket: value}} (Layer 4). |
| `symbol_overrides` | TEXT | `'{}'` | JSON {param: {symbol: value}} (Layer 7). |
| `prompt_layout` | TEXT | `'{}'` | JSON {section: verbosity} (Layer 6). |

### Cost levers

| Column | Type | Default | Description |
|---|---|---|---|
| `disabled_specialists` | TEXT | `'[]'` | JSON list of specialist names to skip (Lever 3). |
| `meta_pregate_threshold` | REAL | 0.5 | Min meta_prob to pass pre-gate (Lever 2). |

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
| `status` | TEXT | `open` / `filled` / `closed` / `canceled`. |
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
| `actual_return_pct` | REAL | Realized return. |
| `resolution_price` | REAL | Price at resolution. |
| `days_held` | INTEGER | Days from prediction to resolution. |
| `resolved_at` | TEXT | UTC ISO timestamp of resolution. |

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

### `pdufa_events` (in `~/quantopsai-altdata/biotechevents/biotechevents.db`)
Scraped FDA PDUFA dates per ticker.

### `users` (master DB)
Operator accounts.

### `alpaca_accounts` (master DB)
Configured Alpaca paper accounts (1-3 per user).

### `decision_log` (master DB)
Cross-profile decision audit trail.

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

Source: `multi_scheduler.py` `_task_*` functions. 37 tasks total. Each is either:

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
| `_task_virtual_audit` | once/day | Virtual position FIFO consistency. |
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
