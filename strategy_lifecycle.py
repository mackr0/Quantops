"""Lifecycle controller for auto-generated strategies (Phase 7).

Handles the state transitions:
    proposed → validated → shadow → active → retired

Each auto-generated strategy enters as `proposed` when persisted by the
proposer. The daily scheduler calls `validate_and_promote()` which:
  1. Renders the spec to strategies/<name>.py
  2. Runs the Phase 2 rigorous validation gate
  3. If PASS, transitions to `validated` and then `shadow`
  4. If FAIL, transitions to `retired` with the failure reasons

Shadow strategies run alongside active ones — their `find_candidates`
still returns candidates (so their predictions are recorded by ai_tracker
and alpha_decay can measure their edge) but the trade pipeline marks
them as shadow-only so no capital is actually deployed to their picks.

`promote_matured_shadows()` looks at each shadow strategy's rolling
metrics; once it has enough predictions and a solid Sharpe with no decay
trigger fired, it's promoted to `active`. The opposite — `retire_failed()`
pulls the rug on shadows that never found an edge.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables for the shadow → active promotion decision.
# ---------------------------------------------------------------------------

SHADOW_PROMOTION = {
    "min_shadow_predictions": 50,   # resolved predictions before promotion eligible
    "min_rolling_sharpe": 0.8,      # rolling 30-day Sharpe required to promote
    "max_shadow_days_without_edge": 60,  # if still no edge after this, retire
    "max_active_auto_strategies": 5,     # hard cap on live auto-strategies
}


# ---------------------------------------------------------------------------
# Validation gate wrapper
# ---------------------------------------------------------------------------

def validate_and_promote(
    db_path: str,
    spec_id: int,
    rigorous: bool = True,
) -> Dict[str, Any]:
    """Render, validate, and transition a proposed spec.

    Returns a dict:
        {"spec_id": ..., "outcome": "validated"|"retired",
         "verdict": "PASS"|"FAIL", "report": {...}}
    """
    from strategy_generator import (
        get_strategy,
        update_status,
        write_strategy_module,
    )

    strat = get_strategy(db_path, spec_id)
    if strat is None:
        raise ValueError(f"no auto-strategy row with id={spec_id}")
    if strat["status"] != "proposed":
        raise ValueError(
            f"spec {spec_id} is in status '{strat['status']}', not 'proposed'"
        )

    import json
    spec = json.loads(strat["spec_json"])

    # 1. Render the module to disk. The registry discovers it on next import.
    path = write_strategy_module(spec, spec_id)
    logger.info("rendered auto-strategy %s → %s", spec["name"], path)

    # 2. Run validation gate. We pick the PRIMARY market from applicable_markets
    #    — Phase 2 tests one market at a time. A multi-market strategy that
    #    passes its primary market is eligible; future work can add per-market
    #    validation loops.
    primary_market = spec["applicable_markets"][0]
    report = _run_validation(spec, primary_market, rigorous=rigorous)

    if report.get("verdict") == "PASS":
        update_status(db_path, spec_id, "validated", validation_report=report)
        update_status(db_path, spec_id, "shadow")
        logger.info("auto-strategy %s promoted proposed→shadow", spec["name"])
        return {"spec_id": spec_id, "outcome": "validated",
                "verdict": "PASS", "report": report}

    # Failed — retire the module file so the registry stops importing it,
    # and record the retirement reason.
    from strategy_generator import delete_module_file
    delete_module_file(spec["name"])
    reason = "validation_failed: " + ", ".join(
        f.get("gate", "?") for f in report.get("failed_gates", [])
    )
    update_status(db_path, spec_id, "retired", retirement_reason=reason,
                  validation_report=report)
    logger.info("auto-strategy %s failed validation: %s", spec["name"], reason)
    return {"spec_id": spec_id, "outcome": "retired",
            "verdict": "FAIL", "report": report}


def _run_validation(spec: Dict[str, Any], market_type: str,
                    rigorous: bool = True) -> Dict[str, Any]:
    """Invoke the Phase 2 gate against the auto-strategy's find_candidates.

    We import the just-written module and adapt its find_candidates into
    the per-symbol signal shape that the backtester expects.
    """
    import importlib
    module = importlib.import_module(f"strategies.{spec['name']}")
    importlib.reload(module)  # ensure we're testing the freshly rendered code

    def signal_fn(symbol, df=None):
        # Wrap find_candidates for a single symbol. We fabricate a minimal
        # ctx; the auto-strategy only looks at bars, not ctx.
        class _Ctx:
            segment = market_type
        try:
            results = module.find_candidates(_Ctx(), [symbol]) or []
            if results:
                return results[0]
        except Exception:
            pass
        return {"signal": "HOLD"}

    from rigorous_backtest import validate_strategy

    if rigorous:
        return validate_strategy(
            strategy_fn=signal_fn,
            market_type=market_type,
            history_days=360,         # 18 months for auto-strategies
            sample_size=15,           # smaller sample — faster iteration
            monte_carlo_iterations=200,
        )

    # Lightweight mode for unit tests — just call backtest_strategy once
    from backtester import backtest_strategy
    result = backtest_strategy(
        market_type=market_type,
        days=180,
        sample_size=5,
        signal_fn=signal_fn,
    )
    trades = result.get("num_trades", 0) or 0
    sharpe = result.get("sharpe_ratio", 0) or 0
    verdict = "PASS" if (trades >= 10 and sharpe >= 0.5) else "FAIL"
    return {"verdict": verdict, "score": sharpe, "passed_gates": [],
            "failed_gates": [] if verdict == "PASS" else [
                {"gate": "light_validation",
                 "reason": f"trades={trades}, sharpe={sharpe:.2f}"}
            ], "metrics": result}


# ---------------------------------------------------------------------------
# Shadow → active promotion
# ---------------------------------------------------------------------------

def promote_matured_shadows(db_path: str) -> List[Dict[str, Any]]:
    """Check every shadow strategy; promote the ones that have earned it.

    Returns a list of transition events.
    """
    from alpha_decay import compute_rolling_metrics, is_deprecated
    from strategy_generator import list_strategies, update_status

    events: List[Dict[str, Any]] = []

    # Enforce the active cap. If we're already at the limit, no promotions.
    active = list_strategies(db_path, status="active")
    slots_free = SHADOW_PROMOTION["max_active_auto_strategies"] - len(active)
    if slots_free <= 0:
        logger.info(
            "auto-strategy active cap reached (%d) — no promotions this cycle",
            len(active),
        )
        return events

    shadows = list_strategies(db_path, status="shadow")
    # Sort by rolling sharpe desc so the best candidates get the open slots.
    scored: List[tuple] = []
    for s in shadows:
        metrics = compute_rolling_metrics(db_path, s["name"])
        scored.append((metrics.get("sharpe_ratio", 0) or 0, metrics, s))
    scored.sort(key=lambda t: t[0], reverse=True)

    for sharpe, metrics, s in scored:
        if slots_free <= 0:
            break
        n = metrics.get("n_predictions", 0) or 0
        if n < SHADOW_PROMOTION["min_shadow_predictions"]:
            continue
        if sharpe < SHADOW_PROMOTION["min_rolling_sharpe"]:
            continue
        if is_deprecated(db_path, s["name"]):
            continue
        update_status(db_path, s["id"], "active")
        events.append({
            "spec_id": s["id"], "name": s["name"],
            "transition": "shadow→active", "sharpe": sharpe, "n": n,
        })
        slots_free -= 1
        logger.info("auto-strategy %s promoted shadow→active (sharpe=%.2f, n=%d)",
                    s["name"], sharpe, n)

    return events


# ---------------------------------------------------------------------------
# Retirement of shadows that never found an edge
# ---------------------------------------------------------------------------

def retire_failed_shadows(db_path: str) -> List[Dict[str, Any]]:
    """Retire shadow strategies that have had enough chances and still lose."""
    from alpha_decay import compute_rolling_metrics
    from strategy_generator import (
        delete_module_file,
        list_strategies,
        update_status,
    )

    events: List[Dict[str, Any]] = []
    shadows = list_strategies(db_path, status="shadow")

    for s in shadows:
        shadow_age_days = _days_since(s.get("shadow_started_at"))
        if shadow_age_days is None:
            continue
        if shadow_age_days < SHADOW_PROMOTION["max_shadow_days_without_edge"]:
            continue
        metrics = compute_rolling_metrics(db_path, s["name"])
        sharpe = metrics.get("sharpe_ratio", 0) or 0
        if sharpe >= SHADOW_PROMOTION["min_rolling_sharpe"]:
            continue
        update_status(
            db_path, s["id"], "retired",
            retirement_reason=f"shadow period exceeded ({shadow_age_days}d) with sharpe={sharpe:.2f}",
        )
        delete_module_file(s["name"])
        events.append({
            "spec_id": s["id"], "name": s["name"],
            "transition": "shadow→retired", "sharpe": sharpe,
            "shadow_days": shadow_age_days,
        })
        logger.info("auto-strategy %s retired after %dd (sharpe=%.2f)",
                    s["name"], shadow_age_days, sharpe)

    return events


def _days_since(iso_ts: Optional[str]) -> Optional[int]:
    if not iso_ts:
        return None
    try:
        from datetime import datetime
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        # SQLite 'datetime(now)' produces "YYYY-MM-DD HH:MM:SS" — handle both.
        if "T" not in iso_ts and "+" not in iso_ts:
            then = datetime.strptime(iso_ts[:19], "%Y-%m-%d %H:%M:%S")
        delta = datetime.utcnow() - then.replace(tzinfo=None)
        return max(0, delta.days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Convenience: one-shot "process all shadows" call for the scheduler.
# ---------------------------------------------------------------------------

def tick(db_path: str) -> Dict[str, Any]:
    """Run both promotion and retirement passes; return a combined report."""
    promoted = promote_matured_shadows(db_path)
    retired = retire_failed_shadows(db_path)
    return {"promoted": promoted, "retired": retired}
