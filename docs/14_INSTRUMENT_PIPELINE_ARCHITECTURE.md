# 14 — Instrument-Class Pipeline Architecture

**Audience**: anyone touching the trading-decision code path; future
contributors evaluating whether to add a new instrument class
(crypto, FX, futures, etc.).
**Purpose**: the canonical architectural model for how QuantOpsAI
makes trading decisions across multiple instrument classes. Defines
the `Pipeline` interface, what's shared vs forked, and how to add a
new instrument class.

---

## 0. Why the architecture is structured this way

Different instrument classes are different kinds of bets and require
different decision logic. A stock candidate's relevant features
(RSI, MACD, sector rotation, news sentiment) are not the option
candidate's relevant features (IV rank, Greeks, DTE, spread
economics); a stock's outcome scale (2–5% moves) is not an option's
outcome scale (50–200% moves); a stock's slippage is best measured
as percent-of-price while an option's is best measured as
dollars-per-contract. Pooling these in the same code paths means
every cross-pipeline aggregation has to either pick one scale
(distorting the other) or invent special-case branches for the
minority.

The architecture treats **instrument-agnostic infrastructure** —
positions, broker connection, journal storage, risk aggregation,
AI provider plumbing — as shared, and **instrument-specific
decision logic** — candidate generation, prompt assembly,
specialist routing, executor, metrics, tuning — as forked into
per-instrument-class pipelines that implement a common ABC. The
shared infrastructure handles the things every instrument needs
identically; the per-pipeline forks handle the things each
instrument needs differently.

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

## 3. Per-pipeline concerns

Each forked component below answers a single structural concern that pooling stock and option logic would damage.

### 3.1 Metrics (`metrics/stock.py`, `metrics/option.py`, `metrics/portfolio.py`)

Per-pipeline aggregations. Stock slippage is measured in percent-of-price; option slippage is measured in dollars-per-contract — pooling them produces nonsense ratios. Cross-pipeline metrics that genuinely span both (total equity, total drawdown) live in `metrics/portfolio.py` and compose from the per-pipeline outputs.

### 3.2 Tuning (`tuning/stock.py`, `tuning/option.py`)

Per-pipeline parameter optimization. Stock tuners read only stock-pipeline outcomes; option tuners read only option-pipeline outcomes. The `ai_predictions.pipeline_kind` column tags every prediction with its pipeline, so tuning queries pre-filter cleanly without inferring from `predicted_signal`.

### 3.3 Prompts (`pipelines/option_prompt.py`)

`OptionPipeline.build_prompt()` renders the option-specific feature set the LLM needs (IV rank, Greeks, DTE, spread economics, max-loss budget) alongside the underlying's technicals. Stock candidates don't waste tokens on irrelevant option fields.

### 3.4 Specialist routing

Every specialist declares an `APPLIES_TO_PIPELINES` tuple. `pipelines/specialist_router.py` filters the ensemble to the applicable specialists per pipeline before `ensemble.run_ensemble` runs. Option-specific specialists (`option_spread_risk`, `iv_skew_specialist`, `gamma_pin_specialist`) don't fire on stock candidates; stock-specific specialists don't fire on option candidates. Multileg open proposals run through the option-specialist veto layer before broker submission.

### 3.5 Outcomes (`pipelines/outcomes/`)

`ai_predictions.pipeline_kind` tags every resolved prediction. `pipelines/outcomes/option_resolver.py` computes option-appropriate return rates (premium delta for single-leg, net spread P&L for multileg) with option-appropriate win/loss thresholds — stock-style percentage math doesn't apply. `ai_tracker._resolve_one` dispatches to the right resolver by pipeline kind.

### 3.6 Risk model (`pipelines/risk/exposure.py`)

Option positions contribute their delta-adjusted exposure (`|delta × qty × 100 × spot|`) — not their notional premium — to the portfolio risk model. Same-underlying stock + option positions roll up into one bucket. The factor regression in `compute_portfolio_risk_from_positions` consumes the effective synthetic-position view rather than dropping OCC symbols (which have no bars). Portfolio Greeks are aggregated by `options_greeks_aggregator.compute_book_greeks` and surfaced in the AI prompt when any options are open.

---

## 4. What this enables

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

The architecture eliminates by construction the entire class of bugs where stock and option logic share a code path that's correct for one and broken for the other:

| Failure mode | How the architecture eliminates it |
|---|---|
| Option slippage measured as percent-of-price produces meaningless multi-hundred-percent aggregates | Per-pipeline metrics — option slippage is always in dollars-per-contract |
| Option outcomes (50–200% moves) pool with stock outcomes (2–5% moves) and corrupt win-rate distributions | Per-pipeline outcomes — `pipeline_kind` separates them in the journal; each pipeline's tuner reads only its own kind |
| Stock-shaped self-tuning rules adjust option parameters based on cross-polluted win rates | Per-pipeline tuning — each pipeline tunes its own parameters from its own metrics |
| AI prompt feeds option candidates only stock technicals (and vice versa) | Per-pipeline prompts — each pipeline builds the prompt its candidates need |
| Multileg trades bypass the option-specialist veto layer | Per-pipeline specialist routing — every pipeline routes through its applicable specialists, multileg included |
| Stock-trained specialists fire on option candidates with no awareness of options structure | Direction-aware specialist routing — option candidates route through same-direction stock rules plus option-specific specialists |
| Option positions enter the risk model as raw notional and over-state portfolio risk | Per-pipeline risk model — option contribution is delta-adjusted; same-underlying stock + option positions roll up to one bucket |

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

## 7. Future pipeline classes

The ABC is designed so additional instrument classes can be added without modifying existing pipelines. Likely candidates if the platform expands:

- **CryptoPipeline** — 24/7 markets, no expiry. The crypto segment + data path already exists in `segments.py` and the per-profile `enable_crypto` flag is wired; building a `CryptoPipeline` that implements the ABC is the remaining step.
- **FXPipeline** — leverage, carry, central-bank-event features. Requires an upstream broker that supports FX (e.g. IBKR).
- **FuturesPipeline** — margin, expiry, contango/backwardation features. Same broker dependency.

Each new pipeline is one file with concrete implementations of the ABC plus its own metrics/tuning/specialists modules. No modifications to existing pipelines.

---

## 8. References

- `position.py` — the type-level disambiguation layer that handles the symbol-vs-OCC distinction this architecture builds on.
- `pipelines/__init__.py` — the `Pipeline` ABC + supporting DTOs.
- `docs/18_OPTIONS_COMPLETION_INVENTORY.md` — per-artifact inventory of the options-pipeline implementation.
