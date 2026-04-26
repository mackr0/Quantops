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
from typing import Dict
from zoneinfo import ZoneInfo

# Load .env BEFORE any module that reads env vars (e.g. market_data uses
# ALPACA_API_KEY for the shared data client). Without this, the scheduler
# process had no env vars → Alpaca data API returned 401 → fell back to
# unreliable yfinance for price resolution, causing 0 resolutions on
# many profiles.
from dotenv import load_dotenv
load_dotenv()

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

def run_task(name, func, db_path=None):
    """Run *func* with logging, timing, error handling, and run tracking.

    If db_path is provided, records start/end in the per-profile
    task_runs table so the watchdog can detect stalled runs.
    """
    logging.info(f"[TASK START] {name}")
    start = time.time()

    tracker = None
    if db_path:
        try:
            from task_watchdog import track_run
            tracker = track_run(db_path, name)
            tracker.__enter__()
        except Exception:
            tracker = None

    try:
        func()
        elapsed = time.time() - start
        logging.info(f"[TASK DONE]  {name} ({elapsed:.1f}s)")
        if tracker:
            try:
                tracker.__exit__(None, None, None)
            except Exception:
                pass
    except Exception as exc:
        elapsed = time.time() - start
        logging.exception(f"[TASK FAIL]  {name} ({elapsed:.1f}s)")
        if tracker:
            try:
                tracker.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass


# ── Segment Cycle ────────────────────────────────────────────────────

def run_segment_cycle(ctx, run_scan=True, run_exits=True,
                      run_predictions=False, run_snapshot=False,
                      run_summary=False):
    """Run one full cycle for a given UserContext.

    All task functions receive ctx — no config.* globals are mutated.
    """
    # Ensure per-profile DB tables exist before any task touches them
    from journal import init_db
    init_db(ctx.db_path)

    seg_label = ctx.display_name or ctx.segment
    logging.info(f"--- [{seg_label.upper()}] segment cycle start ---")

    # CRITICAL ORDERING: exits BEFORE scan.
    # Exits are cheap (~1 sec per profile) and protect realized P&L.
    # Scans can take 5-30 minutes and sometimes hang (yfinance timeouts,
    # Alpaca rate limits, hung API calls). If the scan hangs BEFORE
    # exits run, held positions pass their take-profit / stop-loss
    # thresholds without firing — realized P&L evaporates. Running
    # exits first guarantees they can't be blocked by a downstream
    # failure in the scan pipeline.
    if run_exits:
        run_task(
            f"[{seg_label}] Check Exits",
            lambda: _task_check_exits(ctx),
            db_path=ctx.db_path,
        )
        # Cancel stale limit orders every exit-check cycle
        run_task(
            f"[{seg_label}] Cancel Stale Orders",
            lambda: _task_cancel_stale_orders(ctx),
            db_path=ctx.db_path,
        )
        # Update fill prices from Alpaca for slippage tracking
        run_task(
            f"[{seg_label}] Update Fill Prices",
            lambda: _task_update_fills(ctx),
            db_path=ctx.db_path,
        )
        # Reconcile trade statuses — mark BUY rows closed when their
        # positions go flat, and fix SELL rows whose status never
        # flipped from 'open' to 'closed'.
        run_task(
            f"[{seg_label}] Reconcile Trade Statuses",
            lambda: _task_reconcile_trade_statuses(ctx),
            db_path=ctx.db_path,
        )
        if getattr(ctx, "is_virtual", False):
            run_task(
                f"[{seg_label}] Virtual Audit",
                lambda: _task_virtual_audit(ctx),
                db_path=ctx.db_path,
            )

    if run_scan:
        run_task(
            f"[{seg_label}] Scan & Trade",
            lambda: _task_scan_and_trade(ctx),
            db_path=ctx.db_path,
        )
        # Crisis monitoring (Phase 10) — BEFORE event tick so the event
        # bus picks up crisis_state_change transitions in the same cycle
        run_task(
            f"[{seg_label}] Crisis Monitor",
            lambda: _task_crisis_monitor(ctx),
            db_path=ctx.db_path,
        )
        # Event bus tick (Phase 9) — detect new events, dispatch pending
        run_task(
            f"[{seg_label}] Event Bus Tick",
            lambda: _task_event_tick(ctx),
            db_path=ctx.db_path,
        )
        # Run watchdog — detect any task_runs rows stuck in 'running'
        # state for > 30 minutes and alert. Cheap, idempotent.
        run_task(
            f"[{seg_label}] Run Watchdog",
            lambda: _task_run_watchdog(ctx),
            db_path=ctx.db_path,
        )

    if run_predictions:
        run_task(
            f"[{seg_label}] Resolve AI Predictions",
            lambda: _task_resolve_predictions(ctx),
            db_path=ctx.db_path,
        )

    if run_snapshot:
        run_task(
            f"[{seg_label}] Daily Snapshot",
            lambda: _task_daily_snapshot(ctx),
            db_path=ctx.db_path,
        )
        # API cost check — alert if daily spend is getting high
        run_task(
            f"[{seg_label}] Cost Check",
            lambda: _task_cost_check(ctx),
            db_path=ctx.db_path,
        )
        # Cross-account reconciliation (virtual profiles only)
        if getattr(ctx, "is_virtual", False):
            run_task(
                f"[{seg_label}] Cross-Account Reconcile",
                lambda: _task_cross_account_reconcile(ctx),
                db_path=ctx.db_path,
            )
        # Self-tuning runs once per day alongside the daily snapshot
        if getattr(ctx, "enable_self_tuning", True):
            run_task(
                f"[{seg_label}] Self-Tune",
                lambda: _task_self_tune(ctx),
            db_path=ctx.db_path,
            )
        # Meta-model retraining (Phase 1) — daily at snapshot time
        run_task(
            f"[{seg_label}] Meta-Model Retrain",
            lambda: _task_retrain_meta_model(ctx),
            db_path=ctx.db_path,
        )
        # Alpha decay monitoring (Phase 3) — snapshot + detect + deprecate
        run_task(
            f"[{seg_label}] Alpha Decay Monitor",
            lambda: _task_alpha_decay(ctx),
            db_path=ctx.db_path,
        )
        # SEC filing analysis (Phase 4) — runs once per market_type per
        # cycle, not per profile. The same symbols get the same filings.
        _sec_key = ctx.segment
        if _sec_key not in _sec_checked_this_cycle:
            _sec_checked_this_cycle.add(_sec_key)
            run_task(
                f"[{seg_label}] SEC Filing Monitor",
                lambda: _task_sec_filings(ctx),
                db_path=ctx.db_path,
            )
        # Auto-strategy lifecycle (Phase 7) — promote matured shadows, retire failed
        run_task(
            f"[{seg_label}] Auto-Strategy Lifecycle",
            lambda: _task_auto_strategy_lifecycle(ctx),
            db_path=ctx.db_path,
        )
        # Daily DB backup with rotation (proprietary training data)
        run_task(
            f"[{seg_label}] DB Backup",
            lambda: _task_db_backup(ctx),
            db_path=ctx.db_path,
        )
        # Weekly proposal generation runs on Sundays; on other days this task
        # is a near-immediate no-op.
        run_task(
            f"[{seg_label}] Auto-Strategy Generation",
            lambda: _task_auto_strategy_generation(ctx),
            db_path=ctx.db_path,
        )
        # Weekly AI-work digest — single email across all profiles, fires
        # once per week on Friday evenings. Cheap no-op on other days; the
        # file-based idempotency marker means only the first profile to
        # reach this task on Friday actually sends the email.
        run_task(
            f"[{seg_label}] Weekly AI Digest",
            lambda: _task_weekly_digest(),
            db_path=ctx.db_path,
        )
        # Weekly capital rebalance — Sundays only, file-based idempotency
        # marker prevents re-firing on restart. Iterates users with the
        # auto_capital_allocation toggle ON; respects the per-Alpaca-account
        # group constraint so shared accounts aren't over-committed.
        run_task(
            f"[{seg_label}] Capital Rebalance",
            lambda: _task_capital_rebalance(ctx),
            db_path=ctx.db_path,
        )

    if run_summary:
        run_task(
            f"[{seg_label}] Daily Summary Email",
            lambda: _task_daily_summary_email(ctx),
            db_path=ctx.db_path,
        )

    logging.info(f"--- [{seg_label.upper()}] segment cycle end ---")


# ── Helpers ─────────────────────────────────────────────────────────

def run_full_screen_for_segment(ctx, seg):
    """Run the standard equity screener with ctx-specific parameters.

    Uses dynamic universe discovery first, falls back to hardcoded lists.
    """
    from screener import screen_by_price_range, find_volume_surges, \
        find_momentum_stocks, find_breakouts, screen_dynamic_universe

    hardcoded_universe = seg.get("universe")

    # Try dynamic universe first (cached 24h), fall back to hardcoded
    try:
        universe = screen_dynamic_universe(
            min_price=ctx.min_price,
            max_price=ctx.max_price,
            min_volume=ctx.min_volume,
            market_type=ctx.segment,
            fallback_universe=hardcoded_universe,
            ctx=ctx,
        )
    except Exception:
        universe = hardcoded_universe

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

# Screener cache: keyed by market_type, expires every cycle. Profiles
# with the same market_type share one screener run instead of each
# running independently. Saves ~70% of non-AI calls.
_screener_cache = {}
_screener_cache_cycle = 0
_sec_checked_this_cycle = set()


def _get_screener_cache_key(market_type):
    return market_type


def _get_shared_candidates(ctx, seg, is_crypto):
    """Return screener + MAGA candidates, cached per market_type per cycle."""
    global _screener_cache, _screener_cache_cycle

    # Expire cache every cycle (roughly every 15 minutes)
    import time as _time
    now_bucket = int(_time.time() / 1800)  # 30-minute cache
    if now_bucket != _screener_cache_cycle:
        _screener_cache = {}
        _screener_cache_cycle = now_bucket
        _sec_checked_this_cycle.clear()

    cache_key = _get_screener_cache_key(ctx.segment)
    if cache_key in _screener_cache:
        logging.info(f"[{ctx.display_name}] Using shared screener results for {ctx.segment}")
        return list(_screener_cache[cache_key])

    from screener import run_crypto_screen

    if is_crypto:
        screen_results = run_crypto_screen(universe=seg.get("universe"))
    else:
        screen_results = run_full_screen_for_segment(ctx, seg)

    symbols = set()
    for cat in ("candidates", "volume_surges", "momentum", "breakouts"):
        for s in screen_results.get(cat, []):
            symbols.add(s["symbol"])

    # MAGA Mode oversold scan — also shared
    maga_mode = ctx.maga_mode if ctx is not None else False
    if maga_mode and not is_crypto:
        from market_data import get_bars, add_indicators
        from screener import get_active_alpaca_symbols
        raw_universe = seg.get("universe", [])
        # Filter against Alpaca's active-asset list — skips renamed (SQ→XYZ,
        # PARA→PSKY, GPS→GAP) and delisted names (CFLT/X/AZUL/etc.) that
        # still live in segments.py hardcoded lists. Without this filter,
        # each dead ticker triggers a yfinance "possibly delisted" error
        # (log noise only — the scan already skips empty-bar symbols — but
        # 170+ errors/day makes the journal unreadable). Fail-open: if
        # Alpaca is unreachable and the active-set is empty, use the full
        # raw universe (current behavior preserved).
        active_set = get_active_alpaca_symbols(ctx)
        if active_set:
            universe = [s for s in raw_universe if s in active_set]
            skipped = len(raw_universe) - len(universe)
            if skipped:
                logging.debug(
                    f"[{ctx.display_name}] MAGA universe: {len(raw_universe)} hardcoded "
                    f"→ {len(universe)} Alpaca-active ({skipped} dead tickers filtered)"
                )
        else:
            universe = raw_universe
        logging.info(f"[{ctx.display_name}] MAGA Mode: scanning for oversold opportunities...")
        maga_added = 0
        for sym in universe:
            if sym in symbols:
                continue
            try:
                bars = get_bars(sym, limit=30)
                if bars is None or bars.empty or len(bars) < 15:
                    continue
                bars = add_indicators(bars)
                if "rsi" not in bars.columns:
                    continue
                latest_rsi = float(bars.iloc[-1]["rsi"])
                if latest_rsi < ctx.rsi_oversold:
                    symbols.add(sym)
                    maga_added += 1
            except Exception:
                continue
        logging.info(f"[{ctx.display_name}] MAGA oversold scan: added {maga_added}, {len(symbols)} total")

    result = list(symbols)[:30]
    _screener_cache[cache_key] = result
    return list(result)


def _task_scan_and_trade(ctx):
    """Screen the segment's universe and auto-trade via the AI-first pipeline."""
    from trade_pipeline import run_trade_cycle
    from notifications import notify_trade, notify_veto
    from scan_status import update_status, clear_status

    seg_label = ctx.display_name or ctx.segment
    seg = get_segment(ctx.segment)
    is_crypto = seg.get("is_crypto", False)
    _pid = getattr(ctx, "profile_id", 0)

    update_status(_pid, "Screening universe", seg_label)

    symbols = _get_shared_candidates(ctx, seg, is_crypto)

    update_status(_pid, "Screener done", "%d candidates found" % len(symbols))

    if not symbols:
        clear_status(_pid)
        logging.info(f"[{seg_label}] No candidates found in screen.")
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "scan_summary",
            f"{seg_label} Scan: 0 candidates found",
            "No symbols passed the screener filters this cycle.",
        )
        return

    update_status(_pid, "Running trade pipeline", "%d candidates" % len(symbols))
    logging.info(f"[{seg_label}] Running scan on {len(symbols)} candidates")
    summary = run_trade_cycle(symbols, ctx=ctx)
    clear_status(_pid)
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


def _task_update_fills(ctx):
    """Update fill prices on recent trades from Alpaca order data."""
    import sqlite3
    from client import get_api

    seg_label = ctx.display_name or ctx.segment
    db_path = ctx.db_path
    api = get_api(ctx)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Find trades with no fill_price that have an order_id
        unfilled = conn.execute(
            "SELECT id, order_id, decision_price FROM trades "
            "WHERE fill_price IS NULL AND order_id IS NOT NULL "
            "AND decision_price IS NOT NULL"
        ).fetchall()

        if not unfilled:
            conn.close()
            return

        updated = 0
        for trade in unfilled:
            try:
                order = api.get_order(trade["order_id"])
                if order.filled_avg_price:
                    fill = float(order.filled_avg_price)
                    dec = trade["decision_price"] or 0
                    slip = ((fill - dec) / dec * 100) if dec > 0 else 0
                    conn.execute(
                        "UPDATE trades SET fill_price = ?, slippage_pct = ? WHERE id = ?",
                        (fill, round(slip, 4), trade["id"]),
                    )
                    updated += 1
            except Exception:
                pass  # Order may not exist yet or API error

        conn.commit()
        conn.close()

        if updated > 0:
            logging.info(f"[{seg_label}] Updated fill prices on {updated} trade(s)")
    except Exception:
        logging.exception(f"[{seg_label}] Failed to update fill prices")


def _task_virtual_audit(ctx):
    """Run data integrity checks on a virtual profile every exit cycle."""
    from virtual_audit import audit_virtual_profile
    seg_label = ctx.display_name or ctx.segment
    try:
        problems = audit_virtual_profile(
            db_path=ctx.db_path,
            initial_capital=getattr(ctx, "initial_capital", 100000.0),
            profile_name=seg_label,
        )
        if problems:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "virtual_audit",
                f"Data Integrity Warning: {len(problems)} issue(s)",
                "\n".join(f"- {p}" for p in problems),
            )
    except Exception:
        logging.exception(f"[{seg_label}] Virtual audit failed")


_DAILY_COST_ALERT_THRESHOLD = 3.00  # USD — alert if daily spend exceeds this
_cost_alerted_today = set()
_cross_reconcile_checked = set()


def _task_cross_account_reconcile(ctx):
    """Compare sum of virtual positions against Alpaca's actual holdings.
    Runs once per Alpaca account per snapshot cycle."""
    acct_id = getattr(ctx, "alpaca_account_id", None)
    if not acct_id or acct_id in _cross_reconcile_checked:
        return
    _cross_reconcile_checked.add(acct_id)
    try:
        from virtual_audit import audit_cross_account
        from models import get_user_profiles
        profiles = get_user_profiles(ctx.user_id)
        pids = [p["id"] for p in profiles
                if p.get("enabled") and p.get("alpaca_account_id") == acct_id]
        if len(pids) < 2:
            return
        problems = audit_cross_account(acct_id, pids)
        if problems:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "cross_reconcile",
                "Cross-Account Drift: %d issue(s)" % len(problems),
                "\n".join("- %s" % p for p in problems),
            )
    except Exception as exc:
        logging.warning("Cross-account reconcile failed: %s", exc)


def _task_cost_check(ctx):
    """Check if daily AI spend is exceeding threshold."""
    from ai_cost_ledger import spend_summary
    pid = getattr(ctx, "profile_id", 0)
    if pid in _cost_alerted_today:
        return
    try:
        summary = spend_summary(ctx.db_path)
        today_cost = summary["today"]["usd"]
        if today_cost > _DAILY_COST_ALERT_THRESHOLD / 10:
            # Sum across ALL profiles for total daily cost
            import os, glob
            total = 0
            for f in glob.glob("quantopsai_profile_*.db"):
                s = spend_summary(f)
                total += s["today"]["usd"]
            if total > _DAILY_COST_ALERT_THRESHOLD:
                _cost_alerted_today.add(pid)
                logging.warning(
                    "API cost alert: $%.2f today (threshold $%.2f)",
                    total, _DAILY_COST_ALERT_THRESHOLD)
                _safe_log_activity(
                    pid, ctx.user_id, "cost_alert",
                    "API Cost Alert: $%.2f today" % total,
                    "Daily AI spend has exceeded the $%.2f threshold. "
                    "Consider reducing scan frequency or disabling "
                    "specialist ensemble on test profiles." % _DAILY_COST_ALERT_THRESHOLD,
                )
    except Exception:
        pass


def _task_reconcile_trade_statuses(ctx):
    """Periodically reconcile trades.status.

    For virtual profiles: the internal ledger IS the source of truth,
    so we derive open_symbols from get_virtual_positions() instead of
    asking Alpaca (which holds a combined view of all profiles sharing
    the same account).

    For non-virtual profiles: Alpaca is the source of truth (original
    behavior).
    """
    from journal import reconcile_trade_statuses
    seg_label = ctx.display_name or ctx.segment
    try:
        if getattr(ctx, "is_virtual", False):
            from journal import get_virtual_positions
            virtual_pos = get_virtual_positions(db_path=ctx.db_path)
            open_symbols = {p["symbol"] for p in virtual_pos if p["qty"] > 0}
        else:
            from client import get_api
            api = get_api(ctx)
            positions = api.list_positions()
            open_symbols = {p.symbol for p in positions}
        result = reconcile_trade_statuses(
            db_path=ctx.db_path, open_symbols=open_symbols,
        )
        total = result["sells_fixed"] + result["buys_fixed"]
        if total > 0:
            logging.info(
                f"[{seg_label}] Reconciled trade statuses: "
                f"{result['sells_fixed']} sells, {result['buys_fixed']} buys"
            )
    except Exception:
        logging.exception(f"[{seg_label}] Reconcile trade statuses failed")


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
            from display_names import display_name as _dn
            trigger = _dn(r.get("trigger", "exit"))
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
            # Add to cooldown list so the next scan doesn't immediately
            # re-enter the same symbol (the ASTS churn bug).
            try:
                from journal import record_exit
                record_exit(
                    ctx.db_path, sym,
                    trigger=r.get("trigger", "exit"),
                    exit_price=r.get("exit_price", 0) or 0,
                )
            except Exception as exc:
                logging.debug(f"record_exit failed: {exc}")
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
    """Save end-of-day portfolio snapshot.

    Computes daily_pnl as (today's equity - previous snapshot's equity) so
    the daily_pnl column is actually populated. Previously it was always
    NULL, which broke the equity-delta curve in metrics.
    """
    from journal import init_db, log_daily_snapshot
    from client import get_account_info, get_positions
    import sqlite3 as _sqlite3

    init_db(ctx.db_path)
    account = get_account_info(ctx=ctx)
    positions = get_positions(ctx=ctx)
    equity = account["equity"]

    # Find the most recent prior snapshot to compute a real daily_pnl.
    # Use Python's local `date.today()` to match what log_daily_snapshot
    # writes — SQLite's `date('now')` is UTC and would disagree across
    # midnight UTC, causing the task to either skip its delta calc or
    # double-count a same-day write.
    from datetime import date as _date
    today_str = _date.today().isoformat()
    prior_equity = None
    try:
        conn = _sqlite3.connect(ctx.db_path)
        row = conn.execute(
            "SELECT equity FROM daily_snapshots "
            "WHERE date < ? ORDER BY date DESC LIMIT 1",
            (today_str,),
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            prior_equity = float(row[0])
    except Exception:
        prior_equity = None

    daily_pnl = None
    if prior_equity is not None:
        daily_pnl = round(equity - prior_equity, 2)

    log_daily_snapshot(
        equity=equity,
        cash=account["cash"],
        portfolio_value=account["portfolio_value"],
        num_positions=len(positions),
        daily_pnl=daily_pnl,
        db_path=ctx.db_path,
    )
    logging.info(
        f"Daily snapshot saved: equity=${equity:,.2f}, "
        f"positions={len(positions)}, cash=${account['cash']:,.2f}, "
        f"daily_pnl={'$%.2f' % daily_pnl if daily_pnl is not None else 'N/A'}"
    )


def _task_self_tune(ctx):
    """Run self-tuning auto-adjustments based on AI prediction performance.

    Always logs an activity entry with the outcome — even when nothing
    changed. Without this, the tuner appears dormant to the user even
    though it's running daily and evaluating.
    """
    from self_tuning import apply_auto_adjustments, describe_tuning_state

    seg_label = ctx.display_name or ctx.segment
    state = describe_tuning_state(ctx)
    adjustments = apply_auto_adjustments(ctx)

    reviews = [a for a in adjustments if a.startswith("Reviewed") or a.startswith("REVERSED")]
    recommendations = [a for a in adjustments if a.startswith("Recommendation:")]
    applied = [a for a in adjustments if a not in reviews and a not in recommendations]
    # `real_changes` must be defined unconditionally — the no-changes-needed
    # log path (~30 lines below) references it. Initialize here so when the
    # if/else branches don't set it, the reference still resolves.
    real_changes = applied

    if adjustments:
        for adj in adjustments:
            logging.info(f"[{seg_label}] Self-tune: {adj}")

        detail_parts = []
        if reviews:
            detail_parts.append("PAST ADJUSTMENT REVIEWS:")
            detail_parts.extend(f"  - {r}" for r in reviews)
        if applied:
            if detail_parts:
                detail_parts.append("")
            detail_parts.append("APPLIED:")
            detail_parts.extend(f"  - {a}" for a in applied)
        if recommendations:
            if detail_parts:
                detail_parts.append("")
            detail_parts.append("RECOMMENDATIONS (require human review):")
            detail_parts.extend(f"  - {r}" for r in recommendations)
        if not detail_parts:
            detail_parts = [f"- {a}" for a in adjustments]

        title_parts = []
        if applied:
            title_parts.append(f"{len(applied)} applied")
        if recommendations:
            title_parts.append(f"{len(recommendations)} recommended")
        if reviews:
            title_parts.append(f"{len(reviews)} review(s)")
        title = f"Self-Tuning: {', '.join(title_parts)}" if title_parts else "Self-Tuning: evaluated"

        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "self_tune", title, "\n".join(detail_parts),
        )
    else:
        logging.info(f"[{seg_label}] Self-tune: no adjustments needed — {state['message']}")
        if state.get("can_tune"):
            title = "Self-Tuning: evaluated, no changes needed"
        else:
            title = "Self-Tuning: waiting for data"
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "self_tune", title, state["message"],
        )

    # Always log to tuning_history when the tuner can evaluate — whether
    # changes were made or not. This ensures every profile appears in the
    # Self-Tuning History table on every run.
    if state.get("can_tune") and not real_changes:
        try:
            from self_tuning import _get_conn, _get_current_win_rate
            _c = _get_conn(ctx.db_path)
            wr, n_resolved = _get_current_win_rate(_c)
            _c.close()
            from models import log_tuning_change, _get_conn as _get_main_conn
            summary = f"Evaluated {state['resolved']} predictions, win rate {wr:.0f}% — no changes needed"
            row_id = log_tuning_change(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "evaluation", "none",
                "-", "-", summary,
                win_rate_at_change=wr,
                predictions_resolved=n_resolved,
            )
            mc = _get_main_conn()
            mc.execute(
                "UPDATE tuning_history SET outcome_after='n/a' WHERE id=?",
                (row_id,),
            )
            mc.commit()
            mc.close()
        except Exception:
            pass


def _task_retrain_meta_model(ctx):
    """Retrain the meta-model on accumulated resolved predictions.

    Phase 1 of the Quant Fund Evolution roadmap. See ROADMAP.md.

    Needs >=100 resolved predictions with features_json. If insufficient data,
    simply logs and exits (no error). Saves pickle to meta_model_{id}.pkl.
    """
    try:
        import meta_model
        profile_id = getattr(ctx, "profile_id", 0)
        seg_label = ctx.display_name or ctx.segment
        bundle = meta_model.train_and_save(profile_id, ctx.db_path)
        if bundle is None:
            logging.info(f"[{seg_label}] Meta-model: insufficient training data yet")
            return

        from display_names import display_name as _dn
        metrics = bundle["metrics"]
        top_features = bundle["feature_importance"][:5]
        top_str = ", ".join(f"{_dn(n)} ({i:.3f})" for n, i in top_features)
        logging.info(f"[{seg_label}] Meta-model retrained: "
                     f"AUC={metrics['auc']:.3f}, acc={metrics['accuracy']:.3f}, "
                     f"n={metrics['n_samples']}, top features: {top_str}")

        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "meta_model",
            f"Meta-Model Retrained: AUC {metrics['auc']:.3f}",
            f"Trained on {metrics['n_samples']} predictions. "
            f"Accuracy {metrics['accuracy']:.1%}. "
            f"Top features: {top_str}",
        )
    except Exception as exc:
        logging.warning(f"Meta-model retrain failed: {exc}")


def _task_alpha_decay(ctx):
    """Run the daily alpha decay monitoring cycle.

    Phase 3 of the Quant Fund Evolution roadmap. See ROADMAP.md.

    For every distinct strategy_type in ai_predictions:
      1. Write today's rolling-window snapshot to signal_performance_history
      2. Check for decay (rolling Sharpe < lifetime - 30% for 30+ days)
      3. Auto-deprecate decayed strategies
      4. Restore deprecated strategies whose edge has recovered

    The trade pipeline's _rank_candidates() skips deprecated strategy signals.
    """
    try:
        from alpha_decay import run_decay_cycle
        seg_label = ctx.display_name or ctx.segment
        summary = run_decay_cycle(ctx.db_path)

        logging.info(
            f"[{seg_label}] Alpha decay: "
            f"snapshotted={len(summary['strategies_snapshotted'])}, "
            f"newly_deprecated={summary['newly_deprecated']}, "
            f"restored={summary['restored']}, "
            f"errors={len(summary['errors'])}"
        )

        # Surface meaningful events as activity log entries
        from display_names import display_name
        for stype in summary["newly_deprecated"]:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "alpha_decay",
                f"Strategy deprecated: {display_name(stype)}",
                f"Alpha decay threshold crossed — strategy auto-retired. "
                f"The trade pipeline will now skip signals from this strategy."
            )
        for stype in summary["restored"]:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "alpha_decay",
                f"Strategy restored: {display_name(stype)}",
                "Rolling edge recovered — strategy is active again."
            )
    except Exception as exc:
        logging.warning(f"Alpha decay monitor failed: {exc}")


def _task_sec_filings(ctx):
    """Monitor SEC filings for watchlist symbols and AI-analyze material changes.

    Phase 4 of the Quant Fund Evolution roadmap. See ROADMAP.md.

    Scans the profile's current positions + any symbol that's been in a
    recent shortlist for new 10-K/10-Q/8-K filings. Each new filing is
    fetched, key sections extracted, and compared to the previous filing of
    the same type via AI. Material language changes are saved as alerts
    visible to the trade pipeline and dashboard.

    Crypto profiles are skipped — SEC filings don't apply.
    """
    # SEC doesn't apply to crypto
    if ctx is not None and ctx.segment == "crypto":
        return

    try:
        from sec_filings import monitor_symbol
        from client import get_positions

        seg_label = ctx.display_name or ctx.segment

        # Build watchlist: held positions + last cycle's shortlist (if any)
        symbols = set()
        try:
            positions = get_positions(ctx=ctx)
            for p in positions:
                # Equity symbols only (no slashes)
                if "/" not in p.get("symbol", ""):
                    symbols.add(p["symbol"])
        except Exception:
            pass

        # Add recent shortlist symbols from cycle_data if available
        try:
            import json as _json
            import os as _os
            cycle_file = f"cycle_data_{getattr(ctx, 'profile_id', 0)}.json"
            if _os.path.exists(cycle_file):
                with open(cycle_file) as f:
                    cycle_data = _json.load(f)
                for c in cycle_data.get("shortlist", [])[:10]:
                    sym = c.get("symbol", "")
                    if sym and "/" not in sym:
                        symbols.add(sym)
        except Exception:
            pass

        if not symbols:
            logging.info(f"[{seg_label}] SEC filings: no symbols to check")
            return

        logging.info(f"[{seg_label}] SEC filings: checking {len(symbols)} symbols")

        total_new = 0
        total_alerts = 0
        for sym in sorted(symbols):
            try:
                summary = monitor_symbol(sym, ctx.db_path, ctx=ctx,
                                         days_back=90, max_filings_per_cycle=5)
                total_new += summary["new_filings"]
                total_alerts += len(summary["alerts"])
                for alert in summary["alerts"]:
                    _safe_log_activity(
                        getattr(ctx, "profile_id", 0), ctx.user_id,
                        "sec_alert",
                        f"SEC Alert: {alert['symbol']} {alert['form']}",
                        f"{alert['severity'].upper()} severity — {alert['summary']}",
                        symbol=alert["symbol"],
                    )
            except Exception as exc:
                logging.debug(f"SEC monitor failed for {sym}: {exc}")

        logging.info(f"[{seg_label}] SEC filings: {total_new} new, {total_alerts} alerts")

    except Exception as exc:
        logging.warning(f"SEC filing monitor failed: {exc}")


def _task_run_watchdog(ctx):
    """Detect stalled task runs and alert.

    Any row in `task_runs` with status='running' + started_at older than
    30 min is treated as stalled. Mark it, log, emit an event, send a
    notification email. Idempotent — repeated watchdog runs don't
    re-alert the same stalled row.
    """
    try:
        from task_watchdog import check_stalled_runs
        seg_label = ctx.display_name or ctx.segment
        stalled = check_stalled_runs(ctx.db_path, stall_minutes=30)
        if not stalled:
            return

        logging.warning(
            f"[{seg_label}] Watchdog: {len(stalled)} stalled tasks detected"
        )
        for row in stalled:
            elapsed = row.get("minutes_elapsed", 0) or 0
            task_name = row["task_name"]
            started_at = row["started_at"]

            # Diagnose probable cause
            if elapsed > 120:
                cause = "Service was restarted while this task was running (task survived restart as orphaned 'running' row)."
            elif "Scan" in task_name and elapsed > 30:
                cause = "Scan cycle exceeded 30-minute timeout — likely slow API responses from Alpaca or the AI provider."
            elif "Resolve" in task_name:
                cause = "Prediction resolution hung — likely a price fetch timeout for one or more symbols."
            elif "Snapshot" in task_name:
                cause = "Daily snapshot hung — likely a slow position/account fetch from the broker."
            else:
                cause = "Task did not complete within 30 minutes. Could be a hung API call, a crash, or a service restart."

            logging.warning(
                f"  STALLED: {task_name} "
                f"(started {started_at}, {elapsed:.0f} min elapsed)"
            )
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "task_stalled",
                f"Stalled task: {task_name} ({elapsed:.0f} min)",
                f"Started: {started_at}\n"
                f"Elapsed: {elapsed:.0f} minutes\n"
                f"Diagnosis: {cause}",
            )
            try:
                from event_bus import emit
                emit(
                    ctx.db_path, "task_stalled",
                    symbol=None, severity="high",
                    payload={
                        "task_name": task_name,
                        "started_at": started_at,
                        "minutes_elapsed": round(elapsed, 1),
                        "diagnosis": cause,
                    },
                    dedup_key=f"task_stalled:{row['id']}",
                )
            except Exception:
                pass
            try:
                from notifications import notify_error
                notify_error(
                    error_msg=(
                        f"Stalled task: {row['task_name']} "
                        f"(elapsed {elapsed:.0f} min)"
                    ),
                    context=(
                        f"Profile: {seg_label}\n"
                        f"Task started at: {row['started_at']}\n"
                        f"Elapsed: {elapsed:.0f} minutes without completion.\n\n"
                        f"The task was marked stalled by the watchdog. "
                        f"Check journalctl -u quantopsai for the underlying "
                        f"failure mode."
                    ),
                    ctx=ctx,
                )
            except Exception as exc:
                logging.debug(f"Watchdog notification failed: {exc}")
    except Exception as exc:
        logging.warning(f"Watchdog task failed: {exc}")


def _task_db_backup(ctx):
    """Daily SQLite backup with rotation.

    Per-profile DBs hold all proprietary training data. A plain `cp` of
    a WAL-mode database can corrupt the copy — `backup_db.backup_all`
    uses SQLite's native backup API to produce consistent snapshots
    even while other tasks are writing.

    Runs once per day from the daily snapshot block so we get exactly
    one backup per profile per day. Dedup is per-date: re-running the
    task later the same day overwrites atomically (atomic via .tmp).
    """
    try:
        from backup_db import backup_all
        seg_label = ctx.display_name or ctx.segment
        project_dir = os.path.dirname(os.path.abspath(__file__))
        summary = backup_all(project_dir)
        logging.info(
            f"[{seg_label}] DB backup: "
            f"backed_up={summary['backed_up']}, "
            f"pruned={summary['pruned']}, "
            f"failed={summary['failed']}"
        )
        if summary["failed"] > 0:
            logging.warning(f"[{seg_label}] DB backup had {summary['failed']} failures")
    except Exception as exc:
        logging.warning(f"DB backup task failed: {exc}")


def _task_weekly_digest(master_db_path=None):
    """Send the weekly AI-work digest email.

    Idempotent: only fires once per Friday, after 17:00 server-local
    (5 PM, past the 15:55 self-tune). File-based marker survives
    restarts AND ensures the 10 profiles that hit this task sequentially
    from the daily snapshot block don't produce 10 emails.

    Safe no-op on non-Fridays, before 17:00, or when already sent today.
    """
    try:
        now = datetime.now(ET)
        # Fridays only (weekday 4) in Eastern Time — market-close day
        if now.weekday() != 4:
            return
        # 16:00 ET = market close. Fires with the daily-snapshot block
        # which runs on the first scheduler tick after 15:55 ET. By 16:00
        # the self-tune has already run (15:55 trigger), so the digest
        # captures the week's FINAL tuning decisions.
        # Server runs UTC — explicit ET conversion here matches the other
        # timing-sensitive gates (snapshot, self-tune).
        if now.hour < 16:
            return

        if master_db_path is None:
            import config as _config
            master_db_path = _config.DB_PATH

        marker_path = os.path.join(
            os.path.dirname(os.path.abspath(master_db_path)),
            ".weekly_digest_sent.marker",
        )
        today_str = now.strftime("%Y-%m-%d")
        try:
            with open(marker_path) as f:
                last_sent = f.read().strip()
            if last_sent == today_str:
                return  # already sent this Friday
        except FileNotFoundError:
            pass

        from ai_weekly_summary import build_weekly_summary, render_html
        from notifications import send_email
        summary = build_weekly_summary(master_db_path=master_db_path)
        subject, html = render_html(summary)
        ok = send_email(subject, html, ctx=None)
        if ok:
            # Write marker AFTER a successful send — retry next cycle if failed
            try:
                with open(marker_path, "w") as f:
                    f.write(today_str)
            except Exception as exc:
                logging.warning("Weekly digest marker write failed: %s", exc)
            logging.info(
                "Weekly AI digest sent: %s profiles=%d trades=%d pnl=$%.2f",
                subject,
                len(summary["profiles"]),
                summary["totals"]["buys"] + summary["totals"]["sells"],
                summary["totals"]["realized_pnl"],
            )
        else:
            logging.warning("Weekly digest email failed — will retry next cycle")
    except Exception as exc:
        logging.warning("Weekly digest task failed: %s", exc)


def _task_auto_strategy_lifecycle(ctx):
    """Daily promotion / retirement pass for auto-generated strategies.

    Phase 7 of the Quant Fund Evolution roadmap. See ROADMAP.md.

    Promotes shadow strategies that have cleared the minimum prediction
    count and Sharpe threshold to `active`. Retires shadows that have
    exhausted their shadow period without developing an edge.
    """
    try:
        from strategy_lifecycle import tick
        seg_label = ctx.display_name or ctx.segment
        result = tick(ctx.db_path)
        n_promoted = len(result.get("promoted", []))
        n_retired = len(result.get("retired", []))
        logging.info(
            f"[{seg_label}] Auto-strategy lifecycle: promoted={n_promoted}, retired={n_retired}"
        )
        from display_names import display_name
        for ev in result.get("promoted", []):
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "auto_strategy_promoted",
                f"Strategy promoted to active: {display_name(ev['name'])}",
                f"Shadow Sharpe {ev.get('sharpe', 0):.2f} after "
                f"{ev.get('n', 0)} predictions — now trading live capital."
            )
        for ev in result.get("retired", []):
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "auto_strategy_retired",
                f"Strategy retired: {display_name(ev['name'])}",
                f"Shadow period exceeded ({ev.get('shadow_days', 0)}d) "
                f"with rolling Sharpe {ev.get('sharpe', 0):.2f}."
            )
    except Exception as exc:
        logging.warning(f"Auto-strategy lifecycle failed: {exc}")


def _task_capital_rebalance(ctx):
    """Weekly capital rebalance for users with auto_capital_allocation
    enabled. Runs on Sundays only; file-based idempotency marker
    prevents re-firing if the scheduler restarts on the same Sunday.

    For each enabled user, calls capital_allocator.rebalance(user_id)
    which respects the per-Alpaca-account constraint — profiles
    sharing one real account have their scales normalized within the
    group so the underlying capital is never over-committed."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() != 6:  # Sunday only
        return

    seg_label = ctx.display_name or ctx.segment
    today = now_et.strftime("%Y-%m-%d")
    marker = ".capital_rebalance_done.marker"

    try:
        with open(marker) as f:
            if f.read().strip() == today:
                logging.info(
                    f"[{seg_label}] Capital rebalance already ran today — skipping.")
                return
    except FileNotFoundError:
        pass

    try:
        from capital_allocator import rebalance
        from models import _get_conn
        # Iterate all users who have opted in.
        conn = _get_conn()
        users = conn.execute(
            "SELECT id, email FROM users WHERE auto_capital_allocation = 1"
        ).fetchall()
        conn.close()

        if not users:
            logging.info(
                f"[{seg_label}] No users with auto_capital_allocation enabled.")
            try:
                with open(marker, "w") as f:
                    f.write(today)
            except OSError:
                pass
            return

        for user in users:
            uid = user["id"] if hasattr(user, "keys") else user[0]
            try:
                changes = rebalance(uid)
                if changes:
                    summary = ", ".join(
                        f"{c['name']}: {c['old_scale']:.2f}→{c['new_scale']:.2f}"
                        for c in changes
                    )
                    logging.info(
                        f"[{seg_label}] Capital rebalance for user {uid}: "
                        f"{len(changes)} change(s) — {summary}")
                else:
                    logging.info(
                        f"[{seg_label}] Capital rebalance for user {uid}: no changes.")
            except Exception as exc:
                logging.warning(
                    f"[{seg_label}] Capital rebalance failed for user {uid}: {exc}")

        try:
            with open(marker, "w") as f:
                f.write(today)
        except OSError as exc:
            logging.warning(f"Could not write capital-rebalance marker: {exc}")
    except Exception as exc:
        logging.warning(
            f"[{seg_label}] Capital rebalance task failed: {exc}")


def _task_auto_strategy_generation(ctx):
    """Weekly AI-driven proposal + validation of new auto-strategies.

    Phase 7 of the Quant Fund Evolution roadmap. See ROADMAP.md.

    Runs on Sundays only. Asks the AI for 3 new strategy specs tailored
    to recent performance, validates each via Phase 2 rigorous_backtest,
    and promotes passers into shadow mode.
    """
    import datetime as _dt
    # Only run on Sundays (weekday 6 = Sunday in Python's 0=Mon convention)
    if _dt.datetime.utcnow().weekday() != 6:
        return

    try:
        from strategy_proposer import propose_strategies
        from strategy_generator import save_spec
        from strategy_lifecycle import validate_and_promote
        from multi_strategy import get_allocation_summary

        seg_label = ctx.display_name or ctx.segment

        # Recent performance summary — drives the proposer's context
        try:
            perf = get_allocation_summary(ctx.db_path, ctx.segment)
        except Exception:
            perf = []
        recent_perf = [
            {"name": p["name"], "sharpe": p.get("rolling_sharpe", 0),
             "win_rate": p.get("rolling_win_rate", 0),
             "n_predictions": p.get("rolling_n", 0)}
            for p in perf
        ]
        ctx_summary = (f"{ctx.segment} market, profile '{seg_label}'. "
                       f"Current strategy count: {len(perf)}.")

        proposals = propose_strategies(
            ctx_summary=ctx_summary,
            recent_performance=recent_perf,
            n_proposals=3,
            ai_provider=ctx.ai_provider,
            ai_model=ctx.ai_model,
            ai_api_key=ctx.ai_api_key,
            market_types=[ctx.segment],
            db_path=ctx.db_path,
        )
        logging.info(f"[{seg_label}] Proposer returned {len(proposals)} valid specs")

        validated = 0
        retired = 0
        for spec in proposals:
            try:
                spec_id = save_spec(ctx.db_path, spec)
                result = validate_and_promote(ctx.db_path, spec_id, rigorous=True)
                if result.get("outcome") == "validated":
                    validated += 1
                else:
                    retired += 1
            except Exception as exc:
                logging.warning(f"Failed to validate proposal {spec.get('name')}: {exc}")

        logging.info(
            f"[{seg_label}] Auto-strategy generation: "
            f"proposed={len(proposals)}, validated={validated}, retired={retired}"
        )
        if validated > 0:
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "auto_strategy_generated",
                f"{validated} new auto-strategies entered shadow mode",
                f"AI proposed {len(proposals)} strategies; {validated} cleared "
                f"the Phase 2 validation gate and are now running in shadow mode."
            )
    except Exception as exc:
        logging.warning(f"Auto-strategy generation failed: {exc}")


def _task_crisis_monitor(ctx):
    """Detect cross-asset crisis conditions and persist transitions (Phase 10).

    Runs before every trade cycle. Records state transitions and emits
    `crisis_state_change` events — handled by the existing event bus
    (log_activity handler flags the change in the activity feed).
    """
    try:
        from crisis_state import run_crisis_tick
        seg_label = ctx.display_name or ctx.segment
        result = run_crisis_tick(ctx.db_path)
        if result.get("changed"):
            logging.warning(
                f"[{seg_label}] Crisis transition: "
                f"{result['prior_level']} → {result['level']} "
                f"(size x{result['size_multiplier']:.2f}, "
                f"{len(result['signals'])} signals)"
            )
        else:
            logging.info(
                f"[{seg_label}] Crisis monitor: level={result['level']} "
                f"(unchanged, {len(result['signals'])} signals)"
            )
    except Exception as exc:
        logging.warning(f"Crisis monitor failed: {exc}")


def _task_event_tick(ctx):
    """Run event detectors and dispatch pending events (Phase 9).

    Idempotent: each detector uses a dedup key so repeat invocations
    don't duplicate events. Handler failures are captured per-handler
    and do not abort the tick.
    """
    try:
        from event_bus import dispatch_pending
        from event_detectors import run_all_detectors
        from event_handlers import register_default_handlers

        register_default_handlers()
        emitted = run_all_detectors(ctx)
        summary = dispatch_pending(ctx.db_path, ctx, limit=20)

        seg_label = ctx.display_name or ctx.segment
        n_emitted = sum(v for v in emitted.values() if v > 0)
        logging.info(
            f"[{seg_label}] Event tick: emitted={n_emitted}, "
            f"dispatched={summary['dispatched']}, "
            f"handler_errors={summary['handler_errors']}"
        )
    except Exception as exc:
        logging.warning(f"Event tick failed: {exc}")


def _task_daily_summary_email(ctx):
    """Send end-of-day summary email — once per profile per calendar
    day. File-based idempotency marker survives scheduler restarts so
    every redeploy doesn't re-fire the email (incident 2026-04-25:
    100+ summary emails sent because the in-memory snapshot flag was
    reset on each of ~10 restarts during a heavy deploy day)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    from notifications import notify_daily_summary

    profile_id = getattr(ctx, "profile_id", 0)
    today_et = _dt.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
    marker_path = f".daily_summary_sent_p{profile_id}.marker"

    try:
        with open(marker_path) as f:
            last_sent = f.read().strip()
        if last_sent == today_et:
            logging.info(
                f"Daily summary already sent for profile {profile_id} "
                f"today ({today_et}) — skipping.")
            return
    except FileNotFoundError:
        pass

    notify_daily_summary(ctx=ctx)
    try:
        with open(marker_path, "w") as f:
            f.write(today_et)
    except OSError as exc:
        logging.warning(f"Could not write daily-summary marker: {exc}")
    logging.info(f"Daily summary email sent for profile {profile_id}.")


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

    # ── Interval tracking ────────────────────────────────────────────
    # Two kinds of state:
    #   - `profile_runs[profile_id]`: per-profile last-run timestamps. Each
    #     profile clocks its own scan/exit/predict interval so one slow
    #     cycle on profile N doesn't starve profile M. Solves the
    #     "Large Cap never runs" starvation bug (2026-04-14).
    #   - `last_run`: kept for legacy segment mode + daily_snapshot (global
    #     date-stamp that's shared across all profiles by design — one
    #     snapshot per calendar day system-wide).
    profile_runs: Dict[int, Dict[str, float]] = {}
    # daily_snapshot is restart-persistent via a marker file so the
    # whole snapshot bundle (incl. summary email, DB backup, alpha-decay
    # snapshot) doesn't re-fire on every scheduler restart. Incident
    # 2026-04-25: 100+ daily summary emails sent because in-memory
    # state was reset on each of ~10 deploys in a single day.
    _SNAPSHOT_MARKER = ".daily_snapshot_done.marker"
    _initial_snapshot_date = None
    try:
        with open(_SNAPSHOT_MARKER) as _f:
            _initial_snapshot_date = _f.read().strip() or None
    except FileNotFoundError:
        pass

    last_run = {
        "scan": 0.0,                 # legacy-mode only
        "check_exits": 0.0,          # legacy-mode only
        "resolve_predictions": 0.0,  # legacy-mode only
        "daily_snapshot": _initial_snapshot_date,
    }

    def _persist_snapshot_marker(date_str: str) -> None:
        try:
            with open(_SNAPSHOT_MARKER, "w") as f:
                f.write(date_str)
        except OSError as exc:
            logging.warning("Could not persist snapshot marker: %s", exc)

    def _get_profile_runs(pid: int) -> Dict[str, float]:
        """Return per-profile last-run dict, initializing on first access."""
        if pid not in profile_runs:
            profile_runs[pid] = {
                "scan": 0.0,
                "check_exits": 0.0,
                "resolve_predictions": 0.0,
            }
        return profile_runs[pid]

    INTERVAL_SCAN = 15 * 60   # 15 minutes
    # Exits check every 5 min — cheap, time-critical (TP/SL triggers
    # need to fire within minutes of price hitting threshold, not whenever
    # the 15-min scan happens to complete).
    INTERVAL_CHECK_EXITS = 5 * 60
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

        # Legacy-mode global interval checks (used only by the legacy
        # segment-based branch below — profile branch computes these
        # per-profile on each iteration).
        do_scan = (current_time - last_run["scan"]
                   >= INTERVAL_SCAN)
        do_exits = (current_time - last_run["check_exits"]
                    >= INTERVAL_CHECK_EXITS)
        do_predictions = (current_time - last_run["resolve_predictions"]
                          >= INTERVAL_RESOLVE_PREDICTIONS)
        # Snapshot should fire once per day, on or after the close of the
        # US cash session. The old trigger required exactly 15:55-15:59 —
        # if the scheduler was restarted or paused through that 5-minute
        # window, the day silently got no snapshot. New trigger: ≥ 15:55
        # in server local time, any later time that same day is also fine,
        # and we dedupe using `last_run["daily_snapshot"]` (the date string).
        _after_close = (now.hour > 15 or (now.hour == 15 and now.minute >= 55))
        do_snapshot = (_after_close
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

            # Per-profile due-checks: collect all profiles that are due,
            # then run them in parallel (ThreadPoolExecutor). With 2+ CPUs
            # this cuts total wall-clock from ~15 min (sequential) to ~5 min
            # (the slowest single profile).
            due_profiles = []
            for prof in profiles:
                if _shutdown:
                    break
                pr = _get_profile_runs(prof["id"])
                now_t = time.time()
                prof_do_scan = (now_t - pr["scan"]) >= INTERVAL_SCAN
                prof_do_exits = (now_t - pr["check_exits"]) >= INTERVAL_CHECK_EXITS
                prof_do_predictions = (now_t - pr["resolve_predictions"]) >= INTERVAL_RESOLVE_PREDICTIONS
                if not (prof_do_scan or prof_do_exits or prof_do_predictions or do_snapshot):
                    continue

                try:
                    ctx = _build_ctx_from_profile(prof)
                except Exception:
                    logging.exception(
                        f"Failed to build context for profile #{prof['id']} ({prof['name']})")
                    continue

                if not ctx.is_within_schedule(now):
                    continue

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

                due_profiles.append({
                    "prof": prof, "ctx": ctx, "pr": pr,
                    "do_scan": prof_do_scan, "do_exits": prof_do_exits,
                    "do_predictions": prof_do_predictions,
                })

            def _run_one_profile(item):
                """Run a single profile's cycle. Called from thread pool."""
                prof = item["prof"]
                ctx = item["ctx"]
                logging.info(
                    f"=== Processing profile: {prof['name']} "
                    f"(#{prof['id']}, {prof['market_type']}, "
                    f"schedule={ctx.schedule_type}) — "
                    f"scan={item['do_scan']} exits={item['do_exits']} "
                    f"preds={item['do_predictions']} snap={do_snapshot} ==="
                )
                run_segment_cycle(
                    ctx,
                    run_scan=item["do_scan"], run_exits=item["do_exits"],
                    run_predictions=item["do_predictions"],
                    run_snapshot=do_snapshot, run_summary=do_snapshot,
                )
                # Stamp per-profile timestamps
                finish_t = time.time()
                pr = item["pr"]
                if item["do_scan"]:
                    pr["scan"] = finish_t
                if item["do_exits"]:
                    pr["check_exits"] = finish_t
                if item["do_predictions"]:
                    pr["resolve_predictions"] = finish_t
                return prof["name"]

            if due_profiles:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                max_workers = min(len(due_profiles), 3)
                logging.info(
                    f"Running {len(due_profiles)} profile(s) in parallel "
                    f"(max_workers={max_workers})"
                )
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(_run_one_profile, item): item["prof"]["name"]
                        for item in due_profiles
                    }
                    for future in as_completed(futures):
                        name = futures[future]
                        try:
                            future.result()
                            logging.info(f"Profile {name} completed")
                        except Exception:
                            logging.exception(f"Profile {name} failed")
                ran_something = True

        # Update global timestamps (legacy mode + snapshot dedup)
        if ran_something:
            if do_scan:
                last_run["scan"] = time.time()
            if do_exits:
                last_run["check_exits"] = time.time()
            if do_predictions:
                last_run["resolve_predictions"] = time.time()
            if do_snapshot:
                last_run["daily_snapshot"] = today_str
                _persist_snapshot_marker(today_str)

            # Write status file for the web UI countdown timers
            try:
                status = {
                    "last_scan": last_run["scan"],
                    "next_scan": last_run["scan"] + INTERVAL_SCAN,
                    "last_exit_check": last_run["check_exits"],
                    "next_exit_check": last_run["check_exits"] + INTERVAL_CHECK_EXITS,
                    "last_ai_resolve": last_run["resolve_predictions"],
                    "next_ai_resolve": last_run["resolve_predictions"] + INTERVAL_RESOLVE_PREDICTIONS,
                    "scan_interval_min": INTERVAL_SCAN // 60,
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
                _persist_snapshot_marker(today_str)

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
