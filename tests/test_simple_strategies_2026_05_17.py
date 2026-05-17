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
        user_id=1, segment="largecap", display_name="Test Buy-Hold",
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
    def test_day_one_buys_spy_with_equity(self):
        """No existing SPY position → buy ~95% of equity in SPY."""
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = _stub_api(price_map={"SPY": 500.0}, positions=[])
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info", return_value=api._account,
        ), patch("client.get_positions", return_value=[]), patch(
            "journal.log_trade",
        ) as fake_log_trade:
            summary = run_buy_hold_spy(ctx)
        assert summary["buys"] == 1
        assert summary["errors"] == 0
        # equity=333k, price=500, buffer 5% → buy 632 shares
        # (333_000 * 0.95 / 500 = 632.7 → int 632)
        api.submit_order.assert_called_once()
        call_kwargs = api.submit_order.call_args.kwargs
        assert call_kwargs["symbol"] == "SPY"
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["qty"] == 632
        fake_log_trade.assert_called_once()
        log_kwargs = fake_log_trade.call_args.kwargs
        assert log_kwargs["order_id"].startswith("ord-SPY-buy-")
        assert log_kwargs["strategy"] == "buy_hold_spy"

    def test_already_holds_spy_within_drift_band_holds(self):
        """SPY weight already ≈ 100% → no order."""
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        # 632 shares × $500 = $316,000 ≈ 95% of $333,000 equity
        positions = [SimpleNamespace(symbol="SPY", qty=632)]
        api = _stub_api(price_map={"SPY": 500.0}, positions=positions)
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info",
            return_value={"equity": 333_000.0, "cash": 17_000.0,
                          "buying_power": 17_000.0,
                          "portfolio_value": 333_000.0, "status": "ACTIVE"},
        ), patch("client.get_positions", return_value=positions), patch(
            "journal.log_trade",
        ) as fake_log_trade:
            summary = run_buy_hold_spy(ctx)
        assert summary["holds"] == 1
        assert summary["buys"] == 0
        api.submit_order.assert_not_called()
        fake_log_trade.assert_not_called()

    def test_zero_equity_errors_no_order(self):
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = _stub_api(price_map={"SPY": 500.0})
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info",
            return_value={"equity": 0.0, "cash": 0.0,
                          "buying_power": 0.0,
                          "portfolio_value": 0.0, "status": "ACTIVE"},
        ), patch("client.get_positions", return_value=[]):
            summary = run_buy_hold_spy(ctx)
        assert summary["errors"] == 1
        api.submit_order.assert_not_called()

    def test_price_fetch_failure_errors_no_order(self):
        from simple_strategies import run_buy_hold_spy
        ctx = _ctx()
        api = MagicMock()
        api.get_latest_trade.side_effect = OSError("network down")
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info",
            return_value={"equity": 333_000.0, "cash": 333_000.0,
                          "buying_power": 333_000.0,
                          "portfolio_value": 333_000.0, "status": "ACTIVE"},
        ), patch("client.get_positions", return_value=[]):
            summary = run_buy_hold_spy(ctx)
        assert summary["errors"] == 1
        api.submit_order.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Random Stock-of-Day
# ─────────────────────────────────────────────────────────────────────

class TestRandomStockOfDay:
    def test_picks_are_deterministic_per_profile_date(self):
        """Same (profile_id, date) → same picks every call."""
        from simple_strategies import _pick_random_symbols
        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                    "TSLA", "AMD", "INTC", "QCOM"]
        picks1 = _pick_random_symbols(42, "2026-05-17", universe, 5)
        picks2 = _pick_random_symbols(42, "2026-05-17", universe, 5)
        assert picks1 == picks2

    def test_picks_differ_across_profiles_same_date(self):
        """Different profile_id → different random pick (almost
        always; sample of 5 from 10 → not strictly guaranteed but
        with these seeds they differ)."""
        from simple_strategies import _pick_random_symbols
        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                    "TSLA", "AMD", "INTC", "QCOM"]
        p1 = _pick_random_symbols(1, "2026-05-17", universe, 5)
        p2 = _pick_random_symbols(2, "2026-05-17", universe, 5)
        # Same content possible by chance; assert ordering at least
        # differs (RNG is fully deterministic so order is stable per
        # seed).
        assert p1 != p2

    def test_picks_differ_across_dates_same_profile(self):
        from simple_strategies import _pick_random_symbols
        universe = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                    "TSLA", "AMD", "INTC", "QCOM"]
        d1 = _pick_random_symbols(42, "2026-05-17", universe, 5)
        d2 = _pick_random_symbols(42, "2026-05-18", universe, 5)
        assert d1 != d2

    def test_first_run_no_positions_buys_today_picks(self):
        """Fresh profile with no positions → buys today's 5 picks."""
        from simple_strategies import run_random_stock_of_day
        ctx = _ctx(strategy_type="random")
        api = _stub_api(
            price_map={s: 100.0 for s in (
                "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                "TSLA", "AMD", "INTC", "QCOM", "AVGO", "TXN",
                "MU", "ADI", "NXPI", "KLAC", "LRCX", "AMAT",
                "ASML", "CRM", "ORCL", "ADBE", "NOW", "INTU",
                "WDAY", "TEAM", "ZM", "CDNS", "SNPS", "ANSS",
                "PTC", "FICO", "CPRT", "CSGP", "VRSK", "NFLX",
                "DIS", "CMCSA", "UBER", "BKNG", "CSCO", "IBM",
                "ACN", "DELL", "HPQ", "V", "MA", "PYPL",
                "FIS", "FISV", "GPN", "ADP", "PAYX", "SQ",
                "COF", "AXP", "JPM", "BAC", "WFC", "C", "GS",
                "MS", "USB", "PNC", "TFC", "SCHW", "BLK",
                "BA", "RTX", "LMT", "NOC", "GD", "GE", "HON",
                "CAT", "DE", "MMM", "ITW", "EMR", "ETN", "ROK",
                "ISRG", "DXCM", "ALGN", "IDXX", "ZBH", "SYK",
                "MDT", "ABT", "BSX", "EW", "TMO", "DHR", "A",
                "BIO", "IQV", "VRTX", "REGN", "AMGN", "GILD",
                "BIIB", "BMY", "LLY", "PFE", "MRK", "JNJ",
                "ABBV", "UNH", "CI", "HUM", "CNC", "MOH", "ELV",
                "HCA", "THC", "UHS", "DVA", "XOM", "CVX",
            )},
            positions=[],
        )
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info", return_value=api._account,
        ), patch("client.get_positions", return_value=[]), patch(
            "journal.log_trade",
        ) as fake_log_trade:
            summary = run_random_stock_of_day(ctx)
        assert summary["buys"] == 5
        assert summary["sells"] == 0
        assert fake_log_trade.call_count == 5
        # Each log entry must have an order_id (perfect-matching invariant)
        for call in fake_log_trade.call_args_list:
            assert call.kwargs["order_id"].startswith("ord-")
            assert call.kwargs["strategy"] == "random_stock_of_day"

    def test_closes_positions_not_in_todays_pick(self):
        """Existing positions in symbols not chosen today → sold."""
        from simple_strategies import (
            run_random_stock_of_day, _pick_random_symbols,
        )
        from segments import LARGE_CAP_UNIVERSE
        ctx = _ctx(strategy_type="random")
        today = datetime.now(tz=timezone.utc).date().isoformat()
        picks = _pick_random_symbols(
            ctx.profile_id, today, LARGE_CAP_UNIVERSE, 5,
        )
        # Hold one symbol that's definitely NOT in today's picks
        stale = next(s for s in LARGE_CAP_UNIVERSE if s not in picks)
        positions = [SimpleNamespace(symbol=stale, qty=50)]
        price_map = {s: 100.0 for s in LARGE_CAP_UNIVERSE}
        api = _stub_api(price_map=price_map, positions=positions)
        with patch("client.get_api", return_value=api), patch(
            "client.get_account_info", return_value=api._account,
        ), patch("client.get_positions", return_value=positions), patch(
            "journal.log_trade",
        ) as fake_log_trade:
            summary = run_random_stock_of_day(ctx)
        assert summary["sells"] >= 1
        # Find the SELL call for the stale symbol
        sell_calls = [
            c for c in fake_log_trade.call_args_list
            if c.kwargs.get("side") == "sell"
            and c.kwargs.get("symbol") == stale
        ]
        assert len(sell_calls) == 1


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
