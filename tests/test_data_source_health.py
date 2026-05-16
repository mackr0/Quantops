"""Tests for the data-source health probe.

The 2026-05-15 incident: master Alpaca key was revoked silently;
`market_data.get_bars` fell back to yfinance for the entire system
with no log, no alert. The new `data_source_health` module exists
to make sure THAT specific class of regression can never recur
silently again — it probes critical data sources and surfaces
failures loudly.

This test pins the contract:
  - run_all_probes always runs every probe (so a partial failure
    doesn't mask others)
  - critical-failure aggregation correctly distinguishes critical
    from advisory
  - alert_on_critical_failure dedupes per-source-set within a
    process run (so we don't spam every scheduler cycle while
    degraded)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _clean_health_cache():
    """Each test starts with a fresh health-state dict."""
    import data_source_health as dsh
    dsh._last_health.clear()
    yield
    dsh._last_health.clear()


class TestRunAllProbes:
    def test_runs_every_probe_even_when_some_fail(self, monkeypatch):
        """If probe 1 fails, probes 2 and 3 must still run — the
        dashboard needs the FULL picture, not the first failure."""
        called = []

        def _failing_probe():
            called.append("alpaca_bars")
            return False

        def _passing_probe():
            called.append("alpaca_options")
            import data_source_health as dsh
            dsh._record("alpaca_options", True, "ok")
            return True

        def _passing_news():
            called.append("alpaca_news")
            import data_source_health as dsh
            dsh._record("alpaca_news", True, "ok")
            return True

        import data_source_health as dsh
        monkeypatch.setattr(dsh, "_CRITICAL_PROBES", (
            ("alpaca_bars", _failing_probe),
            ("alpaca_options", _passing_probe),
            ("alpaca_news", _passing_news),
        ))
        monkeypatch.setattr(dsh, "_ADVISORY_PROBES", ())

        h = dsh.run_all_probes()
        assert called == ["alpaca_bars", "alpaca_options", "alpaca_news"]
        assert h["all_critical_ok"] is False
        assert "alpaca_bars" in h["critical_failures"]
        assert "alpaca_options" not in h["critical_failures"]

    def test_probe_crash_is_caught_and_recorded(self, monkeypatch):
        """An exception inside a probe must not abort the suite —
        record the crash and move on."""
        def _crashing_probe():
            raise RuntimeError("boom")

        import data_source_health as dsh
        monkeypatch.setattr(dsh, "_CRITICAL_PROBES", (
            ("alpaca_bars", _crashing_probe),
        ))
        monkeypatch.setattr(dsh, "_ADVISORY_PROBES", ())

        h = dsh.run_all_probes()
        assert h["all_critical_ok"] is False
        assert "alpaca_bars" in h["critical_failures"]
        rec = h["per_source"]["alpaca_bars"]
        assert "RuntimeError" in rec["detail"]
        assert "boom" in rec["detail"]

    def test_advisory_failure_does_not_break_critical_ok(self, monkeypatch):
        """advisory_failures should not flip all_critical_ok to False
        — that's the whole point of the critical/advisory split."""
        import data_source_health as dsh

        def _crit_pass():
            dsh._record("alpaca_bars", True, "ok")
            return True

        def _adv_fail():
            return False

        monkeypatch.setattr(dsh, "_CRITICAL_PROBES", (
            ("alpaca_bars", _crit_pass),
        ))
        monkeypatch.setattr(dsh, "_ADVISORY_PROBES", (
            ("earnings_calendar", _adv_fail),
        ))

        h = dsh.run_all_probes()
        assert h["all_critical_ok"] is True
        assert "earnings_calendar" in h["advisory_failures"]


class TestAlertOnCriticalFailure:
    def test_does_nothing_when_all_critical_ok(self):
        from data_source_health import alert_on_critical_failure
        called = {"notify": 0, "log": 0}

        # Just verify no exception when health is healthy.
        h = {
            "all_critical_ok": True,
            "critical_failures": [],
            "advisory_failures": [],
            "per_source": {},
        }
        alert_on_critical_failure(h, profile_id=1, user_id=1)
        # Nothing to assert beyond "didn't raise" — the function
        # silently returns when healthy.

    def test_dedups_within_process_run(self, monkeypatch):
        """Multiple calls with the same failure set should only
        notify ONCE — otherwise we'd email every 10 min while
        the source is down."""
        import data_source_health as dsh
        notify_count = [0]

        def _fake_notify(error_msg, context):
            notify_count[0] += 1

        class _FakeNotif:
            @staticmethod
            def notify_error(*args, **kwargs):
                _fake_notify(kwargs.get("error_msg"), kwargs.get("context"))

        monkeypatch.setitem(
            sys.modules, "notifications", _FakeNotif,
        )

        h = {
            "all_critical_ok": False,
            "critical_failures": ["alpaca_bars"],
            "advisory_failures": [],
            "per_source": {
                "alpaca_bars": {"detail": "401"},
            },
        }
        dsh.alert_on_critical_failure(h, profile_id=0, user_id=1)
        dsh.alert_on_critical_failure(h, profile_id=0, user_id=1)
        dsh.alert_on_critical_failure(h, profile_id=0, user_id=1)
        assert notify_count[0] == 1, (
            f"Expected 1 notify call (dedup); got {notify_count[0]}"
        )

    def test_different_failure_sets_fire_separate_alerts(self, monkeypatch):
        """If a NEW source goes down after the first alert, the
        new failure set should re-fire (dedup is per-set, not
        permanent)."""
        import data_source_health as dsh
        notify_count = [0]

        class _FakeNotif:
            @staticmethod
            def notify_error(*args, **kwargs):
                notify_count[0] += 1

        monkeypatch.setitem(sys.modules, "notifications", _FakeNotif)

        h1 = {
            "all_critical_ok": False,
            "critical_failures": ["alpaca_bars"],
            "advisory_failures": [],
            "per_source": {"alpaca_bars": {"detail": "401"}},
        }
        dsh.alert_on_critical_failure(h1, profile_id=0, user_id=1)
        assert notify_count[0] == 1

        h2 = {
            "all_critical_ok": False,
            "critical_failures": ["alpaca_bars", "alpaca_options"],
            "advisory_failures": [],
            "per_source": {
                "alpaca_bars": {"detail": "401"},
                "alpaca_options": {"detail": "401"},
            },
        }
        dsh.alert_on_critical_failure(h2, profile_id=0, user_id=1)
        assert notify_count[0] == 2, (
            "New failure set must re-fire the alert"
        )
