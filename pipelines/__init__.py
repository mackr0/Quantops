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

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Option-proposal preflight (2026-07-15)
#
# The specialist ensemble reviewed option proposals through a STOCK-shaped
# renderer that dropped every option field — the model literally saw
# `- COST [? @ $0]:` for a fully-priced bear-call spread, and
# option_spread_risk correctly answered "missing strike and premium data"
# 78 times in a row (the "97% veto storm" was substantially this). The
# preflight (a) ENRICHES each option proposal with the priced economics the
# specialists' prompts demand — the SAME _price_option_rec call the veto
# recorder already made seconds after each veto — and (b) refuses to send
# an unpriceable proposal to the LLM at all: it is dropped loudly, recorded
# as veto_class='invalid_input' (excluded from P(veto) learning), and never
# reaches execution.
# ---------------------------------------------------------------------------

_OPTION_PROPOSAL_ACTIONS = {"MULTILEG_OPEN", "OPTIONS"}


def _enrich_option_proposal(ctx, proposal) -> List[str]:
    """Attach `_specialist_econ` (priced economics + market context) to an
    option proposal, in place. Returns the list of MISSING required fields
    ([] = complete and reviewable).

    Required: symbol, strategy, strikes/strike, expiry, contracts, spot,
    and priced economics (net premium + max loss for verticals; leg
    premium for single-leg OPTIONS). Optional, rendered as 'unavailable'
    when absent: iv_rank, breakeven.
    """
    missing: List[str] = []
    symbol = (proposal.get("symbol") or "").upper()
    action = (proposal.get("action") or "").upper()
    strategy = (proposal.get("strategy_name")
                or proposal.get("option_strategy") or "")
    expiry = proposal.get("expiry")
    try:
        contracts = int(proposal.get("contracts") or 0)
    except (TypeError, ValueError):
        contracts = 0
    if not symbol:
        missing.append("symbol")
    if not strategy:
        missing.append("strategy")
    if not expiry:
        missing.append("expiry")
    if contracts <= 0:
        missing.append("contracts")

    econ: dict = {
        "strategy": strategy,
        "contracts": contracts,
        "expiry": expiry,
        "dte": None,
        "spot": None,
        "iv_rank": None,
        "is_credit": None,
        "entry_net_premium": None,   # per contract, dollars
        "max_loss_per_contract": None,
        "max_gain_per_contract": None,
        "breakeven": None,
        "spread_width_points": None,
        "legs": None,
        "single_leg": action == "OPTIONS",
    }

    # DTE from expiry
    if expiry:
        try:
            from datetime import date as _date, datetime as _dt
            _exp = _date.fromisoformat(str(expiry)[:10])
            econ["dte"] = (_exp - _dt.utcnow().date()).days
        except (ValueError, TypeError):
            missing.append("expiry (unparseable)")

    # Spot + IV rank — oracle first (30-min cached), bars fallback for spot.
    if symbol:
        try:
            from options_oracle import get_options_oracle
            oracle = get_options_oracle(symbol) or {}
            if oracle.get("has_options"):
                econ["spot"] = oracle.get("current_price")
                econ["iv_rank"] = (oracle.get("iv_rank") or {}).get("rank_pct")
        except Exception as _or_exc:
            logger.debug("preflight oracle lookup failed for %s: %s: %s",
                         symbol, type(_or_exc).__name__, _or_exc)
        if not econ["spot"]:
            try:
                from market_data import get_bars
                bars = get_bars(symbol, limit=2)
                if bars is not None and len(bars):
                    econ["spot"] = float(bars["close"].iloc[-1])
            except Exception as _sp_exc:
                logger.debug("preflight spot fallback failed for %s: %s: %s",
                             symbol, type(_sp_exc).__name__, _sp_exc)
    if not econ["spot"]:
        missing.append("spot price")

    # Priced economics.
    if action == "OPTIONS":
        # Single leg: {option_strategy, strike, expiry, contracts}.
        strike = proposal.get("strike")
        if strike is None:
            missing.append("strike")
        elif expiry and symbol and strategy:
            try:
                from datetime import date as _date
                from options_trader import (format_occ_symbol,
                                            SINGLE_LEG_LEG_SPEC)
                from options_strategy_advisor import _cached_option_premium
                # THE canonical strategy → (right, side) table — shared
                # with the executor. A token-matching duplicate here
                # priced protective_put as a CREDIT (review #5/#14);
                # an unknown single-leg strategy is honestly
                # unreviewable rather than guessed.
                if strategy not in SINGLE_LEG_LEG_SPEC:
                    missing.append(
                        f"leg semantics (unknown single-leg strategy "
                        f"{strategy!r})")
                    proposal["_specialist_econ"] = econ
                    return missing
                right, side = SINGLE_LEG_LEG_SPEC[strategy]
                occ = format_occ_symbol(
                    symbol, _date.fromisoformat(str(expiry)[:10]),
                    float(strike), right)
                prem = _cached_option_premium(occ, side)
                if prem and prem > 0:
                    econ["entry_net_premium"] = round(prem * 100.0, 2)
                    econ["is_credit"] = side == "sell"
                    econ["legs"] = [{"occ": occ, "side": side}]
                    if side == "buy":
                        # a long single leg risks exactly the premium
                        econ["max_loss_per_contract"] = round(
                            prem * 100.0, 2)
            except Exception as _sl_exc:
                logger.debug(
                    "preflight single-leg pricing failed for %s: %s: %s",
                    symbol, type(_sl_exc).__name__, _sl_exc)
        if econ["entry_net_premium"] is None:
            missing.append("leg premium (unpriceable)")
    else:
        # Multileg: the same pricing call _price_vetoed_spread makes.
        strikes = proposal.get("strikes") or {}
        if not strikes:
            missing.append("strikes")
        elif symbol and expiry and strategy:
            try:
                from options_strategy_advisor import _price_option_rec
                prec = {"strategy": strategy, "symbol": symbol,
                        "expiry": expiry, "strikes": dict(strikes)}
                _price_option_rec(prec)
                econ["is_credit"] = prec.get("is_credit")
                econ["spread_width_points"] = prec.get("spread_width_points")
                econ["max_loss_per_contract"] = prec.get(
                    "max_loss_per_contract")
                econ["max_gain_per_contract"] = prec.get(
                    "max_gain_per_contract")
                econ["breakeven"] = prec.get("breakeven")
                econ["entry_net_premium"] = prec.get("entry_net_premium")
                econ["legs"] = prec.get("legs")
                if not prec.get("priced"):
                    # Verticals must price (premium + true max loss);
                    # only the conservative width fallback is available
                    # for exotic shapes — those keep max_loss and pass.
                    if {"short", "long"} <= set(strikes.keys()):
                        missing.append("spread pricing (untrusted marks)")
                    elif not econ["max_loss_per_contract"]:
                        missing.append("max loss (unpriceable)")
            except Exception as _ml_exc:
                logger.debug(
                    "preflight spread pricing failed for %s: %s: %s",
                    symbol, type(_ml_exc).__name__, _ml_exc)
                missing.append("spread pricing (error)")

    proposal["_specialist_econ"] = econ
    return missing


def _preflight_option_proposals(ctx, proposals):
    """Split proposals into (ready, dropped_incomplete). Option-shaped
    proposals are enriched in place; incomplete ones are dropped LOUDLY
    before any LLM sees them, marked veto_class='invalid_input' so the
    outcome recorder keeps them out of P(veto) learning, and surfaced in
    the AI Brain via record_trade_drop. Non-option proposals pass through
    untouched."""
    ready, dropped = [], []
    for p in proposals:
        if not isinstance(p, dict):
            ready.append(p)
            continue
        action = (p.get("action") or "").upper()
        if action not in _OPTION_PROPOSAL_ACTIONS:
            ready.append(p)
            continue
        missing = _enrich_option_proposal(ctx, p)
        if not missing:
            ready.append(p)
            continue
        p["_veto_class"] = "invalid_input"
        p["_preflight_missing"] = missing
        # Structured attribution — same fields route_to_specialists
        # stamps on ensemble vetoes, so downstream consumers never
        # parse log text.
        p["_vetoed_by"] = "input_incomplete"
        p["_veto_reason"] = (
            f"option proposal missing usable {', '.join(missing)}; "
            f"dropped before specialist review")
        sym = p.get("symbol", "?")
        logger.error(
            "OPTION PREFLIGHT dropped %s %s before specialist review: "
            "missing/unpriceable %s. The proposal never reached the LLM; "
            "recorded as invalid_input (excluded from veto learning).",
            action, sym, ", ".join(missing),
        )
        db_path = getattr(ctx, "db_path", None) if ctx is not None else None
        if db_path:
            try:
                from journal import record_trade_drop
                record_trade_drop(
                    db_path=db_path,
                    symbol=sym,
                    side=action.lower(),
                    drop_code="OPTION_INPUT_INCOMPLETE",
                    drop_reason=(
                        f"Option proposal was missing usable "
                        f"{', '.join(missing)}, so it could not be "
                        f"risk-reviewed. Dropped before review; not "
                        f"counted against the strategy's veto record."
                    ),
                    ai_confidence=p.get("confidence"),
                    ai_reasoning=p.get("reasoning"),
                )
            except Exception as _td_exc:
                logger.debug(
                    "preflight drop record failed for %s: %s: %s",
                    sym, type(_td_exc).__name__, _td_exc)
        dropped.append(p)
    return ready, dropped


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
        # 2026-07-15 — option-proposal preflight: enrich with priced
        # economics (the render used to drop every option field and the
        # specialists reviewed '- SYM [? @ $0]:'), and refuse to send an
        # unpriceable proposal to the LLM at all. Dropped proposals are
        # BLOCKED from execution (they join `vetoed`) but carry
        # veto_class='invalid_input' so the P(veto) learning never counts
        # a plumbing failure as a risk judgment.
        proposals, preflight_dropped = _preflight_option_proposals(
            ctx, proposals)
        preflight_log = [
            (f"{p.get('symbol', '?')}: VETO (input_incomplete) — option "
             f"proposal missing usable "
             f"{', '.join(p.get('_preflight_missing') or ['inputs'])}; "
             f"dropped before specialist review")
            for p in preflight_dropped
        ]
        if not proposals:
            return SpecialistVerdict(
                approved=[], vetoed=list(preflight_dropped),
                veto_log=preflight_log,
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
        approved = []
        vetoed = list(preflight_dropped)
        veto_log = list(preflight_log)
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
                # 2026-07-15 — ALSO stamp the attribution STRUCTURALLY
                # on the proposal dict. The log line is a display
                # format; consumers that need the fields (the outcome
                # recorder's veto_class, the dispatch dedup) read these
                # instead of regex-parsing text back out.
                if isinstance(proposal, dict):
                    proposal["_vetoed_by"] = vetoed_by
                    proposal["_veto_reason"] = (
                        verdict_data.get("veto_reason") or "")
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
