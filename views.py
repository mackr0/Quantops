"""Main views blueprint — dashboard, settings, trades, AI performance, admin."""

import json
import logging
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, abort,
)
from flask_login import login_required, current_user

from models import (
    build_user_context, get_user_segment_config, update_user_segment_config,
    get_user_by_id, get_user_by_email, get_active_users, get_decisions,
    update_user_credentials, get_api_usage,
    create_default_segment_configs,
    # Trading profiles
    create_trading_profile, get_trading_profile, get_user_profiles,
    get_active_profiles, update_trading_profile, delete_trading_profile,
    build_user_context_from_profile, MARKET_TYPE_NAMES,
    # Activity log
    get_activity_feed, get_activity_count,
)
from segments import SEGMENTS, get_segment
from crypto import decrypt, encrypt
from ai_providers import get_providers

logger = logging.getLogger(__name__)

views_bp = Blueprint("views", __name__, template_folder="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def admin_required(f):
    """Decorator that requires the current user to be an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _safe_account_info(ctx):
    """Try to fetch Alpaca account info, return dict or None on failure."""
    try:
        api = ctx.get_alpaca_api()
        account = api.get_account()
        return {
            "equity": float(account.equity),
            "buying_power": float(account.buying_power),
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "status": account.status,
        }
    except Exception as exc:
        logger.warning("Could not fetch account for %s: %s", ctx.display_name or ctx.segment, exc)
        return None


def _safe_positions(ctx):
    """Try to fetch Alpaca positions, return list or empty on failure."""
    try:
        api = ctx.get_alpaca_api()
        positions = api.list_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "current_price": float(p.current_price),
                "avg_entry_price": float(p.avg_entry_price),
            }
            for p in positions
        ]
    except Exception as exc:
        logger.warning("Could not fetch positions for %s: %s", ctx.display_name or ctx.segment, exc)
        return []


def _get_trade_history_for_profile(profile_id, limit=100):
    """Get trade history from the profile's journal DB."""
    try:
        import sqlite3
        db_path = f"quantopsai_profile_{profile_id}.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_trade_history_for_user(user_id, segment=None, limit=100):
    """Get trade history from the segment's journal DB (legacy)."""
    try:
        import sqlite3
        if segment:
            db_path = f"quantopsai_{segment}.db"
        else:
            db_path = "quantopsai.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _mask_key(key):
    """Mask an API key for display."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@views_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("views.dashboard"))
    return redirect(url_for("auth.login"))


@views_bp.route("/dashboard")
@login_required
def dashboard():
    profiles = get_active_profiles(user_id=current_user.id)
    profiles_data = []

    for prof in profiles:
        try:
            ctx = build_user_context_from_profile(prof["id"])
            account = _safe_account_info(ctx)
            positions = _safe_positions(ctx)
            trades = _get_trade_history_for_profile(prof["id"], limit=10)
            profiles_data.append({
                "id": prof["id"],
                "name": prof["name"],
                "market_type": prof["market_type"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "account": account,
                "positions": positions,
                "recent_trades": trades,
            })
        except Exception as exc:
            logger.warning("Dashboard error for profile #%d: %s", prof["id"], exc)
            profiles_data.append({
                "id": prof["id"],
                "name": prof["name"],
                "market_type": prof["market_type"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "account": None,
                "positions": [],
                "recent_trades": [],
                "error": str(exc),
            })

    # Get recent decisions
    decisions = get_decisions(current_user.id, limit=20)

    # Build per-profile schedule status
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    _now = _dt.now(ZoneInfo("America/New_York"))
    any_profile_active = False
    profile_schedules = []

    all_profiles = get_user_profiles(current_user.id)
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

            profile_schedules.append({
                "name": prof["name"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "active": active,
                "next_session": next_session,
                "schedule_type": ctx.schedule_type,
            })
        except Exception:
            pass

    return render_template("dashboard.html",
                           profiles_data=profiles_data,
                           decisions=decisions,
                           any_profile_active=any_profile_active,
                           profile_schedules=profile_schedules)


@views_bp.route("/settings")
@login_required
def settings():
    user = get_user_by_id(current_user.id)

    # Decrypt keys for display (masked)
    alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
    alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))
    anthropic_key = decrypt(user.get("anthropic_api_key_enc", ""))
    resend_key = decrypt(user.get("resend_api_key_enc", ""))
    notification_email = user.get("notification_email", "")

    keys = {
        "alpaca_api_key": _mask_key(alpaca_key),
        "alpaca_secret_key": _mask_key(alpaca_secret),
        "anthropic_api_key": _mask_key(anthropic_key),
        "resend_api_key": _mask_key(resend_key),
        "notification_email": notification_email,
        "has_alpaca": bool(alpaca_key),
        "has_anthropic": bool(anthropic_key),
        "has_resend": bool(resend_key),
    }

    # Get trading profiles
    profiles = get_user_profiles(current_user.id)

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
    excluded = get_excluded_symbols(current_user.id)
    excluded_str = ", ".join(excluded)

    ai_providers = get_providers()

    return render_template("settings.html",
                           keys=keys,
                           profiles=profiles,
                           market_types=MARKET_TYPE_NAMES,
                           segments=SEGMENTS,
                           excluded_symbols=excluded_str,
                           ai_providers=ai_providers,
                           ai_providers_json=json.dumps(ai_providers))


@views_bp.route("/settings/exclusions", methods=["POST"])
@login_required
def save_exclusions():
    raw = request.form.get("excluded_symbols", "").strip()
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    from models import update_excluded_symbols
    update_excluded_symbols(current_user.id, symbols)
    if symbols:
        flash(f"Restricted symbols updated: {', '.join(symbols)}", "success")
    else:
        flash("Restricted symbols cleared.", "success")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/keys", methods=["POST"])
@login_required
def save_keys():
    alpaca_key = request.form.get("alpaca_api_key", "").strip()
    alpaca_secret = request.form.get("alpaca_secret_key", "").strip()
    anthropic_key = request.form.get("anthropic_api_key", "").strip()
    notification_email = request.form.get("notification_email", "").strip()
    resend_key = request.form.get("resend_api_key", "").strip()

    # Only update fields that were actually provided (not masked placeholders)
    user = get_user_by_id(current_user.id)
    current_alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
    current_alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))
    current_anthropic_key = decrypt(user.get("anthropic_api_key_enc", ""))
    current_resend_key = decrypt(user.get("resend_api_key_enc", ""))

    # If the form value looks masked (contains ****), keep the existing key
    if "****" in alpaca_key:
        alpaca_key = current_alpaca_key
    if "****" in alpaca_secret:
        alpaca_secret = current_alpaca_secret
    if "****" in anthropic_key:
        anthropic_key = current_anthropic_key
    if "****" in resend_key:
        resend_key = current_resend_key

    update_user_credentials(
        current_user.id,
        alpaca_key=alpaca_key,
        alpaca_secret=alpaca_secret,
        anthropic_key=anthropic_key,
        notification_email=notification_email,
        resend_key=resend_key,
    )
    flash("API keys saved successfully.", "success")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/keys/test", methods=["POST"])
@login_required
def test_keys():
    """Test Alpaca connection with the user's saved credentials."""
    try:
        user = get_user_by_id(current_user.id)
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

@views_bp.route("/settings/profile/create", methods=["POST"])
@login_required
def create_profile():
    name = request.form.get("profile_name", "").strip()
    market_type = request.form.get("market_type", "").strip()

    if not name:
        flash("Profile name is required.", "error")
        return redirect(url_for("views.settings"))

    if market_type not in MARKET_TYPE_NAMES:
        flash("Invalid market type.", "error")
        return redirect(url_for("views.settings"))

    profile_id = create_trading_profile(current_user.id, name, market_type)
    flash(f'Profile "{name}" created successfully.', "success")
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


@views_bp.route("/settings/profile/<int:profile_id>", methods=["POST"])
@login_required
def save_profile(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.id:
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
        "enable_short_selling": 1 if form.get("enable_short_selling") else 0,
        "short_stop_loss_pct": float(form.get("short_stop_loss_pct", 0.08)),
        "short_take_profit_pct": float(form.get("short_take_profit_pct", 0.08)),
        "enable_self_tuning": 1 if form.get("enable_self_tuning") else 0,
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

    # Multi-model consensus
    config_updates["enable_consensus"] = 1 if form.get("enable_consensus") else 0
    consensus_model = form.get("consensus_model", "").strip()
    config_updates["consensus_model"] = consensus_model
    consensus_api_key = form.get("consensus_api_key", "").strip()
    if consensus_api_key:
        config_updates["consensus_api_key_enc"] = encrypt(consensus_api_key)

    # Custom watchlist: parse comma-separated text into a JSON list
    watchlist_raw = form.get("custom_watchlist", "").strip()
    if watchlist_raw:
        symbols = [s.strip().upper() for s in watchlist_raw.split(",") if s.strip()]
        config_updates["custom_watchlist"] = symbols
    else:
        config_updates["custom_watchlist"] = []

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


@views_bp.route("/settings/profile/<int:profile_id>/delete", methods=["POST"])
@login_required
def delete_profile_route(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.id:
        abort(404)

    name = profile["name"]
    delete_trading_profile(profile_id)
    flash(f'Profile "{name}" deleted.', "info")
    return redirect(url_for("views.settings"))


@views_bp.route("/settings/profile/<int:profile_id>/toggle", methods=["POST"])
@login_required
def toggle_profile(profile_id):
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.id:
        abort(404)

    new_state = 0 if profile["enabled"] else 1
    update_trading_profile(profile_id, enabled=new_state)
    state_str = "enabled" if new_state else "disabled"
    flash(f'Profile "{profile["name"]}" {state_str}.', "success")
    return redirect(url_for("views.settings") + f"#profile-{profile_id}")


# ---------------------------------------------------------------------------
# Legacy segment routes (kept for backward compatibility)
# ---------------------------------------------------------------------------

@views_bp.route("/settings/segment/<segment>", methods=["POST"])
@login_required
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

    update_user_segment_config(current_user.id, segment, **config_updates)
    flash(f"{SEGMENTS[segment]['name']} configuration saved.", "success")
    return redirect(url_for("views.settings") + f"#segment-{segment}")


@views_bp.route("/settings/segment/<segment>/reset", methods=["POST"])
@login_required
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
    update_user_segment_config(current_user.id, segment, **defaults)
    flash(f"{SEGMENTS[segment]['name']} configuration reset to defaults.", "info")
    return redirect(url_for("views.settings") + f"#segment-{segment}")


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

@views_bp.route("/trades")
@login_required
def trades():
    profiles = get_user_profiles(current_user.id)

    # Parse optional profile filter
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None

    # Filter decisions by profile name (segment column stores profile name)
    if selected_profile_int:
        # Find the profile name for filtering decisions
        prof_name = None
        for p in profiles:
            if p["id"] == selected_profile_int:
                prof_name = p["name"]
                break
        decisions = get_decisions(current_user.id, segment=prof_name, limit=200) if prof_name else []
    else:
        decisions = get_decisions(current_user.id, limit=200)

    # Pull trades from profile journal DBs
    all_trades = []
    if selected_profile_int:
        # Single profile mode
        prof = next((p for p in profiles if p["id"] == selected_profile_int), None)
        if prof:
            prof_trades = _get_trade_history_for_profile(prof["id"], limit=200)
            for t in prof_trades:
                t["profile_name"] = prof["name"]
                t["profile_id"] = prof["id"]
                t["segment"] = prof["name"]
            all_trades.extend(prof_trades)
    else:
        # All profiles mode (current behavior)
        for prof in profiles:
            prof_trades = _get_trade_history_for_profile(prof["id"], limit=100)
            for t in prof_trades:
                t["profile_name"] = prof["name"]
                t["profile_id"] = prof["id"]
                t["segment"] = prof["name"]
            all_trades.extend(prof_trades)

        # Also pull from legacy segment DBs for backward compatibility
        for seg_name in ("microsmall", "midcap", "largecap", "crypto"):
            seg_trades = _get_trade_history_for_user(current_user.id, seg_name, limit=100)
            for t in seg_trades:
                t["segment"] = SEGMENTS.get(seg_name, {}).get("name", seg_name)
                t["profile_name"] = t["segment"]
            all_trades.extend(seg_trades)

    # Sort by timestamp descending
    all_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return render_template("trades.html",
                           decisions=decisions,
                           trades=all_trades[:200],
                           profiles=profiles,
                           selected_profile=selected_profile_int)


@views_bp.route("/trades/<int:decision_id>")
@login_required
def trade_detail(decision_id):
    """JSON endpoint for expandable trade detail."""
    from models import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM decision_log WHERE id = ? AND user_id = ?",
        (decision_id, current_user.id),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404

    d = dict(row)
    # Parse JSON columns
    for col in ("strategy_votes", "strategy_reasons", "ai_risk_factors", "ai_price_targets"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return jsonify(d)


@views_bp.route("/ai-performance")
@login_required
def ai_performance():
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
    profiles = get_user_profiles(current_user.id)
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
        # All profiles mode (current behavior)
        for p in profiles:
            db_path = f"quantopsai_profile_{p['id']}.db"
            if os.path.exists(db_path):
                db_paths.add(db_path)

        # Also check legacy segment DBs
        for legacy in ["quantopsai_microsmall.db", "quantopsai_midcap.db",
                        "quantopsai_largecap.db", "quantopsai_crypto.db",
                        "quantopsai_smallcap.db"]:
            if os.path.exists(legacy):
                db_paths.add(legacy)

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
        except Exception:
            pass

        # Query raw resolved predictions for accurate aggregation
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT predicted_signal, actual_outcome, actual_return_pct, confidence "
                "FROM ai_predictions WHERE status = 'resolved'"
            ).fetchall()
            conn.close()
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
        except Exception:
            pass

        try:
            t = get_performance_summary(db_path=db_path)
            combined_trade["total_trades"] += t.get("total_trades", 0)
            combined_trade["winning_trades"] += t.get("winning_trades", 0)
            combined_trade["losing_trades"] += t.get("losing_trades", 0)
            combined_trade["total_pnl"] += t.get("total_pnl", 0)
            if t.get("best_trade", 0) > combined_trade["best_trade"]:
                combined_trade["best_trade"] = t["best_trade"]
            if t.get("worst_trade", 0) < combined_trade["worst_trade"]:
                combined_trade["worst_trade"] = t["worst_trade"]
        except Exception:
            pass

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
        tuning_history.extend(history)
    tuning_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    return render_template("ai_performance.html",
                           perf=combined_perf,
                           trade_perf=combined_trade,
                           tuning_history=tuning_history[:20],
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           selected_profile_name=selected_profile_name)


@views_bp.route("/admin")
@login_required
@admin_required
def admin():
    """Admin panel — user list, API usage."""
    from models import _get_conn
    conn = _get_conn()
    users = conn.execute(
        "SELECT id, email, display_name, is_admin, is_active, created_at, last_login_at "
        "FROM users ORDER BY id"
    ).fetchall()
    users = [dict(u) for u in users]

    # Get API usage for each user (today)
    from datetime import date
    today = date.today().isoformat()
    for u in users:
        u["api_calls_today"] = get_api_usage(u["id"], today)

    conn.close()
    return render_template("admin.html", users=users)


# ---------------------------------------------------------------------------
# Activity Feed API
# ---------------------------------------------------------------------------

@views_bp.route("/api/activity")
@login_required
def api_activity():
    """Return JSON array of activity log entries for the current user."""
    profile_id = request.args.get("profile_id", type=int)
    offset = request.args.get("offset", 0, type=int)
    limit = request.args.get("limit", 10, type=int)
    limit = min(limit, 100)  # cap at 100

    entries = get_activity_feed(current_user.id, profile_id=profile_id,
                                limit=limit, offset=offset)
    total = get_activity_count(current_user.id, profile_id=profile_id)
    return jsonify({"entries": entries, "total": total})


@views_bp.route("/universe/<int:profile_id>")
@login_required
def universe_popup(profile_id):
    """Render the universe popup window page."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.id:
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
    if not profile or profile["user_id"] != current_user.id:
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
def api_cache_universe_names(profile_id):
    """Trigger background caching of symbol names for a profile's universe."""
    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.id:
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
def toggle_scanning():
    """Admin-only: start/stop AI scanning for the current user."""
    if not current_user.is_admin:
        abort(403)
    from models import is_scanning_active, set_scanning_active
    currently_active = is_scanning_active(current_user.id)
    set_scanning_active(current_user.id, not currently_active)
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
        return jsonify(status)
    except FileNotFoundError:
        return jsonify({"error": "Scheduler not running yet", "scan_remaining": 0, "exit_remaining": 0, "ai_remaining": 0})
