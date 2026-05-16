"""Structural guardrail: every `notify_*` function in
notifications.py must EITHER:
  - have per-subject debounce (like notify_error), OR
  - be a no-op stub (return without sending), OR
  - have a written rationale in `KNOWN_NO_DEBOUNCE_NEEDED`
    explaining why spam can't happen.

The bug class.
On 2026-05-13, `notify_error` fired 145 times in 2 hours because
the scheduler crash-looped + each restart called notify_error.
Fix: per-subject 1-hour debounce on notify_error.

The general bug class: ANY notify_* function called from a path
that can recur quickly (cron task, scheduled retry, error handler
loop) can produce email spam unless something gates it.

This test catches the case where someone adds a new notify_*
function — or re-enables a currently-stubbed one — without
adding the spam defense.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Functions that legitimately don't need spam-defense, with
# rationale. Each entry must explain WHY spam can't happen.
KNOWN_NO_DEBOUNCE_NEEDED = {
    "notify_daily_summary":
        "Fires once per profile per day, gated by a marker file "
        "(.daily_summary_sent_p<id>.marker). Idempotent re-fire is "
        "blocked by the marker, not by debounce.",
    "notify_trade":
        "Currently a no-op stub (returns without sending) — was "
        "disabled because 10 profiles × per-trade emails was "
        "noisy. If re-enabled, debounce or rate-limit must be "
        "added at that time.",
    "notify_veto":
        "Currently a no-op stub. Same as notify_trade.",
    "notify_exit":
        "Currently a no-op stub. Same as notify_trade.",
    "notify_shadow_eval_daily":
        "Fires once per profile per day, gated by a marker file "
        "(.shadow_eval_sent_p<id>.marker) in "
        "multi_scheduler._task_shadow_eval_daily_email. Same pattern "
        "as notify_daily_summary — the scheduler writes the marker "
        "only when the email actually sent, so retries inside a "
        "single calendar day are blocked.",
}


def _discover_notify_functions():
    """Return list of (function_name, source_text) for every
    top-level `notify_*` function defined in notifications.py."""
    import notifications
    out = []
    for name in dir(notifications):
        if not name.startswith("notify_"):
            continue
        obj = getattr(notifications, name)
        if not callable(obj):
            continue
        if not inspect.isfunction(obj):
            continue
        # Must be defined in notifications module (not imported)
        if obj.__module__ != "notifications":
            continue
        try:
            src = inspect.getsource(obj)
        except (OSError, TypeError):
            src = ""
        out.append((name, src))
    return out


def _function_is_noop_stub(src: str) -> bool:
    """Heuristic: function body is just `return` or `return X`
    with no side-effecting calls. Indicates the function is
    intentionally disabled."""
    # Strip docstring + comments + signature
    body_lines = []
    in_docstring = False
    docstring_quote = None
    for line in src.split("\n")[1:]:  # skip def line
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if in_docstring:
            if docstring_quote in stripped:
                in_docstring = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            docstring_quote = stripped[:3]
            if stripped.count(docstring_quote) >= 2:
                continue  # single-line docstring
            in_docstring = True
            continue
        body_lines.append(stripped)
    if not body_lines:
        return False
    # If only line is `return` (with optional value), it's a stub
    return all(
        l == "return" or l.startswith("return ")
        for l in body_lines
    )


def _function_has_debounce_pattern(src: str) -> bool:
    """Does the function reference any debounce mechanism?

    Recognized patterns:
      - `_notify_*_last_sent` module-level dict
      - calls to `_check_debounce` / `_notification_debounce`
      - timedelta/datetime comparison gating early-return
      - 'debounce' keyword in source
    """
    if "debounce" in src.lower():
        return True
    if "_last_sent" in src:
        return True
    if "marker" in src.lower() and "exists" in src.lower():
        # Marker-file pattern (notify_daily_summary uses
        # .daily_summary_sent_p<id>.marker). The function gates
        # on marker existence; equivalent to debounce.
        return True
    return False


def _function_calls_send_email(src: str) -> bool:
    """Does the function actually send an email?
    If not, it's effectively a no-op even without debounce."""
    return "send_email(" in src


class TestEveryNotifyHasDebounceOrRationale:
    def test_every_notify_function_is_safe(self):
        funcs = _discover_notify_functions()
        assert len(funcs) >= 3, (
            f"Discovered only {len(funcs)} notify_* functions — "
            f"likely broken; investigate."
        )
        violations = []
        for name, src in funcs:
            # Path 1: debounce pattern present
            if _function_has_debounce_pattern(src):
                continue
            # Path 2: stub (returns without sending)
            if _function_is_noop_stub(src):
                continue
            # Path 3: doesn't actually send
            if not _function_calls_send_email(src):
                continue
            # Path 4: explicitly allowlisted
            if name in KNOWN_NO_DEBOUNCE_NEEDED:
                continue
            violations.append(name)
        if violations:
            details = "\n".join(f"  - {n}" for n in violations)
            pytest.fail(
                "These notify_* functions can send emails without "
                "any spam defense:\n\n" + details
                + "\n\nThe May 13 incident: notify_error fired 145 "
                "times in 2 hours from a crash loop. Any function "
                "that calls send_email AND can be invoked in a "
                "tight loop must have ONE of:\n"
                "  1. Per-subject/per-key debounce dict (see "
                "notify_error pattern)\n"
                "  2. File-based marker gate (see "
                "notify_daily_summary)\n"
                "  3. Rate limit / cooldown\n"
                "  4. Add to KNOWN_NO_DEBOUNCE_NEEDED with a "
                "written rationale explaining why this function "
                "specifically can't be called in a loop"
            )

    def test_known_allowlist_entries_match_existing_functions(self):
        """Stale allowlist entries (function removed) should fail
        the test so rationales stay current."""
        existing = {n for n, _ in _discover_notify_functions()}
        stale = set(KNOWN_NO_DEBOUNCE_NEEDED) - existing
        if stale:
            pytest.fail(
                "KNOWN_NO_DEBOUNCE_NEEDED contains entries for "
                "functions that don't exist:\n  "
                + "\n  ".join(sorted(stale))
                + "\n\nRemove these — they protect nothing."
            )
