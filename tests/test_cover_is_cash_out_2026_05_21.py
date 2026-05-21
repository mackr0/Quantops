"""Regression tests for the cover-classification bug caught 2026-05-21.

Background
----------
`journal.get_virtual_account_info` previously bucketed `cover` trades
alongside `sell`/`short`/`dividend` as cash-IN. That was wrong: a
`cover` is the close-leg of a stock short — the trader buys shares
back to return them to the lender, which is cash OUT. The
misclassification:
  - counted cover notional as +cash (should have been -cash)
  - never subtracted cover notional from cash (it was placed in the
    wrong bucket)
Net effect: every cover trade inflated cash by `2 × notional`.

Caught when pid16 (EXP-A2-NoAltData) displayed +$40K profit while
realized P&L = $54 and unrealized was essentially flat. Two NVTS
covers totaling ~$20,603 notional were inflating cash by ~$41,206 —
exactly matching the reported phantom equity.

Same bug class appeared in the slippage-cost CASE statement at
journal.py:2050 — cover was bucketed with sell/short instead of with
buy/sell_short. Fixed in the same commit.

Tests pin BOTH the equity cash math AND the slippage-cost direction
so a future refactor that re-buckets cover can't silently regress
either.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def vdb(monkeypatch):
    """Minimal trades-table DB matching what test_virtual_account.py uses."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    path = os.path.join(tmpdir, "cover_test.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            pnl REAL,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.commit()
    conn.close()
    return path


def _t(db, symbol, side, qty, price, ts="2026-05-21T10:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price) "
        "VALUES (?,?,?,?,?)",
        (ts, symbol, side, qty, price),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. The core regression: cover is cash OUT
# ---------------------------------------------------------------------------

class TestCoverIsCashOut:
    def test_cover_alone_reduces_cash(self, vdb):
        """A bare `cover` trade — e.g., the closing leg of a short
        booked by reconcile_journal_to_broker — must reduce cash."""
        from journal import get_virtual_account_info
        _t(vdb, "NVTS", "cover", 421, 23.40)  # 421 × $23.40 = $9,851.40
        info = get_virtual_account_info(db_path=vdb, initial_capital=200000)
        # Cash should be initial - 9851.40 = 190148.60
        assert abs(info["cash"] - 190148.60) < 0.01, (
            f"cover {421} @ $23.40 should reduce cash by $9,851.40; "
            f"got cash={info['cash']} (initial 200000). "
            "Pre-2026-05-21 bug: cover was bucketed as cash IN, so "
            "this would have returned 209851.40 — exactly inverted."
        )

    def test_short_then_cover_at_same_price_is_flat(self, vdb):
        """Open a short and close it at the same price → cash is
        approximately unchanged (modulo any FX/financing fees we
        don't model). The bug made this round trip +2×notional."""
        from journal import get_virtual_account_info
        _t(vdb, "NVTS", "short", 421, 23.40, ts="2026-05-21T10:00:00")
        _t(vdb, "NVTS", "cover", 421, 23.40, ts="2026-05-21T11:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=200000)
        assert info["cash"] == 200000, (
            f"short+cover at same price should leave cash unchanged; "
            f"got cash={info['cash']}. Pre-fix bug: cash would be "
            f"200000 + 9851 (short IN) + 9851 (cover wrongly in IN) "
            f"= 219702."
        )

    def test_short_then_cover_at_loss_reduces_cash(self, vdb):
        """Short @ $20, cover @ $22 = $2/share loss × 100 shares = -$200."""
        from journal import get_virtual_account_info
        _t(vdb, "XYZ", "short", 100, 20.00, ts="2026-05-21T10:00:00")
        _t(vdb, "XYZ", "cover", 100, 22.00, ts="2026-05-21T11:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        # short IN: +2000; cover OUT: -2200; net: -200
        assert info["cash"] == 9800

    def test_short_then_cover_at_gain_increases_cash(self, vdb):
        """Short @ $22, cover @ $20 = $2/share gain × 100 = +$200."""
        from journal import get_virtual_account_info
        _t(vdb, "XYZ", "short", 100, 22.00, ts="2026-05-21T10:00:00")
        _t(vdb, "XYZ", "cover", 100, 20.00, ts="2026-05-21T11:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        # short IN: +2200; cover OUT: -2000; net: +200
        assert info["cash"] == 10200


# ---------------------------------------------------------------------------
# 2. Reproduce the EXACT pid16 inflation magnitude
# ---------------------------------------------------------------------------

class TestPid16RepoCase:
    """Reproduces the exact 2026-05-21 incident: 3 NVTS shorts + 2
    NVTS covers on pid16 inflated equity from ~$200K to ~$241K.
    Locking the magnitude here so a regression that re-buckets cover
    can't sneak past."""

    def test_pid16_actual_trades(self, vdb):
        from journal import get_virtual_account_info
        # 3 shorts (cash IN — correct)
        _t(vdb, "NVTS", "short", 421, 23.54, ts="2026-05-21T14:05:19")
        _t(vdb, "NVTS", "short", 467, 23.36, ts="2026-05-21T14:21:57")
        _t(vdb, "NVTS", "short", 516, 23.235, ts="2026-05-21T14:42:25")
        # 2 covers (cash OUT — was buggy)
        _t(vdb, "NVTS", "cover", 421, 23.40, ts="2026-05-21T14:18:15")
        _t(vdb, "NVTS", "cover", 467, 23.0201, ts="2026-05-21T14:25:46")

        info = get_virtual_account_info(db_path=vdb, initial_capital=200000)

        # Cash math (post-fix):
        #   shorts IN:  421*23.54 + 467*23.36 + 516*23.235
        #            =  9910.34   + 10909.12  + 11989.26
        #            =  32808.72
        #   covers OUT: 421*23.40 + 467*23.0201
        #            =  9851.40   + 10750.39
        #            =  20601.79
        #   cash = 200000 + 32808.72 - 20601.79 = 212206.93
        assert abs(info["cash"] - 212206.93) < 0.5, (
            f"pid16 cash math regressed; got {info['cash']}, "
            f"expected ~212206.93. Pre-fix bug returned ~253413 "
            f"(extra +$41,206 phantom)."
        )

    def test_pre_fix_phantom_was_double_the_cover_notional(self, vdb):
        """Property assertion: the inflation per cover row is exactly
        2 × notional (one as spurious cash-IN, one as never-subtracted
        cash-OUT). Pinning this so a partial fix that addresses only
        one direction stays partial-visible."""
        from journal import get_virtual_account_info
        # With fix: cover is cash OUT
        _t(vdb, "X", "cover", 100, 50.00)  # notional = $5,000
        info_fixed = get_virtual_account_info(db_path=vdb, initial_capital=100000)
        # Pre-fix: cash would have been 100000 + 5000 = 105000
        # Post-fix: cash = 100000 - 5000 = 95000
        # Difference (the bug magnitude): 10000 = 2 × notional
        assert info_fixed["cash"] == 95000


# ---------------------------------------------------------------------------
# 3. Regression: existing buy / sell / short / dividend still correct
# ---------------------------------------------------------------------------

class TestNoRegressionOnOtherSides:
    """The fix touches a `side in (...)` tuple. Pin every existing
    classification so a refactor that moves cover around can't quietly
    rebucket buy/sell/short/dividend at the same time."""

    def test_buy_still_reduces_cash(self, vdb):
        from journal import get_virtual_account_info
        _t(vdb, "AAPL", "buy", 10, 100)
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 9000

    def test_sell_still_increases_cash(self, vdb):
        from journal import get_virtual_account_info
        _t(vdb, "AAPL", "buy", 10, 100, ts="2026-05-21T10:00:00")
        _t(vdb, "AAPL", "sell", 10, 110, ts="2026-05-21T11:00:00")
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 10100

    def test_short_still_increases_cash(self, vdb):
        from journal import get_virtual_account_info
        _t(vdb, "XYZ", "short", 100, 20.00)
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 12000

    def test_dividend_still_increases_cash(self, vdb):
        from journal import get_virtual_account_info
        # dividend rows are stored qty=1, price=dividend_amount
        _t(vdb, "AAPL", "dividend", 1, 50.00)
        info = get_virtual_account_info(db_path=vdb, initial_capital=10000)
        assert info["cash"] == 10050


# ---------------------------------------------------------------------------
# 4. Source-level guardrail: cover is in the cash-OUT bucket
# ---------------------------------------------------------------------------

class TestSourceClassification:
    """The actual SQL/Python `side in (...)` clauses are the contract.
    Scanning the source ensures cover stays where it belongs even if
    someone rewrites the cash-flow loop."""

    def test_get_virtual_account_info_buckets_cover_with_buy(self):
        import inspect
        from journal import get_virtual_account_info
        src = inspect.getsource(get_virtual_account_info)
        assert "(\"buy\", \"cover\")" in src or "('buy', 'cover')" in src, (
            "get_virtual_account_info must bucket 'cover' alongside "
            "'buy' (both are cash-OUT). If you refactor the tuple "
            "shape, keep the property: cover is NOT in the cash-IN "
            "tuple with 'sell'/'short'/'dividend'."
        )

    def test_slippage_cost_clause_buckets_cover_with_buy(self):
        """The slippage CASE statement at journal.py:~2050 has the
        same bug class — cover was with sell/short. Pin the fix."""
        import os
        path = os.path.join(
            os.path.dirname(__file__), os.pardir, "journal.py",
        )
        with open(path) as f:
            src = f.read()
        # The fixed clause must include cover in the cash-OUT (buy)
        # bucket of the slippage CASE.
        assert "'sell_short', 'cover'" in src, (
            "Slippage-cost CASE statement must include 'cover' in the "
            "cash-OUT bucket (alongside 'buy', 'sell_short'). The fix "
            "for the 2026-05-21 cover-classification bug touches both "
            "the cash math AND this CASE — don't regress one half."
        )
