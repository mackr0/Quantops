"""Pin the /admin route's per-user API usage attribution.

Caught 2026-05-09: the route ran a system-wide
`glob("quantopsai_profile_*.db")` then stamped the SAME total onto
every user row. Every user saw every other user's API costs — wrong
as a number and a privacy leak the moment a second account exists.

This test pins:
1. With two users, each user's `api_calls_today` / `api_cost_today`
   reflects ONLY their own profiles' totals (no cross-user leakage).
2. A user with zero profiles shows 0 / $0.00.
3. A profile whose `spend_summary` raises contributes 0 (logged), the
   rest of the user's profiles still aggregate, the route stays 200.

Plus a cross-cutting guardrail (Layer 2): any view in views.py must
NOT use a system-wide `glob("quantopsai_profile_*.db")` — it must
walk a user's profiles via `get_user_profiles(user_id)`.
"""

import ast
import os
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Layer 1 — behavioral
# ---------------------------------------------------------------------------


def _admin():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 1  # match seeded admin user id
    u.email = "alice@x.com"
    u.is_admin = True
    u.role = "admin"
    u.is_viewer = False
    u.linked_to_user_id = None
    u.effective_user_id = 1
    u.display_name = "Alice"
    return u


@pytest.fixture
def admin_client(tmp_main_db, monkeypatch):
    """A test client backed by a real (temp) user DB so the /admin
    SELECT works. Two users seeded: 1 (admin) + 2."""
    # Seed two users in the temp main DB
    conn = sqlite3.connect(tmp_main_db)
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, "
        " is_admin, is_active, created_at) "
        "VALUES (1, 'alice@x.com', 'x', 'Alice', 1, 1, datetime('now'))"
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, "
        " is_admin, is_active, created_at) "
        "VALUES (2, 'bob@x.com', 'x', 'Bob', 0, 1, datetime('now'))"
    )
    conn.commit()
    conn.close()

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _capture_users(monkeypatch):
    """Patch render_template to capture the users list passed in."""
    captured = {}
    def fake_render(name, **kw):
        captured["users"] = kw.get("users", [])
        return "OK"
    monkeypatch.setattr("views.render_template", fake_render)
    return captured


class TestAdminPerUserApiUsage:
    def test_no_cross_user_cost_leakage(self, admin_client, monkeypatch):
        """User 1 has profiles [10, 11]; user 2 has profile [20].
        Each user's row reflects ONLY their own profiles."""
        captured = _capture_users(monkeypatch)

        def fake_user_profiles(user_id):
            return {
                1: [{"id": 10}, {"id": 11}],
                2: [{"id": 20}],
            }.get(user_id, [])

        profile_costs = {
            10: {"calls": 5, "usd": 0.10},
            11: {"calls": 7, "usd": 0.20},
            20: {"calls": 3, "usd": 0.05},
        }

        def fake_spend_summary(db_path):
            pid = int(db_path.replace("quantopsai_profile_", "")
                      .replace(".db", ""))
            c = profile_costs[pid]
            return {"today": {"calls": c["calls"], "usd": c["usd"]}}

        monkeypatch.setattr(
            "models.get_user_profiles", fake_user_profiles,
        )
        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary", fake_spend_summary,
        )

        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = admin_client.get("/admin")
        assert resp.status_code == 200, resp.data[:300]

        users = captured["users"]
        by_id = {u["id"]: u for u in users}
        # Alice: 5+7 = 12 calls, 0.10+0.20 = 0.30 usd
        assert by_id[1]["api_calls_today"] == 12
        assert by_id[1]["api_cost_today"] == pytest.approx(0.30)
        # Bob: 3 calls, $0.05
        assert by_id[2]["api_calls_today"] == 3
        assert by_id[2]["api_cost_today"] == pytest.approx(0.05)
        # CRITICAL: no cross-user leakage. System-wide aggregate would
        # stamp 15 / $0.35 onto BOTH rows.
        assert by_id[1]["api_calls_today"] != 15, (
            "BUG REGRESSION: system-wide aggregate leaked across users"
        )
        assert by_id[2]["api_calls_today"] != 15, (
            "BUG REGRESSION: system-wide aggregate leaked across users"
        )

    def test_user_with_no_profiles_shows_zero(
            self, admin_client, monkeypatch):
        captured = _capture_users(monkeypatch)
        monkeypatch.setattr(
            "models.get_user_profiles", lambda uid: [],
        )
        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary",
            lambda db_path: {"today": {"calls": 0, "usd": 0.0}},
        )
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = admin_client.get("/admin")
        assert resp.status_code == 200
        users = captured["users"]
        for u in users:
            assert u["api_calls_today"] == 0
            assert u["api_cost_today"] == 0.0

    def test_one_profile_failing_doesnt_break_others(
            self, admin_client, monkeypatch):
        captured = _capture_users(monkeypatch)

        def fake_user_profiles(uid):
            # Only user 1 has profiles in this test
            if uid == 1:
                return [{"id": 100}, {"id": 101}, {"id": 102}]
            return []

        def fake_spend_summary(db_path):
            pid = int(db_path.replace("quantopsai_profile_", "")
                      .replace(".db", ""))
            if pid == 101:
                raise RuntimeError("DB locked")
            return {"today": {"calls": 1, "usd": 0.10}}

        monkeypatch.setattr(
            "models.get_user_profiles", fake_user_profiles,
        )
        monkeypatch.setattr(
            "ai_cost_ledger.spend_summary", fake_spend_summary,
        )
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = admin_client.get("/admin")
        assert resp.status_code == 200, resp.data[:300]
        users = captured["users"]
        by_id = {u["id"]: u for u in users}
        # 100 + 102 succeed; 101 raises → contributes 0
        assert by_id[1]["api_calls_today"] == 2
        assert by_id[1]["api_cost_today"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Layer 2 — static guardrail
# ---------------------------------------------------------------------------


VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


def test_no_system_wide_profile_glob_in_views_py():
    """`glob("quantopsai_profile_*.db")` attributes every profile's
    data to whoever-asks — exactly the 2026-05-09 admin bug. No view
    function in views.py should use this pattern; per-user views must
    use `get_user_profiles(user_id)` and per-profile views must use
    a targeted `quantopsai_profile_<id>.db` path.

    If a future view legitimately needs a system-wide walk, allowlist
    its function name below — DON'T silently delete this guardrail.
    """
    with open(VIEWS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    ALLOWED_FN_NAMES: set = set()  # currently none

    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in ALLOWED_FN_NAMES:
            continue
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            func = n.func
            if isinstance(func, ast.Attribute) and func.attr == "glob":
                pass
            elif isinstance(func, ast.Name) and func.id == "glob":
                pass
            else:
                continue
            if not n.args:
                continue
            arg0 = n.args[0]
            if not isinstance(arg0, ast.Constant):
                continue
            if not isinstance(arg0.value, str):
                continue
            if "quantopsai_profile_" not in arg0.value:
                continue
            if "*" not in arg0.value:
                continue
            leaks.append(
                f"  views.py:{n.lineno} in {fn.name}() — "
                f"glob({arg0.value!r}) attributes system-wide profile "
                "data to a single caller; switch to "
                "get_user_profiles(user_id)."
            )
    assert not leaks, (
        "Found system-wide profile DB globs in views.py. The 2026-05-09 "
        "admin bug looked exactly like this and leaked every user's API "
        "cost to every other user.\n\n" + "\n".join(leaks)
    )
