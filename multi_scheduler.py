#!/usr/bin/env python3
"""Multi-account scheduler — runs smallcap, midcap, and largecap segments.

Each segment has its own Alpaca credentials, SQLite database, universe, and
risk parameters.  Segments are processed sequentially (single-threaded) within
each scheduling cycle.  The existing single-account scheduler.py is untouched.

Usage:
    python multi_scheduler.py                  # run all segments
    python multi_scheduler.py smallcap midcap  # run only selected segments
"""

import time
import logging
import signal
import sys
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from segments import get_segment, list_segments, SEGMENTS

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


# ── Config Override Context ──────────────────────────────────────────

def _apply_segment_config(segment_name):
    """Override module-level config values with the given segment's settings.

    Because the scheduler is single-threaded, we can safely mutate
    ``config.*`` before running each segment's tasks.  Returns a dict of
    the previous values so they *could* be restored, though in practice we
    just overwrite with the next segment's values.
    """
    seg = get_segment(segment_name)

    prev = {
        "ALPACA_API_KEY": config.ALPACA_API_KEY,
        "ALPACA_SECRET_KEY": config.ALPACA_SECRET_KEY,
        "DB_PATH": config.DB_PATH,
        "MAX_POSITION_PCT": config.MAX_POSITION_PCT,
        "DEFAULT_STOP_LOSS_PCT": config.DEFAULT_STOP_LOSS_PCT,
        "DEFAULT_TAKE_PROFIT_PCT": config.DEFAULT_TAKE_PROFIT_PCT,
        "AGGRESSIVE_MAX_POSITION_PCT": config.AGGRESSIVE_MAX_POSITION_PCT,
        "AGGRESSIVE_STOP_LOSS_PCT": config.AGGRESSIVE_STOP_LOSS_PCT,
        "AGGRESSIVE_TAKE_PROFIT_PCT": config.AGGRESSIVE_TAKE_PROFIT_PCT,
        "SCREEN_MIN_PRICE": config.SCREEN_MIN_PRICE,
        "SCREEN_MAX_PRICE": config.SCREEN_MAX_PRICE,
        "SCREEN_MIN_VOLUME": config.SCREEN_MIN_VOLUME,
    }

    config.ALPACA_API_KEY = seg["alpaca_key"]
    config.ALPACA_SECRET_KEY = seg["alpaca_secret"]
    config.DB_PATH = seg["db_path"]
    config.MAX_POSITION_PCT = seg["max_position_pct"]
    config.DEFAULT_STOP_LOSS_PCT = seg["stop_loss_pct"]
    config.DEFAULT_TAKE_PROFIT_PCT = seg["take_profit_pct"]
    config.AGGRESSIVE_MAX_POSITION_PCT = seg["max_position_pct"]
    config.AGGRESSIVE_STOP_LOSS_PCT = seg["stop_loss_pct"]
    config.AGGRESSIVE_TAKE_PROFIT_PCT = seg["take_profit_pct"]
    config.SCREEN_MIN_PRICE = seg["min_price"]
    config.SCREEN_MAX_PRICE = seg["max_price"]
    config.SCREEN_MIN_VOLUME = seg["min_volume"]

    return prev


def _restore_config(prev):
    """Restore config values from *prev* dict (returned by _apply_segment_config)."""
    for key, value in prev.items():
        setattr(config, key, value)


# ── Segment Cycle ────────────────────────────────────────────────────

def run_segment_cycle(segment_name, run_scan=True, run_exits=True,
                      run_predictions=False, run_snapshot=False,
                      run_summary=False):
    """Run one full cycle for a given segment.

    Temporarily overrides config.* with the segment's values, then executes
    the requested tasks.
    """
    seg = get_segment(segment_name)
    logging.info(f"--- [{seg['name'].upper()}] segment cycle start ---")

    prev = _apply_segment_config(segment_name)

    try:
        if run_scan:
            run_task(
                f"[{seg['name']}] Aggressive Scan & Trade",
                lambda: _task_aggressive_scan_and_trade(segment_name),
            )

        if run_exits:
            run_task(
                f"[{seg['name']}] Check Exits",
                lambda: _task_check_exits(segment_name),
            )

        if run_predictions:
            run_task(
                f"[{seg['name']}] Resolve AI Predictions",
                _task_resolve_predictions,
            )

        if run_snapshot:
            run_task(
                f"[{seg['name']}] Daily Snapshot",
                _task_daily_snapshot,
            )

        if run_summary:
            run_task(
                f"[{seg['name']}] Daily Summary Email",
                _task_daily_summary_email,
            )
    finally:
        _restore_config(prev)

    logging.info(f"--- [{seg['name'].upper()}] segment cycle end ---")


# ── Task Implementations ─────────────────────────────────────────────
# These mirror scheduler.py but respect the currently-active config
# values set by _apply_segment_config().

def _task_aggressive_scan_and_trade(segment_name):
    """Screen the segment's universe and auto-trade with AI review."""
    from screener import screen_by_price_range, find_volume_surges, \
        find_momentum_stocks, find_breakouts
    from aggressive_trader import run_aggressive_scan_and_trade
    from notifications import notify_trade, notify_veto

    seg = get_segment(segment_name)

    # Use segment-specific universe and price/volume filters
    candidates = screen_by_price_range(
        min_price=seg["min_price"],
        max_price=seg["max_price"],
        min_volume=seg["min_volume"],
        limit=50,
    )
    symbols = set()
    for c in candidates:
        symbols.add(c["symbol"])

    # Also run secondary screens on the candidate list
    sym_list = [c["symbol"] for c in candidates]
    for s in find_volume_surges(sym_list):
        symbols.add(s["symbol"])
    for s in find_momentum_stocks(sym_list):
        symbols.add(s["symbol"])
    for s in find_breakouts(sym_list):
        symbols.add(s["symbol"])

    symbols = list(symbols)[:30]

    if not symbols:
        logging.info(f"[{seg['name']}] No candidates found in screen.")
        return

    logging.info(f"[{seg['name']}] Running aggressive scan on {len(symbols)} candidates")
    summary = run_aggressive_scan_and_trade(symbols)
    logging.info(
        f"[{seg['name']}] Trade summary: "
        f"buys={summary.get('buys', 0)}, "
        f"sells={summary.get('sells', 0)}, "
        f"ai_vetoed={summary.get('ai_vetoed', 0)}, "
        f"holds={summary.get('holds', 0)}, "
        f"errors={summary.get('errors', 0)}"
    )

    for detail in summary.get("details", []):
        if detail.get("action") in ("BUY", "SELL"):
            try:
                notify_trade(detail, detail, detail)
            except Exception:
                logging.exception("Failed to send trade notification")

    for veto in summary.get("vetoed_details", []):
        tech_signal = veto.get("technical_signal", "")
        if "BUY" in str(tech_signal):
            try:
                notify_veto(veto["symbol"], {"signal": tech_signal}, veto)
            except Exception:
                logging.exception("Failed to send veto notification")


def _task_check_exits(segment_name):
    """Check stop-loss and take-profit triggers on open positions."""
    from trader import check_exits
    from notifications import notify_exit

    seg = get_segment(segment_name)
    results = check_exits()
    if results:
        for r in results:
            logging.info(
                f"[{seg['name']}] Exit triggered: {r['symbol']} "
                f"{r['trigger'].upper()} qty={r['qty']} — {r['reason']}"
            )
            try:
                notify_exit(r["symbol"], r["trigger"], r["qty"], r["reason"])
            except Exception:
                logging.exception("Failed to send exit notification")
    else:
        logging.info(f"[{seg['name']}] No exit triggers fired.")


def _task_resolve_predictions():
    """Resolve outstanding AI predictions against actual prices."""
    from ai_tracker import resolve_predictions
    resolve_predictions()
    logging.info("AI predictions resolved.")


def _task_daily_snapshot():
    """Save end-of-day portfolio snapshot."""
    from journal import init_db, log_daily_snapshot
    from client import get_account_info, get_positions

    init_db()
    account = get_account_info()
    positions = get_positions()
    log_daily_snapshot(
        equity=account["equity"],
        cash=account["cash"],
        portfolio_value=account["portfolio_value"],
        num_positions=len(positions),
    )
    logging.info(
        f"Daily snapshot saved: equity=${account['equity']:,.2f}, "
        f"positions={len(positions)}, cash=${account['cash']:,.2f}"
    )


def _task_daily_summary_email():
    """Send end-of-day summary email."""
    from notifications import notify_daily_summary
    notify_daily_summary()
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

    # Validate segment names
    for name in active_segments:
        seg = get_segment(name)
        if not seg["alpaca_key"] or not seg["alpaca_secret"]:
            logging.warning(
                f"Segment {name!r} has no Alpaca credentials — "
                f"set {name.upper()}_ALPACA_KEY and {name.upper()}_ALPACA_SECRET"
            )

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

        if is_market_open(now):
            current_time = time.time()

            do_scan = (current_time - last_run["aggressive_scan"]
                       >= INTERVAL_AGGRESSIVE_SCAN)
            do_exits = (current_time - last_run["check_exits"]
                        >= INTERVAL_CHECK_EXITS)
            do_predictions = (current_time - last_run["resolve_predictions"]
                              >= INTERVAL_RESOLVE_PREDICTIONS)
            do_snapshot = (now.hour == 15 and now.minute >= 55
                           and last_run["daily_snapshot"] != today_str)

            if do_scan or do_exits or do_predictions or do_snapshot:
                for segment_name in active_segments:
                    if _shutdown:
                        break

                    logging.info(f"=== Processing segment: {segment_name} ===")
                    run_segment_cycle(
                        segment_name,
                        run_scan=do_scan,
                        run_exits=do_exits,
                        run_predictions=do_predictions,
                        run_snapshot=do_snapshot,
                        run_summary=do_snapshot,
                    )

                # Update last-run timestamps after iterating all segments
                if do_scan:
                    last_run["aggressive_scan"] = time.time()
                if do_exits:
                    last_run["check_exits"] = time.time()
                if do_predictions:
                    last_run["resolve_predictions"] = time.time()
                if do_snapshot:
                    last_run["daily_snapshot"] = today_str

            # Sleep 30 seconds between checks
            time.sleep(30)

        else:
            # Market closed — but send daily summary if we missed it
            if last_run["daily_snapshot"] != today_str and now.hour >= 16:
                logging.info("Market closed — sending missed daily snapshot and summary")
                for segment_name in active_segments:
                    if _shutdown:
                        break
                    logging.info(f"=== End-of-day snapshot: {segment_name} ===")
                    run_segment_cycle(
                        segment_name,
                        run_scan=False,
                        run_exits=False,
                        run_predictions=False,
                        run_snapshot=True,
                        run_summary=True,
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

    logging.info("QuantOpsAI multi-account scheduler stopped.")


# ── Entry Point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # Accept optional segment names as CLI arguments
    args = sys.argv[1:]
    if args:
        main_loop(active_segments=args)
    else:
        main_loop()
