"""Per-pipeline outcome writers (Phase 5 of pipeline refactor).

Each pipeline writes resolved predictions through its own outcome
module so the storage shape is correct for that instrument class:

  - `pipelines.outcomes.stock.record()` — stock-shape return % (the
    pre-refactor behavior — underlying-price % move).
  - `pipelines.outcomes.option.record()` — option-shape return %
    plus structural pipeline_kind tag so option outcomes never pool
    with stock outcomes in cross-pipeline aggregations.

Closes audit finding #2 by construction: tuning/stock.py and
tuning/option.py filter ai_predictions by pipeline_kind, so a stock
tuner can't see option outcomes regardless of which signal_types
were used historically.

Phase 5a (this commit) establishes the seam + tags new writes.
The legacy `ai_tracker.resolve_pending_predictions` continues to
write the original way for un-migrated callers; backfill in
`journal._migrate_extra_columns` populates pipeline_kind for
existing rows.

Phase 5b (queued) wires the dispatcher so option resolutions go
through `pipelines.outcomes.option.record()` directly — at which
point the option-side return % can also be corrected (today the
resolver computes return on underlying price; for options it should
be premium % or P&L vs max_loss).
"""
from __future__ import annotations

from typing import Optional


def kind_from_signal(signal: str) -> Optional[str]:
    """Infer pipeline_kind from a predicted_signal string.

    Returns 'stock', 'option', or None if the signal doesn't map
    cleanly. Used by the outcome writers when the caller doesn't
    pass an explicit kind, and by the journal backfill migration
    to tag legacy rows.

    Keep in sync with tuning/{stock,option}.py signal lists — these
    are the authoritative source.
    """
    if not signal:
        return None
    s = signal.upper().strip()
    # HOLD is a stock-pipeline decision (AI saw a stock candidate,
    # chose not to trade it). The HOLD volume DOMINATES the
    # prediction stream — 17,111 of 18,318 prod predictions on
    # 2026-05-11 were HOLDs. Excluding them leaves stock
    # calibration with ~5% of available data. Keep in sync with
    # journal.py backfill + tuning/stock.py STOCK_SIGNAL_TYPES.
    stock_signals = {
        "BUY", "STRONG_BUY", "WEAK_BUY",
        "SELL", "STRONG_SELL", "WEAK_SELL",
        "SHORT", "COVER", "HOLD",
    }
    option_signals = {"MULTILEG_OPEN", "OPTIONS", "OPTION_EXERCISE"}
    if s in stock_signals:
        return "stock"
    if s in option_signals:
        return "option"
    return None
