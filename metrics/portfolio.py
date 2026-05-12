"""Portfolio-level metrics aggregations — the parts that genuinely
span all instrument classes.

Phase 1 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Equity, total drawdown, total cash, and combined risk exposure
aggregate across every pipeline's positions. These don't belong
to any single pipeline's namespace — they're the portfolio's
top-level metrics that downstream consumers (the dashboard
header, the daily snapshot writer) need.

Used by the dashboard top-level summary, NOT by any individual
pipeline's `compute_metrics()` (those are pipeline-specific).
"""
from __future__ import annotations

from typing import Optional

from journal import get_slippage_stats as _journal_slippage_stats


def slippage_stats_all(db_path: str) -> Optional[dict]:
    """Cross-instrument slippage aggregate.

    DEPRECATED for the user-facing dashboard. Returns the legacy
    mixed-instrument slippage stats with `kind=None`. The 1130%
    avg_slippage_pct bug originates here.

    Kept ONLY for two narrow uses:
      1. Migration verification — the legacy aggregate must still
         be computable so we can compare new per-pipeline numbers
         against the old mixed-pipeline numbers.
      2. Backwards compatibility for any internal tooling that
         queries the cross-instrument view; these tools are being
         migrated to use `metrics.stock.slippage_stats` +
         `metrics.option.slippage_stats` separately.

    Once all consumers migrate, this function will be removed.
    """
    return _journal_slippage_stats(db_path, kind=None)


# Phase 1 ships the slippage entry. Subsequent commits will add:
#   - total_equity(profile): cross-instrument equity snapshot
#   - total_drawdown(profile): max drawdown across the whole book
#   - total_capital_at_risk(profile): sum of position market_values
#     irrespective of instrument
# These all CALL pipeline-specific computations and SUM the results;
# they don't duplicate logic.
