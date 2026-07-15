"""P&L surfaces must reconcile and state their basis (2026-07-15).

Operator-caught: /trades showed ONLY "Realized P&L +$721.12" under
every view tab — which read as a stuck filter. Root cause: the trades
view referenced `get_account_info` WITHOUT importing it in that scope;
the NameError was swallowed into a warning on every render since the
header shipped, so the Unrealized/Total spans never once appeared.
Meanwhile /performance's headline said +6.2% (as of last close) while
the dashboard said +5.46% (live) with neither stating its basis, and
a card literally labeled "Total P&L" showed realized-only money.

Pinned here:
  - the trades header renders ALL THREE money totals when equity is
    readable, and an EXPLICIT unavailable reason when it is not
    (silently hiding money columns is banned);
  - the performance page labels the realized card "Realized P&L",
    stamps snapshot-derived figures "as of <date> close", and shows a
    live reconciliation line that ties to the dashboard;
  - the undefined-name CLASS is pinned dead across the core modules
    (pyflakes) — this bug and the `_logging` NameError in
    open_options_capital_at_risk's error handler were both instances.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

# Reuse the real-DB + logged-in-client harness from the no-500
# guardrail (same schema/init path as prod).
from tests.test_no_500_per_profile import (   # noqa: F401
    patched_user_with_profiles, _logged_in_client,
)


def _fake_account(equity):
    def _f(ctx=None, api=None):
        return {"equity": equity, "cash": equity,
                "buying_power": equity, "status": "ACTIVE"}
    return _f


class TestTradesHeaderReconciles:
    def test_all_three_totals_render_with_readable_equity(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        with patch("client.get_account_info",
                   side_effect=_fake_account(100_000.0)):
            resp = client.get("/trades?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Realized P&amp;L" in html
        assert "Unrealized (open)" in html, (
            "the Unrealized span is missing — this is the exact "
            "swallowed-NameError regression the operator caught")
        assert "matches the dashboard" in html
        assert "unavailable" not in html.split("Unrealized (open)")[0] \
            or "Total unavailable" not in html

    def test_unreadable_equity_says_so_instead_of_hiding(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        with patch("client.get_account_info",
                   side_effect=RuntimeError("marks outage")):
            resp = client.get("/trades?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Unrealized / Total unavailable" in html, (
            "an unreadable live valuation must EXPLAIN itself — "
            "silent absence reads as a broken filter")

    def test_all_profiles_view_explains_the_missing_split(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        resp = client.get("/trades")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "select a single profile" in html

    def test_summary_reasons_unit(self):
        from views import _trades_pnl_summary
        multi = _trades_pnl_summary([1, 2])
        assert multi["total"] is None
        assert "single profile" in multi["total_unavailable_reason"]
        solo_no_eq = _trades_pnl_summary([999999], {}, {})
        assert solo_no_eq["total"] is None
        assert "could not be fetched" in \
            solo_no_eq["total_unavailable_reason"]

    def test_view_tabs_name_rows_not_money(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "trades.html")).read()
        assert "Closed (realized)" in src
        assert "Open (unrealized)" in src
        assert "filter the <strong>list of rows</strong>" in src


class TestPerformancePageBasis:
    def test_realized_card_label_and_live_recon(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        with patch("client.get_account_info",
                   side_effect=_fake_account(100_000.0)):
            resp = client.get("/performance?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Realized P&amp;L" in html
        assert "Total P&amp;L" not in html, (
            "the realized-only card must not be labeled Total")
        assert "Live right now:" in html
        assert "matches the dashboard" in html

    def test_as_of_stamp_when_snapshots_exist(
            self, patched_user_with_profiles, tmp_path, monkeypatch):
        import sqlite3
        # seed one snapshot for profile 1 so the as-of stamp renders
        db = str(tmp_path / "quantopsai_profile_1.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO daily_snapshots (date, equity) "
                "VALUES ('2026-07-14', 26000.0)")
            conn.commit()
        finally:
            conn.close()
        # the route resolves per-profile DBs by RELATIVE path — point
        # the working directory at the fixture's DBs
        monkeypatch.chdir(tmp_path)
        client, _app = _logged_in_client()
        with patch("client.get_account_info",
                   side_effect=_fake_account(100_000.0)):
            resp = client.get("/performance?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "as of 2026-07-14 close" in html

    def test_dashboard_states_live_basis(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "dashboard.html")).read()
        assert "· live" in src
        assert "marked LIVE at page load" in src


class TestUndefinedNameClass:
    def test_core_modules_have_no_undefined_names(self):
        """The class pin: `get_account_info` in views.trades and
        `_logging` in journal's options-risk handler were both
        undefined names inside swallowed-exception paths — invisible
        to the import check and the suite, fatal to the feature. Any
        undefined name in a core module fails here."""
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter
        import io
        core = [
            "views.py", "journal.py", "trade_pipeline.py", "models.py",
            "multi_scheduler.py", "screener.py", "order_guard.py",
            "options_trader.py", "self_tuning.py", "bracket_orders.py",
            "ai_analyst.py", "pipelines/__init__.py",
            "pipelines/option.py", "pipelines/tuning_writer.py",
        ]
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        problems = []
        for f in core:
            out, err = io.StringIO(), io.StringIO()
            checkPath(os.path.join(root, f), Reporter(out, err))
            for line in out.getvalue().splitlines():
                if "undefined name" in line:
                    problems.append(line)
        assert not problems, (
            "undefined names in core modules (each is a latent "
            "NameError, usually inside a swallowed except):\n"
            + "\n".join(problems))
