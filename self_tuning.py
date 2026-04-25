"""Self-tuning feedback loop — feeds past performance into AI prompts.

Now includes tuning memory: every adjustment is logged, reviewed after 3 days,
and the outcomes are fed back into future decisions so the system learns from
its own learning.
"""

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy type → profile toggle column mapping (for auto-disable)
# ---------------------------------------------------------------------------
_STRATEGY_TYPE_TO_TOGGLE = {
    "momentum_breakout": "strategy_momentum_breakout",
    "volume_spike": "strategy_volume_spike",
    "mean_reversion": "strategy_mean_reversion",
    "gap_and_go": "strategy_gap_and_go",
}

# ---------------------------------------------------------------------------
# In-memory cache (30-minute TTL, keyed by profile db_path)
# ---------------------------------------------------------------------------
_cache: Dict[str, Any] = {}
_CACHE_TTL = 30 * 60  # 30 minutes


def _is_cached(key: str) -> bool:
    ts_key = f"{key}_ts"
    return (
        _cache.get(key) is not None
        and (time.time() - _cache.get(ts_key, 0)) < _CACHE_TTL
    )


def _set_cache(key: str, value: Any) -> None:
    _cache[key] = value
    _cache[f"{key}_ts"] = time.time()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn(db_path=None):
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_current_win_rate(conn):
    """Return (win_rate_pct, total_resolved) from ai_predictions."""
    resolved = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
    ).fetchone()[0]
    if resolved == 0:
        return 0.0, 0
    wins = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome='win'"
    ).fetchone()[0]
    return (wins / resolved * 100), resolved


def _days_ago_label(timestamp_str):
    """Return a human-readable 'X days ago' label from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        delta = datetime.utcnow() - ts
        days = delta.days
        if days == 0:
            return "today"
        elif days == 1:
            return "1 day ago"
        else:
            return f"{days} days ago"
    except Exception:
        return "recently"


# ---------------------------------------------------------------------------
# Tuning history helpers (delegate to models.py)
# ---------------------------------------------------------------------------

def _get_tuning_history(profile_id, limit=20):
    """Get tuning history from the central DB."""
    try:
        from models import get_tuning_history
        return get_tuning_history(profile_id, limit=limit)
    except Exception:
        return []


def _get_recent_adjustment(profile_id, parameter_name, days=3):
    """Check if a specific parameter was adjusted within the last N days.

    Returns the most recent matching adjustment dict, or None.
    """
    history = _get_tuning_history(profile_id, limit=50)
    cutoff = datetime.utcnow() - timedelta(days=days)
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= cutoff and entry["parameter_name"] == parameter_name:
                return entry
        except Exception:
            continue
    return None


def _was_adjustment_effective(profile_id, parameter_name):
    """Check the most recent reviewed adjustment for this parameter.

    Returns: 'improved', 'worsened', 'unchanged', or None if no reviewed data.
    """
    history = _get_tuning_history(profile_id, limit=50)
    for entry in history:
        if (entry["parameter_name"] == parameter_name
                and entry["outcome_after"] != "pending"):
            return entry["outcome_after"]
    return None


# ---------------------------------------------------------------------------
# Cross-Profile Learning (Feature 4)
# ---------------------------------------------------------------------------

def _build_cross_profile_insights(user_id, current_profile_id, current_db_path):
    """Compare performance across all of a user's profiles and build insight text.

    Only produces output if there are 2+ profiles with 20+ resolved predictions each,
    and another profile significantly outperforms the current one (15%+ higher win rate).

    Returns insight string or empty string.
    """
    try:
        from models import get_user_profiles, get_trading_profile
    except ImportError:
        return ""

    try:
        profiles = get_user_profiles(user_id)
    except Exception:
        return ""

    if len(profiles) < 2:
        return ""

    # Gather stats for each profile
    profile_stats = []
    for prof in profiles:
        pid = prof["id"]
        db = f"quantopsai_profile_{pid}.db"

        try:
            conn = _get_conn(db)
        except Exception:
            continue

        try:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
            ).fetchone()
            if not table_check:
                conn.close()
                continue

            resolved = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
            ).fetchone()[0]

            if resolved < 20:
                conn.close()
                continue

            wins = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win'"
            ).fetchone()[0]
            win_rate = (wins / resolved * 100) if resolved > 0 else 0

            avg_return = conn.execute(
                "SELECT AVG(actual_return_pct) FROM ai_predictions WHERE status='resolved'"
            ).fetchone()[0] or 0

            # BUY-specific stats
            buy_total = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND predicted_signal='BUY'"
            ).fetchone()[0]
            buy_wins = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='BUY'"
            ).fetchone()[0]
            buy_avg_ret = conn.execute(
                "SELECT AVG(actual_return_pct) FROM ai_predictions "
                "WHERE status='resolved' AND predicted_signal='BUY'"
            ).fetchone()[0] or 0
            buy_wr = (buy_wins / buy_total * 100) if buy_total > 0 else 0

            conn.close()

            profile_stats.append({
                "id": pid,
                "name": prof["name"],
                "resolved": resolved,
                "win_rate": win_rate,
                "avg_return": avg_return,
                "buy_wr": buy_wr,
                "buy_avg_ret": buy_avg_ret,
                "ai_confidence_threshold": prof.get("ai_confidence_threshold", 25),
                "min_price": prof.get("min_price", 1.0),
                "max_price": prof.get("max_price", 20.0),
            })
        except Exception as exc:
            logger.warning("Cross-profile: error reading profile #%d: %s", pid, exc)
            try:
                conn.close()
            except Exception:
                pass
            continue

    # Need 2+ profiles with enough data
    if len(profile_stats) < 2:
        return ""

    # Find current profile stats
    current_stats = None
    for ps in profile_stats:
        if ps["id"] == current_profile_id:
            current_stats = ps
            break

    if current_stats is None:
        return ""

    # Find profiles that significantly outperform the current one
    lines = []
    for other in profile_stats:
        if other["id"] == current_profile_id:
            continue

        wr_diff = other["win_rate"] - current_stats["win_rate"]
        if wr_diff >= 15:
            lines.append("CROSS-PROFILE INSIGHTS:")
            lines.append(
                f'Your "{other["name"]}" profile wins {other["buy_wr"]:.0f}% '
                f'on BUY signals with avg return {other["buy_avg_ret"]:+.1f}%.'
            )
            lines.append(
                f'This profile ("{current_stats["name"]}") only wins '
                f'{current_stats["buy_wr"]:.0f}% with avg return '
                f'{current_stats["buy_avg_ret"]:+.1f}%.'
            )
            lines.append("")
            lines.append(f'Key differences in "{other["name"]}":')
            lines.append(
                f'  - AI confidence threshold: {other["ai_confidence_threshold"]} '
                f'(this profile: {current_stats["ai_confidence_threshold"]})'
            )
            lines.append(
                f'  - Price range: ${other["min_price"]:.0f}-${other["max_price"]:.0f} '
                f'(this profile: ${current_stats["min_price"]:.0f}-${current_stats["max_price"]:.0f})'
            )
            lines.append("")
            lines.append(
                f'The AI appears more accurate on "{other["name"]}" stocks '
                f'in the current market.'
            )
            lines.append("Consider being more selective on this profile.")
            break  # Only show the best-performing comparison

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-stock reputation (Feature 2: Auto-Blacklist)
# ---------------------------------------------------------------------------

def get_symbol_reputation(db_path, min_predictions=3):
    """Get win rate per symbol from ai_predictions.

    Returns dict: {symbol: {"wins": N, "losses": N, "win_rate": float, "avg_return": float}}
    Only includes symbols with min_predictions resolved.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return {}

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return {}

        rows = conn.execute(
            "SELECT symbol, COUNT(*) as total, "
            "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins, "
            "AVG(actual_return_pct) as avg_return "
            "FROM ai_predictions WHERE status='resolved' "
            "GROUP BY symbol HAVING COUNT(*) >= ?",
            (min_predictions,),
        ).fetchall()
        conn.close()

        result = {}
        for r in rows:
            total = r["total"]
            wins = r["wins"]
            losses = total - wins
            win_rate = (wins / total * 100) if total > 0 else 0
            result[r["symbol"]] = {
                "wins": wins,
                "losses": losses,
                "total": total,
                "win_rate": win_rate,
                "avg_return": r["avg_return"] or 0,
            }
        return result

    except Exception as exc:
        logger.warning("Failed to get symbol reputation: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return {}


# ---------------------------------------------------------------------------
# build_performance_context
# ---------------------------------------------------------------------------

def _build_trade_performance_context(db_path):
    """Build actual trade P&L performance breakdown by strategy type.

    Queries the trades table for completed trades (those with pnl set)
    grouped by side/strategy type. This is more valuable than prediction
    accuracy because it reflects real money outcomes.

    Returns a summary string, or empty string if insufficient data.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return ""

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if not table_check:
            conn.close()
            return ""

        # Query trades grouped by side (buy, sell, short, cover)
        rows = conn.execute(
            "SELECT side, COUNT(*) as cnt, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
            "SUM(pnl) as total_pnl "
            "FROM trades WHERE pnl IS NOT NULL "
            "GROUP BY side"
        ).fetchall()

        if not rows:
            conn.close()
            return ""

        total_trades = sum(r["cnt"] for r in rows)
        if total_trades < 3:
            conn.close()
            return ""

        lines = ["ACTUAL TRADE PERFORMANCE (not just predictions):"]
        warnings = []

        side_labels = {
            "buy": "Long buys",
            "sell": "Long sells",
            "short": "Short sells",
            "cover": "Short covers",
        }

        for r in rows:
            side = r["side"]
            label = side_labels.get(side, side.capitalize())
            cnt = r["cnt"]
            wins = r["wins"] or 0
            losses = r["losses"] or 0
            total_pnl = r["total_pnl"] or 0

            win_rate = (wins / cnt * 100) if cnt > 0 else 0
            pnl_str = f"+${total_pnl:,.0f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.0f}"
            lines.append(f"  {label:16s} {wins} wins / {losses} losses | P&L: {pnl_str}")

            # Flag critical problems
            if side in ("short", "cover") and cnt >= 5 and win_rate == 0:
                warnings.append(
                    f"WARNING: Short selling has a 0% win rate across {cnt} trades.\n"
                    f"Stop-losses are triggering on every short position before they can profit.\n"
                    f"Consider: wider short stop-losses, or only shorting on bounce days."
                )
            elif side in ("short", "cover") and cnt >= 5 and win_rate < 20:
                warnings.append(
                    f"WARNING: Short selling has only a {win_rate:.0f}% win rate across {cnt} trades.\n"
                    f"Short stop-losses may be too tight, or entries are poorly timed."
                )

        for w in warnings:
            lines.append("")
            lines.append(w)

        conn.close()
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to build trade performance context: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return ""


def build_concise_context(ctx, symbol=None):
    """Build a concise AI prompt context (max 4 lines).

    This replaces the verbose build_performance_context() injection in the
    AI prompt. The verbose version is still used internally for self-tuning
    adjustments.

    Returns a string with at most 4 lines of context, or empty string.
    """
    if ctx is None:
        return ""

    db = getattr(ctx, "db_path", None)
    lines = []

    # 1. Market regime (1 line)
    try:
        from market_regime import detect_regime
        regime = detect_regime()
        if regime and regime.get("regime", "unknown") != "unknown":
            vix = regime.get("vix", 0)
            lines.append(f"MARKET: {regime['regime'].upper()} (VIX {vix:.0f})")
    except Exception:
        pass

    # 2. Stock-specific history (1 line)
    if symbol and db:
        try:
            rep = get_symbol_reputation(db)
            if symbol in rep:
                r = rep[symbol]
                lines.append(
                    f"YOUR RECORD ON {symbol}: "
                    f"{r['wins']}W/{r['losses']}L "
                    f"({r['win_rate']:.0f}% win rate)"
                )
        except Exception:
            pass

    # 3. Overall win rate (1 line)
    if db:
        try:
            conn = _get_conn(db)
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
            ).fetchone()
            if table_check:
                total = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
                ).fetchone()[0]
                wins = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved' AND actual_outcome='win'"
                ).fetchone()[0]
                if total >= 10:
                    wr = wins / total * 100
                    selectivity = "Be more selective." if wr < 40 else "Good accuracy."
                    lines.append(
                        f"YOUR OVERALL: {wr:.0f}% win rate "
                        f"({wins}W/{total - wins}L). {selectivity}"
                    )
            conn.close()
        except Exception:
            pass

    # 4. Earnings warning (1 line, only if imminent)
    if symbol:
        try:
            from earnings_calendar import check_earnings
            e = check_earnings(symbol)
            if e and e.get("days_until", 999) <= 5:
                lines.append(
                    f"EARNINGS: {symbol} reports in {e['days_until']} days. High uncertainty."
                )
        except Exception:
            pass

    return "\n".join(lines)


def build_performance_context(ctx, symbol=None, db_path=None):
    """Query the profile's AI predictions database and build a performance
    summary string that gets injected into the AI prompt.

    Returns an empty string if there are fewer than 10 resolved predictions
    or if self-tuning is disabled on the context.
    """
    if ctx is not None and not getattr(ctx, "enable_self_tuning", True):
        return ""

    db = db_path or (ctx.db_path if ctx else None)

    # Check cache (keyed by db_path + symbol)
    cache_key = f"perf_ctx_{db}_{symbol or 'all'}"
    if _is_cached(cache_key):
        return _cache[cache_key]

    try:
        conn = _get_conn(db)
    except Exception:
        return ""

    try:
        # Check if the table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return ""

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 10:
            conn.close()
            _set_cache(cache_key, "")
            return ""

        # --- Overall stats ---
        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        losses = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='loss'"
        ).fetchone()[0]
        win_rate = (wins / resolved * 100) if resolved else 0

        lines = []

        # --- Actual trade P&L by strategy (most important — real money) ---
        trade_perf = _build_trade_performance_context(db)
        if trade_perf:
            lines.append(trade_perf)
            lines.append("")

        # --- Stock-specific track record (most relevant context first) ---
        sym_rows = None
        if symbol:
            sym_rows = conn.execute(
                "SELECT predicted_signal, actual_outcome, actual_return_pct, "
                "price_at_prediction, timestamp "
                "FROM ai_predictions "
                "WHERE status='resolved' AND symbol=? "
                "ORDER BY timestamp DESC",
                (symbol.upper(),),
            ).fetchall()
            if sym_rows:
                sym_total = len(sym_rows)
                sym_wins = sum(1 for r in sym_rows if r["actual_outcome"] == "win")
                sym_losses = sym_total - sym_wins
                sym_wr = (sym_wins / sym_total * 100) if sym_total else 0
                all_rets = [r["actual_return_pct"] or 0 for r in sym_rows]
                sym_avg_ret = sum(all_rets) / len(all_rets) if all_rets else 0

                lines.append(f"STOCK-SPECIFIC TRACK RECORD ({symbol.upper()}):")
                lines.append(
                    f"You have predicted on {symbol.upper()} {sym_total} times: "
                    f"{sym_wins}W / {sym_losses}L ({sym_wr:.0f}% win rate)"
                )
                lines.append(
                    f"Average return on {symbol.upper()}: {sym_avg_ret:+.1f}%"
                )

                # Last 3 predictions
                last_3 = sym_rows[:3]
                if last_3:
                    lines.append("Last 3 predictions:")
                    for r in last_3:
                        ts = r["timestamp"][:10] if r["timestamp"] else "unknown"
                        outcome = "WIN" if r["actual_outcome"] == "win" else "LOSS"
                        ret = r["actual_return_pct"] or 0
                        lines.append(
                            f"  {ts}: {r['predicted_signal']} "
                            f"@ ${r['price_at_prediction']:.2f} "
                            f"-> {outcome} ({ret:+.1f}%)"
                        )

                if sym_wr == 0:
                    lines.append(
                        "WARNING: You have NEVER been right about this stock. "
                        "Consider avoiding it."
                    )
                elif sym_wr > 70:
                    lines.append(
                        "This is one of your best-performing stocks. "
                        "Your analysis of it tends to be accurate."
                    )

                lines.append("")

        # --- Overall stats ---
        lines.append("YOUR PREDICTION TRACK RECORD:")
        lines.append(
            f"Overall: {win_rate:.1f}% win rate "
            f"({wins}W / {losses}L from {resolved} resolved predictions)"
        )

        # --- Confidence calibration ---
        lines.append("")
        lines.append("Confidence Calibration:")
        bands = [(60, 70), (70, 80), (80, 101)]
        band_labels = ["60-70%", "70-80%", "80%+"]
        for (lo, hi), label in zip(bands, band_labels):
            hi_op = "<=" if hi > 100 else "<"
            band_total = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND confidence >= ? AND confidence {hi_op} ?",
                (lo, hi),
            ).fetchone()[0]
            band_wins = conn.execute(
                f"SELECT COUNT(*) FROM ai_predictions "
                f"WHERE status='resolved' AND actual_outcome='win' "
                f"AND confidence >= ? AND confidence {hi_op} ?",
                (lo, hi),
            ).fetchone()[0]
            if band_total > 0:
                bwr = band_wins / band_total * 100
                if bwr < 45:
                    note = "(not reliable at this level)"
                elif bwr < 60:
                    note = "(somewhat reliable)"
                else:
                    note = "(your most reliable predictions)"
                lines.append(f"  {label} confidence: {bwr:.0f}% win rate {note}")

        # --- Signal performance ---
        lines.append("")
        lines.append("Signal Performance:")
        for sig in ("BUY", "SELL"):
            sig_total = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND predicted_signal=?",
                (sig,),
            ).fetchone()[0]
            sig_wins = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal=?",
                (sig,),
            ).fetchone()[0]
            sig_avg_ret = conn.execute(
                "SELECT AVG(actual_return_pct) FROM ai_predictions "
                "WHERE status='resolved' AND predicted_signal=?",
                (sig,),
            ).fetchone()[0] or 0
            if sig_total > 0:
                swr = sig_wins / sig_total * 100
                lines.append(
                    f"  {sig} predictions: {swr:.0f}% win rate, "
                    f"avg return {sig_avg_ret:+.1f}%"
                )

        # --- Recent results (last 10) ---
        recent_rows = conn.execute(
            "SELECT predicted_signal, symbol, price_at_prediction, "
            "actual_outcome, actual_return_pct, reasoning "
            "FROM ai_predictions WHERE status='resolved' "
            "ORDER BY resolved_at DESC LIMIT 10"
        ).fetchall()
        if recent_rows:
            lines.append("")
            lines.append("Recent Results (last 10):")
            for r in recent_rows:
                outcome_label = "WIN" if r["actual_outcome"] == "win" else "LOSS"
                ret = r["actual_return_pct"] or 0
                # Truncate reasoning to keep it concise
                short_reason = (r["reasoning"] or "")[:60]
                if len(r["reasoning"] or "") > 60:
                    short_reason += "..."
                lines.append(
                    f"  {outcome_label}: {r['predicted_signal']} "
                    f"{r['symbol']} @ ${r['price_at_prediction']:.2f} "
                    f"({ret:+.1f}%)"
                )

        # --- Self-tuning guidance ---
        lines.append("")
        lines.append("SELF-TUNING GUIDANCE:")
        lines.append("Based on your track record, adjust your approach:")

        # Check BUY vs SELL performance
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='BUY'"
        ).fetchone()[0]
        buy_wins_count = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='SELL'"
        ).fetchone()[0]
        sell_wins_count = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='SELL'"
        ).fetchone()[0]

        buy_wr = (buy_wins_count / buy_total * 100) if buy_total > 0 else 0
        sell_wr = (sell_wins_count / sell_total * 100) if sell_total > 0 else 0

        if buy_total > 5 and buy_wr < 45:
            lines.append(
                f"- Your BUY predictions in the current market are losing more "
                f"than winning ({buy_wr:.0f}% win rate). Be more selective."
            )
        if sell_total > 5 and sell_wr > buy_wr + 10:
            lines.append(
                f"- SELL predictions ({sell_wr:.0f}%) outperform BUY predictions "
                f"({buy_wr:.0f}%). Consider favoring bearish setups."
            )

        # Check high-confidence performance
        high_conf_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND confidence >= 70"
        ).fetchone()[0]
        high_conf_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND confidence >= 70"
        ).fetchone()[0]
        if high_conf_total > 5:
            hcwr = high_conf_wins / high_conf_total * 100
            if hcwr > win_rate + 10:
                lines.append(
                    f"- Your higher confidence predictions (70%+) perform "
                    f"significantly better ({hcwr:.0f}% vs {win_rate:.0f}%). "
                    f"Only give high confidence when you're truly convinced."
                )

        # Symbol-specific guidance
        if symbol and sym_rows:
            sym_wr = sum(1 for r in sym_rows if r["actual_outcome"] == "win") / len(sym_rows) * 100
            if sym_wr < 40:
                lines.append(
                    f"- You've predicted on {symbol.upper()} before with poor results "
                    f"({sym_wr:.0f}% win rate). Exercise extra caution."
                )
            elif sym_wr > 60:
                lines.append(
                    f"- You've predicted on {symbol.upper()} before with good results "
                    f"({sym_wr:.0f}% win rate)."
                )

        # --- TIME-OF-DAY PERFORMANCE ---
        try:
            time_ctx = _build_time_context(db)
            if time_ctx:
                lines.append("")
                lines.append(time_ctx)
        except Exception as _time_exc:
            logger.warning("Failed to add time context: %s", _time_exc)

        # --- LESSONS LEARNED from tuning history ---
        profile_id = getattr(ctx, "profile_id", None) if ctx else None
        if profile_id:
            lessons = _build_lessons_learned(profile_id)
            if lessons:
                lines.append("")
                lines.append(lessons)

        # --- CROSS-PROFILE INSIGHTS (Feature 4) ---
        if ctx is not None and profile_id:
            try:
                cross_insights = _build_cross_profile_insights(
                    ctx.user_id, profile_id, db)
                if cross_insights:
                    lines.append("")
                    lines.append(cross_insights)
            except Exception as _cross_exc:
                logger.warning("Failed to build cross-profile insights: %s", _cross_exc)

        conn.close()
        result = "\n".join(lines)
        _set_cache(cache_key, result)
        return result

    except Exception as exc:
        logger.warning("Failed to build performance context: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return ""


def _build_time_context(db_path):
    """Build time-of-day performance summary from ai_predictions.

    Groups resolved predictions by hour and calculates win rate per time bucket.
    Only includes results if there are 5+ predictions in at least 2 buckets.

    Returns a summary string, or empty string if insufficient data.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return ""

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return ""

        rows = conn.execute(
            "SELECT timestamp, actual_outcome FROM ai_predictions "
            "WHERE status='resolved' AND timestamp IS NOT NULL"
        ).fetchall()
        conn.close()

        if not rows:
            return ""

        # Group by hour buckets
        buckets = {
            "open": {"label": "9:30-10:00 (Open)", "hours": {9}, "min_minute": 30, "wins": 0, "total": 0},
            "morning": {"label": "10:00-12:00 (Morning)", "hours": {10, 11}, "wins": 0, "total": 0},
            "midday": {"label": "12:00-14:00 (Midday)", "hours": {12, 13}, "wins": 0, "total": 0},
            "afternoon": {"label": "14:00-16:00 (Afternoon)", "hours": {14, 15}, "wins": 0, "total": 0},
        }

        for r in rows:
            ts_str = r["timestamp"]
            if not ts_str:
                continue
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts_str)
                hour = ts.hour
                minute = ts.minute

                # Classify into bucket
                if hour == 9 and minute >= 30:
                    bucket = "open"
                elif hour in (10, 11):
                    bucket = "morning"
                elif hour in (12, 13):
                    bucket = "midday"
                elif hour in (14, 15):
                    bucket = "afternoon"
                else:
                    continue  # Outside market hours

                buckets[bucket]["total"] += 1
                if r["actual_outcome"] == "win":
                    buckets[bucket]["wins"] += 1
            except Exception:
                continue

        # Check if we have enough data: 5+ predictions in at least 2 buckets
        active_buckets = [b for b in buckets.values() if b["total"] >= 5]
        if len(active_buckets) < 2:
            return ""

        lines = ["TIME-OF-DAY PERFORMANCE:"]
        for key in ("open", "morning", "midday", "afternoon"):
            b = buckets[key]
            if b["total"] > 0:
                wr = (b["wins"] / b["total"]) * 100
                if wr < 35:
                    note = "volatile, avoid"
                elif wr < 45:
                    note = "below average"
                elif wr < 55:
                    note = "average"
                elif wr < 65:
                    note = "good window"
                else:
                    note = "your best window"
                lines.append(
                    f"{b['label']}: {wr:.0f}% win rate "
                    f"({b['wins']}/{b['total']}) — {note}"
                )

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Failed to build time context: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return ""


def _build_lessons_learned(profile_id):
    """Build a concise LESSONS LEARNED section from tuning history.

    Kept under 300 tokens for prompt injection.
    """
    history = _get_tuning_history(profile_id, limit=20)
    if not history:
        return ""

    # Separate reviewed vs pending
    reviewed = [h for h in history if h["outcome_after"] != "pending"]
    pending = [h for h in history if h["outcome_after"] == "pending"]

    if not reviewed and not pending:
        return ""

    lines = ["LEARNING FROM PAST ADJUSTMENTS:"]

    # Show recent adjustments and outcomes (max 5)
    if reviewed:
        lines.append("Past adjustments and results:")
        for entry in reviewed[:5]:
            label = _days_ago_label(entry["timestamp"])
            param = entry["parameter_name"]
            old_v = entry["old_value"]
            new_v = entry["new_value"]
            reason = entry["reason"][:80]
            outcome = entry["outcome_after"].upper()
            wr_before = entry.get("win_rate_at_change") or 0
            wr_after = entry.get("win_rate_after") or 0

            if outcome == "IMPROVED":
                verdict = "GOOD DECISION"
            elif outcome == "WORSENED":
                verdict = "BAD DECISION — may need reversal"
            else:
                verdict = "NO CLEAR EFFECT"

            lines.append(
                f"- {label}: Changed {param} from {old_v} to {new_v} "
                f"(reason: {reason})"
            )
            lines.append(
                f"  Result: win rate {wr_before:.0f}% -> {wr_after:.0f}% "
                f"-> {verdict}"
            )

    # Show pending adjustments (max 3)
    if pending:
        for entry in pending[:3]:
            label = _days_ago_label(entry["timestamp"])
            param = entry["parameter_name"]
            old_v = entry["old_value"]
            new_v = entry["new_value"]
            lines.append(
                f"- {label}: Changed {param} from {old_v} to {new_v} "
                f"(awaiting results)"
            )

    # Summarize what works and what doesn't
    if reviewed:
        lines.append("Lessons:")
        param_outcomes = {}
        for entry in reviewed:
            key = entry["parameter_name"]
            if key not in param_outcomes:
                param_outcomes[key] = []
            param_outcomes[key].append(entry["outcome_after"])
        for param, outcomes in param_outcomes.items():
            improved = outcomes.count("improved")
            worsened = outcomes.count("worsened")
            if improved > worsened:
                lines.append(f"- Adjusting {param}: has worked well")
            elif worsened > improved:
                lines.append(f"- Adjusting {param}: has NOT worked, avoid")
            else:
                lines.append(f"- Adjusting {param}: mixed results")

    lines.append("Use this knowledge. Don't repeat strategies that failed.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get_auto_adjustments
# ---------------------------------------------------------------------------

def get_auto_adjustments(ctx, db_path=None):
    """Analyze performance data and return recommended parameter adjustments.

    Now checks tuning history before recommending changes:
    - Won't reverse a recent adjustment that improved things
    - Will reverse adjustments that worsened things
    - Won't repeat the same adjustment within 3 days
    """
    db = db_path or (ctx.db_path if ctx else None)
    profile_id = getattr(ctx, "profile_id", None) if ctx else None

    try:
        conn = _get_conn(db)
    except Exception:
        return {"reasons": []}

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return {"reasons": []}

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 20:
            conn.close()
            return {"reasons": [f"Only {resolved} resolved predictions (need 20+)"]}

        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        overall_wr = wins / resolved * 100

        result = {
            "confidence_threshold": None,
            "prefer_shorts": False,
            "reduce_position_size": overall_wr < 40,
            "reasons": [],
        }

        # --- Check tuning history before making recommendations ---
        # Don't recommend changes that were already tried recently
        if profile_id:
            recent_conf = _get_recent_adjustment(
                profile_id, "ai_confidence_threshold", days=3)
            recent_pos = _get_recent_adjustment(
                profile_id, "max_position_pct", days=3)
        else:
            recent_conf = None
            recent_pos = None

        # Win rate by confidence band
        if not recent_conf:  # Only suggest if not adjusted in last 3 days
            for threshold, label in [(60, "<60%"), (70, "<70%")]:
                band_total = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND confidence < ?",
                    (threshold,),
                ).fetchone()[0]
                band_wins = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND actual_outcome='win' AND confidence < ?",
                    (threshold,),
                ).fetchone()[0]
                if band_total > 5:
                    bwr = band_wins / band_total * 100
                    if bwr < 35:
                        # Check if a past adjustment in this direction worsened things
                        past_outcome = _was_adjustment_effective(
                            profile_id, "ai_confidence_threshold") if profile_id else None
                        if past_outcome == "worsened":
                            result["reasons"].append(
                                f"Win rate at confidence {label} is {bwr:.0f}%, "
                                f"but previous threshold raise worsened results — skipping"
                            )
                        else:
                            result["confidence_threshold"] = threshold
                            result["reasons"].append(
                                f"Win rate at confidence {label} is {bwr:.0f}%, "
                                f"raising threshold to {threshold}"
                            )
        else:
            result["reasons"].append(
                "Confidence threshold was adjusted recently — "
                "waiting for results before changing again"
            )

        # BUY vs SELL performance
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='BUY'"
        ).fetchone()[0]
        buy_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='SELL'"
        ).fetchone()[0]
        sell_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='SELL'"
        ).fetchone()[0]

        buy_wr = (buy_wins / buy_total * 100) if buy_total > 5 else 50
        sell_wr = (sell_wins / sell_total * 100) if sell_total > 5 else 50

        if buy_wr < 30 and sell_wr > 50:
            result["prefer_shorts"] = True
            result["reasons"].append(
                f"BUY win rate ({buy_wr:.0f}%) below threshold, "
                f"SELL win rate ({sell_wr:.0f}%) strong — consider enabling short selling"
            )

        if overall_wr < 40 and not recent_pos:
            result["reasons"].append(
                f"Overall win rate ({overall_wr:.0f}%) below 40%, "
                f"recommend reducing position size"
            )
        elif overall_wr < 40 and recent_pos:
            result["reasons"].append(
                f"Overall win rate ({overall_wr:.0f}%) below 40%, "
                f"but position size was adjusted recently — waiting for results"
            )
            result["reduce_position_size"] = False

        conn.close()
        return result

    except Exception as exc:
        logger.warning("Failed to get auto adjustments: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return {"reasons": [str(exc)]}


# ---------------------------------------------------------------------------
# apply_auto_adjustments
# ---------------------------------------------------------------------------

def describe_tuning_state(ctx, db_path=None):
    """Return a human-readable one-liner explaining why self-tuning may
    or may not have room to adjust anything. Used for dashboard
    visibility so the user sees the tuner is alive even when it doesn't
    change parameters.

    Returns a dict with:
        {
            "can_tune": bool — True if we have enough data to evaluate,
            "resolved": int — # of resolved AI predictions,
            "required": int — threshold required (20),
            "message": str — human-readable explanation,
        }
    """
    required = 20
    if ctx is None:
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": "No profile context available."}
    if not getattr(ctx, "enable_self_tuning", True):
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": "Self-tuning is disabled on this profile."}
    db = db_path or ctx.db_path
    try:
        conn = _get_conn(db)
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table:
            conn.close()
            return {"can_tune": False, "resolved": 0, "required": required,
                    "message": "AI prediction history not yet initialized."}
        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]
        conn.close()
        if resolved < required:
            return {
                "can_tune": False, "resolved": resolved, "required": required,
                "message": (
                    f"Waiting for more AI predictions to resolve — "
                    f"{resolved}/{required} ready. Each prediction takes about "
                    f"5 trading days to resolve; the tuner will start "
                    f"evaluating once the threshold is met."
                ),
            }
        msg = f"Tuner is evaluating against {resolved} resolved predictions."
        if resolved >= 30:
            msg += " Upward optimization active."
        return {
            "can_tune": True, "resolved": resolved, "required": required,
            "message": msg,
        }
    except Exception as exc:
        return {"can_tune": False, "resolved": 0, "required": required,
                "message": f"Could not read prediction state: {exc}"}


def apply_auto_adjustments(ctx, db_path=None):
    """Apply conservative auto-adjustments to the profile based on performance.

    Now also:
    - Logs every change to tuning_history via log_tuning_change()
    - Reviews past adjustments via review_past_adjustments()
    - Checks history to avoid oscillation and repeating failed strategies
    - Returns list of adjustment descriptions including review results
    """
    if ctx is None:
        return []

    if not getattr(ctx, "enable_self_tuning", True):
        return []

    db = db_path or ctx.db_path
    adjustments_made = []

    # --- First, review past adjustments ---
    profile_id = getattr(ctx, "profile_id", None)
    user_id = getattr(ctx, "user_id", None)

    if profile_id:
        try:
            from models import review_past_adjustments
            reviews = review_past_adjustments(profile_id, db_path=db)
            for rev in reviews:
                outcome = rev["outcome_after"].upper()
                param = rev["parameter_name"]
                old_v = rev["old_value"]
                new_v = rev["new_value"]
                wr_before = rev.get("win_rate_at_change") or 0
                wr_after = rev.get("win_rate_after") or 0
                adjustments_made.append(
                    f"Reviewed past adjustment: {param} {old_v}->{new_v} "
                    f"(win rate {wr_before:.0f}%->{wr_after:.0f}%: {outcome})"
                )

                # If a past adjustment worsened things, reverse it
                if rev["outcome_after"] == "worsened":
                    try:
                        from models import update_trading_profile, log_tuning_change
                        # Reverse: set back to old value
                        update_kwargs = {param: _cast_param_value(param, old_v)}
                        update_trading_profile(profile_id, **update_kwargs)

                        # Get current stats for the reversal log
                        try:
                            conn_tmp = _get_conn(db)
                            cur_wr, cur_resolved = _get_current_win_rate(conn_tmp)
                            conn_tmp.close()
                        except Exception:
                            cur_wr, cur_resolved = 0, 0

                        log_tuning_change(
                            profile_id, user_id or 0,
                            "auto_reversal", param,
                            new_v, old_v,
                            f"Reversing previous change — {outcome} "
                            f"(win rate went from {wr_before:.0f}% to {wr_after:.0f}%)",
                            win_rate_at_change=cur_wr,
                            predictions_resolved=cur_resolved,
                        )
                        adjustments_made.append(
                            f"REVERSED: {param} back from {new_v} to {old_v} "
                            f"(previous change worsened performance)"
                        )
                    except Exception as rev_exc:
                        logger.warning("Failed to reverse adjustment: %s", rev_exc)

        except Exception as exc:
            logger.warning("Failed to review past adjustments: %s", exc)

    try:
        conn = _get_conn(db)
    except Exception:
        return adjustments_made

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return adjustments_made

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 20:
            conn.close()
            return adjustments_made

        if not profile_id:
            conn.close()
            return adjustments_made

        from models import update_trading_profile, log_tuning_change

        overall_wr, _ = _get_current_win_rate(conn)

        # --- Check if confidence threshold was adjusted recently ---
        recent_conf = _get_recent_adjustment(
            profile_id, "ai_confidence_threshold", days=3)

        if not recent_conf:
            # Pick the right confidence threshold in ONE step — don't cascade.
            # Check the tighter band (70) first. If both bands are bad, go
            # straight to 70 instead of 60→70 in the same run.
            conf_adjusted = False

            band70_total = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND confidence < 70"
            ).fetchone()[0]
            band70_wins = conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win' AND confidence < 70"
            ).fetchone()[0]

            if band70_total > 5:
                wr70 = band70_wins / band70_total * 100
                if wr70 < 35 and ctx.ai_confidence_threshold < 70:
                    past_outcome = _was_adjustment_effective(
                        profile_id, "ai_confidence_threshold")
                    if past_outcome != "worsened":
                        old_val = ctx.ai_confidence_threshold
                        update_trading_profile(profile_id, ai_confidence_threshold=70)
                        reason = (
                            f"Win rate at <70% confidence was {wr70:.0f}% "
                            f"({band70_wins}/{band70_total})"
                        )
                        log_tuning_change(
                            profile_id, user_id or 0,
                            "confidence_threshold", "ai_confidence_threshold",
                            str(old_val), "70", reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        adjustments_made.append(
                            f"Raised AI confidence threshold from {old_val} "
                            f"to 70 ({reason})"
                        )
                        conf_adjusted = True

            if not conf_adjusted:
                band60_total = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND confidence < 60"
                ).fetchone()[0]
                band60_wins = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND actual_outcome='win' AND confidence < 60"
                ).fetchone()[0]

                if band60_total > 5:
                    wr60 = band60_wins / band60_total * 100
                    if wr60 < 35 and ctx.ai_confidence_threshold < 60:
                        past_outcome = _was_adjustment_effective(
                            profile_id, "ai_confidence_threshold")
                        if past_outcome != "worsened":
                            old_val = ctx.ai_confidence_threshold
                            update_trading_profile(profile_id, ai_confidence_threshold=60)
                            reason = (
                                f"Win rate at <60% confidence was {wr60:.0f}% "
                                f"({band60_wins}/{band60_total})"
                            )
                            log_tuning_change(
                                profile_id, user_id or 0,
                                "confidence_threshold", "ai_confidence_threshold",
                                str(old_val), "60", reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Raised AI confidence threshold from {old_val} "
                                f"to 60 ({reason})"
                            )

        # --- BUY vs SELL performance ---
        buy_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='BUY'"
        ).fetchone()[0]
        buy_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='BUY'"
        ).fetchone()[0]
        sell_total = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND predicted_signal='SELL'"
        ).fetchone()[0]
        sell_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win' AND predicted_signal='SELL'"
        ).fetchone()[0]

        buy_wr = (buy_wins / buy_total * 100) if buy_total > 5 else 50
        sell_wr = (sell_wins / sell_total * 100) if sell_total > 5 else 50

        if buy_wr < 30 and sell_wr > 50 and not ctx.enable_short_selling:
            adjustments_made.append(
                f"Recommendation: enable short selling — BUY win rate {buy_wr:.0f}%, "
                f"SELL win rate {sell_wr:.0f}%"
            )

        # --- Short trade performance — auto-widen stops if 0% win rate ---
        try:
            trade_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone()
            if trade_table:
                short_rows = conn.execute(
                    "SELECT COUNT(*) as cnt, "
                    "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(pnl) as total_pnl "
                    "FROM trades WHERE pnl IS NOT NULL AND side IN ('short', 'cover')"
                ).fetchone()
                short_cnt = short_rows["cnt"] if short_rows else 0
                short_wins = short_rows["wins"] or 0 if short_rows else 0
                short_pnl = short_rows["total_pnl"] or 0 if short_rows else 0
                short_wr = (short_wins / short_cnt * 100) if short_cnt > 0 else 100

                # Auto-widen short stop-loss if 0% win rate with 5+ trades
                if short_cnt >= 5 and short_wr == 0:
                    recent_short_sl = _get_recent_adjustment(
                        profile_id, "short_stop_loss_pct", days=3)
                    if not recent_short_sl:
                        current_sl = getattr(ctx, "short_stop_loss_pct", 0.08)
                        new_sl = round(min(current_sl * 1.5, 0.20), 4)
                        if new_sl > current_sl:
                            update_trading_profile(profile_id, short_stop_loss_pct=new_sl)
                            reason = (
                                f"Short selling 0% win rate across {short_cnt} trades — "
                                f"widening stop-loss by 50%"
                            )
                            log_tuning_change(
                                profile_id, user_id or 0,
                                "short_stop_loss", "short_stop_loss_pct",
                                str(current_sl), str(new_sl), reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Widened short stop-loss from {current_sl:.0%} to "
                                f"{new_sl:.0%} ({reason})"
                            )

                # Auto-disable shorts when consistently losing money: 10+ trades,
                # <20% win rate, negative total P&L. This is defensive (stops
                # bleeding) — safe to auto-action. The reverse case
                # (auto-enabling shorts) is intentionally left as a
                # recommendation only because flipping a high-risk feature ON
                # without human review is dangerous.
                if (short_cnt >= 10 and short_wr < 20 and short_pnl < 0
                        and getattr(ctx, "enable_short_selling", False)):
                    if not _get_recent_adjustment(
                            profile_id, "enable_short_selling", days=3):
                        if _was_adjustment_effective(
                                profile_id, "enable_short_selling") != "worsened":
                            update_trading_profile(
                                profile_id, enable_short_selling=0)
                            reason = (
                                f"Short selling losing across "
                                f"{short_cnt} trades — "
                                f"{short_wins} wins ({short_wr:.0f}%), "
                                f"total P&L ${short_pnl:,.0f}"
                            )
                            log_tuning_change(
                                profile_id, user_id or 0,
                                "short_disable", "enable_short_selling",
                                "1", "0", reason,
                                win_rate_at_change=overall_wr,
                                predictions_resolved=resolved,
                            )
                            adjustments_made.append(
                                f"Disabled short selling ({reason})"
                            )
        except Exception as _short_exc:
            logger.warning("Failed to check short trade performance: %s", _short_exc)

        # --- Overall win rate too low — reduce position size ---
        recent_pos = _get_recent_adjustment(
            profile_id, "max_position_pct", days=3)

        if overall_wr < 30 and not recent_pos:
            past_outcome = _was_adjustment_effective(
                profile_id, "max_position_pct")
            if past_outcome != "worsened":
                new_pct = max(0.03, ctx.max_position_pct * 0.8)
                if new_pct < ctx.max_position_pct:
                    old_val = ctx.max_position_pct
                    update_trading_profile(profile_id, max_position_pct=round(new_pct, 4))
                    reason = f"Overall win rate {overall_wr:.0f}% below 30%"
                    log_tuning_change(
                        profile_id, user_id or 0,
                        "position_size", "max_position_pct",
                        str(old_val), str(round(new_pct, 4)), reason,
                        win_rate_at_change=overall_wr,
                        predictions_resolved=resolved,
                    )
                    adjustments_made.append(
                        f"Reduced max position size from {old_val:.1%} "
                        f"to {new_pct:.1%} ({reason})"
                    )

        # --- Upward optimizations (only when not in disaster mode) ---
        if overall_wr >= 35:
            upward = _apply_upward_optimizations(
                conn, ctx, profile_id, user_id, overall_wr, resolved
            )
            adjustments_made.extend(upward)

        conn.close()
        return adjustments_made

    except Exception as exc:
        logger.warning("Failed to apply auto adjustments: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return adjustments_made


def _cast_param_value(param_name, value_str):
    """Cast a string value back to the appropriate type for a profile parameter."""
    int_params = {"ai_confidence_threshold", "max_total_positions", "min_volume",
                   "avoid_earnings_days", "skip_first_minutes"}
    float_params = {
        "max_position_pct", "stop_loss_pct", "take_profit_pct",
        "short_stop_loss_pct", "short_take_profit_pct",
        "min_price", "max_price", "volume_surge_multiplier",
        "rsi_overbought", "rsi_oversold", "momentum_5d_gain",
        "momentum_20d_gain", "breakout_volume_threshold", "gap_pct_threshold",
        "drawdown_pause_pct", "drawdown_reduce_pct",
    }
    bool_params = {
        "strategy_momentum_breakout", "strategy_volume_spike",
        "strategy_mean_reversion", "strategy_gap_and_go",
        "enable_short_selling", "enable_self_tuning", "maga_mode",
        "enable_consensus",
    }

    if param_name in int_params:
        return int(float(value_str))
    elif param_name in float_params:
        return float(value_str)
    elif param_name in bool_params:
        return int(float(value_str))
    return value_str


# ---------------------------------------------------------------------------
# Upward optimization — actively improve win rate, not just prevent disaster
# ---------------------------------------------------------------------------

def _apply_upward_optimizations(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Run upward optimization strategies on a healthy profile.

    Called only when overall_wr >= 35 (disaster prevention has exclusive
    control below that). Each sub-function makes at most ONE change.
    The orchestrator stops after the first change so auto-reversal can
    attribute any win-rate shift to that specific adjustment.
    """
    from models import update_trading_profile, log_tuning_change

    optimizers = [
        _optimize_confidence_threshold_upward,
        _optimize_regime_position_sizing,
        _optimize_strategy_toggles,
        _optimize_stop_take_profit,
        _optimize_position_size_upward,
        # Wave 1 — Group A (concentration / risk)
        _optimize_max_total_positions,
        _optimize_max_correlation,
        _optimize_max_sector_positions,
        _optimize_drawdown_thresholds,
        _optimize_drawdown_reduce,
        _optimize_price_band,
        # Wave 1 — Group D (timing + flag)
        _optimize_avoid_earnings_days,
        _optimize_skip_first_minutes,
        _optimize_maga_mode,
        # Wave 2 — Group C (entry filters)
        _optimize_min_volume,
        _optimize_volume_surge_multiplier,
        _optimize_breakout_volume_threshold,
        _optimize_gap_pct_threshold,
        _optimize_momentum_5d,
        _optimize_momentum_20d,
        _optimize_rsi_overbought,
        _optimize_rsi_oversold,
        # Wave 3 — Group B (exits — booleans roll into Layer 2 weights)
        _optimize_short_take_profit,
        _optimize_atr_multiplier_sl,
        _optimize_atr_multiplier_tp,
        _optimize_trailing_atr_multiplier,
    ]

    results = []
    for optimizer in optimizers:
        try:
            result = optimizer(conn, ctx, profile_id, user_id, overall_wr, resolved)
            if result:
                results.append(result)
                break  # One change per run
        except Exception as exc:
            logger.warning("Upward optimizer %s failed: %s", optimizer.__name__, exc)

    return results


def _optimize_confidence_threshold_upward(conn, ctx, profile_id, user_id,
                                          overall_wr, resolved):
    """Find the confidence band with the best win rate and raise the
    threshold to focus on that band. Only raises one band at a time."""
    if _get_recent_adjustment(profile_id, "ai_confidence_threshold", days=3):
        return None
    if _was_adjustment_effective(profile_id, "ai_confidence_threshold") == "worsened":
        return None

    rows = conn.execute(
        """SELECT
             CASE WHEN confidence >= 80 THEN 80
                  WHEN confidence >= 70 THEN 70
                  WHEN confidence >= 60 THEN 60
                  WHEN confidence >= 50 THEN 50
                  ELSE 0 END as band_floor,
             COUNT(*) as total,
             SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions WHERE status='resolved'
           GROUP BY band_floor HAVING COUNT(*) >= 10
           ORDER BY band_floor"""
    ).fetchall()

    if not rows:
        return None

    # Find the band with the highest win rate
    best_band = None
    best_wr = 0
    for r in rows:
        wr = (r["wins"] / r["total"] * 100) if r["total"] > 0 else 0
        if wr > best_wr:
            best_wr = wr
            best_band = r["band_floor"]

    if best_band is None or best_band <= ctx.ai_confidence_threshold:
        return None

    # Best band must be meaningfully better than overall
    if best_wr < overall_wr + 10:
        return None

    # Only raise one band at a time
    current = ctx.ai_confidence_threshold
    band_levels = [50, 60, 70, 80]
    new_threshold = current
    for level in band_levels:
        if level > current and level <= best_band:
            new_threshold = level
            break

    if new_threshold <= current or new_threshold > 80:
        return None

    # Verify enough predictions exist above the new threshold
    above_count = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved' AND confidence >= ?",
        (new_threshold,),
    ).fetchone()[0]
    if above_count < 10:
        return None

    from models import update_trading_profile, log_tuning_change
    update_trading_profile(profile_id, ai_confidence_threshold=new_threshold)
    reason = (
        f"Confidence {new_threshold}+ band has {best_wr:.0f}% win rate "
        f"vs {overall_wr:.0f}% overall — focusing on higher-conviction trades"
    )
    log_tuning_change(
        profile_id, user_id, "confidence_threshold_optimization",
        "ai_confidence_threshold", str(current), str(new_threshold), reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return f"Raised confidence threshold from {current} to {new_threshold} ({reason})"


def _optimize_regime_position_sizing(conn, ctx, profile_id, user_id,
                                     overall_wr, resolved):
    """Reduce position size in losing regimes, increase in winning regimes."""
    if overall_wr < 45:
        return None  # Profile must be healthy enough for regime analysis
    if _get_recent_adjustment(profile_id, "max_position_pct", days=3):
        return None

    # Check if regime column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
    if "regime_at_prediction" not in cols:
        return None

    rows = conn.execute(
        """SELECT regime_at_prediction, COUNT(*) as total,
                  SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions
           WHERE status='resolved' AND regime_at_prediction IS NOT NULL
           GROUP BY regime_at_prediction HAVING COUNT(*) >= 10"""
    ).fetchall()

    if not rows:
        return None

    # Get current regime
    try:
        from market_regime import detect_regime
        regime_info = detect_regime()
        current_regime = regime_info.get("regime", "unknown") if regime_info else "unknown"
    except Exception:
        return None

    # Find current regime's win rate
    current_regime_wr = None
    for r in rows:
        if r["regime_at_prediction"] == current_regime:
            current_regime_wr = (r["wins"] / r["total"] * 100) if r["total"] > 0 else None
            break

    if current_regime_wr is None:
        return None

    diff = current_regime_wr - overall_wr
    current_pct = ctx.max_position_pct

    if diff <= -15:
        # Losing regime — reduce by 25%
        if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
            return None
        new_pct = round(max(0.03, current_pct * 0.75), 4)
        if new_pct >= current_pct:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_position_pct=new_pct)
        reason = (
            f"{current_regime} regime win rate {current_regime_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall — reducing exposure"
        )
        log_tuning_change(
            profile_id, user_id, "regime_position_sizing",
            "max_position_pct", str(current_pct), str(new_pct), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Reduced position size from {current_pct:.1%} to {new_pct:.1%} ({reason})"

    elif diff >= 15:
        # Winning regime — increase by 15%
        if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
            return None
        new_pct = round(min(0.20, current_pct * 1.15), 4)
        if new_pct <= current_pct:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_position_pct=new_pct)
        reason = (
            f"{current_regime} regime win rate {current_regime_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall — increasing exposure to edge"
        )
        log_tuning_change(
            profile_id, user_id, "regime_position_sizing",
            "max_position_pct", str(current_pct), str(new_pct), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Increased position size from {current_pct:.1%} to {new_pct:.1%} ({reason})"

    return None


def _optimize_strategy_toggles(conn, ctx, profile_id, user_id,
                                overall_wr, resolved):
    """Disable the worst-performing strategy if it's dragging down results."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
    if "strategy_type" not in cols:
        return None

    rows = conn.execute(
        """SELECT strategy_type, COUNT(*) as total,
                  SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins
           FROM ai_predictions
           WHERE status='resolved' AND strategy_type IS NOT NULL
           GROUP BY strategy_type HAVING COUNT(*) >= 10
           ORDER BY (CAST(SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) AS REAL)
                     / COUNT(*)) ASC"""
    ).fetchall()

    if not rows:
        return None

    # Count currently enabled toggles
    enabled_count = sum(1 for col in _STRATEGY_TYPE_TO_TOGGLE.values()
                        if getattr(ctx, col, True))
    if enabled_count < 2:
        return None  # Never disable the last strategy

    for r in rows:
        stype = r["strategy_type"]
        total = r["total"]
        wr = (r["wins"] / total * 100) if total > 0 else 0

        # Must be both bad absolutely AND bad relative to overall
        if wr >= 30 or (overall_wr - wr) < 15:
            continue

        toggle_col = _STRATEGY_TYPE_TO_TOGGLE.get(stype)
        if not toggle_col:
            # No profile-level toggle — these are the modular `strategies/`
            # plugins (insider_cluster, options-flow-derived, etc.). The
            # alpha_decay module already has a deprecation pipeline that
            # excludes them from `get_active_strategies()`, with automatic
            # restoration when their rolling Sharpe recovers. Wire to it.
            from display_names import display_name as _dn

            # Per-strategy cooldown (3 days) under a synthetic param key so
            # the existing tuning_history machinery can track and respect it.
            deprecate_key = f"deprecate:{stype}"
            if _get_recent_adjustment(profile_id, deprecate_key, days=3):
                continue
            if _was_adjustment_effective(profile_id, deprecate_key) == "worsened":
                continue

            db_path = getattr(ctx, "db_path", None)
            if not db_path:
                continue

            try:
                from alpha_decay import deprecate_strategy, is_deprecated
                if is_deprecated(db_path, stype):
                    continue
                from models import log_tuning_change
                detection = {
                    "reason": (
                        f"Self-tuner: {_dn(stype)} win rate "
                        f"{wr:.0f}% ({r['wins']}/{total}) vs "
                        f"{overall_wr:.0f}% overall"
                    ),
                    "current_rolling_sharpe": None,
                    "lifetime_sharpe": None,
                    "consecutive_bad_days": 0,
                }
                deprecate_strategy(db_path, stype, detection)
                log_tuning_change(
                    profile_id, user_id, "strategy_deprecate",
                    deprecate_key, "active", "deprecated",
                    detection["reason"],
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (
                    f"Deprecated {_dn(stype)} strategy "
                    f"(win rate {wr:.0f}% — will auto-restore when rolling "
                    f"Sharpe recovers)"
                )
            except Exception as _exc:
                logger.warning("Deprecation failed for %s: %s", stype, _exc)
                continue

        if not getattr(ctx, toggle_col, True):
            continue  # Already disabled

        if _get_recent_adjustment(profile_id, toggle_col, days=3):
            continue
        if _was_adjustment_effective(profile_id, toggle_col) == "worsened":
            continue

        from models import update_trading_profile, log_tuning_change
        from display_names import display_name as _dn
        update_trading_profile(profile_id, **{toggle_col: 0})
        reason = (
            f"{_dn(stype)} win rate {wr:.0f}% ({r['wins']}/{total}) "
            f"vs {overall_wr:.0f}% overall"
        )
        log_tuning_change(
            profile_id, user_id, "strategy_toggle",
            toggle_col, "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Disabled {_dn(stype)} strategy ({reason})"

    return None


def _optimize_stop_take_profit(conn, ctx, profile_id, user_id,
                                overall_wr, resolved):
    """Adjust stop-loss and take-profit based on actual trade P&L distribution."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # Get closed trades with P&L
    trades = conn.execute(
        """SELECT price, qty, pnl, stop_loss, take_profit
           FROM trades
           WHERE pnl IS NOT NULL AND lower(side) = 'buy'
             AND price > 0 AND qty > 0"""
    ).fetchall()

    if len(trades) < 15:
        return None

    losses = []
    wins = []
    for t in trades:
        ret_pct = (t["pnl"] / (t["price"] * t["qty"])) * 100
        if t["pnl"] < 0:
            losses.append(ret_pct)
        elif t["pnl"] > 0:
            wins.append(ret_pct)

    # --- Stop-loss optimization ---
    if not _get_recent_adjustment(profile_id, "stop_loss_pct", days=3):
        if _was_adjustment_effective(profile_id, "stop_loss_pct") != "worsened":
            if losses and overall_wr > 45:
                sl_pct = ctx.stop_loss_pct * 100  # e.g., 3.0
                # Count losses that are near the stop level (within 1% of stop)
                near_stop = [l for l in losses if abs(l) <= sl_pct + 1.0]
                if len(near_stop) > len(losses) * 0.4:
                    # 40%+ of losses cluster near the stop → stop is too tight
                    new_sl = round(min(0.08, ctx.stop_loss_pct * 1.20), 4)
                    if new_sl > ctx.stop_loss_pct:
                        from models import update_trading_profile, log_tuning_change
                        old_val = ctx.stop_loss_pct
                        update_trading_profile(profile_id, stop_loss_pct=new_sl)
                        reason = (
                            f"{len(near_stop)}/{len(losses)} losses cluster near "
                            f"{sl_pct:.1f}% stop — widening to give trades more room"
                        )
                        log_tuning_change(
                            profile_id, user_id, "stop_loss_optimization",
                            "stop_loss_pct", str(old_val), str(new_sl), reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        return (
                            f"Widened stop-loss from {old_val:.1%} to {new_sl:.1%} "
                            f"({reason})"
                        )

    # --- Take-profit optimization ---
    if not _get_recent_adjustment(profile_id, "take_profit_pct", days=3):
        if _was_adjustment_effective(profile_id, "take_profit_pct") != "worsened":
            if wins:
                tp_pct = ctx.take_profit_pct * 100  # e.g., 10.0
                avg_win = sum(wins) / len(wins)
                # If average win is less than 50% of the TP, TP is too ambitious
                if avg_win < tp_pct * 0.5 and tp_pct > 3.0:
                    new_tp = round(max(0.03, ctx.take_profit_pct * 0.80), 4)
                    if new_tp < ctx.take_profit_pct:
                        from models import update_trading_profile, log_tuning_change
                        old_val = ctx.take_profit_pct
                        update_trading_profile(profile_id, take_profit_pct=new_tp)
                        reason = (
                            f"Average win is +{avg_win:.1f}% but TP is at "
                            f"{tp_pct:.1f}% — tightening to capture more gains"
                        )
                        log_tuning_change(
                            profile_id, user_id, "take_profit_optimization",
                            "take_profit_pct", str(old_val), str(new_tp), reason,
                            win_rate_at_change=overall_wr,
                            predictions_resolved=resolved,
                        )
                        return (
                            f"Tightened take-profit from {old_val:.1%} to {new_tp:.1%} "
                            f"({reason})"
                        )

    return None


def _optimize_position_size_upward(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Increase position size when there is a proven, consistent edge."""
    if overall_wr < 55 or resolved < 30:
        return None
    if ctx.max_position_pct >= 0.15:
        return None  # Already at cap
    if _get_recent_adjustment(profile_id, "max_position_pct", days=3):
        return None
    if _was_adjustment_effective(profile_id, "max_position_pct") == "worsened":
        return None

    # Verify average return is positive
    avg_ret = conn.execute(
        "SELECT AVG(actual_return_pct) FROM ai_predictions WHERE status='resolved'"
    ).fetchone()[0]
    if avg_ret is None or avg_ret <= 0:
        return None

    current = ctx.max_position_pct
    new_pct = round(min(0.15, current * 1.15), 4)
    if new_pct <= current:
        return None

    from models import update_trading_profile, log_tuning_change
    update_trading_profile(profile_id, max_position_pct=new_pct)
    reason = (
        f"Win rate {overall_wr:.0f}% with +{avg_ret:.2f}% avg return "
        f"on {resolved} predictions — increasing position size to capitalize"
    )
    log_tuning_change(
        profile_id, user_id, "position_size_optimization",
        "max_position_pct", str(current), str(new_pct), reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return f"Increased position size from {current:.1%} to {new_pct:.1%} ({reason})"


# ---------------------------------------------------------------------------
# Wave 1 — extended-coverage optimizers (Group A: concentration/risk,
# Group D: timing). Each rule follows the same pattern: cooldown check,
# reverse-if-worsened guard, signal detection from data, bound clamping
# via param_bounds.clamp, write via update_trading_profile, log via
# log_tuning_change. Returns a human-readable string on action, None
# on no-op.
# ---------------------------------------------------------------------------

def _bound(name, value):
    """Clamp helper — wraps param_bounds.clamp with a stable import."""
    from param_bounds import clamp
    return clamp(name, value)


def _safe_change_guarded(profile_id, param_name):
    """Common cooldown + reverse-if-worsened check. Returns True if the
    rule is ALLOWED to make a change to this parameter right now."""
    if _get_recent_adjustment(profile_id, param_name, days=3):
        return False
    if _was_adjustment_effective(profile_id, param_name) == "worsened":
        return False
    return True


def _optimize_max_total_positions(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Concentration risk: reduce position count cap when avg loss is large
    AND the cap is being hit often. Increase when WR is strong AND the cap
    is constraining capacity."""
    if not _safe_change_guarded(profile_id, "max_total_positions"):
        return None

    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    # Average loss size on closed losers
    row = conn.execute(
        "SELECT AVG(pnl) as avg_loss FROM trades "
        "WHERE pnl IS NOT NULL AND pnl < 0"
    ).fetchone()
    avg_loss = row["avg_loss"] if row and row["avg_loss"] is not None else 0

    current = getattr(ctx, "max_total_positions", 10)
    if not isinstance(current, int):
        current = int(current)

    # Reduce when losses are deep AND we're losing broadly.
    if overall_wr < 40 and avg_loss < -200:
        new_val = _bound("max_total_positions", current - 1)
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_total_positions=new_val)
        reason = (
            f"Concentration risk — avg loss ${avg_loss:.0f} on {overall_wr:.0f}% WR "
            f"— reduce concurrent positions"
        )
        log_tuning_change(
            profile_id, user_id, "concentration_reduce",
            "max_total_positions", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Reduced max concurrent positions from {current} to {new_val} ({reason})"

    # Increase when strong WR AND average winner is meaningful.
    row = conn.execute(
        "SELECT AVG(pnl) as avg_win FROM trades "
        "WHERE pnl IS NOT NULL AND pnl > 0"
    ).fetchone()
    avg_win = row["avg_win"] if row and row["avg_win"] is not None else 0

    if overall_wr >= 60 and avg_win > 100:
        new_val = _bound("max_total_positions", current + 1)
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_total_positions=new_val)
        reason = (
            f"Strong edge — {overall_wr:.0f}% WR, avg winner ${avg_win:.0f} "
            f"— allow more concurrent positions"
        )
        log_tuning_change(
            profile_id, user_id, "concentration_increase",
            "max_total_positions", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Raised max concurrent positions from {current} to {new_val} ({reason})"

    return None


def _optimize_max_correlation(conn, ctx, profile_id, user_id,
                               overall_wr, resolved):
    """Diversification: tighten the correlation cap when losses cluster in
    correlated names. Loosen when too many candidates are gated out."""
    if not _safe_change_guarded(profile_id, "max_correlation"):
        return None

    # Heuristic: count losing trades that occurred within the same week
    # and same sector — a proxy for correlated drawdowns.
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    cluster_row = conn.execute(
        """SELECT strftime('%Y-%W', timestamp) as week, COUNT(*) as cnt
           FROM trades
           WHERE pnl IS NOT NULL AND pnl < 0
           GROUP BY week HAVING cnt >= 3"""
    ).fetchall()

    losing_weeks_with_clusters = len(cluster_row)
    total_weeks_row = conn.execute(
        """SELECT COUNT(DISTINCT strftime('%Y-%W', timestamp)) as n
           FROM trades WHERE pnl IS NOT NULL"""
    ).fetchone()
    total_weeks = total_weeks_row["n"] if total_weeks_row else 0

    if total_weeks < 4:
        return None  # Not enough history

    cluster_rate = losing_weeks_with_clusters / total_weeks if total_weeks > 0 else 0
    current = getattr(ctx, "max_correlation", 0.7)

    if cluster_rate >= 0.4:
        # Tighten — loss clusters on >40% of weeks suggests over-correlation
        new_val = _bound("max_correlation", round(current - 0.05, 4))
        if new_val >= current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_correlation=new_val)
        reason = (
            f"Loss-cluster weeks {cluster_rate:.0%} — tighten correlation cap"
        )
        log_tuning_change(
            profile_id, user_id, "correlation_tighten",
            "max_correlation", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Tightened max_correlation from {current:.2f} to {new_val:.2f} ({reason})"

    # Loosen if very few clustering weeks AND profile is performing well
    if cluster_rate < 0.1 and overall_wr >= 55:
        new_val = _bound("max_correlation", round(current + 0.05, 4))
        if new_val <= current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_correlation=new_val)
        reason = (
            f"Low loss-clustering ({cluster_rate:.0%}) + healthy WR — "
            f"loosen correlation cap to admit more candidates"
        )
        log_tuning_change(
            profile_id, user_id, "correlation_loosen",
            "max_correlation", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Loosened max_correlation from {current:.2f} to {new_val:.2f} ({reason})"

    return None


def _optimize_max_sector_positions(conn, ctx, profile_id, user_id,
                                    overall_wr, resolved):
    """Sector cap: reduce when sector concentration accompanies bad days."""
    if not _safe_change_guarded(profile_id, "max_sector_positions"):
        return None

    # Use the same loss-cluster heuristic but coarser.
    if overall_wr < 35:
        current = getattr(ctx, "max_sector_positions", 5)
        if not isinstance(current, int):
            current = int(current)
        new_val = _bound("max_sector_positions", current - 1)
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, max_sector_positions=new_val)
        reason = (
            f"Overall WR {overall_wr:.0f}% — tighten sector cap to "
            f"avoid concentration drawdowns"
        )
        log_tuning_change(
            profile_id, user_id, "sector_cap_tighten",
            "max_sector_positions", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Reduced max_sector_positions from {current} to {new_val} ({reason})"

    return None


def _optimize_drawdown_thresholds(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Drawdown thresholds: tighten when past pause/reduce events were
    followed by deeper drawdown (didn't catch the slide early enough)."""
    # We don't yet track drawdown-triggered events in a structured way.
    # For now: when overall_wr is in the deteriorating zone (35-45%) AND
    # we haven't tightened recently, tighten by one notch — slightly more
    # conservative defaults during stretches of underperformance.
    if not (35 <= overall_wr < 45):
        return None
    if not _safe_change_guarded(profile_id, "drawdown_pause_pct"):
        return None
    current = getattr(ctx, "drawdown_pause_pct", 0.20)
    new_val = _bound("drawdown_pause_pct", round(current - 0.02, 4))
    if new_val >= current:
        return None
    from models import update_trading_profile, log_tuning_change
    update_trading_profile(profile_id, drawdown_pause_pct=new_val)
    reason = (
        f"WR drifting at {overall_wr:.0f}% — tighten drawdown-pause "
        f"to catch deterioration sooner"
    )
    log_tuning_change(
        profile_id, user_id, "drawdown_pause_tighten",
        "drawdown_pause_pct", str(current), str(new_val), reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (f"Tightened drawdown-pause threshold from {current:.0%} to "
            f"{new_val:.0%} ({reason})")


def _optimize_drawdown_reduce(conn, ctx, profile_id, user_id,
                               overall_wr, resolved):
    """Mirror of pause-threshold but for the reduce-position trigger."""
    if not (35 <= overall_wr < 45):
        return None
    if not _safe_change_guarded(profile_id, "drawdown_reduce_pct"):
        return None
    current = getattr(ctx, "drawdown_reduce_pct", 0.10)
    new_val = _bound("drawdown_reduce_pct", round(current - 0.01, 4))
    if new_val >= current:
        return None
    from models import update_trading_profile, log_tuning_change
    update_trading_profile(profile_id, drawdown_reduce_pct=new_val)
    reason = (
        f"WR drifting at {overall_wr:.0f}% — tighten drawdown-reduce "
        f"trigger so position-size cuts kick in earlier"
    )
    log_tuning_change(
        profile_id, user_id, "drawdown_reduce_tighten",
        "drawdown_reduce_pct", str(current), str(new_val), reason,
        win_rate_at_change=overall_wr, predictions_resolved=resolved,
    )
    return (f"Tightened drawdown-reduce threshold from {current:.0%} to "
            f"{new_val:.0%} ({reason})")


def _optimize_price_band(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Tune min_price / max_price within 0.5x-2.0x of current (and the
    absolute floor/ceiling in PARAM_BOUNDS) when entries near the band
    edges consistently fail."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None

    current_min = float(getattr(ctx, "min_price", 1.0))
    current_max = float(getattr(ctx, "max_price", 20.0))

    # Bottom-of-band failure check: trades entered within 1.5× of min_price.
    bottom_threshold = current_min * 1.5
    bot_row = conn.execute(
        "SELECT COUNT(*) as cnt, "
        " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
        "FROM trades WHERE pnl IS NOT NULL AND price <= ? AND price > 0",
        (bottom_threshold,),
    ).fetchone()
    if (bot_row and bot_row["cnt"] >= 5):
        bot_wr = (bot_row["wins"] or 0) / bot_row["cnt"] * 100
        if bot_wr < 30 and _safe_change_guarded(profile_id, "min_price"):
            # Raise floor — never above 2× current (identity guard) and
            # always within absolute bounds.
            candidate = min(current_min * 1.25, current_min * 2.0)
            new_min = _bound("min_price", round(candidate, 2))
            if new_min > current_min and new_min < current_max:
                from models import update_trading_profile, log_tuning_change
                update_trading_profile(profile_id, min_price=new_min)
                reason = (
                    f"Bottom-of-band entries (≤${bottom_threshold:.2f}) "
                    f"win rate {bot_wr:.0f}% on {bot_row['cnt']} trades — "
                    f"raise min_price floor"
                )
                log_tuning_change(
                    profile_id, user_id, "price_band_min_raise",
                    "min_price", str(current_min), str(new_min), reason,
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (f"Raised min_price from ${current_min:.2f} to "
                        f"${new_min:.2f} ({reason})")

    # Top-of-band failure check: trades entered within 0.85× of max_price.
    top_threshold = current_max * 0.85
    top_row = conn.execute(
        "SELECT COUNT(*) as cnt, "
        " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins "
        "FROM trades WHERE pnl IS NOT NULL AND price >= ?",
        (top_threshold,),
    ).fetchone()
    if (top_row and top_row["cnt"] >= 5):
        top_wr = (top_row["wins"] or 0) / top_row["cnt"] * 100
        if top_wr < 30 and _safe_change_guarded(profile_id, "max_price"):
            candidate = max(current_max * 0.85, current_max * 0.5)
            new_max = _bound("max_price", round(candidate, 2))
            if new_max < current_max and new_max > current_min:
                from models import update_trading_profile, log_tuning_change
                update_trading_profile(profile_id, max_price=new_max)
                reason = (
                    f"Top-of-band entries (≥${top_threshold:.2f}) "
                    f"win rate {top_wr:.0f}% on {top_row['cnt']} trades — "
                    f"lower max_price ceiling"
                )
                log_tuning_change(
                    profile_id, user_id, "price_band_max_lower",
                    "max_price", str(current_max), str(new_max), reason,
                    win_rate_at_change=overall_wr,
                    predictions_resolved=resolved,
                )
                return (f"Lowered max_price from ${current_max:.2f} to "
                        f"${new_max:.2f} ({reason})")

    return None


def _optimize_avoid_earnings_days(conn, ctx, profile_id, user_id,
                                   overall_wr, resolved):
    """Earnings window: shrink when entries near earnings outperform; grow
    when they underperform.

    We don't currently log a clean 'days_to_earnings' on each prediction.
    This rule is a placeholder for the time-bucketed signal — once the
    feature lands, the body fills in. For W1 the rule self-skips
    cleanly (returns None) but is registered so the orchestrator
    structure is in place.
    """
    return None


def _optimize_skip_first_minutes(conn, ctx, profile_id, user_id,
                                  overall_wr, resolved):
    """First-X-minutes filter — needs intraday entry-time data which
    isn't structured today. Same placeholder pattern as
    _optimize_avoid_earnings_days; rule registers for orchestration but
    no-ops until the feature column exists."""
    return None


# ---------------------------------------------------------------------------
# Wave 2 — entry filter optimizers (Layer 1 Group C). Pattern: bucket
# resolved predictions by which side of the threshold they fell on (read
# from features_json), tighten if marginal bucket underperforms, loosen
# if too many would-have-winners are filtered out. All rules degrade
# gracefully (no-op) when the relevant feature isn't present in
# features_json yet.
# ---------------------------------------------------------------------------

def _bucket_by_feature(conn, feature_name):
    """Iterate resolved predictions and yield (feature_value, outcome)
    tuples for those where features_json contains a numeric value for
    `feature_name`. Skips silently for predictions without the feature."""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "features_json" not in cols:
        return
    rows = conn.execute(
        "SELECT actual_outcome, features_json FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND features_json IS NOT NULL"
    ).fetchall()
    import json as _j
    for r in rows:
        try:
            f = _j.loads(r["features_json"])
        except Exception:
            continue
        v = f.get(feature_name)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        yield v, r["actual_outcome"]


def _filter_threshold_signal(conn, feature_name, threshold,
                              tighten_band_factor=1.5):
    """Return (marginal_wr, marginal_n) for predictions whose
    `feature_name` value fell within `tighten_band_factor` of `threshold`
    on the just-passing side. (None, 0) if not enough data."""
    band_top = threshold * tighten_band_factor
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, feature_name):
        if threshold <= v <= band_top:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None, 0
    return (wins / total * 100.0), total


def _optimize_min_volume(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Raise min_volume when bottom-band entries (just above threshold)
    are losing badly. Lower when the system is starved of candidates and
    overall WR is healthy."""
    if not _safe_change_guarded(profile_id, "min_volume"):
        return None
    current = int(getattr(ctx, "min_volume", 500_000))
    wr, n = _filter_threshold_signal(conn, "volume", current,
                                       tighten_band_factor=1.5)
    if wr is None:
        return None
    if wr < 30:
        new_val = _bound("min_volume", int(current * 1.50))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, min_volume=new_val)
        reason = (
            f"Marginal-volume entries (≤ 1.5× threshold) WR {wr:.0f}% on "
            f"{n} samples — raise min_volume floor"
        )
        log_tuning_change(
            profile_id, user_id, "min_volume_raise",
            "min_volume", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Raised min_volume from {current:,} to {new_val:,} ({reason})"
    return None


def _optimize_volume_surge_multiplier(conn, ctx, profile_id, user_id,
                                       overall_wr, resolved):
    """Same shape, on volume_ratio (the multiple of average volume)."""
    if not _safe_change_guarded(profile_id, "volume_surge_multiplier"):
        return None
    current = float(getattr(ctx, "volume_surge_multiplier", 2.0))
    wr, n = _filter_threshold_signal(conn, "volume_ratio", current,
                                       tighten_band_factor=1.25)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("volume_surge_multiplier", round(current + 0.25, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, volume_surge_multiplier=new_val)
        reason = (
            f"Marginal volume-surge entries WR {wr:.0f}% on {n} samples — "
            f"require stronger surge for confirmation"
        )
        log_tuning_change(
            profile_id, user_id, "volume_surge_tighten",
            "volume_surge_multiplier", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised volume_surge_multiplier from {current:.2f} to "
                f"{new_val:.2f} ({reason})")
    return None


def _optimize_breakout_volume_threshold(conn, ctx, profile_id, user_id,
                                         overall_wr, resolved):
    """Tighten the breakout-volume gate when marginal breakouts fail."""
    if not _safe_change_guarded(profile_id, "breakout_volume_threshold"):
        return None
    current = float(getattr(ctx, "breakout_volume_threshold", 1.0))
    wr, n = _filter_threshold_signal(conn, "volume_ratio", current,
                                       tighten_band_factor=1.5)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("breakout_volume_threshold", round(current + 0.25, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, breakout_volume_threshold=new_val)
        reason = (
            f"Marginal-breakout entries WR {wr:.0f}% on {n} samples — "
            f"require more confirmation volume"
        )
        log_tuning_change(
            profile_id, user_id, "breakout_volume_tighten",
            "breakout_volume_threshold", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised breakout_volume_threshold from {current:.2f} to "
                f"{new_val:.2f} ({reason})")
    return None


def _optimize_gap_pct_threshold(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Increase the minimum gap when small-gap entries underperform."""
    if not _safe_change_guarded(profile_id, "gap_pct_threshold"):
        return None
    current = float(getattr(ctx, "gap_pct_threshold", 3.0))
    wr, n = _filter_threshold_signal(conn, "gap_pct", current,
                                       tighten_band_factor=1.2)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("gap_pct_threshold", round(current + 0.5, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, gap_pct_threshold=new_val)
        reason = (
            f"Marginal-gap entries (within 1.2× threshold) WR {wr:.0f}% "
            f"on {n} samples — require larger gap"
        )
        log_tuning_change(
            profile_id, user_id, "gap_threshold_tighten",
            "gap_pct_threshold", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised gap_pct_threshold from {current:.2f}% to "
                f"{new_val:.2f}% ({reason})")
    return None


def _optimize_momentum_5d(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Tighten 5d-momentum entry threshold when marginal entries fail."""
    if not _safe_change_guarded(profile_id, "momentum_5d_gain"):
        return None
    current = float(getattr(ctx, "momentum_5d_gain", 3.0))
    wr, n = _filter_threshold_signal(conn, "momentum_5d", current,
                                       tighten_band_factor=1.3)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("momentum_5d_gain", round(current + 0.5, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, momentum_5d_gain=new_val)
        reason = (
            f"Marginal 5d-momentum entries WR {wr:.0f}% on {n} samples — "
            f"require stronger momentum"
        )
        log_tuning_change(
            profile_id, user_id, "momentum_5d_tighten",
            "momentum_5d_gain", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised momentum_5d_gain from {current:.2f}% to "
                f"{new_val:.2f}% ({reason})")
    return None


def _optimize_momentum_20d(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """Same as 5d but for 20-day window."""
    if not _safe_change_guarded(profile_id, "momentum_20d_gain"):
        return None
    current = float(getattr(ctx, "momentum_20d_gain", 5.0))
    wr, n = _filter_threshold_signal(conn, "momentum_20d", current,
                                       tighten_band_factor=1.3)
    if wr is None:
        return None
    if wr < 35:
        new_val = _bound("momentum_20d_gain", round(current + 0.5, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, momentum_20d_gain=new_val)
        reason = (
            f"Marginal 20d-momentum entries WR {wr:.0f}% on {n} samples — "
            f"require stronger momentum"
        )
        log_tuning_change(
            profile_id, user_id, "momentum_20d_tighten",
            "momentum_20d_gain", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised momentum_20d_gain from {current:.2f}% to "
                f"{new_val:.2f}% ({reason})")
    return None


def _optimize_rsi_overbought(conn, ctx, profile_id, user_id,
                              overall_wr, resolved):
    """Raise the RSI overbought threshold when entries near the current
    threshold continued upward (i.e., the threshold was too sensitive)."""
    if not _safe_change_guarded(profile_id, "rsi_overbought"):
        return None
    current = float(getattr(ctx, "rsi_overbought", 85.0))
    # Bucket: predictions where rsi is within 5 points of current threshold
    band_lo, band_hi = current - 5, current + 5
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, "rsi"):
        if band_lo <= v <= band_hi:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None
    wr = wins / total * 100
    # If near-overbought entries still won often, the threshold is too tight.
    if wr >= 55:
        new_val = _bound("rsi_overbought", round(current + 2, 1))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, rsi_overbought=new_val)
        reason = (
            f"Near-overbought entries (RSI {band_lo:.0f}-{band_hi:.0f}) "
            f"won {wr:.0f}% on {total} samples — raise threshold"
        )
        log_tuning_change(
            profile_id, user_id, "rsi_overbought_raise",
            "rsi_overbought", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Raised rsi_overbought from {current:.0f} to "
                f"{new_val:.0f} ({reason})")
    return None


def _optimize_rsi_oversold(conn, ctx, profile_id, user_id,
                            overall_wr, resolved):
    """Lower the RSI oversold threshold when near-oversold entries continued
    downward (threshold too sensitive)."""
    if not _safe_change_guarded(profile_id, "rsi_oversold"):
        return None
    current = float(getattr(ctx, "rsi_oversold", 25.0))
    band_lo, band_hi = current - 5, current + 5
    wins = total = 0
    for v, outcome in _bucket_by_feature(conn, "rsi"):
        if band_lo <= v <= band_hi:
            total += 1
            if outcome == "win":
                wins += 1
    if total < 5:
        return None
    wr = wins / total * 100
    if wr >= 55:
        new_val = _bound("rsi_oversold", round(current - 2, 1))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, rsi_oversold=new_val)
        reason = (
            f"Near-oversold entries (RSI {band_lo:.0f}-{band_hi:.0f}) "
            f"won {wr:.0f}% on {total} samples — lower threshold"
        )
        log_tuning_change(
            profile_id, user_id, "rsi_oversold_lower",
            "rsi_oversold", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Lowered rsi_oversold from {current:.0f} to "
                f"{new_val:.0f} ({reason})")
    return None


# ---------------------------------------------------------------------------
# Wave 3 — exit parameter optimizers (Layer 1 Group B). Each rule reads
# from the trades table to analyze actual exit behavior and tune the
# parameters that control where stops and take-profits are placed.
# ---------------------------------------------------------------------------

def _optimize_short_take_profit(conn, ctx, profile_id, user_id,
                                  overall_wr, resolved):
    """Analogous to take_profit_pct rule but for shorts.
    Tighten when shorts hit TP and reversed (gave back gains);
    loosen when shorts ran well past TP before exit."""
    if not _safe_change_guarded(profile_id, "short_take_profit_pct"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    rows = conn.execute(
        "SELECT pnl, take_profit, price FROM trades "
        "WHERE pnl IS NOT NULL AND side IN ('short','cover') "
        "  AND take_profit IS NOT NULL AND price > 0"
    ).fetchall()
    if len(rows) < 5:
        return None
    current = float(getattr(ctx, "short_take_profit_pct", 0.08))
    # Compute average winner % vs TP target
    winning_pcts = []
    for r in rows:
        if r["pnl"] > 0 and r["take_profit"] and r["price"]:
            # For shorts, profit = (entry_price - cover_price) / entry_price
            # We approximate via pnl / (price * abs_qty), but simpler:
            # compare distance traveled to target distance.
            target_dist = abs(r["take_profit"] - r["price"]) / r["price"]
            if target_dist > 0:
                # We don't have the exit price, but pnl scaled by qty * price
                # gives % gain. Use a simpler heuristic: assume avg_winner_pct
                # = current * fraction_of_target_hit. With most TPs hit
                # exactly, fraction ≈ 1.0; lower fractions mean we exited early.
                # For tuning purposes: if many winners had small pnl relative
                # to target, the TP was too ambitious.
                winning_pcts.append(target_dist)
    if not winning_pcts:
        return None
    avg = sum(winning_pcts) / len(winning_pcts)
    # If average winner achieved < 50% of target, tighten TP (capture sooner)
    if avg < current * 0.5:
        new_val = _bound("short_take_profit_pct", round(current * 0.8, 4))
        if new_val >= current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, short_take_profit_pct=new_val)
        reason = (
            f"Short TP avg {avg*100:.1f}% < 50% of target "
            f"{current*100:.1f}% — tighten to capture sooner"
        )
        log_tuning_change(
            profile_id, user_id, "short_take_profit_tighten",
            "short_take_profit_pct", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Tightened short_take_profit_pct from {current:.0%} to "
                f"{new_val:.0%} ({reason})")
    return None


def _optimize_atr_multiplier_sl(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Widen ATR-based stop-loss when stops are being hit too tightly
    (>40% of losing trades cluster within 0.2× ATR of the stop). Only
    applies when use_atr_stops is on for the profile."""
    if not getattr(ctx, "use_atr_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "atr_multiplier_sl"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    # Heuristic without per-trade ATR: count losers where pnl was very
    # close to the stop's expected loss size. Without ATR per trade we
    # rely on a simpler proxy: many losses with small magnitude (-1 to -2%)
    # vs current stop suggests too-tight stops.
    rows = conn.execute(
        "SELECT pnl, price, qty FROM trades "
        "WHERE pnl IS NOT NULL AND pnl < 0 AND price > 0 AND qty > 0"
    ).fetchall()
    if len(rows) < 10:
        return None
    # Compute % loss per trade
    losses_pct = [abs(r["pnl"]) / (r["price"] * abs(r["qty"]))
                  for r in rows if r["price"] * abs(r["qty"]) > 0]
    if not losses_pct:
        return None
    # Count losses at the "near-stop" band — magnitude within 20% of
    # the largest loss (cluster at the stop).
    max_loss = max(losses_pct)
    if max_loss <= 0:
        return None
    near_stop = sum(1 for p in losses_pct if p >= max_loss * 0.8)
    near_stop_rate = near_stop / len(losses_pct)
    current = float(getattr(ctx, "atr_multiplier_sl", 2.0))
    if near_stop_rate >= 0.4:
        new_val = _bound("atr_multiplier_sl", round(current + 0.25, 2))
        if new_val == current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, atr_multiplier_sl=new_val)
        reason = (
            f"{near_stop_rate:.0%} of losses cluster near the stop — "
            f"widen ATR-stop multiplier to give trades more room"
        )
        log_tuning_change(
            profile_id, user_id, "atr_sl_widen",
            "atr_multiplier_sl", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Widened atr_multiplier_sl from {current:.2f} to "
                f"{new_val:.2f} ({reason})")
    return None


def _optimize_atr_multiplier_tp(conn, ctx, profile_id, user_id,
                                 overall_wr, resolved):
    """Tighten ATR take-profit when avg winner captures < 50% of the
    target distance. Mirror of the existing take_profit_pct rule."""
    if not getattr(ctx, "use_atr_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "atr_multiplier_tp"):
        return None
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not table_check:
        return None
    rows = conn.execute(
        "SELECT pnl, price, qty FROM trades "
        "WHERE pnl IS NOT NULL AND pnl > 0 AND price > 0 AND qty > 0"
    ).fetchall()
    if len(rows) < 10:
        return None
    wins_pct = [r["pnl"] / (r["price"] * abs(r["qty"]))
                for r in rows if r["price"] * abs(r["qty"]) > 0]
    if not wins_pct:
        return None
    max_win = max(wins_pct)
    if max_win <= 0:
        return None
    avg_win = sum(wins_pct) / len(wins_pct)
    # If avg winner is well below the largest winner achievable, the TP
    # may be set too far — tighten so we capture more consistent wins.
    if avg_win < max_win * 0.5:
        current = float(getattr(ctx, "atr_multiplier_tp", 3.0))
        new_val = _bound("atr_multiplier_tp", round(current - 0.25, 2))
        if new_val >= current:
            return None
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, atr_multiplier_tp=new_val)
        reason = (
            f"Avg winner {avg_win*100:.1f}% well under best winner "
            f"{max_win*100:.1f}% — tighten ATR-TP to capture more"
        )
        log_tuning_change(
            profile_id, user_id, "atr_tp_tighten",
            "atr_multiplier_tp", str(current), str(new_val), reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return (f"Tightened atr_multiplier_tp from {current:.2f} to "
                f"{new_val:.2f} ({reason})")
    return None


def _optimize_trailing_atr_multiplier(conn, ctx, profile_id, user_id,
                                        overall_wr, resolved):
    """Tighten trailing-stop multiplier when winning trades give back
    too much from peak before exit. We approximate 'give-back' via
    the spread between max favorable excursion and final pnl, which
    isn't tracked per-trade today — placeholder no-op until that
    column is added."""
    if not getattr(ctx, "use_trailing_stops", True):
        return None
    if not _safe_change_guarded(profile_id, "trailing_atr_multiplier"):
        return None
    # Placeholder: needs max_favorable_excursion or similar per-trade
    # tracking. Returns None gracefully.
    return None


def _optimize_maga_mode(conn, ctx, profile_id, user_id, overall_wr, resolved):
    """maga_mode binary: auto-disable if predictions made when ON
    materially underperform the global baseline. Auto-enable is left as
    a recommendation (per the no-recommendation-only allowlist's
    asymmetric-on-purpose rule for high-impact feature flags).

    For W1 binary auto-disable only. W4 (Layer 2) makes it weighted.
    """
    if not _safe_change_guarded(profile_id, "maga_mode"):
        return None

    if not getattr(ctx, "maga_mode", False):
        return None  # Already off; nothing to disable

    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(ai_predictions)").fetchall()}
    if "features_json" not in cols:
        return None

    # Bucket resolved predictions by whether features_json includes a
    # political-context signal.
    rows = conn.execute(
        "SELECT actual_outcome, features_json FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome IN ('win','loss') "
        "  AND features_json IS NOT NULL"
    ).fetchall()

    on_total = on_wins = 0
    for r in rows:
        try:
            import json as _j
            feats = _j.loads(r["features_json"]) if r["features_json"] else {}
        except Exception:
            continue
        if feats.get("political_context") or feats.get("maga_mode"):
            on_total += 1
            if r["actual_outcome"] == "win":
                on_wins += 1

    if on_total < 20:
        return None  # Not enough data to judge

    on_wr = on_wins / on_total * 100
    diff = on_wr - overall_wr
    # Auto-disable threshold: clearly underperforming the baseline.
    if diff <= -10:
        from models import update_trading_profile, log_tuning_change
        update_trading_profile(profile_id, maga_mode=0)
        reason = (
            f"Predictions with political context active won {on_wr:.0f}% "
            f"vs {overall_wr:.0f}% overall "
            f"({on_wins}/{on_total} samples) — disable"
        )
        log_tuning_change(
            profile_id, user_id, "maga_disable",
            "maga_mode", "1", "0", reason,
            win_rate_at_change=overall_wr, predictions_resolved=resolved,
        )
        return f"Disabled MAGA mode ({reason})"

    return None


# ---------------------------------------------------------------------------
# Batch context for AI-first pipeline
# ---------------------------------------------------------------------------

def _analyze_failure_patterns(db_path):
    """Find patterns in losing predictions — what conditions lead to losses.

    Queries ai_predictions joined with regime/strategy data to find patterns
    like "breakout signals in volatile markets: 15% win rate."

    Returns list of up to 5 pattern strings.
    """
    try:
        conn = _get_conn(db_path)
    except Exception:
        return []

    patterns = []
    try:
        # Check if regime/strategy columns exist
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_predictions)").fetchall()}
        has_regime = "regime_at_prediction" in cols
        has_strategy = "strategy_type" in cols

        if not has_regime and not has_strategy:
            conn.close()
            return []

        # Pattern 1: Win rate by regime
        if has_regime:
            rows = conn.execute(
                "SELECT regime_at_prediction, COUNT(*) as total, "
                "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                "FROM ai_predictions WHERE status='resolved' AND regime_at_prediction IS NOT NULL "
                "GROUP BY regime_at_prediction HAVING COUNT(*) >= 5"
            ).fetchall()
            overall = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) "
                "FROM ai_predictions WHERE status='resolved'"
            ).fetchone()
            overall_wr = (overall[1] / overall[0] * 100) if overall[0] > 0 else 50

            for r in rows:
                regime = r[0]
                total = r[1]
                wr = r[2] / total * 100
                if wr < overall_wr - 15:  # Significantly worse than average
                    patterns.append(
                        f"Predictions in {regime} markets: {wr:.0f}% win rate "
                        f"(vs {overall_wr:.0f}% overall, {total} trades). Be extra cautious."
                    )
                elif wr > overall_wr + 15:  # Significantly better
                    patterns.append(
                        f"Predictions in {regime} markets: {wr:.0f}% win rate "
                        f"(vs {overall_wr:.0f}% overall, {total} trades). This is your edge."
                    )

        # Pattern 2: Win rate by strategy type
        if has_strategy:
            from display_names import display_name as _dn
            rows = conn.execute(
                "SELECT strategy_type, COUNT(*) as total, "
                "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                "FROM ai_predictions WHERE status='resolved' AND strategy_type IS NOT NULL "
                "GROUP BY strategy_type HAVING COUNT(*) >= 5"
            ).fetchall()
            for r in rows:
                stype = _dn(r[0])
                total = r[1]
                wr = r[2] / total * 100
                if wr < 30:
                    patterns.append(
                        f"{stype} signals: {wr:.0f}% win rate ({total} trades). Avoid this pattern."
                    )
                elif wr > 60:
                    patterns.append(
                        f"{stype} signals: {wr:.0f}% win rate ({total} trades). Favor this pattern."
                    )

        # Pattern 3: Win rate by time of day (hour)
        rows = conn.execute(
            "SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, "
            "COUNT(*) as total, "
            "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
            "FROM ai_predictions WHERE status='resolved' "
            "GROUP BY hour HAVING COUNT(*) >= 5"
        ).fetchall()
        for r in rows:
            hour = r[0]
            total = r[1]
            wr = r[2] / total * 100
            if wr < 25:
                label = f"{hour}:00-{hour+1}:00"
                patterns.append(
                    f"Predictions at {label}: {wr:.0f}% win rate ({total} trades). "
                    f"Avoid trading this hour."
                )

        conn.close()
        return patterns[:5]  # Max 5 patterns

    except Exception as exc:
        logger.warning("Failed to analyze failure patterns: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return []


def get_batch_context_data(ctx, symbols=None):
    """Gather performance context for the AI batch prompt.

    Returns dict with:
        overall_win_rate: float or None
        total_resolved: int
        symbol_records: {symbol: {wins, losses, win_rate, avg_return}}
        profile_summary: str or None (1-line summary)
    """
    db_path = getattr(ctx, "db_path", None)
    if not db_path:
        return {"overall_win_rate": None, "total_resolved": 0,
                "symbol_records": {}, "profile_summary": None}

    try:
        conn = _get_conn(db_path)
        wr, total = _get_current_win_rate(conn)
        conn.close()
    except Exception:
        wr, total = 0.0, 0

    symbol_records = get_symbol_reputation(db_path, min_predictions=1)

    summary = None
    if total >= 5:
        quality = "Good accuracy." if wr >= 45 else "Be more selective."
        summary = f"Overall: {wr:.0f}% win rate ({total} resolved). {quality}"

    # Pattern learning — what conditions lead to wins/losses
    failure_patterns = _analyze_failure_patterns(db_path)

    return {
        "overall_win_rate": wr if total >= 5 else None,
        "total_resolved": total,
        "symbol_records": symbol_records,
        "profile_summary": summary,
        "learned_patterns": failure_patterns,
    }
