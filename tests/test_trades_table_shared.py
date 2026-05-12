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
        assert 'colspan="9"' in html_with
        assert 'colspan="8"' in html_without


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
    def test_sell_row_shows_pnl(self):
        """SELL rows show realized P&L — like a traditional brokerage."""
        t = _sample_trade()
        t["pnl"] = 42.50
        t["side"] = "sell"
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "42.50" in html

    def test_buy_row_has_no_pnl(self):
        """BUY rows don't show P&L — just entry info. P&L belongs on
        the SELL row, like a traditional brokerage trade history."""
        t = _sample_trade()
        t["pnl"] = None
        t["side"] = "buy"
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "P&L" in html  # header exists
        # The P&L cell for a BUY should be empty
        assert "unrealized" not in html.lower()

    def test_single_pnl_column(self):
        """One P&L column, not two. Like Schwab/Fidelity."""
        t = _sample_trade()
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "Unrealized" not in html
        assert "Realized" not in html
        assert ">P&L<" in html or ">P&amp;L<" in html


class TestReconcileBackfillLabelRename:
    """2026-05-12 — reconcile_backfill rows are renamed at the
    display layer to 'Protective Exit (broker)'. The old 'Reconcile
    Backfill' label read as scary data-corruption when it's actually
    the reconciler catching a legitimate broker-side stop/TP fill.

    This pins:
    - Old label is GONE from rendered output
    - New label is present
    - Tooltip explains what it actually is
    - reconcile_backfill_partial gets the (partial) variant
    """

    def _reconcile_trade(self, signal_type="reconcile_backfill"):
        return {
            "timestamp": "2026-05-12T13:33:00",
            "profile_name": "Mid Cap",
            "symbol": "SHOP",
            "side": "sell",
            "qty": 24,
            "price": 101.93,
            "pnl": -130.44,
            "status": "closed",
            "signal_type": signal_type,
            "strategy": signal_type,
            "ai_confidence": None,
            "ai_reasoning": None,
            "stop_loss": None,
            "take_profit": None,
            "decision_price": None,
            "fill_price": 101.93,
            "slippage_pct": None,
        }

    def test_full_backfill_label_renamed(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[self._reconcile_trade("reconcile_backfill")],
        )
        # New label present
        assert "Protective Exit" in html
        # Old label gone
        assert "Reconcile Backfill" not in html
        # Tooltip explains what it actually is
        assert "Broker-side protective order" in html

    def test_partial_backfill_label_variant(self):
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[self._reconcile_trade("reconcile_backfill_partial")],
        )
        assert "Protective Exit (partial)" in html
        assert "Reconcile Backfill" not in html

    def test_normal_signal_types_unaffected(self):
        """Don't touch other signal_types — only the two reconcile variants."""
        t = self._reconcile_trade("STRONG_BUY")
        t["side"] = "buy"
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "Protective Exit" not in html
        assert "Strong Buy" in html

    def test_excluded_reconcile_rows_keep_raw_label(self):
        """EXCLUDED rows (data_quality tagged) are NOT real protective
        exits — they're known-corrupt cascade artifacts from the
        2026-05-11 phantom-stop incident. Renaming them to "Protective
        Exit" with the "sane P&L expected" tooltip would be a lie:
        these rows show +1131% / +4833% P&L. They keep the raw
        "Reconcile Backfill" label so operators can see they came
        from the reconciler; the EXCLUDED badge already tells them
        to ignore the row."""
        t = self._reconcile_trade("reconcile_backfill")
        t["data_quality"] = "phantom_stop_reconcile_2026_05_12"
        t["pnl"] = 23.77  # the actual RIOT corrupt row
        t["qty"] = 1
        t["price"] = 24.74
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        # Old label retained for excluded rows
        assert "Reconcile Backfill" in html
        # Tooltip promising sane P&L MUST NOT appear on excluded rows
        assert "sane P&amp;L expected" not in html
        assert "sane P&L expected" not in html
        # EXCLUDED badge present (the existing audit cue)
        assert "EXCLUDED" in html


class TestActionLabelResolver:
    """2026-05-12 — Mack: the action column should say WHAT KIND of
    buy/sell, not just `buy`/`sell`. The trades_table sub-line now
    derives a specific action label via display_names.action_label.

    For stocks: Long Open / Long Close / Short Open / Short Cover.
    For options: Buy to Open / Sell to Open Leg / Sell to Close /
    Buy to Close.

    The F STRONG_SELL row that triggered this fix was a LONG CLOSE
    (closing an existing long), not a Short Sell. Calling it "Short
    Sell" would be wrong."""

    def test_action_label_function_stocks(self):
        from display_names import action_label
        assert action_label("buy", "BUY", is_option=False) == "Long Open"
        assert action_label("sell", "STRONG_SELL", is_option=False) == "Long Close"
        assert action_label("short", "SHORT", is_option=False) == "Short Open"
        assert action_label("cover", None, is_option=False) == "Short Cover"

    def test_action_label_function_options(self):
        from display_names import action_label
        # multileg legs at entry — sell leg is sell-to-open
        assert action_label("sell", "MULTILEG", is_option=True) == "Sell to Open Leg"
        assert action_label("buy", "MULTILEG", is_option=True) == "Buy to Open Leg"
        # single-leg long open
        assert action_label("buy", "OPTIONS", is_option=True) == "Buy to Open"
        # single-leg close
        assert action_label("sell", "OPTIONS", is_option=True) == "Sell to Close"
        # cover a short option
        assert action_label("cover", None, is_option=True) == "Buy to Close"

    def test_action_label_handles_none(self):
        from display_names import action_label
        assert action_label(None) == "--"
        assert action_label("") == "--"

    def test_template_uses_action_label_for_stocks(self):
        # The F STRONG_SELL trade — must show "Long Close", NOT
        # "Short Sell" or plain "sell"
        t = {
            "timestamp": "2026-05-12T17:41:02",
            "profile_name": "Small Cap",
            "symbol": "F",
            "side": "sell",
            "qty": 139,
            "price": 11.915,
            "pnl": 2.08,
            "status": "closed",
            "signal_type": "STRONG_SELL",
            "strategy": "small",
            "ai_confidence": 62,
            "ai_reasoning": None,
            "stop_loss": None, "take_profit": None,
            "decision_price": 11.915, "fill_price": 11.91,
            "slippage_pct": -0.042,
        }
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "Long Close" in html
        # Raw lowercase `sell` sub-line is gone
        assert "<small class=\"muted\" style=\"font-size:0.7rem;\">sell</small>" not in html

    def test_template_distinguishes_short_open_from_long_close(self):
        # When the journal records side='short' (real short-open),
        # the row should say "Short Open" — distinct from "Long Close"
        t = {
            "timestamp": "2026-05-12T17:41:02",
            "profile_name": "Small Cap Shorts",
            "symbol": "TSLA",
            "side": "short",
            "qty": 50,
            "price": 250.0,
            "pnl": None,
            "status": "open",
            "signal_type": "STRONG_SELL",
            "strategy": "small_shorts",
            "ai_confidence": 70,
            "ai_reasoning": None,
            "stop_loss": 270.0, "take_profit": 230.0,
            "decision_price": 250.0, "fill_price": 250.0,
            "slippage_pct": 0.0,
        }
        html = _render(
            "{% import '_trades_table.html' as tpl %}"
            "{{ tpl.render_trades(trades, show_profile=False) }}",
            trades=[t],
        )
        assert "Short Open" in html
        assert "Long Close" not in html
