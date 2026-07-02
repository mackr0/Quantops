"""OptionPipeline — full instrument-class pipeline for options.

End-state methods now implemented (2026-05-19):
- Owns option-aware candidate generation (IV rank + multileg
  strategy enumeration via options_strategy_advisor).
- Owns option-aware AI decision (call_ai with the option_prompt,
  tolerant parsing, filtered to MULTILEG_OPEN/OPTIONS proposals).
- Owns option-specific specialists with veto authority — fixes
  audit findings #5, #6.
- Owns option execution (multileg + single-leg) — Phase 4c.
- Stores option outcomes at the right scale — fixes audit finding #2.
- Computes option metrics in $ not %, eliminating the 1130%
  slippage display by construction — fixes TODO #8.
- Tunes option-specific parameters (max spread loss, DTE floor,
  IV bands) — fixes audit finding #3.

The scheduler dispatcher today still uses the legacy
trade_pipeline.run_trade_cycle path for both pipelines; OptionPipeline
is now end-to-end runnable via .run_cycle(ctx) for the eventual
cutover and for tests.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date as _date
from typing import Any, Dict, List

from . import (AIResult, Candidate, ExecutionResult, Metrics,
               Outcome, ParameterAdjustments, Pipeline,
               SpecialistVerdict)

logger = logging.getLogger(__name__)


# Module-level helpers used by generate_candidates to enrich each
# Candidate's `extra` dict with option-specific context.

def _days_to_expiry(expiry_str):
    """Parse an ISO date (YYYY-MM-DD) and return days from today.
    Returns None on parse failure rather than raising — this is
    enrichment, not load-bearing logic."""
    if not expiry_str:
        return None
    try:
        y, m, d = str(expiry_str).split("-")
        target = _date(int(y), int(m), int(d))
        return (target - _date.today()).days
    except (ValueError, AttributeError):
        return None


def _strike_summary(strikes):
    """Render a multileg strikes dict as a short readable string for
    the AI prompt context. Examples:
      {"short": 145, "long": 140}                     → "S145/L140"
      {"put_short":140,"put_long":135,"call_short":150,"call_long":155}
                                                         → "P140/135 C150/155"
    Returns None on empty / malformed input."""
    if not isinstance(strikes, dict) or not strikes:
        return None
    # Vertical / strangle / generic
    if "short" in strikes and "long" in strikes:
        return f"S{strikes['short']}/L{strikes['long']}"
    if "put" in strikes and "call" in strikes:
        return f"P{strikes['put']}/C{strikes['call']}"
    # Iron condor — pair both legs
    if all(k in strikes for k in
           ("put_short", "put_long", "call_short", "call_long")):
        return (
            f"P{strikes['put_short']}/{strikes['put_long']} "
            f"C{strikes['call_short']}/{strikes['call_long']}"
        )
    # Unknown shape — render as-is
    try:
        return ", ".join(f"{k}={v}" for k, v in strikes.items())
    except Exception:
        return None


class OptionPipeline(Pipeline):
    name = "option"

    def applies_to(self, ctx) -> bool:
        """Every active profile evaluates options today (the current
        ai_analyst flow proposes multileg trades opportunistically
        regardless of profile flag). Future: profiles can opt out
        via `ctx.disable_options = True`."""
        return not getattr(ctx, "disable_options", False)

    def generate_candidates(self, ctx) -> List[Candidate]:
        """Build option candidates from the per-cycle shortlist.

        Reads `ctx.shortlist` — list of signal dicts with at least
        symbol/signal/price, the same shape `trade_pipeline._build_candidates_data`
        consumes upstream. For each, fetches IV rank via options_oracle
        and emits one Candidate per multileg strategy
        `options_strategy_advisor.evaluate_candidate_for_multileg`
        returns.

        Candidates without options or without an IV rank are skipped
        (can't reason about premium without IV). Top-N by score
        (IV rank, signal-strength tiebreaker) controlled by
        `ctx.option_candidate_top_n` (default 10).

        Fail-soft on per-symbol failures: a single oracle outage
        doesn't kill the cycle.
        """
        shortlist = list(getattr(ctx, "shortlist", None) or [])
        if not shortlist:
            return []

        from options_oracle import get_options_oracle
        from options_strategy_advisor import (
            evaluate_candidate_for_multileg,
            _own_book_held_underlyings,
        )

        regime = getattr(ctx, "market_regime", None)
        # Own-book held set, computed ONCE (isolation-safe — this profile's
        # own book only). Suppresses spreads on already-held underlyings so
        # the pipeline never emits the redundant proposals the
        # adversarial_reviewer vetoes every cycle. Mirrors the live path.
        held = _own_book_held_underlyings(ctx)
        out: List[Candidate] = []
        for signal in shortlist:
            if not isinstance(signal, dict):
                continue
            symbol = signal.get("symbol")
            if not symbol:
                continue
            try:
                price = float(signal.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            try:
                oracle = get_options_oracle(symbol)
            except Exception as exc:
                # Per-symbol oracle failure: log and skip; the rest
                # of the shortlist still gets evaluated. The next
                # cycle will retry.
                logger.warning(
                    "OptionPipeline.generate_candidates: %s oracle "
                    "failed: %s: %s",
                    symbol, type(exc).__name__, exc,
                )
                continue
            if not oracle or not oracle.get("has_options"):
                continue
            iv_rank_pct = (oracle.get("iv_rank") or {}).get("rank_pct")
            if iv_rank_pct is None:
                continue

            recs = evaluate_candidate_for_multileg(
                {
                    "symbol": symbol,
                    "signal": signal.get("signal", ""),
                    "price": price,
                    "volatility_view": signal.get("volatility_view"),
                },
                iv_rank_pct=iv_rank_pct,
                regime=regime,
                ctx=ctx,
                held=held,
            ) or []
            if not recs:
                continue

            base_score = float(iv_rank_pct) / 100.0
            signal_str = (signal.get("signal") or "").upper()
            if signal_str in ("STRONG_BUY", "STRONG_SELL"):
                base_score += 0.10
            elif signal_str in ("BUY", "SELL", "SHORT"):
                base_score += 0.05

            for rec in recs:
                expiry = rec.get("expiry")
                extra: Dict[str, Any] = {
                    "iv_rank": iv_rank_pct,
                    "dte": _days_to_expiry(expiry),
                    "strike": _strike_summary(rec.get("strikes") or {}),
                    "option_strategy": rec.get("strategy"),
                    "rationale": (rec.get("rationale") or "")[:240],
                    "underlying_signal": signal_str,
                    "underlying_score": signal.get("score"),
                }
                # Carry through select underlying technicals when
                # the shortlist row already has them.
                for k in ("rsi", "adx", "atr", "volume_ratio",
                          "pct_from_vwap"):
                    if k in signal:
                        extra[k] = signal[k]
                out.append(Candidate(
                    symbol=symbol,
                    score=base_score,
                    signal="MULTILEG_OPEN",
                    price=price,
                    extra=extra,
                ))

        out.sort(key=lambda c: c.score, reverse=True)
        top_n = int(getattr(ctx, "option_candidate_top_n", 10) or 10)
        return out[:top_n]

    def build_prompt(self, ctx, candidates: List[Candidate]) -> str:
        """Option-aware AI prompt — delegates to the per-pipeline
        builder which surfaces IV rank, Greeks, DTE, strike, and
        spread economics alongside the underlying's technicals.
        Closes audit finding #4 by construction. Phase 3."""
        from . import option_prompt
        return option_prompt.build_prompt(ctx, candidates)

    def decide(self, ctx, prompt: str) -> AIResult:
        """Send the option-aware prompt to the AI provider and parse
        proposals. Mirrors `ai_analyst.ai_select_trades`'s call shape
        (call_ai + _parse_ai_response_tolerant + JSON salvage) so
        truncated responses still surface partial trade lists.

        Filters returned trades to option actions (MULTILEG_OPEN /
        OPTIONS). Stock proposals from the same prompt — if the AI
        misfires — are dropped here so they don't reach the option
        veto layer (which would always pass them).

        Fail-soft: cost-cap and provider errors return an empty
        AIResult with the failure reason; the cycle short-circuits
        cleanly rather than crashing.
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
                purpose="option_pipeline_decide",
            )
            parsed = _parse_ai_response_tolerant(raw)
        except CostCapExceeded as exc:
            logger.warning(
                "OptionPipeline.decide: cost cap blocked call: %s", exc,
            )
            return AIResult(
                proposals=[],
                reasoning=f"Cost cap reached: {exc}",
                raw_response={"cost_capped": True},
            )
        except Exception as exc:
            logger.error(
                "OptionPipeline.decide: AI call/parse failed: %s: %s "
                "— raw[:200]=%r",
                type(exc).__name__, exc, (raw or "")[:200],
            )
            return AIResult(
                proposals=[],
                reasoning=f"AI call failed: {exc}",
            )

        proposals: List[dict] = []
        confidences: List[float] = []
        for trade in (parsed.get("trades") or []):
            if not isinstance(trade, dict):
                continue
            action = (trade.get("action") or "").upper()
            if action not in ("MULTILEG_OPEN", "OPTIONS"):
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
            # P3 feedback: record the vetoed proposal keyed by (spread
            # strategy x sector) so the ledger can discount doomed spreads.
            # ASYNC (2026-07-02 composed-system review): the capture prices the
            # spread's legs (blocking HTTP) — a feedback write must never sit
            # ahead of live order submission for later-ranked trades in the
            # dispatch loop. Capture still happens AT veto time, off-thread.
            if isinstance(vetoed, dict):
                self._record_option_outcome_async(
                    ctx, vetoed, sym, vetoed_flag=1,
                    veto_reason=(f"{vetoed_by}: {veto_reason}"
                                 if vetoed_by else veto_reason))
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
            # P3 feedback: an approved proposal survived the specialist veto —
            # record it (vetoed=0) so P(veto) = vetoed / (vetoed + approved).
            # ASYNC: a cold sector lookup must never delay THIS trade's
            # submission (the order goes to the broker right below).
            self._record_option_outcome_async(ctx, proposal, symbol,
                                              vetoed_flag=0)
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
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                AttributeError, KeyError, OSError, ImportError,
                RuntimeError) as _v_exc:
            # Veto-prediction journal write; veto-state unchanged
            # on log failure. Mocked record_broker_rejection in
            # tests raises RuntimeError("DB locked") — broaden to
            # match real broker-side flakiness too. Surface for follow-up.
            logger.warning(
                "veto-prediction journal write failed: %s: %s",
                type(_v_exc).__name__, _v_exc,
            )

    @staticmethod
    def _price_vetoed_spread(strategy, symbol, proposal):
        """Price a vetoed VERTICAL now so its would-be P&L is resolvable later
        (entry premium + legs + max-loss/gain + breakeven + the two strikes).
        Returns the resolution kwargs for `record_option_proposal_outcome`, or
        {} (all NULL) when the spread can't be priced ACCURATELY — non-vertical
        or untrusted marks. The row then still counts toward P(veto) but stays
        unresolved; we never store an approximation (perfect-data rule)."""
        try:
            import json as _json
            from options_strategy_advisor import _price_option_rec
            strikes = proposal.get("strikes") or {}
            ks = [float(v) for v in strikes.values() if v is not None]
            if len(ks) < 2:
                return {}
            prec = {"strategy": strategy, "symbol": symbol,
                    "expiry": proposal.get("expiry"), "strikes": strikes}
            _price_option_rec(prec)
            if not prec.get("priced"):
                return {}
            legs = prec.get("legs")
            return {
                "entry_net_premium": prec.get("entry_net_premium"),
                "max_loss_per_contract": prec.get("max_loss_per_contract"),
                "max_gain_per_contract": prec.get("max_gain_per_contract"),
                "breakeven": prec.get("breakeven"),
                "lo_strike": min(ks),
                "hi_strike": max(ks),
                "legs_json": _json.dumps(legs) if legs else None,
            }
        except Exception as exc:
            logger.debug("vetoed-spread pricing failed (fields NULL): %s", exc)
            return {}

    @staticmethod
    def _record_option_outcome_async(ctx, proposal, symbol, *, vetoed_flag,
                                     veto_reason=None):
        """Spawn `_record_option_outcome` on a daemon thread so the feedback
        capture (leg pricing / sector lookup — blocking HTTP) NEVER delays live
        order dispatch. Returns the Thread (started) so tests can join it; the
        write itself is fail-open + logged inside `_record_option_outcome`."""
        import copy
        import threading
        # Deep-copy the proposal at spawn: the dispatch loop may mutate it
        # after this returns (e.g. strike snapping during execution), and the
        # recorder must capture the proposal AS VETOED/APPROVED, race-free.
        try:
            snap = copy.deepcopy(proposal) if isinstance(proposal, dict) \
                else proposal
        except Exception as _cp_exc:
            # Fallback still copies the nested strikes dict — the exact field
            # the dispatch loop mutates (strike snapping) — and is logged.
            logger.warning("option-outcome snapshot deepcopy failed (%s: %s); "
                           "using shallow copy + strikes copy",
                           type(_cp_exc).__name__, _cp_exc)
            if isinstance(proposal, dict):
                snap = dict(proposal)
                if isinstance(snap.get("strikes"), dict):
                    snap["strikes"] = dict(snap["strikes"])
            else:
                snap = proposal
        t = threading.Thread(
            target=OptionPipeline._record_option_outcome,
            args=(ctx, snap, symbol),
            kwargs={"vetoed_flag": vetoed_flag, "veto_reason": veto_reason},
            daemon=True, name="option-outcome-recorder",
        )
        t.start()
        return t

    @staticmethod
    def _record_option_outcome(ctx, proposal, symbol, *, vetoed_flag,
                               veto_reason=None):
        """Record ONE option proposal outcome (vetoed_flag 1/0) keyed by the
        SPREAD strategy + sector, for the selection engine's per-(strategy x
        sector) veto-rate discount (P3) and would-be-P&L calibration (P4).
        Vetoed spreads are priced NOW so every row is resolvable (no data gap).
        Own-book (ctx.db_path); fail-open — a feedback write must NEVER affect
        execution."""
        if not ctx or not getattr(ctx, "db_path", None):
            return
        try:
            strategy = (proposal.get("strategy_name")
                        or proposal.get("option_strategy")
                        or proposal.get("action") or "option")
            sym = symbol or proposal.get("symbol", "")
            sector = None
            try:
                from sector_classifier import get_sector
                sector = get_sector(sym) if sym else None
            except Exception as _sec_exc:
                logger.debug("sector lookup failed for %s (sector=None): %s",
                             sym, _sec_exc)
            # Accurate would-be-P&L capture for vetoed spreads only (accepted
            # ones become real trades tracked in ai_predictions/trades).
            fields = (OptionPipeline._price_vetoed_spread(strategy, sym,
                                                          proposal)
                      if vetoed_flag else {})
            from journal import record_option_proposal_outcome
            record_option_proposal_outcome(
                ctx.db_path, symbol=sym, strategy=strategy, sector=sector,
                vetoed=vetoed_flag, veto_reason=veto_reason,
                confidence=proposal.get("confidence"),
                expiry=proposal.get("expiry"), **fields,
            )
        except Exception as exc:
            # Feedback-signal write; execution is unaffected. WARNING (not
            # debug) so a systematic failure of the learning signal is visible.
            logger.warning("option outcome record failed (fail-open): %s: %s",
                           type(exc).__name__, exc)

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

            # 2026-07-01 — fund-grade options CAPITAL-AT-RISK budget gate
            # (the REAL control; the options-delta gate below is now just a
            # wide runaway backstop). A defined-risk spread's risk is its
            # MAX-LOSS, not its delta — so cap the book's aggregate options
            # max-loss at max_options_risk_pct of NAV, the way a real fund
            # sizes a defined-risk book. Own-book only (isolation-safe).
            # Best-effort: any failure is non-blocking so a flaky lookup
            # never blocks a legitimate trade.
            try:
                _risk_pct = getattr(ctx, "max_options_risk_pct", None)
                if _risk_pct is not None and _risk_pct > 0:
                    from client import get_account_info as _gai
                    from journal import (
                        open_options_capital_at_risk as _ocar,
                    )
                    _equity = float((_gai(ctx=ctx) or {}).get("equity") or 0)
                    if _equity <= 0:
                        _equity = float(
                            getattr(ctx, "initial_capital", 0) or 0)
                    # Conservative max-loss even though the execution spec has
                    # no premiums (width×$100 fallback for defined-width
                    # spreads).
                    _proposed_ml = spec.total_max_loss(
                        proposal.get("limit_price"))
                    # A spread we can't assign a max-loss to is width-less and
                    # unpriced — a straddle. long_straddle's real debit is
                    # unknown here and short_straddle is UNCAPPED loss; neither
                    # belongs in a defined-risk capital-at-risk budget, so
                    # refuse rather than admit it at $0.
                    if _proposed_ml <= 0:
                        return {
                            "action": "SKIP", "symbol": symbol,
                            "reason": (
                                f"Options risk budget: cannot size {strategy_name}"
                                " (width-less / unpriced) under a defined-risk "
                                "max-loss budget — refused"
                            ),
                        }
                    _open_ml = _ocar(ctx.db_path)
                    _budget = _risk_pct * _equity
                    if _equity > 0 and (_open_ml + _proposed_ml) > _budget:
                        return {
                            "action": "SKIP", "symbol": symbol,
                            "reason": (
                                f"Options risk budget: open ${_open_ml:,.0f} "
                                f"+ trade ${_proposed_ml:,.0f} > ${_budget:,.0f}"
                                f" ({_risk_pct*100:.0f}% NAV cap)"
                            ),
                        }
            except Exception as _budget_exc:
                logger.debug(
                    "Options risk-budget gate eval failed (non-blocking): "
                    "%s: %s", type(_budget_exc).__name__, _budget_exc,
                )

            # 2026-05-20 (docs/23 / #195 Phase 1): Greeks gate for the
            # multileg path. Mirrors the single-leg gate at
            # options_trader.py:497-540. Aggregates per-leg delta /
            # theta / vega contributions and checks against the
            # profile's greek caps (max_net_options_delta_pct,
            # max_theta_burn_dollars_per_day, max_short_vega_dollars).
            # Closes the regression from #189 where collapsing
            # max_total_positions to stock-only removed the only
            # de-facto cap on multileg leg counts. Best-effort: gate
            # failure is non-blocking (logged debug) so a flaky
            # spot/IV lookup never breaks legitimate execution.
            try:
                from options_greeks_aggregator import (
                    compute_book_greeks, _greek_contribution,
                    _parse_option_position, check_greeks_gates, FALLBACK_IV,
                )
                from market_data import get_bars as _gb_gate
                from datetime import date as _date_gate
                _bars = _gb_gate(symbol, limit=2)
                _spot = (float(_bars["close"].iloc[-1])
                         if _bars is not None and len(_bars) > 0 else None)
                if _spot and _spot > 0:
                    _today = _date_gate.today()
                    _total = {"delta": 0.0, "theta": 0.0, "vega": 0.0}
                    for _leg in spec.legs:
                        _mock = {"occ_symbol": _leg.occ_symbol,
                                 "qty": _leg.signed_qty()}
                        _parsed = _parse_option_position(_mock)
                        if _parsed is None:
                            continue
                        _contrib = _greek_contribution(
                            _parsed, _spot, FALLBACK_IV, today=_today,
                        )
                        if _contrib is None:
                            continue
                        _total["delta"] += _contrib.get("delta", 0)
                        _total["theta"] += _contrib.get("theta", 0)
                        _total["vega"] += _contrib.get("vega", 0)
                    from client import get_positions as _gp
                    _positions_for_gate = _gp(ctx=ctx) or []
                    _book = compute_book_greeks(
                        _positions_for_gate,
                        price_lookup=lambda s: _spot if s == symbol else None,
                        iv_lookup=lambda s: FALLBACK_IV,
                    )
                    _gate = check_greeks_gates(_book, _total, ctx=ctx)
                    if not _gate.get("allowed", True):
                        return {
                            "action": "SKIP", "symbol": symbol,
                            "reason": (
                                f"Greeks gate blocked multileg "
                                f"{strategy_name}: "
                                f"{'; '.join(_gate.get('reasons', []))}"
                            ),
                        }
            except Exception as _gate_exc:
                logger.debug(
                    "Multileg Greeks gate eval failed (non-blocking): "
                    "%s: %s",
                    type(_gate_exc).__name__, _gate_exc,
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
            except (sqlite3.OperationalError, sqlite3.DatabaseError,
                    AttributeError, KeyError, OSError, ImportError,
                    RuntimeError) as _ml_exc:
                # Multileg prediction-to-trade link; trade already
                # executed. Broadened to RuntimeError to handle
                # broker-side or mocked flakiness. Surface for follow-up.
                logger.warning(
                    "multileg prediction-to-trade link failed: %s: %s",
                    type(_ml_exc).__name__, _ml_exc,
                )
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
        migration).

        2026-05-19 (docs/18 item #3): `trade_pipeline.run_trade_cycle`
        now delegates here for `action == "OPTIONS"` instead of
        duplicating the ~37-line body. Bug fixes to single-leg
        option submission now live in one place. The same helper
        is invoked by `OptionPipeline.execute()` when the new
        dispatcher (Scope C) is active."""
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
            except (sqlite3.OperationalError, sqlite3.DatabaseError,
                    AttributeError, KeyError, OSError, ImportError,
                    RuntimeError) as _sl_exc:
                # Single-leg prediction-to-trade link; trade already
                # executed. Broadened to RuntimeError to handle
                # broker-side or mocked flakiness. Surface for follow-up.
                logger.warning(
                    "single-leg prediction-to-trade link failed: %s: %s",
                    type(_sl_exc).__name__, _sl_exc,
                )
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
            # 2026-07-01 — max_net_options_delta_pct REMOVED: the delta gate
            # is retired to a fixed 1.50 backstop, so nothing may tune it
            # (this tuner's old 0.02-0.10 range dragged it straight back into
            # the binding range and re-blocked spreads).
            # The aggregate capital-at-risk budget IS tuned here, within its
            # param_bounds band. (self_tuning._optimize_max_options_risk_pct
            # also nudges it via the shared greek-cap optimizer — both are
            # clamped to 0.10-0.40 and move the same direction on option-bucket
            # P&L, exactly like the theta/vega caps below, so the effect is
            # bounded convergence, not a fight.)
            "max_options_risk_pct":
                (0.10, 0.40, 1.05, 0.95, "float"),
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
