"""Bracket-child protective tracking (2026-06-10 PM).

Bug class: the bracket-order architecture (06-09/06-10) made Alpaca
create the protective stop + TP as server-side child legs — but the
journal contract still assumed every protective order is placed by
our own submit_order call (which writes a pending_protective row).
Three consequences observed in the first post-reset session:

  1. The at-submit `get_order(nested=True)` refetch RACED the
     broker's child materialization: legs came back empty (silently
     — not an exception) on EVERY entry, so protective_*_order_id
     stamps stayed NULL.
  2. No code path wrote pending_protective rows for the children.
  3. When the first child filled (WCT stop 3f61e6fe @ $2.06), the
     reconciler classified it as orphan synthesis ("no pending row
     = submit_order journaling leak") and HALTED all 13 profiles.

The fix adds three layers + one noise fix:
  A. trade_pipeline: retry the nested fetch (children materialize
     within ~a second), then journal both children as
     pending_protective rows.
  B. bracket_orders.ensure_protective_stops: the bracket-skip
     branch heals missing stamps + pending rows every sweep
     (_heal_bracket_child_tracking).
  C. reconcile_journal_to_broker: _is_bracket_child_fill exempts
     fills that are child legs of the entry's bracket parent from
     the halt counter — broker-created legs are EXPECTED synthesis.
  D. intraday_risk_monitor.clear_risk_halt: "no such table" on a
     fresh-start DB is debug, not a WARNING every cycle.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (C) _is_bracket_child_fill unit behavior
# ---------------------------------------------------------------------------

def _conn_with_entry(order_id="parent-1"):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id TEXT)")
    conn.execute(
        "INSERT INTO trades (id, order_id) VALUES (7, ?)", (order_id,))
    conn.commit()
    return conn


def _mock_parent(order_class="bracket", leg_ids=("child-stop", "child-tp")):
    parent = MagicMock()
    parent.order_class = order_class
    legs = []
    for lid in leg_ids:
        leg = MagicMock()
        leg.id = lid
        legs.append(leg)
    parent.legs = legs
    return parent


def test_bracket_child_fill_recognized():
    from reconcile_journal_to_broker import _is_bracket_child_fill
    api = MagicMock()
    api.get_order.return_value = _mock_parent()
    conn = _conn_with_entry()
    assert _is_bracket_child_fill(
        api, conn, {"trade_id": 7}, "child-stop") is True
    api.get_order.assert_called_with("parent-1", nested=True)


def test_non_bracket_parent_not_exempted():
    from reconcile_journal_to_broker import _is_bracket_child_fill
    api = MagicMock()
    api.get_order.return_value = _mock_parent(order_class="simple")
    conn = _conn_with_entry()
    assert _is_bracket_child_fill(
        api, conn, {"trade_id": 7}, "child-stop") is False


def test_unrelated_fill_not_exempted():
    from reconcile_journal_to_broker import _is_bracket_child_fill
    api = MagicMock()
    api.get_order.return_value = _mock_parent()
    conn = _conn_with_entry()
    assert _is_bracket_child_fill(
        api, conn, {"trade_id": 7}, "some-other-order") is False


def test_lookup_failure_falls_back_to_halt_path():
    from reconcile_journal_to_broker import _is_bracket_child_fill
    api = MagicMock()
    api.get_order.side_effect = Exception("api down")
    conn = _conn_with_entry()
    assert _is_bracket_child_fill(
        api, conn, {"trade_id": 7}, "child-stop") is False


# ---------------------------------------------------------------------------
# (B) _heal_bracket_child_tracking unit behavior
# ---------------------------------------------------------------------------

def _full_trades_db(tmp_path):
    db = str(tmp_path / "p.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades ("
        " id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT,"
        " side TEXT, qty REAL, price REAL, order_id TEXT,"
        " signal_type TEXT, status TEXT, reason TEXT,"
        " protective_stop_order_id TEXT, protective_tp_order_id TEXT,"
        " protective_trailing_order_id TEXT)")
    conn.execute(
        "INSERT INTO trades (id, symbol, side, qty, price, order_id,"
        " status) VALUES (1, 'WCT', 'buy', 100, 2.21, 'parent-1',"
        " 'open')")
    conn.commit()
    return db, conn


def _mock_bracket_parent():
    parent = MagicMock()
    parent.order_class = "bracket"
    stop = MagicMock()
    stop.id = "child-stop"
    stop.order_type = "stop"
    stop.status = "new"
    stop.stop_price = "2.06"
    tp = MagicMock()
    tp.id = "child-tp"
    tp.order_type = "limit"
    tp.status = "held"
    tp.limit_price = "2.48"
    parent.legs = [stop, tp]
    return parent


def test_heal_stamps_and_writes_pending_rows(tmp_path):
    from bracket_orders import _heal_bracket_child_tracking
    db, conn = _full_trades_db(tmp_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=1").fetchone()
    _heal_bracket_child_tracking(
        conn, db, row, _mock_bracket_parent(), "WCT", "sell", 100,
    )
    healed = conn.execute(
        "SELECT protective_stop_order_id, protective_tp_order_id "
        "FROM trades WHERE id=1").fetchone()
    assert healed[0] == "child-stop"
    assert healed[1] == "child-tp"
    pend = conn.execute(
        "SELECT order_id, signal_type, status FROM trades "
        "WHERE status='pending_protective' ORDER BY order_id"
    ).fetchall()
    assert {(p[0], p[1]) for p in pend} == {
        ("child-stop", "PROTECTIVE_STOP"),
        ("child-tp", "PROTECTIVE_TP"),
    }


def test_heal_is_idempotent(tmp_path):
    from bracket_orders import _heal_bracket_child_tracking
    db, conn = _full_trades_db(tmp_path)
    conn.row_factory = sqlite3.Row
    for _ in range(2):
        row = conn.execute("SELECT * FROM trades WHERE id=1").fetchone()
        _heal_bracket_child_tracking(
            conn, db, row, _mock_bracket_parent(), "WCT", "sell", 100,
        )
    n = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='pending_protective'"
    ).fetchone()[0]
    assert n == 2  # one per child, not duplicated on the second pass


def test_heal_skips_terminal_unfilled_legs(tmp_path):
    from bracket_orders import _heal_bracket_child_tracking
    db, conn = _full_trades_db(tmp_path)
    conn.row_factory = sqlite3.Row
    parent = _mock_bracket_parent()
    parent.legs[1].status = "canceled"  # OCO partner already dead
    row = conn.execute("SELECT * FROM trades WHERE id=1").fetchone()
    _heal_bracket_child_tracking(
        conn, db, row, parent, "WCT", "sell", 100,
    )
    pend = conn.execute(
        "SELECT order_id FROM trades WHERE status='pending_protective'"
    ).fetchall()
    assert [p[0] for p in pend] == ["child-stop"]


# ---------------------------------------------------------------------------
# (A) source pins — at-submit retry + pending-row writes
# ---------------------------------------------------------------------------

def test_submit_path_retries_nested_fetch_and_writes_pending_rows():
    src = (REPO / "trade_pipeline.py").read_text()
    start = src.index('"order_class": "bracket"')
    end = src.index("---- SELL logic", start)
    block = src[start:end]
    assert "for _bk_attempt in range(3)" in block, (
        "Bracket nested-fetch retry removed — the at-submit child "
        "fetch races broker materialization and silently returns "
        "empty legs (all-profiles halt class, 2026-06-10 PM)."
    )
    assert block.count("_write_pending_protective_row") >= 1, (
        "Bracket children no longer journaled as pending_protective "
        "at submit — the reconciler will halt on every child fill."
    )
    assert '"PROTECTIVE_STOP"' in block and '"PROTECTIVE_TP"' in block


def test_sweep_bracket_branch_heals():
    src = (REPO / "bracket_orders.py").read_text()
    start = src.index("def ensure_protective_stops")
    block = src[start:]
    heal_idx = block.index("_heal_bracket_child_tracking(")
    bracket_idx = block.index('== "bracket"')
    assert heal_idx > bracket_idx, (
        "Bracket-skip branch no longer heals child tracking before "
        "continue — NULL stamps would persist for the position's "
        "whole life."
    )
    assert "nested=True" in block[:heal_idx], (
        "Sweep's parent fetch must use nested=True or legs are "
        "unavailable for healing."
    )


def test_reconciler_exempts_bracket_children_before_halt():
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    pending_idx = src.index("status = 'pending_protective'")
    orphan_idx = src.index("still_orphan_protective.append")
    check_idx = src.index("_is_bracket_child_fill(", pending_idx)
    assert check_idx < orphan_idx, (
        "Reconciler must check bracket-child linkage BEFORE "
        "classifying a protective fill as orphan synthesis — "
        "otherwise every bracket exit halts the profile."
    )


# ---------------------------------------------------------------------------
# (D) clear_risk_halt missing-table noise
# ---------------------------------------------------------------------------

def test_clear_risk_halt_quiet_on_missing_table(tmp_path, caplog):
    import logging
    from intraday_risk_monitor import clear_risk_halt
    db = str(tmp_path / "fresh.db")
    sqlite3.connect(db).close()  # valid DB, no tables
    with caplog.at_level(logging.WARNING):
        clear_risk_halt(db)
    assert not [r for r in caplog.records
                if "halt clear write failed" in r.message], (
        "Missing intraday_risk_halt table on a fresh-start DB must "
        "not WARN every cycle — it means 'never halted', not a "
        "failure."
    )
