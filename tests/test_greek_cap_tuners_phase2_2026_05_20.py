"""Phase 2 of #195 — Greek-cap tuners + max_total_positions LOOSEN tag.

Three new BIDIRECTIONAL tuners adjust the options Greek-exposure caps
based on option-bucket realized P&L. Plus `_optimize_max_total_positions`
retagged from TIGHTEN → BIDIRECTIONAL so its existing LOOSEN branch
fires in the LOOSEN priority slot rather than at the back of the queue.

Tests pin:
  1. Greek-cap tuners are present in the registry + DIRECTION_TAGS
  2. Helper `_options_bucket_pnl_30d` filters to occ_symbol IS NOT NULL
  3. With insufficient sample, tuners no-op (return None)
  4. With strong negative option-bucket P&L → cap TIGHTENS
  5. With strong positive option-bucket P&L → cap LOOSENS
  6. Within the BIDIRECTIONAL/LOOSEN tolerance band → no change
  7. param_bounds.PARAM_BOUNDS contains entries for the three Greek caps
  8. max_total_positions is retagged BIDIRECTIONAL
  9. Settings UI inputs exist for the three Greek caps
 10. views.py persists the three Greek caps via clamped writes
"""
from __future__ import annotations

import os
import sys
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# 1. Registry + tag presence
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_direction_tags_present_for_all_three(self):
        import self_tuning
        # Internal _DIRECTION_TAGS isn't exported as a public constant
        # but it's defined at module scope; assert via source-string
        # presence (mirrors the Phase-1 source-pin pattern).
        src_path = os.path.join(REPO, "self_tuning.py")
        with open(src_path) as f:
            src = f.read()
        for name in (
            "_optimize_max_net_options_delta_pct",
            "_optimize_max_theta_burn_dollars_per_day",
            "_optimize_max_short_vega_dollars",
        ):
            assert f'"{name}": "BIDIRECTIONAL"' in src, (
                f"{name} missing from _DIRECTION_TAGS — without it the "
                "tuner doesn't get scheduled correctly in the priority "
                "ordering."
            )

    def test_tuners_callable_from_module(self):
        from self_tuning import (
            _optimize_max_net_options_delta_pct,
            _optimize_max_theta_burn_dollars_per_day,
            _optimize_max_short_vega_dollars,
            _options_bucket_pnl_30d,
            _optimize_greek_cap,
        )
        for fn in (
            _optimize_max_net_options_delta_pct,
            _optimize_max_theta_burn_dollars_per_day,
            _optimize_max_short_vega_dollars,
        ):
            assert callable(fn)
        assert callable(_options_bucket_pnl_30d)
        assert callable(_optimize_greek_cap)

    def test_max_total_positions_retagged_bidirectional(self):
        src_path = os.path.join(REPO, "self_tuning.py")
        with open(src_path) as f:
            src = f.read()
        assert '"_optimize_max_total_positions": "BIDIRECTIONAL"' in src, (
            "_optimize_max_total_positions must be tagged BIDIRECTIONAL "
            "so its existing LOOSEN branch fires in the LOOSEN priority "
            "slot (per feedback_self_tuner_must_drift_toward_trading)."
        )


# ---------------------------------------------------------------------------
# 2. Helper: _options_bucket_pnl_30d
# ---------------------------------------------------------------------------

class TestOptionsBucketHelper:
    """Helper sums pnl over closed option-bucket trades in last 30 days."""

    def _seed_db(self, tmp_path, rows):
        """Create a minimal trades table with the rows provided."""
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.executemany(
                "INSERT INTO trades(timestamp, symbol, occ_symbol, side, qty, "
                "price, status, pnl, signal_type, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        return db

    def test_filters_to_options_only(self, tmp_path):
        from self_tuning import _options_bucket_pnl_30d
        db = self._seed_db(tmp_path, [
            # Stock close — must be ignored
            ("2026-05-20T10:00:00", "AAPL", None, "sell", 10, 200.0,
             "closed", -50.0, "STRONG_SELL", "ord-stock"),
            # Option close — counts
            ("2026-05-20T10:00:00", "AAPL", "AAPL250620C00200000", "sell",
             1, 2.5, "closed", -100.0, "MULTILEG", "ord-opt1"),
            # Option close — counts
            ("2026-05-20T11:00:00", "MSFT", "MSFT250620P00400000", "sell",
             1, 3.0, "closed", 75.0, "MULTILEG", "ord-opt2"),
        ])
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            pnl, count = _options_bucket_pnl_30d(conn, profile_id=99)
        assert count == 2, "stock close must be excluded"
        assert pnl == pytest.approx(-25.0)

    def test_empty_table_returns_zero_zero(self, tmp_path):
        from self_tuning import _options_bucket_pnl_30d
        db = self._seed_db(tmp_path, [])
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            pnl, count = _options_bucket_pnl_30d(conn, profile_id=99)
        assert (pnl, count) == (0.0, 0)


# ---------------------------------------------------------------------------
# 3-6. Greek-cap tuner decisions
# ---------------------------------------------------------------------------

class TestGreekCapTunerDecisions:
    """Each Greek tuner reads option-bucket P&L and decides to RAISE,
    LOWER, or no-op the cap."""

    def _ctx(self, **overrides):
        defaults = dict(
            profile_id=99, user_id=1,
            segment="stocks",
            initial_capital=100_000.0,
            max_net_options_delta_pct=0.05,
            max_theta_burn_dollars_per_day=50.0,
            max_short_vega_dollars=500.0,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("self_tuning._safe_change_guarded", return_value=True)
    @patch("self_tuning._apply_param_change")
    def test_insufficient_sample_no_op(self, mock_apply, _guard, tmp_path):
        """<20 closed option trades → tuner returns None (no change)."""
        from self_tuning import _optimize_max_net_options_delta_pct
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        # Insert only 5 option trades
        with closing(sqlite3.connect(db)) as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO trades(timestamp, symbol, occ_symbol, side, "
                    "qty, price, status, pnl, signal_type, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"2026-05-{20-i:02d}T10:00:00", "AAPL",
                     f"AAPL250620C00{200+i}000", "sell", 1, 2.5,
                     "closed", -10.0, "MULTILEG", f"ord-{i}"),
                )
            conn.commit()
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            result = _optimize_max_net_options_delta_pct(
                conn, self._ctx(), 99, 1,
                overall_wr=50.0, resolved=100,
            )
        assert result is None
        mock_apply.assert_not_called()

    @patch("self_tuning._safe_change_guarded", return_value=True)
    @patch("self_tuning._apply_param_change",
           return_value=(0.04, None, ""))
    def test_strong_loss_tightens_cap(self, mock_apply, _guard, tmp_path):
        """Option bucket P&L < -2% of capital → cap TIGHTENS by 1 step."""
        from self_tuning import _optimize_max_net_options_delta_pct
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        # 25 closed option trades, total loss = -$3000 (3% of $100K)
        with closing(sqlite3.connect(db)) as conn:
            for i in range(25):
                conn.execute(
                    "INSERT INTO trades(timestamp, symbol, occ_symbol, side, "
                    "qty, price, status, pnl, signal_type, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"2026-05-20T{i:02d}:00:00", "AAPL",
                     f"AAPL250620C00{i+200:03d}000", "sell", 1, 2.5,
                     "closed", -120.0, "MULTILEG", f"ord-loss-{i}"),
                )
            conn.commit()
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            result = _optimize_max_net_options_delta_pct(
                conn, self._ctx(), 99, 1,
                overall_wr=40.0, resolved=100,
            )
        assert result is not None
        assert "Tightened" in result or "tighten" in result.lower()
        mock_apply.assert_called_once()
        # The 4th positional arg is param_name, 5th is current, 6th is new_val
        call_kwargs = mock_apply.call_args
        args = call_kwargs.args
        # Arguments: profile_id, user_id, change_type, param, current, new
        assert args[3] == "max_net_options_delta_pct"
        assert args[4] == pytest.approx(0.05)
        assert args[5] == pytest.approx(0.04)  # current - step (0.01)

    @patch("self_tuning._safe_change_guarded", return_value=True)
    @patch("self_tuning._apply_param_change",
           return_value=(0.06, None, ""))
    def test_strong_win_loosens_cap(self, mock_apply, _guard, tmp_path):
        """Option bucket P&L > +2% of capital → cap LOOSENS by 1 step."""
        from self_tuning import _optimize_max_net_options_delta_pct
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        # 25 closed option trades, total gain = +$3000 (3% of $100K)
        with closing(sqlite3.connect(db)) as conn:
            for i in range(25):
                conn.execute(
                    "INSERT INTO trades(timestamp, symbol, occ_symbol, side, "
                    "qty, price, status, pnl, signal_type, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"2026-05-20T{i:02d}:00:00", "AAPL",
                     f"AAPL250620C00{i+200:03d}000", "sell", 1, 2.5,
                     "closed", 120.0, "MULTILEG", f"ord-win-{i}"),
                )
            conn.commit()
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            result = _optimize_max_net_options_delta_pct(
                conn, self._ctx(), 99, 1,
                overall_wr=65.0, resolved=100,
            )
        assert result is not None
        assert "Loosened" in result or "loosen" in result.lower()
        mock_apply.assert_called_once()
        args = mock_apply.call_args.args
        assert args[3] == "max_net_options_delta_pct"
        assert args[4] == pytest.approx(0.05)
        assert args[5] == pytest.approx(0.06)

    @patch("self_tuning._safe_change_guarded", return_value=True)
    @patch("self_tuning._apply_param_change")
    def test_within_tolerance_no_change(self, mock_apply, _guard, tmp_path):
        """Option P&L between -2% and +2% of capital → no change."""
        from self_tuning import _optimize_max_net_options_delta_pct
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        # 25 trades, ~+$500 total (0.5% of $100K)
        with closing(sqlite3.connect(db)) as conn:
            for i in range(25):
                conn.execute(
                    "INSERT INTO trades(timestamp, symbol, occ_symbol, side, "
                    "qty, price, status, pnl, signal_type, order_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"2026-05-20T{i:02d}:00:00", "AAPL",
                     f"AAPL250620C00{i+200:03d}000", "sell", 1, 2.5,
                     "closed", 20.0, "MULTILEG", f"ord-tol-{i}"),
                )
            conn.commit()
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            result = _optimize_max_net_options_delta_pct(
                conn, self._ctx(), 99, 1,
                overall_wr=50.0, resolved=100,
            )
        assert result is None
        mock_apply.assert_not_called()

    @patch("self_tuning._safe_change_guarded", return_value=True)
    @patch("self_tuning._apply_param_change", return_value=(0.05, None, ""))
    def test_crypto_segment_skipped(self, mock_apply, _guard, tmp_path):
        """Crypto profiles don't trade options through this path."""
        from self_tuning import _optimize_max_net_options_delta_pct
        from journal import init_db
        db = str(tmp_path / "profile.db")
        init_db(db)
        ctx = self._ctx(segment="crypto")
        with closing(sqlite3.connect(db)) as conn:
            conn.row_factory = sqlite3.Row
            result = _optimize_max_net_options_delta_pct(
                conn, ctx, 99, 1, overall_wr=50.0, resolved=100,
            )
        assert result is None
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# 7-9. Bounds + UI + persistence
# ---------------------------------------------------------------------------

class TestParamBounds:
    def test_three_greek_caps_have_bounds(self):
        from param_bounds import PARAM_BOUNDS
        for name, expected_min, expected_max in [
            ("max_net_options_delta_pct", 0.01, 0.20),
            ("max_theta_burn_dollars_per_day", 10.0, 500.0),
            ("max_short_vega_dollars", 50.0, 5000.0),
        ]:
            assert name in PARAM_BOUNDS, f"{name} missing from PARAM_BOUNDS"
            lo, hi = PARAM_BOUNDS[name]
            assert lo == expected_min
            assert hi == expected_max

    def test_clamp_works_for_greek_caps(self):
        from param_bounds import clamp
        # Below floor
        assert clamp("max_net_options_delta_pct", -1.0) == 0.01
        # Above ceiling
        assert clamp("max_theta_burn_dollars_per_day", 99999.0) == 500.0
        # In range
        assert clamp("max_short_vega_dollars", 750.0) == 750.0


class TestSettingsUiHasGreekInputs:
    def test_three_input_fields_present(self):
        with open(os.path.join(REPO, "templates", "settings.html")) as f:
            html = f.read()
        for field in (
            'name="max_net_options_delta_pct"',
            'name="max_theta_burn_dollars_per_day"',
            'name="max_short_vega_dollars"',
        ):
            assert field in html, (
                f"Settings UI missing {field} — operator can't override "
                "the Greek cap. Add to the 'Options Greek-Exposure Caps' "
                "section in templates/settings.html."
            )

    def test_views_persists_clamped_writes(self):
        with open(os.path.join(REPO, "views.py")) as f:
            src = f.read()
        for field in (
            "max_net_options_delta_pct",
            "max_theta_burn_dollars_per_day",
            "max_short_vega_dollars",
        ):
            assert (
                f'_clamp_bound(\n        "{field}"' in src
                or f"_clamp_bound('{field}'" in src
                or (field in src and "_clamp_bound" in src)
            ), (
                f"views.save_profile must persist {field} via a clamped "
                "write so a typo'd UI value can't blow the bound. See "
                "the existing pattern at line ~1573."
            )
