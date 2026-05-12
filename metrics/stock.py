"""Stock-only metrics aggregations.

Phase 1 of the instrument-class pipeline refactor (2026-05-11).
See `docs/14_INSTRUMENT_PIPELINE_ARCHITECTURE.md`.

Every aggregate here filters `WHERE occ_symbol IS NULL` at the SQL
layer so option rows can never pollute the result. Audit finding #1
(1130% slippage display from option-pollution) is fixed by
construction once consumers move from `metrics.legacy.calculate_all_metrics`
to these modules.

Used by `pipelines.stock.StockPipeline.compute_metrics()`.
"""
from __future__ import annotations

from typing import Optional

from journal import get_slippage_stats as _journal_slippage_stats


def slippage_stats(db_path: str) -> Optional[dict]:
    """Stock-only slippage stats.

    Filters `WHERE occ_symbol IS NULL` so option-leg slippage never
    pollutes the average. Returns the same shape as the legacy
    `journal.get_slippage_stats` (so existing dashboard renderers
    can consume it as-is). Returns None when there are no stock
    trades with fill data.
    """
    return _journal_slippage_stats(db_path, kind="stocks")


# Phase 1 deliberately ships only the slippage helper — the one
# unblocking TODO #8. Subsequent commits will move
# stock-only versions of: gross/net return %, Sharpe / Sortino,
# sector beta, drawdown, win rate, profit factor — all filtered
# to stock rows only — out of `metrics.legacy.calculate_all_metrics`.
#
# Until then, `pipelines.stock.StockPipeline.compute_metrics()`
# composes from this module + the legacy aggregator with stock
# filters applied at the dataset assembly layer (see
# `metrics.portfolio.gather_stock_trades`).
