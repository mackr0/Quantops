"""Tests for the equity-identity invariant (#166, 2026-05-17).

Master no-money-hiding check:
    equity == initial_capital + Σ(realized) + Σ(unrealized)

This is the journal proving its own algebra. Bugs that break the
identity:
  - FIFO matcher misattributing pnl
  - market_value computed differently than unrealized_pl
  - Hidden cash flows (dividend credit, fee debit, manual adjustment)
    affecting equity without a matching trade row

The 2026-05-13 cash-logic bugs (stock-short credit + options
multiplier) would have shown up here as the cash-vs-position
disagreement that they caused, even before #165 was added.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_profile_db(tmp_path, pid, trades, schema_extra=""):
    """Build a per-profile DB with a minimal trades table."""
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(f"""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, qty REAL, price REAL,
                pnl REAL, status TEXT, timestamp TEXT, occ_symbol TEXT
                {schema_extra}
            );
        """)
        conn.executemany(
            "INSERT INTO trades (symbol, side, qty, price, pnl, status, "
            "timestamp, occ_symbol) VALUES (?,?,?,?,?,?,?,?)",
            trades,
        )
    return str(db)


def _mock_ctx(profile_id, db_path, initial_capital=100_000.0):
    return SimpleNamespace(
        profile_id=profile_id,
        db_path=db_path,
        initial_capital=initial_capital,
        alpaca_account_id=None,
        api=None,
    )


# ─────────────────────────────────────────────────────────────────────
# Happy path: identity holds
# ─────────────────────────────────────────────────────────────────────

class TestIdentityHolds:
    def test_fresh_profile_no_trades(self, tmp_path):
        """Empty trades table: equity = initial_capital, drift = 0."""
        from integrity_audit import audit_equity_identity
        db = _make_profile_db(tmp_path, 1, [])
        ctx = _mock_ctx(1, db, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ):
            result = audit_equity_identity(1)
        assert result["has_drift"] is False
        assert result["initial_capital"] == 100_000.0
        assert result["realized_total"] == 0.0
        assert result["unrealized_total"] == 0.0
        assert result["expected_equity"] == 100_000.0
        assert result["actual_equity"] == 100_000.0

    def test_one_closed_round_trip_realized_pnl(self, tmp_path):
        """Buy 100 @ $50, sell 100 @ $60: realized=$1000, equity=$101k."""
        from integrity_audit import audit_equity_identity
        # In production the pnl column is on the BUY row after FIFO.
        # Schema mirrors that: BUY closed with pnl=1000, SELL closed
        # with pnl=NULL (cash side carries the proceeds).
        db = _make_profile_db(tmp_path, 1, [
            ("AAPL", "buy",  100, 50.0, 1000.0, "closed", "2026-05-01", None),
            ("AAPL", "sell", 100, 60.0, None,   "closed", "2026-05-02", None),
        ])
        ctx = _mock_ctx(1, db, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ):
            result = audit_equity_identity(1)
        assert result["realized_total"] == 1000.0
        assert result["unrealized_total"] == 0.0
        # expected = 100k + 1000 + 0 = 101k
        assert result["expected_equity"] == 101_000.0
        # actual cash = 100k - 100×50 + 100×60 = 101k, portfolio_value=0
        assert result["actual_equity"] == 101_000.0
        assert result["has_drift"] is False


# ─────────────────────────────────────────────────────────────────────
# Drift detection
# ─────────────────────────────────────────────────────────────────────

class TestIdentityBroken:
    def test_hidden_cash_flow_caught(self, tmp_path):
        """Simulate a $500 mystery debit (e.g. dividend credited by
        broker but not in trades). The journal's cash-from-trades will
        be off by $500 from what equity should be."""
        from integrity_audit import audit_equity_identity
        # No trades, but we override get_virtual_account_info to
        # return equity that doesn't match init_capital. (In reality
        # this would happen if some other code touched the trades
        # table or if the journal had non-trade cash entries.)
        db = _make_profile_db(tmp_path, 1, [])
        ctx = _mock_ctx(1, db, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_account_info",
            return_value={
                "equity": 99_500.0,  # $500 short!
                "cash": 99_500.0,
                "portfolio_value": 0.0,
                "buying_power": 99_500.0, "status": "ACTIVE",
            },
        ):
            result = audit_equity_identity(1)
        assert result["has_drift"] is True
        assert result["drift"] == -500.0
        assert result["expected_equity"] == 100_000.0
        assert result["actual_equity"] == 99_500.0

    def test_fifo_mismatch_caught(self, tmp_path):
        """Realized pnl column says +$1000 but the cash flow says +$2000
        — FIFO computed wrong. Identity catches the inconsistency."""
        from integrity_audit import audit_equity_identity
        # Buy + sell flows imply +$2000 realized
        # but pnl column wrongly says only +$1000
        db = _make_profile_db(tmp_path, 1, [
            ("AAPL", "buy",  100, 50.0, 1000.0, "closed", "2026-05-01", None),
            ("AAPL", "sell", 100, 70.0, None,   "closed", "2026-05-02", None),
        ])
        ctx = _mock_ctx(1, db, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ):
            result = audit_equity_identity(1)
        # actual cash = 100k - 5000 + 7000 = 102k
        # expected = 100k + 1000 (claimed realized) + 0 = 101k
        # drift = 102k - 101k = +1000 (cash side richer than pnl claims)
        assert result["actual_equity"] == 102_000.0
        assert result["expected_equity"] == 101_000.0
        assert result["drift"] == 1000.0
        assert result["has_drift"] is True


# ─────────────────────────────────────────────────────────────────────
# Batch wrapper + error handling
# ─────────────────────────────────────────────────────────────────────

class TestBatchAndErrors:
    def test_batch_separates_drift_and_errored(self, tmp_path):
        """audit_equity_identity_all sorts results into clean / drift / errored."""
        from integrity_audit import audit_equity_identity_all
        # pid 1: clean
        db1 = _make_profile_db(tmp_path, 1, [])
        # pid 2: drift
        db2 = _make_profile_db(tmp_path, 2, [])
        # pid 3: build_user_context raises

        def _build(pid):
            if pid == 1:
                return _mock_ctx(1, db1, 100_000.0)
            if pid == 2:
                return _mock_ctx(2, db2, 100_000.0)
            raise ValueError("no such profile")

        def _equity(db_path=None, initial_capital=0, price_fetcher=None):
            # Force pid 2's equity to mismatch
            if db_path == db2:
                return {"equity": 95_000.0, "cash": 95_000.0,
                        "portfolio_value": 0.0,
                        "buying_power": 95_000.0, "status": "ACTIVE"}
            return {"equity": initial_capital, "cash": initial_capital,
                    "portfolio_value": 0.0,
                    "buying_power": initial_capital, "status": "ACTIVE"}

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ), patch(
            "journal.get_virtual_account_info", side_effect=_equity,
        ):
            result = audit_equity_identity_all([1, 2, 3])

        assert len(result["profiles"]) == 3
        assert len(result["drift"]) == 1
        assert result["drift"][0]["profile_id"] == 2
        assert result["errored"] == [3]


# ─────────────────────────────────────────────────────────────────────
# issues_collector wiring
# ─────────────────────────────────────────────────────────────────────

class TestIssuesCollectorWiring:
    def test_identity_drift_surfaces_on_issues(self):
        """When equity identity is broken, /issues collects an ERROR row."""
        import issues_collector
        issues_collector._DRIFT_CACHE["ts"] = 0  # bypass TTL cache
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={
                "profiles": [],
                "drift": [{
                    "profile_id": 7, "initial_capital": 100_000.0,
                    "realized_total": 0.0, "unrealized_total": 0.0,
                    "expected_equity": 100_000.0,
                    "actual_equity": 99_500.0, "drift": -500.0,
                    "has_drift": True, "errored": None,
                }],
                "errored": [],
            },
        ):
            rows, err = issues_collector._collect_aggregate_drift(since_hours=24)
        assert err is None
        assert len(rows) == 1
        assert rows[0]["source"] == "equity_identity.profile_7"
        assert rows[0]["level"] == "ERROR"
        assert "drift=$-500.00" in rows[0]["message"]
