"""Mobile nav collapse (2026-07-02).

The 11-link top nav was wider than a phone viewport, which made the WHOLE
PAGE horizontally scrollable — swiping a table dragged the page off-screen
instead of scrolling the table. Pins the three-part fix:
  1. the nav links collapse behind a menu toggle on narrow screens
     (toggle + collapsible list present in the rendered page);
  2. the page is clamped to the viewport below the breakpoint;
  3. every data table becomes its OWN horizontal scroll container.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO = os.path.join(os.path.dirname(__file__), os.pardir)


@pytest.fixture
def logged_in(tmp_main_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import config
    config.DB_PATH = str(tmp_main_db)
    from models import create_user
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    create_user("t@t.com", "password123", "T", is_admin=True)
    client = app.test_client()
    client.post("/login", data={"email": "t@t.com",
                                "password": "password123"},
                follow_redirects=True)
    return client


def test_nav_renders_toggle_and_collapsible_links(logged_in):
    r = logged_in.get("/dashboard", follow_redirects=True)
    assert r.status_code == 200
    html = r.data.decode()
    assert 'id="qnav-toggle"' in html, "mobile menu toggle missing from nav"
    assert 'id="qnav-links"' in html, "collapsible link list missing"
    assert 'aria-expanded="false"' in html          # closed by default
    assert "classList.toggle('open')" in html        # toggle wiring present


def test_mobile_css_collapses_nav_and_scopes_table_scroll():
    css = open(os.path.join(REPO, "static", "style.css")).read()
    # Toggle hidden on desktop, shown under the breakpoint.
    assert ".qnav-toggle" in css
    desktop_rule = css.split(".qnav-toggle", 1)[1][:200]
    assert "display: none" in desktop_rule
    # The mobile block: collapse + page clamp + per-table scroll containers.
    assert "@media (max-width: 900px)" in css
    mobile = css.split("@media (max-width: 900px)", 1)[1]
    assert ".qnav .qnav-links" in mobile
    assert "overflow-x: hidden" in mobile, "page-level side-scroll not clamped"
    assert "main.container table" in mobile
    assert "overflow-x: auto" in mobile, "tables must scroll independently"


def test_stylesheet_cache_version_bumped():
    base = open(os.path.join(REPO, "templates", "base.html")).read()
    assert "style.css?v=20260702" in base, (
        "style.css cache-bust version must be bumped with the mobile-nav "
        "CSS or phones keep the stale cached sheet")
