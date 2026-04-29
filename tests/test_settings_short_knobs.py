"""The short-mandate settings (target_short_pct, target_book_beta,
short_max_position_pct, short_max_hold_days) must round-trip
end-to-end: form input → POST handler → DB → ctx.

Caught by the audit on 2026-04-29: backend supported all four since
P1.5/P1.6/P2.2/P4.1 shipped, but the Settings template never exposed
controls and the POST handler never parsed them. Users couldn't
configure the most important short knobs through the UI.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Use a fresh sqlite DB for each test."""
    import config
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from journal import init_db
    from models import init_user_db
    init_db(db_path)
    init_user_db(db_path)
    return db_path


def _make_user_and_profile(tmp_db):
    """Create a user + trading profile, return profile id."""
    from models import create_user, create_trading_profile
    create_user("u@x.com", "hashed_dummy", is_admin=1)
    return create_trading_profile(
        user_id=1, name="Test Shorts", market_type="small",
    )


def test_short_knobs_persist_through_update(tmp_db):
    """update_trading_profile accepts each of the four short knobs
    and the values come back unchanged from get_trading_profile."""
    from models import update_trading_profile, get_trading_profile
    pid = _make_user_and_profile(tmp_db)
    update_trading_profile(
        pid,
        target_short_pct=0.5,
        target_book_beta=0.0,
        short_max_position_pct=0.04,
        short_max_hold_days=7,
    )
    prof = get_trading_profile(pid)
    assert prof["target_short_pct"] == pytest.approx(0.5, abs=1e-6)
    assert prof["target_book_beta"] == pytest.approx(0.0, abs=1e-6)
    assert prof["short_max_position_pct"] == pytest.approx(0.04, abs=1e-6)
    assert prof["short_max_hold_days"] == 7


def test_short_knobs_reach_user_context(tmp_db):
    """The values written via update_trading_profile must surface on
    the UserContext returned by build_user_context_from_profile —
    that's how the live pipeline reads them."""
    from models import update_trading_profile, build_user_context_from_profile
    pid = _make_user_and_profile(tmp_db)
    update_trading_profile(
        pid,
        target_short_pct=0.3,
        target_book_beta=0.5,
        short_max_position_pct=0.06,
        short_max_hold_days=14,
    )
    ctx = build_user_context_from_profile(pid)
    assert getattr(ctx, "target_short_pct", None) == pytest.approx(0.3)
    assert getattr(ctx, "target_book_beta", None) == pytest.approx(0.5)
    assert getattr(ctx, "short_max_position_pct", None) == pytest.approx(0.06)
    assert getattr(ctx, "short_max_hold_days", None) == 14


def test_settings_template_has_all_four_short_knob_inputs():
    """Static check: the Settings template must have form inputs for
    every short knob the backend supports. Without an input, users
    can't configure them through the UI even if the backend works."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "settings.html"
    )
    with open(template_path) as f:
        html = f.read()
    for field in (
        'name="target_short_pct"',
        'name="target_book_beta"',
        'name="short_max_position_pct"',
        'name="short_max_hold_days"',
    ):
        assert field in html, (
            f"Settings template missing {field!r}. Backend supports the "
            "field but the UI has no control — users cannot configure it."
        )


def test_save_profile_handler_parses_each_short_knob():
    """Static check: the save_profile view must parse each of the
    four form fields and pass them to update_trading_profile.
    Otherwise form values are submitted but silently dropped."""
    views_path = os.path.join(os.path.dirname(__file__), "..", "views.py")
    with open(views_path) as f:
        src = f.read()
    # The save_profile function — find by signature anchor.
    save_start = src.find("def save_profile(profile_id):")
    assert save_start >= 0
    # Read until the next top-level def.
    save_end = src.find("\ndef ", save_start + 1)
    if save_end < 0:
        save_end = len(src)
    save_block = src[save_start:save_end]
    for field in (
        "target_short_pct",
        "target_book_beta",
        "short_max_position_pct",
        "short_max_hold_days",
    ):
        assert field in save_block, (
            f"save_profile view doesn't reference {field!r} — form "
            "values for this knob will not be persisted."
        )
