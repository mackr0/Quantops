"""Phase 2 of the instrument-class pipeline refactor (2026-05-11).

Phase 2 splits the win-rate aggregator (audit finding #3 corruption
point) by signal-type. Stock tuning sees only stock predictions;
option tuning sees only option predictions. Neither pollutes the
other → self-tuning corruption is fixed by construction.

Pins:
1. `tuning.stock.current_win_rate` filters to stock signal types
   only — option outcomes are excluded.
2. `tuning.option.current_win_rate` filters to option signal types
   only — stock outcomes are excluded.
3. Mixed dataset → stock-tuning sees only stock numbers; option-
   tuning sees only option numbers. They sum to total but each is
   independent.
4. Pipeline `tune()` returns ParameterAdjustments with the right
   pipeline_name and rationale that mentions the per-pipeline
   win rate.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def db_with_mixed_predictions(tmp_path):
    """Profile DB with resolved predictions across both stock and
    option signal types.

    Stock outcomes: 3 wins, 2 losses → 60% win rate.
    Option outcomes: 1 win, 4 losses → 20% win rate.
    Mixed (legacy aggregate): 4 wins, 6 losses → 40% win rate
    (the audit finding #3 pollution shape — a high-volume option
    losing streak hides the stock pipeline's healthy 60%)."""
    db_path = str(tmp_path / "p.db")
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)

    # 3 stock wins
    for i in range(3):
        conn.execute(
            "INSERT INTO ai_predictions (timestamp, symbol, "
            "predicted_signal, confidence, reasoning, "
            "price_at_prediction, status, actual_outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"2026-05-{10+i}T10:00:00", f"STK{i}", "BUY", 70,
             "test", 100, "resolved", "win"),
        )
    # 2 stock losses
    for i in range(2):
        conn.execute(
            "INSERT INTO ai_predictions (timestamp, symbol, "
            "predicted_signal, confidence, reasoning, "
            "price_at_prediction, status, actual_outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"2026-05-{13+i}T10:00:00", f"STK{i+3}", "STRONG_BUY",
             80, "test", 100, "resolved", "loss"),
        )
    # 1 option win
    conn.execute(
        "INSERT INTO ai_predictions (timestamp, symbol, "
        "predicted_signal, confidence, reasoning, "
        "price_at_prediction, status, actual_outcome) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("2026-05-15T10:00:00", "OPT0", "MULTILEG_OPEN", 75,
         "test", 5, "resolved", "win"),
    )
    # 4 option losses
    for i in range(4):
        conn.execute(
            "INSERT INTO ai_predictions (timestamp, symbol, "
            "predicted_signal, confidence, reasoning, "
            "price_at_prediction, status, actual_outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"2026-05-{16+i}T10:00:00", f"OPT{i+1}", "MULTILEG_OPEN",
             65, "test", 5, "resolved", "loss"),
        )
    conn.commit()
    conn.close()
    return db_path


class TestStockWinRateExcludesOptions:
    def test_stock_win_rate_unaffected_by_option_pollution(
        self, db_with_mixed_predictions,
    ):
        """The audit finding #3 bug shape: option outcomes pollute
        the stock tuner's win-rate signal. Per-pipeline filter at
        the SQL layer prevents that."""
        from tuning import stock as stock_tuning
        wr, n = stock_tuning.current_win_rate(db_with_mixed_predictions)
        # 3 wins / 5 stock predictions = 60%, NOT the 40% mixed avg
        assert n == 5
        assert wr == pytest.approx(60.0)

    def test_stock_win_rate_zero_when_no_stock_predictions(
        self, tmp_path,
    ):
        """Option-only profile → stock win rate is (0.0, 0), no
        crash."""
        db = str(tmp_path / "options_only.db")
        from journal import init_db
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO ai_predictions (timestamp, symbol, "
            "predicted_signal, confidence, reasoning, "
            "price_at_prediction, status, actual_outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-05-10T10:00:00", "OPT0", "MULTILEG_OPEN", 70,
             "test", 5, "resolved", "win"),
        )
        conn.commit()
        conn.close()
        from tuning import stock as stock_tuning
        wr, n = stock_tuning.current_win_rate(db)
        assert wr == 0.0
        assert n == 0


class TestOptionWinRateExcludesStocks:
    def test_option_win_rate_unaffected_by_stock_pollution(
        self, db_with_mixed_predictions,
    ):
        from tuning import option as option_tuning
        wr, n = option_tuning.current_win_rate(db_with_mixed_predictions)
        # 1 win / 5 option predictions = 20%, NOT the 40% mixed avg
        assert n == 5
        assert wr == pytest.approx(20.0)

    def test_option_win_rate_zero_when_no_option_predictions(
        self, tmp_path,
    ):
        db = str(tmp_path / "stocks_only.db")
        from journal import init_db
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO ai_predictions (timestamp, symbol, "
            "predicted_signal, confidence, reasoning, "
            "price_at_prediction, status, actual_outcome) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("2026-05-10T10:00:00", "AAPL", "BUY", 70, "test", 100,
             "resolved", "win"),
        )
        conn.commit()
        conn.close()
        from tuning import option as option_tuning
        wr, n = option_tuning.current_win_rate(db)
        assert wr == 0.0
        assert n == 0


class TestPipelineTuneWiring:
    """`pipelines/{stock,option}.py:tune()` are wired in Phase 2.
    Each returns a ParameterAdjustments DTO with the right
    pipeline_name and a rationale referencing the per-pipeline
    win rate."""

    def test_stock_pipeline_tune(self, db_with_mixed_predictions):
        from pipelines.stock import StockPipeline
        from pipelines import Metrics
        ctx = SimpleNamespace(db_path=db_with_mixed_predictions)
        adj = StockPipeline().tune(ctx, Metrics(pipeline_name="stock"))
        assert adj.pipeline_name == "stock"
        # Rationale mentions the stock-only win rate (60.0%, not 40%)
        assert "60.0%" in adj.rationale
        assert "5 resolved" in adj.rationale

    def test_option_pipeline_tune(self, db_with_mixed_predictions):
        from pipelines.option import OptionPipeline
        from pipelines import Metrics
        ctx = SimpleNamespace(db_path=db_with_mixed_predictions)
        adj = OptionPipeline().tune(ctx, Metrics(pipeline_name="option"))
        assert adj.pipeline_name == "option"
        assert "20.0%" in adj.rationale

    def test_pipeline_tune_no_db_path_returns_empty_rationale(self):
        from pipelines.stock import StockPipeline
        from pipelines import Metrics
        ctx = SimpleNamespace()
        adj = StockPipeline().tune(ctx, Metrics(pipeline_name="stock"))
        # No crash; empty changes; rationale is "" because no read happened
        assert adj.pipeline_name == "stock"
        assert adj.changes == {}
        assert adj.rationale == ""


class TestSignalTypeCoverage:
    """The signal-type lists must cover what the prod pipelines
    actually emit. If a new signal_type is added (e.g., a future
    OPTION_OPEN single-leg signal) it must be assigned to the right
    pipeline's tuning module — or the cross-instrument bug class
    re-emerges via the unrouted signal."""

    def test_stock_and_option_signal_types_disjoint(self):
        from tuning.stock import STOCK_SIGNAL_TYPES
        from tuning.option import OPTION_SIGNAL_TYPES
        overlap = set(STOCK_SIGNAL_TYPES) & set(OPTION_SIGNAL_TYPES)
        assert overlap == set(), (
            f"Signal types must belong to exactly one pipeline. "
            f"Overlap: {overlap}"
        )

    def test_no_pair_trade_in_either(self):
        """PAIR_OPEN/PAIR_CLOSE belong to a future PairPipeline,
        not stock or option. If they sneak in here they'd corrupt
        either pipeline's win rate."""
        from tuning.stock import STOCK_SIGNAL_TYPES
        from tuning.option import OPTION_SIGNAL_TYPES
        for sig in ("PAIR_OPEN", "PAIR_CLOSE", "PAIR_TRADE"):
            assert sig not in STOCK_SIGNAL_TYPES
            assert sig not in OPTION_SIGNAL_TYPES
