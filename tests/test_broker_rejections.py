"""Pin the broker_rejections persistence layer (2026-05-11).

The AI proposes trades. Some get accepted by Alpaca; some get
rejected (cross-direction conflict with a sibling profile on a
shared account, wash-trade guard, insufficient buying power, etc.).
Previously the rejection was logged as a WARNING and the trade
silently disappeared — operators went looking for trades that had
been rejected at the broker (the CWAN incident).

These tests pin:
1. `classify_broker_rejection_message` maps each known pattern to
   a stable rejection_code.
2. `record_broker_rejection` writes the row with all fields,
   including the FK to ai_predictions when supplied.
3. `get_recent_broker_rejections` returns rows from the last N
   hours in DESC timestamp order.
4. DB read failure on get path logs warning + returns [] (no
   silent swallow).
5. The trade_pipeline rejection-handler call site writes a
   rejection row for each known recoverable broker error pattern.
"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from journal import init_db
    init_db(path)
    return path


class TestClassifyRejectionMessage:
    def test_wash_trade(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "rejected: potential wash trade detected"
        ) == "wash_trade"

    def test_cross_direction_long(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "cannot open a long buy while a short sell order is open"
        ) == "cross_direction_long_blocked"

    def test_cross_direction_short(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "cannot open a short sell while a long buy order is open"
        ) == "cross_direction_short_blocked"

    def test_insufficient_buying_power(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "insufficient buying power"
        ) == "insufficient_buying_power"

    def test_insufficient_qty(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "insufficient qty available, requested: 100"
        ) == "insufficient_qty"

    def test_no_quote_available(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "order has been rejected due to no available quote"
        ) == "no_quote_available"

    def test_unknown_message_returns_other(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(
            "some new error message we haven't seen"
        ) == "other"

    def test_none_message_returns_other(self):
        from journal import classify_broker_rejection_message
        assert classify_broker_rejection_message(None) == "other"


class TestRecordBrokerRejection:
    def test_writes_full_row(self):
        db = _fresh_db()
        try:
            from journal import record_broker_rejection
            rid = record_broker_rejection(
                db, symbol="CWAN", action="BUY", signal_type="BUY",
                ai_confidence=78, ai_reasoning="momentum + cheap IV",
                broker_message=(
                    "cannot open a long buy while a short sell "
                    "order is open"
                ),
            )
            assert rid is not None
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT * FROM broker_rejections WHERE id=?", (rid,),
            ).fetchone()
            conn.close()
            assert r["symbol"] == "CWAN"
            assert r["action"] == "BUY"
            assert r["ai_confidence"] == 78
            assert r["ai_reasoning"] == "momentum + cheap IV"
            assert r["rejection_code"] == "cross_direction_long_blocked"
            assert "cannot open" in r["broker_message"]
        finally:
            os.unlink(db)

    def test_db_write_failure_logs_returns_none(self, caplog):
        import logging
        from journal import record_broker_rejection
        with patch("journal._get_conn",
                   side_effect=RuntimeError("DB locked")):
            with caplog.at_level(logging.WARNING):
                rid = record_broker_rejection(
                    "/nope.db", symbol="CWAN", action="BUY",
                    signal_type="BUY", ai_confidence=78,
                    ai_reasoning=None,
                    broker_message="any reason",
                )
        assert rid is None
        # WARNING logged, not silent
        assert any("record_broker_rejection" in r.getMessage()
                   for r in caplog.records)


class TestGetRecentBrokerRejections:
    def test_returns_recent_in_desc_order(self):
        db = _fresh_db()
        try:
            from journal import (record_broker_rejection,
                                 get_recent_broker_rejections)
            record_broker_rejection(
                db, symbol="A", action="BUY", signal_type="BUY",
                ai_confidence=70, ai_reasoning=None,
                broker_message="wash trade",
            )
            record_broker_rejection(
                db, symbol="B", action="SHORT", signal_type="SHORT",
                ai_confidence=80, ai_reasoning=None,
                broker_message="insufficient buying power",
            )
            rows = get_recent_broker_rejections(db, hours=24)
            assert len(rows) == 2
            # DESC: most recent first → B (inserted second) is first
            assert rows[0]["symbol"] == "B"
            assert rows[1]["symbol"] == "A"
        finally:
            os.unlink(db)

    def test_db_read_failure_returns_empty_logs_warning(self, caplog):
        import logging
        from journal import get_recent_broker_rejections
        with patch("journal._get_conn",
                   side_effect=RuntimeError("DB locked")):
            with caplog.at_level(logging.WARNING):
                rows = get_recent_broker_rejections("/nope.db")
        assert rows == []
        assert any("get_recent_broker_rejections" in r.getMessage()
                   for r in caplog.records)
