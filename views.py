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
    """Fetch account info with 30s cache for dashboard."""
    import time
    cache_key = f"account_{getattr(ctx, 'db_path', id(ctx))}"
    cached = _dashboard_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _DASHBOARD_CACHE_TTL:
        return cached[1]
    try:
        from client import get_account_info
        result = get_account_info(ctx=ctx)
        _dashboard_cache[cache_key] = (time.time(), result)
        return result
    except Exception as exc:
        logger.warning("Could not fetch account for %s: %s", ctx.display_name or ctx.segment, exc)
        return None


def _safe_positions(ctx):
    """Fetch positions with 30s cache for dashboard."""
    import time
    cache_key = f"positions_{getattr(ctx, 'db_path', id(ctx))}"
    cached = _dashboard_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _DASHBOARD_CACHE_TTL:
        return cached[1]
    try:
        from client import get_positions
        result = get_positions(ctx=ctx)
        _dashboard_cache[cache_key] = (time.time(), result)
        return result
    except Exception as exc:
        logger.warning("Could not fetch positions for %s: %s", ctx.display_name or ctx.segment, exc)
        return []


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
        import sqlite3
        db_path = f"quantopsai_profile_{profile_id}.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades "
            "WHERE side='buy' OR side='sell_short' "
            "ORDER BY timestamp DESC"
        ).fetchall()
        conn.close()
        for r in rows:
            sym = r["symbol"]
            if sym not in trade_meta:  # keep most recent open-side trade
                trade_meta[sym] = dict(r)
    except Exception as exc:
        logger.warning("Could not enrich positions for profile %d: %s",
                       profile_id, exc)

    out = []
    for p in positions:
        meta = trade_meta.get(p["symbol"], {})
        side = "sell" if p.get("qty", 0) < 0 else "buy"
        out.append({
            "timestamp": meta.get("timestamp"),
            "symbol": p["symbol"],
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
            "pnl": None,
            "unrealized_pl": p["unrealized_pl"],
            "unrealized_plpc": p["unrealized_plpc"],
        })
    # Most recently opened positions first
    out.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return out


def _safe_pending_orders(ctx):
    """Fetch open/accepted Alpaca orders that have not filled yet.

    After-hours submissions queue as `accepted` until the next market
    session. Without surfacing them, the dashboard looks deceptively
    empty — the user can't tell a sitting order from a no-op cycle.
    """
    try:
        api = ctx.get_alpaca_api()
        orders = api.list_orders(status="open", limit=50)
        out = []
        for o in orders:
            try:
                qty = float(o.qty) if o.qty else 0.0
            except (TypeError, ValueError):
                qty = 0.0
            try:
                limit_price = float(o.limit_price) if o.limit_price else None
            except (TypeError, ValueError):
                limit_price = None
            out.append({
                "symbol": o.symbol,
                "side": o.side,
                "qty": qty,
                "order_type": o.order_type,
                "limit_price": limit_price,
                "status": o.status,
                "submitted_at": str(o.submitted_at) if getattr(o, "submitted_at", None) else None,
                "time_in_force": o.time_in_force,
            })
        return out
    except Exception as exc:
        logger.warning("Could not fetch pending orders for %s: %s",
                       ctx.display_name or ctx.segment, exc)
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
    profiles = get_active_profiles(user_id=current_user.effective_user_id)
    profiles_data = []

    def _load_profile(prof):
        """Load one profile's data. Called in parallel."""
        try:
            ctx = build_user_context_from_profile(prof["id"])
            account = _safe_account_info(ctx)
            positions = _enriched_positions(ctx, prof["id"])
            pending_orders = _safe_pending_orders(ctx)
            try:
                from ai_cost_ledger import spend_summary
                cost_today = spend_summary(ctx.db_path)["today"]["usd"]
            except Exception:
                cost_today = 0
            return {
                "id": prof["id"],
                "name": prof["name"],
                "market_type": prof["market_type"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "account": account,
                "positions": positions,
                "pending_orders": pending_orders,
                "is_virtual": getattr(ctx, "is_virtual", False),
                "cost_today": round(cost_today, 2),
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
    try:
        import sqlite3 as _sq_fail
        for prof in profiles:
            db = f"quantopsai_profile_{prof['id']}.db"
            try:
                conn = _sq_fail.connect(db)
                conn.row_factory = _sq_fail.Row
                fails = conn.execute(
                    "SELECT task_name, started_at FROM task_runs "
                    "WHERE status='failed' AND started_at >= datetime('now', '-1 hour') "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchall()
                conn.close()
                for f in fails:
                    scan_failures.append({
                        "profile_name": prof["name"],
                        "task": f["task_name"],
                        "time": f["started_at"],
                    })
            except Exception:
                pass
    except Exception:
        pass

    # Get recent decisions
    decisions = get_decisions(current_user.effective_user_id, limit=20)

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
                    import sqlite3 as _sq_sched
                    import time as _time_sched
                    _c = _sq_sched.connect(ctx.db_path)
                    row = _c.execute(
                        "SELECT started_at FROM task_runs "
                        "WHERE task_name LIKE '%Scan%' AND status IN ('completed','failed') "
                        "ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
                    _c.close()
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
                except Exception:
                    next_scan_text = ""

            profile_schedules.append({
                "profile_id": prof["id"],
                "name": prof["name"],
                "market_type_name": prof.get("market_type_name", prof["market_type"]),
                "active": active,
                "next_session": next_session,
                "next_scan_text": next_scan_text,
                "schedule_type": ctx.schedule_type,
            })
        except Exception:
            pass

    return render_template("dashboard.html",
                           profiles_data=profiles_data,
                           decisions=decisions,
                           any_profile_active=any_profile_active,
                           profile_schedules=profile_schedules,
                           scan_failures=scan_failures)


@views_bp.route("/settings")
@login_required
def settings():
    user = get_user_by_id(current_user.effective_user_id)

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
    }
    try:
        from cost_guard import status as _cost_status
        autonomy["cost_status"] = _cost_status(current_user.effective_user_id)
    except Exception:
        autonomy["cost_status"] = None

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
def update_autonomy():
    """Toggle the per-user opt-in autonomy flags + cost ceiling
    override."""
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

    conn = _get_conn()
    conn.execute(
        "UPDATE users SET auto_capital_allocation = ?, "
        " daily_cost_ceiling_usd = ? WHERE id = ?",
        (enabled, ceiling_value, current_user.effective_user_id),
    )
    conn.commit()
    conn.close()
    msgs = ["Auto capital allocation " + ("enabled" if enabled else "disabled") + "."]
    if ceiling_value is None:
        msgs.append("Cost ceiling: auto-computed (trailing-7d-avg × 1.5).")
    else:
        msgs.append(f"Cost ceiling locked to ${ceiling_value:.2f}/day.")
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
    anthropic_key = request.form.get("anthropic_api_key", "").strip()
    notification_email = request.form.get("notification_email", "").strip()
    resend_key = request.form.get("resend_api_key", "").strip()

    # Only update fields that were actually provided (not masked placeholders)
    user = get_user_by_id(current_user.effective_user_id)
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
        current_user.effective_user_id,
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
    """Get a connection to the main quantopsai.db (not per-profile)."""
    import sqlite3
    conn = sqlite3.connect("quantopsai.db")
    conn.row_factory = sqlite3.Row
    return conn


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
        "enable_short_selling": 1 if form.get("enable_short_selling") else 0,
        "short_stop_loss_pct": float(form.get("short_stop_loss_pct", 0.08)),
        "short_take_profit_pct": float(form.get("short_take_profit_pct", 0.08)),
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

    # Filter decisions by profile name (segment column stores profile name)
    if selected_profile_int:
        # Find the profile name for filtering decisions
        prof_name = None
        for p in profiles:
            if p["id"] == selected_profile_int:
                prof_name = p["name"]
                break
        decisions = get_decisions(current_user.effective_user_id, segment=prof_name, limit=200) if prof_name else []
    else:
        decisions = get_decisions(current_user.effective_user_id, limit=200)

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

    # Trades page is a clean order log — no live P&L enrichment.
    # Unrealized P&L belongs on the dashboard (open positions view).
    # SELL rows show realized P&L from the pnl column. BUY rows are blank.

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

    return render_template("trades.html",
                           decisions=decisions,
                           trades=page_trades,
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           page=page, total_pages=total_pages,
                           total_trades=total, sort_by=sort_by, sort_dir=sort_dir)


@views_bp.route("/trades/<int:decision_id>")
@login_required
def trade_detail(decision_id):
    """JSON endpoint for expandable trade detail."""
    from models import _get_conn
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM decision_log WHERE id = ? AND user_id = ?",
        (decision_id, current_user.effective_user_id),
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
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, symbol, pnl, price, qty "
                "FROM trades WHERE pnl IS NOT NULL "
                "ORDER BY timestamp ASC"
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
            conn.close()
        except Exception:
            pass

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
        # All profiles mode (current behavior)
        for p in profiles:
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
            pname = h.get("parameter_name", "")
            h["parameter_label"] = _format_param_name(pname)
            h["old_value_label"] = _format_param_value(pname, h.get("old_value"))
            h["new_value_label"] = _format_param_value(pname, h.get("new_value"))
        tuning_history.extend(history)
    tuning_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    # Aggregate slippage stats across relevant DBs
    from journal import get_slippage_stats
    combined_slippage = None
    for db_path in db_paths:
        try:
            s = get_slippage_stats(db_path=db_path)
            if s:
                if combined_slippage is None:
                    combined_slippage = {
                        "trades_with_fills": 0, "avg_slippage_pct": 0,
                        "total_slippage_cost": 0, "worst_slippage_pct": 0,
                        "worst_trade": None,
                    }
                combined_slippage["trades_with_fills"] += s["trades_with_fills"]
                combined_slippage["total_slippage_cost"] += s["total_slippage_cost"]
                if s["worst_slippage_pct"] > combined_slippage.get("worst_slippage_pct", 0):
                    combined_slippage["worst_slippage_pct"] = s["worst_slippage_pct"]
                    combined_slippage["worst_trade"] = s.get("worst_trade")
        except Exception:
            pass
    if combined_slippage and combined_slippage["trades_with_fills"] > 0:
        # Re-query for accurate average across all DBs
        total_slip_sum = 0
        total_slip_count = 0
        for db_path in db_paths:
            try:
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                r = c.execute(
                    "SELECT COUNT(*) AS cnt, SUM(slippage_pct) AS s "
                    "FROM trades WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL "
                    "AND decision_price > 0"
                ).fetchone()
                c.close()
                if r and r["cnt"]:
                    total_slip_count += r["cnt"]
                    total_slip_sum += r["s"] or 0
            except Exception:
                pass
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

    profiles = [p for p in get_user_profiles(current_user.effective_user_id) if p.get("enabled")]
    selected_profile = request.args.get("profile_id", "", type=str)
    selected_profile_int = int(selected_profile) if selected_profile else None
    selected_profile_name = None

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
        for p in profiles:
            if not p.get("enabled"):
                continue
            db_path = f"quantopsai_profile_{p['id']}.db"
            if os.path.exists(db_path):
                db_paths.add(db_path)

    # Calculate total initial capital across selected ENABLED profiles only
    total_initial_capital = 0
    capital_by_db = {}
    for p in profiles:
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
    scaling_theoretical = None
    try:
        from scaling_projection import (
            per_profile_breakdown, theoretical_scaling, _recommended_tier,
        )
        import sqlite3 as _sqlite3

        # Filter to profiles we're actually showing (matches db_paths).
        if selected_profile_int:
            target_profiles = [p for p in profiles if p["id"] == selected_profile_int]
        else:
            target_profiles = list(profiles)

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
                conn = _sqlite3.connect(db_path)
                conn.row_factory = _sqlite3.Row
                trade_rows = conn.execute(
                    "SELECT timestamp, symbol, side, qty, price, pnl, "
                    "decision_price, fill_price, slippage_pct "
                    "FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp ASC"
                ).fetchall()
                trades = [dict(r) for r in trade_rows]
                snap = conn.execute(
                    "SELECT equity FROM daily_snapshots "
                    "WHERE equity IS NOT NULL "
                    "ORDER BY date DESC, rowid DESC LIMIT 1"
                ).fetchone()
                if snap and snap["equity"] is not None:
                    latest_eq = float(snap["equity"])
                conn.close()
            except Exception:
                pass
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

        # Theoretical scale-up: aggregate baseline across the visible profiles.
        baseline_cap = float(total_initial_capital)
        baseline_slip = (sum(agg_slips) / len(agg_slips)) if agg_slips else 0.0
        baseline_mt = _recommended_tier(baseline_cap, "small")
        # Use limit-orders-now if it's the dominant execution mode in the
        # selection. For multi-profile aggregate, default to False.
        uses_limit_now = False
        if selected_profile_int and target_profiles:
            uses_limit_now = bool(target_profiles[0].get("use_limit_orders", 0))
        scaling_theoretical = theoretical_scaling(
            baseline_slip_pct=baseline_slip,
            baseline_capital=baseline_cap,
            baseline_market_type=baseline_mt,
            base_return_pct=metrics.get("net_return_pct", 0.0),
            n_trades_with_fills=len(agg_slips),
            use_limit_orders_now=uses_limit_now,
        )
    except Exception as exc:
        logger.warning("Scalability data build failed: %s", exc)

    # Current exposure across the selected profile(s). Virtual profiles
    # source positions/equity from the journal DB; real Alpaca-linked
    # profiles hit the Alpaca account. On All Profiles we aggregate
    # across every enabled profile so the user sees their full book.
    exposure = None
    try:
        if selected_profile_int:
            target_profiles = [get_trading_profile(selected_profile_int)]
            target_profiles = [p for p in target_profiles
                               if p and p["user_id"] == current_user.effective_user_id]
        else:
            target_profiles = profiles  # already filtered to enabled, owned

        long_val = 0.0
        short_val = 0.0
        equity_sum = 0.0
        n_positions = 0
        n_profiles_with_data = 0
        for profile in target_profiles:
            try:
                ctx = build_user_context_from_profile(profile["id"])
                positions = _safe_positions(ctx)
                account = _safe_account_info(ctx)
                if account:
                    equity_sum += account.get("equity", 0) or 0
                if positions:
                    long_val += sum(p["market_value"] for p in positions if p["qty"] > 0)
                    short_val += sum(abs(p["market_value"]) for p in positions if p["qty"] < 0)
                    n_positions += len(positions)
                if positions or account:
                    n_profiles_with_data += 1
            except Exception:
                continue

        if n_profiles_with_data and equity_sum > 0:
            exposure = {
                "net_pct": round((long_val - short_val) / equity_sum * 100, 1),
                "gross_pct": round((long_val + short_val) / equity_sum * 100, 1),
                "num_positions": n_positions,
            }
    except Exception:
        pass

    # AI prediction accuracy (for AI Intelligence tab)
    from ai_tracker import get_ai_performance
    from journal import get_performance_summary
    from models import get_tuning_history
    import sqlite3 as _sqlite3

    ai_perf = {
        "total_predictions": 0, "resolved": 0, "pending": 0,
        "win_rate": 0.0, "avg_confidence_on_wins": 0.0,
        "avg_confidence_on_losses": 0.0, "avg_return_on_buys": 0.0,
        "avg_return_on_sells": 0.0, "best_prediction": None,
        "worst_prediction": None, "profit_factor": 0.0,
    }
    all_wins = 0
    all_losses = 0
    conf_on_wins = []
    conf_on_losses = []
    all_return_buys = []
    all_return_sells = []

    for db_path in db_paths:
        try:
            p = get_ai_performance(db_path=db_path)
            ai_perf["total_predictions"] += p.get("total_predictions", 0)
            ai_perf["resolved"] += p.get("resolved", 0)
            ai_perf["pending"] += p.get("pending", 0)
            if p.get("best_prediction"):
                if ai_perf["best_prediction"] is None or p["best_prediction"].get("return_pct", 0) > ai_perf["best_prediction"].get("return_pct", 0):
                    ai_perf["best_prediction"] = p["best_prediction"]
            if p.get("worst_prediction"):
                if ai_perf["worst_prediction"] is None or p["worst_prediction"].get("return_pct", 0) < ai_perf["worst_prediction"].get("return_pct", 0):
                    ai_perf["worst_prediction"] = p["worst_prediction"]
        except Exception:
            pass

        try:
            conn = _sqlite3.connect(db_path)
            conn.row_factory = _sqlite3.Row
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
                elif outcome == "loss":
                    all_losses += 1
                    conf_on_losses.append(conf)
                if ret is not None:
                    if "BUY" in sig.upper():
                        all_return_buys.append(ret)
                    elif "SELL" in sig.upper():
                        all_return_sells.append(ret)
        except Exception:
            pass

    total_resolved = all_wins + all_losses
    if total_resolved > 0:
        ai_perf["win_rate"] = round(all_wins / total_resolved * 100, 1)
    if conf_on_wins:
        ai_perf["avg_confidence_on_wins"] = round(sum(conf_on_wins) / len(conf_on_wins), 1)
    if conf_on_losses:
        ai_perf["avg_confidence_on_losses"] = round(sum(conf_on_losses) / len(conf_on_losses), 1)
    if all_return_buys:
        ai_perf["avg_return_on_buys"] = round(sum(all_return_buys) / len(all_return_buys), 2)
    if all_return_sells:
        ai_perf["avg_return_on_sells"] = round(sum(all_return_sells) / len(all_return_sells), 2)

    # Profit factor from BUY/SELL predictions only (not HOLDs — a HOLD
    # "loss" means the price moved but no trade was made, so no money lost)
    trade_returns = []
    for db_path in db_paths:
        try:
            conn = _sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT actual_return_pct FROM ai_predictions "
                "WHERE status='resolved' AND actual_return_pct IS NOT NULL "
                "AND predicted_signal IN ('BUY', 'SELL')"
            ).fetchall()
            conn.close()
            trade_returns.extend(r[0] for r in rows if r[0] is not None)
        except Exception:
            pass
    total_gains = sum(r for r in trade_returns if r > 0)
    total_losses_abs = abs(sum(r for r in trade_returns if r < 0))
    if total_gains > 0 and total_losses_abs > 0:
        ai_perf["profit_factor"] = round(total_gains / total_losses_abs, 2)

    # Slippage stats
    slippage = {"avg_pct": 0, "total_cost": 0, "count": 0}
    for db_path in db_paths:
        try:
            from journal import get_slippage_stats
            s = get_slippage_stats(db_path=db_path)
            if s:
                slippage["count"] += s.get("count", 0)
                slippage["total_cost"] += s.get("total_cost", 0)
        except Exception:
            pass
    if slippage["count"] > 0 and slippage["total_cost"] != 0:
        slippage["avg_pct"] = slippage["total_cost"] / slippage["count"]

    # Tuning history — filter to selected profile, or show all
    tuning_history = []
    tuning_profiles = profiles
    if selected_profile_int:
        tuning_profiles = [p for p in profiles if p["id"] == selected_profile_int]
    for p in tuning_profiles:
        try:
            history = get_tuning_history(p["id"], limit=10)
            for h in history:
                h["profile_name"] = p["name"]
                pname = h.get("parameter_name", "")
                h["parameter_label"] = _format_param_name(pname)
                h["old_value_label"] = _format_param_value(pname, h.get("old_value"))
                h["new_value_label"] = _format_param_value(pname, h.get("new_value"))
            tuning_history.extend(history)
        except Exception:
            pass
    tuning_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    # Learned patterns from self-tuning
    learned_patterns = []
    for db_path in db_paths:
        try:
            from self_tuning import _analyze_failure_patterns
            patterns = _analyze_failure_patterns(db_path)
            learned_patterns.extend(patterns)
        except Exception:
            pass

    # Self-tuning status panel — shows whether the tuner is alive,
    # how much data it has, when it last ran, and why it may not
    # have made changes.
    tuning_status = []
    try:
        from self_tuning import describe_tuning_state
        import sqlite3 as _sqlite3
        status_profiles = [p for p in profiles
                           if not selected_profile_int or p["id"] == selected_profile_int]
        for p in status_profiles:
            try:
                ctx = build_user_context_from_profile(p["id"])
                state = describe_tuning_state(ctx)
            except Exception:
                state = {"can_tune": False, "resolved": 0, "required": 20,
                         "message": "Could not load tuning state."}
            # Last run timestamp
            last_run = None
            try:
                _c = _sqlite3.connect(ctx.db_path)
                row = _c.execute(
                    "SELECT started_at FROM task_runs "
                    "WHERE task_name LIKE '%Self-Tune%' "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                _c.close()
                if row:
                    last_run = row[0]
            except Exception:
                pass
            tuning_status.append({
                "profile_id": p["id"],
                "profile_name": p["name"],
                "resolved": state["resolved"],
                "required": state["required"],
                "can_tune": state["can_tune"],
                "message": state["message"],
                "last_run": last_run,
            })
    except Exception:
        pass

    # Meta-model info for dashboard (Phase 1)
    meta_info = {"loaded": False, "profiles": []}
    try:
        import meta_model
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
    except Exception:
        pass

    # Strategy validations (Phase 2)
    validations = []
    try:
        from rigorous_backtest import get_recent_validations
        raw = get_recent_validations(limit=30)
        for v in raw:
            # Parse stored JSON arrays
            try:
                passed = json.loads(v.get("passed_gates", "[]"))
                failed = json.loads(v.get("failed_gates", "[]"))
            except Exception:
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
    except Exception:
        pass

    # Multi-strategy capital allocation (Phase 6)
    allocation_info = {"per_profile": []}
    try:
        from multi_strategy import get_allocation_summary
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
    except Exception:
        pass

    # AI cost spend per profile (last 1d / 7d / 30d)
    ai_cost_info = {"per_profile": [], "totals": {"today": 0.0, "7d": 0.0, "30d": 0.0}}
    try:
        from ai_cost_ledger import spend_summary
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
    except Exception:
        pass

    # Crisis state (Phase 10)
    crisis_info = {"per_profile": [], "max_level": "normal"}
    _level_rank = {"normal": 0, "elevated": 1, "crisis": 2, "severe": 3}
    try:
        from crisis_state import get_current_level, history as _crisis_history
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
    except Exception:
        pass

    # Event stream from last 24h (Phase 9)
    event_info = {"per_profile": []}
    try:
        from event_bus import recent_events
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            try:
                events = recent_events(db, hours=24, limit=25)
            except Exception:
                events = []
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
    except Exception:
        pass

    # Specialist ensemble breakdown from last cycle (Phase 8)
    ensemble_info = {"per_profile": []}
    try:
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            cycle_path = f"cycle_data_{p['id']}.json"
            if not os.path.exists(cycle_path):
                continue
            try:
                with open(cycle_path) as f:
                    cycle = json.load(f)
            except Exception:
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
    except Exception:
        pass

    # Auto-generated strategies (Phase 7)
    auto_strategy_info = {"per_profile": []}
    try:
        from strategy_generator import list_strategies as _list_auto
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            rows = _list_auto(db)
            # Parse spec for human-readable summary
            enriched = []
            for row in rows[:30]:
                try:
                    spec = json.loads(row.get("spec_json") or "{}")
                except Exception:
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
    except Exception:
        pass

    # SEC filing alerts (Phase 4)
    sec_alerts = []
    try:
        from sec_filings import get_active_alerts
        import sqlite3 as _sq3a
        profiles_for_sec = [p for p in profiles
                            if (not selected_profile_int or p["id"] == selected_profile_int)]
        for p in profiles_for_sec:
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            alerts = get_active_alerts(db, min_severity="medium")
            for a in alerts[:20]:
                sec_alerts.append({
                    "profile_id": p["id"],
                    "profile_name": p["name"],
                    "symbol": a.get("symbol", ""),
                    "form": a.get("form_type", ""),
                    "filed_date": a.get("filed_date", ""),
                    "severity": a.get("alert_severity", ""),
                    "signal": a.get("alert_signal", ""),
                    "summary": a.get("alert_summary", ""),
                })
        # Sort by severity, then date desc
        sev_rank = {"high": 0, "medium": 1, "low": 2}
        sec_alerts.sort(key=lambda a: (sev_rank.get(a["severity"], 3),
                                        -(len(a["filed_date"]) and int(a["filed_date"].replace("-", "")) or 0)))
    except Exception:
        pass

    # Alpha decay monitoring (Phase 3) — per-profile rolling metrics and
    # deprecated strategy list. Aggregate across selected profiles.
    decay_info = {"per_profile": [], "any_deprecated": False}
    try:
        from alpha_decay import (list_deprecated, compute_rolling_metrics,
                                  compute_lifetime_metrics)
        profiles_for_decay = [p for p in profiles
                              if (not selected_profile_int or p["id"] == selected_profile_int)]
        import sqlite3 as _sq3
        for p in profiles_for_decay:
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            # Distinct strategy types this profile has recorded predictions for
            strat_types = []
            try:
                c = _sq3.connect(db)
                rows = c.execute(
                    "SELECT DISTINCT strategy_type FROM ai_predictions "
                    "WHERE strategy_type IS NOT NULL AND strategy_type != '' "
                    "AND status = 'resolved'"
                ).fetchall()
                strat_types = [r[0] for r in rows]
                c.close()
            except Exception:
                pass

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
    except Exception:
        pass

    return render_template("performance.html",
                           m=metrics,
                           profiles=profiles,
                           selected_profile=selected_profile_int,
                           selected_profile_name=selected_profile_name,
                           exposure=exposure,
                           ai_perf=ai_perf,
                           slippage=slippage,
                           scaling_real=scaling_real,
                           scaling_theoretical=scaling_theoretical,
                           tuning_history=[],
                           tuning_status=[],
                           learned_patterns=[],
                           meta_info=meta_info,
                           validations=validations,
                           decay_info=decay_info,
                           sec_alerts=[],
                           allocation_info=allocation_info,
                           auto_strategy_info=auto_strategy_info,
                           ensemble_info=ensemble_info,
                           event_info=event_info,
                           crisis_info=crisis_info,
                           ai_cost_info=ai_cost_info)


# ---------------------------------------------------------------------------
# AI Intelligence — 4 sub-pages
# ---------------------------------------------------------------------------

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
    }
    all_wins = 0
    all_losses = 0
    conf_on_wins = []
    conf_on_losses = []
    all_return_buys = []
    all_return_sells = []

    for db_path in db_paths:
        try:
            p = get_ai_performance(db_path=db_path)
            ai_perf["total_predictions"] += p.get("total_predictions", 0)
            ai_perf["resolved"] += p.get("resolved", 0)
            ai_perf["pending"] += p.get("pending", 0)
            if p.get("best_prediction"):
                if ai_perf["best_prediction"] is None or p["best_prediction"].get("return_pct", 0) > ai_perf["best_prediction"].get("return_pct", 0):
                    ai_perf["best_prediction"] = p["best_prediction"]
            if p.get("worst_prediction"):
                if ai_perf["worst_prediction"] is None or p["worst_prediction"].get("return_pct", 0) < ai_perf["worst_prediction"].get("return_pct", 0):
                    ai_perf["worst_prediction"] = p["worst_prediction"]
        except Exception:
            pass
        try:
            conn = _sqlite3.connect(db_path)
            conn.row_factory = _sqlite3.Row
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
                elif outcome == "loss":
                    all_losses += 1
                    conf_on_losses.append(conf)
                if ret is not None:
                    if "BUY" in sig.upper():
                        all_return_buys.append(ret)
                    elif "SELL" in sig.upper():
                        all_return_sells.append(ret)
        except Exception:
            pass

    total_resolved = all_wins + all_losses
    if total_resolved > 0:
        ai_perf["win_rate"] = round(all_wins / total_resolved * 100, 1)
    if conf_on_wins:
        ai_perf["avg_confidence_on_wins"] = round(sum(conf_on_wins) / len(conf_on_wins), 1)
    if conf_on_losses:
        ai_perf["avg_confidence_on_losses"] = round(sum(conf_on_losses) / len(conf_on_losses), 1)
    if all_return_buys:
        ai_perf["avg_return_on_buys"] = round(sum(all_return_buys) / len(all_return_buys), 2)
    if all_return_sells:
        ai_perf["avg_return_on_sells"] = round(sum(all_return_sells) / len(all_return_sells), 2)

    trade_returns = []
    for db_path in db_paths:
        try:
            conn = _sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT actual_return_pct FROM ai_predictions "
                "WHERE status='resolved' AND actual_return_pct IS NOT NULL "
                "AND predicted_signal IN ('BUY', 'SELL')"
            ).fetchall()
            conn.close()
            trade_returns.extend(r[0] for r in rows if r[0] is not None)
        except Exception:
            pass
    total_gains = sum(r for r in trade_returns if r > 0)
    total_losses_abs = abs(sum(r for r in trade_returns if r < 0))
    if total_gains > 0 and total_losses_abs > 0:
        ai_perf["profit_factor"] = round(total_gains / total_losses_abs, 2)

    slippage = {"avg_pct": 0, "total_cost": 0, "count": 0}
    for db_path in db_paths:
        try:
            from journal import get_slippage_stats
            s = get_slippage_stats(db_path=db_path)
            if s:
                slippage["count"] += s.get("count", 0)
                slippage["total_cost"] += s.get("total_cost", 0)
        except Exception:
            pass
    if slippage["count"] > 0 and slippage["total_cost"] != 0:
        slippage["avg_pct"] = slippage["total_cost"] / slippage["count"]

    meta_info = {"loaded": False, "profiles": []}
    try:
        import meta_model
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
    except Exception:
        pass

    validations = []
    try:
        from rigorous_backtest import get_recent_validations
        raw = get_recent_validations(limit=30)
        for v in raw:
            try:
                passed = json.loads(v.get("passed_gates", "[]"))
                failed = json.loads(v.get("failed_gates", "[]"))
            except Exception:
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
    except Exception:
        pass

    allocation_info = {"per_profile": []}
    try:
        from multi_strategy import get_allocation_summary
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
    except Exception:
        pass

    ai_cost_info = {"per_profile": [], "totals": {"today": 0.0, "7d": 0.0, "30d": 0.0}}
    try:
        from ai_cost_ledger import spend_summary
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
    except Exception:
        pass

    crisis_info = {"per_profile": [], "max_level": "normal"}
    _level_rank = {"normal": 0, "elevated": 1, "crisis": 2, "severe": 3}
    try:
        from crisis_state import get_current_level, history as _crisis_history
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
    except Exception:
        pass

    event_info = {"per_profile": []}
    try:
        from event_bus import recent_events
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            try:
                events = recent_events(db, hours=24, limit=25)
            except Exception:
                events = []
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
    except Exception:
        pass

    ensemble_info = {"per_profile": []}
    try:
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            cycle_path = f"cycle_data_{p['id']}.json"
            if not os.path.exists(cycle_path):
                continue
            try:
                with open(cycle_path) as f:
                    cycle = json.load(f)
            except Exception:
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
    except Exception:
        pass

    auto_strategy_info = {"per_profile": []}
    try:
        from strategy_generator import list_strategies as _list_auto
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            rows = _list_auto(db)
            enriched = []
            for row in rows[:30]:
                try:
                    spec = json.loads(row.get("spec_json") or "{}")
                except Exception:
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
    except Exception:
        pass

    decay_info = {"per_profile": [], "any_deprecated": False}
    try:
        from alpha_decay import (list_deprecated, compute_rolling_metrics,
                                  compute_lifetime_metrics)
        import sqlite3 as _sq3
        for p in profiles:
            if selected_profile_int and p["id"] != selected_profile_int:
                continue
            db = f"quantopsai_profile_{p['id']}.db"
            if not os.path.exists(db):
                continue
            strat_types = []
            try:
                c = _sq3.connect(db)
                rows = c.execute(
                    "SELECT DISTINCT strategy_type FROM ai_predictions "
                    "WHERE strategy_type IS NOT NULL AND strategy_type != '' "
                    "AND status = 'resolved'"
                ).fetchall()
                strat_types = [r[0] for r in rows]
                c.close()
            except Exception:
                pass
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
    except Exception:
        pass

    # === END COPIED BLOCK ===

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

    return render_template("ai.html",
                           ai_perf=ai_perf, slippage=slippage, meta_info=meta_info,
                           validations=validations, decay_info=decay_info,
                           allocation_info=allocation_info,
                           auto_strategy_info=auto_strategy_info,
                           crisis_info=crisis_info, event_info=event_info,
                           ensemble_info=ensemble_info,
                           ai_cost_info=ai_cost_info,
                           ai_win_rate_chart_svg=ai_win_rate_chart_svg,
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
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        actual_trades = conn.execute(
            "SELECT * FROM trades WHERE pnl IS NOT NULL AND timestamp >= ? "
            "ORDER BY timestamp DESC",
            (thirty_days_ago,),
        ).fetchall()
        actual_trades = [dict(r) for r in actual_trades]

        # Slippage stats for this profile
        slippage_row = conn.execute("""
            SELECT
                COUNT(*) AS trades_with_fills,
                AVG(slippage_pct) AS avg_slippage_pct,
                SUM(ABS(fill_price - decision_price) * qty) AS total_slippage_cost
            FROM trades
            WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
              AND decision_price > 0 AND timestamp >= ?
        """, (thirty_days_ago,)).fetchone()

        conn.close()
    except Exception as exc:
        logger.warning("Failed to query actual trades for profile %d: %s", profile_id, exc)
        return jsonify({"error": "Failed to query trade data"}), 500

    if len(actual_trades) < 5:
        return jsonify({
            "error": "insufficient_data",
            "message": f"Need at least 5 closed trades in the last 30 days (found {len(actual_trades)})",
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
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        snap = conn2.execute(
            "SELECT equity FROM daily_snapshots "
            "ORDER BY date DESC, rowid DESC LIMIT 1"
        ).fetchone()
        conn2.close()
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
    """Return slippage statistics for a profile."""
    import os

    profile = get_trading_profile(profile_id)
    if not profile or profile["user_id"] != current_user.effective_user_id:
        return jsonify({"error": "Profile not found"}), 404

    db_path = f"quantopsai_profile_{profile_id}.db"
    if not os.path.exists(db_path):
        return jsonify({"error": "No trade data"}), 404

    from journal import get_slippage_stats
    stats = get_slippage_stats(db_path=db_path)
    if stats is None:
        return jsonify({"available": False})

    return jsonify({"available": True, **stats})


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

    # Get actual API usage from per-profile cost ledgers
    import glob
    from ai_cost_ledger import spend_summary
    total_calls = 0
    total_cost = 0
    for f in glob.glob("quantopsai_profile_*.db"):
        s = spend_summary(f)
        total_calls += s["today"]["calls"]
        total_cost += s["today"]["usd"]
    for u in users:
        u["api_calls_today"] = total_calls
        u["api_cost_today"] = round(total_cost, 2)

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

    entries = get_activity_feed(current_user.effective_user_id, profile_id=profile_id,
                                limit=limit, offset=offset)
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
        import sqlite3 as _sq_scan
        import time as _t_scan
        db = f"quantopsai_profile_{profile_id}.db"
        conn = _sq_scan.connect(db)
        row = conn.execute(
            "SELECT started_at FROM task_runs "
            "WHERE task_name LIKE '%Scan%' AND status IN ('completed','failed') "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            from datetime import datetime as _dt_scan, timezone
            last = _dt_scan.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now = _dt_scan.now(timezone.utc)
            elapsed = (now - last).total_seconds()
            status["next_scan_sec"] = max(0, int(900 - elapsed))
    except Exception:
        pass

    return jsonify(status if status else {"step": None})


@views_bp.route("/api/portfolio/<int:profile_id>")
@login_required
def api_portfolio(profile_id):
    """Return live portfolio data for a profile (positions, account info)."""
    try:
        profile = get_trading_profile(profile_id)
        if not profile or profile["user_id"] != current_user.effective_user_id:
            return jsonify({"error": "not found"}), 404

        ctx = build_user_context_from_profile(profile_id)
        account = _safe_account_info(ctx)
        positions = _enriched_positions(ctx, profile_id)
        pending_orders = _safe_pending_orders(ctx)

        return jsonify({
            "account": account,
            "positions": positions,
            "pending_orders": pending_orders,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@views_bp.route("/api/positions-html/<int:profile_id>")
@login_required
def api_positions_html(profile_id):
    """Server-rendered Open Positions table HTML. Used by the dashboard
    auto-refresh so the JS doesn't have to duplicate the expandable
    trade-row markup."""
    from flask import render_template_string
    try:
        profile = get_trading_profile(profile_id)
        if not profile or profile["user_id"] != current_user.effective_user_id:
            return "not found", 404
        ctx = build_user_context_from_profile(profile_id)
        positions = _enriched_positions(ctx, profile_id)
        return render_template_string(
            '{% import "_trades_table.html" as trades_tpl %}'
            '{{ trades_tpl.render_trades(positions, show_profile=False, '
            'empty_message="No open positions in this profile.") }}',
            positions=positions,
        )
    except Exception as exc:
        return f"<p class='muted'>Failed to refresh: {exc}</p>", 500


@views_bp.route("/api/cycle-data/<int:profile_id>")
@login_required
def api_cycle_data(profile_id):
    """Return the last AI cycle data for a profile (decisions, shortlist, reasoning)."""
    try:
        with open(f"cycle_data_{profile_id}.json") as f:
            data = json.load(f)
        return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "No cycle data yet", "shortlist": [], "trades_selected": []})


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
        except Exception:
            state = {"can_tune": False, "resolved": 0, "required": 20, "message": "Error"}
        last_run = None
        try:
            c = _sq.connect(ctx.db_path)
            row = c.execute(
                "SELECT started_at FROM task_runs WHERE task_name LIKE '%Self-Tune%' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            c.close()
            if row:
                last_run = row[0]
        except Exception:
            pass
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
        conn = _get_conn()
        rows = conn.execute(
            "SELECT timestamp, change_type, parameter_name, old_value, "
            " new_value, reason, win_rate_at_change, outcome_after "
            "FROM tuning_history "
            "WHERE profile_id = ? "
            "  AND datetime(timestamp) >= datetime('now', '-' || ? || ' days') "
            "ORDER BY timestamp DESC",
            (profile_id, days),
        ).fetchall()
        conn.close()
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
            import sqlite3
            from display_names import display_name
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT strategy_type, deprecated_at, restored_at, reason "
                "FROM deprecated_strategies "
                "WHERE datetime(deprecated_at) >= datetime('now', '-' || ? || ' days') "
                "ORDER BY deprecated_at DESC",
                (days,),
            ).fetchall()
            conn.close()
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
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
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
            conn.close()
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
        except Exception:
            pass

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
            all_history.extend(history)
        except Exception:
            pass
    all_history.sort(key=lambda h: h.get("timestamp", ""), reverse=True)

    total = len(all_history)
    start = (page - 1) * per_page
    return jsonify({"items": all_history[start:start + per_page], "total": total,
                     "page": page, "pages": -(-total // per_page)})


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
            from self_tuning import _analyze_failure_patterns
            patterns.extend(_analyze_failure_patterns(db_path))
        except Exception:
            pass

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

    from sec_filings import get_active_alerts

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
        except Exception:
            pass

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
