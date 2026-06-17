"""2026-06-17 — O5/O6 SOURCE fixes (defense-in-depth behind the
per-cycle option-orphan backstop): close the orphan at the SOURCE so a
position never even shows a one-cycle transient orphan and P&L
attributes to the real broker close order_id.

O5 — a SUCCESSFUL single-leg option exit (trader.check_exits) journals a
pending_fill close row with the real broker order_id (it wrote nothing
before, so the entry rotted at status='open').

O6 — when the roll manager auto-closes a credit SPREAD's short leg, it
also closes the surviving long partner leg(s) of the same combo (it
closed only the credit leg, leaving the long leg naked).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


# ---------------------------------------------------------------------------
# O6 — roll-manager partner sweep (functional)
# ---------------------------------------------------------------------------


def _seed_leg(db, side, occ, strategy="bull_put_spread", qty=1,
              premium=1.0, expiry=None):
    # both legs of a test combo are seeded microseconds apart → well
    # within the partner-pairing 60s window (timestamp defaults to now).
    from journal import log_trade
    expiry = expiry or (date.today() + timedelta(days=3)).isoformat()
    log_trade(symbol="SMR", side=side, qty=qty, price=premium,
              order_id=f"entry-{side}-{occ[-8:]}", signal_type="MULTILEG",
              strategy=strategy, decision_price=premium, occ_symbol=occ,
              option_strategy=strategy, expiry=expiry, strike=12.0,
              fill_price=premium, db_path=db)


class TestO6PartnerSweep:

    def test_credit_spread_closes_both_legs(self, db):
        from options_roll_manager import auto_close_high_profit_credits
        # short put (credit leg) + long put (protective leg), same combo
        _seed_leg(db, "sell", "SMR260724P00012000", premium=1.50)
        _seed_leg(db, "buy", "SMR260724P00011000", premium=0.50)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="close-short"), MagicMock(id="close-long"),
        ]
        # deep profit on the short leg → AUTO_CLOSE; long leg HOLDs.
        res = auto_close_high_profit_credits(
            api, db, quote_lookup=lambda occ: 0.05,
            today=date.today())
        # both legs closed at the broker
        assert api.submit_order.call_count == 2, (
            "the credit leg AND its surviving partner must both be closed"
        )
        with closing(sqlite3.connect(db)) as c:
            statuses = [r[0] for r in c.execute(
                "SELECT status FROM trades WHERE signal_type='MULTILEG'")]
        assert all(s == "pending_fill" for s in statuses), statuses
        assert "open" not in statuses, (
            "no combo leg may be left status='open' (the naked-partner "
            "orphan)"
        )
        assert res.get("partner_legs_closed", 0) == 1

    def test_single_leg_not_partner_swept(self, db):
        """A single-leg covered_call (one OPTIONS row) is auto-closed
        once; no partner query/extra submit."""
        from journal import log_trade
        from options_roll_manager import auto_close_high_profit_credits
        log_trade(symbol="AAPL", side="sell", qty=1, price=2.0,
                  order_id="cc-1", signal_type="OPTIONS",
                  strategy="covered_call", decision_price=2.0,
                  occ_symbol="AAPL260724C00200000",
                  option_strategy="covered_call",
                  expiry=(date.today() + timedelta(days=3)).isoformat(),
                  strike=200.0, fill_price=2.0, db_path=db)
        api = MagicMock()
        api.submit_order.return_value = MagicMock(id="close-cc")
        auto_close_high_profit_credits(
            api, db, quote_lookup=lambda occ: 0.10, today=date.today())
        assert api.submit_order.call_count == 1

    def test_partner_close_failure_is_loud_not_silent(self, db, caplog):
        import logging
        from options_roll_manager import auto_close_high_profit_credits
        _seed_leg(db, "sell", "SMR260724P00012000", premium=1.50)
        _seed_leg(db, "buy", "SMR260724P00011000", premium=0.50)
        api = MagicMock()
        api.submit_order.side_effect = [
            MagicMock(id="close-short"),
            Exception("broker rejected partner close"),
        ]
        with caplog.at_level(logging.ERROR):
            res = auto_close_high_profit_credits(
                api, db, quote_lookup=lambda occ: 0.05, today=date.today())
        assert res["errors"] >= 1
        assert any("partner close FAILED" in r.message for r in caplog.records)
        with closing(sqlite3.connect(db)) as c:
            # the surviving long leg stays open (never falsely closed)
            st = c.execute(
                "SELECT status FROM trades WHERE side='buy'").fetchone()[0]
        assert st == "open"


# ---------------------------------------------------------------------------
# O5 — single-leg exit journaling (structural pin)
# ---------------------------------------------------------------------------


def test_O5_check_exits_journals_close_on_success():
    """Source pin: trader.check_exits' single-leg option-exit branch
    journals a pending_fill close row with the real broker order_id,
    gated on the submitted-success status."""
    src = (REPO / "trader.py").read_text()
    i = src.find("Option exit submitted:")
    assert i > 0
    window = src[i:i + 2200]
    assert 'result.get("status") == "submitted"' in window, (
        "the close must be journaled only on a submitted success"
    )
    assert "log_trade(" in window and 'status="pending_fill"' in window, (
        "a successful single-leg option exit must journal a pending_fill "
        "close row (else the entry rots open — the O5 orphan)"
    )
    assert 'order_id=result.get("order_id")' in window, (
        "the close row must carry the REAL broker order_id for own-id "
        "attribution"
    )
