"""Pin /performance Self-Tuning & Alerts tab renders all 4 panels.

Caught 2026-05-09: views.api_performance computed tuning_history /
tuning_status / learned_patterns / sec_alerts and threw them away
by passing literal [] to render_template. The lazy fix was to delete
the computations; the proper fix (this commit) was to BUILD the 4
missing panels in performance.html. This test pins that the panels
actually render with the data the route produces.

Layer 1: each panel emits its expected text when given seeded data.
Layer 2: AST guard already exists in test_no_dead_throw_render_kwargs.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


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
    """A Flask app + test request context so templates can resolve
    current_user / url_for / etc. without the test having to stub
    every Flask-Login global."""
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app


class _LooseDict(dict):
    """A dict that returns 0 for missing keys (so the template's
    {{ m.total_pnl }}-style accesses don't blow up when the test
    only seeds a subset). Tests only assert on the panels they care
    about; everything else just needs to render without error."""
    def __getattr__(self, name):
        if name in self:
            v = self[name]
            if isinstance(v, dict) and not isinstance(v, _LooseDict):
                return _LooseDict(v)
            return v
        return 0
    def __getitem__(self, key):
        if key in self.keys():
            v = super().__getitem__(key)
            if isinstance(v, dict) and not isinstance(v, _LooseDict):
                return _LooseDict(v)
            return v
        return 0


def _render_tuning_tab(app, **ctx):
    """Render performance.html with given context inside a real
    request context, then slice out the Self-Tuning & Alerts tab body."""
    base = {
        "m": _LooseDict({
            "has_trades": False, "has_snapshots": False,
            "current_streak": {"computable": False},
            "sharpe_ratio_computable": False,
            "sortino_ratio_computable": False,
            "monthly_returns": [], "drawdown_series": [],
            "rolling_sharpe": [], "rolling_3m_returns": [],
            "worst_periods": [],
        }),
        "profiles": [], "selected_profile": None, "selected_profile_name": "",
        "exposure": _LooseDict(), "profile_target_book_beta": None,
        "perf_kelly_long": None, "perf_kelly_short": None,
        "mfe_capture": _LooseDict(), "ai_perf": _LooseDict(),
        "slippage": _LooseDict(),
        "scaling_real": [], "scaling_capacity": [],
        "tuning_history": [], "tuning_status": [], "learned_patterns": [],
        "sec_alerts": [], "meta_info": {"loaded": False, "profiles": []},
        "validations": [], "decay_info": {"per_profile": [], "any_deprecated": False},
        "allocation_info": _LooseDict(), "auto_strategy_info": _LooseDict(),
        "ensemble_info": _LooseDict(), "event_info": _LooseDict(),
        "crisis_info": _LooseDict(), "ai_cost_info": _LooseDict(),
    }
    base.update(ctx)
    return _slice_tuning_tab(_render_full(app, base))


def _render_full(app, base):
    with app.test_request_context("/performance"):
        with patch("flask_login.utils._get_user", return_value=_user()):
            from flask import render_template
            return render_template("performance.html", **base)


def _slice_tuning_tab(out):
    start = out.find('id="tab-tuning"')
    assert start > 0, "tab-tuning div not found in performance.html"
    end = out.find('</div>\n\n<!-- ', start)
    if end < 0:
        end = out.find('</div>\n\n\n<script>', start)
    if end < 0:
        end = len(out)
    return out[start:end]


class TestSelfTuningStatusPanel:
    def test_status_card_per_profile(self, app_ctx):
        html = _render_tuning_tab(app_ctx, tuning_status=[
            {"profile_id": 1, "profile_name": "Mid Cap",
             "resolved": 12, "required": 20,
             "can_tune": False,
             "message": "Need 8 more resolved predictions",
             "last_run": "2026-05-08T14:30:00"},
            {"profile_id": 2, "profile_name": "Small Cap",
             "resolved": 25, "required": 20,
             "can_tune": True,
             "message": "Tuner active",
             "last_run": "2026-05-09T09:15:00"},
        ])
        assert "Mid Cap" in html
        assert "Small Cap" in html
        assert "12 / 20 resolved predictions" in html
        assert "25 / 20 resolved predictions" in html
        assert "Awaiting data" in html  # Mid Cap is not yet tuneable
        assert "Active" in html         # Small Cap is

    def test_empty_status_shows_placeholder(self, app_ctx):
        html = _render_tuning_tab(app_ctx, tuning_status=[])
        assert "No tuning status available yet." in html


class TestSelfTuningHistoryPanel:
    def test_history_row_renders_change_and_outcome(self, app_ctx):
        html = _render_tuning_tab(app_ctx, tuning_history=[
            {"timestamp": "2026-05-07T10:00:00",
             "profile_name": "Small Cap",
             "parameter_name": "stop_loss_pct",
             "parameter_label": "Stop Loss %",
             "old_value": 0.03, "old_value_label": "3.0%",
             "new_value": 0.04, "new_value_label": "4.0%",
             "reason": "Stops were getting hit too tight in volatile sessions",
             "win_rate_at_change": 42.0,
             "outcome_after": "improved",
             "win_rate_after": 51.0},
        ])
        assert "Small Cap" in html
        assert "Stop Loss %" in html
        assert "3.0%" in html and "4.0%" in html
        # Outcome badge
        assert "Improved" in html
        assert "51.0%" in html
        # Reason snippet
        assert "Stops were getting hit too tight" in html

    def test_empty_history_shows_placeholder(self, app_ctx):
        html = _render_tuning_tab(app_ctx, tuning_history=[])
        assert "No tuning adjustments recorded yet." in html


class TestLearnedPatternsPanel:
    def test_patterns_render_as_bullets(self, app_ctx):
        patterns = [
            "Predictions in volatile markets: 22% win rate (vs 48% overall, 18 trades). Be extra cautious.",
            "momentum_breakout signals: 28% win rate (12 trades). Avoid this pattern.",
        ]
        html = _render_tuning_tab(app_ctx, learned_patterns=patterns)
        for p in patterns:
            # Jinja default escapes — apostrophes / etc. don't appear here
            # but plain ASCII text passes through. Spot-check substrings.
            assert "volatile markets" in html
            assert "22% win rate" in html
            assert "Avoid this pattern" in html

    def test_empty_patterns_shows_placeholder(self, app_ctx):
        html = _render_tuning_tab(app_ctx, learned_patterns=[])
        assert "No patterns identified yet" in html


class TestSecAlertsPanel:
    def test_alert_row_renders_severity_and_summary(self, app_ctx):
        html = _render_tuning_tab(app_ctx, sec_alerts=[
            {"profile_id": 3, "profile_name": "Largecap",
             "symbol": "AAPL", "form": "8-K",
             "filed_date": "2026-05-08",
             "severity": "high",
             "signal": "material_event",
             "summary": "Disclosed regulatory probe related to App Store policies."},
            {"profile_id": 5, "profile_name": "Crypto",
             "symbol": "MSTR", "form": "10-Q",
             "filed_date": "2026-05-07",
             "severity": "medium",
             "signal": "earnings_disclosure",
             "summary": "Quarterly results filed; bitcoin holdings disclosed."},
        ])
        # Severity badges
        assert "HIGH" in html
        assert "Medium" in html
        # Symbol + form
        assert "AAPL" in html and "8-K" in html
        assert "MSTR" in html and "10-Q" in html
        # Humanized signal
        assert "Material Event" in html
        # Summary snippet
        assert "regulatory probe" in html

    def test_empty_alerts_shows_placeholder(self, app_ctx):
        html = _render_tuning_tab(app_ctx, sec_alerts=[])
        assert "No active SEC alerts on held positions." in html


class TestTabNavigation:
    def test_tab_link_present(self, app_ctx):
        """The tab nav must include the Self-Tuning & Alerts link so
        users can actually navigate to the panels."""
        base = {
            "m": _LooseDict({
                "has_trades": False, "has_snapshots": False,
                "current_streak": {"computable": False},
                "sharpe_ratio_computable": False,
                "sortino_ratio_computable": False,
                "monthly_returns": [], "drawdown_series": [],
                "rolling_sharpe": [], "rolling_3m_returns": [],
                "worst_periods": [],
            }),
            "profiles": [], "selected_profile": None,
            "selected_profile_name": "",
            "exposure": _LooseDict(), "profile_target_book_beta": None,
            "perf_kelly_long": None, "perf_kelly_short": None,
            "mfe_capture": _LooseDict(), "ai_perf": _LooseDict(),
            "slippage": _LooseDict(),
            "scaling_real": [], "scaling_capacity": [],
            "tuning_history": [], "tuning_status": [],
            "learned_patterns": [], "sec_alerts": [],
            "meta_info": {"loaded": False, "profiles": []},
            "validations": [],
            "decay_info": {"per_profile": [], "any_deprecated": False},
            "allocation_info": _LooseDict(),
            "auto_strategy_info": _LooseDict(),
            "ensemble_info": _LooseDict(), "event_info": _LooseDict(),
            "crisis_info": _LooseDict(), "ai_cost_info": _LooseDict(),
        }
        full = _render_full(app_ctx, base)
        assert 'href="#tuning"' in full
        assert "Self-Tuning &amp; Alerts" in full
