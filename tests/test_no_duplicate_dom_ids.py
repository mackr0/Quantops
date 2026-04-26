"""Guardrail: every `id="..."` attribute in templates must be unique
within the same template file.

When two DOM elements share an ID, `getElementById` returns only the
first match. Any JS that targets the second element silently does
nothing — and the user sees a permanent "Loading..." (or whatever the
initial placeholder was) on the orphaned element.

This is the bug pattern that hit on 2026-04-25: the new "Active
Lessons" widget on the Operations tab got `id="learned-patterns-widget"`,
but that ID was already used by an older widget on the Brain tab. The
JS targeted the first match (Brain tab) and the new card stayed stuck
on "Loading..." forever.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest


# Templates whose duplicate IDs are known/intentional and shouldn't
# fail the test. Empty for now; add entries with rationale if a real
# need arises (e.g., a partial template included twice on purpose).
ALLOWED_DUPLICATES: dict = {
    # filename -> {set of ID strings that are intentionally duplicated}
}


def _collect_template_ids(template_path: Path) -> Counter:
    """Return a Counter of every `id="..."` value in this template.

    Skips IDs inside JS string literals (single quotes around the
    full id, after `getElementById(`, etc.) — we only care about
    actual HTML id attributes in DOM elements.
    """
    text = template_path.read_text()
    # Strip out <script> and <style> blocks first so JS-generated id
    # strings inside JS literals don't count.
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text,
                   flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text,
                   flags=re.DOTALL | re.IGNORECASE)

    ids = Counter()
    # Match id="..." (HTML attribute syntax). Avoid matching inside
    # CSS selectors or other attributes by anchoring on a preceding
    # space or tag-start.
    for m in re.finditer(r'(?:\s|<[a-zA-Z][^\s>]*\s)id="([^"]+)"', text):
        ids[m.group(1)] += 1
    return ids


def _all_templates() -> list:
    root = Path(__file__).resolve().parent.parent / "templates"
    return sorted(root.rglob("*.html"))


class TestNoDuplicateDomIds:
    def test_no_duplicate_ids_in_any_template(self):
        """For each template file, fail if any id appears more than
        once. Allowlist intentional duplicates in
        ALLOWED_DUPLICATES."""
        offenders = []
        for path in _all_templates():
            ids = _collect_template_ids(path)
            allowed = ALLOWED_DUPLICATES.get(path.name, set())
            dups = {i: c for i, c in ids.items()
                    if c > 1 and i not in allowed}
            if dups:
                offenders.append((path.name, dups))

        if offenders:
            details = "\n".join(
                f"  {fname}:\n" + "\n".join(
                    f"    id={i!r} appears {c}× — JS getElementById"
                    f" returns only the first match, second/etc."
                    f" silently orphaned"
                    for i, c in dups.items())
                for fname, dups in offenders
            )
            pytest.fail(
                "Templates contain duplicate `id=` attributes.\n\n"
                "When two DOM elements share an ID, getElementById\n"
                "returns the FIRST match — any JS targeting the\n"
                "second element does nothing, and the second element\n"
                "shows its initial placeholder forever (e.g., a\n"
                "permanent \"Loading...\").\n\n"
                "Fix one of:\n"
                "  1. Rename one of the duplicate IDs to a unique value.\n"
                "  2. If the duplicate is intentional (e.g., a partial\n"
                "     template included twice), add the ID to\n"
                "     ALLOWED_DUPLICATES[filename] in this test with\n"
                "     a rationale.\n\n"
                f"{details}"
            )
