#!/usr/bin/env python3
"""
QuantOpsAI Autonomous Trading Scheduler

Runs 24/7 on a server, executing trading tasks during market hours:
  - Every 30 min: aggressive scan and trade
  - Every 15 min: check stop-loss / take-profit exits
  - Every 60 min: resolve AI predictions
  - At 3:55 PM ET: save daily portfolio snapshot

Outside market hours, sleeps until next market open.
"""

import time
import logging
import signal
import sys
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Timezone ─────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

# ── Graceful Shutdown ────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logging.info(f"Received signal {signum}, shutting down gracefully...")
    _shutdown = True


# ── Market Hours ─────────────────────────────────────────────────────

def is_market_open(now=None):
    """Return True if 9:30 AM - 4:00 PM ET, Monday-Friday."""
    now = now or datetime.now(ET)
    # Monday=0, Sunday=6
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now < market_close


def next_market_open(now=None):
    """Return datetime of next market open (9:30 AM ET), skipping weekends."""
    now = now or datetime.now(ET)
    # Start from tomorrow if market is closed today or already past open
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= candidate or now.weekday() >= 5:
        candidate += timedelta(days=1)
    # Skip weekends
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


# ── Task Implementations ─────────────────────────────────────────────

def task_aggressive_scan_and_trade():
    """Screen for small-cap candidates and auto-trade with AI review."""
    from screener import run_full_screen
    from aggressive_trader import run_aggressive_scan_and_trade
    from notifications import notify_trade, notify_veto

    screen = run_full_screen()
    symbols = set()
    for cat in ("volume_surges", "momentum", "breakouts", "candidates"):
        for s in screen.get(cat, []):
            symbols.add(s["symbol"])
    symbols = list(symbols)[:30]

    if not symbols:
        logging.info("No candidates found in screen.")
        return

    logging.info(f"Running aggressive scan on {len(symbols)} candidates")
    summary = run_aggressive_scan_and_trade(symbols)
    logging.info(
        f"Aggressive trade summary: "
        f"buys={summary.get('buys', 0)}, "
        f"sells={summary.get('sells', 0)}, "
        f"shorts={summary.get('shorts', 0)}, "
        f"ai_vetoed={summary.get('ai_vetoed', 0)}, "
        f"holds={summary.get('holds', 0)}, "
        f"errors={summary.get('errors', 0)}"
    )

    # Send email for each executed trade
    for detail in summary.get("details", []):
        if detail.get("action") in ("BUY", "SELL", "SHORT"):
            try:
                notify_trade(detail, detail, detail)
            except Exception:
                logging.exception("Failed to send trade notification")

    # Send email only for vetoed BUY signals (not sells on stocks we don't own)
    for veto in summary.get("vetoed_details", []):
        tech_signal = veto.get("technical_signal", "")
        if "BUY" in str(tech_signal):
            try:
                notify_veto(
                    veto["symbol"],
                    {"signal": tech_signal},
                    veto,
                )
            except Exception:
                logging.exception("Failed to send veto notification")


def task_check_exits():
    """Check stop-loss and take-profit triggers on open positions."""
    from trader import check_exits
    from notifications import notify_exit

    results = check_exits()
    if results:
        for r in results:
            logging.info(
                f"Exit triggered: {r['symbol']} {r['trigger'].upper()} "
                f"qty={r['qty']} — {r['reason']}"
            )
            try:
                notify_exit(r["symbol"], r["trigger"], r["qty"], r["reason"])
            except Exception:
                logging.exception("Failed to send exit notification")
    else:
        logging.info("No exit triggers fired.")


def task_resolve_predictions():
    """Resolve outstanding AI predictions against actual prices."""
    from ai_tracker import resolve_predictions

    resolve_predictions()
    logging.info("AI predictions resolved.")


def task_daily_summary_email():
    """Send end-of-day summary email."""
    from notifications import notify_daily_summary
    notify_daily_summary()
    logging.info("Daily summary email sent.")


def task_daily_snapshot():
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


# ── Main Loop ────────────────────────────────────────────────────────

def main_loop():
    global _shutdown

    # ── Logging setup ────────────────────────────────────────────────
    log_dir = os.path.expanduser("~/QuantOpsAI/logs")
    os.makedirs(log_dir, exist_ok=True)

    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"quantopsai_{today_str}.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )

    logging.info("=" * 60)
    logging.info("QuantOpsAI scheduler starting")
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
        "daily_snapshot": None,  # Track by date string, not timestamp
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
            new_log_file = os.path.join(log_dir, f"quantopsai_{today_str}.log")
            # Replace file handler
            root = logging.getLogger()
            for handler in root.handlers[:]:
                if isinstance(handler, logging.FileHandler):
                    root.removeHandler(handler)
                    handler.close()
            root.addHandler(logging.FileHandler(new_log_file))
            logging.info(f"Log rotated to {new_log_file}")

        if is_market_open(now):
            current_time = time.time()

            # Every 30 min: aggressive scan and trade
            if current_time - last_run["aggressive_scan"] >= INTERVAL_AGGRESSIVE_SCAN:
                run_task("Aggressive Scan & Trade", task_aggressive_scan_and_trade)
                last_run["aggressive_scan"] = time.time()

            # Every 15 min: check exits
            if current_time - last_run["check_exits"] >= INTERVAL_CHECK_EXITS:
                run_task("Check Exits", task_check_exits)
                last_run["check_exits"] = time.time()

            # Every 60 min: resolve AI predictions
            if current_time - last_run["resolve_predictions"] >= INTERVAL_RESOLVE_PREDICTIONS:
                run_task("Resolve AI Predictions", task_resolve_predictions)
                last_run["resolve_predictions"] = time.time()

            # At 3:55 PM ET: daily snapshot + summary email (once per day)
            if now.hour == 15 and now.minute >= 55 and last_run["daily_snapshot"] != today_str:
                run_task("Daily Snapshot", task_daily_snapshot)
                run_task("Daily Summary Email", task_daily_summary_email)
                last_run["daily_snapshot"] = today_str

            # Sleep 30 seconds between checks
            time.sleep(30)

        else:
            # Market closed
            next_open = next_market_open(now)
            logging.info(
                f"Market closed, sleeping until {next_open.strftime('%Y-%m-%d %H:%M %Z')}"
            )
            # Sleep in 60-second intervals so SIGINT can interrupt
            while not _shutdown:
                now = datetime.now(ET)
                if is_market_open(now):
                    break
                time.sleep(60)

    logging.info("QuantOpsAI scheduler stopped.")


if __name__ == "__main__":
    main_loop()
