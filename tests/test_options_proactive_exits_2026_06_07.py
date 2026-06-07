"""TODO #7 — proactive pre-expiry exits for single-leg long options.

Two rules to fire:
  1. Premium-based stop: close when current mid ≤ entry * (1 - 50%).
  2. Time-based exit: close at ≤7 days to expiry.

Scope contract (pinned both behaviorally and structurally):
  - Single-leg LONG calls/puts only.
  - Multileg legs (bull_put_spread, iron_condor, etc.) are NOT
    scanned — they're managed at spread level.
  - Short-premium strategies (covered_call, cash_secured_put,
    protective_put) are NOT scanned — premium falling is good for
    short premium; the exit logic would be inverted.

Submission contract:
  - sell_to_close with `position_intent='sell_to_close'` (Alpaca
    rejects option orders without intent).
  - LIMIT at current mid when a quote is available; falls through
    to market only when no quote can be fetched (better than
    leaving a triggered-rule position to decay).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixture: a trades table with one open option position
# ---------------------------------------------------------------------------

def _make_db(tmp_path) -> str:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            status TEXT,
            pnl REAL,
            fill_price REAL,
            occ_symbol TEXT,
            option_strategy TEXT,
            expiry TEXT,
            strike REAL
        )
    """)
    conn.commit()
    conn.close()
    return str(db)


def _insert_option(db_path: str, **overrides) -> int:
    """Insert one open option position with sensible defaults."""
    defaults = {
        "timestamp": "2026-06-01T14:00:00",
        "symbol": "NVDA",
        "side": "buy",
        "qty": 1,
        "price": 5.00,
        "fill_price": 5.00,
        "order_id": "entry-opt-1",
        "signal_type": "OPTIONS",
        "status": "open",
        "occ_symbol": "NVDA260620C00500000",
        "option_strategy": "long_call",
        "expiry": "2026-06-20",
        "strike": 500.0,
    }
    defaults.update(overrides)
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.execute(
            "INSERT INTO trades ("
            + ",".join(defaults.keys())
            + ") VALUES (" + ",".join("?" for _ in defaults) + ")",
            list(defaults.values()),
        )
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Layer 1 — pure logic (no DB, no API)
# ---------------------------------------------------------------------------

class TestPremiumStopLogic:

    def test_drop_at_threshold_fires(self):
        from options_proactive_exits import should_close_on_premium_stop
        # Entry $5.00; current $2.50 = 50% drop → at threshold → fire
        assert should_close_on_premium_stop(5.00, 2.50) is True

    def test_drop_just_below_threshold_does_not_fire(self):
        from options_proactive_exits import should_close_on_premium_stop
        # Entry $5.00; current $2.51 = 49.8% drop → below threshold
        assert should_close_on_premium_stop(5.00, 2.51) is False

    def test_drop_well_past_threshold_fires(self):
        from options_proactive_exits import should_close_on_premium_stop
        # Entry $5.00; current $1.00 = 80% drop → fire
        assert should_close_on_premium_stop(5.00, 1.00) is True

    def test_zero_entry_does_not_fire(self):
        from options_proactive_exits import should_close_on_premium_stop
        assert should_close_on_premium_stop(0.0, 1.0) is False

    def test_negative_current_does_not_fire(self):
        from options_proactive_exits import should_close_on_premium_stop
        assert should_close_on_premium_stop(5.0, -0.1) is False


class TestDaysToExpiryParse:

    def test_future_date_positive_days(self):
        from options_proactive_exits import _days_to_expiry
        today = date(2026, 6, 7)
        assert _days_to_expiry("2026-06-20", today) == 13

    def test_today_zero_days(self):
        from options_proactive_exits import _days_to_expiry
        today = date(2026, 6, 7)
        assert _days_to_expiry("2026-06-07", today) == 0

    def test_past_date_negative_days(self):
        from options_proactive_exits import _days_to_expiry
        today = date(2026, 6, 7)
        assert _days_to_expiry("2026-06-01", today) == -6

    def test_unparseable_returns_none(self):
        from options_proactive_exits import _days_to_expiry
        assert _days_to_expiry("not-a-date") is None
        assert _days_to_expiry("") is None
        assert _days_to_expiry(None) is None


# ---------------------------------------------------------------------------
# Layer 2 — candidate selection (DB query + scope filtering)
# ---------------------------------------------------------------------------

class TestCandidateSelection:

    def test_single_leg_long_call_within_threshold_picked_for_time_exit(
            self, tmp_path,
    ):
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(db, expiry="2026-06-08")  # 1 day out, under 7
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert len(cands) == 1
        assert "time_exit" in cands[0]["_exit_reason"]

    def test_long_put_also_picked(self, tmp_path):
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(
            db, option_strategy="long_put",
            occ_symbol="NVDA260608P00400000",
            expiry="2026-06-08",
        )
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert len(cands) == 1
        assert cands[0]["option_strategy"] == "long_put"

    def test_multileg_leg_NOT_picked(self, tmp_path):
        """The scope contract — multileg legs are managed at spread
        level, never by this sweep. Pre-2026-06-07 regression scenario
        was that a bull_put_spread leg sat in the candidate list."""
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(
            db, option_strategy="bull_put_spread",
            expiry="2026-06-08",
        )
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert cands == [], (
            f"bull_put_spread leg must NOT be picked — multileg "
            f"strategies are managed at spread level. got={cands}"
        )

    def test_short_premium_strategies_NOT_picked(self, tmp_path):
        """covered_call, cash_secured_put, protective_put have inverted
        economics — premium falling is GOOD for short-premium
        positions, the exit rule shape is wrong for them."""
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        for strat in ("covered_call", "cash_secured_put", "protective_put"):
            _insert_option(db, option_strategy=strat, expiry="2026-06-08")
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert cands == [], (
            f"short-premium strategies must NOT be picked. got={cands}"
        )

    def test_closed_rows_NOT_picked(self, tmp_path):
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(db, status="closed", expiry="2026-06-08")
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert cands == []

    def test_sell_rows_NOT_picked(self, tmp_path):
        """Already a close-side row — don't try to close a close."""
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(db, side="sell", expiry="2026-06-08")
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert cands == []

    def test_far_expiry_passes_through_for_premium_check(self, tmp_path):
        """Position with DTE far above threshold gets added but with
        no _exit_reason yet — the sweep computes the premium stop."""
        from options_proactive_exits import find_proactive_exit_candidates
        db = _make_db(tmp_path)
        _insert_option(db, expiry="2026-09-20")  # ~3 months out
        cands = find_proactive_exit_candidates(db, today=date(2026, 6, 7))
        assert len(cands) == 1
        assert cands[0].get("_exit_reason") is None, (
            "Far-expiry candidates pass through with no time-exit "
            "reason; sweep evaluates premium stop separately"
        )


# ---------------------------------------------------------------------------
# Layer 3 — end-to-end sweep (with mocked api + journal)
# ---------------------------------------------------------------------------

class TestSweepEndToEnd:

    def _patches(self, monkeypatch, mid_price=2.0, submit_returns="ord-99"):
        """Mock client._fetch_option_premium, options_trader.submit_
        option_order, and journal.log_trade. Returns the submit mock
        so tests can assert call args."""
        import client as _client_mod
        import options_trader as _ot_mod
        import journal as _journal_mod
        monkeypatch.setattr(
            _client_mod, "_fetch_option_premium",
            lambda api, occ, side="buy": mid_price,
        )
        submit_mock = MagicMock(return_value=submit_returns)
        monkeypatch.setattr(_ot_mod, "submit_option_order", submit_mock)
        # Also monkeypatch on the import inside the module so the
        # late-bound `from options_trader import submit_option_order`
        # inside sweep_proactive_option_exits picks up the mock.
        # The function uses local imports, so patching the source
        # modules is enough.
        log_mock = MagicMock(return_value=999)
        monkeypatch.setattr(_journal_mod, "log_trade", log_mock)
        return submit_mock, log_mock

    def test_time_exit_submits_sell_to_close_at_mid(
            self, tmp_path, monkeypatch,
    ):
        from options_proactive_exits import sweep_proactive_option_exits
        db = _make_db(tmp_path)
        _insert_option(db, expiry="2026-06-08")  # 1 DTE
        submit_mock, log_mock = self._patches(monkeypatch, mid_price=2.0)
        result = sweep_proactive_option_exits(
            None, db_path=db, today=date(2026, 6, 7),
        )
        assert result["time_exits_submitted"] == 1
        assert result["premium_stops_submitted"] == 0
        submit_mock.assert_called_once()
        _api, occ = submit_mock.call_args.args[:2]
        kwargs = submit_mock.call_args.kwargs
        assert occ == "NVDA260620C00500000"
        assert kwargs.get("side") == "sell"
        assert kwargs.get("position_intent") == "sell_to_close", (
            "Alpaca rejects option orders without explicit intent; "
            "the close MUST carry sell_to_close"
        )
        assert kwargs.get("order_type") == "limit"
        assert kwargs.get("limit_price") == 2.0
        # Journal row written with status=pending_fill — state machine
        # closes it when the broker confirms.
        log_mock.assert_called_once()
        log_kwargs = log_mock.call_args.kwargs
        assert log_kwargs.get("status") == "pending_fill"
        assert log_kwargs.get("side") == "sell"
        assert log_kwargs.get("order_id") == "ord-99"

    def test_premium_stop_triggers_at_50pct_drop(
            self, tmp_path, monkeypatch,
    ):
        from options_proactive_exits import sweep_proactive_option_exits
        db = _make_db(tmp_path)
        _insert_option(
            db, fill_price=5.00, expiry="2026-09-20",  # far out
        )
        submit_mock, _ = self._patches(monkeypatch, mid_price=2.40)
        # 2.40 < 5.00 * 0.50 = 2.50 → triggers
        result = sweep_proactive_option_exits(
            None, db_path=db, today=date(2026, 6, 7),
        )
        assert result["premium_stops_submitted"] == 1
        assert result["time_exits_submitted"] == 0

    def test_premium_stop_does_not_trigger_just_above_threshold(
            self, tmp_path, monkeypatch,
    ):
        from options_proactive_exits import sweep_proactive_option_exits
        db = _make_db(tmp_path)
        _insert_option(
            db, fill_price=5.00, expiry="2026-09-20",
        )
        submit_mock, _ = self._patches(monkeypatch, mid_price=2.60)
        # 2.60 > 2.50 → does NOT trigger
        result = sweep_proactive_option_exits(
            None, db_path=db, today=date(2026, 6, 7),
        )
        assert result["premium_stops_submitted"] == 0
        submit_mock.assert_not_called()

    def test_market_fallback_when_no_quote(
            self, tmp_path, monkeypatch,
    ):
        """When the quote fetch returns 0, the time-exit path still
        closes — market fallback. Position needs to close regardless."""
        from options_proactive_exits import sweep_proactive_option_exits
        db = _make_db(tmp_path)
        _insert_option(db, expiry="2026-06-08")  # 1 DTE → time exit
        # mid_price=0 simulates illiquid contract with no quote
        submit_mock, _ = self._patches(monkeypatch, mid_price=0.0)
        sweep_proactive_option_exits(
            None, db_path=db, today=date(2026, 6, 7),
        )
        kwargs = submit_mock.call_args.kwargs
        assert kwargs.get("order_type") == "market", (
            "When no quote is available on a time-exit, the sweep must "
            "fall through to market — the position needs to close"
        )

    def test_empty_db_no_calls(self, tmp_path, monkeypatch):
        from options_proactive_exits import sweep_proactive_option_exits
        db = _make_db(tmp_path)
        submit_mock, log_mock = self._patches(monkeypatch)
        result = sweep_proactive_option_exits(
            None, db_path=db, today=date(2026, 6, 7),
        )
        assert result["scanned"] == 0
        submit_mock.assert_not_called()
        log_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Layer 4 — scheduler wiring (structural pin)
# ---------------------------------------------------------------------------

def test_scheduler_runs_proactive_exits_task():
    """Catches the case where the task is defined but never wired
    into run_segment_cycle. Without this, the whole sweep is dead
    code on prod."""
    src = (REPO_ROOT / "multi_scheduler.py").read_text()
    assert "_task_options_proactive_exits(ctx)" in src, (
        "multi_scheduler.py must register _task_options_proactive_exits "
        "in run_segment_cycle alongside _task_options_lifecycle. "
        "Without this the proactive-exit sweep never runs."
    )
    assert "def _task_options_proactive_exits" in src, (
        "multi_scheduler.py is missing the task definition itself"
    )


def test_strategy_whitelist_is_long_only():
    """Structural pin: SINGLE_LEG_LONG_STRATEGIES must not grow to
    include short-premium or multileg strategies (whose economics
    are wrong for this exit rule)."""
    from options_proactive_exits import SINGLE_LEG_LONG_STRATEGIES
    forbidden = {
        "covered_call", "cash_secured_put", "protective_put",
        "bull_put_spread", "bear_call_spread",
        "iron_condor", "iron_butterfly",
        "long_call_spread", "long_put_spread",
        "short_strangle", "long_strangle", "straddle",
    }
    assert set(SINGLE_LEG_LONG_STRATEGIES).isdisjoint(forbidden), (
        f"SINGLE_LEG_LONG_STRATEGIES grew to include strategies "
        f"with inverted or wrong-shaped exit logic. "
        f"Found: {set(SINGLE_LEG_LONG_STRATEGIES) & forbidden}"
    )
    assert set(SINGLE_LEG_LONG_STRATEGIES) == {"long_call", "long_put"}
