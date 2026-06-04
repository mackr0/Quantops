"""Tests for the non-AI baseline strategies (2026-05-17, batch B).

Covers `simple_strategies.run_buy_hold_spy`, `run_random_stock_of_day`,
and `dispatch`. Also exercises the multi_scheduler short-circuit so a
profile with `strategy_type='buy_hold'` never reaches the AI pipeline.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _ctx(**overrides):
    base = dict(
        user_id=1, segment="stocks", display_name="Test Buy-Hold",
        profile_id=42, db_path=":memory:",
        strategy_type="buy_hold", initial_capital=333_000.0,
        is_virtual=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_api(price_map, positions=None, account=None):
    """Fake Alpaca-style API. price_map symbol→float; any symbol
    not in the map falls back to $100. positions list of
    SimpleNamespace(symbol, qty)."""
    api = MagicMock()

    def latest_trade(sym):
        return SimpleNamespace(price=float(price_map.get(sym, 100.0)))
    api.get_latest_trade.side_effect = latest_trade

    def submit_order(symbol, qty, side, **kwargs):
        return SimpleNamespace(
            id="ord-%s-%s-%d" % (symbol, side, int(qty)),
            symbol=symbol, qty=qty, side=side,
        )
    api.submit_order.side_effect = submit_order

    # 2026-06-04 — _pick_random_symbols now consults get_asset to
    # filter inactive Alpaca symbols. Default this stub to "every
    # symbol is active+tradable" so pre-existing tests still pass;
    # tests that need to simulate inactive symbols can override
    # api.get_asset.side_effect after the stub is built.
    def get_asset(symbol):
        return SimpleNamespace(symbol=symbol, status="active", tradable=True)
    api.get_asset.side_effect = get_asset

    api._positions = positions or []
    api._account = account or {
        "equity": 333_000.0, "cash": 333_000.0,
        "buying_power": 333_000.0, "portfolio_value": 333_000.0,
        "status": "ACTIVE",
    }
    return api


# ─────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_ai_strategy_returns_none(self):
        from simple_strategies import dispatch
        ctx = _ctx(strategy_type="ai")
        assert dispatch(ctx) is None

    def test_buy_hold_dispatches_to_run_buy_hold_spy(self):
        from simple_strategies import dispatch
        ctx = _ctx(strategy_type="buy_hold")
        with patch(
            "simple_strategies.run_buy_hold_spy",
            return_value={"buys": 1, "strategy": "buy_hold"},
        ) as fake:
            out = dispatch(ctx)
        fake.assert_called_once_with(ctx)
        assert out["buys"] == 1

    def test_random_dispatches_to_run_random(self):
        from simple_strategies import dispatch
        ctx = _ctx(strategy_type="random")
        with patch(
            "simple_strategies.run_random_stock_of_day",
            return_value={"buys": 5, "strategy": "random"},
        ) as fake:
            out = dispatch(ctx)
        fake.assert_called_once_with(ctx)
        assert out["buys"] == 5

    def test_unknown_strategy_falls_back_to_ai(self):
        """Unknown strategy_type → None (AI pipeline) but logs an
        error so the operator notices."""
        from simple_strategies import dispatch
        ctx = _ctx(strategy_type="quantum_supremacy")
        with patch("simple_strategies.logger") as fake_log:
            out = dispatch(ctx)
        assert out is None
        fake_log.error.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# Buy & Hold SPY
# ─────────────────────────────────────────────────────────────────────

class TestBuyHoldSpy:
    """Contract (post-2026-05-19 bug fix): run_buy_hold_spy fires
    ONCE per profile lifetime, sizing against per-profile virtual
    equity (NOT shared Alpaca account equity). Every subsequent
    invocation is a no-op."""

    def test_first_fire_buys_spy_with_virtual_equity(self):
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()  # initial_capital=333_000
        api = _stub_api(price_map={"SPY": 500.0}, positions=[])
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=False,
        ), patch(
            "simple_strategies._virtual_equity", return_value=333_000.0,
        ), patch("journal.log_trade") as fake_log_trade:
            summary = run_buy_hold_spy(ctx)
        assert summary["buys"] == 1
        assert summary["errors"] == 0
        # 333_000 * 0.95 / 500 = 632
        api.submit_order.assert_called_once()
        call_kwargs = api.submit_order.call_args.kwargs
        assert call_kwargs["symbol"] == "SPY"
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["qty"] == 632
        fake_log_trade.assert_called_once()
        log_kwargs = fake_log_trade.call_args.kwargs
        assert log_kwargs["strategy"] == "buy_hold_spy"

    def test_prior_entry_makes_subsequent_runs_no_op(self):
        """Once any 'buy_hold_spy' trade is in the journal, every
        future call must be a no-op (holds=1, zero broker contact)."""
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = _stub_api(price_map={"SPY": 500.0})
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=True,
        ), patch("journal.log_trade") as fake_log_trade:
            summary = run_buy_hold_spy(ctx)
        assert summary["holds"] == 1
        assert summary["buys"] == 0
        api.submit_order.assert_not_called()
        api.get_latest_trade.assert_not_called()  # never even fetched price
        fake_log_trade.assert_not_called()

    def test_zero_virtual_equity_errors_no_order(self):
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = _stub_api(price_map={"SPY": 500.0})
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=False,
        ), patch("simple_strategies._virtual_equity", return_value=0.0):
            summary = run_buy_hold_spy(ctx)
        assert summary["errors"] == 1
        api.submit_order.assert_not_called()

    def test_price_fetch_failure_errors_no_order(self):
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = MagicMock()
        api.get_latest_trade.side_effect = OSError("network down")
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=False,
        ), patch("simple_strategies._virtual_equity", return_value=333_000.0):
            summary = run_buy_hold_spy(ctx)
        assert summary["errors"] == 1
        api.submit_order.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Random Stock-of-Day
# ─────────────────────────────────────────────────────────────────────

class TestRandomStockOfDay:
    """Contract (post-2026-05-19 bug fix): run_random_stock_of_day
    fires ONCE per profile lifetime — picks 5 symbols deterministically
    by profile_id alone (NOT date), buys equal-weighted from per-
    profile virtual equity, then holds FOREVER. No daily rotation.
    Every subsequent invocation is a no-op."""

    def test_picks_are_stable_across_days_for_same_profile(self):
        """Same profile_id → same picks regardless of when called.
        The fix dropped the date component from the seed."""
        from simple_strategies import _pick_random_symbols
        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                    "TSLA", "AMD", "INTC", "QCOM"]
        p1 = _pick_random_symbols(42, universe, 5)
        p2 = _pick_random_symbols(42, universe, 5)
        assert p1 == p2

    def test_picks_differ_across_profiles(self):
        from simple_strategies import _pick_random_symbols
        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                    "TSLA", "AMD", "INTC", "QCOM"]
        p1 = _pick_random_symbols(1, universe, 5)
        p2 = _pick_random_symbols(2, universe, 5)
        assert p1 != p2

    def test_first_fire_buys_initial_picks(self):
        from simple_strategies import run_random_stock_of_day
        ctx = _ctx(strategy_type="random")
        from segments import STOCK_UNIVERSE
        price_map = {s: 100.0 for s in STOCK_UNIVERSE}
        api = _stub_api(price_map=price_map, positions=[])
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=False,
        ), patch(
            "simple_strategies._virtual_equity", return_value=333_000.0,
        ), patch("journal.log_trade") as fake_log_trade:
            summary = run_random_stock_of_day(ctx)
        assert summary["buys"] == 5
        assert summary["sells"] == 0
        assert fake_log_trade.call_count == 5
        # Each log row carries the strategy tag the fire-once guard reads
        for call in fake_log_trade.call_args_list:
            assert call.kwargs["strategy"] == "random_stock_of_day"
            assert call.kwargs["side"] == "buy"

    def test_prior_entry_makes_subsequent_runs_no_op(self):
        """Once any 'random_stock_of_day' trade is in the journal,
        every future call must be a no-op (holds=1, zero broker
        contact). This is the core defense against the 2026-05-19
        daily-rotation bug."""
        from simple_strategies import run_random_stock_of_day
        ctx = _ctx(strategy_type="random")
        api = _stub_api(price_map={"AAPL": 100.0})
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=True,
        ), patch("journal.log_trade") as fake_log_trade:
            summary = run_random_stock_of_day(ctx)
        assert summary["holds"] == 1
        assert summary["buys"] == 0
        assert summary["sells"] == 0
        api.submit_order.assert_not_called()
        api.get_latest_trade.assert_not_called()
        fake_log_trade.assert_not_called()

    def test_never_sells(self):
        """Buy-and-hold benchmark should NEVER emit a sell order,
        regardless of what positions exist. The fix removed the
        'sell positions not in today's pick' branch entirely."""
        from simple_strategies import run_random_stock_of_day
        from segments import STOCK_UNIVERSE
        ctx = _ctx(strategy_type="random")
        # First-fire path with positions present — must NOT sell them
        positions = [SimpleNamespace(symbol="AAPL", qty=50)]
        price_map = {s: 100.0 for s in STOCK_UNIVERSE}
        api = _stub_api(price_map=price_map, positions=positions)
        with patch("client.get_api", return_value=api), patch(
            "simple_strategies._has_prior_strategy_entry", return_value=False,
        ), patch(
            "simple_strategies._virtual_equity", return_value=333_000.0,
        ), patch("journal.log_trade") as fake_log_trade:
            summary = run_random_stock_of_day(ctx)
        assert summary["sells"] == 0
        sell_calls = [
            c for c in fake_log_trade.call_args_list
            if c.kwargs.get("side") == "sell"
        ]
        assert sell_calls == []


# ─────────────────────────────────────────────────────────────────────
# Multi-scheduler dispatch wiring
# ─────────────────────────────────────────────────────────────────────

class TestMultiSchedulerDispatch:
    def test_buy_hold_profile_skips_ai_pipeline(self):
        """A profile with strategy_type='buy_hold' must never call
        run_trade_cycle (the AI pipeline). multi_scheduler imports
        run_trade_cycle and dispatch inside the function, so patch
        at the source modules."""
        import multi_scheduler
        ctx = _ctx(strategy_type="buy_hold")
        with patch(
            "simple_strategies.dispatch",
            return_value={"buys": 1, "sells": 0, "holds": 0,
                          "errors": 0, "strategy": "buy_hold"},
        ), patch("trade_pipeline.run_trade_cycle") as fake_cycle, patch(
            "multi_scheduler.get_segment",
            return_value={"is_crypto": False},
        ), patch("multi_scheduler._get_shared_candidates") as fake_screen, patch(
            "multi_scheduler._safe_log_activity",
        ):
            multi_scheduler._task_scan_and_trade(ctx)
        fake_cycle.assert_not_called()
        fake_screen.assert_not_called()

    def test_ai_profile_runs_ai_pipeline(self):
        """A profile with strategy_type='ai' falls through dispatch
        and goes to the AI pipeline as before."""
        import multi_scheduler
        ctx = _ctx(strategy_type="ai")
        with patch(
            "simple_strategies.dispatch", return_value=None,
        ), patch("trade_pipeline.run_trade_cycle",
                  return_value={"buys": 0, "sells": 0, "shorts": 0,
                                "ai_vetoed": 0, "holds": 0,
                                "errors": 0, "pre_filtered": 0,
                                "sent_to_ai": 0}) as fake_cycle, patch(
            "multi_scheduler.get_segment",
            return_value={"is_crypto": False},
        ), patch(
            "multi_scheduler._get_shared_candidates",
            return_value=["AAPL", "MSFT"],
        ), patch(
            "multi_scheduler._build_scan_summary",
            return_value=("title", "detail"),
        ), patch(
            "multi_scheduler._safe_log_activity",
        ), patch("scan_status.update_status"), patch(
            "scan_status.clear_status",
        ), patch("notifications.notify_trade"), patch(
            "notifications.notify_veto",
        ):
            multi_scheduler._task_scan_and_trade(ctx)
        fake_cycle.assert_called_once()
