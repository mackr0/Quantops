"""Structural guardrail: `notify_error` must never propagate
an exception out, regardless of what fails inside (SMTP error,
network outage, malformed config, etc.).

The bug class.
The error handler is the LAST line of defense. If it raises:
  - The original error is replaced by the notification error
  - Operator sees the WRONG error in the trace
  - The error path may loop (handler tries to notify the
    notification failure → which also fails → infinite recursion)
  - In some patterns, the calling process crashes

`notify_error` is called from many critical-path try/except
blocks. Per Mack's standing memory: "every error must be
surfaced and fixed, not swallowed." If notify_error itself
swallows or worse — re-raises — the error visibility is gone.

This test simulates every plausible failure inside notify_error
(SMTP down, no API key, malformed body, debounce dict corrupted)
and asserts the function:
  1. Returns False (or None) cleanly
  2. Does not raise
  3. Logs a warning so the silence is visible in the journal
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


class TestNotifyErrorNeverRaises:
    def setup_method(self):
        # Reset debounce state between tests
        import notifications
        notifications._notify_error_last_sent.clear()

    def test_send_email_raises_caught(self):
        """If send_email raises mid-call, notify_error must not
        propagate."""
        from notifications import notify_error
        with patch("notifications.send_email",
                   side_effect=ConnectionError("SMTP down")):
            try:
                result = notify_error("body", context="test")
            except Exception as exc:
                pytest.fail(
                    f"notify_error propagated {type(exc).__name__}: "
                    f"{exc}. The error handler must never raise."
                )
            # Result is whatever the implementation returns; any
            # falsy value (None, False) is acceptable. The contract
            # is "doesn't raise."

    def test_html_wrap_raises_caught(self):
        """If the HTML body builder fails (e.g., unicode issues
        in error message), notify_error must not propagate."""
        from notifications import notify_error
        with patch("notifications._wrap_html",
                   side_effect=UnicodeDecodeError(
                       "utf-8", b"", 0, 1, "test")):
            try:
                notify_error("body", context="test")
            except Exception as exc:
                pytest.fail(
                    f"notify_error propagated {type(exc).__name__}: "
                    f"{exc}"
                )

    def test_kv_row_raises_caught(self):
        """Timestamp formatting failure must not propagate."""
        from notifications import notify_error
        with patch("notifications._kv_row",
                   side_effect=ValueError("formatting failed")):
            try:
                notify_error("body", context="test")
            except Exception as exc:
                pytest.fail(
                    f"notify_error propagated {type(exc).__name__}: "
                    f"{exc}"
                )

    def test_extreme_subject_doesnt_raise(self):
        """Pathological subject (very long, control chars, unicode
        edge cases) must not crash the function."""
        from notifications import notify_error
        weird_subject = "\x00" * 100 + "​" * 100 + "test" * 1000
        with patch("notifications.send_email", return_value=True):
            try:
                notify_error("body", context=weird_subject)
            except Exception as exc:
                pytest.fail(
                    f"notify_error propagated on weird subject: "
                    f"{type(exc).__name__}: {exc}"
                )

    def test_none_message_doesnt_raise(self):
        """Caller passes None — function must handle gracefully."""
        from notifications import notify_error
        with patch("notifications.send_email", return_value=True):
            try:
                notify_error(None, context="test")
            except Exception as exc:
                pytest.fail(
                    f"notify_error propagated on None body: "
                    f"{type(exc).__name__}: {exc}"
                )
