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
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _ctx(profile_id, alpaca_account_id, db_path, display_name=None):
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


def _make_journal_db(path, trades):
    """trades = list of (symbol, side, qty, price[, occ_symbol]).

    Creates a real SQLite DB at `path` with trades the audit can read.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT DEFAULT 'open',
            pnl REAL, fill_price REAL,
            occ_symbol TEXT
        )
    """)
    for i, t in enumerate(trades):
        sym, side, qty, price = t[:4]
        occ = t[4] if len(t) > 4 else None
        ts = f"2026-04-{15+i%10:02d}T10:00:0{i%10}"
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, occ_symbol) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, sym, side, qty, price, occ),
        )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def mock_module_state(monkeypatch, tmp_path):
    """Build real journal DBs + mock build_user_context_from_profile to
    return ctx pointing at them."""
    profiles = {}
    apis = {}

    def fake_build(p_id):
        if p_id not in profiles:
            raise ValueError(f"unknown profile {p_id}")
        return profiles[p_id]

    monkeypatch.setattr(
        "models.build_user_context_from_profile", fake_build,
    )
    return {"profiles": profiles, "apis": apis, "tmp_path": tmp_path}


def _setup_profile(state, profile_id, account_id, journal_trades=None,
                   shared_api=None):
    """Add a profile to the mock state. journal_trades is a list of
    (symbol, side, qty, price[, occ_symbol]) tuples that get inserted
    into a real per-profile SQLite journal."""
    tmp = state["tmp_path"]
    db_path = _make_journal_db(
        tmp / f"profile_{profile_id}.db",
        journal_trades or [],
    )
    if account_id is not None and account_id not in state["apis"] and shared_api is None:
        state["apis"][account_id] = MagicMock()
        state["apis"][account_id].list_positions.return_value = []
    api = shared_api or (state["apis"].get(account_id) if account_id else MagicMock())
    if account_id is not None:
        state["apis"][account_id] = api
    ctx = _ctx(profile_id, account_id, db_path=db_path)
    ctx.api = api
    ctx.get_alpaca_api = lambda: api
    state["profiles"][profile_id] = ctx
    return ctx


def test_no_drift_returns_empty(mock_module_state):
    """Single profile, single symbol: journal qty matches broker qty."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=1,
                   journal_trades=[("AAPL", "buy", 100, 150.0)],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []


def test_broker_orphan_shares_detected(mock_module_state):
    """Broker has 100 AAPL but no profile owns them — orphan."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=1, journal_trades=[],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert len(result["drift"]) == 1
    d = result["drift"][0]
    assert d["symbol"] == "AAPL"
    assert d["broker_qty"] == 100
    assert d["kind"] == "broker_orphan"


def test_journal_phantom_detected(mock_module_state):
    """Journal claims 50 AAPL but broker has 0 — phantom claim."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = []
    _setup_profile(state, 1, account_id=1,
                   journal_trades=[("AAPL", "buy", 50, 100.0)],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert len(result["drift"]) == 1
    assert result["drift"][0]["kind"] == "journal_phantom"


def test_multi_profile_aggregate_match_no_drift(mock_module_state):
    """Two profiles share account #3. Sum matches broker."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100)]
    _setup_profile(state, 1, account_id=3,
                   journal_trades=[("AAPL", "buy", 50, 100.0)],
                   shared_api=api)
    _setup_profile(state, 2, account_id=3,
                   journal_trades=[("AAPL", "buy", 50, 100.0)],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert result["drift"] == []


def test_multi_profile_overshoot_drift_detected(mock_module_state):
    """The exact 2026-05-06 scenario: broker net-short -200 BBWI from
    cumulative overshoot, no profile claims it."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("BBWI", -200)]
    _setup_profile(state, 1, account_id=3, journal_trades=[], shared_api=api)
    _setup_profile(state, 2, account_id=3, journal_trades=[], shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert len(result["drift"]) == 1
    d = result["drift"][0]
    assert d["symbol"] == "BBWI"
    assert d["broker_qty"] == -200
    assert d["kind"] == "broker_orphan"


def test_archived_profile_skipped(mock_module_state):
    """Profile with no alpaca_account_id is silently skipped."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = []
    _setup_profile(state, 1, account_id=1, shared_api=api)
    _setup_profile(state, 2, account_id=None)
    result = audit_aggregate_drift(profile_ids=[1, 2])
    assert result["drift"] == []


def test_short_positions_summed_correctly(mock_module_state):
    """side='short' contributes negatively to the per-account aggregate."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("MSFT", -17)]
    _setup_profile(state, 1, account_id=1,
                   journal_trades=[("MSFT", "short", 17, 400.0)],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []  # journal -17 matches broker -17


def test_tolerance_ignores_fractional_noise(mock_module_state):
    """0.001-share residuals shouldn't fire drift alerts."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [_broker_position("AAPL", 100.001)]
    _setup_profile(state, 1, account_id=1,
                   journal_trades=[("AAPL", "buy", 100, 100.0)],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []


def test_null_price_rows_still_counted_in_fifo(mock_module_state):
    """log_trade for option legs sometimes stores NULL price (the
    multi-leg execution path doesn't always pass a per-leg price).
    The audit's FIFO must still count the qty for those rows so
    options drift is detectable. Caught 2026-05-06: option contracts
    with NULL price disappeared from the audit, showing as
    broker_orphan even though the journal had them."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [
        _broker_position("WMT260612P00117000", 3),
    ]
    # Insert a row with NULL price directly
    db = state["tmp_path"] / "profile_p.db"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, signal_type TEXT, strategy TEXT, reason TEXT,
            status TEXT DEFAULT 'open', pnl REAL, fill_price REAL,
            occ_symbol TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, occ_symbol) "
        "VALUES ('2026-05-06T14:56:45', 'WMT', 'buy', 3, NULL, 'WMT260612P00117000')",
    )
    conn.commit()
    conn.close()
    state["profiles"][1] = _ctx(1, alpaca_account_id=1, db_path=str(db))
    state["profiles"][1].api = api
    state["profiles"][1].get_alpaca_api = lambda: api
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == [], (
        f"expected no drift but got {result['drift']}"
    )


def test_options_aggregated_by_occ_not_underlying(mock_module_state):
    """Caught after deploying audit to prod: a bull_put_spread BUY
    journal row stores symbol='MSFT' + occ_symbol='MSFT260612P00375000'.
    Without OCC-aware grouping, the audit aggregates by 'MSFT' (stock)
    while broker reports the position under the OCC symbol → false
    drift on both. Audit must group by COALESCE(occ_symbol, symbol)."""
    from aggregate_audit import audit_aggregate_drift
    state = mock_module_state
    api = MagicMock()
    api.list_positions.return_value = [
        _broker_position("MSFT260612P00375000", 1),
    ]
    _setup_profile(state, 1, account_id=1,
                   journal_trades=[
                       ("MSFT", "buy", 1, 5.50, "MSFT260612P00375000"),
                   ],
                   shared_api=api)
    result = audit_aggregate_drift(profile_ids=[1])
    assert result["drift"] == []  # OCC-grouped journal matches broker


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
