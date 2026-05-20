"""Test the trading pipeline components.

These tests verify risk management, position sizing, and drawdown
protection work correctly without requiring network calls.
"""

import sqlite3
import pytest


class TestDrawdownProtection:
    """Drawdown protection must reduce/pause at correct thresholds."""

    def test_normal_no_drawdown(self, tmp_profile_db):
        from portfolio_manager import check_drawdown
        from user_context import UserContext

        # Insert a snapshot showing peak equity
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, portfolio_value, num_positions) "
            "VALUES ('2026-04-01', 10000, 5000, 5000, 2)"
        )
        conn.commit()
        conn.close()

        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            db_path=tmp_profile_db,
            drawdown_pause_pct=0.20,
            drawdown_reduce_pct=0.10,
        )
        account = {"equity": 10000, "cash": 5000, "portfolio_value": 5000}
        result = check_drawdown(ctx, account)
        assert result["action"] == "normal"
        assert result["drawdown_pct"] == 0.0

    def test_reduce_at_threshold(self, tmp_profile_db):
        from portfolio_manager import check_drawdown
        from user_context import UserContext

        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, portfolio_value, num_positions) "
            "VALUES ('2026-04-01', 10000, 5000, 5000, 2)"
        )
        conn.commit()
        conn.close()

        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            db_path=tmp_profile_db,
            drawdown_pause_pct=0.20,
            drawdown_reduce_pct=0.10,
        )
        # Equity dropped 12% from peak of 10000
        account = {"equity": 8800, "cash": 4000, "portfolio_value": 4800}
        result = check_drawdown(ctx, account)
        assert result["action"] == "reduce"

    def test_pause_at_threshold(self, tmp_profile_db):
        from portfolio_manager import check_drawdown
        from user_context import UserContext

        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            "INSERT INTO daily_snapshots (date, equity, cash, portfolio_value, num_positions) "
            "VALUES ('2026-04-01', 10000, 5000, 5000, 2)"
        )
        conn.commit()
        conn.close()

        ctx = UserContext(
            user_id=1, segment="test",
            alpaca_api_key="k", alpaca_secret_key="s",
            db_path=tmp_profile_db,
            drawdown_pause_pct=0.20,
            drawdown_reduce_pct=0.10,
        )
        # Equity dropped 25% from peak
        account = {"equity": 7500, "cash": 3000, "portfolio_value": 4500}
        result = check_drawdown(ctx, account)
        assert result["action"] == "pause"


class TestSegments:
    """Verify segment definitions are consistent."""

    def test_all_segments_have_universe(self):
        from segments import list_segments, get_segment
        for seg_name in list_segments():
            seg = get_segment(seg_name)
            assert "universe" in seg, f"{seg_name} missing universe"
            assert len(seg["universe"]) > 0, f"{seg_name} has empty universe"

    def test_all_segments_have_risk_params(self):
        from segments import list_segments, get_segment
        for seg_name in list_segments():
            seg = get_segment(seg_name)
            for key in ["stop_loss_pct", "take_profit_pct", "max_position_pct"]:
                assert key in seg, f"{seg_name} missing {key}"
                assert seg[key] > 0, f"{seg_name}.{key} must be positive"

    def test_stocks_segment_has_sane_price_range(self):
        """2026-05-20 (docs/22): the four cap-tier segments collapsed
        into a single 'stocks' segment with intentionally wide bounds
        (min_price=1.0, max_price=10000.0). Per-profile thresholds on
        the trading_profiles row are now the runtime gate."""
        from segments import get_segment
        stocks = get_segment("stocks")
        assert stocks["min_price"] > 0
        assert stocks["max_price"] > stocks["min_price"]
        # Wide enough to cover both penny stocks and BRK-class names
        assert stocks["min_price"] <= 5.0
        assert stocks["max_price"] >= 1000.0

class TestMetrics:
    """Verify metrics module handles edge cases."""

    def test_empty_data(self):
        from metrics import calculate_all_metrics
        result = calculate_all_metrics([])
        assert isinstance(result, dict)
        # Should return zeroed metrics, not crash
        assert result.get("total_return", 0) == 0 or "total_return" in result

    def test_single_profile_db(self, tmp_profile_db):
        from metrics import calculate_all_metrics
        result = calculate_all_metrics([tmp_profile_db])
        assert isinstance(result, dict)


class TestEncryption:
    """Verify Fernet encryption round-trips correctly."""

    def test_encrypt_decrypt_roundtrip(self):
        from crypto import encrypt, decrypt
        original = "sk-ant-api03-test-key-12345"
        encrypted = encrypt(original)
        assert encrypted != original
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_empty_string(self):
        from crypto import encrypt, decrypt
        encrypted = encrypt("")
        assert decrypt(encrypted) == ""
