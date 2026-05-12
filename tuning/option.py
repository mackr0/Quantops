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

    TODO #5b (2026-05-11): predictions matching a recent
    broker_rejection row (same symbol + signal, ±5 min) are
    EXCLUDED. Especially important for option pipeline — Phase 4b's
    specialist veto persists to broker_rejections with
    rejection_code='specialist_veto', and those vetoed predictions
    must not count in option win rate.

    Returns (win_rate_pct, total_resolved).
    """
    placeholders = ",".join("?" * len(OPTION_SIGNAL_TYPES))
    where = (
        "ap.status='resolved' AND ("
        "  ap.pipeline_kind = 'option' OR ("
        "    ap.pipeline_kind IS NULL "
        f"    AND ap.predicted_signal IN ({placeholders})"
        "  )"
        ") AND NOT EXISTS ("
        "  SELECT 1 FROM broker_rejections r "
        "  WHERE r.symbol = ap.symbol "
        "  AND r.signal_type = ap.predicted_signal "
        "  AND ABS(julianday(r.timestamp) - julianday(ap.timestamp))"
        "      * 24 * 60 <= 5"
        ")"
    )
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions ap WHERE {where}",
            OPTION_SIGNAL_TYPES,
        ).fetchone()[0]
        if resolved == 0:
            return 0.0, 0
        wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions ap "
            f"WHERE {where} AND ap.actual_outcome='win'",
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
