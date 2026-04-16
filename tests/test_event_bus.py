"""Tests for Phase 9 — event bus, detectors, and handlers.

Covers emit/dedup, subscribe/dispatch routing, handler isolation, and
detector idempotence.
"""

from __future__ import annotations

import json
import sqlite3
import pytest


# ---------------------------------------------------------------------------
# Event bus — emit / dedup / dispatch
# ---------------------------------------------------------------------------

class TestEventBusEmit:
    def test_emit_inserts_row(self, tmp_profile_db):
        from event_bus import emit
        eid = emit(tmp_profile_db, "price_shock", symbol="AAPL",
                   severity="high", payload={"move_pct": 8.5})
        assert eid is not None
        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        conn.close()
        assert row[1] == "price_shock"    # type
        assert row[2] == "AAPL"           # symbol

    def test_emit_rejects_invalid_severity(self, tmp_profile_db):
        from event_bus import emit
        with pytest.raises(ValueError):
            emit(tmp_profile_db, "price_shock", symbol="AAPL",
                 severity="nuclear", payload={})

    def test_dedup_blocks_duplicate_emit(self, tmp_profile_db):
        from event_bus import emit
        key = "price_shock:AAPL:20260414"
        a = emit(tmp_profile_db, "price_shock", symbol="AAPL",
                 severity="high", payload={"x": 1}, dedup_key=key)
        b = emit(tmp_profile_db, "price_shock", symbol="AAPL",
                 severity="high", payload={"x": 2}, dedup_key=key)
        assert a is not None
        assert b is None   # dedup blocked

    def test_default_dedup_key_blocks_same_day_repeat(self, tmp_profile_db):
        from event_bus import emit
        a = emit(tmp_profile_db, "earnings_imminent", symbol="MSFT",
                 severity="medium", payload={})
        b = emit(tmp_profile_db, "earnings_imminent", symbol="MSFT",
                 severity="medium", payload={})
        assert a is not None
        assert b is None


class TestEventBusSubscribe:
    def test_subscribe_and_handlers_for(self):
        from event_bus import clear_subscriptions, subscribe, handlers_for
        clear_subscriptions()

        def h1(ev, ctx):
            return {"ok": True}

        def h2(ev, ctx):
            return {"also": True}

        subscribe(h1, ("price_shock",))
        subscribe(h2, ("price_shock", "earnings_imminent"))

        assert len(handlers_for("price_shock")) == 2
        assert len(handlers_for("earnings_imminent")) == 1
        assert handlers_for("unknown_type") == []

    def test_clear_subscriptions_resets(self):
        from event_bus import clear_subscriptions, subscribe, handlers_for

        subscribe(lambda ev, ctx: {}, ("test_event",))
        assert len(handlers_for("test_event")) == 1
        clear_subscriptions()
        assert handlers_for("test_event") == []


class TestEventBusDispatch:
    def test_dispatch_routes_events_to_handlers(self, tmp_profile_db, sample_ctx):
        from event_bus import clear_subscriptions, dispatch_pending, emit, subscribe

        clear_subscriptions()
        calls = []

        def handler(ev, ctx):
            calls.append(ev["type"])
            return {"handled": True}

        subscribe(handler, ("test_a", "test_b"))
        emit(tmp_profile_db, "test_a", symbol="X", payload={},
             dedup_key="test_a:X:1")
        emit(tmp_profile_db, "test_b", symbol="Y", payload={},
             dedup_key="test_b:Y:1")
        emit(tmp_profile_db, "other", symbol="Z", payload={},
             dedup_key="other:Z:1")

        summary = dispatch_pending(tmp_profile_db, sample_ctx)
        assert summary["dispatched"] == 3
        assert set(calls) == {"test_a", "test_b"}   # 'other' had no handler

    def test_dispatch_marks_events_handled(self, tmp_profile_db, sample_ctx):
        from event_bus import clear_subscriptions, dispatch_pending, emit

        clear_subscriptions()
        emit(tmp_profile_db, "unhandled_type", symbol="A",
             payload={}, dedup_key="u:A:1")
        dispatch_pending(tmp_profile_db, sample_ctx)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT handled_at FROM events WHERE symbol='A'"
        ).fetchone()
        conn.close()
        assert row[0] is not None

    def test_handler_error_does_not_abort_others(self, tmp_profile_db, sample_ctx):
        from event_bus import clear_subscriptions, dispatch_pending, emit, subscribe

        clear_subscriptions()
        got_called = {"good": False}

        def bad_handler(ev, ctx):
            raise RuntimeError("boom")

        def good_handler(ev, ctx):
            got_called["good"] = True
            return {"ok": True}

        subscribe(bad_handler, ("event_a",))
        subscribe(good_handler, ("event_a",))
        emit(tmp_profile_db, "event_a", symbol="X",
             payload={}, dedup_key="event_a:X:1")

        summary = dispatch_pending(tmp_profile_db, sample_ctx)
        assert got_called["good"] is True
        assert summary["handler_errors"] == 1
        assert summary["dispatched"] == 1

    def test_dispatch_limit_respected(self, tmp_profile_db, sample_ctx):
        from event_bus import clear_subscriptions, dispatch_pending, emit, subscribe

        clear_subscriptions()
        for i in range(15):
            emit(tmp_profile_db, "mass_event", symbol=f"T{i}",
                 payload={}, dedup_key=f"mass:{i}")

        # First dispatch, limit=5
        r1 = dispatch_pending(tmp_profile_db, sample_ctx, limit=5)
        assert r1["dispatched"] == 5

        # Rest remain pending
        conn = sqlite3.connect(tmp_profile_db)
        pending = conn.execute(
            "SELECT COUNT(*) FROM events WHERE handled_at IS NULL"
        ).fetchone()[0]
        conn.close()
        assert pending == 10

    def test_handler_results_persisted(self, tmp_profile_db, sample_ctx):
        from event_bus import clear_subscriptions, dispatch_pending, emit, subscribe

        clear_subscriptions()

        def hh(ev, ctx):
            return {"verdict": "BUY", "confidence": 80}

        subscribe(hh, ("my_type",))
        emit(tmp_profile_db, "my_type", symbol="X",
             payload={}, dedup_key="my_type:X:1")
        dispatch_pending(tmp_profile_db, sample_ctx)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT handler_results_json FROM events WHERE symbol='X'"
        ).fetchone()
        conn.close()
        results = json.loads(row[0])
        assert len(results) == 1
        assert results[0]["handler"] == "hh"
        assert results[0]["result"]["verdict"] == "BUY"


class TestRecentEvents:
    def test_recent_events_parses_payload(self, tmp_profile_db):
        from event_bus import emit, recent_events
        emit(tmp_profile_db, "test_event", symbol="ABC",
             payload={"move_pct": 7.5}, dedup_key="test:ABC:1")
        events = recent_events(tmp_profile_db, hours=24)
        assert len(events) == 1
        assert events[0]["payload"]["move_pct"] == 7.5
        assert events[0]["handler_results"] == []


# ---------------------------------------------------------------------------
# Default-handler integration
# ---------------------------------------------------------------------------

class TestDefaultHandlers:
    def test_register_default_handlers_wires_log_activity(self):
        from event_bus import handlers_for
        from event_handlers import register_default_handlers
        register_default_handlers()
        # log_activity should be registered for every event type
        from event_detectors import ALL_EVENT_TYPES
        for t in ALL_EVENT_TYPES:
            assert any(
                h.__name__ == "handler_log_activity"
                for h in handlers_for(t)
            )

    def test_ensemble_handler_skips_non_reactive_types(self, sample_ctx):
        from event_handlers import handler_fire_ensemble
        event = {"type": "earnings_imminent", "symbol": "AAPL", "payload": {}}
        result = handler_fire_ensemble(event, sample_ctx)
        assert "skipped" in result

    def test_ensemble_handler_calls_for_sec_filing(self, sample_ctx, monkeypatch):
        from event_handlers import handler_fire_ensemble

        def fake_ensemble(candidates, ctx, **kw):
            assert candidates[0]["symbol"] == "AAPL"
            return {
                "per_symbol": {"AAPL": {"verdict": "SELL", "confidence": 75,
                                        "vetoed": False,
                                        "specialists": []}},
                "cost_calls": 4,
            }

        monkeypatch.setattr("ensemble.run_ensemble", fake_ensemble)
        event = {"type": "sec_filing_detected", "symbol": "AAPL",
                 "payload": {"form_type": "8-K"}}
        result = handler_fire_ensemble(event, sample_ctx)
        assert result["ensemble_verdict"] == "SELL"
        assert result["cost_calls"] == 4


# ---------------------------------------------------------------------------
# Detector idempotence
# ---------------------------------------------------------------------------

class TestEarningsImminentDetector:
    """Regression guard: event_detectors.detect_earnings_imminent must
    import a function that actually exists in earnings_calendar."""

    def test_detector_import_resolves(self):
        # The bug was importing a non-existent `get_next_earnings`.
        # Just importing the module tests the top-level imports.
        import event_detectors
        assert callable(event_detectors.detect_earnings_imminent)

    def test_detector_runs_without_positions(self, tmp_profile_db, monkeypatch):
        from types import SimpleNamespace
        monkeypatch.setattr("client.get_positions", lambda ctx=None: [])
        from event_detectors import detect_earnings_imminent
        ctx = SimpleNamespace(db_path=tmp_profile_db)
        # Should return 0 events without crashing on the import
        assert detect_earnings_imminent(ctx) == 0


class TestDetectorIdempotence:
    def test_big_winner_detector_dedupes_on_prediction_id(
        self, tmp_profile_db, sample_ctx
    ):
        """Running the detector twice must not double-emit events."""
        from event_detectors import detect_big_resolved_predictions
        import sqlite3 as _sq

        conn = _sq.connect(tmp_profile_db)
        conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence,
                price_at_prediction, status, actual_return_pct,
                strategy_type, resolved_at)
               VALUES (datetime('now'), 'AAPL', 'BUY', 70,
                       100.0, 'resolved', 20.0,
                       'market_engine', datetime('now'))""",
        )
        conn.commit()
        conn.close()

        sample_ctx.db_path = tmp_profile_db
        n1 = detect_big_resolved_predictions(sample_ctx)
        n2 = detect_big_resolved_predictions(sample_ctx)
        assert n1 == 1
        assert n2 == 0  # dedup key held
