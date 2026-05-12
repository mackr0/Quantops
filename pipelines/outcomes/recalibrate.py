"""One-shot specialist calibrator recalibration (2026-05-11).

After Phase 5a (pipeline_kind tag), Phase 5c (option-aware
resolver), Phase 5d (historical option backfill), and the
pipeline-aware calibrator extension, every specialist's calibrator
needs to be RE-FIT against the new clean training data.

Pre-this-commit, calibrators were trained on a mix of stock and
option resolutions where option rows had wrong actual_return_pct
values (computed from underlying stock price, not option premium).
That contamination bled into specialist confidence calibration —
a stock specialist's BUY at confidence=70 might have shown
empirical accuracy of, say, 55% partly because it was being scored
against option-pipeline outcomes the specialist had no business
predicting.

This recalibration:
1. For every specialist, fits calibrators across the
   (direction × pipeline_kind) matrix — long/short × stock/option
   plus the legacy unified fallback.
2. Saves each fitted model with a pipeline-aware filename so
   `get_calibrator(pipeline_kind=...)` finds it.
3. Clears the in-memory cache so the next ensemble run picks up
   the new models.
4. Marks the migration done in `migration_markers` so it runs
   exactly once per profile DB.

Auto-runs at multi_scheduler startup (gated by marker). Manual
`force=True` bypass available for ops re-run after major data fixes.

Returns counts: {fitted, skipped, errors, skipped_already_done}.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)


MIGRATION_KEY = "specialist_calibrator_recalibration_2026_05_11"


def recalibrate_all_specialists(
    db_path: str, force: bool = False,
) -> Dict[str, int]:
    counts = {
        "fitted": 0,
        "skipped": 0,
        "errors": 0,
        "skipped_already_done": 0,
    }
    if not db_path:
        return counts

    from journal import is_migration_done, mark_migration_done
    if not force and is_migration_done(db_path, MIGRATION_KEY):
        counts["skipped_already_done"] = 1
        return counts

    try:
        from specialists import discover_specialists
        from specialist_calibration import (
            fit_calibrator, save_calibrator, clear_calibrator_cache,
        )

        specs = discover_specialists()
        if not specs:
            logger.info("recalibrate: no specialists discovered")
            mark_migration_done(db_path, MIGRATION_KEY,
                                  details="no specialists")
            return counts

        # Fit across (direction, pipeline_kind) matrix.
        # Direction None = the legacy unified direction (back-compat
        #                  fallback when neither long nor short alone
        #                  has enough samples).
        # pipeline_kind None = the legacy unified pipeline (back-compat
        #                      fallback when stock/option-only fits
        #                      lack samples).
        # The fallback chain in get_calibrator walks most-specific to
        # least-specific so callers always get the best available.
        directions = (None, "long", "short")
        kinds = (None, "stock", "option")

        for spec in specs:
            name = spec.NAME
            for d in directions:
                for pk in kinds:
                    try:
                        cal = fit_calibrator(
                            db_path, name, direction=d, pipeline_kind=pk,
                        )
                        if cal is None:
                            counts["skipped"] += 1
                            continue
                        save_calibrator(
                            db_path, name, cal,
                            direction=d, pipeline_kind=pk,
                        )
                        counts["fitted"] += 1
                    except Exception as exc:
                        counts["errors"] += 1
                        logger.debug(
                            "recalibrate %s dir=%s kind=%s failed: %s",
                            name, d, pk, exc,
                        )

        clear_calibrator_cache()
        mark_migration_done(
            db_path, MIGRATION_KEY,
            details=(
                f"fitted={counts['fitted']} skipped={counts['skipped']} "
                f"errors={counts['errors']}"
            ),
        )
        logger.info(
            "Specialist calibrator recalibration on %s: "
            "fitted=%d skipped=%d errors=%d",
            db_path, counts["fitted"], counts["skipped"],
            counts["errors"],
        )
    except Exception as exc:
        logger.warning(
            "Calibrator recalibration failed on %s: %s", db_path, exc,
        )

    return counts
