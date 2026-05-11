"""Pin the rejection-badge enrichment on /api/cycle-data/<profile_id>
(2026-05-11 TODO #5).

The AI Brain panel shows TRADES SELECTED with the AI's proposals.
Without execution outcome, a trade rejected by the broker (e.g.,
Alpaca cross-direction guard) silently disappears — the operator
goes looking for a non-existent fill.

This test pins:
1. Each trades_selected row gets `execution_outcome` and rejection
   metadata stamped when a recent broker_rejection exists for the
   same symbol.
2. Trades without a matching rejection get no rejection fields.
3. DB read failure logs warning + returns the cycle data without
   rejection badges (no silent swallow, no 500).
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def cycle_data_setup(monkeypatch):
    """Create a temp profile DB + cycle data file in cwd."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 999
    db_path = f"quantopsai_profile_{profile_id}.db"
    from journal import init_db, record_broker_rejection
    init_db(db_path)
    # Write a recent rejection on CWAN (the prod incident scenario)
    record_broker_rejection(
        db_path,
        symbol="CWAN", action="BUY", signal_type="BUY",
        ai_confidence=85, ai_reasoning="momentum + cheap IV",
        broker_message="cannot open a long buy while a short sell "
                        "order is open",
    )
    # Cycle data with two AI-selected trades: CWAN (will be flagged
    # rejected) and AAPL (will not — no matching rejection)
    cycle_data = {
        "timestamp": 1747000000,
        "ai_reasoning": "Test cycle",
        "trades_selected": [
            {"action": "BUY", "symbol": "CWAN", "size_pct": 1.5,
             "confidence": 85, "reasoning": "test"},
            {"action": "BUY", "symbol": "AAPL", "size_pct": 2.0,
             "confidence": 78, "reasoning": "test"},
        ],
        "shortlist": [],
    }
    with open(f"cycle_data_{profile_id}.json", "w") as f:
        json.dump(cycle_data, f)
    return profile_id


def _client():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _admin():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 1
    u.is_admin = True
    u.is_viewer = False
    u.role = "admin"
    u.email = "a@x.com"
    u.display_name = "Admin"
    u.effective_user_id = 1
    return u


class TestRejectionBadgeStamping:
    def test_rejected_trade_gets_outcome_and_code(self, cycle_data_setup):
        pid = cycle_data_setup
        with patch("flask_login.utils._get_user", return_value=_admin()):
            r = _client().get(f"/api/cycle-data/{pid}")
        assert r.status_code == 200
        data = json.loads(r.data)
        # Find the CWAN trade row
        cwan = next(t for t in data["trades_selected"]
                    if t["symbol"] == "CWAN")
        assert cwan["execution_outcome"] == "rejected"
        assert cwan["rejection_code"] == "cross_direction_long_blocked"
        # Display string is humanized
        assert "Cross" in cwan["rejection_code_display"]
        # Broker message preserved (truncated to 240 chars)
        assert "cannot open a long buy" in cwan["rejection_message"]

    def test_unrejected_trade_has_no_rejection_fields(self,
                                                       cycle_data_setup):
        pid = cycle_data_setup
        with patch("flask_login.utils._get_user", return_value=_admin()):
            r = _client().get(f"/api/cycle-data/{pid}")
        data = json.loads(r.data)
        aapl = next(t for t in data["trades_selected"]
                    if t["symbol"] == "AAPL")
        # No rejection — fields should be absent
        assert "execution_outcome" not in aapl
        assert "rejection_code" not in aapl

    def test_rejection_fetch_failure_returns_cycle_data_without_badges(
        self, cycle_data_setup, monkeypatch,
    ):
        """If get_recent_broker_rejections raises, the endpoint must
        still return the cycle data (just without rejection badges)
        and log a warning. No silent swallow, no 500."""
        pid = cycle_data_setup
        monkeypatch.setattr(
            "journal.get_recent_broker_rejections",
            lambda db_path, hours=24, limit=200: (_ for _ in ()).throw(
                RuntimeError("DB locked")
            ),
        )
        with patch("flask_login.utils._get_user", return_value=_admin()):
            r = _client().get(f"/api/cycle-data/{pid}")
        # 200 OK — degraded but not failed
        assert r.status_code == 200
        data = json.loads(r.data)
        # Trades present, no rejection fields
        assert len(data["trades_selected"]) == 2
        for t in data["trades_selected"]:
            assert "execution_outcome" not in t
