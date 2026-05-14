"""Structural guardrail (2026-05-13): no view-handler may iterate
per-profile data globally without scoping to the caller's user.

The bug class.
A new dashboard panel is added. Developer copy-pastes a server-side
helper that does:
    for db_path in glob("quantopsai_profile_*.db"):
        ... aggregate ...
This works in dev (single user) but on a multi-tenant prod deployment
it leaks every other user's data into the response. Privacy /
security regression that's silent — no exception, no alarm, just
the wrong data.

The right pattern is to filter by `current_user.effective_user_id`
or pass through `get_user_profiles(user_id=...)`. This test scans
view handlers for the dangerous shapes and requires the corrective
shape to appear in the same function.

Scope: views.py only. Other server-side modules can legitimately
glob across all profiles (the scheduler operates on every profile).
The bug class is specifically VIEW handlers running on behalf of one
user but globbing across all of them.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import List, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS_PATH = os.path.join(REPO_ROOT, "views.py")


# Names of dangerous calls — globbing across profile DBs.
GLOB_CALL_NAMES = {"glob", "iglob"}      # glob.glob / glob.iglob
LISTDIR_CALL_NAMES = {"listdir", "scandir"}  # os.listdir / os.scandir

# Path shapes the dangerous calls might use.
PROFILE_PATH_PATTERNS = (
    "quantopsai_profile_",       # the per-profile DB naming
    "quantopsai_*.db",
    "profile_",
)


def _is_route_decorated(func_node: ast.FunctionDef) -> bool:
    """True iff the function has @views_bp.route or @app.route or
    @<bp>.route decorator."""
    for d in func_node.decorator_list:
        # Decorator is typically a Call: route(...)(func)
        if isinstance(d, ast.Call):
            target = d.func
        else:
            target = d
        if isinstance(target, ast.Attribute) and target.attr == "route":
            return True
    return False


def _function_uses_user_scoping(func_node: ast.FunctionDef) -> bool:
    """True iff the function body references `effective_user_id` OR
    `current_user` OR calls `get_user_profiles` (which takes a
    user_id) OR `get_trading_profile` (which checks ownership).
    Any of these proves user-scoping happens somewhere in the
    function."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Attribute):
            if node.attr in ("effective_user_id",):
                return True
        if isinstance(node, ast.Name):
            if node.id == "current_user":
                return True
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id in (
                "get_user_profiles", "get_trading_profile",
            ):
                return True
            if (isinstance(target, ast.Attribute)
                    and target.attr in (
                        "get_user_profiles", "get_trading_profile",
                    )):
                return True
    return False


def _calls_dangerous_glob(func_node: ast.FunctionDef
                            ) -> List[Tuple[int, str]]:
    """Return (lineno, snippet) for every dangerous glob/listdir
    call in the function whose path argument references a per-profile
    pattern. Empty list if the function is safe."""
    out = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # `glob.glob(...)` / `glob.iglob(...)` / `os.listdir(...)`
        if isinstance(target, ast.Attribute):
            attr_name = target.attr
        elif isinstance(target, ast.Name):
            attr_name = target.id
        else:
            continue
        if attr_name not in (GLOB_CALL_NAMES | LISTDIR_CALL_NAMES):
            continue
        # First positional arg is the path/pattern
        if not node.args:
            continue
        path_arg = node.args[0]
        path_text = ""
        if (isinstance(path_arg, ast.Constant)
                and isinstance(path_arg.value, str)):
            path_text = path_arg.value
        # Only flag if the path looks per-profile
        if any(pat in path_text for pat in PROFILE_PATH_PATTERNS):
            out.append((node.lineno, f"{attr_name}({path_text!r})"))
    return out


class TestCrossUserDataIsolation:
    def test_no_view_handler_globs_profiles_without_user_scope(self):
        with open(VIEWS_PATH) as fh:
            src = fh.read()
        tree = ast.parse(src)
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not _is_route_decorated(node):
                continue
            dangerous = _calls_dangerous_glob(node)
            if not dangerous:
                continue
            if _function_uses_user_scoping(node):
                continue
            for lineno, snippet in dangerous:
                violations.append(
                    (node.name, lineno, snippet)
                )
        if violations:
            details = "\n".join(
                f"  views.py:{lineno}  {func_name}()  {snippet}"
                for func_name, lineno, snippet in violations
            )
            pytest.fail(
                "View handlers with @route decorator that glob "
                "per-profile data WITHOUT visible user-scoping:\n\n"
                + details
                + "\n\nBug class: dashboard panel returns every "
                "user's data instead of just the caller's. Silent "
                "privacy leak. Fix: filter by "
                "`current_user.effective_user_id` or use "
                "`get_user_profiles(user_id=current_user.effective_user_id)`."
            )

    def test_scanner_correctly_identifies_known_safe_pattern(self):
        """Sanity: scanner recognizes the user-scoping helpers we
        rely on. If this test breaks (someone renames
        `get_user_profiles`), the main test goes silent. Keep both
        in sync."""
        sample = """
def safe_handler():
    profiles = get_user_profiles(
        user_id=current_user.effective_user_id)
    for p in profiles:
        ...
"""
        tree = ast.parse(sample)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        assert _function_uses_user_scoping(func), (
            "Scanner doesn't recognize get_user_profiles + "
            "current_user as user-scoping. The main test will "
            "silently pass even on real violations."
        )

    def test_scanner_correctly_identifies_known_danger_pattern(self):
        """Sanity: scanner correctly flags the dangerous shape on
        a synthetic example (since current views.py has none)."""
        sample = """
def dangerous_handler():
    paths = glob.glob("quantopsai_profile_*.db")
    for path in paths:
        ...
"""
        tree = ast.parse(sample)
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        hits = _calls_dangerous_glob(func)
        assert hits, (
            "Scanner failed to flag glob.glob('quantopsai_profile_*.db') "
            "— the main test would silently pass on real violations."
        )
