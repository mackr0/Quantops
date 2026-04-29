"""The AI awareness page must show the user the same long/short
construction context the AI sees in its prompt every cycle. Without
this surface, when the AI emits zero shorts on a 50%-short profile,
the user has no way to verify the prompt is actually computing the
expected numbers (book beta delta, balance gate state, Kelly).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_skips_profiles_with_shorts_disabled():
    from views import _build_long_short_awareness
    profiles = [
        {"id": 1, "name": "Long Only", "enable_short_selling": 0},
        {"id": 2, "name": "Long Only B", "enable_short_selling": False},
    ]
    assert _build_long_short_awareness(profiles) == []


def test_skips_profile_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from views import _build_long_short_awareness
    profiles = [{"id": 99, "name": "P99", "enable_short_selling": 1}]
    assert _build_long_short_awareness(profiles) == []


def test_builds_row_per_shorts_enabled_profile(tmp_path, monkeypatch):
    """Shorts-enabled profiles get one row each. When sub-fetches
    fail (no positions / no Kelly data), the row still renders with
    None placeholders so the user sees the profile is enabled."""
    monkeypatch.chdir(tmp_path)
    # Fake DB file presence
    open(tmp_path / "quantopsai_profile_10.db", "w").close()

    profiles = [{
        "id": 10,
        "name": "Small Cap Shorts",
        "enable_short_selling": 1,
        "target_short_pct": 0.5,
        "target_book_beta": 0.0,
    }]
    # Stub everything that would touch real services.
    with patch("models.build_user_context_from_profile") as ctx_mock, \
         patch("client.get_account_info", return_value={"equity": 100_000}), \
         patch("client.get_positions", return_value=[]), \
         patch("kelly_sizing.compute_kelly_recommendation", return_value=None), \
         patch("portfolio_manager.check_drawdown",
                return_value={"drawdown_pct": 7.5}):
        ctx_mock.return_value = type("Ctx", (), {"db_path": "x.db"})()
        from views import _build_long_short_awareness
        out = _build_long_short_awareness(profiles)

    assert len(out) == 1
    r = out[0]
    assert r["profile_id"] == 10
    assert r["profile_name"] == "Small Cap Shorts"
    assert r["target_short_pct"] == 0.5
    assert r["target_book_beta"] == 0.0
    # Drawdown was 7.5% → scale should be ~0.79 (between 0.85 at 5% and 0.65 at 10%)
    assert r["drawdown_pct"] == 7.5
    assert r["drawdown_scale"] is not None
    assert 0.7 <= r["drawdown_scale"] <= 0.85


def test_includes_kelly_when_data_exists(tmp_path, monkeypatch):
    """When Kelly recommendation returns data, it surfaces in the row."""
    monkeypatch.chdir(tmp_path)
    open(tmp_path / "quantopsai_profile_10.db", "w").close()

    fake_kelly = {
        "win_rate": 0.65, "avg_win_pct": 0.04, "avg_loss_pct": 0.025,
        "n": 50, "full_kelly": 0.36, "fractional_kelly": 0.09,
        "fraction_used": 0.25,
    }
    profiles = [{"id": 10, "name": "P", "enable_short_selling": 1,
                 "target_short_pct": 0.5}]

    def fake_kelly_call(db, direction):
        return fake_kelly if direction == "long" else None

    with patch("models.build_user_context_from_profile") as ctx_mock, \
         patch("client.get_account_info", return_value={"equity": 100_000}), \
         patch("client.get_positions", return_value=[]), \
         patch("kelly_sizing.compute_kelly_recommendation",
                side_effect=fake_kelly_call), \
         patch("portfolio_manager.check_drawdown", return_value={}):
        ctx_mock.return_value = type("Ctx", (), {"db_path": "x.db"})()
        from views import _build_long_short_awareness
        out = _build_long_short_awareness(profiles)

    assert out[0]["kelly_long"]["fractional_kelly"] == 0.09
    assert out[0]["kelly_short"] is None


def test_performance_template_has_book_beta_card():
    """Performance dashboard must show book beta as a single number
    (not just buried in the factor breakdown)."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "performance.html"
    )
    with open(template_path) as f:
        html = f.read()
    assert "exposure.book_beta" in html, \
        "Performance template doesn't surface book_beta as a stat-card"
    assert "Book Beta" in html
    assert "profile_target_book_beta" in html, \
        "Performance template doesn't compare current book_beta to target"


def test_performance_template_has_kelly_panel():
    """Performance dashboard must show Kelly recommendation per direction."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "performance.html"
    )
    with open(template_path) as f:
        html = f.read()
    assert "perf_kelly_long" in html
    assert "perf_kelly_short" in html
    assert "Kelly Position Sizing" in html


def test_performance_view_passes_book_beta_target_and_kelly():
    """performance_dashboard view must pass profile_target_book_beta,
    perf_kelly_long, and perf_kelly_short to render_template."""
    import inspect
    import views
    src = inspect.getsource(views.performance_dashboard)
    for kwarg in ("profile_target_book_beta",
                   "perf_kelly_long",
                   "perf_kelly_short"):
        assert f"{kwarg}=" in src, (
            f"performance_dashboard doesn't pass {kwarg!r} to render_template"
        )


def test_template_renders_awareness_block(tmp_path):
    """Static check: ai.html must include the Long/Short Construction
    table so users can see the prompt context."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "ai.html"
    )
    with open(template_path) as f:
        html = f.read()
    # The block must conditionally render based on long_short_awareness
    assert "long_short_awareness" in html
    assert "Long/Short Construction" in html
    # Required UI columns
    assert "Short Mandate" in html
    assert "Book Beta" in html
    assert "Kelly (Long / Short)" in html
    assert "Drawdown" in html
