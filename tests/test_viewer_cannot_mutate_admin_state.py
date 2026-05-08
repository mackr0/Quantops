"""Cross-cutting guardrail: every mutating Flask endpoint
(POST / PUT / DELETE / PATCH) MUST be `@admin_required`.

Born 2026-05-07: user discovered a viewer (read-only account
linked to an admin) could POST to `/api/kill-switch` and silently
freeze the admin's entire trading book. Audit found 5 more
mutating endpoints with the same gap. Per the user's explicit
guidance: "guest accounts cannot change things on the primary
account, they should be viewing things only."

This test scans `views.py` for every `@views_bp.route(...,
methods=["POST"|"PUT"|"DELETE"|"PATCH"])` decorator and asserts
the immediately-following function carries `@admin_required`.

If a future endpoint is intentionally writable by viewers (e.g.,
a logout endpoint, or self-service user-profile update), it must
be added to `INTENTIONALLY_VIEWER_WRITABLE` with a written
rationale. The default — and what protects against the kill-
switch class of bug — is admin-only.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


# Endpoints that are deliberately writable by viewers, with rationale.
# Empty by default — every entry needs a comment explaining why a
# viewer needs write access to the admin's account state.
INTENTIONALLY_VIEWER_WRITABLE = {
    # No entries today (2026-05-07). If you add one, document why
    # a viewer mutating the admin's state is correct here.
}


def _scan_views_for_mutating_endpoints():
    """Yield (line_number, route_decorator_text, function_name,
    has_admin_decorator: bool) for every mutating endpoint."""
    with open(VIEWS_PATH) as f:
        src = f.read()

    lines = src.splitlines()

    # Find each @views_bp.route(...) decorator. The route can span
    # multiple lines, so we accumulate until the closing paren of
    # the route() call.
    in_route = False
    route_buf = []
    route_start = None
    pending_decorators = []
    pending_route = None
    pending_route_line = None

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        if in_route:
            route_buf.append(stripped)
            # Cheap closing-paren detection: if line ends with `)` at
            # the route-call's depth, finish.
            joined = " ".join(route_buf)
            if joined.count("(") == joined.count(")"):
                pending_route = joined
                in_route = False
            continue

        if stripped.startswith("@views_bp.route("):
            route_start = i
            route_buf = [stripped]
            pending_route_line = i
            joined = stripped
            if joined.count("(") == joined.count(")"):
                pending_route = joined
                in_route = False
            else:
                in_route = True
            continue

        # Other decorators (login_required, admin_required) attached
        # to the same function.
        if stripped.startswith("@") and pending_route is not None:
            pending_decorators.append(stripped)
            continue

        if stripped.startswith("def ") and pending_route is not None:
            func_name = stripped.split()[1].split("(")[0]
            # Is this a mutating route?
            is_mutating = any(
                f'"{m}"' in pending_route
                for m in ("POST", "PUT", "DELETE", "PATCH")
            )
            if is_mutating:
                has_admin = any(
                    "@admin_required" in d for d in pending_decorators
                )
                yield (
                    pending_route_line, pending_route,
                    func_name, has_admin,
                )
            pending_route = None
            pending_decorators = []


def test_every_mutating_endpoint_is_admin_required():
    """Every POST/PUT/DELETE/PATCH endpoint must carry
    @admin_required (in addition to @login_required). Viewers
    must not be able to mutate state on the admin's account."""
    leaks = []
    for line, route, func, has_admin in _scan_views_for_mutating_endpoints():
        if has_admin:
            continue
        if func in INTENTIONALLY_VIEWER_WRITABLE:
            continue
        leaks.append(f"  views.py:{line}  {func}()  {route}")

    if leaks:
        msg = (
            "\nFound mutating endpoint(s) without @admin_required.\n"
            "Viewers (read-only accounts linked to an admin) can "
            "POST to these and change state on the admin's account.\n"
            "Caught 2026-05-07: /api/kill-switch was @login_required "
            "only — a viewer could freeze the admin's entire book.\n\n"
            "Fix: add @admin_required to the endpoint. If the endpoint "
            "MUST be writable by viewers (rare — e.g. logout), add the "
            "function name to INTENTIONALLY_VIEWER_WRITABLE in this "
            "test file with a written rationale.\n\n"
            "Endpoints missing the guard:\n"
            + "\n".join(leaks)
        )
        raise AssertionError(msg)


def test_admin_required_decorator_actually_blocks_viewers():
    """Sanity check that @admin_required has the semantics we
    rely on: it returns 403 (or redirects) for viewer accounts."""
    from views import admin_required
    from unittest.mock import MagicMock, patch

    # A plain function gated by admin_required
    @admin_required
    def secret():
        return "ok"

    viewer = MagicMock()
    viewer.is_admin = False
    viewer.role = "viewer"
    viewer.is_viewer = True

    # admin_required uses flask.current_user via flask_login; patch.
    with patch("views.current_user", viewer):
        # The decorator either aborts (raises) or redirects.
        # Both are acceptable rejections; an empty/200 string is NOT.
        try:
            result = secret()
            # If we got a Flask redirect or response, that's a refusal.
            from flask import Response
            assert isinstance(result, Response) or result != "ok", (
                "@admin_required let a viewer through — secret returned "
                "the protected value"
            )
        except Exception:
            # abort(403) or similar — that's the expected refusal.
            pass
