"""pnl is NULL until it is known from fills (2026-08-24).

Experiment 2's first session, 18 minutes in: the equity-identity audit
flagged p230 at drift +$89.38 while the books were penny-exact. The
exit paths stamped a MARK-based estimate (position.unrealized_pl,
prorated, ± borrow accrual) into the pnl column at submit time; until
recompute_realized_pnl trued it from fills, cash (fill-true legs) and
realized (the pnl column) disagreed by construction. House doctrine is
already "pnl NULL, never an estimate" — these tests pin it on every
stock exit writer, and pin that the fill state machine never needed
the estimate (stock discrimination is status + fill_price; only the
option-close branch uses pnl, by the 2026-07-22 design).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _src(name):
    return open(os.path.join(ROOT, name)).read()


class TestNoEstimateWriters:
    def test_trade_pipeline_exit_writes_null_pnl(self):
        src = _src("trade_pipeline.py")
        # The exit-path journal write passes a literal None.
        idx = src.index('status="pending_fill" if is_exit_of_held else "open"')
        region = src[idx - 600:idx + 200]
        assert "pnl=None" in region
        # The old estimator is gone from the module's sell path.
        assert 'pnl = position.get("unrealized_pl")' not in src

    def test_trader_partial_exit_writes_null_pnl(self):
        src = _src("trader.py")
        assert 'position["unrealized_pl"] * (sell_qty / qty)' not in src

    def test_trader_exit_trigger_writes_null_pnl_and_pending_fill(self):
        src = _src("trader.py")
        assert "pnl = pnl_by_symbol.get(symbol)" not in src
        # Status no longer keyed on the estimate's availability.
        assert '"pending_fill" if pnl is not None else "open"' not in src
        assert '_exit_status = "pending_fill"' in src

    def test_no_unrealized_mark_reaches_any_stock_log_trade(self):
        """Class guard: no stock journal write may feed a mark-derived
        value into pnl. Option close paths (occ rows) keep their
        direction-aware estimate by the 2026-07-22 design — the pnl is
        the fill machine's option-close discriminator — so only the
        stock modules are scanned."""
        for name in ("trader.py", "trade_pipeline.py"):
            src = _src(name)
            for m in re.finditer(r"pnl\s*=\s*([^\n]+)", src):
                rhs = m.group(1)
                assert "unrealized" not in rhs, (name, m.group(0))


class TestIdentityHoldsDuringFillWindow:
    def test_pending_exit_with_null_pnl_keeps_drift_zero(self, tmp_path,
                                                         monkeypatch):
        """A buy (filled) + a pending exit written the NEW way (pnl
        NULL, pending_fill, no fill yet): the identity must hold at
        that instant — the exact window p230 was flagged in."""
        import sqlite3
        db = str(tmp_path / "p.db")
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT,
            qty REAL, price REAL, fill_price REAL, order_id TEXT,
            status TEXT DEFAULT 'open', pnl REAL, occ_symbol TEXT,
            stop_loss REAL, take_profit REAL)""")
        conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, price,"
                     " fill_price, order_id, status) VALUES "
                     "('2026-08-24T13:43', 'MRNA', 'buy', 164, 136.62, "
                     "133.88, 'o1', 'open')")
        # The exit, mid-window: decision price only, NO fill, NO pnl.
        conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, price,"
                     " order_id, status) VALUES "
                     "('2026-08-24T13:48', 'MRNA', 'sell', 164, 133.755, "
                     "'o2', 'pending_fill')")
        conn.commit(); conn.close()

        class _Ctx:
            db_path = db
            initial_capital = 250_000.0
            api = None

        import integrity_audit
        monkeypatch.setattr(
            "models.build_user_context_from_profile", lambda pid: _Ctx())
        out = integrity_audit.audit_equity_identity(230)
        assert out["errored"] is None
        assert out["has_drift"] is False, out
        assert out["drift"] == 0.0, out
