"""Per-instrument-class self-tuning package — Phase 2 of the
instrument-class pipeline refactor (2026-05-11).

See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md` for the full plan.

Phase 2 splits the win-rate aggregator (the audit finding #3
corruption point) by signal-type. Stock tuning sees only stock
predictions; option tuning sees only option predictions. Neither
pollutes the other.

This package adds:
  - `tuning.stock` — stock-only aggregations + parameter
    adjustments. Reads `ai_predictions` filtered to stock signal
    types (BUY/STRONG_BUY/WEAK_BUY/SELL/STRONG_SELL/WEAK_SELL/
    SHORT/COVER).
  - `tuning.option` — option-only aggregations. Reads
    `ai_predictions` filtered to MULTILEG_OPEN (and future
    OPTION_OPEN, OPTION_CLOSE).

The legacy `self_tuning` module continues to exist for cross-cutting
helpers (cache, time context, history) — those are instrument-
agnostic and aren't moving. Per-pipeline aggregators that read win
rates / outcomes will progressively migrate here.
"""
