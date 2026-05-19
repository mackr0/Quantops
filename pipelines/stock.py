"""StockPipeline — full instrument-class pipeline for stocks.

End-state methods now implemented (2026-05-19):
- generate_candidates: reads ctx.shortlist (signal dicts the
  upstream screener already produced) and emits Candidate objects
  with stock technicals in `extra`.
- decide: calls the shared ai_providers.call_ai with the stock
  prompt; tolerant JSON parsing; filters out option actions so
  cross-pipeline misfires don't reach the stock veto layer.
- execute: loops verdict.approved, calls trader.execute_trade per
  symbol, classifies each result into submitted/rejected/skipped/
  errors.

The scheduler dispatcher today still uses the legacy
trade_pipeline.run_trade_cycle path; StockPipeline is now
end-to-end runnable via .run_cycle(ctx) for the eventual cutover
and for tests.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import List

from . import (AIResult, Candidate, ExecutionResult, Metrics,
               Outcome, ParameterAdjustments, Pipeline,
               SpecialistVerdict)

logger = logging.getLogger(__name__)


# Module-level veto helpers. Same shape as `OptionPipeline._veto_reason_for`
# and `OptionPipeline._record_veto` — generic across instrument classes
# (the veto_log format and broker_rejections shape are pipeline-neutral).
# Duplicated here rather than imported from option.py to avoid the
# cross-module dependency for what's essentially shared utility code;
# a future cleanup could lift these into `pipelines/__init__.py`.

def _veto_reason_for_stock(verdict, sym: str):
    """Pick the veto_log entry mentioning `sym` → (reason, vetoed_by)."""
    if not sym:
        return ("specialist veto", None)
    log = list(getattr(verdict, "veto_log", None) or [])
    for entry in log:
        if sym in entry:
            body = entry[len(sym) + 1:].strip() if entry.startswith(f"{sym}:") else entry
            m = re.match(r"VETO\s*(?:\(([^)]+)\))?\s*[—-]\s*(.*)", body)
            if m:
                return (m.group(2).strip(), m.group(1))
            return (body.strip(), None)
    return (log[0] if log else "specialist veto", None)


def _record_stock_veto(ctx, proposal, symbol, reason_or_pair) -> None:
    """Persist a specialist veto to broker_rejections (non-fatal)."""
    if not ctx or not getattr(ctx, "db_path", None):
        return
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
            action=proposal.get("action", "BUY"),
            signal_type=proposal.get("action", "BUY"),
            ai_confidence=proposal.get("confidence"),
            ai_reasoning=proposal.get("reasoning"),
            broker_message=msg,
        )
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            AttributeError, KeyError, OSError, ImportError,
            RuntimeError) as _v_exc:
        logger.warning(
            "stock veto-prediction journal write failed: %s: %s",
            type(_v_exc).__name__, _v_exc,
        )


# Stock technical keys carried through from the shortlist to the
# Candidate.extra so the prompt has them for the AI. Sourced from
# trade_pipeline._build_candidates_data's entry shape.
_STOCK_TECHNICAL_KEYS = (
    "rsi", "volume_ratio", "atr", "adx", "stoch_rsi", "roc_10",
    "pct_from_52w_high", "mfi", "cmf", "squeeze", "pct_from_vwap",
    "nearest_fib_dist", "gap_pct", "candle", "track_record",
    "last_prediction", "votes", "reason", "sector", "score",
)


class StockPipeline(Pipeline):
    name = "stock"

    def applies_to(self, ctx) -> bool:
        """Every active profile trades stocks today. Future: a
        crypto-only profile would set `ctx.disable_stock = True`."""
        return not getattr(ctx, "disable_stock", False)

    def generate_candidates(self, ctx) -> List[Candidate]:
        """Build stock candidates from the per-cycle shortlist.

        Reads `ctx.shortlist` (list of signal dicts produced by the
        upstream strategy / screener layer — same shape as
        `trade_pipeline._build_candidates_data`). Filters to
        actionable signals (BUY / STRONG_BUY / SELL / STRONG_SELL /
        SHORT / STRONG_SHORT) and emits one Candidate per symbol
        with the stock technicals carried in `extra`.

        Skipped: rows missing symbol/price; rows already option-
        tagged (those are the OptionPipeline's responsibility).

        Top-N by score controlled by `ctx.stock_candidate_top_n`
        (default 25 — matches the legacy batch_select sizing).
        """
        shortlist = list(getattr(ctx, "shortlist", None) or [])
        if not shortlist:
            return []

        ACTIONABLE = {
            "BUY", "STRONG_BUY", "WEAK_BUY",
            "SELL", "STRONG_SELL", "WEAK_SELL",
            "SHORT", "STRONG_SHORT", "COVER",
        }

        out: List[Candidate] = []
        for signal in shortlist:
            if not isinstance(signal, dict):
                continue
            symbol = signal.get("symbol")
            if not symbol:
                continue
            sig_str = (signal.get("signal") or "").upper()
            if sig_str and sig_str not in ACTIONABLE and sig_str != "HOLD":
                # Option-action rows ('MULTILEG_OPEN', 'OPTIONS') are
                # OptionPipeline's domain; skip here so a single
                # shortlist can be safely consumed by both pipelines
                # without each duplicating the other's work.
                continue
            try:
                price = float(signal.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            extra = {}
            for k in _STOCK_TECHNICAL_KEYS:
                if k in signal:
                    extra[k] = signal[k]

            try:
                score = float(signal.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0

            out.append(Candidate(
                symbol=symbol,
                score=score,
                signal=sig_str or "HOLD",
                price=price,
                extra=extra,
            ))

        out.sort(key=lambda c: c.score, reverse=True)
        top_n = int(getattr(ctx, "stock_candidate_top_n", 25) or 25)
        return out[:top_n]

    def build_prompt(self, ctx, candidates: List[Candidate]) -> str:
        """Stock-only AI prompt — delegates to the per-pipeline
        builder which strips any option-specific feature keys (IV,
        Greeks, DTE, strike, spread economics) before they reach
        the AI. Phase 3 of the pipeline refactor."""
        from . import stock_prompt
        return stock_prompt.build_prompt(ctx, candidates)

    def decide(self, ctx, prompt: str) -> AIResult:
        """Send the stock-only prompt to the AI provider and parse
        proposals. Mirrors ai_analyst.ai_select_trades's call shape
        (call_ai + tolerant JSON parse). Filters to stock-side
        actions (BUY/SELL/SHORT/COVER); option-typed trade dicts the
        AI might emit are dropped so they don't reach the stock veto
        layer.

        Fail-soft on cost-cap and provider errors: returns an empty
        AIResult with the failure reason in `reasoning`.
        """
        from ai_providers import call_ai
        from ai_analyst import _parse_ai_response_tolerant
        from cost_guard import CostCapExceeded

        provider = getattr(ctx, "ai_provider", "anthropic") or "anthropic"
        model = (getattr(ctx, "ai_model", None)
                 or "claude-haiku-4-5-20251001")
        api_key = getattr(ctx, "ai_api_key", "") or ""
        db_path = getattr(ctx, "db_path", None)

        raw = ""
        try:
            raw = call_ai(
                prompt, provider=provider, model=model,
                api_key=api_key, max_tokens=4096,
                db_path=db_path,
                purpose="stock_pipeline_decide",
            )
            parsed = _parse_ai_response_tolerant(raw)
        except CostCapExceeded as exc:
            logger.warning(
                "StockPipeline.decide: cost cap blocked call: %s", exc,
            )
            return AIResult(
                proposals=[],
                reasoning=f"Cost cap reached: {exc}",
                raw_response={"cost_capped": True},
            )
        except Exception as exc:
            logger.error(
                "StockPipeline.decide: AI call/parse failed: %s: %s "
                "— raw[:200]=%r",
                type(exc).__name__, exc, (raw or "")[:200],
            )
            return AIResult(
                proposals=[],
                reasoning=f"AI call failed: {exc}",
            )

        STOCK_ACTIONS = {"BUY", "SELL", "SHORT", "COVER",
                         "STRONG_BUY", "STRONG_SELL", "WEAK_BUY",
                         "WEAK_SELL", "PAIR_TRADE"}
        proposals: List[dict] = []
        confidences: List[float] = []
        for trade in (parsed.get("trades") or []):
            if not isinstance(trade, dict):
                continue
            action = (trade.get("action") or "").upper()
            if action not in STOCK_ACTIONS:
                continue
            proposals.append(trade)
            try:
                c = trade.get("confidence")
                if c is not None:
                    confidences.append(float(c))
            except (TypeError, ValueError):
                pass

        conf_avg = (sum(confidences) / len(confidences)) if confidences else None
        return AIResult(
            proposals=proposals,
            reasoning=parsed.get("portfolio_reasoning", "") or "",
            confidence_avg=conf_avg,
            raw_response=parsed if isinstance(parsed, dict) else {},
        )

    # route_to_specialists: Phase 4 lifted this to the Pipeline base
    # class — the per-pipeline behavior is fully captured by self.name
    # driving `specialist_router.applicable_specialists`. StockPipeline
    # therefore inherits the routing logic; stock-tagged specialists
    # (pattern_recognizer + the cross-pipeline ones) automatically
    # filter in.

    def execute(self, ctx, verdict: SpecialistVerdict) -> ExecutionResult:
        """Submit each surviving stock proposal via trader.execute_trade.

        Vetoed proposals are persisted to broker_rejections (so the
        dashboard REJECTED badge surfaces them) and surfaced in
        `result.skipped`. Approved proposals call trader.execute_trade
        with a signal dict built from the proposal; the result's
        `action` field classifies it into submitted/rejected/errors.

        Mirrors the executor side of `OptionPipeline.execute`. The
        legacy trade_pipeline.run_trade_cycle keeps using
        trader.execute_trade directly today — this method exists
        for the eventual scheduler cutover and for tests, and is
        end-to-end runnable.
        """
        result = ExecutionResult()
        if not verdict:
            return result

        # Vetoed → persist + skipped
        for vetoed in (verdict.vetoed or []):
            sym = vetoed.get("symbol", "") if isinstance(vetoed, dict) else ""
            veto_reason, vetoed_by = _veto_reason_for_stock(verdict, sym)
            _record_stock_veto(ctx, vetoed, sym, (veto_reason, vetoed_by))
            result.skipped.append({
                "action": "SPECIALIST_VETOED",
                "symbol": sym,
                "reason": veto_reason,
                "vetoed_by": vetoed_by,
            })

        # Approved → execute
        from trader import execute_trade
        for proposal in (verdict.approved or []):
            if not isinstance(proposal, dict):
                continue
            symbol = proposal.get("symbol", "")
            action = (proposal.get("action") or "").upper()
            if not symbol or not action:
                result.errors.append({
                    "action": "ERROR", "symbol": symbol,
                    "reason": "Stock proposal missing symbol or action",
                })
                continue
            signal_dict = {
                "signal": action,
                "price": proposal.get("price"),
                "confidence": proposal.get("confidence"),
                "reason": proposal.get("reasoning")
                           or proposal.get("reason", ""),
                "score": proposal.get("score"),
            }
            try:
                trade_result = execute_trade(
                    symbol, signal_dict, ctx=ctx,
                    strategy_name="stock_pipeline",
                    log=True,
                )
            except Exception as exc:
                result.errors.append({
                    "action": "ERROR", "symbol": symbol,
                    "reason": f"execute_trade crashed: {exc}",
                })
                continue
            ra = ((trade_result or {}).get("action") or "").upper()
            if ra in ("BUY", "SELL", "SHORT", "COVER"):
                result.submitted.append(trade_result)
            elif ra == "ERROR":
                result.errors.append(trade_result)
            elif ra in ("SKIP", "EXCLUDED", "REJECTED"):
                result.rejected.append(trade_result)
            else:
                result.rejected.append(trade_result or {
                    "action": "REJECTED", "symbol": symbol,
                    "reason": "execute_trade returned empty result",
                })
        return result

    def record_outcome(self, ctx, prediction_id: int,
                        outcome: Outcome) -> None:
        """Write a resolved stock prediction with pipeline_kind='stock'.
        Phase 5 of the pipeline refactor — closes audit finding #2 by
        construction (downstream aggregations filter by tag, not
        signal type, so option outcomes can never pool with stock).
        Returns at stock scale (the existing behavior — stocks set
        the baseline)."""
        from .outcomes import stock as stock_outcomes
        db_path = getattr(ctx, "db_path", None)
        if not db_path:
            return
        stock_outcomes.record(db_path, prediction_id, outcome)

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
