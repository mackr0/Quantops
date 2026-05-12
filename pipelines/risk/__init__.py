"""Cross-pipeline portfolio risk infrastructure (Phase 6 of the
pipeline refactor).

Risk is one of the few things that's INTENTIONALLY shared across
pipelines: a $5,000 stock position and an option spread with $5,000
of delta-equivalent exposure consume the same amount of the
profile's risk budget. The pipeline architecture forks DECISION
LOGIC per instrument class but keeps the AGGREGATE RISK VIEW unified.

What lives here:
  - `exposure.py`          — delta-adjusted dollar exposure per position
                             (closes audit finding #7 — option positions
                             contribute their delta-equivalent share
                             exposure, NOT premium).
  - re-exports of `compute_book_greeks` from
    `options_greeks_aggregator` as the canonical book-Greeks computation.

What does NOT live here:
  - per-pipeline metrics    — see `metrics/{stock,option}.py`
  - per-pipeline tuning     — see `tuning/{stock,option}.py`
  - per-pipeline outcomes   — see `pipelines/outcomes/{stock,option}.py`
  - per-pipeline prompts    — see `pipelines/{stock,option}_prompt.py`

Phase 6a (this commit) ships the cross-pipeline exposure math.
Phase 6b will wire it into `portfolio_risk_model.compute_portfolio_risk`
so the factor-regression model uses delta-equivalent weights for
option positions (today it uses qty × price, which counts a long
call as ~$200 of exposure when its actual directional risk is
~$2,000 of underlying).
"""

from __future__ import annotations

# Re-export the canonical book-Greeks computation. The
# `options_greeks_aggregator` module has been the single source of
# truth for Greeks aggregation since Phase A1 of OPTIONS_PROGRAM_PLAN;
# Phase 6 of the pipeline refactor doesn't reinvent it, just gives it
# a per-pipeline namespace.
from options_greeks_aggregator import compute_book_greeks  # noqa: F401

from . import exposure  # noqa: F401

__all__ = ["compute_book_greeks", "exposure"]
