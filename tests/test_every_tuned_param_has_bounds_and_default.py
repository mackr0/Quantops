"""Structural guardrail: every parameter the tuner adjusts must
have a bounds entry in `param_bounds.PARAM_BOUNDS`, a default in
`UserContext`, and a column on `trading_profiles`.

The bug class.
On 2026-05-13 (wave 9a), the meta_pregate tuner I built almost
shipped without adding `meta_pregate_threshold` to PARAM_BOUNDS.
The tuner would have written values via `update_trading_profile`
unbounded — the `_bound()` helper would have raised KeyError on
the param name OR returned the value unclamped.

The general bug class:
  Tuner adds `update_trading_profile(pid, foo=value)` →
  forgets to add foo to PARAM_BOUNDS →
  no clamping happens →
  tuner writes garbage values →
  next ctx-load reads them →
  trading parameters silently corrupted.

This test scans every `_optimize_*` function for the params it
writes, then verifies the full chain:
  1. PARAM_BOUNDS has an entry (for clamping)
  2. UserContext dataclass has a field with a default value
  3. trading_profiles schema migration includes the column

Catches: "I added a tuner; forgot one of the three required
companion entries."
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys
from typing import Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Param names that the tuner writes but are intentionally NOT in
# PARAM_BOUNDS (e.g., JSON-shaped overrides like signal_weights —
# the JSON value is bound by signal_weights' own schema, not by
# PARAM_BOUNDS scalar bounds). Each entry needs a written rationale.
KNOWN_NON_BOUNDS_PARAMS = {
    # Only entries for params that ACTUALLY get written via
    # update_trading_profile(pid, X=value) — the JSON-override
    # writers (set_signal_weight, set_override) bypass this code
    # path so their keys aren't in the tuned-set.
    "entry_blacklist":
        "JSON dict of {symbol: expiry_iso}; written as a serialized "
        "JSON string. Structure validated by entry_blacklist.py "
        "parse helper, not by scalar bounds.",
}


def _self_tuning_source() -> str:
    with open(os.path.join(REPO_ROOT, "self_tuning.py")) as fh:
        return fh.read()


def _extract_tuned_params() -> Set[str]:
    """AST-walk self_tuning.py for all
        update_trading_profile(profile_id, <param_name>=...)
    calls. Return the set of param_names written."""
    src = _self_tuning_source()
    tree = ast.parse(src)
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # Match by callable name (handles both bare and module-attr
        # forms after various import styles)
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        if name != "update_trading_profile":
            continue
        # First positional arg is profile_id; remaining keyword args
        # are param names
        for kw in node.keywords:
            if kw.arg:  # named keyword (not **kwargs)
                out.add(kw.arg)
    return out


def _load_param_bounds() -> Set[str]:
    from param_bounds import PARAM_BOUNDS
    return set(PARAM_BOUNDS.keys())


def _load_user_context_fields() -> Set[str]:
    import dataclasses
    from user_context import UserContext
    return {f.name for f in dataclasses.fields(UserContext)}


def _load_trading_profile_columns() -> Set[str]:
    """Load the union of trading_profiles columns across the
    CREATE TABLE statement and the migration list in models.py."""
    with open(os.path.join(REPO_ROOT, "models.py")) as fh:
        src = fh.read()
    out = set()
    # CREATE TABLE trading_profiles (...) — extract column names
    m = re.search(
        r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?trading_profiles\s*\((.*?)\)\s*[;)]",
        src, re.IGNORECASE | re.DOTALL,
    )
    if m:
        body = m.group(1)
        for line in body.split("\n"):
            line = line.strip().rstrip(",").strip()
            if not line or line.startswith("--") or line.startswith("PRIMARY"):
                continue
            tok = line.split()[0] if line.split() else ""
            if tok and tok.isidentifier():
                out.add(tok)
    # Migration list: ("trading_profiles", "<col>", "<def>")
    for m in re.finditer(
        r'\(\s*"trading_profiles"\s*,\s*"([a-z_][a-z_0-9]*)"',
        src,
    ):
        out.add(m.group(1))
    return out


class TestEveryTunedParamHasBoundsAndDefault:
    def test_every_tuned_param_in_param_bounds(self):
        """Every param the tuner writes (except JSON-shape overrides
        in KNOWN_NON_BOUNDS_PARAMS) must be in PARAM_BOUNDS for the
        _bound() clamping helper to work."""
        tuned = _extract_tuned_params()
        bounds = _load_param_bounds()
        violations = sorted(
            tuned - bounds - set(KNOWN_NON_BOUNDS_PARAMS)
        )
        if violations:
            pytest.fail(
                "Tuner writes params with NO PARAM_BOUNDS entry:\n  "
                + "\n  ".join(violations)
                + "\n\nBug class: tuner writes unclamped values; "
                "_bound() raises KeyError or silently no-ops; "
                "garbage values land in trading_profiles. Add the "
                "param to param_bounds.PARAM_BOUNDS with (min, max) "
                "OR add to KNOWN_NON_BOUNDS_PARAMS in this test "
                "with a written rationale (JSON-shape, sentinel, "
                "etc.)"
            )

    def test_every_tuned_param_in_user_context(self):
        """Every tuned param needs a UserContext field with a
        default. Without it, fresh profiles see None → handlers
        crash on .get / arithmetic."""
        tuned = _extract_tuned_params()
        ctx_fields = _load_user_context_fields()
        violations = sorted(
            tuned - ctx_fields - set(KNOWN_NON_BOUNDS_PARAMS)
        )
        if violations:
            pytest.fail(
                "Tuner writes params NOT on UserContext:\n  "
                + "\n  ".join(violations)
                + "\n\nFresh profiles will see None for these. "
                "Add a field with a default value to user_context.py."
            )

    def test_every_tuned_param_in_schema(self):
        """Every tuned param needs a trading_profiles column.
        update_trading_profile silently drops kwargs not in
        allowed_cols; the row never receives the value."""
        tuned = _extract_tuned_params()
        schema = _load_trading_profile_columns()
        violations = sorted(
            tuned - schema - set(KNOWN_NON_BOUNDS_PARAMS)
        )
        if violations:
            pytest.fail(
                "Tuner writes params NOT in trading_profiles "
                "schema:\n  " + "\n  ".join(violations)
                + "\n\nupdate_trading_profile drops these as "
                "rejected kwargs. Add to the migration list in "
                "models.init_user_db."
            )

    def test_known_non_bounds_entries_match_actual_writes(self):
        """Stale allowlist entries (params no longer written by any
        tuner) should fail this test so rationales stay current."""
        tuned = _extract_tuned_params()
        stale = set(KNOWN_NON_BOUNDS_PARAMS) - tuned
        if stale:
            pytest.fail(
                "KNOWN_NON_BOUNDS_PARAMS contains entries no "
                "tuner writes anymore:\n  "
                + "\n  ".join(sorted(stale))
                + "\n\nRemove these — they protect nothing."
            )
