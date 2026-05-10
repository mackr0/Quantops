"""Pin humanization of dynamic snake_case fields in templates.

Caught 2026-05-09: dashboard.html / trades.html / ai.html /
ai_strategy.html rendered raw `decision_type`, `action_taken`,
`ai_signal`, `market_type` cells WITHOUT piping through `humanize`.
Result: cells displayed `STRONG_BUY`, `bull_put_spread`,
`small_cap_shorts`, `MULTILEG_OPEN` to the user instead of
"Strong Buy", "Bull Put Spread", "Small Cap Shorts", "Multileg Open".
Same bug class as the prior `insufficient_history` slippage leak.

This test pins:
1. Behavioral: rendering the template snippet for a dynamic field
   with a raw snake_case value produces humanized output.
2. Cross-cutting (AST/regex): any template render of a known-dynamic
   field name MUST pipe through `humanize` (or `display_name` / a few
   other allowed humanizing filters). Closed allowlist of fields —
   add to it as new dynamic fields are introduced.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "templates",
)


# ---------------------------------------------------------------------------
# Layer 1 — behavioral: humanize actually transforms the cells we fixed
# ---------------------------------------------------------------------------


@pytest.fixture
def jinja_env():
    """Real Jinja env with the project's filters wired up."""
    from app import create_app
    app = create_app()
    return app.jinja_env


class TestHumanizeRendersDynamicFields:
    def test_dashboard_decision_type_humanized(self, jinja_env):
        """`{{ d.decision_type | humanize }}` turns raw STRONG_BUY into
        'Strong Buy'. If a future refactor drops the filter, this fails."""
        tmpl = jinja_env.from_string(
            "{{ d.decision_type | humanize }}"
        )
        out = tmpl.render(d={"decision_type": "STRONG_BUY"})
        assert out == "Strong Buy", out

    def test_dashboard_action_taken_humanized(self, jinja_env):
        tmpl = jinja_env.from_string(
            "{{ (d.action_taken | humanize) if d.action_taken else '--' }}"
        )
        out = tmpl.render(d={"action_taken": "bull_put_spread"})
        assert out == "Bull Put Spread", out
        # Empty case still renders the placeholder
        out = tmpl.render(d={"action_taken": None})
        assert out == "--", out

    def test_trades_ai_signal_humanized(self, jinja_env):
        tmpl = jinja_env.from_string(
            "{{ d.ai_signal | humanize }}"
        )
        out = tmpl.render(d={"ai_signal": "MULTILEG_OPEN"})
        assert out == "Multileg Open", out

    def test_market_type_humanized(self, jinja_env):
        """market_type values are snake_case slugs (small_cap_shorts,
        options_earnings, etc.) — must not render raw."""
        tmpl = jinja_env.from_string("{{ v.market_type | humanize }}")
        out = tmpl.render(v={"market_type": "small_cap_shorts"})
        assert out == "Small Cap Shorts", out

    def test_account_status_humanized(self, jinja_env):
        """Alpaca account.status: humanize transforms multi-token forms
        like ACCOUNT_RESTRICTED. Single tokens (ACTIVE) pass through
        — humanize only acts on snake_case / UPPER_SNAKE patterns,
        and ACTIVE is already short/readable enough not to need it."""
        tmpl = jinja_env.from_string(
            "{{ prof.account.status | humanize }}"
        )
        assert (
            tmpl.render(prof={"account": {"status": "ACCOUNT_RESTRICTED"}})
            == "Account Restricted"
        )
        # Single-word values pass through unchanged — by design.
        assert (
            tmpl.render(prof={"account": {"status": "ACTIVE"}}) == "ACTIVE"
        )


# ---------------------------------------------------------------------------
# Layer 2 — guardrail: known-dynamic fields must always pipe through a
# humanizing filter
# ---------------------------------------------------------------------------


# These fields hold dynamic snake_case-or-UPPER_SNAKE values that come
# from the LLM, the prediction recorder, the market_type slug system,
# or third-party APIs (Alpaca status). Rendering them raw shows the
# user values like `STRONG_BUY` / `bull_put_spread` / `small_cap_shorts`.
# Add a field to this allowlist when you introduce one — that's the
# enforcement point.
DYNAMIC_FIELDS = {
    "decision_type",
    "action_taken",
    "ai_signal",
    "predicted_signal",
    "prediction_type",
    "market_type",
    "exit_trigger",
    "veto_rule",
    "regime",
    "strategy_type",
}

# Filters that produce human-readable output. If any of these is in the
# pipeline, the field is considered humanized.
HUMANIZING_FILTERS = {
    "humanize",
    "display_name",
    "title",  # |title is acceptable for already-Title-Case sources
}

# Files whose templates we intentionally don't gate (e.g., admin debug
# pages, raw-data downloads, json blob viewers).
EXEMPT_TEMPLATES: set = set()


# Match `{{ ... .<field>[ filters... ] }}`. We want the filter chain
# (everything after the field, before the closing `}}`).
_RENDER_RE = re.compile(
    r"\{\{\s*([^{}]+?)\s*\}\}",
    re.DOTALL,
)


def _filter_chain_humanizes(expr_after_field: str) -> bool:
    """Given the substring AFTER `<field>`, look for `| humanize`,
    `| display_name`, `| title` anywhere in the filter chain."""
    # Strip default expressions like `... or '--'` and arithmetic so we
    # only consider the filter pipeline. Filters appear as `| name`.
    filters_in_chain = re.findall(r"\|\s*([a-zA-Z_][a-zA-Z0-9_]*)",
                                   expr_after_field)
    return any(f in HUMANIZING_FILTERS for f in filters_in_chain)


def _is_predicate_or_slug(expr_before_field, full_text, match_abs_start):
    """True if the field is being used as a predicate (e.g.
    `if 'BUY' in d.ai_signal`) or as an HTML-attribute slug
    (e.g. `action="/path/{{ d.strategy_type }}"`), neither of
    which renders user-visible text."""
    # Predicate use: `in `, `==`, `!=`, `<`, `>` immediately before.
    # Look at the last ~40 chars before the field name.
    tail = expr_before_field[-40:].strip()
    if re.search(r"\b(in|not\s+in|==|!=|<=|>=|<|>)\s*\(?\s*$", tail):
        return True
    # HTML attribute slug: the {{...}} expression sits inside an
    # unclosed `attr="..."` in the surrounding template.
    head = full_text[max(0, match_abs_start - 200):match_abs_start]
    if re.search(r'[a-zA-Z\-:]+\s*=\s*"[^"]*$', head):
        return True
    return False


def _scan_template(path):
    """Yield (line_no, field_name, full_render_expr) for each render
    in `path` that references a DYNAMIC_FIELDS field WITHOUT a
    humanizing filter, AND isn't a predicate or HTML-attribute slug."""
    with open(path) as f:
        text = f.read()
    leaks = []
    for m in _RENDER_RE.finditer(text):
        expr = m.group(1)
        line_no = text.count("\n", 0, m.start()) + 1
        for field in DYNAMIC_FIELDS:
            field_re = re.compile(
                r"\.\b" + re.escape(field) + r"\b"
            )
            field_match = field_re.search(expr)
            if not field_match:
                continue
            if _filter_chain_humanizes(expr):
                continue
            # Skip predicates / HTML slugs.
            if _is_predicate_or_slug(
                expr[:field_match.start()], text, m.start(),
            ):
                continue
            leaks.append((line_no, field, expr.strip()))
    return leaks


def test_no_raw_render_of_dynamic_snake_case_fields():
    """Every render of a known-dynamic field MUST pipe through a
    humanizing filter. The 2026-05-09 audit found 7 cells across 4
    templates that didn't — STRONG_BUY, bull_put_spread, etc.
    leaked to the user verbatim.

    To add a new dynamic field: extend DYNAMIC_FIELDS at top of file.
    To allow a render without humanize: extend HUMANIZING_FILTERS or
    move the template to EXEMPT_TEMPLATES (with a comment explaining
    why)."""
    all_leaks = []
    for root, dirs, files in os.walk(TEMPLATES_DIR):
        for name in files:
            if not name.endswith(".html"):
                continue
            if name in EXEMPT_TEMPLATES:
                continue
            path = os.path.join(root, name)
            for line, field, expr in _scan_template(path):
                rel = os.path.relpath(path, TEMPLATES_DIR)
                all_leaks.append(
                    f"  templates/{rel}:{line} — `{{{{ {expr} }}}}` "
                    f"renders dynamic field `{field}` without "
                    "`| humanize` (or `| display_name` / `| title`)."
                )
    assert not all_leaks, (
        "Found template renders of dynamic snake_case fields with no "
        "humanizing filter. Each one shows the user raw values like "
        "STRONG_BUY / bull_put_spread instead of human-readable text. "
        "Pipe through `| humanize` (idempotent + safe).\n\n"
        + "\n".join(all_leaks)
    )
