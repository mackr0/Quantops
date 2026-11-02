"""Confidence-tiered position sizing — RETIRED to a flat multiplier (2026-11-02).

History. 2026-05-12: a 19K-prediction audit showed monotonic win-rate
buckets by AI confidence (60s 50.7%, 70s 54.4%, 80s 58.0%), and this
module scaled positions 0.7× below 60, 1.2× in the 70s and 1.5× at 80+.

2026-11-02 (docs/25 D13, docs/26 Experiment 2 §4): on the 10,532
directional predictions resolved since the 2026-08-24 reset the
relationship is INVERTED — the 80–89 bucket is the worst (40% win,
−0.99% per call) and the 60s the best (+0.61%). A ladder that put the
most money on the worst bucket was costing the books; the self-reported
confidence of these models does not carry sizing information. Every
tier is now 1.0×. Confidence is still recorded on every prediction so
the relationship keeps being measured; if a future model's confidence
calibrates, the ladder can be re-derived from that evidence — never
from the model's say-so.

The API is unchanged so the two call sites in `trade_pipeline` (long
and short sizing) and their tests keep working.
"""
from __future__ import annotations

from typing import Optional


# (lower_bound_inclusive, multiplier). Highest match wins. Flat since
# 2026-11-02 — see the module docstring for why.
CONFIDENCE_TIERS = (
    (0.0, 1.0),
)

#: True while the ladder is flat. Pinned by test so a re-introduction
#: of confidence-scaled sizing is a deliberate, documented decision.
CONFIDENCE_SIZING_FLAT = all(m == 1.0 for _lo, m in CONFIDENCE_TIERS)


def confidence_multiplier(ai_confidence: Optional[float]) -> float:
    """Return the position-size multiplier for an AI confidence value
    (1.0 for every value while the ladder is flat; None / non-numeric
    → 1.0 as before)."""
    if ai_confidence is None:
        return 1.0
    try:
        c = float(ai_confidence)
    except (TypeError, ValueError):
        return 1.0
    for floor, mult in CONFIDENCE_TIERS:
        if c >= floor:
            return mult
    return 1.0


def apply_confidence_sizing(base_alloc_pct: float,
                              ai_confidence: Optional[float],
                              max_cap_pct: float) -> float:
    """Scale a base allocation percentage by the confidence multiplier,
    capped at `max_cap_pct` (the profile's max-position-pct ceiling)."""
    mult = confidence_multiplier(ai_confidence)
    return min(base_alloc_pct * mult, max_cap_pct)
