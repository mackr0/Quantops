"""Confidence-tiered position sizing multiplier (2026-05-12).

The AI's confidence score is calibrated — buckets show a monotonic
win-rate relationship from a 19K-prediction audit on 2026-05-12:

  conf  0- 59  →  weaker than baseline (treat as uncertain)
  conf 60- 69  →  win rate 50.7% over 222 trades  (baseline)
  conf 70- 79  →  win rate 54.4% over 717 trades  (+3.7pt)
  conf 80- 89  →  win rate 58.0% over  56 trades  (+7.3pt)
  conf 90-100  →  small sample, conservatively bucketed with 80-89

The old position-size logic applied a flat 1.25× boost at conf ≥ 80
and no multiplier elsewhere. That left the calibration advantage at
the lower buckets unused AND failed to scale risk DOWN on
low-confidence trades. This module replaces it with a 4-tier ladder
that leans into the calibration we have:

  conf < 60          → 0.7×  (pull back on low conviction)
  conf 60-69         → 1.0×  (baseline)
  conf 70-79         → 1.2×  (above-baseline win rate)
  conf 80+           → 1.5×  (highest-calibrated bucket)

Same risk envelope as before (the trade_pipeline still caps at
max_position_pct / short_cap_pct after the multiplier). Better
expected return because we lean into the calibrated edge.

Tunable: the tier multipliers can become AI-tuned per profile in a
future wave by learning the per-bucket win rates over the profile's
own resolved predictions. For v1 this is a hard table — the data
across 11 profiles is consistent enough that one ladder is a sane
default.
"""
from __future__ import annotations

from typing import Optional


# (lower_bound_inclusive, multiplier). Highest match wins.
CONFIDENCE_TIERS = (
    (80.0, 1.5),
    (70.0, 1.2),
    (60.0, 1.0),
    (0.0,  0.7),
)


def confidence_multiplier(ai_confidence: Optional[float]) -> float:
    """Return the position-size multiplier for an AI confidence value.

    When `ai_confidence` is None (no AI prediction attached to the
    trade — pure technical signal) returns 1.0 so the caller's
    baseline allocation is unchanged.
    """
    if ai_confidence is None:
        return 1.0
    try:
        c = float(ai_confidence)
    except (TypeError, ValueError):
        return 1.0
    for floor, mult in CONFIDENCE_TIERS:
        if c >= floor:
            return mult
    return 1.0  # unreachable — last tier is 0.0


def apply_confidence_sizing(base_alloc_pct: float,
                              ai_confidence: Optional[float],
                              max_cap_pct: float) -> float:
    """Scale a base allocation percentage by the confidence multiplier,
    capped at `max_cap_pct` (the profile's max-position-pct ceiling).

    Returns the scaled allocation pct. Equivalent to the old line
        if ai_confidence and ai_confidence >= 80:
            alloc_pct = min(alloc_pct * 1.25, max_cap_pct)
    but using the 4-tier ladder.
    """
    mult = confidence_multiplier(ai_confidence)
    return min(base_alloc_pct * mult, max_cap_pct)
