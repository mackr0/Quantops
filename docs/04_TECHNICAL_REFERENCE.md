# 04 — Technical Reference

**Audience:** software engineers joining the project or reviewing it.
**Purpose:** map the codebase. After reading this, an engineer should be able to find any module, understand its responsibility, trace a request end-to-end, and identify where to add new functionality.
**Last updated:** 2026-05-03.

## 1. System overview

```
                                  Multi-User Web App (Flask)
                                  ────────────────────────
                                  app.py · auth.py · views.py
                                              ↑ ↓
        ┌────────────────────── master DB (quantopsai.db) ──────────────────────┐
        │   users · trading_profiles · alpaca_accounts ·                       │
        │   universe_audit_runs · app_store_history · pdufa_scrape_runs ·     │
        │   alt_data_cache · shared_ai_cache                                   │
        └──────────────────────────────────────────────────────────────────────┘
                                              ↕
                    Multi-Scheduler (multi_scheduler.py · 24/7 process)
                    ────────────────────────────────────────────────
                    37 scheduled tasks, per-profile + once-per-day
                                              ↕
        ┌─────────────────────  Per-profile DB (quantopsai_profile_<id>.db) ────┐
        │  trades · ai_predictions · daily_snapshots · signals ·               │
        │  signal_performance_history · deprecated_strategies ·                │
        │  sec_filings_history · task_runs · recently_exited_symbols ·         │
        │  ai_cost_ledger · crisis_state_history · events ·                    │
        │  auto_generated_strategies · stat_arb_pairs ·                        │
        │  intraday_risk_halt · portfolio_risk_snapshots · long_vol_hedges     │
        └──────────────────────────────────────────────────────────────────────┘
                                              ↕
        ┌────────────────  3 Alpaca paper accounts (broker layer) ────────────┐
        │   Account 1 (e.g. mid-cap) · Account 2 (small-cap) · Account 3 (...) │
        │   Each shared by N profiles via virtual-account reconciliation.       │
        └──────────────────────────────────────────────────────────────────────┘
                                              ↕
                    External APIs / Data sources (read-only)
                    ─────────────────────────────────────
                    Anthropic / OpenAI / Google · Alpaca data ·
                    SEC EDGAR · Wikimedia · pytrends · iTunes RSS ·
                    Reddit (PRAW) · StockTwits · ClinicalTrials.gov ·
                    Senate eFD · House Clerk · Ken French CSV ·
                    yfinance (grandfathered for 10 unique data
                      types Alpaca/altdata don't cover — see §15
                      for the full list)
```

## 2. Top-level processes

The deployed system runs three processes:

| Process | Module | Purpose |
|---|---|---|
| `quantopsai-web` | `app.py` (Flask + gunicorn) | User-facing web app. Settings, dashboards, API endpoints. |
| `quantopsai-scheduler` | `multi_scheduler.py` | The trading loop. 5-15 minute cycles per profile + once-daily tasks. |
| `nginx` | (system) | TLS termination + reverse proxy → gunicorn:8000. |

Scheduler and web run as systemd units. `sync.sh` deploys both (rsync + systemd reload).

## 3. Module map

### 3a. Entry points
| Module | Purpose |
|---|---|
| `app.py` | Flask app factory. Registers blueprints (views, auth). |
| `auth.py` | Login/logout, password hashing, session management. |
| `views.py` | All HTTP routes (≈4,500 lines, the second-largest module). |
| `multi_scheduler.py` | Scheduler entry point + the 37 `_task_*` functions. |
| `main.py` | Legacy single-profile entry; deprecated. |

### 3b. Core trade pipeline
| Module | Purpose |
|---|---|
| `trade_pipeline.py` | The cycle orchestrator. Universe → screen → rank → ensemble → AI → validate → execute. |
| `ai_analyst.py` | LLM prompt assembly + response parsing. |
| `ai_providers.py` | Anthropic / OpenAI / Google adapter layer. |
| `ai_cost_ledger.py` | Per-call cost accounting. |
| `ai_pricing.py` | Provider price tables. |
| `ai_tracker.py` | Prediction journaling + resolution. |
| `ensemble.py` | 8-specialist LLM-narrative ensemble synthesizer (5 stock-pipeline + 3 options-pipeline). Five of the eight are re-scoped (2026-05-18, Phase 3) to synthesize from the deterministic panel rather than re-derive facts. |
| `deterministic_specialists/` | 147 pure-Python rule checkers (zero API cost per rule). Each rule = `(candidate, ctx) → Optional[{severity: VETO/CAUTION/CONFIRM, reasoning}]`. Fired verdicts surface in the apex AI prompt and as a compact `RULES: [V]name [C]name ...` suffix in each re-scoped LLM specialist's candidate render. |
| `case_file_rag.py` | RAG over resolved `ai_predictions`. TF-IDF over a rolling 2000-case corpus; injects top-3 most-similar past cases per candidate into the apex prompt as concrete cases-to-reason-from. |
| `specialist_calibration.py` | Platt-scaling per specialist. |
| `meta_model.py` | GBM batch model. |
| `online_meta_model.py` | SGD freshness layer. |
| `client.py` | Alpaca REST adapter (orders, positions, account, asset metadata). The shared price-fetcher routes OCC option symbols (21 chars, C/P at index 12, 8-digit strike suffix) to `/v1beta1/options/snapshots/<underlying>` for mid premium; stock symbols use the snapshots/latest-trade path. Both paths share the in-process TTL cache. |
| `order_guard.py` | Schedule-window + duplicate-order checks before submit. |
| `bracket_orders.py` | Broker-managed protective stops + take-profits. |
| `trader.py` | Per-position exit logic; trailing-stop reconciliation. Exit-fired SELL/COVER rows write `status='pending_fill'` until broker confirms (deferred to `_task_update_fills`). |
| `journal.py` | `trades` + journal-table CRUD + schema migrations. Status values: `open` (entry), `pending_fill` (close awaiting broker confirmation), `closed` (broker-confirmed close), `canceled` (entry never filled / phantom undo). FIFO `get_virtual_positions` keys each position by OCC symbol when set, otherwise by stock symbol, and applies the ×100 contract multiplier on dollar fields (`unrealized_pl`, `market_value`) for option positions. Includes everything except `status='canceled'`. |

### 3c. Strategy engines
| Module | Purpose |
|---|---|
| `strategies/*.py` | 25 plugin-style strategies, each a pure function. |
| `strategy_micro.py`, `strategy_small.py`, `strategy_mid.py`, `strategy_large.py`, `strategy_crypto.py` | Legacy market-type-specific strategy modules. |
| `fallback_strategy.py` | Hosts the "core four" (momentum_breakout, volume_spike, mean_reversion, gap_and_go) referenced by the `strategy_*` profile-toggle columns. |
| `strategy_router.py` | Dispatches to the right per-market strategy module. |
| `strategy_generator.py` | Synthesizes new strategy variants. |
| `multi_strategy.py` | Strategy orchestration: run all strategies on a symbol, aggregate votes. |
| `strategy_proposer.py` | Commissions auto-generated strategy variants. |
| `strategy_lifecycle.py` | Per-strategy enable/disable + probationary period. |
| `strategy_capital_allocator.py` | Per-strategy weight: `sharpe × (1 + win_rate)`. |
| `alpha_decay.py` | Rolling-Sharpe alpha decay tracker. |

### 3d. Options program
| Module | Purpose |
|---|---|
| `options_oracle.py` | Per-symbol IV rank, term structure, skew, GEX, max pain, implied move. |
| `options_chain_alpaca.py` | Replaces yfinance for options chain fetches. |
| `options_trader.py` | Single-leg option order execution. `submit_option_order` accepts `position_intent` kwarg (defaults to `*_to_open` by side); `execute_option_strategy` has a journal-level dup guard (refuses re-submit when an open row matches the OCC). |
| `options_multileg.py` | 11 strategy primitives + atomic multi-leg execution. Both combo and sequential paths pass `position_intent` (`_INTENT_OPEN` for opening, `_INTENT_CLOSE` for rollback) — Alpaca async-cancels short option opens that arrive without intent. Sequential rollback unwinds previously-submitted legs with close intent so it isn't treated as a new position. Journal-level dup guard refuses re-submit when any leg's OCC matches an open row. |
| `options_strategy_advisor.py` | Read-side advisor (covered call / protective put recommendations). |
| `options_vol_regime.py` | Vol regime classifier (premium_rich / cheap, skew steep_put / call, term contango / backwardation). |
| `options_earnings_plays.py` | Pre-earnings IV crush capture (iron condor) / long straddle. |
| `options_roll_manager.py` | Auto-close credit positions at ≥80% max profit; recommend rolls. |
| `options_lifecycle.py` | Expiry / assignment detection. |
| `options_wheel.py` | CSP → assigned → CC state machine. |
| `options_delta_hedger.py` | Stock-side rebalance for long-vol option positions. |
| `options_greeks_aggregator.py` | Book-level net Greeks. |
| `options_backtester.py` | Synthetic options backtester (Phase H). |

### 3e. Risk management
| Module | Purpose |
|---|---|
| `crisis_detector.py` | Cross-asset crisis signals. |
| `crisis_state.py` | State machine + size multipliers. |
| `intraday_risk_monitor.py` | 4 intraday checks (drawdown, vol, sector, halts). |
| `portfolio_risk_model.py` | Barra-style 21-factor risk model. |
| `risk_stress_scenarios.py` | 7 historical scenario projections. |
| `long_vol_hedge.py` | Active SPY put tail hedge. |
| `portfolio_exposure.py` | Sector + factor + direction exposure tracking. |
| `portfolio_manager.py` | Drawdown checks; portfolio-level state aggregation. |
| `risk_parity.py` | Per-position vol-budget sizing. |
| `kelly_sizing.py` | Per-direction fractional Kelly. |
| `drawdown_scaling.py` | Continuous size modifier on drawdown. |
| `mfe_capture.py` | MFE-vs-realized P&L analysis. |
| `correlation.py` | Rolling correlation between positions. |
| `cost_guard.py` | Per-user daily AI-spend ceiling. Two enforcement paths: (1) hard block at `ai_providers.call_ai` / `call_ai_structured` — every AI call is gated against a worst-case cost estimate before the provider is invoked; raises `CostCapExceeded` when over budget. (2) advisory at three self-tuner sites (`self_tuning.py`) — over-budget tuner actions surface as `Recommendation: cost-gated` strings instead of auto-applying. Ceiling = user's `daily_cost_ceiling_usd` override or auto-computed `max($5, trailing_7d_avg × 1.5)`. Cap fires write `activity_type='cost_cap_blocked'` rows; dashboard renders a banner when `headroom_usd ≤ $0.05`. |
| `task_watchdog.py` | Run-completion tracking + stall detection. `track_run(db, name)` context manager records start / end / duration / status of every long-running task. `mark_orphaned_at_startup(db)` — called by the scheduler on boot — bulk-converts every still-`running` row to `orphaned_restart` so deploys don't generate false-positive stall alerts. `check_stalled_runs(db, mins)` finds rows running > N minutes with no completion and marks them `stalled` (one alert per row, not per watchdog tick). `diagnose_stalled_run(db, name, started_at, mins)` returns an evidence-based diagnosis built from `ai_cost_ledger` / `activity_log` / `ai_predictions` rows — never fabricates a culprit when no evidence is present. |
| `short_borrow.py` | Short-borrow rate lookup + accrual on cover. |
| `kill_switch.py` | Master kill switch — single boolean blocks every new entry across every profile. Auto-activates on book day-P&L floor breach (default −8%). Manual via `/api/kill-switch` + dashboard banner. State persists in master DB. |
| `book_concentration.py` | Cross-profile single-name concentration cap. Sums $ exposure to a symbol across every profile DB; rejects entries that would push aggregate share past `max_book_exposure_pct_per_symbol` (default 25%). |
| `single_trade_gate.py` | Pre-trade gate that rejects trades whose $ value exceeds 5× the profile's recent average position. Catches qty-arithmetic bugs the dollar check misses when price input is wrong. |
| `stop_coverage.py` | Per-cycle: alerts when fewer than 80% of open longs have a broker protective stop. Optional auto-kill via `auto_kill_on_stop_coverage`. |
| `position_runaway.py` | Per-cycle sentinel for duplicate-submit bugs (>1 open buy / symbol) and excessive single-trade qty (>5× profile-recent median). |
| `ai_consistency_floor.py` | Per-cycle: recent-100 directional win rate per profile. <30% for 5 consecutive cycles → alert / optional auto-kill. Captures "model is broken" before "book is bleeding". |
| `broker_health.py` | Per-process broker health tracker. Three consecutive Alpaca failures → `BROKER_DISCONNECTED` state; pre-trade gate refuses new entries. Auto-clears on next success. |
| `provider_circuit.py` | Per-provider circuit breaker for AI provider calls. 3 consecutive 5xx/timeout failures OPEN the circuit for 5min (exponential backoff to 30min). `ai_providers.call_ai` auto-routes to fallback (OpenAI / Google) when primary's circuit is open. **Anthropic fallback is suppressed by default** (`_build_fallback_chain`) — profiles configured for Gemini/OpenAI never silently route to paid Claude. Opt in via `AI_ALLOW_ANTHROPIC_FALLBACK=1` env var. |
| `db_integrity.py` | Startup `PRAGMA quick_check` on every DB. Filters NULL-in-NOT-NULL constraint violations as non-fatal (those are pre-existing rows after ALTER TABLE, not file corruption). Halts scheduler on real corruption. `restore_from_backup()` helper for nightly-snapshot recovery. |

### 3f. Data sources
| Module | Purpose |
|---|---|
| `market_data.py` | Alpaca historical bars + cache. |
| `alternative_data.py` | Single canonical entry point for all 34 alt-data signals — `get_all_alternative_data(symbol)` returns the unified dict. See `docs/16_ALT_DATA_CANDIDATES.md` for the full inventory. Sub-modules: `sec_8k_broad.py`, `sec_13dg_activist.py`, `altdata_tier2_corporate.py`, `altdata_tier2_macro.py`, `altdata_tier3.py`. Macro signals reach the prompt via `alternative_data._get_cached_macro()` (same canonical bucket, not a separate pipeline). |
| `news_sentiment.py` | Per-stock news from Alpaca. |
| `social_sentiment.py` | Reddit ticker mentions via PRAW. |
| `political_sentiment.py` | Macro political context (MAGA mode). |
| `factor_data.py` | yfinance fundamentals + Ken French factor returns. |
| `sector_classifier.py` | yfinance sector lookup (only allowed yfinance use). |
| `macro_data.py` | FRED indicators, yield curve, ETF flows, sector rotation. |
| `macro_event_tracker.py` | FOMC / CPI / NFP calendar (hand-curated). |
| `pdufa_scraper.py` | BiopharmCatalyst PDUFA scrape. |
| `market_regime.py` | Regime classification (bull/bear/sideways/volatile). |
| `sec_filings.py` | SEC EDGAR 10-K / 10-Q / 8-K analysis + insider Form 4. |
| `earnings_calendar.py` | Earnings date lookup with cache. |
| `screener.py` | Universe scanning + sector rotation. |
| `historical_universe_augment.py` | Daily diff of Alpaca's active asset list (survivorship-bias correction). |
| `segments.py` | Live universe definitions per market type. Note: 2026-05-19 — within stock markets (largecap/midcap/small/micro) the strategy mix is identical; the label is for profile organization + the stock-vs-crypto data-source split, not behavior gating. See `strategies/__init__.py:_strategy_applies_to_market`. |
| `segments_historical.py` | Frozen baseline for backtest. |

### 3g. Self-tuning + learning
| Module | Purpose |
|---|---|
| `self_tuning.py` | The 12-layer self-tuner (the largest single module). |
| `signal_weights.py` | Layer 2 weighted signal intensity. |
| `regime_overrides.py` | Layer 3 per-regime parameter overrides. |
| `tod_overrides.py` | Layer 4 per-time-of-day overrides. |
| `symbol_overrides.py` | Layer 7 per-symbol overrides. |
| `prompt_layout.py` | Layer 6 per-section verbosity. |
| `insight_propagation.py` | Layer 5 cross-profile insight transfer. |
| `post_mortem.py` | Losing-week clustering + learned patterns. |
| `capital_allocator.py` | Layer 9 auto capital allocation. |

### 3h. Database & migrations
| Module | Purpose |
|---|---|
| `models.py` | Master DB (`quantopsai.db`) schema + ORM-equivalent functions. |
| `journal.py` | Per-profile DB schema + functions. |
| `migrate.py` | One-shot migrations utility. |
| `migrate_activity_log_format.py` | Specific migration script. |
| `recover_cycle_data.py` | Recovery utility for incomplete cycles. |
| `backup_db.py` | DB backup utility. |

### 3i. Backtesting
| Module | Purpose |
|---|---|
| `rigorous_backtest.py` | 10-gate gauntlet. |
| `backtester.py` | Equity strategy walk-forward backtester. |
| `backtest_worker.py` | Async job runner. |
| `mc_backtest.py` | Monte Carlo backtest with slippage bootstrap. |
| `slippage_model.py` | 4-component slippage cost model. |

### 3j. Event-driven layer
| Module | Purpose |
|---|---|
| `event_bus.py` | Pub/sub for system events. |
| `event_detectors.py` | Pre-cycle event detection (price shocks, halts, SEC alerts). |
| `event_handlers.py` | Per-event-type handlers. |

### 3k. Web app helpers
| Module | Purpose |
|---|---|
| `dashboard.py` | Dashboard data assembly. |
| `display_names.py` | snake_case → human label registry + Jinja filters (`humanize`, `display_name`, `format_occ`, `action_label`, `friendly_time`, `friendly_date`). `action_label(side, signal_type, is_option)` derives Long Open / Long Close / Short Open / Short Cover for stocks and Buy to Open / Sell to Open Leg / Sell to Close / Buy to Close for options. API endpoints that return user-facing text MUST call `humanize()` server-side — caught by `tests/test_no_allcaps_snake_case_in_api.py` regex guardrail. |
| `param_bounds.py` | Min/max bounds for every tunable parameter. |
| `notifications.py` | Alert dispatching. |
| `metrics.py` | Performance metric computation. |
| `scan_status.py` | Per-profile scan-cycle health/timeliness. |

### 3m. Reporting & monitoring
| Module | Purpose |
|---|---|
| `ai_weekly_summary.py` | Sunday weekly digest (HTML + email payload). |
| `task_watchdog.py` | Per-task run tracker + stuck-task self-heal helper used by `_task_run_watchdog`. |
| `scaling_projection.py` | Capacity / capital-graduation modeling helper used by the scaling section of the dashboard. |
| `run_backtest_validation.py`, `run_phase2_validations.py` | One-off validation scripts; not part of the running services. |

### 3l. UserContext
| Module | Purpose |
|---|---|
| `user_context.py` | The dataclass passed everywhere. Built per-profile per-cycle from the schema row. |
| `config.py` | Global config (DB path, env vars). |

## 4. Request flow: a complete trade cycle

When `_task_scan_and_trade(ctx)` fires for one profile:

1. **Universe load** — `segments.get_universe(ctx)` returns the symbol list per market type.
2. **Pre-filter** — blacklist (`recently_exited_symbols`), earnings (`earnings_calendar`), drawdown gate.
3. **Strategy votes** — each strategy in `strategies/` runs on each symbol; emits a vote.
4. **Rank** — `multi_strategy.rank_candidates` computes composite score; takes top 30 with reserved long/short slots.
5. **Meta-pregate** — `meta_model.predict_probability` per candidate; drop if < `meta_pregate_threshold`.
6. **Ensemble** — `ensemble.run_ensemble(survivors)` runs the 8 LLM specialists in parallel (5 stock-pipeline always; 3 options-pipeline when candidate is an options strategy). Vetoes from `risk_assessor`, `adversarial_reviewer`, or `option_spread_risk` drop the candidate. Each specialist receives a per-role subset of the alt-data dict via `format_candidate_for_specialist`. Six of the eight specialists (re-scoped 2026-05-18, Phase 3 of docs/17) receive the candidate render with a compact `RULES: [V]name [C]name ...` suffix carrying the deterministic-panel verdicts so their prompts synthesize from the rule layer rather than re-derive facts; `gamma_pin_specialist` and `option_spread_risk` are intentionally left alone because they cover unique territory the rule library doesn't subsume.
7. **Build candidate context** — `_build_candidates_data` enriches each remaining candidate with: technicals, alt_data dict, options_oracle, factor exposures, track_record, last_prediction, slippage_estimate, borrow_rate (shorts), SEC alerts. Also computes the deterministic rule panel (`deterministic_specialists.run_panel`) and the RAG case-file block (`case_file_rag.build_prompt_block`) for injection into the apex prompt.
8. **Build market context** — `_build_market_context` returns regime, VIX, SPY trend, sector rotation, crisis_context, macro_context, political_context, portfolio_risk_summary, portfolio_risk_scenarios, long_vol_hedge_block, macro_event_block.
9. **Build portfolio state** — equity, cash, positions, exposure breakdown, book beta, Kelly recommendations, drawdown scale, risk-budget, MFE capture, sector concentration warnings.
10. **AI batch call** — `ai_analyst.ai_select_trades(candidates_data, portfolio_state, market_ctx, ctx)` makes one LLM call. Returns 0-3 trade proposals with reasoning.
11. **Validate** — `_validate_ai_trades` runs the gate stack: balance gate, asymmetric short cap, HTB penalty, neutrality gate, crisis gate, intraday halt gate, cost guard, wash-trade guard.
12. **Re-weight by meta-model** — each accepted trade gets `meta_prob`, `online_meta_prob`, `meta_divergence` attached. Confidence adjusted via `adjust_confidence`. Below `SUPPRESSION_THRESHOLD` → drop.
13. **Apply strategy capital allocator** — per-strategy weight scales `size_pct`.
14. **Submit** — `_execute_buy` / `_execute_sell` / `execute_option_strategy` / pair_trade / multileg_open. Captures predicted_slippage_bps + adv_at_decision at submit.
15. **Place protective stops** — `bracket_orders.ensure_protective_stops`.
16. **Journal** — `log_trade` writes the trade row; `track_ai_prediction` writes the prediction row with full feature snapshot.
17. **Specialist outcome backfill** — `record_outcomes_for_prediction` writes one row per specialist per prediction.

## 5. Resolution flow

`_task_resolve_predictions(ctx)` per cycle:

1. Pull all `pending` predictions.
2. For each, fetch current price; check if take_profit / stop_loss / time_stop reached.
3. If resolved, update row with `actual_outcome` (`win` / `loss` / `neutral`), `actual_return_pct`, `resolved_at`, `resolution_price`, `days_held`.
4. Call `specialist_calibration.update_outcomes_on_resolve` to backfill specialist outcomes.
5. Call `online_meta_model.update_online_model(profile_id, features, outcome_label)` — single-row partial_fit on the SGD model.

## 6. The virtual account architecture

The platform virtualizes 10+ profiles into 3 Alpaca paper accounts via the following mechanism.

### 6a. Mapping

`alpaca_accounts` (master DB) table holds 1-3 paper account configurations per user. Each `trading_profiles.alpaca_account_id` is a foreign key to the actual paper account.

So profiles 1, 4, 7 might all share Alpaca Account A. Profiles 2, 5, 8 share Account B. Profiles 3, 6 share Account C. Etc.

### 6b. Per-profile state

Each profile has its own SQLite database (`quantopsai_profile_<id>.db`) holding:

- Its own `trades` table.
- Its own `ai_predictions` journal.
- Its own `daily_snapshots`.
- Its own `meta_model_<id>.pkl` and `online_meta_model_p<id>.pkl`.
- Its own learned patterns, post-mortems, calibrators.
- Its own `initial_capital` figure.

### 6c. Virtual P&L

When a profile submits a trade, it goes to the shared Alpaca account. The fill comes back. The fill is journaled to that profile's `trades` table.

Per-profile virtual position book is computed from `journal.get_virtual_positions(db_path)` — FIFO accounting over the trades table. This returns shape-identical output to `client.get_positions()` so downstream code (trade_pipeline, views, performance reporting) works without branching.

Per-profile virtual equity: `initial_capital + sum(realized_pnl) + sum(unrealized_pnl from current price)`. Cash: `initial_capital - sum(open_position_market_value)`.

### 6d. Cross-account reconciliation

`_task_cross_account_reconcile(ctx)` runs daily:

1. For each `alpaca_account_id`, sum the virtual positions across all profiles mapped to it.
2. Pull the actual broker positions.
3. Verify: `sum(virtual) ≈ actual` per symbol.
4. Drift triggers a warning + diff log.

### 6e. Why this is novel infrastructure

- 10+ strategies in parallel without 10 brokerage accounts.
- Each profile has its own meta-model, slippage K, learned patterns, alpha decay tracker.
- Alt-data fetches are cached at the master-DB layer, so 10 profiles asking for AAPL's insider data make one upstream call.
- Per-profile P&L attribution is exact (FIFO from the trades table).
- Trades for different profiles don't interfere (the validation gates check the originating profile's exposure, not the broker's combined book).

The same architecture, when extended to live trading, becomes the foundation for running multiple isolated capital pools with isolated risk budgets and independent audit trails.

## 7. Multi-scheduler internals

`multi_scheduler.run_scheduler()` is the main loop. Architecture:

- One process; multiple profiles processed sequentially per cycle.
- Cycle cadence: 5 minutes during market hours (configurable per profile via `schedule_type`).
- Each per-profile cycle invokes `run_segment_cycle(ctx, run_scan, run_exits, run_predictions, run_snapshot, run_summary)`.
- Inside `run_segment_cycle`, individual `run_task(label, fn, db_path)` calls invoke each `_task_*` with full error isolation — one task failing doesn't break the cycle.
- `_task_run_watchdog` self-heals stuck tasks (records start/end timestamps, kills tasks running longer than the cycle).

Once-per-day tasks are gated on a master-DB marker table per task. The first profile to land on a given UTC day fires the daily task; subsequent profiles see the marker and skip.

Schedules:

- `_task_scan_and_trade` + `_task_check_exits`: every cycle.
- `_task_resolve_predictions`: every cycle.
- `_task_daily_snapshot`: once / day per profile.
- `_task_self_tune`, `_task_retrain_meta_model`, `_task_calibrate_specialists`, `_task_db_backup`, etc: once / day, marker-protected.
- `_task_post_mortem`: weekly (Sunday).
- `_task_auto_strategy_generation`: weekly.
- `_task_universe_audit`, `_task_app_store_snapshot`, `_task_pdufa_scrape`: once / day, marker-protected.

## 8. Database schemas

The master DB (`quantopsai.db`) and per-profile DBs are SQLite. Schema definitions live in:

- `models.py` — master DB.
- `journal.py` — per-profile DB.

Migration pattern: each `init_*_db()` function:

1. Issues `CREATE TABLE IF NOT EXISTS` for every table.
2. Iterates an `_EXPECTED_COLUMNS` dict; issues `ALTER TABLE ... ADD COLUMN` for each missing column.
3. Catches `sqlite3.OperationalError` (column already exists).

Adding a new column:

1. Add to the `CREATE TABLE` block in the relevant init function.
2. Add a `("table_name", "column_name", "TYPE NOT NULL DEFAULT ...")` entry to the migrations list.
3. Update `update_trading_profile`'s allowlist.
4. Update form parser + settings UI + `MANUAL_PARAMETERS` allowlist (if user-editable).
5. Update `UserContext` + `build_user_context_from_profile` (if consumed by code).

The complete schema is in `docs/05_DATA_DICTIONARY.md`.

## 9. Caching layers

Multiple TTL-based caches across the system. Source of TTLs: `alternative_data._CACHE_TTL` and module-specific defaults.

| Cache | TTL | Backing |
|---|---|---|
| `market_data.get_bars` | 5 min in-process | dict + lock |
| `alt_data_cache` | varies (1-30 days) | master DB SQLite table |
| `shared_ai_cache` | 1 hour | master DB SQLite (Lever 1) |
| `app_store_history` | persistent (90+ days) | master DB |
| `slippage_calibration` | 7 days | disk file (`.cache/slippage_calibration/`) |
| `french_factors` | 7 days | disk file (`.cache/french_factors/`) |
| `options_oracle` | 30 min | in-process |
| `factor_data.get_realized_vol` | 7 days | disk |
| `crypto_chain` | n/a (no caching) | direct fetch |

`alt_data_cache` is the workhorse: every alternative_data helper writes results here keyed by `<helper_name>_<symbol>` so 10 profiles asking for AAPL insider data make one upstream call.

## 10. Test suite

Source: `tests/`. 302 test files covering:

- **Per-module unit tests** (~170 files): one per major module.
- **Integration tests**: `test_today_integration.py` (scheduler wiring), `test_pipeline.py` (end-to-end cycle).
- **Guardrail tests** (the architectural invariants, listed in `docs/10_METHODOLOGY.md` §3).
- **Regression tests** for specific incidents documented in CHANGELOG.

Run: `venv/bin/python -m pytest tests/ -q`.

Test discipline:

- 3,963 tests passing (1 skipped — an `_EMPTY_FIRE_EXEMPT` rule whose purpose is to fire on minimal context).
- pytest-randomly for order-independence.
- 30s default timeout per test.
- Mocked external APIs (no network calls).

## 11. Deployment

Single droplet at `67.205.155.63`. Layout:

- `/opt/quantopsai/` — code (rsynced via `sync.sh`).
- `/opt/quantopsai/venv/` — Python 3.9 venv with all deps.
- `/opt/quantopsai/quantopsai.db` — master DB.
- `/opt/quantopsai/quantopsai_profile_<id>.db` — per-profile DBs.
- `/opt/quantopsai/.cache/` — disk caches (slippage K, Ken French CSVs).
- `/opt/quantopsai/altdata/` — bundled alt-data scrapers (`congresstrades`, `stocktwits`, `biotechevents`, `edgar13f`). Merged into the Quantops repo on 2026-05-04 (commit `086aed2`); previously lived in 4 separate private GitHub repos rsync'd to `/opt/quantopsai-altdata/`. Each scraper writes to `altdata/<project>/data/<project>.db`. Daily refresh via `altdata/run-altdata-daily.sh` (cron 06:00 UTC).

`sync.sh`:

1. rsync exclude `__pycache__`, `.cache/`, `*.db`.
2. `git fetch && git reset --hard origin/main` on prod (keeps prod git in sync).
3. systemd reload of `quantopsai-web` + `quantopsai-scheduler` when scheduler is idle.

## 12. AI provider integration

`ai_providers.py`:

- `call_ai(prompt, provider, model, api_key, ...)` is the single entry point. Routes to:
  - Anthropic Claude via `anthropic` SDK.
  - OpenAI GPT via `openai` SDK.
  - Google Gemini via `google.generativeai`.
- Cost accounting wrapped: every successful call writes to `ai_cost_ledger` with provider + model + token counts + USD.
- Defensive parsing: malformed JSON responses logged but never propagate as exceptions.

`ai_pricing.py` carries per-model token prices. Updated when providers publish new pricing.

## 13. Web app

Flask + Jinja2. Templates in `templates/`. Major pages:

- `/dashboard` — multi-profile portfolio overview.
- `/ai` — AI Intelligence dashboard (4 tabs: Brain, Strategy, Awareness, Operations).
- `/performance` — per-profile performance breakdown.
- `/trades` — trade ledger.
- `/settings` — per-profile settings.

Major API endpoints in `views.py` (~50 routes). Documented inline; selected endpoints in `docs/06_USER_GUIDE.md`.

## 14. Adding a new module

Follow the conventions in `docs/10_METHODOLOGY.md` §4 and `docs/11_INTEGRATION_GUIDE.md`.

## 15. Data-source policy and yfinance grandfathered uses

Data-source priority (enforced by the `feedback_alpaca_first_data` memory rule):

1. **Alpaca first** — use Alpaca for bars (any timeframe), quotes, trades, snapshots, options chains, news (`/v1beta1/news`), corporate actions, positions, orders, account state. Credentials resolved via `market_data._resolve_alpaca_credentials()` (env vars first, then any working row from the `alpaca_accounts` master DB table — self-healing for credential rotation).
2. **Custom altdata second** — `altdata/<name>/` modules. Currently built: `congresstrades` (House STOCK Act), `edgar13f` (institutional 13F-HR), `biotechevents` (clinical trials + PDUFA stub), `stocktwits` (retail sentiment).
3. **yfinance last** — grandfathered for data types neither Alpaca nor a custom altdata module covers. Each yfinance use must live in a single dedicated module — strategies must never `import yfinance` directly.

### Current yfinance dependencies (grandfathered exceptions)

| Module | Function | Data | Why grandfathered |
|---|---|---|---|
| `earnings_calendar.py` | `check_earnings()`, `days_since_last_earnings()` | Future + past earnings announcement dates | Alpaca has no earnings calendar endpoint at all |
| `analyst_data.py` | `recommendation_shift()` | Aggregate analyst rating distributions per period | Alpaca has no analyst recommendations API; Polygon/Finnhub paid tiers exist but not free |
| `sector_classifier.py` | `get_sector()` | GICS sector classification | Alpaca asset endpoint has no sector field |
| `factor_data.py` | `get_book_to_market()` | Book value, market cap, shares outstanding | Alpaca doesn't expose fundamentals |
| `macro_data.py` | `get_cboe_skew()` | CBOE ^SKEW index value | Alpaca doesn't serve equity volatility indices |
| `alternative_data.get_insider_activity()` | Recent insider buys/sells per symbol | Form 4 disclosures | No Alpaca endpoint. **Phase 6 candidate** — replace with `altdata/edgar_form4` module (SEC EDGAR Form 4 XML, free public source). |
| `alternative_data.get_short_interest()` | Short % of float, short ratio | FINRA short interest reports | No Alpaca endpoint. **Phase 6 candidate** — replace with `altdata/finra_short` module. |
| `alternative_data.get_fundamentals()` | P/E, dividends, ownership %, institution % | Fundamentals from Yahoo Finance | No equivalent |
| `alternative_data.get_options_unusual()` | Options unusual activity (chains) | yfinance option chains | **Phase 6 quick-win** — Alpaca now has options chains (already used by `options_chain_alpaca.py`); migrate `get_options_unusual` to use the existing Alpaca wrapper. |
| `alternative_data.get_analyst_estimates()` | EPS / revenue forecasts | yfinance analyst estimates | No equivalent free source |
| `alternative_data.get_earnings_surprise()` | Beat/miss history per quarter | yfinance earnings history | No equivalent free source |
| `alternative_data.get_patent_activity()` | Company name lookup for USPTO query | yfinance ticker info | Trivial name lookup; not worth migrating |
| `market_data.py` | `_fetch_via_yfinance()` (FALLBACK) | OHLCV bars | Primary is Alpaca; yfinance only fires when (a) crypto symbol (`/` in name) or (b) Alpaca returns empty/error. Healthy. |

### What's been migrated AWAY from yfinance

- Daily bars + intraday 5-min bars → Alpaca `/v2/stocks/{sym}/bars` (primary; yfinance fallback retained for crypto + outage resilience)
- Latest trade snapshot → Alpaca `/v2/stocks/{sym}/latest/trade`
- Options chain fetch → Alpaca options-chain endpoint via `options_chain_alpaca.py`
- News → Alpaca `/v1beta1/news` via `news_sentiment.py`

## See also

- `docs/05_DATA_DICTIONARY.md` — schema reference.
- `docs/07_OPERATIONS.md` — deploy, monitoring, incident response.
- `docs/10_METHODOLOGY.md` — engineering conventions.
- `docs/11_INTEGRATION_GUIDE.md` — extending the system.
