"""2026-06-18 — the /trades ledger muddled realized and unrealized rows:
a still-open position (live unrealized mark) sat next to a finalized
closed trade (locked-in P&L) in the same table. Add an All / Realized /
Unrealized toggle (orthogonal to the Stocks/Options `kind` tab), filtered
at the SQL layer by trade status.

Partition (see views._FINALIZED_TRADE_STATUSES):
  * Realized   = finalized status (closed / canceled / expired / rejected
                 / done_for_day / auto_reconciled_phantom_close) OR a
                 booked realized `pnl` (covers filled round-trips whose
                 close row carries pnl but keeps status='filled').
  * Unrealized = NOT a finalized status AND no booked pnl — a still-open
                 lot (open / filled / pending_fill / pending /
                 needs_review / any non-finalized status) currently held.

The finalized-status set is the same exclusion `get_virtual_positions`
uses for ENTRY lots, so the Unrealized view's open-lot rows line up with
the dashboard's held entries. (One known divergence: a position held only
as the leftover qty of an oversold/reversed exit row has no open entry
row of its own and stays under Realized — oversells are anomalous and
prevented upstream; the dashboard is authoritative for that case.)
"""
import os
import re
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

REPO = os.path.join(os.path.dirname(__file__), os.pardir)
sys.path.insert(0, REPO)


@pytest.fixture
def tmp_profile_db_with_lifecycle_rows(monkeypatch):
    """Seed one profile DB with a row in every lifecycle bucket."""
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 777
    db_path = f"quantopsai_profile_{profile_id}.db"
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    rows = [
        # symbol, side, qty, price, pnl, status
        ("OPENBUY", "buy", 10, 100.0, None, "open"),            # held
        ("PENDBUY", "buy", 5, 50.0, None, "pending_fill"),      # in-flight
        ("FILLOPEN", "buy", 10, 100.0, None, "filled"),         # held (filled entry, no pnl)
        ("NEEDSREV", "buy", 10, 100.0, None, "needs_review"),   # held per dashboard (post-expiry review)
        ("CLOSEW", "sell", 10, 110.0, 100.0, "closed"),         # realized win
        ("CLOSEL", "sell", 10, 90.0, -100.0, "closed"),         # realized loss
        ("FILLCLOSE", "sell", 10, 110.0, 75.0, "filled"),       # filled close WITH booked pnl
        ("SELLOPENPNL", "sell", 10, 90.0, -20.0, "open"),       # transient: pnl booked, status not yet flipped
        ("EXPIRED", "sell", 10, 0.0, None, "expired"),          # never filled
        ("CANCELD", "buy", 10, 0.0, None, "canceled"),          # never filled
        # pending_protective must NEVER show on /trades (any view).
        ("PROT", "buy", 10, 0.0, None, "pending_protective"),
    ]
    for sym, side, qty, price, pnl, status in rows:
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, "
            "pnl, signal_type, status) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-06-17T10:00:00", sym, side, qty, price, pnl, "BUY", status),
        )
    conn.commit()
    conn.close()
    return profile_id


def _names(rows):
    return {r["symbol"] for r in rows}


# ── Data-layer filter ──────────────────────────────────────────────────

class TestViewFilter:
    def test_all_excludes_only_pending_protective(
            self, tmp_profile_db_with_lifecycle_rows):
        from views import _get_trade_history_for_profile
        rows = _get_trade_history_for_profile(
            tmp_profile_db_with_lifecycle_rows, limit=100, view=None)
        # all real rows; pending_protective filtered by the base WHERE.
        assert _names(rows) == {
            "OPENBUY", "PENDBUY", "FILLOPEN", "NEEDSREV", "CLOSEW",
            "CLOSEL", "FILLCLOSE", "SELLOPENPNL", "EXPIRED", "CANCELD"}
        assert "PROT" not in _names(rows)

    def test_realized_returns_only_finalized(
            self, tmp_profile_db_with_lifecycle_rows):
        from views import _get_trade_history_for_profile
        rows = _get_trade_history_for_profile(
            tmp_profile_db_with_lifecycle_rows, limit=100, view="realized")
        # closed + never-filled + a FILLED close (pnl present) + a still-
        # 'open'-status sell that already booked pnl (pnl present wins).
        assert _names(rows) == {
            "CLOSEW", "CLOSEL", "FILLCLOSE", "SELLOPENPNL",
            "EXPIRED", "CANCELD"}

    def test_unrealized_returns_only_open_lots_without_pnl(
            self, tmp_profile_db_with_lifecycle_rows):
        from views import _get_trade_history_for_profile
        rows = _get_trade_history_for_profile(
            tmp_profile_db_with_lifecycle_rows, limit=100, view="unrealized")
        # held lots only: any non-finalized status (open / pending_fill /
        # filled / needs_review) with NO booked pnl. A filled row WITH
        # pnl (FILLCLOSE) is realized.
        assert _names(rows) == {"OPENBUY", "PENDBUY", "FILLOPEN", "NEEDSREV"}

    def test_needs_review_is_held_matching_dashboard(
            self, tmp_profile_db_with_lifecycle_rows):
        """needs_review (broker still holds contracts post-expiry, no
        booked pnl) is counted as a live lot by get_virtual_positions, so
        the Unrealized view must show it — not bury it under Realized."""
        from views import _get_trade_history_for_profile
        pid = tmp_profile_db_with_lifecycle_rows
        assert "NEEDSREV" in _names(
            _get_trade_history_for_profile(pid, view="unrealized"))
        assert "NEEDSREV" not in _names(
            _get_trade_history_for_profile(pid, view="realized"))

    def test_filled_close_with_pnl_is_realized_not_held(
            self, tmp_profile_db_with_lifecycle_rows):
        """The load-bearing edge: a closed round-trip recorded as
        status='filled' (not 'closed') but carrying a realized pnl must
        be Realized, never Unrealized."""
        from views import _get_trade_history_for_profile
        pid = tmp_profile_db_with_lifecycle_rows
        assert "FILLCLOSE" in _names(
            _get_trade_history_for_profile(pid, view="realized"))
        assert "FILLCLOSE" not in _names(
            _get_trade_history_for_profile(pid, view="unrealized"))

    def test_realized_and_unrealized_partition_all(
            self, tmp_profile_db_with_lifecycle_rows):
        """The two views are disjoint and together equal the full
        (non-protective) ledger — no row hides from both, none in both."""
        from views import _get_trade_history_for_profile
        pid = tmp_profile_db_with_lifecycle_rows
        allr = _names(_get_trade_history_for_profile(pid, view=None))
        real = _names(_get_trade_history_for_profile(pid, view="realized"))
        unre = _names(_get_trade_history_for_profile(pid, view="unrealized"))
        assert real & unre == set()
        assert real | unre == allr

    def test_view_composes_with_kind(
            self, tmp_profile_db_with_lifecycle_rows):
        """view + kind are orthogonal; both filters apply together."""
        from views import _get_trade_history_for_profile
        pid = tmp_profile_db_with_lifecycle_rows
        # all seeded rows are stocks (occ_symbol NULL) → realized+stocks
        # equals realized; realized+options equals empty.
        assert _names(_get_trade_history_for_profile(
            pid, view="realized", kind="stocks")) == {
                "CLOSEW", "CLOSEL", "FILLCLOSE", "SELLOPENPNL",
                "EXPIRED", "CANCELD"}
        assert _get_trade_history_for_profile(
            pid, view="realized", kind="options") == []


# ── Route smoke test ───────────────────────────────────────────────────

class TestTradesRouteAcceptsViewParam:
    def _client(self):
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True
        return app.test_client()

    def _admin(self):
        u = MagicMock()
        u.is_authenticated = True
        u.id = 1
        u.is_admin = True
        u.is_viewer = False
        u.role = "admin"
        u.email = "a@x.com"
        u.display_name = "Admin"
        u.effective_user_id = 1
        return u

    def _run(self, monkeypatch, url):
        captured = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append(view)
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "stocks"}],
        )
        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get(url)
        return r, captured

    @pytest.mark.parametrize("param,expected", [
        ("realized", "realized"),
        ("unrealized", "unrealized"),
    ])
    def test_view_param_threads_to_data_layer(self, monkeypatch, param,
                                               expected):
        r, captured = self._run(monkeypatch, f"/trades?view={param}")
        assert r.status_code == 200
        assert captured and all(v == expected for v in captured), captured

    def test_no_view_param_is_all(self, monkeypatch):
        r, captured = self._run(monkeypatch, "/trades")
        assert r.status_code == 200
        assert all(v is None for v in captured)

    def test_garbage_view_falls_back_to_all(self, monkeypatch):
        r, captured = self._run(monkeypatch, "/trades?view=' OR 1=1 --")
        assert r.status_code == 200
        assert all(v is None for v in captured)

    def test_search_is_url_encoded_in_nav_links(self, monkeypatch):
        """A search containing '&' must be percent-encoded in the tab
        hrefs, or it splits the query string and drops view/kind/sort."""
        r, _ = self._run(monkeypatch, "/trades?search=A%26B")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "search=A%26B" in html        # encoded, one param value
        assert "search=A&B" not in html      # never a raw delimiter

    def test_garbage_sort_and_dir_fall_back(self, monkeypatch):
        """sort/dir are reflected into an inline <script>; unknown values
        must clamp to the defaults, not pass through and break it."""
        r, _ = self._run(monkeypatch, "/trades?sort=__nope__&dir=sideways")
        html = r.get_data(as_text=True)
        assert r.status_code == 200
        assert "var currentSort = 'timestamp'" in html
        assert "var currentDir = 'desc'" in html

    def test_view_composes_with_kind_in_route(self, monkeypatch):
        """Both filters thread through together (orthogonal axes)."""
        captured = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append((kind, view))
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "stocks"}],
        )
        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get("/trades?kind=options&view=realized")
        assert r.status_code == 200
        assert all(c == ("options", "realized") for c in captured), captured


# ── Static guard: every visible toggle option is exercised above ───────

def test_visible_view_options_match_tested_set():
    """The toggle's visible non-'All' options in trades.html must be
    exactly the set the route/data tests cover — so a new tab can't ship
    without a test (mirrors the kind-tab smoke-test convention)."""
    tmpl = os.path.join(REPO, "templates", "trades.html")
    with open(tmpl) as f:
        html = f.read()
    # Toggle option links look like  ...view=realized&...  /  view=unrealized
    rendered_options = set(re.findall(r"view=(realized|unrealized)\b", html))
    tested_options = {"realized", "unrealized"}
    assert rendered_options == tested_options, (
        f"trades.html exposes view options {rendered_options} but tests "
        f"cover {tested_options}; add a test for any new option.")


def test_finalized_status_set_matches_get_virtual_positions_exclusion():
    """The realized/unrealized split keys off the shared finalized set,
    which must equal get_virtual_positions' ENTRY-lot exclusion (minus
    pending_protective, already dropped by the /trades base filter) — so
    a status held by the dashboard isn't shown as Realized here."""
    from views import _FINALIZED_TRADE_STATUSES
    fin = set(_FINALIZED_TRADE_STATUSES)
    # finalized states
    assert {"closed", "canceled", "expired", "rejected", "done_for_day",
            "auto_reconciled_phantom_close"} <= fin
    # open-lifecycle states the dashboard counts as held must NOT be here
    for s in ("open", "filled", "pending_fill", "pending", "needs_review"):
        assert s not in fin
