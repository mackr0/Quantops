#!/usr/bin/env python3
"""Multi-account scheduler — runs segments via UserContext (no config.* mutation).

Each segment gets a UserContext that carries all credentials, DB paths, and risk
parameters through the entire call chain.  There is no _apply_segment_config /
_restore_config pattern.

For backward compatibility during migration, the scheduler can build a
UserContext from either:
  - The database (build_user_context) for multi-user mode, or
  - segments.py + config.py (build_context_from_segment) for the original
    single-owner, env-var-based setup.

Usage:
    python multi_scheduler.py                  # run all segments
    python multi_scheduler.py microsmall midcap  # run only selected segments
"""

import time
import logging
import signal
import sys
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from segments import list_segments, get_segment, SEGMENTS

# ── Timezone ─────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

# ── Graceful Shutdown ────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logging.info(f"Received signal {signum}, shutting down gracefully...")
    _shutdown = True


# ── Market Hours (same logic as scheduler.py) ────────────────────────

def is_market_open(now=None):
    """Return True if 9:30 AM - 4:00 PM ET, Monday-Friday."""
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now < market_close


def next_market_open(now=None):
    """Return datetime of next market open (9:30 AM ET), skipping weekends."""
    now = now or datetime.now(ET)
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= candidate or now.weekday() >= 5:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


# ── Build UserContext ────────────────────────────────────────────────

def _build_ctx(segment_name):
    """Build a UserContext for a segment.

    First tries the database (multi-user mode via models.build_user_context).
    Falls back to the env-var-based builder (build_context_from_segment) if the
    DB-based approach fails (e.g. no user DB set up yet).
    """
    try:
        from models import build_user_context
        return build_user_context(1, segment_name)
    except Exception:
        pass

    from user_context import build_context_from_segment
    return build_context_from_segment(segment_name)


# ── Task Runner ──────────────────────────────────────────────────────

def run_task(name, func):
    """Run *func* with logging, timing, and error handling."""
    logging.info(f"[TASK START] {name}")
    start = time.time()
    try:
        func()
        elapsed = time.time() - start
        logging.info(f"[TASK DONE]  {name} ({elapsed:.1f}s)")
    except Exception:
        elapsed = time.time() - start
        logging.exception(f"[TASK FAIL]  {name} ({elapsed:.1f}s)")


# ── Segment Cycle ────────────────────────────────────────────────────

def run_segment_cycle(ctx, run_scan=True, run_exits=True,
                      run_predictions=False, run_snapshot=False,
                      run_summary=False):
    """Run one full cycle for a given UserContext.

    All task functions receive ctx — no config.* globals are mutated.
    """
    seg_label = ctx.display_name or ctx.segment
    logging.info(f"--- [{seg_label.upper()}] segment cycle start ---")

    if run_scan:
        run_task(
            f"[{seg_label}] Aggressive Scan & Trade",
            lambda: _task_aggressive_scan_and_trade(ctx),
        )

    if run_exits:
        run_task(
            f"[{seg_label}] Check Exits",
            lambda: _task_check_exits(ctx),
        )

    if run_predictions:
        run_task(
            f"[{seg_label}] Resolve AI Predictions",
            lambda: _task_resolve_predictions(ctx),
        )

    if run_snapshot:
        run_task(
            f"[{seg_label}] Daily Snapshot",
            lambda: _task_daily_snapshot(ctx),
        )

    if run_summary:
        run_task(
            f"[{seg_label}] Daily Summary Email",
            lambda: _task_daily_summary_email(ctx),
        )

    logging.info(f"--- [{seg_label.upper()}] segment cycle end ---")


# ── Helpers ─────────────────────────────────────────────────────────

def run_full_screen_for_segment(ctx, seg):
    """Run the standard equity screener with ctx-specific parameters."""
    from screener import screen_by_price_range, find_volume_surges, \
        find_momentum_stocks, find_breakouts

    universe = seg.get("universe")
    candidates = screen_by_price_range(
        min_price=ctx.min_price,
        max_price=ctx.max_price,
        min_volume=ctx.min_volume,
        limit=50,
        universe=universe,
    )
    sym_list = [c["symbol"] for c in candidates]
    volume_surges = find_volume_surges(
        sym_list, volume_multiplier=ctx.volume_surge_multiplier)
    momentum = find_momentum_stocks(
        sym_list, min_gain_5d=ctx.momentum_5d_gain,
        min_gain_20d=ctx.momentum_20d_gain)
    breakouts = find_breakouts(sym_list)

    return {
        "candidates": candidates,
        "volume_surges": volume_surges,
        "momentum": momentum,
        "breakouts": breakouts,
    }


# ── Task Implementations ─────────────────────────────────────────────
# Each task receives a UserContext and passes it through.

def _task_aggressive_scan_and_trade(ctx):
    """Screen the segment's universe and auto-trade with AI review."""
    from screener import screen_by_price_range, find_volume_surges, \
        find_momentum_stocks, find_breakouts, run_crypto_screen
    from aggressive_trader import run_aggressive_scan_and_trade
    from notifications import notify_trade, notify_veto

    seg_label = ctx.display_name or ctx.segment
    seg = get_segment(ctx.segment)
    is_crypto = seg.get("is_crypto", False)

    if is_crypto:
        # Crypto uses its own screener with symbol conversion
        screen_results = run_crypto_screen(universe=seg.get("universe"))
    else:
        # Equity segments use the standard screener
        screen_results = run_full_screen_for_segment(ctx, seg)

    symbols = set()
    for cat in ("candidates", "volume_surges", "momentum", "breakouts"):
        for s in screen_results.get(cat, []):
            symbols.add(s["symbol"])

    symbols = list(symbols)[:30]

    if not symbols:
        logging.info(f"[{seg_label}] No candidates found in screen.")
        return

    logging.info(f"[{seg_label}] Running aggressive scan on {len(symbols)} candidates")
    summary = run_aggressive_scan_and_trade(symbols, ctx=ctx)
    logging.info(
        f"[{seg_label}] Trade summary: "
        f"buys={summary.get('buys', 0)}, "
        f"sells={summary.get('sells', 0)}, "
        f"ai_vetoed={summary.get('ai_vetoed', 0)}, "
        f"holds={summary.get('holds', 0)}, "
        f"errors={summary.get('errors', 0)}"
    )

    for detail in summary.get("details", []):
        if detail.get("action") in ("BUY", "SELL"):
            try:
                notify_trade(detail, detail, detail, ctx=ctx)
            except Exception:
                logging.exception("Failed to send trade notification")

    for veto in summary.get("vetoed_details", []):
        tech_signal = veto.get("technical_signal", "")
        if "BUY" in str(tech_signal):
            try:
                notify_veto(veto["symbol"], {"signal": tech_signal}, veto, ctx=ctx)
            except Exception:
                logging.exception("Failed to send veto notification")


def _task_check_exits(ctx):
    """Check stop-loss and take-profit triggers on open positions."""
    from trader import check_exits
    from notifications import notify_exit

    seg_label = ctx.display_name or ctx.segment
    results = check_exits(ctx=ctx)
    if results:
        for r in results:
            logging.info(
                f"[{seg_label}] Exit triggered: {r['symbol']} "
                f"{r['trigger'].upper()} qty={r['qty']} — {r['reason']}"
            )
            try:
                notify_exit(r["symbol"], r["trigger"], r["qty"], r["reason"], ctx=ctx)
            except Exception:
                logging.exception("Failed to send exit notification")
    else:
        logging.info(f"[{seg_label}] No exit triggers fired.")


def _task_resolve_predictions(ctx):
    """Resolve outstanding AI predictions against actual prices."""
    from ai_tracker import resolve_predictions
    from client import get_api

    api = get_api(ctx)
    resolve_predictions(api=api, db_path=ctx.db_path)
    logging.info("AI predictions resolved.")


def _task_daily_snapshot(ctx):
    """Save end-of-day portfolio snapshot."""
    from journal import init_db, log_daily_snapshot
    from client import get_account_info, get_positions

    init_db(ctx.db_path)
    account = get_account_info(ctx=ctx)
    positions = get_positions(ctx=ctx)
    log_daily_snapshot(
        equity=account["equity"],
        cash=account["cash"],
        portfolio_value=account["portfolio_value"],
        num_positions=len(positions),
        db_path=ctx.db_path,
    )
    logging.info(
        f"Daily snapshot saved: equity=${account['equity']:,.2f}, "
        f"positions={len(positions)}, cash=${account['cash']:,.2f}"
    )


def _task_daily_summary_email(ctx):
    """Send end-of-day summary email."""
    from notifications import notify_daily_summary
    notify_daily_summary(ctx=ctx)
    logging.info("Daily summary email sent.")


# ── Main Loop ────────────────────────────────────────────────────────

def main_loop(active_segments=None):
    """Run the multi-account scheduling loop.

    Parameters
    ----------
    active_segments : list[str] or None
        Segment names to run.  Defaults to all segments.
    """
    global _shutdown

    if active_segments is None:
        active_segments = list_segments()

    # ── Logging setup ────────────────────────────────────────────────
    log_dir = os.path.expanduser("~/QuantOpsAI/logs")
    os.makedirs(log_dir, exist_ok=True)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"quantopsai_multi_{today_str}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )

    logging.info("=" * 60)
    logging.info("QuantOpsAI MULTI-ACCOUNT scheduler starting")
    logging.info(f"Active segments: {active_segments}")
    logging.info(f"Log file: {log_file}")
    logging.info("=" * 60)

    # ── Signal handlers ──────────────────────────────────────────────
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── Interval tracking (last-run timestamps) ──────────────────────
    last_run = {
        "aggressive_scan": 0.0,
        "check_exits": 0.0,
        "resolve_predictions": 0.0,
        "daily_snapshot": None,  # Track by date string
    }

    INTERVAL_AGGRESSIVE_SCAN = 30 * 60   # 30 minutes
    INTERVAL_CHECK_EXITS = 15 * 60       # 15 minutes
    INTERVAL_RESOLVE_PREDICTIONS = 60 * 60  # 60 minutes

    while not _shutdown:
        now = datetime.now(ET)

        # Rotate log file if day changed
        new_today = now.strftime("%Y-%m-%d")
        if new_today != today_str:
            today_str = new_today
            new_log_file = os.path.join(log_dir, f"quantopsai_multi_{today_str}.log")
            root = logging.getLogger()
            for handler in root.handlers[:]:
                if isinstance(handler, logging.FileHandler):
                    root.removeHandler(handler)
                    handler.close()
            root.addHandler(logging.FileHandler(new_log_file))
            logging.info(f"Log rotated to {new_log_file}")

        current_time = time.time()
        market_open = is_market_open(now)

        do_scan = (current_time - last_run["aggressive_scan"]
                   >= INTERVAL_AGGRESSIVE_SCAN)
        do_exits = (current_time - last_run["check_exits"]
                    >= INTERVAL_CHECK_EXITS)
        do_predictions = (current_time - last_run["resolve_predictions"]
                          >= INTERVAL_RESOLVE_PREDICTIONS)
        do_snapshot = (now.hour == 15 and now.minute >= 55
                       and last_run["daily_snapshot"] != today_str)

        # Separate equity and crypto segments
        equity_segments = [s for s in active_segments if s != "crypto"]
        crypto_segments = [s for s in active_segments if s == "crypto"]

        ran_something = False

        # Equity segments: only during market hours
        if market_open and (do_scan or do_exits or do_predictions or do_snapshot):
            for seg_name in equity_segments:
                if _shutdown:
                    break
                try:
                    ctx = _build_ctx(seg_name)
                except Exception:
                    logging.exception(f"Failed to build context for segment {seg_name!r}")
                    continue

                logging.info(f"=== Processing segment: {seg_name} ===")
                run_segment_cycle(
                    ctx,
                    run_scan=do_scan,
                    run_exits=do_exits,
                    run_predictions=do_predictions,
                    run_snapshot=do_snapshot,
                    run_summary=do_snapshot,
                )
            ran_something = True

        # Crypto segments: 24/7
        if crypto_segments and (do_scan or do_exits or do_predictions):
            for seg_name in crypto_segments:
                if _shutdown:
                    break
                try:
                    ctx = _build_ctx(seg_name)
                except Exception:
                    logging.exception(f"Failed to build context for segment {seg_name!r}")
                    continue

                logging.info(f"=== Processing segment: {seg_name} (24/7) ===")
                run_segment_cycle(
                    ctx,
                    run_scan=do_scan,
                    run_exits=do_exits,
                    run_predictions=do_predictions,
                    run_snapshot=do_snapshot,
                    run_summary=do_snapshot,
                )
            ran_something = True

        # Update timestamps
        if ran_something:
            if do_scan:
                last_run["aggressive_scan"] = time.time()
            if do_exits:
                last_run["check_exits"] = time.time()
            if do_predictions:
                last_run["resolve_predictions"] = time.time()
            if do_snapshot:
                last_run["daily_snapshot"] = today_str

        if not market_open and not crypto_segments:
            # No crypto and market closed — sleep until next open
            if last_run["daily_snapshot"] != today_str and now.hour >= 16:
                logging.info("Market closed — sending missed daily snapshot")
                for seg_name in equity_segments:
                    if _shutdown:
                        break
                    try:
                        ctx = _build_ctx(seg_name)
                    except Exception:
                        logging.exception(f"Failed to build context for segment {seg_name!r}")
                        continue
                    run_segment_cycle(
                        ctx,
                        run_scan=False, run_exits=False,
                        run_predictions=False,
                        run_snapshot=True, run_summary=True,
                    )
                last_run["daily_snapshot"] = today_str

            nxt = next_market_open(now)
            logging.info(
                f"Market closed, sleeping until {nxt.strftime('%Y-%m-%d %H:%M %Z')}"
            )
            while not _shutdown:
                now = datetime.now(ET)
                if is_market_open(now):
                    break
                time.sleep(60)
        else:
            # Sleep 30 seconds between checks
            time.sleep(30)

    logging.info("QuantOpsAI multi-account scheduler stopped.")


# ── Entry Point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # Accept optional segment names as CLI arguments
    args = sys.argv[1:]
    if args:
        main_loop(active_segments=args)
    else:
        main_loop()
