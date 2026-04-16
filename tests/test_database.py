"""Test database schema integrity.

Verifies all tables exist with correct columns after init.
This catches the class of bugs where CREATE TABLE IF NOT EXISTS
doesn't add new columns to existing tables.
"""

import sqlite3
import pytest


class TestMainDatabase:
    """Main database (quantopsai.db) schema tests."""

    def _get_columns(self, db_path, table):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        return cols

    def _get_tables(self, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        return tables

    def test_all_tables_exist(self, tmp_main_db):
        tables = self._get_tables(tmp_main_db)
        expected = {
            "users", "user_segment_configs", "trading_profiles",
            "decision_log", "user_api_usage", "activity_log",
            "symbol_names", "tuning_history",
        }
        for t in expected:
            assert t in tables, f"Missing table: {t}"

    def test_users_columns(self, tmp_main_db):
        cols = self._get_columns(tmp_main_db, "users")
        required = {
            "id", "email", "password_hash", "display_name",
            "is_active", "is_admin", "alpaca_api_key_enc",
            "alpaca_secret_key_enc", "anthropic_api_key_enc",
            "notification_email", "resend_api_key_enc",
            "excluded_symbols", "scanning_active",
        }
        for c in required:
            assert c in cols, f"Missing users column: {c}"

    def test_trading_profiles_columns(self, tmp_main_db):
        cols = self._get_columns(tmp_main_db, "trading_profiles")
        required = {
            "id", "user_id", "name", "market_type", "enabled",
            "alpaca_api_key_enc", "alpaca_secret_key_enc",
            "stop_loss_pct", "take_profit_pct", "max_position_pct",
            "max_total_positions", "ai_confidence_threshold",
            "min_price", "max_price", "min_volume",
            "maga_mode", "enable_short_selling",
            "short_stop_loss_pct", "short_take_profit_pct",
            "enable_self_tuning",
            "ai_provider", "ai_model", "ai_api_key_enc",
            "schedule_type", "custom_start", "custom_end", "custom_days",
            "drawdown_pause_pct", "drawdown_reduce_pct",
            "avoid_earnings_days", "skip_first_minutes",
            "enable_consensus", "consensus_model", "consensus_api_key_enc",
            "use_atr_stops", "atr_multiplier_sl", "atr_multiplier_tp",
            "use_trailing_stops", "trailing_atr_multiplier",
            "use_limit_orders",
            "max_correlation", "max_sector_positions",
        }
        for c in required:
            assert c in cols, f"Missing trading_profiles column: {c}"

    def test_user_segment_configs_columns(self, tmp_main_db):
        cols = self._get_columns(tmp_main_db, "user_segment_configs")
        required = {
            "user_id", "segment", "enabled",
            "alpaca_api_key_enc", "alpaca_secret_key_enc",
        }
        for c in required:
            assert c in cols, f"Missing user_segment_configs column: {c}"

    def test_tuning_history_columns(self, tmp_main_db):
        cols = self._get_columns(tmp_main_db, "tuning_history")
        required = {
            "id", "profile_id", "user_id", "timestamp",
            "adjustment_type", "parameter_name",
            "old_value", "new_value", "reason",
            "outcome_after", "win_rate_after",
        }
        for c in required:
            assert c in cols, f"Missing tuning_history column: {c}"

    def test_migration_idempotent(self, tmp_main_db):
        """Running init_user_db twice should not error or lose data."""
        from models import init_user_db, create_user, get_user_by_email
        import config
        original = config.DB_PATH
        config.DB_PATH = tmp_main_db
        try:
            # Create a user
            uid = create_user("test@test.com", "password123", "Test")
            # Run init again
            init_user_db(tmp_main_db)
            # User should still exist
            user = get_user_by_email("test@test.com")
            assert user is not None
            assert user["id"] == uid
        finally:
            config.DB_PATH = original


class TestProfileDatabase:
    """Per-profile database (journal) schema tests."""

    def _get_columns(self, db_path, table):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        return cols

    def _get_tables(self, db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        return tables

    def test_all_tables_exist(self, tmp_profile_db):
        tables = self._get_tables(tmp_profile_db)
        expected = {
            "trades", "signals", "daily_snapshots", "ai_predictions",
            # Phase 3: alpha decay monitoring tables
            "signal_performance_history", "deprecated_strategies",
            # Phase 4: SEC filing history
            "sec_filings_history",
        }
        for t in expected:
            assert t in tables, f"Missing table: {t}"

    def test_trades_has_slippage_columns(self, tmp_profile_db):
        cols = self._get_columns(tmp_profile_db, "trades")
        for c in ["decision_price", "fill_price", "slippage_pct"]:
            assert c in cols, f"Missing trades column: {c}"

    def test_ai_predictions_columns(self, tmp_profile_db):
        cols = self._get_columns(tmp_profile_db, "ai_predictions")
        required = {
            "id", "timestamp", "symbol", "predicted_signal",
            "confidence", "reasoning", "price_at_prediction",
            "status", "actual_outcome", "actual_return_pct",
            # Phase 1: meta-model training columns
            "regime_at_prediction", "strategy_type", "features_json",
        }
        for c in required:
            assert c in cols, f"Missing ai_predictions column: {c}"

    def test_journal_init_idempotent(self, tmp_profile_db):
        """Running init_db twice should not error."""
        from journal import init_db, log_trade, get_trade_history
        init_db(tmp_profile_db)
        # Log a trade
        log_trade(
            symbol="AAPL", side="BUY", qty=10, price=150.0,
            signal_type="BUY", strategy="test", reason="test",
            db_path=tmp_profile_db,
        )
        # Run init again
        init_db(tmp_profile_db)
        # Trade should still exist
        trades = get_trade_history(db_path=tmp_profile_db)
        assert len(trades) >= 1
        assert trades[0]["symbol"] == "AAPL"
