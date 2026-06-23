"""2026-06-22 — Hard-to-borrow protective stops: retry as DAY orders + learn.

The bug: SPCX was bought as a long (Alpaca reports easy_to_borrow=True, so the
asset-flag gate let it in), but the order engine rejects EVERY GTC protective
stop with *"only day orders are allowed for hard-to-borrow asset"*. The old
code logged the rejection and returned None — the long rode NAKED and churned
the same doomed GTC order every cycle.

The fix (class-wide, broker-driven — not a per-name list):
  1. ``bracket_orders._submit_protective`` retries the SAME order as a DAY
     order when the broker refuses the GTC for HTB reasons, so the held
     position is actually protected (the per-cycle polling stop-loss backstops
     between cycles).
  2. On that authoritative order-time rejection it records the symbol via
     ``journal.record_htb_cooldown`` so the entry gate stops opening fresh
     positions in a name we can't protect with a standing stop. Alpaca's
     unreliable asset flag can no longer keep putting us back in.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The exact string Alpaca emits (verified in prod logs 2026-06-22).
HTB_MSG = 'only day orders are allowed for hard-to-borrow asset "SPCX"'


def _order(oid="ord-day-1"):
    return SimpleNamespace(id=oid)


def _gtc_rejects_day_ok_api():
    """api whose submit_order raises the HTB error on time_in_force='gtc'
    and succeeds on time_in_force='day' (the real broker behavior)."""
    api = MagicMock()

    def _submit(**kwargs):
        if kwargs.get("time_in_force") == "gtc":
            raise Exception(HTB_MSG)
        return _order()

    api.submit_order.side_effect = _submit
    return api


def _tifs(api):
    return [c.kwargs.get("time_in_force") for c in api.submit_order.call_args_list]


# ---------------------------------------------------------------------------
# _submit_protective — the core retry+learn unit
# ---------------------------------------------------------------------------

def _kwargs_trailing():
    return {"symbol": "SPCX", "qty": 100, "side": "sell",
            "type": "trailing_stop", "trail_percent": "5.0"}


def test_is_htb_rejection_matches_real_message():
    import bracket_orders
    assert bracket_orders._is_htb_rejection(Exception(HTB_MSG)) is True
    assert bracket_orders._is_htb_rejection(Exception("hard to borrow")) is True
    assert bracket_orders._is_htb_rejection(
        Exception("insufficient buying power")) is False


def test_submit_protective_gtc_success_no_retry_no_learn(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = MagicMock()
    api.submit_order.return_value = _order("ord-gtc")
    order = bracket_orders._submit_protective(
        api, _kwargs_trailing(), db, "SPCX", "trailing stop for SPCX")
    assert order is not None and order.id == "ord-gtc"
    assert _tifs(api) == ["gtc"]                       # no day retry
    assert "SPCX" not in journal.get_htb_cooldown_symbols(db, 30)  # not learned


def test_submit_protective_retries_as_day_on_htb_and_learns(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = _gtc_rejects_day_ok_api()
    order = bracket_orders._submit_protective(
        api, _kwargs_trailing(), db, "SPCX", "trailing stop for SPCX")
    assert order is not None and order.id == "ord-day-1"
    assert _tifs(api) == ["gtc", "day"]               # GTC first, then DAY
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)  # learned


def test_submit_protective_non_htb_does_not_retry_or_learn(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = MagicMock()
    api.submit_order.side_effect = Exception("insufficient buying power")
    order = bracket_orders._submit_protective(
        api,
        {"symbol": "AAPL", "qty": 10, "side": "sell",
         "type": "stop", "stop_price": 1.0},
        db, "AAPL", "stop for AAPL")
    assert order is None
    assert api.submit_order.call_count == 1           # no day retry
    assert "AAPL" not in journal.get_htb_cooldown_symbols(db, 30)


def test_submit_protective_learns_even_when_day_also_fails(tmp_path):
    """If even the DAY order is refused, we still LEARN the symbol — the
    whole point is to stop opening fresh positions we cannot protect."""
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = MagicMock()
    api.submit_order.side_effect = Exception(HTB_MSG)   # both gtc and day fail
    order = bracket_orders._submit_protective(
        api, _kwargs_trailing(), db, "SPCX", "trailing stop for SPCX")
    assert order is None
    assert api.submit_order.call_count == 2            # gtc then day attempt
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)


# ---------------------------------------------------------------------------
# The three public protective submitters all route through the retry
# ---------------------------------------------------------------------------

def test_trailing_stop_places_day_order_for_htb(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = _gtc_rejects_day_ok_api()
    oid = bracket_orders.submit_protective_trailing(
        api, "SPCX", 100, "sell", 5.0, db_path=db, entry_trade_id=None)
    assert oid == "ord-day-1"
    assert "day" in _tifs(api)
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)


def test_take_profit_places_day_order_for_htb(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = _gtc_rejects_day_ok_api()
    oid = bracket_orders.submit_protective_take_profit(
        api, "SPCX", 100, "sell", 200.0, db_path=db, entry_trade_id=None)
    assert oid == "ord-day-1"
    assert "day" in _tifs(api)
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)


def test_static_stop_places_day_order_for_htb(tmp_path):
    import journal, bracket_orders
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    api = _gtc_rejects_day_ok_api()
    oid = bracket_orders.submit_protective_stop(
        api, "SPCX", 100, "sell", 150.0, db_path=db, entry_trade_id=None)
    assert oid == "ord-day-1"
    assert "day" in _tifs(api)
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)


# ---------------------------------------------------------------------------
# journal.record_htb_cooldown / get_htb_cooldown_symbols — persistence + window
# ---------------------------------------------------------------------------

def test_htb_cooldown_round_trip_and_window(tmp_path):
    import journal
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    journal.record_htb_cooldown(db, "spcx")            # lowercase input
    assert "SPCX" in journal.get_htb_cooldown_symbols(db, 30)  # stored upper
    # Age it past the window — it should fall out.
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE recently_exited_symbols SET exited_at = "
            "datetime('now', '-40 days') WHERE symbol = 'SPCX'")
        c.commit()
    assert "SPCX" not in journal.get_htb_cooldown_symbols(db, 30)


def test_htb_cooldown_does_not_leak_into_wash_or_recent(tmp_path):
    """The HTB marker uses its own trigger — it must not be confused with
    the wash cooldown or the short recent-exit window."""
    import journal
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    journal.record_htb_cooldown(db, "SPCX")
    assert journal.get_wash_cooldown_symbols(db, 30) == set()
    # But the 60-min generic recent-exit query DOES see any row in the
    # table (it's trigger-agnostic) — that's fine, it also blocks entry.
    assert "SPCX" in journal.get_recently_exited(db, 60)


# ---------------------------------------------------------------------------
# Structural: the entry gates + pre-filter actually consult the learned set
# ---------------------------------------------------------------------------

def test_entry_pipeline_consults_learned_htb():
    """trade_pipeline must wire the learned-HTB set into BOTH entry gates
    (long + short) AND the pre-filter early-drop. A refactor that drops any
    of these silently re-opens the naked-HTB hole."""
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "trade_pipeline.py")).read()
    # 2 per-symbol gates (BUY + SHORT) + 1 pre-filter import/use.
    assert src.count("get_htb_cooldown_symbols") >= 3, (
        "learned-HTB gate missing from one of: BUY gate, SHORT gate, "
        "pre-filter")
    assert "learned hard-to-borrow" in src.lower()
