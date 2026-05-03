"""Short-borrow cost accrual.

DYNAMIC_UNIVERSE_PLAN.md / TECHNICAL_DOCUMENTATION.md §15 deferred
item — actual broker P&L on a short includes the daily borrow rate
charged by the lender. Alpaca's `unrealized_pl` does NOT account
for this; on most names the rate is 0.25-2% annualized so for trades
held same-day it's noise, but for shorts held 5+ days it can swing
the realized P&L meaningfully.

This module computes a deterministic, conservative borrow accrual
that's subtracted from cover-time P&L. Defaults match Interactive
Brokers' "general collateral" rate for liquid names (~1.8% annualized
≈ 0.5 bps/day). Per-symbol overrides for known hard-to-borrow names
can be added to `HARD_TO_BORROW_BPS_PER_DAY`.

The accrual is intentionally pessimistic: it never assumes the broker
gave us a free borrow. Better to report P&L slightly low than fool
ourselves on overnight holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

# Default annualized borrow rate for general-collateral liquid stocks
# Alpaca flags as `easy_to_borrow=True`. ~1.8% / 365 ≈ 0.49 bps/day.
DEFAULT_BPS_PER_DAY = 0.5

# Tier for `easy_to_borrow=False` names that aren't in the HTB map —
# typical IBKR borrow fee is ~5-15% annualized for non-GC names. Use
# 8% / 365 ≈ 22 bps/day. Conservative — real rates can be lower for
# moderately liquid names but we'd rather over-charge than under.
MEDIUM_BORROW_BPS_PER_DAY = 22.0

# Per-symbol overrides for known hard-to-borrow names. Update as
# needed; rates here are illustrative typical IBKR ranges. Symbols
# missing from this map fall through to MEDIUM_BORROW_BPS_PER_DAY
# when `easy_to_borrow=False`, else DEFAULT_BPS_PER_DAY.
HARD_TO_BORROW_BPS_PER_DAY: dict = {
    # Meme / squeeze names historically expensive to short
    "GME": 12.0,    # ~25% annualized typical (variable, sometimes 100%+)
    "AMC": 8.0,
    "BBBY": 20.0,
    # Hot biotech / SPAC names commonly hard to borrow
    "DJT": 30.0,    # historically extreme rates
    # Add more as encountered
}


def get_borrow_rate_bps_per_day(
    symbol: str,
    easy_to_borrow: Optional[bool] = None,
) -> float:
    """Look up the per-day borrow rate (in basis points) for `symbol`.

    Three-tier lookup:
      1. Symbol in HARD_TO_BORROW_BPS_PER_DAY → use that rate.
      2. easy_to_borrow=False (Alpaca flag) → MEDIUM_BORROW_BPS_PER_DAY.
      3. Otherwise → DEFAULT_BPS_PER_DAY (general-collateral).

    `easy_to_borrow` should be the Alpaca asset.easy_to_borrow flag.
    Pass None when unknown (legacy callers); falls back to default.
    """
    if symbol:
        sym = symbol.upper()
        if sym in HARD_TO_BORROW_BPS_PER_DAY:
            return HARD_TO_BORROW_BPS_PER_DAY[sym]
    if easy_to_borrow is False:
        return MEDIUM_BORROW_BPS_PER_DAY
    return DEFAULT_BPS_PER_DAY


def annual_pct_for_symbol(
    symbol: str,
    easy_to_borrow: Optional[bool] = None,
) -> float:
    """Convenience: bps/day → annualized percent. For prompt rendering."""
    bps = get_borrow_rate_bps_per_day(symbol, easy_to_borrow=easy_to_borrow)
    return round(bps * 365 / 100, 2)


def render_borrow_rate_for_prompt(
    symbol: str,
    easy_to_borrow: Optional[bool] = None,
) -> str:
    """One-line annotation: 'borrow ~1.8%/yr (GC)' / '~8%/yr (non-GC)'
    / '~110%/yr (HTB)'. Used per-candidate on shorts so the AI sees
    real cost-of-carry.
    """
    annual_pct = annual_pct_for_symbol(symbol, easy_to_borrow)
    if not symbol:
        tier = "GC"
    elif symbol.upper() in HARD_TO_BORROW_BPS_PER_DAY:
        tier = "HTB"
    elif easy_to_borrow is False:
        tier = "non-GC"
    else:
        tier = "GC"
    return f"borrow ~{annual_pct:.1f}%/yr ({tier})"


def compute_borrow_cost(
    shares: float,
    entry_price: float,
    days_held: float,
    symbol: Optional[str] = None,
    bps_per_day: Optional[float] = None,
) -> float:
    """Return the accrued borrow cost in USD for a short position.

    notional = shares × entry_price
    cost     = notional × (bps_per_day / 10000) × days_held

    Why bps not rate: financial convention. 1 bp = 0.01%.
    1 bp/day × 365 days = 365 bps/year ≈ 3.65% annualized.

    `symbol` lookup happens only when `bps_per_day` is None — explicit
    rate always wins.

    Returns 0.0 for non-positive inputs (defensive). Result is always
    >= 0 by construction; caller subtracts it from gross short P&L.
    """
    if shares <= 0 or entry_price <= 0 or days_held <= 0:
        return 0.0
    if bps_per_day is None:
        # Without easy_to_borrow context we can't tier; fall back to
        # the default. Live callers in the trade pipeline pass
        # bps_per_day explicitly (computed via get_borrow_rate_bps_per_day
        # with the Alpaca flag) so this branch is for post-cover
        # accrual where we don't have the live flag handy.
        bps_per_day = get_borrow_rate_bps_per_day(symbol or "")
    notional = shares * entry_price
    cost = notional * (bps_per_day / 10000.0) * days_held
    return round(cost, 4)


def days_between(entry_iso: str, exit_iso: Optional[str] = None) -> float:
    """Calendar days between two ISO timestamps. Borrow accrues on
    calendar days, including weekends — broker doesn't refund the
    weekend just because the market was closed.

    Returns 0.0 on parse failure (treats as same-day, no accrual).
    """
    if not entry_iso:
        return 0.0
    try:
        entry_dt = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if exit_iso:
        try:
            exit_dt = datetime.fromisoformat(exit_iso.replace("Z", "+00:00"))
        except Exception:
            return 0.0
    else:
        exit_dt = datetime.utcnow()
    # Strip tz for delta math
    if entry_dt.tzinfo is not None:
        entry_dt = entry_dt.replace(tzinfo=None)
    if exit_dt.tzinfo is not None:
        exit_dt = exit_dt.replace(tzinfo=None)
    delta = exit_dt - entry_dt
    return max(0.0, delta.total_seconds() / 86400.0)


def accrue_for_cover(
    db_path: Optional[str],
    symbol: str,
    cover_shares: float,
    cover_timestamp_iso: Optional[str] = None,
) -> float:
    """Look up the most recent open `sell_short` entry for `symbol`
    in the journal, compute the borrow cost from entry to now (or
    `cover_timestamp_iso`), and return it as a positive USD amount
    to be subtracted from the cover's gross P&L.

    Returns 0.0 when:
    - `db_path` is None or unreadable
    - No matching open sell_short entry exists in the journal
    - The entry is malformed (no timestamp or zero price)
    - Holding period is < 1 calendar day (intraday cover; borrow
      accrual is sub-1-bps and rounding makes it zero anyway)

    This function is fail-open: any error returns 0.0 rather than
    breaking the cover-logging path. Worst case we under-report
    borrow cost; we never crash a trade write.
    """
    if not db_path or cover_shares <= 0:
        return 0.0
    try:
        import sqlite3 as _sqlite
        conn = _sqlite.connect(db_path)
        row = conn.execute(
            "SELECT timestamp, price FROM trades "
            "WHERE symbol = ? AND side = 'sell_short' "
            "AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        conn.close()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    entry_ts, entry_price = row
    if not entry_price or entry_price <= 0:
        return 0.0
    days = days_between(entry_ts, cover_timestamp_iso)
    if days < 1.0:
        return 0.0
    return compute_borrow_cost(
        cover_shares, float(entry_price), days, symbol=symbol,
    )
