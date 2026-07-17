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
    # NOTE: the trades header's equity fan-out goes through
    # views._safe_account_info, which the patched_user_with_profiles
    # fixture already fakes (equity by profile_id: 1→$100K, 5→$25K,
    # 10→$50K; initial capital $25K each). Tests below re-patch that
    # SAME seam to simulate outages — patching client.get_account_info
    # would be a no-op here and a wrong-signature-mock trap.

    def test_all_three_totals_render_with_readable_equity(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        resp = client.get("/trades?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Total P&amp;L" in html
        assert "Unrealized (open)" in html, (
            "the Unrealized span is missing — this is the exact "
            "swallowed-NameError regression the operator caught")
        assert "closed trade" in html
        assert "matches the dashboard" in html
        assert "Unrealized / Total unavailable" not in html

    def test_unreadable_equity_says_so_instead_of_hiding(
            self, patched_user_with_profiles):
        client, _app = _logged_in_client()
        with patch("views._safe_account_info", return_value=None):
            resp = client.get("/trades?profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Unrealized / Total unavailable" in html, (
            "an unreadable live valuation must EXPLAIN itself — "
            "silent absence reads as a broken filter")
        assert "could not be fetched" in html

    def test_all_profiles_view_shows_aggregate_total(
            self, patched_user_with_profiles):
        """Operator 2026-07-15: the aggregate view used to punt with
        'select a single profile to see the live Unrealized and Total
        split'. The dashboard values every profile live on one page,
        so the trades page must too — Σ(equity) − Σ(initial capital).
        The aggregate Total is tagged 'Σ across N profiles', NOT
        'matches the dashboard' (the dashboard deliberately shows no
        book-wide total to match against)."""
        client, _app = _logged_in_client()
        resp = client.get("/trades")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "select a single profile" not in html
        assert "Unrealized (open)" in html
        assert "Σ across 3 profiles" in html
        assert "matches the dashboard" not in html
        # fixture equities: 100K + 25K + 50K − 3×25K initial = +100K
        assert "+$100,000.00" in html

    def test_all_profiles_partial_outage_names_the_gap(
            self, patched_user_with_profiles):
        """One book's valuation unreadable → the aggregate must NOT
        silently book it at $0 P&L; it says how many are missing."""
        def _one_down(ctx):
            if getattr(ctx, "profile_id", None) == 5:
                return None
            return {"equity": 100_000.0, "cash": 100_000.0,
                    "buying_power": 100_000.0, "status": "ACTIVE"}
        client, _app = _logged_in_client()
        with patch("views._safe_account_info", side_effect=_one_down):
            resp = client.get("/trades")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Unrealized / Total unavailable" in html
        assert "could not be fetched for 1 of 3 profiles" in html

    def test_summary_reasons_unit(self, tmp_path, monkeypatch):
        import os as _os
        from journal import init_db as _init_journal_db
        from views import _trades_pnl_summary
        # isolate: the summary reads profile DBs by RELATIVE path —
        # without the chdir this test wrote DB files into the repo
        # root (review 2026-07-16 #7)
        monkeypatch.chdir(tmp_path)
        for pid in (1, 2):
            _init_journal_db(f"quantopsai_profile_{pid}.db")
        # multi-profile WITH full live equity → real aggregate split
        multi = _trades_pnl_summary(
            [1, 2],
            {1: 30_000.0, 2: 20_000.0},
            {1: 25_000.0, 2: 25_000.0})
        assert multi["total"] == pytest.approx(0.0)
        assert multi["total_unavailable_reason"] is None
        assert multi["realized_incomplete"] is False
        # any missing equity → fail-HONEST, never a silently-short sum
        partial = _trades_pnl_summary(
            [1, 2], {1: 30_000.0}, {1: 25_000.0, 2: 25_000.0})
        assert partial["total"] is None
        assert "could not be fetched" in \
            partial["total_unavailable_reason"]
        # missing capital baseline is a DIFFERENT failure than a
        # marks outage and must not be misreported as one
        bad_cap = _trades_pnl_summary([1], {1: 30_000.0}, {1: 0.0})
        assert bad_cap["total"] is None
        assert "initial-capital baseline" in \
            bad_cap["total_unavailable_reason"]
        # an unreadable BOOK poisons realized AND the split: realized
        # is stamped incomplete, totals withheld with the read reason
        # (unrealized = total − realized would be inflated by exactly
        # the missing book — review 2026-07-16 #2)
        _os.mkdir("quantopsai_profile_3.db")  # dir-as-db = unreadable
        unread = _trades_pnl_summary(
            [1, 3], {1: 30_000.0, 3: 30_000.0},
            {1: 25_000.0, 3: 25_000.0})
        assert unread["total"] is None
        assert unread["realized_incomplete"] is True
        assert "could not be read" in \
            unread["total_unavailable_reason"]

    @pytest.mark.parametrize("query,lead,headline", [
        ("", "all", "Total P&amp;L"),
        ("view=realized&", "realized", "Realized P&amp;L"),
        ("view=unrealized&", "unrealized", "Unrealized P&amp;L (open)"),
    ])
    def test_headline_follows_the_active_tab(
            self, patched_user_with_profiles, query, lead, headline):
        """Operator 2026-07-15: 'it doesn't make sense to change tabs
        and only ever see the realized'. The active tab decides the
        headline metric; the other two stay visible smaller."""
        client, _app = _logged_in_client()
        resp = client.get(f"/trades?{query}profile_id=1")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f'data-lead="{lead}"' in html
        bar = html.split('id="pnl-lead"', 1)[1]
        first_label = min(
            (bar.index(s), s) for s in
            ("Total P&amp;L", "Realized P&amp;L",
             "Unrealized P&amp;L (open)") if s in bar)[1]
        assert first_label == headline, (
            f"tab {lead!r} must lead with {headline!r}, "
            f"got {first_label!r}")

    def test_view_tabs_name_rows_not_money(self):
        src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "templates", "trades.html")).read()
        assert "Closed (realized)" in src
        assert "Open (unrealized)" in src
        assert "filter the <strong>list of rows</strong>" in src
        assert "set the headline total to match" in src


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


class TestDegradedBookHonesty:
    """An unreadable trades table must never surface as fabricated
    money (review 2026-07-16 #1). journal.get_virtual_account_info
    keeps its initial-capital fallback for long-standing consumers,
    but stamps it degraded=True — and every money DISPLAY/PERSIST
    path treats degraded as unavailable:
      - views._safe_account_info → None (and never caches it), which
        covers the dashboard rows, api_portfolio, and the trades
        header fan-out in one seam;
      - multi_scheduler._task_daily_snapshot REFUSES to write (a
        poisoned daily_snapshots row is permanent history);
      - performance's live-recon line and the notification emails
        omit the section (their existing fetch-failure paths).
    Trading entries were already safe: the buy-side cash door calls
    get_virtual_cash itself and fails CLOSED on the same None."""

    def test_journal_stamps_degraded_on_unreadable_book(
            self, tmp_path, monkeypatch):
        from journal import get_virtual_account_info, init_db
        monkeypatch.chdir(tmp_path)
        # An openable file with NO trades table: the SELECT inside
        # get_virtual_cash fails → returns None → the fallback dict.
        # (A file sqlite can't even OPEN raises out of _get_conn
        # instead — that path is honest already; this one was the
        # silent fabricator.)
        open("broken.db", "w").close()
        info = get_virtual_account_info(
            db_path="broken.db", initial_capital=25_000.0)
        assert info["degraded"] is True
        assert info["equity"] == 25_000.0  # documented fallback shape
        # a READABLE book must not carry the flag
        init_db("ok.db")
        ok = get_virtual_account_info(
            db_path="ok.db", initial_capital=25_000.0)
        assert not ok.get("degraded")

    def test_safe_account_info_treats_degraded_as_unavailable(self):
        from types import SimpleNamespace
        import views
        ctx = SimpleNamespace(
            db_path="degraded_seam_test.db",
            display_name="seam-test", segment="seam-test")
        views._dashboard_cache.pop(
            "account_degraded_seam_test.db", None)
        degraded = {"equity": 25_000.0, "cash": 25_000.0,
                    "buying_power": 25_000.0, "portfolio_value": 0.0,
                    "status": "ACTIVE", "degraded": True}
        with patch("client.get_account_info", return_value=degraded):
            assert views._safe_account_info(ctx) is None
        assert "account_degraded_seam_test.db" not in \
            views._dashboard_cache, (
                "a degraded result is a FAILURE and failures are "
                "never cached — caching it would pin 'unavailable' "
                "for 30s after the book recovers")

    def test_daily_snapshot_refuses_fabricated_equity(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import multi_scheduler
        ctx = SimpleNamespace(db_path="snap_refuse_test.db",
                              display_name="snap-test",
                              segment="snap-test", profile_id=1)
        degraded = {"equity": 25_000.0, "cash": 25_000.0,
                    "buying_power": 25_000.0, "portfolio_value": 0.0,
                    "status": "ACTIVE", "degraded": True}
        snap_writer = MagicMock()
        with patch("journal.init_db"), \
                patch("journal.log_daily_snapshot", snap_writer), \
                patch("client.get_account_info",
                      return_value=degraded), \
                patch("client.get_positions", return_value=[]):
            with pytest.raises(RuntimeError, match="degraded"):
                multi_scheduler._task_daily_snapshot(ctx)
        assert not snap_writer.called, (
            "a degraded book must never persist fabricated equity "
            "into daily_snapshots — that table is permanent history")

    def test_performance_live_recon_omitted_on_degraded(
            self, patched_user_with_profiles):
        degraded = {"equity": 25_000.0, "cash": 25_000.0,
                    "buying_power": 25_000.0, "portfolio_value": 0.0,
                    "status": "ACTIVE", "degraded": True}
        client, _app = _logged_in_client()
        with patch("client.get_account_info", return_value=degraded):
            resp = client.get("/performance?profile_id=1")
        assert resp.status_code == 200
        assert "Live right now:" not in resp.get_data(as_text=True), (
            "the live-recon line must not present the journal's "
            "initial-capital fallback as live truth")


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
