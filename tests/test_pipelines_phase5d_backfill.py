"""Phase 5d of the instrument-class pipeline refactor (2026-05-11).

Phase 5d backfills historical option prediction rows that were
resolved with the broken pre-Phase-5c math. The backfill:
  1. Finds rows where pipeline_kind='option' AND status='resolved'
     AND option_order_id IS NULL AND occ_symbol IS NULL.
  2. Looks up matching trades in the trades table within ±60 min.
  3. Populates option_order_id (multileg via combo_id from reason
     string OR order_id) or occ_symbol (single-leg from trade row).
  4. Resets the row to 'pending' with NULL actual_return_pct so
     the Phase 5c resolver re-resolves it correctly.
  5. Marks the migration done so subsequent calls no-op.

Auto-runs at multi_scheduler startup. Idempotency is double-gated:
- migration_markers table check (skips on subsequent calls).
- WHERE clause itself filters out already-linked rows (safe even
  with force=True).

This file pins:
- MULTILEG MATCHING: prediction matched to trade by symbol +
  signal + ±60min window; combo_id extracted from reason
  string (combo path) or order_id (sequential path).
- SINGLE-LEG MATCHING: occ_symbol extracted from a non-MULTILEG
  trade with the same symbol within window.
- RESET BEHAVIOR: linked row gets status='pending',
  actual_return_pct=NULL, actual_outcome=NULL — so Phase 5c
  resolver picks it up next cycle.
- IDEMPOTENCY: second call returns skipped_already_done=1 (no
  scan); force=True bypasses the marker but still self-gates via
  the WHERE clause (no double-link).
- NO-MATCH SAFETY: predictions with no matching trade in window
  remain unchanged (counted as no_match).
- MIGRATION MARKER: is_migration_done / mark_migration_done
  helpers behave correctly.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.outcomes import backfill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_resolved_option_pred(db_path, *, symbol, signal,
                                  pred_ts, wrong_return_pct=42.0,
                                  wrong_outcome="win"):
    """Insert a pre-Phase-5c historical option row: resolved with
    a wrong actual_return_pct, no occ_symbol/option_order_id."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence,
                reasoning, price_at_prediction, status, actual_outcome,
                actual_return_pct, resolved_at, resolution_price,
                pipeline_kind, occ_symbol, option_order_id)
               VALUES (?, ?, ?, 60, 'historical', 1.20, 'resolved',
                       ?, ?, ?, 0.0, 'option', NULL, NULL)""",
            (pred_ts, symbol, signal, wrong_outcome, wrong_return_pct,
             pred_ts),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_trade(db_path, *, symbol, signal_type, ts,
                   order_id=None, occ_symbol=None, reason=None,
                   side="buy", qty=1, price=1.0):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO trades
               (timestamp, symbol, side, qty, price, fill_price,
                order_id, signal_type, reason, status,
                occ_symbol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'filled', ?)""",
            (ts, symbol, side, qty, price, price, order_id,
             signal_type, reason, occ_symbol),
        )
        conn.commit()
    finally:
        conn.close()


def _row(db_path, pred_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM ai_predictions WHERE id = ?", (pred_id,),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Multileg matching — combo_id extraction from reason string OR order_id
# ---------------------------------------------------------------------------

class TestMultilegBackfill:
    def test_combo_path_links_via_order_id(self, db_path):
        """In combo path, every leg shares the parent's order_id.
        Backfill picks up that order_id directly."""
        pred_ts = datetime.utcnow().isoformat()
        pid = _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=2)).isoformat()
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=trade_ts, order_id="combo-parent-123",
            reason="bull_put_spread leg 1/2 (combo=combo-parent-123)",
            occ_symbol="AAPL  260612P00150000",
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        assert counts["linked_multileg"] == 1
        row = _row(db_path, pid)
        assert row["option_order_id"] == "combo-parent-123"
        assert row["status"] == "pending"
        assert row["actual_return_pct"] is None
        assert row["actual_outcome"] is None

    def test_combo_id_extracted_from_reason_when_order_id_differs(
        self, db_path,
    ):
        """In sequential path, each leg has its OWN order_id, but
        the parent combo id is in the reason string. Backfill
        prefers the combo id from the reason."""
        pred_ts = datetime.utcnow().isoformat()
        pid = _insert_resolved_option_pred(
            db_path, symbol="CWAN", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=1)).isoformat()
        _insert_trade(
            db_path, symbol="CWAN", signal_type="MULTILEG",
            ts=trade_ts,
            order_id="leg-own-id-456",   # leg's own id (sequential)
            reason="bull_put_spread leg 1/2 (combo=combo-real-789)",
            occ_symbol="CWAN  260612P00050000",
        )
        backfill.backfill_historical_option_predictions(db_path)
        row = _row(db_path, pid)
        # Backfill prefers the parent combo id from reason string
        assert row["option_order_id"] == "combo-real-789"

    def test_no_matching_trade_leaves_row_alone(self, db_path):
        """Prediction with no matching trade in window → counted as
        no_match; row stays in its (wrong) resolved state until a
        future backfill iteration finds a match."""
        pred_ts = datetime.utcnow().isoformat()
        pid = _insert_resolved_option_pred(
            db_path, symbol="NOTRADE", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        assert counts["no_match"] == 1
        row = _row(db_path, pid)
        # Row stays resolved with wrong values (no harm done)
        assert row["status"] == "resolved"
        assert row["option_order_id"] is None

    def test_trade_outside_window_not_matched(self, db_path):
        """Trade more than ±60min from prediction → not matched."""
        pred_ts = datetime.utcnow().isoformat()
        _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        far_trade_ts = (datetime.utcnow()
                         + timedelta(hours=3)).isoformat()
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=far_trade_ts, order_id="too-late",
            reason="too late (combo=too-late)",
            occ_symbol="AAPL  260612P00150000",
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        assert counts["no_match"] == 1
        assert counts["linked_multileg"] == 0


# ---------------------------------------------------------------------------
# Single-leg matching
# ---------------------------------------------------------------------------

class TestSingleLegBackfill:
    def test_single_leg_links_occ_symbol(self, db_path):
        pred_ts = datetime.utcnow().isoformat()
        pid = _insert_resolved_option_pred(
            db_path, symbol="MSFT", signal="OPTIONS",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=3)).isoformat()
        _insert_trade(
            db_path, symbol="MSFT", signal_type="OPTIONS",
            ts=trade_ts, order_id="opt-order-1",
            occ_symbol="MSFT  260612C00400000",
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        assert counts["linked_single_leg"] == 1
        row = _row(db_path, pid)
        assert row["occ_symbol"] == "MSFT  260612C00400000"
        assert row["status"] == "pending"

    def test_multileg_trade_not_used_for_single_leg(self, db_path):
        """A MULTILEG trade row's occ_symbol should NOT be used to
        link a single-leg OPTIONS prediction (different signal
        types — would corrupt the linkage)."""
        pred_ts = datetime.utcnow().isoformat()
        _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="OPTIONS",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=1)).isoformat()
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=trade_ts, order_id="ml-1",
            occ_symbol="AAPL  260612P00150000",
            reason="leg (combo=ml-1)",
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        # Single-leg query specifically excludes MULTILEG trades.
        assert counts["linked_single_leg"] == 0
        assert counts["no_match"] == 1


# ---------------------------------------------------------------------------
# Idempotency — marker-gated + self-gating WHERE clause
# ---------------------------------------------------------------------------

class TestBackfillIdempotency:
    def test_second_call_skips_via_marker(self, db_path):
        pred_ts = datetime.utcnow().isoformat()
        _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=1)).isoformat()
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=trade_ts, order_id="combo-1",
            reason="leg (combo=combo-1)",
            occ_symbol="AAPL  260612P00150000",
        )
        first = backfill.backfill_historical_option_predictions(db_path)
        assert first["linked_multileg"] == 1
        # Second call: marker is set → returns immediately
        second = backfill.backfill_historical_option_predictions(db_path)
        assert second["skipped_already_done"] == 1
        assert second["scanned"] == 0

    def test_force_true_bypasses_marker_but_self_gates(self, db_path):
        """force=True ignores the marker but the WHERE clause still
        excludes already-linked rows — re-running with force is
        safe (no double-write)."""
        pred_ts = datetime.utcnow().isoformat()
        _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        trade_ts = (datetime.utcnow()
                     + timedelta(minutes=1)).isoformat()
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=trade_ts, order_id="combo-1",
            reason="leg (combo=combo-1)",
            occ_symbol="AAPL  260612P00150000",
        )
        backfill.backfill_historical_option_predictions(db_path)
        # Force re-run — marker bypassed but WHERE clause excludes
        # the now-linked row (option_order_id IS NOT NULL)
        counts = backfill.backfill_historical_option_predictions(
            db_path, force=True,
        )
        assert counts["scanned"] == 0


# ---------------------------------------------------------------------------
# migration_markers helpers
# ---------------------------------------------------------------------------

class TestMigrationMarkerHelpers:
    def test_is_migration_done_initially_false(self, db_path):
        from journal import is_migration_done
        assert is_migration_done(db_path, "test_key") is False

    def test_mark_then_check(self, db_path):
        from journal import is_migration_done, mark_migration_done
        mark_migration_done(db_path, "test_key", details="phase X")
        assert is_migration_done(db_path, "test_key") is True

    def test_mark_idempotent(self, db_path):
        from journal import mark_migration_done
        # Second mark MUST NOT error — INSERT OR REPLACE
        assert mark_migration_done(db_path, "k", details="d1") is True
        assert mark_migration_done(db_path, "k", details="d2") is True

    def test_no_db_path_safe(self):
        from journal import is_migration_done, mark_migration_done
        assert is_migration_done(None, "k") is False
        assert mark_migration_done(None, "k") is False


# ---------------------------------------------------------------------------
# Defensive: no_db_path / empty DB
# ---------------------------------------------------------------------------

class TestBackfillSafety:
    def test_empty_db_returns_zero_counts(self, db_path):
        counts = backfill.backfill_historical_option_predictions(db_path)
        # Empty DB: scanned=0, all linked=0, no_match=0
        assert counts["scanned"] == 0
        assert counts["linked_multileg"] == 0
        assert counts["linked_single_leg"] == 0

    def test_no_db_path_returns_zero(self):
        counts = backfill.backfill_historical_option_predictions("")
        assert counts["scanned"] == 0

    def test_does_not_touch_already_linked_rows(self, db_path):
        """Pre-existing linked option rows (Phase 5c flow) must NOT
        be reset by the Phase 5d backfill."""
        # Insert a row that's already linked + resolved (the Phase
        # 5c-correct end state).
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO ai_predictions
                   (timestamp, symbol, predicted_signal, confidence,
                    reasoning, price_at_prediction, status,
                    actual_outcome, actual_return_pct,
                    pipeline_kind, occ_symbol, option_order_id)
                   VALUES (datetime('now'), 'AAPL', 'MULTILEG_OPEN',
                           70, 'phase 5c row', 0.50, 'resolved',
                           'win', 25.0, 'option', NULL,
                           'combo-already-linked')"""
            )
            conn.commit()
        finally:
            conn.close()
        backfill.backfill_historical_option_predictions(db_path)
        row = sqlite3.connect(db_path).execute(
            "SELECT status, actual_return_pct FROM ai_predictions"
        ).fetchone()
        # Untouched: status stays resolved, return_pct stays 25.0
        assert row[0] == "resolved"
        assert row[1] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Mixed-batch — multileg + single-leg + no-match in one run
# ---------------------------------------------------------------------------

class TestMixedBatch:
    def test_mixed_signals_classified_correctly(self, db_path):
        pred_ts = datetime.utcnow().isoformat()
        # Multileg with matching trade
        _insert_resolved_option_pred(
            db_path, symbol="AAPL", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        _insert_trade(
            db_path, symbol="AAPL", signal_type="MULTILEG",
            ts=(datetime.utcnow()
                 + timedelta(minutes=2)).isoformat(),
            order_id="combo-A", reason="leg (combo=combo-A)",
            occ_symbol="AAPL  260612P00150000",
        )
        # Single-leg with matching trade
        _insert_resolved_option_pred(
            db_path, symbol="MSFT", signal="OPTIONS",
            pred_ts=pred_ts,
        )
        _insert_trade(
            db_path, symbol="MSFT", signal_type="OPTIONS",
            ts=(datetime.utcnow()
                 + timedelta(minutes=2)).isoformat(),
            occ_symbol="MSFT  260612C00400000",
        )
        # Multileg with NO matching trade
        _insert_resolved_option_pred(
            db_path, symbol="ZZZZ", signal="MULTILEG_OPEN",
            pred_ts=pred_ts,
        )
        counts = backfill.backfill_historical_option_predictions(db_path)
        assert counts["scanned"] == 3
        assert counts["linked_multileg"] == 1
        assert counts["linked_single_leg"] == 1
        assert counts["no_match"] == 1
