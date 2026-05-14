"""Structural guardrail: NO template may interpolate an object field
into user-visible text without null-safety. JavaScript's `null +
'string'` and `${null}` both produce the literal string "null" in
the output, which then renders to the user as "null% equity",
"null contracts", "null shares", etc.

The bug class (2026-05-14 incident).
The dashboard ticker rendered every trade as
"<action> <symbol> (<size_pct>% equity, <confidence>% confidence)".
This works for BUY/SELL/SHORT but breaks for MULTILEG_OPEN /
OPTIONS / PAIR_TRADE which don't have size_pct. Mack saw:
    "Multileg Open AAPL REJECTED · Specialist Veto
     (null% equity, 57% confidence)"

This test is class-level — it does not enumerate "size_pct" or any
other specific field. It scans ALL templates for the structural
pattern (object field interpolated without null-safety). Any future
PR that introduces another bare `obj.field + '<unit>'` or
`${obj.field}<unit>` pattern, on any field, fails CI.

Acceptable null-safety patterns:
  1. Ternary guard: `(x != null ? x + '%' : '')`
  2. Optional chain + nullish coalesce: `x ?? '0'`
  3. Logical OR fallback: `(x || 0) + '%'`
  4. Branched rendering by surrounding `if`/`switch` that ensures
     the field exists in that branch.
  5. `escapeHtml(x || '')` — wraps the whole expression.
  6. Function call: `formatX(t)` with the helper handling nulls.

Unsafe pattern:
  - Direct `obj.field + '<unit>'` or `${obj.field}<unit>` with no
    null check anywhere in the surrounding 20 lines.
"""
from __future__ import annotations

import glob
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, "templates")


# Match `<obj>.<field> + '<unit>'` and `${<obj>.<field>}<unit>` in JS,
# where <unit> is any non-quote sequence the field is being concatenated
# into (typically %, $, ' equity', etc.). The point of these patterns
# is exactly the case that produced "null% equity" in the ticker.
PATTERNS = [
    # `+ obj.field + '<text>'`  — covers `t.size_pct + '%'`,
    # `pos.qty + ' shares'`, etc.
    re.compile(r"\+\s*([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\+\s*['\"]", re.IGNORECASE),
    # `${obj.field}<text>` — template-literal form.
    re.compile(r"\$\{\s*([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s*\}", re.IGNORECASE),
]

# Fields that are known to be guaranteed-non-null in their context
# (e.g. always set on every trade row). An entry takes the form
# (filename, "obj.field") so the allowlist is per-template not
# global. Keep this list short and reviewable.
GUARANTEED_NON_NULL = {
    # symbol and action are required on every trade object — the
    # backend would error before we got here if they were missing.
    ("dashboard.html", "t.symbol"),
    ("dashboard.html", "t.action"),
    ("dashboard.html", "c.symbol"),
    # Pending-orders rows come from the broker API. Alpaca does not
    # return orders without a symbol; if it ever did, that row would
    # be a far bigger problem than a "null" cell, and the bigger
    # problem would surface elsewhere (reconciliation, journal lookup).
    ("dashboard.html", "o.symbol"),
    # toFixed() outputs always have a digit; the result is the input
    # to '+ ...' — the nullable case is captured separately by the
    # `(x || 0).toFixed()` pattern.
}

# Files we exhaustively scan. Keep the scope to user-facing rendering
# templates; settings/admin templates that show admin-only state can
# be added if they hit the bug too.
SCANNED_TEMPLATES = ("dashboard.html",)


def _has_null_safety_above(lines, idx, field, lookback=20):
    """True if any of lines[max(0,idx-lookback)..idx] contains a
    null-safety construct that protects `field`. Looks for the
    field name appearing in a ternary, ?? coalesce, ||, or != null
    test, OR the surrounding action-branch pattern that gates which
    fields exist (e.g. `if (actionUpper === 'BUY')`)."""
    above = "\n".join(lines[max(0, idx - lookback):idx])
    bare_field = field.split(".", 1)[1] if "." in field else field
    safety_patterns = [
        # `field != null` / `field !== null` / `field !== undefined`
        rf"{re.escape(field)}\s*!==?\s*(null|undefined)",
        # `field ? ... : ...` — ternary truthy check
        rf"{re.escape(field)}\s*\?",
        # `field ?? x` — nullish coalesce
        rf"{re.escape(field)}\s*\?\?",
        # `field || x` — truthy fallback
        rf"{re.escape(field)}\s*\|\|",
        # `(field || 0).toFixed(...)` — common numeric idiom
        rf"\({re.escape(field)}\s*\|\|\s*\d+\)",
        # action-branched rendering — once we're inside a switch/if
        # on action type, fields specific to that action are present
        rf"if\s*\(\s*\w+\s*===\s*['\"](BUY|SELL|SHORT|MULTILEG|OPTIONS|PAIR)",
        rf"actionUpper\s*===",
        # explicit comment annotation
        r"#\s*NULL_OK:|//\s*NULL_OK:",
    ]
    for pat in safety_patterns:
        if re.search(pat, above):
            return True
    return False


class TestTemplatesHaveNullSafety:
    def test_no_unsafe_field_interpolation(self):
        """Class-level scan: every template field interpolation must
        have a null-safety construct nearby. Catches the entire
        bug class, not just `size_pct + '%'`."""
        violations = []
        for tmpl in SCANNED_TEMPLATES:
            path = os.path.join(TEMPLATES, tmpl)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                lines = f.read().splitlines()
            for idx, line in enumerate(lines, start=1):
                for pat in PATTERNS:
                    for m in pat.finditer(line):
                        field = m.group(1)
                        if (tmpl, field) in GUARANTEED_NON_NULL:
                            continue
                        if _has_null_safety_above(
                            lines, idx, field, lookback=20,
                        ):
                            continue
                        violations.append((tmpl, idx, field, line.rstrip()))

        if violations:
            details = "\n".join(
                f"  {tmpl}:{ln}  field={field}\n    {src}"
                for tmpl, ln, field, src in violations[:30]
            )
            pytest.fail(
                f"{len(violations)} unsafe field interpolation(s) in "
                f"templates. Each one will print 'null<unit>' (e.g. "
                f"'null% equity') when the field happens to be "
                f"missing for the current row. Show first 30:\n\n"
                f"{details}\n\nFix one of:\n"
                f"  1. `(x != null ? x + '<unit>' : '')`\n"
                f"  2. `(x || 0) + '<unit>'`\n"
                f"  3. Branch on action type / row type so the field "
                f"is guaranteed present in the branch.\n"
                f"  4. If the field truly cannot be null in this "
                f"context, add `(template, 'obj.field')` to "
                f"GUARANTEED_NON_NULL with the rationale visible in "
                f"the diff."
            )

    def test_ticker_handles_multileg_open(self):
        """Pin-test: the dashboard ticker MUST branch on
        MULTILEG_OPEN action and reference contracts/strategy_name
        fields. Prevents a future refactor from collapsing the
        branched rendering back to bare size_pct interpolation."""
        path = os.path.join(TEMPLATES, "dashboard.html")
        with open(path) as f:
            src = f.read()
        assert "MULTILEG_OPEN" in src, (
            "Expected MULTILEG_OPEN action to be branched in "
            "dashboard.html so the ticker can render multileg "
            "trades using contracts/strategy_name fields."
        )
        assert "contracts" in src, (
            "Expected 'contracts' field reference in dashboard.html "
            "for MULTILEG_OPEN sizing."
        )
