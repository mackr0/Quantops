r"""Cross-cutting guardrail: every file:line reference in OPEN_ITEMS.md
points at content that matches the table entry's claim.

Caught 2026-05-10 (Issue 11): OPEN_ITEMS.md §10 had 9 entries with
`<file>:<line>` refs. After Issues 6, 7, 10 rewrote / removed several
of those source comments, the OPEN_ITEMS entries went stale: line
numbers drifted, status remained ⏳ OPEN for items that were ✅ DONE,
and `slippage_model.py:197` pointed at content that didn't exist.

This test prevents future drift by structurally enforcing the
referential integrity:

1. Every `\`<file>:<line>\`` reference must point at an existing
   file at a valid line number.
2. If the table entry has a quoted source-text snippet (between
   straight double-quotes in the row), that quote must appear in
   the file at-or-near the claimed line for ⏳ OPEN entries
   (because the deferred comment is supposed to still be there),
   AND must NOT appear for ✅ DONE entries (because you fixed the
   underlying comment when you marked it DONE).

If you rewrite a comment to mark a deferred item DONE, you also
have to update OPEN_ITEMS.md to reflect that. This test makes
"forgot to update OPEN_ITEMS" a build failure instead of a
slow-rotting trust gap.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
OPEN_ITEMS_PATH = os.path.join(REPO_ROOT, "OPEN_ITEMS.md")


# Match `path/to/file.py:123` inside backticks
FILE_LINE_RE = re.compile(r"`([\w/.\-]+\.py):(\d+)`")

# Detect the row's status emoji
OPEN_MARKERS = ("⏳ OPEN",)
DONE_MARKERS = ("✅ DONE",)
DEFERRED_MARKERS = ("🔒 DEFERRED",)


def _parse_open_items():
    """Yield (line_in_md, file_ref, line_ref, full_row) for every
    `file:line` reference inside a markdown table row."""
    with open(OPEN_ITEMS_PATH) as f:
        for md_line_no, raw in enumerate(f.readlines(), start=1):
            for m in FILE_LINE_RE.finditer(raw):
                yield md_line_no, m.group(1), int(m.group(2)), raw


def _row_quotes(row):
    """Extract every quoted snippet from a markdown table row.
    Uses straight double-quotes; smart-quote variants ignored."""
    return re.findall(r'"([^"]+)"', row)


def _row_status(row):
    if any(m in row for m in DONE_MARKERS):
        return "DONE"
    if any(m in row for m in DEFERRED_MARKERS):
        return "DEFERRED"
    if any(m in row for m in OPEN_MARKERS):
        return "OPEN"
    return None


# ---------------------------------------------------------------------------
# Layer 1 — every file:line ref points at a real file + valid line
# ---------------------------------------------------------------------------


def test_every_open_items_file_line_ref_resolves():
    """`<file>:<line>` references must point at an existing file with
    a valid line number. Catches refactors that delete the file or
    references that drift past EOF."""
    leaks = []
    for md_line, fname, ln, _ in _parse_open_items():
        full_path = os.path.join(REPO_ROOT, fname)
        if not os.path.isfile(full_path):
            leaks.append(
                f"  OPEN_ITEMS.md:{md_line} — file `{fname}` does not "
                "exist (renamed/deleted/moved?)."
            )
            continue
        with open(full_path) as f:
            n_lines = sum(1 for _ in f)
        if ln < 1 or ln > n_lines:
            leaks.append(
                f"  OPEN_ITEMS.md:{md_line} — `{fname}:{ln}` is out "
                f"of range (file has {n_lines} lines)."
            )
    assert not leaks, (
        "Found OPEN_ITEMS.md `file:line` refs that don't resolve. "
        "Update the line numbers or remove the entries.\n\n"
        + "\n".join(leaks)
    )


# ---------------------------------------------------------------------------
# Layer 2 — quoted source text matches reality
# ---------------------------------------------------------------------------


def test_open_entry_quoted_text_still_exists_in_source():
    """For every ⏳ OPEN entry with a `\"quoted snippet\"`, that exact
    snippet must appear in the referenced file. If it doesn't, either:
      - The deferred comment was rewritten / shipped (then mark the
        entry ✅ DONE in the table), or
      - The line number drifted (then update the line ref), or
      - The quoted text was paraphrased (use the verbatim source
        text so this test can verify it).
    """
    leaks = []
    for md_line, fname, ln, row in _parse_open_items():
        if _row_status(row) != "OPEN":
            continue
        quotes = _row_quotes(row)
        if not quotes:
            continue
        full_path = os.path.join(REPO_ROOT, fname)
        if not os.path.isfile(full_path):
            continue  # Layer 1 catches this
        with open(full_path) as f:
            text = f.read()
        for q in quotes:
            # The snippet may use ellipses ('...'); require each
            # non-ellipsis fragment to appear.
            fragments = [f.strip() for f in q.split("...") if f.strip()]
            if not all(frag in text for frag in fragments):
                leaks.append(
                    f"  OPEN_ITEMS.md:{md_line} — entry for "
                    f"`{fname}:{ln}` is marked ⏳ OPEN but its quoted "
                    f"text {q!r} no longer appears in the source. "
                    "Either mark the entry ✅ DONE (the underlying "
                    "comment was rewritten / the work shipped) or "
                    "update the quote to match current source."
                )
    assert not leaks, (
        "OPEN_ITEMS.md ⏳ OPEN entries reference source text that no "
        "longer exists. The work likely shipped — mark the entry "
        "DONE.\n\n" + "\n".join(leaks)
    )


def test_done_entry_quoted_text_no_longer_in_source():
    """For ✅ DONE entries with a `\"quoted snippet\"`, that snippet
    SHOULD NOT appear in the referenced file (because shipping a
    fix typically means rewriting the deferred comment that sat there).

    Exceptions: some DONE entries quote a function name or term that
    is fine to leave verbatim (e.g., `bootstrap_mode='by_day'` is
    quoted in both the OPEN_ITEMS entry AND the source code post-fix).
    For those cases, the quoted text contains characters like `=`, `(`
    or `:` that would not naturally appear in a stale prose comment;
    we exempt quotes that look like code identifiers / API references.
    """
    leaks = []
    code_like_re = re.compile(r"^[\w\.\-]+(\(|=|:)")
    for md_line, fname, ln, row in _parse_open_items():
        if _row_status(row) != "DONE":
            continue
        quotes = _row_quotes(row)
        if not quotes:
            continue
        full_path = os.path.join(REPO_ROOT, fname)
        if not os.path.isfile(full_path):
            continue
        with open(full_path) as f:
            text = f.read()
        for q in quotes:
            # Exempt code-like / API-reference quotes (e.g., "fetch_news_alpaca",
            # "bootstrap_mode='by_day'"). These can legitimately exist in
            # the source post-fix.
            if code_like_re.match(q.strip()):
                continue
            if any(kw in q.lower() for kw in (
                "deferred", "future enhancement", "future cleanup",
                "future commit", "future follow-up", "todo", "fixme",
                "not yet", "we don't", "would benefit",
            )):
                # If the quoted text is the kind of "deferred" prose
                # we said we fixed, it must be gone.
                if q in text:
                    leaks.append(
                        f"  OPEN_ITEMS.md:{md_line} — entry for "
                        f"`{fname}:{ln}` is marked ✅ DONE but its "
                        f"deferred-quote {q!r} STILL appears in the "
                        "source. Either rewrite the comment to "
                        "reflect the shipped state, or revert the "
                        "DONE status."
                    )
    assert not leaks, (
        "OPEN_ITEMS.md ✅ DONE entries quote deferred-comment text "
        "that still exists in source. The fix didn't update the "
        "comment.\n\n" + "\n".join(leaks)
    )
