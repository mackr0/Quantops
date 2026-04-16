"""Event handlers — react to events emitted by `event_detectors`.

Handlers are plain callables of shape `handler(event_row: dict, ctx) -> dict`
registered via `event_bus.subscribe()`. The default wiring (see
`register_default_handlers`) runs at scheduler startup; future work can
add more specialized handlers per event type.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def handler_log_activity(event: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """Append a human-readable row to the profile activity feed."""
    from multi_scheduler import _safe_log_activity  # local import — avoids circular

    symbol = event.get("symbol") or "-"
    payload = event.get("payload", {}) or {}
    ev_type = event["type"]
    severity = event.get("severity", "info")

    from display_names import display_name

    # Convert internal strategy identifier to a human-readable label when
    # it appears in the title/detail (e.g., "market_engine" → "Market
    # Structure Engine").
    strat_internal = payload.get("strategy", "")
    strat_label = display_name(strat_internal) if strat_internal else ""

    title_map = {
        "sec_filing_detected": f"SEC filing alert: {symbol} ({payload.get('form_type', '')})",
        "earnings_imminent":   f"Earnings imminent: {symbol} in {payload.get('days_until', 0)}d",
        "price_shock":         f"Price shock: {symbol} {payload.get('move_pct', 0):+.1f}%",
        "prediction_big_winner": f"Big winner: {symbol} +{payload.get('return_pct', 0):.1f}%",
        "prediction_big_loser":  f"Big loser: {symbol} {payload.get('return_pct', 0):.1f}%",
        "strategy_deprecated":   f"Strategy deprecated: {strat_label or '?'}",
    }
    title = title_map.get(ev_type, f"{display_name(ev_type)}: {symbol}")

    detail = payload.get("summary") or payload.get("signal") or ""
    if not detail and "return_pct" in payload:
        detail = f"Strategy: {strat_label or '?'}"

    try:
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), getattr(ctx, "user_id", 0),
            f"event_{ev_type}", title, str(detail)[:500],
        )
        return {"logged": True, "severity": severity}
    except Exception as exc:
        return {"logged": False, "error": str(exc)}


def handler_fire_ensemble(event: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """Run the specialist ensemble on the event's symbol for fast analysis.

    Only fires for event types that warrant a reactive AI analysis (SEC
    filings, price shocks). For other types (earnings imminent, big
    winner/loser) the existing polling pipeline will pick them up on
    the next scan — we don't need to spend AI calls here.
    """
    ev_type = event["type"]
    if ev_type not in ("sec_filing_detected", "price_shock"):
        return {"skipped": f"no ensemble trigger for {ev_type}"}

    symbol = event.get("symbol")
    if not symbol:
        return {"skipped": "no symbol"}

    try:
        from ensemble import run_ensemble
    except Exception as exc:
        return {"error": f"ensemble import failed: {exc}"}

    # Build a minimal candidate dict — the specialist prompts need at
    # least symbol, signal, price, reason. We can't backfill indicators
    # without a DB hit, so we keep it spare and let specialists work
    # from the context they can see.
    candidate = {
        "symbol": symbol,
        "signal": "REACTIVE",
        "price": 0,
        "reason": f"Event-driven reaction: {ev_type} — {event.get('payload', {})}",
    }

    try:
        result = run_ensemble(
            [candidate], ctx,
            ai_provider=getattr(ctx, "ai_provider", "anthropic"),
            ai_model=getattr(ctx, "ai_model", "claude-haiku-4-5-20251001"),
            ai_api_key=getattr(ctx, "ai_api_key", ""),
        )
    except Exception as exc:
        return {"error": f"ensemble call failed: {exc}"}

    entry = (result.get("per_symbol") or {}).get(symbol) or {}
    return {
        "ensemble_verdict": entry.get("verdict", "HOLD"),
        "ensemble_confidence": entry.get("confidence", 0),
        "vetoed": entry.get("vetoed", False),
        "cost_calls": result.get("cost_calls", 0),
    }


def register_default_handlers() -> None:
    """Subscribe all default handlers to every event type. Idempotent."""
    from event_bus import clear_subscriptions, subscribe
    from event_detectors import ALL_EVENT_TYPES

    clear_subscriptions()
    # Activity logging runs on every event — cheap and always useful
    subscribe(handler_log_activity, ALL_EVENT_TYPES)
    # Ensemble reaction is cost-gated to the two types where real-time
    # specialist analysis matters the most
    subscribe(handler_fire_ensemble, ("sec_filing_detected", "price_shock"))
