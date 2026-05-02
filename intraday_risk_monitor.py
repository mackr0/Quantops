"""Item 2b of COMPETITIVE_GAP_PLAN.md — intraday risk monitoring.

Distinct from `crisis_detector` (daily cadence, lifetime-baseline
comparison). This module operates at every cycle (~15 min) and
triggers on ABSOLUTE intraday moves, not slow regime drift. Pro
risk teams have this layer for tail-event protection — vol spikes,
correlation breakdowns, halts, single-day drawdown acceleration.

Checks shipped:

  1. drawdown_acceleration: today's intraday drawdown > 2x the 7-day
     average daily drawdown → alert. Catches "today is unusually bad."

  2. vol_spike: SPY's last-hour realized vol > 3x the 20-day average
     hourly realized vol → alert. Catches sudden vol expansion that
     hasn't yet shown in VIX (which lags).

  3. sector_concentration_swing: largest sector's intraday move >
     3% in absolute value → alert. Catches sector-driven blowups
     even when the broader market is stable.

  4. held_position_halts: any held position has trading_halted=True
     at the broker → alert. Halt while we hold = liquidity gone.

When alerts fire, a "risk halt" state is written. Trade pipeline
reads this state and BLOCKS new BUY / SHORT entries while the halt
is active. Halts auto-clear after 60 minutes if no fresh alert.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Tunable thresholds
DRAWDOWN_ACCEL_MULTIPLE = 2.0   # today vs 7d average drawdown
VOL_SPIKE_MULTIPLE = 3.0         # current hour vs 20d avg hour
SECTOR_SWING_PCT = 3.0           # absolute % move in top sector
HALT_AUTO_CLEAR_SECONDS = 60 * 60  # 60 min


@dataclass
class IntradayRiskAlert:
    """One risk-check alert."""
    check_name: str
    severity: str   # "warning" | "critical"
    message: str
    metric_value: float
    threshold: float
    suggested_action: str  # "monitor" | "block_new_entries" | "pause_all"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check_name": self.check_name,
            "severity": self.severity,
            "message": self.message,
            "metric_value": round(self.metric_value, 4),
            "threshold": round(self.threshold, 4),
            "suggested_action": self.suggested_action,
        }


def check_drawdown_acceleration(
    today_intraday_pct: float,
    avg_7d_intraday_pct: float,
) -> Optional[IntradayRiskAlert]:
    """Today's intraday drawdown > N× the 7-day average → alert.

    Args:
        today_intraday_pct: today's high-to-current drawdown (positive
            number; 0.04 = 4% drawdown from intraday high).
        avg_7d_intraday_pct: 7-day average of daily high-to-current
            drawdowns (also positive).
    """
    if avg_7d_intraday_pct <= 0:
        return None
    multiple = today_intraday_pct / avg_7d_intraday_pct
    if multiple < DRAWDOWN_ACCEL_MULTIPLE:
        return None
    severity = "critical" if multiple >= 3.0 else "warning"
    return IntradayRiskAlert(
        check_name="drawdown_acceleration",
        severity=severity,
        message=(
            f"Intraday drawdown {today_intraday_pct*100:.2f}% is "
            f"{multiple:.1f}x the 7d average ({avg_7d_intraday_pct*100:.2f}%)"
        ),
        metric_value=multiple,
        threshold=DRAWDOWN_ACCEL_MULTIPLE,
        suggested_action=("pause_all" if severity == "critical"
                            else "block_new_entries"),
    )


def check_vol_spike(
    current_hourly_vol: float,
    avg_20d_hourly_vol: float,
) -> Optional[IntradayRiskAlert]:
    """Current-hour SPY realized vol > N× 20d average → alert."""
    if avg_20d_hourly_vol <= 0:
        return None
    multiple = current_hourly_vol / avg_20d_hourly_vol
    if multiple < VOL_SPIKE_MULTIPLE:
        return None
    severity = "critical" if multiple >= 5.0 else "warning"
    return IntradayRiskAlert(
        check_name="vol_spike",
        severity=severity,
        message=(
            f"SPY realized vol {current_hourly_vol*100:.2f}% is "
            f"{multiple:.1f}x the 20d hourly avg "
            f"({avg_20d_hourly_vol*100:.2f}%)"
        ),
        metric_value=multiple,
        threshold=VOL_SPIKE_MULTIPLE,
        suggested_action=("pause_all" if severity == "critical"
                            else "block_new_entries"),
    )


def check_sector_concentration_swing(
    sector_moves: Dict[str, float],
) -> Optional[IntradayRiskAlert]:
    """Any sector's intraday move (absolute %) > threshold → alert.

    Args:
        sector_moves: {sector_name: percent_change_today} (signed,
            e.g. -0.04 = -4%).
    """
    if not sector_moves:
        return None
    biggest = max(sector_moves.items(),
                  key=lambda kv: abs(kv[1]))
    sector, move = biggest
    abs_move_pct = abs(move) * 100
    if abs_move_pct < SECTOR_SWING_PCT:
        return None
    severity = "critical" if abs_move_pct >= 5.0 else "warning"
    return IntradayRiskAlert(
        check_name="sector_concentration_swing",
        severity=severity,
        message=(
            f"Sector {sector} moved {move*100:+.2f}% intraday "
            f"({abs_move_pct:.2f}% absolute, > {SECTOR_SWING_PCT:.1f}% "
            f"threshold)"
        ),
        metric_value=abs_move_pct,
        threshold=SECTOR_SWING_PCT,
        suggested_action=("block_new_entries"),
    )


def check_held_position_halts(
    halted_held_symbols: List[str],
) -> Optional[IntradayRiskAlert]:
    """Any held position is halted → alert. Halt = no liquidity."""
    if not halted_held_symbols:
        return None
    n = len(halted_held_symbols)
    severity = "critical" if n >= 3 else "warning"
    return IntradayRiskAlert(
        check_name="held_position_halts",
        severity=severity,
        message=(
            f"{n} held position(s) halted: "
            f"{', '.join(halted_held_symbols[:5])}"
        ),
        metric_value=n,
        threshold=1,
        suggested_action="pause_all" if severity == "critical" else "block_new_entries",
    )


def collect_intraday_alerts(
    today_intraday_pct: float = 0.0,
    avg_7d_intraday_pct: float = 0.0,
    current_hourly_vol: float = 0.0,
    avg_20d_hourly_vol: float = 0.0,
    sector_moves: Optional[Dict[str, float]] = None,
    halted_held_symbols: Optional[List[str]] = None,
) -> List[IntradayRiskAlert]:
    """Run all checks; return active alerts.

    Inputs are pre-computed by the scheduler task that owns the data
    fetches. This function is pure compute — easy to unit-test.
    """
    alerts: List[IntradayRiskAlert] = []
    for alert in (
        check_drawdown_acceleration(today_intraday_pct,
                                       avg_7d_intraday_pct),
        check_vol_spike(current_hourly_vol, avg_20d_hourly_vol),
        check_sector_concentration_swing(sector_moves or {}),
        check_held_position_halts(halted_held_symbols or []),
    ):
        if alert is not None:
            alerts.append(alert)
    return alerts


def aggregate_action(alerts: List[IntradayRiskAlert]) -> str:
    """Combine multiple alerts into a single recommended action.

    Hierarchy: pause_all > block_new_entries > monitor > pass.
    """
    if not alerts:
        return "pass"
    actions = [a.suggested_action for a in alerts]
    if "pause_all" in actions:
        return "pause_all"
    if "block_new_entries" in actions:
        return "block_new_entries"
    return "monitor"


# ---------------------------------------------------------------------------
# Risk-halt state — written when an alert fires; read by trade_pipeline
# ---------------------------------------------------------------------------

def write_risk_halt_state(db_path: str,
                              action: str,
                              alerts: List[IntradayRiskAlert]) -> None:
    """Write the current risk-halt state to the journal DB.

    Stored in a single-row `intraday_risk_halt` table (created on
    first write). Trade pipeline reads via `get_active_risk_halt`.
    Auto-clears after HALT_AUTO_CLEAR_SECONDS via the read path.
    """
    import json
    from journal import _get_conn
    conn = _get_conn(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intraday_risk_halt (
              id INTEGER PRIMARY KEY,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              action TEXT NOT NULL,
              alerts_json TEXT
        )"""
    )
    # Replace the single row (id=1) atomically
    conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
    conn.execute(
        """INSERT INTO intraday_risk_halt (id, action, alerts_json)
           VALUES (1, ?, ?)""",
        (action, json.dumps([a.as_dict() for a in alerts])),
    )
    conn.commit()
    conn.close()


def get_active_risk_halt(db_path: str) -> Optional[Dict[str, Any]]:
    """Return the active risk-halt state, or None if cleared/expired.

    Auto-expires entries older than HALT_AUTO_CLEAR_SECONDS. This is
    a read-side feature — the next call after expiry returns None.
    """
    import json
    from journal import _get_conn
    try:
        conn = _get_conn(db_path)
        row = conn.execute(
            "SELECT created_at, action, alerts_json "
            "FROM intraday_risk_halt WHERE id=1"
        ).fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None

    # Expire by age
    try:
        from datetime import datetime as _dt
        created = _dt.fromisoformat(row["created_at"])
        age_seconds = (_dt.utcnow() - created).total_seconds()
    except Exception:
        age_seconds = 0
    if age_seconds > HALT_AUTO_CLEAR_SECONDS:
        # Stale — clear and return None
        try:
            conn = _get_conn(db_path)
            conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
            conn.commit()
            conn.close()
        except Exception:
            pass
        return None

    if row["action"] in ("pass", None):
        return None
    return {
        "created_at": row["created_at"],
        "action": row["action"],
        "alerts": json.loads(row["alerts_json"] or "[]"),
        "age_seconds": age_seconds,
    }


def clear_risk_halt(db_path: str) -> None:
    """Manually clear the halt state."""
    from journal import _get_conn
    try:
        conn = _get_conn(db_path)
        conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
        conn.commit()
        conn.close()
    except Exception:
        pass
