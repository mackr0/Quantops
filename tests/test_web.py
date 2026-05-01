"""Test web application routes return correct HTTP status codes.

Uses Flask test client — no real server needed.
"""

import pytest


@pytest.fixture
def client(tmp_main_db):
    """Create a Flask test client with a temporary database."""
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


@pytest.fixture
def logged_in_client(client, tmp_main_db):
    """Create a test client with an authenticated session."""
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user
    create_user("test@test.com", "password123", "Test", is_admin=True)

    # Log in
    client.post("/login", data={
        "email": "test@test.com",
        "password": "password123",
    }, follow_redirects=True)
    return client


class TestPublicRoutes:
    """Unauthenticated routes should redirect or return 200."""

    def test_login_page(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_root_redirects(self, client):
        resp = client.get("/")
        assert resp.status_code in (200, 302)

    def test_dashboard_requires_login(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code in (302, 401)

    def test_settings_requires_login(self, client):
        resp = client.get("/settings")
        assert resp.status_code in (302, 401)


class TestAuthenticatedRoutes:
    """Logged-in routes should return 200."""

    def test_dashboard(self, logged_in_client):
        resp = logged_in_client.get("/dashboard")
        assert resp.status_code == 200

    def test_settings(self, logged_in_client):
        resp = logged_in_client.get("/settings")
        assert resp.status_code == 200

    def test_trades(self, logged_in_client):
        resp = logged_in_client.get("/trades")
        assert resp.status_code == 200

    def test_performance(self, logged_in_client):
        resp = logged_in_client.get("/performance")
        assert resp.status_code == 200

    def test_ai_performance(self, logged_in_client):
        resp = logged_in_client.get("/ai-performance")
        # May redirect to /performance (302) or render directly (200)
        assert resp.status_code in (200, 302)

    # Smoke tests — every visible page must render. 500s here catch
    # template syntax errors before they hit prod (see 2026-04-29
    # incident where ai.html had an unclosed {% if %} block that only
    # surfaced on prod because tests checked /performance but not /ai).
    def test_ai_dashboard(self, logged_in_client):
        resp = logged_in_client.get("/ai")
        assert resp.status_code == 200, (
            f"AI dashboard /ai returned {resp.status_code} — "
            f"likely template syntax error or view exception. "
            f"Body preview: {resp.data[:300]!r}"
        )

    def test_ai_brain_redirect(self, logged_in_client):
        resp = logged_in_client.get("/ai/brain", follow_redirects=True)
        assert resp.status_code == 200

    def test_ai_strategy_redirect(self, logged_in_client):
        resp = logged_in_client.get("/ai/strategy", follow_redirects=True)
        assert resp.status_code == 200

    def test_ai_awareness_redirect(self, logged_in_client):
        resp = logged_in_client.get("/ai/awareness", follow_redirects=True)
        assert resp.status_code == 200

    def test_ai_operations_redirect(self, logged_in_client):
        resp = logged_in_client.get("/ai/operations", follow_redirects=True)
        assert resp.status_code == 200

    def test_admin(self, logged_in_client):
        resp = logged_in_client.get("/admin")
        assert resp.status_code == 200, (
            f"Admin page returned {resp.status_code}"
        )

    def test_settings_with_null_short_fields(self, logged_in_client,
                                                tmp_main_db):
        """Regression: prod profiles created before the long/short
        columns existed had NULL in short_*_pct. The settings template
        was doing prof.get('field', default) * 100, but .get returns
        None when the key exists with a NULL value — None * 100 raises.
        Fix: use (.get(field) or default), or for fields where 0 is a
        valid distinct value, use (.get(field) if not none else default).

        2026-05-01 incident timeline:
          - Round 1: short_max_position_pct NULL → line 489 crash
          - Round 2: target_book_beta NULL → line 516 crash (different
            field, same root cause). This test now NULLs every nullable
            numeric profile field so a 3rd-round can't slip by.
        """
        import sqlite3
        conn = sqlite3.connect(tmp_main_db)
        user_row = conn.execute(
            "SELECT id FROM users WHERE email='test@test.com'"
        ).fetchone()
        assert user_row, "test user not created"
        user_id = user_row[0]

        # Discover every nullable numeric / float column in
        # trading_profiles and NULL them all. Avoids hard-coding which
        # fields exist (the schema has churned a lot).
        cols = conn.execute(
            "PRAGMA table_info(trading_profiles)"
        ).fetchall()
        # PRAGMA columns: cid, name, type, notnull, dflt_value, pk
        nullable_numeric = [
            row[1] for row in cols
            if row[2].upper() in ("REAL", "INTEGER", "NUMERIC", "FLOAT")
            and row[3] == 0  # not NOT NULL
            and row[5] == 0  # not primary key
            and row[1] not in ("user_id", "id")  # don't NULL FK / PK
        ]
        if nullable_numeric:
            sets = ", ".join(f"{c}=NULL" for c in nullable_numeric)
            conn.execute(
                f"UPDATE trading_profiles SET {sets} WHERE user_id=?",
                (user_id,),
            )
        conn.commit()
        conn.close()

        resp = logged_in_client.get("/settings")
        assert resp.status_code == 200, (
            f"/settings returned {resp.status_code} when profile has NULL "
            f"in every nullable numeric column — regression of the "
            f"2026-05-01 incident. Body preview: {resp.data[:500]!r}"
        )


class TestAPIRoutes:
    """API endpoints should return JSON."""

    def test_activity_api(self, logged_in_client):
        resp = logged_in_client.get("/api/activity")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")

    def test_scheduler_status(self, logged_in_client):
        resp = logged_in_client.get("/api/scheduler-status")
        assert resp.status_code == 200
