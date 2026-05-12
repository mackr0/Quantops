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

    Filters resolved predictions to stock signal types so option
    outcomes (premium %-moves are 10-100× stock %-moves) can no
    longer dominate the aggregate. Returns (win_rate_pct,
    total_resolved).
    """
    placeholders = ",".join("?" * len(STOCK_SIGNAL_TYPES))
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' "
            f"AND predicted_signal IN ({placeholders})",
            STOCK_SIGNAL_TYPES,
        ).fetchone()[0]
        if resolved == 0:
            return 0.0, 0
        wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' AND actual_outcome='win' "
            f"AND predicted_signal IN ({placeholders})",
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
