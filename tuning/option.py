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

    Filters resolved predictions to option signal types so the
    aggregate reflects option-trade outcomes only — no stock
    pollution. Returns (win_rate_pct, total_resolved).
    """
    placeholders = ",".join("?" * len(OPTION_SIGNAL_TYPES))
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' "
            f"AND predicted_signal IN ({placeholders})",
            OPTION_SIGNAL_TYPES,
        ).fetchone()[0]
        if resolved == 0:
            return 0.0, 0
        wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' AND actual_outcome='win' "
            f"AND predicted_signal IN ({placeholders})",
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
