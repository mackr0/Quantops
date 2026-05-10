"""Cross-cutting guardrail: every templates/*.html must be reachable.

Caught 2026-05-10 (Issue 12): four templates (`ai_brain.html`,
`ai_strategy.html`, `ai_awareness.html`, `ai_operations.html`) had
been orphaned for months. They had no `render_template(...)` call
anywhere in `views.py` and weren't extended/included by any other
template. They drifted independently — `ai_awareness.html` even
referenced API fields that don't exist (`fred_indicators`,
`etf_flows.net_flow`) — and would have shipped broken code if
anyone tried to revive them.

This test prevents future orphan templates by enforcing reachability:
every `.html` in `templates/` must be either:
  1. Rendered by `render_template("name.html", ...)` somewhere in
     a non-test .py file, OR
  2. Extended/included by another template via `{% extends %}` or
     `{% include %}`, OR
  3. Explicitly allowlisted in this test with a documented reason
     (e.g., a base template for a deferred feature).

A template that isn't reachable is dead code — it confuses future
maintainers and silently rots when downstream APIs change.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")


# Templates allowed to exist without a renderer/includer.
# Add only with rationale; should be empty by default.
ALLOWLIST: dict = {
    # Example format:
    # "deferred_panel.html": "Scaffolded for Q3 feature; tracker INGEST-123",
}


def _every_template_filename():
    """Recursively list every .html file under templates/."""
    out = []
    for root, _, files in os.walk(TEMPLATES_DIR):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            rel = os.path.relpath(os.path.join(root, fname), TEMPLATES_DIR)
            # Use forward-slash form regardless of OS so the
            # `render_template("subdir/file.html")` match is consistent.
            out.append(rel.replace(os.sep, "/"))
    return out


def _scan_for_template_use(template_name):
    """Return True iff `template_name` is referenced by:
      - render_template("...") OR render_template_string('{% import ... %}')
        in any .py file outside tests/, or
      - {% extends %} / {% include %} / {% import %} / {% from ... import %}
        in any .html file (Jinja import is the macro-bringing convention,
        easy to miss).
    """
    # Patterns that count as "this template is used":
    #   render_template("name.html")           — Flask render
    #   {% extends "name.html" %}              — template inheritance
    #   {% include "name.html" %}              — partial include
    #   {% import "name.html" as x %}          — bring macros
    #   {% from "name.html" import macro %}    — bring specific macro
    # The Jinja patterns may appear inside Python strings (passed to
    # render_template_string) so we scan .py files with the same
    # patterns too.
    use_re = re.compile(
        r'(?:render_template\s*\(\s*|'
        r'\{%\s*(?:extends|include|import)\s*|'
        r'\{%\s*from\s*)["\']' + re.escape(template_name) + r'["\']'
    )

    # 1. Python files (non-test code only — render_template calls or
    # render_template_string with Jinja imports embedded).
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in
                   ("venv", ".git", "__pycache__", "tests",
                    "node_modules", "exports", "docs")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path) as f:
                    if use_re.search(f.read()):
                        return True
            except OSError:
                continue

    # 2. Other templates' extends / include / import / from-import
    for root, _, files in os.walk(TEMPLATES_DIR):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            path = os.path.join(root, fname)
            with open(path) as f:
                if use_re.search(f.read()):
                    return True
    return False


def test_no_orphan_templates():
    """Every templates/*.html must be reachable. Orphans accumulate
    silently and drift away from the live code. Add to ALLOWLIST
    only when a template is intentionally scaffolded for a deferred
    feature with a tracker reference."""
    orphans = []
    for tmpl_name in _every_template_filename():
        if tmpl_name in ALLOWLIST:
            continue
        if not _scan_for_template_use(tmpl_name):
            orphans.append(tmpl_name)
    assert not orphans, (
        "Found orphan template(s) — no `render_template(...)` call "
        "and no `{% extends %}` / `{% include %}` reference. Either:\n"
        "  - Render or include the template (verify it's actually "
        "used), or\n"
        "  - Delete the template (orphans drift and break silently), "
        "or\n"
        "  - Add the template name to ALLOWLIST in this test with a "
        "tracker reference if it's intentionally scaffolded for "
        "future use.\n\n"
        "Orphans:\n  " + "\n  ".join(sorted(orphans))
    )
