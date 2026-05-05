"""Broker disconnect detection tests.

When Alpaca is unreachable, account / positions / order calls fail.
Tracking consecutive failures lets the trade pipeline refuse new
entries (instead of failing one at a time) AND auto-recover on the
next successful call.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _reset_state():
    from broker_health import reset
    reset()
    yield
    reset()


def test_starts_healthy():
    from broker_health import is_disconnected, status
    assert is_disconnected() is False
    assert status()["status"] == "healthy"


def test_one_failure_is_degraded():
    from broker_health import record_failure, is_disconnected, status
    record_failure(Exception("503"))
    assert is_disconnected() is False
    assert status()["status"] == "degraded"
    assert status()["consecutive_failures"] == 1


def test_three_consecutive_failures_disconnects():
    from broker_health import record_failure, is_disconnected, status
    for _ in range(3):
        record_failure(Exception("connection refused"))
    assert is_disconnected() is True
    assert status()["status"] == "disconnected"


def test_success_clears_disconnection():
    from broker_health import (
        record_failure, record_success, is_disconnected,
    )
    for _ in range(3):
        record_failure(Exception("503"))
    assert is_disconnected() is True
    record_success()
    assert is_disconnected() is False


def test_intermittent_failures_dont_disconnect():
    from broker_health import (
        record_failure, record_success, is_disconnected,
    )
    record_failure(Exception("blip"))
    record_success()
    record_failure(Exception("blip"))
    record_success()
    record_failure(Exception("blip"))
    assert is_disconnected() is False  # only 1 in a row at the end


def test_call_with_health_tracking_records_success():
    from broker_health import call_with_health_tracking, status

    def good():
        return "ok"

    out = call_with_health_tracking(good)
    assert out == "ok"
    assert status()["consecutive_failures"] == 0


def test_call_with_health_tracking_records_failure_and_reraises():
    from broker_health import call_with_health_tracking, status

    def bad():
        raise RuntimeError("api down")

    with pytest.raises(RuntimeError, match="api down"):
        call_with_health_tracking(bad)
    assert status()["consecutive_failures"] == 1


def test_three_failures_via_wrapper_disconnects():
    from broker_health import call_with_health_tracking, is_disconnected

    def bad():
        raise RuntimeError("api down")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            call_with_health_tracking(bad)
    assert is_disconnected() is True
