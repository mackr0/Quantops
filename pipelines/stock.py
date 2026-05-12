"""StockPipeline — Phase 0 of the instrument-class pipeline refactor.

This is a SHELL. The methods below are placeholders that subsequent
phases will fill in with the actual stock-trading logic (currently
spread across `ai_analyst`, `trader`, `trade_pipeline`, `metrics`,
`self_tuning`).

Phase 0 contract:
- The class exists and is registered.
- `applies_to(ctx)` works correctly so the registry can dispatch.
- Other methods raise `NotImplementedError` with a clear pointer
  to the phase that will land them. The scheduler does NOT call
  these yet — it continues using the existing flow.

Subsequent phases incrementally move logic OUT of the existing
modules and INTO the per-method implementations here. After Phase
6, this class owns all stock-decision logic; the existing modules
become thin compatibility shims that get deleted in a final
cleanup pass.
"""
from __future__ import annotations

from typing import List

from . import (AIResult, Candidate, ExecutionResult, Metrics,
               Outcome, ParameterAdjustments, Pipeline,
               SpecialistVerdict)


class StockPipeline(Pipeline):
    name = "stock"

    def applies_to(self, ctx) -> bool:
        """Every active profile trades stocks today. Future: a
        crypto-only profile would set `ctx.disable_stock = True`."""
        return not getattr(ctx, "disable_stock", False)

    def generate_candidates(self, ctx) -> List[Candidate]:
        raise NotImplementedError(
            "Phase 1 wires this to the existing stock candidate "
            "generation in trade_pipeline.run_trade_cycle / "
            "auto_strategy_factory."
        )

    def build_prompt(self, ctx, candidates: List[Candidate]) -> str:
        raise NotImplementedError(
            "Phase 3 forks the AI prompt — stock-only features "
            "(RSI, MACD, sector rotation, sentiment, news). Pulled "
            "out of ai_analyst.analyze_symbol's stock branch."
        )

    def decide(self, ctx, prompt: str) -> AIResult:
        raise NotImplementedError(
            "Phase 3 wires this to the shared ai_providers call."
        )

    def route_to_specialists(self, ctx,
                              ai_result: AIResult) -> SpecialistVerdict:
        raise NotImplementedError(
            "Phase 4 routes proposals through stock specialists "
            "(technical, sector, sentiment, risk_assessor, "
            "adversarial_reviewer)."
        )

    def execute(self, ctx, verdict: SpecialistVerdict) -> ExecutionResult:
        raise NotImplementedError(
            "Phase 4 wires this to the existing trader.execute_trade "
            "for surviving stock proposals."
        )

    def record_outcome(self, ctx, prediction_id: int,
                        outcome: Outcome) -> None:
        raise NotImplementedError(
            "Phase 5 writes outcome to ai_predictions with "
            "stock-scale return % (current behavior — no scaling "
            "needed since stocks set the baseline scale)."
        )

    def compute_metrics(self, ctx) -> Metrics:
        raise NotImplementedError(
            "Phase 1 implements: Sharpe on stock-only equity "
            "contributions, sector beta, stock-book drawdown, "
            "stock-only slippage in $."
        )

    def tune(self, ctx, metrics: Metrics) -> ParameterAdjustments:
        raise NotImplementedError(
            "Phase 2 implements: stop_loss_pct, max_position_pct, "
            "stock-momentum params — all driven by stock-only "
            "metrics, no option pollution."
        )
