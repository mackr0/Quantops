"""Tests must never send real emails (2026-06-16).

The 2026-06-15 spam incident: config.load_dotenv() loads the repo
.env (real RESEND_API_KEY + operator address) at import, so every
test reaching notify_error/send_email emailed the operator for
real. ~15 full-suite runs that day = an inbox flood of reconciler-
halt alerts built from test fixtures (profile 99, GT 573, …).

The conftest autouse fixture `_block_real_email_in_tests` clears
config.RESEND_API_KEY so send_email() is a guaranteed no-op. These
tests pin that protection — including the exact path that spammed
(a reconciler halt → halt_and_alert → notify_error → send_email)
must NOT touch the network.
"""
from __future__ import annotations

from unittest.mock import patch


def test_resend_key_cleared_during_tests():
    """The autouse fixture must have neutralized the live key."""
    import config
    assert not config.RESEND_API_KEY, (
        "RESEND_API_KEY is live during tests — the autouse email "
        "block is missing/broken; the suite will spam the operator."
    )


def test_send_email_is_noop_without_key():
    """send_email returns False (no network) when the key is cleared."""
    import notifications
    with patch("notifications.urllib.request.urlopen") as urlopen:
        sent = notifications.send_email("subject", "<p>body</p>")
    assert sent is False
    urlopen.assert_not_called(), "send_email hit the network despite no key"


def test_halt_and_alert_sends_no_real_email(tmp_path):
    """The exact spam path: a halt fires notify_error -> send_email.
    With the key cleared it must NOT reach the network."""
    import sqlite3
    from contextlib import closing
    db = str(tmp_path / "p.db")
    with closing(sqlite3.connect(db)) as c:
        c.execute("CREATE TABLE audit_alerts (id INTEGER PRIMARY KEY, "
                  "timestamp TEXT DEFAULT (datetime('now')), alert_type TEXT, "
                  "severity TEXT, title TEXT, detail TEXT)")
        c.commit()
    import halt_helpers
    with patch("notifications.urllib.request.urlopen") as urlopen, \
         patch("halt_helpers.halt_profile", return_value=True), \
         patch("halt_helpers._write_audit_alert"):
        # first_transition=True → notify_error path runs
        halt_helpers.halt_and_alert(
            profile_id=99, db_path=db, alert_type="reconciler_synthesis_halt",
            title="Profile 99 HALTED", detail="GT qty=573 gt-trail",
        )
    urlopen.assert_not_called(), (
        "halt_and_alert sent a REAL email during tests — the "
        "2026-06-15 reconciler-halt spam path is unprotected."
    )
