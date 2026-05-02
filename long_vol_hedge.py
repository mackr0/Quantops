"""Item 1c (long-vol portfolio hedge) — active tail-risk insurance.

We already have three layers of *passive* risk reduction:

  - `crisis_state`         — cuts new long entries (defensive sizing)
  - `intraday_risk_monitor` — halts new entries during alerts
  - per-trade stops          — protect individual positions

What was missing: ACTIVE protection. When the book is in drawdown OR
a regime-stress signal fires OR projected portfolio VaR is uncomfortably
high, this module proposes long SPY puts so that further SPY weakness
PAYS us. The puts gain value as the book bleeds, partially offsetting
losses. Insurance, not a bet.

Different from `crisis_state` and friends: those reduce exposure (pull
the book in). This one ADDS exposure (a long put leg) — explicit cost
in exchange for explicit downside coverage.

Triggers (any one fires when activation is requested):

  1. Drawdown ≥ `long_vol_hedge_drawdown_pct` (default 5%) from the
     30-day equity peak.
  2. crisis_state level ≥ "elevated".
  3. Latest portfolio_risk snapshot's 95% VaR ≥ `long_vol_hedge_var_pct`
     (default 3%) of equity.

Hedge construction (default; tunable per profile):

  - Instrument: SPY puts (most liquid, cheapest spread).
  - Strike: ~5% out-of-the-money (delta ≈ -0.30) — far enough OTM to
    be cheap, close enough to actually pay off in a meaningful drop.
  - Expiry: 30-60 days. Balances time decay (cheaper short-dated)
    against duration of coverage (longer = less rolling).
  - Premium budget: `long_vol_hedge_premium_pct` (default 1% of equity)
    per active hedge.
  - One open hedge per profile at a time.

Management:

  - Roll when DTE < 14 OR delta has decayed past -0.10 (the put has
    gone deeply OTM and is no longer hedging anything).
  - Close all hedges when ALL triggers clear simultaneously
    (drawdown recovered, crisis level normal, VaR back below
    threshold). Otherwise leave the hedge in place — paying for
    insurance you might still need.

The AI prompt sees:
  - Whether a hedge is currently open (strike, expiry, contracts,
    P&L since entry).
  - Which trigger fired (so the AI can reason about whether to
    increase / reduce other risk in light of the same signal).
  - Cost-to-date on rolled hedges over the rolling 90 days.

Persisted in `long_vol_hedges` table (one row per opened hedge,
status updated on close / roll). The latest active hedge is read by
trade_pipeline to surface in the prompt.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Defaults — overridable per profile via UserContext fields.
DEFAULT_DRAWDOWN_TRIGGER = 0.05    # 5% drawdown from 30d peak
DEFAULT_VAR_TRIGGER = 0.03         # 3% of book in 95% VaR
DEFAULT_PREMIUM_PCT = 0.01         # 1% of book per hedge
DEFAULT_OTM_PCT = 0.05             # strike 5% OTM
DEFAULT_TARGET_DTE = 45            # 30-60 day band; pick midpoint
ROLL_DTE_THRESHOLD = 14            # roll when DTE < this
ROLL_DELTA_THRESHOLD = -0.10       # roll when |delta| has decayed below

HEDGE_UNDERLYING = "SPY"


@dataclass
class HedgeTrigger:
    """One trigger that fired (or didn't). Surfaced to the AI prompt
    so the model can reason about WHY a hedge is open."""
    name: str          # 'drawdown' | 'crisis_state' | 'var'
    fired: bool
    metric_value: float
    threshold: float
    detail: str        # human-readable

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "fired": self.fired,
            "metric_value": round(self.metric_value, 4),
            "threshold": round(self.threshold, 4),
            "detail": self.detail,
        }


def evaluate_triggers(
    drawdown_pct: float,
    crisis_level: str,
    var_95_pct_of_equity: Optional[float],
    drawdown_trigger: float = DEFAULT_DRAWDOWN_TRIGGER,
    var_trigger: float = DEFAULT_VAR_TRIGGER,
) -> List[HedgeTrigger]:
    """Run each trigger condition; return all three with `fired` set."""
    triggers: List[HedgeTrigger] = []

    triggers.append(HedgeTrigger(
        name="drawdown",
        fired=drawdown_pct >= drawdown_trigger,
        metric_value=drawdown_pct,
        threshold=drawdown_trigger,
        detail=(
            f"Book is {drawdown_pct * 100:.2f}% below the 30-day peak "
            f"(trigger: {drawdown_trigger * 100:.1f}%)"
        ),
    ))

    crisis_fired = crisis_level in ("elevated", "crisis", "severe")
    triggers.append(HedgeTrigger(
        name="crisis_state",
        fired=crisis_fired,
        metric_value=1.0 if crisis_fired else 0.0,
        threshold=1.0,
        detail=f"Crisis level: {crisis_level}",
    ))

    if var_95_pct_of_equity is not None:
        triggers.append(HedgeTrigger(
            name="var",
            fired=var_95_pct_of_equity >= var_trigger,
            metric_value=var_95_pct_of_equity,
            threshold=var_trigger,
            detail=(
                f"95% portfolio VaR is {var_95_pct_of_equity * 100:.2f}% "
                f"of book (trigger: {var_trigger * 100:.1f}%)"
            ),
        ))
    else:
        triggers.append(HedgeTrigger(
            name="var",
            fired=False, metric_value=0.0, threshold=var_trigger,
            detail="No portfolio risk snapshot available yet",
        ))
    return triggers


def any_trigger_fired(triggers: List[HedgeTrigger]) -> bool:
    return any(t.fired for t in triggers)


def all_triggers_clear(triggers: List[HedgeTrigger]) -> bool:
    return not any(t.fired for t in triggers)


# ---------------------------------------------------------------------------
# Hedge sizing + strike selection
# ---------------------------------------------------------------------------

def select_hedge_strike(
    spot_price: float,
    otm_pct: float = DEFAULT_OTM_PCT,
) -> float:
    """Pick a put strike `otm_pct` below spot. Rounded to whole dollar
    (SPY strikes are $1 increments at typical price levels)."""
    raw = spot_price * (1 - otm_pct)
    return round(raw)


def select_hedge_expiry(
    today: Optional[date] = None,
    target_dte: int = DEFAULT_TARGET_DTE,
) -> date:
    """Pick the expiry date `target_dte` days out. Caller should
    snap to the nearest available SPY weekly/monthly expiry — this
    function returns the target; the chain-fetch path picks the
    closest real expiry."""
    today = today or date.today()
    return today + timedelta(days=target_dte)


def size_hedge_contracts(
    equity: float,
    estimated_premium_per_contract: float,
    premium_budget_pct: float = DEFAULT_PREMIUM_PCT,
) -> int:
    """How many contracts to buy. Budget = `premium_budget_pct` × equity.
    Each contract = 100 shares × premium. Floor to 0 if budget can't
    afford even one contract."""
    if equity <= 0 or estimated_premium_per_contract <= 0:
        return 0
    budget_dollars = equity * premium_budget_pct
    cost_per_contract = estimated_premium_per_contract * 100
    n = int(budget_dollars // cost_per_contract)
    return max(0, n)


# ---------------------------------------------------------------------------
# Roll / close decisions
# ---------------------------------------------------------------------------

def should_roll(
    expiry: date,
    delta: Optional[float],
    today: Optional[date] = None,
) -> Optional[str]:
    """Return reason string if the hedge should be rolled, else None."""
    today = today or date.today()
    dte = (expiry - today).days
    if dte < ROLL_DTE_THRESHOLD:
        return f"DTE {dte} < {ROLL_DTE_THRESHOLD} threshold"
    if delta is not None and delta > ROLL_DELTA_THRESHOLD:
        # Put delta is negative; "greater than -0.10" means decayed
        # toward zero (deeply OTM, no longer hedging).
        return (
            f"Delta {delta:.3f} has decayed past {ROLL_DELTA_THRESHOLD} "
            f"(put is too far OTM to provide meaningful protection)"
        )
    return None


def should_close(triggers: List[HedgeTrigger]) -> Optional[str]:
    """Return reason string if the hedge should be closed, else None.
    Hedge closes only when ALL triggers have cleared simultaneously."""
    if all_triggers_clear(triggers):
        return "All triggers cleared — drawdown recovered, crisis normal, VaR below threshold"
    return None


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _ensure_table(db_path: str) -> None:
    from journal import _get_conn
    conn = _get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS long_vol_hedges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            occ_symbol TEXT NOT NULL,
            underlying TEXT NOT NULL DEFAULT 'SPY',
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            contracts INTEGER NOT NULL,
            entry_premium REAL,
            entry_spot REAL,
            entry_delta REAL,
            close_reason TEXT,
            close_premium REAL,
            close_pnl_dollars REAL,
            triggers_json TEXT,
            order_id TEXT,
            close_order_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_active_hedge(db_path: str) -> Optional[Dict[str, Any]]:
    """Return the currently-open hedge row, or None."""
    _ensure_table(db_path)
    from journal import _get_conn
    conn = _get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM long_vol_hedges WHERE status='open' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_hedge_opened(
    db_path: str, hedge: Dict[str, Any], triggers: List[HedgeTrigger],
    order_id: Optional[str] = None,
) -> int:
    """Insert a new hedge row when an opening order is submitted."""
    _ensure_table(db_path)
    from journal import _get_conn
    conn = _get_conn(db_path)
    cur = conn.execute(
        """INSERT INTO long_vol_hedges (
            occ_symbol, underlying, strike, expiry, contracts,
            entry_premium, entry_spot, entry_delta, triggers_json,
            order_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            hedge["occ_symbol"], hedge.get("underlying", HEDGE_UNDERLYING),
            float(hedge["strike"]), hedge["expiry"],
            int(hedge["contracts"]),
            hedge.get("entry_premium"), hedge.get("entry_spot"),
            hedge.get("entry_delta"),
            json.dumps([t.as_dict() for t in triggers]),
            order_id,
        ),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def record_hedge_closed(
    db_path: str, hedge_id: int, close_reason: str,
    close_premium: Optional[float] = None,
    close_pnl_dollars: Optional[float] = None,
    close_order_id: Optional[str] = None,
) -> None:
    """Mark a hedge row closed."""
    _ensure_table(db_path)
    from journal import _get_conn
    conn = _get_conn(db_path)
    conn.execute(
        """UPDATE long_vol_hedges SET
            status = 'closed',
            closed_at = datetime('now'),
            close_reason = ?,
            close_premium = ?,
            close_pnl_dollars = ?,
            close_order_id = ?
        WHERE id = ?""",
        (close_reason, close_premium, close_pnl_dollars,
         close_order_id, hedge_id),
    )
    conn.commit()
    conn.close()


def hedge_cost_summary(db_path: str, days: int = 90) -> Dict[str, Any]:
    """Roll-up of cost spent on hedges over the past `days`. Surfaced
    to the AI prompt + dashboard so the user/AI can see the running
    insurance bill."""
    _ensure_table(db_path)
    from journal import _get_conn
    conn = _get_conn(db_path)
    rows = conn.execute(
        f"""SELECT entry_premium, contracts, close_pnl_dollars
        FROM long_vol_hedges
        WHERE opened_at >= datetime('now', '-{int(days)} days')""",
    ).fetchall()
    conn.close()

    n = len(rows)
    total_premium = 0.0
    total_pnl = 0.0
    for r in rows:
        if r["entry_premium"] and r["contracts"]:
            total_premium += float(r["entry_premium"]) * 100 * int(r["contracts"])
        if r["close_pnl_dollars"] is not None:
            total_pnl += float(r["close_pnl_dollars"])
    return {
        "n_hedges": n,
        "total_premium_paid": round(total_premium, 2),
        "total_pnl": round(total_pnl, 2),
        "net_cost": round(total_premium - total_pnl, 2),
        "lookback_days": days,
    }


# ---------------------------------------------------------------------------
# Drawdown helper (30-day rolling peak)
# ---------------------------------------------------------------------------

def compute_drawdown_from_30d_peak(db_path: str, current_equity: float) -> float:
    """Read the past 30 daily snapshots, find the peak equity, return
    drawdown vs current as a positive fraction. 0.0 if no history."""
    from journal import _get_conn
    try:
        conn = _get_conn(db_path)
        rows = conn.execute(
            "SELECT MAX(equity) FROM daily_snapshots "
            "WHERE date >= date('now', '-30 days')"
        ).fetchone()
        conn.close()
    except Exception:
        return 0.0
    if not rows or rows[0] is None:
        return 0.0
    peak = float(rows[0])
    if peak <= 0:
        return 0.0
    if current_equity >= peak:
        return 0.0
    return (peak - current_equity) / peak


# ---------------------------------------------------------------------------
# AI-prompt rendering
# ---------------------------------------------------------------------------

def render_hedge_for_prompt(
    hedge: Optional[Dict[str, Any]],
    triggers: Optional[List[HedgeTrigger]],
    cost_summary: Optional[Dict[str, Any]],
) -> str:
    """Render the active hedge state + trigger context for the AI's
    market-context block. Empty string when nothing to surface.

    The AI sees: whether the hedge is open, why it opened, and what
    insurance has cost lately. Lets the model factor protection into
    its sizing reasoning ("we already pay for tail protection — so
    this single-name short isn't doing double duty")."""
    if hedge is None and not (triggers and any_trigger_fired(triggers)):
        return ""
    lines = ["LONG-VOL TAIL HEDGE:"]
    if hedge:
        lines.append(
            f"  Active hedge: {hedge.get('contracts')} contracts of "
            f"{hedge.get('occ_symbol')} (SPY put, strike "
            f"${hedge.get('strike')}, expiry {hedge.get('expiry')})"
        )
        if hedge.get("entry_premium") is not None:
            lines.append(
                f"  Entry: ${hedge['entry_premium']:.2f}/contract on "
                f"{hedge.get('opened_at', '')[:10]}"
            )
    else:
        lines.append("  No active hedge yet — at least one trigger fired:")

    if triggers:
        for t in triggers:
            mark = "FIRED" if t.fired else "clear"
            lines.append(f"    [{mark}] {t.name}: {t.detail}")

    if cost_summary and cost_summary.get("n_hedges", 0) > 0:
        lines.append(
            f"  Insurance cost (last {cost_summary['lookback_days']}d): "
            f"${cost_summary['total_premium_paid']:,.0f} premium paid, "
            f"${cost_summary['total_pnl']:+,.0f} P&L on closed hedges, "
            f"net cost ${cost_summary['net_cost']:+,.0f}"
        )
    return "\n".join(lines)
