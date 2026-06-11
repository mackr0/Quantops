"""Dashboard trading-halt banner (2026-06-10 PM).

Operator requirement: when a profile is halted (reconciler safety
net, or any future setter of trading_profiles.trading_halted), the
DASHBOARD must show it. The 2026-06-10 all-profiles false halt ran
for half a session before the operator noticed, because the only
banner lived on /settings — a page nobody watches during the day.

Pins:
  1. Halted profile → /dashboard renders the TRADING HALTED banner
     with the profile name and the halt reason (real Flask render
     through the test client, not a source grep).
  2. No halted profiles → banner absent.
  3. The dashboard view passes halted_profiles independently of the
     per-profile account loaders (source pin) — a broken Alpaca
     credential must not be able to hide the banner.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def logged_in_with_profile(client, tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user, create_trading_profile
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={
        "email": "test@test.com",
        "password": "password123",
    }, follow_redirects=True)
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        uid = conn.execute(
            "SELECT id FROM users WHERE email='test@test.com'"
        ).fetchone()[0]
    pid = create_trading_profile(uid, "Halt Banner Test", "stocks")
    return client, tmp_main_db, pid


def _halt(db_path, pid, reason):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE trading_profiles SET trading_halted = 1, "
            "halt_reason = ?, halted_at = '2026-06-10T17:46:09' "
            "WHERE id = ?",
            (reason, pid),
        )
        conn.commit()


class TestDashboardHaltBanner:

    def test_halted_profile_shows_banner(self, logged_in_with_profile):
        client, db, pid = logged_in_with_profile
        _halt(db, pid, "Reconciler safety net: 1 synthesis action(s) "
                       "needed — profile HALTED")
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="ignore")
        assert "TRADING HALTED" in body, (
            "Halted profile did not surface the TRADING HALTED banner "
            "on /dashboard — the operator only finds out from "
            "/settings, which they don't watch intraday."
        )
        assert "Halt Banner Test" in body
        assert "Reconciler safety net" in body, (
            "Banner must show the halt reason, not just that a halt "
            "exists."
        )

    def test_no_halt_no_banner(self, logged_in_with_profile):
        client, _db, _pid = logged_in_with_profile
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8", errors="ignore")
        assert "TRADING HALTED" not in body, (
            "Banner rendered with no halted profiles — false alarms "
            "train the operator to ignore it."
        )


def test_banner_data_independent_of_account_loaders():
    """Source pin: halted_profiles is built from the raw profile rows
    (trading_halted column), NOT inside _load_profile's try/except —
    so a dead Alpaca credential or API outage cannot suppress the
    banner."""
    src = (REPO / "views.py").read_text()
    start = src.index("def dashboard()")
    end = src.index("def settings", start)
    body = src[start:end]
    build_idx = body.index("halted_profiles = [")
    loader_start = body.index("def _load_profile")
    loader_end = body.index("ThreadPoolExecutor")
    assert not (loader_start < build_idx < loader_end), (
        "halted_profiles must be built outside _load_profile — the "
        "loader swallows per-profile exceptions and would hide halts "
        "on API failures."
    )
    assert 'if p.get("trading_halted")' in body
    assert "halted_profiles=halted_profiles" in body, (
        "halted_profiles not passed to render_template — banner "
        "can never render."
    )
