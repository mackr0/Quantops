"""Guardrail: the inline price-change in _trades_table.html must use
the right sign for long vs short positions.

History: on 2026-04-27 the dashboard gained an inline "current price +
% change" line under each open position's entry price. The first cut
naively computed `(current - entry) / entry` regardless of side — so
a short position would show GREEN +2.1% as the underlying price rose
above the short entry, which is the exact opposite of the position's
P&L. Fixed before any short opened in prod, this test guards the fix.
"""

from jinja2 import Environment, FileSystemLoader
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")


def _render(trade):
    """Render the single trade row through the macro and return HTML."""
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.filters["friendly_time"] = lambda x: x or "--"
    tmpl = env.from_string(
        "{% from '_trades_table.html' import render_trades %}"
        "{{ render_trades([t]) }}"
    )
    return tmpl.render(t=trade)


def _open_position(side, entry, current):
    """Build a position dict shaped like _enriched_positions output."""
    return {
        "timestamp": "2026-04-27T13:00:00",
        "symbol": "TEST",
        "side": side,
        "qty": 100,
        "price": entry,
        "current_price": current,
        "market_value": current * 100,
        "unrealized_pl": (current - entry) * 100 * (1 if side == "buy" else -1),
        "unrealized_plpc": ((current - entry) / entry) * (1 if side == "buy" else -1),
        "ai_confidence": 70,
        "ai_reasoning": None,
        "reason": None,
        "stop_loss": None,
        "take_profit": None,
        "decision_price": None,
        "fill_price": None,
        "slippage_pct": None,
        "pnl": None,
    }


def test_long_winning_position_renders_green_positive():
    html = _render(_open_position("buy", entry=100.0, current=110.0))
    # +10% rise on a long = winning, green class, "+10.0%"
    assert "+10.0%" in html, f"Expected +10.0% in long winner, got:\n{html}"
    assert "pnl-pos" in html
    # The line that contains the percent should be the pos class one
    line = [l for l in html.splitlines() if "+10.0%" in l][0]
    assert "pnl-pos" in line, "Long winner must render with pnl-pos class"


def test_long_losing_position_renders_red_negative():
    html = _render(_open_position("buy", entry=100.0, current=95.0))
    assert "-5.0%" in html
    line = [l for l in html.splitlines() if "-5.0%" in l][0]
    assert "pnl-neg" in line, "Long loser must render with pnl-neg class"


def test_short_winning_position_renders_green_positive():
    """A short opened at $100 with price now $90 has GAINED ~10%.
    Must render green, with a positive percent."""
    html = _render(_open_position("sell_short", entry=100.0, current=90.0))
    # Short profits when price falls. Price -10%, position +10%.
    assert "+10.0%" in html, (
        "Short winner (price fell 10%) should render +10.0% gain. "
        f"Got:\n{html}"
    )
    line = [l for l in html.splitlines() if "+10.0%" in l][0]
    assert "pnl-pos" in line, (
        "Short winner must render with pnl-pos class. The bug to "
        "guard against: showing the price's direction instead of "
        "the position's direction."
    )


def test_short_losing_position_renders_red_negative():
    """A short opened at $100 with price now $110 has LOST ~10%."""
    html = _render(_open_position("sell_short", entry=100.0, current=110.0))
    assert "-10.0%" in html, (
        "Short loser (price rose 10%) should render -10.0%. Got:\n" + html
    )
    line = [l for l in html.splitlines() if "-10.0%" in l][0]
    assert "pnl-neg" in line


def test_dashboard_short_alias_side_sell():
    """_enriched_positions sets side='sell' for shorts (qty<0). The
    template must treat that the same as 'sell_short'."""
    html = _render(_open_position("sell", entry=100.0, current=110.0))
    assert "-10.0%" in html, "side='sell' (dashboard short alias) must invert sign"
    line = [l for l in html.splitlines() if "-10.0%" in l][0]
    assert "pnl-neg" in line


def test_no_current_price_renders_no_change_line():
    """Closed-trade rows on the /trades page have no current_price.
    The new line must not appear at all for them."""
    pos = _open_position("buy", entry=100.0, current=110.0)
    pos["current_price"] = None
    html = _render(pos)
    assert "@ $" not in html, (
        "Rows without current_price must not render the inline "
        "@ $... (% change) line. Got:\n" + html
    )
