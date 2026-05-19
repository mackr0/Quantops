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

REVERSAL_TAG = "reverse_2026_05_19_overbuy"


def _profile_db_path(profile_id: int) -> str:
    return f"/opt/quantopsai/quantopsai_profile_{profile_id}.db"


def _todays_bad_buys(profile_id: int):
    """Return a list of (symbol, qty, price) for every BUY row on
    profile_id's journal with date(timestamp) = '2026-05-19' AND
    strategy in (buy_hold_spy, random_stock_of_day) AND no matching
    reversal sell yet. Uses the running sum of net qty per symbol
    so partial reversals already done don't get double-sold."""
    db = _profile_db_path(profile_id)
    if not os.path.exists(db):
        return []
    try:
        with closing(sqlite3.connect(db)) as conn:
            buy_rows = conn.execute(
                "SELECT symbol, qty, price FROM trades "
                "WHERE date(timestamp) = '2026-05-19' "
                "AND strategy IN ('buy_hold_spy', 'random_stock_of_day') "
                "AND side = 'buy'"
            ).fetchall()
            # Total qty already reversed per symbol
            reversed_rows = conn.execute(
                "SELECT symbol, SUM(qty) FROM trades "
                "WHERE strategy = ? AND side = 'sell' "
                "GROUP BY symbol",
                (REVERSAL_TAG,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    reversed_qty = {r[0]: float(r[1] or 0) for r in reversed_rows}
    # Sum bad-buy qty per symbol
    buys_per_symbol: dict = {}
    last_price: dict = {}
    for sym, qty, px in buy_rows:
        buys_per_symbol[sym] = buys_per_symbol.get(sym, 0.0) + float(qty)
        last_price[sym] = float(px)
    out = []
    for sym, total_qty in buys_per_symbol.items():
        net = total_qty - reversed_qty.get(sym, 0.0)
        if net > 0:
            out.append((sym, int(net), last_price[sym]))
    return out


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

    for profile_id in (12, 13, 14):
        reversals = _todays_bad_buys(profile_id)
        if not reversals:
            logger.info("== profile %d: nothing left to reverse ==", profile_id)
            continue
        ctx = _get_ctx(profile_id)
        api = get_api(ctx)
        logger.info("== profile %d (%s) ==", profile_id, ctx.display_name)

        for symbol, qty, original_price in reversals:
            total_attempted += 1
            current_price = _fetch_price(api, symbol)
            if not current_price:
                logger.error("  [error] %s — could not fetch price", symbol)
                continue
            reason = (
                f"REVERSAL of 2026-05-19 bad buy: net unreversed qty={qty} "
                f"(strategy bug — baseline re-fired). Selling at market "
                f"to revert to yesterday's holdings."
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
        "REVERSAL summary: attempted=%d succeeded=%d",
        total_attempted, total_succeeded,
    )


if __name__ == "__main__":
    main()
