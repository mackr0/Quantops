"""Caught 2026-05-07: /api/kill-switch POST was @login_required only,
not @admin_required. A viewer (read-only account linked to an admin)
could POST {"action":"activate","reason":"..."} and silently freeze
every trade in the admin's book.

These tests pin both layers of the fix:

1. Backend: the endpoint refuses non-admin / viewer accounts with 403.
2. Frontend: the dashboard hides the activate / deactivate UI for
   viewer accounts (verified via static template scan).
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _viewer_user(linked_to=42):
    user = MagicMock()
    user.is_authenticated = True
    user.id = 99
    user.email = "viewer@example.com"
    user.is_admin = False
    user.role = "viewer"
    user.is_viewer = True
    user.linked_to_user_id = linked_to
    user.effective_user_id = linked_to
    user.display_name = "Viewer"
    return user


def _admin_user():
    user = MagicMock()
    user.is_authenticated = True
    user.id = 42
    user.email = "admin@example.com"
    user.is_admin = True
    user.role = "admin"
    user.is_viewer = False
    user.linked_to_user_id = None
    user.effective_user_id = 42
    user.display_name = "Admin"
    return user


def _client_with(user):
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["LOGIN_DISABLED"] = True
    return app.test_client(), app


class TestKillSwitchAdminOnly:

    def test_viewer_cannot_activate(self):
        """A viewer linked to an admin account must NOT be able to
        flip the master kill switch via POST /api/kill-switch.
        Without this guard, the viewer could freeze the admin's
        entire book by calling the API directly (or by clicking a
        UI button — also fixed in the dashboard template)."""
        viewer = _viewer_user(linked_to=42)
        client, app = _client_with(viewer)
        with patch("flask_login.utils._get_user", return_value=viewer):
            with patch("kill_switch.activate") as mock_activate:
                resp = client.post(
                    "/api/kill-switch",
                    data=json.dumps({"action": "activate",
                                       "reason": "viewer trying to freeze book"}),
                    content_type="application/json",
                )
        assert resp.status_code == 403, (
            f"Expected 403 Forbidden for viewer; got {resp.status_code} "
            f"body={resp.data!r}"
        )
        mock_activate.assert_not_called()

    def test_viewer_cannot_deactivate(self):
        """Symmetric: a viewer can't turn the kill switch OFF
        either. If an admin activated it, only the admin can clear."""
        viewer = _viewer_user(linked_to=42)
        client, app = _client_with(viewer)
        with patch("flask_login.utils._get_user", return_value=viewer):
            with patch("kill_switch.deactivate") as mock_deactivate:
                resp = client.post(
                    "/api/kill-switch",
                    data=json.dumps({"action": "deactivate"}),
                    content_type="application/json",
                )
        assert resp.status_code == 403
        mock_deactivate.assert_not_called()

    def test_admin_can_activate(self):
        """The admin path still works."""
        admin = _admin_user()
        client, app = _client_with(admin)
        with patch("flask_login.utils._get_user", return_value=admin):
            with patch("kill_switch.activate") as mock_activate, \
                 patch("kill_switch.is_active",
                        return_value=(True, "test reason")):
                resp = client.post(
                    "/api/kill-switch",
                    data=json.dumps({"action": "activate",
                                       "reason": "test reason"}),
                    content_type="application/json",
                )
        assert resp.status_code == 200, resp.data
        mock_activate.assert_called_once()


class TestKillSwitchTemplateGuard:
    """Static check that the dashboard template hides the activate /
    deactivate UI for viewers. Defense-in-depth on top of the API
    guard above."""

    def test_dashboard_template_gates_buttons_on_is_viewer(self):
        path = os.path.join(
            os.path.dirname(__file__), os.pardir,
            "templates", "dashboard.html",
        )
        with open(path) as f:
            src = f.read()
        # The activate / deactivate buttons must each sit inside an
        # `if not current_user.is_viewer` branch.
        # We do a coarse check: the literal "kill-switch-activate"
        # button id must appear AFTER an `is_viewer` guard.
        assert "current_user.is_viewer" in src, (
            "Dashboard template must reference current_user.is_viewer "
            "to hide the kill-switch UI from viewers."
        )
        # Both button ids should be inside guard blocks
        for btn_id in ("kill-switch-activate", "kill-switch-deactivate"):
            assert btn_id in src, f"Button id {btn_id} missing"
            # Find the button's position; the most-recent `is_viewer`
            # check before it must be `not current_user.is_viewer`
            # (i.e., admin-only path).
            idx = src.find(btn_id)
            preamble = src[:idx]
            last_viewer_guard = preamble.rfind("current_user.is_viewer")
            assert last_viewer_guard >= 0, (
                f"{btn_id} not gated by is_viewer check"
            )
            # The guard should be `if not current_user.is_viewer` —
            # check the surrounding ~80 chars.
            guard_window = src[last_viewer_guard - 30:last_viewer_guard + 30]
            assert "not current_user.is_viewer" in guard_window, (
                f"{btn_id} appears without a 'not current_user.is_viewer' "
                f"guard nearby. Window:\n{guard_window}"
            )
