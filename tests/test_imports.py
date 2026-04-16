"""Test that all modules import without errors.

This is the first line of defense — if an import fails, everything else
is broken. Run this after every code change.
"""

import pytest


class TestCoreImports:
    """Every Python module must import cleanly."""

    def test_config(self):
        import config

    def test_user_context(self):
        from user_context import UserContext

    def test_segments(self):
        from segments import list_segments, get_segment

    def test_journal(self):
        from journal import init_db, log_trade, get_trade_history

    def test_models(self):
        from models import init_user_db, create_user, get_user_profiles

    def test_crypto_module(self):
        from crypto import encrypt, decrypt

    def test_backtester(self):
        from backtester import backtest_strategy, backtest_comparison

    def test_backtest_worker(self):
        from backtest_worker import start_backtest, get_job_status


class TestStrategyImports:
    """All 5 strategy engines + router must import."""

    def test_strategy_router(self):
        from strategy_router import run_strategy

    def test_strategy_micro(self):
        from strategy_micro import micro_combined_strategy

    def test_strategy_small(self):
        from strategy_small import small_combined_strategy

    def test_strategy_mid(self):
        from strategy_mid import mid_combined_strategy

    def test_strategy_large(self):
        from strategy_large import large_combined_strategy

    def test_strategy_crypto(self):
        from strategy_crypto import crypto_combined_strategy


class TestIntelligenceImports:
    """All intelligence features must import."""

    def test_self_tuning(self):
        from self_tuning import build_performance_context

    def test_market_regime(self):
        from market_regime import detect_regime

    def test_earnings_calendar(self):
        from earnings_calendar import check_earnings

    def test_political_sentiment(self):
        from political_sentiment import get_maga_mode_context

    def test_ai_analyst(self):
        from ai_analyst import analyze_symbol

    def test_ai_providers(self):
        from ai_providers import call_ai

    def test_ai_tracker(self):
        from ai_tracker import record_prediction


class TestTradingImports:
    """Trading pipeline modules must import."""

    def test_trade_pipeline(self):
        from trade_pipeline import run_trade_cycle

    def test_trader(self):
        import trader

    def test_portfolio_manager(self):
        from portfolio_manager import check_drawdown, calculate_atr_stops

    def test_correlation(self):
        from correlation import check_correlation

    def test_screener(self):
        from screener import screen_by_price_range


class TestWebImports:
    """Web application modules must import."""

    def test_app_factory(self):
        from app import create_app

    def test_metrics(self):
        from metrics import calculate_all_metrics

    def test_notifications(self):
        import notifications


class TestSchedulerImports:
    """Scheduler must import."""

    def test_multi_scheduler(self):
        from multi_scheduler import run_segment_cycle
