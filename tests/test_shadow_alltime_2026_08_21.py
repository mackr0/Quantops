"""/shadow shows the experiment's FULL history — no rolling window.

Operator directive 2026-08-21: "it's supposed to show me the current
state at all time." The page had a rolling 30-day window that never
bit only because the arms started 2026-07-23 — from late August it
would have silently dropped the earliest scored decisions off the
back, weakening settled verdicts while everything worked.

Pins the new contract:
  - collect_fleet_metrics defaults to the ENTIRE shadow history;
  - the standings' cost column stays a 30-day run-rate (cost_30d)
    while `cost` is the lifetime total;
  - the daily trend table is display-capped at DAILY_TREND_DAYS
    most-recent days without affecting any aggregate;
  - the /shadow view never passes a `days` window to the collector.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shadow_metrics import (  # noqa: E402
    collect_fleet_metrics, DAILY_TREND_DAYS,
)
from tests.test_shadow_metrics_page_2026_07_25 import _mk_profile  # noqa: E402

PP_SELL = '{"symbol": "NVDA", "verdict": "SELL"}'


def _ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)
            ).strftime("%Y-%m-%d 14:00:00")


def _row(days_ago: int, agreement: int = 1, cost: float = 0.01):
    return (_ts(days_ago), "ensemble:pattern_recognizer", "anthropic",
            "haiku", "SELL", agreement, None, cost, 900, PP_SELL)


class TestAllTimeDefault:
    def test_rows_older_than_30_days_are_counted(self, tmp_path):
        """A 90-day-old shadow row must appear in the default metrics —
        the page is the experiment's full record, not a rolling month."""
        db = _mk_profile(tmp_path, "401",
                         shadow_rows=[_row(90), _row(1)])
        v = collect_fleet_metrics([db])["per_model"]["anthropic:haiku"]
        assert v["calls"] == 2
        assert v["graded"] == 2

    def test_explicit_days_still_windows(self, tmp_path):
        """The bounded slice remains available for callers that want
        one — it is only the PAGE default that is all-history."""
        db = _mk_profile(tmp_path, "402",
                         shadow_rows=[_row(90), _row(1)])
        v = collect_fleet_metrics([db], days=30)[
            "per_model"]["anthropic:haiku"]
        assert v["calls"] == 1

    def test_cost_total_vs_30d_run_rate(self, tmp_path):
        """`cost` is lifetime spend; `cost_30d` counts only the last
        30 days — the standings column answers "what is this arm
        costing me NOW", never the lifetime bill."""
        db = _mk_profile(tmp_path, "403",
                         shadow_rows=[_row(90, cost=1.00),
                                      _row(1, cost=0.25)])
        m = collect_fleet_metrics([db])
        v = m["per_model"]["anthropic:haiku"]
        assert v["cost"] == 1.25
        assert v["cost_30d"] == 0.25
        assert m["overview"]["cost"] == 1.25
        assert m["overview"]["cost_30d"] == 0.25

    def test_daily_trend_capped_but_aggregates_are_not(self, tmp_path):
        """The per-day table keeps only the latest DAILY_TREND_DAYS
        days; the totals above it still count every row."""
        n_days = DAILY_TREND_DAYS + 5
        rows = [_row(d) for d in range(n_days)]
        db = _mk_profile(tmp_path, "404", shadow_rows=rows)
        m = collect_fleet_metrics([db])
        assert m["per_model"]["anthropic:haiku"]["calls"] == n_days
        assert len(m["daily"]) == DAILY_TREND_DAYS
        # The kept days are the MOST RECENT ones (day strings taken
        # from the inserted rows so a UTC midnight crossing mid-test
        # can't flake the comparison).
        assert max(m["daily"]) == rows[0][0][:10]
        assert rows[-1][0][:10] not in m["daily"]

    def test_shadow_view_never_passes_a_window(self):
        """The page route must call the collector with its all-history
        default — reintroducing `days=` on the view recreates the
        silent evidence-drop this file exists to prevent."""
        import inspect
        import views
        src = inspect.getsource(views.shadow_page.__wrapped__
                                if hasattr(views.shadow_page, "__wrapped__")
                                else views.shadow_page)
        call = src.split("collect_fleet_metrics(", 1)[1]
        assert "days" not in call.split(")")[0], (
            "shadow_page passes a days= window to collect_fleet_metrics; "
            "the /shadow page must show the full experiment history "
            "(operator directive 2026-08-21)")
