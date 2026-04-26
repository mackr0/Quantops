"""Tests for Layer 5 — cross-profile insight propagation.

Verifies that when an adjustment is marked 'improved' on one profile,
the same detection rule runs against peer profiles. Critically: peers
get the change applied based on their OWN data, not copy-pasted from
the source profile."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


class TestDetectorMapping:
    def test_known_change_types_map_to_optimizers(self):
        """Every change_type the tuner emits should have a propagation
        target. New types added in future waves will need an entry."""
        from insight_propagation import _detector_for
        # Spot-check key entries — full coverage isn't enforced here so
        # we don't break when intentionally non-propagatable types
        # are added later.
        for change_type in [
            "confidence_threshold_optimization",
            "regime_position_sizing",
            "strategy_toggle",
            "concentration_reduce",
            "min_volume_raise",
            "rsi_overbought_raise",
        ]:
            assert _detector_for(change_type) is not None, (
                f"No detector mapped for change_type {change_type!r}")

    def test_unknown_change_type_returns_none(self):
        from insight_propagation import _detector_for
        assert _detector_for("user_initiated_manual_change") is None
        assert _detector_for("") is None


class TestPeerEnumeration:
    def test_peers_excludes_source_profile(self):
        from insight_propagation import _peer_profiles
        with patch("models.get_trading_profile",
                    return_value={"id": 5, "user_id": 1}):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                {"id": 1, "user_id": 1, "name": "P1"},
                {"id": 3, "user_id": 1, "name": "P3"},
            ]
            with patch("models._get_conn", return_value=mock_conn):
                peers = _peer_profiles(5)
                # SQL query already filters id != 5; just ensure we got
                # the rows.
                assert len(peers) == 2
                # Verify the SQL excluded the source
                args = mock_conn.execute.call_args[0]
                assert "id != ?" in args[0]
                assert args[1][1] == 5  # second param is the source id

    def test_no_peers_when_source_missing(self):
        from insight_propagation import _peer_profiles
        with patch("models.get_trading_profile", return_value=None):
            assert _peer_profiles(99) == []


class TestPropagateInsight:
    def test_no_op_when_change_type_unknown(self):
        from insight_propagation import propagate_insight
        result = propagate_insight(1, "unknown_change_type", "some_param")
        assert result == []

    def test_no_op_when_no_peers(self):
        from insight_propagation import propagate_insight
        with patch("insight_propagation._peer_profiles", return_value=[]):
            result = propagate_insight(1, "concentration_reduce",
                                        "max_total_positions")
            assert result == []

    def test_propagates_to_peer_when_detection_triggers(self, tmp_path):
        """End-to-end: peer profile has its own data that triggers the
        detection rule, gets the change applied."""
        from insight_propagation import propagate_insight
        # Create a peer profile DB with predictions that would trigger
        # _optimize_max_total_positions (deep losses + low WR)
        peer_db = str(tmp_path / "quantopsai_profile_2.db")
        conn = sqlite3.connect(peer_db)
        conn.executescript("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT,
                predicted_signal TEXT, confidence REAL,
                price_at_prediction REAL, status TEXT DEFAULT 'resolved',
                actual_outcome TEXT
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                qty REAL, price REAL, pnl REAL
            );
        """)
        # Seed peer with predictions: 30 total, 9 wins -> 30% WR
        for i in range(9):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'win')",
                (f"W{i}",))
        for i in range(21):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss')",
                (f"L{i}",))
        # Seed trades — deep losses (avg < -200)
        for i in range(15):
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl) "
                "VALUES (?, 'buy', 100, 10, ?)",
                (f"T{i}", -300.0))
        conn.commit()
        conn.close()

        peer = {
            "id": 2, "user_id": 1, "name": "Peer",
            "max_total_positions": 10, "enable_self_tuning": 1,
            "max_position_pct": 0.10, "ai_confidence_threshold": 25,
            "stop_loss_pct": 0.03, "take_profit_pct": 0.10,
            "drawdown_pause_pct": 0.20, "drawdown_reduce_pct": 0.10,
            "max_correlation": 0.7, "max_sector_positions": 5,
        }

        # Need to chdir for the relative DB path
        import os
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("insight_propagation._peer_profiles", return_value=[peer]):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        with patch("self_tuning._get_recent_adjustment", return_value=None):
                            with patch("self_tuning._was_adjustment_effective", return_value=None):
                                result = propagate_insight(
                                    1, "concentration_reduce",
                                    "max_total_positions")
                                # Detection should have triggered on peer
                                assert len(result) == 1
                                assert "Peer" in result[0]
                                mock_up.assert_called()
        finally:
            os.chdir(original_cwd)

    def test_no_change_when_peer_data_doesnt_trigger(self, tmp_path):
        """If the peer's own data doesn't support the same change, no
        change is applied. This is the "no value-copying" guarantee."""
        from insight_propagation import propagate_insight
        peer_db = str(tmp_path / "quantopsai_profile_2.db")
        conn = sqlite3.connect(peer_db)
        conn.executescript("""
            CREATE TABLE ai_predictions (
                id INTEGER PRIMARY KEY, symbol TEXT,
                predicted_signal TEXT, confidence REAL,
                price_at_prediction REAL, status TEXT DEFAULT 'resolved',
                actual_outcome TEXT
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY, symbol TEXT, side TEXT,
                qty REAL, price REAL, pnl REAL
            );
        """)
        # Healthy peer: 30 predictions, 21 wins -> 70% WR
        for i in range(21):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'win')",
                (f"W{i}",))
        for i in range(9):
            conn.execute(
                "INSERT INTO ai_predictions "
                "(symbol, predicted_signal, confidence, price_at_prediction, "
                " status, actual_outcome) "
                "VALUES (?, 'BUY', 70, 100, 'resolved', 'loss')",
                (f"L{i}",))
        # Tiny losses
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, pnl) "
                "VALUES (?, 'buy', 100, 10, ?)",
                (f"T{i}", -10.0))
        conn.commit()
        conn.close()

        peer = {
            "id": 2, "user_id": 1, "name": "HealthyPeer",
            "max_total_positions": 10, "enable_self_tuning": 1,
            "max_position_pct": 0.10, "ai_confidence_threshold": 25,
            "stop_loss_pct": 0.03, "take_profit_pct": 0.10,
            "drawdown_pause_pct": 0.20, "drawdown_reduce_pct": 0.10,
            "max_correlation": 0.7, "max_sector_positions": 5,
        }

        import os
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("insight_propagation._peer_profiles", return_value=[peer]):
                with patch("models.update_trading_profile") as mock_up:
                    with patch("models.log_tuning_change"):
                        with patch("self_tuning._get_recent_adjustment", return_value=None):
                            with patch("self_tuning._was_adjustment_effective", return_value=None):
                                result = propagate_insight(
                                    1, "concentration_reduce",
                                    "max_total_positions")
                                # Healthy peer doesn't trigger
                                assert result == []
        finally:
            os.chdir(original_cwd)
