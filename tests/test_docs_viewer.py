"""Tests for the in-app docs viewer (`/docs` + `/docs/<filename>`).

The viewer renders Docs/*.md on every request (mtime-cached) so the
HTML reflects the current source — no separate publish step.
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _client(user=None):
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    return app.test_client()


def _viewer():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 1
    u.is_admin = False
    u.role = "viewer"
    u.is_viewer = True
    u.linked_to_user_id = 42
    u.effective_user_id = 42
    u.email = "viewer@example.com"
    u.display_name = "Viewer"
    return u


def _admin():
    u = MagicMock()
    u.is_authenticated = True
    u.id = 42
    u.is_admin = True
    u.role = "admin"
    u.is_viewer = False
    u.linked_to_user_id = None
    u.effective_user_id = 42
    u.email = "admin@example.com"
    u.display_name = "Admin"
    return u


class TestDocsIndex:
    def test_index_lists_known_docs(self):
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/docs")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # The index should list every numbered doc currently in
        # Docs/. Spot-check a few we know exist.
        assert "01" in body  # exec summary
        assert "13" in body  # quality / reliability
        # It should be HTML, not raw markdown
        assert "<html" in body.lower() or "<!doctype" in body.lower()

    def test_index_visible_to_viewers(self):
        """Docs are reference material, not user-private. A viewer
        linked to an admin can read them."""
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_viewer()):
            resp = client.get("/docs")
        assert resp.status_code == 200


class TestDocsView:
    def test_renders_a_real_doc(self):
        """Pick a doc that exists and verify the HTML body renders."""
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/docs/13_QUALITY_RELIABILITY.md")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        # Doc title should appear
        assert "Quality" in body and "Reliability" in body
        # Markdown table from the doc should be rendered as HTML
        assert "<table" in body

    def test_invalid_filename_returns_404(self):
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/docs/does_not_exist.md")
        assert resp.status_code == 404

    def test_path_traversal_blocked(self):
        """A request with `..` or `/` in the filename must NOT
        escape the docs directory and read arbitrary files."""
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            # `../views.py` — would read source if not blocked
            resp = client.get("/docs/..%2Fviews.py")
            # Flask normalizes %2F differently across versions; the
            # important thing is we don't get a 200 with views.py
            # source.
            assert resp.status_code in (404, 400, 301), (
                "Path-traversal-shaped filename must NOT return 200; "
                f"got {resp.status_code}"
            )

    def test_non_md_extension_blocked(self):
        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp = client.get("/docs/views.py")
        assert resp.status_code == 404


class TestDocsAlwaysFresh:
    """The user requirement: 'each time updates are made there
    should be a fresh version' — the rendered HTML must reflect
    the current source. We test by editing a temp doc, hitting
    the route twice, and verifying the second response shows the
    edited content."""

    def test_render_reflects_source_after_edit(self, tmp_path, monkeypatch):
        # Write a temp doc
        d = tmp_path / "Docs"
        d.mkdir()
        f = d / "99_TEMP.md"
        f.write_text("# First version\n\nbody-v1")

        # Point the viewer at our tmp dir
        import views
        monkeypatch.setattr(views, "_DOCS_DIR", str(d))
        # Clear any prior cache that might survive between tests
        views._docs_render_cache.clear()

        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp1 = client.get("/docs/99_TEMP.md")
            assert resp1.status_code == 200
            assert "First version" in resp1.data.decode("utf-8")

            # Edit the file. mtime must change for the cache to bust;
            # bump explicitly because the test runs faster than the
            # filesystem mtime granularity on some platforms.
            import os
            f.write_text("# Second version\n\nbody-v2")
            new_mtime = os.path.getmtime(f) + 1
            os.utime(f, (new_mtime, new_mtime))

            resp2 = client.get("/docs/99_TEMP.md")
            assert resp2.status_code == 200
            body2 = resp2.data.decode("utf-8")
            assert "Second version" in body2, (
                "Doc viewer returned cached content after source "
                "edit — the cache is not honoring mtime invalidation"
            )
            assert "First version" not in body2

    def test_cache_returns_same_html_when_unchanged(self, tmp_path, monkeypatch):
        """Sanity that the mtime cache works as intended in the
        no-change case (otherwise we'd re-parse markdown on every
        single page load)."""
        d = tmp_path / "Docs"
        d.mkdir()
        f = d / "98_TEMP.md"
        f.write_text("# Stable\n\nbody")

        import views
        monkeypatch.setattr(views, "_DOCS_DIR", str(d))
        views._docs_render_cache.clear()

        client = _client()
        with patch("flask_login.utils._get_user", return_value=_admin()):
            resp1 = client.get("/docs/98_TEMP.md")
            resp2 = client.get("/docs/98_TEMP.md")
        assert resp1.data == resp2.data
