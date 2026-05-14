"""Structural guardrail: every `severity=` value emitted by
production code is in the set the templates know how to render.

The bug class.
A new code path emits `severity="urgent"` (or "severe", "catastrophic",
"warning", etc.). The templates' if/elif ladder for severity styling
only tests against `high`, `critical`, `medium`. Anything else falls
through to the muted "LOW" rendering. Result:
  - A `severity="catastrophic"` risk-stress alert renders as "LOW"
  - A `severity="severe"` stress-test scenario renders as "LOW"
  - A `severity="urgent"` event flag renders as "LOW"
The operator sees the alert in the activity feed but it's styled
identically to a low-priority filing notice — they ignore it.

Two sub-tests:
  1. STATIC: every string literal assigned to `severity=` (kwarg) or
     `"severity": "..."` (dict literal) in production code is in
     KNOWN_SEVERITIES.
  2. CROSS-CHECK: every value in KNOWN_SEVERITIES has a corresponding
     branch in the template severity-styling ladder. If the operator
     adds `severity="warning"` to KNOWN_SEVERITIES but never updates
     the template, the test surfaces the mismatch.

Acceptable patterns:
  1. Severity literal is in KNOWN_SEVERITIES → ok
  2. Severity literal is in INTENTIONALLY_UNRENDERED with rationale
     (e.g., emitted only to logs / DB, never displayed)
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import List, Optional, Set, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Severity values that templates know how to render. Sourced from
# `templates/ai.html` (the only template that branches on severity)
# and the SEC-filings sev_order map (`low/medium/high/critical`).
# The Jinja `if e.severity == 'high' or e.severity == 'critical'`
# ladder treats `high` and `critical` identically; the JS in the
# scrolled SEC alerts table treats `medium` distinctly; everything
# else falls through to the muted LOW styling.
#
# The full set of legitimate severities (post-2026-05-14 audit):
KNOWN_SEVERITIES: Set[str] = {
    "low",
    "medium",
    "high",
    "critical",
    # Stress-scenario severities — these are produced by
    # `risk_stress_scenarios.py` and surface in the stress-test
    # report only (no template renders them as alerts; they're
    # plotted as columns). They MUST stay distinct from the alert
    # severities because the stress report uses them as labels.
    "moderate",
    "severe",
    "catastrophic",
}


# Severities emitted by production code that are intentionally
# never rendered by templates. Each entry needs written rationale.
# These are typically values logged to DB or returned in API JSON
# but never surfaced in HTML alert rendering.
INTENTIONALLY_UNRENDERED: dict = {
    "moderate":
        "Stress-scenario severity only. Surfaced in the stress-test "
        "report as a label, never as an alert badge.",
    "severe":
        "Stress-scenario severity only. Surfaced in the stress-test "
        "report as a label, never as an alert badge.",
    "catastrophic":
        "Stress-scenario severity only. Surfaced in the stress-test "
        "report as a label, never as an alert badge.",
}


# Production source files. Excludes tests, vendor, scripts.
def _walk_critical_path_files() -> List[str]:
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (
            "venv", "__pycache__", ".git", ".claude", "tests",
            "exports", "backups", "logs", "altdata", "node_modules",
            "docs",
        )]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f.startswith("test_"):
                continue
            out.append(os.path.join(root, f))
    return out


def _string_value(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _walk_severity_assignments(tree: ast.AST) -> List[Tuple[int, str, str]]:
    """Return (lineno, kind, value) for every literal severity emit:
      - dict literal {"severity": "..."}
      - kwarg severity="..." in a Call
      - subscript d["severity"] = "..."
    Skips dynamic expressions (variables, function calls, ternaries).
    Skips `severity` SQL fragments / docstring examples by requiring
    the value match a simple identifier shape.
    """
    out = []
    for node in ast.walk(tree):
        # dict literal {"severity": "..."}
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant)
                        and k.value == "severity"):
                    s = _string_value(v)
                    if s is not None:
                        out.append((k.lineno, "dict", s))
        # call kwarg severity="..."
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "severity":
                    s = _string_value(kw.value)
                    if s is not None:
                        out.append((kw.value.lineno, "kwarg", s))
        # subscript d["severity"] = "..."
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                slice_node = target.slice
                if (isinstance(slice_node, ast.Constant)
                        and slice_node.value == "severity"):
                    s = _string_value(node.value)
                    if s is not None:
                        out.append((target.lineno, "subscript", s))
    return out


# Regex for the template severity-comparison ladder. Captures the
# string each branch tests for, so we can verify cross-coverage.
_TEMPLATE_SEVERITY_RE = re.compile(
    r"severity\s*(?:==|===)\s*['\"]([a-z_]+)['\"]"
)


def _template_recognized_severities() -> Set[str]:
    """Scan the templates dir for severity == '...' comparisons and
    return the set of severity values the templates branch on. All
    others fall through to the catch-all (muted/LOW) style."""
    out: Set[str] = set()
    tpl_dir = os.path.join(REPO_ROOT, "templates")
    if not os.path.isdir(tpl_dir):
        return out
    for root, _, files in os.walk(tpl_dir):
        for f in files:
            if not f.endswith(".html"):
                continue
            with open(os.path.join(root, f)) as fh:
                src = fh.read()
            for m in _TEMPLATE_SEVERITY_RE.finditer(src):
                out.add(m.group(1).lower())
    return out


class TestAlertSeverityRendering:
    """Two-prong check: (1) every emitted severity is known;
    (2) every alert-style severity has a template branch."""

    def test_all_emitted_severities_are_known(self):
        violations: List[Tuple[str, int, str, str]] = []
        for src_path in _walk_critical_path_files():
            rel = os.path.relpath(src_path, REPO_ROOT)
            try:
                with open(src_path) as fh:
                    src = fh.read()
            except Exception:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for lineno, kind, value in _walk_severity_assignments(tree):
                if value in KNOWN_SEVERITIES:
                    continue
                violations.append((rel, lineno, kind, value))

        if violations:
            details = "\n".join(
                f"  {rel}:{lineno}  ({kind}) severity={value!r}"
                for rel, lineno, kind, value in violations
            )
            pytest.fail(
                f"{len(violations)} sites emit a `severity=...` value "
                f"not in KNOWN_SEVERITIES. Templates branch on a "
                f"closed set of severity strings; unknown values fall "
                f"through to the muted LOW styling — operators ignore "
                f"the alert.\n\nViolations:\n{details}\n\n"
                f"Fix one of:\n"
                f"  1. Use one of the existing KNOWN_SEVERITIES values "
                f"(low/medium/high/critical/moderate/severe/catastrophic)\n"
                f"  2. If a new severity tier is genuinely needed, "
                f"add to KNOWN_SEVERITIES AND update every template's "
                f"severity ladder (templates/*.html `{{% if "
                f"e.severity == ... %}}`)\n"
                f"  3. If the value is internal-only (logs/DB/API JSON) "
                f"and never rendered, add to INTENTIONALLY_UNRENDERED "
                f"with rationale"
            )

    def test_alert_severities_have_template_branch(self):
        """Every severity value that's CONSUMED as an alert by
        templates must have a matching branch in the severity ladder.

        Allowlist: severities in INTENTIONALLY_UNRENDERED are
        skipped (they're not consumed as alerts)."""
        recognized = _template_recognized_severities()
        # `low` is the implicit fallback in the existing template
        # ladder (the `else` branch renders LOW). Treat it as
        # recognized even though there's no explicit branch.
        recognized.add("low")
        gaps = []
        for sev in KNOWN_SEVERITIES:
            if sev in INTENTIONALLY_UNRENDERED:
                continue
            if sev not in recognized:
                gaps.append(sev)
        if gaps:
            details = "\n".join(f"  {s}" for s in sorted(gaps))
            pytest.fail(
                f"{len(gaps)} severity values in KNOWN_SEVERITIES "
                f"have no matching branch in the template severity "
                f"ladder — they will fall through to the muted "
                f"fallback styling.\n\nMissing template branches:\n"
                + details + "\n\nFix:\n"
                f"  - Add a `{{% elif e.severity == '<sev>' %}}` "
                f"branch to templates/ai.html with appropriate "
                f"styling\n"
                f"  - OR: if the severity is internal-only and "
                f"never rendered as an alert, add to "
                f"INTENTIONALLY_UNRENDERED with rationale"
            )
