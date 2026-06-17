"""2026-06-17 — every option CLOSE books realized P&L (the +$23,727
multileg-decomposition gap).

ROOT CAUSE: a short option leg is sell-to-open (side='sell'); its close
is buy-to-close (side='buy', occ set). In the fill state-machine that
confirmed close matched NEITHER the sell/cover branch NOR the
pnl-stamped option branch, so it fell to the default and was re-OPENED
(status='open') — re-opening a close order. The short entry never
FIFO-closed and its realized P&L never booked. Every closed multileg
short leg rotted at pnl=NULL → multileg realized summed to $0 → the
decomposition gap that rebuilt on each spread close.

CLASS FIX (choke point): _task_update_fills now routes a
confirmed-filled BUY on an OCC whose net position is SHORT through the
same close+FIFO machinery as a sell/cover; recompute_realized_pnl
(same cycle, after fill_price backfilled) books the realized P&L. This
covers single-leg covered-call/CSP exits, multileg short legs, the
orphan rollback, and any future buy-to-close — with a status- and
reuse-robust discriminator.

O6 in-place partner close stamps the close-time estimate so the LONG
partner (net ≥0) routes via the pnl branch instead of re-opening.
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_FUTURE = (date.today() + timedelta(days=21)).isoformat()


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _order(oid, fill, status="filled", filled_qty=1):
    o = MagicMock()
    o.id = oid
    o.filled_avg_price = fill
    o.status = status
    o.filled_qty = filled_qty
    o.legs = []
    return o


def _run_update_fills(monkeypatch, db, orders_by_id):
    """Drive _task_update_fills with a broker whose get_order returns
    the supplied filled orders. Returns nothing — assert on the DB."""
    import client
    import multi_scheduler

    api = MagicMock()
    api.get_order.side_effect = lambda oid: orders_by_id.get(oid)
    monkeypatch.setattr(client, "get_api", lambda ctx: api)
    ctx = SimpleNamespace(db_path=db, display_name="T", segment="seg",
                          profile_id=1)
    multi_scheduler._task_update_fills(ctx)


def _status_pnl(db, order_id):
    with closing(sqlite3.connect(db)) as c:
        return c.execute(
            "SELECT status, pnl FROM trades WHERE order_id=?",
            (order_id,)).fetchone()


# ---------------------------------------------------------------------------
# 1. The signed-pnl helper — single source of the sign convention.
# ---------------------------------------------------------------------------


class TestRealizedHelper:

    def test_long_profit_and_loss(self):
        from journal import realized_option_close_pnl
        # long: bought 0.50, sold 1.50 → +$100/contract
        assert realized_option_close_pnl(0.50, 1.50, 1, "buy") == 100.0
        # long loss
        assert realized_option_close_pnl(0.50, 0.05, 1, "buy") == -45.0
        # multiplier + qty
        assert realized_option_close_pnl(1.0, 2.0, 3, "buy") == 300.0

    def test_short_profit_and_loss(self):
        from journal import realized_option_close_pnl
        # short: sold 2.00, bought back 0.50 → +$150/contract
        assert realized_option_close_pnl(2.0, 0.5, 1, "sell") == 150.0
        # short loss: sold 1.00, bought back 3.00 → -$200
        assert realized_option_close_pnl(1.0, 3.0, 1, "sell") == -200.0

    def test_none_on_missing_or_invalid(self):
        from journal import realized_option_close_pnl
        assert realized_option_close_pnl(None, 1.0, 1, "buy") is None
        assert realized_option_close_pnl(1.0, None, 1, "sell") is None
        assert realized_option_close_pnl(0.0, 1.0, 1, "buy") is None
        assert realized_option_close_pnl(1.0, -1.0, 1, "sell") is None
        assert realized_option_close_pnl(1.0, 1.0, 0, "buy") is None
        # unknown side never fabricates
        assert realized_option_close_pnl(1.0, 2.0, 1, "weird") is None


# ---------------------------------------------------------------------------
# 2. Functional — the buy-to-close routing + pnl booking through the
#    real fill state-machine (the class fix).
# ---------------------------------------------------------------------------


class TestBuyToCloseBooksPnl:

    def _seed_short_entry(self, db, occ, premium, signal="OPTIONS",
                          strat="covered_call"):
        from journal import log_trade
        log_trade(symbol="AAPL", side="sell", qty=1, price=premium,
                  fill_price=premium, order_id="entry-short",
                  signal_type=signal, occ_symbol=occ, status="open",
                  expiry=_FUTURE, strike=200.0, option_strategy=strat,
                  db_path=db)

    def _seed_close(self, db, occ, signal="OPTIONS"):
        from journal import log_trade
        log_trade(symbol="AAPL", side="buy", qty=1, price=None,
                  order_id="close-buy", signal_type=signal,
                  occ_symbol=occ, status="pending_fill",
                  reason="single_leg_exit: short_premium_take_profit",
                  db_path=db)

    def test_single_leg_short_close_books_profit(self, monkeypatch, db):
        occ = "AAPL260724C00200000"
        self._seed_short_entry(db, occ, 2.0)
        self._seed_close(db, occ)
        # bought back at 0.50 → short profit (2.00 - 0.50)*100 = +150
        _run_update_fills(monkeypatch, db, {
            "close-buy": _order("close-buy", 0.50)})
        cs, cp = _status_pnl(db, "close-buy")
        es, _ = _status_pnl(db, "entry-short")
        assert cs == "closed", "the buy-to-close must terminalize, not re-open"
        assert es == "closed", "the short entry must FIFO-close"
        assert cp is not None and abs(cp - 150.0) < 0.01, (
            f"short close pnl must book (sold 2.00, bought 0.50 = +150); "
            f"got {cp}")

    def test_single_leg_short_close_books_loss(self, monkeypatch, db):
        occ = "AAPL260724C00200000"
        self._seed_short_entry(db, occ, 1.0)
        self._seed_close(db, occ)
        # bought back at 3.00 → short loss (1.00 - 3.00)*100 = -200
        _run_update_fills(monkeypatch, db, {
            "close-buy": _order("close-buy", 3.0)})
        cs, cp = _status_pnl(db, "close-buy")
        assert cs == "closed"
        assert cp is not None and abs(cp - (-200.0)) < 0.01, (
            f"short close loss must book -200; got {cp}")

    def test_multileg_short_leg_close_books_pnl(self, monkeypatch, db):
        occ = "SMR260724P00012000"
        self._seed_short_entry(db, occ, 1.5, signal="MULTILEG",
                               strat="bull_put_spread")
        self._seed_close(db, occ, signal="MULTILEG")
        _run_update_fills(monkeypatch, db, {
            "close-buy": _order("close-buy", 0.30)})
        cs, cp = _status_pnl(db, "close-buy")
        assert cs == "closed"
        assert cp is not None and abs(cp - 120.0) < 0.01, (
            f"multileg short close (1.50 -> 0.30 = +120) must book; got {cp}")

    def test_multileg_long_leg_close_still_books(self, monkeypatch, db):
        """Regression — the LONG leg close (sell-to-close) already went
        through branch A; it must keep booking pnl."""
        from journal import log_trade
        occ = "SMR260724P00011000"
        log_trade(symbol="SMR", side="buy", qty=1, price=0.50,
                  fill_price=0.50, order_id="entry-long",
                  signal_type="MULTILEG", occ_symbol=occ, status="open",
                  expiry=_FUTURE, strike=11.0,
                  option_strategy="bull_put_spread", db_path=db)
        log_trade(symbol="SMR", side="sell", qty=1, price=None,
                  order_id="close-sell", signal_type="MULTILEG",
                  occ_symbol=occ, status="pending_fill", db_path=db)
        _run_update_fills(monkeypatch, db, {
            "close-sell": _order("close-sell", 0.20)})
        cs, cp = _status_pnl(db, "close-sell")
        es, _ = _status_pnl(db, "entry-long")
        assert cs == "closed" and es == "closed"
        # long: bought 0.50, sold 0.20 → -30
        assert cp is not None and abs(cp - (-30.0)) < 0.01, cp

    def test_long_buy_to_OPEN_is_not_treated_as_close(self, monkeypatch, db):
        """The discriminator must NOT over-fire: a fresh long
        buy-to-OPEN (no prior short on the OCC) confirms to 'open',
        never 'closed'."""
        from journal import log_trade
        occ = "TSLA260724C00300000"
        log_trade(symbol="TSLA", side="buy", qty=1, price=None,
                  order_id="open-long", signal_type="OPTIONS",
                  occ_symbol=occ, status="pending_fill", expiry=_FUTURE,
                  strike=300.0, option_strategy="long_call", db_path=db)
        _run_update_fills(monkeypatch, db, {
            "open-long": _order("open-long", 1.25)})
        st, pnl = _status_pnl(db, "open-long")
        assert st == "open", "a buy-to-OPEN must become 'open', not 'closed'"
        assert pnl is None, "an open leg never books realized pnl"


# ---------------------------------------------------------------------------
# 3. O6 partner sweep stamps realized pnl on the in-place close.
# ---------------------------------------------------------------------------


def test_O6_partner_close_stamps_pnl(db):
    from journal import log_trade
    from options_roll_manager import auto_close_high_profit_credits

    def _leg(side, occ, premium):
        log_trade(symbol="SMR", side=side, qty=1, price=premium,
                  fill_price=premium, order_id="comboA",
                  signal_type="MULTILEG", strategy="bull_put_spread",
                  decision_price=premium, occ_symbol=occ,
                  option_strategy="bull_put_spread",
                  expiry=(date.today() + timedelta(days=3)).isoformat(),
                  strike=12.0, db_path=db)

    _leg("sell", "SMR260724P00012000", 1.50)   # credit (short) leg
    _leg("buy", "SMR260724P00011000", 0.50)    # protective (long) partner
    api = MagicMock()
    api.submit_order.side_effect = [
        MagicMock(id="close-short"), MagicMock(id="close-long")]
    auto_close_high_profit_credits(
        api, db, quote_lookup=lambda occ: 0.05, today=date.today())
    with closing(sqlite3.connect(db)) as c:
        rows = c.execute(
            "SELECT occ_symbol, status, pnl FROM trades "
            "WHERE signal_type='MULTILEG'").fetchall()
    by_occ = {r[0]: (r[1], r[2]) for r in rows}
    # long partner closed at 0.05 (entry 0.50) → (0.05-0.50)*100 = -45
    p_status, p_pnl = by_occ["SMR260724P00011000"]
    assert p_status == "pending_fill"
    assert p_pnl is not None and abs(p_pnl - (-45.0)) < 0.01, (
        f"the in-place LONG partner close must stamp realized pnl so it "
        f"routes via the pnl branch instead of re-opening; got {p_pnl}")


# ---------------------------------------------------------------------------
# 4. Structural pins — the contract can't silently regress.
# ---------------------------------------------------------------------------


def test_state_machine_routes_option_buy_to_close():
    src = (REPO / "multi_scheduler.py").read_text()
    assert "def _occ_net_position(" in src, (
        "the net-position discriminator helper must exist")
    i = src.find("def _task_update_fills")
    j = src.find("\ndef ", i + 1)
    body = src[i:j if j > 0 else len(src)]
    assert "_opt_buy_to_close" in body, (
        "the fill state-machine must recognise an option buy-to-close")
    assert "or _opt_buy_to_close" in body, (
        "the buy-to-close must be OR'd into the close+FIFO branch")
    # still gated on a confirmed SHORT net (reuse/ status robust)
    assert "_occ_net_position(" in body and "< -1e-6" in body
    # branch B (pnl discriminator for in-place option closes) intact
    assert 'trade["pnl"] is not None' in body


def test_net_position_excludes_terminal_rows():
    src = (REPO / "multi_scheduler.py").read_text()
    i = src.find("def _occ_net_position(")
    j = src.find("\ndef ", i + 1)
    body = src[i:j if j > 0 else i + 1600]
    for terminal in ("canceled", "rejected", "expired"):
        assert terminal in body, (
            f"net position must exclude {terminal} rows")
    assert "id != ?" in body, "the row under evaluation must be excluded"


def test_helper_is_single_sign_source():
    src = (REPO / "journal.py").read_text()
    assert "def realized_option_close_pnl(" in src
    # O6 reuses the helper rather than re-deriving the sign
    rm = (REPO / "options_roll_manager.py").read_text()
    assert "realized_option_close_pnl(" in rm
