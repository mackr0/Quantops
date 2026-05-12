"""OptionPipeline — Phase 0 of the instrument-class pipeline refactor.

Like StockPipeline, this is a SHELL. Methods are placeholders that
subsequent phases will fill in by extracting option logic from
`options_multileg`, `options_trader`, `ai_analyst` (multileg branch),
and the shared metrics/tuning modules.

Phase 0 contract:
- The class exists and is registered.
- `applies_to(ctx)` works correctly.
- Other methods raise `NotImplementedError`. The scheduler does NOT
  call these yet.

The end-state of this class (post Phase 6):
- Owns option-aware feature extraction (IV rank, Greeks, DTE,
  spread economics) — fixes audit finding #4.
- Owns option-specific specialists with veto authority — fixes
  audit findings #5, #6.
- Stores option outcomes at the right scale — fixes audit finding #2.
- Computes option metrics in $ not %, eliminating the 1130%
  slippage display by construction — fixes TODO #8.
- Tunes option-specific parameters (max spread loss, DTE floor,
  IV bands) — fixes audit finding #3.
"""
from __future__ import annotations

from typing import List

from . import (AIResult, Candidate, ExecutionResult, Metrics,
               Outcome, ParameterAdjustments, Pipeline,
               SpecialistVerdict)


class OptionPipeline(Pipeline):
    name = "option"

    def applies_to(self, ctx) -> bool:
        """Every active profile evaluates options today (the current
        ai_analyst flow proposes multileg trades opportunistically
        regardless of profile flag). Future: profiles can opt out
        via `ctx.disable_options = True`."""
        return not getattr(ctx, "disable_options", False)

    def generate_candidates(self, ctx) -> List[Candidate]:
        raise NotImplementedError(
            "Phase 1 wires this to options_strategy_advisor."
            "evaluate_candidate_for_multileg + IV-regime scoring. "
            "Returns (underlying, strategy_name, strikes, expiry) "
            "tuples scored by IV rank + technical alignment."
        )

    def build_prompt(self, ctx, candidates: List[Candidate]) -> str:
        raise NotImplementedError(
            "Phase 3 forks the AI prompt — option-specific features: "
            "IV rank, Greeks (delta/gamma/theta/vega), days-to-expiry, "
            "spread max-loss/max-gain, contract bid-ask. Closes "
            "audit finding #4 (today's prompt feeds option candidates "
            "only stock technicals)."
        )

    def decide(self, ctx, prompt: str) -> AIResult:
        raise NotImplementedError(
            "Phase 3 wires this to the shared ai_providers call."
        )

    def route_to_specialists(self, ctx,
                              ai_result: AIResult) -> SpecialistVerdict:
        raise NotImplementedError(
            "Phase 4 routes proposals through option-specific "
            "specialists: IV-skew, Greeks risk, spread P&L, plus "
            "the cross-pipeline adversarial_reviewer. Closes audit "
            "findings #5 (multileg bypasses veto today) and #6 "
            "(stock specialists shouldn't see option proposals)."
        )

    def execute(self, ctx, verdict: SpecialistVerdict) -> ExecutionResult:
        raise NotImplementedError(
            "Phase 4 wires this to options_multileg."
            "execute_multileg_strategy and options_trader for "
            "surviving option proposals."
        )

    def record_outcome(self, ctx, prediction_id: int,
                        outcome: Outcome) -> None:
        raise NotImplementedError(
            "Phase 5 writes outcome with option-scaled return % "
            "(notional-weighted) so it doesn't pool with stock "
            "outcomes. Closes audit finding #2."
        )

    def compute_metrics(self, ctx) -> Metrics:
        """Option-only metrics. Phase 1: option slippage in $ (never
        as % of penny premiums — see `metrics/option.py`). Closes
        TODO #8 / audit finding #1 by construction. Subsequent
        commits will add theta-decay-adjusted return, gamma
        exposure, IV-rank-bucketed P&L.
        """
        from metrics import option as option_metrics
        db_path = getattr(ctx, "db_path", None)
        numbers = {}
        if db_path:
            slip = option_metrics.slippage_stats(db_path)
            if slip is not None:
                numbers["slippage"] = slip
        return Metrics(pipeline_name=self.name, numbers=numbers)

    def tune(self, ctx, metrics: Metrics) -> ParameterAdjustments:
        """Option-only tuning. Phase 2: ships option-filtered win
        rate. Subsequent commits add option-specific parameter
        adjustments (max_spread_loss_pct, min_dte, iv_rank_threshold).
        """
        from tuning import option as option_tuning
        db_path = getattr(ctx, "db_path", None)
        changes = {}
        rationale_parts = []
        if db_path:
            wr, n = option_tuning.current_win_rate(db_path)
            rationale_parts.append(
                f"option win rate {wr:.1f}% over {n} resolved "
                f"option predictions"
            )
        return ParameterAdjustments(
            pipeline_name=self.name,
            changes=changes,
            rationale="; ".join(rationale_parts),
        )
