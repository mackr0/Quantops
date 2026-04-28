"""Guardrail: every trading_profiles column the live code accesses
via `ctx.<column_name>` must (a) be a UserContext field AND (b) be
populated by `models.build_user_context_from_profile`.

History: 2026-04-28. The Lever 3 health check correctly wrote
`disabled_specialists` to the DB. But ensemble.run_ensemble reads
the disable list via `getattr(ctx, "disabled_specialists", "[]")`
— and `disabled_specialists` was NEVER added to the UserContext
dataclass NOR populated in `build_user_context_from_profile`. So
the DB write was real but the running scheduler couldn't see it
through ctx. Lever 3's effect was silently zero in production.

Same risk applied to `meta_pregate_threshold`. Both fixed in this
commit; this test prevents future column additions from hitting
the same silent-disconnect.

Approach:
1. Walk every .py file in the repo (excluding tests/) for the
   pattern `getattr(ctx, "<name>", ...)` and `ctx.<name>`.
2. Filter to names that are also column names on trading_profiles.
3. Each surviving name must be:
   a. A field on the UserContext dataclass.
   b. Assigned in `build_user_context_from_profile`.
"""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import fields
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Allowlist of ctx attributes that exist on the dataclass but aren't
# named after a profile column (e.g. ctx.user_id ≠ profiles.user_id).
# Only entries that need an exception belong here.
NON_PROFILE_CTX_FIELDS = {
    "user_id", "segment", "display_name", "profile_id",
    "alpaca_api_key", "alpaca_secret_key", "alpaca_base_url",
    "ai_api_key", "consensus_api_key",
    "db_path", "notification_email", "resend_api_key",
    "ai_confidence_threshold",  # nullable on profiles, default-set elsewhere
    "ai_provider", "ai_model",
}


def _profile_columns():
    """Parse trading_profiles columns from models.py."""
    src = (REPO_ROOT / "models.py").read_text()
    cols = set()
    create_match = re.search(
        r"CREATE TABLE IF NOT EXISTS trading_profiles \((.*?)\);",
        src, flags=re.DOTALL,
    )
    if create_match:
        for line in create_match.group(1).splitlines():
            m = re.match(r"\s+([a-z_][a-z0-9_]*)\s+", line)
            if m:
                cols.add(m.group(1))
    for m in re.finditer(
        r'\(\s*"trading_profiles"\s*,\s*"([a-z_][a-z0-9_]*)"\s*,',
        src,
    ):
        cols.add(m.group(1))
    return cols


def _ctx_attrs_accessed_in_repo():
    """Find every `ctx.<name>` and `getattr(ctx, "<name>", ...)` use
    in repo .py files (excluding tests/, venv/, etc.)."""
    accessed = set()
    for path in REPO_ROOT.rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if any(p in rel for p in ("tests/", "/venv/", "/__pycache__/", "/.git/")):
            continue
        try:
            src = path.read_text()
        except Exception:
            continue
        for m in re.finditer(r"\bctx\.([a-z_][a-z0-9_]*)\b", src):
            accessed.add(m.group(1))
        for m in re.finditer(
            r"""getattr\(\s*ctx\s*,\s*['"]([a-z_][a-z0-9_]*)['"]""",
            src,
        ):
            accessed.add(m.group(1))
    return accessed


def _user_context_fields():
    from user_context import UserContext
    return {f.name for f in fields(UserContext)}


def _build_user_context_assignments():
    """Parse build_user_context_from_profile and return the set of
    keyword names assigned in the UserContext(...) constructor call."""
    import models
    src = inspect.getsource(models.build_user_context_from_profile)
    # Match foo=... at any nesting depth
    return set(re.findall(r"\b([a-z_][a-z0-9_]*)\s*=", src))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_every_ctx_attr_for_profile_column_is_a_user_context_field():
    """If the repo accesses `ctx.X` AND X is also a trading_profiles
    column, X must be a field on UserContext. Otherwise the access
    silently returns the default (or AttributeError caught by
    getattr-with-default), making the column inert from the running
    code's perspective."""
    profile_cols = _profile_columns()
    accessed = _ctx_attrs_accessed_in_repo()
    ctx_fields = _user_context_fields()

    # Filter to attributes that ARE profile columns (so missing
    # ctx field is meaningful) AND are not on the non-profile allowlist.
    relevant = (accessed & profile_cols) - NON_PROFILE_CTX_FIELDS
    missing = relevant - ctx_fields

    if missing:
        pytest.fail(
            "The following profile-column names are accessed via\n"
            "`ctx.X` or `getattr(ctx, X, ...)` somewhere in the repo,\n"
            "but are NOT fields on the UserContext dataclass. The DB\n"
            "value never reaches the running code; default is used\n"
            "instead. See 2026-04-28 disabled_specialists incident.\n"
            "\n"
            "Add each name as a UserContext field AND assign it in\n"
            "models.build_user_context_from_profile.\n"
            "\n"
            f"Missing: {sorted(missing)}"
        )


def test_every_user_context_field_for_profile_column_is_populated():
    """Every UserContext field that corresponds to a trading_profiles
    column must be assigned in build_user_context_from_profile.
    Otherwise the field stays at the dataclass default forever."""
    profile_cols = _profile_columns()
    ctx_fields = _user_context_fields()
    assigned = _build_user_context_assignments()

    relevant = (ctx_fields & profile_cols)
    unassigned = relevant - assigned

    if unassigned:
        pytest.fail(
            "The following UserContext fields correspond to\n"
            "trading_profiles columns but are NOT assigned in\n"
            "build_user_context_from_profile. They will stay at the\n"
            "dataclass default for all profiles, so the per-profile\n"
            "DB values never reach the running code.\n"
            "\n"
            f"Unassigned: {sorted(unassigned)}"
        )


def test_disabled_specialists_round_trip_specifically():
    """Explicit guard for the 2026-04-28 incident — disabled_specialists
    must round-trip: DB → ctx → ensemble.run_ensemble."""
    ctx_fields = _user_context_fields()
    assigned = _build_user_context_assignments()
    assert "disabled_specialists" in ctx_fields, (
        "REGRESSION: disabled_specialists removed from UserContext. "
        "Lever 3 disable list won't reach ensemble.run_ensemble."
    )
    assert "disabled_specialists" in assigned, (
        "REGRESSION: build_user_context_from_profile no longer "
        "populates disabled_specialists. ctx.disabled_specialists "
        "stays at default, ensemble runs all 4 specialists."
    )


def test_meta_pregate_threshold_round_trip_specifically():
    ctx_fields = _user_context_fields()
    assigned = _build_user_context_assignments()
    assert "meta_pregate_threshold" in ctx_fields
    assert "meta_pregate_threshold" in assigned
