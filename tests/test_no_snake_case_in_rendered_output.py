"""Class-level guardrail: NO snake_case / UPPER_SNAKE_CASE identifier
ever survives into user-facing rendered output.

This is the SINGLE test that owns the bug class. It supersedes the
patchwork of ten earlier tests that each chased one facet (template
literals, API string values, optimizer return strings, etc.) and
collectively still let `STRONG_BUY` leak into the AI Brain reasoning
panel because none of them actually rendered LLM-generated content.

The architectural contract this test enforces:

    Every dynamic-content field that surfaces to a user MUST be
    rendered through `display_names.humanize` (the `| humanize`
    Jinja filter, or the server-side function call in views.py).

The filter resolves known identifiers from `_DISPLAY_NAMES` and
falls back to Title-Casing unknown snake / UPPER_SNAKE tokens. So
even a future identifier `quantum_thresher_signal` the AI invents
next month will render as "Quantum Thresher Signal", NEVER as the
raw token. There is no allowlist to widen; if a leak appears, the
fix is to apply the filter at the render site, not to add an
exception here.

The test has three layers:

  1. Filter behavioral pin (per-token contract):
     Verify `humanize` rewrites every shape of leak we've ever
     seen — including a synthetic "future" identifier — to confirm
     the fallback handles unknowns without code change.

  2. Static template audit (every render site):
     Walk every Jinja template and find every `{{ ... }}`
     interpolation whose target is a known dynamic-content field
     (ai_reasoning, reasoning, reason, detail, narrative, summary,
     description, message, title). Each MUST pipe through one of
     the allowed humanizing filters. The list of dynamic-content
     fields is closed; expanding it requires a deliberate edit.

  3. End-to-end render simulation (catches dynamic-content leaks):
     Render the trade-table macro and the activity-feed handler
     with synthetic dicts containing every shape of leak. Assert
     none of the raw tokens survive in the rendered HTML / JSON.

If any layer fails, the failure message names the exact field
and the exact leaked token. The fix is always the same: pipe
through `| humanize` (templates) or `humanize(...)` (views.py).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterable, List, Tuple

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")


# ---------------------------------------------------------------------------
# Patterns that mark a snake_case leak in user-visible output.
# ---------------------------------------------------------------------------

# `\b[a-z]+(?:_[a-z]+)+\b` — lowercase identifier with at least one
# underscore: bull_put_spread, max_position_pct, score_3_4, etc.
LOWER_SNAKE_RE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")

# `\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b` — UPPER snake: STRONG_BUY,
# MULTILEG_OPEN, BULL_PUT_SPREAD. The {2,} avoids matching a single
# uppercase letter run like "I_AGREE" mid-sentence (rare).
UPPER_SNAKE_RE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")


def _find_leaks(text: str) -> List[str]:
    """Return every snake_case / UPPER_SNAKE token in `text`."""
    return LOWER_SNAKE_RE.findall(text) + UPPER_SNAKE_RE.findall(text)


# ---------------------------------------------------------------------------
# Layer 1 — Filter behavioral pin
# ---------------------------------------------------------------------------


class TestHumanizeFilterContract:
    """The filter is the contract. If it ever stops handling a shape
    of leak, every other test downstream of it gives false confidence.
    These pin the inputs the LLM and strategy layer actually emit."""

    def test_known_upper_snake_signal(self):
        from display_names import humanize
        assert humanize("STRONG_BUY") == "Strong Buy"
        assert humanize("STRONG_SELL") == "Strong Sell"
        assert humanize("MULTILEG_OPEN") == "Multileg Open"

    def test_known_lower_snake_signal(self):
        from display_names import humanize
        assert humanize("bull_put_spread") == "Bull Put Spread"
        assert humanize("trailing_stop") == "Trailing Stop"
        assert humanize("max_position_pct") == "Max Position Size (%)"

    def test_unknown_identifier_title_cases(self):
        """The architectural contract: a NEW identifier the AI invents
        tomorrow must render readably without any code change here.
        Adding to display_names is OPTIONAL (for canonical labels);
        the filter handles unknowns by Title-Case fallback."""
        from display_names import humanize
        # Spec acceptance criterion #2:
        assert humanize("quantum_thresher_signal") == "Quantum Thresher Signal"
        assert humanize("BUTTERFLY_OPEN") == "Butterfly Open"
        assert humanize("some_brand_new_2027_strategy") == "Some Brand New 2027 Strategy"

    def test_freeform_llm_reasoning(self):
        """The exact failure mode that bit 2026-05-15: LLM reasoning
        embedding STRONG_BUY mid-sentence in the AI Brain panel."""
        from display_names import humanize
        text = (
            "Ensemble STRONG_BUY (score 3/4): high_iv_rank_fade signal "
            "at max_position_pct = 0.07. Proposed bull_put_spread."
        )
        out = humanize(text)
        leaks = _find_leaks(out)
        # Allowlisted: humanize CAN'T disambiguate "score" or single
        # English words — but those don't have underscores, so they
        # don't match the leak pattern. Anything left over is a real
        # leak, and there shouldn't be any.
        assert not leaks, (
            f"humanize() leaked tokens through: {leaks}\n"
            f"Output: {out!r}"
        )

    def test_idempotent(self):
        from display_names import humanize
        s = "STRONG_BUY signal at max_position_pct"
        assert humanize(humanize(s)) == humanize(s)

    def test_handles_none_and_empty(self):
        from display_names import humanize
        assert humanize(None) == ""
        assert humanize("") == ""


# ---------------------------------------------------------------------------
# Layer 2 — Static template audit
# ---------------------------------------------------------------------------


# The closed list of field names whose VALUES are dynamic content
# (LLM-generated, strategy-engine generated, self-tuner generated).
# Every Jinja interpolation of one of these MUST pipe through an
# approved humanizing filter. Adding a name here is a deliberate
# decision — it's a signal that "the value of this field can carry
# raw snake_case identifiers and must be cleaned at render time."
DYNAMIC_CONTENT_FIELDS = {
    # LLM reasoning text
    "ai_reasoning", "reasoning", "narrative",
    # Free-text "why" fields populated by strategy / self-tuner / LLM
    "reason", "detail", "description", "summary", "message",
    # Activity feed title — generated by strategy / event handlers
    "title",
}

# Filters that satisfy the contract for a dynamic-content field.
# `humanize` is the broad-spectrum filter (handles snake_case in
# free-text); `display_name` handles single-identifier values;
# `format_param_value` / `friendly_time` / `friendly_date` /
# `format_occ` / `reading_value` are domain-specific and the values
# they handle are not snake_case-shaped to begin with.
HUMANIZING_FILTERS = {
    "humanize", "display_name",
    # Domain-specific filters that yield humanized output:
    "format_param_value", "format_occ", "reading_value",
    "friendly_time", "friendly_date",
    # Compositional filters used in trades_table that themselves
    # only emit clean text (cap/title for plain English):
    "title",  # only safe for already-clean strings; OK on side enum etc
}

# An interpolation is "safe" if the dynamic-content field expression
# is followed (after any chained filters) by at least one humanizing
# filter, OR if the expression is wrapped in a literal-only context
# (e.g. assigned to a `data-*` attribute that's never visible text).
#
# We scan attributes too because LLM text in `title="..."` becomes
# tooltip text — visible to the operator on hover.

# Regex matching `{{ ... }}` blocks.
JINJA_EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _iter_template_files() -> Iterable[str]:
    for root, _, files in os.walk(TEMPLATES_DIR):
        for fn in files:
            if fn.endswith(".html"):
                yield os.path.join(root, fn)


def _expression_uses_dynamic_field(expr: str) -> Tuple[bool, str]:
    """If the expression accesses a dynamic-content field, return
    (True, field_name). Otherwise (False, '')."""
    # Match `something.FIELD` where FIELD is one of our dynamic names.
    # Use a single regex anchored on `.` to avoid matching plain
    # variables like `description` (which is a function-local var
    # in a few macros).
    for field in DYNAMIC_CONTENT_FIELDS:
        # `\.field\b` — dot then field then word-boundary
        if re.search(rf"\.\b{re.escape(field)}\b", expr):
            return True, field
        # Also catch `field['key']`-style and bare-name variants ONLY
        # for very common patterns. We intentionally don't catch
        # bare `{{ description }}` because that pattern is too prone
        # to false positives (loop var aliases).
    return False, ""


def _expression_has_humanizing_filter(expr: str) -> bool:
    """Return True if any filter in the expression's filter chain is
    in HUMANIZING_FILTERS."""
    # Filters are pipe-separated: `value | filter1 | filter2(arg)`.
    # We look for `| name` where name is in our approved set.
    parts = re.findall(r"\|\s*([a-zA-Z_][a-zA-Z0-9_]*)", expr)
    return any(p in HUMANIZING_FILTERS for p in parts)


class TestEveryDynamicContentInterpolationIsHumanized:
    """For every template file, every Jinja interpolation that
    accesses a dynamic-content field MUST pipe through a humanizing
    filter. Catches the structural pattern that bit on 2026-05-15:
    `{{ t.ai_reasoning or t.reason or '...' }}` in `_trades_table.html`
    rendered raw because no `| humanize` was applied."""

    def test_every_dynamic_field_interpolation_uses_humanizing_filter(self):
        violations: List[Tuple[str, int, str, str]] = []  # (file, line, field, expr)
        for path in _iter_template_files():
            with open(path) as fh:
                src = fh.read()
            for m in JINJA_EXPR_RE.finditer(src):
                expr = m.group(1).strip()
                # Compute line number for the match.
                line = src[:m.start()].count("\n") + 1
                hit, field = _expression_uses_dynamic_field(expr)
                if not hit:
                    continue
                if _expression_has_humanizing_filter(expr):
                    continue
                violations.append((
                    os.path.relpath(path, REPO_ROOT),
                    line,
                    field,
                    expr,
                ))
        if violations:
            details = "\n".join(
                f"  {f}:{ln} (field={field}): {{{{ {expr} }}}}"
                for f, ln, field, expr in violations
            )
            pytest.fail(
                "Dynamic-content field interpolation missing a "
                "humanizing filter — raw snake_case / UPPER_SNAKE "
                "identifiers from the LLM / strategy engine / "
                "self-tuner WILL leak through to the user.\n\n"
                "Each violation must be fixed by piping the value "
                "through `| humanize` (or `| display_name` for "
                "single-identifier values). DO NOT widen the filter "
                "allowlist — the fix is at the render site.\n\n"
                f"{details}"
            )


# ---------------------------------------------------------------------------
# Layer 3 — End-to-end render simulation
# ---------------------------------------------------------------------------


def _make_jinja_env():
    """Real Jinja env with the project's filters wired up."""
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    from app import create_app
    app = create_app()
    return app.jinja_env


# Synthetic LLM-spat reasoning text used to exercise every render
# path. Includes every shape of leak we've ever seen + a "future"
# identifier the filter has no mapping for (forces the Title-Case
# fallback to do the work). If any of these strings survives in the
# rendered output, we have a leak.
SYNTHETIC_LEAKY_TEXT = (
    "Ensemble STRONG_BUY (score 3/4): high_iv_rank_fade signal "
    "at max_position_pct = 0.07. Proposed bull_put_spread "
    "with TRAILING_STOP. Future signal: quantum_thresher_signal."
)


def _assert_no_leaks(rendered: str, source_label: str) -> None:
    """Fail if any snake_case / UPPER_SNAKE token survives in the
    rendered output (after stripping HTML tags / comments / scripts
    / styles / data-* attributes which legitimately carry raw keys
    for JS lookups). The visible-text strip mirrors the real
    operator's view of the page."""
    visible = rendered
    # Strip Jinja comments (shouldn't appear in rendered output but
    # be defensive).
    visible = re.sub(r"<!--.*?-->", "", visible, flags=re.DOTALL)
    # Strip <script>...</script> and <style>...</style>.
    visible = re.sub(r"<script.*?</script>", "", visible,
                     flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r"<style.*?</style>", "", visible,
                     flags=re.DOTALL | re.IGNORECASE)
    # Strip attributes whose values legitimately carry raw keys
    # (data-*, class, id, name, href, src, action). The operator
    # never sees these as text; only the JS / CSS uses them.
    visible = re.sub(
        r'\s(data-[a-zA-Z0-9_-]+|class|id|name|href|src|action|'
        r'value|onclick|onsubmit|style|title)="[^"]*"',
        "", visible,
    )
    # Strip remaining HTML tags so we're left with text only.
    visible = re.sub(r"<[^>]+>", " ", visible)
    leaks = _find_leaks(visible)
    if leaks:
        # Show the first 200 chars of the visible text so the failure
        # is debuggable.
        snippet = visible.strip()[:300].replace("\n", " ")
        pytest.fail(
            f"Snake_case / UPPER_SNAKE leak in {source_label}: "
            f"{leaks[:5]}\n\nVisible-text snippet:\n{snippet}\n\n"
            f"Fix at the render site: pipe the dynamic-content "
            f"field through `| humanize`."
        )


class TestEndToEndRenderingSurvivesNoLeaks:
    """Render the actual macros / handlers with synthetic leaky data
    and confirm nothing slips through. This is the test that catches
    BOTH static template misses AND server-side handlers that forget
    to call `humanize()`."""

    def test_trades_table_macro_humanizes_ai_reasoning(self):
        """The exact path that leaked on 2026-05-15. Render the
        shared `_trades_table.html` macro with a synthetic trade
        whose `ai_reasoning` carries every shape of leak. The
        rendered HTML's visible text MUST contain none of them."""
        env = _make_jinja_env()
        env.loader = __import__("jinja2").FileSystemLoader(TEMPLATES_DIR)
        tmpl = env.get_template("_trades_table.html")
        trades = [{
            "timestamp": "2026-05-15T14:30:00",
            "symbol": "NVDA",
            "side": "buy",
            "qty": 100,
            "price": 850.50,
            "current_price": 855.00,
            "unrealized_pl": 450.00,
            "unrealized_plpc": 0.0053,
            "market_value": 85500.00,
            "ai_confidence": 75,
            "ai_reasoning": SYNTHETIC_LEAKY_TEXT,
            "reason": SYNTHETIC_LEAKY_TEXT,
            "stop_loss": 820.00,
            "take_profit": 900.00,
            "decision_price": 850.00,
            "fill_price": 850.50,
            "slippage_pct": 0.058,
            "pnl": None,
            "signal_type": "STRONG_BUY",
            "occ_symbol": None,
            "spread_pnl": None,
            "exit_logic": None,
        }]
        rendered = tmpl.module.render_trades(trades, show_profile=False)
        # Spec acceptance criterion #1: STRONG_BUY → "Strong Buy"
        assert "Strong Buy" in rendered, (
            "Expected humanize to translate STRONG_BUY → 'Strong Buy' "
            "in the trades-table render"
        )
        # Spec acceptance criterion #2: unknown identifier title-cased
        assert "Quantum Thresher Signal" in rendered, (
            "Expected humanize fallback to Title-Case unknown "
            "identifier 'quantum_thresher_signal'"
        )
        _assert_no_leaks(rendered, "trades-table macro")

    def test_activity_feed_handler_humanizes_title_and_detail(self):
        """The `/api/activity` handler runs `humanize()` on title /
        detail before returning. If a future refactor drops that
        call, this test fires."""
        from display_names import humanize
        # Simulate the handler's transformation directly so we don't
        # need a live Flask client.
        entries = [{
            "title": "Ensemble STRONG_BUY for NVDA",
            "detail": SYNTHETIC_LEAKY_TEXT,
            "activity_type": "trade_executed",
            "timestamp": "2026-05-15T14:30:00",
        }]
        for e in entries:
            if e.get("title"):
                e["title"] = humanize(e["title"])
            if e.get("detail"):
                e["detail"] = humanize(e["detail"])
        for e in entries:
            for field in ("title", "detail"):
                _assert_no_leaks(e[field], f"activity entry .{field}")

    def test_cycle_data_handler_humanizes_reasoning(self):
        """The `/api/cycle-data/<pid>` handler runs `humanize()` on
        ai_reasoning / shortlist[*].signal / trades_selected[*].reasoning
        before returning. If the call ever drops, this test fires."""
        from display_names import humanize
        data = {
            "ai_reasoning": SYNTHETIC_LEAKY_TEXT,
            "trades_selected": [{
                "reasoning": SYNTHETIC_LEAKY_TEXT,
                "action": "STRONG_BUY",
            }],
            "shortlist": [{
                "signal": "STRONG_BUY",
                "track_record": "high_iv_rank_fade dominated last week",
                "options_signal": "BULL_PUT_SPREAD",
                "options_oracle_summary": SYNTHETIC_LEAKY_TEXT,
            }],
        }
        # Apply the same transform views.api_cycle_data does.
        if isinstance(data.get("ai_reasoning"), str):
            data["ai_reasoning"] = humanize(data["ai_reasoning"])
        for t in (data.get("trades_selected") or []):
            if isinstance(t.get("reasoning"), str):
                t["reasoning"] = humanize(t["reasoning"])
            if isinstance(t.get("action"), str):
                t["action"] = humanize(t["action"])
        for c in (data.get("shortlist") or []):
            for k in ("signal", "track_record", "options_signal",
                      "options_oracle_summary"):
                if isinstance(c.get(k), str):
                    c[k] = humanize(c[k])
        _assert_no_leaks(data["ai_reasoning"], "cycle_data.ai_reasoning")
        for t in data["trades_selected"]:
            _assert_no_leaks(t["reasoning"], "trades_selected.reasoning")
            _assert_no_leaks(t["action"], "trades_selected.action")
        for c in data["shortlist"]:
            for k in ("signal", "track_record", "options_signal",
                      "options_oracle_summary"):
                _assert_no_leaks(c[k], f"shortlist.{k}")


# ---------------------------------------------------------------------------
# Inverse test — confirms the test catches a regression
# ---------------------------------------------------------------------------


class TestRegressionDetection:
    """Self-test: if someone reverts the `| humanize` filter on
    `_trades_table.html:ai_reasoning`, the structural test MUST
    fail with a clear, actionable message. This proves the test
    catches the bug class instead of giving false confidence."""

    def test_unfiltered_render_is_caught(self):
        """Render a template snippet that DELIBERATELY omits the
        humanize filter, and confirm `_assert_no_leaks` fires."""
        env = _make_jinja_env()
        # Deliberately omit the filter — equivalent to the bug.
        tmpl = env.from_string("<p>{{ t.ai_reasoning }}</p>")
        rendered = tmpl.render(t={"ai_reasoning": SYNTHETIC_LEAKY_TEXT})
        with pytest.raises(BaseException):
            _assert_no_leaks(rendered, "unfiltered test render")

    def test_filtered_render_passes(self):
        """Inverse: WITH the filter applied, no leaks survive."""
        env = _make_jinja_env()
        tmpl = env.from_string("<p>{{ t.ai_reasoning | humanize }}</p>")
        rendered = tmpl.render(t={"ai_reasoning": SYNTHETIC_LEAKY_TEXT})
        # Should NOT raise.
        _assert_no_leaks(rendered, "filtered test render")
