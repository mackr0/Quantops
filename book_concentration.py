"""Cross-profile concentration check.

Each profile already enforces `max_position_pct` against its own
equity. But if 10 profiles independently long AAPL, the AGGREGATE
book exposure to AAPL can exceed any single profile's intended limit
— and a single-name blow-up hits all of them simultaneously.

This module computes total $ exposure to one symbol summed across
every profile, divides by total book equity, and rejects new entries
that would push the combined exposure past
`max_book_exposure_pct_per_symbol` (default 25%).

It does NOT touch existing positions — only blocks entries that would
WORSEN concentration. Existing concentration drains naturally as
positions exit.
"""
from __future__ import annotations

import glob
import logging
import os
import sqlite3
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


def _profile_db_paths() -> Iterable[str]:
    """Find all per-profile DB files in the repo root."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    return glob.glob(os.path.join(repo_root, "quantopsai_profile_*.db"))


def get_book_exposure_to_symbol(
    symbol: str, db_paths: Optional[Iterable[str]] = None,
) -> Tuple[float, float]:
    """Sum (qty × current_price) for OPEN long+short positions of
    `symbol` across every profile. Also sums total book equity so the
    caller can compute %.

    Returns (total_exposure_dollars, total_book_equity).

    Returns (0.0, 0.0) when no profiles or no data — caller should
    treat that as "no constraint applies" (we can't be over-
    concentrated against zero base).
    """
    paths = list(db_paths or _profile_db_paths())
    if not paths:
        return 0.0, 0.0

    sym = (symbol or "").upper()
    total_exposure = 0.0
    total_equity = 0.0

    for path in paths:
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            # Open trades on this profile for this symbol. Uses the
            # `price` column (entry price). The trades table doesn't
            # store live current_price; for a conservative concentration
            # check, entry-cost exposure is the right unit anyway —
            # what matters is "how much capital is committed to this
            # single name?", not "what would it sell for right now?"
            rows = conn.execute(
                "SELECT qty, price FROM trades "
                "WHERE status = 'open' AND UPPER(symbol) = ?",
                (sym,),
            ).fetchall()
            for r in rows:
                qty = float(r["qty"] or 0)
                px = float(r["price"] or 0)
                # Long and short both contribute to single-name exposure
                # — a -50% move on a 25% book-share short is just as
                # bad as a -50% move on a 25% book-share long.
                total_exposure += abs(qty) * px

            # Latest equity from daily_snapshots
            eq_row = conn.execute(
                "SELECT equity FROM daily_snapshots "
                "ORDER BY date DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if eq_row and eq_row["equity"] is not None:
                total_equity += float(eq_row["equity"])
            conn.close()
        except Exception as exc:
            logger.debug("book_concentration: %s skipped (%s)", path, exc)
            continue

    return total_exposure, total_equity


def would_breach(
    symbol: str,
    proposed_trade_value: float,
    max_book_pct: float = 0.25,
    db_paths: Optional[Iterable[str]] = None,
) -> Tuple[bool, str, dict]:
    """Check whether adding `proposed_trade_value` to `symbol`'s
    aggregate book exposure would push the symbol's total share past
    `max_book_pct`.

    Returns (would_breach, reason, detail_dict).

    `detail_dict` carries the diagnostic numbers so the caller can
    surface them in error messages and the activity log:
        existing_book_exposure_dollars
        prospective_book_exposure_dollars
        total_book_equity
        prospective_pct
        cap_pct
    """
    existing_exposure, total_equity = get_book_exposure_to_symbol(
        symbol, db_paths=db_paths,
    )
    detail = {
        "existing_book_exposure_dollars": round(existing_exposure, 2),
        "prospective_book_exposure_dollars": round(
            existing_exposure + max(proposed_trade_value, 0.0), 2,
        ),
        "total_book_equity": round(total_equity, 2),
        "cap_pct": max_book_pct,
        "prospective_pct": None,
    }
    if total_equity <= 0:
        return False, "no equity baseline — concentration cap N/A", detail
    prospective_pct = (existing_exposure + max(proposed_trade_value, 0.0)) / total_equity
    detail["prospective_pct"] = round(prospective_pct, 4)
    if prospective_pct > max_book_pct:
        reason = (
            f"Cross-profile concentration: {symbol} would be "
            f"{prospective_pct:.1%} of book "
            f"(${detail['prospective_book_exposure_dollars']:,.0f} of "
            f"${total_equity:,.0f}), cap is {max_book_pct:.0%}"
        )
        return True, reason, detail
    return False, "within cap", detail
