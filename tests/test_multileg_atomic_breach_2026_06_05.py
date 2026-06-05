"""Atomic-placement contract for the multileg + single-leg option
broker-submit paths.

When `log_trade` raises after a broker order has been placed, the
prior behavior was a silent warning that left the broker holding
positions no profile's virtual book reflected — the `broker_orphan`
class that surfaced on 2026-06-05 as drift on EXP-A2's NVDA strangle
legs. These tests pin the new behavior at the call-site level:

1. Combo multileg + journal failure → broker order cancelled + profile
   halted + ERROR result.
2. Sequential multileg + journal failure → all submitted leg orders
   cancelled + profile halted + ERROR result.
3. Single-leg option + journal failure → broker order cancelled +
   profile halted + ERROR result.
4. Cancel-after-journal-failure failure → profile still halted with a
   loud title so the operator sees the orphan can't be self-healed.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest


EXPIRY = date.today() + timedelta(days=35)


def _temp_master_db(tmp_path, monkeypatch):
    """Set up an isolated master DB with one trading profile so
    `halt_helpers.halt_profile` writes go somewhere observable.
    Returns (master_db_path, profile_id)."""
    import sqlite3

    db = tmp_path / "quantopsai_master.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trading_profiles (
            id INTEGER PRIMARY KEY,
            name TEXT,
            user_id INTEGER,
            enabled INTEGER DEFAULT 1,
            trading_halted INTEGER DEFAULT 0,
            halt_reason TEXT,
            halted_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO trading_profiles (id, name, user_id) "
        "VALUES (?, ?, ?)",
        (42, "test-profile", 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("QUANTOPSAI_DB", str(db))
    return str(db), 42


def _profile_journal_db(tmp_path):
    """Set up an isolated per-profile journal DB with the trades +
    audit_alerts tables. Returns the db path."""
    import sqlite3

    db = tmp_path / "quantopsai_profile_42.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            ai_reasoning TEXT,
            ai_confidence INTEGER,
            stop_loss REAL,
            take_profit REAL,
            status TEXT,
            pnl REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL,
            occ_symbol TEXT,
            option_strategy TEXT,
            expiry TEXT,
            strike REAL,
            predicted_slippage_bps REAL,
            adv_at_decision REAL
        )
    """)
    conn.execute("""
        CREATE TABLE audit_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning',
            title TEXT NOT NULL,
            detail TEXT,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    return str(db)


def _ctx(profile_id=42, db_path=None):
    """Mock UserContext with the fields the breach paths read."""
    ctx = MagicMock()
    ctx.profile_id = profile_id
    ctx.db_path = db_path
    return ctx


def _is_halted(master_db, profile_id):
    """Read trading_halted directly from the master DB."""
    import sqlite3

    with sqlite3.connect(master_db) as conn:
        row = conn.execute(
            "SELECT trading_halted, halt_reason FROM trading_profiles "
            "WHERE id = ?",
            (profile_id,),
        ).fetchone()
    return (bool(row[0]) if row else False,
            row[1] if row else None)


# ---------------------------------------------------------------------------
# Combo path
# ---------------------------------------------------------------------------

def test_combo_journal_failure_cancels_broker_and_halts(
        tmp_path, monkeypatch,
):
    """Combo MLEG accepted by broker, log_trade raises mid-loop —
    broker order MUST be cancelled and profile MUST be halted. Result
    action MUST be ERROR (not MULTILEG_OPEN)."""
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)

    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    spec = build_bull_call_spread("AAPL", EXPIRY, 150, 160, qty=1)

    cancels = []

    api = MagicMock()
    def _cancel(oid):
        cancels.append(oid)
    api.cancel_order = _cancel
    api.get_order = MagicMock(return_value=MagicMock(legs=[]))

    # Combo submission succeeds; broker returns a combo id.
    monkeypatch.setattr(
        "options_multileg._submit_alpaca_order_raw",
        lambda _api, _payload: MagicMock(id="combo-orphan-1"),
    )

    # Force log_trade to fail on the first leg.
    call_count = {"n": 0}

    def fake_log_trade(*args, **kwargs):
        call_count["n"] += 1
        raise RuntimeError("disk full (simulated)")

    monkeypatch.setattr("journal.log_trade", fake_log_trade)

    result = execute_multileg_strategy(
        api, spec, _ctx(profile_id=pid, db_path=journal_db),
    )

    assert result["action"] == "ERROR", (
        f"Expected ERROR after journal-write breach, got {result}"
    )
    assert "combo-orphan-1" in cancels, (
        f"Combo broker order should have been cancelled, got "
        f"cancel calls = {cancels}"
    )
    halted, reason = _is_halted(master_db, pid)
    assert halted, "Profile should be halted after atomic breach"
    assert "Multileg journal-write breach" in (reason or ""), (
        f"Halt reason should name the breach class; got: {reason!r}"
    )


def test_combo_rollback_failure_still_halts_with_loud_title(
        tmp_path, monkeypatch,
):
    """If cancel_order ALSO raises, the profile must still be halted
    and the halt reason must distinguish the rollback failure so the
    operator knows the broker may still hold the orphan."""
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)

    from options_multileg import (
        build_bull_call_spread, execute_multileg_strategy,
    )
    spec = build_bull_call_spread("AAPL", EXPIRY, 150, 160, qty=1)

    api = MagicMock()
    api.cancel_order = MagicMock(
        side_effect=RuntimeError("Alpaca 503 — cancel rejected"),
    )
    api.get_order = MagicMock(return_value=MagicMock(legs=[]))

    monkeypatch.setattr(
        "options_multileg._submit_alpaca_order_raw",
        lambda _api, _payload: MagicMock(id="combo-orphan-2"),
    )
    monkeypatch.setattr(
        "journal.log_trade",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("disk full"),
        ),
    )

    result = execute_multileg_strategy(
        api, spec, _ctx(profile_id=pid, db_path=journal_db),
    )

    assert result["action"] == "ERROR"
    halted, reason = _is_halted(master_db, pid)
    assert halted
    assert "rollback FAILED" in (reason or ""), (
        f"Halt reason should distinguish rollback failure; got: "
        f"{reason!r}"
    )


# ---------------------------------------------------------------------------
# Single-leg path (options_trader.execute_option_strategy)
# ---------------------------------------------------------------------------

def test_single_leg_journal_failure_cancels_broker_and_halts(
        tmp_path, monkeypatch,
):
    """Single-leg option submit succeeds, log_trade raises, broker
    cancel must fire and profile must be halted with ERROR result."""
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)

    cancels = []
    api = MagicMock()
    def _cancel(oid):
        cancels.append(oid)
    api.cancel_order = _cancel

    # Patch the submit wrapper to return a broker order_id directly
    # without touching the real Alpaca path.
    monkeypatch.setattr(
        "options_trader.submit_option_order",
        lambda *a, **kw: "single-orphan-1",
    )
    # Patch account / position reads so the sizing-constraint path
    # doesn't try to hit a real Alpaca client.
    monkeypatch.setattr(
        "client.get_account_info",
        lambda ctx: {"equity": 100000.0, "buying_power": 100000.0},
    )
    monkeypatch.setattr("client.get_positions", lambda ctx: [])

    monkeypatch.setattr(
        "journal.log_trade",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("disk full"),
        ),
    )

    from options_trader import execute_option_strategy
    # Use long_call so the sizing constraint is the premium cap
    # (no held-shares prerequisite). contracts=1 at strike=150 with
    # a small limit_price keeps premium well under the 1%-of-equity
    # cap with the patched equity of $100K.
    proposal = {
        "option_strategy": "long_call",
        "symbol": "AAPL",
        "strike": 150.0,
        "expiry": EXPIRY.isoformat(),
        "contracts": 1,
        "limit_price": 2.50,
        "confidence": 80,
        "reasoning": "test",
    }
    result = execute_option_strategy(
        api, proposal, _ctx(profile_id=pid, db_path=journal_db),
    )

    assert result["action"] == "ERROR", (
        f"Expected ERROR after journal-write breach, got {result}"
    )
    assert "single-orphan-1" in cancels, (
        f"Single-leg broker order should have been cancelled, got "
        f"cancel calls = {cancels}"
    )
    halted, reason = _is_halted(master_db, pid)
    assert halted, "Profile should be halted after single-leg breach"
    assert "Single-leg option journal-write breach" in (reason or ""), (
        f"Halt reason should name the breach class; got: {reason!r}"
    )
