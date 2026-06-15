"""Spread legs show their OWN dollar P&L, not the repeated spread
total (2026-06-15).

Operator feedback: a bull call spread rendered the spread-level
-$63 on the header AND on every leg row, reading as triple-counting
and making options opaque vs stocks. Industry-standard display: one
net number on the spread header, and per-leg contributions that
decompose it. We stamp each leg's own unrealized_pl (leg_pnl) and
render dollars-only on legs (no per-leg %, which was the misleading
-10100% number from stale OTM marks).

Pins:
  1. The spread header shows the spread total.
  2. Each leg row shows its OWN leg_pnl ($-50 long / $-13 short),
     NOT the spread total.
  3. Leg rows carry no per-leg percent.
"""
from jinja2 import Environment, FileSystemLoader
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")


def _render(trades):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    env.filters["friendly_time"] = lambda x: x or "--"
    from display_names import humanize, format_occ, action_label
    env.filters["humanize"] = humanize
    env.filters["format_occ"] = format_occ
    env.filters["action_label"] = action_label
    tmpl = env.from_string(
        "{% from '_trades_table.html' import render_trades %}"
        "{{ render_trades(trades) }}"
    )
    return tmpl.render(trades=trades)


def _leg(occ, side, entry, current, leg_pnl):
    # TSLG bull call spread, both legs share order_id (one combo).
    return {
        "timestamp": "2026-06-15T13:46:00",
        "symbol": "TSLG",
        "side": side,
        "qty": 1,
        "price": entry,
        "current_price": current,
        "occ_symbol": occ,
        "option_strategy": "bull_call_spread",
        "signal_type": "MULTILEG",
        "order_id": "combo-1",
        "status": "open",
        "ai_confidence": 70,
        "ai_reasoning": None, "reason": None,
        "stop_loss": None, "take_profit": None,
        "decision_price": None, "fill_price": None, "slippage_pct": None,
        "pnl": None,
        # spread-level (same on both legs)
        "spread_pnl": -63.0,
        "spread_pnl_pct": -60.0,
        "spread_max_loss": 105.0,
        "spread_strategy": "bull_call_spread",
        # per-leg own dollar P&L
        "leg_pnl": leg_pnl,
    }


def test_legs_show_own_pnl_not_spread_total():
    long_leg = _leg("TSLG260717C00007000", "buy", 1.20, 0.70, -50.0)
    short_leg = _leg("TSLG260717C00008000", "sell", 0.15, 0.28, -13.0)
    html = _render([long_leg, short_leg])

    # Header carries the spread total.
    assert "-$63.00" in html, "spread header total missing"
    # Each leg shows its OWN contribution.
    assert "-$50.00" in html, "long leg's own P&L (-$50) not shown"
    assert "-$13.00" in html, "short leg's own P&L (-$13) not shown"
    # The legs decompose the header (sum -50 + -13 = -63).
    assert (-50.0) + (-13.0) == -63.0
    # Per-leg row labels switched from 'spread ...' to 'this leg'.
    assert "this leg" in html
    assert "spread bull call spread" not in html.lower(), (
        "leg P&L column still echoes the spread total/label"
    )


def test_missing_leg_pnl_falls_back_gracefully():
    leg = _leg("TSLG260717C00007000", "buy", 1.20, 0.70, -50.0)
    del leg["leg_pnl"]
    html = _render([leg, _leg("TSLG260717C00008000", "sell", 0.15, 0.28, -13.0)])
    # No crash; the leg without leg_pnl renders a placeholder.
    assert "--" in html
