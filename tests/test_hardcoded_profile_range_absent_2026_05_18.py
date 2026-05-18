"""Guardrail: no hardcoded `range(1, 12)` or `range(1, 11)` etc. in
production code that should iterate active profiles.

The hardcoded 1-11 range was a relic of the original 11-profile setup.
When the 13-profile fresh-start experiment created profiles 12-24, the
hardcoded ranges in multi_scheduler, aggregate_audit, and
reconcile_journal_to_broker silently excluded them — causing:

  - `_all_journal_sell_order_ids(range(1, 12))` returned an empty set
    of cross-profile SELL order_ids, so the reconciler thought every
    manual_cleanup SELL was an unmatched broker exit and inserted a
    phantom duplicate `reconcile_backfill` row.
  - Aggregate audit gated on `profile_id == 1` never fired (lowest
    active id is 12), so cross-account drift detection was silently
    disabled for the entire experiment.

This test pins the structural invariant: hardcoded profile-id ranges
must not appear in the listed production modules. Replace any with
`models.get_active_profile_ids()`.
"""
from __future__ import annotations

import re

# Files that iterate over the live profile universe and must use
# get_active_profile_ids() instead of hardcoded ranges.
GUARDED_FILES = [
    "multi_scheduler.py",
    "aggregate_audit.py",
    "reconcile_journal_to_broker.py",
    "reconcile_aggregate_drift.py",
]

# Regex matches range(<low>, <high>) where low <= 1 and high <= 50
# — the suspicious "all-profiles" hardcoded pattern. Doesn't match
# things like range(0, len(items)) or range(1, n+1).
_RANGE_PAT = re.compile(r"range\(\s*[01]\s*,\s*\d{1,2}\s*\)")

# Doc/comment-only mentions are fine. We only flag actual code lines.
def _strip_comments_and_strings(src: str) -> str:
    """Remove # comments + docstring/string literals so the regex only
    looks at executable Python (best-effort)."""
    out_lines = []
    in_triple = False
    triple_delim = None
    for line in src.splitlines():
        stripped = line
        # Toggle triple-string blocks
        for delim in ('"""', "'''"):
            if delim in stripped:
                # Count occurrences
                n = stripped.count(delim)
                if n % 2 == 1:
                    in_triple = not in_triple
        if in_triple:
            continue
        # Remove single-line # comments
        if "#" in stripped:
            # naive but adequate for this guardrail
            stripped = stripped.split("#", 1)[0]
        out_lines.append(stripped)
    return "\n".join(out_lines)


def test_no_hardcoded_profile_range_in_production_modules():
    failures = []
    for fname in GUARDED_FILES:
        with open(fname, encoding="utf-8") as f:
            src = _strip_comments_and_strings(f.read())
        for m in _RANGE_PAT.finditer(src):
            # Allow only when within a Test-only or example string.
            # The strip pass already removes strings, so any match
            # here is a real code occurrence.
            failures.append(f"{fname}: {m.group(0)!r}")
    assert not failures, (
        "Hardcoded profile-id range detected — replace with "
        "models.get_active_profile_ids():\n  "
        + "\n  ".join(failures)
    )


def test_get_active_profile_ids_returns_list_of_ints():
    """Sanity: the helper exists, is callable, returns list of ints.
    Mocks the underlying get_active_profiles since this test runs
    locally without a real DB."""
    from unittest.mock import patch
    from models import get_active_profile_ids
    fake = [{"id": 12}, {"id": 13}, {"id": 14}]
    with patch("models.get_active_profiles", return_value=fake):
        ids = get_active_profile_ids()
    assert ids == [12, 13, 14]
    for i in ids:
        assert isinstance(i, int)
