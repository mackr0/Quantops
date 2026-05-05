"""Email subject sanitizer — Resend (and most email APIs) reject
subjects with newline / control characters.

Watchdog incident 2026-05-04: stalled-task alerts on the Mid Cap
profile sat in the queue for 66 hours because the watchdog passed a
multi-line `context` block to `notify_error`, which built the subject
as `f"QuantOpsAI ERROR: {ctx_label}"` — that subject had embedded \n
and Resend returned HTTP 422. Mack never got the alert.

This test pins the new defense-in-depth in `notifications.send_email`:
every subject is sanitized before being sent, regardless of what the
caller passed in.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_strips_newlines():
    from notifications import _sanitize_subject
    out = _sanitize_subject("hello\nworld")
    assert "\n" not in out
    assert out == "hello world"


def test_strips_carriage_returns():
    from notifications import _sanitize_subject
    out = _sanitize_subject("hello\r\nworld")
    assert "\r" not in out
    assert "\n" not in out


def test_strips_tabs_and_collapses_whitespace():
    from notifications import _sanitize_subject
    out = _sanitize_subject("a\tb   c\n\n\nd")
    assert out == "a b c d"


def test_truncates_long_subjects():
    from notifications import _sanitize_subject
    out = _sanitize_subject("x" * 500, max_len=50)
    assert len(out) <= 50
    # Truncation marker should appear
    assert out.endswith("…")


def test_empty_falls_back_to_brand_name():
    from notifications import _sanitize_subject
    assert _sanitize_subject("") == "QuantOpsAI"
    assert _sanitize_subject(None) == "QuantOpsAI"


def test_realworld_watchdog_context_block():
    """The exact pattern that broke 2026-05-04: a multi-line context
    paragraph that the watchdog passed as `context` to notify_error."""
    from notifications import _sanitize_subject
    block = (
        "QuantOpsAI ERROR: Profile: Mid Cap\n"
        "Task started at: 2026-05-01 19:20:42\n"
        "Elapsed: 3974 minutes without completion."
    )
    out = _sanitize_subject(block)
    assert "\n" not in out
    # Should still convey the gist as a single line
    assert "Mid Cap" in out
    assert "Elapsed" in out


def test_normal_subject_unchanged():
    from notifications import _sanitize_subject
    s = "QuantOpsAI: Mid Cap stalled: Resolve AI Predictions"
    assert _sanitize_subject(s) == s


def test_subject_preserves_unicode():
    from notifications import _sanitize_subject
    s = "QuantOpsAI Daily Summary — 2026-05-04"
    assert _sanitize_subject(s) == s


# ---------------------------------------------------------------------------
# Email dedup — stops tight-loop spam (2026-05-04 incident: 599 identical
# DB-corruption error emails sent over 24h before Resend hit daily quota)
# ---------------------------------------------------------------------------

def test_dedup_first_call_passes():
    from notifications import _is_duplicate_within_window, _EMAIL_DEDUP
    _EMAIL_DEDUP.clear()
    assert _is_duplicate_within_window("test subject") is False


def test_dedup_second_call_blocks():
    from notifications import _is_duplicate_within_window, _EMAIL_DEDUP
    _EMAIL_DEDUP.clear()
    _is_duplicate_within_window("test subject")
    assert _is_duplicate_within_window("test subject") is True


def test_dedup_different_subjects_independent():
    from notifications import _is_duplicate_within_window, _EMAIL_DEDUP
    _EMAIL_DEDUP.clear()
    assert _is_duplicate_within_window("subject A") is False
    assert _is_duplicate_within_window("subject B") is False
    # Both A and B are now seen — same subjects should dedup
    assert _is_duplicate_within_window("subject A") is True
    assert _is_duplicate_within_window("subject B") is True


def test_dedup_unblocks_after_window():
    """Simulate window expiry by manipulating the cache timestamp."""
    import notifications
    notifications._EMAIL_DEDUP.clear()
    notifications._is_duplicate_within_window("expiring subject")
    # Force the timestamp to be 2h ago (window is 1h)
    import time as _time
    notifications._EMAIL_DEDUP["expiring subject"] = _time.time() - 7200
    assert notifications._is_duplicate_within_window("expiring subject") is False


def test_send_email_deduped_returns_true_silently():
    """A deduped call returns True (caller treats it as success)
    and does NOT make an HTTP request."""
    from unittest.mock import patch
    import notifications
    notifications._EMAIL_DEDUP.clear()
    with patch.object(notifications, "config") as cfg, \
         patch("notifications.urllib.request.urlopen") as mock_urlopen:
        cfg.RESEND_API_KEY = "test-key"
        cfg.NOTIFICATION_EMAIL = "test@test.com"
        # Mock the response context manager
        mock_response = mock_urlopen.return_value.__enter__.return_value
        mock_response.read.return_value = b"{}"
        # First call — sends
        ok1 = notifications.send_email("dedup test", "<p>body</p>")
        # Second call within window — deduped, should NOT call urlopen
        ok2 = notifications.send_email("dedup test", "<p>body</p>")
    assert ok1 is True
    assert ok2 is True
    # urlopen called exactly once (first email only)
    assert mock_urlopen.call_count == 1
