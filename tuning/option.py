"""Option-only tuning aggregations.

Phase 2 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Every aggregate here filters `ai_predictions` to OPTION signal
types only. Stock outcomes can no longer pollute option win-rate,
and vice versa. Audit finding #3 (self-tuning corruption) is fixed
by construction.

Used by `pipelines.option.OptionPipeline.tune()`.
"""
from __future__ import annotations

import sqlite3
from typing import Tuple

# The set of signal_type strings that belong to the option pipeline.
# Today's prod values: MULTILEG_OPEN dominates. OPTIONS is the
# legacy single-leg signal_type written by options_trader. Future
# OPTION_OPEN / OPTION_CLOSE will land as the option pipeline gains
# explicit single-leg support.
OPTION_SIGNAL_TYPES = (
    "MULTILEG_OPEN",
    "OPTIONS",
    "OPTION_EXERCISE",
)


def current_win_rate(db_path: str) -> Tuple[float, int]:
    """Option-only win rate from ai_predictions.

    Phase 5 of the pipeline refactor: prefers the structural
    `pipeline_kind` tag added in Phase 5a's migration, falling back
    to the signal-type enumeration for legacy rows the migration
    couldn't classify.

    Filter logic per row:
      - pipeline_kind = 'option'                 → IN
      - pipeline_kind = 'stock' or other         → OUT
      - pipeline_kind IS NULL AND signal IN      → IN  (legacy fallback)
        OPTION_SIGNAL_TYPES
      - pipeline_kind IS NULL AND not in option  → OUT

    Returns (win_rate_pct, total_resolved).
    """
    placeholders = ",".join("?" * len(OPTION_SIGNAL_TYPES))
    where = (
        "status='resolved' AND ("
        "  pipeline_kind = 'option' OR ("
        "    pipeline_kind IS NULL "
        f"    AND predicted_signal IN ({placeholders})"
        "  )"
        ")"
    )
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions WHERE {where}",
            OPTION_SIGNAL_TYPES,
        ).fetchone()[0]
        if resolved == 0:
            return 0.0, 0
        wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE {where} AND actual_outcome='win'",
            OPTION_SIGNAL_TYPES,
        ).fetchone()[0]
        return (wins / resolved * 100), resolved
    finally:
        conn.close()


# Subsequent commits will land option-specific parameters that
# don't apply to stocks: max_spread_loss_pct (cap defined-risk
# spread loss as % of account equity), min_dte (refuse spreads
# closer than N days to expiry), iv_rank_threshold (only sell
# premium when IV rank is above N). All driven by option-only
# metrics → option-only tuning, no cross-pollution.
