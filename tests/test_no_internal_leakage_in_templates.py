"""Blanket guardrail: NO snake_case identifiers and NO internal-tracker
references ever appear in user-visible template text.

This test is deliberately broad. Earlier guardrails (test_no_snake_case_
in_user_facing_ids.py, test_display_names.py) checked specific known
identifier families (sectors, factors, scenarios) and specific
ID-binding mistakes. They missed:

  - Any new dropdown that ships with snake_case option text.
  - Any new <h3> header that includes "(Item 5c)" / "(OPEN_ITEMS #6)".
  - Any new placeholder / tooltip / inline label that leaks an
    internal variable name.

The standing rule the user has reinforced multiple times: **NEVER ship
a UI surface with snake_case visible text. NEVER ship a UI surface
with internal-tracker references like "(Item 5c)".** This guardrail
enforces that on the entire template tree, statically — no need to
render the page or hit the route. If you intentionally need a token
that matches one of the patterns (very rare — e.g. a code example
shown in a `<code>` block), add it to the explicit allowlist below
WITH a justification in the comment.
"""
from __future__ import annotations

import os
import re
from typing import List, Tuple


def _template_root() -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "templates")
    )


# ---------------------------------------------------------------------------
# 1. Strip everything that is NOT visible text from a template.
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_JINJA_COMMENT_RE = re.compile(r"\{#[\s\S]*?#\}")
# Lazy match `[\s\S]*?` — needed because Jinja format-string expressions
# embed literal `}` chars (e.g. `{{ "{:+.1f}".format(x.foo) }}`), so a
# `[^}]*` non-greedy class would stop too early. Lazy any-char matches
# minimally to the first `}}` / `%}`, which is the actual closer.
_JINJA_EXPR_RE = re.compile(r"\{\{[\s\S]*?\}\}")
_JINJA_TAG_RE = re.compile(r"\{%[\s\S]*?%\}")
# Strip values of attributes that legitimately carry snake_case
# identifiers (class names, IDs, form field names, etc.) — those are
# not visible text. ALSO strip the `value` attribute of <option>,
# since the displayed text is the inner content, not the value.
_ATTR_RE = re.compile(
    r'\s+(class|id|value|name|for|type|placeholder|data-[\w-]+|'
    r'aria-[\w-]+|role|method|action|src|href|alt|title|target|'
    r'rel|charset|lang|http-equiv|content|property|http|wire:)='
    r'(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)
# `<code>...</code>` and `<pre>...</pre>` blocks legitimately render
# code (with snake_case). Strip them so the test doesn't false-positive
# on intentional code samples in docs.
_CODE_RE = re.compile(r"<code[\s\S]*?</code>", re.IGNORECASE)
_PRE_RE = re.compile(r"<pre[\s\S]*?</pre>", re.IGNORECASE)


def _strip_to_visible_text(html: str) -> str:
    html = _SCRIPT_RE.sub("", html)
    html = _STYLE_RE.sub("", html)
    html = _CODE_RE.sub("", html)
    html = _PRE_RE.sub("", html)
    html = _HTML_COMMENT_RE.sub("", html)
    html = _JINJA_COMMENT_RE.sub("", html)
    html = _JINJA_EXPR_RE.sub("", html)
    html = _JINJA_TAG_RE.sub("", html)
    html = _ATTR_RE.sub("", html)
    return html


# ---------------------------------------------------------------------------
# 2. Patterns that constitute a leak.
# ---------------------------------------------------------------------------

# Snake_case word: 2+ lowercase letters, underscore, 2+ lowercase
# letters. Trailing underscores allowed (e.g. `foo_bar_baz`). Boundary-
# anchored to avoid stripping mid-word matches in legitimate text like
# "long-running" (kebab-case is fine; only underscores are forbidden).
_SNAKE_CASE_RE = re.compile(r"\b[a-z]{2,}(?:_[a-z]{2,})+\b")

# Internal-tracker references like "(Item 5c)" or "(OPEN_ITEMS #6)" or
# "(W3.)". These are commit-message / planning-doc identifiers that
# have no place in user-facing copy.
_TRACKER_RE = re.compile(
    r"\((?:Item\s+\w+|OPEN_ITEMS[^)]*|W\d[^)]*)\)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 3. Allowlists — keep TIGHT. Prefer fixing the template over allowlisting.
# ---------------------------------------------------------------------------

# Tokens that match _SNAKE_CASE_RE but are deliberately rendered as
# code (technical labels users explicitly need to see). Each entry
# MUST have a comment explaining why allowlisting is correct.
_SNAKE_CASE_ALLOWLIST = {
    # (no entries yet — every leak should be fixed at the template level)
}


# ---------------------------------------------------------------------------
# 4. The actual scan.
# ---------------------------------------------------------------------------

def _visible_text_segments(html: str) -> List[Tuple[int, str]]:
    """Return (line_num, text) tuples for visible text segments.

    Visible text lives between `>` and `<`. The regex MUST run across
    the whole document with DOTALL so multi-line paragraph text is
    captured — earlier per-line scanning missed any visible text whose
    `>...<` pair straddled a newline (e.g. block-paragraph copy in
    `<small>` / `<p>` tags), which silently let `pos_pct = avg_position
    ÷ current_capital` slip through the static guardrail."""
    stripped = _strip_to_visible_text(html)
    segments: List[Tuple[int, str]] = []
    for m in re.finditer(r">([^<]+)<", stripped, flags=re.DOTALL):
        text = m.group(1).strip()
        if text:
            line_num = stripped[:m.start()].count("\n") + 1
            segments.append((line_num, text))
    return segments


def _walk_templates() -> List[str]:
    paths: List[str] = []
    for root, _, files in os.walk(_template_root()):
        for fn in files:
            if fn.endswith(".html"):
                paths.append(os.path.join(root, fn))
    return paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNoSnakeCaseInTemplateVisibleText:
    """Static scan: every .html in templates/ must have zero snake_case
    tokens in visible text. Allowlist via _SNAKE_CASE_ALLOWLIST only."""

    def test_no_snake_case_visible_text(self):
        leaks: List[str] = []
        for path in _walk_templates():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for line_num, text in _visible_text_segments(content):
                for m in _SNAKE_CASE_RE.finditer(text):
                    token = m.group(0)
                    if token in _SNAKE_CASE_ALLOWLIST:
                        continue
                    rel = os.path.relpath(path, _template_root())
                    leaks.append(
                        f"{rel}:{line_num}  {token!r}  in {text!r}"
                    )
        assert not leaks, (
            "snake_case visible in template text — these leak internal "
            "identifiers to users:\n  "
            + "\n  ".join(leaks)
            + "\n\nFix the template (replace `foo_bar` with 'Foo Bar' "
            "in the visible text). DO NOT widen the allowlist unless "
            "absolutely required."
        )


class TestNoInternalTrackerReferencesInTemplates:
    """Internal-tracker references like '(Item 5c)' or '(OPEN_ITEMS #6)'
    are commit-message / planning artifacts. They have no place in any
    user-visible text — header, label, tooltip, anything."""

    def test_no_item_or_open_items_references(self):
        leaks: List[str] = []
        for path in _walk_templates():
            with open(path, encoding="utf-8") as f:
                content = f.read()
            for line_num, text in _visible_text_segments(content):
                for m in _TRACKER_RE.finditer(text):
                    rel = os.path.relpath(path, _template_root())
                    leaks.append(
                        f"{rel}:{line_num}  {m.group(0)!r}  in {text!r}"
                    )
        assert not leaks, (
            "Internal tracker references in template visible text — "
            "these leak commit-message / planning-doc identifiers to "
            "users:\n  "
            + "\n  ".join(leaks)
            + "\n\nRemove '(Item 5c)' / '(OPEN_ITEMS #6)' / '(W3.)' "
            "from the displayed header / label. Keep them in HTML / "
            "Jinja comments if you need the cross-reference."
        )
