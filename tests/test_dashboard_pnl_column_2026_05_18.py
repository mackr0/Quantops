"""Guardrail: dashboard overview must have a P&L column on the
per-profile rows + footer total + the API endpoint must include
`pnl` per profile and `total_pnl` book-wide.

Operator-requested feature on 2026-05-18 — P&L used to exist on the
overview but disappeared over time. This test pins it back in.
"""
from __future__ import annotations


def test_overview_table_has_pnl_header():
    with open("templates/dashboard.html", encoding="utf-8") as f:
        src = f.read()
    # The header row of the overview table must contain the P&L
    # column. The template uses HTML entities so accept both forms.
    assert ("<th>P&amp;L</th>" in src or "<th>P&L</th>" in src), (
        "Overview table missing P&L column header"
    )


def test_overview_per_row_pnl_cell_present():
    with open("templates/dashboard.html", encoding="utf-8") as f:
        src = f.read()
    # The per-row td must use a totals-pnl-<id> element so the JS
    # refresh can update it.
    assert "totals-pnl-{{ prof.id }}" in src, (
        "Per-row P&L cell missing or unaddressable from JS"
    )


def test_overview_footer_total_pnl_present():
    with open("templates/dashboard.html", encoding="utf-8") as f:
        src = f.read()
    assert "totals-book-pnl" in src, (
        "Footer total P&L cell missing"
    )


def test_dashboard_totals_api_includes_pnl_keys():
    """The /api/dashboard-totals payload must include `pnl` per
    profile and `total_pnl` at the book level — JS reads these to
    refresh the cells every 30s."""
    with open("views.py", encoding="utf-8") as f:
        src = f.read()
    # Per-profile rows.append() must include pnl
    assert '"pnl": pnl' in src, (
        "api_dashboard_totals per-profile row missing pnl key"
    )
    # Book-level total
    assert '"total_pnl"' in src, (
        "api_dashboard_totals payload missing total_pnl key"
    )


def test_pnl_color_branches_present_in_js():
    """JS must apply green for positive, red for negative, neutral for
    zero. Without color the P&L is much less useful at a glance."""
    with open("templates/dashboard.html", encoding="utf-8") as f:
        src = f.read()
    assert "#2e7d32" in src, "Positive P&L color (green) missing"
    assert "#c62828" in src, "Negative P&L color (red) missing"
