"""Tests for the cash-parity and basis-parity audits (#167, 2026-05-17).

Together these complete the cross-system integrity coverage:
  qty-parity     (existing)  share counts
  value-parity   (#165)       position dollars
  cash-parity    (#167a)      uninvested dollars
  basis-parity   (#167b)      per-share cost basis
  equity-identity (#166)      per-profile self-consistency
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _mock_ctx(profile_id, alpaca_account_id, db_path, api,
              initial_capital=100_000.0):
    return SimpleNamespace(
        profile_id=profile_id,
        alpaca_account_id=alpaca_account_id,
        db_path=db_path,
        api=api,
        initial_capital=initial_capital,
    )


# ─────────────────────────────────────────────────────────────────────
# Cash parity
# ─────────────────────────────────────────────────────────────────────

class TestCashParityNoDrift:
    def test_single_profile_perfect_match(self):
        from aggregate_audit import audit_account_cash_parity
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(cash=50_000.0)
        ctx = _mock_ctx(1, 10, "p1.db", api, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_account_info",
            return_value={"cash": 50_000.0, "equity": 0,
                          "portfolio_value": 0,
                          "buying_power": 50_000.0, "status": "ACTIVE"},
        ):
            result = audit_account_cash_parity([1])
        assert result["drift"] == []
        assert result["accounts"][10]["broker_cash"] == 50_000.0
        assert result["accounts"][10]["journal_cash"] == 50_000.0

    def test_multi_profile_sums_correctly(self):
        from aggregate_audit import audit_account_cash_parity
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(cash=300_000.0)
        ctx1 = _mock_ctx(1, 10, "p1.db", api, initial_capital=100_000.0)
        ctx2 = _mock_ctx(2, 10, "p2.db", api, initial_capital=100_000.0)
        ctx3 = _mock_ctx(3, 10, "p3.db", api, initial_capital=100_000.0)

        def _build(pid):
            return {1: ctx1, 2: ctx2, 3: ctx3}[pid]

        def _virtual(db_path=None, initial_capital=0, price_fetcher=None):
            # All three profiles have $100k cash each → broker = $300k
            return {"cash": initial_capital, "equity": 0,
                    "portfolio_value": 0,
                    "buying_power": initial_capital, "status": "ACTIVE"}

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ), patch(
            "journal.get_virtual_account_info", side_effect=_virtual,
        ):
            result = audit_account_cash_parity([1, 2, 3])
        assert result["drift"] == []
        assert result["accounts"][10]["journal_cash"] == 300_000.0


class TestCashParityDrift:
    def test_broker_cash_orphan_caught(self):
        """Broker received $500 dividend the journal doesn't know about."""
        from aggregate_audit import audit_account_cash_parity
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(cash=100_500.0)
        ctx = _mock_ctx(1, 10, "p1.db", api, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_account_info",
            return_value={"cash": 100_000.0, "equity": 0,
                          "portfolio_value": 0,
                          "buying_power": 100_000.0, "status": "ACTIVE"},
        ):
            result = audit_account_cash_parity([1])
        assert len(result["drift"]) == 1
        d = result["drift"][0]
        assert d["kind"] == "broker_cash_orphan"
        assert d["drift"] == 500.0

    def test_journal_cash_phantom_caught(self):
        """Journal thinks it has more cash than broker (trade hit broker
        but not journal, or initial_capital configured higher than the
        broker was actually funded with)."""
        from aggregate_audit import audit_account_cash_parity
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(cash=95_000.0)
        ctx = _mock_ctx(1, 10, "p1.db", api, initial_capital=100_000.0)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_account_info",
            return_value={"cash": 100_000.0, "equity": 0,
                          "portfolio_value": 0,
                          "buying_power": 100_000.0, "status": "ACTIVE"},
        ):
            result = audit_account_cash_parity([1])
        assert len(result["drift"]) == 1
        assert result["drift"][0]["kind"] == "journal_cash_phantom"
        assert result["drift"][0]["drift"] == -5_000.0


# ─────────────────────────────────────────────────────────────────────
# Basis parity
# ─────────────────────────────────────────────────────────────────────

class TestBasisParityNoDrift:
    def test_single_profile_matches_broker(self):
        from aggregate_audit import audit_account_basis_parity
        api = MagicMock()
        api.list_positions.return_value = [
            SimpleNamespace(symbol="AAPL", qty=100, avg_entry_price=150.00),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_positions",
            return_value=[{
                "symbol": "AAPL", "qty": 100, "avg_entry_price": 150.00,
                "occ_symbol": None,
            }],
        ):
            result = audit_account_basis_parity([1])
        assert result["drift"] == []
        accounts = result["accounts"]
        assert accounts[10]["AAPL"]["broker_avg"] == 150.0
        assert accounts[10]["AAPL"]["journal_avg"] == 150.0

    def test_multi_profile_weighted_avg(self):
        """Two profiles holding AAPL: p1 has 100 @ $100, p2 has 200 @ $200.
        Weighted avg = (100*100 + 200*200) / 300 = 50000/300 = $166.67."""
        from aggregate_audit import audit_account_basis_parity
        api = MagicMock()
        api.list_positions.return_value = [
            SimpleNamespace(symbol="AAPL", qty=300,
                            avg_entry_price=166.67),
        ]
        ctx1 = _mock_ctx(1, 10, "p1.db", api)
        ctx2 = _mock_ctx(2, 10, "p2.db", api)

        def _build(pid):
            return {1: ctx1, 2: ctx2}[pid]

        def _positions(db_path=None):
            if db_path == "p1.db":
                return [{"symbol": "AAPL", "qty": 100,
                         "avg_entry_price": 100.00, "occ_symbol": None}]
            return [{"symbol": "AAPL", "qty": 200,
                     "avg_entry_price": 200.00, "occ_symbol": None}]

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ), patch(
            "journal.get_virtual_positions", side_effect=_positions,
        ):
            result = audit_account_basis_parity([1, 2])
        assert result["drift"] == []
        aapl = result["accounts"][10]["AAPL"]
        assert abs(aapl["journal_avg"] - 166.6667) < 0.001


class TestBasisParityDrift:
    def test_wrong_price_fill_caught(self):
        """Broker recorded entry at $150 but journal logged at $145
        (e.g. price typo or post-fill adjustment never propagated)."""
        from aggregate_audit import audit_account_basis_parity
        api = MagicMock()
        api.list_positions.return_value = [
            SimpleNamespace(symbol="AAPL", qty=100, avg_entry_price=150.00),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_positions",
            return_value=[{
                "symbol": "AAPL", "qty": 100, "avg_entry_price": 145.00,
                "occ_symbol": None,
            }],
        ):
            result = audit_account_basis_parity([1])
        assert len(result["drift"]) == 1
        d = result["drift"][0]
        assert d["symbol"] == "AAPL"
        assert d["drift"] == 5.00  # broker - journal
        assert d["kind"] == "basis_drift"

    def test_one_sided_position_not_flagged_as_basis_drift(self):
        """Broker has AAPL, journal doesn't — that's a qty-parity issue
        (the existing audit catches), NOT a basis issue. Basis only
        applies when BOTH sides hold the symbol."""
        from aggregate_audit import audit_account_basis_parity
        api = MagicMock()
        api.list_positions.return_value = [
            SimpleNamespace(symbol="AAPL", qty=100, avg_entry_price=150.00),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_positions", return_value=[],  # empty
        ):
            result = audit_account_basis_parity([1])
        # No basis drift; the AAPL row is in accounts but with j_qty=0
        assert result["drift"] == []
        assert result["accounts"][10]["AAPL"]["journal_qty"] == 0.0

    def test_option_symbol_uses_occ(self):
        """Option positions: journal stores OCC in occ_symbol; broker
        reports OCC as the symbol. The audit matches on OCC."""
        from aggregate_audit import audit_account_basis_parity
        api = MagicMock()
        api.list_positions.return_value = [
            SimpleNamespace(symbol="AAPL260618C00200000",
                            qty=5, avg_entry_price=5.00),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "journal.get_virtual_positions",
            return_value=[{
                "symbol": "AAPL", "qty": 5, "avg_entry_price": 5.00,
                "occ_symbol": "AAPL260618C00200000",
            }],
        ):
            result = audit_account_basis_parity([1])
        assert result["drift"] == []
        assert "AAPL260618C00200000" in result["accounts"][10]


# ─────────────────────────────────────────────────────────────────────
# issues_collector wiring
# ─────────────────────────────────────────────────────────────────────

class TestIssuesWiring:
    def test_cash_drift_surfaces(self):
        import issues_collector
        issues_collector._DRIFT_CACHE["ts"] = 0
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={
                "accounts": {10: {}},
                "drift": [{
                    "account": 10, "broker_cash": 100_500.0,
                    "journal_cash": 100_000.0, "drift": 500.0,
                    "tolerance": 100.5, "profile_ids": [1],
                    "kind": "broker_cash_orphan",
                }],
                "errored": [],
            },
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ):
            rows, err = issues_collector._collect_aggregate_drift(since_hours=24)
        assert err is None
        cash_rows = [r for r in rows if r["source"].startswith("cash_parity")]
        assert len(cash_rows) == 1
        assert "broker_cash_orphan" in cash_rows[0]["message"]
        assert cash_rows[0]["level"] == "ERROR"

    def test_basis_drift_surfaces(self):
        import issues_collector
        issues_collector._DRIFT_CACHE["ts"] = 0
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={
                "accounts": {10: {"AAPL": {}}},
                "drift": [{
                    "account": 10, "symbol": "AAPL",
                    "broker_avg": 150.0, "journal_avg": 145.0,
                    "broker_qty": 100, "journal_qty": 100,
                    "drift": 5.0, "tolerance": 0.75,
                    "profile_ids": [1], "kind": "basis_drift",
                }],
                "errored": [],
            },
        ):
            rows, err = issues_collector._collect_aggregate_drift(since_hours=24)
        assert err is None
        basis_rows = [r for r in rows
                      if r["source"].startswith("basis_parity")]
        assert len(basis_rows) == 1
        assert "AAPL" in basis_rows[0]["message"]
        assert "basis_drift" in basis_rows[0]["message"]
