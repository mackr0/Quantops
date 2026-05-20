"""Tests for the options P&L auto-cutoff optimizer (#171, 2026-05-17).

Direct response to the 2026-05-13 episode where the system lost
$200K+ on options because nothing made the bleeding visible to the
AI; without a cutoff, options trading just kept running.

The optimizer:
  - DISABLES enable_options=0 when 30-day options realized P&L
    < -3% of initial_capital AND ≥10 closed options trades
  - AUTO-RE-ENABLES after 14 days (per the "self-tuner must drift
    toward confident trading" memory — no permanent off-state;
    rescue scripts = architectural failure)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _build_conn_with_trades(trades):
    """In-memory DB with trades table + the rows in `trades`."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT, side TEXT, qty REAL, price REAL,
            pnl REAL, status TEXT, occ_symbol TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "pnl, status, occ_symbol) VALUES (?,?,?,?,?,?,?,?)",
        trades,
    )
    conn.commit()
    return conn


def _ctx(profile_id=1, initial_capital=100_000.0, enable_options=True):
    return SimpleNamespace(
        profile_id=profile_id,
        user_id=1,
        initial_capital=initial_capital,
        enable_options=enable_options,
        segment="stocks",
    )


def _ts_days_ago(n):
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)
            ).strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────────────────────────────
# Disable branch
# ─────────────────────────────────────────────────────────────────────

class TestDisableBranch:
    def test_bleeding_options_disabled(self):
        """10 options trades over 30 days summing to -$4k on $100k cap
        (= -4%, below -3% threshold) → flip OFF."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = []
        for i in range(10):
            rows.append((
                _ts_days_ago(i), "AAPL", "buy", 100, 5.0,
                -400.0,  # each trade lost $400 → total $4k
                "closed", f"AAPL26061{i}C00200000",
            ))
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch(
            "models.update_trading_profile",
        ) as fake_upd, patch(
            "models.log_tuning_change",
        ) as fake_log:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is not None
        assert "below -3% threshold" in result
        fake_upd.assert_called_once_with(1, enable_options=0)
        fake_log.assert_called_once()

    def test_insufficient_trades_no_change(self):
        """Only 5 options trades — below the 10-trade minimum sample."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = [
            (_ts_days_ago(i), "AAPL", "buy", 1, 5.0, -500.0,
             "closed", f"AAPL26061{i}C00200000")
            for i in range(5)
        ]
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()

    def test_within_tolerance_no_change(self):
        """10 trades summing to only -$1k on $100k cap (-1% > -3%)."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = [
            (_ts_days_ago(i), "AAPL", "buy", 1, 5.0, -100.0,
             "closed", f"AAPL26061{i}C00200000")
            for i in range(10)
        ]
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()

    def test_profitable_options_no_change(self):
        """If options ARE profitable, don't touch the toggle."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = [
            (_ts_days_ago(i), "AAPL", "buy", 1, 5.0, +500.0,
             "closed", f"AAPL26061{i}C00200000")
            for i in range(10)
        ]
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()

    def test_stock_trades_dont_pollute_options_pnl(self):
        """Big losing stock trades should NOT trigger the options
        cutoff — only rows with occ_symbol IS NOT NULL count."""
        from self_tuning import _optimize_options_pnl_cutoff
        # 10 huge stock losses (occ_symbol = None)
        rows = [
            (_ts_days_ago(i), "AAPL", "buy", 100, 50.0, -10_000.0,
             "closed", None)
            for i in range(10)
        ]
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()

    def test_old_options_losses_excluded(self):
        """Losses from 60 days ago shouldn't count — only last 30d."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = [
            (_ts_days_ago(60), "AAPL", "buy", 1, 5.0, -5_000.0,
             "closed", "AAPL26061C00200000"),
        ] * 10
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Re-enable branch
# ─────────────────────────────────────────────────────────────────────

class TestReEnableBranch:
    def test_re_enable_after_14_days(self):
        """enable_options has been OFF for ≥14 days → flip back ON."""
        from self_tuning import _optimize_options_pnl_cutoff
        conn = _build_conn_with_trades([])
        ctx = _ctx(enable_options=False)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            # _get_recent_adjustment(profile, "enable_options", days=14)
            # → None means no recent adjustment in last 14 days, i.e.
            # the toggle has been settled OFF for at least 14 days.
            "self_tuning._get_recent_adjustment", return_value=None,
        ), patch(
            "models.update_trading_profile",
        ) as fake_upd, patch(
            "models.log_tuning_change",
        ) as fake_log:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is not None
        assert "Auto-re-enabling options" in result
        fake_upd.assert_called_once_with(1, enable_options=1)

    def test_no_re_enable_within_14_days(self):
        """Recent adjustment found → stay OFF, no flip."""
        from self_tuning import _optimize_options_pnl_cutoff
        conn = _build_conn_with_trades([])
        ctx = _ctx(enable_options=False)
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            # Non-None means "yes there was an adjustment in the
            # window"
            "self_tuning._get_recent_adjustment",
            return_value={"timestamp": "recent", "parameter_name": "enable_options"},
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Cooldowns / segment skipping
# ─────────────────────────────────────────────────────────────────────

class TestGuardrails:
    def test_crypto_profile_skipped(self):
        from self_tuning import _optimize_options_pnl_cutoff
        conn = _build_conn_with_trades([])
        ctx = _ctx(enable_options=True)
        ctx.segment = "crypto"
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()

    def test_recent_adjustment_blocks_disable(self):
        """3-day cooldown prevents flip-flopping."""
        from self_tuning import _optimize_options_pnl_cutoff
        rows = [
            (_ts_days_ago(i), "AAPL", "buy", 100, 5.0, -500.0,
             "closed", f"AAPL26061{i}C00200000")
            for i in range(10)
        ]
        conn = _build_conn_with_trades(rows)
        ctx = _ctx(enable_options=True)

        # _get_recent_adjustment is called twice in the disable path:
        # once with days=14 (re-enable branch, but ctx.enable_options is
        # True so that branch is skipped — call doesn't happen) and once
        # with days=3 (cooldown for disable). Just return non-None on
        # any call to simulate a recent adjustment.
        with patch(
            "self_tuning._safe_change_guarded", return_value=True,
        ), patch(
            "self_tuning._get_recent_adjustment",
            return_value={"timestamp": "recent"},
        ), patch(
            "self_tuning._was_adjustment_effective", return_value=None,
        ), patch("models.update_trading_profile") as fake_upd:
            result = _optimize_options_pnl_cutoff(
                conn, ctx, 1, 1, overall_wr=50.0, resolved=10,
            )
        assert result is None
        fake_upd.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Registry wiring
# ─────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_optimizer_registered_in_direction_map(self):
        from self_tuning import _OPTIMIZER_DIRECTION
        assert "_optimize_options_pnl_cutoff" in _OPTIMIZER_DIRECTION
        # Bidirectional so it can fire either disable OR re-enable
        assert _OPTIMIZER_DIRECTION["_optimize_options_pnl_cutoff"] == "BIDIRECTIONAL"
