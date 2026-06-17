"""2026-06-17 — the per-cycle OPTION-ORPHAN BACKSTOP makes option
orphans impossible from ANY cause (early assignment/exercise, manual
or external close, or a missed close-journaling — O5/O6/O7/O8).

reconcile_option_orphans runs every reconcile cycle and closes any
OPEN option leg (LONG or SHORT) the broker no longer holds. The key
correctness rules it must obey:

  • SHARED-ACCOUNT SAFE: act ONLY when the account-level OCC qty is
    ZERO (flat for everyone ⇒ flat for us). A non-zero OCC qty may be
    a sibling's identical contract on the shared conduit, so it is left
    untouched — never consume a sibling's contract.
  • EXPIRY HANDOFF: expiry<=today is left for sweep_expired_options
    (which owns intrinsic value / assignment legs); this pass acts only
    on expiry>today or NULL — the exact inverse.
  • NEVER A HALT: a broker-flat option close is an EXPECTED reconcile.
  • Short legs (O8): the stock loop's `if side=='sell': continue`
    skipped them entirely; this pass covers them.
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

_FUTURE = (date.today() + timedelta(days=30)).isoformat()
_PAST = (date.today() - timedelta(days=2)).isoformat()


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _seed(db, **c):
    from journal import log_trade
    log_trade(db_path=db, **c)


def _occ(sym="SMR", right="C", strike="00012000"):
    return f"{sym}260724{right}{strike}"


def _pos(symbol, qty):
    p = MagicMock(); p.symbol = symbol; p.qty = qty
    return p


def _order(oid, status="filled", filled_qty=1):
    o = MagicMock(); o.id = oid; o.status = status
    o.filled_qty = filled_qty
    return o


def _virtual_occ_qty(db, occ):
    from journal import get_virtual_positions
    pos = get_virtual_positions(db, price_fetcher=lambda s: 1.0)
    for r in (pos if isinstance(pos, list) else []):
        if r.get("occ_symbol") == occ:
            return r.get("qty", 0)
    return 0


def _run(db, positions, get_order=None, apply_changes=True):
    from reconcile_journal_to_broker import reconcile_option_orphans
    api = MagicMock()
    api.get_order.side_effect = (
        get_order or (lambda oid: _order(oid, "filled", 1)))
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return reconcile_option_orphans(
            api, conn, positions, today=date.today(),
            apply_changes=apply_changes)


class TestBackstop:

    def test_O8_short_leg_broker_flat_is_closed(self, db):
        """A SHORT option leg (side='sell') the broker no longer holds
        must be auto-closed — the stock loop skipped short legs (O8)."""
        occ = _occ()
        _seed(db, symbol="SMR", side="sell", qty=2, price=0.59,
              order_id="short-1", signal_type="MULTILEG",
              occ_symbol=occ, expiry=_FUTURE, strike=12.0,
              option_strategy="bull_call_spread")
        closed = _run(db, positions=[])  # broker flat
        assert len(closed) == 1 and closed[0]["kind"] == "auto_closed"
        assert _virtual_occ_qty(db, occ) == 0, (
            "a broker-flat short option leg must leave the book "
            "(the O8 orphan)"
        )

    def test_O7_long_leg_broker_flat_is_closed(self, db):
        occ = _occ(right="C", strike="00011500")
        _seed(db, symbol="SMR", side="buy", qty=2, price=1.76,
              order_id="long-1", signal_type="OPTIONS",
              occ_symbol=occ, expiry=_FUTURE, strike=11.5,
              option_strategy="long_call")
        closed = _run(db, positions=[])
        assert len(closed) == 1 and closed[0]["kind"] == "auto_closed"
        assert _virtual_occ_qty(db, occ) == 0

    def test_held_option_left_untouched(self, db):
        """Broker STILL holds the OCC → leave it (could be ours or a
        sibling's on the shared account). Never close."""
        occ = _occ()
        _seed(db, symbol="SMR", side="buy", qty=2, price=1.76,
              order_id="held-1", signal_type="OPTIONS",
              occ_symbol=occ, expiry=_FUTURE, strike=11.5,
              option_strategy="long_call")
        closed = _run(db, positions=[_pos(occ, "2")])  # broker holds it
        assert closed == []
        with closing(sqlite3.connect(db)) as c:
            st = c.execute("SELECT status FROM trades WHERE order_id='held-1'"
                           ).fetchone()[0]
        assert st == "open"

    def test_expiry_handoff_skips_expired(self, db):
        """expiry<=today is the expiry sweep's domain — skip it here."""
        occ = _occ()
        _seed(db, symbol="SMR", side="buy", qty=2, price=1.76,
              order_id="exp-1", signal_type="MULTILEG",
              occ_symbol=occ, expiry=_PAST, strike=11.5,
              option_strategy="long_call")
        closed = _run(db, positions=[])  # broker flat but expired
        assert closed == [], "expired legs are deferred to sweep_expired_options"

    def test_canceled_entry_marked_canceled(self, db):
        occ = _occ()
        _seed(db, symbol="SMR", side="buy", qty=2, price=1.76,
              order_id="cxl-1", signal_type="OPTIONS",
              occ_symbol=occ, expiry=_FUTURE, strike=11.5,
              option_strategy="long_call")
        closed = _run(db, positions=[],
                      get_order=lambda oid: _order(oid, "canceled", 0))
        assert len(closed) == 1 and closed[0]["kind"] == "canceled"
        with closing(sqlite3.connect(db)) as c:
            st = c.execute("SELECT status FROM trades WHERE order_id='cxl-1'"
                           ).fetchone()[0]
        assert st == "canceled"

    def test_dry_run_does_not_write(self, db):
        occ = _occ()
        _seed(db, symbol="SMR", side="sell", qty=2, price=0.59,
              order_id="dry-1", signal_type="MULTILEG",
              occ_symbol=occ, expiry=_FUTURE, strike=12.0,
              option_strategy="bull_call_spread")
        closed = _run(db, positions=[], apply_changes=False)
        assert len(closed) == 1  # reported
        with closing(sqlite3.connect(db)) as c:
            st = c.execute("SELECT status FROM trades WHERE order_id='dry-1'"
                           ).fetchone()[0]
        assert st == "open", "dry-run must not write"


class TestBackstopIntegrationNoHalt:

    def test_broker_flat_option_does_not_halt(self, db, monkeypatch):
        """Through reconcile_with_ctx: a broker-flat option closes via
        option_orphan_close and does NOT trip the synthesis HALT."""
        from reconcile_journal_to_broker import reconcile_with_ctx
        occ = _occ()
        _seed(db, symbol="SMR", side="sell", qty=2, price=0.59,
              order_id="nh-1", signal_type="MULTILEG",
              occ_symbol=occ, expiry=_FUTURE, strike=12.0,
              option_strategy="bull_call_spread")
        api = MagicMock()
        api.list_positions.return_value = []      # broker flat
        api.get_order.side_effect = lambda oid: _order(oid, "filled", 2)
        halted = {}
        import halt_helpers
        monkeypatch.setattr(halt_helpers, "halt_and_alert",
                            lambda *a, **k: halted.setdefault("h", True))
        ctx = SimpleNamespace(
            api=api, get_alpaca_api=lambda: api, db_path=db,
            display_name="T", profile_id=99, alpaca_account_id=1)
        res = reconcile_with_ctx(ctx, apply_changes=True)
        assert len(res["option_orphan_close"]) == 1
        assert res.get("halted_synthesis_count") in (None, 0)
        assert "h" not in halted, "a normal option close must NOT halt"
        assert _virtual_occ_qty(db, occ) == 0


def test_backstop_is_invoked_unconditionally():
    """Structural: reconcile_with_ctx calls reconcile_option_orphans
    with no feature-flag/getattr gate."""
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    idx = src.find("def reconcile_with_ctx")
    end = src.find("\ndef ", idx + 1)
    body = src[idx:end if end > 0 else len(src)]
    # the call site (inside reconcile_with_ctx), not the def/docstring
    call = body.find("_opt_orphans = reconcile_option_orphans(")
    assert call > 0, "reconcile_with_ctx must call reconcile_option_orphans"
    # not guarded by an `if <flag>:`/getattr gate just before the call
    preceding = body[:call].splitlines()[-3:]
    assert not any("getattr" in l and l.strip().startswith("if ")
                   for l in preceding)
