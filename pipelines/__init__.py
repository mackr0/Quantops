"""Per-instrument-class trading pipelines.

See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` for the full
architectural rationale.

Phase 0 (this commit): introduces the `Pipeline` ABC + DTO types.
Concrete `StockPipeline` and `OptionPipeline` (in sibling modules)
delegate to existing functions — no behavior change.

Phases 1-6 (queued in TODO.md) progressively move metrics, tuning,
prompt construction, specialist routing, outcome tracking, and risk
model into per-pipeline namespaces. The end state:

  - Stocks and options share infrastructure (broker, journal,
    Position class, scheduler, AI provider).
  - Each instrument class owns its decision logic end-to-end:
    feature extraction, prompt, specialist veto, executor, metrics,
    tuning.
  - Adding a new instrument class (crypto, FX, futures) is one
    new concrete `Pipeline` subclass, not modifications to every
    `if instrument == 'stock'` branch in the codebase.

Public surface:
  - `Pipeline` — abstract base class with the cycle interface.
  - DTO types: `Candidate`, `AIResult`, `SpecialistVerdict`,
    `ExecutionResult`, `Outcome`, `Metrics`,
    `ParameterAdjustments`.
  - Re-exported from concrete subclass modules (`pipelines.stock`,
    `pipelines.option`) once those land.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional


# ---------------------------------------------------------------------------
# Data-transfer objects (DTOs) flowing between pipeline stages
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One candidate the pipeline thinks the AI should consider this
    cycle. Output of `generate_candidates`; input to `build_prompt`.

    Generic across instrument classes — pipeline-specific data
    lives in the `extra` dict so the ABC stays minimal.
    """
    symbol: str           # underlying ticker (always)
    score: float          # pipeline-internal ranking signal
    signal: str           # e.g. 'BUY', 'STRONG_BUY', 'MULTILEG_OPEN'
    price: float          # reference price at evaluation time
    extra: dict = field(default_factory=dict)
    # Free-form per-pipeline payload — option pipelines stash IV,
    # Greeks, DTE here; stock pipelines stash sector, momentum, etc.


@dataclass
class AIResult:
    """The AI's decision for this pipeline's candidates this cycle.
    Output of `decide`; input to `route_to_specialists`."""
    proposals: List[dict]      # raw AI-trade dicts (same shape the
                               # current ai_analyst returns)
    reasoning: str = ""        # the AI's prose rationale
    confidence_avg: Optional[float] = None
    raw_response: dict = field(default_factory=dict)


@dataclass
class SpecialistVerdict:
    """Specialist ensemble's verdict on the AI's proposals.
    Output of `route_to_specialists`; input to `execute`.

    Pipelines independently route their proposals through their own
    specialist sets — see audit finding #5 (multileg bypasses
    specialist veto today)."""
    approved: List[dict] = field(default_factory=list)
    vetoed: List[dict] = field(default_factory=list)   # each with .veto_reason
    veto_log: List[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    """Outcome of `execute` — what actually got submitted to the broker.

    Pipeline-agnostic shape. The pipeline knows whether it submitted
    stock orders or option contracts; consumers don't need to."""
    submitted: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)   # broker refusal
    skipped: List[dict] = field(default_factory=list)    # gate refusal
    errors: List[dict] = field(default_factory=list)


@dataclass
class Outcome:
    """A single resolved prediction's outcome.
    Input to `record_outcome`. Pipelines write at the right scale —
    stock pipelines store stock-scale return %, option pipelines
    write option-scaled (notional-weighted) return % so cross-
    instrument aggregations don't conflate two-orders-of-magnitude
    different scales (audit finding #2)."""
    prediction_id: int
    actual_outcome: str         # 'win' / 'loss' / 'scratch'
    actual_return_pct: float    # scaled appropriately by the pipeline
    resolved_at: str
    resolution_price: float
    extra: dict = field(default_factory=dict)


@dataclass
class Metrics:
    """Pipeline-specific metrics. Output of `compute_metrics`; input
    to `tune` and to the dashboard's per-pipeline panels.

    Each pipeline defines its own meaningful metrics in `numbers`;
    the dashboard renderer doesn't need to know what they are.
    Contractual fields (Sharpe, win_rate) are at the top level for
    the rare cross-pipeline comparison; pipeline-specific data is
    nested in `numbers`."""
    pipeline_name: str
    n_predictions: int = 0
    win_rate: Optional[float] = None
    sharpe: Optional[float] = None
    numbers: dict = field(default_factory=dict)   # pipeline-specific


@dataclass
class ParameterAdjustments:
    """Tuner output — what parameters the pipeline wants to change
    based on its own metrics. Output of `tune`. Each pipeline tunes
    its OWN parameters; the audit-finding #3 cross-pollution
    (stock parameters tuned on option-dominated win rate) is fixed
    by-construction here."""
    pipeline_name: str
    changes: dict = field(default_factory=dict)
    rationale: str = ""


# ---------------------------------------------------------------------------
# The Pipeline ABC
# ---------------------------------------------------------------------------

class Pipeline(ABC):
    """One instrument-class trading pipeline.

    Each concrete pipeline (StockPipeline, OptionPipeline,
    CryptoPipeline, ...) implements this contract end-to-end. The
    cycle dispatcher calls these methods in order each scheduler
    tick.

    Pipelines compose by sharing infrastructure (Position, Journal,
    Broker) but NOT decision logic. See
    `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` for the full design.
    """

    name: str  # subclasses set this — "stock", "option", "crypto", etc.

    # -------------------------------------------------------------
    # Lifecycle: each scheduler cycle calls these in order
    # -------------------------------------------------------------

    @abstractmethod
    def applies_to(self, ctx) -> bool:
        """True iff this pipeline should run for the given profile.

        Most profiles enable both stock and option pipelines. A
        future Crypto profile would enable only the crypto pipeline.
        Reads `ctx.enabled_pipelines` or per-profile flags.
        """

    @abstractmethod
    def generate_candidates(self, ctx) -> List[Candidate]:
        """Build the universe + score signals → return top-N candidates
        the AI should consider this cycle.

        Pipeline-specific:
          - StockPipeline: stock universe + technical/sector signals.
          - OptionPipeline: option chains + IV-regime / spread-economics
            scoring.
        """

    @abstractmethod
    def build_prompt(self, ctx, candidates: List[Candidate]) -> str:
        """Render the AI prompt for THIS pipeline's candidates.

        Pipeline-specific — the prompt is what makes the decision
        instrument-aware:
          - StockPipeline: technicals, sector rotation, sentiment, news.
          - OptionPipeline: stock technicals plus IV rank, Greeks,
            DTE, spread max-loss/max-gain, contract bid-ask.
        """

    def decide(self, ctx, prompt: str) -> AIResult:
        """Call the AI provider with the pipeline's prompt.

        Default implementation: shared AI provider call. Pipelines
        rarely need to override — the prompt makes the decision
        instrument-specific, not the model. Subclasses may override
        if they need a different model selection or post-processing.
        """
        # Default behavior added in Phase 0.5 / Phase 3 when the
        # prompt fork lands; for Phase 0 each concrete pipeline
        # implements its own decide() that delegates to the existing
        # ai_analyst code path.
        raise NotImplementedError(
            "Concrete pipelines must implement decide() until the "
            "shared AI provider call lands in Phase 3."
        )

    def route_to_specialists(self, ctx,
                              ai_result: AIResult) -> SpecialistVerdict:
        """Route AI proposals through this pipeline's specialist
        ensemble. Each specialist can VETO a proposal.

        Phase 4 of the pipeline refactor: this is a concrete base-
        class method. The per-pipeline behavior is captured entirely
        by `self.name` driving the specialist filter — stock pipeline
        sees stock-tagged specialists; option pipeline sees option-
        tagged specialists; future CryptoPipeline or FXPipeline
        subclasses get correct routing for free without overriding.

        Closes audit findings:
          #5 — multileg trades bypassed all specialist checks today
               (the legacy options_multileg path skips ensemble
               entirely). Once Phase 4b wires the dispatcher, the
               pipeline.run_cycle() path runs every option proposal
               through option_spread_risk + adversarial_reviewer.
          #6 — stock specialists like pattern_recognizer fired on
               option proposals and produced noise. The router now
               filters them out by tag.
        """
        from . import specialist_router
        spec_list = specialist_router.applicable_specialists(self.name)
        proposals = list(getattr(ai_result, "proposals", []) or [])
        if not proposals:
            return SpecialistVerdict(
                approved=[], vetoed=[],
                veto_log=[
                    f"{self.name} pipeline: no proposals to route "
                    f"(would have used {len(spec_list)} specialists)"
                ],
            )
        # Compose the per-pipeline ensemble call. Tests patch
        # `ensemble.run_ensemble` to verify the specialist list flows
        # through without making AI calls; production callers get the
        # real ensemble.
        from ensemble import run_ensemble
        result = run_ensemble(
            candidates=proposals,
            ctx=ctx,
            ai_provider=getattr(ctx, "ai_provider", "anthropic"),
            ai_model=getattr(ctx, "ai_model", ""),
            ai_api_key=getattr(ctx, "ai_api_key", ""),
            specialists_override=spec_list,
            # Pipeline-aware calibrator lookup — stock pipeline gets
            # stock-trained calibration; option pipeline gets
            # option-trained. See specialist_calibration.fit_calibrator.
            pipeline_kind=self.name,
        )
        per_symbol = (result or {}).get("per_symbol", {})
        approved, vetoed, veto_log = [], [], []
        for proposal in proposals:
            sym = proposal.get("symbol") if isinstance(proposal, dict) else None
            verdict_data = per_symbol.get(sym, {}) if sym else {}
            if verdict_data.get("vetoed"):
                vetoed.append(proposal)
                # 2026-05-12 — include WHICH specialist vetoed so the
                # dashboard / broker_rejections message can attribute
                # the block to a specific reviewer (e.g.,
                # "VETO (option_spread_risk) — max loss exceeds budget").
                # Format consumed by OptionPipeline._record_veto and
                # the trade_pipeline.py log line.
                vetoed_by = verdict_data.get("vetoed_by")
                attr = f" ({vetoed_by})" if vetoed_by else ""
                veto_log.append(
                    f"{sym}: VETO{attr} — "
                    f"{(verdict_data.get('veto_reason') or '')[:120]}"
                )
            else:
                approved.append(proposal)
        return SpecialistVerdict(
            approved=approved, vetoed=vetoed, veto_log=veto_log,
        )

    @abstractmethod
    def execute(self, ctx, verdict: SpecialistVerdict) -> ExecutionResult:
        """Submit orders for surviving proposals; log to journal.

        Pipeline-specific submission paths:
          - StockPipeline: api.submit_order(symbol=ticker, ...)
          - OptionPipeline: api.submit_order(symbol=OCC,
            position_intent, ...) for single-leg; combo POST for
            multileg.
        """

    @abstractmethod
    def record_outcome(self, ctx, prediction_id: int,
                        outcome: Outcome) -> None:
        """Store a resolved prediction at the right scale for THIS
        pipeline.

        Critical for audit finding #2 (return_pct scaling): stocks
        store ~2% range; options must scale or store separately so
        downstream tuning sees comparable distributions.
        """

    @abstractmethod
    def compute_metrics(self, ctx) -> Metrics:
        """Pipeline-specific metrics for the dashboard + tuner.

        Each pipeline owns its meaningful metrics:
          - StockPipeline: Sharpe on stock-only equity contributions,
            sector beta, drawdown of stock book.
          - OptionPipeline: theta-decay-adjusted return, gamma
            exposure, IV-rank-bucketed P&L; slippage in dollars,
            never as % of penny premiums.
        """

    @abstractmethod
    def tune(self, ctx, metrics: Metrics) -> ParameterAdjustments:
        """Adjust THIS pipeline's parameters based on ITS metrics.

        Eliminates audit finding #3 (self-tuning corruption) by
        construction: stock tuner only sees stock metrics, option
        tuner only sees option metrics.
        """

    # -------------------------------------------------------------
    # Convenience: full cycle (for testing + scheduler dispatch)
    # -------------------------------------------------------------

    def run_cycle(self, ctx) -> ExecutionResult:
        """Compose the lifecycle methods into one cycle.

        Used by the scheduler dispatcher and by tests. A concrete
        pipeline can override if it needs custom orchestration
        (e.g., exits before entries, post-execution tasks)."""
        if not self.applies_to(ctx):
            return ExecutionResult()
        candidates = self.generate_candidates(ctx)
        if not candidates:
            return ExecutionResult()
        prompt = self.build_prompt(ctx, candidates)
        ai_result = self.decide(ctx, prompt)
        verdict = self.route_to_specialists(ctx, ai_result)
        return self.execute(ctx, verdict)


__all__ = [
    "Pipeline",
    "Candidate",
    "AIResult",
    "SpecialistVerdict",
    "ExecutionResult",
    "Outcome",
    "Metrics",
    "ParameterAdjustments",
]
