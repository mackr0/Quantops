"""Guardrail: dashboard overview P&L columns + the API payload that
feeds the 30s live refresh.

History:
  2026-05-18 — operator restored a P&L column that had disappeared from
               the overview; pinned per-row P&L + a book-wide total.
  2026-05-22 — book-wide equity/P&L/cash/position TOTALS removed: each
               profile runs a different strategy at a different capital
               base, so summing them is meaningless. Replaced with a
               per-account **P&L %** column (return on initial capital),
               which IS comparable across accounts. The only book-wide
               total kept is AI cost (genuinely additive). This file now
               pins that new contract, including the *negative* assertion
               that the dead book totals do not creep back.
"""
from __future__ import annotations


def _dashboard():
    with open("templates/dashboard.html", encoding="utf-8") as f:
        return f.read()


def _views():
    with open("views.py", encoding="utf-8") as f:
        return f.read()


def test_overview_table_has_pnl_and_pnl_pct_headers():
    src = _dashboard()
    assert ("<th>P&amp;L</th>" in src or "<th>P&L</th>" in src), (
        "Overview table missing absolute P&L column header"
    )
    # P&L % header — the cross-account-comparable column. Accept the
    # entity form and tolerate the title= tooltip attribute.
    assert "P&amp;L %</th>" in src or "P&L %</th>" in src, (
        "Overview table missing the P&L % column header (added 2026-05-22 "
        "so accounts at different capital bases are comparable)"
    )


def test_overview_per_row_pnl_and_pnlpct_cells_present():
    src = _dashboard()
    assert "totals-pnl-{{ prof.id }}" in src, (
        "Per-row absolute P&L cell missing or unaddressable from JS"
    )
    assert "totals-pnlpct-{{ prof.id }}" in src, (
        "Per-row P&L % cell missing or unaddressable from JS — the live "
        "refresh updates it by this id"
    )


def test_overview_footer_has_no_book_wide_value_totals():
    """The footer must NOT sum equity / P&L / cash / positions across
    profiles — that's meaningless across heterogeneous strategies and was
    removed 2026-05-22. Pin the negative so it can't silently return."""
    src = _dashboard()
    for dead in ("totals-book-equity", "totals-book-pnl",
                 "totals-book-cash", "totals-book-positions"):
        assert dead not in src, (
            f"{dead} reappeared in the overview footer. Book-wide "
            "equity/P&L/cash/position totals are not additive across "
            "strategies — compare by the per-account P&L % column instead."
        )


def test_overview_footer_keeps_ai_cost_total():
    """AI cost IS additive book-wide and stays in the footer."""
    src = _dashboard()
    assert "totals-book-cost" in src, (
        "AI Cost Total cell missing from the overview footer — it's the "
        "one book-wide total that remains meaningful."
    )


def test_dashboard_totals_api_includes_per_row_pnl_keys():
    """The /api/dashboard-totals payload must include `pnl` and `pnl_pct`
    per profile — JS reads these to refresh the per-row cells every 30s."""
    src = _views()
    assert '"pnl": pnl' in src, (
        "api_dashboard_totals per-profile row missing pnl key"
    )
    assert '"pnl_pct": pnl_pct' in src, (
        "api_dashboard_totals per-profile row missing pnl_pct key — the "
        "P&L % column would never refresh after first paint"
    )


def test_dashboard_totals_api_dropped_dead_book_totals():
    """The endpoint must not recompute the removed book-wide totals on
    every 30s poll (dead work + a vector for the dead UI to return)."""
    src = _views()
    # Scope the check to the api_dashboard_totals function body.
    start = src.index("def api_dashboard_totals(")
    end = src.index("\n@views_bp.route", start)
    body = src[start:end]
    for dead in ('"total_equity"', '"total_pnl"',
                 '"total_cash"', '"total_positions"'):
        assert dead not in body, (
            f"{dead} is still built in api_dashboard_totals — it has no "
            "consumer since the footer totals were removed 2026-05-22."
        )
    assert '"total_cost"' in body, (
        "api_dashboard_totals must still return total_cost (the AI-cost "
        "footer total)."
    )


def test_pnl_color_branches_present_in_js():
    """Green for positive, red for negative, neutral for zero — applied to
    both the P&L and P&L % cells."""
    src = _dashboard()
    assert "#2e7d32" in src, "Positive P&L color (green) missing"
    assert "#c62828" in src, "Negative P&L color (red) missing"
