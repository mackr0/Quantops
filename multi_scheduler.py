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
import sqlite3
import sys
import os
import json as _json
from contextlib import closing
from datetime import datetime, timedelta
from typing import Dict, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

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
    """Return True if the regular US cash session is open right now.

    Holiday- and half-day-aware via Alpaca's clock (see market_calendar);
    falls back to a weekday + hardcoded-holiday heuristic when Alpaca is
    unreachable."""
    import market_calendar
    return market_calendar.is_market_open(now)


def next_market_open(now=None):
    """Return datetime of next regular market open, skipping weekends
    and holidays (Alpaca clock when live, heuristic otherwise)."""
    import market_calendar
    return market_calendar.next_market_open(now)


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
    except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError,
            AttributeError, KeyError, ValueError, OSError) as _ctx_exc:
        # Primary DB user-context path — falls through to env-var-
        # based fallback below. Surface for follow-up.
        logger.debug(
            "primary user-context path failed for %s, falling back to env: %s: %s",
            segment_name, type(_ctx_exc).__name__, _ctx_exc,
        )

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
            except (sqlite3.OperationalError, sqlite3.DatabaseError,
                    AttributeError, OSError) as _tc_exc:
                # Tracker cleanup on success path; failure must not
                # affect task outcome. Surface for follow-up.
                logger.debug(
                    "task_watchdog tracker close (success) failed: %s: %s",
                    type(_tc_exc).__name__, _tc_exc,
                )
    except Exception as exc:
        elapsed = time.time() - start
        logging.exception(f"[TASK FAIL]  {name} ({elapsed:.1f}s)")
        if tracker:
            try:
                tracker.__exit__(type(exc), exc, exc.__traceback__)
            except (sqlite3.OperationalError, sqlite3.DatabaseError,
                    AttributeError, OSError) as _tc_exc:
                # Tracker cleanup on failure path; original task
                # error already logged. Surface for follow-up.
                logger.debug(
                    "task_watchdog tracker close (failure) failed: %s: %s",
                    type(_tc_exc).__name__, _tc_exc,
                )


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

    # Phase 5d of pipeline refactor (2026-05-11): one-time backfill
    # of historical option-prediction rows that were resolved with
    # the broken pre-Phase-5c math (underlying-stock-derived
    # actual_return_pct on option premiums). Gated by a migration
    # marker — runs ONCE per profile DB, no-ops thereafter. Failure
    # is non-fatal; the cycle continues. See pipelines/outcomes/
    # backfill.py for details.
    try:
        from pipelines.outcomes.backfill import (
            backfill_historical_option_predictions,
        )
        backfill_historical_option_predictions(ctx.db_path)
    except Exception as _exc:
        logging.debug(
            f"Phase 5d backfill failed (non-fatal): {_exc}",
        )

    # 2026-05-12 — Phase 2b option tuner WRITES. Each cycle, the
    # OptionPipeline.tune() proposes adjustments to the three
    # Greek-budget params (max_net_options_delta_pct,
    # max_theta_burn_dollars_per_day, max_short_vega_dollars) based
    # on option win rate, and persists them to trading_profiles.
    # StockPipeline.tune() is also called but its tuning isn't yet
    # writing parameters — that's a separate future commit.
    # gated on enable_self_tuning so a profile can opt out.
    try:
        if getattr(ctx, "enable_self_tuning", True):
            from pipelines.tuning_writer import run_pipeline_tuning
            run_pipeline_tuning(ctx)
    except Exception as _exc:
        logging.debug(
            f"Pipeline tuning failed (non-fatal): {_exc}",
        )

    # 2026-05-11: pipeline-aware specialist calibrator recalibration.
    # Pre-this-commit, calibrators were trained on a mix of stock +
    # option resolutions where option rows had wrong actual_return_pct
    # values (Phase 5b/5c fixed forward; Phase 5d fixed historical).
    # This recalibration refits every specialist's calibrators across
    # the new (direction × pipeline_kind) matrix on the now-clean
    # training data. Marker-gated; runs once per profile DB.
    try:
        from pipelines.outcomes.recalibrate import (
            recalibrate_all_specialists,
        )
        recalibrate_all_specialists(ctx.db_path)
    except Exception as _exc:
        logging.debug(
            f"Specialist recalibration failed (non-fatal): {_exc}",
        )

    seg_label = ctx.display_name or ctx.segment
    logging.info(f"--- [{seg_label.upper()}] segment cycle start ---")

    # Benchmark profiles (buy_hold, random) must NOT inherit the
    # AI-driven exit/risk tasks — otherwise they're not pure nulls
    # and the AI-vs-baseline comparison is contaminated. Caught
    # 2026-05-18 14:53 ET when EXP-A1-RandomA's SNPS position hit
    # a trailing stop ($492.73 < $499.00 = high $517.17 - 1.5×ATR
    # $12.11), auto-exited, then random re-bought it on the next
    # cycle, then it hit the trailing stop again — same churn for
    # P14's AMD. Random was supposed to be "buy 5 random picks +
    # hold until next day's picks" per docs/15_EXPERIMENT_DESIGN.
    _is_baseline = getattr(ctx, "strategy_type", "ai") in ("buy_hold", "random")

    # CRITICAL ORDERING: exits BEFORE scan.
    # Exits are cheap (~1 sec per profile) and protect realized P&L.
    # Scans can take 5-30 minutes and sometimes hang (yfinance timeouts,
    # Alpaca rate limits, hung API calls). If the scan hangs BEFORE
    # exits run, held positions pass their take-profit / stop-loss
    # thresholds without firing — realized P&L evaporates. Running
    # exits first guarantees they can't be blocked by a downstream
    # failure in the scan pipeline.
    if run_exits:
        # === AI-only auto-exit tasks (skipped for benchmark profiles) ===
        # These drive exit decisions or AI-specific risk controls.
        # Running them on buy_hold/random would impose stop-loss /
        # trailing-stop / take-profit / kill-switch behavior that
        # those benchmarks are designed NOT to have.
        if not _is_baseline:
            run_task(
                f"[{seg_label}] Check Exits",
                lambda: _task_check_exits(ctx),
                db_path=ctx.db_path,
            )
            # Stop-order coverage — auto-attaches protective stops
            # to open longs. Contaminates benchmarks (random would
            # never have a stop in pure form).
            run_task(
                f"[{seg_label}] Stop Coverage",
                lambda: _task_check_stop_coverage(ctx),
                db_path=ctx.db_path,
            )
            # Position-runaway sentinel — detects AI duplicate-submit
            # / oversize-qty bugs. Irrelevant for non-AI strategies.
            run_task(
                f"[{seg_label}] Position Runaway",
                lambda: _task_check_position_runaway(ctx),
                db_path=ctx.db_path,
            )
            # AI consistency floor — alerts when AI win rate drops.
            # No AI in baselines → no win rate to floor.
            run_task(
                f"[{seg_label}] AI Consistency",
                lambda: _task_check_ai_consistency(ctx),
                db_path=ctx.db_path,
            )
            # Book-wide daily-loss floor — auto-flips the kill switch
            # on drawdown. Benchmarks must take their drawdowns
            # untouched for a clean comparison.
            run_task(
                f"[{seg_label}] Book Loss Floor",
                lambda: _task_check_book_loss_floor(ctx),
                db_path=ctx.db_path,
            )
            if getattr(ctx, "enable_intraday_risk_halt", True):
                run_task(
                    f"[{seg_label}] Intraday Risk Check",
                    lambda: _task_intraday_risk_check(ctx),
                    db_path=ctx.db_path,
                )
            if getattr(ctx, "enable_long_vol_hedge", False):
                run_task(
                    f"[{seg_label}] Long-Vol Hedge",
                    lambda: _task_manage_long_vol_hedge(ctx),
                    db_path=ctx.db_path,
                )

        # === Tasks that run for ALL profiles (data accuracy, broker reconcile) ===
        # Sanity / journal-state maintenance. Don't drive exit
        # decisions — safe for benchmarks too.
        run_task(
            f"[{seg_label}] Cancel Stale Orders",
            lambda: _task_cancel_stale_orders(ctx),
            db_path=ctx.db_path,
        )
        run_task(
            f"[{seg_label}] Update Fill Prices",
            lambda: _task_update_fills(ctx),
            db_path=ctx.db_path,
        )
        run_task(
            f"[{seg_label}] Reconcile Trade Statuses",
            lambda: _task_reconcile_trade_statuses(ctx),
            db_path=ctx.db_path,
        )
        # Activities capture (dividends, option events) — REQUIRED
        # for benchmarks too. buy_hold_spy specifically needs DIV
        # credits posted to the journal to compute correct equity.
        run_task(
            f"[{seg_label}] Capture Broker Activities",
            lambda: _task_capture_broker_activities(ctx),
            db_path=ctx.db_path,
        )
        # Options-related tasks are no-ops for stock-only baselines
        # (random + buy_hold trade SPY/equities) but cheap to run.
        run_task(
            f"[{seg_label}] Options Lifecycle",
            lambda: _task_options_lifecycle(ctx),
            db_path=ctx.db_path,
        )
        run_task(
            f"[{seg_label}] Options Roll Manager",
            lambda: _task_options_roll_manager(ctx),
            db_path=ctx.db_path,
        )
        run_task(
            f"[{seg_label}] Delta Hedger",
            lambda: _task_options_delta_hedger(ctx),
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
        # Data-source health probe — alerts LOUDLY if a critical
        # source (Alpaca bars / options / news) is silently failing.
        # Internally rate-limited to every 10 min across all profiles.
        run_task(
            f"[{seg_label}] Data Source Health",
            lambda: _task_data_source_health(ctx),
            db_path=ctx.db_path,
        )
        # Auto-expiry of gate-tightening tuning changes that haven't
        # shown evidence of improving win rate. Daily — internally
        # rate-limited via _auto_expiry_last_run_date.
        run_task(
            f"[{seg_label}] Auto-Expire Gate Tightens",
            lambda: _task_auto_expire_gate_tightens(ctx),
            db_path=ctx.db_path,
        )
        # Trade-rate anomaly check (Item 5 of docs/17 Phase 1) —
        # operator-visibility alert when weekly entry count drops
        # >50%. Daily — internally rate-limited via
        # _trade_rate_anomaly_last_run_date. Does NOT pause the
        # tuner (per feedback_ai_driven_no_manual_loop).
        run_task(
            f"[{seg_label}] Trade-Rate Anomaly Check",
            lambda: _task_trade_rate_anomaly_check(ctx),
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
        # Portfolio risk model snapshot (Item 2a of COMPETITIVE_GAP_PLAN)
        # — daily Barra-style factor exposures, parametric + Monte Carlo
        # VaR, and historical scenario stress tests. Persisted to
        # portfolio_risk_snapshots so dashboards and the AI prompt can
        # read the latest reading without recomputing. Per-profile
        # toggle so users can opt out (the snapshot is informational
        # but not free — pulls bars + Ken French CSV).
        if getattr(ctx, "enable_portfolio_risk_snapshot", True):
            run_task(
                f"[{seg_label}] Portfolio Risk Snapshot",
                lambda: _task_portfolio_risk_snapshot(ctx),
                db_path=ctx.db_path,
            )
        # Specialist calibrators (Wave 3 / Fix #9 of
        # METHODOLOGY_FIX_PLAN.md) — refit each specialist's
        # Platt-scaling layer on accumulated outcomes
        run_task(
            f"[{seg_label}] Specialist Calibration",
            lambda: _task_calibrate_specialists(ctx),
            db_path=ctx.db_path,
        )
        # Specialist health check (Lever 3 of
        # COST_AND_QUALITY_LEVERS_PLAN.md) — auto-disable specialists
        # whose calibrators show anti-correlation; auto-re-enable
        # when slope recovers. Hard floor: ≥2 specialists active.
        run_task(
            f"[{seg_label}] Specialist Health Check",
            lambda: _task_specialist_health_check(ctx),
            db_path=ctx.db_path,
        )
        # Universe audit (Wave 4 / Issue #10 of METHODOLOGY_FIX_PLAN.md)
        # — daily diff of Alpaca's active asset set; captures departures
        # for backtest survivorship-bias correction. Idempotent across
        # the day so it only really runs once per UTC date.
        run_task(
            f"[{seg_label}] Universe Audit",
            lambda: _task_universe_audit(ctx),
            db_path=ctx.db_path,
        )
        # Item 2 of OPEN_ITEMS — App Store rankings daily snapshot.
        # Idempotent across the day; populates app_store_history so
        # the WoW-change feature on get_app_store_ranking can compute
        # rank deltas vs last week.
        run_task(
            f"[{seg_label}] App Store Rankings Snapshot",
            lambda: _task_app_store_snapshot(ctx),
            db_path=ctx.db_path,
        )
        # Item 6 of OPEN_ITEMS — PDUFA event scrape. Idempotent across
        # the day; populates pdufa_events table so
        # get_biotech_milestones can return upcoming PDUFA dates.
        run_task(
            f"[{seg_label}] PDUFA Scrape",
            lambda: _task_pdufa_scrape(ctx),
            db_path=ctx.db_path,
        )
        # Alpha decay monitoring (Phase 3) — snapshot + detect + deprecate
        run_task(
            f"[{seg_label}] Alpha Decay Monitor",
            lambda: _task_alpha_decay(ctx),
            db_path=ctx.db_path,
        )
        # Item 1b — stat-arb cointegrated pair book. Off by default
        # because pair trades require both legs (long + short) so
        # long-only profiles can't act on the surfaced pairs. Per-
        # profile opt-in.
        if getattr(ctx, "enable_stat_arb_pairs", False):
            # Daily retest of active pairs — re-runs the Engle-Granger
            # test; ejects pairs whose p-value has drifted above 0.10
            # (cointegration broken).
            run_task(
                f"[{seg_label}] Stat-Arb Pair Retest",
                lambda: _task_stat_arb_retest(ctx),
                db_path=ctx.db_path,
            )
            # Weekly universe scan to discover new pairs. Sunday-only
            # with marker idempotency; no-op on other days.
            run_task(
                f"[{seg_label}] Stat-Arb Universe Scan",
                lambda: _task_stat_arb_universe_scan(ctx),
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
        # docs/18 item #2: nightly Phase 5c backfill — catches new
        # historical option rows that get created during the day
        # with broken underlying-price math. force=True bypasses
        # the migration marker; row-level WHERE clause keeps it
        # cheap on clean DBs.
        run_task(
            f"[{seg_label}] Phase 5c Backfill (Nightly)",
            lambda: _task_phase5c_backfill_nightly(ctx),
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
        # Weekly losing-week post-mortem — Sundays only. Triggers a
        # learned_pattern injection when the past 7 days
        # underperformed the long-term baseline.
        run_task(
            f"[{seg_label}] Losing-Week Post-Mortem",
            lambda: _task_post_mortem(ctx),
            db_path=ctx.db_path,
        )

    if run_summary:
        run_task(
            f"[{seg_label}] Daily Summary Email",
            lambda: _task_daily_summary_email(ctx),
            db_path=ctx.db_path,
        )
        if getattr(ctx, "enable_shadow_eval", False):
            run_task(
                f"[{seg_label}] Shadow Eval Digest Email",
                lambda: _task_shadow_eval_daily_email(ctx),
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
        except (KeyError, ValueError, AttributeError, TypeError,
                IndexError, OSError) as _ar_exc:
            # Per-symbol enrichment loop; one bad symbol shouldn't
            # kill the report. Surface for follow-up.
            logger.debug(
                "scan-report enrichment failed for %s: %s: %s",
                sym, type(_ar_exc).__name__, _ar_exc,
            )
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
            except (KeyError, ValueError, AttributeError, TypeError,
                    IndexError, OSError) as _ms_exc:
                # Per-symbol MAGA oversold scan; one bad symbol
                # shouldn't kill the loop. Surface for follow-up.
                logger.debug(
                    "MAGA oversold scan failed for %s: %s: %s",
                    sym, type(_ms_exc).__name__, _ms_exc,
                )
                continue
        logging.info(f"[{ctx.display_name}] MAGA oversold scan: added {maga_added}, {len(symbols)} total")

    result = list(symbols)[:30]
    _screener_cache[cache_key] = result
    return list(result)


def _task_scan_and_trade(ctx):
    """Screen the segment's universe and auto-trade via the AI-first pipeline.

    For profiles with `strategy_type` ∈ {buy_hold, random}, dispatch
    to simple_strategies (non-AI baselines) instead. See
    docs/15_EXPERIMENT_DESIGN_2026_05_17.md.
    """
    from trade_pipeline import run_trade_cycle
    from notifications import notify_trade, notify_veto
    from scan_status import update_status, clear_status

    seg_label = ctx.display_name or ctx.segment
    seg = get_segment(ctx.segment)
    is_crypto = seg.get("is_crypto", False)
    _pid = getattr(ctx, "profile_id", 0)

    # Strategy dispatch: non-AI baselines short-circuit the screener
    # + AI pipeline entirely.
    from simple_strategies import dispatch as _simple_dispatch
    _simple_summary = _simple_dispatch(ctx)
    if _simple_summary is not None:
        clear_status(_pid)
        logger.info(
            "[%s %s] summary: buys=%d sells=%d holds=%d errors=%d",
            seg_label, _simple_summary.get("strategy", "?"),
            _simple_summary.get("buys", 0),
            _simple_summary.get("sells", 0),
            _simple_summary.get("holds", 0),
            _simple_summary.get("errors", 0),
        )
        try:
            _safe_log_activity(
                _pid, ctx.user_id, "scan_summary",
                "%s %s cycle" % (seg_label, _simple_summary.get("strategy", "?")),
                "buys=%d sells=%d holds=%d errors=%d" % (
                    _simple_summary.get("buys", 0),
                    _simple_summary.get("sells", 0),
                    _simple_summary.get("holds", 0),
                    _simple_summary.get("errors", 0),
                ),
            )
        except Exception:
            logger.exception("Failed to log simple-strategy activity")
        return

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

    # 2026-05-19 reconciler safety net: if the reconciler HALTED this
    # profile (it detected drift that would require synthesizing
    # journal rows), skip the trade-pipeline dispatch entirely so
    # NEW ENTRIES are blocked. Existing exits / monitoring / risk-
    # snapshot tasks continue elsewhere — only the new-entry path is
    # gated here. Auto-clears next reconcile pass when drift resolves.
    try:
        from halt_helpers import is_halted
        halted, halt_reason = is_halted(_pid)
    except Exception as _hc_exc:
        # is_halted is read-only and best-effort, but if its DB
        # query fails we must NOT block trading — false positives
        # are operationally expensive. Default to not-halted.
        logging.warning(
            f"[{seg_label}] is_halted check failed (continuing as "
            f"NOT halted): {type(_hc_exc).__name__}: {_hc_exc}"
        )
        halted, halt_reason = False, None
    if halted:
        clear_status(_pid)
        logging.warning(
            f"[{seg_label}] TRADING HALTED — skipping trade-pipeline "
            f"dispatch. Reason: {halt_reason}"
        )
        try:
            _safe_log_activity(
                _pid, ctx.user_id, "trading_halted",
                f"{seg_label} HALTED — trade pipeline skipped",
                f"Reconciler safety net is HALTING this profile. "
                f"Reason: {halt_reason}\n\n"
                "Existing exits + monitoring continue; only new entries "
                "are blocked. Halt auto-clears next reconcile pass when "
                "no synthesis is needed."
            )
        except Exception:
            logging.exception("Failed to log halt activity")
        return

    update_status(_pid, "Running trade pipeline", "%d candidates" % len(symbols))
    # Scope C cutover gate: per-profile flag selects which dispatcher
    # the scheduler uses for THIS cycle. The two paths are mutually
    # exclusive — one cycle = one dispatcher, never both (otherwise
    # every trade would be submitted twice). Default OFF preserves
    # legacy behavior. Flip per profile only after shadow soak shows
    # verdict agreement ≥ 95%.
    if getattr(ctx, "use_pipeline_dispatch", False):
        from pipelines.dispatch import run_via_pipelines
        logging.info(
            f"[{seg_label}] dispatch=pipeline (Pipeline.run_cycle) — "
            f"{len(symbols)} candidates")
        summary = run_via_pipelines(symbols, ctx)
    else:
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

    # 2026-05-14 — every action type that the AI can propose must
    # generate a notification + activity log entry, not just stock
    # actions. Mack flagged that options trades were appearing on
    # the trades page but missing from the dashboard ticker because
    # MULTILEG_OPEN / OPTIONS / PAIR_TRADE were silently filtered
    # here.
    EXECUTED_ACTIONS = {
        "BUY", "STRONG_BUY", "SELL", "STRONG_SELL",
        "SHORT", "COVER",
        "OPTIONS", "MULTILEG_OPEN", "MULTILEG_CLOSE",
        "PAIR_TRADE",
    }
    for detail in summary.get("details", []):
        if detail.get("action") in EXECUTED_ACTIONS:
            try:
                notify_trade(detail, detail, detail, ctx=ctx)
            except Exception:
                logging.exception("Failed to send trade notification")

            # Log trade executed activity. Sizing detail varies by
            # action type — branch on the action so the ticker
            # doesn't render "BUY 0 SHOP @ $0.00" for multileg
            # trades that don't carry qty/price at the top level.
            sym = detail.get("symbol", "?")
            action = detail.get("action", "?")
            if action in ("MULTILEG_OPEN", "MULTILEG_CLOSE"):
                strat = detail.get("strategy_name") or detail.get("multileg_strategy") or ""
                contracts = detail.get("contracts")
                title = (
                    f"{action} {strat} {sym}"
                    f"{' x' + str(contracts) if contracts else ''}"
                ).strip()
            elif action == "OPTIONS":
                strat = detail.get("option_strategy") or ""
                contracts = detail.get("contracts")
                title = (
                    f"{action} {strat} {sym}"
                    f"{' x' + str(contracts) if contracts else ''}"
                ).strip()
            elif action == "PAIR_TRADE":
                title = f"{action} {sym}"
            else:
                qty = detail.get("qty", 0)
                price = detail.get("price", 0)
                title = (
                    f"{action} {qty:,.0f} {sym} @ ${price:,.2f}"
                    if qty and price
                    else f"{action} {sym}"
                )
            reason = detail.get("reason", "")
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "trade_executed",
                title,
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
    """Update fill prices + state-machine transitions on recent trades.

    Three responsibilities (added incrementally):
    1. Backfill ``fill_price`` (and ``price`` if NULL) when the
       broker has confirmed a fill but the journal hasn't yet seen
       it. Multileg legs lack ``decision_price`` (option chain quote
       isn't available at submit time) — slippage stays NULL on
       those rows. The ``decision_price IS NOT NULL`` filter that
       previously excluded multileg legs was removed 2026-05-07.
    2. (NEW 2026-05-07) Flip ``status='pending_fill'`` SELL/COVER
       rows to ``status='closed'`` once the broker confirms the
       close fill. Then flip the matching open BUY/SHORT rows to
       ``status='closed'`` as well so the trades page reflects the
       confirmed exit. Without this transition, the immediate-close
       write would create a phantom-SELL window if Alpaca
       async-canceled (caught 2026-05-06).
    """
    import sqlite3
    from client import get_api

    seg_label = ctx.display_name or ctx.segment
    db_path = ctx.db_path
    api = get_api(ctx)

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Pull every row missing a fill_price; multileg legs lack
        # decision_price so we can't filter on it. Status filter
        # excludes rows already marked terminal-unfilled so we don't
        # re-poll Alpaca for the same expired order forever.
        # Additionally re-process MULTILEG rows where the per-leg
        # `price` was poisoned with the combo's signed net (negative)
        # — see the MULTILEG branch below. This makes the bug
        # self-heal on the next cycle once the per-leg fix ships,
        # rather than needing a one-shot backfill script.
        unfilled = conn.execute(
            "SELECT id, order_id, price, decision_price, side, "
            "       symbol, status, signal_type, option_strategy, "
            "       occ_symbol, qty, timestamp "
            "FROM trades "
            "WHERE ("
            "      fill_price IS NULL"
            "      OR (signal_type = 'MULTILEG' AND fill_price <= 0)"
            ") "
            "  AND order_id IS NOT NULL "
            "  AND COALESCE(status, 'open') NOT IN "
            "      ('expired', 'canceled', 'rejected', 'done_for_day')"
        ).fetchall()

        if not unfilled:
            return

        updated = 0
        confirmed_closes = 0
        terminal_unfilled = 0
        orphan_rollbacks = 0
        for trade in unfilled:
            try:
                order = api.get_order(trade["order_id"])
            except Exception as exc:
                logging.debug(
                    "[%s] update_fills: get_order(%s) failed: %s",
                    seg_label, trade["order_id"], exc,
                )
                continue
            # Terminal-unfilled detection. Without this, an expired/
            # canceled/rejected order with filled_qty=0 is silently
            # skipped (the `if not filled_avg_price: continue` below)
            # and the journal row sits at status='open' with
            # price=NULL forever — which is what put 3 orphan
            # multileg legs on prod for 2 days as silent half-fills
            # masquerading as live spreads (caught 2026-05-10).
            broker_status = (getattr(order, "status", "") or "").lower()
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            if (broker_status in ("expired", "canceled", "rejected",
                                  "done_for_day")
                    and filled_qty == 0):
                conn.execute(
                    "UPDATE trades SET status = ?, price = 0 "
                    "WHERE id = ?",
                    (broker_status, trade["id"]),
                )
                terminal_unfilled += 1
                logging.warning(
                    "[%s] order %s ended %s with 0 filled qty — "
                    "row #%d marked status=%s",
                    seg_label, trade["order_id"], broker_status,
                    trade["id"], broker_status,
                )
                # Multileg partial-fill rollback. The sequential path
                # in execute_multileg_strategy only rolls back on
                # SUBMIT failure (immediate exception). When a leg
                # submits cleanly but later expires/cancels unfilled
                # while its partner leg fills, no rollback fires and
                # the AI's intended spread becomes a naked single-leg
                # position (caught 2026-05-10: 3 orphans on prod for
                # 2 days). Same opposite-side close logic the
                # existing submit-failure rollback uses, just
                # triggered by the late-arriving fill-failure signal.
                if (trade["signal_type"] == "MULTILEG"
                        and trade["option_strategy"]):
                    # Commit the just-applied terminal-status update
                    # before the rollback. log_trade opens a fresh
                    # SQLite connection; without this commit it would
                    # block on the outer transaction's pending writes
                    # and time out on busy_timeout. Per-row commits
                    # are correct here — each terminal-status pin is
                    # independently durable; we don't want the whole
                    # task's progress to depend on a single rollback
                    # succeeding.
                    conn.commit()
                    closed = _rollback_orphaned_multileg_partners(
                        conn, api, trade, seg_label, db_path,
                    )
                    orphan_rollbacks += closed
                continue
            if not order.filled_avg_price:
                continue
            # MULTILEG: if `order.legs[]` has a matching OCC, this is
            # the COMBO path — `order.filled_avg_price` is the SIGNED
            # NET PREMIUM (negative for credit spreads); per-leg
            # fills live on `combo.legs[i].filled_avg_price`
            # (positive). The May 11 `_log_strategy_legs` fix
            # correctly used `combo.legs[]` at entry-write time, but
            # THIS backfill path was missed and silently overwrote
            # the correctly-NULL'd per-leg prices with the combo's
            # negative net on the next cycle (caught 2026-05-16, 7+
            # rows on prod with price=-0.64 etc., invisible to
            # `get_virtual_positions`). If `order.legs[]` is empty,
            # this is the SEQUENTIAL path (each leg has its own
            # order) and `order.filled_avg_price` IS the per-leg
            # price — use it directly.
            if (trade["signal_type"] == "MULTILEG"
                    and trade["occ_symbol"]):
                leg_price = None
                combo_legs = list(getattr(order, "legs", None) or [])
                if combo_legs:
                    for cl in combo_legs:
                        if getattr(cl, "symbol", None) == trade["occ_symbol"]:
                            cl_fap = getattr(cl, "filled_avg_price", None)
                            if cl_fap is not None and float(cl_fap) > 0:
                                leg_price = float(cl_fap)
                            break
                    if leg_price is None:
                        # COMBO with no matching positive per-leg
                        # fill — skip; NEVER fall back to combo
                        # net for a per-leg write.
                        continue
                    fill = leg_price
                else:
                    # SEQUENTIAL path: order is for this leg only.
                    fill = float(order.filled_avg_price)
            else:
                fill = float(order.filled_avg_price)
            # Defense-in-depth: refuse to write a non-positive price
            # for any row. If we got here with a bad value, leaving
            # the column NULL preserves the recovery path.
            if fill <= 0:
                logging.warning(
                    "[%s] update_fills: refusing non-positive fill "
                    "%s on trade #%d (%s %s order=%s) — leaving NULL "
                    "for next cycle to backfill",
                    seg_label, fill, trade["id"], trade["signal_type"],
                    trade.get("occ_symbol") or trade["symbol"],
                    trade["order_id"],
                )
                continue
            dec = trade["decision_price"]
            if dec is not None and dec > 0:
                slip = round((fill - dec) / dec * 100, 4)
            else:
                slip = None
            # Populate `price` when missing so dashboards reading
            # `t.price` (e.g. trades ledger) stop showing "$--".
            if trade["price"] is None or trade["price"] <= 0:
                # Also overwrite an earlier non-positive `price` write
                # (the pre-2026-05-16 combo-net bug left -0.64 etc.).
                conn.execute(
                    "UPDATE trades "
                    "SET price = ?, fill_price = ?, slippage_pct = ? "
                    "WHERE id = ?",
                    (fill, fill, slip, trade["id"]),
                )
            else:
                conn.execute(
                    "UPDATE trades "
                    "SET fill_price = ?, slippage_pct = ? "
                    "WHERE id = ?",
                    (fill, slip, trade["id"]),
                )
            updated += 1

            # State-machine transition. If this row was a SELL/COVER
            # awaiting fill confirmation ('pending_fill'), the
            # fill_avg_price reply confirms the close happened.
            # Flip the SELL/COVER itself to 'closed', then FIFO-walk
            # the symbol's entries to close ONLY those whose lot has
            # been fully consumed by all closing-side trades.
            #
            # Pre-2026-05-18 this used a blanket
            # `UPDATE trades SET status='closed' WHERE symbol=? AND
            # side=opp_side AND status='open'` — which closed EVERY
            # open entry for the symbol regardless of qty. Hit live
            # on P12 (BuyHoldSPY) when a 16-share SPY SELL confirmed
            # and BOTH the 322-share BUY and the 16-share BUY got
            # flipped to closed even though 322 shares were still
            # legitimately held. Same pattern affects SHORT/COVER
            # pairs via the opp_side branch. The FIFO walk below
            # only closes lots whose remaining qty is 0 — partial
            # closes leave the lot 'open' with its qty unchanged
            # (read-time FIFO in get_virtual_positions correctly
            # computes the remaining qty without needing a status
            # flip).
            if (trade["status"] == "pending_fill"
                    and trade["side"] in ("sell", "cover")):
                conn.execute(
                    "UPDATE trades SET status = 'closed' WHERE id = ?",
                    (trade["id"],),
                )
                opp_side = "buy" if trade["side"] == "sell" else "short"
                exit_side = trade["side"]  # 'sell' or 'cover'
                rows = conn.execute(
                    "SELECT id, side, qty FROM trades "
                    "WHERE symbol = ? AND side IN (?, ?) "
                    "  AND COALESCE(status, 'open') != 'canceled' "
                    "ORDER BY timestamp ASC, id ASC",
                    (trade["symbol"], opp_side, exit_side),
                ).fetchall()
                # FIFO walk: entries open lots, exits consume them
                lots = []  # [trade_id, qty_remaining]
                for r in rows:
                    side_i = r[1]
                    qty_i = float(r[2] or 0)
                    if side_i == opp_side:
                        lots.append([r[0], qty_i])
                    else:  # exit side
                        remaining = qty_i
                        for lot in lots:
                            if remaining <= 0:
                                break
                            if lot[1] <= 0:
                                continue
                            consumed = min(lot[1], remaining)
                            lot[1] -= consumed
                            remaining -= consumed
                # Close lots whose remaining qty is 0 (within fp tolerance)
                for lot_id, lot_remaining in lots:
                    if lot_remaining <= 1e-6:
                        conn.execute(
                            "UPDATE trades SET status = 'closed' "
                            "WHERE id = ? AND COALESCE(status, 'open') = 'open'",
                            (lot_id,),
                        )
                confirmed_closes += 1
            elif (trade["status"] == "pending_fill"
                    and trade["occ_symbol"]):
                # Roll-manager / option-leg close path: flip to
                # 'closed' once confirmed; no opposite-side rows
                # to flip (option close stands alone). Gated on
                # occ_symbol presence so that BUY-side pending_fill
                # rows for stocks don't fall into this branch and
                # get wrongly closed on their first fill.
                # Caught 2026-05-18 17:28 ET when P12/P13/P14 day-1
                # BUYs flipped to closed the instant the broker
                # confirmed each fill — only the SELL/COVER branch
                # above is meant to flip status; for BUY pending_fill
                # the correct transition is to 'open' (handled by
                # the fill-price backfill above this block) so the
                # entry stays in get_virtual_positions.
                conn.execute(
                    "UPDATE trades SET status = 'closed' WHERE id = ?",
                    (trade["id"],),
                )
                confirmed_closes += 1
            elif trade["status"] == "pending_fill":
                # Stock BUY/SHORT pending_fill just got its fill
                # confirmed by Alpaca — transition pending_fill -> open
                # so it shows up as a held position in
                # get_virtual_positions. (Prior code wrongly closed
                # these.)
                conn.execute(
                    "UPDATE trades SET status = 'open' WHERE id = ?",
                    (trade["id"],),
                )

        conn.commit()

        if updated > 0 or terminal_unfilled > 0 or orphan_rollbacks > 0:
            parts = []
            if updated > 0:
                parts.append(f"{updated} fill(s) backfilled")
                if confirmed_closes:
                    parts.append(f"{confirmed_closes} closes confirmed")
            if terminal_unfilled > 0:
                parts.append(
                    f"{terminal_unfilled} terminal-unfilled marked"
                )
            if orphan_rollbacks > 0:
                parts.append(
                    f"{orphan_rollbacks} orphan multileg leg(s) closed"
                )
            logging.info(f"[{seg_label}] update_fills: " + "; ".join(parts))
    except Exception:
        logging.exception(f"[{seg_label}] Failed to update fill prices")
    finally:
        if conn is not None:
            conn.close()


def _rollback_orphaned_multileg_partners(conn, api, expired_leg,
                                         seg_label, db_path):
    """Close any sibling legs of a multileg combo whose partner just
    expired/canceled unfilled. Returns count of legs closed.

    Pairing rule mirrors how `_record_multileg_legs` writes legs:
    same `option_strategy` (combo name), same underlying `symbol`,
    timestamp within 60 seconds (legs are written milliseconds apart
    but allow slack for sequential submission). For each sibling
    that filled (`fill_price IS NOT NULL` AND `status='open'`), submit
    an opposite-side market close on its OCC, log the close as a new
    trade row, and flip the original entry row to status='closed'.

    Same opposite-side close pattern as the submit-failure rollback
    in `options_multileg.execute_multileg_strategy` — just triggered
    by the fill-failure signal that arrives later via Alpaca's order
    status. Without this, a half-filled spread (one leg filled, one
    expired) becomes a permanent naked single-leg position the AI
    didn't intend.
    """
    import sqlite3
    from journal import log_trade

    siblings = conn.execute(
        "SELECT id, order_id, side, qty, occ_symbol, price, "
        "       fill_price, ai_confidence, ai_reasoning, "
        "       option_strategy, expiry, strike, timestamp, "
        "       symbol "
        "FROM trades "
        "WHERE signal_type = 'MULTILEG' "
        "  AND option_strategy = ? "
        "  AND symbol = ? "
        "  AND id != ? "
        "  AND COALESCE(status, 'open') = 'open' "
        "  AND fill_price IS NOT NULL "
        "  AND ABS(strftime('%s', timestamp) - "
        "          strftime('%s', ?)) < 60",
        (expired_leg["option_strategy"], expired_leg["symbol"],
         expired_leg["id"], expired_leg["timestamp"]),
    ).fetchall()

    if not siblings:
        return 0

    closed = 0
    for sib in siblings:
        # Submit opposite-side market close on the sibling's OCC.
        # buy → sell, sell/short → buy. Mirrors _INTENT_CLOSE in
        # options_multileg.
        rev_side = "sell" if sib["side"] == "buy" else "buy"
        try:
            close_order = api.submit_order(
                symbol=sib["occ_symbol"],
                qty=int(sib["qty"]),
                side=rev_side,
                type="market",
                time_in_force="day",
            )
        except Exception as exc:
            logging.error(
                "[%s] CRITICAL: orphan-leg rollback FAILED for combo %s "
                "%s leg #%d (%s): %s. Position remains open.",
                seg_label, expired_leg["option_strategy"],
                expired_leg["symbol"], sib["id"], sib["occ_symbol"], exc,
            )
            continue

        close_order_id = getattr(close_order, "id", None)
        # Log the rollback close as a new trade row. fill_price
        # populates on a later _task_update_fills cycle. pnl filled
        # in by the same path (cost basis vs close price).
        try:
            log_trade(
                symbol=sib["symbol"],
                side=rev_side,
                qty=int(sib["qty"]),
                price=None,  # backfilled when broker confirms
                order_id=close_order_id,
                signal_type="MULTILEG",
                strategy=sib["option_strategy"],
                reason=(
                    f"Auto-rollback: combo {sib['option_strategy']} on "
                    f"{sib['symbol']} had partner leg expire unfilled "
                    f"(row #{expired_leg['id']}, order "
                    f"{expired_leg['order_id']}). Closing this filled "
                    f"leg to restore intended position state."
                ),
                ai_reasoning=sib["ai_reasoning"],
                ai_confidence=sib["ai_confidence"],
                occ_symbol=sib["occ_symbol"],
                option_strategy=sib["option_strategy"],
                expiry=sib["expiry"],
                strike=sib["strike"],
                status="pending_fill",
                db_path=db_path,
            )
        except Exception as exc:
            logging.error(
                "[%s] orphan-leg rollback close submitted (%s) but "
                "log_trade failed for sibling #%d: %s",
                seg_label, close_order_id, sib["id"], exc,
            )

        # Flip the originally-filled sibling row to closed so the
        # virtual book stops carrying it as open. Commit immediately
        # — log_trade above already opened/closed its own connection
        # so we're not holding a lock, and per-leg commits keep
        # rollback progress durable across iterations.
        conn.execute(
            "UPDATE trades SET status = 'closed' WHERE id = ?",
            (sib["id"],),
        )
        conn.commit()
        closed += 1
        logging.warning(
            "[%s] orphan-leg rollback: combo %s on %s — partner "
            "leg #%d expired (order %s), closed sibling leg #%d "
            "(%s qty=%d) via market %s order %s",
            seg_label, expired_leg["option_strategy"],
            expired_leg["symbol"], expired_leg["id"],
            expired_leg["order_id"], sib["id"], sib["occ_symbol"],
            int(sib["qty"]), rev_side, close_order_id,
        )

    return closed


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


# The advisory cost-alert fires at 80% of the user's actual daily
# ceiling — gives a heads-up before the cost_guard HARD BLOCK
# (`ai_providers._enforce_cost_cap`) starts rejecting AI calls.
# Removed the hard-coded $3 threshold (2026-05-15): it predated the
# user-settable cap on the settings page and contradicted whatever
# the user actually configured. Now reads from cost_guard so the
# advisory tracks the cap that's truly in effect.
_COST_ALERT_THRESHOLD_RATIO = 0.80
_cost_alerted_today = set()
# Per-account last-audit epoch. Used to throttle the cross-account
# reconcile to one run every _CROSS_RECONCILE_MIN_INTERVAL_SECONDS
# per Alpaca account so it doesn't fire on every profile in a
# snapshot pass, but DOES re-run on the next pass when drift
# persists (e.g., after-hours options-close rejection that should
# retry at market open). Replaces the old `set()` which dedup'd
# for the whole process lifetime and prevented retry.
_cross_reconcile_last_run: dict = {}
_CROSS_RECONCILE_MIN_INTERVAL_SECONDS = 300  # 5 minutes


def _task_cross_account_reconcile(ctx):
    """Compare sum of virtual positions against Alpaca's actual
    holdings, then auto-remediate any broker_orphan drift on OCC
    option contracts (broker holds more than the virtual book
    reflects).

    Per `feedback_ai_driven_no_manual_loop`: the audit alone is not
    the design. When drift is detected, the system either (a) closes
    the orphan contracts at the broker so the next audit pass
    clears, or (b) halts the responsible profile and surfaces a loud
    alert if the close fails. Asking the operator to log into the
    broker dashboard is the failure mode, not the design.

    Stock-side drift is not auto-remediated here — it's handled by
    `reconcile_journal_to_broker` which has the per-symbol context
    to attribute trades to the right profile.

    Runs once per Alpaca account per snapshot cycle.
    """
    import time as _time
    acct_id = getattr(ctx, "alpaca_account_id", None)
    if not acct_id:
        return
    last = _cross_reconcile_last_run.get(acct_id, 0.0)
    if (_time.time() - last) < _CROSS_RECONCILE_MIN_INTERVAL_SECONDS:
        return
    _cross_reconcile_last_run[acct_id] = _time.time()
    try:
        from virtual_audit import audit_cross_account
        from models import get_user_profiles
        profiles = get_user_profiles(ctx.user_id)
        pids = [p["id"] for p in profiles
                if p.get("enabled") and p.get("alpaca_account_id") == acct_id]
        if len(pids) < 2:
            return
        problems = audit_cross_account(acct_id, pids)
        if not problems:
            return

        # Surface the drift in the activity feed (operator visibility).
        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "cross_reconcile",
            "Cross-Account Drift: %d issue(s)" % len(problems),
            "\n".join("- %s" % p for p in problems),
        )

        # Auto-remediate option-contract orphans. The remediator
        # submits `sell_to_close` / `buy_to_close` for each orphan
        # OCC and journals each close atomically (halts the profile
        # if the journal write fails after the broker accepts the
        # close). Stock-side drift is skipped — handled by
        # reconcile_journal_to_broker which has per-symbol attribution.
        try:
            from auto_close_broker_orphans import remediate_account_drift
            results = remediate_account_drift(
                alpaca_account_id=acct_id,
                profile_ids=pids,
                problems=problems,
            )
            if results:
                summary_lines = [
                    f"- {r['occ_symbol']}: {r['action']} "
                    f"(diff={r['diff_qty']:.0f}, reason={r['reason']})"
                    for r in results
                ]
                _safe_log_activity(
                    getattr(ctx, "profile_id", 0), ctx.user_id,
                    "cross_reconcile_remediation",
                    "Auto-Close Orphan Contracts: %d action(s)"
                    % len(results),
                    "\n".join(summary_lines),
                )
        except Exception as exc:
            logging.exception(
                "Auto-close remediation failed for account %s: %s",
                acct_id, exc,
            )
    except Exception as exc:
        logging.warning("Cross-account reconcile failed: %s", exc)


def _task_cost_check(ctx):
    """Advisory: alert when daily AI spend reaches 80% of the user's
    cap. The HARD block at $cap is in `ai_providers._enforce_cost_cap`
    (raises `CostCapExceeded`); this is the early-warning so the user
    sees it coming before AI calls start being rejected.

    Threshold reads from `cost_guard.daily_ceiling_usd` so it tracks
    whatever the user actually has set (or the auto-computed default).
    """
    from ai_cost_ledger import spend_summary
    from cost_guard import daily_ceiling_usd
    pid = getattr(ctx, "profile_id", 0)
    if pid in _cost_alerted_today:
        return
    try:
        ceiling = daily_ceiling_usd(ctx.user_id)
        alert_at = ceiling * _COST_ALERT_THRESHOLD_RATIO
        summary = spend_summary(ctx.db_path)
        today_cost = summary["today"]["usd"]
        # Cheap pre-filter so we don't sum across every profile DB on
        # every scheduler iteration when nothing's close to the cap.
        if today_cost > alert_at / 10:
            import os, glob
            total = 0
            for f in glob.glob("quantopsai_profile_*.db"):
                s = spend_summary(f)
                total += s["today"]["usd"]
            if total > alert_at:
                _cost_alerted_today.add(pid)
                logging.warning(
                    "API cost alert: $%.2f today (%.0f%% of $%.2f cap)",
                    total,
                    (total / ceiling * 100) if ceiling > 0 else 0,
                    ceiling,
                )
                _safe_log_activity(
                    pid, ctx.user_id, "cost_alert",
                    "API Cost Alert: $%.2f today (%.0f%% of cap)" % (
                        total,
                        (total / ceiling * 100) if ceiling > 0 else 0,
                    ),
                    "Daily AI spend has reached %.0f%% of your "
                    "$%.2f daily cap. The hard cap will block new "
                    "AI calls at $%.2f. Raise the ceiling on the "
                    "settings page if this is intentional." % (
                        (total / ceiling * 100) if ceiling > 0 else 0,
                        ceiling, ceiling,
                    ),
                )
    except (sqlite3.OperationalError, sqlite3.DatabaseError,
            ImportError, KeyError, ValueError, AttributeError,
            TypeError, OSError) as _cost_exc:
        # Cost-alert notify is best-effort; delivery failure must
        # not break scheduler. Surface for follow-up so we don't
        # silently lose spend visibility.
        logger.warning(
            "cost-alert check failed for profile %s: %s: %s",
            pid, type(_cost_exc).__name__, _cost_exc,
        )


_health_probe_last_run = 0.0
_HEALTH_PROBE_INTERVAL_SEC = 600  # every 10 min


_auto_expiry_last_run_date = None
_trade_rate_anomaly_last_run_date = None


def _task_trade_rate_anomaly_check(ctx):
    """Item 5 of docs/17 Phase 1 — operator-visibility layer.

    Compares the last 7 days of stock entries vs the prior 7 days.
    Fires an `audit_alerts` row of type `trade_rate_anomaly` when
    current-week entries fall to <50% of prior-week entries (with a
    noise floor of >=5 prior-week entries). Per
    `feedback_ai_driven_no_manual_loop`, the tuner is NOT paused —
    the alert is purely informational.

    Runs once per profile per UTC calendar day; the underlying
    detection / write functions are idempotent so a duplicate run
    within the day refreshes the same audit_alerts row rather than
    creating new ones.

    Master-DB resolution mirrors `_task_auto_expire_gate_tightens`:
    use config.DB_PATH (the master DB the audit_alerts table lives
    in) regardless of the profile's per-profile DB.
    """
    global _trade_rate_anomaly_last_run_date
    import datetime as _dt
    today = _dt.date.today().isoformat()
    pid = getattr(ctx, "profile_id", 0)
    state_key = f"{today}:{pid}"
    if _trade_rate_anomaly_last_run_date == state_key:
        return
    _trade_rate_anomaly_last_run_date = state_key

    try:
        import config
        from trade_rate_anomaly import check_and_alert
        status = check_and_alert(
            profile_id=pid,
            profile_db_path=ctx.db_path,
            main_db_path=config.DB_PATH,
        )
        if status.get("fired"):
            seg = ctx.display_name or ctx.segment
            d = status["details"]
            logging.info(
                "[%s] Trade-rate anomaly: %d→%d entries (-%.0f%%) vs prior week",
                seg, d["prior_week_entries"], d["current_week_entries"],
                d["drop_pct"],
            )
        elif status.get("resolved"):
            seg = ctx.display_name or ctx.segment
            logging.info(
                "[%s] Trade-rate anomaly resolved (recovered)", seg,
            )
    except Exception as exc:
        logging.warning(
            "trade_rate_anomaly check failed for profile %s: %s: %s",
            pid, type(exc).__name__, exc,
        )


def _task_auto_expire_gate_tightens(ctx):
    """Daily auto-expiry of gate-tightening tuning changes.

    Rule (evidence-based, per Mack 2026-05-16):
      For each gate_tighten change >= 7 days old where no later
      change has already moved the parameter AND >= 20 predictions
      have resolved since the change AND outcome_after != 'improved'
      → revert the parameter and log an 'auto_expiry_revert' row.

    The fourth permanent guardrail from the 2026-05-14 over-
    restriction collapse memo. Prevents the slow-accumulation
    pattern (30+ 'unchanged' tightenings compounding to zero stock
    entries) that the existing auto_reversal can't catch (it only
    catches outright worseners).

    Runs once per profile per calendar day.
    """
    global _auto_expiry_last_run_date
    import datetime as _dt
    today = _dt.date.today().isoformat()
    pid = getattr(ctx, "profile_id", 0)
    state_key = f"{today}:{pid}"
    if _auto_expiry_last_run_date == state_key:
        return
    _auto_expiry_last_run_date = state_key

    try:
        from tuning_auto_expiry import revert_expired_gate_tightens
        actions = revert_expired_gate_tightens(
            profile_id=pid,
            user_id=getattr(ctx, "user_id", 1),
            profile_db_path=ctx.db_path,
        )
        reverted = [a for a in actions if a.get("action") == "reverted"]
        if reverted:
            seg_label = ctx.display_name or ctx.segment
            logging.info(
                "[%s] Auto-expiry reverted %d gate-tightening(s): %s",
                seg_label, len(reverted),
                ", ".join(r.get("parameter_name", "?") for r in reverted),
            )
    except Exception as exc:
        logging.warning(
            "auto_expiry task failed for profile %s: %s: %s",
            pid, type(exc).__name__, exc,
        )


def _task_data_source_health(ctx):
    """Probe every critical data source against a known-liquid symbol
    and alert if any source has silently degraded.

    Added 2026-05-15 after the master Alpaca key was discovered to
    have been revoked silently, causing every bar fetch system-wide
    to fall back to yfinance for an unknown period. The bar fetcher
    has a yfinance fallback by design; this probe ensures the
    fallback firing is LOUDLY surfaced, not silently masked by
    "predictions still get recorded, trades still fire" surface
    metrics.

    Runs every 10 minutes (cheap probes; the in-memory dedup in
    `alert_on_critical_failure` prevents spam)."""
    global _health_probe_last_run
    import time as _t
    now = _t.time()
    if now - _health_probe_last_run < _HEALTH_PROBE_INTERVAL_SEC:
        return
    _health_probe_last_run = now

    try:
        from data_source_health import run_all_probes, alert_on_critical_failure
        health = run_all_probes()
        if not health["all_critical_ok"]:
            alert_on_critical_failure(
                health,
                profile_id=getattr(ctx, "profile_id", 0),
                user_id=getattr(ctx, "user_id", 1),
            )
        else:
            logging.info(
                "Data source health: all critical OK "
                "(advisory failures: %s)",
                health.get("advisory_failures") or "none",
            )
    except Exception as exc:
        logging.warning("data_source_health probe crashed: %s", exc)


def _task_stat_arb_universe_scan(ctx):
    """Weekly universe scan to discover new cointegrated pairs.

    Sunday only, file-based idempotency marker per-profile. Quadratic
    scan is expensive (~25s for 100 symbols); the daily retest task
    handles the cheap incremental work in between.

    Symbol universe: this profile's segment universe (capped at
    `STAT_ARB_SCAN_SYMBOL_LIMIT` to keep wall time bounded). Pairs are
    persisted via `upsert_pair` so reruns don't duplicate.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo

    now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() != 6:  # Sunday only
        return

    profile_id = getattr(ctx, "profile_id", None)
    seg_label = ctx.display_name or ctx.segment
    today = now_et.strftime("%Y-%m-%d")
    marker = f".stat_arb_scan_done_p{profile_id or 'X'}.marker"

    try:
        with open(marker) as f:
            if f.read().strip() == today:
                logging.info(
                    f"[{seg_label}] Stat-arb universe scan already "
                    f"ran today — skipping.")
                return
    except FileNotFoundError:
        pass

    try:
        from segments import get_live_universe
        from market_data import get_bars
        from stat_arb_pair_book import scan_and_persist_pairs

        symbols = get_live_universe(ctx.segment, ctx=ctx) or []
        # Bound wall time: 60 symbols → 60·59/2 = 1770 EG tests, ~9s
        STAT_ARB_SCAN_SYMBOL_LIMIT = 60
        symbols = symbols[:STAT_ARB_SCAN_SYMBOL_LIMIT]

        def _ph(symbol):
            try:
                bars = get_bars(symbol, limit=200)
                if bars is None or len(bars) < 60:
                    return None
                return bars["close"].tolist()
            except Exception:
                return None

        result = scan_and_persist_pairs(
            ctx.db_path, symbols, price_history=_ph,
        )
        logging.info(
            f"[{seg_label}] Stat-arb universe scan: "
            f"scanned={result['scanned_symbols']}, "
            f"found={result['found']}, persisted={result['persisted']}"
        )
        # Idempotency marker
        try:
            with open(marker, "w") as f:
                f.write(today)
        except OSError as exc:
            logging.warning(
                f"[{seg_label}] Could not write stat-arb scan marker: {exc}")
    except Exception:
        logging.exception(
            f"[{seg_label}] Stat-arb universe scan failed")


def _task_stat_arb_retest(ctx):
    """Item 1b — daily retest of stat-arb pair book.

    For each active pair in this profile's book, re-run the EG
    cointegration test on fresh price data. Refresh hedge_ratio /
    p_value / half_life when still cointegrated; retire pair when
    p_value >= 0.10 (relationship broke) or other tradeability filters
    fail.

    No-op when the pair book is empty.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from stat_arb_pair_book import retest_active_pairs, get_active_pairs
        active = get_active_pairs(ctx.db_path)
        if not active:
            return  # nothing to retest

        from market_data import get_bars

        def _price_history(symbol):
            try:
                bars = get_bars(symbol, limit=200)
                if bars is None or len(bars) < 30:
                    return None
                return bars["close"].tolist()
            except Exception:
                return None

        result = retest_active_pairs(ctx.db_path, _price_history)
        logging.info(
            f"[{seg_label}] Stat-arb pair retest: "
            f"retested={result['retested']}, refreshed={result['refreshed']}, "
            f"retired={result['retired']}, errors={result['errors']}"
        )
        if result["retired"] > 0:
            for d in result["details"]:
                if d.get("outcome") == "retired":
                    logging.info(
                        f"[{seg_label}] Retired pair {d['pair']}: "
                        f"p={d['p_value']:.3f}, hl={d['half_life_days']:.1f}d"
                    )
    except Exception:
        logging.exception(f"[{seg_label}] Stat-arb pair retest failed")


_HALT_CACHE: Dict[str, Tuple[float, bool]] = {}
_HALT_CACHE_TTL = 15 * 60  # 15 minutes


def _compute_sector_moves():
    """Today's signed pct change per sector ETF.

    Returns {sector_name: pct_change_today} where pct_change is
    `(today_close - yesterday_close) / yesterday_close` (signed).
    Sectors whose 2-bar history is unavailable are silently omitted
    so we never fire a false alert from missing data.
    """
    from market_data import get_bars, SECTOR_ETFS
    moves = {}
    for sector, etf in SECTOR_ETFS.items():
        try:
            df = get_bars(etf, limit=2)
            if df is None or len(df) < 2:
                continue
            yest_close = float(df["close"].iloc[-2])
            today_close = float(df["close"].iloc[-1])
            if yest_close > 0:
                moves[sector] = (today_close - yest_close) / yest_close
        except Exception as exc:
            logging.warning(
                "intraday_risk: sector_moves fetch failed for %s (%s): %s",
                sector, etf, exc,
            )
    return moves


def _compute_halted_held_symbols(ctx):
    """Symbols where the user holds a position AND the underlying is
    halted/non-tradable on Alpaca.

    `tradable=False` is Alpaca's signal for halted/restricted/delisted
    assets — the same field already used in `client.py:318` for the
    shortable check. 15-min in-process cache to avoid hammering the
    asset endpoint every cycle. Fetch failures log a WARNING and
    return [] (NEVER an alert from a broken fetch).
    """
    import time as _time
    from client import get_api, get_positions
    halted = []
    try:
        positions = get_positions(ctx=ctx) or []
        api = get_api(ctx)
        now = _time.time()
        for p in positions:
            sym = p.get("symbol")
            if not sym:
                continue
            cached = _HALT_CACHE.get(sym)
            if cached and (now - cached[0]) < _HALT_CACHE_TTL:
                if cached[1]:
                    halted.append(sym)
                continue
            try:
                asset = api.get_asset(sym)
                tradable = bool(getattr(asset, "tradable", True))
                _HALT_CACHE[sym] = (now, not tradable)
                if not tradable:
                    halted.append(sym)
            except Exception as exc:
                logging.warning(
                    "intraday_risk: get_asset(%s) failed: %s", sym, exc,
                )
                # Don't cache failures — retry next cycle.
    except Exception as exc:
        logging.warning(
            "intraday_risk: halted-symbol enumeration failed: %s", exc,
        )
        return []
    return halted


def _task_intraday_risk_check(ctx):
    """Item 2b — intraday risk monitoring. Runs every cycle. Wires all
    four checks: drawdown acceleration, vol spike, sector concentration
    swing, and held-position halts. Writes a risk-halt state when
    alerts fire; the trade pipeline reads this state to block new
    entries.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from intraday_risk_monitor import (
            collect_intraday_alerts, aggregate_action,
            write_risk_halt_state, clear_risk_halt,
        )
        from market_data import get_bars

        # SPY for market-wide vol + drawdown signals
        spy_daily = get_bars("SPY", limit=10)
        if spy_daily is None or len(spy_daily) < 8:
            return

        # Today's intraday drawdown: today's high vs today's last
        # close. Falls back to 0 if today's bar isn't yet present.
        today_close = float(spy_daily["close"].iloc[-1])
        today_high = float(spy_daily["high"].iloc[-1])
        today_intraday_dd = ((today_high - today_close) / today_high
                              if today_high > 0 else 0)

        # 7-day avg of daily intraday drawdowns
        last_7 = spy_daily.tail(8).iloc[:-1]  # exclude today
        if len(last_7) >= 5:
            dds = []
            for _, row in last_7.iterrows():
                h, c = float(row["high"]), float(row["close"])
                if h > 0:
                    dds.append((h - c) / h)
            avg_7d_dd = sum(dds) / len(dds) if dds else 0
        else:
            avg_7d_dd = 0

        # Vol spike: SPY 1-hour realized vol vs 20-day average
        # Approximation: use last bar's intraday range / close as
        # "current hourly vol", and 20-day average of same metric.
        current_hourly_vol = 0.0
        avg_20d_hourly_vol = 0.0
        try:
            spy_20 = get_bars("SPY", limit=22)
            if spy_20 is not None and len(spy_20) >= 20:
                ranges = []
                for _, row in spy_20.tail(20).iterrows():
                    c = float(row["close"])
                    if c > 0:
                        ranges.append((float(row["high"]) - float(row["low"])) / c)
                if ranges:
                    avg_20d_hourly_vol = sum(ranges) / len(ranges)
                    current_hourly_vol = ranges[-1] if ranges else 0
        except (KeyError, ValueError, AttributeError, TypeError,
                ZeroDivisionError, OSError) as _atr_exc:
            # ATR vol calc is enrichment for the brief; report
            # continues without it. Surface for follow-up.
            logger.debug(
                "intraday-risk ATR vol calc failed: %s: %s",
                type(_atr_exc).__name__, _atr_exc,
            )

        sector_moves = _compute_sector_moves()
        halted_held_symbols = _compute_halted_held_symbols(ctx)

        alerts = collect_intraday_alerts(
            today_intraday_pct=today_intraday_dd,
            avg_7d_intraday_pct=avg_7d_dd,
            current_hourly_vol=current_hourly_vol,
            avg_20d_hourly_vol=avg_20d_hourly_vol,
            sector_moves=sector_moves,
            halted_held_symbols=halted_held_symbols,
        )
        action = aggregate_action(alerts)

        if alerts:
            logging.info(
                f"[{seg_label}] Intraday risk: action={action}, "
                f"alerts={[a.check_name for a in alerts]}"
            )
            write_risk_halt_state(ctx.db_path, action, alerts)
        else:
            clear_risk_halt(ctx.db_path)
    except Exception:
        logging.exception(f"[{seg_label}] Intraday risk check failed")


def _task_options_delta_hedger(ctx):
    """Phase D1 — dynamic delta hedging for long-vol option positions.

    Runs every check_exits cycle. Cheap: no-op when no hedgeable
    options are open. Submits stock-side rebalance orders only when
    delta drift exceeds the threshold to avoid churning.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from options_delta_hedger import rebalance_hedges
        from client import get_api, get_positions
        api = get_api(ctx)
        positions = get_positions(ctx=ctx) or []

        from market_data import get_bars
        def _price(sym):
            try:
                bars = get_bars(sym, limit=2)
                if bars is not None and len(bars) > 0:
                    return float(bars["close"].iloc[-1])
            except (KeyError, ValueError, AttributeError, TypeError,
                    IndexError, OSError) as _pp_exc:
                # Per-symbol price fetch fallback for delta hedger;
                # caller handles None price.
                logger.debug(
                    "delta-hedger price fetch failed for %s: %s: %s",
                    sym, type(_pp_exc).__name__, _pp_exc,
                )
            return None

        result = rebalance_hedges(
            api, db_path=ctx.db_path, positions=positions,
            price_lookup=_price,
            # IV lookup intentionally None → falls back to FALLBACK_IV
            # (25%). For higher accuracy we'd plumb the options oracle;
            # the hedger's role is direction not magnitude perfection.
            iv_lookup=lambda s: None,
        )
        if result["rebalanced"] > 0 or result["errors"] > 0:
            logging.info(
                f"[{seg_label}] Delta hedger: "
                f"evaluated={result['evaluated']}, "
                f"rebalanced={result['rebalanced']}, "
                f"errors={result['errors']}"
            )
    except Exception:
        logging.exception(f"[{seg_label}] Delta hedger failed")


def _task_manage_long_vol_hedge(ctx):
    """Item 1c — open / roll / close the long-vol portfolio hedge.

    Each cycle:
      1. Read drawdown / crisis level / latest 95% VaR.
      2. evaluate_triggers → list of HedgeTrigger.
      3. If no active hedge AND any trigger fired → open one.
      4. If active hedge AND should_close → close.
      5. If active hedge AND should_roll → close + open at new strike.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from datetime import date, datetime
        import json as _json
        import long_vol_hedge as lvh
        from client import get_api, get_account_info
        from crisis_state import get_current_level
        from options_chain_alpaca import fetch_chain_alpaca
        from options_trader import (
            build_long_put, format_occ_symbol, submit_option_order,
        )

        account = get_account_info(ctx=ctx) or {}
        equity = float(account.get("equity") or 0)
        if equity <= 0:
            return

        # ── Triggers ──────────────────────────────────────────────
        drawdown_pct = lvh.compute_drawdown_from_30d_peak(
            ctx.db_path, equity,
        )
        try:
            crisis_info = get_current_level(ctx.db_path) or {}
            crisis_level = crisis_info.get("level", "normal")
        except Exception:
            crisis_level = "normal"

        # Latest VaR snapshot (Item 2a) if available
        var_pct = None
        try:
            from journal import _get_conn as _gc
            _conn = _gc(ctx.db_path)
            row = _conn.execute(
                "SELECT var_95_dollars, equity FROM portfolio_risk_snapshots "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            _conn.close()
            if row and row["equity"] and row["equity"] > 0:
                var_pct = float(row["var_95_dollars"] or 0) / float(row["equity"])
        except Exception:
            var_pct = None

        triggers = lvh.evaluate_triggers(
            drawdown_pct=drawdown_pct,
            crisis_level=crisis_level,
            var_95_pct_of_equity=var_pct,
            drawdown_trigger=getattr(
                ctx, "long_vol_hedge_drawdown_pct",
                lvh.DEFAULT_DRAWDOWN_TRIGGER),
            var_trigger=getattr(
                ctx, "long_vol_hedge_var_pct",
                lvh.DEFAULT_VAR_TRIGGER),
        )

        # ── Decide ───────────────────────────────────────────────
        active = lvh.get_active_hedge(ctx.db_path)
        api = get_api(ctx)

        def _close_hedge(active_row, reason):
            """Submit a sell-to-close on the option, mark closed."""
            order_id = submit_option_order(
                api, active_row["occ_symbol"], side="sell",
                qty=int(active_row["contracts"]),
                position_intent="sell_to_close",
            )
            # Best-effort fill price for P&L (broker price may not be
            # available immediately; settle for None and let the next
            # cycle resolve)
            close_premium = None
            close_pnl = None
            try:
                pos = api.get_position(active_row["occ_symbol"])
                close_premium = float(getattr(pos, "current_price", 0) or 0)
                if close_premium and active_row.get("entry_premium"):
                    close_pnl = (
                        (close_premium - float(active_row["entry_premium"]))
                        * 100 * int(active_row["contracts"])
                    )
            except (AttributeError, ValueError, TypeError, OSError) as _cp_exc:
                # close-pnl annotation; record_hedge_closed below
                # proceeds without it. Surface for follow-up.
                logger.debug(
                    "long-vol hedge close-pnl annotation failed: %s: %s",
                    type(_cp_exc).__name__, _cp_exc,
                )
            lvh.record_hedge_closed(
                ctx.db_path, int(active_row["id"]), reason,
                close_premium=close_premium,
                close_pnl_dollars=close_pnl,
                close_order_id=order_id,
            )
            logging.info(
                f"[{seg_label}] Long-vol hedge CLOSED: "
                f"{active_row['occ_symbol']} ({reason})"
            )

        def _open_hedge():
            """Pick strike + expiry, fetch SPY chain, submit BTO order."""
            chain = fetch_chain_alpaca(lvh.HEDGE_UNDERLYING)
            if not chain:
                logging.warning(
                    f"[{seg_label}] Long-vol hedge: SPY chain unavailable"
                )
                return
            spot = float(chain.get("current_price") or 0)
            if spot <= 0:
                return
            target_strike = lvh.select_hedge_strike(spot)
            target_expiry = lvh.select_hedge_expiry()

            # Snap to closest available expiry
            available_exps = chain.get("expirations") or []
            if not available_exps:
                return
            picked_exp_iso = min(
                available_exps,
                key=lambda d: abs(
                    (date.fromisoformat(d) - target_expiry).days
                ),
            )
            picked_exp = date.fromisoformat(picked_exp_iso)

            # Find the matching puts DataFrame and snap to closest strike
            puts_df = None
            for c in chain.get("chains") or []:
                if c.get("expiration") == picked_exp_iso:
                    puts_df = c.get("puts")
                    break
            if puts_df is None or puts_df.empty:
                logging.warning(
                    f"[{seg_label}] Long-vol hedge: no puts for "
                    f"{picked_exp_iso}"
                )
                return
            puts_df = puts_df.copy()
            puts_df["strike_dist"] = (puts_df["strike"] - target_strike).abs()
            best = puts_df.sort_values("strike_dist").iloc[0]
            strike = float(best["strike"])
            entry_premium = float(best.get("ask", 0) or best.get("last", 0) or 0)
            if entry_premium <= 0:
                logging.warning(
                    f"[{seg_label}] Long-vol hedge: no premium quote on "
                    f"target strike — skipping this cycle"
                )
                return
            entry_delta = float(best.get("delta", 0) or 0) or None

            contracts = lvh.size_hedge_contracts(
                equity, entry_premium,
                premium_budget_pct=getattr(
                    ctx, "long_vol_hedge_premium_pct",
                    lvh.DEFAULT_PREMIUM_PCT),
            )
            if contracts <= 0:
                logging.info(
                    f"[{seg_label}] Long-vol hedge: budget too small for "
                    f"any contracts at premium ${entry_premium:.2f} "
                    f"(equity ${equity:,.0f})"
                )
                return

            occ = format_occ_symbol(
                lvh.HEDGE_UNDERLYING, picked_exp, strike, "P",
            )
            order_id = submit_option_order(
                api, occ, side="buy", qty=contracts,
            )
            if order_id is None:
                logging.warning(
                    f"[{seg_label}] Long-vol hedge: order submission failed"
                )
                return

            spec = {
                "occ_symbol": occ,
                "underlying": lvh.HEDGE_UNDERLYING,
                "strike": strike,
                "expiry": picked_exp_iso,
                "contracts": contracts,
                "entry_premium": entry_premium,
                "entry_spot": spot,
                "entry_delta": entry_delta,
            }
            row_id = lvh.record_hedge_opened(
                ctx.db_path, spec, triggers, order_id=order_id,
            )
            fired = [t.name for t in triggers if t.fired]
            logging.info(
                f"[{seg_label}] Long-vol hedge OPENED #{row_id}: "
                f"{contracts}x SPY {strike}P {picked_exp_iso} @ "
                f"${entry_premium:.2f} (triggers: {','.join(fired)})"
            )
            _safe_log_activity(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "long_vol_hedge",
                f"Long-Vol Hedge Opened: {contracts}x SPY {strike}P "
                f"{picked_exp_iso}",
                f"Premium ${entry_premium:.2f}/contract, total cost "
                f"${entry_premium * 100 * contracts:,.0f}. "
                f"Triggers fired: {', '.join(fired)}.",
            )

        # ── Apply decision ───────────────────────────────────────
        if active:
            # Check roll first, then close. Roll = close-then-open.
            try:
                expiry = date.fromisoformat(active["expiry"])
            except Exception:
                expiry = date.today() + timedelta(days=30)
            # Best-effort current delta (broker doesn't expose option
            # delta directly; we'd need the snapshot. Fall back to None.)
            current_delta = None
            roll_reason = lvh.should_roll(expiry, current_delta)
            close_reason = lvh.should_close(triggers)
            if roll_reason:
                _close_hedge(active, f"roll: {roll_reason}")
                if lvh.any_trigger_fired(triggers):
                    _open_hedge()
            elif close_reason:
                _close_hedge(active, close_reason)
            else:
                logging.debug(
                    f"[{seg_label}] Long-vol hedge: holding active "
                    f"hedge {active['occ_symbol']}"
                )
        else:
            if lvh.any_trigger_fired(triggers):
                _open_hedge()
    except Exception:
        logging.exception(f"[{seg_label}] Long-vol hedge task failed")


def _task_options_roll_manager(ctx):
    """Phase C1 — daily roll-manager pass for near-expiry options.

    Auto-closes credit positions at ≥80% of max profit. Surface roll
    candidates to the AI via the next batch prompt. Cheap when no
    near-expiry options are open.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from options_roll_manager import auto_close_high_profit_credits
        from client import get_api
        api = get_api(ctx)

        # Quote lookup: try the broker's options chain first, fall
        # back to the option's last fill price (stale but better than
        # nothing). Best-effort.
        def _quote_lookup(occ_symbol):
            try:
                pos = api.get_position(occ_symbol)
                cur = float(getattr(pos, "current_price", 0) or 0)
                if cur > 0:
                    return cur
            except (AttributeError, ValueError, TypeError, OSError) as _ql_exc:
                # Per-position broker fetch fallback; caller handles
                # None price. Surface for follow-up.
                logger.debug(
                    "options-roll quote lookup failed for %s: %s: %s",
                    occ_symbol, type(_ql_exc).__name__, _ql_exc,
                )
            return None

        result = auto_close_high_profit_credits(
            api, db_path=ctx.db_path, quote_lookup=_quote_lookup,
            window_days=getattr(ctx, "options_roll_window_days", 7),
            auto_close_profit_pct=getattr(
                ctx, "options_auto_close_profit_pct", 0.80),
            roll_recommend_profit_pct=getattr(
                ctx, "options_roll_recommend_profit_pct", 0.50),
        )
        if result["evaluated"]:
            logging.info(
                f"[{seg_label}] Roll manager: "
                f"evaluated={result['evaluated']}, "
                f"auto_closed={result['auto_closed']}, "
                f"errors={result['errors']}"
            )
    except Exception:
        logging.exception(f"[{seg_label}] Roll manager failed")


def _task_options_lifecycle(ctx):
    """Sweep expired option contracts. Closes worthless rows with
    realized P&L, flags assignment cases for manual review.

    Cheap when there are no open option trades — query is bounded by
    `signal_type='OPTIONS' AND status='open' AND expiry < today`.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from options_lifecycle import sweep_expired_options
        from client import get_api
        api = get_api(ctx)
        result = sweep_expired_options(api, db_path=ctx.db_path)
        if result["expired_found"]:
            logging.info(
                f"[{seg_label}] Options lifecycle: "
                f"found={result['expired_found']}, "
                f"closed_worthless={result['closed_worthless']}, "
                f"assignment_flagged={result['assignment_flagged']}, "
                f"errors={result['errors']}"
            )
    except Exception:
        logging.exception(f"[{seg_label}] Options lifecycle sweep failed")


def _task_capture_broker_activities(ctx):
    """Pull DIV/OPEXP/OPASN/OPXRC activities from Alpaca and write
    matching journal rows (#168). Idempotent via Alpaca activity
    id == trades.order_id."""
    seg_label = ctx.display_name or ctx.segment
    try:
        from activities_capture import capture_activities_for_profile
        summary = capture_activities_for_profile(ctx)
        wrote = sum(summary.values())
        if wrote:
            logging.info(
                "[%s] activities captured: %s",
                seg_label,
                ", ".join(f"{k}={v}" for k, v in summary.items() if v),
            )
    except Exception:
        logging.exception(
            "[%s] capture_broker_activities failed", seg_label,
        )


def _task_reconcile_trade_statuses(ctx):
    """Periodically reconcile trades.status against broker truth.

    Earlier implementation skipped the broker check for virtual
    profiles and read open_symbols from the journal itself — making
    the reconcile circular and unable to detect drift. Result observed
    2026-05-06: 40/126 (31%) "open" journal entries across 11 profiles
    were phantoms — entry orders that never filled, or BUYs whose
    protective stops fired at the broker without a SELL row landing
    in the journal.

    The broker-aware reconcile (`reconcile_journal_to_broker`) handles
    both classes:
      - cancel-without-fill: entry order_id is canceled/expired/
        rejected with filled_qty=0 → mark journal status='canceled'.
      - broker-sold-via-stop: entry filled but no current shares →
        find matching broker SELL fill, INSERT a SELL row from the
        broker fill, mark BUY status='closed', let the FIFO P&L pass
        backfill realized P&L.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from reconcile_journal_to_broker import (
            reconcile_with_ctx, _all_journal_sell_order_ids,
        )
        from models import get_active_profile_ids
        # Cross-profile dedup so the fallback match path doesn't
        # attribute one broker fill to multiple profiles. Dynamic
        # lookup of active profile IDs — the hardcoded range(1, 12)
        # this replaced silently excluded experiment profiles 12-24,
        # which caused the reconciler to interpret manual_cleanup
        # SELLs as unmatched broker exits and insert phantom
        # duplicate SELL rows (caught 2026-05-18 in P12 id=10/11
        # and P13 id=36/40, id=37/41).
        cross_used = _all_journal_sell_order_ids(get_active_profile_ids())
        result = reconcile_with_ctx(ctx, apply_changes=True,
                                    cross_profile_used_ids=cross_used)
        n_cancel = len(result.get("cancel", []))
        n_backfill = len(result.get("backfill_sell", []))
        n_amb = len(result.get("ambiguous", []))
        if n_cancel or n_backfill:
            logging.info(
                f"[{seg_label}] Reconciled journal-to-broker: "
                f"{n_cancel} canceled, {n_backfill} backfilled SELLs, "
                f"{n_amb} ambiguous"
            )
        if n_amb:
            logging.warning(
                f"[{seg_label}] {n_amb} ambiguous journal entries: "
                f"{[a.get('symbol') + '#' + str(a.get('trade_id')) for a in result['ambiguous'][:5]]}"
            )
    except Exception:
        logging.exception(f"[{seg_label}] Reconcile trade statuses failed")

    # 2026-05-21 — order_id-keyed protective-order invariant. Verifies
    # every protective order_id this profile's journal records as
    # active is actually live at Alpaca. Catches stale-linkage drift
    # (the FCX-class bug: journal points at a protective order that
    # fired/canceled or drifted, so ensure_protective_stops keeps
    # re-attempting an already-protected position). Logged, not
    # halting — stale linkage isn't a trading-safety issue like an
    # orphan FILL; the next ensure_protective_stops sweep self-heals
    # the pointer against broker truth. The WARNING makes "journal ==
    # Alpaca" a CHECKED property instead of a hope.
    try:
        from bracket_orders import verify_protective_order_sync
        from client import get_api as _get_api_sync
        sync = verify_protective_order_sync(
            _get_api_sync(ctx), ctx.db_path)
        if sync["stale"]:
            logging.warning(
                "[%s] Protective-order linkage drift: %d journal "
                "pointer(s) name an order not live at Alpaca "
                "(self-heals next sweep): %s",
                seg_label, len(sync["stale"]),
                [f"{s['symbol']}:{(s['order_id'] or '')[:8]}"
                 for s in sync["stale"][:5]],
            )
    except Exception as _sync_exc:
        logging.warning(
            "[%s] protective-order sync check failed (%s: %s)",
            seg_label, type(_sync_exc).__name__, _sync_exc,
        )

    # 2026-06-04 — PROACTIVE chain-walk sweep. Closes gap #3 from the
    # post-reset orphan-prevention list. For each pending_protective
    # row, advance its order_id through Alpaca's replace chain so the
    # journal's recorded id stays within 1-2 hops of the live id. The
    # fill-time chain walk in _detect_protective_fill then has a
    # near-trivial chain to traverse, keeping max_depth headroom huge.
    try:
        from bracket_orders import sync_pending_protective_order_ids
        from client import get_api as _get_api_csync
        csync = sync_pending_protective_order_ids(
            _get_api_csync(ctx), ctx.db_path)
        if csync["advanced"] or csync["marked_canceled"] or csync["errored"]:
            logging.info(
                "[%s] Pending-protective chain sync: "
                "checked=%d advanced=%d canceled=%d errored=%d",
                seg_label, csync["checked"], csync["advanced"],
                csync["marked_canceled"], csync["errored"],
            )
    except Exception as _csync_exc:
        logging.warning(
            "[%s] pending-protective chain sync failed (%s: %s)",
            seg_label, type(_csync_exc).__name__, _csync_exc,
        )

    # Aggregate audit — defense-in-depth alongside the per-profile
    # reconcile. Compares sum(virtual_positions across profiles routing
    # to the same Alpaca account) vs broker.list_positions for that
    # account. Catches drift the per-profile reconcile or pre-trade
    # guard might miss (manual broker actions, future code paths that
    # forget the guard, race conditions). Run once per orchestrator
    # cycle, not per-profile, so we don't duplicate work — gated to
    # only run when ctx is the FIRST active profile in the iteration
    # order. The hardcoded `profile_id == 1` gate this replaced never
    # fired for the experiment profiles (the lowest active id is 12),
    # so aggregate drift detection was silently disabled and never
    # would have caught today's reconcile bug (caught 2026-05-18).
    from models import get_active_profile_ids
    _first_active = next(iter(get_active_profile_ids()), None)
    if (_first_active is not None
            and getattr(ctx, "profile_id", None) == _first_active):
        try:
            from aggregate_audit import audit_aggregate_drift, format_drift_summary
            audit = audit_aggregate_drift(profile_ids=get_active_profile_ids())
            if audit.get("drift"):
                logging.error(
                    "AGGREGATE AUDIT DRIFT DETECTED:\n%s",
                    format_drift_summary(audit),
                )
                try:
                    from notifications import notify_error
                    notify_error(
                        error_msg=format_drift_summary(audit),
                        context="aggregate journal-vs-broker drift",
                    )
                except (ImportError, AttributeError, OSError) as _ne_exc:
                    # Drift-alert notify is best-effort; alert
                    # delivery failure must not break audit. Surface
                    # for follow-up so we don't quietly lose alerts.
                    logger.warning(
                        "drift-alert notify_error delivery failed: %s: %s",
                        type(_ne_exc).__name__, _ne_exc,
                    )
        except Exception:
            logging.exception("Aggregate audit failed")

        # 2026-06-04 — Manual broker-side order audit (D). Closes the
        # last orphan path the codebase contracts don't cover: orders
        # placed at the broker via Alpaca.com UI, external scripts,
        # or any path that doesn't go through submit_order in this
        # system. Diffs live broker orders against the union of every
        # profile's journaled order_ids per Alpaca account; anything
        # left is a manual order. Logs ERROR + sends email alert on
        # detection (low-frequency, high-signal — manual broker
        # activity is rare and always worth surfacing).
        try:
            from aggregate_audit import (
                audit_manual_broker_orders, format_manual_orders_summary,
            )
            manual_audit = audit_manual_broker_orders(
                profile_ids=get_active_profile_ids())
            if manual_audit.get("manual"):
                summary = format_manual_orders_summary(manual_audit)
                logging.error("MANUAL BROKER ORDER DETECTED:\n%s", summary)
                try:
                    from notifications import notify_error
                    notify_error(
                        error_msg=summary,
                        context=(
                            "manual broker-side order — placed outside "
                            "this system (Alpaca.com UI / external "
                            "tool) so it bypasses every contract that "
                            "prevents orphans for system-placed orders"
                        ),
                    )
                except (ImportError, AttributeError, OSError) as _ne_exc:
                    logger.warning(
                        "manual-order-alert notify_error delivery "
                        "failed: %s: %s",
                        type(_ne_exc).__name__, _ne_exc,
                    )
        except Exception:
            logging.exception("Manual-order audit failed")


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


def _task_check_stop_coverage(ctx):
    """Doomsday: alert when fewer than 80% of open longs have a
    broker protective stop. Logs naked symbols. Optional auto-kill
    on extended breach via ctx.auto_kill_on_stop_coverage."""
    try:
        from stop_coverage import check_coverage_floor
        floor = float(getattr(ctx, "stop_coverage_floor_pct", 80.0))
        snap = check_coverage_floor(floor_pct=floor)
        if snap["total_longs"] == 0:
            return
        msg = (
            f"Stop coverage: {snap['covered']}/{snap['total_longs']} "
            f"({snap['coverage_pct']}%) — floor {floor}%"
        )
        if snap["breached"]:
            naked_preview = ", ".join(
                f"profile_{p}:{s}"
                for p, s in snap["naked_symbols"][:5]
            )
            logging.warning(
                "STOP COVERAGE BREACH — %s. Naked: %s%s",
                msg, naked_preview,
                "..." if len(snap["naked_symbols"]) > 5 else "",
            )
            if getattr(ctx, "auto_kill_on_stop_coverage", False):
                try:
                    from kill_switch import activate
                    activate(
                        f"auto: stop coverage "
                        f"{snap['coverage_pct']}% below floor {floor}%",
                        set_by="auto_stop_coverage",
                    )
                except Exception as exc:
                    logging.debug("auto-kill from stop coverage: %s", exc)
        else:
            logging.info(msg)
    except Exception as exc:
        logging.warning("Stop coverage check failed: %s", exc)


def _task_check_position_runaway(ctx):
    """Doomsday: per-profile sentinel for duplicate-submit bugs and
    excessive single-trade qty. Alerts only — by the time we see
    these, the trade has already filled."""
    try:
        from position_runaway import runaway_snapshot
        snap = runaway_snapshot(ctx.db_path)
        seg = ctx.display_name or ctx.segment or "?"
        for d in snap.get("duplicate_buys", []):
            logging.warning(
                "[%s] DUPLICATE OPEN BUYS for %s: %d open rows, "
                "total qty %.2f",
                seg, d["symbol"], d["count"], d["total_qty"],
            )
        for e in snap.get("excessive_qty", []):
            logging.warning(
                "[%s] EXCESSIVE QTY trade #%d %s: qty=%.2f, "
                "%.1fx median (median=%.2f)",
                seg, e["trade_id"], e["symbol"], e["qty"],
                e["multiple"], e["median"],
            )
    except Exception as exc:
        logging.warning("Position runaway check failed: %s", exc)


def _task_check_ai_consistency(ctx):
    """Doomsday: alert when recent-100-resolved win rate drops below
    floor for N consecutive cycles. Captures 'model is broken'
    before the daily-loss floor catches 'book is bleeding'."""
    try:
        from ai_consistency_floor import check_floor
        floor = float(getattr(ctx, "ai_consistency_floor_pct", 30.0))
        consec = int(getattr(ctx, "ai_consistency_consec_cycles", 5))
        seg = ctx.display_name or ctx.segment or "?"
        info = check_floor(
            ctx.db_path, profile_label=seg,
            floor_pct=floor, consecutive_required=consec,
        )
        if info["alert_now"]:
            logging.error(
                "[%s] AI CONSISTENCY FLOOR BREACHED — win rate "
                "%.1f%% (n=%d) below %.1f%% for %d consecutive checks",
                seg, info["win_rate_pct"], info["n_resolved"],
                floor, info["consecutive"],
            )
            if getattr(ctx, "auto_kill_on_consistency_floor", False):
                try:
                    from kill_switch import activate
                    activate(
                        f"auto: AI win rate {info['win_rate_pct']}% "
                        f"below floor {floor}% for {info['consecutive']} cycles",
                        set_by="auto_consistency_floor",
                    )
                except Exception as exc:
                    logging.debug(
                        "auto-kill from consistency floor: %s", exc,
                    )
    except Exception as exc:
        logging.warning("AI consistency check failed: %s", exc)


def _task_check_book_loss_floor(ctx):
    """Doomsday gate: if cumulative book-wide day-of P&L drops below
    the configured floor, auto-activate the master kill switch.

    Runs once per profile per cycle (cheap — a few SELECTs per profile
    DB), but is fully idempotent: every profile that runs computes the
    same answer, and `kill_switch.activate()` only writes a history
    row on transitions or reason changes."""
    try:
        import os as _os
        import glob as _glob
        from kill_switch import (
            check_and_activate_on_loss_floor, is_active,
        )
        # Floor — default -8.0%; per-user override via ctx if ever
        # exposed in settings.
        floor = float(getattr(ctx, "book_loss_floor_pct", -8.0))
        # All per-profile DBs in /opt/quantopsai (or local equivalent).
        repo_root = _os.path.dirname(_os.path.abspath(__file__))
        candidates = (
            _glob.glob(_os.path.join(repo_root, "quantopsai_profile_*.db"))
        )
        # Skip if switch is already on — nothing to compute, just
        # log periodically that the auto-gate is still in effect.
        already_on, reason = is_active()
        if already_on:
            logging.info(
                "Kill switch is ACTIVE — reason: %s", reason,
            )
            return
        pnl_pct = check_and_activate_on_loss_floor(candidates, floor_pct=floor)
        if pnl_pct is None:
            logging.debug("Book loss floor: not enough snapshot data yet")
        else:
            logging.info(
                "Book day P&L: %.2f%% (floor %.2f%%)",
                pnl_pct, floor,
            )
    except Exception as exc:
        logging.warning("Book loss floor check failed: %s", exc)


def _task_resolve_predictions(ctx):
    """Resolve outstanding AI predictions against actual prices, then
    fill in any multi-horizon outcome rows whose horizon has elapsed.

    The horizon-measurement pass (#185, 2026-05-20) is intentionally
    bundled into the same task as resolve_predictions because they
    share the same data dependency (recent predictions + price bars)
    and the same cadence makes sense (a horizon row should appear as
    soon as the horizon elapses, so the self-tuner can use it on the
    next cycle). Idempotent via the UNIQUE (prediction_id,
    horizon_days) constraint — a re-run does nothing for already-
    filled rows.
    """
    from ai_tracker import resolve_predictions, measure_horizon_outcomes
    from client import get_api

    api = get_api(ctx)
    resolve_predictions(
        api=api, db_path=ctx.db_path,
        profile_id=getattr(ctx, "profile_id", None),
    )
    logging.info("AI predictions resolved.")
    try:
        n_horizons = measure_horizon_outcomes(
            api=api, db_path=ctx.db_path,
        )
        if n_horizons:
            logging.info(
                "Horizon-outcomes written this cycle: %d", n_horizons,
            )
    except Exception as exc:
        # Per feedback_no_silent_failures: surface and continue.
        # The horizon-measurement pass is additive (new dataset
        # rows for fine-tuning); a failure here must not block the
        # primary resolver that's already run successfully above.
        logging.warning(
            "measure_horizon_outcomes failed (%s: %s) — resolver "
            "completed normally; horizon rows for this cycle will "
            "be backfilled on the next pass via the UNIQUE-constraint "
            "idempotency.",
            type(exc).__name__, exc,
        )


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
    # Use ET-localized "today" to match what log_daily_snapshot writes —
    # the droplet runs in UTC, so date.today() would roll into the next
    # calendar day at midnight UTC (~8pm ET) and disagree with the
    # snapshot's recorded date.
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo
    today_str = _dt.now(_ZoneInfo("America/New_York")).date().isoformat()
    prior_equity = None
    try:
        conn = _sqlite3.connect(ctx.db_path)
        row = conn.execute(
            "SELECT equity FROM daily_snapshots "
            "WHERE date < ? "
            "ORDER BY date DESC, rowid DESC LIMIT 1",
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
            with closing(_get_conn(ctx.db_path)) as _c:
                wr, n_resolved = _get_current_win_rate(_c)
            from models import log_tuning_change, _get_conn as _get_main_conn
            summary = f"Evaluated {state['resolved']} predictions, win rate {wr:.0f}% — no changes needed"
            row_id = log_tuning_change(
                getattr(ctx, "profile_id", 0), ctx.user_id,
                "evaluation", "none",
                "-", "-", summary,
                win_rate_at_change=wr,
                predictions_resolved=n_resolved,
            )
            with closing(_get_main_conn()) as mc:
                mc.execute(
                    "UPDATE tuning_history SET outcome_after='n/a' WHERE id=?",
                    (row_id,),
                )
                mc.commit()
        except (sqlite3.OperationalError, sqlite3.DatabaseError,
                ImportError, AttributeError, OSError) as _th_exc:
            # Tuning-history "no change" log is informational;
            # tuner state already correct. Surface for follow-up.
            logger.debug(
                "tuning-history 'no change' log failed: %s: %s",
                type(_th_exc).__name__, _th_exc,
            )


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

        # Item 5a — also (re)bootstrap the SGD online meta-model so it
        # starts from the same training set as the GBM. Subsequent
        # resolved predictions update it incrementally via
        # ai_tracker.resolve_predictions.
        try:
            from online_meta_model import initialize_from_history
            online = initialize_from_history(profile_id, ctx.db_path)
            if online is not None:
                logging.info(
                    f"[{seg_label}] Online meta-model initialized "
                    f"(n_updates={online['n_updates']})"
                )
        except Exception as _exc:
            logging.debug(f"Online meta-model init skipped: {_exc}")
    except Exception as exc:
        logging.warning(f"Meta-model retrain failed: {exc}")


def _task_portfolio_risk_snapshot(ctx):
    """Item 2a — daily Barra-style portfolio risk snapshot.

    Pulls live positions from the broker, computes factor exposures via
    OLS regression vs ~21-factor universe (Ken French + sector ETFs +
    style ETFs), then runs parametric VaR + Monte Carlo VaR + 7
    historical stress scenarios.

    Persisted to `portfolio_risk_snapshots` table for dashboard reads
    and AI prompt context. Skips silently when there are no positions
    or factor data is unavailable.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from client import get_api
        from portfolio_risk_model import (
            compute_portfolio_risk_from_positions, render_risk_summary_for_prompt,
        )
        from risk_stress_scenarios import run_all_scenarios

        api = get_api(ctx)
        positions = []
        try:
            for p in api.list_positions():
                positions.append({
                    "symbol": p.symbol,
                    "market_value": float(p.market_value),
                })
        except Exception as exc:
            logging.debug(f"[{seg_label}] Risk snapshot — list_positions failed: {exc}")
            return
        if not positions:
            logging.info(f"[{seg_label}] Risk snapshot — no positions")
            return

        try:
            account = api.get_account()
            equity = float(account.portfolio_value)
        except Exception:
            equity = sum(abs(p["market_value"]) for p in positions)

        risk = compute_portfolio_risk_from_positions(
            positions, portfolio_value=equity,
        )
        if risk is None:
            logging.info(f"[{seg_label}] Risk snapshot — insufficient factor data")
            return

        # Phase 6b of pipeline refactor: attach aggregate book Greeks
        # to the risk snapshot so render_risk_summary_for_prompt can
        # surface them in the AI prompts. Pipelines now see the
        # book's net delta/gamma/vega/theta alongside the factor-risk
        # numbers — visibility on the option-specific risk
        # dimensions (theta decay, vol exposure) the factor model
        # can't capture. Failure-tolerant: if Greeks computation
        # raises, the snapshot continues without them.
        try:
            from pipelines.risk import compute_book_greeks
            risk["book_greeks"] = compute_book_greeks(positions) or {}
        except Exception as exc:
            logging.debug(
                f"[{seg_label}] Greeks aggregation failed (non-fatal): {exc}"
            )
            risk["book_greeks"] = {}

        # docs/18 item #6: IV-rank degradation alarm. The options oracle
        # silently degrades when chains can't be fetched; before this,
        # operators only noticed via "why no options trades?". The
        # fallback_iv_count metric is wired in compute_book_greeks
        # (auto-IV lookup, 2026-05-19); if ≥80% of legs needed the
        # 25% fallback in a cycle, fire a loud audit_alerts row.
        try:
            bg = risk.get("book_greeks") or {}
            n_legs = int(bg.get("n_options_legs") or 0)
            n_fb = int(bg.get("fallback_iv_count") or 0)
            if n_legs >= 3 and n_fb / max(1, n_legs) >= 0.80:
                pct = round(100.0 * n_fb / n_legs, 1)
                msg = (
                    f"IV-rank lookup degraded: {n_fb}/{n_legs} option "
                    f"legs ({pct}%) used FALLBACK_IV=0.25 this cycle. "
                    "Investigate options_oracle / Alpaca options chain "
                    "fetch — silent degradation means delta-adjusted "
                    "exposure understates risk for high-IV underlyings."
                )
                logging.warning(f"[{seg_label}] {msg}")
                try:
                    from journal import _get_conn as _gc
                    with closing(_gc(ctx.db_path)) as conn:
                        conn.execute("""
                            CREATE TABLE IF NOT EXISTS audit_alerts (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                                alert_type TEXT NOT NULL,
                                severity TEXT NOT NULL DEFAULT 'warning',
                                title TEXT NOT NULL,
                                detail TEXT,
                                resolved INTEGER NOT NULL DEFAULT 0)
                        """)
                        conn.execute(
                            "INSERT INTO audit_alerts "
                            "(alert_type, severity, title, detail) "
                            "VALUES (?, ?, ?, ?)",
                            ("iv_rank_degradation", "warning",
                             f"IV-rank lookup degraded ({pct}%)",
                             msg),
                        )
                        conn.commit()
                except Exception as _alert_exc:
                    logging.warning(
                        f"[{seg_label}] audit_alerts insert for IV "
                        f"degradation failed: {_alert_exc}"
                    )
        except Exception as _iv_exc:
            logging.debug(
                f"[{seg_label}] IV degradation check failed "
                f"(non-fatal): {_iv_exc}"
            )

        scenarios = run_all_scenarios(
            risk["weights"], risk["exposures"], portfolio_value=equity,
        )

        _persist_risk_snapshot(ctx.db_path, risk, scenarios, equity)

        summary = render_risk_summary_for_prompt(risk)
        worst = scenarios[0] if scenarios else None
        worst_str = (
            f" — worst scenario {worst['scenario']}: "
            f"{worst['total_pnl_pct'] * 100:+.2f}%"
        ) if worst else ""
        logging.info(f"[{seg_label}] Risk snapshot: {summary}{worst_str}")

        _safe_log_activity(
            getattr(ctx, "profile_id", 0), ctx.user_id,
            "portfolio_risk",
            f"Portfolio Risk Snapshot: 95% VaR ${risk['var_95_dollars']:,.0f}",
            (f"Daily σ {risk['sigma'] * 100:.2f}%, "
             f"95% VaR ${risk['var_95_dollars']:,.0f} "
             f"({risk['var_95_pct'] * 100:.2f}%), "
             f"95% ES ${risk['es_95_dollars']:,.0f}, "
             f"{risk['n_symbols']} positions, {len(scenarios)} scenarios projected"),
        )
    except Exception as exc:
        logging.warning(f"[{seg_label}] Portfolio risk snapshot failed: {exc}")


def _persist_risk_snapshot(db_path, risk, scenarios, equity):
    """Write the snapshot to portfolio_risk_snapshots; keep last 90 days."""
    import json
    from journal import _get_conn
    with closing(_get_conn(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS portfolio_risk_snapshots (
                  id INTEGER PRIMARY KEY,
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  equity REAL,
                  sigma REAL,
                  var_95_dollars REAL,
                  var_99_dollars REAL,
                  es_95_dollars REAL,
                  es_99_dollars REAL,
                  mc_var_95_dollars REAL,
                  factor_exposures_json TEXT,
                  grouped_decomposition_json TEXT,
                  scenarios_json TEXT,
                  n_symbols INTEGER
            )"""
        )
        mc = risk.get("monte_carlo") or {}
        conn.execute(
            """INSERT INTO portfolio_risk_snapshots (
                  equity, sigma,
                  var_95_dollars, var_99_dollars,
                  es_95_dollars, es_99_dollars,
                  mc_var_95_dollars,
                  factor_exposures_json, grouped_decomposition_json,
                  scenarios_json, n_symbols
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                equity, risk["sigma"],
                risk["var_95_dollars"], risk["var_99_dollars"],
                risk["es_95_dollars"], risk["es_99_dollars"],
                mc.get("var_95_dollars"),
                json.dumps(risk["factor_exposures"]),
                json.dumps(risk["grouped_decomposition"]),
                json.dumps([{
                    "scenario": s["scenario"],
                    "description": s["description"],
                    "severity": s["severity"],
                    "total_pnl_pct": s["total_pnl_pct"],
                    "total_pnl_dollars": s["total_pnl_dollars"],
                    "worst_day_pct": s["worst_day_pct"],
                    "worst_day_date": s["worst_day_date"],
                    "max_drawdown_pct": s["max_drawdown_pct"],
                    "approximation_quality": s["approximation_quality"],
                } for s in scenarios]),
                risk["n_symbols"],
            ),
        )
        # Trim history (keep 90 days)
        conn.execute(
            "DELETE FROM portfolio_risk_snapshots "
            "WHERE created_at < datetime('now', '-90 days')"
        )
        conn.commit()


def _task_app_store_snapshot(ctx):
    """Daily snapshot of App Store rankings into app_store_history.
    Idempotent: only runs once per UTC day across the whole scheduler.
    """
    seg_label = ctx.display_name or ctx.segment
    try:
        from datetime import datetime as _dt
        import sqlite3 as _sq
        today = _dt.utcnow().date().isoformat()
        marker = "quantopsai.db"
        conn = _sq.connect(marker)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_store_snapshot_runs (
                run_date TEXT PRIMARY KEY,
                ran_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        if conn.execute(
            "SELECT 1 FROM app_store_snapshot_runs WHERE run_date = ?",
            (today,),
        ).fetchone():
            conn.close()
            return
        conn.close()
        from alternative_data import snapshot_app_store_rankings_for_all_tickers
        n = snapshot_app_store_rankings_for_all_tickers()
        # Marker
        conn = _sq.connect(marker)
        conn.execute(
            "INSERT INTO app_store_snapshot_runs (run_date) VALUES (?)",
            (today,),
        )
        conn.commit()
        conn.close()
        logging.info(f"[{seg_label}] App Store snapshot wrote {n} rows")
    except Exception:
        logging.exception(f"[{seg_label}] App Store snapshot failed")


def _task_pdufa_scrape(ctx):
    """OPEN_ITEMS #6 — daily PDUFA event scrape. Once-per-UTC-day
    idempotent; populates the pdufa_events table read by
    alternative_data.get_biotech_milestones."""
    seg_label = ctx.display_name or ctx.segment
    try:
        from datetime import datetime as _dt
        import sqlite3 as _sq
        today = _dt.utcnow().date().isoformat()
        marker = "quantopsai.db"
        conn = _sq.connect(marker)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pdufa_scrape_runs (
                run_date TEXT PRIMARY KEY,
                ran_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        if conn.execute(
            "SELECT 1 FROM pdufa_scrape_runs WHERE run_date = ?",
            (today,),
        ).fetchone():
            conn.close()
            return
        conn.close()
        from pdufa_scraper import run_full_sync
        n_fetched, n_written = run_full_sync()
        conn = _sq.connect(marker)
        conn.execute(
            "INSERT INTO pdufa_scrape_runs (run_date) VALUES (?)",
            (today,),
        )
        conn.commit()
        conn.close()
        logging.info(
            f"[{seg_label}] PDUFA scrape: {n_fetched} fetched, "
            f"{n_written} written"
        )
    except Exception:
        logging.exception(f"[{seg_label}] PDUFA scrape failed")


def _task_universe_audit(ctx):
    """Daily snapshot of Alpaca's active US-equity asset set + diff vs
    yesterday. Symbols that fell off the active list are recorded in
    `historical_universe_additions` so future backtests over windows
    that include their `last_seen_active` date can include them in
    the universe.

    Wave 4 / Issue #10 of METHODOLOGY_FIX_PLAN.md (survivorship bias).

    Uses `screener.get_active_alpaca_symbols` which is already cached
    daily in-process — adds ZERO extra Alpaca calls. Runs once per
    daily snapshot block; subsequent profiles in the same scheduler
    cycle hit the no-op idempotency check below.
    """
    seg_label = ctx.display_name or ctx.segment
    # Idempotency: only run once per UTC day across the whole
    # scheduler. Master DB is shared.
    try:
        from datetime import datetime as _dt
        import sqlite3 as _sq
        today = _dt.utcnow().date().isoformat()
        marker_db = "quantopsai.db"
        conn = _sq.connect(marker_db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS universe_audit_runs (
                run_date TEXT PRIMARY KEY,
                ran_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        existing = conn.execute(
            "SELECT 1 FROM universe_audit_runs WHERE run_date = ?",
            (today,),
        ).fetchone()
        if existing:
            conn.close()
            logging.info(
                f"[{seg_label}] Universe audit already ran today; skipping."
            )
            return
        conn.close()
    except Exception as exc:
        logging.warning(f"Universe audit idempotency check failed: {exc}")
        # Continue — better to potentially run twice than to fail silently.

    try:
        from screener import get_active_alpaca_symbols
        from historical_universe_augment import (
            record_daily_snapshot, diff_and_record_departures,
        )
        active = get_active_alpaca_symbols(ctx)
        if not active:
            logging.info(
                f"[{seg_label}] Universe audit: empty active set "
                "(Alpaca cache miss + cold lookup failed). Skipping; "
                "will retry tomorrow."
            )
            return
        new_departures = diff_and_record_departures(active)
        recorded = record_daily_snapshot(active)
        # Mark today's run as complete (idempotency for the rest of
        # today's profile loop).
        try:
            import sqlite3 as _sq
            conn = _sq.connect("quantopsai.db")
            conn.execute(
                "INSERT OR IGNORE INTO universe_audit_runs (run_date) "
                "VALUES (?)",
                (today,),
            )
            conn.commit()
            conn.close()
        except (_sq.OperationalError, _sq.DatabaseError, OSError) as _im_exc:
            # Idempotency-marker write; next-day retry handles
            # missing row. Surface for follow-up.
            logger.warning(
                "universe-audit idempotency marker write failed: %s: %s",
                type(_im_exc).__name__, _im_exc,
            )
        logging.info(
            f"[{seg_label}] Universe audit: {recorded} active symbols "
            f"snapshotted; {new_departures} new departures recorded."
        )
    except Exception as exc:
        logging.warning(f"Universe audit failed: {exc}")


def _task_specialist_health_check(ctx):
    """Auto-(dis)enable specialists based on calibrator slope.

    Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md. Reads each
    specialist's fitted calibrator and applies a "health" rule:

    - DISABLE specialist if calibrator maps raw=90 to cal<35 AND
      we have ≥50 resolved samples for that specialist (avoid
      acting on small samples). Indicates clearly-inverse
      correlation; specialist is anti-signal.
    - RE-ENABLE specialist if it's currently disabled AND its
      calibrator now maps raw=90 to cal>50. Indicates the slope
      flipped back to positive (fresh regime / new training data).

    Hard floor: never DISABLE if it would leave <2 specialists
    active per profile (the ensemble synth needs ≥2 to mean
    anything). Floor enforcement also lives in ensemble.py.

    Reads/writes profile.disabled_specialists JSON column.
    """
    profile_id = getattr(ctx, "profile_id", None)
    if profile_id is None:
        return
    try:
        import json as _json
        from specialist_calibration import (
            get_calibrator, apply_calibration,
        )
        from specialists import discover_specialists
        seg_label = ctx.display_name or ctx.segment

        all_names = [getattr(m, "NAME", None) for m in discover_specialists()]
        all_names = [n for n in all_names if n]
        if not all_names:
            return

        # Read current disabled list
        current_raw = getattr(ctx, "disabled_specialists", "[]") or "[]"
        current = set(_json.loads(current_raw)) if isinstance(current_raw, str) else set(current_raw)

        # Count resolved samples per specialist (need ≥50 to act)
        import sqlite3 as _sq
        sample_counts = {}
        try:
            conn = _sq.connect(ctx.db_path)
            for name in all_names:
                row = conn.execute(
                    "SELECT COUNT(*) FROM specialist_outcomes "
                    "WHERE specialist_name = ? AND was_correct IS NOT NULL",
                    (name,),
                ).fetchone()
                sample_counts[name] = (row[0] if row else 0)
            conn.close()
        except Exception:
            sample_counts = {n: 0 for n in all_names}

        new_disabled = set(current)
        actions = []

        for name in all_names:
            if sample_counts.get(name, 0) < 50:
                continue
            cal = get_calibrator(ctx.db_path, name)
            if cal is None:
                continue
            # Probe slope by mapping raw=90 → calibrated value.
            cal_at_90 = apply_calibration(90, cal)

            if name not in current and cal_at_90 < 35:
                new_disabled.add(name)
                actions.append(
                    f"DISABLE {name} (raw=90 → cal={cal_at_90}, "
                    f"n={sample_counts[name]}; clear anti-signal)"
                )
            elif name in current and cal_at_90 > 50:
                new_disabled.discard(name)
                actions.append(
                    f"RE-ENABLE {name} (raw=90 → cal={cal_at_90}, "
                    f"n={sample_counts[name]}; slope recovered)"
                )

        # Hard floor: ensure ≥2 specialists active. If applying the
        # new disabled set would leave fewer, undo the most-recent
        # disable until floor satisfied.
        active_count = len(all_names) - len(new_disabled)
        if active_count < 2:
            # Sort newly-added disables alphabetically for deterministic
            # tie-break, then restore until we have ≥2 active.
            newly_added = sorted(new_disabled - current)
            while active_count < 2 and newly_added:
                restore = newly_added.pop()
                new_disabled.discard(restore)
                active_count = len(all_names) - len(new_disabled)
                actions.append(
                    f"FLOOR-RESTORE {restore} (would leave <2 active)"
                )

        if new_disabled != current:
            from models import update_trading_profile
            update_trading_profile(
                profile_id,
                disabled_specialists=_json.dumps(sorted(new_disabled)),
            )
            logging.info(
                f"[{seg_label}] Specialist health check applied: "
                + "; ".join(actions)
            )
        else:
            logging.info(
                f"[{seg_label}] Specialist health check: no changes "
                f"(disabled={sorted(current)}, samples="
                + ",".join(f"{n}={sample_counts.get(n,0)}" for n in all_names)
                + ")"
            )
    except Exception as exc:
        logging.warning(f"Specialist health check failed: {exc}")


def _task_calibrate_specialists(ctx):
    """Refit per-specialist Platt-scaling calibrators on each
    profile's accumulated specialist outcomes. Wave 3 / Fix #9 of
    METHODOLOGY_FIX_PLAN.md. Runs in the daily snapshot block; no-op
    when there's insufficient resolved data per specialist."""
    try:
        from specialist_calibration import refit_all
        from specialists import discover_specialists
        seg_label = ctx.display_name or ctx.segment
        # discover_specialists() returns a list of module objects;
        # each exposes a NAME constant we use as the calibrator key.
        names = [getattr(m, "NAME", None) for m in discover_specialists()]
        names = [n for n in names if n]
        if not names:
            return
        results = refit_all(ctx.db_path, names)
        fitted = [n for n, ok in results.items() if ok]
        skipped = [n for n, ok in results.items() if not ok]
        logging.info(
            f"[{seg_label}] Specialist calibrators refit: "
            f"fitted={fitted}, skipped (insufficient data)={skipped}"
        )
    except Exception as exc:
        logging.warning(f"Specialist calibration refit failed: {exc}")


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
        except (KeyError, AttributeError, TypeError, OSError) as _ps_exc:
            # Positions seed for SEC watchlist; falls through to
            # shortlist seed below. Surface for follow-up.
            logger.debug(
                "SEC watchlist positions seed failed: %s: %s",
                type(_ps_exc).__name__, _ps_exc,
            )

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
        except (OSError, _json.JSONDecodeError, KeyError, AttributeError,
                TypeError) as _ss_exc:
            # Shortlist seed for SEC watchlist; positions seed above
            # is sufficient. Surface for follow-up.
            logger.debug(
                "SEC watchlist shortlist seed failed: %s: %s",
                type(_ss_exc).__name__, _ss_exc,
            )

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

            # Evidence-based diagnosis. Reads ai_cost_ledger,
            # activity_log, and ai_predictions to report what the
            # task was actually doing — replaces the previous
            # if/elif on task name + elapsed time which fabricated
            # culprits ("likely Alpaca slow") with no evidence.
            # Orphaned-restart rows never reach this code path —
            # they're filtered out at scheduler startup by
            # `mark_orphaned_at_startup` before the watchdog runs.
            from task_watchdog import diagnose_stalled_run
            cause = diagnose_stalled_run(
                ctx.db_path, task_name, started_at, elapsed,
            )

            # Log the diagnosis inline with the stall warning so the
            # evidence makes it into journald + the /issues page. Pre-
            # 2026-05-16 the diagnosis only landed in activity_log and
            # the email body — the operator scanning journalctl saw
            # "stalled, 35 min elapsed" with no hint why.
            logging.warning(
                f"  STALLED: {task_name} "
                f"(started {started_at}, {elapsed:.0f} min elapsed) "
                f"— {cause}"
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
            except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError,
                    AttributeError, OSError) as _eb_exc:
                # Event-bus emit for stalled task is best-effort;
                # notify_error below is the redundant alert. Surface
                # for follow-up.
                logger.debug(
                    "task_stalled event-bus emit failed: %s: %s",
                    type(_eb_exc).__name__, _eb_exc,
                )
            try:
                from notifications import notify_error
                # `context` is used for the email subject — keep it short
                # and single-line. All multi-line detail (started_at,
                # elapsed, suggested action) belongs in `error_msg`.
                short_ctx = (
                    f"{seg_label} stalled: {row['task_name']}"
                )
                notify_error(
                    error_msg=(
                        f"Stalled task: {row['task_name']}\n"
                        f"Profile: {seg_label}\n"
                        f"Task started at: {row['started_at']}\n"
                        f"Elapsed: {elapsed:.0f} minutes without completion.\n\n"
                        f"The task was marked stalled by the watchdog. "
                        f"Check journalctl -u quantopsai for the underlying "
                        f"failure mode."
                    ),
                    context=short_ctx,
                    ctx=ctx,
                )
            except Exception as exc:
                # Pre-2026-05-16 this was debug-level — meaning a
                # watchdog that detected a stall but FAILED to notify
                # was completely invisible. Now WARNING so the gap in
                # alerting surfaces on /issues. (The stall itself
                # already got the log.warning above; this covers the
                # email/notification leg.)
                logging.warning(
                    "Watchdog DETECTED stall but FAILED to send "
                    "notification: %s: %s — stall is logged but the "
                    "operator alert was lost",
                    type(exc).__name__, exc,
                )
    except Exception as exc:
        # The watchdog itself crashing means stalls go undetected for
        # the whole next cycle. Bump to ERROR — this is operational
        # blindness.
        logging.error(
            "Watchdog task itself CRASHED: %s: %s — stall detection "
            "is DOWN until the next watchdog cycle; investigate "
            "immediately",
            type(exc).__name__, exc,
        )


def _task_phase5c_backfill_nightly(ctx):
    """docs/18 item #2: nightly Phase 5c/d backfill of historical
    option predictions.

    The boot-time call in `cycle_segment` is gated by a migration
    marker — it only runs ONCE per profile DB and no-ops thereafter.
    This nightly task calls with `force=True` so any new
    `pipeline_kind='option'` resolved row that's missing
    `option_order_id` / `occ_symbol` gets re-resolved with the
    Phase 5c option-aware math instead of leaving the broken
    underlying-price-based `actual_return_pct` in place.

    Idempotency is row-level via the `option_order_id IS NULL AND
    occ_symbol IS NULL` WHERE clause — even forced runs only touch
    rows that genuinely need backfilling. Cost: one cheap query +
    zero updates on a clean DB.
    """
    try:
        from pipelines.outcomes.backfill import (
            backfill_historical_option_predictions,
        )
        seg_label = ctx.display_name or ctx.segment
        counts = backfill_historical_option_predictions(
            ctx.db_path, force=True,
        )
        n_linked = (counts.get("linked_multileg", 0)
                     + counts.get("linked_single_leg", 0))
        if counts.get("scanned", 0) == 0:
            logging.info(
                f"[{seg_label}] Phase 5c nightly backfill: nothing "
                f"to do (no unlinked historical option rows)"
            )
            return
        logging.info(
            f"[{seg_label}] Phase 5c nightly backfill: scanned="
            f"{counts.get('scanned', 0)}, linked_multileg="
            f"{counts.get('linked_multileg', 0)}, "
            f"linked_single_leg={counts.get('linked_single_leg', 0)}, "
            f"no_match={counts.get('no_match', 0)}"
        )
        if n_linked > 0:
            try:
                _safe_log_activity(
                    getattr(ctx, "profile_id", 0), ctx.user_id,
                    "phase5c_backfill",
                    f"Phase 5c nightly backfill linked {n_linked} rows",
                    (f"Re-linked {n_linked} historical option "
                     f"prediction(s) so the Phase 5c option-aware "
                     f"resolver re-resolves them with correct math "
                     f"on the next cycle. (multileg="
                     f"{counts.get('linked_multileg', 0)}, "
                     f"single-leg="
                     f"{counts.get('linked_single_leg', 0)})"),
                )
            except Exception:
                logging.exception(
                    "Phase 5c backfill activity log failed"
                )
    except Exception as exc:
        logging.warning(
            f"Phase 5c nightly backfill task failed: "
            f"{type(exc).__name__}: {exc}"
        )


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
        with closing(_get_conn()) as conn:
            users = conn.execute(
                "SELECT id, email FROM users WHERE auto_capital_allocation = 1"
            ).fetchall()

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


def _task_post_mortem(ctx):
    """Weekly losing-week post-mortem: when the past 7 days
    materially underperformed the long-term baseline, cluster the
    losing trades' features and store the dominant pattern as a
    learned_pattern. The AI prompt picks it up automatically next
    week via the existing learned_patterns plumbing.

    Sundays only; file-based idempotency marker prevents re-fire on
    restart. Per-profile (each profile's losses are clustered against
    its own baseline)."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    now_et = _dt.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() != 6:  # Sunday only
        return

    seg_label = ctx.display_name or ctx.segment
    profile_id = getattr(ctx, "profile_id", 0)
    today = now_et.strftime("%Y-%m-%d")
    marker = f".post_mortem_done_p{profile_id}.marker"

    try:
        with open(marker) as f:
            if f.read().strip() == today:
                return  # already ran for this profile this week
    except FileNotFoundError:
        pass

    try:
        from post_mortem import analyze_recent_week
        result = analyze_recent_week(ctx.db_path)
        if result:
            logging.info(
                f"[{seg_label}] Post-mortem: WR {result['period_wr']:.0f}% "
                f"vs baseline {result['baseline_wr']:.0f}%, "
                f"{result['losing_trade_count']} losses analyzed. "
                f"Pattern stored.")
        else:
            logging.info(
                f"[{seg_label}] Post-mortem: week was healthy or "
                f"insufficient data — no pattern stored.")
        try:
            with open(marker, "w") as f:
                f.write(today)
        except OSError:
            pass
    except Exception as exc:
        logging.warning(
            f"[{seg_label}] Post-mortem task failed: {exc}")


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


def _task_shadow_eval_daily_email(ctx):
    """Send the shadow-eval daily digest — separate email from the
    main daily summary so it can be muted independently. Marker-file
    idempotent, same pattern as _task_daily_summary_email. Skips
    silently when shadow eval is disabled or produced no rows."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    from notifications import notify_shadow_eval_daily

    profile_id = getattr(ctx, "profile_id", 0)
    today_et = _dt.now(_ZI("America/New_York")).strftime("%Y-%m-%d")
    marker_path = f".shadow_eval_sent_p{profile_id}.marker"

    try:
        with open(marker_path) as f:
            last_sent = f.read().strip()
        if last_sent == today_et:
            logging.info(
                f"Shadow-eval digest already sent for profile {profile_id} "
                f"today ({today_et}) — skipping.")
            return
    except FileNotFoundError:
        pass

    sent = notify_shadow_eval_daily(ctx=ctx)
    if sent:
        try:
            with open(marker_path, "w") as f:
                f.write(today_et)
        except OSError as exc:
            logging.warning(
                f"Could not write shadow-eval marker: {exc}")
        logging.info(
            f"Shadow-eval digest sent for profile {profile_id}.")


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

    # yfinance logs HTTP 404s at ERROR for symbols missing fundamentals
    # (most ETFs: JEPI / QLD / IJH / etc). These aren't actionable —
    # we already fall back gracefully when fundamentals are missing.
    # Suppress to keep journalctl signal-to-noise sane.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    logging.info("=" * 60)
    logging.info("QuantOpsAI MULTI-ACCOUNT scheduler starting")
    if legacy_mode:
        logging.info(f"Mode: LEGACY (segments: {active_segments})")
    else:
        logging.info("Mode: PROFILES (iterating all active trading profiles)")
    logging.info(f"Log file: {log_file}")
    logging.info("=" * 60)

    # ── DB integrity check on startup ───────────────────────────────
    # Before we touch any DB for real work, run PRAGMA integrity_check
    # on every DB file we'll be writing to. If any are corrupt, halt
    # immediately — refusing to trade on a corrupt DB beats silently
    # mis-recording every fill. The operator restores from the
    # backup_daily.sh snapshot using db_integrity.restore_from_backup().
    try:
        from db_integrity import (
            check_all_dbs, critical_corrupt, non_critical_corrupt,
        )
        results = check_all_dbs()
        critical = critical_corrupt(results)
        non_critical = non_critical_corrupt(results)
        # Non-critical corruption: log + email (debounced) but
        # CONTINUE. The 2026-05-13 incident: a 0-byte
        # strategy_validations.db crashed the scheduler in a 30-sec
        # restart loop, sending 145 ERROR emails. Non-critical DBs
        # don't carry trade-pipeline truth and the scheduler can
        # safely run without them.
        if non_critical:
            for bad in non_critical:
                logging.warning(
                    "DB CORRUPT (non-critical, continuing): %s — %s",
                    bad, results[bad]["detail"],
                )
            try:
                from notifications import notify_error
                notify_error(
                    error_msg=(
                        "Non-critical DB integrity_check failed for: "
                        + ", ".join(non_critical)
                        + "\nScheduler is continuing — non-critical DBs "
                        "don't carry trade-pipeline truth and can be "
                        "recreated from scratch. Investigate when "
                        "convenient."
                    ),
                    context="DB corruption detected (non-critical)",
                )
            except (ImportError, AttributeError, OSError) as _ne_exc:
                # notify_error is best-effort; corruption already
                # logged above. Surface for follow-up.
                logger.warning(
                    "non-critical DB corruption notify_error failed: %s: %s",
                    type(_ne_exc).__name__, _ne_exc,
                )
        # Critical corruption: halt as before.
        if critical:
            for bad in critical:
                logging.error(
                    "DB CORRUPT (critical): %s — %s",
                    bad, results[bad]["detail"],
                )
            try:
                from notifications import notify_error
                notify_error(
                    error_msg=(
                        "CRITICAL DB integrity_check failed for: "
                        + ", ".join(critical)
                        + "\nScheduler is halting. Restore from backup with:"
                        + "\n  python3 -c 'from db_integrity import "
                        + "restore_from_backup; print(restore_from_backup(\"<filename>\"))'"
                    ),
                    context="DB corruption detected",
                )
            except (ImportError, AttributeError, OSError) as _ne_exc:
                # notify_error is best-effort; scheduler still exits
                # 1 below regardless. Surface for follow-up.
                logger.warning(
                    "critical DB corruption notify_error failed: %s: %s",
                    type(_ne_exc).__name__, _ne_exc,
                )
            logging.error(
                "Scheduler refusing to start with critical DB corruption — exit 1"
            )
            sys.exit(1)
        if not critical and not non_critical:
            logging.info(
                "DB integrity check: %d DBs healthy", len(results),
            )
    except Exception as exc:
        logging.warning(
            "DB integrity check failed to run (continuing): %s", exc,
        )

    # ── Alpaca credentials invariant ────────────────────────────────
    # 2026-05-19: a post-reset bug left alpaca_accounts empty while
    # every profile had its own per-profile encrypted keys. Trades
    # still went through (resolver falls back to per-profile keys
    # when alpaca_account_id is NULL), but data_source_health probes
    # — which read from alpaca_accounts only — failed every cycle and
    # silent yfinance fallback fired system-wide. Refuse to boot when
    # we detect that broken state.
    try:
        from alpaca_credentials_invariant import check_alpaca_credentials
        ok, problems = check_alpaca_credentials("quantopsai.db")
        if not ok:
            for p in problems:
                logging.error("ALPACA CREDENTIALS INVARIANT: %s", p)
            try:
                from notifications import notify_error
                notify_error(
                    error_msg=(
                        "Scheduler refusing to start — Alpaca "
                        "credentials invariant failed:\n\n"
                        + "\n\n".join(problems)
                    ),
                    context="Alpaca credentials invariant failed",
                )
            except (ImportError, AttributeError, OSError) as _ne_exc:
                logger.warning(
                    "alpaca-creds invariant notify_error failed: %s: %s",
                    type(_ne_exc).__name__, _ne_exc,
                )
            logging.error(
                "Scheduler refusing to start — fix Alpaca credentials "
                "configuration and restart."
            )
            sys.exit(1)
        logging.info("Alpaca credentials invariant: OK")
    except SystemExit:
        raise
    except Exception as _inv_exc:
        # Never silently swallow — surface it but don't halt on the
        # invariant itself failing (DB lock, missing module). The
        # underlying state may still be fine.
        logging.warning(
            "Alpaca credentials invariant check failed to run "
            "(continuing): %s: %s",
            type(_inv_exc).__name__, _inv_exc,
        )

    # ── Orphan-restart cleanup ──────────────────────────────────────
    # Any task_runs row still labeled `running` in any profile DB at
    # this point is by definition a zombie — its parent process was
    # killed by the previous shutdown / deploy. Bulk-mark them as
    # `orphaned_restart` BEFORE the watchdog can later mis-diagnose
    # them as "API hang." For each profile that had an orphaned Scan
    # & Trade, remember to fire a make-up scan as soon as the main
    # loop starts (zeroes the per-profile last-scan time below).
    _profiles_needing_makeup_scan: set = set()
    try:
        from task_watchdog import mark_orphaned_at_startup
        from glob import glob as _glob
        for _pdb in _glob("quantopsai_profile_*.db"):
            try:
                _orphans = mark_orphaned_at_startup(_pdb)
            except Exception as _exc:
                logging.warning(
                    "orphan cleanup failed for %s: %s", _pdb, _exc,
                )
                continue
            if not _orphans:
                continue
            # Extract profile id from filename for make-up scheduling.
            import re as _re
            _m = _re.search(r"profile_(\d+)\.db$", _pdb)
            if not _m:
                continue
            _pid = int(_m.group(1))
            _had_scan = any(
                "Scan" in (r.get("task_name") or "")
                for r in _orphans
            )
            if _had_scan:
                _profiles_needing_makeup_scan.add(_pid)
            logging.info(
                "Cleaned %d orphaned task(s) from %s%s",
                len(_orphans), _pdb,
                " — make-up Scan & Trade scheduled" if _had_scan else "",
            )
    except ImportError as _exc:
        logging.warning("orphan cleanup module unavailable: %s", _exc)

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
        """Return per-profile last-run dict, initializing on first access.

        All intervals start at 0.0 so the first loop iteration after
        a (re)start always fires a fresh scan / exits-check / resolve
        cycle — no work is "lost" by a restart, the cycle just
        re-runs immediately under the new process. (This is also the
        de facto make-up-scan mechanism: a cycle killed mid-flight
        gets re-attempted within seconds of the new process booting.)
        """
        if pid not in profile_runs:
            profile_runs[pid] = {
                "scan": 0.0,
                "check_exits": 0.0,
                "resolve_predictions": 0.0,
            }
            if pid in _profiles_needing_makeup_scan:
                logging.info(
                    "[profile %d] previous Scan & Trade was killed by "
                    "restart; first-iteration scan will recover the cycle",
                    pid,
                )
        return profile_runs[pid]

    # 2026-06-04 — scan cadence is operator-tunable via the Settings
    # page (users.scan_interval_minutes; default 15). Read fresh on
    # every loop iteration below so a UI change takes effect on the
    # next cycle without a restart. The literal `INTERVAL_SCAN` is
    # gone — every reference reads through _scan_interval_seconds()
    # so the value can't drift from the operator's intent.
    from models import get_scan_interval_minutes as _get_scan_min

    def _scan_interval_seconds() -> int:
        return int(_get_scan_min()) * 60

    # Exits check every 5 min — cheap, time-critical (TP/SL triggers
    # need to fire within minutes of price hitting threshold, not
    # whenever the scan happens to complete). Not operator-tunable;
    # 5min is the bound below which broker-side latency dominates.
    INTERVAL_CHECK_EXITS = 5 * 60
    INTERVAL_RESOLVE_PREDICTIONS = 60 * 60  # 60 minutes
    # 2026-05-17 (#169): cross-profile integrity audit + first-detection
    # alerter. 10 min cadence — fast enough that drift is caught quickly,
    # slow enough that the five audits + the email path don't dominate
    # the scheduler loop.
    INTERVAL_AUDIT_RUNNER = 10 * 60
    last_audit_run = 0.0

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
        _scan_secs = _scan_interval_seconds()
        do_scan = (current_time - last_run["scan"] >= _scan_secs)
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
                prof_do_scan = (now_t - pr["scan"]) >= _scan_secs
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

        # Audit runner (#169) — once per cycle, gated by its own
        # interval. Cross-profile so it lives outside the per-profile
        # task loop. Emails on first detection of any new drift
        # signature; idempotent across re-runs.
        if (time.time() - last_audit_run) >= INTERVAL_AUDIT_RUNNER:
            try:
                from audit_runner import detect_and_alert_new_drift
                summary = detect_and_alert_new_drift()
                if summary["new"]:
                    logging.warning(
                        "audit_runner: %d new drift item(s) (signatures: %s)",
                        summary["new"],
                        [it["signature"] for it in summary["new_items"]],
                    )
                if summary["resolved"]:
                    logging.info(
                        "audit_runner: %d drift item(s) resolved",
                        summary["resolved"],
                    )
            except Exception:
                logging.exception("audit_runner: cycle failed")
            last_audit_run = time.time()

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
                    "next_scan": last_run["scan"] + _scan_secs,
                    "last_exit_check": last_run["check_exits"],
                    "next_exit_check": last_run["check_exits"] + INTERVAL_CHECK_EXITS,
                    "last_ai_resolve": last_run["resolve_predictions"],
                    "next_ai_resolve": last_run["resolve_predictions"] + INTERVAL_RESOLVE_PREDICTIONS,
                    "scan_interval_min": _scan_secs // 60,
                    "exit_interval_min": INTERVAL_CHECK_EXITS // 60,
                    "ai_interval_min": INTERVAL_RESOLVE_PREDICTIONS // 60,
                    "market_open": market_open,
                    "has_crypto": has_crypto if not legacy_mode else bool([s for s in (active_segments or []) if s == "crypto"]),
                    "updated_at": time.time(),
                }
                with open("scheduler_status.json", "w") as f:
                    _json.dump(status, f)
            except (OSError, TypeError, ValueError) as _sf_exc:
                # Status-file write is for the web UI countdown;
                # never break the scheduler. Surface for follow-up.
                logger.debug(
                    "scheduler_status.json write failed: %s: %s",
                    type(_sf_exc).__name__, _sf_exc,
                )

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
