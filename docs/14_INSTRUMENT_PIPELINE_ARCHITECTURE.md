# 14 — Instrument-Class Pipeline Architecture

**Audience**: anyone touching the trading-decision code path; future
contributors evaluating whether to add a new instrument class
(crypto, FX, futures, etc.).
**Purpose**: the canonical architectural model for how QuantOpsAI
makes trading decisions across multiple instrument classes. Defines
the `Pipeline` interface, what's shared vs forked, and the migration
path from the current stock-first-with-bolted-on-options
implementation to a clean per-instrument-class pipeline model.

**Status**: ratified 2026-05-11 in response to a sweep of bugs
caused by stock/option conflation (see CHANGELOG entries from that
date and `docs/archive/2026-06-04-pre-audit/AUDIT_2026_05_11_AI_PIPELINE.md`). Phase 0 implementation
in progress; Phases 1-6 queued in `TODO.md`.

---

## 0. Why this exists

The system was built stock-first. Options were added later. As of
2026-05-11 we identified at least **eleven** bugs across the
codebase where option trades were processed through stock-shaped
code paths. Symptoms ranged from minor display issues
(inconsistent badges) to data-integrity failures (23 phantom
stock-side trailing-stop orders armed at the broker for option
positions that didn't exist on the underlying).

Today's Position class refactor fixed the *symbol-vs-OCC* layer of
that conflation — at the position-tracking layer, an option
position is now unambiguously typed and routed correctly. But the
audit (`docs/archive/2026-06-04-pre-audit/AUDIT_2026_05_11_AI_PIPELINE.md`) confirmed the same bug
class lives in **every layer above position-tracking**:

- The AI prompt feeds option candidates only stock technicals.
- `actual_return_pct` collates option moves (50-200%) with stock
  moves (2-5%) into a single distribution.
- Self-tuning adjusts stock parameters based on
  option-pollution-distorted win rates.
- Multileg trades bypass the specialist veto pipeline.
- Slippage % math designed for stock prices produces 1130%
  aggregates when applied to penny option premiums.
- The risk model regresses option positions 1:1 against the
  underlying instead of delta-adjusted.

These aren't independent bugs to be patched one at a time. They're
all the same architectural problem in different layers: **option
trades use stock-shaped decision logic.** Each individual fix would
re-emerge until the architecture itself separates instrument
classes.

The architectural answer is to acknowledge that **different
instrument classes are different kinds of bets** and require
different decision pipelines.

---

## 1. The model

```
┌──────────────────────────────────────────────────────────┐
│  Shared infrastructure (instrument-agnostic)             │
│  ─────────────────────────────────────────────────       │
│  • Position class + factories (position.py)              │
│  • Broker connection (client.py)                         │
│  • Journal storage (journal.py — trades, predictions)    │
│  • Scheduler / task dispatcher (multi_scheduler.py)      │
│  • Risk model (portfolio_risk_model.py — delta-adjusted) │
│  • broker_rejections logging (journal.py)                │
│  • UI shared panels (account stats, P&L, equity curve)   │
│  • AI provider client (ai_providers.py)                  │
│  • Configuration / profiles (models.py)                  │
└──────────────────────────────────────────────────────────┘
                       ▲
                       │ Used by all pipelines
                       │
┌──────────────────────┼──────────────────────┬────────────┐
│ StockPipeline        │ OptionPipeline       │ Future:    │
│                      │                      │            │
│ • candidates: stock  │ • candidates: option │ Crypto     │
│   universe + signals │   chains + IV regime │ Pipeline   │
│                      │                      │            │
│ • prompt: tech       │ • prompt: stock      │ FX         │
│   indicators, sector │   technicals + IV    │ Pipeline   │
│   rotation, news     │   rank + Greeks +    │            │
│                      │   DTE + spread       │ Futures    │
│                      │   economics          │ Pipeline   │
│                      │                      │            │
│ • specialists:       │ • specialists:       │            │
│   technical, sector, │   IV-skew, Greeks    │            │
│   sentiment, risk    │   risk, spread P&L,  │            │
│                      │   risk               │            │
│                      │                      │            │
│ • executor:          │ • executor:          │            │
│   stock orders       │   option orders +    │            │
│                      │   multileg combos    │            │
│                      │                      │            │
│ • metrics:           │ • metrics:           │            │
│   Sharpe on equity,  │   theta-decay-adj    │            │
│   sector beta,       │   return, gamma      │            │
│   drawdown           │   exposure, IV PnL   │            │
│                      │                      │            │
│ • tuning:            │ • tuning:            │            │
│   stop_loss_pct,     │   spread max-loss    │            │
│   max_position_pct,  │   thresholds, DTE    │            │
│   stock-mom params   │   floor, IV bands    │            │
└──────────────────────┴──────────────────────┴────────────┘
```

Each pipeline owns its decision logic end-to-end. Shared
infrastructure is the stuff every instrument class genuinely needs:
you talk to one broker, you write to one journal, you sum to one
portfolio risk view, you call the same AI provider.

### 1.1 What stays shared (and why)

| Shared component | Why it's shared |
|---|---|
| `position.py` | A position is a position regardless of instrument; the type tag (`is_option`) is on the object. |
| `client.py` (broker) | One Alpaca account per profile (or shared); the routing happens at order construction, not at the broker connection. |
| `journal.py` (storage) | Trades and predictions hit the same SQLite tables; the *queries* per instrument class become per-pipeline. |
| `multi_scheduler.py` (dispatcher) | The scheduler dispatches each pipeline per cycle; the cycle structure (entries / exits / snapshots) is the same shape for every instrument. |
| `portfolio_risk_model.py` | Risk aggregates across the whole book; you can't risk-manage one pipeline without seeing the others. **But**: the model becomes pipeline-aware — a position contributes its delta-adjusted exposure, not raw market_value (audit finding #7). |
| `ai_providers.py` | The Anthropic / OpenAI / Google call signatures are the same regardless of what's in the prompt. |
| `broker_rejections` | Tracking why the broker said no is identical regardless of the instrument. |
| Account / equity / P&L UI panels | Top-line "what's my equity?" is portfolio-wide. |

### 1.2 What forks (and why)

| Per-pipeline component | Why it forks |
|---|---|
| Candidate generation | Different universes (stock universe vs option chains), different scoring (stock-momentum vs IV-regime). |
| AI prompt | Different features: stocks need RSI/MACD; options need IV/Greeks/DTE/spread economics. |
| Specialists | Strictly option-specific specialists (IV-skew, Greeks, spread P&L like `option_spread_risk`, `gamma_pin_specialist`, `iv_skew_specialist`) make no sense for stocks. *Underlying-shaped* deterministic rules (sector rotation, technical, sentiment, regime) DO apply to options of matching direction — a bullish option entry on AAPL faces the same "is AAPL overextended?" concerns as a bullish AAPL stock entry. The router (`run_panel`) computes the candidate's direction from `(signal, option_strategy)` and routes OPTIONS/MULTILEG_OPEN to the same-direction stock rules. See `docs/02_AI_SYSTEM.md §4a` for routing details. |
| Executor | Stock = `submit_order(symbol, qty, side)`. Option = OCC routing + position_intent + combo legs. |
| Metrics | Stock Sharpe on equity returns vs option theta-decay-adjusted return. Slippage is dollars-per-share for stocks vs dollars-per-contract for options. |
| Tuning | Stock parameters (`stop_loss_pct`) don't apply to options; option parameters (spread max-loss tolerance, DTE floor) don't apply to stocks. |
| UI panel views | Stock trades render differently from option trades (already split into tabs as of TODO #1). |

### 1.3 The principle

> Instrument-agnostic *infrastructure*; instrument-specific *decisions*.

Anywhere a piece of code asks "is this an option or a stock?" to
make a decision, the answer is: that decision belongs in the
pipeline. Anywhere code uses a position, a broker, a journal row —
that's shared infrastructure.

---

## 2. The `Pipeline` interface

Every concrete pipeline implements this contract. The interface is
deliberately minimal: each method has one job, and the cycle
dispatcher composes them.

```python
class Pipeline(ABC):
    """One instrument-class trading pipeline.

    The cycle dispatcher calls these in order each scheduler tick.
    Each method is independently testable; pipelines compose by
    sharing infrastructure (Position, Journal, Broker) but not
    decision logic.
    """

    name: str  # "stock" / "option" / "crypto" / etc.

    @abstractmethod
    def applies_to(self, ctx: UserContext) -> bool:
        """True if this pipeline should run for this profile.

        Most profiles will enable both stock and option pipelines;
        a future Crypto profile would enable only the crypto pipeline.
        Reads ctx.enabled_pipelines or a per-profile flag.
        """

    @abstractmethod
    def generate_candidates(self, ctx: UserContext) -> list[Candidate]:
        """Universe + scoring → list of candidate symbols/contracts.

        StockPipeline: scans the stock universe, runs strategy
            signals, returns top-N stocks with scores.
        OptionPipeline: scans option chains for symbols meeting
            IV-regime criteria, returns top-N (underlying, strategy)
            pairs with scores.
        """

    @abstractmethod
    def build_prompt(self, ctx: UserContext,
                     candidates: list[Candidate]) -> str:
        """Render the AI prompt for THIS pipeline's candidates.

        StockPipeline: technicals, sector rotation, sentiment, news.
        OptionPipeline: IV rank, Greeks, DTE, spread economics
            alongside the underlying's technicals.
        """

    def decide(self, ctx: UserContext,
               prompt: str) -> AIResult:
        """Call the AI provider with the pipeline's prompt.

        Default implementation: shared AI provider call. Pipelines
        rarely need to override this; the prompt is what makes the
        decision instrument-specific, not the model.
        """

    @abstractmethod
    def route_to_specialists(self, ctx: UserContext,
                              ai_result: AIResult) -> SpecialistVerdict:
        """Route the AI's proposals through this pipeline's
        specialist ensemble for veto authority.

        StockPipeline: technical, sector, sentiment, risk specialists.
        OptionPipeline: IV-skew, Greeks risk, spread P&L specialists.

        Each specialist can VETO a proposal. Closes the audit gap
        where MULTILEG_OPEN bypassed all specialist checks.
        """

    @abstractmethod
    def execute(self, ctx: UserContext,
                verdict: SpecialistVerdict) -> ExecutionResult:
        """Submit orders for surviving proposals; log to journal.

        StockPipeline: api.submit_order(symbol=ticker, ...).
        OptionPipeline: api.submit_order(symbol=OCC, position_intent,
            time_in_force) for single-leg; combo POST for multileg.
        """

    @abstractmethod
    def record_outcome(self, ctx: UserContext,
                        prediction_id: int, outcome: Outcome) -> None:
        """When a prediction resolves, record outcome at the correct
        scale.

        StockPipeline: actual_return_pct in stock units (typical
            range 0-10%).
        OptionPipeline: actual_return_pct_option_scaled OR
            outcome stored in a separate option-prediction table to
            prevent self-tuning corruption.
        """

    @abstractmethod
    def compute_metrics(self, ctx: UserContext) -> Metrics:
        """Pipeline-specific metrics for the dashboard + tuner.

        StockPipeline: Sharpe on stock-only equity contributions,
            sector beta, drawdown of stock book.
        OptionPipeline: theta-decay-adjusted return, gamma exposure,
            IV-rank-bucketed P&L; slippage in $ not %.
        """

    @abstractmethod
    def tune(self, ctx: UserContext,
             metrics: Metrics) -> ParameterAdjustments:
        """Adjust pipeline-specific parameters based on its metrics.

        StockPipeline: stop_loss_pct, max_position_pct.
        OptionPipeline: max_spread_loss_pct, min_dte, iv_rank_threshold.
        """
```

### 2.1 How the scheduler uses pipelines

```python
def run_segment_cycle(ctx):
    """Run one full cycle for one profile."""
    pipelines = get_pipelines_for_profile(ctx)  # [StockPipeline(), OptionPipeline()]
    for pipeline in pipelines:
        if not pipeline.applies_to(ctx):
            continue
        try:
            candidates = pipeline.generate_candidates(ctx)
            prompt = pipeline.build_prompt(ctx, candidates)
            ai_result = pipeline.decide(ctx, prompt)
            verdict = pipeline.route_to_specialists(ctx, ai_result)
            result = pipeline.execute(ctx, verdict)
            log_cycle_result(pipeline.name, result)
        except Exception as exc:
            logger.error(
                "Pipeline %s failed for profile %s: %s",
                pipeline.name, ctx.profile_id, exc, exc_info=True,
            )
            # Other pipelines for this profile keep running.
```

Pipelines are independent: an option-pipeline failure on a profile
doesn't stop the stock pipeline for that profile.

---

## 3. Migration phases

Each phase is independently shippable and individually testable.
No phase introduces a behavior change without an explicit
test+CHANGELOG entry. The migration takes 6 phases; we estimate
1-3 sessions per phase (Phases 1, 4, 5 are the longer ones).

### Phase 0 — Define the abstraction; no behavior change

**Goal**: introduce the `Pipeline` ABC + concrete `StockPipeline` and
`OptionPipeline` that wrap the existing code. Like the Position
class shim phase: adds the abstraction layer without moving any
business logic.

**Shipped artifacts**:
- `pipelines/__init__.py` — `Pipeline` ABC, `Candidate`,
  `AIResult`, `SpecialistVerdict`, `ExecutionResult`, `Outcome`,
  `Metrics`, `ParameterAdjustments` types.
- `pipelines/stock.py` — `StockPipeline`, methods delegate to
  existing functions (`ai_analyst.analyze_symbol`, `trader.execute_trade`,
  etc.).
- `pipelines/option.py` — `OptionPipeline`, methods delegate to
  existing options code (`options_multileg.execute_multileg_strategy`,
  etc.).
- `pipelines/registry.py` — `get_pipelines_for_profile(ctx)` returns
  the list of pipelines this profile should run. Today: stock only,
  with option added when `ctx.enable_options` is True.
- Tests pinning the contract: every concrete pipeline implements
  every abstract method; each method's return type matches.

**Exit criteria**:
- 100% of existing tests still pass (no behavior change).
- `get_pipelines_for_profile(ctx)` returns the expected pipeline list
  for every existing profile.
- Both pipelines pass a smoke test: `pipeline.generate_candidates(ctx)`
  doesn't throw on a real prod-like context.

**Estimated work**: ~1 session.

### Phase 1 — Move metrics into per-pipeline namespaces

**Goal**: `metrics/stock.py` and `metrics/option.py`. Stock metrics
queries filter `WHERE occ_symbol IS NULL`; option metrics query the
inverse. Slippage 1130% bug (TODO #8) becomes naturally fixed:
option slippage is computed in dollars-per-contract, not as
percent-of-premium.

**Shipped artifacts**:
- `metrics/stock.py` with all stock-aggregation functions.
- `metrics/option.py` with option-aggregation (theta-decay-adjusted
  return, gamma exposure, dollar-only slippage).
- Dashboard performance page splits per-instrument-class metrics
  panels (stock + option, summed).
- Aggregate metrics that genuinely span both (total equity, total
  drawdown) live in `metrics/portfolio.py`.

**Exit criteria**:
- TODO #8 (1130% slippage) is naturally fixed by separation.
- No test regression.
- `pipelines/{stock,option}.py:compute_metrics()` call into the
  per-pipeline metrics modules.

**Estimated work**: ~2 sessions.

### Phase 2 — Move tuning into per-pipeline namespaces

**Goal**: `tuning/stock.py` and `tuning/option.py`. Stock tuning
adjusts stock parameters from stock metrics only. Option tuning
adjusts option parameters from option metrics only. No
cross-pollution.

**Shipped artifacts**:
- `tuning/stock.py` reading `metrics/stock.py` outputs only.
- `tuning/option.py` reading `metrics/option.py` outputs only.
- `ai_predictions` schema gets a `pipeline_name` column or per-pipeline
  table; tuner queries pre-filter by pipeline.
- Audit finding #3 (self-tuning corruption) is structurally
  eliminated.

**Exit criteria**:
- Self-tuning history shows independent stock and option parameter
  adjustments.
- Backfill / migration script for existing `ai_predictions` rows
  to set `pipeline_name`.

**Estimated work**: ~2 sessions.

### Phase 3 — Fork the AI prompt

**Goal**: `StockPipeline.build_prompt()` doesn't include IV/Greeks;
`OptionPipeline.build_prompt()` does. Audit finding #4 fixed.

**Shipped artifacts**:
- `pipelines/stock_prompt.py` with stock-specific feature rendering.
- `pipelines/option_prompt.py` with option-specific feature
  rendering (IV rank, Greeks, DTE, spread economics).
- `ai_analyst.py` retires its instrument-branching logic; the
  pipelines own their prompts.

**Exit criteria**:
- Option proposals consistently reference IV/Greeks/DTE in their
  reasoning (verified by inspecting recent ai_predictions).
- Stock proposals don't waste tokens on irrelevant option fields.

**Estimated work**: ~2 sessions.

### Phase 4 — Specialist routing per pipeline   ✅ Phase 4a shipped 2026-05-11

**Goal**: each pipeline owns its specialist list. Multileg trades
route through option-specific specialists with veto authority.
Audit findings #5, #6 fixed.

**Shipped artifacts (Phase 4a)**:
- Each specialist module declares `APPLIES_TO_PIPELINES` tuple — `pattern_recognizer` is stock-only, `option_spread_risk` is option-only, the other 4 are cross-pipeline.
- `specialists/option_spread_risk.py` — NEW option-specific specialist with VETO authority. Hunts max-loss-vs-budget, IV crush exposure, near-expiry gamma blowup, credit/max-loss ratio.
- `pipelines/specialist_router.py` — pure `applicable_specialists(pipeline_name)` filter; untagged modules default to `("stock",)` for back-compat.
- `Pipeline.route_to_specialists()` lifted to a concrete base-class method — per-pipeline behavior fully captured by `self.name` driving the router. Future `CryptoPipeline` / `FXPipeline` subclasses get correct routing for free without overriding.
- `ensemble.run_ensemble(specialists_override=...)` new opt-in kwarg lets pipeline routing pass a pre-filtered specialist list. Defaults to `None` for legacy callers.
- Legacy `_specialists_for_market` updated: equity-default path now filters out option-only specialists so stock-shaped legacy callers don't suddenly run `option_spread_risk` on stock candidates. Pre-refactor 5-specialist behavior preserved exactly.

**Phase 4b shipped 2026-05-11**:
- New helper `trade_pipeline.check_multileg_specialist_veto(ctx, ai_trade, symbol)` — calls `OptionPipeline.route_to_specialists()` and returns `(vetoed, reason)`.
- The `MULTILEG_OPEN` elif branch in `run_trade_cycle` calls the helper BEFORE the broker submission. Vetoed trades skip execution + log to broker_rejections; non-vetoed trades proceed unchanged.
- Failure-tolerant: if routing raises (ensemble crash, AI provider down, network error), the helper returns `(False, "")` so the trade proceeds. Phase 4b adds a veto LAYER — never introduces a new failure mode that blocks trades.

**Exit criteria** — all met:
- ✅ `pattern_recognizer` excluded from option proposals by construction (audit finding #6).
- ✅ `option_spread_risk` slot exists with veto authority (audit finding #5 framework).
- ✅ Live multileg cycle runs option_spread_risk veto on every MULTILEG_OPEN proposal.
- ✅ Routing failures don't block trades (failure-tolerance preserved).

**Optional refinements (not in scope)**: full pipeline migration of the multileg branch to `OptionPipeline.execute()` — today's Phase 4b keeps the existing `execute_multileg_strategy` call site and just gates it with the veto check; a future cleanup can move the executor itself into `OptionPipeline.execute()` and delete the legacy elif branch.

### Phase 5 — Per-pipeline outcomes + scaled return   ✅ Phase 5a shipped 2026-05-11

**Goal**: option `actual_return_pct` is scaled or stored separately
so it doesn't pool with stock `actual_return_pct`. Audit findings
#2, #3 fixed structurally (Phase 2's tuning fork already eliminated
the *consumer* side of the bug; Phase 5a eliminates the *storage*
side).

**Shipped artifacts (Phase 5a)**:
- `ai_predictions.pipeline_kind TEXT` column added via journal migration with idempotent backfill from `predicted_signal`.
- `pipelines/outcomes/{stock,option}.py` writers tag every new outcome write with the correct `pipeline_kind`.
- `pipelines/outcomes/__init__.py:kind_from_signal()` — single source of truth for the inference rule (used by backfill + tests).
- `pipelines/{stock,option}.py:record_outcome()` wired to the writers.
- `tuning/{stock,option}.py:current_win_rate()` filters by `pipeline_kind` with `IS NULL`+signal-type fallback for legacy rows.

**Phase 5b shipped 2026-05-11 (safety floor)**:
- `_resolve_one` defers ALL option signals (MULTILEG_OPEN, OPTIONS, OPTION_EXERCISE) — returns None so the row stays 'pending'. No option row gets a wrong actual_return_pct/actual_outcome value written. Stock resolution unchanged.
- `resolve_pending_predictions` logs the deferred-option count per cycle (visible in journalctl).
- New schema columns: `ai_predictions.occ_symbol` (for single-leg lookup via `_fetch_option_premium`) and `ai_predictions.option_order_id` (for multileg leg lookup via the trades table). NULL today; Phase 5c will populate.

**Phase 5c shipped 2026-05-11**:
- New `pipelines/outcomes/option_resolver.py` — pure functions for single-leg (premium delta) and multileg (net spread P&L) return % computation, with option-appropriate win/loss thresholds.
- `journal.link_option_prediction_to_trade()` — UPDATE helper that captures `occ_symbol` (single-leg) and `option_order_id` (multileg) on the prediction row immediately after successful trade execution.
- `journal.get_multileg_legs_by_combo_order()` — leg lookup via either order_id match or reason-string match (handles combo and sequential paths).
- `trade_pipeline.py` calls `link_option_prediction_to_trade()` after both `OPTIONS` and `MULTILEG_OPEN` executions. Best-effort; failure non-fatal.
- `ai_tracker._resolve_one` routes option signals through the resolver. Min-hold window applies. Defers when metadata missing (Phase 5b safety floor still applies for that case).
- `ai_tracker.resolve_pending_predictions` no longer requires stock price for option rows; injects `db_path` into the prediction dict for multileg resolution.

**Phase 5d shipped 2026-05-11**:
- `pipelines/outcomes/backfill.py:backfill_historical_option_predictions(db_path)` — finds pre-Phase-5c option rows, looks up matching trades within ±60min window, populates `option_order_id` (multileg) or `occ_symbol` (single-leg), resets row to 'pending' for Phase 5c re-resolution.
- New `migration_markers` table + `journal.is_migration_done` / `mark_migration_done` helpers — generic infrastructure for future one-shot migrations.
- Auto-runs at `multi_scheduler._run_full_cycle` startup. Marker-gated (one-shot) + WHERE-clause-gated (force-safe). Failure non-fatal.

**Exit criteria** — all met:
- ✅ Phase 5a: Stock and option win-rate distributions don't pool.
- ✅ Phase 5a: Backfill idempotent.
- ✅ Phase 5b: NO option row gets incorrect actual_return_pct.
- ✅ Phase 5b: `_OPTION_SIGNALS` ↔ `kind_from_signal` agree.
- ✅ Phase 5c: option `actual_return_pct` reflects option economics (premium delta or spread P&L).
- ✅ Phase 5c: link_option_prediction_to_trade fires after every successful option execution.
- ✅ Phase 5d: historical option rows backfilled. The nightly task `_task_phase5c_backfill_nightly` calls `backfill_historical_option_predictions(force=True)` once per profile per day; row-level WHERE clause keeps it cheap on clean DBs. Shipped 2026-05-19 (per `docs/18` exit-criteria audit).

**Estimated work remaining**: none for Phases 0-6. All exit criteria met. See `docs/18_OPTIONS_COMPLETION_INVENTORY.md` for the per-artifact completion status.

### Phase 6 — Risk model: delta-adjusted exposure aggregation   ✅ Phase 6a shipped 2026-05-11

**Goal**: the risk model aggregates across pipelines into one
portfolio risk view. For options, position contribution is
delta-adjusted (audit finding #7), not 1:1 market_value. Greek
aggregation surfaces in the AI prompt for all pipelines.

**Shipped artifacts (Phase 6a)**:
- `pipelines/risk/exposure.py:delta_adjusted_position_value()` — pure function. Stocks: |qty × price|. Options: |delta × qty × 100 × spot| using existing `_greek_contribution`. Never raises; returns 0 for un-pricable inputs.
- `pipelines/risk/exposure.py:portfolio_delta_exposure()` — aggregates per-position contributions into `{underlying: $exposure}`. Same-underlying stock + option positions roll up into one bucket.
- `pipelines/risk/__init__.py` re-exports `compute_book_greeks` from `options_greeks_aggregator` (canonical since Phase A1 of OPTIONS_PROGRAM_PLAN — Phase 6 wraps, doesn't reinvent).

**Phase 6b shipped 2026-05-11**:
- `compute_portfolio_risk_from_positions` calls `effective_positions_for_risk_model` before the factor regression. Option positions stop being silently dropped (OCC symbols had no bars); they now roll up under the underlying ticker with signed delta-equivalent market_value.
- New helpers in `pipelines/risk/exposure.py`: `signed_portfolio_delta_exposure` (sign-preserving) and `effective_positions_for_risk_model` (synthetic-position roll-up).
- `multi_scheduler` attaches `book_greeks` to the risk snapshot via `compute_book_greeks`. `render_risk_summary_for_prompt` surfaces a `Greeks: Δ ... Γ ... ν ... θ ...` line when `n_options_legs > 0`; omitted for stock-only books (back-compat preserved).

**Exit criteria** — all met:
- ✅ Long call exposure ≥ 5× premium-based exposure.
- ✅ Same-underlying stock+option positions aggregate to one bucket.
- ✅ Long/short same-params positions have equal absolute exposure.
- ✅ Factor regressions in `portfolio_risk_model` consume delta-equivalent weights.
- ✅ Prompt visibly includes portfolio Greeks when options are present.

**Optional refinements (not in scope)**: live IV oracle wired to `iv_lookup` (today's first wiring uses `FALLBACK_IV=0.25`); position-level Greek breakdown surfaced in the risk dashboard panel.

---

## 4. What this enables (post-migration)

- **Adding crypto = build `CryptoPipeline`.** Implements the same
  ABC; reuses every shared infrastructure piece. No need to find
  every `if instrument == 'stock'` and add a third branch.
- **Adding FX = build `FXPipeline`.** Same.
- **Pipeline-specific A/B testing.** Want to test a new option
  prompt? Run `OptionPipeline_v2` alongside `OptionPipeline_v1`
  with traffic split.
- **Pipeline-specific kill switches.** Disable options across all
  profiles with one config change; stocks keep trading.
- **Cleaner ML feature engineering.** Each pipeline has its own
  feature set; meta-models can train on instrument-specific
  outcome distributions instead of mixed.

## 5. What this prevents

Every audit finding from `docs/archive/2026-06-04-pre-audit/AUDIT_2026_05_11_AI_PIPELINE.md` is
either eliminated by construction or made impossible without
explicit cross-pipeline contamination:

| Finding | How the architecture eliminates it |
|---|---|
| #1 Slippage 1130% | Metrics fork (Phase 1): option slippage in $, never %. |
| #2 return_pct scaling | Outcome fork (Phase 5): option outcomes scaled or separate. |
| #3 Tuning corruption | Tuning fork (Phase 2): each pipeline tunes its own params. |
| #4 Stock-only prompt | Prompt fork (Phase 3): option prompt has IV/Greeks. |
| #5 Multileg specialist bypass | Specialist fork (Phase 4): every pipeline routes through its specialists. |
| #6 Stock specialists on options | **Resolved 2026-05-19** — `deterministic_specialists.run_panel` now translates OPTIONS/MULTILEG_OPEN candidates to a direction via `signal_direction(candidate)` and routes them to same-direction stock rules. A bullish option strategy sees the 123 long-only rules; a bearish strategy sees the 15 short-only set. No per-rule edits needed. |
| #7 Risk model 1:1 market_value | Risk model upgrade (Phase 6): delta-adjusted. |

---

## 6. What does NOT change

- Profile concept stays. A profile (Mid Cap, Large Cap, etc.) can
  enable multiple pipelines.
- Scheduler stays single-process. Pipelines run sequentially per
  profile in each cycle.
- Single Alpaca account per profile (or shared across profiles).
  The pipeline routes orders correctly via `pos.broker_symbol`
  which already handles OCC vs ticker.
- One journal DB per profile. Per-instrument queries become
  per-pipeline filters at the metrics layer.

---

## 7. Long-term roadmap

| Quarter | Pipeline additions |
|---|---|
| Now | StockPipeline, OptionPipeline (this doc's Phases 0-6) |
| +1 quarter | CryptoPipeline (24/7 markets, no expiry) |
| +2 quarters | FXPipeline (leverage, carry, central-bank features) |
| +3 quarters | FuturesPipeline (margin, expiry, contango/backwardation) |

Each new pipeline is one file with concrete implementations of the
ABC, plus its own metrics/tuning/specialists modules. No
modifications to existing pipelines; no cross-instrument bug class
to manage.

---

## 8. References

- `docs/archive/2026-06-04-pre-audit/AUDIT_2026_05_11_AI_PIPELINE.md` — the symptom map this
  architecture eliminates.
- `position.py` — the type-level disambiguation layer this
  architecture builds on.
- `CHANGELOG.md` 2026-05-11 — the option-handling incident that
  triggered this refactor.
- `TODO.md` — Phase status + remaining work items.
