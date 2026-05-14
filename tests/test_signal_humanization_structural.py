"""Structural guardrail (2026-05-13): every signal type the
AI / strategy layer can emit must be humanized correctly by
`display_names.humanize`.

The bug class.
On 2026-05-12 the Strategy Activity ticker showed `STRONG_SELL
signal (-2/4 score)...` because the activity-feed handler
forgot to call humanize(). I added a regex guardrail for the
shape (`\\b[A-Z]{2,}(_[A-Z]+)+\\b` — see
`tests/test_no_allcaps_snake_case_in_api.py`).

That regex catches the shape, but doesn't catch the case where
humanize() ITSELF produces an ugly output for a new signal
type. If the AI invents `BUTTERFLY_OPEN` next month and the
display_names mapping doesn't include it, humanize() falls back
to title-casing which produces `Butterfly Open` — usable but
non-canonical, and may be ugly for other signal patterns.

This test enforces the structural contract: every AI/strategy
signal type discoverable in the codebase must humanize to a
clean form (no underscores, no run of all-caps tokens).

Discovery sources:
  - display_names._DISPLAY_NAMES keys (canonical mappings)
  - signal_weights.WEIGHTABLE_SIGNALS names
  - String literals in strategies.py matching the SIGNAL pattern
  - Common signal_type values from journal trades schema
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import Set

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Pattern for tokens that look like multi-word AI/strategy signals.
# REQUIRES an underscore — single-word tokens like VIX/RSI/TECH are
# legitimate canonical forms and humanize() correctly leaves them
# untouched. The bug class we care about is specifically:
#   STRONG_SELL → "Strong Sell"      (good)
#   STRONG_SELL → "STRONG SELL"      (bad — humanize didn't capitalize)
#   STRONG_SELL → "Strong_sell"      (bad — humanize didn't split)
SIGNAL_RE = re.compile(r"^[A-Z]{2,}(_[A-Z]+)+$")


# Tokens that match the regex but are NOT signal types
# (Python keywords, environment vars, technical artifacts).
NON_SIGNAL_TOKENS = {
    "TRUE", "FALSE", "NONE", "NULL",
    "PATH", "HOME", "PWD", "USER",
    "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL",
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
    "ASC", "DESC",
    "SELECT", "INSERT", "UPDATE", "WHERE", "FROM", "AND",
    "DEFAULT_BACKUP_DIR", "DEFAULT_RETAIN_DAYS",
    "BUY", "SELL",  # 3-char single-word — too generic, handle explicitly
    # Environment / config sentinels
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALPACA_API_KEY",
    "ENCRYPTION_KEY", "RESEND_API_KEY",
}


# Single-word "signals" (BUY, SELL, SHORT, HOLD, COVER) — these
# don't need humanization since they're already a single word.
# Test verifies humanize() leaves them unchanged or capitalizes.
SINGLE_WORD_SIGNALS = {"BUY", "SELL", "SHORT", "HOLD", "COVER"}


def _discover_signals_from_display_names() -> Set[str]:
    """Return signal-shaped keys from the canonical mapping
    (lowercase form — display_names stores keys lowercase but
    humanize handles both cases)."""
    from display_names import _DISPLAY_NAMES
    return {k for k in _DISPLAY_NAMES if SIGNAL_RE.match(k.upper())}


def _discover_signals_from_strategies() -> Set[str]:
    """Walk strategies.py for assignments like
        signal = "STRONG_BUY"
    and return the literal values."""
    path = os.path.join(REPO_ROOT, "strategies.py")
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as fh:
            src = fh.read()
        tree = ast.parse(src)
    except Exception:
        return set()
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Look for `signal = "STRONG_BUY"` style
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id != "signal":
                continue
            if (isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                val = node.value.value
                if SIGNAL_RE.match(val):
                    out.add(val)
    return out


def _hardcoded_strategy_signals() -> Set[str]:
    """Known signals emitted by trade_pipeline / ai_analyst /
    options paths. These are the signals the AI may return in its
    JSON response, which then become predicted_signal in
    ai_predictions and signal_type in trades."""
    return {
        "BUY", "STRONG_BUY", "WEAK_BUY",
        "SELL", "STRONG_SELL", "WEAK_SELL",
        "HOLD", "SHORT", "COVER",
        "MULTILEG_OPEN", "MULTILEG_CLOSE",
        "OPTIONS", "OPTION_EXERCISE",
        "PAIR_OPEN", "PAIR_CLOSE",
        "DELTA_HEDGE",
    }


def _all_signals() -> Set[str]:
    """Union all sources, normalized to upper-case. Filter out
    NON_SIGNAL_TOKENS."""
    discovered = (
        _discover_signals_from_display_names()
        | _discover_signals_from_strategies()
        | _hardcoded_strategy_signals()
    )
    return {s.upper() for s in discovered
            if s.upper() not in NON_SIGNAL_TOKENS}


class TestSignalHumanizationStructural:
    def test_every_signal_humanizes_cleanly(self):
        """Every discovered signal type must humanize to a form
        that contains no underscores AND no run of all-caps
        tokens (matches the bug pattern from the May 12
        STRONG_SELL leak)."""
        from display_names import humanize
        signals = _all_signals()
        assert len(signals) >= 8, (
            f"Discovered only {len(signals)} signal types — "
            f"discovery is broken; investigate."
        )
        violations = []
        for sig in sorted(signals):
            # Single-word signals (BUY, HOLD, COVER, SHORT) are
            # legitimately their canonical form. Tested separately
            # by test_single_word_signals_capitalize_correctly.
            if "_" not in sig:
                continue
            humanized = humanize(sig)
            # Clean: no underscores in output
            if "_" in humanized:
                violations.append(
                    (sig, humanized, "contains underscore")
                )
                continue
            # Clean: no run of all-caps tokens longer than 1 char
            # (single-letter words like "I", "A" are fine, but
            # "STRONG SELL" or "MULTI LEG" passing through would
            # mean humanize() didn't capitalize).
            allcaps_words = re.findall(r"\b[A-Z]{2,}\b", humanized)
            if allcaps_words:
                # Allow legit acronyms (IV, ATR, etc.) — but not
                # if the WHOLE output is all-caps multi-word
                if len(allcaps_words) > 1 or humanized.isupper():
                    violations.append(
                        (sig, humanized,
                         f"all-caps word(s): {allcaps_words}")
                    )
                    continue
        if violations:
            details = "\n".join(
                f"  {sig:25s} → '{out}'  [{why}]"
                for sig, out, why in violations
            )
            pytest.fail(
                "Signal types not humanizing cleanly:\n\n"
                + details
                + "\n\nAdd canonical mapping to "
                "display_names._DISPLAY_NAMES so humanize() returns "
                "a clean form, OR confirm the signal isn't "
                "user-visible and add it to NON_SIGNAL_TOKENS in "
                "this test."
            )

    def test_single_word_signals_capitalize_correctly(self):
        """BUY/SELL/HOLD/etc. are already single words — humanize()
        should at minimum return a non-empty string and not break
        them."""
        from display_names import humanize
        for sig in SINGLE_WORD_SIGNALS:
            result = humanize(sig)
            assert result, f"humanize('{sig}') returned empty"
            # Should be either the original (BUY) or title-cased
            # (Buy). Both are acceptable for a single word.
            assert result.lower() == sig.lower(), (
                f"humanize('{sig}') = '{result}' — single-word "
                f"signals should preserve the underlying word"
            )

    def test_humanize_is_idempotent(self):
        """humanize(humanize(x)) == humanize(x). The activity-feed
        path runs humanize() defensively even on already-humanized
        text; idempotence is the contract."""
        from display_names import humanize
        for sig in _all_signals():
            once = humanize(sig)
            twice = humanize(once)
            assert once == twice, (
                f"humanize NOT idempotent on '{sig}': "
                f"'{once}' → '{twice}'"
            )
