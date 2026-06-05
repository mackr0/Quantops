"""Autonomous remediation of broker_orphan drift.

When the aggregate audit fires with `broker has > journal claims open`,
the system should auto-close at the broker (not require an operator
to do it manually). These tests pin:

  1. OCC drift → sell_to_close submitted at the broker + journal row
     written + result reports AUTO_CLOSED.
  2. Stock-side drift → SKIP (handled by reconcile_journal_to_broker).
  3. Broker flat between audit and remediation → BROKER_FLAT (no
     spurious close submitted).
  4. Broker accepts close but journal write fails → broker order
     cancelled + profile halted.
  5. Idempotency — running the remediator twice in a row only acts
     once (second pass sees broker == virtual after the first close).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock

import pytest


def _temp_master_db(tmp_path, monkeypatch):
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
        (42, "EXP-A2-NoAltData", 1),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("QUANTOPSAI_DB", str(db))
    return str(db), 42


def _profile_journal_db(tmp_path):
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


def _patch_ctx_and_api(monkeypatch, journal_db, broker_positions=None,
                       cancel_recorder=None):
    """Patch build_user_context_from_profile + get_api so the
    remediator runs against a controllable test environment."""
    from auto_close_broker_orphans import (
        remediate_account_drift,  # noqa: F401 — import side-effects
    )

    ctx = MagicMock()
    ctx.profile_id = 42
    ctx.db_path = journal_db

    api = MagicMock()
    if broker_positions is None:
        broker_positions = []
    api.list_positions = MagicMock(return_value=[
        MagicMock(
            symbol=p["symbol"], qty=str(p["qty"]),
            side=p.get("side", "long"),
            avg_entry_price=str(p.get("avg_entry_price", 1.0)),
        )
        for p in broker_positions
    ])
    if cancel_recorder is not None:
        api.cancel_order = lambda oid: cancel_recorder.append(oid)

    monkeypatch.setattr(
        "models.build_user_context_from_profile",
        lambda pid: ctx,
    )
    monkeypatch.setattr("client.get_api", lambda _ctx: api)
    return ctx, api


def _drift_lines(occ, virtual, alpaca):
    """Format a drift line the way virtual_audit.audit_cross_account
    writes them."""
    diff = abs(virtual - alpaca)
    return [
        f"{occ}: virtual total={virtual:.0f} vs Alpaca={alpaca:.0f} "
        f"(diff={diff:.0f} shares)"
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_occ_drift_auto_closes_at_broker_and_journals(
        tmp_path, monkeypatch,
):
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)
    occ = "NVDA260710C00240000"
    _ctx, api = _patch_ctx_and_api(
        monkeypatch, journal_db,
        broker_positions=[{
            "symbol": occ, "qty": 6, "side": "long",
            "avg_entry_price": 4.64,
        }],
    )

    # Patch submit_option_close to return a synthetic order_id without
    # touching the real broker.
    submitted_payloads = []

    def fake_submit(api_, occ_symbol, qty, side_to_close="sell",
                      limit_price=None):
        submitted_payloads.append({
            "occ_symbol": occ_symbol, "qty": qty,
            "side_to_close": side_to_close,
        })
        return {
            "action": "OPTION_CLOSE",
            "occ_symbol": occ_symbol,
            "qty": qty,
            "side": side_to_close,
            "order_id": "auto-close-order-1",
            "status": "submitted",
        }
    monkeypatch.setattr(
        "options_exits.submit_option_close", fake_submit,
    )

    from auto_close_broker_orphans import remediate_account_drift
    results = remediate_account_drift(
        alpaca_account_id=2,
        profile_ids=[pid, 43, 44],
        problems=_drift_lines(occ, virtual=4, alpaca=6),
    )

    assert len(results) == 1
    r = results[0]
    assert r["action"] == "AUTO_CLOSED", r
    assert r["occ_symbol"] == occ
    assert r["close_order_id"] == "auto-close-order-1"
    assert submitted_payloads == [{
        "occ_symbol": occ, "qty": 2, "side_to_close": "sell",
    }]

    # Journal row written?
    with closing(sqlite3.connect(journal_db)) as conn:
        rows = conn.execute(
            "SELECT signal_type, qty, side, occ_symbol, order_id, "
            "status FROM trades WHERE occ_symbol = ?",
            (occ,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "AUTO_RECONCILE_CLOSE"
    assert rows[0][1] == 2
    assert rows[0][2] == "sell"
    assert rows[0][3] == occ
    assert rows[0][4] == "auto-close-order-1"
    assert rows[0][5] == "pending_fill"


# ---------------------------------------------------------------------------
# Stock-side drift is skipped (handled by a different reconciler)
# ---------------------------------------------------------------------------

def test_stock_drift_is_skipped(tmp_path, monkeypatch):
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)
    _ctx, _api = _patch_ctx_and_api(monkeypatch, journal_db)

    from auto_close_broker_orphans import remediate_account_drift
    results = remediate_account_drift(
        alpaca_account_id=2,
        profile_ids=[pid],
        problems=["AAPL: virtual total=10 vs Alpaca=11 (diff=1 shares)"],
    )
    assert len(results) == 1
    assert results[0]["action"] == "SKIP"
    assert "not an OCC symbol" in results[0]["reason"]


# ---------------------------------------------------------------------------
# Broker flat — drift already cleared between audit and remediation
# ---------------------------------------------------------------------------

def test_broker_flat_returns_broker_flat_action(
        tmp_path, monkeypatch,
):
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)
    occ = "NVDA260710C00240000"
    _ctx, _api = _patch_ctx_and_api(
        monkeypatch, journal_db,
        broker_positions=[],  # broker is now flat
    )

    from auto_close_broker_orphans import remediate_account_drift
    results = remediate_account_drift(
        alpaca_account_id=2,
        profile_ids=[pid],
        problems=_drift_lines(occ, virtual=4, alpaca=6),
    )
    assert len(results) == 1
    assert results[0]["action"] == "BROKER_FLAT"


# ---------------------------------------------------------------------------
# Atomic-placement contract on the remediation close itself
# ---------------------------------------------------------------------------

def test_after_hours_rejection_returns_deferred_not_error(
        tmp_path, monkeypatch,
):
    """Alpaca rejects options market orders outside trading hours
    (code 42210000, msg 'options market orders are only allowed
    during market hours'). The remediator should classify this as
    DEFERRED so the next audit cycle retries — not ERROR (which
    would imply something requires operator attention)."""
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)
    occ = "NVDA260710C00240000"
    _ctx, _api = _patch_ctx_and_api(
        monkeypatch, journal_db,
        broker_positions=[{
            "symbol": occ, "qty": 6, "side": "long",
        }],
    )

    monkeypatch.setattr(
        "options_exits.submit_option_close",
        lambda api_, occ_symbol, qty, side_to_close="sell",
                limit_price=None: {
            "action": "ERROR",
            "occ_symbol": occ_symbol,
            "status": "failed",
            "reason": (
                'Alpaca order rejected (422): '
                '{"code":42210000,"message":"options market orders '
                'are only allowed during market hours"}'
            ),
        },
    )

    from auto_close_broker_orphans import remediate_account_drift
    results = remediate_account_drift(
        alpaca_account_id=14,
        profile_ids=[pid],
        problems=_drift_lines(occ, virtual=4, alpaca=6),
    )
    assert len(results) == 1
    assert results[0]["action"] == "DEFERRED", (
        f"After-hours rejection should defer; got {results[0]}"
    )
    assert "retry" in results[0]["reason"].lower()

    # Profile must NOT be halted — this is a transient broker
    # restriction, not an atomic-placement contract violation.
    with closing(sqlite3.connect(master_db)) as conn:
        row = conn.execute(
            "SELECT trading_halted FROM trading_profiles "
            "WHERE id = ?",
            (pid,),
        ).fetchone()
    assert row[0] == 0, "Profile must NOT be halted on DEFERRED"


def test_journal_write_failure_cancels_close_and_halts(
        tmp_path, monkeypatch,
):
    master_db, pid = _temp_master_db(tmp_path, monkeypatch)
    journal_db = _profile_journal_db(tmp_path)
    occ = "NVDA260710C00240000"
    cancels: list = []
    _ctx, api = _patch_ctx_and_api(
        monkeypatch, journal_db,
        broker_positions=[{
            "symbol": occ, "qty": 6, "side": "long",
        }],
        cancel_recorder=cancels,
    )

    monkeypatch.setattr(
        "options_exits.submit_option_close",
        lambda api_, occ_symbol, qty, side_to_close="sell",
                limit_price=None: {
            "action": "OPTION_CLOSE",
            "occ_symbol": occ_symbol, "qty": qty,
            "side": side_to_close,
            "order_id": "auto-close-orphan",
            "status": "submitted",
        },
    )
    monkeypatch.setattr(
        "journal.log_trade",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("disk full"),
        ),
    )

    from auto_close_broker_orphans import remediate_account_drift
    results = remediate_account_drift(
        alpaca_account_id=2,
        profile_ids=[pid],
        problems=_drift_lines(occ, virtual=4, alpaca=6),
    )
    assert len(results) == 1
    assert results[0]["action"] == "ERROR"
    assert "auto-close-orphan" in cancels, (
        "Broker close should have been cancelled on journal failure"
    )

    # Profile halted?
    with closing(sqlite3.connect(master_db)) as conn:
        row = conn.execute(
            "SELECT trading_halted, halt_reason FROM trading_profiles "
            "WHERE id = ?",
            (pid,),
        ).fetchone()
    assert row[0] == 1
    assert "Auto-close journal-write breach" in (row[1] or "")
