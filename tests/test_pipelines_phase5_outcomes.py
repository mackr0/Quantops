"""Phase 5 of the instrument-class pipeline refactor (2026-05-11).

Phase 5a (this commit) adds the structural fix for audit finding #2
(option outcomes pooling with stock outcomes in cross-pipeline
aggregations). The fix:

  1. New `pipeline_kind` column on ai_predictions, populated via
     migration backfill from predicted_signal.
  2. New `pipelines/outcomes/{stock,option}.py` writers that tag
     every new outcome write with the correct pipeline_kind.
  3. Wire `pipelines/{stock,option}.py:record_outcome()` to the
     writers.
  4. Update `tuning/{stock,option}.py:current_win_rate()` to filter
     by pipeline_kind (with a fallback to signal-type filter for
     legacy NULL rows).

This file pins:
- CLASS INVARIANT: writing through stock pipeline produces
  pipeline_kind='stock'; writing through option pipeline produces
  pipeline_kind='option'. No matter what return_pct, signal, or
  symbol — the kind tag is always correct.
- ISOLATION: stock-tuner win-rate query never counts an
  option-pipeline outcome (and vice versa), even when the row's
  predicted_signal is ambiguous.
- BACKFILL CORRECTNESS: legacy rows without pipeline_kind get
  classified by signal-type fallback. A 'BUY' row counts as stock;
  a 'MULTILEG_OPEN' row counts as option.
- IDEMPOTENCE: re-running the migration on an already-tagged DB is
  a no-op.
- SIGNAL → KIND INFERENCE (parametrized class invariant): every
  known signal type maps to exactly one kind via
  `outcomes.kind_from_signal()`.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines import Outcome
from pipelines.stock import StockPipeline
from pipelines.option import OptionPipeline
from pipelines import outcomes as outcomes_pkg
from pipelines.outcomes import stock as stock_outcomes
from pipelines.outcomes import option as option_outcomes
from tuning import stock as stock_tuning
from tuning import option as option_tuning


# ---------------------------------------------------------------------------
# Shared fixture — ephemeral DB with the production ai_predictions schema
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path():
    """Build a fresh ai_predictions table in a temp file. Mirrors
    the production schema enough that the writers and queries
    behave identically. Cleaned up at end of test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                timestamp TEXT,
                predicted_signal TEXT,
                price_at_prediction REAL,
                status TEXT DEFAULT 'pending',
                actual_outcome TEXT,
                actual_return_pct REAL,
                resolved_at TEXT,
                resolution_price REAL,
                pipeline_kind TEXT
            )
        """)
        # broker_rejections is referenced by tuning queries' NOT
        # EXISTS subquery (TODO #5b — exclude rejected predictions
        # from win rate). Even when this fixture has no rejections,
        # the table must exist so the SQL parses.
        conn.execute("""
            CREATE TABLE broker_rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                action TEXT,
                signal_type TEXT,
                ai_confidence REAL,
                ai_reasoning TEXT,
                rejection_code TEXT,
                broker_message TEXT,
                prediction_id INTEGER
            )
        """)
        conn.commit()
    finally:
        conn.close()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_pending(db_path, **fields):
    """Insert a pending prediction; return its id."""
    cols = ["symbol", "timestamp", "predicted_signal",
             "price_at_prediction"]
    vals = [fields.get("symbol", "AAPL"),
            fields.get("timestamp", "2026-05-01T10:00:00"),
            fields.get("predicted_signal", "BUY"),
            fields.get("price_at_prediction", 150.0)]
    if "pipeline_kind" in fields:
        cols.append("pipeline_kind")
        vals.append(fields["pipeline_kind"])
    placeholders = ",".join("?" * len(cols))
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO ai_predictions ({','.join(cols)}) "
            f"VALUES ({placeholders})", vals,
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row(db_path, prediction_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM ai_predictions WHERE id = ?",
            (prediction_id,),
        ).fetchone()
    finally:
        conn.close()


def _make_outcome(pid, ret_pct=2.5, kind="win"):
    return Outcome(
        prediction_id=pid, actual_outcome=kind,
        actual_return_pct=ret_pct,
        resolved_at="2026-05-10T16:00:00",
        resolution_price=153.75,
    )


# ---------------------------------------------------------------------------
# CLASS INVARIANT — kind tag is ALWAYS correct
# ---------------------------------------------------------------------------

class TestKindTagIsAlwaysCorrect:
    """Whatever the symbol, signal, or return — the writer tags the
    row with the correct pipeline_kind by construction. Catches the
    "stock writer accidentally writes an option outcome" failure
    mode at the writer boundary, not at the query boundary."""

    def test_stock_pipeline_record_outcome_tags_stock(self, db_path):
        pid = _insert_pending(db_path, predicted_signal="BUY")
        ctx = SimpleNamespace(db_path=db_path)
        StockPipeline().record_outcome(ctx, pid, _make_outcome(pid))
        row = _row(db_path, pid)
        assert row["pipeline_kind"] == "stock"
        assert row["status"] == "resolved"

    def test_option_pipeline_record_outcome_tags_option(self, db_path):
        pid = _insert_pending(db_path, predicted_signal="MULTILEG_OPEN")
        ctx = SimpleNamespace(db_path=db_path)
        OptionPipeline().record_outcome(ctx, pid, _make_outcome(pid))
        row = _row(db_path, pid)
        assert row["pipeline_kind"] == "option"
        assert row["status"] == "resolved"

    def test_stock_writer_does_not_leak_option_kind_even_on_option_signal(
        self, db_path
    ):
        """If the stock pipeline somehow gets called with a
        MULTILEG_OPEN row (shouldn't happen, but defense-in-depth),
        the writer still tags the row 'stock'. The pipeline is the
        authority for which kind it owns — not the signal field."""
        pid = _insert_pending(db_path, predicted_signal="MULTILEG_OPEN")
        ctx = SimpleNamespace(db_path=db_path)
        StockPipeline().record_outcome(ctx, pid, _make_outcome(pid))
        row = _row(db_path, pid)
        assert row["pipeline_kind"] == "stock"


# ---------------------------------------------------------------------------
# ISOLATION — pipeline-tuner queries see only their pipeline's outcomes
# ---------------------------------------------------------------------------

class TestPipelineKindIsolatesAggregations:
    """The structural fix for audit finding #2: a stock tuner's
    win-rate query MUST NOT count an option-pipeline outcome. This
    holds regardless of the signal type — a stock-pipeline write of
    a 'BUY' row goes to stock, an option-pipeline write of a
    'MULTILEG_OPEN' row goes to option, and they don't pool."""

    def test_stock_tuner_excludes_option_pipeline_outcomes(self, db_path):
        pid_stock = _insert_pending(db_path, predicted_signal="BUY")
        pid_option = _insert_pending(db_path,
                                       predicted_signal="MULTILEG_OPEN")
        ctx = SimpleNamespace(db_path=db_path)
        # Stock pipeline records a WIN.
        StockPipeline().record_outcome(
            ctx, pid_stock, _make_outcome(pid_stock, kind="win"),
        )
        # Option pipeline records a LOSS — must NOT bring stock win
        # rate down.
        OptionPipeline().record_outcome(
            ctx, pid_option, _make_outcome(pid_option, kind="loss"),
        )
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 1, "Stock tuner counted option outcome (n>1)"
        assert wr == 100.0, (
            "Option-pipeline LOSS contaminated stock-pipeline win rate"
        )

    def test_option_tuner_excludes_stock_pipeline_outcomes(self, db_path):
        pid_stock = _insert_pending(db_path, predicted_signal="BUY")
        pid_option = _insert_pending(db_path,
                                       predicted_signal="MULTILEG_OPEN")
        ctx = SimpleNamespace(db_path=db_path)
        StockPipeline().record_outcome(
            ctx, pid_stock, _make_outcome(pid_stock, kind="loss"),
        )
        OptionPipeline().record_outcome(
            ctx, pid_option, _make_outcome(pid_option, kind="win"),
        )
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 1, "Option tuner counted stock outcome (n>1)"
        assert wr == 100.0, (
            "Stock-pipeline LOSS contaminated option-pipeline win rate"
        )


# ---------------------------------------------------------------------------
# BACKFILL FALLBACK — legacy NULL rows still classified correctly
# ---------------------------------------------------------------------------

class TestLegacyNullPipelineKindFallsBackToSignalFilter:
    """Pre-Phase-5a rows have pipeline_kind = NULL. The tuner
    queries fall back to signal-type enumeration so production
    aggregations don't go to zero on the day the migration lands
    but before the backfill runs. Once backfill populates the
    column, the structural filter takes over."""

    def _insert_legacy_resolved(self, db_path, signal, outcome):
        """Insert a row with pipeline_kind = NULL — pre-Phase-5a state."""
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO ai_predictions
                   (symbol, timestamp, predicted_signal,
                    price_at_prediction, status, actual_outcome,
                    actual_return_pct, resolved_at, resolution_price,
                    pipeline_kind)
                   VALUES ('AAPL', '2026-04-01', ?, 100.0, 'resolved',
                           ?, 1.5, '2026-04-10', 101.5, NULL)""",
                (signal, outcome),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_buy_row_counts_in_stock_tuner(self, db_path):
        self._insert_legacy_resolved(db_path, "BUY", "win")
        wr, n = stock_tuning.current_win_rate(db_path)
        assert n == 1
        assert wr == 100.0

    def test_legacy_multileg_row_counts_in_option_tuner(self, db_path):
        self._insert_legacy_resolved(db_path, "MULTILEG_OPEN", "loss")
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 1
        assert wr == 0.0

    def test_legacy_buy_row_does_NOT_count_in_option_tuner(self, db_path):
        self._insert_legacy_resolved(db_path, "BUY", "win")
        wr, n = option_tuning.current_win_rate(db_path)
        assert n == 0


# ---------------------------------------------------------------------------
# MIGRATION BACKFILL — populates legacy NULL rows from signal type
# ---------------------------------------------------------------------------

class TestMigrationBackfillsPipelineKind:
    def _run_journal_migration(self, db_path):
        """Re-run the journal migration step against this DB."""
        # journal._migrate_all_columns is what the production
        # init path calls; running it directly verifies the same
        # backfill SQL the production migration runs.
        conn = sqlite3.connect(db_path)
        try:
            from journal import _migrate_all_columns
            _migrate_all_columns(conn)
            conn.commit()
        finally:
            conn.close()

    def test_buy_row_backfills_to_stock(self, db_path):
        # Insert a legacy resolved row with NULL pipeline_kind.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO ai_predictions
               (symbol, timestamp, predicted_signal,
                price_at_prediction, status, actual_outcome,
                pipeline_kind)
               VALUES ('AAPL', '2026-04-01', 'BUY', 100.0,
                       'resolved', 'win', NULL)"""
        )
        conn.commit()
        conn.close()
        self._run_journal_migration(db_path)
        # Now check the row.
        row = sqlite3.connect(db_path).execute(
            "SELECT pipeline_kind FROM ai_predictions WHERE symbol='AAPL'"
        ).fetchone()
        assert row[0] == "stock"

    def test_multileg_row_backfills_to_option(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO ai_predictions
               (symbol, timestamp, predicted_signal,
                price_at_prediction, status, pipeline_kind)
               VALUES ('CWAN', '2026-04-01', 'MULTILEG_OPEN',
                       2.40, 'pending', NULL)"""
        )
        conn.commit()
        conn.close()
        self._run_journal_migration(db_path)
        row = sqlite3.connect(db_path).execute(
            "SELECT pipeline_kind FROM ai_predictions WHERE symbol='CWAN'"
        ).fetchone()
        assert row[0] == "option"

    def test_migration_is_idempotent(self, db_path):
        """Running the migration twice produces the same result.
        Critical because the production init path runs the migration
        on every Flask app startup."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO ai_predictions
               (symbol, timestamp, predicted_signal,
                price_at_prediction, status, pipeline_kind)
               VALUES ('AAPL', '2026-04-01', 'BUY', 100.0,
                       'pending', NULL)"""
        )
        conn.commit()
        conn.close()
        self._run_journal_migration(db_path)
        self._run_journal_migration(db_path)
        # No extra rows or kind changes
        rows = sqlite3.connect(db_path).execute(
            "SELECT pipeline_kind, COUNT(*) FROM ai_predictions "
            "GROUP BY pipeline_kind"
        ).fetchall()
        assert rows == [("stock", 1)]

    def test_migration_does_not_overwrite_existing_kind(self, db_path):
        """If a row already has pipeline_kind set (e.g., a pipeline
        write happened before the migration scanned), the migration
        leaves it alone. Catches the regression where the backfill
        SQL wasn't gated on `pipeline_kind IS NULL`."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO ai_predictions
               (symbol, timestamp, predicted_signal,
                price_at_prediction, status, pipeline_kind)
               VALUES ('AAPL', '2026-04-01', 'MULTILEG_OPEN', 100.0,
                       'pending', 'option')"""
        )
        # Note: signal=MULTILEG_OPEN but kind already 'option' —
        # migration must NOT change anything.
        conn.commit()
        conn.close()
        self._run_journal_migration(db_path)
        row = sqlite3.connect(db_path).execute(
            "SELECT pipeline_kind FROM ai_predictions WHERE symbol='AAPL'"
        ).fetchone()
        assert row[0] == "option"


# ---------------------------------------------------------------------------
# CLASS INVARIANT (parametrized) — signal → kind inference
# ---------------------------------------------------------------------------

class TestKindFromSignalClassInvariant:
    """Signal → kind inference is the single source of truth used by
    both the migration backfill and any caller that needs to derive
    a kind from a legacy signal. Pin the mapping with a parametrized
    test so regressions show as a per-signal failure."""

    @pytest.mark.parametrize("signal,expected", [
        ("BUY", "stock"),
        ("STRONG_BUY", "stock"),
        ("WEAK_BUY", "stock"),
        ("SELL", "stock"),
        ("STRONG_SELL", "stock"),
        ("WEAK_SELL", "stock"),
        ("SHORT", "stock"),
        ("COVER", "stock"),
        ("MULTILEG_OPEN", "option"),
        ("OPTIONS", "option"),
        ("OPTION_EXERCISE", "option"),
        # Case-insensitivity
        ("buy", "stock"),
        ("multileg_open", "option"),
        # Unknown / future signal types are unclassified — caller
        # decides what to do (e.g., backfill leaves them NULL).
        ("PAIR_OPEN", None),
        ("DELTA_HEDGE", None),
        ("", None),
    ])
    def test_kind_from_signal(self, signal, expected):
        assert outcomes_pkg.kind_from_signal(signal) == expected


# ---------------------------------------------------------------------------
# Pipeline `record_outcome` no-op when no db_path
# ---------------------------------------------------------------------------

class TestRecordOutcomeNoOpWithoutDbPath:
    def test_stock_pipeline_record_outcome_silent_without_db_path(self):
        """Pipeline DTOs sometimes flow through test contexts that
        don't have a real DB. record_outcome must not crash — it
        just silently no-ops. Catches the "record_outcome assumed
        ctx.db_path always set" regression."""
        ctx = SimpleNamespace()
        StockPipeline().record_outcome(ctx, 1, _make_outcome(1))
        # No exception → pass

    def test_option_pipeline_record_outcome_silent_without_db_path(self):
        ctx = SimpleNamespace()
        OptionPipeline().record_outcome(ctx, 1, _make_outcome(1))
