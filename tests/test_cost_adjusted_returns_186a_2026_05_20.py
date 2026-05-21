"""#186 Phase A — cost-adjusted return field on ai_predictions
(2026-05-20).

Adds `actual_return_pct_net` column populated by the resolver alongside
the existing `actual_return_pct`. Net = gross - estimated_round_trip_cost
where round-trip cost = 2 × entry_slippage_pct (from the matched entry
trade row).

Tests pin:
  1. Migration: the new column gets added by _migrate_all_columns
  2. Helper: _estimate_round_trip_cost_pct returns 2x entry slippage
     when matching trade exists; 0 when not
  3. Helper: 0 for option signals (option resolver handles costs
     separately at the premium level)
  4. Resolver writes both columns; net is gross - cost
  5. Net is MORE NEGATIVE for losses (cost adds to loss magnitude)
  6. Net equals gross when no matching trade (unmatched / paper-only
     predictions where AI emitted a signal but execution was blocked)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1. Migration adds the new column
# ---------------------------------------------------------------------------

class TestMigrationAddsColumn:
    def test_actual_return_pct_net_present_after_init(self, tmp_path):
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        with closing(sqlite3.connect(db)) as conn:
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)")
            }
        assert "actual_return_pct_net" in cols, (
            "Migration didn't add actual_return_pct_net. Check "
            "_migrate_all_columns ai_predictions list in journal.py."
        )

    def test_migration_idempotent(self, tmp_path):
        """Re-init must not error or duplicate the column."""
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        # Idempotent
        init_db(db)
        with closing(sqlite3.connect(db)) as conn:
            cols = [
                r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)")
            ]
        assert cols.count("actual_return_pct_net") == 1


# ---------------------------------------------------------------------------
# 2-3. _estimate_round_trip_cost_pct helper
# ---------------------------------------------------------------------------

class TestRoundTripCostEstimator:
    def _make_db_with_trade(self, tmp_path, *, symbol, side, slippage_pct,
                              ts_offset_min=0):
        """Build a DB with one trade row near the prediction timestamp."""
        from journal import init_db
        db = str(tmp_path / f"p_{symbol}.db")
        init_db(db)
        pred_ts = datetime.utcnow()
        trade_ts = pred_ts + timedelta(minutes=ts_offset_min)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO trades(timestamp, symbol, side, qty, price, "
                "slippage_pct, status, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_ts.isoformat(), symbol, side, 10, 100.0,
                 slippage_pct, "open", "ord-1"),
            )
            conn.commit()
        return db, pred_ts.isoformat()

    def test_returns_double_entry_slippage_for_buy(self, tmp_path):
        """A BUY prediction matched to an entry trade with 0.15%
        slippage should produce a round-trip cost of 0.30%."""
        from ai_tracker import _estimate_round_trip_cost_pct
        db, ts = self._make_db_with_trade(
            tmp_path, symbol="AAPL", side="buy", slippage_pct=0.15,
            ts_offset_min=1,
        )
        prediction = {
            "symbol": "AAPL", "predicted_signal": "BUY", "timestamp": ts,
        }
        cost = _estimate_round_trip_cost_pct(prediction, db)
        assert cost == pytest.approx(0.30, abs=1e-6), (
            f"Expected 2 × 0.15 = 0.30; got {cost}"
        )

    def test_returns_double_entry_slippage_for_short(self, tmp_path):
        """A SHORT prediction matched to a sell entry with 0.20%
        slippage should produce a 0.40% round-trip cost."""
        from ai_tracker import _estimate_round_trip_cost_pct
        db, ts = self._make_db_with_trade(
            tmp_path, symbol="MSFT", side="sell", slippage_pct=0.20,
            ts_offset_min=1,
        )
        prediction = {
            "symbol": "MSFT", "predicted_signal": "SHORT", "timestamp": ts,
        }
        cost = _estimate_round_trip_cost_pct(prediction, db)
        assert cost == pytest.approx(0.40, abs=1e-6)

    def test_returns_zero_when_no_matching_trade(self, tmp_path):
        """AI emitted BUY but execution was blocked — no trade row.
        Cost is 0 (paper prediction)."""
        from ai_tracker import _estimate_round_trip_cost_pct
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        prediction = {
            "symbol": "AAPL", "predicted_signal": "BUY",
            "timestamp": datetime.utcnow().isoformat(),
        }
        assert _estimate_round_trip_cost_pct(prediction, db) == 0.0

    def test_returns_zero_for_option_signals(self, tmp_path):
        """Option resolver handles costs at the premium level; this
        helper returns 0 for option signals to avoid double-counting."""
        from ai_tracker import _estimate_round_trip_cost_pct
        from journal import init_db
        db = str(tmp_path / "p.db")
        init_db(db)
        prediction = {
            "symbol": "AAPL", "predicted_signal": "MULTILEG_OPEN",
            "timestamp": datetime.utcnow().isoformat(),
        }
        assert _estimate_round_trip_cost_pct(prediction, db) == 0.0

    def test_window_excludes_unrelated_later_trade(self, tmp_path):
        """A trade made hours later on the same symbol must NOT match —
        it's a different decision, not the round-trip of this prediction."""
        from ai_tracker import _estimate_round_trip_cost_pct
        db, ts = self._make_db_with_trade(
            tmp_path, symbol="AAPL", side="buy", slippage_pct=0.50,
            ts_offset_min=60,  # 1 hour later — outside the ±10min window
        )
        prediction = {
            "symbol": "AAPL", "predicted_signal": "BUY", "timestamp": ts,
        }
        assert _estimate_round_trip_cost_pct(prediction, db) == 0.0

    def test_uses_absolute_slippage(self, tmp_path):
        """slippage_pct can be negative (favorable fill) or positive
        (unfavorable). Cost is the absolute magnitude × 2 — favorable
        slippage is rare but still a real round-trip cost we model."""
        from ai_tracker import _estimate_round_trip_cost_pct
        db, ts = self._make_db_with_trade(
            tmp_path, symbol="AAPL", side="buy", slippage_pct=-0.10,
            ts_offset_min=1,
        )
        prediction = {
            "symbol": "AAPL", "predicted_signal": "BUY", "timestamp": ts,
        }
        cost = _estimate_round_trip_cost_pct(prediction, db)
        assert cost == pytest.approx(0.20, abs=1e-6)


# ---------------------------------------------------------------------------
# 4-6. Resolver writes both columns; net = gross - cost; loss-side sign
# ---------------------------------------------------------------------------

class TestResolverWritesNetReturn:
    def _seed_prediction(self, db_path, *, symbol, signal, ts,
                          price_at_prediction):
        from ai_tracker import init_tracker_db
        init_tracker_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO ai_predictions("
                "timestamp, symbol, predicted_signal, confidence, "
                "reasoning, price_at_prediction, status, "
                "prediction_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, symbol, signal, 75, "test",
                 price_at_prediction, "pending",
                 "directional_long" if signal == "BUY" else "directional_short"),
            )
            conn.commit()
            return cursor.lastrowid

    def _seed_matching_trade(self, db_path, *, symbol, side, slippage_pct,
                               ts):
        """Trade row that the cost helper will match."""
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO trades(timestamp, symbol, side, qty, price, "
                "slippage_pct, status, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, symbol, side, 10, 100.0,
                 slippage_pct, "open", "ord-x"),
            )
            conn.commit()

    def test_resolver_writes_both_columns(self, tmp_path, monkeypatch):
        """When a prediction resolves, both actual_return_pct (gross)
        and actual_return_pct_net (cost-adjusted) get written."""
        from journal import init_db
        from ai_tracker import resolve_predictions
        # Far enough in the past to clear MIN_HOLD_DAYS_BEFORE_RESOLVE
        # (defaults to 3 trading days; use 7 calendar days)
        pred_ts = (datetime.utcnow() - timedelta(days=14)).isoformat()
        db = str(tmp_path / "p.db")
        init_db(db)
        pid = self._seed_prediction(
            db, symbol="AAPL", signal="BUY", ts=pred_ts,
            price_at_prediction=100.0,
        )
        self._seed_matching_trade(
            db, symbol="AAPL", side="buy",
            slippage_pct=0.10, ts=pred_ts,
        )
        # Stub the bulk price fetch to return a price that produces +5% gross
        monkeypatch.setattr(
            "ai_tracker._get_current_prices_bulk",
            lambda symbols, api=None: {s: 105.0 for s in symbols},
        )
        # Stub Alpaca API
        from unittest.mock import MagicMock
        api = MagicMock()
        resolve_predictions(api=api, db_path=db, profile_id=None)
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT actual_return_pct, actual_return_pct_net "
                "FROM ai_predictions WHERE id = ?", (pid,),
            ).fetchone()
        assert row is not None
        gross, net = row
        assert gross == pytest.approx(5.0, abs=0.01), (
            f"Gross should be +5%; got {gross}"
        )
        # Cost = 2 × 0.10 = 0.20; net = 5.0 - 0.20 = 4.80
        assert net == pytest.approx(4.80, abs=0.01), (
            f"Net should be gross - cost = 5.0 - 0.20 = 4.80; got {net}"
        )

    def test_loss_becomes_more_negative_after_cost(self, tmp_path, monkeypatch):
        """For a losing prediction, the cost ADDS to the loss magnitude
        (we lose on the price move AND pay the round-trip cost)."""
        from journal import init_db
        from ai_tracker import resolve_predictions
        from unittest.mock import MagicMock
        pred_ts = (datetime.utcnow() - timedelta(days=14)).isoformat()
        db = str(tmp_path / "p.db")
        init_db(db)
        pid = self._seed_prediction(
            db, symbol="MSFT", signal="BUY", ts=pred_ts,
            price_at_prediction=100.0,
        )
        self._seed_matching_trade(
            db, symbol="MSFT", side="buy",
            slippage_pct=0.20, ts=pred_ts,
        )
        monkeypatch.setattr(
            "ai_tracker._get_current_prices_bulk",
            lambda symbols, api=None: {s: 97.0 for s in symbols},
        )
        api = MagicMock()
        resolve_predictions(api=api, db_path=db, profile_id=None)
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT actual_return_pct, actual_return_pct_net "
                "FROM ai_predictions WHERE id = ?", (pid,),
            ).fetchone()
        gross, net = row
        # Gross: (97 - 100) / 100 = -3%
        assert gross == pytest.approx(-3.0, abs=0.01)
        # Cost = 2 × 0.20 = 0.40; net = -3.0 - 0.40 = -3.40
        assert net == pytest.approx(-3.40, abs=0.01), (
            "Loss must become MORE negative once costs are subtracted "
            "(we paid the round-trip slippage on top of the price loss)."
        )

    def test_net_equals_gross_when_no_matching_trade(self, tmp_path,
                                                       monkeypatch):
        """Prediction was emitted but execution was blocked (pre-filter,
        blacklist, cash, etc.). No trade row → cost = 0 → net = gross."""
        from journal import init_db
        from ai_tracker import resolve_predictions
        from unittest.mock import MagicMock
        pred_ts = (datetime.utcnow() - timedelta(days=14)).isoformat()
        db = str(tmp_path / "p.db")
        init_db(db)
        pid = self._seed_prediction(
            db, symbol="GOOG", signal="BUY", ts=pred_ts,
            price_at_prediction=200.0,
        )
        # NO matching trade
        monkeypatch.setattr(
            "ai_tracker._get_current_prices_bulk",
            lambda symbols, api=None: {s: 210.0 for s in symbols},
        )
        api = MagicMock()
        resolve_predictions(api=api, db_path=db, profile_id=None)
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT actual_return_pct, actual_return_pct_net "
                "FROM ai_predictions WHERE id = ?", (pid,),
            ).fetchone()
        gross, net = row
        assert gross == pytest.approx(5.0, abs=0.01)
        assert net == pytest.approx(gross, abs=1e-6), (
            "With no matching trade row, cost = 0 and net should "
            "equal gross. (Paper prediction — the AI emitted a "
            "directional view but no trade actually executed, so "
            "there's no real round-trip cost to subtract.)"
        )
