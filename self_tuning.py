"""Self-tuning feedback loop — feeds past performance into AI prompts."""

import logging
import sqlite3
import time
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


# ---------------------------------------------------------------------------
# build_performance_context
# ---------------------------------------------------------------------------

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

        lines = ["YOUR PREDICTION TRACK RECORD:"]
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

        # --- Symbol-specific history ---
        if symbol:
            sym_rows = conn.execute(
                "SELECT predicted_signal, actual_outcome, actual_return_pct "
                "FROM ai_predictions "
                "WHERE status='resolved' AND symbol=?",
                (symbol.upper(),),
            ).fetchall()
            if sym_rows:
                sym_buys = [r for r in sym_rows if r["predicted_signal"] == "BUY"]
                sym_sells = [r for r in sym_rows if r["predicted_signal"] == "SELL"]
                sym_buy_wins = sum(1 for r in sym_buys if r["actual_outcome"] == "win")
                sym_sell_wins = sum(1 for r in sym_sells if r["actual_outcome"] == "win")
                all_rets = [r["actual_return_pct"] or 0 for r in sym_rows]
                avg_ret = sum(all_rets) / len(all_rets) if all_rets else 0
                parts = []
                if sym_buys:
                    parts.append(
                        f"{len(sym_buys)} BUY(s) ({sym_buy_wins} win, "
                        f"{len(sym_buys) - sym_buy_wins} loss)"
                    )
                if sym_sells:
                    parts.append(
                        f"{len(sym_sells)} SELL(s) ({sym_sell_wins} win, "
                        f"{len(sym_sells) - sym_sell_wins} loss)"
                    )
                lines.append("")
                lines.append(
                    f"Your past predictions on {symbol.upper()}: "
                    f"{', '.join(parts)}, avg return {avg_ret:+.1f}%"
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


# ---------------------------------------------------------------------------
# get_auto_adjustments
# ---------------------------------------------------------------------------

def get_auto_adjustments(ctx, db_path=None):
    """Analyze performance data and return recommended parameter adjustments.

    Returns a dict with recommended changes and reasons.
    """
    db = db_path or (ctx.db_path if ctx else None)

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

        # Win rate by confidence band
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
                    result["confidence_threshold"] = threshold
                    result["reasons"].append(
                        f"Win rate at confidence {label} is {bwr:.0f}%, "
                        f"raising threshold to {threshold}"
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

        if overall_wr < 40:
            result["reasons"].append(
                f"Overall win rate ({overall_wr:.0f}%) below 40%, "
                f"recommend reducing position size"
            )

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

def apply_auto_adjustments(ctx, db_path=None):
    """Apply conservative auto-adjustments to the profile based on performance.

    Only adjusts if there are at least 20 resolved predictions.
    Returns list of adjustment descriptions.
    """
    if ctx is None:
        return []

    if not getattr(ctx, "enable_self_tuning", True):
        return []

    db = db_path or ctx.db_path
    adjustments_made = []

    try:
        conn = _get_conn(db)
    except Exception:
        return []

    try:
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
        ).fetchone()
        if not table_check:
            conn.close()
            return []

        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
        ).fetchone()[0]

        if resolved < 20:
            conn.close()
            return []

        profile_id = getattr(ctx, "profile_id", None)
        if not profile_id:
            conn.close()
            return []

        from models import update_trading_profile

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
                update_trading_profile(profile_id, ai_confidence_threshold=60)
                adjustments_made.append(
                    f"Raised AI confidence threshold from {ctx.ai_confidence_threshold} "
                    f"to 60 (win rate at <60% confidence was {wr60:.0f}%)"
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
                update_trading_profile(profile_id, ai_confidence_threshold=70)
                adjustments_made.append(
                    f"Raised AI confidence threshold to 70 "
                    f"(win rate at <70% confidence was {wr70:.0f}%)"
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

        # --- Overall win rate too low — reduce position size ---
        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        overall_wr = wins / resolved * 100

        if overall_wr < 30:
            new_pct = max(0.03, ctx.max_position_pct * 0.8)
            if new_pct < ctx.max_position_pct:
                update_trading_profile(profile_id, max_position_pct=round(new_pct, 4))
                adjustments_made.append(
                    f"Reduced max position size from {ctx.max_position_pct:.1%} "
                    f"to {new_pct:.1%} (overall win rate {overall_wr:.0f}%)"
                )

        conn.close()
        return adjustments_made

    except Exception as exc:
        logger.warning("Failed to apply auto adjustments: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return []
