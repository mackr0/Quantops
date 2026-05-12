"""Stock-only tuning aggregations.

Phase 2 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Every aggregate here filters `ai_predictions` to STOCK signal types
only — option outcomes (50-200% premium swings) can no longer
pollute stock win-rate, eliminating audit finding #3 by
construction.

Used by `pipelines.stock.StockPipeline.tune()`.
"""
from __future__ import annotations

import sqlite3
from typing import Optional, Tuple

# The full set of signal_type strings the stock pipeline emits.
# MULTILEG_OPEN, OPTIONS, OPTION_EXERCISE, PAIR_OPEN, PAIR_CLOSE,
# DELTA_HEDGE are EXCLUDED — those belong to other pipelines.
STOCK_SIGNAL_TYPES = (
    "BUY", "STRONG_BUY", "WEAK_BUY",
    "SELL", "STRONG_SELL", "WEAK_SELL",
    "SHORT", "COVER",
)


def current_win_rate(db_path: str) -> Tuple[float, int]:
    """Stock-only win rate from ai_predictions.

    Phase 5 of the pipeline refactor: prefers the structural
    `pipeline_kind` tag added in Phase 5a's migration, falling back
    to the signal-type enumeration for legacy rows the migration
    couldn't classify (e.g., custom signal types written by future
    pipelines before they tag themselves).

    Filter logic per row:
      - pipeline_kind = 'stock'                  → IN
      - pipeline_kind = 'option' or other        → OUT
      - pipeline_kind IS NULL AND signal IN      → IN  (legacy fallback)
        STOCK_SIGNAL_TYPES
      - pipeline_kind IS NULL AND not in stock   → OUT

    Returns (win_rate_pct, total_resolved).
    """
    placeholders = ",".join("?" * len(STOCK_SIGNAL_TYPES))
    where = (
        "status='resolved' AND ("
        "  pipeline_kind = 'stock' OR ("
        "    pipeline_kind IS NULL "
        f"    AND predicted_signal IN ({placeholders})"
        "  )"
        ")"
    )
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions WHERE {where}",
            STOCK_SIGNAL_TYPES,
        ).fetchone()[0]
        if resolved == 0:
            return 0.0, 0
        wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE {where} AND actual_outcome='win'",
            STOCK_SIGNAL_TYPES,
        ).fetchone()[0]
        return (wins / resolved * 100), resolved
    finally:
        conn.close()


# Phase 2 deliberately ships only the win-rate helper — the one
# that fixes audit finding #3 by construction. Subsequent commits
# move the per-parameter adjustment logic (stop_loss_pct,
# max_position_pct, etc.) into per-pipeline tuners; for now the
# legacy self_tuning module continues to handle parameter writes
# but reads its win-rate signal from this module.
