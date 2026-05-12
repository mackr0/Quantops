"""Phase 1 of the instrument-class pipeline refactor (2026-05-11).

Phase 1 moves slippage stats out of the cross-instrument
`metrics.legacy.calculate_all_metrics` mixed aggregate into
per-pipeline namespaces (`metrics.stock`, `metrics.option`). The
critical invariant: option premium %-moves can no longer pollute
stock slippage averages, because the SQL filter at the data layer
prevents it.

Closes:
- TODO #8 (1130% slippage display) by construction.
- Audit finding #1 from `AUDIT_2026_05_11_AI_PIPELINE.md`.

Pins:
1. Stock-only slippage stats exclude option rows from the average.
2. Option-only slippage stats are dollar-denominated (no %
   reported), and dollar fields apply the contract multiplier.
3. Mixed legacy aggregate still works (back-compat for any
   existing consumer that hasn't migrated).
4. Pipeline `compute_metrics()` returns the right Metrics shape
   with slippage stamped under `numbers["slippage"]`.
5. The `metrics/__init__.py` re-exports the legacy public surface
   so existing `from metrics import ...` imports keep working.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def db_with_mixed_slippage(tmp_path):
    """Profile DB seeded with both stock and option fills. Stock
    slippage is small and reasonable (%-of-price math works).
    Option slippage is the prod-bug shape: penny premium with a
    big mark swing that produces 900%+ % math but ~$0.45 in real
    dollar cost."""
    db_path = str(tmp_path / "p.db")
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)

    # Stock fill: AAPL 100 shares, decision $150.00, fill $150.30
    # → 0.20% slippage, $30 dollar cost. Sane.
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "decision_price, fill_price, slippage_pct, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-05-10T10:00:00", "AAPL", "buy", 100, 150.30, 150.00,
         150.30, 0.20, "open"),
    )
    # Option fill: PCG $18C, decision $0.05, fill $0.50 → 900%
    # slippage by stock-style math, $0.45/share = $45 actual cost.
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "decision_price, fill_price, slippage_pct, occ_symbol, "
        "signal_type, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-05-10T11:00:00", "PCG", "buy", 1, 0.50, 0.05, 0.50,
         900.0, "PCG260612C00018000", "MULTILEG", "open"),
    )
    conn.commit()
    conn.close()
    return db_path


class TestStockSlippageExcludesOptions:
    def test_stock_avg_unaffected_by_option_pollution(
        self, db_with_mixed_slippage,
    ):
        """The 1130% bug is exactly this: option's 900% pollutes
        the stock average. Per-pipeline filter at the SQL layer
        prevents that."""
        from metrics import stock as stock_metrics
        s = stock_metrics.slippage_stats(db_with_mixed_slippage)
        assert s is not None
        assert s["trades_with_fills"] == 1   # only the stock row
        assert s["avg_slippage_pct"] == pytest.approx(0.20)
        assert s["worst_slippage_pct"] == pytest.approx(0.20)

    def test_returns_none_for_db_with_no_stock_fills(self, tmp_path):
        """Option-only profile → stock slippage stats is None
        rather than a meaningless 0/0 row."""
        db = str(tmp_path / "options_only.db")
        from journal import init_db
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, occ_symbol, "
            "signal_type, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-05-10T11:00:00", "PCG", "buy", 1, 0.50, 0.05, 0.50,
             900.0, "PCG260612C00018000", "MULTILEG", "open"),
        )
        conn.commit()
        conn.close()
        from metrics import stock as stock_metrics
        assert stock_metrics.slippage_stats(db) is None


class TestOptionSlippageIsDollarsOnly:
    def test_pct_fields_are_None(self, db_with_mixed_slippage):
        """Option slippage in % is meaningless on penny premiums.
        The Phase 1 invariant: option metrics module NEVER reports
        %, only $."""
        from metrics import option as option_metrics
        o = option_metrics.slippage_stats(db_with_mixed_slippage)
        assert o is not None
        assert o["avg_slippage_pct"] is None, (
            "Option slippage % must be None — penny premiums make "
            "the % math nonsensical (1130% prod bug)."
        )
        assert o["worst_slippage_pct"] is None

    def test_dollar_fields_apply_contract_multiplier(
        self, db_with_mixed_slippage,
    ):
        """Option qty is in CONTRACTS; one contract = 100 shares
        of the underlying. The dollar field reflects portfolio
        impact, not per-share premium delta."""
        from metrics import option as option_metrics
        o = option_metrics.slippage_stats(db_with_mixed_slippage)
        # raw magnitude per the SQL: |0.50 - 0.05| * 1 = $0.45
        # × 100 contract multiplier = $45
        assert o["total_slippage_magnitude"] == pytest.approx(45.0)
        assert o["trades_with_fills"] == 1   # only the option row

    def test_returns_none_for_db_with_no_option_fills(self, tmp_path):
        db = str(tmp_path / "stocks_only.db")
        from journal import init_db
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "decision_price, fill_price, slippage_pct, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-05-10T10:00:00", "AAPL", "buy", 100, 150.30,
             150.00, 150.30, 0.20, "open"),
        )
        conn.commit()
        conn.close()
        from metrics import option as option_metrics
        assert option_metrics.slippage_stats(db) is None


class TestLegacyAggregateStillWorks:
    """Migration safety: every existing `from metrics import X`
    consumer must keep working. The legacy module is re-exported
    via metrics/__init__.py."""

    def test_calculate_all_metrics_importable(self):
        from metrics import calculate_all_metrics
        assert callable(calculate_all_metrics)

    def test_render_equity_curve_svg_importable(self):
        from metrics import render_equity_curve_svg
        assert callable(render_equity_curve_svg)

    def test_legacy_module_directly_accessible(self):
        from metrics.legacy import calculate_all_metrics
        assert callable(calculate_all_metrics)


class TestPortfolioAggregateIsBackCompat:
    """Legacy mixed-instrument slippage is still accessible via
    metrics.portfolio for migration verification. This is the path
    that produced the 1130% display, NOT for end-user use."""

    def test_portfolio_slippage_returns_mixed(
        self, db_with_mixed_slippage,
    ):
        from metrics import portfolio
        p = portfolio.slippage_stats_all(db_with_mixed_slippage)
        assert p is not None
        # Mixes both rows. avg of 0.20 + 900.0 = ~450 (the bug shape).
        assert p["trades_with_fills"] == 2


class TestPipelineComputeMetrics:
    """`pipelines/{stock,option}.py:compute_metrics()` now use the
    per-pipeline modules. Phase 0's NotImplementedError is gone for
    this method."""

    def test_stock_pipeline_compute_metrics(
        self, db_with_mixed_slippage,
    ):
        from pipelines.stock import StockPipeline
        ctx = SimpleNamespace(db_path=db_with_mixed_slippage)
        m = StockPipeline().compute_metrics(ctx)
        assert m.pipeline_name == "stock"
        assert "slippage" in m.numbers
        # Avg matches the stock-only number, not the polluted mixed avg.
        assert m.numbers["slippage"]["avg_slippage_pct"] == pytest.approx(0.20)

    def test_option_pipeline_compute_metrics(
        self, db_with_mixed_slippage,
    ):
        from pipelines.option import OptionPipeline
        ctx = SimpleNamespace(db_path=db_with_mixed_slippage)
        m = OptionPipeline().compute_metrics(ctx)
        assert m.pipeline_name == "option"
        assert "slippage" in m.numbers
        # Option slippage is dollar-only; % fields are None.
        assert m.numbers["slippage"]["avg_slippage_pct"] is None

    def test_pipeline_compute_metrics_returns_empty_when_no_db(self):
        """If ctx has no db_path, compute_metrics returns a Metrics
        with empty numbers — doesn't raise."""
        from pipelines.stock import StockPipeline
        ctx = SimpleNamespace()
        m = StockPipeline().compute_metrics(ctx)
        assert m.pipeline_name == "stock"
        assert m.numbers == {}
