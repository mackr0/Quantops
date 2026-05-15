# 08 — Risk Controls

**Audience:** risk officers, compliance, anyone auditing what stops the system from blowing up.
**Purpose:** enumerate every gate, every kill switch, every safety override. After reading this, an auditor can identify exactly what mechanisms protect capital and reproduce the conditions under which each fires.
**Last updated:** 2026-05-03.

## Operating principles

QuantOpsAI's risk philosophy:

1. **Multiple independent layers.** Six independent risk controls. Any one is sufficient. They are not cumulative — each is a complete safety net by itself.
2. **Hard gates, not soft suggestions.** Where the AI's discretion is dangerous, the system uses hard `if` blocks at the validation layer, not advisory text in the prompt.
3. **Symmetric where appropriate.** Trades that improve risk metrics always pass; trades that worsen them by a material delta are blocked.
4. **Audit trail.** Every gate logs why a trade was rejected. Visible on the AI dashboard's vetoed-trades panel.
5. **Honest limits documented.** Where a control has known coverage gaps, those gaps are explicit (see §10 below).

## 1. Crisis state monitor

**Module:** `crisis_detector.py` + `crisis_state.py`
**Cadence:** every cycle.

Monitors cross-asset distress signals. State machine: `normal → elevated → crisis → severe`.

Signals (all must be present in some combination):

- VIX absolute level + term-structure inversion (front-month > back-month).
- SPY / TLT / GLD / UUP correlation spikes (pairwise rolling 20d corr breaking historical band).
- Bond/stock divergence (TLT down + SPY down).
- Gold safe-haven rally (GLD up while SPY down by ≥ 2σ).
- Credit spread widening (HYG / LQD ratio).
- Cluster of recent price shocks across held positions.

State effects:

| Level | Position size | New long entries | New short entries |
|---|---|---|---|
| `normal` | 1.00× | Allowed | Allowed |
| `elevated` | 0.85× → 0.65× (per signal severity) | Allowed | Allowed |
| `crisis` | 0.0× | **Blocked** | Allowed |
| `severe` | 0.0× | **Blocked** | Allowed (but pause-all considered) |

State transitions logged to `crisis_state_history`. Surfaced in AI prompt as `*** CRISIS STATE: ELEVATED (size x0.65) ***` so the AI sees the level and reasons accordingly.

## 2. Intraday risk monitor

**Module:** `intraday_risk_monitor.py`
**Cadence:** every cycle (gated by `enable_intraday_risk_halt`, default ON).

Four checks. Any one firing → write an `intraday_risk_halt` row → trade pipeline blocks new entries.

| Check | Condition | Severity (warning) | Severity (critical) |
|---|---|---|---|
| `drawdown_acceleration` | today's high-to-current drawdown / 7d-avg drawdown ≥ 2× | At 2-3× | At ≥3× |
| `vol_spike` | last-hour SPY realized vol / 20d hourly avg ≥ 3× | At 3-5× | At ≥5× |
| `sector_concentration_swing` | Largest sector intraday move ≥ 3% absolute | At 3-5% | At ≥5% |
| `held_position_halts` | Number of held positions trading-halted | 1-2 names | ≥3 names |

Alert action mapping:

| Severity | Suggested action |
|---|---|
| `warning` | `block_new_entries` |
| `critical` | `pause_all` |

Aggregate action across multiple alerts: `pause_all > block_new_entries > monitor > pass` (most-restrictive wins).

Auto-clear: 60 minutes after the last alert. The trade pipeline reads `get_active_risk_halt(db_path)` per cycle and refuses new entries when active.

## 3. Per-trade stops

**Module:** `bracket_orders.py`
**Cadence:** every entry.

Every entry receives a broker-managed protective order at submission time:

- **Trailing stop** (when `use_trailing_stops=1`, default): Alpaca `type='trailing_stop'` with `trail_percent` derived from `stop_loss_pct`, clamped [2%, 10%].
- **Static stop loss** (otherwise): Alpaca `type='stop'` at `entry_price × (1 − stop_loss_pct)`.

Exactly one protective order per position (Alpaca treats each open sell-side order as a qty reservation; placing stop+TP+trailing on the same shares triggers qty conflicts).

Take-profit detection runs in the polling fallback (cycle-based). Polling defers to the broker when `bracket_orders.has_active_broker_trailing(api, db_path, symbol)` confirms an active broker-side trailing — without the defer, polling would beat the broker to a worse fill on every cycle.

When `use_conviction_tp_override=1` and a position hits its fixed take-profit, the system can SKIP the fixed TP and let the trailing stop manage the exit — but ONLY when ALL of:

- AI confidence ≥ `conviction_tp_min_confidence` (default 70).
- ADX ≥ `conviction_tp_min_adx` (default 25), confirming trend strength.
- Price is making new highs.

The conviction override is OFF by default. Enabling it accepts more upside but requires more discretion.

## 3.5 Doomsday gates (added 2026-05-04 / 2026-05-05)

A defense-in-depth layer above the per-trade and validation gates. These exist for catastrophic-failure scenarios that the existing risk controls don't cover individually. **Each gate is independent and any one of them is sufficient to stop the bleed.**

| Gate | Trigger | Action | Module |
|---|---|---|---|
| **Master kill switch** | Manual flip on dashboard banner OR auto-flip on book-wide day-P&L floor breach | Returns `KILL_SWITCH` for every new entry across every profile until manually deactivated | `kill_switch.py` |
| **Book daily-loss floor** | Cumulative book day-of P&L < −8% of opening equity | Auto-flips master kill switch with reason `auto: book day P&L X% breached floor −8%` | `kill_switch.check_and_activate_on_loss_floor` |
| **Cross-profile concentration cap** | Aggregate $ exposure to a single symbol > 25% of book | Returns `BOOK_CONCENTRATION_CAP` for the proposed entry | `book_concentration.py` |
| **Catastrophic single-trade gate** | Proposed trade $ value > 5× profile's recent avg position size | Returns `CATASTROPHIC_SINGLE_TRADE` | `single_trade_gate.py` |
| **Broker disconnect detection** | 3 consecutive Alpaca calls fail | Returns `BROKER_DISCONNECTED` for new entries until next success | `broker_health.py` |
| **AI provider failover** | 3 consecutive 5xx/timeout from active AI provider | Auto-routes to OpenAI / Google fallback (when configured) | `provider_circuit.py` |
| **Stop-order coverage alarm** | <80% of open longs have a broker protective stop | Logs warning + naked symbols; optional auto-kill via `auto_kill_on_stop_coverage` | `stop_coverage.py` |
| **Position-runaway sentinel** | Duplicate open buys for same symbol OR fill qty > 5× profile median | Logs warning per occurrence (already-filled, alert only) | `position_runaway.py` |
| **AI consistency floor** | Recent-100 directional win rate < 30% for 5 consecutive cycles | Logs error; optional auto-kill via `auto_kill_on_consistency_floor` | `ai_consistency_floor.py` |
| **DB integrity check** | `PRAGMA quick_check` reports actual file-level corruption | Halts scheduler on startup; sends notification (deduped 1h); `restore_from_backup()` is one-command | `db_integrity.py` |

**Pre-trade gate order in `trade_pipeline.run_evaluate_buy/sell/short`** (highest priority first):
1. Broker disconnect → `BROKER_DISCONNECTED`
2. Master kill switch → `KILL_SWITCH`
3. Catastrophic single-trade → `CATASTROPHIC_SINGLE_TRADE`
4. Cross-profile concentration → `BOOK_CONCENTRATION_CAP`
5. Drawdown pause → `DRAWDOWN_PAUSE`
6. Per-trade portfolio constraints → existing checks

The existing crisis-state, intraday-risk, and validation gates run alongside / after these. The doomsday layer is fail-closed: when in doubt, refuse the entry.

**Notification dedup**: the email service (`notifications.send_email`) deduplicates identical subjects within a 1-hour rolling window per process. Prevents crash-loop spam (incident 2026-05-04: 599 identical "DB corruption detected" errors over 24h hit Resend daily quota before the underlying bug was fixed).

## 4. Validation gates (in `_validate_ai_trades`)

**Module:** `trade_pipeline._validate_ai_trades`
**Cadence:** every AI-proposed trade.

Each gate is a hard `if`-block. Failures log a reason and surface on the AI Awareness panel.

### 4a. Balance gate (long/short profiles)

When `target_short_pct > 0` and the book has drifted >25pp from target, block new entries on the over-weighted side.

Example: target 50% shorts, current 80% shorts. Long entry: PASS (improves balance). Short entry: BLOCK.

Symmetric — entries that improve balance always pass.

### 4b. Asymmetric short cap

Longs sized against `max_position_pct` (e.g. 10%). Shorts capped at `short_max_position_pct` (defaults to half — e.g. 5%) — asymmetric-risk convention since short positions have unbounded upside risk.

### 4c. HTB borrow penalty

Hard-to-borrow shorts have their cap halved again on top of the asymmetric one. So a 5% short cap becomes 2.5% for an HTB short. (Borrow rate ≥ 10%/yr eats real money on multi-day holds.)

### 4d. Market-neutrality enforcement

When `target_book_beta` is set, the gate computes the projected book beta if the trade went through (`portfolio_exposure.simulate_book_beta_with_entry`) and blocks the trade if:

```
|projected_beta - target_beta| - |current_beta - target_beta| > 0.5
```

Symmetric — entries that improve neutrality always pass; entries that worsen it by >0.5 are blocked. Skipped for SELL exits (closing a position can only improve, not worsen, neutrality on net).

### 4e. Crisis gate

(See §1.) `crisis` and `severe` levels block new long entries; `elevated` scales position sizes via `crisis_size_multiplier`.

### 4f. Intraday risk halt gate

(See §2.) When `get_active_risk_halt(db_path)` returns an active state, new entries blocked.

### 4g. Cost guard

When today's projected AI spend exceeds the daily ceiling, AI-cost-affecting actions (re-runs, model upgrades) are deferred. Trades that DON'T require additional AI calls (e.g. exits) still fire.

### 4h. Wash-trade guard

Alpaca rejects `wash trade detected` errors. The trade pipeline catches these as recoverable SKIP, not ERROR, and writes a 30-day cooldown row to `recently_exited_symbols(trigger='wash_cooldown')`. Pre-filter loop unions wash-cooldown into the existing recent-exit set so wash-flagged symbols don't re-attempt every cycle.

### 4i. Cross-direction guard

Alpaca rejects "cannot open a long buy while a short sell order is open" (and the symmetric short-side case). Recoverable SKIP. The conflicting order resolves first, then the new entry is re-attempted on a subsequent cycle.

### 4j. Insufficient quantity / buying power

Recoverable SKIP, not ERROR. Typically resolves on the next cycle as other orders fill or cancel.

### 4k. Schedule window

`order_guard.check_can_submit(ctx, symbol, side)` blocks orders submitted outside the profile's `schedule_type` window.

### 4l. Duplicate prevention (broker-order level)

`order_guard` blocks entries when an open order for the same `(symbol, side)` already exists.

### 4m. Position dup guards (journal-level)

A separate layer above broker-level dup prevention: every entry executor pre-queries the per-profile journal for any open row matching the proposed position; if found, refuses with `action='SKIP'`. Without this, the AI re-proposing the same trade on consecutive cycles would re-fire indefinitely whenever one leg async-cancels at the broker.

| Executor | Match key | Module |
|---|---|---|
| `execute_multileg_strategy` | OCC symbol on any leg | `options_multileg.py` |
| `execute_option_strategy` | OCC symbol | `options_trader.py` |
| `execute_pair_trade` | symbol on either leg, `strategy='pair_trade'` | `stat_arb_pair_book.py` |

Coverage is enforced by `tests/test_broker_submit_invariants.py::test_every_entry_executor_has_dup_guard` — adding a new entry executor without a dup-guard marker fails CI.

### 4n. Option `position_intent` invariant

Every option `api.submit_order` call must include `position_intent` (`buy_to_open` / `sell_to_open` / `buy_to_close` / `sell_to_close`). Alpaca async-cancels short option opens that arrive without an intent declaration. Both `options_multileg.py` (combo + sequential paths) and `options_trader.submit_option_order` enforce this; sequential rollback uses close-intent so reversal legs aren't treated as new opens. Enforced by `tests/test_broker_submit_invariants.py::test_every_option_submit_passes_position_intent`.

### 4o. Multileg partial-fill rollback

`execute_multileg_strategy`'s sequential fallback submits each leg one by one when Alpaca's MLEG combo endpoint returns a transient 500. Submit-failure rollback is immediate (close any legs that submitted before the exception). **Fill-failure rollback** is the late-arriving counterpart: `_task_update_fills` (`multi_scheduler.py`) detects when a MULTILEG leg ends `expired` / `canceled` / `rejected` with `filled_qty=0`, finds its sibling legs by `(option_strategy, symbol, timestamp ±60s)`, and closes any that filled via opposite-side market order. The rollback close is logged as a new MULTILEG row carrying the original AI confidence + reasoning so the trade history reads as a coherent narrative; the closed sibling row flips to `status='closed'`. Without this, a half-filled spread (one leg filled, one expired) would become a permanent naked single-leg position the AI never decided to take. Enforced by `tests/test_multileg_partial_fill_rollback.py` (10 tests covering terminal-status pinning + pairing rules).

### 4p. Terminal-unfilled status pinning

`_task_update_fills` writes `status=<broker_status>` (`expired` / `canceled` / `rejected` / `done_for_day`) on journal rows when the broker confirms the order ended without filling. Without this, the row sits at `status='open'` with `price=NULL` indefinitely — the silent-failure shape that masked 3 orphan multileg legs on prod for 2 days (caught 2026-05-10). The trades-table macro renders these rows greyed/italicized with a status badge so the operator sees what happened at a glance.

### 4q. Combo-path 5xx retry (multileg prevention layer)

`_combo_submit_with_retry` (`options_multileg.py`) wraps the MLEG combo POST in a precise retry loop. Retries only on transient signals: `RuntimeError "Alpaca order rejected (5NN)"` and `requests.exceptions.{ConnectionError, Timeout, ChunkedEncodingError}`. 4xx HTTP and bare exceptions fail fast — they're either client errors that retry can't help or permanent config issues that would waste real time. Backoff `(0.5s, 1.5s)`, max 2 retries → ~2s worst-case before falling through to sequential. Cuts combo-path failures from ~30% to <5% on observed prod traffic, which means most multilegs stay on the atomic path and the partial-fill rollback (4o) becomes a rarely-exercised safety net rather than a regular cleanup. Enforced by `tests/test_combo_submit_retry.py` (6 tests covering retry policy + end-to-end fallthrough).

### 4r. Auto-exit confidence propagation

`journal.get_open_entry_metadata(db_path, symbol, occ_symbol=None)` returns the most-recent open BUY/SHORT entry's `ai_confidence` + `ai_reasoning`. Called by every auto-exit close path (`trader.py` protective close, `options_lifecycle.py` synthetic equity leg from exercise, `stat_arb_pair_book.py` pair exit) so close rows inherit the AI's original conviction. The trades-table macro renders inherited confidence as `78%` with a small `auto-exit` label underneath, distinguishing it from AI-decided sells while preserving the trade narrative end-to-end. Enforced by `tests/test_auto_exit_confidence_propagation.py` (7 tests — including no-silent-failure behavior on DB read errors).

## 5. Portfolio risk model

**Module:** `portfolio_risk_model.py`
**Cadence:** daily snapshot (gated by `enable_portfolio_risk_snapshot`, default ON).

Barra-style 21-factor risk model. Computes:

- Daily portfolio σ (from factor + idiosyncratic variance decomposition).
- Parametric 95% / 99% Value-at-Risk and Expected Shortfall.
- Monte Carlo VaR / ES (10,000 Cholesky-decomposed factor draws + idio draws).
- Top factor exposures (long/short β across 21 factors).
- Per-factor variance decomposition (sectors / styles / French / idiosyncratic).

Surfaced in AI prompt under MARKET CONTEXT > PORTFOLIO RISK, on AI Awareness panel, and persisted to `portfolio_risk_snapshots` (90-day retention).

**No hard gate currently — informational.** A future enhancement could add a `max_var_95_pct_of_book` hard cap that blocks new entries when projected post-trade VaR exceeds the threshold (tracked in OPEN_ITEMS). Today, the AI sees the readings and can choose to size down or skip; the hard mechanism is the long-vol hedge (§7).

## 6. Stress scenarios

**Module:** `risk_stress_scenarios.py`
**Cadence:** daily (alongside portfolio risk snapshot).

Seven historical windows replayed against current portfolio exposures:

| Scenario | Period | Severity | Notes |
|---|---|---|---|
| `1987_blackmonday` | 1987-10-12 to 1987-10-31 | catastrophic | French factors only (sector ETFs didn't exist). Quality flagged as "low." |
| `2000_dotcom` | 2000-04-01 to 2000-06-30 | severe | Sector ETFs partially available (XLK from 1998). Quality "medium." |
| `2008_lehman` | 2008-09-01 to 2008-10-31 | catastrophic | Full coverage (sector ETFs all live). Quality "high." |
| `2018_q4_selloff` | 2018-10-01 to 2018-12-24 | moderate | Full coverage. |
| `2020_covid` | 2020-02-19 to 2020-03-23 | severe | Full coverage. |
| `2022_rates` | 2022-01-01 to 2022-10-31 | severe | Full coverage but rate factor missing — under-reports. |
| `2023_svb` | 2023-03-08 to 2023-03-15 | moderate | Full coverage. |

Output per scenario: total_pnl_pct, total_pnl_dollars, worst_day_pct, worst_day_date, max_drawdown_pct, idio_band_pct, factors_available, factors_missing, approximation_quality.

Worst-3 surfaced in AI prompt. **No hard gate** — informational.

## 7. Long-vol portfolio hedge

**Module:** `long_vol_hedge.py`
**Cadence:** every cycle (gated by `enable_long_vol_hedge`, default OFF).

Active tail-risk insurance. When triggers fire, opens SPY puts (~5% OTM, ~45 DTE, premium budget 1% of book per active hedge).

Triggers (any one fires):

1. Drawdown ≥ `long_vol_hedge_drawdown_pct` (default 5%) from 30-day equity peak.
2. Crisis state ≥ "elevated".
3. 95% VaR ≥ `long_vol_hedge_var_pct` (default 3%) of book.

Management:

- Roll when DTE < 14 OR delta has decayed past −0.10.
- Close when ALL triggers clear simultaneously.

State persisted in `long_vol_hedges` table. Cost summary (90-day rolling premium paid + closed P&L + net cost) surfaced in AI prompt and on AI Awareness panel.

**Honest limits:**
- SPY puts hedge BETA, not idio. Concentrated single-name books still bleed even if SPY rallies.
- Premium bleeds in calm markets — meaningful drag on calm-market returns. Default OFF for that reason.

## 8. Strategy alpha decay monitor

**Module:** `alpha_decay.py`
**Cadence:** daily.

Tracks per-strategy rolling 30d Sharpe vs lifetime baseline.

- Auto-deprecate after 30+ consecutive days of degradation.
- Auto-restore after 14+ days of recovery.
- Manual restore via Restore button on AI page Strategy tab.

Deprecated strategies don't fire on the live engine but their historical contribution stays in the record.

## 9. Cost guard

**Module:** `cost_guard.py`
**Cadence:** every AI call (hard block) + every self-tuner action (advisory).

Daily AI-spend ceiling per user. Default: `max($5, trailing_7d_avg × 1.5)`. User can override with an explicit value on the settings page (`Maximum daily AI spend`); when set, the override stays fixed regardless of historical drift.

Two enforcement paths:

1. **Pipeline-wide hard block** (added 2026-05-15). Every AI call routed through `ai_providers.call_ai` / `call_ai_structured` is gated against a worst-case cost estimate (`len(prompt)//3` input tokens + `max_tokens` output, priced via `ai_pricing.estimate_cost_usd`) before the provider is invoked. Over-budget calls raise `CostCapExceeded`; the trade pipeline catches it distinctly (returns `{cost_capped: True}` from `ai_select_trades`) so the cycle skips the AI step without crashing or being mistaken for a broken-AI failure. Each cap fire writes an `activity_type='cost_cap_blocked'` row to `activity_log`. The dashboard renders a yellow banner when `headroom_usd ≤ $0.05`.

2. **Self-tuner advisory** (3 sites in `self_tuning.py`: strategy commissioning, parameter tuning, guardrail expansion). Over-budget tuner actions are surfaced as `Recommendation: cost-gated …` strings instead of auto-applying. This is the only legitimate use of the `Recommendation:` prefix allowed by the no-recommendation-only guardrail test.

Existing positions and broker stops are NOT affected by a cap fire — only new AI-driven entry decisions stop. Cap resets at midnight ET.

The cost ledger (`ai_cost_ledger`) persists per-call USD costs. Daily roll-up via `spend_summary` for monitoring.

Class-level guardrail: `tests/test_cost_cap_pipeline_enforcement.py::test_every_public_call_function_invokes_cost_cap` AST-walks `ai_providers.py` and fails if any future `call_*` function forgets to invoke `_enforce_cost_cap`. New entry points inherit enforcement automatically; the test catches the gap at test time, not in production.

## 10. Honest limits

These are documented coverage gaps in the risk system. They are not bugs — they are scope constraints of the current implementation.

- **Parametric VaR assumes normal returns.** Tails are under-reported. Monte Carlo VaR helps but inherits the normality of the factor distribution. Mitigation: stress scenarios provide non-parametric worst-case exposure.
- **Stress scenarios miss cross-asset risk.** No rates / FX / commodities in the factor set. 2022-style rate shocks under-report. Mitigation: not yet — see OPEN_ITEMS for cross-asset extension.
- **Older scenarios use partial factor data.** 1987 / dot-com lack sector ETFs (those didn't exist). `approximation_quality` flagged as "low" or "medium" so the AI sees it.
- **Slippage MC is IID per trade.** Correlated regimes (full days of wide spreads) are partially captured by `bootstrap_mode='by_day'` (default), but not perfectly — the by-day mode uses the SAME slippage realization for every trade on a day, which over-corrects. True correlated bootstrap would require more sophistication.
- **Slippage K is paper-fitted.** Real-money fills will deviate. Mitigation: re-run calibration after 30+ days live trading.
- **Synthetic options backtester ≠ precise P&L.** Doesn't capture bid-ask spread, IV term structure, catalyst vol expansion. Sufficient for strategy validation, not precise P&L forecasting.
- **Long-vol hedge bleeds premium in calm markets.** Off by default for that reason; user opts in.
- **Crisis state can lag.** It depends on cross-asset signal aggregation; sudden single-asset crashes may not trigger before damage is done. Mitigation: intraday risk monitor catches single-asset shocks.

## 11. Manual override

The operator retains manual control:

- **Disable a profile entirely** via the master toggle — stops all trading for that profile.
- **Cancel orders** at the broker via Alpaca dashboard or via the platform's pending-orders panel.
- **Stop the scheduler** via systemd: `systemctl stop quantopsai-scheduler`. Web app stays up; existing protective stops at the broker remain active.
- **Manual close** of a position via the platform's per-position close button (submits a market order via Alpaca).
- **Restore deprecated strategies** via the Strategy tab Restore button.
- **Override Layer 2 weights** via Operations tab.

## 12. What is NOT in the risk system

Documenting absence is as important as documenting presence:

- **No automatic position liquidation on severe crisis** beyond blocking new entries. The `severe` state is a strong recommendation; the operator decides whether to flatten.
- **No automatic kill-switch on AI provider error.** Provider failures pause new AI calls but don't block existing protective stops.
- **No regulatory compliance layer.** Pattern-day-trader rules, short-sale uptick rules, etc. are not enforced — Alpaca enforces these at the broker.
- **No multi-tenant audit isolation.** Single-operator design.
- **No formal disaster recovery plan beyond daily DB backups.** RPO ~24h, RTO ~hours (manual restore).

## 13. Audit trail

Everything the system does is logged:

- Trade orders → `trades` table.
- AI predictions → `ai_predictions` table.
- Specialist verdicts → `specialist_outcomes` (after resolution).
- Risk halts → `intraday_risk_halt` + persistent log.
- Crisis transitions → `crisis_state_history`.
- Self-tuner changes → `tuning_history` table.
- Backups → daily snapshots.
- AI costs → `ai_cost_ledger`.
- Scheduler task runs → `task_runs` (timestamps + duration + errors).
- Events handled → `events` table.

Any trade can be reconstructed from the journal: what the AI saw, what the specialists said, what the meta-model thought, what regime was active, what risk readings were live.

## 14. Reference: which gate fires when

Use this table to answer "why didn't this trade execute?" or "why was this trade smaller than I expected?"

| Mechanism | What it blocks | What it scales | Visible in |
|---|---|---|---|
| Balance gate | New entries on over-weighted side | — | AI prompt + dashboard |
| Asymmetric short cap | Short size | Short max_position_pct | Settings + dashboard |
| HTB borrow penalty | — | Short max_position_pct ÷ 2 | Trade detail + AI prompt |
| Neutrality gate | Entries that worsen book beta | — | AI prompt + dashboard |
| Crisis state | New long entries (crisis/severe) | All entry sizes (elevated) | Crisis monitor panel |
| Intraday risk halt | New entries (during 60-min window) | — | Intraday risk panel |
| Wash-trade cooldown | Entries on same symbol within 30 days | — | Recently-exited cache |
| Cost guard | AI-cost-affecting autonomous actions | — | Cost guard panel |
| Schedule window | Orders outside session | — | Order guard log |
| Cross-direction guard | New side while opposing order is open | — | Trade pipeline log |
| Insufficient qty / BP | Recoverable; order skipped this cycle | — | Trade pipeline log |
| Drawdown capital scale | — | All entry sizes (1.0× → 0.25×) | Awareness panel |
| Strategy capital allocator | — | Per-strategy size (0.25× → 2.0×) | Strategy tab |
| Risk-budget vol scaling | — | Per-position size (0.4× → 1.6×) | Risk-budget panel |
| Kelly recommendation | — | AI's sizing reasoning | AI prompt |
| Long-vol hedge | (active hedge ≠ block) | — | Long-vol hedge panel |

## See also

- `docs/03_TRADING_STRATEGY.md` for the operating philosophy of the risk system.
- `docs/05_DATA_DICTIONARY.md` for the schema columns each control reads/writes.
- `docs/07_OPERATIONS.md` for the manual override procedures.
