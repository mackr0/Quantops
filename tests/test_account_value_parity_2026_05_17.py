"""Tests for the account-value parity invariant (#165, 2026-05-17).

This invariant is the dollar-side counterpart to the existing
quantity-side aggregate_audit:
  qty-parity catches mismatched share counts
  value-parity catches mismatched dollar amounts (different marks,
                missing multipliers, stale snapshots, etc.)

Together with the order_id pairing invariant (#157), these form
the three-tier integrity check between Alpaca and the virtual
profiles routing through each shared account.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _mock_ctx(profile_id, alpaca_account_id, db_path, api):
    """Minimal ctx supporting build_user_context_from_profile patching."""
    ctx = SimpleNamespace(
        profile_id=profile_id,
        alpaca_account_id=alpaca_account_id,
        db_path=db_path,
        api=api,
    )
    return ctx


def _broker_position(symbol, market_value, qty=1):
    return SimpleNamespace(symbol=symbol, market_value=market_value, qty=qty)


# ─────────────────────────────────────────────────────────────────────
# Happy path: values match → zero drift
# ─────────────────────────────────────────────────────────────────────

class TestNoDrift:
    def test_perfect_match_no_drift(self):
        """Single profile, broker holds exactly what journal claims."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.return_value = [
            _broker_position("AAPL", 50000.0, 100),
            _broker_position("MSFT", 30000.0, 50),
        ]
        ctx = _mock_ctx(1, alpaca_account_id=10, db_path="p1.db", api=api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "aggregate_audit._journal_positions_value", return_value=80000.0,
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1])
        assert result["drift"] == []
        assert result["accounts"][10]["broker_value"] == 80000.0
        assert result["accounts"][10]["journal_value"] == 80000.0
        assert result["accounts"][10]["drift"] == 0.0

    def test_match_within_tolerance_no_drift(self):
        """$10 drift on a $100k account is well inside the 0.1% tolerance."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.return_value = [
            _broker_position("AAPL", 100_000.0, 200),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "aggregate_audit._journal_positions_value",
            return_value=99_990.0,  # $10 short
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1])
        assert result["drift"] == []  # within $100 (0.1%)


# ─────────────────────────────────────────────────────────────────────
# Drift detection
# ─────────────────────────────────────────────────────────────────────

class TestDriftDetection:
    def test_broker_value_orphan_detected(self):
        """Broker holds $10k more than profiles claim → broker_value_orphan."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.return_value = [
            _broker_position("AAPL", 110_000.0, 220),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "aggregate_audit._journal_positions_value", return_value=100_000.0,
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1])
        assert len(result["drift"]) == 1
        d = result["drift"][0]
        assert d["kind"] == "broker_value_orphan"
        assert d["drift"] == 10_000.0
        assert d["account"] == 10

    def test_journal_value_phantom_detected(self):
        """Profiles claim $5k more than broker holds → journal_value_phantom."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.return_value = [
            _broker_position("AAPL", 95_000.0, 190),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "aggregate_audit._journal_positions_value", return_value=100_000.0,
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1])
        assert len(result["drift"]) == 1
        d = result["drift"][0]
        assert d["kind"] == "journal_value_phantom"
        assert d["drift"] == -5_000.0

    def test_missing_multiplier_caught(self):
        """Regression: if virtual position value forgets the options
        contract multiplier (×100), broker shows $50k worth of contracts
        but journal shows $500 — the value audit catches the 100× error
        even though quantities match."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        # Broker correctly marks the option premium ($5 × 100 × 100 contracts)
        api.list_positions.return_value = [
            _broker_position("AAPL260618C00200000", 50_000.0, 100),
        ]
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            # Buggy journal: forgot ×100 multiplier → $500 instead of $50k
            "aggregate_audit._journal_positions_value", return_value=500.0,
        ), patch("client._make_price_fetcher", return_value=lambda s: 5.0):
            result = audit_account_value_parity([1])
        assert len(result["drift"]) == 1
        # 99% drift on the broker value — comfortably above any tolerance
        assert abs(result["drift"][0]["drift"]) > 49_000.0


# ─────────────────────────────────────────────────────────────────────
# Multi-profile aggregation
# ─────────────────────────────────────────────────────────────────────

class TestMultiProfile:
    def test_summed_across_profiles_on_same_account(self):
        """Two profiles on the same Alpaca account → broker_value
        must equal sum of both profile values."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.return_value = [
            _broker_position("AAPL", 80_000.0, 160),
        ]
        ctx1 = _mock_ctx(1, 10, "p1.db", api)
        ctx2 = _mock_ctx(2, 10, "p2.db", api)

        def _build(pid):
            return {1: ctx1, 2: ctx2}[pid]

        def _journal_value(db_path, price_fetcher=None):
            return {"p1.db": 30_000.0, "p2.db": 50_000.0}[db_path]

        with patch(
            "models.build_user_context_from_profile", side_effect=_build,
        ), patch(
            "aggregate_audit._journal_positions_value",
            side_effect=_journal_value,
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1, 2])

        # 30k + 50k = 80k, exactly matches broker — no drift
        assert result["drift"] == []
        assert result["accounts"][10]["journal_value"] == 80_000.0
        assert result["accounts"][10]["profile_ids"] == [1, 2]

    def test_profile_without_account_id_skipped(self):
        """A profile with alpaca_account_id=None is just bookkeeping,
        not a broker-routing profile — must be excluded."""
        from aggregate_audit import audit_account_value_parity
        ctx = _mock_ctx(1, alpaca_account_id=None,
                        db_path="p1.db", api=MagicMock())
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ):
            result = audit_account_value_parity([1])
        assert result["accounts"] == {}
        assert result["drift"] == []


# ─────────────────────────────────────────────────────────────────────
# Error handling
# ─────────────────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_profile_load_failure_listed_in_errored(self):
        from aggregate_audit import audit_account_value_parity
        with patch(
            "models.build_user_context_from_profile",
            side_effect=ValueError("boom"),
        ):
            result = audit_account_value_parity([99])
        assert result["errored"] == [99]
        assert result["accounts"] == {}

    def test_broker_list_positions_failure_returns_zero_value(self):
        """If the broker call fails, broker_value treated as 0 (with a
        WARN log). A journal showing > 0 would then surface as drift,
        which is the desired loud-failure behavior."""
        from aggregate_audit import audit_account_value_parity
        api = MagicMock()
        api.list_positions.side_effect = OSError("network")
        ctx = _mock_ctx(1, 10, "p1.db", api)
        with patch(
            "models.build_user_context_from_profile", return_value=ctx,
        ), patch(
            "aggregate_audit._journal_positions_value", return_value=10_000.0,
        ), patch("client._make_price_fetcher", return_value=lambda s: 100.0):
            result = audit_account_value_parity([1])
        # Broker showed 0, journal showed 10k → drift = -10k (phantom)
        assert len(result["drift"]) == 1
        assert result["drift"][0]["kind"] == "journal_value_phantom"


# ─────────────────────────────────────────────────────────────────────
# issues_collector wiring
# ─────────────────────────────────────────────────────────────────────

class TestIssuesCollectorWiring:
    def test_value_drift_surfaces_on_issues(self):
        """When value-parity reports drift, the issues collector emits
        an ERROR row with source='value_parity.<acct>'."""
        import issues_collector
        # Force the drift cache fresh.
        issues_collector._DRIFT_CACHE["ts"] = 0
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"accounts": {}, "drift": [], "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={
                "accounts": {10: {}},
                "drift": [{
                    "account": 10, "broker_value": 110_000.0,
                    "journal_value": 100_000.0, "drift": 10_000.0,
                    "tolerance": 110.0, "profile_ids": [1, 2],
                    "kind": "broker_value_orphan",
                }],
                "errored": [],
            },
        ):
            rows, err = issues_collector._collect_aggregate_drift(since_hours=24)
        assert err is None
        assert len(rows) == 1
        assert rows[0]["source"] == "value_parity.10"
        assert rows[0]["level"] == "ERROR"
        assert "broker=$110,000.00" in rows[0]["message"]
        assert "broker_value_orphan" in rows[0]["message"]
        assert rows[0]["is_live_snapshot"] is True
