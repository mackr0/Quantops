"""Apply ParameterAdjustments to the trading_profiles row
(Phase 2b, 2026-05-12).

The Pipeline.tune() method returns a `ParameterAdjustments` DTO
with a `changes` dict. Until this commit, that dict was never
consumed — Phase 2 was framework only. Now `apply_parameter_adjustments`
walks the dict and persists each change via the existing
`models.update_trading_profile` writer (which gates on
`allowed_cols` — adjustments to unknown columns get logged but
silently ignored).

Also records the adjustment to a `tuning_history` table so
operators can see what the tuner has done over time.

Auto-runs at multi_scheduler startup alongside the other Phase 5
tasks. Per-profile. Failure non-fatal.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)


def apply_parameter_adjustments(
    profile_id: int,
    db_path: str,
    adjustments: Any,
    ctx: Optional[Any] = None,
) -> int:
    """Apply a `ParameterAdjustments` DTO to the trading_profiles row.

    Records each adjustment via `models.log_tuning_change` —
    the SAME tuning_history table that the legacy self_tuning
    module + capital_allocator already write to. Operators see
    pipeline-tuner adjustments in the same panel as legacy
    self-tuner adjustments (`ai_weekly_summary` already reads
    from this table).

    Args:
        profile_id: trading_profiles.id to update.
        db_path: profile's per-profile DB path (kept in signature
            for back-compat with earlier callers; not used for
            history logging anymore — that goes to the main
            config DB via models.log_tuning_change).
        adjustments: ParameterAdjustments DTO with
            `.pipeline_name`, `.changes` (dict of param→new_value),
            `.rationale`.
        ctx: optional UserContext for old-value lookup +
            user_id required by log_tuning_change.

    Returns: count of params actually written.
    """
    if not profile_id or not adjustments:
        return 0
    changes = getattr(adjustments, "changes", None) or {}
    if not changes:
        return 0

    pipeline_name = getattr(adjustments, "pipeline_name", "unknown")
    rationale = getattr(adjustments, "rationale", "") or ""
    user_id = getattr(ctx, "user_id", 0) if ctx is not None else 0

    # 2026-07-15 — this writer used to bypass every guardrail the
    # legacy funnel enforces (operator-only firewall, type cast,
    # PARAM_BOUNDS) and logged the ledger row BEFORE attempting the
    # profile write, so a failed write still left history claiming
    # the change happened. Align with _apply_param_change's contract:
    # refuse operator-only params, produce ONE canonical (cast +
    # bounds-clamped) value, write the profile FIRST, and only then
    # record that same value to the ledger.
    from self_tuning import _OPERATOR_ONLY_PARAMS, _cast_param_value
    from param_bounds import clamp as _bounds_clamp

    sanitized = {}
    for param, new_val in changes.items():
        if param in _OPERATOR_ONLY_PARAMS:
            logger.warning(
                "pipeline tuner refused to change operator-only param "
                "%s (profile %s): universe/liquidity floors are set in "
                "Settings, never auto-tuned", param, profile_id,
            )
            continue
        # _cast_param_value types KNOWN params; for params outside its
        # sets it returns the string we passed UNCHANGED (same object),
        # in which case keep the caller's native value — stringifying a
        # float here regressed max_net_options_delta_pct to the TEXT
        # '0.06' (caught by test_option_tuner_writes).
        _s = str(new_val)
        canonical = _cast_param_value(param, _s)
        if canonical is _s:
            canonical = new_val
        try:
            canonical = _bounds_clamp(param, canonical)
        except (KeyError, TypeError, ValueError) as _bc_exc:
            # EXPECTED fall-through for non-numeric param values.
            logger.debug(
                "PARAM_BOUNDS clamp skipped for %s=%r: %s: %s",
                param, canonical, type(_bc_exc).__name__, _bc_exc,
            )
        except Exception as _bc_exc:
            # UNEXPECTED bounds-table failure — the safety clamp must
            # not degrade at debug level.
            logger.warning(
                "PARAM_BOUNDS clamp FAILED for %s=%r (clamp skipped — "
                "investigate the bounds table): %s: %s",
                param, canonical, type(_bc_exc).__name__, _bc_exc,
            )
        sanitized[param] = canonical
    if not sanitized:
        return 0

    # Persist the parameter values to trading_profiles via the
    # central writer — BEFORE the ledger, so tuning_history can never
    # assert a change that didn't land.
    try:
        from models import update_trading_profile
        update_trading_profile(profile_id, **sanitized)
    except Exception as exc:
        logger.warning(
            "apply_parameter_adjustments: update_trading_profile "
            "for profile_id=%s failed (no ledger rows written): %s",
            profile_id, exc,
        )
        return 0

    # Record each applied change to the canonical tuning_history
    # table (main config DB). `adjustment_type` distinguishes
    # pipeline-tuner adjustments from legacy self-tuner ones so
    # operators can filter the history view by source. new_value is
    # the SAME canonical value written above — one value, two stores.
    try:
        from models import log_tuning_change
        for param, canonical in sanitized.items():
            old_val = (
                getattr(ctx, param, None) if ctx is not None
                else None
            )
            log_tuning_change(
                profile_id=profile_id,
                user_id=user_id,
                adjustment_type=f"pipeline_tuner_{pipeline_name}",
                parameter_name=param,
                old_value=str(old_val) if old_val is not None else "",
                new_value=str(canonical),
                reason=rationale,
            )
    except Exception as exc:
        # The profile write already landed; a missing ledger row is a
        # visibility gap, not a config divergence. Loud so it gets fixed.
        logger.warning(
            "log_tuning_change failed for profile=%s pipeline=%s "
            "(profile write already applied): %s",
            profile_id, pipeline_name, exc,
        )

    logger.info(
        "Applied %d parameter adjustment(s) to profile %s "
        "(pipeline=%s): %s. Rationale: %s",
        len(sanitized), profile_id, pipeline_name,
        {k: (f"{v:.4f}" if isinstance(v, float) else str(v))
         for k, v in sanitized.items()},
        rationale,
    )
    return len(sanitized)


def run_pipeline_tuning(ctx: Any) -> dict:
    """Run all pipeline tune() methods for this ctx and apply their
    adjustments. Called once per scheduler cycle.

    Returns {pipeline_name: count_of_params_written}.
    """
    results = {}
    try:
        from pipelines.registry import get_pipelines_for_profile
    except Exception:
        return results

    try:
        pipelines = get_pipelines_for_profile(ctx)
    except Exception as exc:
        logger.debug("run_pipeline_tuning: get_pipelines failed: %s", exc)
        return results

    profile_id = getattr(ctx, "profile_id", None)
    db_path = getattr(ctx, "db_path", None)

    for pipeline in pipelines:
        try:
            metrics = pipeline.compute_metrics(ctx)
            adjustments = pipeline.tune(ctx, metrics)
            if profile_id and getattr(adjustments, "changes", None):
                n = apply_parameter_adjustments(
                    profile_id, db_path, adjustments, ctx=ctx,
                )
                results[pipeline.name] = n
            else:
                results[pipeline.name] = 0
        except Exception as exc:
            logger.debug(
                "run_pipeline_tuning: %s.tune() failed: %s",
                pipeline.name, exc,
            )
            results[pipeline.name] = 0

    return results
