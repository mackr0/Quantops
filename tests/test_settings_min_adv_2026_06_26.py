"""`min_adv` Settings round-trip + wiring (2026-06-26).

`feedback_ui_buttons_must_have_smoke_tests.md`: a form field that compiles
is not a field that works. This proves the operator-visible round-trip for
the new Min Avg Daily $ Volume input end to end —

    operator types a dollar floor into Settings
        -> POST /settings/profile/<id>
        -> models.update_trading_profile  (allowlisted column)
        -> SQLite trading_profiles.min_adv
        -> build_user_context_from_profile
        -> UserContext.min_adv  (what the screener reads)

plus static checks that every layer references the field (so a silent drop
on any layer fails a specific assertion instead of "the floor didn't
apply").
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Static wiring — each layer must name `min_adv`.
# ---------------------------------------------------------------------------

def test_settings_html_exposes_min_adv_input():
    html = open(os.path.join(REPO, "templates", "settings.html")).read()
    assert re.search(r'<input\b[^>]*\bname="min_adv"', html), (
        "Settings 'Screener Parameters' must expose a Min Avg Daily $ Volume "
        "input named min_adv")


def test_both_save_handlers_parse_min_adv():
    views = open(os.path.join(REPO, "views.py")).read()
    # save_profile AND the legacy save_segment must both read it, or one
    # save path silently drops the value.
    assert len(re.findall(r'form\.get\("min_adv"', views)) >= 2, (
        "both settings POST handlers must read min_adv from the form")
    assert '"min_adv": "Min Avg Daily $ Volume"' in views, (
        "FIELD_LABELS must label min_adv")


def test_schema_and_allowlist_carry_min_adv():
    models = open(os.path.join(REPO, "models.py")).read()
    assert "min_adv REAL NOT NULL DEFAULT 5000000" in models, (
        "trading_profiles schema must define min_adv")
    assert '("trading_profiles", "min_adv"' in models, (
        "an idempotent ALTER-ADD migration must add min_adv to existing DBs")
    assert '"min_adv"' in models  # update_trading_profile allowlist


def test_usercontext_has_min_adv_field():
    from user_context import UserContext
    assert "min_adv" in UserContext.__dataclass_fields__


# ---------------------------------------------------------------------------
# Flask round-trip — POST persists, reads back, and reaches UserContext.
# ---------------------------------------------------------------------------

@pytest.fixture
def logged_in_client_with_profile(tmp_main_db):
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    from models import create_user, create_trading_profile
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={"email": "test@test.com",
                                "password": "password123"},
                follow_redirects=True)
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE email='test@test.com'").fetchone()[0]
    profile_id = create_trading_profile(user_id, "ADV Test", "stocks")
    return client, profile_id


def test_min_adv_form_round_trip(logged_in_client_with_profile, tmp_main_db):
    client, pid = logged_in_client_with_profile
    resp = client.post(
        f"/settings/profile/{pid}",
        data={"profile_name": "ADV Test", "enabled": "1",
              "min_adv": "10000000"},
        follow_redirects=True,
    )
    assert resp.status_code == 200, (
        f"Settings POST returned {resp.status_code}; "
        f"preview: {resp.data[:300]!r}")

    with closing(sqlite3.connect(tmp_main_db)) as conn:
        stored = conn.execute(
            "SELECT min_adv FROM trading_profiles WHERE id=?", (pid,)
        ).fetchone()[0]
    assert float(stored) == 10_000_000.0, (
        f"min_adv must persist to the DB; got {stored!r}")

    # And it must reach the UserContext the screener actually reads.
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(pid)
    assert ctx.min_adv == 10_000_000.0, (
        f"UserContext.min_adv must reflect the saved value; got {ctx.min_adv!r}")
