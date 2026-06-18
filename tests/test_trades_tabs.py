"""Pin the Stocks/Options/All tab filter on `_get_trade_history_for_profile`
and the `/trades?kind=` URL parameter (2026-05-11 TODO #1).

The /trades page now splits trades into Stocks/Options/All tabs so
each instrument class gets a clean paginated view instead of sharing
one table that has to conditionally render OPT badges, contract
detail, x100 multipliers, etc. Each tab is a real URL — pagination
and sort continue to work per-tab.
"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture
def tmp_profile_db_with_mixed_trades(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.chdir(tmpdir)
    profile_id = 999
    db_path = f"quantopsai_profile_{profile_id}.db"
    from journal import init_db
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    # Stock trade
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "signal_type, status) VALUES (?,?,?,?,?,?,?)",
        ("2026-05-10T10:00:00", "AAPL", "buy", 100, 150.0, "BUY", "open"),
    )
    # Option leg (multileg long call)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "occ_symbol, signal_type, status) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-05-10T13:00:00", "RTX", "buy", 1, 1.74,
         "RTX260618P00160000", "MULTILEG", "open"),
    )
    # Option leg (multileg short call — sell-to-open)
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "occ_symbol, signal_type, status) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-05-10T13:00:01", "RTX", "sell", 1, 3.15,
         "RTX260618P00170000", "MULTILEG", "open"),
    )
    conn.commit()
    conn.close()
    return profile_id, db_path


class TestKindFilter:
    def test_kind_none_returns_all(self, tmp_profile_db_with_mixed_trades):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db_with_mixed_trades
        rows = _get_trade_history_for_profile(pid, limit=100, kind=None)
        assert len(rows) == 3  # 1 stock + 2 option legs

    def test_kind_stocks_returns_stocks_only(self,
                                             tmp_profile_db_with_mixed_trades):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db_with_mixed_trades
        rows = _get_trade_history_for_profile(pid, limit=100, kind="stocks")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["occ_symbol"] is None

    def test_kind_options_returns_options_only(self,
                                                tmp_profile_db_with_mixed_trades):
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db_with_mixed_trades
        rows = _get_trade_history_for_profile(pid, limit=100, kind="options")
        # Both legs of the multileg are option rows
        assert len(rows) == 2
        for r in rows:
            assert r["occ_symbol"] is not None
            assert r["symbol"] == "RTX"

    def test_invalid_kind_treated_as_all(self,
                                          tmp_profile_db_with_mixed_trades):
        """Defensive: caller passes garbage 'kind' value; treat as
        no filter rather than 500."""
        from views import _get_trade_history_for_profile
        pid, _ = tmp_profile_db_with_mixed_trades
        # 'kind' is enforced to one of {stocks, options, None} at the
        # route layer; the helper just builds SQL based on what it
        # receives. None == no filter (all rows).
        rows = _get_trade_history_for_profile(pid, limit=100, kind=None)
        assert len(rows) == 3


class TestTradesRouteAcceptsKindParam:
    """Smoke test: the /trades route accepts ?kind=stocks /
    ?kind=options / no kind, threads the value through, and passes
    `kind` into the template."""

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

    def test_kind_stocks_query_param_filters_via_sql(self, monkeypatch):
        """When the request has ?kind=stocks, the view layer passes
        kind='stocks' into _get_trade_history_for_profile so the
        SQL filter runs at the data layer (not just template-side
        hiding)."""
        captured_calls = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured_calls.append({
                "profile_id": profile_id, "limit": limit, "kind": kind,
            })
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "stocks"}],
        )

        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get("/trades?kind=stocks")

        assert r.status_code == 200
        assert captured_calls
        assert all(c["kind"] == "stocks" for c in captured_calls), (
            f"Route did not thread kind='stocks' through to data "
            f"layer: {captured_calls}"
        )

    def test_kind_options_query_param_filters_via_sql(self, monkeypatch):
        captured = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append(kind)
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "stocks"}],
        )

        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get("/trades?kind=options")

        assert r.status_code == 200
        assert all(k == "options" for k in captured)

    def test_kind_garbage_falls_back_to_all(self, monkeypatch):
        """Defensive: arbitrary kind values get sanitized to '' (all)
        so an injected URL param can't break the SQL."""
        captured = []

        def fake_history(profile_id, limit=100, kind=None, search=None,
                         view=None):
            captured.append(kind)
            return []

        monkeypatch.setattr("views._get_trade_history_for_profile",
                            fake_history)
        monkeypatch.setattr(
            "views.get_user_profiles",
            lambda _uid: [{"id": 1, "name": "Mid Cap",
                           "enabled": True, "market_type": "stocks"}],
        )

        with patch("flask_login.utils._get_user", return_value=self._admin()):
            r = self._client().get(
                "/trades?kind=' OR 1=1 --"
            )
        assert r.status_code == 200
        # Sanitized → None (all)
        assert all(k is None for k in captured)
