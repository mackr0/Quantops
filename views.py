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
)
from segments import SEGMENTS, get_segment
from crypto import decrypt

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
        logger.warning("Could not fetch account for segment %s: %s", ctx.segment, exc)
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
        logger.warning("Could not fetch positions for segment %s: %s", ctx.segment, exc)
        return []


def _get_trade_history_for_user(user_id, segment=None, limit=100):
    """Get trade history from the segment's journal DB."""
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
    segments_data = []
    for seg_name in ("microsmall", "midcap", "largecap"):
        seg_config = get_user_segment_config(current_user.id, seg_name)
        if not seg_config or not seg_config.get("enabled"):
            continue
        try:
            ctx = build_user_context(current_user.id, seg_name)
            account = _safe_account_info(ctx)
            positions = _safe_positions(ctx)
            trades = _get_trade_history_for_user(current_user.id, seg_name, limit=10)
            segments_data.append({
                "name": SEGMENTS[seg_name]["name"],
                "key": seg_name,
                "account": account,
                "positions": positions,
                "recent_trades": trades,
            })
        except Exception as exc:
            logger.warning("Dashboard error for segment %s: %s", seg_name, exc)
            segments_data.append({
                "name": SEGMENTS[seg_name]["name"],
                "key": seg_name,
                "account": None,
                "positions": [],
                "recent_trades": [],
                "error": str(exc),
            })

    # Get recent decisions
    decisions = get_decisions(current_user.id, limit=20)

    return render_template("dashboard.html",
                           segments_data=segments_data,
                           decisions=decisions)


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

    # Mask keys for display
    def mask(key):
        if not key:
            return ""
        if len(key) <= 8:
            return "****"
        return key[:4] + "*" * (len(key) - 8) + key[-4:]

    keys = {
        "alpaca_api_key": mask(alpaca_key),
        "alpaca_secret_key": mask(alpaca_secret),
        "anthropic_api_key": mask(anthropic_key),
        "resend_api_key": mask(resend_key),
        "notification_email": notification_email,
        "has_alpaca": bool(alpaca_key),
        "has_anthropic": bool(anthropic_key),
        "has_resend": bool(resend_key),
    }

    # Get segment configs
    seg_configs = {}
    for seg_name in ("microsmall", "midcap", "largecap"):
        cfg = get_user_segment_config(current_user.id, seg_name)
        if cfg:
            cfg = dict(cfg) if not isinstance(cfg, dict) else cfg
            # Add masked Alpaca key for display
            enc_key = cfg.get("alpaca_api_key_enc", "")
            if enc_key:
                try:
                    decrypted = decrypt(enc_key)
                    cfg["_alpaca_key_masked"] = mask(decrypted)
                except Exception:
                    cfg["_alpaca_key_masked"] = "****"
            seg_configs[seg_name] = cfg
        else:
            seg_configs[seg_name] = {}

    return render_template("settings.html",
                           keys=keys,
                           seg_configs=seg_configs,
                           segments=SEGMENTS)


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
        from crypto import encrypt
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


@views_bp.route("/trades")
@login_required
def trades():
    decisions = get_decisions(current_user.id, limit=200)
    # Also pull from the journal trades tables for each segment
    all_trades = []
    for seg_name in ("microsmall", "midcap", "largecap"):
        seg_trades = _get_trade_history_for_user(current_user.id, seg_name, limit=100)
        for t in seg_trades:
            t["segment"] = seg_name
        all_trades.extend(seg_trades)
    # Sort by timestamp descending
    all_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return render_template("trades.html", decisions=decisions, trades=all_trades[:200])


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
    """AI prediction accuracy dashboard."""
    try:
        from ai_tracker import get_ai_performance
        perf = get_ai_performance()
    except Exception:
        perf = {
            "total_predictions": 0, "resolved": 0, "pending": 0,
            "win_rate": 0.0, "avg_confidence_on_wins": 0.0,
            "avg_confidence_on_losses": 0.0, "avg_return_on_buys": 0.0,
            "avg_return_on_sells": 0.0, "accuracy_by_confidence": {},
            "best_prediction": None, "worst_prediction": None,
            "profit_factor": 0.0,
        }

    try:
        from journal import get_performance_summary
        trade_perf = get_performance_summary()
    except Exception:
        trade_perf = {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            "best_trade": 0.0, "worst_trade": 0.0,
        }

    return render_template("ai_performance.html", perf=perf, trade_perf=trade_perf)


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
