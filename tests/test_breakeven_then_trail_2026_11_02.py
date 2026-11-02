"""2026-11-02 — breakeven-then-trail: the protective sweep arms a
trailing stop only once the position is up ≥ 1R, protects with a static
stop at entry until then, and sizes the trail to the name's volatility.

Why: since the 08-24 reset the 5% trail-from-the-high armed at entry
produced 565 trailing fills for −$122,421 (the AI's own exit decisions:
+$74,201), median 0.1 days after entry, 78% of stopped longs had been
up >1% first, and price was back +1.8% in the position's favor by the
next close in 69% of a sample — an intraday-noise stop.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import bracket_orders as bo  # noqa: E402


# ── pure helpers ────────────────────────────────────────────────────

class TestTrailWidth:
    def test_never_tighter_than_the_stop_width(self):
        assert bo.trail_percent_for_position(0.05, None) == 5.0
        assert bo.trail_percent_for_position(0.05, 0.01) == 5.0        # 2.5×1% = 2.5 < 5

    def test_scales_with_volatility_and_caps(self):
        assert bo.trail_percent_for_position(0.05, 0.03) == 7.5        # 2.5×3%
        assert bo.trail_percent_for_position(0.05, 0.06) == bo.TRAIL_PERCENT_MAX_VOL

    def test_invalid_stop_is_none(self):
        assert bo.trail_percent_for_position(0, 0.03) is None


class TestArming:
    def test_long_arms_at_one_r(self):
        assert not bo.trail_armed(100.0, 104.9, False, 0.05)
        assert bo.trail_armed(100.0, 105.0, False, 0.05)

    def test_short_arms_when_price_falls_one_r(self):
        assert not bo.trail_armed(100.0, 96.0, True, 0.05)
        assert bo.trail_armed(100.0, 95.0, True, 0.05)

    def test_unknown_price_never_arms(self):
        assert not bo.trail_armed(100.0, 0, False, 0.05)
        assert not bo.trail_armed(100.0, None, False, 0.05)
        assert not bo.trail_armed(0, 105.0, False, 0.05)


# ── the sweep ───────────────────────────────────────────────────────

def _ctx():
    return SimpleNamespace(stop_loss_pct=0.05, short_stop_loss_pct=None,
                           use_trailing_stops=True)


def _position(symbol="BMNR", qty=100, avg=20.0, cur=None):
    return {"symbol": symbol, "qty": qty, "avg_entry_price": avg,
            "current_price": avg * 1.01 if cur is None else cur,
            "is_option": False, "occ_symbol": None}


def _entry(db_path, *, symbol="BMNR", qty=100, trailing_id=None, stop_id=None):
    import journal
    journal.init_db(db_path)
    journal.log_trade(symbol=symbol, side="buy", qty=qty, price=20.0,
                      order_id=f"entry-{symbol}", signal_type="BUY", db_path=db_path)
    sets, vals = [], []
    if trailing_id is not None:
        sets.append("protective_trailing_order_id = ?"); vals.append(trailing_id)
    if stop_id is not None:
        sets.append("protective_stop_order_id = ?"); vals.append(stop_id)
    if sets:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE symbol = ? "
                         "AND side = 'buy' AND status = 'open'", (*vals, symbol))
            conn.commit()


def _api(active_ids=(), broker_qty=100):
    api = MagicMock()
    api.get_position.side_effect = lambda s: SimpleNamespace(symbol=s, qty=str(broker_qty))
    api.get_order.side_effect = lambda oid, *a, **k: SimpleNamespace(
        id=oid, status="new" if oid in active_ids else "canceled", order_class="", legs=[], qty="0")
    api.list_orders.return_value = []
    api.list_positions.return_value = []
    n = {"i": 0}
    def _submit(**kw):
        n["i"] += 1
        return SimpleNamespace(id=f"new-{n['i']}", status="new", qty=str(kw.get("qty", 0)))
    api.submit_order.side_effect = _submit
    return api


def _run(api, db, pos, monkeypatch, atr_pct=0.03):
    monkeypatch.setattr("bracket_orders.active_protective_coverage", lambda _api: {})
    monkeypatch.setattr("bracket_orders._atr_pct", lambda symbol, price: atr_pct)
    bo.ensure_protective_stops(api=api, positions=[pos], ctx=_ctx(), db_path=db)


def _types(api):
    return [c.kwargs.get("type") for c in api.submit_order.call_args_list]


def _row(db):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT protective_stop_order_id s, protective_trailing_order_id t "
                                 "FROM trades WHERE side='buy'").fetchone())


class TestSweepBreakevenThenTrail:
    def test_fresh_position_gets_a_static_stop_not_a_trail(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db)
        api = _api()
        _run(api, db, _position(cur=20.2), monkeypatch)      # +1%: below 1R
        assert _types(api) == ["stop"]
        kw = api.submit_order.call_args_list[0].kwargs
        assert float(kw["stop_price"]) == pytest.approx(19.0, abs=0.01)   # entry − 5%
        r = _row(db); assert r["s"] == "new-1" and r["t"] is None

    def test_position_up_one_r_arms_a_volatility_scaled_trail(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db, stop_id="stop-old")
        api = _api(active_ids=("stop-old",))
        _run(api, db, _position(cur=21.1), monkeypatch, atr_pct=0.03)   # +5.5% ≥ 1R
        assert _types(api) == ["trailing_stop"]
        kw = api.submit_order.call_args_list[0].kwargs
        assert float(kw["trail_percent"]) == 7.5                          # max(5, 2.5×3%)
        api.cancel_order.assert_called_with("stop-old")                   # one reservation
        assert _row(db)["t"] == "new-1"

    def test_live_trailing_stop_is_never_churned(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db, trailing_id="trail-live")
        api = _api(active_ids=("trail-live",))
        _run(api, db, _position(cur=20.2), monkeypatch)      # below 1R, but a trail is live
        assert _types(api) == []
        api.cancel_order.assert_not_called()

    def test_live_static_stop_below_one_r_is_left_alone(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db, stop_id="stop-live")
        api = _api(active_ids=("stop-live",))
        _run(api, db, _position(cur=20.2), monkeypatch)
        assert _types(api) == []

    def test_unknown_current_price_protects_with_the_static_stop(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db)
        api = _api()
        _run(api, db, _position(cur=0), monkeypatch)
        assert _types(api) == ["stop"]

    def test_no_atr_falls_back_to_the_stop_width(self, tmp_path, monkeypatch):
        db = str(tmp_path / "p.db"); _entry(db)
        api = _api()
        _run(api, db, _position(cur=21.5), monkeypatch, atr_pct=None)
        assert _types(api) == ["trailing_stop"]
        assert float(api.submit_order.call_args_list[0].kwargs["trail_percent"]) == 5.0
