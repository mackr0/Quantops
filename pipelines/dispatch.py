"""Cutover dispatcher for Pipeline.run_cycle.

Scope C cutover (gated). Same call-signature as
`trade_pipeline.run_trade_cycle(candidates, ctx=ctx)` — returns the
same `summary` dict keys — but the work is done by iterating over
`get_pipelines_for_profile(ctx)` and invoking each pipeline's
`run_cycle(ctx)`.

Gating: `ctx.use_pipeline_dispatch`. Default OFF. When the legacy
behavior is unchanged. When ON the scheduler calls THIS dispatcher
in place of `run_trade_cycle`. Soak the shadow harness first; flip
this flag only after verdict-layer agreement ≥ 95% for 1–2 trading
days.

CRITICAL CONTRACT: must NEVER be invoked alongside legacy
`run_trade_cycle` for the same cycle — that would execute every
trade twice. The scheduler's call-site is an if/else, not both.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _normalize_shortlist(candidates: Any) -> List[Dict[str, Any]]:
    """Pipelines read `ctx.shortlist` as a list of dicts; the scheduler
    passes a list of bare ticker strings. Convert."""
    if not candidates:
        return []
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates
    return [{"symbol": s} for s in candidates if s]


def _bucket_action(action: str, summary: Dict[str, int]) -> None:
    """Map ExecutionResult.submitted entries to legacy summary keys.

    Legacy keys: buys / sells / shorts. We collapse:
      - BUY-like (BUY/STRONG_BUY/WEAK_BUY/OPTIONS/MULTILEG_OPEN) → buys
      - SELL-like (SELL/STRONG_SELL/WEAK_SELL/COVER) → sells
      - SHORT → shorts

    OPTIONS/MULTILEG_OPEN are bucketed as buys because they open new
    positions (capital outflow / risk-on), mirroring the legacy
    summarizer's classification.
    """
    a = (action or "").upper()
    if a in ("BUY", "STRONG_BUY", "WEAK_BUY", "OPTIONS", "MULTILEG_OPEN"):
        summary["buys"] += 1
    elif a in ("SELL", "STRONG_SELL", "WEAK_SELL", "COVER"):
        summary["sells"] += 1
    elif a == "SHORT":
        summary["shorts"] += 1


def run_via_pipelines(candidates: Any, ctx: Any) -> Dict[str, Any]:
    """Alternative to `trade_pipeline.run_trade_cycle` — dispatches
    through `Pipeline.run_cycle` for each enabled pipeline.

    Returns a summary dict with the SAME keys the legacy function
    returns, so callers (scheduler, dashboard activity log) don't
    need a separate code path.
    """
    from pipelines.registry import get_pipelines_for_profile

    # Pipelines read ctx.shortlist; scheduler hands us bare symbols.
    try:
        ctx.shortlist = _normalize_shortlist(candidates)
    except Exception:
        # SimpleNamespace from tests — set directly via __dict__ as fallback
        try:
            object.__setattr__(ctx, "shortlist", _normalize_shortlist(candidates))
        except Exception as _ctx_exc:
            # ctx doesn't accept shortlist (frozen dataclass, namespace
            # of weird type) — pipelines tolerate missing shortlist via
            # getattr default. Surface for follow-up so we don't silently
            # run on a broken candidate set.
            logger.warning(
                "ctx.shortlist set failed both ways (%s); pipelines "
                "will see empty shortlist this cycle",
                type(_ctx_exc).__name__,
            )

    summary: Dict[str, Any] = {
        "total": len(candidates) if candidates else 0,
        "buys": 0,
        "sells": 0,
        "shorts": 0,
        "holds": 0,
        "skips": 0,
        "ai_vetoed": 0,
        "errors": 0,
        "pre_filtered": 0,
        "sent_to_ai": 0,
        "details": [],
        "vetoed_details": [],
        "ai_reasoning": "",
        # Marker so downstream code (logging / dashboards) can tell
        # which dispatcher produced this row. Legacy callers ignore
        # unknown keys.
        "dispatch": "pipeline",
    }

    pipelines = get_pipelines_for_profile(ctx)
    if not pipelines:
        return summary

    for pipeline in pipelines:
        try:
            result = pipeline.run_cycle(ctx)
        except Exception as exc:
            logger.exception(
                "[%s] pipeline=%s run_cycle crashed: %s: %s",
                getattr(ctx, "display_name", "?"),
                getattr(pipeline, "name", type(pipeline).__name__),
                type(exc).__name__, exc,
            )
            summary["errors"] += 1
            continue

        # If decide() returned proposals at all, this pipeline did
        # call the AI — count one ai-call (legacy reports 0/1 in a
        # combined cycle; we report the number of pipeline AI calls).
        # `sent_to_ai` in legacy is bool-shaped (0 or 1); on the
        # pipeline path we accumulate to expose how many pipelines
        # actually ran. Downstream logging just stringifies it so this
        # is non-breaking.
        if result.submitted or result.skipped or result.rejected:
            summary["sent_to_ai"] += 1

        for tr in (result.submitted or []):
            if not isinstance(tr, dict):
                continue
            _bucket_action(tr.get("action", ""), summary)
            summary["details"].append(tr)

        # `skipped` = gate / specialist refusal — the legacy summary
        # calls these ai_vetoed (well, specialist-vetoed surfaces in
        # vetoed_details). Keep it simple: count as ai_vetoed +
        # surface in vetoed_details.
        for sk in (result.skipped or []):
            if not isinstance(sk, dict):
                continue
            summary["ai_vetoed"] += 1
            summary["vetoed_details"].append(sk)

        # `rejected` = broker refusal — bucket as errors so the
        # dashboard's error counter surfaces them.
        summary["errors"] += len(result.rejected or [])
        # `errors` = exceptions during execute — straight through.
        summary["errors"] += len(result.errors or [])

    return summary
