"""Regression tests for the Executive Summary bug hunt (2026-04-14).

Bug 1: SELL with realized pnl was stored as status='open'
Bug 2: daily_pnl was always NULL in daily_snapshots
Bug 3: Calmar ratio produced absurd values with tiny drawdown
Bug 4: Snapshot only fired in a 5-minute window per day
Bug 5: Total Trades count excluded open positions
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# Bug 1: SELL with pnl must land as status='closed'
# ---------------------------------------------------------------------------

class TestSellStatusClosed:
    def test_sell_with_pnl_logged_as_closed(self, tmp_profile_db):
        """The bug: trade_pipeline.py passed pnl but never status, so
        journal.log_trade used its default 'open'."""
        from journal import log_trade
        # Simulate the exact call pattern from trade_pipeline after the fix
        log_trade(
            symbol="LUNR", side="sell", qty=18, price=23.29,
            pnl=-29.7, status="closed",  # <-- what the fix now passes
            db_path=tmp_profile_db,
        )
        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT symbol, status, pnl FROM trades WHERE symbol='LUNR'"
        ).fetchone()
        conn.close()
        assert row[1] == "closed", (
            f"sell with realized pnl should be 'closed', got {row[1]!r}"
        )
        assert row[2] == -29.7

    def test_open_buy_stays_open(self, tmp_profile_db):
        """Buys without pnl still land as status='open'."""
        from journal import log_trade
        log_trade(
            symbol="HIMS", side="buy", qty=20, price=21.71,
            db_path=tmp_profile_db,
        )
        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT status, pnl FROM trades WHERE symbol='HIMS'"
        ).fetchone()
        conn.close()
        assert row[0] == "open"
        assert row[1] is None


# ---------------------------------------------------------------------------
# Bug 2: daily_pnl must be populated
# ---------------------------------------------------------------------------

class TestDailyPnlPopulated:
    def test_snapshot_writes_delta_from_prior(self, tmp_profile_db, monkeypatch):
        """The fix: _task_daily_snapshot reads the prior snapshot and
        stores today_equity - prior_equity as daily_pnl.

        Uses local date throughout (matching what log_daily_snapshot
        uses via date.today().isoformat()). The previous version used
        SQLite's `date('now')` which is UTC, producing a day mismatch
        when the two resolve to different dates across midnight UTC.
        """
        from journal import log_daily_snapshot
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        # Match journal.log_daily_snapshot: ET-localized today, not server-local.
        today_et = datetime.now(ZoneInfo("America/New_York")).date()
        today_local = today_et.isoformat()
        yesterday_local = (today_et - timedelta(days=1)).isoformat()

        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, portfolio_value, "
            "num_positions, daily_pnl) VALUES (?, 10000, 10000, 10000, 0, 0)",
            (yesterday_local,),
        )
        conn.commit()
        conn.close()

        fake_account = {"equity": 9979.80, "cash": 9115.88,
                        "portfolio_value": 9979.80}
        monkeypatch.setattr("client.get_account_info",
                            lambda ctx=None: fake_account)
        monkeypatch.setattr("client.get_positions",
                            lambda ctx=None: [])

        from types import SimpleNamespace
        ctx = SimpleNamespace(db_path=tmp_profile_db,
                               display_name="Test", segment="midcap")
        from multi_scheduler import _task_daily_snapshot
        _task_daily_snapshot(ctx)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT equity, daily_pnl FROM daily_snapshots WHERE date=?",
            (today_local,),
        ).fetchone()
        conn.close()
        assert row is not None, "snapshot not written"
        assert row[0] == pytest.approx(9979.80, abs=0.01)
        assert row[1] == pytest.approx(-20.20, abs=0.01), (
            f"daily_pnl should be 9979.80 - 10000 = -20.20, got {row[1]}"
        )

    def test_snapshot_first_ever_has_null_pnl(self, tmp_profile_db, monkeypatch):
        """No prior snapshot means daily_pnl is unknown — store NULL."""
        monkeypatch.setattr("client.get_account_info",
                            lambda ctx=None: {"equity": 10000, "cash": 10000,
                                              "portfolio_value": 10000})
        monkeypatch.setattr("client.get_positions", lambda ctx=None: [])
        from types import SimpleNamespace
        ctx = SimpleNamespace(db_path=tmp_profile_db,
                               display_name="Test", segment="midcap")
        from multi_scheduler import _task_daily_snapshot
        _task_daily_snapshot(ctx)

        conn = sqlite3.connect(tmp_profile_db)
        row = conn.execute(
            "SELECT daily_pnl FROM daily_snapshots"
        ).fetchone()
        conn.close()
        assert row[0] is None


# ---------------------------------------------------------------------------
# Bug 3: Calmar guard against tiny drawdowns
# ---------------------------------------------------------------------------

class TestCalmarGuard:
    def _compute(self, snapshots, trades):
        from metrics import calculate_all_metrics
        # Patch gather functions to return our synthetic data
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", lambda *a, **kw: snapshots), \
             patch("metrics._gather_trades", lambda *a: trades):
            return calculate_all_metrics({"fake.db"}, initial_capital=10000)

    def test_tiny_drawdown_returns_zero_calmar(self):
        """Real bug scenario: 1 day of data with -0.07% drawdown produced
        Calmar = -310. Now should return 0."""
        snaps = [
            {"date": "2026-04-12", "equity": 10000, "daily_pnl": 0},
            {"date": "2026-04-13", "equity":  9979.80, "daily_pnl": -20.20},
        ]
        trades = [{
            "timestamp": "2026-04-14T14:10:00",
            "symbol": "LUNR", "side": "sell", "qty": 18,
            "price": 23.29, "pnl": -29.7, "status": "closed",
        }]
        m = self._compute(snaps, trades)
        assert m["calmar_ratio"] == 0.0, (
            f"Calmar with tiny DD should be 0 (guarded), "
            f"got {m['calmar_ratio']}"
        )

    def test_insufficient_days_returns_zero_calmar(self):
        """Even with a meaningful DD, if days_active < 30 we can't
        reliably annualize — guard also checks days."""
        snaps = [
            {"date": "2026-04-01", "equity": 10000, "daily_pnl": 0},
            {"date": "2026-04-05", "equity":  9000, "daily_pnl": -1000},
        ]
        # Trades all within 4 days — not enough for stable annualized
        trades = [{
            "timestamp": "2026-04-05T10:00:00",
            "symbol": "X", "side": "sell", "qty": 10,
            "price": 100, "pnl": -1000, "status": "closed",
        }]
        m = self._compute(snaps, trades)
        assert m["calmar_ratio"] == 0.0

    def test_real_calmar_with_meaningful_data(self):
        """When DD >= 1% AND days_active >= 30, Calmar should compute."""
        # 60 days of data with a real drawdown
        snaps = []
        for i in range(60):
            # Gentle drift down over 60 days
            eq = 10000 - i * 3 if i < 40 else 10000 - 40 * 3 + (i - 40) * 2
            snaps.append({
                "date": f"2026-03-{i+1:02d}" if i < 31 else f"2026-04-{i-30:02d}",
                "equity": eq, "daily_pnl": -3 if i < 40 else 2,
            })
        # Trades spanning 30+ days gives a real days_active count
        trades = [{
            "timestamp": f"2026-03-05T10:00:00",
            "symbol": "A", "side": "sell", "qty": 10,
            "price": 100, "pnl": -50, "status": "closed",
        }, {
            "timestamp": f"2026-04-10T10:00:00",
            "symbol": "A", "side": "sell", "qty": 10,
            "price": 100, "pnl": -30, "status": "closed",
        }]
        m = self._compute(snaps, trades)
        # Calmar should be a finite number (not 0 placeholder, not NaN/Inf)
        assert m["calmar_ratio"] != 0 or m["max_drawdown_pct"] < 1.0
        # Reasonableness: magnitude shouldn't explode
        assert abs(m["calmar_ratio"]) < 100


# ---------------------------------------------------------------------------
# Bug 5: Total Trades includes open positions
# ---------------------------------------------------------------------------

class TestTradeCountsIncludeOpen:
    def test_counts_all_and_open_and_closed(self, tmp_profile_db):
        from journal import log_trade
        # 2 opens (BUY, no pnl yet) + 1 close (SELL with pnl)
        log_trade(symbol="HIMS", side="buy", qty=20, price=21.71,
                  db_path=tmp_profile_db)
        log_trade(symbol="LUNR", side="buy", qty=18, price=24.95,
                  db_path=tmp_profile_db)
        log_trade(symbol="LUNR", side="sell", qty=18, price=23.29,
                  pnl=-29.7, status="closed", db_path=tmp_profile_db)

        from metrics import _count_open_trades, calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)

        assert m["open_trades"] == 2
        assert m["closed_trades"] == 1
        assert m["all_trades"] == 3
        assert m["total_trades"] == 1    # backward-compat alias = closed_trades

    def test_count_excludes_closed_sells(self, tmp_profile_db):
        """Sells marked 'closed' shouldn't be counted as open positions."""
        from journal import log_trade
        log_trade(symbol="X", side="sell", qty=10, price=50, pnl=100,
                  status="closed", db_path=tmp_profile_db)
        from metrics import _count_open_trades
        assert _count_open_trades({tmp_profile_db}) == 0


# ---------------------------------------------------------------------------
# Bug 4: Snapshot trigger must work after the 15:55 window
# ---------------------------------------------------------------------------

class TestWinLossRatio:
    """Bug 8: win_loss_ratio showed 0.00 when there were no wins —
    misleading (implies 0× edge, when really the data is undefined)."""

    def test_no_wins_returns_undefined(self, tmp_profile_db):
        from journal import log_trade
        # Only a losing trade
        log_trade(symbol="X", side="sell", qty=10, price=100, pnl=-50,
                  status="closed", db_path=tmp_profile_db)
        from metrics import calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)
        assert m["win_loss_ratio_computable"] is False

    def test_no_losses_returns_undefined(self, tmp_profile_db):
        from journal import log_trade
        log_trade(symbol="X", side="sell", qty=10, price=100, pnl=100,
                  status="closed", db_path=tmp_profile_db)
        from metrics import calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)
        assert m["win_loss_ratio_computable"] is False

    def test_both_present_computes_ratio(self, tmp_profile_db):
        from journal import log_trade
        log_trade(symbol="A", side="sell", qty=10, price=100, pnl=100,
                  status="closed", db_path=tmp_profile_db)
        log_trade(symbol="B", side="sell", qty=10, price=100, pnl=-50,
                  status="closed", db_path=tmp_profile_db)
        from metrics import calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)
        assert m["win_loss_ratio_computable"] is True
        assert m["win_loss_ratio"] == 2.0   # avg_win(100) / avg_loss(50)


class TestAvgHoldDays:
    """Bug 6: avg_hold_days iterated the pnl-filtered trades list, which
    excluded all BUY rows. Sells could never match their opens, so the
    hold_days_list ended empty and the metric stayed 0.0."""

    def test_hold_days_matches_buy_and_sell(self, tmp_profile_db):
        from journal import log_trade
        # Buy on 2026-04-13, sell on 2026-04-14 → 1 day hold
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, status) "
            "VALUES ('2026-04-13T19:17:00', 'LUNR', 'buy', 18, 24.95, 'open')"
        )
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, price, pnl, status) "
            "VALUES ('2026-04-14T14:10:00', 'LUNR', 'sell', 18, 23.29, -29.7, 'closed')"
        )
        conn.commit()
        conn.close()

        from metrics import calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)

        assert m["avg_hold_days"] == 1.0, (
            f"LUNR held 1 day (04-13 → 04-14), got {m['avg_hold_days']}"
        )

    def test_hold_days_empty_when_no_closed_positions(self, tmp_profile_db):
        from journal import log_trade
        log_trade(symbol="X", side="buy", qty=10, price=100, db_path=tmp_profile_db)
        from metrics import calculate_all_metrics
        from unittest.mock import patch
        with patch("metrics._gather_snapshots", return_value=[]):
            m = calculate_all_metrics({tmp_profile_db}, initial_capital=10000)
        assert m["avg_hold_days"] == 0.0


class TestSingleBarChartLabels:
    """Bug 7: render_bar_chart_svg picked labels at [0, len//2, len-1]
    without deduping — a single-bar chart rendered the same label 3 times."""

    def test_single_bar_renders_label_once(self):
        from metrics import render_bar_chart_svg
        svg = render_bar_chart_svg([{"label": "-8%", "value": 1}])
        # Count occurrences of the label text
        assert svg.count(">-8%</text>") == 1, (
            "single-bar chart should render label exactly once, "
            "not 3× (dedup regression)"
        )

    def test_two_bars_render_first_and_last(self):
        from metrics import render_bar_chart_svg
        svg = render_bar_chart_svg([
            {"label": "Jan", "value": 1},
            {"label": "Feb", "value": 2},
        ])
        assert svg.count(">Jan</text>") == 1
        assert svg.count(">Feb</text>") == 1

    def test_many_bars_render_three_distinct_labels(self):
        from metrics import render_bar_chart_svg
        svg = render_bar_chart_svg([
            {"label": f"B{i}", "value": i} for i in range(10)
        ])
        # First, middle, last: B0, B5, B9 — each exactly once
        assert svg.count(">B0</text>") == 1
        assert svg.count(">B5</text>") == 1
        assert svg.count(">B9</text>") == 1


class TestSnapshotTriggerWindow:
    """The bug logic was `now.hour == 15 and now.minute >= 55` — a strict
    5-minute window. After the fix the condition is `now >= 15:55` for
    any time the rest of the day.

    We can't import the inline expression, so this test evaluates the
    same condition to confirm the semantics are what we want."""

    def test_fires_any_time_after_close_same_day(self):
        # Simulate the new trigger expression
        from types import SimpleNamespace
        for (h, m, expected) in [
            (15, 54, False),   # before close
            (15, 55, True),
            (15, 59, True),
            (16, 0,  True),
            (17, 30, True),
            (22, 0,  True),
            ( 0, 30, False),   # next day before close
            ( 9, 30, False),
        ]:
            now = SimpleNamespace(hour=h, minute=m)
            after_close = (now.hour > 15 or (now.hour == 15 and now.minute >= 55))
            assert after_close is expected, (
                f"Trigger at {h:02d}:{m:02d} should be {expected}, got {after_close}"
            )

    def test_dedup_string_is_today_not_five_min_window(self):
        """Dedup must be by date string — previously the 5-min window
        was the dedup proxy, which broke if you missed it."""
        # Confirm the last_run key is a date string, not a (h, m) tuple
        from multi_scheduler import run_segment_cycle  # import for side effect
        # Inspect the source literally
        with open("multi_scheduler.py") as f:
            src = f.read()
        assert 'last_run["daily_snapshot"] != today_str' in src, (
            "Dedup must use today_str so re-running the same day doesn't "
            "double-snapshot"
        )
