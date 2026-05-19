"""Pipeline shadow eval — Scope C of the per-pipeline refactor.

Full per-layer comparison of the new `Pipeline.run_cycle` dispatch
path against the legacy `trade_pipeline.run_trade_cycle`. Runs at
the end of each legacy cycle when `ctx.enable_pipeline_shadow_eval`
is set; writes one row to `pipeline_shadow_runs` per cycle.

CRITICAL CONTRACTS (load-bearing — see
`feedback_no_orphan_broker_fills` memory):
- Shadow code MUST NEVER affect the live trade flow. Every call is
  wrapped in try/except. A bug in shadow code must not prevent
  legacy from recording trades or returning its result.
- Shadow code MUST NOT submit orders to the broker. It calls
  generate_candidates → build_prompt → decide → route_to_specialists
  on the pipeline path, but STOPS before execute(). The "would
  have submitted" set is verdict.approved — no broker calls.
- Shadow code makes its OWN AI call (one per pipeline that has
  candidates). Cost ~$0.01-0.02 per cycle per shadow-enabled
  profile on gemini-2.5-flash-lite. Operator turns shadow on
  per-profile during soak; off after cutover.

What gets compared (per cycle, per profile):

  Layer 1 — Candidates
    legacy:    `_build_candidates_data` shortlist (combined)
    pipeline:  StockPipeline.generate_candidates + OptionPipeline.generate_candidates
    diff:      which symbols each path produced; per-pipeline-class split

  Layer 2 — Prompt
    legacy:    one combined `_build_batch_prompt`
    pipeline:  two separate prompts (stock_prompt.build_prompt + option_prompt.build_prompt)
    diff:      sha256 digest + char length per prompt (full prompts too big to log every cycle)

  Layer 3 — AI proposals
    legacy:    one `call_ai` returning mixed BUY/SELL/MULTILEG_OPEN/OPTIONS
    pipeline:  two separate calls; each pipeline's `decide()` filters returned trades to its own action set
    diff:      symbol set, action mismatches

  Layer 4 — Specialist verdict
    legacy:    `check_multileg_specialist_veto` per multileg + ensemble per stock
    pipeline:  unified `Pipeline.route_to_specialists` per pipeline (same specialist set, filtered by name)
    diff:      per-symbol approved/vetoed mismatches

After soak with high agreement (target >95% on verdict layer for
N cycles), the scheduler can safely cut over from legacy to
pipeline dispatch. If disagreement appears, the per-layer diff
points at exactly which layer needs investigation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_STOCK_ACTIONS = {"BUY", "SELL", "SHORT", "COVER",
                   "STRONG_BUY", "STRONG_SELL", "WEAK_BUY",
                   "WEAK_SELL", "PAIR_TRADE"}
_OPTION_ACTIONS = {"OPTIONS", "MULTILEG_OPEN"}


def _enabled_for(ctx: Any) -> bool:
    """Per-profile opt-in OR global env override. Default OFF."""
    if getattr(ctx, "enable_pipeline_shadow_eval", False):
        return True
    return os.getenv("AI_PIPELINE_SHADOW_EVAL", "").strip() == "1"


def _digest(text: str) -> Tuple[str, int]:
    """Return (sha256_hex, char_length) for a prompt — store digest
    instead of full text to keep the table compact across many
    cycles."""
    if not text:
        return ("", 0)
    h = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    return (h, len(text))


def _classify_legacy_details(details: List[Dict]) -> Dict[str, str]:
    """Walk legacy `details` → {symbol: verdict}. See module docstring
    for verdict meanings."""
    out: Dict[str, str] = {}
    for d in details or []:
        if not isinstance(d, dict):
            continue
        sym = d.get("symbol", "")
        if not sym:
            continue
        action = (d.get("action") or "").upper()
        if action in ("BUY", "SELL", "SHORT", "COVER",
                       "OPTIONS", "MULTILEG_OPEN"):
            out[sym] = "submitted"
        elif action in ("SPECIALIST_VETOED", "AI_VETOED",
                         "EARNINGS_SKIP", "COOLDOWN"):
            out[sym] = "vetoed"
        elif action in ("SKIP", "EXCLUDED", "REJECTED", "ERROR"):
            out[sym] = "rejected"
        else:
            out[sym] = "unknown"
    return out


def _diff_symbol_sets(legacy: List[str],
                       pipeline: List[str]) -> Dict[str, Any]:
    """Set diff between two lists of symbols → JSON-serialisable."""
    l = set(legacy or [])
    p = set(pipeline or [])
    return {
        "only_in_legacy": sorted(l - p),
        "only_in_pipeline": sorted(p - l),
        "in_both": sorted(l & p),
        "legacy_count": len(l),
        "pipeline_count": len(p),
    }


def _diff_verdicts(legacy: Dict[str, str],
                    pipeline: Dict[str, str]) -> Dict[str, Any]:
    """Per-symbol verdict diff. Captures three failure modes:
    only-in-legacy, only-in-pipeline, and same-symbol-different-verdict."""
    l_syms = set(legacy.keys())
    p_syms = set(pipeline.keys())
    in_both = l_syms & p_syms
    mismatches = {
        s: {"legacy": legacy[s], "pipeline": pipeline[s]}
        for s in sorted(in_both) if legacy[s] != pipeline[s]
    }
    return {
        "only_in_legacy": sorted(l_syms - p_syms),
        "only_in_pipeline": sorted(p_syms - l_syms),
        "mismatches": mismatches,
        "total_in_both": len(in_both),
        "agreement_pct": (
            round((1 - len(mismatches) / max(1, len(in_both))) * 100.0, 1)
            if in_both else None
        ),
    }


def shadow_compare(ctx: Any,
                    shortlist: List[Dict],
                    legacy_prompt: str,
                    legacy_ai_proposals: List[Dict],
                    legacy_details: List[Dict],
                    cycle_id: Optional[str] = None) -> None:
    """Run the full per-layer shadow comparison.

    Args:
      ctx: per-profile UserContext for this cycle (carries db_path,
        ai keys, asset-class flags).
      shortlist: the screener's shortlist of symbol dicts that
        seeded the cycle. Pipeline's `generate_candidates` reads
        this from `ctx.shortlist` so we set it on the ctx before
        invoking.
      legacy_prompt: the prompt the legacy `ai_select_trades` sent.
      legacy_ai_proposals: legacy AI response's `trades` list AFTER
        `_validate_ai_trades` filtering.
      legacy_details: the per-symbol outcomes the legacy path built
        (used to classify legacy verdicts at the specialist layer).
      cycle_id: optional unique identifier for this cycle for future
        cross-system correlation.

    Returns: None. Writes one row to `pipeline_shadow_runs` (success
    or failure). Never raises — every exception is caught + logged.
    """
    if not _enabled_for(ctx):
        return
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return

    start = time.time()

    # Default values for the row; populated as we go.
    payload: Dict[str, Any] = {
        "cycle_id": cycle_id,
        "profile_id": getattr(ctx, "profile_id", None),
        # Layer 1 — candidates
        "legacy_candidates_count": len(shortlist or []),
        "legacy_candidates_symbols": [
            s.get("symbol", "") for s in (shortlist or []) if isinstance(s, dict)
        ],
        "pipeline_stock_candidates_count": 0,
        "pipeline_stock_candidates_symbols": [],
        "pipeline_option_candidates_count": 0,
        "pipeline_option_candidates_symbols": [],
        "candidates_diff": {},
        # Layer 2 — prompt
        "legacy_prompt_digest": "",
        "legacy_prompt_length": 0,
        "pipeline_stock_prompt_digest": "",
        "pipeline_stock_prompt_length": 0,
        "pipeline_option_prompt_digest": "",
        "pipeline_option_prompt_length": 0,
        # Layer 3 — AI proposals
        "legacy_proposal_count": len(legacy_ai_proposals or []),
        "legacy_proposal_symbols": [
            p.get("symbol", "") for p in (legacy_ai_proposals or []) if isinstance(p, dict)
        ],
        "pipeline_proposal_count": 0,
        "pipeline_proposal_symbols": [],
        "proposals_diff": {},
        # Layer 4 — specialist verdict
        "legacy_approved_count": 0,
        "legacy_vetoed_count": 0,
        "pipeline_approved_count": 0,
        "pipeline_vetoed_count": 0,
        "verdict_diff": {},
        # Aggregate
        "layers_with_divergence": 0,
        "agreement_pct": None,
        # Cost / timing / status
        "shadow_ai_cost_usd": 0.0,
        "duration_ms": 0.0,
        "success": 1,
        "error_message": None,
    }

    try:
        from pipelines import AIResult
        from pipelines.stock import StockPipeline
        from pipelines.option import OptionPipeline

        # The pipeline classes read ctx.shortlist; set it from the
        # shortlist the legacy path used so both paths see the same
        # input.
        try:
            ctx.shortlist = shortlist
        except Exception:
            # SimpleNamespace from tests — fall through; pipelines
            # tolerate missing shortlist via getattr default.
            pass

        legacy_dig, legacy_len = _digest(legacy_prompt or "")
        payload["legacy_prompt_digest"] = legacy_dig
        payload["legacy_prompt_length"] = legacy_len

        stock_pipe = StockPipeline()
        opt_pipe = OptionPipeline()

        # ─── Layer 1: candidates ────────────────────────────────
        stock_cands = stock_pipe.generate_candidates(ctx) or []
        opt_cands = opt_pipe.generate_candidates(ctx) or []
        payload["pipeline_stock_candidates_count"] = len(stock_cands)
        payload["pipeline_stock_candidates_symbols"] = [
            c.symbol for c in stock_cands
        ]
        payload["pipeline_option_candidates_count"] = len(opt_cands)
        payload["pipeline_option_candidates_symbols"] = [
            c.symbol for c in opt_cands
        ]
        legacy_syms = payload["legacy_candidates_symbols"]
        pipeline_syms = (
            payload["pipeline_stock_candidates_symbols"]
            + payload["pipeline_option_candidates_symbols"]
        )
        payload["candidates_diff"] = _diff_symbol_sets(
            legacy_syms, pipeline_syms,
        )

        # ─── Layer 2: prompts ───────────────────────────────────
        stock_prompt = ""
        opt_prompt = ""
        if stock_cands:
            try:
                stock_prompt = stock_pipe.build_prompt(ctx, stock_cands)
            except Exception as exc:
                logger.warning("shadow: stock build_prompt failed: %s", exc)
        if opt_cands:
            try:
                opt_prompt = opt_pipe.build_prompt(ctx, opt_cands)
            except Exception as exc:
                logger.warning("shadow: option build_prompt failed: %s", exc)
        sd, sl = _digest(stock_prompt)
        od, ol = _digest(opt_prompt)
        payload["pipeline_stock_prompt_digest"] = sd
        payload["pipeline_stock_prompt_length"] = sl
        payload["pipeline_option_prompt_digest"] = od
        payload["pipeline_option_prompt_length"] = ol

        # ─── Layer 3: AI decision (separate calls — extra cost) ──
        # Each pipeline's `decide()` makes its own call_ai. Cost is
        # logged to the existing ai_cost_ledger via call_ai's
        # db_path → log_ai_call path; we surface the total here too.
        cost_before = _cost_today(db_path)
        stock_result = AIResult(proposals=[])
        opt_result = AIResult(proposals=[])
        if stock_cands:
            try:
                stock_result = stock_pipe.decide(ctx, stock_prompt)
            except Exception as exc:
                logger.warning("shadow: stock decide failed: %s", exc)
        if opt_cands:
            try:
                opt_result = opt_pipe.decide(ctx, opt_prompt)
            except Exception as exc:
                logger.warning("shadow: option decide failed: %s", exc)
        cost_after = _cost_today(db_path)
        payload["shadow_ai_cost_usd"] = round(
            max(0.0, cost_after - cost_before), 6,
        )
        pipeline_proposals = (
            list(stock_result.proposals) + list(opt_result.proposals)
        )
        payload["pipeline_proposal_count"] = len(pipeline_proposals)
        payload["pipeline_proposal_symbols"] = [
            p.get("symbol", "") for p in pipeline_proposals
            if isinstance(p, dict)
        ]
        payload["proposals_diff"] = _diff_symbol_sets(
            payload["legacy_proposal_symbols"],
            payload["pipeline_proposal_symbols"],
        )

        # ─── Layer 4: specialist verdict ────────────────────────
        stock_verdict = stock_pipe.route_to_specialists(ctx, stock_result)
        opt_verdict = opt_pipe.route_to_specialists(ctx, opt_result)
        pipeline_by_symbol: Dict[str, str] = {}
        for p in (stock_verdict.approved + opt_verdict.approved):
            sym = p.get("symbol") if isinstance(p, dict) else None
            if sym:
                pipeline_by_symbol[sym] = "submitted"
        for p in (stock_verdict.vetoed + opt_verdict.vetoed):
            sym = p.get("symbol") if isinstance(p, dict) else None
            if sym:
                pipeline_by_symbol[sym] = "vetoed"
        legacy_by_symbol = _classify_legacy_details(legacy_details)
        payload["legacy_approved_count"] = sum(
            1 for v in legacy_by_symbol.values() if v == "submitted"
        )
        payload["legacy_vetoed_count"] = sum(
            1 for v in legacy_by_symbol.values() if v == "vetoed"
        )
        payload["pipeline_approved_count"] = (
            len(stock_verdict.approved) + len(opt_verdict.approved)
        )
        payload["pipeline_vetoed_count"] = (
            len(stock_verdict.vetoed) + len(opt_verdict.vetoed)
        )
        payload["verdict_diff"] = _diff_verdicts(
            legacy_by_symbol, pipeline_by_symbol,
        )
        payload["agreement_pct"] = payload["verdict_diff"].get(
            "agreement_pct"
        )

        # ─── Aggregate: how many layers diverged ────────────────
        divergent = 0
        if payload["candidates_diff"].get("only_in_legacy") or \
           payload["candidates_diff"].get("only_in_pipeline"):
            divergent += 1
        if payload["legacy_prompt_digest"] != payload[
            "pipeline_stock_prompt_digest"
        ] and payload["legacy_prompt_digest"]:
            # Prompts WILL always differ by design (combined vs split)
            # — only flag when both are non-empty and they don't match
            # the structural expectation. For now record as divergent
            # to surface in the row; operator interprets via
            # prompt-length deltas.
            divergent += 1
        if payload["proposals_diff"].get("only_in_legacy") or \
           payload["proposals_diff"].get("only_in_pipeline"):
            divergent += 1
        if payload["verdict_diff"].get("mismatches") or \
           payload["verdict_diff"].get("only_in_legacy") or \
           payload["verdict_diff"].get("only_in_pipeline"):
            divergent += 1
        payload["layers_with_divergence"] = divergent

    except Exception as exc:
        payload["success"] = 0
        payload["error_message"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "pipeline shadow_compare crashed (non-fatal, legacy "
            "flow unaffected): %s", payload["error_message"],
        )

    payload["duration_ms"] = round((time.time() - start) * 1000.0, 2)
    _write_row(db_path, payload)


def _cost_today(db_path: str) -> float:
    """Sum today's ai_cost_ledger entries for this DB. Used to
    measure how much extra spend the shadow AI calls added in this
    cycle."""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM "
                "ai_cost_ledger WHERE date(timestamp) = date('now')"
            ).fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def _write_row(db_path: str, p: Dict[str, Any]) -> None:
    """Insert one row into pipeline_shadow_runs. Wrapped in try so
    even a write failure is non-fatal to the legacy flow."""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute(
                    """INSERT INTO pipeline_shadow_runs (
                        cycle_id,
                        legacy_proposal_count, pipeline_proposal_count,
                        legacy_approved_count, pipeline_approved_count,
                        legacy_vetoed_count, pipeline_vetoed_count,
                        legacy_symbols, pipeline_symbols,
                        symbols_diff, verdict_diff,
                        duration_ms, success, error_message
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        p.get("cycle_id"),
                        p["legacy_proposal_count"],
                        p["pipeline_proposal_count"],
                        p["legacy_approved_count"],
                        p["pipeline_approved_count"],
                        p["legacy_vetoed_count"],
                        p["pipeline_vetoed_count"],
                        json.dumps(p["legacy_proposal_symbols"]),
                        json.dumps(p["pipeline_proposal_symbols"]),
                        json.dumps({
                            "candidates": p["candidates_diff"],
                            "proposals": p["proposals_diff"],
                            "prompt_layer": {
                                "legacy_digest": p["legacy_prompt_digest"],
                                "legacy_length": p["legacy_prompt_length"],
                                "pipeline_stock_digest": p["pipeline_stock_prompt_digest"],
                                "pipeline_stock_length": p["pipeline_stock_prompt_length"],
                                "pipeline_option_digest": p["pipeline_option_prompt_digest"],
                                "pipeline_option_length": p["pipeline_option_prompt_length"],
                            },
                            "aggregate": {
                                "layers_with_divergence": p["layers_with_divergence"],
                                "agreement_pct": p["agreement_pct"],
                                "shadow_ai_cost_usd": p["shadow_ai_cost_usd"],
                            },
                        }),
                        json.dumps(p["verdict_diff"]),
                        p["duration_ms"],
                        p["success"],
                        p["error_message"],
                    ),
                )
    except Exception as exc:
        logger.warning(
            "pipeline shadow_compare row insert failed: %s: %s",
            type(exc).__name__, exc,
        )
