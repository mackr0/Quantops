"""Guardrail: every column the system tries to write to
trading_profiles via update_trading_profile() must be in the
allowed_cols allowlist — OR get a loud warning instead of silent
swallow.

History: 2026-04-28. The daily _task_specialist_health_check
correctly identified pattern_recognizer as anti-correlated on
Small Cap (raw=90 → cal=28) and called
`update_trading_profile(profile_id, disabled_specialists=...)`.
But `disabled_specialists` wasn't in the allowed_cols allowlist
— the kwarg was silently filtered out and no UPDATE happened.
The function returned cleanly. The health check logged
"Specialist health check applied: DISABLE pattern_recognizer".
But in the database, disabled_specialists stayed at "[]" for
every profile.

Whole class of bug: any new column added to the schema is
useless to autonomous tuners until it's also added to the
allowlist. There was no test catching this.

Fix at the source: update_trading_profile now logs a warning
when it rejects a kwarg as not-in-allowlist (instead of silent
swallow). Plus this test enforces the structural invariant:

1. The known-tuned columns (the ones live code actually writes
   via update_trading_profile) must all be in allowed_cols.
2. The allowlist itself must mention disabled_specialists and
   meta_pregate_threshold (the Lever 2/3 columns the daily
   health check writes).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _allowed_cols_set():
    """Parse allowed_cols literal out of update_trading_profile's source."""
    import models
    src = inspect.getsource(models.update_trading_profile)
    # Find the allowed_cols = { ... } block
    m = re.search(r"allowed_cols\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    assert m, "Couldn't find allowed_cols literal in update_trading_profile"
    block = m.group(1)
    # Extract every double-quoted string literal
    return set(re.findall(r'"([a-z_]+)"', block))


def _kwargs_passed_to_update_trading_profile():
    """Walk every .py file in the repo and extract every
    `update_trading_profile(..., key=value)` kwarg name.

    Returns the set of column names the live code TRIES to update.
    """
    cols = set()
    for path in REPO_ROOT.rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if any(s in rel for s in ("/venv/", "/__pycache__/", "tests/", "/.git/")):
            continue
        try:
            src = path.read_text()
        except Exception:
            continue
        # Match: update_trading_profile(profile_id, col=value, col2=value, ...)
        # Capture all kwarg names inside the parens.
        for call_match in re.finditer(
            r"update_trading_profile\s*\(([^)]*)\)",
            src,
            flags=re.DOTALL,
        ):
            args = call_match.group(1)
            # Find every kwarg-style assignment (col=value), excluding
            # **kwargs / dict expansions.
            for kw in re.finditer(r"(\b[a-z_]+)\s*=", args):
                name = kw.group(1)
                # Skip Python keywords + obvious non-column names
                if name in {"self", "profile_id", "user_id"}:
                    continue
                cols.add(name)
        # Match: update_trading_profile(profile_id, **{<col>: ...})
        for star in re.finditer(
            r"update_trading_profile\s*\(\s*\w+\s*,\s*\*\*\{\s*['\"]([a-z_]+)['\"]",
            src,
        ):
            cols.add(star.group(1))
    return cols


def test_every_kwarg_passed_is_in_allowed_cols():
    """The set of column names live code passes to
    update_trading_profile must be a subset of allowed_cols.
    Otherwise the kwarg is silently filtered out — exactly the
    2026-04-28 disabled_specialists bug."""
    allowed = _allowed_cols_set()
    used = _kwargs_passed_to_update_trading_profile()

    missing = used - allowed
    if missing:
        pytest.fail(
            "The following kwargs are passed to update_trading_profile()\n"
            "somewhere in the codebase but are NOT in its allowed_cols\n"
            "allowlist. Without an entry in the allowlist, these UPDATE\n"
            "calls are silently filtered out and no DB write happens.\n"
            "\n"
            "Add each missing column to the allowed_cols set in\n"
            "models.update_trading_profile.\n"
            "\n"
            f"Missing: {sorted(missing)}"
        )


def test_lever_2_3_columns_in_allowlist():
    """Explicit guard: the Lever 2 + Lever 3 columns must be in
    the allowlist. Adding them was the entire point of the
    2026-04-28 fix."""
    allowed = _allowed_cols_set()
    for col in ("disabled_specialists", "meta_pregate_threshold"):
        assert col in allowed, (
            f"REGRESSION: {col!r} missing from update_trading_profile's\n"
            f"allowed_cols. Without it, the autonomous health check\n"
            f"can't actually persist its decisions — the disabled\n"
            f"specialist list stays [] forever even though detection\n"
            f"says 'DISABLE pattern_recognizer'."
        )


def test_update_trading_profile_logs_rejected_kwargs():
    """The function must log a warning when it rejects a kwarg
    that isn't in the allowlist. The 2026-04-28 incident hid
    because the rejection was silent."""
    import models
    src = inspect.getsource(models.update_trading_profile)
    assert "rejected" in src.lower() and "logger.warning" in src, (
        "REGRESSION: update_trading_profile no longer logs a\n"
        "warning when it rejects a kwarg as unknown. Without the\n"
        "loud log, callers can't tell their UPDATE didn't apply.\n"
        "Re-add the rejected-kwarg warning logic."
    )
