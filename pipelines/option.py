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
        """Phase 4c (2026-05-12): execute the option proposals that
        survived the specialist veto. Vetoed proposals are persisted
        to broker_rejections + surfaced in `skipped`; approved
        multileg proposals flow through `execute_multileg_strategy`;
        approved single-leg proposals (signal == 'OPTIONS') flow
        through `execute_option_strategy`.

        Replaces the legacy `elif action == "MULTILEG_OPEN":` branch
        in `trade_pipeline.run_trade_cycle`. The branch is now a
        thin caller that builds a one-element SpecialistVerdict and
        delegates here.

        Each entry in `verdict.approved` is a proposal dict with the
        same shape the legacy elif branch consumed (symbol,
        strategy_name, strikes, expiry, contracts, limit_price,
        confidence, reasoning).

        Returns ExecutionResult with submitted/rejected/skipped/
        errors lists. `submitted[i]` and `skipped[i]` are dicts
        compatible with the legacy `details.append(trade_result)`
        flow — same keys.
        """
        result = ExecutionResult()
        if not verdict:
            return result

        # SKIPPED — vetoed proposals
        for vetoed in (verdict.vetoed or []):
            sym = vetoed.get("symbol", "") if isinstance(vetoed, dict) else ""
            veto_reason = self._veto_reason_for(verdict, sym)
            self._record_veto(ctx, vetoed, sym, veto_reason)
            result.skipped.append({
                "action": "SPECIALIST_VETOED",
                "symbol": sym,
                "reason": veto_reason,
            })

        # APPROVED — execute each proposal
        for proposal in (verdict.approved or []):
            if not isinstance(proposal, dict):
                continue
            action = (proposal.get("action") or "").upper()
            symbol = proposal.get("symbol", "")
            try:
                if action == "MULTILEG_OPEN":
                    res = self._execute_multileg(ctx, proposal, symbol)
                elif action == "OPTIONS":
                    res = self._execute_single_leg(ctx, proposal, symbol)
                else:
                    res = {
                        "action": "ERROR", "symbol": symbol,
                        "reason": f"Unknown option action {action!r}",
                    }
                # Classify into submitted vs errors based on the
                # result's action field
                ra = (res.get("action") or "").upper()
                if ra in ("MULTILEG_OPEN", "OPTIONS", "BUY", "SELL"):
                    result.submitted.append(res)
                elif ra == "ERROR":
                    result.errors.append(res)
                else:
                    # SKIP / REJECT / etc. — surface as rejected
                    result.rejected.append(res)
            except Exception as exc:
                result.errors.append({
                    "action": "ERROR", "symbol": symbol,
                    "reason": f"OptionPipeline.execute crashed: {exc}",
                })

        return result

    # -----------------------------------------------------------------
    # Internal helpers — extracted from the legacy elif branches
    # -----------------------------------------------------------------

    @staticmethod
    def _veto_reason_for(verdict, sym: str) -> str:
        """Pick the veto_log entry that mentions this symbol; fall
        back to the first entry or a generic message."""
        if not sym:
            return "specialist veto"
        log = list(getattr(verdict, "veto_log", None) or [])
        for entry in log:
            if sym in entry:
                # Strip leading "<sym>: " prefix when present
                if entry.startswith(f"{sym}:"):
                    return entry[len(sym) + 1:].strip().lstrip("VETO —").strip()
                return entry
        return log[0] if log else "specialist veto"

    @staticmethod
    def _record_veto(ctx, proposal, symbol, reason) -> None:
        """Persist a specialist veto to broker_rejections so the
        dashboard's REJECTED badge fires. Failure is non-fatal."""
        if not ctx or not getattr(ctx, "db_path", None):
            return
        try:
            from journal import record_broker_rejection
            record_broker_rejection(
                ctx.db_path,
                symbol=symbol or proposal.get("symbol", ""),
                action=proposal.get("action", "MULTILEG_OPEN"),
                signal_type=proposal.get("action", "MULTILEG_OPEN"),
                ai_confidence=proposal.get("confidence"),
                ai_reasoning=proposal.get("reasoning"),
                broker_message=f"specialist veto: {reason}",
            )
        except Exception:
            pass

    @staticmethod
    def _execute_multileg(ctx, proposal, symbol):
        """Build the multileg strategy + submit to broker. Body
        extracted from trade_pipeline.run_trade_cycle's
        `elif action == "MULTILEG_OPEN":` branch (Phase 4c migration)."""
        from options_multileg import (
            ALL_MULTILEG_BUILDERS, execute_multileg_strategy,
        )
        from client import get_api as _get_api
        from datetime import date as _ml_date
        from trade_pipeline import _build_multileg_strategy

        api_for_ml = _get_api(ctx)
        strategy_name = proposal.get("strategy_name")
        strikes = proposal.get("strikes") or {}
        expiry_str = proposal.get("expiry")
        contracts = int(proposal.get("contracts") or 0)

        builder = ALL_MULTILEG_BUILDERS.get(strategy_name)
        if not builder:
            return {
                "action": "ERROR", "symbol": symbol,
                "reason": f"Unknown strategy {strategy_name!r}",
            }
        try:
            y, m, d = expiry_str.split("-")
            expiry_date = _ml_date(int(y), int(m), int(d))
        except Exception as exc:
            return {
                "action": "ERROR", "symbol": symbol,
                "reason": f"Invalid expiry {expiry_str!r}: {exc}",
            }

        try:
            print(f"  Executing: MULTILEG_OPEN {strategy_name} "
                  f"{symbol} ({contracts}× exp {expiry_str})")
            spec = _build_multileg_strategy(
                builder, strategy_name, symbol,
                expiry_date, strikes, contracts,
            )
            trade_result = execute_multileg_strategy(
                api_for_ml, spec, ctx=ctx, log=print,
                limit_price=proposal.get("limit_price"),
                ai_confidence=proposal.get("confidence"),
                ai_reasoning=proposal.get("reasoning"),
            )
            trade_result.setdefault("symbol", symbol)
            # Phase 5c linkage — best-effort
            try:
                combo_id = trade_result.get("combo_order_id") or (
                    trade_result.get("leg_order_ids") or [None]
                )[0]
                if combo_id and getattr(ctx, "db_path", None):
                    from journal import link_option_prediction_to_trade
                    link_option_prediction_to_trade(
                        ctx.db_path, symbol=symbol,
                        signal="MULTILEG_OPEN",
                        option_order_id=combo_id,
                    )
            except Exception:
                pass
            return trade_result
        except Exception as exc:
            return {
                "action": "ERROR", "symbol": symbol,
                "reason": f"Multi-leg build/submit failed: {exc}",
            }

    @staticmethod
    def _execute_single_leg(ctx, proposal, symbol):
        """Single-leg option execution. Body extracted from
        trade_pipeline's `if action == "OPTIONS":` branch (Phase 4c
        migration). For symmetry with multileg — the elif branch
        for single-leg can also delegate here in a future cleanup."""
        from options_trader import execute_option_strategy
        from client import get_api as _get_api
        api = _get_api(ctx)
        try:
            print(f"  Executing: OPTIONS {proposal.get('option_strategy', '?')} "
                  f"{symbol} ({proposal.get('contracts', '?')}× "
                  f"@ ${proposal.get('strike', '?')}/{proposal.get('expiry', '?')})")
            trade_result = execute_option_strategy(
                api, proposal, ctx=ctx, log=print,
            )
            trade_result.setdefault("symbol", symbol)
            try:
                occ = (trade_result.get("occ_symbol")
                       or proposal.get("occ_symbol"))
                if occ and getattr(ctx, "db_path", None):
                    from journal import link_option_prediction_to_trade
                    link_option_prediction_to_trade(
                        ctx.db_path, symbol=symbol,
                        signal="OPTIONS", occ_symbol=occ,
                    )
            except Exception:
                pass
            return trade_result
        except Exception as exc:
            return {
                "action": "ERROR", "symbol": symbol,
                "reason": f"Single-leg option submit failed: {exc}",
            }

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
