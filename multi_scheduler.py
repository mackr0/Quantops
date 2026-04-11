#!/usr/bin/env python3
"""Multi-account scheduler — runs trading profiles via UserContext.

Each profile gets a UserContext that carries all credentials, DB paths, and risk
parameters through the entire call chain.  There is no _apply_segment_config /
_restore_config pattern.

The scheduler iterates all enabled trading profiles across all users.  Crypto
profiles (market_type == 'crypto') run 24/7; equity profiles run during market
hours only.

For backward compatibility during migration, the scheduler can still build a
UserContext from segments.py + config.py if the profile-based approach fails.

Usage:
    python multi_scheduler.py                  # run all active profiles
    python multi_scheduler.py --legacy         # run legacy segment mode
"""

import time
import logging
import signal
import sys
import os
import json as _json
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

def _build_ctx_from_profile(profile):
    """Build a UserContext from a trading profile dict."""
    from models import build_user_context_from_profile
    return build_user_context_from_profile(profile["id"])


def _build_ctx(segment_name):
    """Build a UserContext for a segment (legacy mode).

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
        # Cancel stale limit orders every exit-check cycle
        run_task(
            f"[{seg_label}] Cancel Stale Orders",
            lambda: _task_cancel_stale_orders(ctx),
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
        # Self-tuning runs once per day alongside the daily snapshot
        if getattr(ctx, "enable_self_tuning", True):
            run_task(
                f"[{seg_label}] Self-Tune",
                lambda: _task_self_tune(ctx),
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


# ── Activity Log Helpers ──────────────────────────────────────────────

def _safe_log_activity(profile_id, user_id, activity_type, title, detail,
                       symbol=None):
    """Log an activity entry, swallowing errors so it never breaks the scan."""
    try:
        from models import log_activity
        log_activity(profile_id, user_id, activity_type, title, detail,
                     symbol=symbol)
    except Exception:
        logging.exception("Failed to log activity entry")


def _build_scan_summary(ctx, candidates, summary):
    """Build a human-readable scan summary with indicator details.

    Returns (title, detail) strings for the activity log.
    """
    from market_data import get_bars, add_indicators

    seg_label = ctx.display_name or ctx.segment
    total = summary.get("total", len(candidates))
    buys = summary.get("buys", 0)
    sells = summary.get("sells", 0)
    shorts = summary.get("shorts", 0)
    holds = summary.get("holds", 0)
    ai_vetoed = summary.get("ai_vetoed", 0)

    # Determine market mood
    if buys > 0 and sells == 0 and shorts == 0:
        mood = "bullish signals"
    elif (sells > 0 or shorts > 0) and buys == 0:
        mood = "bearish signals"
    elif buys > 0 and (sells > 0 or shorts > 0):
        mood = "mixed signals"
    else:
        mood = "market flat"

    shorts_part = f", {shorts} shorts" if shorts > 0 else ""
    title = (f"{seg_label} Scan: {total} analyzed, {buys} buys, "
             f"{sells} sells{shorts_part} — {mood}")

    # Build a clean, structured detail summary
    top_symbols = list(candidates)[:5]
    asset_rows = []

    for sym in top_symbols:
        try:
            df = get_bars(sym, limit=30)
            if df.empty or len(df) < 5:
                continue
            df = df.copy()
            df = add_indicators(df)
            latest = df.iloc[-1]

            price = float(latest["close"])
            rsi = float(latest.get("rsi", 0) or 0)
            vol = float(latest.get("volume", 0) or 0)
            vol_avg = float(latest.get("volume_sma_20", 0) or 0)
            vol_ratio = vol / vol_avg if vol_avg > 0 else 0
            high_20d = float(df["high"].tail(20).max()) if len(df) >= 20 else float(df["high"].max())
            pct_from_high = ((price - high_20d) / high_20d * 100) if high_20d > 0 else 0

            # RSI condition label
            if rsi < 25:
                rsi_label = "Oversold"
            elif rsi < 40:
                rsi_label = "Weak"
            elif rsi < 60:
                rsi_label = "Neutral"
            elif rsi < 75:
                rsi_label = "Strong"
            else:
                rsi_label = "Overbought"

            # Volume label
            if vol_ratio >= 2.0:
                vol_label = "Surging"
            elif vol_ratio >= 1.0:
                vol_label = "Normal"
            else:
                vol_label = "Low"

            asset_rows.append({
                "sym": sym, "price": price, "rsi": rsi, "rsi_label": rsi_label,
                "vol_ratio": vol_ratio, "vol_label": vol_label,
                "pct_from_high": pct_from_high,
            })
        except Exception:
            continue

    # Build the detail text — clean structured format
    lines = []

    if asset_rows:
        lines.append("MARKET CONDITIONS")
        lines.append("-" * 40)
        for a in asset_rows:
            lines.append(f"{a['sym']}")
            lines.append(f"  Price: ${a['price']:,.2f}  |  RSI: {a['rsi']:.0f} ({a['rsi_label']})  |  Vol: {a['vol_ratio']:.1f}x ({a['vol_label']})")
            lines.append(f"  From 20d high: {a['pct_from_high']:+.1f}%")
            lines.append("")

    lines.append("SCAN RESULT")
    lines.append("-" * 40)
    if buys == 0 and sells == 0:
        lines.append("No trades executed — waiting for stronger signals.")
        reasons = []
        if asset_rows:
            avg_rsi = sum(a["rsi"] for a in asset_rows) / len(asset_rows)
            avg_vol = sum(a["vol_ratio"] for a in asset_rows) / len(asset_rows)
            if avg_rsi > 25:
                reasons.append(f"RSI range {min(a['rsi'] for a in asset_rows):.0f}-{max(a['rsi'] for a in asset_rows):.0f} (need <25 for mean reversion)")
            if avg_vol < 2.0:
                reasons.append(f"Volume {avg_vol:.1f}x avg (need 2x+ for volume spike)")
            if all(a["pct_from_high"] < -3 for a in asset_rows):
                reasons.append("All assets below 20-day highs (no breakouts)")
        if reasons:
            for r in reasons:
                lines.append(f"  • {r}")
        if ai_vetoed > 0:
            lines.append(f"  • {ai_vetoed} signal(s) vetoed by AI review")
    else:
        parts = []
        if buys > 0:
            parts.append(f"{buys} buy(s)")
        if sells > 0:
            parts.append(f"{sells} sell(s)")
        lines.append(f"Executed {', '.join(parts)}.")
        if ai_vetoed > 0:
            lines.append(f"  • {ai_vetoed} additional signal(s) vetoed by AI")

    detail = "\n".join(lines)
    return title, detail


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
    maga_mode = ctx.maga_mode if ctx is not None else False

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

    # MAGA Mode: also scan the full universe for deeply oversold stocks
    # These might not pass normal screener filters but are mean reversion candidates
    if maga_mode and not is_crypto:
        from market_data import get_bars, add_indicators
        universe = seg.get("universe", [])
        import yfinance as _yf
        logging.info(f"[{seg_label}] MAGA Mode: scanning for oversold opportunities...")
        try:
            yf_data = _yf.download(universe, period="1mo", progress=False,
                                   group_by="ticker", threads=True)
            for sym in universe:
                if sym in symbols:
                    continue
                try:
                    sym_df = yf_data[sym].dropna(subset=["Close"])
                    if len(sym_df) < 15:
                        continue
                    # Quick RSI calculation
                    close = sym_df["Close"]
                    delta = close.diff()
                    gain = delta.where(delta > 0, 0).rolling(14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs = gain / loss
                    rsi = 100 - (100 / (1 + rs))
                    latest_rsi = float(rsi.iloc[-1])
                    if latest_rsi < ctx.rsi_oversold:
                        symbols.add(sym)
                except Exception:
                    pass
        except Exception:
            logging.warning(f"[{seg_label}] MAGA oversold scan failed")
        logging.info(f"[{seg_label}] After MAGA oversold scan: {len(symbols)} total candidates")

    symbols = list(symbols)[:30]

    if not symbols:
        logging.info(f"[{seg_label}] No candidates found in screen.")
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "scan_summary",
            f"{seg_label} Scan: 0 candidates found",
            "No symbols passed the screener filters this cycle.",
        )
        return

    logging.info(f"[{seg_label}] Running aggressive scan on {len(symbols)} candidates")
    summary = run_aggressive_scan_and_trade(symbols, ctx=ctx)
    logging.info(
        f"[{seg_label}] Trade summary: "
        f"buys={summary.get('buys', 0)}, "
        f"sells={summary.get('sells', 0)}, "
        f"shorts={summary.get('shorts', 0)}, "
        f"ai_vetoed={summary.get('ai_vetoed', 0)}, "
        f"holds={summary.get('holds', 0)}, "
        f"pre_filtered={summary.get('pre_filtered', 0)}, "
        f"sent_to_ai={summary.get('sent_to_ai', '?')}, "
        f"errors={summary.get('errors', 0)}"
    )

    # Log scan summary activity
    try:
        scan_title, scan_detail = _build_scan_summary(ctx, symbols, summary)
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "scan_summary", scan_title, scan_detail,
        )
    except Exception:
        logging.exception("Failed to build scan summary for activity log")

    for detail in summary.get("details", []):
        if detail.get("action") in ("BUY", "SELL", "SHORT"):
            try:
                notify_trade(detail, detail, detail, ctx=ctx)
            except Exception:
                logging.exception("Failed to send trade notification")

            # Log trade executed activity
            sym = detail.get("symbol", "?")
            action = detail.get("action", "?")
            qty = detail.get("qty", 0)
            price = detail.get("price", 0)
            reason = detail.get("reason", "")
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "trade_executed",
                f"{action} {qty:,.0f} {sym} @ ${price:,.2f}" if qty and price
                else f"{action} {sym}",
                f"Trade executed: {action} {sym}\n{reason}",
                symbol=sym,
            )

    for veto in summary.get("vetoed_details", []):
        tech_signal = veto.get("technical_signal", "")
        sym = veto.get("symbol", "?")
        ai_conf = veto.get("ai_confidence", 0)
        ai_reasoning = veto.get("ai_reasoning", "")

        # Log AI veto activity
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "trade_vetoed",
            f"AI Vetoed {tech_signal} {sym} — confidence only {ai_conf:.0f}%"
            if ai_conf else f"AI Vetoed {tech_signal} {sym}",
            f"Technical signal: {tech_signal}\n"
            f"AI confidence: {ai_conf:.0f}%\n"
            f"Reasoning: {ai_reasoning}",
            symbol=sym,
        )

        if "BUY" in str(tech_signal):
            # Don't send veto emails for JSON parse failures — those are errors, not real vetoes
            if ai_conf == 0 and ("not valid JSON" in str(ai_reasoning) or "parse" in str(ai_reasoning).lower()):
                logging.warning(f"Skipping veto email for {sym} — AI response was a parse error")
            else:
                try:
                    notify_veto(
                        veto["symbol"],
                        {"signal": tech_signal, "score": veto.get("score", ""), "reason": veto.get("reason", "")},
                        {"signal": veto.get("ai_signal", ""), "confidence": ai_conf, "reasoning": ai_reasoning,
                         "risk_factors": veto.get("ai_risk_factors", [])},
                        ctx=ctx,
                    )
                except Exception:
                    logging.exception("Failed to send veto notification")


def _task_cancel_stale_orders(ctx):
    """Cancel limit orders older than 5 minutes that haven't been filled."""
    from client import get_api
    from datetime import datetime, timezone

    seg_label = ctx.display_name or ctx.segment

    if not getattr(ctx, "use_limit_orders", False):
        return

    try:
        api = get_api(ctx)
        open_orders = api.list_orders(status="open")
        now = datetime.now(timezone.utc)
        stale_cutoff = timedelta(minutes=5)
        cancelled = 0

        for order in open_orders:
            if order.type != "limit":
                continue
            # Parse order creation time
            created_at = order.created_at
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age = now - created_at
            if age > stale_cutoff:
                api.cancel_order(order.id)
                cancelled += 1
                logging.info(
                    f"[{seg_label}] Cancelled stale limit order {order.id} "
                    f"for {order.symbol} (age {age.total_seconds():.0f}s)"
                )

        if cancelled > 0:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "stale_order_cancel",
                f"Cancelled {cancelled} stale limit order(s)",
                f"Orders older than 5 minutes were cancelled",
            )
    except Exception:
        logging.exception(f"[{seg_label}] Failed to cancel stale orders")


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

            # Log exit activity
            sym = r["symbol"]
            trigger = r.get("trigger", "exit").capitalize()
            reason = r.get("reason", "")
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "exit_triggered",
                f"{trigger} {sym} — {reason[:60]}" if reason
                else f"{trigger} {sym}",
                f"Exit triggered for {sym}\n"
                f"Trigger: {trigger}\n"
                f"Qty: {r.get('qty', '?')}\n"
                f"Reason: {reason}",
                symbol=sym,
            )
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


def _task_self_tune(ctx):
    """Run self-tuning auto-adjustments based on AI prediction performance."""
    from self_tuning import apply_auto_adjustments

    adjustments = apply_auto_adjustments(ctx)
    if adjustments:
        seg_label = ctx.display_name or ctx.segment

        # Separate reviews from new adjustments for clearer logging
        reviews = [a for a in adjustments if a.startswith("Reviewed") or a.startswith("REVERSED")]
        new_adj = [a for a in adjustments if a not in reviews]

        for adj in adjustments:
            logging.info(f"[{seg_label}] Self-tune: {adj}")

        # Build a structured detail message
        detail_parts = []
        if reviews:
            detail_parts.append("PAST ADJUSTMENT REVIEWS:")
            detail_parts.extend(f"  - {r}" for r in reviews)
        if new_adj:
            if detail_parts:
                detail_parts.append("")
            detail_parts.append("NEW ADJUSTMENTS:")
            detail_parts.extend(f"  - {a}" for a in new_adj)
        if not detail_parts:
            detail_parts = [f"- {a}" for a in adjustments]

        title_parts = []
        if new_adj:
            title_parts.append(f"{len(new_adj)} new adjustment(s)")
        if reviews:
            title_parts.append(f"{len(reviews)} review(s)")
        title = f"Self-Tuning: {', '.join(title_parts)}"

        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "self_tune", title, "\n".join(detail_parts),
        )
    else:
        logging.info("Self-tuning: no adjustments needed.")


def _task_daily_summary_email(ctx):
    """Send end-of-day summary email."""
    from notifications import notify_daily_summary
    notify_daily_summary(ctx=ctx)
    logging.info("Daily summary email sent.")


# ── Profile-based Main Loop ──────────────────────────────────────────

def _load_active_profiles():
    """Load all enabled trading profiles from the database."""
    try:
        from models import get_active_profiles
        return get_active_profiles()
    except Exception:
        logging.exception("Failed to load active profiles from DB")
        return []


def main_loop(active_segments=None, legacy_mode=False):
    """Run the multi-account scheduling loop.

    Parameters
    ----------
    active_segments : list[str] or None
        Segment names to run (legacy mode only).  Defaults to all segments.
    legacy_mode : bool
        If True, use the old segment-based iteration instead of profiles.
    """
    global _shutdown

    if legacy_mode and active_segments is None:
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
    if legacy_mode:
        logging.info(f"Mode: LEGACY (segments: {active_segments})")
    else:
        logging.info("Mode: PROFILES (iterating all active trading profiles)")
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

        ran_something = False

        if legacy_mode:
            # ── Legacy segment-based iteration ───────────────────────
            equity_segments = [s for s in active_segments if s != "crypto"]
            crypto_segments = [s for s in active_segments if s == "crypto"]

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
                        run_scan=do_scan, run_exits=do_exits,
                        run_predictions=do_predictions,
                        run_snapshot=do_snapshot, run_summary=do_snapshot,
                    )
                ran_something = True

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
                        run_scan=do_scan, run_exits=do_exits,
                        run_predictions=do_predictions,
                        run_snapshot=do_snapshot, run_summary=do_snapshot,
                    )
                ran_something = True

            has_crypto = bool(crypto_segments)

        else:
            # ── Profile-based iteration ──────────────────────────────
            profiles = _load_active_profiles()

            # Check BEFORE timing logic if any profile has a non-market-hours schedule
            has_always_on = False
            for prof in profiles:
                stype = prof.get("schedule_type", "market_hours")
                if stype in ("24_7", "extended_hours", "custom"):
                    has_always_on = True
                    break
            has_crypto = has_always_on

            if do_scan or do_exits or do_predictions or do_snapshot:
                for prof in profiles:
                    if _shutdown:
                        break
                    try:
                        ctx = _build_ctx_from_profile(prof)
                    except Exception:
                        logging.exception(
                            f"Failed to build context for profile #{prof['id']} ({prof['name']})")
                        continue

                    if not ctx.is_within_schedule(now):
                        continue  # Skip this profile — not within its schedule

                    # Feature 7: Skip first N minutes after market open
                    if ctx.skip_first_minutes > 0 and now.weekday() < 5:
                        market_open_time = now.replace(
                            hour=9, minute=30, second=0, microsecond=0)
                        skip_until = market_open_time + timedelta(
                            minutes=ctx.skip_first_minutes)
                        if market_open_time <= now < skip_until:
                            logging.info(
                                f"Skipping profile {prof['name']} — within "
                                f"first {ctx.skip_first_minutes} minutes of "
                                f"market open (until {skip_until.strftime('%H:%M')} ET)")
                            continue

                    logging.info(f"=== Processing profile: {prof['name']} (#{prof['id']}, {prof['market_type']}, schedule={ctx.schedule_type}) ===")
                    run_segment_cycle(
                        ctx,
                        run_scan=do_scan, run_exits=do_exits,
                        run_predictions=do_predictions,
                        run_snapshot=do_snapshot, run_summary=do_snapshot,
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

            # Write status file for the web UI countdown timers
            try:
                status = {
                    "last_scan": last_run["aggressive_scan"],
                    "next_scan": last_run["aggressive_scan"] + INTERVAL_AGGRESSIVE_SCAN,
                    "last_exit_check": last_run["check_exits"],
                    "next_exit_check": last_run["check_exits"] + INTERVAL_CHECK_EXITS,
                    "last_ai_resolve": last_run["resolve_predictions"],
                    "next_ai_resolve": last_run["resolve_predictions"] + INTERVAL_RESOLVE_PREDICTIONS,
                    "scan_interval_min": INTERVAL_AGGRESSIVE_SCAN // 60,
                    "exit_interval_min": INTERVAL_CHECK_EXITS // 60,
                    "ai_interval_min": INTERVAL_RESOLVE_PREDICTIONS // 60,
                    "market_open": market_open,
                    "has_crypto": has_crypto if not legacy_mode else bool([s for s in (active_segments or []) if s == "crypto"]),
                    "updated_at": time.time(),
                }
                with open("scheduler_status.json", "w") as f:
                    _json.dump(status, f)
            except Exception:
                pass  # Never break the scheduler for a status file

        if not market_open and not has_crypto:
            # No crypto and market closed — sleep until next open
            if last_run["daily_snapshot"] != today_str and now.hour >= 16:
                logging.info("Market closed — sending missed daily snapshot")
                if legacy_mode:
                    items = [(s, lambda s=s: _build_ctx(s)) for s in (active_segments or []) if s != "crypto"]
                else:
                    profiles = _load_active_profiles()
                    items = [(p["name"], lambda p=p: _build_ctx_from_profile(p))
                             for p in profiles if p["market_type"] != "crypto"]

                for label, ctx_builder in items:
                    if _shutdown:
                        break
                    try:
                        ctx = ctx_builder()
                    except Exception:
                        logging.exception(f"Failed to build context for {label}")
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
    args = sys.argv[1:]
    if "--legacy" in args:
        args.remove("--legacy")
        main_loop(active_segments=args or None, legacy_mode=True)
    elif args:
        # If segment names are passed, assume legacy mode
        main_loop(active_segments=args, legacy_mode=True)
    else:
        # Default: profile-based mode
        main_loop()
