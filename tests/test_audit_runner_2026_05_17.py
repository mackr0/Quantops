"""Tests for audit_runner (#169, 2026-05-17).

Cross-profile audit scheduler + first-detection alerter. Verifies:
  - all five audits are invoked
  - signatures are stable across runs (same drift item → same sig)
  - new drift items trigger notify_fn ONCE (not every cycle)
  - resolved drift items get marked, no email
  - reappearing drift items re-trigger notify_fn (the recovery edge)
  - audit-level exceptions don't break the runner
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def main_db(tmp_path):
    db = tmp_path / "quantopsai.db"
    sqlite3.connect(db).close()  # empty — audit_runner creates the table
    return str(db)


def _qty_drift(acct, sym):
    return {"account": acct, "symbol": sym,
            "journal_qty": 100, "broker_qty": 110, "drift": 10,
            "kind": "broker_orphan"}


def _value_drift(acct, drift):
    return {"account": acct, "broker_value": 100_000 + drift,
            "journal_value": 100_000, "drift": drift,
            "tolerance": 100.0, "profile_ids": [1],
            "kind": "broker_value_orphan" if drift > 0 else "journal_value_phantom"}


# ─────────────────────────────────────────────────────────────────────
# Signatures
# ─────────────────────────────────────────────────────────────────────

class TestSignatures:
    def test_stable_per_drift_item(self):
        """Same drift content → identical signature across runs."""
        from audit_runner import _signature
        d = _qty_drift(10, "AAPL")
        assert _signature("qty_parity", d) == _signature("qty_parity", d)

    def test_different_accounts_different_sigs(self):
        from audit_runner import _signature
        s1 = _signature("qty_parity", _qty_drift(10, "AAPL"))
        s2 = _signature("qty_parity", _qty_drift(11, "AAPL"))
        assert s1 != s2

    def test_different_audit_types_different_sigs(self):
        from audit_runner import _signature
        # Same account, but different audit → different sig.
        s1 = _signature("qty_parity", {"account": 10, "symbol": "AAPL"})
        s2 = _signature("value_parity", {"account": 10})
        assert s1 != s2


# ─────────────────────────────────────────────────────────────────────
# First-detection alerting
# ─────────────────────────────────────────────────────────────────────

class TestFirstDetectionAlert:
    def test_new_drift_triggers_notify(self, main_db):
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock()
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [_qty_drift(10, "AAPL")],
                          "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            result = detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        assert result["new"] == 1
        assert notify.call_count == 1

    def test_persistent_drift_only_alerts_once(self, main_db):
        """Same drift item across two cycles → notify called once."""
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock()
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [_qty_drift(10, "AAPL")],
                          "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            r1 = detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
            r2 = detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        assert r1["new"] == 1
        assert r2["new"] == 0
        assert notify.call_count == 1

    def test_resolved_drift_logged_not_alerted(self, main_db):
        """Drift item disappears between runs → marked resolved, no notify."""
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock()
        # First cycle: drift exists
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [_qty_drift(10, "AAPL")],
                          "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        notify.reset_mock()
        # Second cycle: drift gone
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            r = detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        assert r["resolved"] == 1
        assert notify.call_count == 0

    def test_reappearing_drift_re_alerts(self, main_db):
        """Drift cleared then returned → alert again (recovery + new edge)."""
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock()

        def _cycle(drift_present):
            patches = [
                patch("aggregate_audit.audit_aggregate_drift",
                      return_value={
                          "drift": [_qty_drift(10, "AAPL")] if drift_present else [],
                          "accounts": {}, "errored": [],
                      }),
                patch("aggregate_audit.audit_account_value_parity",
                      return_value={"drift": [], "accounts": {},
                                    "errored": []}),
                patch("aggregate_audit.audit_account_cash_parity",
                      return_value={"drift": [], "accounts": {},
                                    "errored": []}),
                patch("aggregate_audit.audit_account_basis_parity",
                      return_value={"drift": [], "accounts": {},
                                    "errored": []}),
                patch("integrity_audit.audit_equity_identity_all",
                      return_value={"profiles": [], "drift": [],
                                    "errored": []}),
            ]
            for p in patches:
                p.start()
            try:
                return detect_and_alert_new_drift(
                    profile_ids=[1], notify_fn=notify, main_db=main_db,
                )
            finally:
                for p in patches:
                    p.stop()

        _cycle(True)   # first detection — alert
        _cycle(False)  # resolved — no alert
        _cycle(True)   # reappeared — alert again
        assert notify.call_count == 2


# ─────────────────────────────────────────────────────────────────────
# Robustness
# ─────────────────────────────────────────────────────────────────────

class TestRobustness:
    def test_individual_audit_exception_doesnt_stop_others(self, main_db):
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock()
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            side_effect=RuntimeError("qty audit broken"),
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [_value_drift(10, 5_000.0)],
                          "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            r = detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        # Value-parity drift still surfaces even though qty raised
        assert r["new"] == 1
        assert notify.call_count == 1

    def test_no_main_db_returns_safely(self):
        """If quantopsai.db doesn't exist, audit_runner skips with a
        warning rather than crashing."""
        from audit_runner import detect_and_alert_new_drift
        r = detect_and_alert_new_drift(
            profile_ids=[1], notify_fn=MagicMock(),
            main_db="/nonexistent/path/quantopsai.db",
        )
        assert r["total"] == 0
        assert r["new"] == 0

    def test_notify_failure_keeps_alert_unsent(self, main_db):
        """If notify_fn raises, the alert is NOT marked sent — next
        cycle will retry."""
        from audit_runner import detect_and_alert_new_drift
        notify = MagicMock(side_effect=OSError("smtp down"))
        with patch(
            "aggregate_audit.audit_aggregate_drift",
            return_value={"drift": [_qty_drift(10, "AAPL")],
                          "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_value_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_cash_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "aggregate_audit.audit_account_basis_parity",
            return_value={"drift": [], "accounts": {}, "errored": []},
        ), patch(
            "integrity_audit.audit_equity_identity_all",
            return_value={"profiles": [], "drift": [], "errored": []},
        ):
            detect_and_alert_new_drift(
                profile_ids=[1], notify_fn=notify, main_db=main_db,
            )
        with sqlite3.connect(main_db) as conn:
            sent = conn.execute(
                "SELECT alert_sent FROM audit_alerts WHERE "
                "signature = 'qty_parity:10:AAPL'"
            ).fetchone()
        assert sent[0] == 0  # notify failed, alert_sent stays 0
