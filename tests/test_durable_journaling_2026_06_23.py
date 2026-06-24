"""Slice 7 — durable journaling / recovery for submitted orders (2026-06-23).

If an order is accepted by the broker but its journal write is then lost (DB
lock / disk), the order would otherwise become an unowned broker position →
orphan-halt. This closes that gap per-profile:
  1. log_trade retries transient DB failures (a momentary lock can't orphan).
  2. the door records every accepted order in submitted_orders the moment the
     broker returns it (before the full log_trade).
  3. the reconciler reconstructs a filled ENTRY from broker truth and drops
     never-filled records, so an unjournaled submit recovers instead of halts.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _db(tmp_path):
    import journal
    db = str(tmp_path / "p.db")
    journal.init_db(db)
    return db


def test_log_trade_retries_transient_db_failure(tmp_path, monkeypatch):
    """A momentary SQLite lock must not orphan a submitted order: log_trade
    retries before giving up."""
    import journal
    db = _db(tmp_path)
    real_get_conn = journal._get_conn
    calls = {"n": 0}

    def flaky(dbp=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_get_conn(dbp)

    monkeypatch.setattr(journal, "_get_conn", flaky)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    tid = journal.log_trade("AAPL", "buy", 10, price=100, order_id="o1",
                            signal_type="BUY", db_path=db)
    assert tid is not None
    assert calls["n"] == 3      # 2 transient failures, then success


def test_record_find_and_prune_submitted_orders(tmp_path):
    import journal
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-123", "AAPL", "buy", 10)
    pend = journal.unjournaled_submitted_orders(db)
    assert len(pend) == 1 and pend[0]["order_id"] == "o-123"
    # once the real trades row exists, it is no longer 'unjournaled'
    journal.log_trade("AAPL", "buy", 10, price=100, order_id="o-123",
                      signal_type="BUY", db_path=db)
    assert journal.unjournaled_submitted_orders(db) == []
    journal.prune_journaled_submitted_orders(db)
    with sqlite3.connect(db) as c:
        assert c.execute(
            "SELECT count(*) FROM submitted_orders").fetchone()[0] == 0


def test_record_submitted_order_is_best_effort(tmp_path):
    """A recovery-record failure must never raise (the order is already live;
    failing here would look like a submit failure)."""
    import journal
    # bad db_path -> internal sqlite error, swallowed
    journal.record_submitted_order("/nonexistent/dir/x.db", "o", "A", "buy", 1)


def test_door_records_submitted_order(tmp_path):
    import journal, order_guard
    db = _db(tmp_path)
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: SimpleNamespace())
    api = MagicMock()
    api.submit_order.return_value = SimpleNamespace(id="o-abc")
    gapi = order_guard.GuardedAlpacaApi(api, ctx)
    gapi.submit_order(symbol="AAPL", side="buy", qty=10, type="market")
    assert any(p["order_id"] == "o-abc"
               for p in journal.unjournaled_submitted_orders(db))


def test_reconstruct_unjournaled_filled_buy(tmp_path):
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-buy", "AAPL", "buy", 10)
    api = MagicMock()
    api.get_order.return_value = SimpleNamespace(
        id="o-buy", status="filled", filled_qty="10", filled_avg_price="101.0")
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    assert R._reconstruct_unjournaled_submits(ctx) == 1
    pos = {p["symbol"]: p["qty"] for p in journal.get_virtual_positions(db)}
    assert pos.get("AAPL") == 10


def test_reconstruct_drops_canceled_order(tmp_path):
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-x", "AAPL", "buy", 10)
    api = MagicMock()
    api.get_order.return_value = SimpleNamespace(
        id="o-x", status="canceled", filled_qty="0", filled_avg_price=None)
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    assert R._reconstruct_unjournaled_submits(ctx) == 0
    assert journal.unjournaled_submitted_orders(db) == []   # dropped


def test_reconstruct_leaves_long_close_sell_for_reconciler(tmp_path):
    """A filled long-close SELL (broker side='sell', no open_short intent) must
    NOT be reconstructed here — its close goes through the reconciler's FIFO."""
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-sell", "AAPL", "sell", 10)
    api = MagicMock()
    api.get_order.return_value = SimpleNamespace(
        id="o-sell", status="filled", filled_qty="10", filled_avg_price="105.0")
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    assert R._reconstruct_unjournaled_submits(ctx) == 0
    assert any(p["order_id"] == "o-sell"
               for p in journal.unjournaled_submitted_orders(db))


def test_reconstruct_short_entry_as_short_not_phantom_long(tmp_path):
    """A deliberate short entry records broker side='sell' + intent='open_short'.
    It must rebuild as a journal SHORT (negative position) — the old code
    skipped it (side='sell') and never recovered the short."""
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-sh", "TSLA", "sell", 10,
                                   intent="open_short")
    api = MagicMock()
    api.get_order.return_value = SimpleNamespace(
        id="o-sh", status="filled", filled_qty="10", filled_avg_price="200.0")
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    assert R._reconstruct_unjournaled_submits(ctx) == 1
    pos = {p["symbol"]: p["qty"] for p in journal.get_virtual_positions(db)}
    assert pos.get("TSLA") == -10              # a SHORT, never a phantom long


def test_reconstruct_skips_buy_to_cover(tmp_path):
    """A broker 'buy' while the profile holds an open SHORT is a buy-to-cover —
    the reconciler's FIFO owns it. It must NOT be rebuilt as a phantom long
    (the bug the side-namespace fix closes)."""
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO trades (symbol,side,qty,price,fill_price,status,"
                  "order_id) VALUES ('NOK','short',10,5.0,5.0,'open','o-se')")
        c.commit()
    journal.record_submitted_order(db, "o-cover", "NOK", "buy", 10)
    api = MagicMock()
    api.get_order.return_value = SimpleNamespace(
        id="o-cover", status="filled", filled_qty="10", filled_avg_price="4.0")
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    assert R._reconstruct_unjournaled_submits(ctx) == 0   # NOT reconstructed
    pos = {p["symbol"]: p["qty"] for p in journal.get_virtual_positions(db)}
    assert pos.get("NOK") == -10               # still a short, no phantom long


def test_reconstruct_short_and_cover_both_lost_no_phantom_long(tmp_path):
    """Two-lost-writes edge: a short-open AND its cover-buy both lost their
    journal writes (both still recorded in the recovery ledger by the door).
    The short is reconstructed FIRST (sells before buys), so the cover-buy then
    correctly SKIPS — never a phantom long."""
    import journal
    import reconcile_journal_to_broker as R
    db = _db(tmp_path)
    journal.record_submitted_order(db, "o-short", "GME", "sell", 5,
                                   intent="open_short")
    journal.record_submitted_order(db, "o-cover", "GME", "buy", 5)

    def _get_order(oid):
        return {
            "o-short": SimpleNamespace(id="o-short", status="filled",
                                       filled_qty="5", filled_avg_price="20.0"),
            "o-cover": SimpleNamespace(id="o-cover", status="filled",
                                       filled_qty="5", filled_avg_price="18.0"),
        }[oid]

    api = MagicMock()
    api.get_order.side_effect = _get_order
    ctx = SimpleNamespace(db_path=db, get_alpaca_api=lambda: api)
    R._reconstruct_unjournaled_submits(ctx)
    pos = {p["symbol"]: p["qty"] for p in journal.get_virtual_positions(db)}
    # the short rebuilt (-5); the cover skipped (left to the reconciler's FIFO).
    # Crucially NOT a phantom long.
    assert pos.get("GME", 0) <= 0, f"phantom long created: {pos.get('GME')}"


def test_option_combo_payload_is_freshness_gated(monkeypatch):
    """Option combo orders carry `legs` with no top-level `symbol`. The
    freshness gate must still fire by falling back to a leg's symbol (the bug:
    the gate read None and silently skipped the default multileg path)."""
    import options_multileg
    import reconcile_journal_to_broker as R
    seen = []
    monkeypatch.setattr(R, "ensure_symbol_fresh",
                        lambda ctx, sym: seen.append(sym))

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "x", "status": "accepted"}
        text = ""

    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp())
    api = SimpleNamespace(_ctx=SimpleNamespace(db_path="x"), _key_id="k",
                          _secret_key="s",
                          _base_url="https://paper-api.alpaca.markets")
    payload = {"order_class": "mleg", "qty": 1,
               "legs": [{"symbol": "AAPL260101C00100000", "side": "buy",
                         "ratio_qty": 1}]}
    options_multileg._submit_alpaca_order_raw(api, payload)
    assert seen == ["AAPL260101C00100000"]      # gated via the leg symbol
