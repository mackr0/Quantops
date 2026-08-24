"""The equity-identity audit must be immune to marks moving between
reads: a nonzero drift must ALWAYS mean a real bookkeeping error.

2026-07-24 — p210 flagged `equity identity broken ... drift=$-1.03`
mid-trading while its books were penny-exact (verified 0.00 drift twice
at frozen marks, and the mark-independent residual — cash + Σ open×
avg_entry vs initial + Σpnl — bisected to exactly zero per symbol).
Cause: the audit computed unrealized from one `get_virtual_positions`
call and equity from a separate `get_virtual_account_info` call;
"reusing the price_fetcher" did not freeze marks (30s cache TTL; broker
marks only cover account-held symbols), so a volatile minute yielded
phantom cents that crossed the $1.00 tolerance. Fix: actual equity is
now cash (mark-independent, `get_virtual_cash`) + Σ market_value of the
SAME positions snapshot, so mark terms cancel algebraically in `drift`.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _mk_profile_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            order_id TEXT, status TEXT DEFAULT 'open',
            pnl REAL, fill_price REAL, occ_symbol TEXT,
            stop_loss REAL, take_profit REAL
        )
    """)
    # A correct book: buy 100 @50, sell 40 @55 (closed, pnl +200 exact),
    # 60 shares remain open at avg 50.
    rows = [
        ("2026-07-20T10:00:00", "AAPL", "buy", 100, 50.0, 50.0, "o1", "open", None),
        ("2026-07-21T10:00:00", "AAPL", "sell", 40, 55.0, 55.0, "o2", "closed", 200.0),
    ]
    for ts, sym, side, q, px, fp, oid, st, pnl in rows:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "fill_price, order_id, status, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, sym, side, q, px, fp, oid, st, pnl))
    conn.commit()
    conn.close()
    return str(path)


class _Ctx:
    def __init__(self, db_path):
        self.db_path = db_path
        self.initial_capital = 100000.0
        self.api = object()   # truthy → the audit builds a price fetcher

    def get_alpaca_api(self):
        return self.api


def _volatile_fetcher(api):
    """A price fetcher whose mark MOVES on every call — the exact
    condition that produced the phantom drift."""
    state = {"n": 0}

    def fetch(symbol, side="buy"):
        state["n"] += 1
        return 60.0 + 10.0 * state["n"]   # 70, 80, 90, ... per call
    return fetch


def _run_audit(db):
    from integrity_audit import audit_equity_identity
    with patch("models.build_user_context_from_profile",
               return_value=_Ctx(db)), \
         patch("client._make_price_fetcher", _volatile_fetcher):
        return audit_equity_identity(1)


class TestRaceFree:
    def test_moving_marks_produce_zero_drift_on_correct_books(self, tmp_path):
        """The p210 shape: correct books, volatile marks. The audit
        must read drift exactly 0 — mark terms cancel by construction."""
        db = _mk_profile_db(tmp_path / "p.db")
        d = _run_audit(db)
        assert d["errored"] is None, d["errored"]
        assert d["drift"] == 0.0, (
            f"Moving marks produced phantom drift {d['drift']:+.2f} on "
            f"penny-exact books — the two-read race is back."
        )
        assert d["has_drift"] is False

    def test_pnl_column_error_bites_exactly_as_its_own_finding(self, tmp_path):
        """Tamper the stamped pnl by +$5. 2026-08-24: the identity's
        realized side is LEG-derived (same fill-true basis as cash), so
        the books identity stays exactly 0 — and the corrupted COLUMN is
        caught by the separate pnl_column_mismatch finding, off by
        exactly the injected -$5.00 (leg truth 200 vs stored 205)."""
        db = _mk_profile_db(tmp_path / "p2.db")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trades SET pnl = 205.0 WHERE side='sell'")
        conn.commit(); conn.close()
        d = _run_audit(db)
        assert d["errored"] is None, d["errored"]
        assert d["drift"] == 0.0 and d["has_drift"] is False
        assert d["pnl_column_mismatch"] == -5.0, d


class TestStructural:
    def test_actual_equity_shares_the_positions_snapshot(self):
        """The two-read shape must not return: actual equity comes from
        get_virtual_cash + the SAME positions list, never a separate
        get_virtual_account_info call."""
        src = open(os.path.join(
            os.path.dirname(__file__), os.pardir,
            "integrity_audit.py")).read()
        assert "get_virtual_cash" in src, (
            "integrity_audit no longer uses the mark-independent cash "
            "source."
        )
        assert "get_virtual_account_info" not in src, (
            "The separate account-info read is back in the equity "
            "identity — the mark race returns with it."
        )
