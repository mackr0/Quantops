"""2026-06-17 — get_virtual_positions only warns about qty>0 / price<=0
rows that are genuinely STUCK (past the _task_update_fills backfill
window), NOT freshly-entered multileg legs awaiting their first fill.

A spread leg writes price=NULL by design at entry
(options_multileg._log_strategy_legs refuses a non-positive price) and
the per-leg fill backfills a cycle later. The old warning fired on every
such leg with a misleading "likely a combo-net bug, run the backfill"
message — pure noise. Now it's age-gated.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _seed_null_price_leg(db, occ, ts):
    """A MULTILEG leg with NULL price/fill_price (qty>0) — the exact
    shape of a just-submitted spread leg before its fill backfills."""
    from journal import log_trade
    log_trade(symbol="SMR", side="buy", qty=1, price=None, fill_price=None,
              order_id="oid-" + occ[-6:], signal_type="MULTILEG",
              occ_symbol=occ, status="open", db_path=db)
    with closing(sqlite3.connect(db)) as c:
        c.execute("UPDATE trades SET timestamp=? WHERE occ_symbol=?", (ts, occ))
        c.commit()


def _bad_price_warnings(caplog):
    return [r for r in caplog.records
            if "price<=0" in r.getMessage() or "STUCK" in r.getMessage()]


def test_recent_pending_leg_does_not_warn(db, caplog):
    from journal import get_virtual_positions
    _seed_null_price_leg(db, "SMR260724P00012000", datetime.utcnow().isoformat())
    with caplog.at_level(logging.WARNING):
        get_virtual_positions(db, price_fetcher=lambda s: 1.0)
    assert not _bad_price_warnings(caplog), (
        "a just-entered leg awaiting its first fill must NOT warn — it "
        "self-heals via _task_update_fills")


def test_old_stuck_leg_warns(db, caplog):
    from journal import get_virtual_positions
    old = (datetime.utcnow() - timedelta(minutes=45)).isoformat()
    _seed_null_price_leg(db, "SMR260724P00012000", old)
    with caplog.at_level(logging.WARNING):
        get_virtual_positions(db, price_fetcher=lambda s: 1.0)
    assert _bad_price_warnings(caplog), (
        "a row stuck at price<=0 past the backfill window is genuinely "
        "bad data and must still be surfaced")


def test_warning_is_age_gated_structurally():
    src = (REPO / "journal.py").read_text()
    i = src.find("def get_virtual_positions")
    j = src.find("\ndef ", i + 1)
    body = src[i:j if j > 0 else len(src)]
    assert "_stuck_cutoff" in body and "timedelta(minutes=20)" in body, (
        "the price<=0 warning must be gated on row age, not fire on "
        "every just-entered leg")
