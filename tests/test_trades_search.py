"""Pin the symbol-search filter on /trades (TODO #3, 2026-05-11).

Adds a `?search=<symbol>` URL parameter and a search input on
templates/trades.html. Filters at the SQL level — case-insensitive
prefix match on `symbol` AND on `occ_symbol`'s underlying root, so
"CWAN" matches both stock CWAN trades AND CWAN option leg trades.
SQL-injection-safe via parameter binding.
"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def tmp_profile_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 999
    db_path = f"quantopsai_profile_{profile_id}.db"
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    # Stock trades on different symbols
    conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, "
                 "price, signal_type, status) VALUES (?,?,?,?,?,?,?)",
                 ("2026-05-10T10:00:00", "CWAN", "buy", 100, 24.0,
                  "BUY", "open"))
    conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, "
                 "price, signal_type, status) VALUES (?,?,?,?,?,?,?)",
                 ("2026-05-10T11:00:00", "AAPL", "buy", 50, 150.0,
                  "BUY", "open"))
    # CWAN option leg
    conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, "
                 "price, occ_symbol, signal_type, status) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("2026-05-10T13:00:00", "CWAN", "buy", 1, 4.80,
                  "CWAN260612C00026000", "MULTILEG", "open"))
    # AAPL option leg (to test that "CWAN" search excludes AAPL options)
    conn.execute("INSERT INTO trades (timestamp, symbol, side, qty, "
                 "price, occ_symbol, signal_type, status) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("2026-05-10T14:00:00", "AAPL", "buy", 1, 5.0,
                  "AAPL260612C00150000", "MULTILEG", "open"))
    conn.commit()
    conn.close()
    return profile_id, db_path


class TestSearchFilter:
    def test_search_none_returns_all(self, tmp_profile_db):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows = _get_trade_history_for_profile(pid, search=None)
        assert len(rows) == 4

    def test_search_empty_returns_all(self, tmp_profile_db):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows = _get_trade_history_for_profile(pid, search="")
        assert len(rows) == 4

    def test_search_matches_stock_and_option_for_underlying(self,
                                                             tmp_profile_db):
        """'CWAN' must match the stock CWAN row AND the CWAN option
        leg row, but NOT AAPL stock or AAPL option."""
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows = _get_trade_history_for_profile(pid, search="CWAN")
        symbols_and_occs = [(r["symbol"], r.get("occ_symbol"))
                            for r in rows]
        assert ("CWAN", None) in symbols_and_occs
        assert ("CWAN", "CWAN260612C00026000") in symbols_and_occs
        # AAPL excluded
        for sym, occ in symbols_and_occs:
            assert sym != "AAPL"

    def test_search_case_insensitive(self, tmp_profile_db):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows_upper = _get_trade_history_for_profile(pid, search="CWAN")
        rows_lower = _get_trade_history_for_profile(pid, search="cwan")
        assert len(rows_upper) == len(rows_lower) == 2

    def test_search_no_match_returns_empty(self, tmp_profile_db):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows = _get_trade_history_for_profile(pid, search="ZZZZ")
        assert rows == []

    def test_search_combined_with_kind_options(self, tmp_profile_db):
        """Search + kind compose: 'CWAN' + kind='options' returns only
        the CWAN option leg (not the CWAN stock)."""
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        rows = _get_trade_history_for_profile(pid, kind="options",
                                               search="CWAN")
        assert len(rows) == 1
        assert rows[0]["occ_symbol"] == "CWAN260612C00026000"

    def test_search_sql_injection_safe(self, tmp_profile_db):
        """A search term with SQL syntax must be treated as a literal,
        not interpolated into the query. The bind-parameter approach
        guarantees this."""
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db
        # Classic injection attempt — should match nothing, NOT
        # bypass the filter and return all rows.
        rows = _get_trade_history_for_profile(
            pid, search="' OR 1=1 --",
        )
        assert rows == []


class TestRouteAcceptsSearch:
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

    def test_search_param_threaded_into_data_layer(self, monkeypatch):
        captured = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append({"kind": kind, "search": search})
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "midcap"}],
        )

        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get("/trades?search=CWAN")

        assert r.status_code == 200
        assert all(c["search"] == "CWAN" for c in captured), (
            f"Route did not thread search='CWAN' through: {captured}"
        )

    def test_search_whitespace_stripped(self, monkeypatch):
        captured = []
        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append(search)
            return []
        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "M", "enabled": True,
                           "market_type": "midcap"}],
        )
        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get("/trades?search=%20%20CWAN%20%20")
        assert r.status_code == 200
        assert all(s == "CWAN" for s in captured)

    def test_search_oversized_truncated(self, monkeypatch):
        """Defensive: a 1000-char search input gets capped at 32 chars
        so it can't be used to construct an absurd query."""
        captured = []
        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append(search)
            return []
        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "M", "enabled": True,
                           "market_type": "midcap"}],
        )
        big = "A" * 1000
        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get(f"/trades?search={big}")
        assert r.status_code == 200
        assert all(len(s) == 32 for s in captured), (
            f"Search not truncated to 32 chars: lengths={[len(s) for s in captured]}"
        )
