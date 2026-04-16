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
        return {
            "can_tune": True, "resolved": resolved, "required": required,
            "message": f"Tuner is evaluating against {resolved} resolved predictions.",
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
            # --- Check win rate at confidence < 60 ---
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
                    # Check if raising threshold previously worsened things
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

            # --- Check win rate at confidence < 70 ---
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
                            f"Raised AI confidence threshold to 70 ({reason})"
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

        # --- Cross-profile learning: recommend confidence threshold from better profiles ---
        try:
            from models import get_user_profiles as _get_profiles
            other_profiles = _get_profiles(user_id)
            for other_prof in other_profiles:
                if other_prof["id"] == profile_id:
                    continue
                other_db = f"quantopsai_profile_{other_prof['id']}.db"
                try:
                    other_conn = _get_conn(other_db)
                    other_table = other_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
                    ).fetchone()
                    if not other_table:
                        other_conn.close()
                        continue
                    other_resolved = other_conn.execute(
                        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
                    ).fetchone()[0]
                    if other_resolved < 20:
                        other_conn.close()
                        continue
                    other_wins = other_conn.execute(
                        "SELECT COUNT(*) FROM ai_predictions "
                        "WHERE status='resolved' AND actual_outcome='win'"
                    ).fetchone()[0]
                    other_wr = (other_wins / other_resolved * 100) if other_resolved > 0 else 0
                    other_conn.close()

                    if other_wr - overall_wr >= 20:
                        other_threshold = other_prof.get("ai_confidence_threshold", 25)
                        adjustments_made.append(
                            f"Cross-profile suggestion: \"{other_prof['name']}\" has "
                            f"{other_wr:.0f}% win rate vs this profile's {overall_wr:.0f}%. "
                            f"Consider raising confidence threshold to match "
                            f"({other_threshold}) — not auto-applied (cross-profile)"
                        )
                except Exception:
                    continue
        except Exception as _cross_exc:
            logger.warning("Cross-profile auto-adjust check failed: %s", _cross_exc)

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

                # Recommend disabling shorts if overall negative P&L with 10+ trades and <20% win rate
                if short_cnt >= 10 and short_wr < 20 and short_pnl < 0:
                    adjustments_made.append(
                        f"Recommendation: DISABLE short selling — "
                        f"{short_wins}/{short_cnt} wins ({short_wr:.0f}%), "
                        f"total P&L ${short_pnl:,.0f}"
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
            rows = conn.execute(
                "SELECT strategy_type, COUNT(*) as total, "
                "SUM(CASE WHEN actual_outcome='win' THEN 1 ELSE 0 END) as wins "
                "FROM ai_predictions WHERE status='resolved' AND strategy_type IS NOT NULL "
                "GROUP BY strategy_type HAVING COUNT(*) >= 5"
            ).fetchall()
            for r in rows:
                stype = r[0]
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
