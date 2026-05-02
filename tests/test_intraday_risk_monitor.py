"""Item 2b — intraday risk monitoring tests."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def tmp_db():
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class TestCheckDrawdownAcceleration:
    def test_no_alert_when_today_within_avg(self):
        from intraday_risk_monitor import check_drawdown_acceleration
        # Today 1.5%, avg 1% → 1.5x, below 2.0 threshold
        assert check_drawdown_acceleration(0.015, 0.010) is None

    def test_alert_at_2x_avg(self):
        from intraday_risk_monitor import check_drawdown_acceleration
        # Today 2.5%, avg 1% → 2.5x → warning
        alert = check_drawdown_acceleration(0.025, 0.010)
        assert alert is not None
        assert alert.severity == "warning"
        assert alert.suggested_action == "block_new_entries"

    def test_critical_at_3x_avg(self):
        from intraday_risk_monitor import check_drawdown_acceleration
        # Today 4%, avg 1% → 4x → critical
        alert = check_drawdown_acceleration(0.04, 0.010)
        assert alert is not None
        assert alert.severity == "critical"
        assert alert.suggested_action == "pause_all"

    def test_zero_avg_returns_none(self):
        from intraday_risk_monitor import check_drawdown_acceleration
        # Avoid div-by-zero
        assert check_drawdown_acceleration(0.04, 0.0) is None


class TestCheckVolSpike:
    def test_no_alert_within_threshold(self):
        from intraday_risk_monitor import check_vol_spike
        # 2x avg, threshold 3x
        assert check_vol_spike(0.02, 0.01) is None

    def test_alert_at_3x(self):
        from intraday_risk_monitor import check_vol_spike
        alert = check_vol_spike(0.03, 0.01)
        assert alert is not None
        assert alert.severity == "warning"

    def test_critical_at_5x(self):
        from intraday_risk_monitor import check_vol_spike
        alert = check_vol_spike(0.05, 0.01)
        assert alert is not None
        assert alert.severity == "critical"


class TestCheckSectorConcentrationSwing:
    def test_no_alert_below_threshold(self):
        from intraday_risk_monitor import check_sector_concentration_swing
        # Largest move 2% — below 3% threshold
        assert check_sector_concentration_swing({
            "tech": 0.02, "energy": -0.015, "healthcare": 0.01,
        }) is None

    def test_alert_above_threshold(self):
        from intraday_risk_monitor import check_sector_concentration_swing
        # Energy -4% → alert
        alert = check_sector_concentration_swing({
            "tech": 0.01, "energy": -0.04,
        })
        assert alert is not None
        assert "energy" in alert.message.lower()

    def test_critical_at_5pct(self):
        from intraday_risk_monitor import check_sector_concentration_swing
        alert = check_sector_concentration_swing({"financials": -0.06})
        assert alert is not None
        assert alert.severity == "critical"

    def test_empty_input_returns_none(self):
        from intraday_risk_monitor import check_sector_concentration_swing
        assert check_sector_concentration_swing({}) is None


class TestCheckHeldPositionHalts:
    def test_no_halts_no_alert(self):
        from intraday_risk_monitor import check_held_position_halts
        assert check_held_position_halts([]) is None

    def test_one_halt_warns(self):
        from intraday_risk_monitor import check_held_position_halts
        alert = check_held_position_halts(["AAPL"])
        assert alert is not None
        assert alert.severity == "warning"

    def test_three_halts_critical(self):
        from intraday_risk_monitor import check_held_position_halts
        alert = check_held_position_halts(["AAPL", "TSLA", "NVDA"])
        assert alert is not None
        assert alert.severity == "critical"


class TestAggregateAction:
    def test_empty_returns_pass(self):
        from intraday_risk_monitor import aggregate_action
        assert aggregate_action([]) == "pass"

    def test_pause_all_dominates(self):
        from intraday_risk_monitor import (
            aggregate_action, IntradayRiskAlert,
        )
        alerts = [
            IntradayRiskAlert(
                check_name="x", severity="critical", message="m",
                metric_value=1, threshold=1,
                suggested_action="pause_all",
            ),
            IntradayRiskAlert(
                check_name="y", severity="warning", message="m",
                metric_value=1, threshold=1,
                suggested_action="block_new_entries",
            ),
        ]
        assert aggregate_action(alerts) == "pause_all"

    def test_block_new_when_only_warning(self):
        from intraday_risk_monitor import (
            aggregate_action, IntradayRiskAlert,
        )
        alerts = [
            IntradayRiskAlert(
                check_name="x", severity="warning", message="m",
                metric_value=1, threshold=1,
                suggested_action="block_new_entries",
            ),
        ]
        assert aggregate_action(alerts) == "block_new_entries"


class TestRiskHaltState:
    def test_write_and_read_round_trip(self, tmp_db):
        from intraday_risk_monitor import (
            collect_intraday_alerts, write_risk_halt_state,
            get_active_risk_halt, aggregate_action,
        )
        alerts = collect_intraday_alerts(
            today_intraday_pct=0.04, avg_7d_intraday_pct=0.01,
        )
        action = aggregate_action(alerts)
        write_risk_halt_state(tmp_db, action, alerts)

        active = get_active_risk_halt(tmp_db)
        assert active is not None
        assert active["action"] == action
        assert len(active["alerts"]) >= 1

    def test_no_state_when_action_is_pass(self, tmp_db):
        """If action is 'pass' (no alerts), get_active returns None
        even if a stale row exists."""
        from intraday_risk_monitor import (
            write_risk_halt_state, get_active_risk_halt,
        )
        write_risk_halt_state(tmp_db, "pass", [])
        assert get_active_risk_halt(tmp_db) is None

    def test_clear_removes_state(self, tmp_db):
        from intraday_risk_monitor import (
            write_risk_halt_state, get_active_risk_halt,
            clear_risk_halt, IntradayRiskAlert,
        )
        write_risk_halt_state(tmp_db, "block_new_entries", [
            IntradayRiskAlert(
                check_name="x", severity="warning", message="m",
                metric_value=1, threshold=1,
                suggested_action="block_new_entries",
            ),
        ])
        assert get_active_risk_halt(tmp_db) is not None
        clear_risk_halt(tmp_db)
        assert get_active_risk_halt(tmp_db) is None

    def test_no_halt_returns_none(self, tmp_db):
        """Fresh DB with no halt-state row → None."""
        from intraday_risk_monitor import get_active_risk_halt
        assert get_active_risk_halt(tmp_db) is None


class TestCollectIntradayAlerts:
    def test_no_inputs_no_alerts(self):
        from intraday_risk_monitor import collect_intraday_alerts
        assert collect_intraday_alerts() == []

    def test_multiple_simultaneous_alerts(self):
        from intraday_risk_monitor import collect_intraday_alerts
        alerts = collect_intraday_alerts(
            today_intraday_pct=0.05, avg_7d_intraday_pct=0.01,  # 5x
            current_hourly_vol=0.05, avg_20d_hourly_vol=0.01,   # 5x
            sector_moves={"tech": -0.06},                        # 6%
            halted_held_symbols=["AAPL"],
        )
        assert len(alerts) == 4
        names = {a.check_name for a in alerts}
        assert names == {
            "drawdown_acceleration", "vol_spike",
            "sector_concentration_swing", "held_position_halts",
        }
