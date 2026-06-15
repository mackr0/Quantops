"""Public self-registration is disabled (2026-06-15).

Operator directive: this is a single-operator system. Accounts are
created manually by the operator (models.create_user), never through
the web. Pins:
  1. GET  /register → 404 (no form served)
  2. POST /register → 404 AND no user created (can't bypass the
     removed login-page link by POSTing directly)
  3. The login page no longer links to /register
  4. models.create_user still works — manual creation is the
     intended path and must not be collateral damage
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest


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


def test_get_register_is_404(client):
    assert client.get("/register").status_code == 404


def test_post_register_is_404_and_creates_no_user(client, tmp_main_db):
    resp = client.post("/register", data={
        "email": "intruder@example.com",
        "password": "hunter2hunter2",
        "confirm_password": "hunter2hunter2",
        "display_name": "Intruder",
    })
    assert resp.status_code == 404
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE email = ?",
            ("intruder@example.com",),
        ).fetchone()[0]
    assert n == 0, "POST /register created a user — self-registration leak"


def test_login_page_has_no_register_link(client):
    body = client.get("/login").data.decode("utf-8", errors="ignore")
    assert "/register" not in body, (
        "Login page still links to /register — the create-account "
        "affordance must be gone."
    )


def test_manual_create_user_still_works(tmp_main_db):
    """Disabling the web route must not touch the manual path."""
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user, get_user_by_email
    uid = create_user("owner@example.com", "password123",
                      display_name="Owner", is_admin=True)
    assert uid
    assert get_user_by_email("owner@example.com") is not None
