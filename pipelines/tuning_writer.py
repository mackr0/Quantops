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


def _ensure_tuning_history_table(db_path: str) -> None:
    """Create tuning_history if it doesn't exist. Per-profile DB."""
    if not db_path:
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tuning_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                pipeline_name TEXT NOT NULL,
                param_name TEXT NOT NULL,
                old_value REAL,
                new_value REAL NOT NULL,
                rationale TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tuning_history_ts "
            "ON tuning_history(timestamp DESC)"
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("tuning_history init failed: %s", exc)


def apply_parameter_adjustments(
    profile_id: int,
    db_path: str,
    adjustments: Any,
    ctx: Optional[Any] = None,
) -> int:
    """Apply a `ParameterAdjustments` DTO to the trading_profiles row.

    Args:
        profile_id: the trading_profiles.id to update.
        db_path: the profile's per-profile DB path (for
            tuning_history logging).
        adjustments: ParameterAdjustments DTO with `.pipeline_name`,
            `.changes` (dict of param→new_value), `.rationale`.
        ctx: optional UserContext for reading current values so
            tuning_history can record old→new. When provided,
            avoids a separate DB read.

    Returns: count of params actually written.
    """
    if not profile_id or not adjustments:
        return 0
    changes = getattr(adjustments, "changes", None) or {}
    if not changes:
        return 0

    pipeline_name = getattr(adjustments, "pipeline_name", "unknown")
    rationale = getattr(adjustments, "rationale", "") or ""

    # Record old→new transitions for visibility
    _ensure_tuning_history_table(db_path)
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            for param, new_val in changes.items():
                old_val = (
                    getattr(ctx, param, None) if ctx is not None
                    else None
                )
                conn.execute(
                    "INSERT INTO tuning_history "
                    "(pipeline_name, param_name, old_value, "
                    " new_value, rationale) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pipeline_name, param,
                     float(old_val) if old_val is not None else None,
                     float(new_val), rationale),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug("tuning_history insert failed: %s", exc)

    # Persist to trading_profiles via the central writer
    try:
        from models import update_trading_profile
        update_trading_profile(profile_id, **changes)
    except Exception as exc:
        logger.warning(
            "apply_parameter_adjustments: update_trading_profile "
            "for profile_id=%s failed: %s", profile_id, exc,
        )
        return 0

    logger.info(
        "Applied %d parameter adjustment(s) to profile %s "
        "(pipeline=%s): %s. Rationale: %s",
        len(changes), profile_id, pipeline_name,
        {k: f"{v:.4f}" for k, v in changes.items()},
        rationale,
    )
    return len(changes)


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
