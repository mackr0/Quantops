"""SVG charts must fill their container, not cap at max-width:700px.

History: 2026-04-29. The win rate trend chart on /ai had
`style="width:100%;max-width:700px;"` which capped the chart at 700px
on dashboards rendered into wider containers. The result was a chart
that filled only ~half the available width — visually broken.

Fix: drop max-width and use width:100%;height:auto so the SVG scales
proportionally with the container. Default preserveAspectRatio
keeps text proportions correct.

This test pins all chart renderers in metrics.py so the regression
can't sneak back in.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _all_renderers():
    """Return (name, callable, args) for every chart renderer."""
    from metrics import (
        render_equity_curve_svg,
        render_drawdown_svg,
        render_bar_chart_svg,
        render_rolling_sharpe_svg,
        render_win_rate_svg,
    )
    sample_curve = [
        {"date": f"2026-04-{i:02d}", "equity": 10000 + i * 100} for i in range(1, 11)
    ]
    sample_drawdown = [
        {"date": f"2026-04-{i:02d}", "drawdown_pct": -i * 0.5} for i in range(1, 11)
    ]
    sample_bars = [
        {"label": f"Apr {i:02d}", "value": (i - 5) * 1.0} for i in range(1, 11)
    ]
    sample_sharpe = [
        {"date": f"2026-04-{i:02d}", "sharpe": 1.5 + i * 0.1} for i in range(1, 11)
    ]
    sample_win_rate = [
        {"date": f"2026-04-{i:02d}", "win_rate": 50 + i, "n": 5} for i in range(1, 11)
    ]
    return [
        ("equity_curve", render_equity_curve_svg, [sample_curve]),
        ("drawdown", render_drawdown_svg, [sample_drawdown]),
        ("bar_chart", render_bar_chart_svg, [sample_bars]),
        ("rolling_sharpe", render_rolling_sharpe_svg, [sample_sharpe]),
        ("win_rate_trend", render_win_rate_svg, [sample_win_rate]),
    ]


def test_no_chart_uses_max_width_constraint():
    """No chart SVG should contain max-width:Npx — that caps the chart
    at a fixed pixel width and leaves the rest of the container empty
    on dashboards rendered into wider columns. The 2026-04-29 incident
    where /ai's win-rate chart filled only half the box was caused by
    `max-width:700px;` on the SVG style.
    """
    for name, fn, args in _all_renderers():
        svg = fn(*args)
        assert "max-width:" not in svg, (
            f"{name} chart still uses max-width — drop it so the SVG "
            f"scales with its container. SVG snippet: {svg[:200]}"
        )


def test_every_chart_sets_width_100_percent():
    """Width:100% is what makes the SVG fill the parent. If a renderer
    forgets it, the SVG falls back to its intrinsic 700px width and
    looks broken on wide pages."""
    for name, fn, args in _all_renderers():
        svg = fn(*args)
        assert "width:100%" in svg or "width: 100%" in svg, (
            f"{name} chart missing width:100% — the SVG won't scale "
            f"with its container. Snippet: {svg[:200]}"
        )


def test_empty_data_state_also_responsive():
    """The 'not enough data' fallback SVG must also be responsive —
    the same chart can flip between data and empty state across
    cycles, and the layout must not change."""
    from metrics import (
        render_equity_curve_svg, render_drawdown_svg,
        render_bar_chart_svg, render_rolling_sharpe_svg,
        render_win_rate_svg,
    )
    empties = [
        ("equity_curve", render_equity_curve_svg([])),
        ("drawdown", render_drawdown_svg([])),
        ("bar_chart", render_bar_chart_svg([])),
        ("rolling_sharpe", render_rolling_sharpe_svg([])),
        ("win_rate_trend", render_win_rate_svg([])),
    ]
    for name, svg in empties:
        # Empty state should still produce a valid SVG (not empty string)
        assert svg.startswith("<svg"), f"{name} empty state didn't render an SVG"
        assert "max-width:" not in svg, (
            f"{name} empty-state SVG uses max-width — same regression "
            f"as the data-state fix. Snippet: {svg[:200]}"
        )
