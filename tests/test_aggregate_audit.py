"""Aggregate journal-vs-broker audit — defense-in-depth on top of the
per-profile reconcile and the pre-trade overshoot guard.

The bug it catches: cumulative drift across profiles sharing a single
Alpaca account. Per-profile reconcile sees each profile's journal as
in sync with the broker (because it only checks "does the broker have
ANY shares" for each symbol). But sum-of-profiles can disagree with
the broker total — and that's the case that produced 31 phantom
broker shorts across 3 accounts on 2026-05-06.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _ctx(profile_id, alpaca_account_id, db_path=":memory:", display_name=None):
    ctx = SimpleNamespace()
    ctx.profile_id = profile_id
    ctx.alpaca_account_id = alpaca_account_id
    ctx.db_path = db_path
    ctx.display_name = display_name or f"Profile {profile_id}"
    return ctx


def _broker_position(symbol, qty):
    p = MagicMock()
    p.symbol = symbol
    p.qty = str(qty)
    return p


def _api(positions):
    api = MagicMock()
    api.list_positions.return_value = positions
    return api


@pytest.fixture
def mock_module_state(monkeypatch):
    """Patch build_user_context_from_profile + get_virtual_positions
    to return controlled fixtures."""
    profiles = {}
    journals = {}
    apis = {}

    def fake_build(p_id):
        if p_id not in profiles:
            raise ValueError(f"unknown profile {p_id}")
        return profiles[p_id]

    def fake_get_virtual_positions(db_path=None, **kwargs):
        return journals.get(db_path, [])

    monkeypatch.setattr(
        "models.build_user_context_from_profile", fake_build,
    )
    monkeypatch.setattr(
        "journal.get_virtual_positions", fake_get_virtual_positions,
    )
    return {"profiles": profiles, "journals": journals, "apis": apis}


def _setup_profile(state, profile_id, account_id, journal_positions=None,
                   shared_api=None):
    """Add a profile to the mock state. journal_positions is a list of
    {symbol, qty} dicts representing get_virtual_positions output."""
    db_path = f":memory:profile_{profile_id}"
    state["journals"][db_path] = journal_positions or []
    if account_id not in state["apis"] and shared_api is None:
        state["apis"][account_id] = MagicMock()
        state["apis"][account_id].list_positions.return_value = []
    api = shared_api or state["apis"][account_id]
    state["apis"][account_id] = api
    ctx = _ctx(profile_id, account_id, db_path=db_path)
    ctx.api = api
    ctx.get_alpaca_api = lambda: api
    state["profiles"][profile_id] = ctx
    return ctx


def test_no_drift_returns_empty(mock_module_state):
    """Single profile, single symbol: journal qty matches broker qty.
    No drift detected."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=1,
                   journal_positions=[{"symbol": "AAPL", "qty": 100}],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []


def test_broker_orphan_shares_detected(mock_module_state):
    """Broker has 100 AAPL but no profile owns them in journal. Drift
    of +100 — orphan shares the broker holds with no virtual owner."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=1, journal_positions=[],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert len(result["drift"]) == 1
    d = result["drift"][0]
    assert d["symbol"] == "AAPL"
    assert d["broker_qty"] == 100
    assert d["journal_qty"] == 0
    assert d["kind"] == "broker_orphan"


def test_journal_phantom_detected(mock_module_state):
    """Journal claims 50 AAPL across profiles but broker has 0. Drift
    of -50 — phantom claim."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = []
    _setup_profile(state, 1, account_id=1,
                   journal_positions=[{"symbol": "AAPL", "qty": 50}],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert len(result["drift"]) == 1
    d = result["drift"][0]
    assert d["kind"] == "journal_phantom"
    assert d["drift"] == -50


def test_multi_profile_aggregate_match_no_drift(mock_module_state):
    """Two profiles share account #3. Each has 50 AAPL in journal.
    Broker has 100 AAPL total. Aggregate matches → no drift."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=3,
                   journal_positions=[{"symbol": "AAPL", "qty": 50}],
                   shared_api=api)
    _setup_profile(state, 2, account_id=3,
                   journal_positions=[{"symbol": "AAPL", "qty": 50}],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert result["drift"] == []


def test_multi_profile_overshoot_drift_detected(mock_module_state):
    """The exact 2026-05-06 scenario: two profiles share account #3.
    Each closed its long (journal flat). Broker net-short -200 BBWI
    from cumulative overshoot. Aggregate audit catches drift=-200."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("BBWI", -200)]
    _setup_profile(state, 1, account_id=3, journal_positions=[],
                   shared_api=api)
    _setup_profile(state, 2, account_id=3, journal_positions=[],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert len(result["drift"]) == 1
    d = result["drift"][0]
    assert d["symbol"] == "BBWI"
    assert d["broker_qty"] == -200
    assert d["kind"] == "broker_orphan"  # broker holds short, no profile claims it


def test_archived_profile_skipped(mock_module_state):
    """Profile with no alpaca_account_id (archived) doesn't contribute
    to any account's aggregate — silently skipped."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = []
    _setup_profile(state, 1, account_id=1, shared_api=api)
    # Profile 2 is archived — no account_id
    state["profiles"][2] = _ctx(2, alpaca_account_id=None,
                                 db_path=":memory:profile_2",
                                 display_name="Archived")
    state["journals"][":memory:profile_2"] = []
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert result["drift"] == []
    assert result["errored"] == []


def test_short_positions_summed_correctly(mock_module_state):
    """A profile with a short journal entry (negative qty) contributes
    negatively to the per-account aggregate."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("MSFT", -17)]
    _setup_profile(state, 1, account_id=1,
                   journal_positions=[{"symbol": "MSFT", "qty": -17}],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []  # journal -17 matches broker -17


def test_tolerance_ignores_fractional_noise(mock_module_state):
    """0.001-share residuals from prior partial fills shouldn't fire
    drift alerts."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100.001)]
    _setup_profile(state, 1, account_id=1,
                   journal_positions=[{"symbol": "AAPL", "qty": 100.0}],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []


def test_format_summary_for_no_drift():
    from aggregate_audit import format_drift_summary
    s = format_drift_summary({"drift": []})
    assert "0 drift items" in s


def test_format_summary_for_real_drift():
    from aggregate_audit import format_drift_summary
    audit = {
        "drift": [{
            "account": 3, "symbol": "BBWI",
            "journal_qty": 0, "broker_qty": -374, "drift": -374,
            "kind": "broker_orphan",
        }],
    }
    s = format_drift_summary(audit)
    assert "1 drift items" in s
    assert "BBWI" in s
    assert "broker_orphan" in s
