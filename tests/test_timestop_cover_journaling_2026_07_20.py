"""2026-07-20 — pins for the un-journaled time-stop cover incident.

Twelve equity shorts crossed short_max_hold_days on the same Monday
open. The time-stop trigger dict carried no "price" key, and
_process_exit_trigger's log_trade AND its A0 minimal-journal fallback
both subscripted trigger_signal["price"] — so the KeyError escaped the
except handler and every cover went live at the broker with no journal
row, no halt, and no alert (journal shorts vs broker flat → kill
switch). Separately, activities capture had been dead for weeks:
Alpaca rejects the comma-joined activity_types string as one unknown
type.

Pins:
  1. A trigger WITHOUT "price" (the legacy time-stop shape) still
     journals the live cover — price NULL, real order_id — and never
     lets an exception escape.
  2. Even if the rich log_trade fails, the fallback path cannot be
     defeated by the same missing key (structural: the call site
     reads .get(), and the invocation is wrapped).
  3. The time-stop trigger dict now carries "price" (structural).
  4. activities_capture fetches PER TYPE and survives an API that
     rejects comma-joined type strings.
  5. options_lifecycle._fetch_expiry_activities does the same, and
     returns None (defer) when any type's fetch fails.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def tmp_db():
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _short_ctx(db):
    ctx = MagicMock()
    ctx.db_path = db
    ctx.profile_id = 999
    return ctx


def _seed_short(db, symbol="GOOG", qty=10, price=352.0):
    from journal import log_trade
    log_trade(symbol=symbol, side="short", qty=qty, price=price,
              order_id="ent-1", signal_type="STRONG_SELL",
              status="open", fill_price=price, db_path=db)


class TestCoverJournaledWithoutTriggerPrice:
    def _run(self, db, trigger):
        from trader import _process_exit_trigger
        api = MagicMock()
        order = MagicMock()
        order.id = "cover-oid-1"
        api.submit_order.return_value = order
        results = []
        with patch("order_guard.check_can_submit", return_value=True), \
             patch("order_guard.allowable_cover_qty",
                   return_value=(trigger["qty"], "ok")), \
             patch("trader._entry_order_filled_at_broker",
                   return_value=True), \
             patch("bracket_orders.cancel_for_symbol",
                   return_value=False):
            _process_exit_trigger(trigger, api, _short_ctx(db), db,
                                  [], {}, results)
        return api, results

    def test_legacy_priceless_trigger_still_journals(self, tmp_db):
        """The incident shape: is_short time_stop trigger with NO
        'price' key. The cover must submit AND land in the journal
        with the real order_id (price may be NULL)."""
        _seed_short(tmp_db)
        trigger = {
            "symbol": "GOOG", "qty": 10, "is_short": True,
            "trigger": "time_stop",
            "reason": "Short held > 10 days",
            # deliberately NO "price"
        }
        api, results = self._run(tmp_db, trigger)
        assert api.submit_order.called
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT side, qty, order_id FROM trades "
            "WHERE order_id = 'cover-oid-1'").fetchone()
        assert row is not None, (
            "live cover order was NOT journaled — the 2026-07-20 "
            "phantom-drift class")
        assert row[0] == "cover"
        assert row[1] == 10
        assert results and results[0]["action"] == "COVER"

    def test_rich_logtrade_failure_still_minimal_journals(self, tmp_db):
        """Force log_trade to raise: the A0 minimal fallback must
        still persist the order_id (and never raise out)."""
        _seed_short(tmp_db)
        trigger = {"symbol": "GOOG", "qty": 10, "is_short": True,
                   "trigger": "time_stop", "reason": "r"}
        from trader import _process_exit_trigger
        api = MagicMock()
        order = MagicMock()
        order.id = "cover-oid-2"
        api.submit_order.return_value = order
        with patch("order_guard.check_can_submit", return_value=True), \
             patch("order_guard.allowable_cover_qty",
                   return_value=(10, "ok")), \
             patch("trader._entry_order_filled_at_broker",
                   return_value=True), \
             patch("bracket_orders.cancel_for_symbol",
                   return_value=False), \
             patch("trader.log_trade",
                   side_effect=RuntimeError("rich write failed")):
            _process_exit_trigger(trigger, api, _short_ctx(tmp_db),
                                  tmp_db, [], {}, [])
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT side, order_id, timestamp FROM trades "
            "WHERE order_id = 'cover-oid-2'").fetchone()
        assert row is not None, "minimal fallback did not persist"
        assert row[2] is not None, (
            "minimal fallback must stamp a timestamp (NULL breaks "
            "FIFO ordering)")


class TestTimeStopTriggerShape:
    def test_time_stop_trigger_carries_price(self):
        """Structural: the time-stop trigger construction includes a
        'price' key, and no trigger-price read in
        _process_exit_trigger uses a bare subscript."""
        src = open(os.path.join(REPO, "trader.py")).read()
        block = src[src.index("P1.5 of LONG_SHORT_PLAN.md"):]
        block = block[:block.index("except Exception")]
        assert '"price"' in block, (
            "time-stop trigger dict must carry 'price'")
        # Scan CODE only (comments may legitimately describe the old
        # broken pattern).
        code = "\n".join(
            re.sub(r"#.*$", "", line) for line in src.splitlines())
        assert 'trigger_signal["price"]' not in code, (
            "bare trigger_signal['price'] subscripts defeated the A0 "
            "fallback — use .get()")


class TestActivitiesFetchPerType:
    """Alpaca rejects 'DIV,OPEXP,OPASN,OPXRC' as one unknown type —
    prod log: \"invalid activity type\". Both fetchers must call per
    type."""

    class _RejectingApi:
        def __init__(self):
            self.calls = []

        def get_activities(self, activity_types=None, after=None):
            self.calls.append(activity_types)
            if "," in (activity_types or ""):
                raise ValueError(
                    f"invalid activity type: {activity_types}")
            a = MagicMock()
            a.id = f"act-{activity_types}"
            a.activity_type = activity_types
            a.symbol = ""
            a.qty = 1
            return [a]

    def test_activities_capture_fetches_per_type(self, tmp_db):
        from activities_capture import (capture_activities_for_profile,
                                        _HANDLED_TYPES)
        ctx = _short_ctx(tmp_db)
        api = self._RejectingApi()
        ctx.get_alpaca_api = lambda: api
        capture_activities_for_profile(ctx)
        assert set(api.calls) == set(_HANDLED_TYPES), (
            f"expected one call per type, got {api.calls}")
        assert not any("," in c for c in api.calls)

    def test_lifecycle_fetch_per_type_and_defers_on_partial(self):
        from options_lifecycle import (_fetch_expiry_activities,
                                       _EXPIRY_ACTIVITY_TYPES)
        api = self._RejectingApi()
        acts = _fetch_expiry_activities(api, since_iso="2026-07-17")
        assert acts is not None
        assert len(acts) == len(_EXPIRY_ACTIVITY_TYPES)
        assert not any("," in c for c in api.calls)

        class _OneTypeDown(self._RejectingApi):
            def get_activities(self, activity_types=None, after=None):
                if activity_types == "OPASN":
                    raise RuntimeError("500")
                return super().get_activities(activity_types, after)

        acts = _fetch_expiry_activities(_OneTypeDown(),
                                        since_iso="2026-07-17")
        assert acts is None, (
            "an incomplete activities answer must read as unavailable "
            "(defer) — partial data could close an assigned contract "
            "as worthless")
