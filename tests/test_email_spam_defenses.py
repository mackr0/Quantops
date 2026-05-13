"""Defenses against the 2026-05-13 email-spam incident.

Background: a 0-byte `strategy_validations.db` was being treated as
critical-DB corruption by `multi_scheduler` startup integrity check.
Scheduler exited (exit 1), systemd restarted every 30s, fired
`notify_error` on each restart → 145 ERROR emails in ~2 hours
before the operator caught it.

Three defenses pinned by these tests:

  1. db_integrity classifies DBs as critical vs non-critical;
     strategy_validations.db is non-critical.
  2. notifications.notify_error is per-subject debounced
     (1-hour window) so even if any error recurs, it can't spam.
  3. multi_scheduler exits ONLY on critical corruption — non-critical
     corruption logs + emails (debounced) + continues.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Fix 1 — DB criticality classification
# ---------------------------------------------------------------------------

class TestDbCriticalityClassification:
    def test_strategy_validations_is_non_critical(self):
        from db_integrity import is_critical
        assert is_critical("strategy_validations.db") is False

    def test_master_quantopsai_is_critical(self):
        from db_integrity import is_critical
        assert is_critical("quantopsai.db") is True

    def test_per_profile_db_is_critical(self):
        from db_integrity import is_critical
        assert is_critical("quantopsai_profile_3.db") is True
        assert is_critical("quantopsai_profile_10.db") is True

    def test_alt_data_dbs_are_critical(self):
        from db_integrity import is_critical
        # AI prompt reads from these — corruption mid-trade-day means
        # silently degraded inputs to the AI. Halt is correct.
        assert is_critical("altdata/biotechevents/data/biotechevents.db") is True

    def test_critical_corrupt_excludes_strategy_validations(self):
        from db_integrity import critical_corrupt, non_critical_corrupt
        results = {
            "quantopsai.db":              {"status": "ok", "detail": "ok"},
            "strategy_validations.db":    {"status": "corrupt",
                                            "detail": "0 bytes"},
            "quantopsai_profile_3.db":    {"status": "corrupt",
                                            "detail": "page 4 out of order"},
        }
        critical = critical_corrupt(results)
        non_critical = non_critical_corrupt(results)
        assert "strategy_validations.db" not in critical
        assert "quantopsai_profile_3.db" in critical
        assert "strategy_validations.db" in non_critical
        assert "quantopsai_profile_3.db" not in non_critical


# ---------------------------------------------------------------------------
# Fix 2 — notify_error per-subject debounce
# ---------------------------------------------------------------------------

class TestNotifyErrorDebounce:
    def setup_method(self):
        # Clear the debounce state between tests
        import notifications
        notifications._notify_error_last_sent.clear()

    def test_first_call_sends(self):
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True) as send:
            result = notify_error("test error", context="unit")
            assert result is True
            assert send.call_count == 1

    def test_second_call_within_window_debounces(self):
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True) as send:
            notify_error("first", context="ctx-x")
            result = notify_error("second", context="ctx-x")
            assert result is False
            assert send.call_count == 1  # only the first

    def test_different_subjects_do_not_debounce_each_other(self):
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True) as send:
            notify_error("first", context="ctx-A")
            notify_error("second", context="ctx-B")
            assert send.call_count == 2

    def test_after_debounce_window_resends(self):
        import notifications
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True) as send:
            notify_error("first", context="ctx-X")
            # Manually age the entry by > 1h
            notifications._notify_error_last_sent["QuantOpsAI ERROR: ctx-X"] = (
                datetime.utcnow() - timedelta(hours=1, minutes=5)
            )
            result = notify_error("after window", context="ctx-X")
            assert result is True
            assert send.call_count == 2

    def test_spam_loop_can_only_fire_once(self):
        """Simulate the May 13 incident: 50 rapid-fire calls with the
        same subject. Only the first should send."""
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True) as send:
            for _ in range(50):
                notify_error(
                    "DB integrity check failed for: strategy_validations.db",
                    context="DB corruption detected",
                )
            assert send.call_count == 1


# ---------------------------------------------------------------------------
# Fix 3 — scheduler doesn't exit() on non-critical corruption
# ---------------------------------------------------------------------------

class TestSchedulerNonCriticalContinues:
    """Pin the multi_scheduler startup integrity-check behavior. We
    can't easily run the whole scheduler entrypoint in a test, so
    these tests verify the structural shape of the integrity-check
    block: critical_corrupt + non_critical_corrupt are both
    inspected, and only critical triggers sys.exit()."""

    def test_integrity_check_block_uses_critical_classifier(self):
        import inspect
        import multi_scheduler
        # The integrity-check block lives in main(). Find it.
        src = inspect.getsource(multi_scheduler)
        # Both classifier helpers must be imported
        assert "critical_corrupt" in src
        assert "non_critical_corrupt" in src
        # sys.exit must be guarded by critical-only
        assert "if critical:" in src

    def test_integrity_check_block_logs_non_critical_separately(self):
        import inspect
        import multi_scheduler
        src = inspect.getsource(multi_scheduler)
        # Non-critical bucket gets its own log message + email
        assert "non-critical, continuing" in src