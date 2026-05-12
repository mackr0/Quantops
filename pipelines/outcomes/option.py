"""Option outcome writer (Phase 5 of pipeline refactor).

Writes a resolved option prediction with `pipeline_kind = 'option'`.
Tagging is the structural fix that closes audit finding #2 by
construction: tuning queries that filter by pipeline_kind never see
option outcomes in stock aggregations regardless of what
predicted_signal contained.

Phase 5a (this commit): tags writes with pipeline_kind = 'option'.
The return_pct value passed in is whatever the caller computed —
Phase 5a doesn't yet correct the upstream resolver's wrong-price
issue (option resolver today reads underlying price, not premium).

Phase 5b (queued): the option pipeline resolver computes
return_pct from premium changes (single-leg) or net P&L vs
max_loss (multileg) — at which point this writer's return_pct
will be option-economics-correct.

Used by `pipelines.option.OptionPipeline.record_outcome()`.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def record(db_path: str, prediction_id: int, outcome: Any) -> None:
    """Update an ai_predictions row with the resolved option outcome.

    Pipeline_kind = 'option' tag is the structural fix; downstream
    aggregations filter by it. Idempotent on replay.
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
                   pipeline_kind = 'option'
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
