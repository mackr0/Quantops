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
            veto_reason, vetoed_by = self._veto_reason_for(verdict, sym)
            self._record_veto(ctx, vetoed, sym, (veto_reason, vetoed_by))
            result.skipped.append({
                "action": "SPECIALIST_VETOED",
                "symbol": sym,
                "reason": veto_reason,
                # 2026-05-12 — surface the specialist name on the
                # result entry so callers (dashboard, logs) can
                # attribute the block.
                "vetoed_by": vetoed_by,
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
    def _veto_reason_for(verdict, sym: str):
        """Pick the veto_log entry that mentions this symbol and
        return (reason, vetoed_by). vetoed_by is None when the
        log entry doesn't include a parenthesized specialist name.

        Pipeline.route_to_specialists formats entries as
        '<sym>: VETO (<specialist>) — <reason>' (2026-05-12+) or
        the older '<sym>: VETO — <reason>' format. Both parsed.
        """
        if not sym:
            return ("specialist veto", None)
        log = list(getattr(verdict, "veto_log", None) or [])
        for entry in log:
            if sym in entry:
                # Strip leading "<sym>: " prefix when present
                body = entry[len(sym) + 1:].strip() if entry.startswith(f"{sym}:") else entry
                # Parse "VETO (<specialist>) — <reason>" → reason +
                # vetoed_by
                import re
                m = re.match(
                    r"VETO\s*(?:\(([^)]+)\))?\s*[—-]\s*(.*)",
                    body,
                )
                if m:
                    return (m.group(2).strip(), m.group(1))
                # Older format with no VETO prefix — return as-is
                return (body.strip(), None)
        return (log[0] if log else "specialist veto", None)

    @staticmethod
    def _record_veto(ctx, proposal, symbol, reason_or_pair) -> None:
        """Persist a specialist veto to broker_rejections so the
        dashboard's REJECTED badge fires. Failure is non-fatal.

        `reason_or_pair` may be a string (legacy) or a
        (reason, specialist_name) tuple. When the specialist name
        is known, format broker_message as
        'specialist veto (<name>): <reason>' so the dashboard
        tooltip surfaces the attribution."""
        if not ctx or not getattr(ctx, "db_path", None):
            return
        # Normalize input
        if isinstance(reason_or_pair, tuple):
            reason, vetoed_by = reason_or_pair
        else:
            reason, vetoed_by = reason_or_pair, None
        msg = (
            f"specialist veto ({vetoed_by}): {reason}"
            if vetoed_by else f"specialist veto: {reason}"
        )
        try:
            from journal import record_broker_rejection
            record_broker_rejection(
                ctx.db_path,
                symbol=symbol or proposal.get("symbol", ""),
                action=proposal.get("action", "MULTILEG_OPEN"),
                signal_type=proposal.get("action", "MULTILEG_OPEN"),
                ai_confidence=proposal.get("confidence"),
                ai_reasoning=proposal.get("reasoning"),
                broker_message=msg,
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
        """Option-only tuning (2026-05-12).

        Adjusts every AI-tunable option parameter based on option
        win rate over resolved predictions:
          - 3 Greek-budget caps (delta / theta / vega)
          - 5 single-leg exit thresholds (LONG stop/TP/DTE; SHORT TP/stop)
          - 3 option_spread_risk specialist VETO thresholds
            (iv-rank, gamma-DTE, credit-ratio)

        Adjustment rule (simple, defensible):
          - win rate ≥ 60% over ≥ MIN_SAMPLES → LOOSEN. System is
            making money on options; give it more rope.
          - win rate ≤ 40% over ≥ MIN_SAMPLES → TIGHTEN. Bleeding;
            pull in.
          - between 40-60% OR sample size < MIN_SAMPLES: no change.

        Direction is per-param. For most caps "looser = higher",
        but for DTE-based exits and gamma-DTE veto, "looser = LOWER"
        (close less aggressively / veto less often). Each entry in
        BOUNDS carries explicit (loosen_multiplier, tighten_multiplier)
        so each param's direction is unambiguous.

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

        if wr >= 60.0:
            mode = "loosen"
            direction = "loosened"
        elif wr <= 40.0:
            mode = "tighten"
            direction = "tightened"
        else:
            rationale_parts.append(
                f"win rate in neutral band 40-60%; no adjustments"
            )
            return ParameterAdjustments(
                pipeline_name=self.name, changes=changes,
                rationale="; ".join(rationale_parts),
            )

        # (floor, ceiling, loosen_mult, tighten_mult, kind).
        # kind = "int" for params stored as integers (rounded after
        # multiplication); "float" for the rest.
        # For most caps, looser = larger threshold (multiplier > 1).
        # INVERTED params (DTE-based exits / gamma-DTE veto /
        # credit-ratio veto) have looser = smaller threshold.
        BOUNDS = {
            # Greek-budget caps (existing — 2026-05-12 Phase 2b).
            "max_net_options_delta_pct":
                (0.02, 0.10, 1.05, 0.95, "float"),
            "max_theta_burn_dollars_per_day":
                (25.0, 100.0, 1.05, 0.95, "float"),
            "max_short_vega_dollars":
                (250.0, 1000.0, 1.05, 0.95, "float"),
            # LONG single-leg exits — looser = wider band (more
            # negative stop, higher TP threshold).
            "option_premium_stop_loss_pct":
                (-0.80, -0.20, 1.05, 0.95, "float"),
            "option_premium_take_profit_pct":
                (0.50, 2.00, 1.05, 0.95, "float"),
            # DTE exit — INVERTED. Looser = LOWER N (close less
            # aggressively near expiry); tighter = HIGHER N.
            "option_dte_exit_threshold_days":
                (3, 14, 0.95, 1.05, "int"),
            # SHORT single-leg exits — looser = wider band.
            "option_short_premium_take_profit_pct":
                (-0.80, -0.25, 1.05, 0.95, "float"),
            "option_short_premium_stop_loss_pct":
                (0.50, 2.00, 1.05, 0.95, "float"),
            # option_spread_risk VETO thresholds.
            # iv_rank veto: looser = HIGHER threshold (fewer vetoes).
            "option_spread_iv_rank_veto_threshold":
                (50.0, 95.0, 1.05, 0.95, "float"),
            # gamma_dte veto: INVERTED. Looser = LOWER threshold
            # (veto only the deepest-near-expiry trades).
            "option_spread_gamma_dte_veto_threshold":
                (3, 14, 0.95, 1.05, "int"),
            # credit_ratio veto: INVERTED. Looser = LOWER threshold
            # (accept thinner credits).
            "option_spread_credit_ratio_veto_threshold":
                (0.10, 0.40, 0.95, 1.05, "float"),
            # 2026-05-12 — candidate-gen IV thresholds. Defaults
            # 55/55 close the dead zone. When option wins, widen
            # the dead zone (rich UP, cheap DOWN — only propose on
            # extreme IV); when losing, contract dead zone further
            # (rich DOWN, cheap UP — propose more aggressively to
            # accumulate sample).
            "option_iv_rich_threshold":
                (50.0, 75.0, 1.02, 0.98, "float"),
            "option_iv_cheap_threshold":
                (35.0, 60.0, 0.98, 1.02, "float"),
        }
        for param, (floor, ceil, loosen_m, tighten_m, kind) in BOUNDS.items():
            current = getattr(ctx, param, None)
            if current is None:
                continue
            multiplier = loosen_m if mode == "loosen" else tighten_m
            new_val = float(current) * multiplier
            new_val = max(floor, min(ceil, new_val))
            if kind == "int":
                new_val = int(round(new_val))
                if int(new_val) == int(current):
                    continue
                changes[param] = int(new_val)
            else:
                if abs(new_val - float(current)) > 1e-9:
                    changes[param] = float(new_val)

        if changes:
            rationale_parts.append(
                f"{direction} {len(changes)} param(s)"
            )

        return ParameterAdjustments(
            pipeline_name=self.name, changes=changes,
            rationale="; ".join(rationale_parts),
        )
