"""One-shot reversal of the 2026-05-19 baseline-strategy over-buys.

Caused by simple_strategies bug (buy_hold rebalanced daily, random
re-rolled daily). For each baseline profile (12/BuyHoldSPY,
13/RandomA, 14/RandomB), sells exactly the qty/symbol of today's
buy rows so the portfolio reverts to yesterday's holdings.

IDEMPOTENT — checks per-(profile, symbol) for an existing reversal
sell with strategy='reverse_2026_05_19_overbuy' and skips if present.

Run on prod via:
    /opt/quantopsai/venv/bin/python scripts/reverse_2026_05_19_overbuy.py
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

logger = logging.getLogger("reversal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# Profile → list of (symbol, qty, original_price) buys to reverse.
# Pulled by hand from `trades` rows with date(timestamp) = 2026-05-19
# at 2026-05-19T13:30 UTC (the bad first-cycle today).
TO_REVERSE = {
    # Profile 12 (BuyHoldSPY): drift-rebalance bug → 2nd SPY buy
    12: [("SPY", 322, 735.11)],
    # Profile 13 (RandomA): daily re-roll → today's 5 picks
    13: [
        ("KMB", 487, 96.755),
        ("ROK", 109, 432.11),
        ("MSFT", 109, 430.555),
        ("T", 1919, 24.59),
        ("CMCSA", 1872, 25.205),
    ],
    # Profile 14 (RandomB): daily re-roll → today's 5 picks
    14: [
        ("JNJ", 205, 228.72),
        ("REGN", 74, 631.6),
        ("WDAY", 353, 133.46),
        ("AAPL", 158, 296.73),
        ("TMO", 106, 442.765),
    ],
}

REVERSAL_TAG = "reverse_2026_05_19_overbuy"


def _profile_db_path(profile_id: int) -> str:
    return f"/opt/quantopsai/quantopsai_profile_{profile_id}.db"


def _already_reversed(db: str, symbol: str) -> bool:
    """Idempotency check: is there already a reversal sell logged
    for this (profile, symbol)?"""
    try:
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE strategy = ? AND symbol = ? "
                "AND side = 'sell' LIMIT 1",
                (REVERSAL_TAG, symbol),
            ).fetchone()
            return row is not None
    except sqlite3.OperationalError:
        return False


def _get_ctx(profile_id: int):
    """Build the full UserContext via the canonical helper so
    get_alpaca_api() resolves to the right per-profile credentials."""
    from models import build_user_context_from_profile
    return build_user_context_from_profile(profile_id)


def main():
    from simple_strategies import _submit_and_log, _fetch_price
    from client import get_api

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    logger.info("REVERSAL run started at %s", now_iso)

    total_attempted = 0
    total_succeeded = 0
    total_skipped = 0

    for profile_id, reversals in TO_REVERSE.items():
        db = _profile_db_path(profile_id)
        if not os.path.exists(db):
            logger.warning("Profile %d DB not found at %s — skipping",
                           profile_id, db)
            continue
        ctx = _get_ctx(profile_id)
        api = get_api(ctx)
        logger.info("== profile %d (%s) ==", profile_id, ctx.display_name)

        for symbol, qty, original_price in reversals:
            total_attempted += 1
            if _already_reversed(db, symbol):
                logger.info("  [skip] %s — already reversed (idempotent)", symbol)
                total_skipped += 1
                continue
            current_price = _fetch_price(api, symbol)
            if not current_price:
                logger.error("  [error] %s — could not fetch price", symbol)
                continue
            reason = (
                f"REVERSAL of 2026-05-19 bad buy: original buy "
                f"qty={qty} @ ${original_price:.2f} (strategy bug — "
                f"baseline re-fired). Selling at market to revert to "
                f"yesterday's holdings."
            )
            logger.info("  [sell] %s qty=%d @ ~$%.2f (orig $%.2f)",
                        symbol, qty, current_price, original_price)
            ok = _submit_and_log(
                api, ctx, symbol, "sell", qty, current_price,
                REVERSAL_TAG, reason,
            )
            if ok:
                total_succeeded += 1
            else:
                logger.error("  [error] %s — submit/log failed", symbol)

    logger.info(
        "REVERSAL summary: attempted=%d succeeded=%d skipped=%d",
        total_attempted, total_succeeded, total_skipped,
    )


if __name__ == "__main__":
    main()
