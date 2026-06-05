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
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# Tunable thresholds
DRAWDOWN_ACCEL_MULTIPLE = 2.0   # today vs 7d average drawdown
# 2026-05-20 — absolute-magnitude floor on the drawdown-acceleration
# check. Without this, post-reset days produce tiny baselines
# (e.g., 7d avg = 0.24%) that today's normal noise (0.53%) easily
# exceeds by >2× → ALL trades blocked despite no actual risk event.
# 1.5% absolute is the floor below which "acceleration" is just
# market microstructure and shouldn't gate new entries.
DRAWDOWN_ACCEL_MIN_ABS = 0.015   # 1.5% intraday drawdown absolute floor
VOL_SPIKE_MULTIPLE = 3.0         # current hour vs 20d avg hour

# 2026-06-05 — three-layer sector / breadth risk model. The pre-2026-06-05
# rule (`SECTOR_SWING_PCT = 3.0 abs`) treated tech-5% and healthcare+4% as
# equivalent danger signals and halted the entire portfolio on either.
# Replaced by an asymmetric desk-style model:
#   Layer 1 — sector-specific halts (asymmetric thresholds; downside
#             tighter than upside)
#   Layer 2 — correlated-sector spillover (hard-down sectors infect
#             historically correlated sectors)
#   Layer 3 — breadth-level portfolio halt (kicks in only on a real
#             macro event: many sectors down, SPY down hard, VIX spike)
SECTOR_DOWN_HALT_PCT = 3.0       # sector move ≤ -3% → halt new longs in it
SECTOR_UP_HALT_PCT = 6.0         # sector move ≥ +6% → halt new longs (parabolic/squeeze risk)
SECTOR_HARD_HALT_PCT = 5.0       # sector move ≤ -5% triggers spillover
SECTOR_CORRELATIONS = {
    "tech":          ["comm_services", "consumer_disc"],
    "finance":       ["real_estate"],
    "real_estate":   ["finance", "utilities"],
    "energy":        ["materials"],
    "consumer_disc": ["tech"],
    "comm_services": ["tech"],
}
BREADTH_HALT_COUNT = 3           # ≥3 sectors halted → portfolio-wide
SPY_BROAD_HALT_PCT = 2.0         # SPY ≤ -2% intraday → portfolio-wide
VIX_SPIKE_LEVEL = 35.0           # VIX > 35 absolute → portfolio-wide

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
    """Today's intraday drawdown > N× the 7-day average AND
    >= DRAWDOWN_ACCEL_MIN_ABS absolute → alert.

    Args:
        today_intraday_pct: today's high-to-current drawdown (positive
            number; 0.04 = 4% drawdown from intraday high).
        avg_7d_intraday_pct: 7-day average of daily high-to-current
            drawdowns (also positive).

    2026-05-20: requires BOTH conditions (multiple ≥ 2.0 AND
    absolute ≥ 1.5%) to avoid false-positive halts when the 7-day
    baseline is small (post-reset, calm-week, etc.) and ordinary
    noise mechanically exceeds 2× the baseline. Without the
    absolute floor, every Tuesday morning following a quiet
    Monday triggered the halt — caught on the 2026-05-20 open
    when post-reset 0.24% baseline + 0.53% today's drawdown
    blocked all 13 profiles' trades despite no real risk event.
    """
    if avg_7d_intraday_pct <= 0:
        return None
    # NEW: absolute-magnitude floor — drawdowns below this are
    # market microstructure noise, not a risk event.
    if today_intraday_pct < DRAWDOWN_ACCEL_MIN_ABS:
        return None
    multiple = today_intraday_pct / avg_7d_intraday_pct
    if multiple < DRAWDOWN_ACCEL_MULTIPLE:
        return None
    severity = "critical" if multiple >= 3.0 else "warning"
    # 2026-06-05 — research/paper-book book never escalates to
    # pause_all. pause_all blocks EXITS too, which traps held risk on
    # exactly the days the operator most wants to be able to close
    # positions. block_new_entries stops adding risk while allowing
    # exits to fire — the actual conservative behavior.
    return IntradayRiskAlert(
        check_name="drawdown_acceleration",
        severity=severity,
        message=(
            f"Intraday drawdown {today_intraday_pct*100:.2f}% is "
            f"{multiple:.1f}x the 7d average ({avg_7d_intraday_pct*100:.2f}%)"
        ),
        metric_value=multiple,
        threshold=DRAWDOWN_ACCEL_MULTIPLE,
        suggested_action="block_new_entries",
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
    # 2026-06-05 — same reasoning as drawdown_acceleration: never
    # escalate to pause_all. Vol spikes are exactly when exits MUST
    # be allowed to fire (stop-losses, take-profits). Blocking them
    # traps risk.
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
        suggested_action="block_new_entries",
    )


def check_sector_halts(
    sector_moves: Dict[str, float],
) -> Dict[str, str]:
    """Layer 1 — sector-specific halts, asymmetric.

    Returns {sector: reason} for every sector whose intraday move
    breaches either the downside or the upside threshold. Downside is
    tighter (3%); upside is looser (6%) and only fires on parabolic
    moves that indicate squeeze / panic-buying risk.

    Args:
        sector_moves: {sector_name: signed pct change today}.

    Returns: dict of halted sector → human reason. Empty if none.
    """
    halted: Dict[str, str] = {}
    for sector, move in (sector_moves or {}).items():
        pct = move * 100
        if pct <= -SECTOR_DOWN_HALT_PCT:
            halted[sector] = f"down {pct:+.2f}% (threshold -{SECTOR_DOWN_HALT_PCT:.1f}%)"
        elif pct >= SECTOR_UP_HALT_PCT:
            halted[sector] = f"parabolic up {pct:+.2f}% (threshold +{SECTOR_UP_HALT_PCT:.1f}%)"
    return halted


def apply_correlated_spillover(
    halted_sectors: Dict[str, str],
    sector_moves: Dict[str, float],
) -> Dict[str, str]:
    """Layer 2 — extend halted set to correlated sectors when any
    halted sector is HARD down (≤ -5%).

    Tech down 5% drags comm_services and consumer_disc. The spillover
    set is added to `halted_sectors` with reason naming the source.

    Returns the (possibly extended) halted_sectors dict.
    """
    if not halted_sectors or not sector_moves:
        return halted_sectors
    extended = dict(halted_sectors)
    for src_sector in list(halted_sectors.keys()):
        src_move = (sector_moves.get(src_sector) or 0) * 100
        if src_move > -SECTOR_HARD_HALT_PCT:
            continue  # not hard enough to spill
        for downstream in SECTOR_CORRELATIONS.get(src_sector, []):
            if downstream not in extended:
                extended[downstream] = (
                    f"correlated spillover from {src_sector} "
                    f"({src_move:+.2f}%)"
                )
    return extended


def check_breadth_collapse(
    halted_sectors: Dict[str, str],
    spy_move_pct: float = 0.0,
    vix_level: float = 0.0,
) -> Optional[IntradayRiskAlert]:
    """Layer 3 — portfolio-wide halt only when there's evidence of a
    real macro event: many sectors halted, SPY itself down hard, or
    VIX spike. Otherwise sector-specific halts remain in force without
    portfolio-wide block.

    Args:
        halted_sectors: from check_sector_halts + spillover.
        spy_move_pct: SPY intraday move (signed pct, e.g. -2.3 = -2.3%).
        vix_level: current VIX level (absolute, not pct).

    Returns a portfolio-wide alert OR None when no broad event.
    """
    breadth_reasons = []
    if len(halted_sectors) >= BREADTH_HALT_COUNT:
        breadth_reasons.append(
            f"{len(halted_sectors)} sectors halted (≥{BREADTH_HALT_COUNT})"
        )
    if spy_move_pct <= -SPY_BROAD_HALT_PCT:
        breadth_reasons.append(
            f"SPY {spy_move_pct:+.2f}% (≤ -{SPY_BROAD_HALT_PCT:.1f}%)"
        )
    if vix_level >= VIX_SPIKE_LEVEL:
        breadth_reasons.append(
            f"VIX {vix_level:.1f} (≥ {VIX_SPIKE_LEVEL:.1f})"
        )
    if not breadth_reasons:
        return None
    return IntradayRiskAlert(
        check_name="breadth_collapse",
        severity="critical",
        message=" + ".join(breadth_reasons),
        metric_value=float(len(halted_sectors)),
        threshold=float(BREADTH_HALT_COUNT),
        suggested_action="block_new_entries",
    )


# Backward-compat alias used by anything still importing the old name.
# Returns a single alert summarizing the halted set, or None.
def check_sector_concentration_swing(
    sector_moves: Dict[str, float],
) -> Optional[IntradayRiskAlert]:
    halted = check_sector_halts(sector_moves)
    if not halted:
        return None
    msg = "; ".join(f"{s}: {r}" for s, r in sorted(halted.items()))
    # Severity escalates to "critical" once any halted sector hit
    # ≥ 5% absolute — preserves the pre-rewrite UI contract that
    # any 5%+ move is a critical alert (not just a warning).
    max_abs_pct = max(
        (abs(sector_moves.get(s, 0)) * 100 for s in halted),
        default=0.0,
    )
    severity = "critical" if max_abs_pct >= 5.0 else "warning"
    return IntradayRiskAlert(
        check_name="sector_concentration_swing",
        severity=severity,
        message=msg,
        metric_value=max_abs_pct,
        threshold=SECTOR_DOWN_HALT_PCT,
        suggested_action="block_new_entries",
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
    spy_move_pct: float = 0.0,
    vix_level: float = 0.0,
) -> List[IntradayRiskAlert]:
    """Run all non-sector checks; return active alerts.

    Use compute_halt_decision() to get the full 3-layer decision
    including halted_sectors. This function preserves the old shape
    so existing callers keep compiling.
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
    # Layer 3 (breadth) surfaces here so /issues sees it on the
    # legacy path too. Critically, we count only the PRIMARY (Layer 1)
    # halts against the breadth threshold — counting the
    # spillover-extended set would let a single hard-down sector
    # masquerade as a market-wide event.
    primary_halted = check_sector_halts(sector_moves or {})
    breadth = check_breadth_collapse(primary_halted, spy_move_pct, vix_level)
    if breadth is not None:
        alerts.append(breadth)
    return alerts


@dataclass
class HaltDecision:
    """Output of the 3-layer model. Replaces the old single-string
    `action`. Trade pipeline uses this to make per-trade decisions
    instead of portfolio-wide ones.
    """
    portfolio_action: str   # "pass" | "block_new_entries" | "pause_all"
    halted_sectors: Dict[str, str]   # sector → reason for the halt
    alerts: List[IntradayRiskAlert]  # all firing alerts for UI

    def is_sector_halted(self, sector: Optional[str]) -> bool:
        """Is this specific trade blocked?

        True if portfolio-wide halt, OR the trade's sector is in the
        halted set. Empty/None sector falls back to portfolio answer
        — we don't have enough info to allow it through.
        """
        if self.portfolio_action in ("pause_all", "block_new_entries"):
            # Portfolio-wide halt only when breadth/SPY/VIX triggered.
            # Per-sector alerts WITHOUT breadth are stored with
            # portfolio_action="pass" so non-halted sectors trade.
            return True
        if sector and sector in self.halted_sectors:
            return True
        return False


def compute_halt_decision(
    sector_moves: Optional[Dict[str, float]] = None,
    spy_move_pct: float = 0.0,
    vix_level: float = 0.0,
    other_alerts: Optional[List[IntradayRiskAlert]] = None,
) -> HaltDecision:
    """Run the 3-layer model end-to-end.

    Layer 1: per-sector halts (asymmetric thresholds)
    Layer 2: correlated-sector spillover (hard-down sectors extend to
             their correlated sectors)
    Layer 3: breadth collapse → portfolio-wide halt only when there is
             real evidence of a macro event

    `other_alerts` are the non-sector alerts (drawdown accel, vol
    spike, held-position halts) — these still drive portfolio-wide
    halts via their existing `suggested_action`.
    """
    sector_moves = sector_moves or {}
    other_alerts = other_alerts or []
    # Layer 1: primary sector halts. These are the "independent
    # sources of selling pressure" used by the breadth check.
    primary_halted = check_sector_halts(sector_moves)
    # Layer 2: extend with correlated-sector spillover. The extended
    # set is what trade_pipeline checks against; the primary set is
    # what Layer 3 uses to count breadth.
    halted = apply_correlated_spillover(primary_halted, sector_moves)
    # Layer 3: breadth uses primary count only, so a single hard-down
    # sector (which spills to 2-3 others) is NOT mis-read as broad
    # selloff.
    breadth = check_breadth_collapse(primary_halted, spy_move_pct, vix_level)

    # The breadth alert AND any other portfolio-wide alert can force
    # portfolio_action. Sector halts alone DO NOT — they're scoped.
    all_alerts = list(other_alerts)
    if breadth is not None:
        all_alerts.append(breadth)
    portfolio_actions = [
        a.suggested_action for a in all_alerts
        if a.suggested_action in ("pause_all", "block_new_entries")
    ]
    if "pause_all" in portfolio_actions:
        portfolio_action = "pause_all"
    elif "block_new_entries" in portfolio_actions:
        portfolio_action = "block_new_entries"
    else:
        portfolio_action = "pass"

    # We don't dump per-sector halts into `alerts` because the issues
    # page would explode; they're stored separately in halted_sectors.
    # The summary alert from check_sector_concentration_swing already
    # surfaces "N sectors halted" on the UI.
    if halted:
        all_alerts.append(IntradayRiskAlert(
            check_name="sector_concentration_swing",
            severity="warning",
            message="; ".join(
                f"{s}: {r}" for s, r in sorted(halted.items())
            ),
            metric_value=float(len(halted)),
            threshold=1.0,
            suggested_action="block_new_entries",
        ))
    return HaltDecision(
        portfolio_action=portfolio_action,
        halted_sectors=halted,
        alerts=all_alerts,
    )


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
                              alerts: List[IntradayRiskAlert],
                              halted_sectors: Optional[Dict[str, str]] = None) -> None:
    """Write the current risk-halt state to the journal DB.

    Stored in a single-row `intraday_risk_halt` table (created on
    first write). Trade pipeline reads via `get_active_risk_halt`.
    Auto-clears after HALT_AUTO_CLEAR_SECONDS via the read path.

    `halted_sectors` is the new sector-scoped layer (2026-06-05). The
    column is added on the fly for tables that pre-date the rewrite.
    """
    import json
    from journal import _get_conn
    halted_sectors = halted_sectors or {}
    with closing(_get_conn(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS intraday_risk_halt (
                  id INTEGER PRIMARY KEY,
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  action TEXT NOT NULL,
                  alerts_json TEXT,
                  halted_sectors_json TEXT
            )"""
        )
        # Pre-existing tables won't have the new column. Add it if
        # absent; ALTER is a no-op once it's already present.
        try:
            conn.execute(
                "ALTER TABLE intraday_risk_halt "
                "ADD COLUMN halted_sectors_json TEXT"
            )
        except sqlite3.OperationalError:
            pass
        conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
        conn.execute(
            "INSERT INTO intraday_risk_halt "
            "(id, action, alerts_json, halted_sectors_json) "
            "VALUES (1, ?, ?, ?)",
            (
                action,
                json.dumps([a.as_dict() for a in alerts]),
                json.dumps(halted_sectors),
            ),
        )
        conn.commit()


def get_active_risk_halt(db_path: str) -> Optional[Dict[str, Any]]:
    """Return the active risk-halt state, or None if cleared/expired.

    Auto-expires entries older than HALT_AUTO_CLEAR_SECONDS. This is
    a read-side feature — the next call after expiry returns None.
    """
    import json
    from journal import _get_conn
    try:
        with closing(_get_conn(db_path)) as conn:
            # Use SELECT * so that the halted_sectors_json column
            # (added 2026-06-05) is included even when older callers
            # haven't migrated yet. Missing columns are treated as null.
            row = conn.execute(
                "SELECT * FROM intraday_risk_halt WHERE id=1"
            ).fetchone()
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
            with closing(_get_conn(db_path)) as conn:
                conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
                conn.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _sh_exc:
            # Stale-halt cleanup write; halt state will re-evaluate
            # on next read. Surface for follow-up.
            logger.warning(
                "stale-halt cleanup write failed: %s: %s",
                type(_sh_exc).__name__, _sh_exc,
            )
        return None

    if row["action"] in ("pass", None):
        return None
    try:
        alerts = json.loads(row["alerts_json"] or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        logging.warning(
            "intraday_risk_halt.alerts_json is corrupt (id=1); "
            "falling back to empty list so halt gate keeps working",
            exc_info=True,
        )
        alerts = []
    try:
        halted_sectors = json.loads(
            (row["halted_sectors_json"] if "halted_sectors_json" in row.keys() else None) or "{}"
        )
    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
        halted_sectors = {}
    return {
        "created_at": row["created_at"],
        "action": row["action"],
        "alerts": alerts,
        "halted_sectors": halted_sectors,
        "age_seconds": age_seconds,
    }


def clear_risk_halt(db_path: str) -> None:
    """Manually clear the halt state."""
    from journal import _get_conn
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute("DELETE FROM intraday_risk_halt WHERE id=1")
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _hc_exc:
        # Halt clear write; next read re-evaluates. Surface for follow-up.
        logger.warning(
            "halt clear write failed: %s: %s",
            type(_hc_exc).__name__, _hc_exc,
        )
