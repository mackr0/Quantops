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
        """Option-aware AI prompt — delegates to the per-pipeline
        builder which surfaces IV rank, Greeks, DTE, strike, and
        spread economics alongside the underlying's technicals.
        Closes audit finding #4 by construction. Phase 3."""
        from . import option_prompt
        return option_prompt.build_prompt(ctx, candidates)

    def decide(self, ctx, prompt: str) -> AIResult:
        raise NotImplementedError(
            "Phase 3 wires this to the shared ai_providers call."
        )

    # route_to_specialists: Phase 4 lifted this to the Pipeline base
    # class — the per-pipeline behavior is fully captured by self.name
    # driving `specialist_router.applicable_specialists`. OptionPipeline
    # therefore inherits the routing logic; option-tagged specialists
    # (option_spread_risk + the cross-pipeline ones) filter in,
    # stock-only specialists like pattern_recognizer filter out.

    def execute(self, ctx, verdict: SpecialistVerdict) -> ExecutionResult:
        raise NotImplementedError(
            "Phase 4 wires this to options_multileg."
            "execute_multileg_strategy and options_trader for "
            "surviving option proposals."
        )

    def record_outcome(self, ctx, prediction_id: int,
                        outcome: Outcome) -> None:
        """Write a resolved option prediction with pipeline_kind='option'.
        Phase 5a (this commit) ships the structural tag — downstream
        tuning aggregations filter by it so option outcomes can never
        pool with stock outcomes (audit finding #2). Phase 5b will
        also correct the upstream resolver's wrong-price issue
        (today's resolver computes underlying price %, not premium %
        or net P&L vs max-loss)."""
        from .outcomes import option as option_outcomes
        db_path = getattr(ctx, "db_path", None)
        if not db_path:
            return
        option_outcomes.record(db_path, prediction_id, outcome)

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
        """Option-only tuning (Phase 2b, 2026-05-12).

        Computes adjustments to the three option-Greeks budget
        parameters that already exist on UserContext / trading_profiles:
          - max_net_options_delta_pct (directional cap)
          - max_theta_burn_dollars_per_day (long-vol budget)
          - max_short_vega_dollars (short-vol cap)

        Adjustment rule (simple, defensible):
          - win rate ≥ 60% over ≥ MIN_SAMPLES → LOOSEN by 5%
            (multiply by 1.05, clipped to ceiling). The system is
            making money with options; give it slightly more rope.
          - win rate ≤ 40% over ≥ MIN_SAMPLES → TIGHTEN by 5%
            (multiply by 0.95, clipped to floor). Bleeding money;
            pull in.
          - between 40% and 60%, OR sample size < MIN_SAMPLES:
            no change. Don't tune on noise.

        Floors and ceilings keep the tuner from running away. The
        changes dict is consumed by `apply_parameter_adjustments`
        which UPDATEs the trading_profiles row.
        """
        from tuning import option as option_tuning
        db_path = getattr(ctx, "db_path", None)
        changes: dict = {}
        rationale_parts = []
        if not db_path:
            return ParameterAdjustments(
                pipeline_name=self.name, changes=changes, rationale="",
            )

        wr, n = option_tuning.current_win_rate(db_path)
        rationale_parts.append(
            f"option win rate {wr:.1f}% over {n} resolved "
            f"option predictions"
        )

        MIN_SAMPLES = 20
        if n < MIN_SAMPLES:
            rationale_parts.append(
                f"insufficient samples (need ≥{MIN_SAMPLES}); "
                f"no parameter adjustments"
            )
            return ParameterAdjustments(
                pipeline_name=self.name, changes=changes,
                rationale="; ".join(rationale_parts),
            )

        # Direction of adjustment
        if wr >= 60.0:
            multiplier = 1.05
            direction = "loosened"
        elif wr <= 40.0:
            multiplier = 0.95
            direction = "tightened"
        else:
            rationale_parts.append(
                f"win rate in neutral band 40-60%; no adjustments"
            )
            return ParameterAdjustments(
                pipeline_name=self.name, changes=changes,
                rationale="; ".join(rationale_parts),
            )

        # Range guards per parameter (floor, ceiling).
        BOUNDS = {
            "max_net_options_delta_pct": (0.02, 0.10),
            "max_theta_burn_dollars_per_day": (25.0, 100.0),
            "max_short_vega_dollars": (250.0, 1000.0),
        }
        for param, (floor, ceil) in BOUNDS.items():
            current = getattr(ctx, param, None)
            if current is None:
                continue
            new_val = float(current) * multiplier
            new_val = max(floor, min(ceil, new_val))
            # Only record a change if the bound-clipped value is
            # actually different (avoid no-op writes).
            if abs(new_val - float(current)) > 1e-9:
                changes[param] = new_val

        if changes:
            rationale_parts.append(
                f"{direction} {len(changes)} param(s)"
            )

        return ParameterAdjustments(
            pipeline_name=self.name, changes=changes,
            rationale="; ".join(rationale_parts),
        )
