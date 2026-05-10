"""Cross-cutting guardrail: no inline JS reimplementation of server-side
formatters in templates.

Caught 2026-05-10 (Issue 13): `templates/dashboard.html` had two
inline JS functions that duplicated server-side formatters from
`display_names.py`:

  - `function humanizeJs(s)` re-implemented `display_names.humanize()`.
    If anyone added a custom mapping to `_DISPLAY_NAMES`, the JS
    silently drifted (rendered "Stop Limit" instead of the
    operator-preferred "Stop-Limit", etc.).
  - `function formatTimestamp(ts)` re-implemented `friendly_time()`
    with its own absolute/relative time logic.

Both were replaced by server-provided pre-formatted fields
(`o.order_type_label`, `entry.timestamp_friendly`). This test
prevents the same shape from being re-introduced: any future
template that defines a JS function whose name matches a known
server-side formatter fails the build.

The fix path when this test fires: enrich the API response
server-side with a pre-formatted label field, and have the JS
render that field directly. Don't bring back the duplicate JS
function.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "templates",
)


# JS function names that re-implement server-side formatters or
# duplicate the shared static/js/format.js helpers. Each maps to its
# canonical replacement so the error message tells the dev exactly
# what to use instead.
FORBIDDEN_JS_FORMATTERS = {
    "humanizeJs": "display_names.humanize() — server-side; return a "
                   "<field>_label in the API response",
    "humanize_js": "display_names.humanize() — server-side; return a "
                    "<field>_label in the API response",
    "formatTimestamp": "display_names.friendly_time() — server-side; "
                        "return a <field>_friendly in the API response",
    "format_timestamp": "display_names.friendly_time() — server-side; "
                         "return a <field>_friendly in the API response",
    "friendlyTime": "display_names.friendly_time() — server-side",
    "friendly_time": "display_names.friendly_time() — server-side",
    "displayName": "display_names.display_name() — server-side; "
                    "return a <field>_label in the API response",
    "display_name": "display_names.display_name() — server-side",
}

# Function NAME PREFIXES that strongly suggest a duplicate price /
# number formatter (e.g., `fmt`, `fmt0`, `fmt2`, `formatPrice`,
# `formatDollar`). These match the structural pattern of the bug
# class — multiple inline `function fmt(n)` declarations across
# templates with inconsistent conventions (Issue 13). Use the shared
# `static/js/format.js` (window.QF.*) instead.
FORBIDDEN_PREFIXES = (
    "fmt",        # `function fmt`, `function fmt0`, `function fmt2`...
    "format",     # `function formatPrice`, `formatDollar`, etc.
)


# Per-template allowlist — `(template_path, function_name)`. Add only
# when the function is genuinely NOT duplicating a price/number
# formatter (e.g., it formats minutes:seconds for a countdown, not
# a dollar amount). Each entry needs a comment with rationale.
ALLOWLIST: dict = {
    # dashboard.html scan-countdown formatter — formats Mm:Ss timer,
    # not a price. Wrapping in QF.* would be silly (it's not a
    # number-formatter at all; it's a countdown-state-machine).
    ("dashboard.html", "fmt"): (
        "scan countdown formatter (mm:ss + 'Scanning...' / 'NOW' "
        "states) — not a price/number formatter; safe."
    ),
}


# Match `function name(...)` declarations
_FN_DEF_RE = re.compile(
    r"\bfunction\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
)


_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", flags=re.DOTALL)


def _strip_js_comments(body):
    """Remove `// ...` and `/* ... */` comments without changing line
    counts (replace with spaces so the line-number arithmetic stays
    accurate)."""
    def _line_replace(m):
        return " " * len(m.group(0))
    def _block_replace(m):
        # Preserve newlines so line numbers don't shift
        return "".join(c if c == "\n" else " " for c in m.group(0))
    body = _LINE_COMMENT_RE.sub(_line_replace, body)
    body = _BLOCK_COMMENT_RE.sub(_block_replace, body)
    return body


def _scripts_in_template(text):
    """Yield each <script>...</script> body in the template, with JS
    comments stripped. Stripping prevents false positives where a
    forbidden function name appears inside a `// removed` comment.
    Returns (start_offset_in_text, body_text_with_comments_blanked)."""
    for m in re.finditer(
        r"<script\b[^>]*>(.*?)</script>", text,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        yield m.start(1), _strip_js_comments(m.group(1))


def test_no_inline_js_formatter_definitions_in_templates():
    """No template may define a JS function whose name matches a
    known server-side formatter. Returns the API a `<field>_label`
    pre-formatted server-side instead.

    Adding to FORBIDDEN_JS_FORMATTERS is how you extend coverage
    when a new server-side formatter is introduced — match the JS
    name to the Python helper so future drift fails the build."""
    leaks = []
    for root, _, files in os.walk(TEMPLATES_DIR):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, TEMPLATES_DIR)
            with open(path) as f:
                text = f.read()
            for script_start, script_body in _scripts_in_template(text):
                for fn_match in _FN_DEF_RE.finditer(script_body):
                    fn_name = fn_match.group(1)
                    canonical = None
                    # Layer 1 — exact-name match against named server-side
                    # formatters
                    if fn_name in FORBIDDEN_JS_FORMATTERS:
                        canonical = FORBIDDEN_JS_FORMATTERS[fn_name]
                    # Layer 2 — prefix match (`fmt`, `format`) catches
                    # the structural pattern of price-formatter
                    # duplication
                    elif any(fn_name.startswith(p) for p in FORBIDDEN_PREFIXES):
                        canonical = (
                            "static/js/format.js helpers (window.QF.*) — "
                            "use QF.dollars2 / QF.dollars0 / QF.signedDollars0 "
                            "/ QF.intCommas / QF.percent / QF.signedPct"
                        )
                    if canonical is None:
                        continue
                    if (rel, fn_name) in ALLOWLIST:
                        continue
                    abs_offset = script_start + fn_match.start()
                    line_no = text[:abs_offset].count("\n") + 1
                    leaks.append(
                        f"  templates/{rel}:{line_no} — "
                        f"`function {fn_name}(...)` re-implements a "
                        f"shared formatter. Use: {canonical}."
                    )
    assert not leaks, (
        "Found inline JS function definitions that re-implement "
        "server-side formatters from display_names.py. JS-side "
        "duplicates silently drift when the server's formatter is "
        "extended. Pipe a pre-formatted `<field>_label` field through "
        "the API response and have the JS render it directly.\n\n"
        + "\n".join(leaks)
    )
