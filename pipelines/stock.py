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
        """Stock-only metrics. Phase 1: stock-only slippage stats
        (the only metric extracted into per-pipeline namespaces so
        far). Subsequent commits will add Sharpe / sector beta /
        stock-book drawdown / win rate as they're moved out of
        `metrics.legacy.calculate_all_metrics`.
        """
        from metrics import stock as stock_metrics
        db_path = getattr(ctx, "db_path", None)
        numbers = {}
        if db_path:
            slip = stock_metrics.slippage_stats(db_path)
            if slip is not None:
                numbers["slippage"] = slip
        return Metrics(pipeline_name=self.name, numbers=numbers)

    def tune(self, ctx, metrics: Metrics) -> ParameterAdjustments:
        """Stock-only tuning. Phase 2: ships the win-rate aggregator
        (the audit finding #3 corruption point) filtered to stock
        signal types. Subsequent commits move the per-parameter
        adjustment logic (stop_loss_pct, max_position_pct, etc.)
        into this method.
        """
        from tuning import stock as stock_tuning
        db_path = getattr(ctx, "db_path", None)
        changes = {}
        rationale_parts = []
        if db_path:
            wr, n = stock_tuning.current_win_rate(db_path)
            rationale_parts.append(
                f"stock win rate {wr:.1f}% over {n} resolved "
                f"stock predictions"
            )
            # Phase 2 returns the read but doesn't yet WRITE
            # parameter changes — the legacy self_tuning module
            # still owns the write path. Subsequent commits move
            # parameter writes here, gated on this stock-only
            # win rate signal.
        return ParameterAdjustments(
            pipeline_name=self.name,
            changes=changes,
            rationale="; ".join(rationale_parts),
        )
