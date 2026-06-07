"""TODO `wheel_symbols` per-profile setup — Flask test-client smoke
test for the end-to-end round-trip.

The piece-parts have been shipped for months: schema column, model
allowlist, parser, UserContext field, AI-prompt wheel-block renderer,
even the Settings UI input. What was missing — and what the standing
memory rule `feedback_ui_buttons_must_have_smoke_tests.md` says is
mandatory before declaring a feature 'done' — is a parametrized
happy-path test through the Flask test client that proves the
operator-visible round-trip works:

    Operator types tickers into the Settings textarea
        ↓
    POST /settings/profile/<id>  (form data)
        ↓
    models.update_trading_profile  (allowlisted column)
        ↓
    SQLite: trading_profiles.wheel_symbols = '["AAPL","MSFT","NVDA"]'
        ↓
    get_trading_profile  (next request reads back)
        ↓
    UserContext.wheel_symbols = ["AAPL","MSFT","NVDA"]
        ↓
    ai_analyst → render_wheel_block_for_prompt sees the symbols

Each test asserts one layer of that chain. If any layer ever silently
drops the value, the corresponding test fails — pinpointing the
exact break instead of "wheel didn't fire."
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def client(tmp_main_db):
    """Flask test client backed by a temp main DB."""
    import config
    config.DB_PATH = tmp_main_db
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


@pytest.fixture
def logged_in_client_with_profile(client, tmp_main_db):
    """Authenticated client + one trading profile we can edit."""
    import config
    config.DB_PATH = tmp_main_db
    from models import create_user, create_trading_profile
    create_user("test@test.com", "password123", "Test", is_admin=True)
    client.post("/login", data={
        "email": "test@test.com",
        "password": "password123",
    }, follow_redirects=True)
    # Resolve the user id and create a profile owned by them
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE email='test@test.com'"
        ).fetchone()[0]
    profile_id = create_trading_profile(user_id, "Wheel Test", "stocks")
    return client, profile_id


# ---------------------------------------------------------------------------
# Layer 1 — Form POST persists wheel_symbols as JSON list in the DB
# ---------------------------------------------------------------------------

def test_settings_form_post_persists_wheel_symbols_as_json(
        logged_in_client_with_profile, tmp_main_db,
):
    """Operator pastes 'AAPL, MSFT, NVDA' into the wheel textarea
    and saves. The DB row must hold a JSON list of uppercased
    tickers."""
    client, pid = logged_in_client_with_profile
    # Minimal form payload — only the wheel_symbols field is what
    # we're testing; the rest are required defaults the save handler
    # reads from form (it raises on missing required fields).
    resp = client.post(
        f"/settings/profile/{pid}",
        data={
            "profile_name": "Wheel Test",
            "enabled": "1",
            "wheel_symbols": "aapl, MSFT,  nvda  ",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200, (
        f"Settings POST returned {resp.status_code}; preview: "
        f"{resp.data[:300]!r}"
    )
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        raw = conn.execute(
            "SELECT wheel_symbols FROM trading_profiles WHERE id=?",
            (pid,),
        ).fetchone()[0]
    parsed = json.loads(raw)
    assert parsed == ["AAPL", "MSFT", "NVDA"], (
        f"wheel_symbols must persist as a JSON list of UPPERCASE "
        f"tickers, stripped of whitespace. got: {parsed!r}"
    )


def test_empty_form_input_persists_empty_list(
        logged_in_client_with_profile, tmp_main_db,
):
    """An operator clearing the textarea explicitly disables the
    wheel for that profile. The DB row must reflect that as `[]`,
    not a stale prior list."""
    client, pid = logged_in_client_with_profile
    # Seed with a non-empty list
    client.post(f"/settings/profile/{pid}", data={
        "profile_name": "Wheel Test",
        "wheel_symbols": "AAPL, MSFT",
    })
    # Now clear it
    client.post(f"/settings/profile/{pid}", data={
        "profile_name": "Wheel Test",
        "wheel_symbols": "",
    })
    with closing(sqlite3.connect(tmp_main_db)) as conn:
        raw = conn.execute(
            "SELECT wheel_symbols FROM trading_profiles WHERE id=?",
            (pid,),
        ).fetchone()[0]
    assert json.loads(raw) == [], (
        "Clearing the textarea must persist an empty list; otherwise "
        "the operator's 'turn off the wheel' action is silently ignored"
    )


# ---------------------------------------------------------------------------
# Layer 2 — UserContext.wheel_symbols reflects what the DB holds
# ---------------------------------------------------------------------------

def test_user_context_wheel_symbols_reflects_db(
        logged_in_client_with_profile, tmp_main_db,
):
    """The bridge between the model layer and the AI pipeline. The
    UserContext built from a profile MUST carry wheel_symbols as a
    Python list, not the raw JSON string."""
    client, pid = logged_in_client_with_profile
    client.post(f"/settings/profile/{pid}", data={
        "profile_name": "Wheel Test",
        "wheel_symbols": "KO, JNJ",
    })
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(pid)
    assert ctx.wheel_symbols == ["KO", "JNJ"], (
        f"UserContext.wheel_symbols must be the parsed list, not the "
        f"raw JSON string. got: {ctx.wheel_symbols!r}"
    )


def test_user_context_wheel_symbols_defaults_to_empty_list(
        logged_in_client_with_profile,
):
    """A freshly-created profile has wheel_symbols='[]' (the schema
    default). UserContext must parse that to [], not raise on the
    empty-list JSON."""
    _, pid = logged_in_client_with_profile
    from models import build_user_context_from_profile
    ctx = build_user_context_from_profile(pid)
    assert ctx.wheel_symbols == [], (
        f"A new profile must default to an empty wheel list. "
        f"got: {ctx.wheel_symbols!r}"
    )


# ---------------------------------------------------------------------------
# Layer 3 — settings.html actually renders the wheel_symbols textarea
# ---------------------------------------------------------------------------

def test_settings_page_renders_wheel_symbols_textarea(
        logged_in_client_with_profile,
):
    """The textarea has to exist in the rendered HTML or the operator
    can't enter symbols. Catches the case where someone refactors the
    form and accidentally drops the wheel input."""
    client, pid = logged_in_client_with_profile
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="ignore")
    assert 'name="wheel_symbols"' in body, (
        "settings.html must render an <input>/<textarea> with "
        'name="wheel_symbols". Without this the operator has no way '
        "to opt into the wheel; the whole feature is invisible."
    )


def test_settings_page_renders_existing_wheel_symbols(
        logged_in_client_with_profile,
):
    """After setting wheel_symbols, the Settings page must render
    the saved values back into the textarea so the operator can see
    + edit them. Catches the round-trip-display bug shape: form
    posts work but the page renders blank, making it look like
    nothing was saved."""
    client, pid = logged_in_client_with_profile
    client.post(f"/settings/profile/{pid}", data={
        "profile_name": "Wheel Test",
        "wheel_symbols": "NVDA, AMD",
    })
    resp = client.get("/settings")
    body = resp.data.decode("utf-8", errors="ignore")
    # The template joins with ", " — both ticker symbols must appear
    # somewhere in the body (the textarea content is plain text).
    assert "NVDA" in body and "AMD" in body, (
        "After saving wheel_symbols, the Settings page must render "
        "them back so the operator sees their saved configuration. "
        "If this fails, the form posts succeed but the UI displays "
        "blank — operator thinks nothing was saved and re-enters."
    )
