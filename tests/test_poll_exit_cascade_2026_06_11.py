"""Poll-exit cascade pins (2026-06-11).

p97 lost $24.6K of book value in one session to a four-bug cascade:
pre-entry trailing high-water → poll sell against bracket-reserved
shares → partial fill → entry already flipped closed → remainder
orphaned at the broker. See CHANGELOG 2026-06-11 for the full
chain. Each link is pinned here.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (1) Water mark is position-scoped
# ---------------------------------------------------------------------------

def test_trailing_water_mark_floored_at_entry():
    src = (REPO / "portfolio_manager.py").read_text()
    assert "max(\n                    entry_price" in src.replace(
        "max(\n                        entry_price",
        "max(\n                    entry_price"), (
        "Long high-water no longer floored at entry price — "
        "pre-entry highs put the trail above current price and "
        "fire on any +1¢ tick (SMCI $39.70-trail class)."
    )
    assert 'recent_bars["high"].max()' not in src, (
        "Trailing stop is using the 5-day pre-entry high again."
    )
    assert 'recent_bars["low"].min()' not in src, (
        "Short trailing stop is using the 5-day pre-entry low again."
    )


# ---------------------------------------------------------------------------
# (2) Poll exits defer to live bracket protection
# ---------------------------------------------------------------------------

def test_check_exits_defers_bracket_protected_symbols():
    src = (REPO / "trader.py").read_text()
    idx = src.index("has_live_bracket_protection")
    process_idx = src.index("_process_exit_trigger(")
    assert idx < process_idx, (
        "check_exits must filter bracket-protected symbols out of "
        "`triggered` BEFORE processing — the poll selling against "
        "bracket-reserved shares is how the orphaned-remainder "
        "class starts."
    )


def test_has_live_bracket_protection_logic(tmp_path):
    import sqlite3
    from contextlib import closing
    from bracket_orders import has_live_bracket_protection
    db = str(tmp_path / "p.db")
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT,"
            " side TEXT, qty REAL, status TEXT, occ_symbol TEXT,"
            " protective_stop_order_id TEXT,"
            " protective_tp_order_id TEXT)")
        conn.execute(
            "INSERT INTO trades (symbol, side, qty, status, occ_symbol,"
            " protective_stop_order_id, protective_tp_order_id)"
            " VALUES ('PLUG', 'buy', 6241, 'open', NULL,"
            " 'stop-1', 'tp-1')")
        conn.commit()
    api = MagicMock()
    live = MagicMock()
    live.status = "new"
    api.get_order.return_value = live
    assert has_live_bracket_protection(api, db, "PLUG") is True
    # Both children terminal → protection gone → poll may act
    dead = MagicMock()
    dead.status = "canceled"
    api.get_order.return_value = dead
    assert has_live_bracket_protection(api, db, "PLUG") is False
    # No stamps → not bracket-protected
    assert has_live_bracket_protection(api, db, "NOPE") is False


# ---------------------------------------------------------------------------
# (3) fix_partial_sell reopens the entry
# ---------------------------------------------------------------------------

def test_fix_partial_sell_reopens_entry():
    src = (REPO / "reconcile_journal_to_broker.py").read_text()
    blk_idx = src.index('for a in actions["fix_partial_sell"]')
    end_idx = src.index('for a in actions["uncancel_sell"]', blk_idx)
    block = src[blk_idx:end_idx]
    assert "SET status='open'" in block, (
        "fix_partial_sell no longer reopens the matching entry — "
        "partial exits orphan the remainder at the broker again "
        "(p97 $24.6K class)."
    )


# ---------------------------------------------------------------------------
# (4) update_fills trues qty on terminal orders
# ---------------------------------------------------------------------------

def test_update_fills_repolls_recent_open_entries():
    """Qty-truth is useless if the row is never revisited: the
    original selection only pulled `fill_price IS NULL` rows, so a
    partial fill whose price stamped on pass one kept its wrong
    quantity forever. The 48h open-entry re-poll arm must stay."""
    src = (REPO / "multi_scheduler.py").read_text()
    assert "datetime('now', '-2 days')" in src, (
        "update_fills no longer re-polls recent OPEN entries — "
        "partial fills with an early price stamp keep phantom "
        "quantities forever (BATL class, second variant)."
    )


def test_update_fills_trues_qty_on_terminal_orders():
    src = (REPO / "multi_scheduler.py").read_text()
    assert "qty corrected" in src and "_filled_qty" in src, (
        "update_fills no longer corrects journal qty from broker "
        "filled_qty on terminal orders — partially-filled DAY "
        "entries leave phantom virtual shares (BATL 16,419 class)."
    )
    idx = src.index("_filled_qty - _row_qty")
    window = src[max(0, idx - 2000):idx]
    assert '"filled", "canceled", "expired"' in window, (
        "Qty truth must be gated on TERMINAL broker states — "
        "correcting from a still-working order writes a number "
        "that can change again."
    )
