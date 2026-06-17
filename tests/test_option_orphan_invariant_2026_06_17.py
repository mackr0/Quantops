"""2026-06-17 — OPTION EXPIRY ORPHANS ARE IMPOSSIBLE.

The operator's hard rule: an orphan (journal shows an open position the
broker does not have) must be structurally impossible — an orphan means
something is broken.

The bug: find_expired_open_options matched only signal_type='OPTIONS',
so every MULTILEG / MULTILEG_OPEN spread leg was never swept at expiry.
After expiry the broker zeroes the contract but the leg stayed
status='open' forever, and get_virtual_positions kept reporting its OCC
as a held lot — a permanent orphan.

The fix keys the sweep on `occ_symbol IS NOT NULL` (every option row has
one; nothing else does) instead of a signal_type allow-list that
silently falls behind. This pins:

  INVARIANT — after the lifecycle sweep, NO option-bearing row can be
  (status counted-open by get_virtual_positions) AND (expiry < today).
  The set of expired-but-held option lots after a sweep is EMPTY.

Plus negative controls (a rollback CLOSE row must not be mis-booked as
an expired entry; equity legs and unexpired options are untouched) and a
structural pin (the filter must not regress to a signal_type allow-list).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_TODAY = date(2026, 6, 17)
_PAST = "2026-06-01"     # expired
_FUTURE = "2099-01-15"   # not expired


@pytest.fixture
def db(tmp_path):
    from journal import init_db
    p = str(tmp_path / "p.db")
    init_db(p)
    return p


def _seed(db, **c):
    from journal import log_trade
    log_trade(db_path=db, **c)


def _occ(sym, right="C", strike="00012000"):
    # 21-char-ish OCC; only occ_symbol presence + [12]=right matter here
    return f"{sym}260601{right}{strike}"


def _virtual_occ_qty(db, occ):
    from journal import get_virtual_positions
    pos = get_virtual_positions(db, price_fetcher=lambda s: 1.0)
    for r in (pos if isinstance(pos, list) else []):
        if r.get("occ_symbol") == occ:
            return r.get("qty", 0)
    return 0


def _stub_api(broker_positions=None):
    api = MagicMock()
    api.list_positions.return_value = broker_positions or []
    return api


class TestExpiryOrphanInvariant:

    def test_multileg_legs_are_swept_no_orphan(self, db):
        """The primary fix: MULTILEG and MULTILEG_OPEN expired legs must
        be swept (closed), leaving NO expired option held in the book."""
        from options_lifecycle import (sweep_expired_options,
                                        find_expired_open_options)
        # bull call spread, both legs expired OTM (underlying below both)
        _seed(db, symbol="SMR", side="buy", qty=2, price=1.76,
              order_id="long", signal_type="MULTILEG",
              strategy="bull_call_spread", option_strategy="bull_call_spread",
              occ_symbol=_occ("SMR", "C", "00011500"), expiry=_PAST,
              strike=11.5, decision_price=1.76)
        _seed(db, symbol="SMR", side="sell", qty=2, price=0.24,
              order_id="short", signal_type="MULTILEG_OPEN",
              strategy="bull_call_spread", option_strategy="bull_call_spread",
              occ_symbol=_occ("SMR", "C", "00012000"), expiry=_PAST,
              strike=12.0, decision_price=0.24)
        api = _stub_api()  # broker flat
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=9.89):  # below both strikes → OTM
            res = sweep_expired_options(api, db_path=db, today=_TODAY)
        assert res["expired_found"] == 2
        # INVARIANT: nothing expired remains matchable, and the book
        # holds none of these OCCs.
        assert find_expired_open_options(db, today=_TODAY) == []
        assert _virtual_occ_qty(db, _occ("SMR", "C", "00011500")) == 0
        assert _virtual_occ_qty(db, _occ("SMR", "C", "00012000")) == 0
        # rows are terminal
        with closing(sqlite3.connect(db)) as c:
            statuses = [r[0] for r in c.execute(
                "SELECT status FROM trades WHERE occ_symbol IS NOT NULL")]
        assert all(s == "closed" for s in statuses), statuses

    def test_single_leg_still_swept(self, db):
        """Regression guard: single-leg OPTIONS expiry still handled."""
        from options_lifecycle import find_expired_open_options
        _seed(db, symbol="AAPL", side="buy", qty=1, price=2.5,
              order_id="o", signal_type="OPTIONS", strategy="long_call",
              option_strategy="long_call", occ_symbol=_occ("AAPL"),
              expiry=_PAST, strike=12.0, decision_price=2.5)
        assert len(find_expired_open_options(db, today=_TODAY)) == 1

    def test_unexpired_option_untouched(self, db):
        from options_lifecycle import find_expired_open_options
        _seed(db, symbol="AAPL", side="buy", qty=1, price=2.5,
              order_id="o", signal_type="MULTILEG", option_strategy="x",
              occ_symbol=_occ("AAPL"), expiry=_FUTURE, strike=12.0)
        assert find_expired_open_options(db, today=_TODAY) == []

    def test_equity_leg_untouched(self, db):
        """OPTION_EXERCISE synthetic equity legs (no occ_symbol) must
        never be swept by the option sweep."""
        from options_lifecycle import find_expired_open_options
        _seed(db, symbol="AAPL", side="buy", qty=100, price=150,
              order_id="eq", signal_type="OPTION_EXERCISE")  # no occ/expiry
        assert find_expired_open_options(db, today=_TODAY) == []

    def test_rollback_close_row_not_mis_booked(self, db):
        """O2: a partner-rollback CLOSE row (reason 'Auto-rollback:…')
        that decayed to status='open' must NOT be swept as an expired
        entry and given a fabricated expired-worthless P&L."""
        from options_lifecycle import (sweep_expired_options,
                                        find_expired_open_options)
        _seed(db, symbol="SMR", side="sell", qty=2, price=0.50,
              order_id="rb", signal_type="MULTILEG",
              option_strategy="bull_call_spread",
              occ_symbol=_occ("SMR", "C", "00012000"), expiry=_PAST,
              strike=12.0, decision_price=0.50,
              reason="Auto-rollback: combo bull_call_spread on SMR had "
                     "partner leg expire unfilled. Closing this leg.")
        # excluded from the entry sweep
        assert find_expired_open_options(db, today=_TODAY) == []
        api = _stub_api()
        with patch("options_lifecycle._underlying_close_at_expiry",
                   return_value=9.89):
            res = sweep_expired_options(api, db_path=db, today=_TODAY)
        assert res["expired_found"] == 0, (
            "a rollback CLOSE row must not be swept as an expired entry "
            "(it would fabricate a +$100 'short premium kept' P&L)"
        )
        with closing(sqlite3.connect(db)) as c:
            pnl = c.execute(
                "SELECT pnl FROM trades WHERE order_id='rb'").fetchone()[0]
        assert pnl is None, "the close row's P&L must not be fabricated"


class TestStructuralOrphanProof:

    @staticmethod
    def _code_only(body):
        # strip python `#` comments and SQL `--` comments so structural
        # checks target real code, not explanatory prose.
        out = []
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            line = line.split(" -- ")[0]
            out.append(line)
        return "\n".join(out)

    def test_filter_is_occ_based_not_signal_type_allowlist(self):
        """The sweep filter must key on occ_symbol presence, not a
        signal_type allow-list — an allow-list silently falls behind
        when a new option signal_type is added (MULTILEG_OPEN already
        exists), re-opening the orphan class."""
        src = (REPO / "options_lifecycle.py").read_text()
        idx = src.find("def find_expired_open_options")
        body = src[idx:src.find("\ndef ", idx + 1)]
        # isolate the executed SQL's WHERE clause (the query may SELECT
        # signal_type as a returned column; what matters is the filter).
        sql_start = body.find('"""SELECT')
        sql = body[sql_start:body.find('""",', sql_start)]
        where = sql[sql.find("WHERE"):]
        # drop SQL-comment lines so the explanatory '-- ' note doesn't
        # trip the check
        where = "\n".join(l for l in where.splitlines()
                          if "--" not in l)
        assert "occ_symbol IS NOT NULL" in where, (
            "find_expired_open_options must filter on occ_symbol IS NOT "
            "NULL so it covers EVERY option signal_type (orphan-proof)"
        )
        assert "signal_type" not in where, (
            "the sweep WHERE clause must NOT filter on signal_type — an "
            "allow-list skips MULTILEG/MULTILEG_OPEN legs (orphan bug)"
        )

    def test_lifecycle_task_reads_real_summary_keys(self):
        """O9: the scheduler log line must read keys sweep_expired_options
        actually returns (no swallowed KeyError that hides the broadened
        sweep's results)."""
        src = (REPO / "multi_scheduler.py").read_text()
        idx = src.find("def _task_options_lifecycle")
        body = self._code_only(src[idx:idx + 1600])
        log_start = body.find("logging.info(")
        log_call = body[log_start:body.find(")", log_start)]
        assert "assignment_flagged" not in log_call, (
            "the log reads result['assignment_flagged'] which does not "
            "exist — KeyError swallowed, success log suppressed"
        )
        assert "assigned" in log_call


def test_task_options_lifecycle_no_keyerror(db):
    """Integration: _task_options_lifecycle must not raise/swallow a
    KeyError when expired options are found."""
    import multi_scheduler
    import client
    _seed(db, symbol="AAPL", side="buy", qty=1, price=2.5, order_id="o",
          signal_type="MULTILEG", option_strategy="long_call",
          occ_symbol=_occ("AAPL"), expiry=_PAST, strike=12.0,
          decision_price=2.5)
    api = MagicMock()
    api.list_positions.return_value = []
    ctx = MagicMock()
    ctx.db_path = db
    ctx.display_name = "X"
    ctx.segment = "s"
    captured = {}
    with patch.object(client, "get_api", return_value=api), \
         patch("options_lifecycle._underlying_close_at_expiry",
               return_value=9.0), \
         patch.object(multi_scheduler.logging, "exception",
                      side_effect=lambda *a, **k: captured.setdefault("exc", a)):
        multi_scheduler._task_options_lifecycle(ctx)
    assert "exc" not in captured, (
        "the lifecycle task logged an exception (the swallowed KeyError) "
        "instead of the success line"
    )
