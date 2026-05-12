"""Stock outcome writer (Phase 5 of pipeline refactor).

Writes a resolved stock prediction with `pipeline_kind = 'stock'`.
The return_pct is in stock-scale (~2% range typical) — the
pre-refactor format that all stock-tuning queries already expect.

Used by `pipelines.stock.StockPipeline.record_outcome()`.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def record(db_path: str, prediction_id: int, outcome: Any) -> None:
    """Update an ai_predictions row with the resolved stock outcome.

    Idempotent: if the row was already resolved, the UPDATE replays
    the same values. The pipeline_kind tag is set unconditionally so
    a row that was previously written without the tag (legacy rows
    pre-Phase 5a) gets correctly tagged on resolution.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """UPDATE ai_predictions
               SET status = 'resolved',
                   actual_outcome = ?,
                   actual_return_pct = ?,
                   resolved_at = ?,
                   resolution_price = ?,
                   pipeline_kind = 'stock'
               WHERE id = ?""",
            (
                outcome.actual_outcome,
                round(float(outcome.actual_return_pct), 4),
                outcome.resolved_at,
                float(outcome.resolution_price),
                int(prediction_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()
