"""2026-06-18 — a terminal-unfilled order (expired / canceled / rejected,
0 fill) has NO realized P&L. The trades ledger was borrowing the still-open
position's unrealized mark onto it: an expired protective take-profit rendered
"-$157.32 (-1.1%)", which reads as a realized loss on a closing trade when the
position is actually still open. Now those rows show "did not fill" in the P&L
column. A real (filled) close still shows its P&L.
"""
import os

from jinja2 import Environment, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")


def _render(trade):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.filters["friendly_time"] = lambda x: x or "--"
    from display_names import humanize, format_occ, action_label
    env.filters["humanize"] = humanize
    env.filters["format_occ"] = format_occ
    env.filters["action_label"] = action_label
    tmpl = env.from_string(
        "{% from '_trades_table.html' import render_trades %}"
        "{{ render_trades([t]) }}"
    )
    return tmpl.render(t=trade)


def _base(**over):
    t = {
        "timestamp": "2026-06-17T19:43:00", "symbol": "CDE", "side": "sell",
        "signal_type": "PROTECTIVE_TP", "status": "open", "qty": 828,
        "price": 17.53, "current_price": None, "market_value": 14514.84,
        "unrealized_pl": -157.32, "unrealized_plpc": -0.011, "pnl": None,
        "ai_confidence": None, "ai_reasoning": "bracket child take-profit",
        "reason": None, "stop_loss": None, "take_profit": None,
        "decision_price": None, "fill_price": None, "slippage_pct": None,
    }
    t.update(over)
    return t


def test_expired_protective_shows_did_not_fill_not_borrowed_pnl():
    html = _render(_base(status="expired"))
    assert "did not fill" in html
    # the borrowed unrealized mark must NOT be rendered as a P&L number
    assert "157.32" not in html, (
        "an expired/unfilled order must not show the open position's "
        "unrealized mark as if it were its realized P&L")


def test_canceled_and_rejected_also_suppress_pnl():
    for st in ("canceled", "rejected", "done_for_day"):
        html = _render(_base(status=st))
        assert "did not fill" in html, st
        assert "157.32" not in html, st


def test_real_filled_close_still_shows_pnl():
    # a genuinely closed sell with realized pnl must still render its P&L
    html = _render(_base(status="closed", pnl=243.10, unrealized_pl=None,
                         unrealized_plpc=None))
    assert "did not fill" not in html
    assert "243.10" in html
