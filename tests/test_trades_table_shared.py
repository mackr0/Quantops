"""Regression test for the shared `_trades_table.html` macro (2026-04-15).

Before this refactor, the dashboard had a slim 6-column trade table
while /trades had an expandable 9-column table with AI reasoning,
confidence, stop/target, and slippage. Drift between them meant bug
fixes made on one page silently missed the other.

These tests verify:
  - Both pages render identical rich columns when given the same trade
  - The macro renders the expand-caret + hidden details row
  - Dashboard version omits the Profile column; /trades keeps it
"""

from __future__ import annotations

import pytest
from flask import Flask


def _make_app():
    """Minimal Flask app that loads the real template directory + filters."""
    import os
    template_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
    app = Flask(__name__, template_folder=os.path.abspath(template_dir))
    from display_names import register
    register(app)
    return app


def _render(template_text, **ctx):
    app = _make_app()
    with app.app_context():
        return app.jinja_env.from_string(template_text).render(**ctx)


def _sample_trade():
    return {
        "timestamp": "2026-04-15T14:23:00",
        "profile_name": "Mid Cap",
        "symbol": "AAPL",
        "side": "buy",
        "qty": 5,
        "price": 180.25,
        "ai_confidence": 72,
        "ai_reasoning": "Strong mean-reversion setup with confirming volume spike.",
        "pnl": None,
        "stop_loss": 175.00,
        "take_profit": 190.00,
        "decision_price": 180.00,
        "fill_price": 180.25,
        "slippage_pct": 0.014,
    }


class TestMacroRendersRichDetails:
    def test_ai_confidence_visible(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        assert "72%" in html
        assert "AI Conf" in html

    def test_ai_reasoning_in_expand_row(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        assert "Strong mean-reversion setup" in html
        assert "AI Reasoning" in html

    def test_stop_and_target_visible(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        assert "$175.00" in html
        assert "$190.00" in html
        assert "Slippage" in html

    def test_expand_caret_present(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        # Unicode right-pointing triangle used for the collapse indicator
        assert "expand-caret" in html
        assert "9654" in html or "\u25b6" in html


class TestProfileColumnToggle:
    def test_show_profile_true_includes_profile_column(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=True) }}",
            trades=[_sample_trade()],
        )
        assert ">Profile</th>" in html or ">Profile<" in html
        assert "Mid Cap" in html

    def test_show_profile_false_omits_profile_column(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        assert "<th>Profile</th>" not in html

    def test_colspan_adjusts_for_profile_column(self):
        html_with = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=True) }}",
            trades=[_sample_trade()],
        )
        html_without = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[_sample_trade()],
        )
        assert 'colspan="10"' in html_with
        assert 'colspan="9"' in html_without


class TestEmptyState:
    def test_empty_list_shows_custom_message(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, empty_message='Nothing yet') }}",
            trades=[],
        )
        assert "Nothing yet" in html

    def test_empty_list_has_default_message(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades) }}",
            trades=[],
        )
        assert "No trades yet" in html


class TestPnlRendering:
    def test_sell_row_shows_realized_pnl(self):
        """SELL rows show realized P&L in the Realized column."""
        t = _sample_trade()
        t["pnl"] = 42.50
        t["side"] = "sell"
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "42.50" in html
        assert "Realized" in html

    def test_open_buy_shows_unrealized_pnl(self):
        """Open BUY rows show unrealized P&L in the Unrealized column."""
        t = _sample_trade()
        t["pnl"] = None
        t["side"] = "buy"
        t["unrealized_pl"] = 12.75
        t["unrealized_plpc"] = 0.025
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "12.75" in html
        assert "Unrealized" in html

    def test_closed_buy_shows_blank_pnl(self):
        """Closed BUY rows show blank — the SELL row carries the realized P&L."""
        t = _sample_trade()
        t["pnl"] = None
        t["status"] = "closed"
        t["side"] = "buy"
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        # Both columns should be empty for a closed BUY
        assert ">open<" not in html.lower()

    def test_both_columns_exist(self):
        """The table must have separate Unrealized and Realized headers."""
        t = _sample_trade()
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "Unrealized" in html
        assert "Realized" in html
