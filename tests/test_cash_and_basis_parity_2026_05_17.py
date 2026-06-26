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
# Basis parity — DISABLED 2026-06-26
# ─────────────────────────────────────────────────────────────────────
# The broker's per-symbol avg_entry_price is an account-level FIFO-net basis,
# not attributable to any single profile on a shared conduit account (broker
# FIFO and per-profile FIFO disagree on which lots remain after a partial
# close). Comparing it to a profile's journal basis was a GUARANTEED false
# positive on every partially-closed symbol. The audit is now a no-op: a
# profile's true basis comes from its OWN order fills, and Σ qty == broker is
# the real per-symbol invariant (audit_aggregate_drift).

class TestBasisParityDisabled:
    def test_returns_empty_no_drift_regardless_of_input(self):
        from aggregate_audit import audit_account_basis_parity
        assert audit_account_basis_parity([1, 2, 3]) == {
            "accounts": {}, "drift": [], "errored": []}

    def test_no_broker_or_journal_reads(self):
        """The no-op short-circuits — it must not touch the broker or the
        journal, so a mismatching broker-avg vs journal-avg (the old
        'wrong price' case, and the shared-account FIFO case) can NEVER
        produce a false basis_drift again."""
        from aggregate_audit import audit_account_basis_parity
        with patch("models.build_user_context_from_profile") as bld, \
             patch("journal.get_virtual_positions") as gvp:
            result = audit_account_basis_parity([1])
        assert result["drift"] == []
        bld.assert_not_called()
        gvp.assert_not_called()

    def test_source_documents_disabled_and_no_longer_flags(self):
        import inspect
        import aggregate_audit
        src = inspect.getsource(aggregate_audit.audit_account_basis_parity)
        assert "DISABLED" in src  # rationale documented
        # The flagging logic is gone (these are code tokens, not docstring):
        assert "drift_rows.append" not in src
        assert "_broker_basis_per_symbol" not in src
        assert 'return {"accounts": {}, "drift": [], "errored": []}' in src


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
