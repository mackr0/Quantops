"""Main views blueprint — dashboard, settings, trades, AI performance, admin."""

import json
import logging
import os
import time
from contextlib import closing
from functools import wraps
from typing import Any, Dict, Tuple

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, abort,
)
from flask_login import login_required, current_user

from models import (
    build_user_context, get_user_segment_config, update_user_segment_config,
    get_user_by_id, get_user_by_email, get_active_users,
    update_user_credentials, get_api_usage,
    create_default_segment_configs,
    # Trading profiles
    create_trading_profile, get_trading_profile, get_user_profiles,
    get_active_profiles, update_trading_profile, delete_trading_profile,
    build_user_context_from_profile, MARKET_TYPE_NAMES,
    # Activity log
    get_activity_feed, get_activity_count,
    # DB helpers — open_profile_db is the SINGLE authorized way to open
    # a per-profile DB from views.py. Includes WAL + busy_timeout=5000
    # + idempotent schema migration (init_tracker_db). Eliminates the
    # transient-lock and schema-drift failure modes that the silent-
    # pass swallows in views.py were protecting against. _get_conn is
    # the master-DB equivalent (also WAL + busy_timeout + FK on).
    open_profile_db, _get_conn as _get_main_db_conn,
)
from segments import SEGMENTS, get_segment
from crypto import decrypt, encrypt
from ai_providers import get_providers

# Per-feature modules formerly imported lazily inside try/except blocks
# in route handlers. Hoisted to module top because none of them are
# actually optional in production — burying the import inside a try
# block masked real ImportErrors at runtime as silent missing data.
# If any of these fail to import, we want a startup failure (loud,
# diagnosable) rather than a runtime swallow.
from kelly_sizing import compute_kelly_recommendation
from mfe_capture import compute_capture_ratio
from rigorous_backtest import get_recent_validations
from multi_strategy import get_allocation_summary
from ai_cost_ledger import spend_summary
from crisis_state import get_current_level
from event_bus import recent_events as _recent_events
from strategy_generator import list_strategies
from alpha_decay import (
    list_deprecated, compute_rolling_metrics, compute_lifetime_metrics,
)
from portfolio_exposure import compute_book_beta, compute_exposure
from risk_parity import analyze_position_risk
from portfolio_manager import check_drawdown
from drawdown_scaling import compute_capital_scale
from options_greeks_aggregator import compute_book_greeks
from stat_arb_pair_book import get_active_pairs
from sec_filings import get_active_alerts
from journal import get_slippage_stats, get_performance_summary
from ai_tracker import get_ai_performance
from models import get_tuning_history
from self_tuning import (
    describe_tuning_state, _analyze_failure_patterns,
)
import meta_model

logger = logging.getLogger(__name__)

views_bp = Blueprint("views", __name__, template_folder="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def admin_required(f):
    """Decorator that requires the current user to be an admin (not a viewer)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin and not getattr(current_user, 'role', 'admin') == 'admin':
            abort(403)
        if getattr(current_user, 'is_viewer', False):
            flash("View-only accounts cannot make changes.", "error")
            return redirect(url_for("views.dashboard"))
        return f(*args, **kwargs)
    return decorated


_dashboard_cache = {}
_DASHBOARD_CACHE_TTL = 30  # seconds — dashboard auto-refreshes every 15s


def _safe_account_info(ctx):
    """Fetch account info with 30s cache for dashboard.

    Cache is keyed on `ctx.db_path` (the only stable identifier for a
    profile across requests). Skips caching entirely when db_path is
    missing — using `id(ctx)` as fallback was unsafe because CPython
    reuses object IDs after GC, causing rare cross-test pollution
    when a fresh ctx happened to land at a recently-freed address
    within the 30s TTL window (caught 2026-05-10 via flake in
    test_enriched_positions::test_short_position_gets_sell_side).
    """
    import time
    db_path = getattr(ctx, "db_path", None)
    if db_path:
        cache_key = f"account_{db_path}"
        cached = _dashboard_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _DASHBOARD_CACHE_TTL:
            return cached[1]
    try:
        from client import get_account_info
        result = get_account_info(ctx=ctx)
        if db_path:
            _dashboard_cache[f"account_{db_path}"] = (time.time(), result)
        return result
    except Exception as exc:
        logger.warning("Could not fetch account for %s: %s", ctx.display_name or ctx.segment, exc)
        return None


def _safe_positions(ctx):
    """Fetch positions with 30s cache for dashboard.

    Cache is keyed on `ctx.db_path` (the only stable identifier for a
    profile across requests). Skips caching entirely when db_path is
    missing — see `_safe_account_info` docstring for the id(ctx)
    pollution incident.
    """
    import time
    db_path = getattr(ctx, "db_path", None)
    if db_path:
        cache_key = f"positions_{db_path}"
        cached = _dashboard_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _DASHBOARD_CACHE_TTL:
            return cached[1]
    try:
        from client import get_positions
        result = get_positions(ctx=ctx)
        if db_path:
            _dashboard_cache[f"positions_{db_path}"] = (time.time(), result)
        return result
    except Exception as exc:
        logger.warning("Could not fetch positions for %s: %s", ctx.display_name or ctx.segment, exc)
        return []


def _resolve_exit_logic(ctx, meta):
    """Return a structured description of which exit logic is
    managing the position, so the UI can communicate it instead of
    showing a stale fixed take-profit value.

    Returns a dict like:
        {"label": "Trailing stop (conviction override)",
         "kind": "conviction_trailing",
         "tooltip": "...",
         "fixed_target_active": False}

    The two main cases:
      - Conviction-TP override active: profile has
        use_conviction_tp_override=1 AND the entry's ai_confidence
        was >= conviction_tp_min_confidence. Trailing stop manages
        the exit; the displayed fixed target is informational only.
      - Fixed TP active (default): the displayed take_profit is the
        actual exit trigger; system will sell when reached.
    """
    if ctx is None:
        return {"label": "Fixed target", "kind": "fixed",
                "tooltip": "", "fixed_target_active": True}
    use_override = bool(getattr(ctx, "use_conviction_tp_override", 0))
    if not use_override:
        return {"label": "Fixed target", "kind": "fixed",
                "tooltip": (
                    "Profile sells at the displayed Target price "
                    "when reached."),
                "fixed_target_active": True}
    min_conf = float(
        getattr(ctx, "conviction_tp_min_confidence", 70.0) or 70.0
    )
    entry_conf = meta.get("ai_confidence")
    try:
        entry_conf = float(entry_conf) if entry_conf is not None else 0.0
    except (TypeError, ValueError):
        entry_conf = 0.0
    if entry_conf >= min_conf:
        return {
            "label": "Trailing stop (conviction override)",
            "kind": "conviction_trailing",
            "tooltip": (
                f"AI confidence at entry was {entry_conf:.0f}% "
                f"(>= {min_conf:.0f}% threshold). The conviction-TP "
                f"override is letting the trailing stop manage the "
                f"exit — the position can run past the displayed "
                f"target. The trailing stop will sell when price "
                f"pulls back from its high."
            ),
            "fixed_target_active": False,
        }
    return {
        "label": "Fixed target",
        "kind": "fixed",
        "tooltip": (
            f"AI confidence at entry was {entry_conf:.0f}% "
            f"(< {min_conf:.0f}% threshold). Conviction-TP override "
            f"NOT active for this position — fixed target applies."),
        "fixed_target_active": True,
    }


def _enriched_positions(ctx, profile_id):
    """Alpaca live positions + the AI metadata (reasoning, confidence,
    stop, target, slippage) from the most recent matching trade in the
    profile's journal DB. Rendered by the shared `_trades_table.html`
    macro.

    Fields we add on top of `_safe_positions` output:
      ai_confidence, ai_reasoning, reason, stop_loss, take_profit,
      decision_price, fill_price, slippage_pct, timestamp, side
    We reuse `unrealized_pl` / `unrealized_plpc` / `current_price` /
    `market_value` from Alpaca so the macro's open-position path renders.
    """
    positions = _safe_positions(ctx)
    if not positions:
        return []

    trade_meta = {}
    try:
        db_path = f"quantopsai_profile_{profile_id}.db"
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM trades "
                # Entry rows for metadata: BUY/SHORT for stock entries,
                # plus option SELL rows (multileg short legs use
                # side='sell' for sell-to-open — same overloaded
                # vocabulary as stock close-a-long). Without the
                # option-side branch, multileg short legs have no
                # matching entry row → meta={} → timestamp/ai_confidence
                # render as '--' on the dashboard. Caught 2026-05-11.
                # Exclude canceled rows so phantom limit orders that
                # never filled at the broker don't poison the lookup.
                "WHERE (side='buy' OR side='short' "
                "       OR (side='sell' AND occ_symbol IS NOT NULL)) "
                "AND COALESCE(status, 'open') != 'canceled' "
                "ORDER BY timestamp DESC"
            ).fetchall()
        for r in rows:
            # Key by OCC for option legs (each contract is its own
            # journal entry); by underlying symbol for stock. This
            # mirrors the get_virtual_positions output keying.
            occ = r["occ_symbol"] if "occ_symbol" in r.keys() else None
            key = occ if occ else r["symbol"]
            if key not in trade_meta:  # keep most recent open-side trade
                trade_meta[key] = dict(r)
    except Exception as exc:
        logger.warning("Could not enrich positions for profile %d: %s",
                       profile_id, exc)

    out = []
    for p in positions:
        # Phase 3 of Position class refactor: pos.is_option /
        # pos.broker_symbol / pos.display_symbol are the canonical
        # attributes. Metadata lookup keys by OCC for options (every
        # leg is a distinct journal row), by underlying for stocks.
        is_option = getattr(p, "is_option", False) or bool(p.get("occ_symbol"))
        meta_key = (p.get("occ_symbol") if is_option
                    else getattr(p, "display_symbol", None) or p["symbol"])
        meta = trade_meta.get(meta_key, {})
        side = "sell" if p.get("qty", 0) < 0 else "buy"
        out.append({
            "timestamp": meta.get("timestamp"),
            # display_symbol is always the underlying — what humans
            # recognize. The macro renders it as the strong header
            # and the OCC underneath as a contract detail.
            "symbol": getattr(p, "display_symbol", None) or p["symbol"],
            "occ_symbol": p.get("occ_symbol"),
            "side": side,
            "qty": abs(p["qty"]),
            "price": p["avg_entry_price"],
            "current_price": p["current_price"],
            "market_value": p["market_value"],
            "ai_confidence": meta.get("ai_confidence"),
            "ai_reasoning": meta.get("ai_reasoning"),
            "reason": meta.get("reason"),
            "stop_loss": meta.get("stop_loss"),
            "take_profit": meta.get("take_profit"),
            "decision_price": meta.get("decision_price"),
            "fill_price": meta.get("fill_price"),
            "slippage_pct": meta.get("slippage_pct"),
            # 2026-05-21 — surface order_id + option_strategy +
            # signal_type so the trades-table macro's multileg
            # grouping logic (which uses order_id as the spread
            # group key) actually fires on dashboard positions. Pre-
            # this, only the /trades route's raw-trades query carried
            # these fields, so the dashboard rendered each leg as an
            # independent row with no SPREAD header. Pulling from
            # `meta` since that's the most-recent entry trade for
            # this leg, where the combo order_id was set.
            "order_id": meta.get("order_id"),
            "option_strategy": meta.get("option_strategy"),
            "signal_type": meta.get("signal_type"),
            "expiry": meta.get("expiry"),
            "pnl": None,
            "unrealized_pl": p["unrealized_pl"],
            "unrealized_plpc": p["unrealized_plpc"],
            # 2026-05-15 — surface conviction-TP override so the UI
            # can communicate WHICH exit logic is managing this
            # position. When the override is active for a position
            # the trailing stop manages the exit, NOT the fixed TP
            # shown above. Without this flag the UI displays a
            # stale fixed target and the operator sees a position
            # past its target with no exit and assumes a bug.
            "exit_logic": _resolve_exit_logic(ctx, meta),
        })
    # Phase 4 of Position class refactor: group multileg legs into
    # Spread objects so the macro can render per-spread P&L capped
    # at structural max loss. Eliminates the per-leg -10100% display
    # that broker stale-marks on illiquid OTM options produced
    # (caught 2026-05-11: PCG bull_call_spread legs showed -$505
    # loss on a position with $230 structural max loss).
    #
    # For each leg in a recognized Spread, stamp:
    #   spread_pnl, spread_pnl_pct, spread_max_loss, spread_group_key
    # The macro reads spread_pnl when present and renders it instead
    # of the per-leg unrealized_pl. Per-leg numbers stay accessible
    # in the expand-row for diagnostics.
    try:
        from spread import group_into_spreads
        # Reconstruct Position objects from the enriched dicts so
        # we can run the grouper. (Original Position objects already
        # exist in `positions` but `out` has the enriched dict shape
        # the macro consumes.)
        option_legs_for_grouping = [
            p for p in positions
            if getattr(p, "is_option", False) or p.get("occ_symbol")
        ]
        # journal_rows = the trade_meta we built above flattened back
        journal_rows = list(trade_meta.values())
        if option_legs_for_grouping and journal_rows:
            spreads, _ungrouped = group_into_spreads(
                option_legs_for_grouping, journal_rows,
            )
            # Build OCC -> spread lookup
            spread_by_occ = {}
            for sp in spreads:
                for leg in sp.legs:
                    spread_by_occ[leg.occ_symbol] = sp
            # Stamp spread-level fields onto matching out-rows
            for row in out:
                occ = row.get("occ_symbol")
                if not occ or occ not in spread_by_occ:
                    continue
                sp = spread_by_occ[occ]
                row["spread_pnl"] = sp.display_unrealized_pl
                row["spread_pnl_pct"] = sp.display_unrealized_pl_pct
                row["spread_max_loss"] = sp.structural_max_loss
                row["spread_strategy"] = sp.strategy_name
                row["spread_group_key"] = (
                    f"{sp.strategy_name}/{sp.underlying}/"
                    f"{sp.earliest_entry_ts}"
                )
    except Exception as exc:
        logger.warning(
            "Spread grouping failed for profile %d: %s — falling back "
            "to per-leg P&L display", profile_id, exc,
        )

    # Most recently opened positions first
    out.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return out


def _safe_pending_orders(ctx):
    """Fetch open/accepted Alpaca orders for THIS profile only.

    Multiple profiles share each Alpaca account (10 profiles → 3
    accounts). `api.list_orders()` returns every open order on the
    shared account, including ones placed by other profiles. To show
    only orders this profile owns, we cross-reference each order's id
    against the profile's trades table (entry order_id +
    protective_stop_order_id + protective_tp_order_id +
    protective_trailing_order_id). Orders whose id isn't in our
    tables belong to a sibling profile and are hidden from this view.

    After-hours submissions queue as `accepted` until the next market
    session. Without surfacing them, the dashboard looks deceptively
    empty — the user can't tell a sitting order from a no-op cycle.
    """
    try:
        api = ctx.get_alpaca_api()
        orders = api.list_orders(status="open", limit=200)

        # Build the set of order IDs this profile owns. Pulled fresh
        # each call rather than cached because protective IDs churn
        # cycle-to-cycle as positions open/close. When ctx has no
        # db_path attribute (older test fixtures, ad-hoc invocations),
        # owned_ids stays None → fail-open.
        owned_ids = None
        db_path = getattr(ctx, "db_path", None)
        if db_path:
            try:
                # open_profile_db ensures init_tracker_db + journal.init_db
                # (which runs _migrate_columns), so all four protective-
                # order columns are guaranteed to exist before the SELECT.
                with closing(open_profile_db(db_path)) as conn:
                    ids: set = set()
                    for col in ("order_id", "protective_stop_order_id",
                                 "protective_tp_order_id",
                                 "protective_trailing_order_id"):
                        rows = conn.execute(
                            f"SELECT {col} FROM trades WHERE {col} IS NOT NULL"
                        ).fetchall()
                        ids.update(r[0] for r in rows if r[0])
                    owned_ids = ids
            except Exception as exc:
                logger.warning(
                    "_safe_pending_orders: could not load owned order IDs from %s: %s",
                    db_path, exc,
                )
                # Fail open — better to show extra orders than none at all
                owned_ids = None

        out = []
        for o in orders:
            # Filter: only show orders this profile placed
            if owned_ids is not None and o.id not in owned_ids:
                continue
            try:
                qty = float(o.qty) if o.qty else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            # Capture every flavor of order's pricing field so the
            # dashboard always has SOMETHING to show in the price
            # column — the user's complaint: a trailing-stop row
            # showing "—" is unhelpful when the broker has a
            # current stop price + trail distance available.
            def _f(attr):
                v = getattr(o, attr, None)
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            limit_price = _f("limit_price")
            stop_price = _f("stop_price")
            trail_percent = _f("trail_percent")
            trail_price = _f("trail_price")
            hwm = _f("hwm")
            submitted_at_iso = (
                str(o.submitted_at) if getattr(o, "submitted_at", None)
                else None
            )
            # Pre-format the timestamp the same way every other table
            # on the site does so the JS auto-refresh path doesn't
            # have to re-implement timezone math. The Jinja filter
            # is the canonical formatter.
            from display_names import friendly_time, humanize
            submitted_at_friendly = (
                friendly_time(submitted_at_iso) if submitted_at_iso else None
            )
            # `order_type_label` pre-humanized server-side so the JS
            # doesn't re-implement humanize() (which it used to do via
            # a custom `humanizeJs()` function — Issue 13). Single
            # source of truth: any new entry in display_names._DISPLAY_NAMES
            # picks up here without a JS edit.
            out.append({
                "symbol": o.symbol,
                "side": o.side,
                "qty": qty,
                "order_type": o.order_type,
                "order_type_label": humanize(o.order_type),
                "limit_price": limit_price,
                "stop_price": stop_price,
                "trail_percent": trail_percent,
                "trail_price": trail_price,
                "hwm": hwm,
                "status": o.status,
                "submitted_at": submitted_at_iso,
                "submitted_at_friendly": submitted_at_friendly,
                "time_in_force": o.time_in_force,
            })
        return out
    except Exception as exc:
        logger.warning("Could not fetch pending orders for %s: %s",
                       ctx.display_name or ctx.segment, exc)
        return []


def _get_trade_history_for_profile(profile_id, limit=100, kind=None,
                                   search=None):
    """Get trade history from the profile's journal DB.

    Args:
        kind: 'stocks' (occ_symbol IS NULL), 'options' (occ_symbol
            IS NOT NULL), or None (all). Wired 2026-05-11 for the
            dashboard/trades tab split.
        search: optional case-insensitive symbol prefix filter. Matches
            on `symbol` AND on `occ_symbol`'s underlying root, so
            "CWAN" finds both stock CWAN rows and CWAN option leg
            rows. SQL-injection-safe via parameter binding.
            Wired 2026-05-11 (TODO #3).
    """
    db_path = f"quantopsai_profile_{profile_id}.db"
    where = []
    params = []
    if kind == "stocks":
        where.append("occ_symbol IS NULL")
    elif kind == "options":
        where.append("occ_symbol IS NOT NULL")
    if search and isinstance(search, str):
        # Match symbol prefix (case-insensitive) OR OCC substring
        # (OCCs start with the underlying root, so a prefix match
        # works for both: 'CWAN' matches 'CWAN' stock + 'CWAN260612...').
        s = search.strip().upper()
        if s:
            where.append("(UPPER(symbol) LIKE ? OR "
                         "UPPER(COALESCE(occ_symbol, '')) LIKE ?)")
            params.extend([f"{s}%", f"{s}%"])
    sql = "SELECT * FROM trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    try:
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(
            "_get_trade_history_for_profile(profile_id=%d, kind=%s, "
            "search=%r): could not read trades from %s: %s",
            profile_id, kind, search, db_path, exc,
        )
        return []


def _enrich_trade_history_with_live_pnl(trades, ctx):
    """Attach `current_price`, `unrealized_pl`, `unrealized_plpc` from
    Alpaca's live position list to the most recent journal row per OCC
    (option) or symbol (stock) whose position is currently open. ALSO
    runs the spread grouper for open multileg legs so the trades
    page's SPREAD header can show the Unrealized P&L the same way the
    dashboard does (2026-05-21).

    Without per-leg enrichment: BUY rows and the SELL leg of a
    multileg open render blank P&L on /trades — because realized P&L
    only lives on the closing trade, and the dashboard's
    `_enriched_positions` was the only path injecting unrealized P&L
    for the shared `_trades_table.html` macro. Caught 2026-05-10:
    every open option leg displayed `--` in the P&L column.

    Without spread-level enrichment: the SPREAD header on /trades
    rendered "Net credit $X" but no Unrealized clause, because
    `spread_pnl` was only stamped by `_enriched_positions` on the
    dashboard path. Caught 2026-05-21.

    Mutates `trades` in place. Only the most recent journal row per
    position key gets enriched, so historical adds-to-position don't
    each show the same position-level unrealized P&L. Spread fields
    are stamped on EVERY leg of an open multileg group so the
    template macro's header (which reads from the first leg) always
    has the data.
    """
    if not trades:
        return
    positions = _safe_positions(ctx)
    if not positions:
        return
    pos_by_key = {}
    for p in positions:
        occ = p.get("occ_symbol")
        key = occ if occ else p.get("symbol")
        if key:
            pos_by_key[key] = p
    seen = set()
    for t in trades:  # _get_trade_history_for_profile returns DESC
        occ = t.get("occ_symbol")
        key = occ if occ else t.get("symbol")
        if not key or key in seen or key not in pos_by_key:
            continue
        p = pos_by_key[key]
        t["current_price"] = p.get("current_price")
        t["unrealized_pl"] = p.get("unrealized_pl")
        t["unrealized_plpc"] = p.get("unrealized_plpc")
        t["market_value"] = p.get("market_value")
        seen.add(key)

    # 2026-05-21 — Spread-level enrichment for /trades.
    # Mirror the dashboard's spread grouping (views._enriched_positions)
    # so trade rows for currently-open multileg legs carry spread_pnl /
    # spread_pnl_pct / spread_max_loss. Stamps on EVERY matching leg
    # (not just one per group) so the macro's header — which reads
    # from the first leg in iteration order — always has the data.
    # For closed legs (no matching open position) the stamping is a
    # no-op, which is correct: a closed spread has realized P&L per
    # leg already, no unrealized to compute.
    try:
        from spread import group_into_spreads
        option_legs_for_grouping = [
            p for p in positions
            if getattr(p, "is_option", False) or p.get("occ_symbol")
        ]
        # Build the "journal rows" the grouper expects from the
        # currently-OPEN multileg trade rows we already pulled.
        journal_rows = [
            t for t in trades
            if t.get("occ_symbol")
            and t.get("signal_type") in ("MULTILEG", "MULTILEG_OPEN")
        ]
        if option_legs_for_grouping and journal_rows:
            spreads, _ungrouped = group_into_spreads(
                option_legs_for_grouping, journal_rows,
            )
            spread_by_occ = {}
            for sp in spreads:
                for leg in sp.legs:
                    spread_by_occ[leg.occ_symbol] = sp
            for t in trades:
                occ = t.get("occ_symbol")
                if not occ or occ not in spread_by_occ:
                    continue
                sp = spread_by_occ[occ]
                t["spread_pnl"] = sp.display_unrealized_pl
                t["spread_pnl_pct"] = sp.display_unrealized_pl_pct
                t["spread_max_loss"] = sp.structural_max_loss
                t["spread_strategy"] = sp.strategy_name
                t["spread_group_key"] = (
                    f"{sp.strategy_name}/{sp.underlying}/"
                    f"{sp.earliest_entry_ts}"
                )
    except Exception as exc:
        logger.warning(
            "/trades spread enrichment failed (%s: %s) — header will "
            "render only entry credit/debit; per-leg unrealized still "
            "populated above.",
            type(exc).__name__, exc,
        )


def _mask_key(key):
    """Mask an API key for display."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# Human-readable names for trading parameters
PARAMETER_LABELS = {
    "ai_confidence_threshold": "AI Confidence Threshold",
    "max_position_pct": "Max Position Size (%)",
    "max_total_positions": "Max Total Positions",
    "stop_loss_pct": "Stop-Loss (%)",
    "take_profit_pct": "Take-Profit (%)",
    "drawdown_pause_pct": "Drawdown Pause Threshold",
    "drawdown_reduce_pct": "Drawdown Reduce Threshold",
    "short_stop_loss_pct": "Short Stop-Loss (%)",
    "short_take_profit_pct": "Short Take-Profit (%)",
    "enable_short_selling": "Short Selling",
    "enable_self_tuning": "Self-Tuning",
    "enable_consensus": "Multi-Model Consensus",
    "enable_intraday_risk_halt": "Intraday Risk Auto-Halt",
    "enable_stat_arb_pairs": "Statistical Arbitrage Pair Book",
    "enable_portfolio_risk_snapshot": "Portfolio Risk Daily Snapshot",
    "enable_long_vol_hedge": "Long-Vol Portfolio Hedge",
    "long_vol_hedge_drawdown_pct": "Hedge Drawdown Trigger",
    "long_vol_hedge_var_pct": "Hedge VaR Trigger",
    "long_vol_hedge_premium_pct": "Hedge Premium Budget",
    "use_atr_stops": "ATR-Based Stops",
    "use_trailing_stops": "Trailing Stops",
    "use_limit_orders": "Limit Orders",
    "atr_multiplier_sl": "ATR Stop Multiplier",
    "atr_multiplier_tp": "ATR Target Multiplier",
    "trailing_atr_multiplier": "Trailing Stop Multiplier",
    "max_correlation": "Max Correlation",
    "max_sector_positions": "Max Positions per Sector",
    "min_price": "Min Stock Price",
    "max_price": "Max Stock Price",
    "min_volume": "Min Volume",
    "volume_surge_multiplier": "Volume Surge Multiplier",
    "rsi_overbought": "RSI Overbought Threshold",
    "rsi_oversold": "RSI Oversold Threshold",
    "momentum_5d_gain": "5-Day Momentum Gain (%)",
    "momentum_20d_gain": "20-Day Momentum Gain (%)",
    "breakout_volume_threshold": "Breakout Volume Threshold",
    "gap_pct_threshold": "Gap % Threshold",
    "avoid_earnings_days": "Avoid Earnings (days)",
    "skip_first_minutes": "Skip Opening Minutes",
    "strategy_momentum_breakout": "Strategy: Momentum Breakout",
    "strategy_volume_spike": "Strategy: Volume Spike",
    "strategy_mean_reversion": "Strategy: Mean Reversion",
    "strategy_gap_and_go": "Strategy: Gap and Go",
    "maga_mode": "MAGA Mode",
}


def _format_param_name(name):
    """Convert a parameter key to a human-readable label.

    Delegates to display_names.display_name (the single source of truth
    for snake_case → human mapping). Local PARAMETER_LABELS above is
    kept only for backward-compat references; new entries should land
    in display_names._DISPLAY_NAMES.
    """
    if not name:
        return ""
    from display_names import display_name
    return display_name(name)


def _format_param_value(name, value):
    """Convert a tuning-parameter VALUE to a human-readable string.

    Decimal percentages (0.07 → '7.0%'), boolean toggles ('Enabled'),
    integers, plain floats — each rendered correctly. Used by the
    tuning-history API and the weekly digest so the dashboard never
    has to display raw decimals like '0.07 → 0.0805'.
    """
    from display_names import format_param_value
    return format_param_value(name, value)


# Tuning-adjustment categorization is shared with the scheduler
# (tuning_auto_expiry.py reads it for revert decisions) — single
# source of truth lives in tuning_categories.py.
from tuning_categories import categorize as _categorize_tuning_adjustment


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@views_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    return redirect(url_for("auth.login"))


@views_bp.route("/issues")
@login_required
def issues_page():
    """Surface every WARNING/ERROR/CRITICAL from journald, altdata
    cron logs, and scrape_runs in one place. No more hiding silent
    failures behind logger.debug or buried log files.

    Time window + level filter via query params:
      ?hours=24  (default; supports 1, 6, 24, 168)
      ?level=ERROR,CRITICAL  (default: all of WARN/ERR/CRIT)
    """
    from issues_collector import collect_issues
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 168))   # cap at one week
    level_filter = request.args.get("level")
    summary = collect_issues(since_hours=hours, level_filter=level_filter)
    return render_template(
        "issues.html",
        summary=summary,
        hours=hours,
        level_filter=level_filter or "",
    )


@views_bp.route("/shadow")
@login_required
def shadow_page():
    """Scope C of the per-pipeline refactor: cross-path comparison
    dashboard. Shows recent `pipeline_shadow_runs` rows aggregated
    per profile so the operator can monitor agreement between the
    legacy `trade_pipeline.run_trade_cycle` dispatch and the new
    `Pipeline.run_cycle` dispatch — read from any profile DB that
    has rows.

    Surfaces:
      - per-profile recent rows (last 50)
      - rolling agreement % over the last N cycles
      - per-layer divergence breakdown
      - total shadow AI cost
    """
    import sqlite3, json as _json
    from contextlib import closing as _closing
    profiles = get_user_profiles(current_user.effective_user_id)
    per_profile = []
    for p in profiles:
        pid = p["id"]
        db = f"/opt/quantopsai/quantopsai_profile_{pid}.db"
        try:
            with _closing(sqlite3.connect(db)) as conn:
                conn.row_factory = sqlite3.Row
                # Verify table exists (migrations may lag)
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='pipeline_shadow_runs'"
                ).fetchone()
                if not exists:
                    continue
                rows = list(conn.execute(
                    "SELECT * FROM pipeline_shadow_runs "
                    "ORDER BY id DESC LIMIT 50"
                ).fetchall())
                if not rows:
                    continue
                rolling = [r["verdict_diff"] for r in rows[:20]]
                # Compute agreement %
                agreements = []
                for vd in rolling:
                    try:
                        d = _json.loads(vd) if vd else {}
                        a = d.get("agreement_pct")
                        if a is not None:
                            agreements.append(a)
                    except Exception as _vd_exc:
                        # Skip malformed verdict_diff rows but log so a
                        # systemic corruption pattern surfaces.
                        logger.debug(
                            "shadow_page: skipped malformed "
                            "verdict_diff (%s)",
                            type(_vd_exc).__name__,
                        )
                rolling_agreement = (
                    round(sum(agreements) / len(agreements), 1)
                    if agreements else None
                )
                total_cost = 0.0
                for r in rows:
                    try:
                        sd = _json.loads(r["symbols_diff"] or "{}")
                        total_cost += float(
                            sd.get("aggregate", {}).get("shadow_ai_cost_usd", 0)
                        )
                    except Exception as _sd_exc:
                        logger.debug(
                            "shadow_page: skipped malformed "
                            "symbols_diff (%s)",
                            type(_sd_exc).__name__,
                        )
                # Decode JSON columns to dicts so the template
                # doesn't need a custom filter
                decoded_rows = []
                for r in rows:
                    d = dict(r)
                    for k in ("legacy_symbols", "pipeline_symbols",
                               "symbols_diff", "verdict_diff"):
                        try:
                            d[k] = _json.loads(d[k]) if d.get(k) else {}
                        except Exception:
                            d[k] = {}
                    # Promote agreement_pct + layers_with_divergence
                    # from nested JSON to top-level for easier rendering
                    sd = d.get("symbols_diff", {}) or {}
                    agg = sd.get("aggregate", {}) if isinstance(sd, dict) else {}
                    vd = d.get("verdict_diff", {}) or {}
                    d["agreement_pct"] = (
                        vd.get("agreement_pct") if isinstance(vd, dict) else None
                    )
                    d["layers_with_divergence"] = agg.get(
                        "layers_with_divergence", 0,
                    )
                    d["shadow_ai_cost_usd"] = agg.get(
                        "shadow_ai_cost_usd", 0,
                    )
                    decoded_rows.append(d)
                per_profile.append({
                    "profile": p,
                    "rows": decoded_rows,
                    "rolling_agreement_pct": rolling_agreement,
                    "total_shadow_cost_usd": round(total_cost, 4),
                    "shadow_eval_enabled": bool(
                        p.get("enable_pipeline_shadow_eval", 0)
                    ),
                    "row_count": len(rows),
                })
        except Exception as exc:
            logger.warning(
                "shadow_page: read profile %d failed: %s", pid, exc,
            )
    return render_template("shadow.html",
                            per_profile=per_profile,
                            profiles=profiles)


@views_bp.route("/api/issues-count")
@login_required
def api_issues_count():
    """Lightweight count for the nav badge — fetched async by JS so
    it doesn't add latency to every page render."""
    from issues_collector import issues_count
    return jsonify(issues_count(since_hours=24))


@views_bp.route("/api/comparative-returns")
@login_required
def api_comparative_returns():
    """Time-series % return for every active profile, tagged by
    strategy_type so the dashboard chart can highlight the buy_hold
    and random baselines distinctly. See docs/15."""
    from comparative_returns import build_payload
    return jsonify(build_payload(user_id=current_user.effective_user_id))


@views_bp.route("/dashboard")
@login_required
def dashboard():
    profiles = get_active_profiles(user_id=current_user.effective_user_id)
    profiles_data = []

    def _load_profile(prof):
        """Load one profile's data. Called in parallel."""
        try:
            ctx = build_user_context_from_profile(prof["id"])
            account = _safe_account_info(ctx)
            positions = _enriched_positions(ctx, prof["id"])
            # Split into stock vs option for the dashboard's Open
            # Positions tabs. Each instrument class has different
            # row shapes — options need OPT badge + OCC + per-spread
            # P&L, stocks need shares + share price. Tabs render
            # each cleanly without conditionals. Wired 2026-05-11.
            stock_positions = [p for p in positions
                               if not p.get("occ_symbol")]
            option_positions = [p for p in positions
                                if p.get("occ_symbol")]
            pending_orders = _safe_pending_orders(ctx)
            try:
                from ai_cost_ledger import spend_summary
                # Match the defensive `.get(...) or {}` chain used by
                # /api/dashboard-totals so the initial page render
                # doesn't display 0 when spend_summary returns an
                # unusual shape (e.g. dict without 'today' key on a
                # freshly-initialized DB). Pre-fix the bare
                # `[...][...]` access would KeyError, the except
                # caught it, and the column rendered 0 until the
                # 30s API refresh repopulated.
                _ss = spend_summary(ctx.db_path) or {}
                cost_today = float(
                    (_ss.get("today") or {}).get("usd") or 0
                )
            except Exception as exc:
                logger.warning(
                    "dashboard server-render: spend_summary failed "
                    "for profile %s: %s", prof.get("id"), exc,
                )
                cost_today = 0
            return {
                "id": prof["id"],
                "name": prof["name"],
                "market_type": prof["market_type"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "account": account,
                "positions": positions,  # legacy — preserved for any
                                         # consumer not yet using tabs
                "stock_positions": stock_positions,
                "option_positions": option_positions,
                "pending_orders": pending_orders,
                "is_virtual": getattr(ctx, "is_virtual", False),
                # Keep the raw float — DO NOT round to 2 decimals here.
                # Pre-rounding per-profile destroys precision when costs
                # are < $0.005 each: round(0.0033, 2) = 0.0 → template
                # accumulates zeros → footer total shows $0.00 even
                # though the true sum is $0.01+. The API path stores
                # the raw float and lets JS / template format at display
                # time (`{:.2f}`), which preserves the precision through
                # the summation. Caught 2026-05-18: 13 profiles each
                # with ~$0.001 AI cost rendered as $0.00 footer until
                # the 30s API refresh showed real $0.01.
                "cost_today": cost_today,
                # 2026-05-18 — initial_capital must be in the server-
                # render dict so the overview-table P&L column renders
                # real values on the very first page paint. Without
                # this the template's `if prof.initial_capital`
                # branch falls through to 0 and the column shows $0
                # until the 30s /api/dashboard-totals poll lands.
                # Same pattern as the cost_today column.
                "initial_capital": float(prof.get("initial_capital") or 0),
            }
        except Exception as exc:
            logger.warning("Dashboard error for profile #%d: %s", prof["id"], exc)
            return {
                "id": prof["id"],
                "name": prof["name"],
                "market_type": prof["market_type"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "account": None,
                "positions": [],
                "stock_positions": [],
                "option_positions": [],
                "pending_orders": [],
                "is_virtual": False,
                "error": str(exc),
            }

    # Load all profiles in parallel — 10 sequential Alpaca API calls
    # was taking 17+ seconds; parallel cuts it to ~3-4s.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as pool:
        profiles_data = list(pool.map(_load_profile, profiles))

    # Check for recent scan failures across all profiles
    scan_failures = []
    for prof in profiles:
        db = f"quantopsai_profile_{prof['id']}.db"
        try:
            with closing(open_profile_db(db)) as conn:
                fails = conn.execute(
                    "SELECT task_name, started_at FROM task_runs "
                    "WHERE status='failed' AND started_at >= datetime('now', '-1 hour') "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchall()
            for f in fails:
                scan_failures.append({
                    "profile_name": prof["name"],
                    "task": f["task_name"],
                    "time": f["started_at"],
                })
        except Exception as exc:
            # task_runs read failure should not break the dashboard but
            # MUST surface so we can diagnose. Logging per-profile lets
            # us see whether one DB is broken vs the whole batch.
            logger.warning(
                "dashboard: scan_failures lookup failed for profile %s: %s",
                prof.get("id"), exc,
            )

    # Build per-profile schedule status
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    _now = _dt.now(ZoneInfo("America/New_York"))
    any_profile_active = False
    profile_schedules = []

    all_profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    for prof in all_profiles:
        if not prof.get("enabled"):
            continue
        try:
            ctx = build_user_context_from_profile(prof["id"])
            active = ctx.is_within_schedule(_now)
            if active:
                any_profile_active = True

            # Determine next session text
            if active:
                next_session = ""
            elif ctx.schedule_type == "24_7":
                next_session = "Always on"
            elif ctx.schedule_type in ("market_hours", "extended_hours"):
                weekday = _now.weekday()
                start = "9:30 AM" if ctx.schedule_type == "market_hours" else "4:00 AM"
                if weekday < 5 and _now.hour < 16:
                    next_session = f"{start} ET today"
                elif weekday >= 4:
                    next_session = f"{start} ET Monday"
                else:
                    next_session = f"{start} ET tomorrow"
            else:
                next_session = f"{ctx.custom_start} ET"

            # Per-profile scan timing from task_runs
            next_scan_text = ""
            if active:
                try:
                    with closing(open_profile_db(ctx.db_path)) as _c:
                        row = _c.execute(
                            "SELECT started_at FROM task_runs "
                            "WHERE task_name LIKE '%Scan%' AND status IN ('completed','failed') "
                            "ORDER BY started_at DESC LIMIT 1"
                        ).fetchone()
                    if row:
                        from datetime import datetime as _dt2
                        last_scan_dt = _dt2.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S")
                        elapsed = (_dt.now(ZoneInfo("UTC")).replace(tzinfo=None) - last_scan_dt).total_seconds()
                        remaining = max(0, 900 - elapsed)  # 15 min = 900s
                        if remaining > 0:
                            mins = int(remaining // 60)
                            next_scan_text = f"{mins}m"
                        else:
                            next_scan_text = "Due"
                except Exception as exc:
                    # next_scan_text falls back to "" (no scan-time
                    # callout); the rest of the row still renders.
                    logger.warning(
                        "dashboard: next_scan_text build failed for profile %s: %s",
                        prof.get("id"), exc,
                    )

            profile_schedules.append({
                "profile_id": prof["id"],
                "name": prof["name"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "active": active,
                "next_session": next_session,
                "next_scan_text": next_scan_text,
                "schedule_type": ctx.schedule_type,
            })
        except Exception as exc:
            logger.warning(
                "dashboard: profile_schedule build failed for profile %s: %s",
                prof.get("id"), exc,
            )

    # Master kill-switch state for the banner
    try:
        from kill_switch import is_active as _ks_is_active
        _ks_on, _ks_reason = _ks_is_active()
        kill_switch_state = {"enabled": _ks_on, "reason": _ks_reason}
    except Exception:
        kill_switch_state = {"enabled": False, "reason": ""}

    # Cost cap status — surfaces a banner when today's spend reaches
    # the daily ceiling (AI calls now hard-block; the banner explains
    # why no new entries are landing).
    try:
        from cost_guard import status as _cost_status
        cost_status = _cost_status(current_user.effective_user_id)
    except Exception as _cs_exc:
        logger.warning("dashboard: cost_status build failed: %s", _cs_exc)
        cost_status = None

    return render_template("dashboard.html",
                           profiles_data=profiles_data,
                           any_profile_active=any_profile_active,
                           profile_schedules=profile_schedules,
                           scan_failures=scan_failures,
                           kill_switch=kill_switch_state,
                           cost_status=cost_status)


@views_bp.route("/settings")
@login_required
def settings():
    user = get_user_by_id(current_user.effective_user_id)

    # Decrypt keys for display (masked)
    alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
    alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))
    # 2026-05-19 — column `anthropic_api_key_enc` now stores any
    # provider's key (per `users.llm_provider`). UI label is
    # "Fallback LLM Key"; column rename is a future refactor.
    llm_key = decrypt(user.get("anthropic_api_key_enc", ""))
    llm_provider = user.get("llm_provider") or "anthropic"
    # 2026-05-21 — same-provider fallback model. The model select on
    # the Settings page reads from this; empty/None means "use the
    # provider's default model" for back-compat.
    llm_model = user.get("llm_model") or ""
    resend_key = decrypt(user.get("resend_api_key_enc", ""))
    notification_email = user.get("notification_email", "")

    keys = {
        "alpaca_api_key": _mask_key(alpaca_key),
        "alpaca_secret_key": _mask_key(alpaca_secret),
        "llm_api_key": _mask_key(llm_key),
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "resend_api_key": _mask_key(resend_key),
        "notification_email": notification_email,
        "has_alpaca": bool(alpaca_key),
        "has_llm": bool(llm_key),
        "has_resend": bool(resend_key),
    }

    # Get trading profiles
    profiles = get_user_profiles(current_user.effective_user_id)

    # Add masked Alpaca key for each profile
    for prof in profiles:
        enc_key = prof.get("alpaca_api_key_enc", "")
        if enc_key:
            try:
                decrypted = decrypt(enc_key)
                prof["_alpaca_key_masked"] = _mask_key(decrypted)
            except Exception:
                prof["_alpaca_key_masked"] = "****"

    # Get excluded symbols
    from models import get_excluded_symbols
    excluded = get_excluded_symbols(current_user.effective_user_id)
    excluded_str = ", ".join(excluded)

    ai_providers = get_providers()

    from models import get_alpaca_accounts
    alpaca_accounts = get_alpaca_accounts(current_user.effective_user_id)
    for acct in alpaca_accounts:
        try:
            acct["_key_masked"] = _mask_key(decrypt(acct.get("alpaca_api_key_enc", "")))
        except Exception:
            acct["_key_masked"] = "****"

    # Layer 9 — auto capital allocation user opt-in + cost ceiling
    autonomy = {
        "auto_capital_allocation": bool(user.get("auto_capital_allocation", 0)),
        "daily_cost_ceiling_usd": user.get("daily_cost_ceiling_usd"),
        "shadow_daily_cost_cap_usd": user.get("shadow_daily_cost_cap_usd"),
    }
    try:
        from cost_guard import status as _cost_status
        autonomy["cost_status"] = _cost_status(current_user.effective_user_id)
    except Exception as exc:
        # Cost status is rendered next to the spend ceiling field. If
        # it fails to compute we still want the settings page to render
        # the field itself — log so the failure shows up in the
        # journal rather than silently rendering "--".
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "settings cost_status fetch failed: %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        autonomy["cost_status"] = None
    try:
        from shadow_eval import shadow_status as _shadow_status
        autonomy["shadow_status"] = _shadow_status(current_user.effective_user_id)
    except Exception as exc:
        # Same shape as cost_status above. Shadow eval is observational
        # only — a failed status read must never block the settings
        # page from rendering, but it must be discoverable.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "settings shadow_status fetch failed: %s: %s",
            type(exc).__name__, exc, exc_info=True,
        )
        autonomy["shadow_status"] = None

    return render_template("settings.html",
                           keys=keys,
                           profiles=profiles,
                           market_types=MARKET_TYPE_NAMES,
                           segments=SEGMENTS,
                           excluded_symbols=excluded_str,
                           ai_providers=ai_providers,
                           ai_providers_json=json.dumps(ai_providers),
                           alpaca_accounts=alpaca_accounts,
                           autonomy=autonomy)


@views_bp.route("/settings/autonomy", methods=["POST"])
@login_required
@admin_required
def update_autonomy():
    """Toggle the per-user opt-in autonomy flags + cost ceiling
    override. Admin-only — viewers must not be able to change the
    admin's cost ceiling or autonomy state."""
    from models import _get_conn
    enabled = 1 if request.form.get("auto_capital_allocation") else 0

    # Cost ceiling: empty string clears the override (back to auto-compute)
    raw_ceiling = (request.form.get("daily_cost_ceiling_usd") or "").strip()
    if raw_ceiling == "":
        ceiling_value = None
    else:
        try:
            ceiling_value = float(raw_ceiling)
            if ceiling_value <= 0:
                ceiling_value = None  # zero or negative = clear
        except ValueError:
            flash(f"Invalid cost ceiling value: {raw_ceiling!r}", "error")
            return redirect(url_for("views.settings") + "#autonomy")

    # Shadow eval daily cap — empty string clears the override (back
    # to SHADOW_DAILY_COST_CAP_USD env-var default).
    raw_shadow_cap = (request.form.get("shadow_daily_cost_cap_usd") or "").strip()
    if raw_shadow_cap == "":
        shadow_cap_value = None
    else:
        try:
            shadow_cap_value = float(raw_shadow_cap)
            if shadow_cap_value <= 0:
                shadow_cap_value = None
        except ValueError:
            flash(f"Invalid shadow cost cap value: {raw_shadow_cap!r}", "error")
            return redirect(url_for("views.settings") + "#autonomy")

    with closing(_get_conn()) as conn:
        conn.execute(
            "UPDATE users SET auto_capital_allocation = ?, "
            " daily_cost_ceiling_usd = ?, "
            " shadow_daily_cost_cap_usd = ? WHERE id = ?",
            (enabled, ceiling_value, shadow_cap_value,
             current_user.effective_user_id),
        )
        conn.commit()
    msgs = ["Auto capital allocation " + ("enabled" if enabled else "disabled") + "."]
    if ceiling_value is None:
        msgs.append("Cost ceiling: auto-computed (trailing-7d-avg × 1.5).")
    else:
        msgs.append(f"Cost ceiling locked to ${ceiling_value:.2f}/day.")
    if shadow_cap_value is None:
        msgs.append("Shadow eval cap: env default ($1/day).")
    else:
        msgs.append(
            f"Shadow eval cap locked to ${shadow_cap_value:.2f}/day."
        )
    flash(" ".join(msgs), "success")
    return redirect(url_for("views.settings") + "#autonomy")


@views_bp.route("/settings/exclusions", methods=["POST"])
@login_required
@admin_required
def save_exclusions():
    raw = request.form.get("excluded_symbols", "").strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    from models import update_excluded_symbols
    update_excluded_symbols(current_user.effective_user_id, symbols)
    if symbols:
        flash(f"Restricted symbols updated: {', '.join(symbols)}", "success")
    else:
        flash("Restricted symbols cleared.", "success")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/keys", methods=["POST"])
@login_required
@admin_required
def save_keys():
    alpaca_key = request.form.get("alpaca_api_key", "").strip()
    alpaca_secret = request.form.get("alpaca_secret_key", "").strip()
    # 2026-05-19 — generalised: was `anthropic_api_key`, now
    # `llm_api_key` paired with `llm_provider` so the field can hold
    # a key for any supported provider.
    llm_key = request.form.get("llm_api_key", "").strip()
    llm_provider = request.form.get("llm_provider", "").strip() or None
    # 2026-05-21 — same-provider fallback model. Empty string is a
    # legitimate value (clear the explicit override → use the
    # provider's default model).
    llm_model = request.form.get("llm_model", "").strip()
    notification_email = request.form.get("notification_email", "").strip()
    resend_key = request.form.get("resend_api_key", "").strip()

    # Only update fields that were actually provided (not masked placeholders)
    user = get_user_by_id(current_user.effective_user_id)
    current_alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
    current_alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))
    current_llm_key = decrypt(user.get("anthropic_api_key_enc", ""))
    current_resend_key = decrypt(user.get("resend_api_key_enc", ""))

    # If the form value looks masked (contains ****), keep the existing key
    if "****" in alpaca_key:
        alpaca_key = current_alpaca_key
    if "****" in alpaca_secret:
        alpaca_secret = current_alpaca_secret
    if "****" in llm_key:
        llm_key = current_llm_key
    if "****" in resend_key:
        resend_key = current_resend_key

    update_user_credentials(
        current_user.effective_user_id,
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        llm_key=llm_key,
        llm_provider=llm_provider,
        llm_model=llm_model,
        notification_email=notification_email,
        resend_key=resend_key,
    )
    flash("API keys saved successfully.", "success")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/keys/test", methods=["POST"])
@login_required
@admin_required
def test_keys():
    """Test Alpaca connection with the user's saved credentials."""
    try:
        user = get_user_by_id(current_user.effective_user_id)
        alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
        alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))

        if not alpaca_key or not alpaca_secret:
            return jsonify({"success": False, "message": "No Alpaca keys configured."})

        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(
            alpaca_key, alpaca_secret,
            "https://paper-api.alpaca.markets",
            api_version="v2",
        )
        account = api.get_account()
        return jsonify({
            "success": True,
            "message": f"Connected! Account status: {account.status}, "
                       f"Equity: ${float(account.equity):,.2f}",
        })
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


# ---------------------------------------------------------------------------
# Trading Profile routes
# ---------------------------------------------------------------------------

def _get_main_conn():
    """Get a connection to the main quantopsai.db (not per-profile).
    Uses models._get_conn so the master-DB connection inherits the
    same WAL + busy_timeout + foreign_keys PRAGMAs every other
    connection in the codebase uses."""
    return _get_main_db_conn()


@views_bp.route("/settings/alpaca-accounts", methods=["POST"])
@login_required
@admin_required
def manage_alpaca_account():
    """Create or delete a named Alpaca account reference."""
    from models import create_alpaca_account
    action = request.form.get("action", "create")
    if action == "delete":
        account_id = request.form.get("account_id")
        if account_id:
            conn = _get_main_conn()
            conn.execute("DELETE FROM alpaca_accounts WHERE id=? AND user_id=?",
                         (int(account_id), current_user.effective_user_id))
            conn.commit()
            conn.close()
            flash("Alpaca account removed.", "success")
        return redirect(url_for("views.settings"))

    name = request.form.get("account_name", "").strip() or "Paper Account"
    api_key = request.form.get("account_api_key", "").strip()
    secret_key = request.form.get("account_secret_key", "").strip()
    if not api_key or not secret_key:
        flash("Both API key and secret key are required.", "error")
        return redirect(url_for("views.settings"))
    create_alpaca_account(
        current_user.effective_user_id, name,
        encrypt(api_key), encrypt(secret_key),
    )
    flash(f'Alpaca account "{name}" added.', "success")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/profile/create", methods=["POST"])
@login_required
@admin_required
def create_profile():
    name = request.form.get("profile_name", "").strip()
    market_type = request.form.get("market_type", "").strip()

    if not name:
        flash("Profile name is required.", "error")
        return redirect(url_for("views.settings"))

    if market_type not in MARKET_TYPE_NAMES:
        flash("Invalid market type.", "error")
        return redirect(url_for("views.settings"))

    profile_id = create_trading_profile(current_user.effective_user_id, name, market_type)

    # Virtual account setup
    alpaca_account_id = request.form.get("alpaca_account_id", "").strip()
    initial_capital = request.form.get("initial_capital", "100000").strip()
    if alpaca_account_id:
        update_trading_profile(profile_id,
            alpaca_account_id=int(alpaca_account_id),
            is_virtual=1,
            initial_capital=float(initial_capital),
        )

    flash(f'Profile "{name}" created successfully.', "success")
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


@views_bp.route("/settings/profile/<int:profile_id>", methods=["POST"])
@login_required
@admin_required
def save_profile(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)

    form = request.form

    config_updates = {
        "name": form.get("profile_name", profile["name"]).strip(),
        "enabled": 1 if form.get("enabled") else 0,
        "stop_loss_pct": float(form.get("stop_loss_pct", 0.03)),
        "take_profit_pct": float(form.get("take_profit_pct", 0.10)),
        "max_position_pct": float(form.get("max_position_pct", 0.10)),
        "max_total_positions": int(form.get("max_total_positions", 10)),
        "ai_confidence_threshold": int(form.get("ai_confidence_threshold", 25)),
        "min_price": float(form.get("min_price", 1.0)),
        "max_price": float(form.get("max_price", 20.0)),
        "min_volume": int(form.get("min_volume", 500000)),
        "volume_surge_multiplier": float(form.get("volume_surge_multiplier", 2.0)),
        "rsi_overbought": float(form.get("rsi_overbought", 85.0)),
        "rsi_oversold": float(form.get("rsi_oversold", 25.0)),
        "momentum_5d_gain": float(form.get("momentum_5d_gain", 3.0)),
        "momentum_20d_gain": float(form.get("momentum_20d_gain", 5.0)),
        "breakout_volume_threshold": float(form.get("breakout_volume_threshold", 1.0)),
        "gap_pct_threshold": float(form.get("gap_pct_threshold", 3.0)),
        "strategy_momentum_breakout": 1 if form.get("strategy_momentum_breakout") else 0,
        "strategy_volume_spike": 1 if form.get("strategy_volume_spike") else 0,
        "strategy_mean_reversion": 1 if form.get("strategy_mean_reversion") else 0,
        "strategy_gap_and_go": 1 if form.get("strategy_gap_and_go") else 0,
        "maga_mode": 1 if form.get("maga_mode") else 0,
        # 2026-05-19 — per-asset-class enablement flags. Operator
        # explicitly opts in/out of stocks / options / crypto.
        "enable_stocks": 1 if form.get("enable_stocks") else 0,
        "enable_options": 1 if form.get("enable_options") else 0,
        "enable_crypto": 1 if form.get("enable_crypto") else 0,
        # 2026-05-19 Scope C: read-only A/B vs the new Pipeline.run_cycle
        # path. See pipelines/shadow.py — opt-in per profile.
        "enable_pipeline_shadow_eval": 1 if form.get(
            "enable_pipeline_shadow_eval") else 0,
        # 2026-05-19 Scope C cutover: when 1, scheduler uses
        # Pipeline.run_cycle dispatch instead of legacy run_trade_cycle.
        # Submits real orders — only flip after shadow soak passes.
        "use_pipeline_dispatch": 1 if form.get(
            "use_pipeline_dispatch") else 0,
        "enable_short_selling": 1 if form.get("enable_short_selling") else 0,
        "short_stop_loss_pct": float(form.get("short_stop_loss_pct", 0.08)),
        "short_take_profit_pct": float(form.get("short_take_profit_pct", 0.08)),
        # P1.5/P1.6 of LONG_SHORT_PLAN.md — short-side hold + sizing caps.
        "short_max_position_pct": float(form.get("short_max_position_pct", 0.05)),
        "short_max_hold_days": int(form.get("short_max_hold_days", 10)),
        # P2.2 of LONG_SHORT_PLAN.md — long/short balance mandate.
        "target_short_pct": float(form.get("target_short_pct", 0.0)),
        # P4.1 of LONG_SHORT_PLAN.md — book beta target. Empty form
        # value means "no target" (NULL); preserve existing if blank.
        **({"target_book_beta": float(form["target_book_beta"])}
           if form.get("target_book_beta", "").strip() != "" else {}),
        "enable_self_tuning": 1 if form.get("enable_self_tuning") else 0,
        "ai_model_auto_tune": 1 if form.get("ai_model_auto_tune") else 0,
        # Drawdown protection
        "drawdown_reduce_pct": float(form.get("drawdown_reduce_pct", 0.10)),
        "drawdown_pause_pct": float(form.get("drawdown_pause_pct", 0.20)),
        # Earnings calendar
        "avoid_earnings_days": int(form.get("avoid_earnings_days", 2)),
        # Time-of-day patterns
        "skip_first_minutes": int(form.get("skip_first_minutes", 0)),
        # Trading schedule
        "schedule_type": form.get("schedule_type", "market_hours"),
        "custom_start": form.get("custom_start", "09:30"),
        "custom_end": form.get("custom_end", "16:00"),
        "custom_days": ",".join(form.getlist("custom_days")) or "0,1,2,3,4",
    }

    # Correlation management
    config_updates["max_correlation"] = float(form.get("max_correlation", 0.7))
    config_updates["max_sector_positions"] = int(form.get("max_sector_positions", 5))

    # ATR-based stops
    config_updates["use_atr_stops"] = 1 if form.get("use_atr_stops") else 0
    config_updates["atr_multiplier_sl"] = float(form.get("atr_multiplier_sl", 2.0))
    config_updates["atr_multiplier_tp"] = float(form.get("atr_multiplier_tp", 3.0))
    # Trailing stops
    config_updates["use_trailing_stops"] = 1 if form.get("use_trailing_stops") else 0
    config_updates["trailing_atr_multiplier"] = float(form.get("trailing_atr_multiplier", 1.5))
    # Conviction-based take-profit override
    config_updates["use_conviction_tp_override"] = 1 if form.get("use_conviction_tp_override") else 0
    config_updates["conviction_tp_min_confidence"] = float(form.get("conviction_tp_min_confidence", 70.0))
    config_updates["conviction_tp_min_adx"] = float(form.get("conviction_tp_min_adx", 25.0))
    # Limit orders
    config_updates["use_limit_orders"] = 1 if form.get("use_limit_orders") else 0

    # COMPETITIVE_GAP_PLAN feature toggles. Each gates a scheduled
    # task that runs per-profile per-cycle. Without these on the
    # save handler the form values get silently dropped.
    config_updates["enable_intraday_risk_halt"] = (
        1 if form.get("enable_intraday_risk_halt") else 0
    )
    config_updates["enable_stat_arb_pairs"] = (
        1 if form.get("enable_stat_arb_pairs") else 0
    )
    config_updates["enable_portfolio_risk_snapshot"] = (
        1 if form.get("enable_portfolio_risk_snapshot") else 0
    )
    # Item 1c — long-vol hedge toggle + thresholds
    config_updates["enable_long_vol_hedge"] = (
        1 if form.get("enable_long_vol_hedge") else 0
    )
    config_updates["long_vol_hedge_drawdown_pct"] = float(
        form.get("long_vol_hedge_drawdown_pct", 0.05)
    )
    config_updates["long_vol_hedge_var_pct"] = float(
        form.get("long_vol_hedge_var_pct", 0.03)
    )
    config_updates["long_vol_hedge_premium_pct"] = float(
        form.get("long_vol_hedge_premium_pct", 0.01)
    )

    # Multi-model consensus
    config_updates["enable_consensus"] = 1 if form.get("enable_consensus") else 0
    consensus_model = form.get("consensus_model", "").strip()
    config_updates["consensus_model"] = consensus_model
    consensus_api_key = form.get("consensus_api_key", "").strip()
    if consensus_api_key:
        config_updates["consensus_api_key_enc"] = encrypt(consensus_api_key)

    # Shadow model evaluation — parallel candidate-model calls. Stored
    # as JSON: shadow_models is a list of "provider:model" strings,
    # shadow_api_keys_enc is a dict keyed by provider with each value
    # encrypted via crypto.encrypt. Empty submissions clear the list
    # but preserve already-saved keys (the password input is blank
    # unless the user is changing it).
    config_updates["enable_shadow_eval"] = (
        1 if form.get("enable_shadow_eval") else 0
    )
    shadow_models_raw = form.getlist("shadow_models")
    shadow_models_clean = [
        s.strip() for s in shadow_models_raw
        if s and ":" in s
    ]
    config_updates["shadow_models"] = json.dumps(shadow_models_clean)

    # Merge new keys into the existing encrypted dict so unfilled
    # providers don't lose their saved keys on submit.
    try:
        existing_keys = json.loads(profile.get("shadow_api_keys_enc") or "{}")
        if not isinstance(existing_keys, dict):
            existing_keys = {}
    except (TypeError, ValueError):
        existing_keys = {}
    from ai_providers import get_providers as _get_providers
    for provider_key in _get_providers().keys():
        new_val = form.get(f"shadow_api_key_{provider_key}", "").strip()
        if new_val:
            existing_keys[provider_key] = encrypt(new_val)
    config_updates["shadow_api_keys_enc"] = json.dumps(existing_keys)

    # Custom watchlist: parse comma-separated text into a JSON list
    watchlist_raw = form.get("custom_watchlist", "").strip()
    if watchlist_raw:
        symbols = [s.strip().upper() for s in watchlist_raw.split(",") if s.strip()]
        config_updates["custom_watchlist"] = symbols
    else:
        config_updates["custom_watchlist"] = []

    # OPEN_ITEMS #4 — wheel symbols (comma-separated → JSON list)
    wheel_raw = form.get("wheel_symbols", "").strip()
    if wheel_raw:
        wheel_syms = [s.strip().upper() for s in wheel_raw.split(",") if s.strip()]
        config_updates["wheel_symbols"] = json.dumps(wheel_syms)
    else:
        config_updates["wheel_symbols"] = "[]"

    # OPEN_ITEMS #10 — options roll-window knobs
    config_updates["options_roll_window_days"] = max(1, min(
        int(form.get("options_roll_window_days", 7) or 7), 30,
    ))
    config_updates["options_auto_close_profit_pct"] = max(0.10, min(
        float(form.get("options_auto_close_profit_pct", 0.80) or 0.80),
        0.99,
    ))
    config_updates["options_roll_recommend_profit_pct"] = max(0.10, min(
        float(form.get("options_roll_recommend_profit_pct", 0.50) or 0.50),
        0.99,
    ))

    # #195 Phase 2 (docs/23) — Greek-exposure caps. Clamped to
    # PARAM_BOUNDS so a typo'd UI value can't blow out the cap; the
    # self-tuner re-evaluates from outcomes on its next run.
    from param_bounds import clamp as _clamp_bound
    config_updates["max_net_options_delta_pct"] = _clamp_bound(
        "max_net_options_delta_pct",
        float(form.get("max_net_options_delta_pct", 0.05) or 0.05),
    )
    config_updates["max_theta_burn_dollars_per_day"] = _clamp_bound(
        "max_theta_burn_dollars_per_day",
        float(form.get("max_theta_burn_dollars_per_day", 50.0) or 50.0),
    )
    config_updates["max_short_vega_dollars"] = _clamp_bound(
        "max_short_vega_dollars",
        float(form.get("max_short_vega_dollars", 500.0) or 500.0),
    )

    # AI provider/model configuration
    ai_provider = form.get("ai_provider", "").strip()
    ai_model = form.get("ai_model", "").strip()
    ai_api_key = form.get("ai_api_key", "").strip()

    if ai_provider:
        config_updates["ai_provider"] = ai_provider
    if ai_model:
        config_updates["ai_model"] = ai_model
    if ai_api_key:
        config_updates["ai_api_key_enc"] = encrypt(ai_api_key)

    # Per-profile Alpaca keys (only update if new values provided)
    alpaca_key = form.get("alpaca_api_key", "").strip()
    alpaca_secret = form.get("alpaca_secret_key", "").strip()
    if alpaca_key and "****" not in alpaca_key and alpaca_secret:
        config_updates["alpaca_api_key_enc"] = encrypt(alpaca_key)
        config_updates["alpaca_secret_key_enc"] = encrypt(alpaca_secret)
    elif alpaca_key and "****" not in alpaca_key and not alpaca_secret:
        flash("Alpaca secret key is required when updating the API key.", "warning")

    update_trading_profile(profile_id, **config_updates)
    flash(f'Profile "{config_updates["name"]}" saved.', "success")
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


@views_bp.route("/api/backtest/<int:profile_id>", methods=["POST"])
@login_required
@admin_required
def api_backtest(profile_id):
    """Start a backtest in background. Returns job_id immediately."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    market_type = profile["market_type"]

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON payload"}), 400

    # Build current params from saved profile
    current_params = {
        "stop_loss_pct": float(profile.get("stop_loss_pct", 0.03)),
        "take_profit_pct": float(profile.get("take_profit_pct", 0.10)),
        "max_position_pct": float(profile.get("max_position_pct", 0.10)),
        "use_atr_stops": bool(profile.get("use_atr_stops", 1)),
        "atr_multiplier_sl": float(profile.get("atr_multiplier_sl", 2.0)),
        "atr_multiplier_tp": float(profile.get("atr_multiplier_tp", 3.0)),
        "use_trailing_stops": bool(profile.get("use_trailing_stops", 1)),
        "trailing_atr_multiplier": float(profile.get("trailing_atr_multiplier", 1.5)),
        "ai_confidence_threshold": int(profile.get("ai_confidence_threshold", 25)),
        "strategy_momentum_breakout": bool(profile.get("strategy_momentum_breakout", 1)),
        "strategy_volume_spike": bool(profile.get("strategy_volume_spike", 1)),
        "strategy_mean_reversion": bool(profile.get("strategy_mean_reversion", 1)),
        "strategy_gap_and_go": bool(profile.get("strategy_gap_and_go", 1)),
        "rsi_oversold": float(profile.get("rsi_oversold", 25.0)),
        "rsi_overbought": float(profile.get("rsi_overbought", 85.0)),
        "volume_surge_multiplier": float(profile.get("volume_surge_multiplier", 2.0)),
    }

    # Build proposed params from submitted form data
    proposed_params = {
        "stop_loss_pct": float(data.get("stop_loss_pct", current_params["stop_loss_pct"])),
        "take_profit_pct": float(data.get("take_profit_pct", current_params["take_profit_pct"])),
        "max_position_pct": float(data.get("max_position_pct", current_params["max_position_pct"])),
        "use_atr_stops": bool(data.get("use_atr_stops", current_params["use_atr_stops"])),
        "atr_multiplier_sl": float(data.get("atr_multiplier_sl", current_params["atr_multiplier_sl"])),
        "atr_multiplier_tp": float(data.get("atr_multiplier_tp", current_params["atr_multiplier_tp"])),
        "use_trailing_stops": bool(data.get("use_trailing_stops", current_params["use_trailing_stops"])),
        "trailing_atr_multiplier": float(data.get("trailing_atr_multiplier", current_params["trailing_atr_multiplier"])),
        "ai_confidence_threshold": int(data.get("ai_confidence_threshold", current_params["ai_confidence_threshold"])),
        "strategy_momentum_breakout": bool(data.get("strategy_momentum_breakout", current_params["strategy_momentum_breakout"])),
        "strategy_volume_spike": bool(data.get("strategy_volume_spike", current_params["strategy_volume_spike"])),
        "strategy_mean_reversion": bool(data.get("strategy_mean_reversion", current_params["strategy_mean_reversion"])),
        "strategy_gap_and_go": bool(data.get("strategy_gap_and_go", current_params["strategy_gap_and_go"])),
        "rsi_oversold": float(data.get("rsi_oversold", current_params["rsi_oversold"])),
        "rsi_overbought": float(data.get("rsi_overbought", current_params["rsi_overbought"])),
        "volume_surge_multiplier": float(data.get("volume_surge_multiplier", current_params["volume_surge_multiplier"])),
    }

    # Identify what changed for the results display
    param_labels = {
        "stop_loss_pct": "Stop Loss",
        "take_profit_pct": "Take Profit",
        "max_position_pct": "Max Position",
        "use_atr_stops": "ATR Stops",
        "atr_multiplier_sl": "ATR SL Multiplier",
        "atr_multiplier_tp": "ATR TP Multiplier",
        "use_trailing_stops": "Trailing Stops",
        "trailing_atr_multiplier": "Trailing Multiplier",
        "ai_confidence_threshold": "AI Confidence",
        "strategy_momentum_breakout": "Momentum Breakout",
        "strategy_volume_spike": "Volume Spike",
        "strategy_mean_reversion": "Mean Reversion",
        "strategy_gap_and_go": "Gap and Go",
        "rsi_oversold": "RSI Oversold",
        "rsi_overbought": "RSI Overbought",
        "volume_surge_multiplier": "Volume Surge Mult",
    }
    changes = []
    for key in current_params:
        curr_val = current_params[key]
        prop_val = proposed_params[key]
        if curr_val != prop_val:
            label = param_labels.get(key, key)
            if isinstance(curr_val, float) and curr_val < 1:
                changes.append(f"{label}: {curr_val*100:.1f}% → {prop_val*100:.1f}%")
            elif isinstance(curr_val, bool):
                changes.append(f"{label}: {'ON' if curr_val else 'OFF'} → {'ON' if prop_val else 'OFF'}")
            else:
                changes.append(f"{label}: {curr_val} → {prop_val}")

    from backtest_worker import start_backtest
    job_id = start_backtest(market_type, current_params, proposed_params, days=90,
                            changes_summary=changes)
    return jsonify({"job_id": job_id})


@views_bp.route("/api/backtest/status/<job_id>")
@login_required
def api_backtest_status(job_id):
    """Poll for backtest job status."""
    from backtest_worker import get_job_status
    return jsonify(get_job_status(job_id))


@views_bp.route("/settings/profile/<int:profile_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_profile_route(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)

    name = profile["name"]
    delete_trading_profile(profile_id)
    flash(f'Profile "{name}" deleted.', "info")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/profile/<int:profile_id>/clear-halt",
                methods=["POST"])
@login_required
@admin_required
def clear_profile_halt(profile_id):
    """Operator override: clear the reconciler safety-net halt on a
    profile after investigating the root-cause submit_order leak.

    The halt also auto-clears next reconcile pass when no synthesis
    is needed — this route is for the case where the operator wants
    to resume trading sooner (e.g., the orphan was a known one-off
    that's been manually reconciled, or the operator is confident
    the upstream fix is deployed and wants to skip waiting for the
    next 15-min reconcile cron tick).
    """
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)
    from halt_helpers import clear_halt
    cleared = clear_halt(profile_id, source="settings_ui")
    if cleared:
        flash(
            f'Halt cleared on "{profile["name"]}". Trading dispatch '
            f"will resume on the next scheduler cycle.",
            "success",
        )
    else:
        flash(
            f'Profile "{profile["name"]}" was not halted; nothing to clear.',
            "info",
        )
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


@views_bp.route("/settings/profile/<int:profile_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_profile(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)

    new_state = 0 if profile["enabled"] else 1
    update_trading_profile(profile_id, enabled=new_state)
    state_str = "enabled" if new_state else "disabled"
    flash(f'Profile "{profile["name"]}" {state_str}.', "success")
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


@views_bp.route("/ai/profile/<int:profile_id>/restore-strategy/<strategy_type>",
                methods=["POST"])
@login_required
@admin_required
def restore_deprecated_strategy(profile_id, strategy_type):
    """Manually undo an alpha-decay (or self-tuner) deprecation. The
    pipeline will start considering the strategy's signals again on the
    next cycle."""
    import os
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)
    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        abort(404)
    try:
        from alpha_decay import restore_strategy
        restore_strategy(db_path, strategy_type)
        from display_names import display_name
        flash(
            f'Restored "{display_name(strategy_type)}" — back in the active '
            f'strategy mix on the next cycle.',
            "success",
        )
    except Exception as exc:
        logger.warning("Manual restore failed: %s", exc)
        flash(f"Restore failed: {exc}", "error")
    return redirect(url_for("views.ai_dashboard") + "#strategy")


# ---------------------------------------------------------------------------
# Legacy segment routes (kept for backward compatibility)
# ---------------------------------------------------------------------------

@views_bp.route("/settings/segment/<segment>", methods=["POST"])
@login_required
@admin_required
def save_segment(segment):
    if segment not in SEGMENTS:
        abort(404)

    form = request.form

    config_updates = {
        "enabled": 1 if form.get("enabled") else 0,
        "stop_loss_pct": float(form.get("stop_loss_pct", 0.03)),
        "take_profit_pct": float(form.get("take_profit_pct", 0.10)),
        "max_position_pct": float(form.get("max_position_pct", 0.10)),
        "max_total_positions": int(form.get("max_total_positions", 10)),
        "ai_confidence_threshold": int(form.get("ai_confidence_threshold", 25)),
        "min_price": float(form.get("min_price", 1.0)),
        "max_price": float(form.get("max_price", 20.0)),
        "min_volume": int(form.get("min_volume", 500000)),
        "volume_surge_multiplier": float(form.get("volume_surge_multiplier", 2.0)),
        "rsi_overbought": float(form.get("rsi_overbought", 85.0)),
        "rsi_oversold": float(form.get("rsi_oversold", 25.0)),
        "momentum_5d_gain": float(form.get("momentum_5d_gain", 3.0)),
        "momentum_20d_gain": float(form.get("momentum_20d_gain", 5.0)),
        "breakout_volume_threshold": float(form.get("breakout_volume_threshold", 1.0)),
        "gap_pct_threshold": float(form.get("gap_pct_threshold", 3.0)),
        "strategy_momentum_breakout": 1 if form.get("strategy_momentum_breakout") else 0,
        "strategy_volume_spike": 1 if form.get("strategy_volume_spike") else 0,
        "strategy_mean_reversion": 1 if form.get("strategy_mean_reversion") else 0,
        "strategy_gap_and_go": 1 if form.get("strategy_gap_and_go") else 0,
    }

    # Custom watchlist: parse comma-separated text into a JSON list
    watchlist_raw = form.get("custom_watchlist", "").strip()
    if watchlist_raw:
        symbols = [s.strip().upper() for s in watchlist_raw.split(",") if s.strip()]
        config_updates["custom_watchlist"] = symbols
    else:
        config_updates["custom_watchlist"] = []

    # Per-segment Alpaca keys (only update if new values provided)
    alpaca_key = form.get("alpaca_api_key", "").strip()
    alpaca_secret = form.get("alpaca_secret_key", "").strip()
    if alpaca_key and alpaca_secret:
        config_updates["alpaca_api_key_enc"] = encrypt(alpaca_key)
        config_updates["alpaca_secret_key_enc"] = encrypt(alpaca_secret)
    elif alpaca_key and not alpaca_secret:
        flash("Alpaca secret key is required when updating the API key.", "warning")

    update_user_segment_config(current_user.effective_user_id, segment, **config_updates)
    flash(f"{SEGMENTS[segment]['name']} configuration saved.", "success")
    return redirect(url_for("views.settings") + f"#segment-{segment}")


@views_bp.route("/settings/segment/<segment>/reset", methods=["POST"])
@login_required
@admin_required
def reset_segment(segment):
    if segment not in SEGMENTS:
        abort(404)

    seg_def = SEGMENTS[segment]
    defaults = {
        "enabled": 1,
        "stop_loss_pct": seg_def["stop_loss_pct"],
        "take_profit_pct": seg_def["take_profit_pct"],
        "max_position_pct": seg_def["max_position_pct"],
        "max_total_positions": 10,
        "ai_confidence_threshold": 25,
        "min_price": seg_def["min_price"],
        "max_price": seg_def["max_price"],
        "min_volume": seg_def["min_volume"],
        "volume_surge_multiplier": 2.0,
        "rsi_overbought": 85.0,
        "rsi_oversold": 25.0,
        "momentum_5d_gain": 3.0,
        "momentum_20d_gain": 5.0,
        "breakout_volume_threshold": 1.0,
        "gap_pct_threshold": 3.0,
        "strategy_momentum_breakout": 1,
        "strategy_volume_spike": 1,
        "strategy_mean_reversion": 1,
        "strategy_gap_and_go": 1,
        "custom_watchlist": [],
    }
    update_user_segment_config(current_user.effective_user_id, segment, **defaults)
    flash(f"{SEGMENTS[segment]['name']} configuration reset to defaults.", "info")
    return redirect(url_for("views.settings") + f"#segment-{segment}")


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

@views_bp.route("/trades")
@login_required
def trades():
    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]

    # Parse optional profile filter
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None

    # Tab kind: 'stocks' (occ_symbol IS NULL), 'options' (occ_symbol
    # IS NOT NULL), or '' (all). Server-driven tabs (separate URLs
    # per tab) so pagination + sort + search continue to work per
    # tab without loading everything client-side. Wired 2026-05-11.
    kind = request.args.get("kind", "", type=str)
    if kind not in ("stocks", "options"):
        kind = ""  # all
    sql_kind = kind or None

    # Symbol search — case-insensitive prefix on symbol/OCC.
    # Sanitize: strip whitespace, cap length to defend against absurd
    # inputs. Empty/None means no filter. Wired 2026-05-11 (TODO #3).
    search_raw = request.args.get("search", "", type=str) or ""
    search = search_raw.strip()[:32] or None

    # Pull trades from profile journal DBs
    all_trades = []
    if selected_profile_int:
        # Single profile mode
        prof = next((p for p in profiles if p["id"] == selected_profile_int), None)
        if prof:
            prof_trades = _get_trade_history_for_profile(
                prof["id"], limit=200, kind=sql_kind, search=search,
            )
            for t in prof_trades:
                t["profile_name"] = prof["name"]
                t["profile_id"] = prof["id"]
                t["segment"] = prof["name"]
            try:
                ctx = build_user_context_from_profile(prof["id"])
                _enrich_trade_history_with_live_pnl(prof_trades, ctx)
            except Exception as exc:
                logger.warning(
                    "trades(): live-P&L enrichment failed for profile %d: %s",
                    prof["id"], exc,
                )
            all_trades.extend(prof_trades)
    else:
        # All profiles mode (current behavior)
        for prof in profiles:
            prof_trades = _get_trade_history_for_profile(
                prof["id"], limit=100, kind=sql_kind, search=search,
            )
            for t in prof_trades:
                t["profile_name"] = prof["name"]
                t["profile_id"] = prof["id"]
                t["segment"] = prof["name"]
            try:
                ctx = build_user_context_from_profile(prof["id"])
                _enrich_trade_history_with_live_pnl(prof_trades, ctx)
            except Exception as exc:
                logger.warning(
                    "trades(): live-P&L enrichment failed for profile %d: %s",
                    prof["id"], exc,
                )
            all_trades.extend(prof_trades)

    # Server-side sorting
    sort_by = request.args.get("sort", "timestamp")
    sort_dir = request.args.get("dir", "desc")
    sort_key_map = {
        "timestamp": lambda t: t.get("timestamp", ""),
        "symbol": lambda t: t.get("symbol", ""),
        "side": lambda t: t.get("side", ""),
        "qty": lambda t: float(t.get("qty", 0) or 0),
        "price": lambda t: float(t.get("price", 0) or 0),
        "ai_confidence": lambda t: float(t.get("ai_confidence", 0) or 0),
        "pnl": lambda t: float(t.get("pnl", 0) or 0),
        "profile": lambda t: t.get("profile_name", ""),
    }
    key_fn = sort_key_map.get(sort_by, sort_key_map["timestamp"])
    all_trades.sort(key=key_fn, reverse=(sort_dir == "desc"))

    # Server-side pagination
    page = request.args.get("page", 1, type=int)
    per_page = 50
    total = len(all_trades)
    total_pages = max(1, -(-total // per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_trades = all_trades[start:start + per_page]
    page_links = _build_page_links(page, total_pages, window=2)

    return render_template("trades.html",
                           trades=page_trades,
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           page=page, total_pages=total_pages,
                           page_links=page_links,
                           total_trades=total, sort_by=sort_by, sort_dir=sort_dir,
                           kind=kind, search=search or "")


def _build_page_links(current_page, total_pages, window=2):
    """Build the page-bar entries for a numbered pagination control.

    Returns a list where each entry is either:
      - an int page number (renders as a clickable link, or as the
        active page if it equals current_page)
      - None (renders as an ellipsis gap)

    Always includes page 1 and the last page. Around the current page
    shows `window` neighbors on each side (default 2 = current ±2).
    Gaps wider than 1 between consecutive numbers get an ellipsis.

    Examples (window=2):
      current=1, total=20  → [1, 2, 3, None, 20]
      current=10, total=20 → [1, None, 8, 9, 10, 11, 12, None, 20]
      current=20, total=20 → [1, None, 18, 19, 20]
      total=5              → [1, 2, 3, 4, 5]
    """
    if total_pages <= 1:
        return [1]
    pages = set([1, total_pages])
    pages.update(range(max(2, current_page - window),
                       min(total_pages, current_page + window) + 1))
    ordered = sorted(pages)
    out = []
    prev = None
    for p in ordered:
        if prev is not None:
            gap = p - prev
            if gap == 2:
                # Single page missing — render it instead of an
                # ellipsis. Avoids an ellipsis for a 1-page skip
                # which looks worse than just showing the page.
                out.append(prev + 1)
            elif gap > 2:
                out.append(None)
        out.append(p)
        prev = p
    return out


def _calculate_risk_metrics(db_paths):
    """Calculate risk and consistency metrics from trade data across multiple DBs.

    Returns a dict with max drawdown, tail risk, streak, and monthly return data.
    """
    import sqlite3
    from collections import defaultdict
    from datetime import datetime as _dt

    # Collect all closed trades with pnl across all DBs
    all_trades = []  # list of (timestamp, symbol, pnl, price, qty)
    all_snapshots = []  # list of (date, equity)

    for db_path in db_paths:
        try:
            with closing(open_profile_db(db_path)) as conn:
                # Phase 5e — exclude data_quality-tagged rows. Their
                # `price` field is corrupted (option premium logged
                # for stock-side trades); using them in per-trade %
                # calcs (CVaR, drawdown tail, monthly returns)
                # produces nonsense. Back-compat for legacy DBs
                # without the column.
                cols = {row[1] for row in conn.execute(
                    "PRAGMA table_info(trades)"
                ).fetchall()}
                dq_clause = (
                    " AND data_quality IS NULL"
                    if "data_quality" in cols else ""
                )
                rows = conn.execute(
                    f"SELECT timestamp, symbol, pnl, price, qty "
                    f"FROM trades WHERE pnl IS NOT NULL{dq_clause} "
                    f"ORDER BY timestamp ASC"
                ).fetchall()
                for r in rows:
                    all_trades.append({
                        "timestamp": r["timestamp"] or "",
                        "symbol": r["symbol"] or "",
                        "pnl": r["pnl"] or 0,
                        "price": r["price"] or 0,
                        "qty": r["qty"] or 0,
                    })

                # Daily snapshots for drawdown — pick latest row per date
                # so historical days with multiple snapshot writes (pre-2026-04-25
                # marker fix) don't inflate drawdown by treating intra-day
                # variations as separate days.
                snap_rows = conn.execute(
                    "SELECT date, equity FROM daily_snapshots "
                    "WHERE equity IS NOT NULL "
                    "AND rowid IN (SELECT MAX(rowid) FROM daily_snapshots GROUP BY date) "
                    "ORDER BY date ASC"
                ).fetchall()
                for r in snap_rows:
                    all_snapshots.append({
                        "date": r["date"],
                        "equity": r["equity"],
                    })
        except Exception as exc:
            logger.warning(
                "_calculate_risk_metrics: per-DB rollup failed for %s: %s",
                db_path, exc,
            )

    # Sort trades by timestamp
    all_trades.sort(key=lambda t: t["timestamp"])

    result = {
        "has_data": len(all_trades) >= 5,
        # Drawdown
        "max_drawdown_pct": 0.0,
        "max_drawdown_peak": 0.0,
        "max_drawdown_trough": 0.0,
        "max_drawdown_dates": "N/A",
        # Tail risk
        "worst_trade_pnl": 0.0,
        "worst_trade_symbol": "N/A",
        "worst_trade_pct": 0.0,
        "worst_day_pnl": 0.0,
        "worst_day_date": "N/A",
        "var_95": 0.0,
        # Streaks
        "longest_losing_streak": 0,
        "longest_winning_streak": 0,
        "current_streak": 0,
        "current_streak_type": "none",
        "avg_losing_streak": 0.0,
        # Monthly returns
        "monthly_returns": [],
    }

    if len(all_trades) < 5:
        return result

    # --- Max Drawdown from daily_snapshots (preferred) or trades ---
    if all_snapshots:
        all_snapshots.sort(key=lambda s: s["date"])
        peak = all_snapshots[0]["equity"]
        max_dd = 0.0
        dd_peak = peak
        dd_trough = peak
        dd_peak_date = all_snapshots[0]["date"]
        dd_trough_date = all_snapshots[0]["date"]
        cur_peak_date = all_snapshots[0]["date"]

        for s in all_snapshots:
            eq = s["equity"]
            if eq > peak:
                peak = eq
                cur_peak_date = s["date"]
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
                    dd_peak = peak
                    dd_trough = eq
                    dd_peak_date = cur_peak_date
                    dd_trough_date = s["date"]

        result["max_drawdown_pct"] = round(max_dd * 100, 1)
        result["max_drawdown_peak"] = round(dd_peak, 2)
        result["max_drawdown_trough"] = round(dd_trough, 2)
        if max_dd > 0:
            result["max_drawdown_dates"] = (
                f"{dd_peak_date} to {dd_trough_date}, "
                f"peak ${dd_peak:,.0f} → trough ${dd_trough:,.0f}"
            )
    else:
        # Fallback: reconstruct equity curve from cumulative trade PnL
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in all_trades:
            cumulative += t["pnl"]
            if cumulative > peak:
                peak = cumulative
            if peak > 0:
                dd = (peak - cumulative) / peak
                if dd > max_dd:
                    max_dd = dd
        result["max_drawdown_pct"] = round(max_dd * 100, 1)

    # --- Tail Risk ---
    # Worst single trade
    worst_trade = min(all_trades, key=lambda t: t["pnl"])
    result["worst_trade_pnl"] = round(worst_trade["pnl"], 2)
    result["worst_trade_symbol"] = worst_trade["symbol"]
    if worst_trade["price"] and worst_trade["qty"] and worst_trade["price"] > 0:
        cost_basis = worst_trade["price"] * worst_trade["qty"]
        if cost_basis > 0:
            result["worst_trade_pct"] = round(worst_trade["pnl"] / cost_basis * 100, 1)

    # Worst single day P&L
    daily_pnl = defaultdict(float)
    for t in all_trades:
        day = t["timestamp"][:10]
        daily_pnl[day] += t["pnl"]
    if daily_pnl:
        worst_day = min(daily_pnl.items(), key=lambda x: x[1])
        result["worst_day_pnl"] = round(worst_day[1], 2)
        result["worst_day_date"] = worst_day[0]

    # VaR at 95% — based on trade PnL as % of cost basis
    trade_returns = []
    for t in all_trades:
        if t["price"] and t["qty"] and t["price"] > 0:
            cost = t["price"] * t["qty"]
            if cost > 0:
                trade_returns.append(t["pnl"] / cost * 100)
    if trade_returns:
        trade_returns.sort()
        idx = max(0, int(len(trade_returns) * 0.05))
        result["var_95"] = round(trade_returns[idx], 1)

    # --- Streaks ---
    losing_streaks = []
    winning_streaks = []
    current_streak_len = 0
    current_streak_type = "none"

    for t in all_trades:
        if t["pnl"] < 0:
            if current_streak_type == "losing":
                current_streak_len += 1
            else:
                if current_streak_type == "winning" and current_streak_len > 0:
                    winning_streaks.append(current_streak_len)
                current_streak_type = "losing"
                current_streak_len = 1
        elif t["pnl"] > 0:
            if current_streak_type == "winning":
                current_streak_len += 1
            else:
                if current_streak_type == "losing" and current_streak_len > 0:
                    losing_streaks.append(current_streak_len)
                current_streak_type = "winning"
                current_streak_len = 1
        # pnl == 0 treated as neutral, doesn't break streak

    # Don't forget the final streak
    if current_streak_type == "losing" and current_streak_len > 0:
        losing_streaks.append(current_streak_len)
    elif current_streak_type == "winning" and current_streak_len > 0:
        winning_streaks.append(current_streak_len)

    result["current_streak"] = current_streak_len
    result["current_streak_type"] = current_streak_type
    result["longest_losing_streak"] = max(losing_streaks) if losing_streaks else 0
    result["longest_winning_streak"] = max(winning_streaks) if winning_streaks else 0
    result["avg_losing_streak"] = (
        round(sum(losing_streaks) / len(losing_streaks), 1) if losing_streaks else 0.0
    )

    # --- Monthly Returns ---
    monthly = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
    for t in all_trades:
        ts = t["timestamp"]
        if len(ts) >= 7:
            month_key = ts[:7]  # YYYY-MM
        else:
            continue
        monthly[month_key]["trades"] += 1
        monthly[month_key]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            monthly[month_key]["wins"] += 1
        elif t["pnl"] < 0:
            monthly[month_key]["losses"] += 1

    # Build monthly returns list sorted most recent first
    # For return_pct, we use pnl / first equity of that month from snapshots,
    # or if no snapshots, just show raw PnL with 0 return_pct
    snapshot_by_month = {}
    for s in all_snapshots:
        mk = s["date"][:7]
        if mk not in snapshot_by_month:
            snapshot_by_month[mk] = s["equity"]

    monthly_list = []
    for mk in sorted(monthly.keys(), reverse=True):
        m = monthly[mk]
        try:
            dt = _dt.strptime(mk, "%Y-%m")
            label = dt.strftime("%b %Y")
        except Exception:
            label = mk
        equity_start = snapshot_by_month.get(mk, 0)
        return_pct = 0.0
        if equity_start and equity_start > 0:
            return_pct = round(m["pnl"] / equity_start * 100, 1)
        monthly_list.append({
            "month": label,
            "trades": m["trades"],
            "wins": m["wins"],
            "losses": m["losses"],
            "pnl": round(m["pnl"], 2),
            "return_pct": return_pct,
        })
    result["monthly_returns"] = monthly_list

    return result


@views_bp.route("/ai-performance")
@login_required
def ai_performance():
    """Legacy AI performance page -- redirects to new Performance Dashboard."""
    profile_id = request.args.get("profile_id", "")
    target = "/performance"
    if profile_id:
        target += f"?profile_id={profile_id}"
    return redirect(target)


@views_bp.route("/ai-performance-legacy")
@login_required
def ai_performance_legacy():
    """AI prediction accuracy dashboard — aggregated across all user's profiles."""
    from ai_tracker import get_ai_performance
    from journal import get_performance_summary
    import os

    # Aggregate AI performance across all profile databases + legacy segment DBs
    combined_perf = {
        "total_predictions": 0, "resolved": 0, "pending": 0,
        "win_rate": 0.0, "avg_confidence_on_wins": 0.0,
        "avg_confidence_on_losses": 0.0, "avg_return_on_buys": 0.0,
        "avg_return_on_sells": 0.0, "accuracy_by_confidence": {},
        "best_prediction": None, "worst_prediction": None,
        "profit_factor": 0.0,
    }
    combined_trade = {
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
        "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
    }

    # Parse optional profile filter
    from profile_classification import is_baseline_profile
    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None
    selected_profile_name = None

    # Collect DB paths based on filter
    db_paths = set()
    if selected_profile_int:
        # Single profile mode
        for p in profiles:
            if p["id"] == selected_profile_int:
                selected_profile_name = p["name"]
                db_path = f"quantopsai_profile_{p['id']}.db"
                if os.path.exists(db_path):
                    db_paths.add(db_path)
                break
    else:
        # All-profiles mode — exclude baselines. They run buy_hold/random
        # and generate ZERO ai_predictions, so they contribute nothing
        # today; this is a structural guard so the AI-accuracy aggregate
        # can never include a control profile if that ever changes.
        for p in profiles:
            if is_baseline_profile(p):
                continue
            db_path = f"quantopsai_profile_{p['id']}.db"
            if os.path.exists(db_path):
                db_paths.add(db_path)


    # Aggregate raw data across all DBs for accurate metric calculation
    import sqlite3
    all_wins = 0
    all_losses = 0
    all_return_buys = []
    all_return_sells = []
    conf_on_wins = []
    conf_on_losses = []
    total_gains = 0.0
    total_losses_amt = 0.0

    for db_path in db_paths:
        try:
            p = get_ai_performance(db_path=db_path)
            combined_perf["total_predictions"] += p.get("total_predictions", 0)
            combined_perf["resolved"] += p.get("resolved", 0)
            combined_perf["pending"] += p.get("pending", 0)
            if p.get("best_prediction"):
                if (combined_perf["best_prediction"] is None or
                        p["best_prediction"].get("return_pct", 0) >
                        combined_perf["best_prediction"].get("return_pct", 0)):
                    combined_perf["best_prediction"] = p["best_prediction"]
            if p.get("worst_prediction"):
                if (combined_perf["worst_prediction"] is None or
                        p["worst_prediction"].get("return_pct", 0) <
                        combined_perf["worst_prediction"].get("return_pct", 0)):
                    combined_perf["worst_prediction"] = p["worst_prediction"]
        except Exception as exc:
            logger.warning(
                "ai_performance_legacy: get_ai_performance failed for %s: %s",
                db_path, exc,
            )

        # Query raw resolved predictions for accurate aggregation.
        # open_profile_db ensures schema + busy_timeout; the only
        # remaining failure mode would be a real bug, which we want
        # to see.
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(
                "SELECT predicted_signal, actual_outcome, actual_return_pct, confidence "
                "FROM ai_predictions WHERE status = 'resolved'"
            ).fetchall()
        for r in rows:
            outcome = r["actual_outcome"]
            ret = r["actual_return_pct"]
            conf = r["confidence"] or 0
            sig = r["predicted_signal"] or ""

            if outcome == "win":
                all_wins += 1
                conf_on_wins.append(conf)
                if ret and ret > 0:
                    total_gains += ret
            elif outcome == "loss":
                all_losses += 1
                conf_on_losses.append(conf)
                if ret and ret < 0:
                    total_losses_amt += abs(ret)

            if ret is not None:
                if "BUY" in sig.upper():
                    all_return_buys.append(ret)
                elif "SELL" in sig.upper():
                    all_return_sells.append(ret)

        # get_performance_summary returns an empty/zero dict on failure
        # internally; outer try here was over-defensive.
        t = get_performance_summary(db_path=db_path)
        combined_trade["total_trades"] += t.get("total_trades", 0)
        combined_trade["winning_trades"] += t.get("winning_trades", 0)
        combined_trade["losing_trades"] += t.get("losing_trades", 0)
        combined_trade["total_pnl"] += t.get("total_pnl", 0)
        if t.get("best_trade", 0) > combined_trade["best_trade"]:
            combined_trade["best_trade"] = t["best_trade"]
        if t.get("worst_trade", 0) < combined_trade["worst_trade"]:
            combined_trade["worst_trade"] = t["worst_trade"]

    # Calculate derived metrics from raw aggregated data
    total_resolved = all_wins + all_losses
    if total_resolved > 0:
        combined_perf["win_rate"] = round(all_wins / total_resolved * 100, 1)
    if conf_on_wins:
        combined_perf["avg_confidence_on_wins"] = round(sum(conf_on_wins) / len(conf_on_wins), 1)
    if conf_on_losses:
        combined_perf["avg_confidence_on_losses"] = round(sum(conf_on_losses) / len(conf_on_losses), 1)
    if all_return_buys:
        combined_perf["avg_return_on_buys"] = round(sum(all_return_buys) / len(all_return_buys), 2)
    if all_return_sells:
        combined_perf["avg_return_on_sells"] = round(sum(all_return_sells) / len(all_return_sells), 2)
    if total_losses_amt > 0:
        combined_perf["profit_factor"] = round(total_gains / total_losses_amt, 2)

    if combined_trade["total_trades"] > 0:
        combined_trade["win_rate"] = (
            combined_trade["winning_trades"] / combined_trade["total_trades"] * 100
        )
        combined_trade["avg_pnl"] = (
            combined_trade["total_pnl"] / combined_trade["total_trades"]
        )

    # Get tuning history — filtered by profile if selected
    from models import get_tuning_history
    tuning_history = []
    profiles_to_query = profiles
    if selected_profile_int:
        profiles_to_query = [p for p in profiles if p["id"] == selected_profile_int]
    for p in profiles_to_query:
        history = get_tuning_history(p["id"], limit=10)
        for h in history:
            h["profile_name"] = p["name"]
            pname = h.get("parameter_name", "")
            h["parameter_label"] = _format_param_name(pname)
            h["old_value_label"] = _format_param_value(pname, h.get("old_value"))
            h["new_value_label"] = _format_param_value(pname, h.get("new_value"))
        tuning_history.extend(history)
    tuning_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    # 2026-05-12 fix: SCOPE the slippage % aggregation to STOCK
    # rows only. The previous code mixed stock + option rows
    # together and option premium %-moves (10-100% per cycle on
    # small underlying moves) drove the displayed average to
    # +1130% — a number that's mathematically real but operationally
    # meaningless. Stock-side % is the meaningful display.
    # Options-side surfaces separately as a $-cost line because %
    # on penny premiums isn't comparable to stock-share %.
    combined_slippage = None
    options_slippage = None
    for db_path in db_paths:
        s = get_slippage_stats(db_path=db_path, kind="stocks")
        if s:
            if combined_slippage is None:
                combined_slippage = {
                    "trades_with_fills": 0, "avg_slippage_pct": 0,
                    "total_slippage_cost": 0, "worst_slippage_pct": 0,
                    "worst_trade": None,
                    # Phase 5e — count of rows excluded due to a
                    # data_quality tag (e.g., 'phantom_stop_2026_05_11').
                    "excluded_data_quality": 0,
                }
            combined_slippage["trades_with_fills"] += s["trades_with_fills"]
            combined_slippage["total_slippage_cost"] += s["total_slippage_cost"]
            combined_slippage["excluded_data_quality"] += s.get(
                "excluded_data_quality", 0
            ) or 0
            if s["worst_slippage_pct"] > combined_slippage.get("worst_slippage_pct", 0):
                combined_slippage["worst_slippage_pct"] = s["worst_slippage_pct"]
                combined_slippage["worst_trade"] = s.get("worst_trade")
        # Option-side: $-cost only (% is meaningless on penny
        # premium denominators).
        s_opt = get_slippage_stats(db_path=db_path, kind="options")
        if s_opt:
            if options_slippage is None:
                options_slippage = {
                    "trades_with_fills": 0, "total_slippage_cost": 0,
                    "total_slippage_magnitude": 0,
                }
            options_slippage["trades_with_fills"] += s_opt["trades_with_fills"]
            options_slippage["total_slippage_cost"] += s_opt["total_slippage_cost"]
            options_slippage["total_slippage_magnitude"] += s_opt.get(
                "total_slippage_magnitude", 0
            ) or 0
    if combined_slippage and combined_slippage["trades_with_fills"] > 0:
        # Re-query for accurate average across all DBs — STOCK rows only
        # (occ_symbol IS NULL). 2026-05-12 fix matching the
        # get_slippage_stats kind='stocks' filter above.
        total_slip_sum = 0
        total_slip_count = 0
        for db_path in db_paths:
            with closing(open_profile_db(db_path)) as c:
                r = c.execute(
                    "SELECT COUNT(*) AS cnt, SUM(slippage_pct) AS s "
                    "FROM trades WHERE fill_price IS NOT NULL "
                    "AND decision_price IS NOT NULL "
                    "AND decision_price > 0 AND occ_symbol IS NULL"
                ).fetchone()
            if r and r["cnt"]:
                total_slip_count += r["cnt"]
                total_slip_sum += r["s"] or 0
        if total_slip_count > 0:
            combined_slippage["avg_slippage_pct"] = round(total_slip_sum / total_slip_count, 4)

    # Calculate risk & consistency metrics
    risk_metrics = _calculate_risk_metrics(db_paths)

    return render_template("ai_performance.html",
                           perf=combined_perf,
                           trade_perf=combined_trade,
                           tuning_history=tuning_history[:20],
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           selected_profile_name=selected_profile_name,
                           slippage=combined_slippage,
                           options_slippage=options_slippage,
                           risk=risk_metrics,
                           monthly_returns=risk_metrics.get("monthly_returns", []))


# ---------------------------------------------------------------------------
# Institutional Performance Dashboard (5-tab)
# ---------------------------------------------------------------------------

@views_bp.route("/performance")
@login_required
def performance_dashboard():
    """Institutional metrics dashboard -- 5-tab layout."""
    import os
    from metrics import calculate_all_metrics
    from profile_classification import is_baseline_profile

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None
    selected_profile_name = None

    # When NO single profile is selected, every metric on this page is an
    # aggregate across profiles. The buy_hold / random profiles are
    # experiment CONTROLS, not our system — folding them into the "All
    # System Profiles" aggregate is meaningless (each tests a different
    # strategy). So the aggregate is built over agg_profiles (AI only).
    # A baseline is still fully viewable by selecting it in the dropdown,
    # in which case the per-profile branches below use its own id.
    agg_profiles = [p for p in profiles if not is_baseline_profile(p)]

    # Collect DB paths based on filter
    db_paths = set()
    if selected_profile_int:
        for p in profiles:
            if p["id"] == selected_profile_int:
                selected_profile_name = p["name"]
                db_path = f"quantopsai_profile_{p['id']}.db"
                if os.path.exists(db_path):
                    db_paths.add(db_path)
                break
    else:
        # Baselines excluded — db_paths feeds both calculate_all_metrics
        # (headline metrics) and the AI-accuracy rollup below, so this one
        # filter keeps the whole "All System Profiles" view baseline-free.
        for p in agg_profiles:
            if not p.get("enabled"):
                continue
            db_path = f"quantopsai_profile_{p['id']}.db"
            if os.path.exists(db_path):
                db_paths.add(db_path)

    # Calculate total initial capital across selected ENABLED profiles only.
    # No-selection aggregate uses agg_profiles (AI only) so the capital base
    # matches the baseline-free db_paths above.
    total_initial_capital = 0
    capital_by_db = {}
    for p in (profiles if selected_profile_int else agg_profiles):
        if not p.get("enabled"):
            continue
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        capital = p.get("initial_capital", 100000)
        total_initial_capital += capital
        capital_by_db[f"quantopsai_profile_{p['id']}.db"] = capital
    if total_initial_capital <= 0:
        total_initial_capital = 100000

    metrics = calculate_all_metrics(db_paths, initial_capital=total_initial_capital,
                                     capital_by_db=capital_by_db)

    # Scalability tab data — TWO sections:
    #   1. Per-profile breakdown: real measured slippage / return for
    #      each profile we actually run. No projection math.
    #   2. Theoretical scale-up: projection rows at $5M/$10M/$25M/$50M/$100M
    #      using square-root market impact + tier liquidity.
    scaling_real = []
    scaling_capacity = []
    try:
        from scaling_projection import (
            per_profile_breakdown, capacity_analysis, _recommended_tier,
        )
        import sqlite3 as _sqlite3

        # Filter to profiles we're actually showing (matches db_paths).
        if selected_profile_int:
            target_profiles = [p for p in profiles if p["id"] == selected_profile_int]
        else:
            target_profiles = list(agg_profiles)  # baseline-free aggregate

        # Build per-profile data: name, capital, market_type, trades, latest_equity.
        profile_data = []
        agg_slips = []
        for p in target_profiles:
            db_path = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db_path):
                continue
            trades = []
            latest_eq = p.get("initial_capital") or 0
            try:
                with closing(open_profile_db(db_path)) as conn:
                    # Phase 5e — exclude data_quality-tagged rows from
                    # per-profile scaling analytics (corrupt `price`
                    # field would poison participation-rate calcs).
                    cols = {row[1] for row in conn.execute(
                        "PRAGMA table_info(trades)"
                    ).fetchall()}
                    dq_clause = (
                        " AND data_quality IS NULL"
                        if "data_quality" in cols else ""
                    )
                    trade_rows = conn.execute(
                        f"SELECT timestamp, symbol, side, qty, price, pnl, "
                        f"decision_price, fill_price, slippage_pct "
                        f"FROM trades WHERE pnl IS NOT NULL{dq_clause} "
                        f"ORDER BY timestamp ASC"
                    ).fetchall()
                    trades = [dict(r) for r in trade_rows]
                    snap = conn.execute(
                        "SELECT equity FROM daily_snapshots "
                        "WHERE equity IS NOT NULL "
                        "ORDER BY date DESC, rowid DESC LIMIT 1"
                    ).fetchone()
                    if snap and snap["equity"] is not None:
                        latest_eq = float(snap["equity"])
            except Exception as exc:
                logger.warning(
                    "performance: latest_equity snapshot failed for profile %s: %s",
                    p.get("id"), exc,
                )
            profile_data.append({
                "name": p.get("name", f"profile {p['id']}"),
                "capital": p.get("initial_capital") or 0,
                "market_type": p.get("market_type") or "small",
                "trades": trades,
                "latest_equity": latest_eq,
            })
            for t in trades:
                slip = t.get("slippage_pct")
                if slip is not None and slip != 0:
                    agg_slips.append(abs(slip))

        scaling_real = per_profile_breakdown(profile_data)
        scaling_capacity = capacity_analysis(profile_data)
    except Exception as exc:
        logger.warning("Scalability data build failed: %s", exc)

    # Current exposure across the selected profile(s). Virtual profiles
    # source positions/equity from the journal DB; real Alpaca-linked
    # profiles hit the Alpaca account. On All Profiles we aggregate
    # across every enabled profile so the user sees their full book.
    # P2.1 of LONG_SHORT_PLAN.md — also breaks down by sector so the
    # dashboard surfaces concentration risk (long-tech stacked on
    # long-tech etc.) and the AI prompt can avoid stacking it further.
    exposure = None
    try:
        if selected_profile_int:
            target_profiles = [get_trading_profile(selected_profile_int)]
            target_profiles = [p for p in target_profiles
                               if p and p["user_id"] == current_user.effective_user_id]
        else:
            # baseline-free aggregate — controls aren't part of "the book"
            target_profiles = agg_profiles

        all_positions = []  # gathered across target profiles
        equity_sum = 0.0
        n_profiles_with_data = 0
        for profile in target_profiles:
            try:
                ctx = build_user_context_from_profile(profile["id"])
                positions = _safe_positions(ctx)
                account = _safe_account_info(ctx)
                if account:
                    equity_sum += account.get("equity", 0) or 0
                if positions:
                    all_positions.extend(positions)
                if positions or account:
                    n_profiles_with_data += 1
            except (KeyError, ValueError, AttributeError, TypeError,
                    OSError, ConnectionError, TimeoutError, ImportError) as _bp_exc:
                # Per-profile broker fetch in dashboard rollup; one
                # bad profile shouldn't kill the page. Surface for follow-up.
                logger.debug(
                    "dashboard rollup per-profile broker fetch failed: %s: %s",
                    type(_bp_exc).__name__, _bp_exc,
                )
                continue

        if n_profiles_with_data and equity_sum > 0:
            exposure = compute_exposure(all_positions, equity_sum)
    except Exception as exc:
        logger.warning("performance: exposure aggregation failed: %s", exc)

    # P4.1 of LONG_SHORT_PLAN — surface target_book_beta when a single
    # profile is selected (aggregate "All Profiles" view has no single
    # target, so it stays None).
    profile_target_book_beta = None
    if selected_profile_int:
        sp = get_trading_profile(selected_profile_int)
        if sp and sp["user_id"] == current_user.effective_user_id:
            profile_target_book_beta = sp.get("target_book_beta")

    # P4.2 of LONG_SHORT_PLAN — Kelly recommendations per direction.
    # Same scope: only meaningful when a single profile is selected.
    # compute_kelly_recommendation returns None on insufficient sample;
    # no try-wrapper needed.
    perf_kelly_long = None
    perf_kelly_short = None
    if selected_profile_int:
        prof_db = f"quantopsai_profile_{selected_profile_int}.db"
        if os.path.exists(prof_db):
            perf_kelly_long = compute_kelly_recommendation(prof_db, "long")
            perf_kelly_short = compute_kelly_recommendation(prof_db, "short")

    # Fix 1 — MFE capture ratio. Only meaningful per-profile (capture
    # logic differs by profile risk parameters). Aggregated capture
    # across profiles would mix incompatible exit regimes.
    # compute_capture_ratio returns None on insufficient data.
    mfe_capture = None
    if selected_profile_int:
        prof_db = f"quantopsai_profile_{selected_profile_int}.db"
        if os.path.exists(prof_db):
            mfe_capture = compute_capture_ratio(prof_db)

    # AI prediction accuracy (for AI Intelligence tab)
    import sqlite3 as _sqlite3

    ai_perf = {
        "total_predictions": 0, "resolved": 0, "pending": 0,
        "win_rate": 0.0, "avg_confidence_on_wins": 0.0,
        "avg_confidence_on_losses": 0.0, "avg_return_on_buys": 0.0,
        "avg_return_on_sells": 0.0, "best_prediction": None,
        "worst_prediction": None, "profit_factor": 0.0,
        "n_buys": 0, "n_sells": 0,
        # Per prediction_type breakdown so the dashboard can show
        # exit-quality separately from directional-bearish accuracy.
        "avg_return_on_directional_long": 0.0,
        "avg_return_on_directional_short": 0.0,
        "avg_return_on_exit_long": 0.0,
        "n_directional_long": 0,
        "n_directional_short": 0,
        "n_exit_long": 0,
        # Split metric — directional trades vs HOLDs (added 2026-05-05).
        "directional_resolved": 0,
        "directional_wins": 0,
        "directional_win_rate": 0.0,
        "hold_resolved": 0,
        "hold_pass_rate": 0.0,
        # Best/worst split — directional trades vs HOLD outcomes.
        "best_trade": None,
        "worst_trade": None,
        "biggest_missed_gain": None,
        "biggest_avoided_loss": None,
    }
    all_wins = 0
    all_losses = 0
    conf_on_wins = []
    conf_on_losses = []
    all_return_buys = []
    all_return_sells = []
    returns_by_type = {"directional_long": [], "directional_short": [],
                       "exit_long": [], "exit_short": []}

    for db_path in db_paths:
        try:
            p = get_ai_performance(db_path=db_path)
            ai_perf["total_predictions"] += p.get("total_predictions", 0)
            ai_perf["resolved"] += p.get("resolved", 0)
            ai_perf["pending"] += p.get("pending", 0)
            # Aggregate directional/HOLD split book-wide.
            ai_perf["directional_resolved"] += p.get("directional_resolved", 0)
            ai_perf["directional_wins"] += p.get("directional_wins", 0)
            ai_perf["hold_resolved"] += p.get("hold_resolved", 0)
            hr = p.get("hold_resolved", 0)
            hpr = p.get("hold_pass_rate", 0.0) or 0.0
            # Contract: ai_tracker.get_ai_performance returns
            # hold_pass_rate as a percent in [0, 100]. The aggregation
            # math below divides by 100 — a fraction (0..1) here would
            # silently undercount HOLD wins ~100x in the rollup. Assert
            # the contract so a future change to ai_tracker fails loud.
            if not isinstance(hpr, (int, float)) or hpr < 0.0 or hpr > 100.0:
                raise ValueError(
                    f"hold_pass_rate from {db_path} is {hpr!r} (type "
                    f"{type(hpr).__name__}); must be a number in "
                    "[0, 100] — see ai_tracker.get_ai_performance."
                )
            if hr > 0:
                ai_perf.setdefault("_hold_wins_running", 0)
                ai_perf["_hold_wins_running"] += round(hr * hpr / 100.0)
            if p.get("best_prediction"):
                if ai_perf["best_prediction"] is None or p["best_prediction"].get("return_pct", 0) > ai_perf["best_prediction"].get("return_pct", 0):
                    ai_perf["best_prediction"] = p["best_prediction"]
            if p.get("worst_prediction"):
                if ai_perf["worst_prediction"] is None or p["worst_prediction"].get("return_pct", 0) < ai_perf["worst_prediction"].get("return_pct", 0):
                    ai_perf["worst_prediction"] = p["worst_prediction"]
            # New trade-vs-HOLD split (added 2026-05-04). Aggregate
            # by trade_pnl_pct (directional, sign-flipped for shorts)
            # and actual_return_pct (HOLDs).
            bt = p.get("best_trade")
            if bt:
                cur = ai_perf.get("best_trade")
                if cur is None or bt.get("trade_pnl_pct", 0) > cur.get("trade_pnl_pct", 0):
                    ai_perf["best_trade"] = bt
            wt = p.get("worst_trade")
            if wt:
                cur = ai_perf.get("worst_trade")
                if cur is None or wt.get("trade_pnl_pct", 0) < cur.get("trade_pnl_pct", 0):
                    ai_perf["worst_trade"] = wt
            mg = p.get("biggest_missed_gain")
            if mg:
                cur = ai_perf.get("biggest_missed_gain")
                if cur is None or mg.get("return_pct", 0) > cur.get("return_pct", 0):
                    ai_perf["biggest_missed_gain"] = mg
            al = p.get("biggest_avoided_loss")
            if al:
                cur = ai_perf.get("biggest_avoided_loss")
                if cur is None or al.get("return_pct", 0) < cur.get("return_pct", 0):
                    ai_perf["biggest_avoided_loss"] = al
            # Per-DB raw-row aggregation for win_rate / calibration /
            # per-type returns. Lives INSIDE the per-profile loop so
            # the accumulators (initialized above the loop) sum across
            # every profile, not just the last one. Before 2026-05-09
            # this query lived OUTSIDE the loop and used the leftover
            # `db_path` (set iteration → non-deterministic) so the
            # aggregate metrics reflected one random profile's data.
            try:
                with closing(open_profile_db(db_path)) as conn:
                    rows = conn.execute(
                        "SELECT predicted_signal, actual_outcome, actual_return_pct, "
                        "confidence, prediction_type "
                        "FROM ai_predictions WHERE status = 'resolved'"
                    ).fetchall()
                for r in rows:
                    outcome = r["actual_outcome"]
                    ret = r["actual_return_pct"]
                    conf = r["confidence"] or 0
                    sig = r["predicted_signal"] or ""
                    ptype = r["prediction_type"]
                    if outcome == "win":
                        all_wins += 1
                        conf_on_wins.append(conf)
                    elif outcome == "loss":
                        all_losses += 1
                        conf_on_losses.append(conf)
                    if ret is not None:
                        if "BUY" in sig.upper():
                            all_return_buys.append(ret)
                        elif "SELL" in sig.upper() or "SHORT" in sig.upper():
                            all_return_sells.append(ret)
                        if ptype and ptype in returns_by_type:
                            returns_by_type[ptype].append(ret)
            except Exception as _exc:
                logger.warning(
                    "ai-performance per-DB aggregation failed for %s: %s",
                    db_path, _exc,
                )
        except Exception as _exc:
            logger.warning(
                "ai-performance per-profile rollup failed for %s: %s",
                db_path, _exc,
            )

    if ai_perf["directional_resolved"] > 0:
        ai_perf["directional_win_rate"] = round(
            100.0 * ai_perf["directional_wins"] / ai_perf["directional_resolved"], 1,
        )
    if ai_perf["hold_resolved"] > 0:
        hw = ai_perf.get("_hold_wins_running", 0)
        ai_perf["hold_pass_rate"] = round(
            100.0 * hw / ai_perf["hold_resolved"], 1,
        )

    total_resolved = all_wins + all_losses
    if total_resolved > 0:
        ai_perf["win_rate"] = round(all_wins / total_resolved * 100, 1)
    if conf_on_wins:
        ai_perf["avg_confidence_on_wins"] = round(sum(conf_on_wins) / len(conf_on_wins), 1)
    if conf_on_losses:
        ai_perf["avg_confidence_on_losses"] = round(sum(conf_on_losses) / len(conf_on_losses), 1)
    ai_perf["n_buys"] = len(all_return_buys)
    ai_perf["n_sells"] = len(all_return_sells)
    if all_return_buys:
        ai_perf["avg_return_on_buys"] = round(sum(all_return_buys) / len(all_return_buys), 2)
    if all_return_sells:
        ai_perf["avg_return_on_sells"] = round(sum(all_return_sells) / len(all_return_sells), 2)
    # Per-type aggregates for the split dashboard cards.
    for ptype, vals in returns_by_type.items():
        ai_perf[f"n_{ptype}"] = len(vals)
        if vals:
            ai_perf[f"avg_return_on_{ptype}"] = round(sum(vals) / len(vals), 2)

    # Profit factor: every prediction that resulted in a real trade.
    # HOLD is the established no-trade sentinel (matches the convention
    # used elsewhere; see ai_tracker.py UPPER(predicted_signal)='HOLD').
    # We HOLD-exclude rather than IN(...)-whitelist so future signal
    # types (SHORT, MULTILEG_OPEN, and any new strategy verb) are
    # counted automatically — whitelisting was the 2026-05-09 bug shape.
    trade_returns = []
    for db_path in db_paths:
        try:
            with closing(open_profile_db(db_path)) as conn:
                rows = conn.execute(
                    "SELECT actual_return_pct FROM ai_predictions "
                    "WHERE status='resolved' AND actual_return_pct IS NOT NULL "
                    "AND predicted_signal IS NOT NULL "
                    "AND UPPER(predicted_signal) != 'HOLD'"
                ).fetchall()
            trade_returns.extend(r[0] for r in rows if r[0] is not None)
        except Exception as _exc:
            logger.warning(
                "performance: profit_factor query failed for %s: %s",
                db_path, _exc,
            )
    total_gains = sum(r for r in trade_returns if r > 0)
    total_losses_abs = abs(sum(r for r in trade_returns if r < 0))
    if total_gains > 0 and total_losses_abs > 0:
        ai_perf["profit_factor"] = round(total_gains / total_losses_abs, 2)

    # Slippage stats — aggregate per-profile get_slippage_stats output.
    # `total_slippage_cost` is signed (favorable slippage reduces it);
    # `magnitude` is the absolute version (sum of |fill-decision|*qty).
    # Both surface so the user can distinguish "execution variance"
    # from "net economic cost."
    slippage = {"avg_pct": 0.0, "total_cost": 0.0, "magnitude": 0.0, "count": 0}
    # 2026-05-12 fix: scope % to STOCK rows so option premium %-moves
    # don't dilute the average to nonsense (+1130% incident).
    weighted_pct_sum = 0.0
    slippage["excluded_data_quality"] = 0  # Phase 5e count
    for db_path in db_paths:
        s = get_slippage_stats(db_path=db_path, kind="stocks")
        if s:
            n = s.get("trades_with_fills", 0) or 0
            slippage["count"] += n
            slippage["total_cost"] += s.get("total_slippage_cost", 0) or 0
            slippage["magnitude"] += s.get("total_slippage_magnitude", 0) or 0
            weighted_pct_sum += (s.get("avg_slippage_pct", 0) or 0) * n
            slippage["excluded_data_quality"] += s.get(
                "excluded_data_quality", 0
            ) or 0
    if slippage["count"] > 0:
        slippage["avg_pct"] = weighted_pct_sum / slippage["count"]

    # Meta-model info for dashboard (Phase 1)
    meta_info = {"loaded": False, "profiles": []}
    profiles_to_check = [p for p in profiles
                         if (not selected_profile_int or p["id"] == selected_profile_int)]
    for p in profiles_to_check:
        path = meta_model.model_path_for_profile(p["id"])
        bundle = meta_model.load_model(path)
        if bundle:
            meta_info["loaded"] = True
            meta_info["profiles"].append({
                "name": p["name"],
                "id": p["id"],
                "auc": bundle["metrics"]["auc"],
                "accuracy": bundle["metrics"]["accuracy"],
                "n_samples": bundle["metrics"]["n_samples"],
                "positive_rate": bundle["metrics"]["positive_rate"],
                "top_features": bundle["feature_importance"][:10],
            })

    # Strategy validations (Phase 2). JSON parses on stored gate-arrays
    # narrowly catch malformed-JSON (legacy rows may have empty strings).
    validations = []
    # Pull a wider window than we display so the post-filter trim
    # to the active market_type still has enough rows to fill the
    # 30-row view. Pre-2026-05-16 this fetched limit=30 directly
    # and ignored the page's profile filter — a Mid Cap user saw
    # crypto / largecap / micro validations mixed in.
    raw = get_recent_validations(limit=200)
    selected_market_type = None
    if selected_profile_int:
        for p in profiles:
            if p["id"] == selected_profile_int:
                selected_market_type = p.get("market_type")
                break
    if selected_market_type:
        raw = [v for v in raw if v.get("market_type") == selected_market_type]
    raw = raw[:30]
    for v in raw:
        try:
            passed = json.loads(v.get("passed_gates", "[]"))
            failed = json.loads(v.get("failed_gates", "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            passed, failed = [], []
        validations.append({
            "id": v.get("id"),
            "timestamp": v.get("timestamp", ""),
            "strategy_name": v.get("strategy_name", ""),
            "market_type": v.get("market_type", ""),
            "verdict": v.get("verdict", ""),
            "score": v.get("score", 0),
            "passed_count": len(passed),
            "total_gates": len(passed) + len(failed),
            "elapsed_sec": v.get("elapsed_sec") or 0,
        })

    # Multi-strategy capital allocation (Phase 6)
    allocation_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        summary = get_allocation_summary(db, p["market_type"])
        allocation_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "market_type": p["market_type"],
            "strategies": summary,
        })

    # AI cost spend per profile (last 1d / 7d / 30d)
    ai_cost_info = {"per_profile": [], "totals": {"today": 0.0, "7d": 0.0, "30d": 0.0}}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        summary = spend_summary(db)
        ai_cost_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "today": summary["today"],
            "seven_d": summary["7d"],
            "thirty_d": summary["30d"],
            "by_purpose": summary["by_purpose_30d"],
            "by_model": summary["by_model_30d"],
        })
        ai_cost_info["totals"]["today"] += summary["today"]["usd"]
        ai_cost_info["totals"]["7d"] += summary["7d"]["usd"]
        ai_cost_info["totals"]["30d"] += summary["30d"]["usd"]

    # Crisis state (Phase 10)
    crisis_info = {"per_profile": [], "max_level": "normal"}
    _level_rank = {"normal": 0, "elevated": 1, "crisis": 2, "severe": 3}
    from crisis_state import history as _crisis_history
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        cur = get_current_level(db)
        hist = _crisis_history(db, limit=10)
        crisis_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "level": cur.get("level", "normal"),
            "size_multiplier": cur.get("size_multiplier", 1.0),
            "transitioned_at": cur.get("transitioned_at"),
            "signals": cur.get("signals", []),
            "readings": cur.get("readings", {}),
            "history": hist,
        })
        lvl = cur.get("level", "normal")
        if _level_rank.get(lvl, 0) > _level_rank.get(crisis_info["max_level"], 0):
            crisis_info["max_level"] = lvl

    # Event stream from last 24h (Phase 9)
    event_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        events = _recent_events(db, hours=24, limit=25)
        if not events:
            continue
        counts = {"high": 0, "medium": 0, "low": 0, "info": 0, "critical": 0}
        for e in events:
            sev = e.get("severity", "info")
            counts[sev] = counts.get(sev, 0) + 1
        event_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "events": events,
            "counts": counts,
        })

    # Specialist ensemble breakdown from last cycle (Phase 8).
    # cycle_data_*.json may be a partial write while the scheduler is
    # mid-rotation; narrow JSON parse handles malformed.
    ensemble_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        cycle_path = f"cycle_data_{p['id']}.json"
        if not os.path.exists(cycle_path):
            continue
        try:
            with open(cycle_path) as f:
                cycle = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "performance: cycle_data parse failed for profile %s: %s",
                p["id"], exc,
            )
            continue
        ens = cycle.get("ensemble") or {}
        if not ens.get("enabled"):
            continue
        ensemble_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "cost_calls": ens.get("cost_calls", 0),
            "vetoed": ens.get("vetoed", []),
            "rows": ens.get("rows", [])[:12],
            "timestamp": cycle.get("timestamp"),
        })

    # Auto-generated strategies (Phase 7)
    auto_strategy_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        rows = list_strategies(db)
        # Parse spec for human-readable summary; legacy rows may have
        # malformed spec_json — narrow catch.
        enriched = []
        for row in rows[:30]:
            try:
                spec = json.loads(row.get("spec_json") or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                spec = {}
            enriched.append({
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "generation": row["generation"],
                "description": spec.get("description", ""),
                "markets": spec.get("applicable_markets", []),
                "direction": spec.get("direction", ""),
                "created_at": row.get("created_at", ""),
                "shadow_started_at": row.get("shadow_started_at", ""),
                "promoted_at": row.get("promoted_at", ""),
                "retired_at": row.get("retired_at", ""),
                "retirement_reason": row.get("retirement_reason", ""),
            })
        counts = {
            "proposed": sum(1 for r in rows if r["status"] == "proposed"),
            "shadow":   sum(1 for r in rows if r["status"] == "shadow"),
            "active":   sum(1 for r in rows if r["status"] == "active"),
            "retired":  sum(1 for r in rows if r["status"] == "retired"),
        }
        auto_strategy_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "market_type": p["market_type"],
            "strategies": enriched,
            "counts": counts,
        })

    # Alpha decay monitoring (Phase 3) — per-profile rolling metrics and
    # deprecated strategy list. Aggregate across selected profiles.
    decay_info = {"per_profile": [], "any_deprecated": False}
    profiles_for_decay = [p for p in profiles
                          if (not selected_profile_int or p["id"] == selected_profile_int)]
    for p in profiles_for_decay:
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        # Distinct strategy types this profile has recorded predictions for
        with closing(open_profile_db(db)) as c:
            rows = c.execute(
                "SELECT DISTINCT strategy_type FROM ai_predictions "
                "WHERE strategy_type IS NOT NULL AND strategy_type != '' "
                "AND status = 'resolved'"
            ).fetchall()
        strat_types = [r[0] for r in rows]

        entries = []
        for stype in strat_types:
            rolling = compute_rolling_metrics(db, stype, window_days=30)
            lifetime = compute_lifetime_metrics(db, stype)
            entries.append({
                "strategy_type": stype,
                "rolling": rolling,
                "lifetime": lifetime,
                "edge_change_pct": (
                    round((rolling["sharpe_ratio"] - lifetime["sharpe_ratio"])
                          / abs(lifetime["sharpe_ratio"]) * 100, 1)
                    if lifetime["sharpe_ratio"] and lifetime["n_predictions"] >= 50 else None
                ),
            })

        deprecated = list_deprecated(db)
        if deprecated:
            decay_info["any_deprecated"] = True

        decay_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "entries": entries,
            "deprecated": deprecated,
        })

    return render_template("performance.html",
                           m=metrics,
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           selected_profile_name=selected_profile_name,
                           exposure=exposure,
                           profile_target_book_beta=profile_target_book_beta,
                           perf_kelly_long=perf_kelly_long,
                           perf_kelly_short=perf_kelly_short,
                           mfe_capture=mfe_capture,
                           ai_perf=ai_perf,
                           slippage=slippage,
                           scaling_real=scaling_real,
                           scaling_capacity=scaling_capacity,
                           meta_info=meta_info,
                           validations=validations,
                           decay_info=decay_info,
                           allocation_info=allocation_info,
                           auto_strategy_info=auto_strategy_info,
                           ensemble_info=ensemble_info,
                           event_info=event_info,
                           crisis_info=crisis_info,
                           ai_cost_info=ai_cost_info)


# ---------------------------------------------------------------------------
# AI Intelligence — 4 sub-pages
# ---------------------------------------------------------------------------

def _build_long_short_awareness(profiles):
    """Per-profile snapshot of the long/short construction context the
    AI sees on every cycle. Used by both the AI awareness tab and the
    performance dashboard so users can verify the prompt is computing
    the same numbers they'd compute by hand.

    Returns list of dicts shaped like:
      {
        "profile_id": int,
        "profile_name": str,
        "shorts_enabled": bool,
        "target_short_pct": float | None,
        "current_short_share": float | None,
        "balance_state": "pass" | "block_shorts" | "block_longs" | "n/a",
        "target_book_beta": float | None,
        "current_book_beta": float | None,
        "book_beta_delta": float | None,
        "kelly_long": dict | None,
        "kelly_short": dict | None,
        "drawdown_pct": float | None,
        "drawdown_scale": float | None,
        # Full prompt-context coverage:
        "risk_budget": dict | None,        # analyze_position_risk output
        "exposure": dict | None,            # compute_exposure output (top sectors/factors)
        "concentration_warnings": list,     # sectors >=30% gross
        "num_positions": int,
      }
    """
    import os
    out = []
    for p in profiles or []:
        if not p.get("enable_short_selling"):
            continue
        prof_db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(prof_db):
            continue
        row = {
            "profile_id": p["id"],
            "profile_name": p.get("name", f"Profile {p['id']}"),
            "shorts_enabled": True,
            "target_short_pct": p.get("target_short_pct"),
            "target_book_beta": p.get("target_book_beta"),
            "current_short_share": None,
            "balance_state": "n/a",
            "current_book_beta": None,
            "book_beta_delta": None,
            "kelly_long": None,
            "kelly_short": None,
            "drawdown_pct": None,
            "drawdown_scale": None,
            "risk_budget": None,
            "exposure": None,
            "concentration_warnings": [],
            "num_positions": 0,
        }
        try:
            from models import build_user_context_from_profile
            from client import get_account_info, get_positions
            ctx = build_user_context_from_profile(p["id"])
            account = get_account_info(ctx=ctx) or {}
            poss = get_positions(ctx=ctx) or []
            equity = float(account.get("equity") or 0)
            positions = []
            for pos in poss:
                qty = float(pos.get("qty") or 0)
                mv = float(pos.get("market_value") or 0)
                side = (pos.get("side") or "").lower()
                if not qty or not mv:
                    continue
                if "short" in side and mv > 0:
                    mv = -mv
                if "short" in side and qty > 0:
                    qty = -qty
                positions.append({
                    "symbol": pos.get("symbol"),
                    "qty": qty,
                    "market_value": mv,
                })

            # Current book beta. compute_book_beta returns None on
            # insufficient data; no try wrapper needed.
            row["current_book_beta"] = compute_book_beta(positions, equity)
            if (row["current_book_beta"] is not None
                    and row["target_book_beta"] is not None):
                row["book_beta_delta"] = (
                    row["current_book_beta"] - row["target_book_beta"]
                )

            # Exposure (sector + factor) — full output, plus derived
            # current short share and balance gate state.
            from portfolio_exposure import balance_gate
            if equity > 0 and positions:
                exp = compute_exposure(positions, equity)
                row["exposure"] = exp
                row["num_positions"] = exp.get("num_positions", 0)
                gross = float(exp.get("gross_pct") or 0)
                if gross > 0:
                    cur_short = sum(
                        (b.get("short_pct") or 0)
                        for b in (exp.get("by_sector") or {}).values()
                    )
                    row["current_short_share"] = cur_short / gross
                    if row["target_short_pct"] is not None:
                        row["balance_state"] = balance_gate(
                            target_short_pct=row["target_short_pct"],
                            current_exposure=exp,
                        )
                    # Concentration warnings — sectors over 30% gross
                    for sec, b in (exp.get("by_sector") or {}).items():
                        sec_gross = (b.get("long_pct") or 0) + (b.get("short_pct") or 0)
                        if sec_gross >= 30.0:
                            row["concentration_warnings"].append({
                                "sector": sec,
                                "gross_pct": sec_gross,
                            })

            # Risk-budget breakdown (P4.4) — per-position weight × vol.
            # analyze_position_risk returns None on insufficient sample.
            row["risk_budget"] = analyze_position_risk(positions, equity)

            # Kelly recommendations per direction. Returns None on edge.
            row["kelly_long"] = compute_kelly_recommendation(prof_db, "long")
            row["kelly_short"] = compute_kelly_recommendation(prof_db, "short")

            # Drawdown + capital scale.
            dd = check_drawdown(ctx, account, db_path=prof_db) or {}
            row["drawdown_pct"] = dd.get("drawdown_pct")
            if row["drawdown_pct"] is not None:
                row["drawdown_scale"] = compute_capital_scale(
                    row["drawdown_pct"]
                )
        except Exception as exc:
            # Profile-level failure: log AND keep the empty row so the
            # user sees the profile is enabled but data wasn't readable.
            # Logging the exception lets us trace which profile / which
            # feature failed instead of just seeing a blank cell.
            logger.warning(
                "long_short_awareness: rollup failed for profile %s: %s",
                p.get("id"), exc,
            )
        out.append(row)
    return out


def _build_portfolio_risk_awareness(profiles):
    """Per-profile snapshot of the Barra-style portfolio risk model
    that the AI now sees on every cycle (Item 2a). Reads the most
    recent row from `portfolio_risk_snapshots` per profile.

    Returns list of dicts shaped like:
      {
        "profile_id": int,
        "profile_name": str,
        "snapshot_at": str | None,
        "equity": float,
        "sigma_pct": float,                 # daily portfolio σ in %
        "var_95_dollars": float,
        "var_99_dollars": float,
        "es_95_dollars": float,
        "mc_var_95_dollars": float | None,
        "n_symbols": int,
        "factor_exposures": list[(name,β)],  # top-6 by |β|
        "grouped_share": dict,               # sectors/styles/french/idio %
        "scenarios": list[dict],             # worst-3 stress scenarios
      }
    """
    import os
    import json as _json
    out = []
    for p in profiles or []:
        prof_db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(prof_db):
            continue
        row_dict = {
            "profile_id": p["id"],
            "profile_name": p.get("name", f"Profile {p['id']}"),
            "snapshot_at": None,
            "equity": None,
            "sigma_pct": None,
            "var_95_dollars": None,
            "var_99_dollars": None,
            "es_95_dollars": None,
            "mc_var_95_dollars": None,
            "n_symbols": 0,
            "factor_exposures": [],
            "grouped_share": {},
            "scenarios": [],
        }
        try:
            import sqlite3 as _sq
            _conn = _sq.connect(prof_db)
            _conn.row_factory = _sq.Row
            row = _conn.execute(
                "SELECT created_at, equity, sigma, "
                "var_95_dollars, var_99_dollars, es_95_dollars, "
                "mc_var_95_dollars, n_symbols, "
                "factor_exposures_json, grouped_decomposition_json, "
                "scenarios_json "
                "FROM portfolio_risk_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            _conn.close()
        except Exception:
            row = None
        if not row:
            continue
        row_dict["snapshot_at"] = row["created_at"]
        row_dict["equity"] = row["equity"]
        row_dict["sigma_pct"] = (row["sigma"] or 0) * 100
        row_dict["var_95_dollars"] = row["var_95_dollars"]
        row_dict["var_99_dollars"] = row["var_99_dollars"]
        row_dict["es_95_dollars"] = row["es_95_dollars"]
        row_dict["mc_var_95_dollars"] = row["mc_var_95_dollars"]
        row_dict["n_symbols"] = row["n_symbols"] or 0
        try:
            fx = _json.loads(row["factor_exposures_json"] or "{}")
            ranked = sorted(
                fx.items(), key=lambda kv: abs(kv[1] or 0), reverse=True,
            )
            row_dict["factor_exposures"] = [
                (name, float(beta)) for name, beta in ranked[:6]
            ]
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            # Legacy rows may have malformed factor JSON; the row's
            # other fields still render, just without factor_exposures.
            logger.debug(
                "portfolio_risk: factor_exposures parse failed for snapshot %s: %s",
                row_dict.get("snapshot_at"), exc,
            )
        try:
            grouped = _json.loads(row["grouped_decomposition_json"] or "{}")
            total = sum(abs(v or 0) for v in grouped.values()) or 1.0
            row_dict["grouped_share"] = {
                k: round((v or 0) / total * 100, 1)
                for k, v in grouped.items()
            }
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug(
                "portfolio_risk: grouped_share parse failed for snapshot %s: %s",
                row_dict.get("snapshot_at"), exc,
            )
        try:
            scenarios = _json.loads(row["scenarios_json"] or "[]")
            row_dict["scenarios"] = scenarios[:5]    # worst 5
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.debug(
                "portfolio_risk: scenarios parse failed for snapshot %s: %s",
                row_dict.get("snapshot_at"), exc,
            )
        out.append(row_dict)
    return out


def _ai_common(page_name):
    """Common setup for all AI pages: profiles, profile filter, db_paths."""
    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None
    selected_profile_name = None

    db_paths = set()
    import os
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            if not selected_profile_name and p["id"] == selected_profile_int:
                selected_profile_name = p["name"]
            continue
        if selected_profile_int and p["id"] == selected_profile_int:
            selected_profile_name = p["name"]
        db_path = f"quantopsai_profile_{p['id']}.db"
        if os.path.exists(db_path):
            db_paths.add(db_path)

    return {
        "profiles": profiles,
        "selected_profile": selected_profile_int,
        "selected_profile_name": selected_profile_name,
        "ai_page": page_name,
        "db_paths": db_paths,
    }


@views_bp.route("/ai")
@login_required
def ai_dashboard():
    """AI Intelligence — unified 4-tab page.

    Uses the EXACT same data computation as performance_dashboard() lines
    1572-2060 — copied verbatim to avoid data structure mismatches.
    """
    import os
    from ai_tracker import get_ai_performance
    from journal import get_performance_summary
    from models import get_tuning_history
    import sqlite3 as _sqlite3

    ctx = _ai_common("ai")
    db_paths = ctx["db_paths"]
    profiles = ctx["profiles"]
    selected_profile_int = ctx["selected_profile"]

    # === COPIED FROM performance_dashboard() lines 1578-2060 ===

    ai_perf = {
        "total_predictions": 0, "resolved": 0, "pending": 0,
        "win_rate": 0.0, "avg_confidence_on_wins": 0.0,
        "avg_confidence_on_losses": 0.0, "avg_return_on_buys": 0.0,
        "avg_return_on_sells": 0.0, "best_prediction": None,
        "worst_prediction": None, "profit_factor": 0.0,
        "n_buys": 0, "n_sells": 0,
        # Per prediction_type breakdown so the dashboard can show
        # exit-quality separately from directional-bearish accuracy.
        "avg_return_on_directional_long": 0.0,
        "avg_return_on_directional_short": 0.0,
        "avg_return_on_exit_long": 0.0,
        "n_directional_long": 0,
        "n_directional_short": 0,
        "n_exit_long": 0,
        # Split metric — directional trades vs HOLDs (added 2026-05-05).
        "directional_resolved": 0,
        "directional_wins": 0,
        "directional_win_rate": 0.0,
        "hold_resolved": 0,
        "hold_pass_rate": 0.0,
        # Best/worst split — directional trades vs HOLD outcomes.
        "best_trade": None,
        "worst_trade": None,
        "biggest_missed_gain": None,
        "biggest_avoided_loss": None,
    }
    all_wins = 0
    all_losses = 0
    conf_on_wins = []
    conf_on_losses = []
    all_return_buys = []
    all_return_sells = []
    returns_by_type = {"directional_long": [], "directional_short": [],
                       "exit_long": [], "exit_short": []}

    for db_path in db_paths:
        try:
            p = get_ai_performance(db_path=db_path)
            ai_perf["total_predictions"] += p.get("total_predictions", 0)
            ai_perf["resolved"] += p.get("resolved", 0)
            ai_perf["pending"] += p.get("pending", 0)
            # Aggregate directional/HOLD split book-wide.
            ai_perf["directional_resolved"] += p.get("directional_resolved", 0)
            ai_perf["directional_wins"] += p.get("directional_wins", 0)
            ai_perf["hold_resolved"] += p.get("hold_resolved", 0)
            hr = p.get("hold_resolved", 0)
            hpr = p.get("hold_pass_rate", 0.0) or 0.0
            # Contract: ai_tracker.get_ai_performance returns
            # hold_pass_rate as a percent in [0, 100]. The aggregation
            # math below divides by 100 — a fraction (0..1) here would
            # silently undercount HOLD wins ~100x in the rollup. Assert
            # the contract so a future change to ai_tracker fails loud.
            if not isinstance(hpr, (int, float)) or hpr < 0.0 or hpr > 100.0:
                raise ValueError(
                    f"hold_pass_rate from {db_path} is {hpr!r} (type "
                    f"{type(hpr).__name__}); must be a number in "
                    "[0, 100] — see ai_tracker.get_ai_performance."
                )
            if hr > 0:
                ai_perf.setdefault("_hold_wins_running", 0)
                ai_perf["_hold_wins_running"] += round(hr * hpr / 100.0)
            if p.get("best_prediction"):
                if ai_perf["best_prediction"] is None or p["best_prediction"].get("return_pct", 0) > ai_perf["best_prediction"].get("return_pct", 0):
                    ai_perf["best_prediction"] = p["best_prediction"]
            if p.get("worst_prediction"):
                if ai_perf["worst_prediction"] is None or p["worst_prediction"].get("return_pct", 0) < ai_perf["worst_prediction"].get("return_pct", 0):
                    ai_perf["worst_prediction"] = p["worst_prediction"]
            # New trade-vs-HOLD split (added 2026-05-04). Aggregate
            # by trade_pnl_pct (directional, sign-flipped for shorts)
            # and actual_return_pct (HOLDs).
            bt = p.get("best_trade")
            if bt:
                cur = ai_perf.get("best_trade")
                if cur is None or bt.get("trade_pnl_pct", 0) > cur.get("trade_pnl_pct", 0):
                    ai_perf["best_trade"] = bt
            wt = p.get("worst_trade")
            if wt:
                cur = ai_perf.get("worst_trade")
                if cur is None or wt.get("trade_pnl_pct", 0) < cur.get("trade_pnl_pct", 0):
                    ai_perf["worst_trade"] = wt
            mg = p.get("biggest_missed_gain")
            if mg:
                cur = ai_perf.get("biggest_missed_gain")
                if cur is None or mg.get("return_pct", 0) > cur.get("return_pct", 0):
                    ai_perf["biggest_missed_gain"] = mg
            al = p.get("biggest_avoided_loss")
            if al:
                cur = ai_perf.get("biggest_avoided_loss")
                if cur is None or al.get("return_pct", 0) < cur.get("return_pct", 0):
                    ai_perf["biggest_avoided_loss"] = al
            # Per-DB raw-row aggregation for win_rate / calibration /
            # per-type returns. Lives INSIDE the per-profile loop so
            # the accumulators (initialized above the loop) sum across
            # every profile, not just one. See views.api_performance
            # for the matching fix and the 2026-05-09 root-cause note.
            try:
                with closing(open_profile_db(db_path)) as conn:
                    rows = conn.execute(
                        "SELECT predicted_signal, actual_outcome, actual_return_pct, "
                        "confidence, prediction_type "
                        "FROM ai_predictions WHERE status = 'resolved'"
                    ).fetchall()
                for r in rows:
                    outcome = r["actual_outcome"]
                    ret = r["actual_return_pct"]
                    conf = r["confidence"] or 0
                    sig = r["predicted_signal"] or ""
                    ptype = r["prediction_type"]
                    if outcome == "win":
                        all_wins += 1
                        conf_on_wins.append(conf)
                    elif outcome == "loss":
                        all_losses += 1
                        conf_on_losses.append(conf)
                    if ret is not None:
                        if "BUY" in sig.upper():
                            all_return_buys.append(ret)
                        elif "SELL" in sig.upper() or "SHORT" in sig.upper():
                            all_return_sells.append(ret)
                        if ptype and ptype in returns_by_type:
                            returns_by_type[ptype].append(ret)
            except Exception as _exc:
                logger.warning(
                    "ai-dashboard per-DB aggregation failed for %s: %s",
                    db_path, _exc,
                )
        except Exception as _exc:
            logger.warning(
                "ai-dashboard per-profile rollup failed for %s: %s",
                db_path, _exc,
            )

    if ai_perf["directional_resolved"] > 0:
        ai_perf["directional_win_rate"] = round(
            100.0 * ai_perf["directional_wins"] / ai_perf["directional_resolved"], 1,
        )
    if ai_perf["hold_resolved"] > 0:
        hw = ai_perf.get("_hold_wins_running", 0)
        ai_perf["hold_pass_rate"] = round(
            100.0 * hw / ai_perf["hold_resolved"], 1,
        )

    total_resolved = all_wins + all_losses
    if total_resolved > 0:
        ai_perf["win_rate"] = round(all_wins / total_resolved * 100, 1)
    if conf_on_wins:
        ai_perf["avg_confidence_on_wins"] = round(sum(conf_on_wins) / len(conf_on_wins), 1)
    if conf_on_losses:
        ai_perf["avg_confidence_on_losses"] = round(sum(conf_on_losses) / len(conf_on_losses), 1)
    ai_perf["n_buys"] = len(all_return_buys)
    ai_perf["n_sells"] = len(all_return_sells)
    if all_return_buys:
        ai_perf["avg_return_on_buys"] = round(sum(all_return_buys) / len(all_return_buys), 2)
    if all_return_sells:
        ai_perf["avg_return_on_sells"] = round(sum(all_return_sells) / len(all_return_sells), 2)
    # Per-type aggregates for the split dashboard cards.
    for ptype, vals in returns_by_type.items():
        ai_perf[f"n_{ptype}"] = len(vals)
        if vals:
            ai_perf[f"avg_return_on_{ptype}"] = round(sum(vals) / len(vals), 2)

    # Profit factor: HOLD-exclude (every traded signal counts).
    # See performance_dashboard for the convention rationale.
    trade_returns = []
    for db_path in db_paths:
        try:
            with closing(open_profile_db(db_path)) as conn:
                rows = conn.execute(
                    "SELECT actual_return_pct FROM ai_predictions "
                    "WHERE status='resolved' AND actual_return_pct IS NOT NULL "
                    "AND predicted_signal IS NOT NULL "
                    "AND UPPER(predicted_signal) != 'HOLD'"
                ).fetchall()
            trade_returns.extend(r[0] for r in rows if r[0] is not None)
        except Exception as _exc:
            logger.warning(
                "ai-dashboard: profit_factor query failed for %s: %s",
                db_path, _exc,
            )
    total_gains = sum(r for r in trade_returns if r > 0)
    total_losses_abs = abs(sum(r for r in trade_returns if r < 0))
    if total_gains > 0 and total_losses_abs > 0:
        ai_perf["profit_factor"] = round(total_gains / total_losses_abs, 2)

    # Slippage stats — signed total_cost + absolute magnitude both
    # surfaced. See performance_dashboard for the rationale.
    slippage = {"avg_pct": 0.0, "total_cost": 0.0, "magnitude": 0.0, "count": 0}
    # 2026-05-12 fix: scope % to STOCK rows so option premium %-moves
    # don't dilute the average to nonsense (+1130% incident).
    weighted_pct_sum = 0.0
    slippage["excluded_data_quality"] = 0  # Phase 5e count
    for db_path in db_paths:
        s = get_slippage_stats(db_path=db_path, kind="stocks")
        if s:
            n = s.get("trades_with_fills", 0) or 0
            slippage["count"] += n
            slippage["total_cost"] += s.get("total_slippage_cost", 0) or 0
            slippage["magnitude"] += s.get("total_slippage_magnitude", 0) or 0
            weighted_pct_sum += (s.get("avg_slippage_pct", 0) or 0) * n
            slippage["excluded_data_quality"] += s.get(
                "excluded_data_quality", 0
            ) or 0
    if slippage["count"] > 0:
        slippage["avg_pct"] = weighted_pct_sum / slippage["count"]

    meta_info = {"loaded": False, "profiles": []}
    profiles_to_check = [p for p in profiles
                         if (not selected_profile_int or p["id"] == selected_profile_int)]
    for p in profiles_to_check:
        path = meta_model.model_path_for_profile(p["id"])
        bundle = meta_model.load_model(path)
        if bundle:
            meta_info["loaded"] = True
            # Item 5a — pull online (SGD freshness layer) info. The
            # online SGD layer is a runtime-optional augment; if it
            # fails, we surface online=None so the meta-model card
            # still renders without the freshness sub-detail.
            try:
                from online_meta_model import get_online_model_info
                online_info = get_online_model_info(p["id"])
            except Exception as exc:
                logger.warning(
                    "ai-dashboard: online meta-info lookup failed for profile %s: %s",
                    p["id"], exc,
                )
                online_info = None
            meta_info["profiles"].append({
                "name": p["name"],
                "id": p["id"],
                "auc": bundle["metrics"]["auc"],
                "accuracy": bundle["metrics"]["accuracy"],
                "n_samples": bundle["metrics"]["n_samples"],
                "positive_rate": bundle["metrics"]["positive_rate"],
                "top_features": bundle["feature_importance"][:10],
                "online": online_info,
            })

    validations = []
    # Pull a wider window than we display so the post-filter trim
    # to the active market_type still has enough rows to fill the
    # 30-row view. Pre-2026-05-16 this fetched limit=30 directly
    # and ignored the page's profile filter — a Mid Cap user saw
    # crypto / largecap / micro validations mixed in.
    raw = get_recent_validations(limit=200)
    selected_market_type = None
    if selected_profile_int:
        for p in profiles:
            if p["id"] == selected_profile_int:
                selected_market_type = p.get("market_type")
                break
    if selected_market_type:
        raw = [v for v in raw if v.get("market_type") == selected_market_type]
    raw = raw[:30]
    for v in raw:
        try:
            passed = json.loads(v.get("passed_gates", "[]"))
            failed = json.loads(v.get("failed_gates", "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            passed, failed = [], []
        validations.append({
            "id": v.get("id"),
            "timestamp": v.get("timestamp", ""),
            "strategy_name": v.get("strategy_name", ""),
            "market_type": v.get("market_type", ""),
            "verdict": v.get("verdict", ""),
            "score": v.get("score", 0),
            "passed_count": len(passed),
            "total_gates": len(passed) + len(failed),
            "elapsed_sec": v.get("elapsed_sec") or 0,
        })

    allocation_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        summary = get_allocation_summary(db, p["market_type"])
        allocation_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "market_type": p["market_type"],
            "strategies": summary,
        })

    ai_cost_info = {"per_profile": [], "totals": {"today": 0.0, "7d": 0.0, "30d": 0.0}}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        summary = spend_summary(db)
        ai_cost_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "today": summary["today"],
            "seven_d": summary["7d"],
            "thirty_d": summary["30d"],
            "by_purpose": summary["by_purpose_30d"],
            "by_model": summary["by_model_30d"],
        })
        ai_cost_info["totals"]["today"] += summary["today"]["usd"]
        ai_cost_info["totals"]["7d"] += summary["7d"]["usd"]
        ai_cost_info["totals"]["30d"] += summary["30d"]["usd"]

    crisis_info = {"per_profile": [], "max_level": "normal"}
    _level_rank = {"normal": 0, "elevated": 1, "crisis": 2, "severe": 3}
    from crisis_state import history as _crisis_history
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        cur = get_current_level(db)
        hist = _crisis_history(db, limit=10)
        crisis_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "level": cur.get("level", "normal"),
            "size_multiplier": cur.get("size_multiplier", 1.0),
            "transitioned_at": cur.get("transitioned_at"),
            "signals": cur.get("signals", []),
            "readings": cur.get("readings", {}),
            "history": hist,
        })
        lvl = cur.get("level", "normal")
        if _level_rank.get(lvl, 0) > _level_rank.get(crisis_info["max_level"], 0):
            crisis_info["max_level"] = lvl

    event_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        events = _recent_events(db, hours=24, limit=25)
        if not events:
            continue
        counts = {"high": 0, "medium": 0, "low": 0, "info": 0, "critical": 0}
        for e in events:
            sev = e.get("severity", "info")
            counts[sev] = counts.get(sev, 0) + 1
        event_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "events": events,
            "counts": counts,
        })

    # Specialist ensemble breakdown from last cycle (Phase 8). cycle_data
    # may be a partial write while the scheduler is mid-rotation; narrow
    # JSON parse handles malformed rows.
    ensemble_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        cycle_path = f"cycle_data_{p['id']}.json"
        if not os.path.exists(cycle_path):
            continue
        try:
            with open(cycle_path) as f:
                cycle = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "ai-dashboard: cycle_data parse failed for profile %s: %s",
                p["id"], exc,
            )
            continue
        ens = cycle.get("ensemble") or {}
        if not ens.get("enabled"):
            continue
        ensemble_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "cost_calls": ens.get("cost_calls", 0),
            "vetoed": ens.get("vetoed", []),
            "rows": ens.get("rows", [])[:12],
            "timestamp": cycle.get("timestamp"),
        })

    # Per-specialist veto activity, last 7 days. Scope: matches the
    # active profile filter — when a single profile is selected the
    # widget shows only that profile's vetoes; when "All Profiles"
    # is active it aggregates across every profile DB. Pre-2026-05-16
    # this always passed `db_paths` (full list), so a single-profile
    # view silently showed cross-profile aggregates.
    try:
        from journal import get_specialist_veto_stats
        veto_db_paths = db_paths
        if selected_profile_int:
            scoped = f"quantopsai_profile_{selected_profile_int}.db"
            veto_db_paths = [d for d in db_paths if d.endswith(scoped)]
        ensemble_info["veto_stats"] = get_specialist_veto_stats(
            veto_db_paths, days=7,
        )
        if ensemble_info["veto_stats"]:
            ensemble_info["veto_stats"]["scope"] = (
                "single_profile" if selected_profile_int else "all_profiles"
            )
    except Exception:
        ensemble_info["veto_stats"] = None

    # Phase A3 of OPTIONS_PROGRAM_PLAN.md — per-profile Greeks panel.
    # Aggregates net delta/gamma/vega/theta across each profile's open
    # positions (stock + options). Empty when a profile has no positions.
    greeks_info = {"per_profile": []}
    from client import get_positions
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        try:
            pctx = build_user_context_from_profile(p["id"])
            positions = get_positions(ctx=pctx) or []
        except Exception as exc:
            logger.warning(
                "ai-dashboard: positions fetch failed for profile %s: %s",
                p["id"], exc,
            )
            continue
        if not positions:
            continue
        # Lightweight: pass position's own current_price as the "lookup"
        # — no extra fetches.
        price_by = {pp.get("symbol"): float(pp.get("current_price") or 0)
                    for pp in positions}
        # 2026-05-19 (docs/18 #1): omit iv_lookup so the auto-wired
        # default (per-call cached oracle hit) fires. Before this,
        # the dashboard explicitly passed `lambda s: None` which
        # always fell back to FALLBACK_IV=0.25 even for high-IV
        # underlyings. Per-request cache keeps cost bounded.
        summary = compute_book_greeks(
            positions,
            price_lookup=lambda s: price_by.get(s),
        )
        if summary["n_options_legs"] == 0 and summary["n_stock_positions"] == 0:
            continue
        greeks_info["per_profile"].append({
            "profile_id": p["id"], "name": p["name"],
            "summary": summary,
            "limits": {
                "max_net_options_delta_pct": getattr(
                    pctx, "max_net_options_delta_pct", None),
                "max_theta_burn_dollars_per_day": getattr(
                    pctx, "max_theta_burn_dollars_per_day", None),
                "max_short_vega_dollars": getattr(
                    pctx, "max_short_vega_dollars", None),
            },
        })

    # Item 1b — stat-arb pair book per profile.
    pair_book_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        active = get_active_pairs(db)
        if not active:
            continue
        rows = [{
            "label": ap.label,
            "hedge_ratio": round(ap.hedge_ratio, 3),
            "p_value": round(ap.p_value, 3),
            "half_life_days": round(ap.half_life_days, 1),
            "correlation": round(ap.correlation, 2),
        } for ap in active[:20]]
        pair_book_info["per_profile"].append({
            "profile_id": p["id"], "name": p["name"],
            "active_count": len(active),
            "rows": rows,
        })

    auto_strategy_info = {"per_profile": []}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        rows = list_strategies(db)
        enriched = []
        for row in rows[:30]:
            try:
                spec = json.loads(row.get("spec_json") or "{}")
            except (json.JSONDecodeError, TypeError, ValueError):
                spec = {}
            enriched.append({
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "generation": row["generation"],
                "description": spec.get("description", ""),
                "markets": spec.get("applicable_markets", []),
                "direction": spec.get("direction", ""),
                "created_at": row.get("created_at", ""),
                "shadow_started_at": row.get("shadow_started_at", ""),
                "promoted_at": row.get("promoted_at", ""),
                "retired_at": row.get("retired_at", ""),
                "retirement_reason": row.get("retirement_reason", ""),
            })
        counts = {
            "proposed": sum(1 for r in rows if r["status"] == "proposed"),
            "shadow":   sum(1 for r in rows if r["status"] == "shadow"),
            "active":   sum(1 for r in rows if r["status"] == "active"),
            "retired":  sum(1 for r in rows if r["status"] == "retired"),
        }
        auto_strategy_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "market_type": p["market_type"],
            "strategies": enriched,
            "counts": counts,
        })

    decay_info = {"per_profile": [], "any_deprecated": False}
    for p in profiles:
        if selected_profile_int and p["id"] != selected_profile_int:
            continue
        db = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db):
            continue
        with closing(open_profile_db(db)) as c:
            rows = c.execute(
                "SELECT DISTINCT strategy_type FROM ai_predictions "
                "WHERE strategy_type IS NOT NULL AND strategy_type != '' "
                "AND status = 'resolved'"
            ).fetchall()
        strat_types = [r[0] for r in rows]
        entries = []
        for stype in strat_types:
            rolling = compute_rolling_metrics(db, stype, window_days=30)
            lifetime = compute_lifetime_metrics(db, stype)
            entries.append({
                "strategy_type": stype,
                "rolling": rolling,
                "lifetime": lifetime,
                "edge_change_pct": (
                    round((rolling["sharpe_ratio"] - lifetime["sharpe_ratio"])
                          / abs(lifetime["sharpe_ratio"]) * 100, 1)
                    if lifetime["sharpe_ratio"] and lifetime["n_predictions"] >= 50 else None
                ),
            })
        deprecated = list_deprecated(db)
        if deprecated:
            decay_info["any_deprecated"] = True
        decay_info["per_profile"].append({
            "profile_id": p["id"],
            "name": p["name"],
            "entries": entries,
            "deprecated": deprecated,
        })

    # Rolling AI win-rate trend (7-day window, last 60 days).
    try:
        from ai_tracker import compute_rolling_win_rate
        from metrics import render_win_rate_svg
        win_rate_series = compute_rolling_win_rate(
            db_paths, window_days=7, lookback_days=60)
        ai_win_rate_chart_svg = render_win_rate_svg(win_rate_series)
    except Exception as exc:
        logger.warning("AI win-rate chart failed: %s", exc)
        ai_win_rate_chart_svg = ""

    long_short_awareness = _build_long_short_awareness(profiles)
    portfolio_risk_awareness = _build_portfolio_risk_awareness(profiles)

    return render_template("ai.html",
                           ai_perf=ai_perf, slippage=slippage, meta_info=meta_info,
                           validations=validations, decay_info=decay_info,
                           allocation_info=allocation_info,
                           auto_strategy_info=auto_strategy_info,
                           crisis_info=crisis_info, event_info=event_info,
                           ensemble_info=ensemble_info,
                           pair_book_info=pair_book_info,
                           greeks_info=greeks_info,
                           ai_cost_info=ai_cost_info,
                           ai_win_rate_chart_svg=ai_win_rate_chart_svg,
                           long_short_awareness=long_short_awareness,
                           portfolio_risk_awareness=portfolio_risk_awareness,
                           **ctx)


# Keep old sub-routes as redirects so bookmarks don't break
@views_bp.route("/ai/brain")
@login_required
def ai_brain_redirect():
    return redirect(url_for("views.ai_dashboard") + "#brain")

@views_bp.route("/ai/strategy")
@login_required
def ai_strategy_redirect():
    return redirect(url_for("views.ai_dashboard") + "#strategy")

@views_bp.route("/ai/awareness")
@login_required
def ai_awareness_redirect():
    return redirect(url_for("views.ai_dashboard") + "#awareness")

@views_bp.route("/ai/operations")
@login_required
def ai_operations_redirect():
    return redirect(url_for("views.ai_dashboard") + "#operations")



@views_bp.route("/api/kill-switch", methods=["GET"])
@login_required
def api_kill_switch_status():
    """Return current kill-switch state + recent history."""
    from kill_switch import is_active, get_history
    enabled, reason = is_active()
    return jsonify({
        "enabled": enabled,
        "reason": reason,
        "history": [
            {
                "action": r["action"],
                "reason": r["reason"],
                "set_by": r["set_by"],
                "set_at": r["set_at"],
            }
            for r in get_history(limit=20)
        ],
    })


@views_bp.route("/api/kill-switch", methods=["POST"])
@login_required
@admin_required
def api_kill_switch_set():
    """Manually toggle the master kill switch.

    Body: {"action": "activate" | "deactivate", "reason": "..."}

    Activating blocks all new trade entries across every profile until
    explicitly deactivated. Existing positions and broker stops are
    untouched.

    SECURITY: viewers / non-admin accounts cannot toggle the kill
    switch. The switch affects EVERY profile on the admin's
    account; a viewer that could flip it would silently freeze the
    admin's book. (Caught 2026-05-07: endpoint was @login_required
    only, so any viewer linked to an admin could POST and stop
    every trade in the book.)
    """
    if not getattr(current_user, "is_admin", False) or \
            getattr(current_user, "is_viewer", False):
        return jsonify({
            "error": "View-only accounts cannot toggle the master "
                     "kill switch. Contact the account administrator."
        }), 403

    payload = request.get_json(silent=True) or {}
    action = (payload.get("action") or "").lower().strip()
    reason = (payload.get("reason") or "").strip()
    set_by = f"user:{current_user.email}" if hasattr(current_user, "email") else "user"

    from kill_switch import activate, deactivate, is_active
    if action == "activate":
        if not reason:
            return jsonify({"error": "reason required"}), 400
        activate(reason, set_by=set_by)
    elif action == "deactivate":
        deactivate(set_by=set_by)
    else:
        return jsonify({"error": "action must be 'activate' or 'deactivate'"}), 400
    enabled, current_reason = is_active()
    return jsonify({"enabled": enabled, "reason": current_reason})


@views_bp.route("/api/macro-data")
@login_required
def api_macro_data():
    """Return current macro data (yield curve, CBOE skew, ETF flows, FRED)."""
    try:
        from macro_data import get_all_macro_data
        return jsonify(get_all_macro_data())
    except Exception as exc:
        return jsonify({"error": str(exc)})


@views_bp.route("/api/backtest-vs-reality/<int:profile_id>")
@login_required
def api_backtest_vs_reality(profile_id):
    """Compare backtest predictions with actual trading results over last 30 days.

    Runs a backtest with the profile's current settings on the last 30 days,
    then compares with actual trades from the same period.
    Returns JSON comparison data.
    """
    import sqlite3
    import os
    from datetime import datetime, timedelta

    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No trade data for this profile"}), 404

    # --- Actual trade results (last 30 days) ---
    thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).isoformat()
    try:
        with closing(open_profile_db(db_path)) as conn:
            # Phase 5e — exclude data_quality-tagged rows
            from journal import data_quality_clause
            _dq = data_quality_clause(conn)

            actual_trades = conn.execute(
                f"SELECT * FROM trades WHERE pnl IS NOT NULL "
                f"AND timestamp >= ?{_dq} "
                f"ORDER BY timestamp DESC",
                (thirty_days_ago,),
            ).fetchall()
            actual_trades = [dict(r) for r in actual_trades]

            # Slippage stats for this profile (last 30 days).
            # 2026-05-12 fix: scope to STOCK rows (occ_symbol IS NULL).
            # Mixing stock + option premium % moves drove the displayed
            # average to +1130% in the prior bug. Stock-side % is the
            # meaningful display; option-side $ cost is surfaced
            # separately on the AI performance page.
            slippage_row = conn.execute("""
                SELECT
                    COUNT(*) AS trades_with_fills,
                    AVG(slippage_pct) AS avg_slippage_pct,
                    SUM(ABS(fill_price - decision_price) * qty) AS total_slippage_cost
                FROM trades
                WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
                  AND decision_price > 0 AND timestamp >= ?
                  AND occ_symbol IS NULL
            """, (thirty_days_ago,)).fetchone()
    except Exception as exc:
        logger.warning("Failed to query actual trades for profile %d: %s", profile_id, exc)
        return jsonify({"error": "Failed to query trade data"}), 500

    if len(actual_trades) < 5:
        msg = (
            f"Need at least 5 closed trades in the last 30 days "
            f"(found {len(actual_trades)})."
        )
        # `error_code` is the JS-switch value; `error` is the human-readable
        # message that gets displayed if the JS doesn't recognize the code.
        # Keeping the snake_case code in `error_code` (allowlisted) instead
        # of the generic `error` field, which other endpoints use for plain-
        # text error strings.
        return jsonify({
            "error_code": "insufficient_data",
            "error": msg,
            "actual_trade_count": len(actual_trades),
        })

    # Actual metrics
    actual_wins = sum(1 for t in actual_trades if (t.get("pnl") or 0) > 0)
    actual_losses = len(actual_trades) - actual_wins
    actual_total_pnl = sum(t.get("pnl") or 0 for t in actual_trades)
    actual_win_rate = (actual_wins / len(actual_trades) * 100) if actual_trades else 0

    actual_slippage = round(slippage_row["avg_slippage_pct"] or 0, 3) if slippage_row and slippage_row["trades_with_fills"] else None
    actual_slippage_cost = round(slippage_row["total_slippage_cost"] or 0, 2) if slippage_row and slippage_row["trades_with_fills"] else None

    # --- Backtest results (last 30 days with current settings) ---
    market_type = profile["market_type"]
    try:
        from backtester import backtest_strategy
        bt = backtest_strategy(market_type, days=30,
                               initial_capital=10_000, sample_size=15,
                               atr_sl_mult=float(profile.get("atr_multiplier_sl", 2.0)),
                               atr_tp_mult=float(profile.get("atr_multiplier_tp", 3.0)))
        bt_win_rate = bt.get("win_rate", 0)
        bt_total_return = bt.get("total_return_pct", 0)
        bt_num_trades = bt.get("num_trades", 0)
        bt_avg_slippage = 0.2  # Simulated slippage estimate
    except Exception as exc:
        logger.warning("Backtest failed for profile %d: %s", profile_id, exc)
        bt_win_rate = 0
        bt_total_return = 0
        bt_num_trades = 0
        bt_avg_slippage = 0.2

    # Calculate total return pct from actual trades (approx: pnl relative to equity)
    # Use daily snapshots for better accuracy
    try:
        with closing(open_profile_db(db_path)) as conn2:
            snap = conn2.execute(
                "SELECT equity FROM daily_snapshots "
                "ORDER BY date DESC, rowid DESC LIMIT 1"
            ).fetchone()
        equity_base = snap["equity"] if snap else 10_000
    except Exception:
        equity_base = 10_000

    actual_return_pct = (actual_total_pnl / equity_base * 100) if equity_base > 0 else 0

    comparison = {
        "backtest": {
            "win_rate": round(bt_win_rate, 1),
            "total_return_pct": round(bt_total_return, 1),
            "num_trades": bt_num_trades,
            "avg_slippage_pct": bt_avg_slippage,
        },
        "actual": {
            "win_rate": round(actual_win_rate, 1),
            "total_return_pct": round(actual_return_pct, 1),
            "num_trades": len(actual_trades),
            "winning_trades": actual_wins,
            "losing_trades": actual_losses,
            "total_pnl": round(actual_total_pnl, 2),
            "avg_slippage_pct": actual_slippage,
            "total_slippage_cost": actual_slippage_cost,
        },
        "gap": {
            "win_rate": round(actual_win_rate - bt_win_rate, 1),
            "total_return_pct": round(actual_return_pct - bt_total_return, 1),
            "slippage_pct": round((actual_slippage or 0) - bt_avg_slippage, 1) if actual_slippage is not None else None,
        },
        "period_days": 30,
        "actual_trade_count": len(actual_trades),
    }

    return jsonify(comparison)


@views_bp.route("/api/slippage-stats/<int:profile_id>")
@login_required
def api_slippage_stats(profile_id):
    """Return slippage statistics for a profile.

    2026-05-12 fix: now returns BOTH stocks and options aggregates
    separately. The unscoped legacy version mixed the two and
    produced nonsense %-values (+1130% incident). Stock-side: %
    + $ are both meaningful. Option-side: $ only (% on penny
    premiums is noise)."""
    import os

    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No trade data"}), 404

    from journal import get_slippage_stats
    stocks_stats = get_slippage_stats(db_path=db_path, kind="stocks")
    options_stats = get_slippage_stats(db_path=db_path, kind="options")
    if stocks_stats is None and options_stats is None:
        return jsonify({"available": False})

    return jsonify({
        "available": True,
        "stocks": stocks_stats,
        "options": options_stats,
    })


@views_bp.route("/api/slippage-model/<int:profile_id>")
@login_required
def api_slippage_model(profile_id):
    """Item 5c — slippage model calibration + components.

    Returns the fitted K, sample count, bootstrap-bucket sizes, and
    a sample estimate so the user can SEE what the model thinks.
    """
    import os
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No data yet"}), 404

    try:
        from slippage_model import (
            calibrate_from_history, estimate_slippage,
        )
        market_type = profile.get("market_type")
        fit = calibrate_from_history(db_path, market_type=market_type)
        # Build a sample estimate for a representative order size so
        # the user can see what the model produces without running a
        # backtest.
        sample = estimate_slippage(
            symbol="SAMPLE", qty=1000, side="buy", decision_price=50.0,
            spread_bps=4.0, adv_shares=1_000_000,
            daily_vol_bps=200.0, db_path=db_path, market_type=market_type,
        )
        # Bucket size summary
        bucket_summary = {}
        for k, samples in (fit.get("bootstrap_residuals") or {}).items():
            bucket_summary[k] = len(samples)
        # Source values like "insufficient_history" / "no_db" / "fit" /
        # "default" are internal identifiers — route through
        # display_name so the UI shows "Insufficient history" not
        # raw snake_case (caught 2026-05-07 dashboard inspection).
        from display_names import display_name as _dn
        return jsonify({
            "available": True,
            "K_bps": fit.get("K_bps"),
            "n_samples": fit.get("n_samples"),
            "mean_residual_bps": fit.get("mean_residual_bps"),
            "fitted_at": fit.get("fitted_at"),
            "source": _dn(fit.get("source") or "unknown"),
            "source_raw": fit.get("source"),
            "market_type": market_type,
            "buckets": bucket_summary,
            "sample_estimate": {
                "components": sample.get("components"),
                "total_bps": sample.get("total_bps"),
                "fill_price": sample.get("fill_price"),
                "K_source": _dn(sample.get("K_source") or "unknown"),
                "K_source_raw": sample.get("K_source"),
            },
        })
    except Exception as exc:
        logger.warning("slippage-model API failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@views_bp.route("/api/mc-backtest/<int:profile_id>", methods=["POST"])
@login_required
@admin_required
def api_mc_backtest(profile_id):
    """Item 5c — Monte Carlo backtest. Runs N MC trajectories on the
    profile's recent closed trades, returns the P&L distribution.
    Admin-only: a viewer triggering this would consume the admin's
    compute / AI cost budget.

    POST body (optional JSON): {n_sims: int, lookback_days: int}
    """
    import os
    import sqlite3
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No data yet"}), 404

    payload = request.get_json(silent=True) or {}
    n_sims = max(100, min(int(payload.get("n_sims") or 1000), 5000))
    lookback_days = max(7, min(int(payload.get("lookback_days") or 90), 365))

    # Pull recent closed trades from the journal
    try:
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(
                f"""SELECT entry_price, exit_price, side, pnl, pnl_pct,
                           symbol, exit_date
                FROM (
                    SELECT t1.fill_price AS entry_price,
                           t2.fill_price AS exit_price,
                           'long' AS side,
                           t2.pnl AS pnl,
                           NULL AS pnl_pct,
                           t1.symbol AS symbol,
                           t2.timestamp AS exit_date
                    FROM trades t1
                    JOIN trades t2 ON t2.symbol = t1.symbol
                      AND t2.id > t1.id
                    WHERE t1.side='buy' AND t2.side='sell'
                      AND t1.fill_price IS NOT NULL
                      AND t2.fill_price IS NOT NULL
                      AND t1.status='filled' AND t2.status='filled'
                      AND t2.timestamp >= datetime('now', '-{lookback_days} days')
                    ORDER BY t2.timestamp DESC
                    LIMIT 200
                )"""
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": f"DB query failed: {exc}"}), 500

    trades = [
        {"entry_price": float(r["entry_price"]),
         "exit_price": float(r["exit_price"]),
         "side": r["side"],
         # Use the exit date as the trade-day key; entry_date isn't on
         # the joined query and exit-day correlation is what dominates
         # slippage (every fill that hour shares the same liquidity).
         "entry_date": (r["exit_date"] or "")[:10],
         "exit_date": (r["exit_date"] or "")[:10]}
        for r in rows
    ]
    if len(trades) < 5:
        return jsonify({
            "available": False,
            "n_trades_found": len(trades),
            "message": (f"Need ≥5 closed trades in the last "
                        f"{lookback_days} days for a meaningful "
                        f"distribution; found {len(trades)}."),
        })

    from mc_backtest import run_monte_carlo
    result = run_monte_carlo(
        trades, db_path=db_path,
        market_type=profile.get("market_type"),
        n_sims=n_sims,
    )
    return jsonify({"available": True, **result})


def _summarize_options_trades(strategy_name, symbol, period_start,
                                period_end, trades):
    """Aggregate a list of trade dicts into the same shape that
    options_backtester.BacktestSummary.as_dict() returns. Used by the
    single-leg path which calls simulate_single_leg in a manual loop
    (backtest_strategy_over_period is multi-leg only)."""
    if not trades:
        return {
            "strategy_name": strategy_name, "symbol": symbol,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "n_trades": 0, "n_wins": 0, "n_losses": 0,
            "win_rate_pct": 0.0,
            "total_pnl_dollars": 0.0, "avg_pnl_dollars": 0.0,
            "best_trade_pnl": 0.0, "worst_trade_pnl": 0.0,
            "avg_days_held": 0.0, "sharpe_proxy": 0.0,
            "trades": [],
        }
    pnls = [float(t.get("pnl_dollars") or 0) for t in trades]
    days = [int(t.get("days_held") or 0) for t in trades]
    n_wins = sum(1 for p in pnls if p > 0)
    n_losses = sum(1 for p in pnls if p < 0)
    total = sum(pnls)
    avg = total / len(pnls)
    if len(pnls) > 1:
        var = sum((p - avg) ** 2 for p in pnls) / (len(pnls) - 1)
        std = var ** 0.5
        sharpe = (avg / std) if std > 0 else 0.0
    else:
        sharpe = 0.0
    return {
        "strategy_name": strategy_name, "symbol": symbol,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "n_trades": len(trades),
        "n_wins": n_wins, "n_losses": n_losses,
        "win_rate_pct": round(100.0 * n_wins / len(trades), 1),
        "total_pnl_dollars": round(total, 2),
        "avg_pnl_dollars": round(avg, 2),
        "best_trade_pnl": round(max(pnls), 2),
        "worst_trade_pnl": round(min(pnls), 2),
        "avg_days_held": round(sum(days) / len(days), 1),
        "sharpe_proxy": round(sharpe, 3),
        "trades": trades,
    }


@views_bp.route("/api/options-backtest", methods=["POST"])
@login_required
@admin_required
def api_options_backtest():
    """OPEN_ITEMS #5 — synthetic options backtester run from dashboard.

    Body: {symbol, strategy, lookback_days?, otm_pct?, target_dte?,
           cycle_days?, profit_target_pct_of_max?, stop_loss_pct_of_max?}

    strategy: 'long_put' | 'long_call' | 'bull_call_spread' |
              'bear_put_spread' | 'iron_condor'
    """
    payload = request.get_json(silent=True) or {}
    symbol = (payload.get("symbol") or "").upper().strip()
    strategy = (payload.get("strategy") or "").lower().strip()
    if not symbol or not strategy:
        return jsonify({"error": "symbol and strategy required"}), 400

    lookback_days = max(30, min(int(payload.get("lookback_days") or 365), 1825))
    otm_pct = max(0.005, min(float(payload.get("otm_pct") or 0.05), 0.30))
    target_dte = max(7, min(int(payload.get("target_dte") or 30), 120))
    cycle_days = max(1, min(int(payload.get("cycle_days") or 7), 30))
    profit_target = payload.get("profit_target_pct_of_max")
    stop_loss = payload.get("stop_loss_pct_of_max")

    from datetime import date as _d, timedelta as _td
    end = _d.today() - _td(days=2)    # avoid end-of-day-data races
    start = end - _td(days=lookback_days)

    try:
        from options_backtester import (
            backtest_strategy_over_period, historical_spot,
            simulate_single_leg,
        )
    except Exception as exc:
        return jsonify({"error": f"options backtester not available: {exc}"}), 500

    SINGLE_LEG = {"long_put", "long_call"}
    MULTI_LEG = {"bull_call_spread", "bear_put_spread", "iron_condor"}
    if strategy not in (SINGLE_LEG | MULTI_LEG):
        return jsonify(
            {"error": f"unknown strategy {strategy!r}. Supported: "
                       f"{sorted(SINGLE_LEG | MULTI_LEG)}"}
        ), 400

    try:
        if strategy in SINGLE_LEG:
            # Single-leg path: simulate_single_leg in a manual loop.
            # backtest_strategy_over_period only handles multi-leg, so
            # we reproduce its loop shape here.
            is_call = (strategy == "long_call")
            trades = []
            cursor = start
            while cursor <= end:
                spot = historical_spot(symbol, cursor)
                if spot is not None:
                    direction = otm_pct if is_call else -otm_pct
                    strike = round(spot * (1 + direction), 0)
                    expiry = cursor + _td(days=target_dte)
                    trade = simulate_single_leg(
                        symbol=symbol, entry_date=cursor,
                        strike=strike, expiry=expiry, is_call=is_call,
                        side="buy", qty=1,
                        profit_target_pct=profit_target,
                        stop_loss_pct=stop_loss,
                        time_stop_days_before_expiry=2,
                    )
                    if trade is not None:
                        trades.append(trade.as_dict())
                cursor += _td(days=cycle_days)
            out = _summarize_options_trades(
                strategy_name=strategy, symbol=symbol,
                period_start=start, period_end=end, trades=trades,
            )
        else:
            # Multi-leg path via the existing per-period backtester.
            from options_multileg import (
                build_bull_call_spread, build_bear_put_spread,
                build_iron_condor,
            )

            def _factory(as_of):
                spot = historical_spot(symbol, as_of)
                if spot is None:
                    return None
                expiry = as_of + _td(days=target_dte)
                if strategy == "bull_call_spread":
                    long_k = round(spot, 0)
                    short_k = round(spot * (1 + otm_pct), 0)
                    return build_bull_call_spread(
                        symbol, expiry, long_k, short_k, qty=1,
                    )
                if strategy == "bear_put_spread":
                    long_k = round(spot, 0)
                    short_k = round(spot * (1 - otm_pct), 0)
                    return build_bear_put_spread(
                        symbol, expiry, long_k, short_k, qty=1,
                    )
                if strategy == "iron_condor":
                    put_long = round(spot * (1 - otm_pct - 0.025), 0)
                    put_short = round(spot * (1 - otm_pct), 0)
                    call_short = round(spot * (1 + otm_pct), 0)
                    call_long = round(spot * (1 + otm_pct + 0.025), 0)
                    return build_iron_condor(
                        symbol, expiry, put_long, put_short,
                        call_short, call_long, qty=1,
                    )
                return None

            summary = backtest_strategy_over_period(
                strategy_factory=_factory,
                symbol=symbol,
                period_start=start,
                period_end=end,
                entry_rule=lambda s, d: True,
                cycle_days=cycle_days,
                profit_target_pct_of_max=profit_target,
                stop_loss_pct_of_max=stop_loss,
                time_stop_days_before_expiry=2,
            )
            out = summary.as_dict()
    except Exception as exc:
        logger.warning("Options backtest failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    # Equity curve (cumulative P&L over time). Both single-leg and
    # multi-leg trade dicts use `pnl_dollars` as the per-trade P&L field.
    cum = 0.0
    curve = []
    for t in out.get("trades", []):
        pnl = float(t.get("pnl_dollars") or 0)
        cum += pnl
        curve.append({
            "date": t.get("entry_date"),
            "cum_pnl": round(cum, 2),
            "trade_pnl": round(pnl, 2),
        })
    out["equity_curve"] = curve
    # Humanize the strategy identifier alongside the raw value so the
    # dashboard renders "Iron Condor" instead of `iron_condor`.
    from display_names import display_name as _dn_opts
    out["params"] = {
        "symbol": symbol, "strategy": strategy,
        "strategy_label": _dn_opts(strategy),
        "lookback_days": lookback_days, "otm_pct": otm_pct,
        "target_dte": target_dte, "cycle_days": cycle_days,
    }
    out["available"] = out["n_trades"] > 0
    return jsonify(out)


@views_bp.route("/api/mc-backtest-by-strategy/<int:profile_id>", methods=["POST"])
@login_required
@admin_required
def api_mc_backtest_by_strategy(profile_id):
    """Item 5c — per-strategy Monte Carlo backtest.

    Groups closed trades by `strategy` field; runs MC per group.
    Returns one row per strategy with its distribution stats so
    weak strategies stand out (high P(loss) under realistic
    slippage variance) vs robust ones (narrow band, low P(loss)).
    """
    import os, sqlite3
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No data yet"}), 404

    payload = request.get_json(silent=True) or {}
    n_sims = max(100, min(int(payload.get("n_sims") or 500), 2000))
    lookback_days = max(7, min(int(payload.get("lookback_days") or 90), 365))
    min_trades_per_strategy = int(payload.get("min_trades_per_strategy") or 5)

    try:
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(
                f"""SELECT t1.fill_price AS entry_price,
                           t2.fill_price AS exit_price,
                           'long' AS side,
                           COALESCE(t1.strategy, 'unknown') AS strategy,
                           t1.symbol AS symbol
                FROM trades t1
                JOIN trades t2 ON t2.symbol = t1.symbol AND t2.id > t1.id
                WHERE t1.side='buy' AND t2.side='sell'
                  AND t1.fill_price IS NOT NULL AND t2.fill_price IS NOT NULL
                  AND t1.status='filled' AND t2.status='filled'
                  AND t2.timestamp >= datetime('now', '-{lookback_days} days')
                ORDER BY t2.timestamp DESC
                LIMIT 500"""
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": f"DB query failed: {exc}"}), 500

    # Bucket by strategy
    buckets = {}
    for r in rows:
        strat = r["strategy"] or "unknown"
        buckets.setdefault(strat, []).append({
            "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"]),
            "side": r["side"],
        })

    from mc_backtest import run_monte_carlo
    market_type = profile.get("market_type")
    results = []
    for strat, trades in buckets.items():
        if len(trades) < min_trades_per_strategy:
            continue
        mc = run_monte_carlo(
            trades, db_path=db_path, market_type=market_type,
            n_sims=n_sims,
        )
        if mc.get("error"):
            continue
        results.append({
            "strategy": strat,
            "n_trades": len(trades),
            "p5_return": mc.get("p5_return"),
            "p50_return": mc.get("p50_return"),
            "p95_return": mc.get("p95_return"),
            "mean_return": mc.get("mean_return"),
            "std_return": mc.get("std_return"),
            "prob_loss": mc.get("prob_loss"),
        })
    # Sort by median return descending — best strategies first
    results.sort(key=lambda r: r.get("p50_return", 0) or 0, reverse=True)
    return jsonify({
        "available": len(results) > 0,
        "n_strategies": len(results),
        "n_sims": n_sims,
        "lookback_days": lookback_days,
        "rows": results,
    })


@views_bp.route("/api/slippage-history/<int:profile_id>")
@login_required
def api_slippage_history(profile_id):
    """Item 5c — slippage predicted vs realized over time, for
    calibration-drift tracking.

    Returns list of (timestamp, symbol, side, predicted_bps, realized_bps)
    for the last N filled trades, plus aggregate metrics: mean / std
    of (realized - predicted), correlation, n_samples.
    """
    import os, sqlite3
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No data yet"}), 404

    try:
        with closing(open_profile_db(db_path)) as conn:
            rows = conn.execute(
                """SELECT timestamp, symbol, side, qty,
                          decision_price, fill_price, slippage_pct,
                          predicted_slippage_bps
                FROM trades
                WHERE predicted_slippage_bps IS NOT NULL
                  AND fill_price IS NOT NULL
                  AND decision_price IS NOT NULL
                  AND status='filled'
                ORDER BY timestamp DESC
                LIMIT 200"""
            ).fetchall()
    except Exception as exc:
        return jsonify({"error": f"DB query failed: {exc}"}), 500

    series = []
    deltas = []
    for r in rows:
        side = (r["side"] or "").lower()
        dp = float(r["decision_price"])
        fp = float(r["fill_price"])
        if dp <= 0 or fp <= 0:
            continue
        # Realized in adverse direction (positive bps = bad)
        if "buy" in side:
            realized_bps = (fp - dp) / dp * 10000
        else:
            realized_bps = (dp - fp) / dp * 10000
        predicted_bps = float(r["predicted_slippage_bps"] or 0)
        delta = realized_bps - predicted_bps
        deltas.append(delta)
        series.append({
            "timestamp": r["timestamp"],
            "symbol": r["symbol"],
            "side": side,
            "predicted_bps": round(predicted_bps, 2),
            "realized_bps": round(realized_bps, 2),
            "delta_bps": round(delta, 2),
        })

    n = len(deltas)
    mean_delta = sum(deltas) / n if n else 0.0
    if n >= 2:
        var = sum((d - mean_delta) ** 2 for d in deltas) / (n - 1)
        std_delta = var ** 0.5
    else:
        std_delta = 0.0

    # Pearson correlation predicted vs realized
    correlation = None
    if n >= 5:
        preds = [s["predicted_bps"] for s in series]
        reals = [s["realized_bps"] for s in series]
        mp = sum(preds) / n
        mr = sum(reals) / n
        num = sum((p - mp) * (r - mr) for p, r in zip(preds, reals))
        sp = (sum((p - mp) ** 2 for p in preds)) ** 0.5
        sr = (sum((r - mr) ** 2 for r in reals)) ** 0.5
        if sp > 0 and sr > 0:
            correlation = num / (sp * sr)

    return jsonify({
        "available": n > 0,
        "n_samples": n,
        "mean_delta_bps": round(mean_delta, 2),
        "std_delta_bps": round(std_delta, 2),
        "correlation": round(correlation, 3) if correlation is not None else None,
        "rows": list(reversed(series)),     # oldest first for chart
    })


@views_bp.route("/api/weightable-signals-matrix")
@login_required
def api_weightable_signals_matrix():
    """Cross-profile matrix view of every weightable signal.

    Returns rows = signals, columns = profiles, cells = weight.
    Solves the "all-profiles view looked untuned" UX gap (2026-05-15):
    when no profile was selected, the per-profile endpoint defaulted
    to pid 1 and showed all 1.0 defaults, making the system look like
    it had never tuned a signal — when in reality 7 of 10 profiles
    had at least one override.
    """
    from signal_weights import (
        WEIGHTABLE_SIGNALS, get_all_weights, display_label,
    )
    user_id = current_user.effective_user_id
    profiles = get_user_profiles(user_id)
    enabled = [p for p in profiles if p.get("enabled")]
    # Preload each profile's overrides once.
    overrides_by_pid = {
        p["id"]: get_all_weights(p) for p in enabled
    }
    rows = []
    for entry in WEIGHTABLE_SIGNALS:
        if isinstance(entry, tuple) and len(entry) >= 2:
            name = entry[0]
            label = entry[1]
        else:
            name = entry if isinstance(entry, str) else str(entry)
            label = display_label(name)
        cells = []
        n_overridden_for_this_signal = 0
        for p in enabled:
            ov = overrides_by_pid[p["id"]]
            weight = ov.get(name, 1.0)
            is_ov = name in ov
            if is_ov:
                n_overridden_for_this_signal += 1
            cells.append({
                "profile_id": p["id"],
                "weight": float(weight),
                "is_overridden": is_ov,
            })
        rows.append({
            "name": name,
            "label": label,
            "cells": cells,
            "n_overridden": n_overridden_for_this_signal,
        })
    # Sort: most-overridden signals first (most-interesting to see),
    # then alphabetical for stable secondary order.
    rows.sort(key=lambda r: (-r["n_overridden"], r["name"]))
    return jsonify({
        "profiles": [
            {"id": p["id"], "name": p["name"]} for p in enabled
        ],
        "n_signals": len(rows),
        "n_total_overrides": sum(r["n_overridden"] for r in rows),
        "rows": rows,
    })


@views_bp.route("/api/weightable-signals/<int:profile_id>")
@login_required
def api_weightable_signals(profile_id):
    """List EVERY weightable signal + its current weight for this profile.

    Solves the "hidden lever" problem for Layer-2 weights: get_all_weights()
    only returns non-default (≠1.0) entries, so users couldn't see the
    full list of tunable signals without reading the code.
    """
    import os
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    from signal_weights import (
        WEIGHTABLE_SIGNALS, get_all_weights, display_label,
    )
    overrides = get_all_weights(profile)   # only non-default entries
    rows = []
    for entry in WEIGHTABLE_SIGNALS:
        if isinstance(entry, tuple) and len(entry) >= 2:
            name = entry[0]
            label = entry[1]
        else:
            name = entry if isinstance(entry, str) else str(entry)
            label = display_label(name)
        weight = overrides.get(name, 1.0)
        is_overridden = name in overrides
        rows.append({
            "name": name,
            "label": label,
            "weight": float(weight),
            "is_overridden": is_overridden,
            "is_disabled": float(weight) <= 0.0,
        })
    rows.sort(key=lambda r: (not r["is_overridden"], r["name"]))
    return jsonify({
        "profile_id": profile_id,
        "n_signals": len(rows),
        "n_overridden": sum(1 for r in rows if r["is_overridden"]),
        "rows": rows,
    })


@views_bp.route("/api/attention-signals/<int:profile_id>")
@login_required
def api_attention_signals(profile_id):
    """Item 3a — recent attention-signal snapshot for held + watched
    symbols on this profile (Google Trends + Wikipedia + App Store)."""
    import os
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    # Get current positions for this profile
    try:
        from models import build_user_context_from_profile
        from client import get_positions
        ctx = build_user_context_from_profile(profile_id)
        positions = get_positions(ctx=ctx) or []
        symbols = sorted({p.get("symbol", "") for p in positions
                            if p.get("symbol")})
    except Exception:
        symbols = []

    if not symbols:
        return jsonify({"available": False, "symbols": [],
                          "rows": []})

    from alternative_data import (
        get_google_trends_signal, get_wikipedia_pageviews_signal,
        get_app_store_ranking,
    )
    rows = []
    has_any_data = False
    has_any_gt = False
    has_any_wp = False
    has_any_ap = False
    gt_disabled_reason = None
    for sym in symbols[:25]:   # cap at 25 to keep API call cost bounded
        gt = get_google_trends_signal(sym) or {}
        wp = get_wikipedia_pageviews_signal(sym) or {}
        ap = get_app_store_ranking(sym) or {}
        if gt.get("disabled_reason") and not gt_disabled_reason:
            gt_disabled_reason = gt["disabled_reason"]
        # Only include rows that have AT LEAST ONE signal of data.
        # A row of all dashes adds noise without information.
        if not (gt.get("has_data") or wp.get("has_data") or ap.get("has_data")):
            continue
        has_any_data = True
        if gt.get("has_data"): has_any_gt = True
        if wp.get("has_data"): has_any_wp = True
        if ap.get("has_data"): has_any_ap = True
        rows.append({
            "symbol": sym,
            "google_trends": {
                "z": gt.get("trend_z_score"),
                "direction": gt.get("trend_direction"),
                "current_index": gt.get("current_index"),
                "has_data": gt.get("has_data", False),
            },
            "wikipedia": {
                "z": wp.get("pageview_z_score"),
                "spike": wp.get("pageview_spike_flag"),
                "current_7d_avg": wp.get("current_7d_avg"),
                "article": wp.get("article"),
                "has_data": wp.get("has_data", False),
            },
            "app_store": {
                "best_grossing_rank": ap.get("best_grossing_rank"),
                "best_free_rank": ap.get("best_free_rank"),
                "primary_app": (ap.get("apps") or [{}])[0].get("name"),
                "has_data": ap.get("has_data", False),
            },
        })

    # When no held position has any attention data, surface a clear
    # explanation rather than an empty table — the user shouldn't
    # have to figure out whether the panel is broken or whether their
    # holdings just don't have consumer-attention coverage.
    explain = None
    if not has_any_data:
        if gt_disabled_reason:
            explain = (
                "Google Trends is rate-limited and currently "
                "unavailable. Wikipedia + App Store have no coverage "
                "for the held symbols (these tickers don't appear in "
                "consumer-attention sources)."
            )
        else:
            explain = (
                "No attention-signal data for current holdings. "
                "These symbols don't appear in Google Trends, "
                "Wikipedia, or the App Store charts. Attention "
                "signals are most useful for consumer-brand tickers "
                "(AAPL, TSLA, NVDA, META, NFLX) — institutional / "
                "ETF / dividend tickers typically have no coverage."
            )

    return jsonify({
        "available": True,
        "symbols": symbols,
        "rows": rows,
        "has_any_data": has_any_data,
        "has_any_gt": has_any_gt,
        "has_any_wp": has_any_wp,
        "has_any_ap": has_any_ap,
        "google_trends_disabled_reason": gt_disabled_reason,
        "explain_when_empty": explain,
    })


@views_bp.route("/admin")
@login_required
@admin_required
def admin():
    """Admin panel — user list, API usage."""
    from models import _get_conn
    with closing(_get_conn()) as conn:
        users = conn.execute(
            "SELECT id, email, display_name, is_admin, is_active, created_at, last_login_at "
            "FROM users ORDER BY id"
        ).fetchall()
    users = [dict(u) for u in users]

    # Per-user API usage: each user's API Calls Today must reflect ONLY
    # the profiles owned by that user. A system-wide glob would attribute
    # every user's cost to every other user — wrong as a number and a
    # privacy leak as soon as a second account exists.
    from ai_cost_ledger import spend_summary
    from models import get_user_profiles
    for u in users:
        profiles = get_user_profiles(u["id"])
        calls = 0
        cost = 0.0
        for p in profiles:
            db_path = f"quantopsai_profile_{p['id']}.db"
            try:
                s = spend_summary(db_path)
                calls += s["today"]["calls"]
                cost += s["today"]["usd"]
            except Exception as exc:
                logger.warning(
                    "admin: spend_summary failed for user=%s profile=%s db=%s: %s",
                    u["id"], p["id"], db_path, exc,
                )
        u["api_calls_today"] = calls
        u["api_cost_today"] = round(cost, 2)

    return render_template("admin.html", users=users)


# ---------------------------------------------------------------------------
# Activity Feed API
# ---------------------------------------------------------------------------

@views_bp.route("/api/activity")
@login_required
def api_activity():
    """Return JSON array of activity log entries for the current user.

    Each entry carries `timestamp_friendly` (server-rendered via
    `friendly_time`) so the JS ticker doesn't re-implement timestamp
    formatting — single source of truth, matches the format used in
    server-rendered tables. Issue 13 fix.
    """
    from display_names import friendly_time, humanize
    profile_id = request.args.get("profile_id", type=int)
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 10, type=int)
    limit = min(limit, 100)  # cap at 100

    entries = get_activity_feed(current_user.effective_user_id, profile_id=profile_id,
                                limit=limit, offset=offset)
    for e in entries:
        e["timestamp_friendly"] = friendly_time(e.get("timestamp"))
        # 2026-05-12 — humanize title + detail so raw AI tokens
        # (STRONG_SELL, MULTILEG_OPEN, bull_put_spread) don't leak
        # into the Strategy Activity ticker. The activity feed
        # detail field stores raw AI reasoning verbatim — the LLM
        # routinely echoes the action token it was asked about.
        # Without this pass the operator sees "STRONG_SELL signal
        # (-2/4 score)..." on a panel that's supposed to be
        # human-readable. Idempotent (humanize is a no-op on
        # already-humanized text).
        if e.get("title"):
            e["title"] = humanize(e["title"])
        if e.get("detail"):
            e["detail"] = humanize(e["detail"])
    total = get_activity_count(current_user.effective_user_id, profile_id=profile_id)
    return jsonify({"entries": entries, "total": total})


@views_bp.route("/universe/<int:profile_id>")
@login_required
def universe_popup(profile_id):
    """Render the universe popup window page."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        abort(404)

    market_type = profile["market_type"]
    segment = SEGMENTS.get(market_type)
    if not segment:
        abort(404)

    base_universe = sorted(segment["universe"])

    custom_watchlist = profile.get("custom_watchlist", []) or []
    if isinstance(custom_watchlist, str):
        try:
            custom_watchlist = json.loads(custom_watchlist)
        except (json.JSONDecodeError, TypeError):
            custom_watchlist = []

    from models import get_cached_names
    all_syms = base_universe + [s.strip().upper() for s in custom_watchlist if s.strip()]
    names = get_cached_names(all_syms)
    base_set = set(base_universe)

    symbols = []
    for sym in base_universe:
        symbols.append({"symbol": sym, "name": names.get(sym, ""), "custom": False})
    for sym in custom_watchlist:
        sym = sym.strip().upper()
        if sym and sym not in base_set:
            symbols.append({"symbol": sym, "name": names.get(sym, ""), "custom": True})

    return render_template("universe_popup.html",
                           profile=profile,
                           symbols=symbols,
                           base_count=len(base_universe),
                           custom_count=len([s for s in symbols if s["custom"]]))


@views_bp.route("/api/universe/<int:profile_id>")
@login_required
def api_universe(profile_id):
    """Return the full symbol universe for a trading profile."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Not found"}), 404

    market_type = profile["market_type"]
    segment = SEGMENTS.get(market_type)
    if not segment:
        return jsonify({"error": f"Unknown market type: {market_type}"}), 400

    # Base universe from segments.py
    base_universe = list(segment["universe"])
    base_set = set(base_universe)

    # Custom watchlist from the profile
    custom_watchlist = profile.get("custom_watchlist", []) or []
    if isinstance(custom_watchlist, str):
        try:
            custom_watchlist = json.loads(custom_watchlist)
        except (json.JSONDecodeError, TypeError):
            custom_watchlist = []

    # Get cached names (fast — from DB)
    from models import get_cached_names
    all_syms = sorted(base_universe) + [s.strip().upper() for s in custom_watchlist if s.strip()]
    names = get_cached_names(all_syms)

    # Build symbol list: base first, then custom
    symbols = []
    for sym in sorted(base_universe):
        symbols.append({"symbol": sym, "name": names.get(sym, sym), "source": "base"})

    custom_count = 0
    for sym in custom_watchlist:
        sym = sym.strip().upper()
        if sym and sym not in base_set:
            symbols.append({"symbol": sym, "name": names.get(sym, sym), "source": "custom"})
            custom_count += 1

    market_type_name = SEGMENTS[market_type].get("name", market_type)

    return jsonify({
        "market_type": market_type,
        "market_type_name": market_type_name,
        "base_count": len(base_universe),
        "custom_count": custom_count,
        "symbols": symbols,
    })


@views_bp.route("/api/universe/<int:profile_id>/cache-names", methods=["POST"])
@login_required
@admin_required
def api_cache_universe_names(profile_id):
    """Trigger background caching of symbol names for a profile's universe."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Not found"}), 404

    market_type = profile["market_type"]
    segment = SEGMENTS.get(market_type)
    if not segment:
        return jsonify({"error": "Unknown market type"}), 400

    from models import fetch_and_cache_names
    universe = list(segment["universe"])
    names = fetch_and_cache_names(universe)
    return jsonify({"cached": len(names)})


@views_bp.route("/scanning/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_scanning():
    """Admin-only: start/stop AI scanning for the current user."""
    if not current_user.is_admin:
        abort(403)
    from models import is_scanning_active, set_scanning_active
    currently_active = is_scanning_active(current_user.effective_user_id)
    set_scanning_active(current_user.effective_user_id, not currently_active)
    status = "started" if not currently_active else "stopped"
    flash(f"AI scanning {status}.", "success")
    return redirect(url_for("views.dashboard"))


@views_bp.route("/api/scheduler-status")
@login_required
def api_scheduler_status():
    """Return scheduler timing info for countdown timers."""
    import time as _time
    try:
        with open("scheduler_status.json") as f:
            status = json.load(f)
        now = _time.time()
        # Calculate seconds remaining for each cycle
        status["scan_remaining"] = max(0, int(status.get("next_scan", 0) - now))
        status["exit_remaining"] = max(0, int(status.get("next_exit_check", 0) - now))
        status["ai_remaining"] = max(0, int(status.get("next_ai_resolve", 0) - now))
        # Market open flag — check actual market hours, not scan timing
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        _et = _dt.now(ZoneInfo("America/New_York"))
        _wd = _et.weekday()  # 0=Mon, 6=Sun
        _is_market_hours = (_wd < 5 and 9 <= _et.hour < 16 and
                            not (_et.hour == 9 and _et.minute < 30))
        status["market_open"] = (_is_market_hours or
                                  status["scan_remaining"] > 0 or
                                  status["exit_remaining"] > 0)
        return jsonify(status)
    except FileNotFoundError:
        return jsonify({"error": "Scheduler not running yet", "scan_remaining": 0, "exit_remaining": 0, "ai_remaining": 0})


@views_bp.route("/api/scan-status/<int:profile_id>")
@login_required
def api_scan_status(profile_id):
    """Return current scan step for a profile + next scan countdown."""
    from scan_status import get_status
    status = get_status(profile_id) or {}

    # Add next scan countdown from task_runs
    try:
        db = f"quantopsai_profile_{profile_id}.db"
        with closing(open_profile_db(db)) as conn:
            row = conn.execute(
                "SELECT started_at FROM task_runs "
                "WHERE task_name LIKE '%Scan%' AND status IN ('completed','failed') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row:
            from datetime import datetime as _dt_scan, timezone
            last = _dt_scan.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now = _dt_scan.now(timezone.utc)
            elapsed = (now - last).total_seconds()
            status["next_scan_sec"] = max(0, int(900 - elapsed))
    except Exception as exc:
        logger.warning(
            "api_scan_status: next_scan_sec lookup failed for profile %s: %s",
            profile_id, exc,
        )

    return jsonify(status if status else {"step": None})


# ---------------------------------------------------------------------------
# Per-user TTL cache for Alpaca-polled endpoints (Issue 14)
# ---------------------------------------------------------------------------
# JS auto-refresh polls /api/dashboard-totals and /api/portfolio/<id>
# every ~30s. Without server-side caching, each poll cascades into
# 2 Alpaca calls per profile (get_account_info + get_positions). With
# 11 profiles, that's 22 calls per 30s = 44/min, multiplied by every
# open browser tab. Account state doesn't change second-to-second;
# the calls are wasted. A 30s per-(user, route, args) TTL cache
# matches the JS poll cadence — every poll within the window hits
# cache, the first poll after expiration fetches fresh.
#
# Failures are NOT cached: a 500 response or an exception leaves the
# cache untouched so a brief Alpaca outage doesn't cascade for 30s
# after recovery. The `_ttl_cache_set` helper enforces this.

_TTL_CACHE: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
_TTL_CACHE_DEFAULT = 30.0  # seconds


def _ttl_cache_get(key: Tuple[Any, ...], ttl: float = _TTL_CACHE_DEFAULT):
    """Return cached value for `key` if it's within `ttl` seconds.
    Returns None on miss/expiration."""
    cached = _TTL_CACHE.get(key)
    if cached is None:
        return None
    if (time.time() - cached[0]) >= ttl:
        return None
    return cached[1]


def _ttl_cache_set(key: Tuple[Any, ...], value: Any) -> None:
    """Store value with current timestamp."""
    _TTL_CACHE[key] = (time.time(), value)


@views_bp.route("/api/portfolio/<int:profile_id>")
@login_required
def api_portfolio(profile_id):
    """Return live portfolio data for a profile (positions, account info).

    Cached 30s per (user, profile) — JS polls this every 30s per
    profile, so every-poll Alpaca calls were wasted. See Issue 14
    deep-dive in `AUDIT_2026_05_09.md`.
    """
    try:
        profile = get_trading_profile(profile_id)
        if not profile or profile["user_id"] != current_user.effective_user_id:
            return jsonify({"error": "not found"}), 404

        cache_key = ("api_portfolio",
                      current_user.effective_user_id, profile_id)
        cached = _ttl_cache_get(cache_key)
        if cached is not None:
            return jsonify(cached)

        ctx = build_user_context_from_profile(profile_id)
        account = _safe_account_info(ctx)
        positions = _enriched_positions(ctx, profile_id)
        pending_orders = _safe_pending_orders(ctx)
        # 2026-05-20 — expose initial_capital so the JS auto-refresh
        # can recompute profile-level P&L (equity − initial_capital)
        # without needing a second server roundtrip. Server-rendered
        # P&L stays in the dashboard template; this just keeps it
        # live across the 30s auto-refresh.
        payload = {
            "account": account,
            "positions": positions,
            "pending_orders": pending_orders,
            "initial_capital": float(profile.get("initial_capital") or 0),
        }
        # Cache only on success (we got here without an exception).
        _ttl_cache_set(cache_key, payload)
        return jsonify(payload)
    except Exception as exc:
        # Failures NOT cached; next poll retries cleanly.
        return jsonify({"error": str(exc)}), 500


@views_bp.route("/api/positions-html/<int:profile_id>")
@login_required
def api_positions_html(profile_id):
    """Server-rendered Open Positions table HTML. Used by the dashboard
    auto-refresh so the JS doesn't have to duplicate the expandable
    trade-row markup.

    Returns the SAME 3-pane (Stocks / Options / All) structure the
    initial dashboard render produces — without it, the auto-refresh
    swap would replace the tab structure with a flat table and tabs
    would stop working ~15s after page load. Caught 2026-05-11.
    """
    from flask import render_template_string
    try:
        profile = get_trading_profile(profile_id)
        if not profile or profile["user_id"] != current_user.effective_user_id:
            return "not found", 404
        ctx = build_user_context_from_profile(profile_id)
        positions = _enriched_positions(ctx, profile_id)
        stock_positions = [p for p in positions
                           if not p.get("occ_symbol")]
        option_positions = [p for p in positions
                            if p.get("occ_symbol")]
        # Default-active pane matches the initial-render template:
        # "All" — guarantees the dashboard is populated on load
        # regardless of profile composition. Inline style.display
        # bypasses any cached CSS state.
        return render_template_string(
            '{% import "_trades_table.html" as trades_tpl %}'
            '<div id="op-pane-stocks-{{ pid }}" class="perf-tab-content op-pane" style="display:none;">'
            '{{ trades_tpl.render_trades(stock_positions, show_profile=False, '
            'table_id="trades-table-stocks-" ~ pid, '
            'empty_message="No open stock positions in this profile.") }}'
            '</div>'
            '<div id="op-pane-options-{{ pid }}" class="perf-tab-content op-pane" style="display:none;">'
            '{{ trades_tpl.render_trades(option_positions, show_profile=False, '
            'table_id="trades-table-options-" ~ pid, '
            'empty_message="No open option positions in this profile.") }}'
            '</div>'
            '<div id="op-pane-all-{{ pid }}" class="perf-tab-content op-pane active" style="display:block;">'
            '{{ trades_tpl.render_trades(positions, show_profile=False, '
            'table_id="trades-table-all-" ~ pid, '
            'empty_message="No open positions in this profile.") }}'
            '</div>',
            positions=positions,
            stock_positions=stock_positions,
            option_positions=option_positions,
            pid=profile_id,
        )
    except Exception as exc:
        return f"<p class='muted'>Failed to refresh: {exc}</p>", 500


@views_bp.route("/api/dashboard-totals")
@login_required
def api_dashboard_totals():
    """Live per-profile equity / cash / positions / P&L / AI-cost-today
    snapshot for the dashboard overview. Polled by JS at 30s cadence
    during market hours and 5min otherwise. Returns:
      {profiles: [{id, name, equity, cash, num_positions, cost_today,
                   initial_capital, pnl, pnl_pct}],
       total_cost}
    Book-wide equity/P&L/cash/position sums were dropped 2026-05-22 — not
    additive across heterogeneous strategies; per-account P&L % (in each
    row) is the cross-profile comparison. AI cost is the one true total.

    Cached 30s per user — JS polls this every 30s, so every-poll
    Alpaca calls were wasted (11 profiles × 2 calls = 22/poll).
    See Issue 14 deep-dive in `AUDIT_2026_05_09.md`.
    """
    from models import get_active_profiles, build_user_context_from_profile
    from ai_cost_ledger import spend_summary
    from client import get_account_info, get_positions

    cache_key = ("api_dashboard_totals", current_user.effective_user_id)
    cached = _ttl_cache_get(cache_key)
    if cached is not None:
        return jsonify(cached)

    profiles = get_active_profiles(user_id=current_user.effective_user_id)
    rows = []
    # AI cost is the only book-wide total still shown on the overview —
    # equity/P&L/cash/position sums were removed 2026-05-22 (not additive
    # across heterogeneous strategies; compare by per-row P&L % instead).
    total_cost = 0.0
    for p in profiles:
        try:
            ctx = build_user_context_from_profile(p["id"])
            account = get_account_info(ctx=ctx)
            positions = get_positions(ctx=ctx)
            # Per-profile today's USD spend. Mirrors the server-render
            # path used at the dashboard route initial load so the
            # JS auto-refresh shows the same value the page loaded with.
            try:
                cost_today = float(
                    (spend_summary(ctx.db_path) or {})
                    .get("today", {}).get("usd") or 0
                )
            except Exception as exc:
                logger.warning(
                    "dashboard-totals: spend_summary failed for "
                    "profile %s: %s", p.get("id"), exc,
                )
                cost_today = 0.0
            equity = float(account.get("equity") or 0)
            cash = float(account.get("cash") or 0)
            n_pos = len(positions)
            # Total P&L = current equity − initial capital. Drives the
            # P&L column on the overview table (restored 2026-05-18
            # after the operator noted it disappeared from the
            # overview at some point even though per-profile views
            # still surfaced it).
            initial_capital = float(p.get("initial_capital") or 0)
            pnl = equity - initial_capital if initial_capital > 0 else 0.0
            # % return on initial capital — the cross-account-comparable
            # number the overview table sorts on (each profile runs a
            # different strategy at a different capital base, so absolute
            # P&L isn't comparable; % is).
            pnl_pct = (pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0
            rows.append({
                "id": p["id"],
                "name": p["name"],
                "equity": equity,
                "cash": cash,
                "num_positions": n_pos,
                "cost_today": cost_today,
                "initial_capital": initial_capital,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })
            total_cost += cost_today
        except Exception as exc:
            # Surface at WARNING so silent skips don't hide a real
            # failure (e.g., the user_context import bug that 500'd
            # this endpoint silently for weeks before 2026-05-09).
            logger.warning(
                "dashboard-totals: %s (id=%s) skipped: %s",
                p.get("name"), p.get("id"), exc,
            )
            continue
    payload = {
        "profiles": rows,
        # Only AI cost is summed book-wide — see the comment at the
        # accumulator init above. Per-profile equity/cash/pnl/pnl_pct ride
        # in `rows` for the per-row live refresh.
        "total_cost": total_cost,
    }
    # Cache only on success. The endpoint never raises explicitly
    # (per-profile failures are absorbed via the WARNING above), so
    # we get here for any non-empty result. An empty `rows` list with
    # no profiles configured is still a valid payload to cache.
    _ttl_cache_set(cache_key, payload)
    return jsonify(payload)


@views_bp.route("/api/cycle-data/<int:profile_id>")
@login_required
def api_cycle_data(profile_id):
    """Return the last AI cycle data for a profile (decisions, shortlist, reasoning).

    LLM-generated reasoning text is humanized server-side so dashboard
    rendering doesn't leak `STRONG_BUY` / `bull_put_spread` / etc.
    """
    try:
        with open(f"cycle_data_{profile_id}.json") as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify({"error": "No cycle data yet", "shortlist": [], "trades_selected": []})

    from display_names import humanize
    if isinstance(data.get("ai_reasoning"), str):
        data["ai_reasoning"] = humanize(data["ai_reasoning"])
    for t in (data.get("trades_selected") or []):
        if isinstance(t.get("reasoning"), str):
            t["reasoning"] = humanize(t["reasoning"])
        if isinstance(t.get("action"), str):
            t["action"] = humanize(t["action"])
    # 2026-05-12 — shortlist's `signal` field renders raw in the
    # Candidates Considered panel (JS does `td>' + c.signal + '<`
    # in templates/dashboard.html:676). Humanize server-side so
    # STRONG_BUY → "Strong Buy" before it reaches the browser.
    # Same for track_record (LLM-emitted) and alt-data strings.
    for c in (data.get("shortlist") or []):
        if isinstance(c.get("signal"), str):
            c["signal"] = humanize(c["signal"])
        if isinstance(c.get("track_record"), str):
            c["track_record"] = humanize(c["track_record"])
        if isinstance(c.get("options_signal"), str):
            c["options_signal"] = humanize(c["options_signal"])
        if isinstance(c.get("options_oracle_summary"), str):
            c["options_oracle_summary"] = humanize(c["options_oracle_summary"])

    # 2026-05-12 — profit-taking analytics on the brain ticker.
    # `stop_to_tp_ratio` shows whether the new tuner's converged
    # toward the desired 0.5-2.5 band; `mfe_capture` shows
    # whether we're leaving money on the table. Operator sees
    # what the AI sees.
    try:
        from mfe_capture import compute_stop_to_tp_ratio, compute_capture_ratio
        db_path = f"quantopsai_profile_{profile_id}.db"
        s2t = compute_stop_to_tp_ratio(db_path, window_days=30)
        cap = compute_capture_ratio(db_path, lookback=50)
        data["stop_to_tp"] = s2t
        data["mfe_capture"] = cap
    except Exception as _exc:
        logger.debug(
            "cycle_data profit-taking metrics skipped: %s", _exc
        )
        data["stop_to_tp"] = None
        data["mfe_capture"] = None

    # 2026-05-12 — surface the AI-intent vs executed-outcome
    # mismatch on the brain ticker. Mack's case: AI proposed
    # SHORT F (1.25% equity) but F was already held long; the
    # trade pipeline routed STRONG_SELL action through the
    # close-existing-long branch (trade_pipeline.py:1056-1057).
    # Result: brain ticker said "SHORT F" but no short ever
    # opened. Stamp each trades_selected entry with what
    # actually traded for the symbol within the last 4 hours.
    try:
        import sqlite3 as _sql
        from contextlib import closing
        db_path = f"quantopsai_profile_{profile_id}.db"
        with closing(_sql.connect(db_path)) as conn:
            # Most recent trade per symbol in the last 4 hours
            recent = {}
            rows = conn.execute(
                "SELECT symbol, side, signal_type, status, timestamp, "
                "occ_symbol FROM trades "
                "WHERE timestamp >= datetime('now', '-4 hours') "
                "ORDER BY timestamp DESC"
            ).fetchall()
            for r in rows:
                sym = (r[0] or "").upper()
                if sym and sym not in recent:
                    recent[sym] = {
                        "side": r[1], "signal_type": r[2],
                        "status": r[3], "timestamp": r[4],
                        "is_option": bool(r[5]),
                    }
        from display_names import action_label as _act_label
        for t in (data.get("trades_selected") or []):
            sym = (t.get("symbol") or "").upper()
            if not sym:
                continue
            rec = recent.get(sym)
            if rec:
                executed = _act_label(
                    rec["side"], rec.get("signal_type"),
                    is_option=rec.get("is_option", False),
                )
                t["executed_action"] = executed
                # Detect intent-vs-outcome mismatch. AI's action is
                # already humanized above ("Strong Sell" / "Short").
                # The conversion we care about: intent says "Short"
                # but the executed action was "Long Close".
                intent = (t.get("action") or "").lower()
                if "short" in intent and executed == "Long Close":
                    t["execution_outcome"] = "converted_to_close"
                    t["execution_outcome_display"] = (
                        "Executed as long-close — "
                        "F was already held long, can't open a new "
                        "short on the same symbol")
                # 2026-05-14 — surface canceled trades on the brain
                # ticker. Limit-order profiles can submit a trade
                # that never fills (market moves past the limit) and
                # the stale-cleanup task cancels it after N minutes.
                # Without this, the brain ticker shows the trade as
                # if it fired and the operator goes hunting for a
                # non-existent position. Skip if a more specific
                # outcome was already stamped above.
                elif rec.get("status") == "canceled":
                    t["execution_outcome"] = "canceled"
                    t["execution_outcome_display"] = (
                        "Order canceled — limit price not filled "
                        "within the stale-order window, or the order "
                        "was canceled by reconcile (broker had no "
                        "matching position)."
                    )
            else:
                # 2026-05-14 — NO-FILL: AI selected the trade but no
                # row was created in the trades table. Most common
                # causes: already-positioned dedup ("Already short
                # KO"), pre-broker safety gate, or post-AI
                # meta-model suppression (meta_prob below the
                # SUPPRESSION_THRESHOLD). Without this badge, the
                # trade silently disappears from the brain ticker
                # and the operator can't tell what happened.
                t["execution_outcome"] = "no_fill"
                t["execution_outcome_display"] = (
                    "Not submitted — most likely already-positioned "
                    "dedup, pre-broker safety gate, or post-AI "
                    "meta-model suppression. No trades row was "
                    "created."
                )
    except Exception as _exc:
        logger.warning(
            "api_cycle_data: execution-outcome enrichment failed for "
            "profile %d: %s — proceeding without badges",
            profile_id, _exc,
        )

    # TODO #5: stamp execution outcome on each TRADES SELECTED row.
    # Cross-reference recent broker_rejections for this profile so the
    # AI Brain panel can render an inline "REJECTED" badge with the
    # reason instead of the trade silently disappearing (Mack's CWAN
    # incident — operator went looking for a trade that Alpaca had
    # rejected via the cross-direction guard).
    try:
        from journal import get_recent_broker_rejections
        db_path = f"quantopsai_profile_{profile_id}.db"
        rejections = get_recent_broker_rejections(db_path, hours=2)
        # Index by symbol for O(1) lookup. Within the 2h window, the
        # most recent rejection per symbol is the relevant one.
        rej_by_symbol = {}
        for r in rejections:
            sym = (r.get("symbol") or "").upper()
            if sym and sym not in rej_by_symbol:
                rej_by_symbol[sym] = r
        for t in (data.get("trades_selected") or []):
            sym = (t.get("symbol") or "").upper()
            if not sym:
                continue
            r = rej_by_symbol.get(sym)
            if r:
                # Humanize the rejection_code so "cross_direction_long_blocked"
                # reads as "Cross Direction Long Blocked" in the badge tooltip.
                t["execution_outcome"] = "rejected"
                t["rejection_code"] = r.get("rejection_code") or "other"
                t["rejection_code_display"] = humanize(
                    r.get("rejection_code") or "other"
                )
                # Truncate the broker message — Alpaca reasons can be
                # multi-line; the UI just needs the gist on hover.
                msg = r.get("broker_message") or ""
                t["rejection_message"] = msg[:240]
                # 2026-05-12 — for specialist_veto rejections, parse
                # the specialist NAME from broker_message format
                # "specialist veto (<name>): <reason>" and surface
                # as vetoed_by so the badge can attribute the block
                # to a specific reviewer (option_spread_risk vs.
                # adversarial_reviewer vs. risk_assessor etc.).
                if r.get("rejection_code") == "specialist_veto":
                    import re as _re
                    m = _re.match(
                        r"specialist veto\s*\(([^)]+)\):\s*(.*)",
                        msg, _re.IGNORECASE,
                    )
                    if m:
                        t["vetoed_by"] = m.group(1)
                        t["vetoed_by_display"] = humanize(m.group(1))
                        # Replace rejection_message with just the
                        # reason (no leading "specialist veto (X):")
                        t["rejection_message"] = m.group(2).strip()[:240]
    except Exception as exc:
        logger.warning(
            "api_cycle_data: rejection-badge enrichment failed for "
            "profile %d: %s — proceeding without badges",
            profile_id, exc,
        )

    return jsonify(data)


@views_bp.route("/api/sector-rotation")
@login_required
def api_sector_rotation():
    """Return current sector rotation data."""
    try:
        from market_data import get_sector_rotation
        rotation = get_sector_rotation()
        return jsonify(rotation)
    except Exception as exc:
        return jsonify({"error": str(exc)})


# ---------------------------------------------------------------------------
# Server-side paginated widgets for performance dashboard
# ---------------------------------------------------------------------------

@views_bp.route("/api/tuning-status")
@login_required
def api_tuning_status():
    """Paginated self-tuning readiness per profile."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 5, type=int)
    profile_id = request.args.get("profile_id", type=int)

    from self_tuning import describe_tuning_state
    import sqlite3 as _sq

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    items = []
    for p in profiles:
        try:
            ctx = build_user_context_from_profile(p["id"])
            state = describe_tuning_state(ctx)
        except Exception as exc:
            logger.warning(
                "api_tuning_status: tuning state build failed for profile %s: %s",
                p["id"], exc,
            )
            state = {"can_tune": False, "resolved": 0, "required": 20, "message": "Error"}
        last_run = None
        try:
            with closing(open_profile_db(ctx.db_path)) as c:
                row = c.execute(
                    "SELECT started_at FROM task_runs WHERE task_name LIKE '%Self-Tune%' "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            if row:
                last_run = row[0]
        except Exception as exc:
            logger.warning(
                "api_tuning_status: last_run lookup failed for profile %s: %s",
                p["id"], exc,
            )
        items.append({
            "profile_name": p["name"], "resolved": state["resolved"],
            "required": state["required"], "can_tune": state["can_tune"],
            "message": state["message"], "last_run": last_run,
        })

    total = len(items)
    start = (page - 1) * per_page
    return jsonify({"items": items[start:start + per_page], "total": total,
                     "page": page, "pages": -(-total // per_page)})


@views_bp.route("/api/cost-guard-status")
@login_required
def api_cost_guard_status():
    """Cost-guard daily status snapshot — today's spend, ceiling,
    headroom, trailing-7-day average."""
    try:
        from cost_guard import status
        return jsonify(status(current_user.effective_user_id))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@views_bp.route("/api/autonomy-timeline")
@login_required
def api_autonomy_timeline():
    """Chronological merged timeline of every autonomous change for a
    profile: tuning adjustments, strategy deprecations/restorations,
    post-mortem patterns extracted, signal weight nudges, capital
    rebalances. From tuning_history + deprecated_strategies +
    learned_patterns tables."""
    import os
    profile_id = request.args.get("profile_id", type=int)
    days = request.args.get("days", 30, type=int)
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    profile = get_trading_profile(profile_id)
    if not profile or profile.get("user_id") != current_user.effective_user_id:
        return jsonify({"error": "profile not found"}), 404

    events = []

    # Tuning history (master DB)
    try:
        from models import _get_conn
        from display_names import display_name
        with closing(_get_conn()) as conn:
            rows = conn.execute(
                "SELECT timestamp, change_type, parameter_name, old_value, "
                " new_value, reason, win_rate_at_change, outcome_after "
                "FROM tuning_history "
                "WHERE profile_id = ? "
                "  AND datetime(timestamp) >= datetime('now', '-' || ? || ' days') "
                "ORDER BY timestamp DESC",
                (profile_id, days),
            ).fetchall()
        for r in rows:
            events.append({
                "timestamp": r["timestamp"],
                "kind": "tuning",
                "label": display_name(r["parameter_name"] or r["change_type"]),
                "from": r["old_value"], "to": r["new_value"],
                "reason": r["reason"],
                "outcome": r["outcome_after"],
                "win_rate_at": r["win_rate_at_change"],
            })
    except Exception as exc:
        logger.debug("tuning_history fetch failed: %s", exc)

    # Per-profile DB events
    db_path = f"quantopsai_profile_{profile_id}.db"
    if os.path.exists(db_path):
        # Strategy deprecations
        try:
            from display_names import display_name
            with closing(open_profile_db(db_path)) as conn:
                rows = conn.execute(
                    "SELECT strategy_type, deprecated_at, restored_at, reason "
                    "FROM deprecated_strategies "
                    "WHERE datetime(deprecated_at) >= datetime('now', '-' || ? || ' days') "
                    "ORDER BY deprecated_at DESC",
                    (days,),
                ).fetchall()
            for r in rows:
                events.append({
                    "timestamp": r["deprecated_at"],
                    "kind": "strategy_deprecate",
                    "label": display_name(r["strategy_type"]),
                    "reason": r["reason"],
                })
                if r["restored_at"]:
                    events.append({
                        "timestamp": r["restored_at"],
                        "kind": "strategy_restore",
                        "label": display_name(r["strategy_type"]),
                        "reason": "Rolling Sharpe recovered",
                    })
        except Exception as exc:
            logger.debug("deprecated_strategies fetch failed: %s", exc)

        # Post-mortem patterns
        try:
            with closing(open_profile_db(db_path)) as conn:
                # Tolerate missing table (profile may pre-date the post_mortem
                # feature; analyze_recent_week creates it on first run).
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='learned_patterns'"
                ).fetchone()
                if tbl:
                    rows = conn.execute(
                        "SELECT created_at, pattern_text, period_wr, baseline_wr, "
                        " losing_trade_count "
                        "FROM learned_patterns "
                        "WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days') "
                        "ORDER BY created_at DESC",
                        (days,),
                    ).fetchall()
                    for r in rows:
                        events.append({
                            "timestamp": r["created_at"],
                            "kind": "post_mortem",
                            "label": "Losing-week pattern extracted",
                            "reason": r["pattern_text"],
                            "period_wr": r["period_wr"],
                            "baseline_wr": r["baseline_wr"],
                            "losing_trade_count": r["losing_trade_count"],
                        })
        except Exception as exc:
            logger.debug("learned_patterns fetch failed: %s", exc)

    # Sort all events by timestamp DESC (most recent first)
    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
    return jsonify({
        "profile_id": profile_id,
        "profile_name": profile.get("name"),
        "days": days,
        "events": events,
    })


@views_bp.route("/api/resolve-param")
@login_required
def api_resolve_param():
    """Show how a parameter resolves through the override chain
    right now. Args: profile_id, param_name, optional symbol.
    Returns the value at each layer + which one wins."""
    profile_id = request.args.get("profile_id", type=int)
    param_name = request.args.get("param_name", "")
    symbol = (request.args.get("symbol") or "").strip().upper() or None
    if not profile_id or not param_name:
        return jsonify({"error": "profile_id and param_name required"}), 400

    profile = get_trading_profile(profile_id)
    if not profile or profile.get("user_id") != current_user.effective_user_id:
        return jsonify({"error": "profile not found"}), 404

    # Walk each layer. Return value at each tier so the UI can show
    # the chain.
    chain = []
    final_value = None
    final_source = None

    global_value = profile.get(param_name)
    chain.append({"layer": "global", "value": global_value, "source": "profile"})

    # Layer 4 — TOD
    try:
        from tod_overrides import resolve_param as _tod_resolve, _current_tod
        cur_tod = _current_tod()
        if cur_tod:
            tod_val = _tod_resolve(profile, param_name, cur_tod, default=None)
            if tod_val is not None and tod_val != global_value:
                chain.append({"layer": "tod", "tod": cur_tod,
                                "value": tod_val, "source": "tod_overrides"})
                final_value, final_source = tod_val, f"tod:{cur_tod}"
    except Exception:
        cur_tod = None

    # Layer 3 — regime
    try:
        from regime_overrides import resolve_param as _regime_resolve, _current_regime
        cur_regime = _current_regime()
        if cur_regime:
            reg_val = _regime_resolve(profile, param_name, cur_regime, default=None)
            if reg_val is not None and reg_val != global_value:
                chain.append({"layer": "regime", "regime": cur_regime,
                                "value": reg_val, "source": "regime_overrides"})
                final_value, final_source = reg_val, f"regime:{cur_regime}"
    except Exception:
        cur_regime = None

    # Layer 7 — symbol (most specific; wins if set)
    if symbol:
        try:
            from symbol_overrides import resolve_param as _sym_resolve
            sym_val = _sym_resolve(profile, param_name, symbol, default=None)
            if sym_val is not None and sym_val != global_value:
                chain.append({"layer": "symbol", "symbol": symbol,
                                "value": sym_val, "source": "symbol_overrides"})
                final_value, final_source = sym_val, f"symbol:{symbol}"
        except Exception as exc:
            logger.warning(
                "api_resolve_param: per-symbol resolve failed (param=%s symbol=%s): %s",
                param_name, symbol, exc,
            )

    # If no override fired, the global value wins.
    if final_value is None:
        final_value = global_value
        final_source = "global"

    # capital_scale multiplier (Layer 9) applies to position-sizing
    # parameters at execution time. Show it as a separate annotation.
    cap_scale = float(profile.get("capital_scale") or 1.0)

    from display_names import display_name as _dn
    return jsonify({
        "profile_name": profile.get("name"),
        "param_name": param_name,
        "param_label": _dn(param_name),
        "symbol": symbol,
        "current_regime": cur_regime,
        "current_regime_label": _dn(cur_regime) if cur_regime else None,
        "current_tod": cur_tod,
        "current_tod_label": _dn(cur_tod) if cur_tod else None,
        "chain": chain,
        "final_value": final_value,
        "final_source": final_source,
        "final_source_label": _dn(final_source) if final_source not in (None, "global") else "Profile global",
        "capital_scale": cap_scale,
    })


@views_bp.route("/api/active-lessons")
@login_required
def api_active_lessons():
    """Active post-mortem patterns + tuner-detected failure patterns
    per profile — what the AI is being told to be cautious about right
    now (everything currently being injected into the AI prompt's
    LEARNED PATTERNS section)."""
    import os
    profile_id = request.args.get("profile_id", type=int)
    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    items = []
    for p in profiles:
        db_path = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db_path):
            continue
        try:
            from post_mortem import get_active_patterns
            patterns = get_active_patterns(db_path)
        except Exception:
            patterns = []
        try:
            from self_tuning import _analyze_failure_patterns
            tuner_patterns = _analyze_failure_patterns(db_path)
        except Exception:
            tuner_patterns = []
        items.append({
            "profile_id": p["id"],
            "profile_name": p["name"],
            "post_mortem_patterns": patterns,
            "tuner_patterns": tuner_patterns,
        })
    return jsonify({"items": items})


@views_bp.route("/api/autonomy-status")
@login_required
def api_autonomy_status():
    """Snapshot of all active per-profile autonomy state — signal weights,
    regime/TOD/symbol overrides, prompt-layout verbosity, capital_scale.

    Returns one entry per enabled profile. Empty dicts mean no overrides
    are active for that layer (i.e., everything at default)."""
    profile_id = request.args.get("profile_id", type=int)
    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    items = []
    for p in profiles:
        # Each layer has its own helper that handles parse-and-default-strip
        try:
            from signal_weights import get_all_weights as _sw
            weights = _sw(p)
        except Exception:
            weights = {}
        try:
            from regime_overrides import get_all_overrides as _ro
            regime = _ro(p)
        except Exception:
            regime = {}
        try:
            from tod_overrides import get_all_overrides as _to
            tod = _to(p)
        except Exception:
            tod = {}
        try:
            from symbol_overrides import get_all_overrides as _so
            symbols = _so(p)
        except Exception:
            symbols = {}
        try:
            from prompt_layout import all_verbosities as _pl
            layout_full = _pl(p)
            # Strip "normal" — only show non-default
            layout = {k: v for k, v in layout_full.items() if v != "normal"}
        except Exception:
            layout = {}
        # Pre-resolve display names server-side so the UI never sees a
        # raw snake_case key. The signal_weights helper has a richer
        # display_label; everything else flows through display_name.
        from display_names import display_name as _dn
        try:
            from signal_weights import display_label as _sig_label
        except Exception:
            _sig_label = _dn

        signal_weights_labeled = [
            {"key": k, "label": _sig_label(k), "weight": v}
            for k, v in weights.items()
        ]
        regime_overrides_labeled = [
            {"key": pname, "label": _dn(pname), "regime": r,
             "regime_label": _dn(r), "value": v}
            for pname, rmap in regime.items()
            for r, v in rmap.items()
        ]
        tod_overrides_labeled = [
            {"key": pname, "label": _dn(pname), "tod": t,
             "tod_label": _dn(t), "value": v}
            for pname, tmap in tod.items()
            for t, v in tmap.items()
        ]
        symbol_overrides_labeled = [
            {"key": pname, "label": _dn(pname), "symbol": s,
             "value": v}
            for pname, smap in symbols.items()
            for s, v in smap.items()
        ]
        prompt_layout_labeled = [
            {"key": k, "label": _dn(k), "verbosity": v}
            for k, v in layout.items()
        ]

        items.append({
            "profile_id": p["id"],
            "profile_name": p["name"],
            "capital_scale": float(p.get("capital_scale") or 1.0),
            "signal_weights": signal_weights_labeled,
            "regime_overrides": regime_overrides_labeled,
            "tod_overrides": tod_overrides_labeled,
            "symbol_overrides": symbol_overrides_labeled,
            "prompt_layout": prompt_layout_labeled,
        })
    return jsonify({"items": items})


@views_bp.route("/api/tuning-history")
@login_required
def api_tuning_history():
    """Paginated self-tuning history."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 5, type=int)
    profile_id = request.args.get("profile_id", type=int)

    from models import get_tuning_history

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    all_history = []
    for p in profiles:
        try:
            history = get_tuning_history(p["id"], limit=100)
            for h in history:
                h["profile_name"] = p["name"]
                pname = h.get("parameter_name", "")
                h["parameter_label"] = _format_param_name(pname)
                # Format old/new values — was leaking raw decimals like
                # '0.07 → 0.0805' to the tuning-history widget. Now the
                # API returns '7.0% → 8.05%' so the JS doesn't have to
                # know about percentage-vs-int parameter conventions.
                h["old_value_label"] = _format_param_value(pname, h.get("old_value"))
                h["new_value_label"] = _format_param_value(pname, h.get("new_value"))
                h["category"] = _categorize_tuning_adjustment(
                    h.get("adjustment_type"),
                )
            all_history.extend(history)
        except Exception as exc:
            logger.warning(
                "api_tuning_history: per-profile fetch failed for %s: %s",
                p["id"], exc,
            )
    all_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    # 7-day rollup by category (so the dashboard can show counts
    # without the user having to flip through history pages).
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.utcnow() - _td(days=7)).isoformat()
    summary = {"gate_tighten": 0, "refinement": 0,
                "loosen": 0, "neutral": 0}
    for h in all_history:
        if (h.get("timestamp") or "") < cutoff:
            continue
        summary[h.get("category", "neutral")] = summary.get(
            h.get("category", "neutral"), 0,
        ) + 1

    total = len(all_history)
    start = (page - 1) * per_page
    return jsonify({"items": all_history[start:start + per_page], "total": total,
                     "page": page, "pages": -(-total // per_page),
                     "summary_7d": summary})


@views_bp.route("/api/learned-patterns")
@login_required
def api_learned_patterns():
    """Paginated learned patterns."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 5, type=int)
    profile_id = request.args.get("profile_id", type=int)
    import os

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    patterns = []
    for p in profiles:
        db_path = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db_path):
            continue
        try:
            patterns.extend(_analyze_failure_patterns(db_path))
        except Exception as exc:
            logger.warning(
                "api_learned_patterns: analyze failed for %s: %s",
                db_path, exc,
            )

    # Deduplicate
    patterns = list(dict.fromkeys(patterns))
    total = len(patterns)
    start = (page - 1) * per_page
    return jsonify({"items": patterns[start:start + per_page], "total": total,
                     "page": page, "pages": -(-total // per_page)})


@views_bp.route("/api/sec-alerts")
@login_required
def api_sec_alerts():
    """Paginated SEC filing alerts."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 5, type=int)
    profile_id = request.args.get("profile_id", type=int)
    import os

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    if profile_id:
        profiles = [p for p in profiles if p["id"] == profile_id]

    alerts = []
    for p in profiles:
        db_path = f"quantopsai_profile_{p['id']}.db"
        if not os.path.exists(db_path):
            continue
        try:
            for a in get_active_alerts(db_path, min_severity="medium")[:20]:
                alerts.append({
                    "profile_name": p["name"],
                    "symbol": a.get("symbol", ""),
                    "form": a.get("form_type", ""),
                    "filed_date": a.get("filed_date", ""),
                    "severity": a.get("alert_severity", ""),
                    "signal": a.get("alert_signal", ""),
                    "summary": a.get("alert_summary", ""),
                })
        except Exception as exc:
            logger.warning(
                "api_sec_alerts: get_active_alerts failed for %s: %s",
                db_path, exc,
            )

    sev_rank = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (sev_rank.get(a["severity"], 3), a.get("filed_date", "")),
                reverse=False)

    total = len(alerts)
    start = (page - 1) * per_page
    return jsonify({"items": alerts[start:start + per_page], "total": total,
                     "page": page, "pages": -(-total // per_page)})


# ---------------------------------------------------------------------------
# Backtesting
# ---------------------------------------------------------------------------

@views_bp.route("/backtest/<market_type>")
@login_required
def run_backtest(market_type):
    """Run a backtest and display results."""
    valid_types = {"micro", "small", "midcap", "largecap", "crypto"}
    if market_type not in valid_types:
        flash(f"Invalid market type: {market_type}. Must be one of: {', '.join(sorted(valid_types))}", "error")
        return redirect(url_for("views.dashboard"))

    days = request.args.get("days", 180, type=int)
    days = max(30, min(days, 365))  # Clamp to reasonable range

    try:
        from backtester import backtest_strategy
        results = backtest_strategy(market_type, days=days)
    except Exception as exc:
        logger.error("Backtest failed for %s: %s", market_type, exc)
        flash(f"Backtest failed: {exc}", "error")
        return redirect(url_for("views.dashboard"))

    return render_template("backtest.html", results=results, market_type=market_type, days=days)


# ---------------------------------------------------------------------------
# Docs viewer (/docs)
#
# Renders Docs/*.md on demand. The HTML reflects the current source
# every time — no separate publish step. A small in-process cache
# keyed on file mtime avoids re-rendering on every request without
# letting stale content stick around when a file changes.
# ---------------------------------------------------------------------------

_DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")
_docs_render_cache = {}  # path -> (mtime, html)


def _list_docs():
    """Return [(filename, title)] sorted by filename. Filename order
    happens to be the doc-number order (01_, 02_, ...) which matches
    the recommended reading sequence."""
    if not os.path.isdir(_DOCS_DIR):
        return []
    out = []
    for fname in sorted(os.listdir(_DOCS_DIR)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(_DOCS_DIR, fname)
        # Title = first H1 in the file, falling back to filename.
        title = fname.replace(".md", "").replace("_", " ")
        try:
            with open(path, "r") as f:
                for line in f:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError as exc:
            # Title falls back to the filename-derived default; log so
            # a permission-or-encoding issue doesn't go unnoticed.
            logger.warning(
                "_list_docs: title-extract failed for %s: %s",
                fname, exc,
            )
        out.append((fname, title))
    return out


def _render_doc(filename):
    """Render one doc's markdown to HTML. Cached by mtime so a
    refresh after editing the source returns the new content
    without restart, but a re-request without changes is cheap."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    path = os.path.join(_DOCS_DIR, filename)
    if not os.path.isfile(path) or not filename.endswith(".md"):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    cached = _docs_render_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import markdown as _md
    except ImportError:
        return None
    with open(path, "r") as f:
        src = f.read()
    html = _md.markdown(
        src,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )
    _docs_render_cache[path] = (mtime, html)
    return html


@views_bp.route("/docs")
@login_required
def docs_index():
    """List of system docs. Visible to every authenticated user
    (viewers + admins) — the docs describe the system, not
    user-private data."""
    return render_template("docs_index.html", docs=_list_docs())


@views_bp.route("/docs/<filename>")
@login_required
def docs_view(filename):
    """Render one doc as HTML. Always reflects the current
    source on disk (mtime-cached, no restart needed after edits)."""
    html = _render_doc(filename)
    if html is None:
        abort(404)
    # Find the title (same logic as _list_docs)
    docs = _list_docs()
    title = next((t for f, t in docs if f == filename), filename)
    return render_template(
        "docs_view.html",
        filename=filename, title=title, body_html=html, docs=docs,
    )
