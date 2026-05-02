"""Item 1c — long-vol portfolio hedge tests."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    """Tmp journal DB with daily_snapshots seeded so drawdown helper
    has data."""
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Trigger logic
# ---------------------------------------------------------------------------

class TestEvaluateTriggers:
    def test_drawdown_below_threshold_does_not_fire(self):
        from long_vol_hedge import evaluate_triggers, any_trigger_fired
        triggers = evaluate_triggers(
            drawdown_pct=0.02,            # 2%
            crisis_level="normal",
            var_95_pct_of_equity=0.01,    # 1%
            drawdown_trigger=0.05,        # 5%
            var_trigger=0.03,             # 3%
        )
        assert not any_trigger_fired(triggers)

    def test_drawdown_at_threshold_fires(self):
        from long_vol_hedge import evaluate_triggers
        triggers = evaluate_triggers(
            drawdown_pct=0.05, crisis_level="normal",
            var_95_pct_of_equity=0.01,
        )
        dd = next(t for t in triggers if t.name == "drawdown")
        assert dd.fired

    def test_crisis_elevated_fires(self):
        from long_vol_hedge import evaluate_triggers
        triggers = evaluate_triggers(
            drawdown_pct=0.0, crisis_level="elevated",
            var_95_pct_of_equity=0.0,
        )
        crisis = next(t for t in triggers if t.name == "crisis_state")
        assert crisis.fired

    def test_crisis_severe_fires(self):
        from long_vol_hedge import evaluate_triggers, any_trigger_fired
        triggers = evaluate_triggers(
            drawdown_pct=0.0, crisis_level="severe",
            var_95_pct_of_equity=0.0,
        )
        assert any_trigger_fired(triggers)

    def test_crisis_normal_does_not_fire(self):
        from long_vol_hedge import evaluate_triggers
        triggers = evaluate_triggers(
            drawdown_pct=0.0, crisis_level="normal",
            var_95_pct_of_equity=0.0,
        )
        crisis = next(t for t in triggers if t.name == "crisis_state")
        assert not crisis.fired

    def test_var_above_threshold_fires(self):
        from long_vol_hedge import evaluate_triggers
        triggers = evaluate_triggers(
            drawdown_pct=0.0, crisis_level="normal",
            var_95_pct_of_equity=0.04,    # 4%
            var_trigger=0.03,
        )
        var = next(t for t in triggers if t.name == "var")
        assert var.fired

    def test_var_none_does_not_fire(self):
        from long_vol_hedge import evaluate_triggers
        triggers = evaluate_triggers(
            drawdown_pct=0.0, crisis_level="normal",
            var_95_pct_of_equity=None,
        )
        var = next(t for t in triggers if t.name == "var")
        assert not var.fired
        assert "No portfolio risk snapshot" in var.detail

    def test_all_clear_when_no_signals(self):
        from long_vol_hedge import evaluate_triggers, all_triggers_clear
        triggers = evaluate_triggers(
            drawdown_pct=0.01, crisis_level="normal",
            var_95_pct_of_equity=0.01,
        )
        assert all_triggers_clear(triggers)


# ---------------------------------------------------------------------------
# Strike + expiry + sizing math
# ---------------------------------------------------------------------------

class TestSelectHedgeStrike:
    def test_5pct_otm(self):
        from long_vol_hedge import select_hedge_strike
        # SPY at $500 → 5% OTM put = strike $475
        assert select_hedge_strike(500.0) == 475

    def test_custom_otm(self):
        from long_vol_hedge import select_hedge_strike
        assert select_hedge_strike(500.0, otm_pct=0.10) == 450

    def test_rounds_to_whole_dollar(self):
        from long_vol_hedge import select_hedge_strike
        # SPY $493.27, 5% OTM → 468.6 → 469
        assert select_hedge_strike(493.27) == 469


class TestSelectHedgeExpiry:
    def test_default_45_days(self):
        from long_vol_hedge import select_hedge_expiry
        d = date(2026, 5, 1)
        result = select_hedge_expiry(today=d)
        assert (result - d).days == 45

    def test_custom_dte(self):
        from long_vol_hedge import select_hedge_expiry
        d = date(2026, 5, 1)
        result = select_hedge_expiry(today=d, target_dte=60)
        assert (result - d).days == 60


class TestSizeHedgeContracts:
    def test_basic_sizing(self):
        from long_vol_hedge import size_hedge_contracts
        # $100k book, 1% budget = $1000 / $300 per contract = 3 contracts
        # ($3/share × 100 shares/contract = $300/contract)
        assert size_hedge_contracts(
            equity=100_000, estimated_premium_per_contract=3.0,
            premium_budget_pct=0.01,
        ) == 3

    def test_zero_when_premium_too_expensive(self):
        from long_vol_hedge import size_hedge_contracts
        # $10k book, 1% budget = $100, but $5/share = $500/contract
        # → can't afford even one
        assert size_hedge_contracts(
            equity=10_000, estimated_premium_per_contract=5.0,
            premium_budget_pct=0.01,
        ) == 0

    def test_zero_on_invalid_inputs(self):
        from long_vol_hedge import size_hedge_contracts
        assert size_hedge_contracts(0, 1.0) == 0
        assert size_hedge_contracts(100_000, 0) == 0
        assert size_hedge_contracts(-1, 1.0) == 0


# ---------------------------------------------------------------------------
# Roll / close decisions
# ---------------------------------------------------------------------------

class TestShouldRoll:
    def test_long_dte_high_delta_no_roll(self):
        from long_vol_hedge import should_roll
        today = date(2026, 5, 1)
        # 30 days out, delta -0.30 (still meaningful)
        assert should_roll(today + timedelta(days=30), -0.30, today=today) is None

    def test_short_dte_triggers_roll(self):
        from long_vol_hedge import should_roll
        today = date(2026, 5, 1)
        # 10 days out → < 14 threshold
        result = should_roll(today + timedelta(days=10), -0.30, today=today)
        assert result is not None
        assert "DTE" in result

    def test_decayed_delta_triggers_roll(self):
        from long_vol_hedge import should_roll
        today = date(2026, 5, 1)
        # 30 days out but delta -0.05 (decayed)
        result = should_roll(today + timedelta(days=30), -0.05, today=today)
        assert result is not None
        assert "Delta" in result

    def test_none_delta_does_not_force_roll(self):
        """When delta is unknown (broker can't supply), we don't roll
        based on delta — only DTE."""
        from long_vol_hedge import should_roll
        today = date(2026, 5, 1)
        assert should_roll(today + timedelta(days=30), None, today=today) is None


class TestShouldClose:
    def test_close_when_all_clear(self):
        from long_vol_hedge import evaluate_triggers, should_close
        triggers = evaluate_triggers(
            drawdown_pct=0.01, crisis_level="normal",
            var_95_pct_of_equity=0.01,
        )
        result = should_close(triggers)
        assert result is not None
        assert "All triggers cleared" in result

    def test_no_close_while_anything_active(self):
        from long_vol_hedge import evaluate_triggers, should_close
        triggers = evaluate_triggers(
            drawdown_pct=0.10, crisis_level="normal",
            var_95_pct_of_equity=0.01,
        )
        assert should_close(triggers) is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestHedgePersistence:
    def test_open_close_round_trip(self, tmp_db):
        from long_vol_hedge import (
            evaluate_triggers, record_hedge_opened,
            record_hedge_closed, get_active_hedge,
        )
        triggers = evaluate_triggers(0.10, "normal", 0.04)
        spec = {
            "occ_symbol": "SPY   260619P00475000",
            "underlying": "SPY",
            "strike": 475.0,
            "expiry": "2026-06-19",
            "contracts": 3,
            "entry_premium": 4.20,
            "entry_spot": 500.0,
            "entry_delta": -0.30,
        }
        row_id = record_hedge_opened(tmp_db, spec, triggers, order_id="abc")
        assert row_id > 0

        active = get_active_hedge(tmp_db)
        assert active is not None
        assert active["occ_symbol"] == "SPY   260619P00475000"
        assert active["status"] == "open"

        record_hedge_closed(
            tmp_db, row_id, "test close",
            close_premium=5.0, close_pnl_dollars=240.0,
            close_order_id="def",
        )
        assert get_active_hedge(tmp_db) is None

    def test_get_active_returns_none_on_empty_db(self, tmp_db):
        from long_vol_hedge import get_active_hedge
        assert get_active_hedge(tmp_db) is None

    def test_cost_summary_aggregates(self, tmp_db):
        from long_vol_hedge import (
            evaluate_triggers, record_hedge_opened,
            record_hedge_closed, hedge_cost_summary,
        )
        triggers = evaluate_triggers(0.10, "elevated", None)
        for i in range(3):
            row_id = record_hedge_opened(tmp_db, {
                "occ_symbol": f"SPY___FAKE_{i}",
                "underlying": "SPY", "strike": 475.0,
                "expiry": "2026-06-19", "contracts": 2,
                "entry_premium": 3.0, "entry_spot": 500.0,
                "entry_delta": -0.30,
            }, triggers)
            record_hedge_closed(
                tmp_db, row_id, "test",
                close_premium=2.0, close_pnl_dollars=-200.0,
            )
        summary = hedge_cost_summary(tmp_db, days=90)
        assert summary["n_hedges"] == 3
        # Each hedge: $3 × 100 × 2 = $600 paid; total $1800
        assert summary["total_premium_paid"] == 1800.0
        # Each lost $200; total -$600
        assert summary["total_pnl"] == -600.0
        # Net cost = $1800 - (-$600) = $2400
        assert summary["net_cost"] == 2400.0


# ---------------------------------------------------------------------------
# Drawdown helper
# ---------------------------------------------------------------------------

class TestComputeDrawdownFrom30dPeak:
    def test_returns_zero_with_no_history(self, tmp_db):
        from long_vol_hedge import compute_drawdown_from_30d_peak
        assert compute_drawdown_from_30d_peak(tmp_db, 100_000) == 0.0

    def test_returns_zero_when_at_peak(self, tmp_db):
        from long_vol_hedge import compute_drawdown_from_30d_peak
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, "
            "portfolio_value, num_positions, daily_pnl) "
            "VALUES (date('now', '-5 days'), 100000, 50000, 100000, 0, 0)"
        )
        conn.commit()
        conn.close()
        # Current = 110000 → above peak → drawdown 0
        assert compute_drawdown_from_30d_peak(tmp_db, 110_000) == 0.0

    def test_computes_drawdown(self, tmp_db):
        from long_vol_hedge import compute_drawdown_from_30d_peak
        from journal import _get_conn
        conn = _get_conn(tmp_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, "
            "portfolio_value, num_positions, daily_pnl) "
            "VALUES (date('now', '-5 days'), 100000, 50000, 100000, 0, 0)"
        )
        conn.commit()
        conn.close()
        # Peak 100k, now 90k → 10% drawdown
        result = compute_drawdown_from_30d_peak(tmp_db, 90_000)
        assert abs(result - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# AI prompt rendering
# ---------------------------------------------------------------------------

class TestRenderHedgeForPrompt:
    def test_empty_when_nothing_active(self):
        from long_vol_hedge import render_hedge_for_prompt, evaluate_triggers
        triggers = evaluate_triggers(0.0, "normal", 0.0)
        result = render_hedge_for_prompt(None, triggers, None)
        assert result == ""

    def test_renders_active_hedge(self):
        from long_vol_hedge import render_hedge_for_prompt, evaluate_triggers
        triggers = evaluate_triggers(0.10, "elevated", 0.04)
        active = {
            "occ_symbol": "SPY   260619P00475000",
            "strike": 475.0, "expiry": "2026-06-19",
            "contracts": 3, "entry_premium": 4.20,
            "opened_at": "2026-05-01 12:00:00",
        }
        result = render_hedge_for_prompt(active, triggers, None)
        assert "Active hedge" in result
        assert "SPY" in result
        assert "FIRED" in result      # at least one trigger fired

    def test_renders_triggers_only_when_no_hedge_yet(self):
        from long_vol_hedge import render_hedge_for_prompt, evaluate_triggers
        triggers = evaluate_triggers(0.10, "normal", None)
        result = render_hedge_for_prompt(None, triggers, None)
        assert "No active hedge yet" in result
        assert "drawdown" in result.lower()
