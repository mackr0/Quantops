"""Regression: when apply_auto_adjustments returns an empty list (the
common 'no changes needed' case), _task_self_tune must not raise. The
'real_changes' variable used by the no-changes-needed log path must be
defined unconditionally — not only inside the `if adjustments:` branch.

This test catches the production regression seen on 2026-04-25 where
the notification rewrite (applied vs recommended counts) accidentally
moved `real_changes = applied` into the `if adjustments:` block,
causing a NameError on every clean tuner run."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _ctx(tmp_path, **overrides):
    db = str(tmp_path / "task.db")
    defaults = dict(
        profile_id=1, user_id=1, db_path=db, enable_self_tuning=True,
        display_name="Test", segment="small",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestSelfTuneTaskNoChangePath:
    def test_no_change_path_does_not_raise(self, tmp_path):
        """The exact scenario that broke prod: tuner returns []."""
        from multi_scheduler import _task_self_tune
        ctx = _ctx(tmp_path)
        with patch("self_tuning.apply_auto_adjustments", return_value=[]):
            with patch("self_tuning.describe_tuning_state", return_value={
                "can_tune": True,
                "resolved": 50,
                "required": 20,
                "message": "Active — 50 resolved predictions",
            }):
                with patch("multi_scheduler._safe_log_activity"):
                    with patch("self_tuning._get_conn"):
                        with patch("self_tuning._get_current_win_rate",
                                    return_value=(50.0, 50)):
                            with patch("models.log_tuning_change",
                                        return_value=42):
                                with patch("models._get_conn"):
                                    # Must NOT raise NameError
                                    _task_self_tune(ctx)

    def test_change_path_still_works(self, tmp_path):
        """Sanity: applied changes still flow through correctly."""
        from multi_scheduler import _task_self_tune
        ctx = _ctx(tmp_path)
        with patch("self_tuning.apply_auto_adjustments",
                    return_value=["Reduced position size from 10% to 8%"]):
            with patch("self_tuning.describe_tuning_state", return_value={
                "can_tune": True, "resolved": 50, "required": 20,
                "message": "Active",
            }):
                with patch("multi_scheduler._safe_log_activity") as mock_log:
                    _task_self_tune(ctx)
                    mock_log.assert_called()
                    args = mock_log.call_args[0]
                    # Title should include "1 applied"
                    assert "1 applied" in args[3]

    def test_recommendation_only_path(self, tmp_path):
        """Recommendations show up in title with 'recommended' suffix."""
        from multi_scheduler import _task_self_tune
        ctx = _ctx(tmp_path)
        with patch("self_tuning.apply_auto_adjustments",
                    return_value=["Recommendation: enable short selling"]):
            with patch("self_tuning.describe_tuning_state", return_value={
                "can_tune": True, "resolved": 50, "required": 20,
                "message": "Active",
            }):
                with patch("multi_scheduler._safe_log_activity") as mock_log:
                    _task_self_tune(ctx)
                    mock_log.assert_called()
                    args = mock_log.call_args[0]
                    assert "recommended" in args[3]
                    assert "applied" not in args[3]
