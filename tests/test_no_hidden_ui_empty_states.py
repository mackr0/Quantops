"""Pin that no UI panel disappears when its backing data is empty.

Caught 2026-05-09 (sweep after Issue 5 / Issue 8 / Issue 9 hidden-UI
discoveries): templates had ~13 `{% if X %}<article>...</article>{% endif %}`
wrappers that hid an entire `<article>` (with its `<h3>` header) when
the backing data was empty. The user never knew these sections
existed because they were silently invisible. This is a no-hidden-UI
rule violation even when the wrapper PRE-EXISTS the change you're
making — preserving an existing violation IS itself a violation.

This test pins:
1. Behavioral: rendering each affected template with EMPTY backing
   data shows the section header AND an explicit empty-state
   message (never silent invisibility).
2. Cross-cutting AST/regex guardrail: scan templates/ for
   `{% if X %}` immediately followed by `<article` AND a closing
   `{% endif %}` after the article. That's the hide-when-empty
   pattern. New occurrences require an explicit allowlist entry
   with a comment explaining why it's not a violation
   (e.g. alert banners that legitimately only show in error states).
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


TEMPLATES_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "templates",
)


def _user():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 1
    u.is_admin = True
    u.is_viewer = False
    u.role = "admin"
    u.email = "test@example.com"
    u.display_name = "Test"
    u.effective_user_id = 1
    return u


@pytest.fixture
def app_ctx():
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


def _render(app, template_name, **ctx):
    with app.test_request_context("/"):
        with patch("flask_login.utils._get_user", return_value=_user()):
            from flask import render_template
            return render_template(template_name, **ctx)


# ---------------------------------------------------------------------------
# Layer 1 — behavioral: every panel renders header + empty-state with empty data
# ---------------------------------------------------------------------------


class _Loose(dict):
    """Loose dict that returns 0 for missing attrs (so comparisons in
    Jinja templates don't blow up on minimal test contexts)."""
    def __getattr__(self, k):
        if k in self:
            v = self[k]
            return _Loose(v) if isinstance(v, dict) and not isinstance(v, _Loose) else v
        return 0
    def __getitem__(self, k):
        if k in self.keys():
            v = super().__getitem__(k)
            return _Loose(v) if isinstance(v, dict) and not isinstance(v, _Loose) else v
        return 0


def _ai_perf_ctx(**overrides):
    base = {
        "perf": _Loose({}),
        "trade_perf": _Loose({"total_trades": 0}),
        "tuning_history": [],
        "profiles": [], "selected_profile": None,
        "selected_profile_name": "",
        "slippage": _Loose(),
        "risk": _Loose(),
        "monthly_returns": [],
    }
    base.update(overrides)
    return base


class TestAIPerformanceEmptyStates:
    def test_monthly_returns_section_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai_performance.html",
                       **_ai_perf_ctx(monthly_returns=[]))
        # Header always present
        assert "<h3>Monthly Returns</h3>" in html
        # Explicit empty-state message
        assert "No monthly data yet" in html

    def test_accuracy_by_confidence_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai_performance.html",
                       **_ai_perf_ctx(perf=_Loose({"accuracy_by_confidence": {}})))
        assert "Accuracy by Confidence Band" in html
        assert "Not enough resolved predictions yet" in html

    def test_best_worst_trade_show_headers_and_empty_msgs(self, app_ctx):
        html = _render(app_ctx, "ai_performance.html",
                       **_ai_perf_ctx(perf=_Loose({"best_trade": None, "worst_trade": None})))
        assert "<h4>Best Trade</h4>" in html
        assert "<h4>Worst Trade</h4>" in html
        # Both share the same empty-state copy
        assert html.count("No closed trades yet") >= 2

    def test_missed_gain_avoided_loss_show_headers_and_empty_msgs(self, app_ctx):
        html = _render(app_ctx, "ai_performance.html",
                       **_ai_perf_ctx(perf=_Loose({
                           "biggest_missed_gain": None,
                           "biggest_avoided_loss": None,
                       })))
        assert "<h4>Biggest Missed Gain</h4>" in html
        assert "<h4>Biggest Avoided Loss</h4>" in html
        assert "the AI passed and the stock ran" in html
        assert "the AI passed and the stock dropped" in html

    def test_self_tuning_history_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai_performance.html",
                       **_ai_perf_ctx(tuning_history=[]))
        assert "<h3>Self-Tuning History</h3>" in html
        assert "No automatic tuning adjustments yet" in html


class TestBacktestEmptyStates:
    def test_all_trades_section_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "backtest.html",
                       results=_Loose({"trades": []}),
                       market_type="small", days=30)
        assert "All Trades (0)" in html
        assert "No trades in this backtest result" in html


class TestAIDashEmptyStates:
    def _ctx(self, **overrides):
        base = {
            "profiles": [], "selected_profile": None,
            "selected_profile_name": "",
            "long_short_awareness": [],
            "portfolio_risk_awareness": [],
            "ai_perf": _Loose(), "slippage": _Loose(),
            "ai_cost_info": _Loose({"per_profile": [], "totals": _Loose({"today": 0, "7d": 0, "30d": 0})}),
            "crisis_info": _Loose({"per_profile": [], "max_level": "normal"}),
            "ensemble_info": _Loose({"per_profile": []}),
            "event_info": _Loose({"per_profile": []}),
            "validations": [],
            "decay_info": _Loose({"per_profile": [], "any_deprecated": False}),
            "allocation_info": _Loose({"per_profile": []}),
            "auto_strategy_info": _Loose({"per_profile": []}),
            "meta_info": _Loose({"loaded": False, "profiles": []}),
            "macro_info": _Loose(),
            "kill_switch": _Loose({"enabled": False}),
            "scaling_real": [], "scaling_capacity": [],
            "perf_kelly_long": None, "perf_kelly_short": None,
            "mfe_capture": None,
            "exposure": None, "profile_target_book_beta": None,
            "crisis_level": "normal",
        }
        base.update(overrides)
        return base

    def test_long_short_construction_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai.html", **self._ctx(long_short_awareness=[]))
        assert "Long/Short Construction" in html
        assert "No long/short construction data yet" in html

    def test_risk_budget_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai.html", **self._ctx(long_short_awareness=[]))
        assert "Risk Budget" in html
        assert "No risk-budget breakdown yet" in html

    def test_portfolio_risk_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai.html", **self._ctx(portfolio_risk_awareness=[]))
        assert "Portfolio Risk" in html
        assert "No portfolio risk snapshot yet" in html

    def test_sector_concentration_shows_header_and_empty_msg(self, app_ctx):
        html = _render(app_ctx, "ai.html", **self._ctx(long_short_awareness=[]))
        assert "Sector Concentration" in html
        assert "No concentration warnings right now" in html


# ---------------------------------------------------------------------------
# Layer 2 — cross-cutting guardrail: no `{% if X %}<article>` hide patterns
# ---------------------------------------------------------------------------


# Allowed `{% if X %}<article>` sites — these are legitimately conditional
# and are NOT hide-when-empty violations:
#   - dashboard.html broken_profiles / scan_failures: alert banners that only
#     appear in degraded states (intentional — alerting UI)
#   - dashboard.html prof.account: per-profile Alpaca-account section that
#     only renders when the profile has linked credentials (a config state,
#     not a data state)
#   - settings.html current_user.is_viewer: viewer-mode notice (not data)
#   - ai_performance.html selected_profile: scope-only section
#     (Backtest vs Reality requires single-profile mode; addressed
#     separately if/when scope-empty surfaces also need an empty-state).
ALLOWLIST = {
    ("dashboard.html", "broken_profiles"),
    ("dashboard.html", "scan_failures"),
    ("dashboard.html", "prof.account"),
    ("settings.html", "current_user.is_viewer"),
    ("ai_performance.html", "selected_profile"),
    # ai.html risk_budget per-row block — the OUTER Risk Budget article
    # always renders with an empty-state message when no profiles have
    # any risk_budget data; the per-row inner <article> conditionally
    # only renders for profiles that DO have data. The user always sees
    # the section header.
    ("ai.html", "r.risk_budget"),
}


def test_no_new_hide_when_empty_article_wrappers():
    """`{% if X %}<article>...</article>{% endif %}` hides the
    section's header (and the user's awareness it exists) when X
    is empty. Add a panel-empty-state instead. New occurrences must
    be added to ALLOWLIST in this file with a justification."""
    pattern = re.compile(
        r'\{%\s*if\s+([a-zA-Z_][a-zA-Z_0-9.]*)\s*%\}\s*\n\s*<article',
    )
    leaks = []
    for root, _, files in os.walk(TEMPLATES_DIR):
        for fname in files:
            if not fname.endswith(".html"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, TEMPLATES_DIR)
            with open(path) as f:
                text = f.read()
            for m in pattern.finditer(text):
                var = m.group(1)
                if (rel, var) in ALLOWLIST:
                    continue
                line_no = text[:m.start()].count("\n") + 1
                leaks.append(
                    f"  templates/{rel}:{line_no}  "
                    f"`{{% if {var} %}}<article>` hides the section "
                    "when empty. Add an `{% else %}` empty-state "
                    "branch INSIDE the article instead."
                )
    assert not leaks, (
        "Found template patterns that hide an entire <article> "
        "(including its header) when backing data is empty. The user "
        "never knows the section exists. Move the conditional INSIDE "
        "the article so the header always renders, and add an explicit "
        "empty-state message in the `{% else %}` branch.\n\n"
        + "\n".join(leaks)
    )
