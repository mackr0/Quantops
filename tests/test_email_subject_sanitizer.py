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
