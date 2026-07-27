"""Option-orphan write-offs require broker-verified closing evidence
(2026-07-27, the p211 XOM +$335 incident).

reconcile_option_orphans treated "OCC absent from ONE positions
snapshot + entry order filled" as proof of an external close and
stamped `auto_closed_external` — handing the row's cash
accountability to an activities pass that had nothing to book,
because no close had actually happened (the positions feed's known
visibility lag). The still-live put was later sold by our OWN
dte_exit and $335 of proceeds landed basis-less → decomposition gap
→ kill switch.

The invariant restored here: journal state changes require broker
EVENTS (orders / ID'd activities), never position-snapshot absence.
No corroborating OPASN/OPEXC/OPXRC/FILL activity → the pass REFUSES
and the leg stays open (feed lag self-heals; a real external close
produces its activity within a cycle and closes then).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from journal import init_db  # noqa: E402
from reconcile_journal_to_broker import reconcile_option_orphans  # noqa: E402

OCC = "XOM260731P00155000"


def _db(tmp_path):
    path = str(tmp_path / "p.db")
    init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    expiry = (date.today() + timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO trades (symbol, occ_symbol, side, qty, price, "
        "status, order_id, expiry) VALUES ('XOM', ?, 'buy', 1, 2.30, "
        "'open', 'ord-entry-1', ?)", (OCC, expiry))
    conn.commit()
    return conn


def _api(entry_status="filled", entry_filled=1.0, activities=(),
          activities_raise=False):
    api = MagicMock()
    order = MagicMock()
    order.status = entry_status
    order.filled_qty = entry_filled
    api.get_order.return_value = order
    if activities_raise:
        api.get_activities.side_effect = RuntimeError("api down")
    else:
        def _get(activity_types=None, after=None):
            return [a for a in activities
                    if a.activity_type == activity_types]
        api.get_activities.side_effect = _get
    return api


def _activity(a_type, symbol, act_id="act-123"):
    a = MagicMock()
    a.activity_type = a_type
    a.symbol = symbol
    a.id = act_id
    return a


def _leg_status(conn):
    return conn.execute(
        "SELECT status, reason FROM trades WHERE occ_symbol = ?",
        (OCC,)).fetchone()


class TestEvidenceRequired:
    def test_snapshot_absence_alone_refuses(self, tmp_path):
        """The exact incident shape: account-flat snapshot, filled
        entry, NO closing activity — must NOT be written off."""
        conn = _db(tmp_path)
        out = reconcile_option_orphans(
            _api(), conn, positions=[], today=date.today(),
            apply_changes=True)
        row = _leg_status(conn)
        assert row["status"] == "open", (
            "a live position was written off on positions-snapshot "
            "absence again — the +$335 basis-less-proceeds class."
        )
        assert out == []

    def test_assignment_activity_permits_close(self, tmp_path):
        conn = _db(tmp_path)
        api = _api(activities=[_activity("OPASN", OCC, "act-9")])
        out = reconcile_option_orphans(
            api, conn, positions=[], today=date.today(),
            apply_changes=True)
        row = _leg_status(conn)
        assert row["status"] == "auto_closed_external"
        assert "OPASN" in row["reason"] and "act-9" in row["reason"], (
            "the closing activity's identity must be recorded on the "
            "row — evidence is auditable or it isn't evidence."
        )
        assert len(out) == 1

    def test_other_symbols_activity_is_not_evidence(self, tmp_path):
        conn = _db(tmp_path)
        api = _api(activities=[_activity("OPASN", "CVX260731C00160000")])
        reconcile_option_orphans(
            api, conn, positions=[], today=date.today(),
            apply_changes=True)
        assert _leg_status(conn)["status"] == "open"

    def test_activities_api_failure_refuses(self, tmp_path):
        """Unverifiable is not proof — API down means leave it open."""
        conn = _db(tmp_path)
        api = _api(activities_raise=True)
        reconcile_option_orphans(
            api, conn, positions=[], today=date.today(),
            apply_changes=True)
        assert _leg_status(conn)["status"] == "open"

    def test_never_filled_entry_still_cancels_without_activity(
            self, tmp_path):
        """The canceled branch rests on ORDER evidence (broker says
        the entry never filled) — no activity needed."""
        conn = _db(tmp_path)
        api = _api(entry_status="canceled", entry_filled=0.0)
        reconcile_option_orphans(
            api, conn, positions=[], today=date.today(),
            apply_changes=True)
        assert _leg_status(conn)["status"] == "canceled"

    def test_broker_still_holding_untouched(self, tmp_path):
        conn = _db(tmp_path)
        pos = MagicMock()
        pos.symbol = OCC
        pos.qty = 1
        reconcile_option_orphans(
            _api(), conn, positions=[pos], today=date.today(),
            apply_changes=True)
        assert _leg_status(conn)["status"] == "open"
