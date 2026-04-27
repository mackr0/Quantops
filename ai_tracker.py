"""Track AI prediction accuracy over time.

Records AI-generated BUY/SELL/HOLD predictions, resolves them against actual
price movements, and produces performance reports so we can measure whether the
AI is actually helping make money.
"""

import sqlite3
import logging
from datetime import datetime, timedelta

import config
from client import get_api
from market_data import get_bars

logger = logging.getLogger(__name__)

# --- Resolution thresholds ---
BUY_WIN_PCT = 2.0       # Price must rise >= 2% for a BUY to WIN
BUY_LOSS_PCT = 2.0      # Price drops >= 2% for a BUY to LOSS
SELL_WIN_PCT = 2.0       # Price must drop >= 2% for a SELL to WIN
SELL_LOSS_PCT = 2.0      # Price rises >= 2% for a SELL to LOSS
HOLD_MAX_CHANGE_PCT = 2.0  # HOLD is correct if abs(change) < 2%
HOLD_RESOLVE_DAYS = 3    # Trading days before resolving a HOLD prediction
TIMEOUT_DAYS = 10        # Max trading days before force-resolving as neutral

# Minimum trading days a prediction must age BEFORE we evaluate
# whether the price target was hit. Without this, BUY predictions
# made at 10am that drift +2% by 11am resolve as "win" within an
# hour — testing intraday noise rather than real signal. Wave 1 of
# the methodology fix plan introduces this gate so labels reflect
# meaningful forward-horizon outcomes. See METHODOLOGY_FIX_PLAN.md.
MIN_HOLD_DAYS_BEFORE_RESOLVE = 5  # 5 trading days ≈ 1 trading week


# ---------------------------------------------------------------------------
# Database helpers (mirrors journal.py patterns)
# ---------------------------------------------------------------------------

def _get_conn(db_path=None):
    """Get a connection to the journal database.

    Parameters
    ----------
    db_path : str, optional
        Path to the SQLite database file.  Falls back to config.DB_PATH
        when not provided (backward compat for CLI).
    """
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tracker_db(db_path=None):
    """Create the ai_predictions table if it doesn't exist."""
    conn = _get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            predicted_signal TEXT NOT NULL,
            confidence INTEGER,
            reasoning TEXT,
            price_at_prediction REAL NOT NULL,
            target_entry REAL,
            target_stop_loss REAL,
            target_take_profit REAL,
            status TEXT DEFAULT 'pending',
            actual_outcome TEXT,
            actual_return_pct REAL,
            resolved_at TEXT,
            resolution_price REAL,
            days_held INTEGER
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1. Record predictions
# ---------------------------------------------------------------------------

def record_prediction(symbol, predicted_signal, confidence, reasoning,
                      price_at_prediction, price_targets=None, db_path=None,
                      regime=None, strategy_type=None, features=None):
    """Save an AI prediction to the database.

    Parameters
    ----------
    symbol : str
        Ticker symbol.
    predicted_signal : str
        One of BUY, SELL, HOLD.
    confidence : int
        Confidence score 0-100.
    reasoning : str
        Free-text explanation from the AI.
    price_at_prediction : float
        Price when the prediction was made.
    price_targets : dict, optional
        May contain 'entry', 'stop_loss', 'take_profit'.
    db_path : str, optional
        Override database path.
    regime : str, optional
        Market regime at time of prediction (bull/bear/sideways/volatile).
    strategy_type : str, optional
        Which strategy generated the signal (e.g., "mean_reversion").
    features : dict, optional
        Full feature context the AI saw at prediction time (indicators, alt
        data, sector context, track record). Serialized to JSON and stored
        for the Phase 1 meta-model. See ROADMAP.md.

    Returns
    -------
    int
        Row id of the inserted prediction.
    """
    import json as _json

    init_tracker_db(db_path)

    # Guard: predictions with no valid price can never resolve.
    if not price_at_prediction or price_at_prediction <= 0:
        logger.warning(
            "Skipping prediction for %s: invalid price_at_prediction=%s",
            symbol, price_at_prediction,
        )
        return -1

    price_targets = price_targets or {}
    features_json = _json.dumps(features) if features else None

    conn = _get_conn(db_path)
    cursor = conn.execute(
        """INSERT INTO ai_predictions
           (timestamp, symbol, predicted_signal, confidence, reasoning,
            price_at_prediction, target_entry, target_stop_loss,
            target_take_profit, status, regime_at_prediction, strategy_type,
            features_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            symbol.upper(),
            predicted_signal.upper(),
            confidence,
            reasoning,
            price_at_prediction,
            price_targets.get("entry"),
            price_targets.get("stop_loss"),
            price_targets.get("take_profit"),
            regime,
            strategy_type,
            features_json,
        ),
    )
    conn.commit()
    prediction_id = cursor.lastrowid
    conn.close()
    logger.info(
        "Recorded AI prediction #%d: %s %s @ %.2f (confidence %d%%)",
        prediction_id, predicted_signal.upper(), symbol, price_at_prediction,
        confidence,
    )
    return prediction_id


# ---------------------------------------------------------------------------
# 2. Resolve predictions
# ---------------------------------------------------------------------------

def _get_current_price(symbol, api=None):
    """Fetch the latest price for a symbol.

    Primary: use the per-profile Alpaca REST client directly (already
    authenticated via UserContext credentials). This avoids the
    module-level market_data client which depends on env vars that may
    not be loaded.

    Fallback: market_data.get_bars (shared Alpaca data client → yfinance).
    """
    # Primary: Alpaca last trade via the profile's own API client
    if api is not None:
        try:
            trade = api.get_latest_trade(symbol)
            if trade and trade.price:
                return float(trade.price)
        except Exception:
            pass

    # Fallback: market_data pipeline (shared data client → yfinance)
    try:
        df = get_bars(symbol, limit=5)
        if df is not None and not df.empty:
            return float(df.iloc[-1]["close"])
    except Exception as exc:
        logger.warning("Could not fetch price for %s: %s", symbol, exc)

    return None


def _trading_days_since(timestamp_str):
    """Rough estimate of trading days elapsed since *timestamp_str*."""
    pred_dt = datetime.fromisoformat(timestamp_str)
    now = datetime.utcnow()
    calendar_days = (now - pred_dt).days
    # Approximate: 5 trading days per 7 calendar days
    return int(calendar_days * 5 / 7)


def _resolve_one(prediction, current_price):
    """Determine outcome for a single prediction.

    Returns (outcome, return_pct, days_held) or None if not yet resolvable.

    Wave 1 / Fix #6: enforces MIN_HOLD_DAYS_BEFORE_RESOLVE so a BUY
    that drifts +2% in an hour does NOT resolve as "win" same-day.
    Predictions only check the win/loss thresholds AFTER aging at
    least MIN_HOLD_DAYS_BEFORE_RESOLVE trading days — meaning the
    label captures whether the price target held over a meaningful
    forward horizon, not whether it was crossed by noise.
    """
    pred_price = prediction["price_at_prediction"]
    signal = prediction["predicted_signal"]
    days_elapsed = _trading_days_since(prediction["timestamp"])

    if pred_price is None or pred_price == 0:
        return None

    return_pct = ((current_price - pred_price) / pred_price) * 100.0

    # Forward-horizon gate. Without this, BUY/SELL predictions can
    # resolve to win/loss within hours of being made, testing noise
    # not signal. HOLD already had its own days-elapsed gate; we
    # extend the same discipline to BUY/SELL.
    if signal in ("BUY", "SELL") and days_elapsed < MIN_HOLD_DAYS_BEFORE_RESOLVE:
        return None

    if signal == "BUY":
        if return_pct >= BUY_WIN_PCT:
            return ("win", return_pct, days_elapsed)
        if return_pct <= -BUY_LOSS_PCT:
            return ("loss", return_pct, days_elapsed)
    elif signal == "SELL":
        # For SELL, a price drop is a win
        if return_pct <= -SELL_WIN_PCT:
            return ("win", return_pct, days_elapsed)
        if return_pct >= SELL_LOSS_PCT:
            return ("loss", return_pct, days_elapsed)
    elif signal == "HOLD":
        if days_elapsed >= HOLD_RESOLVE_DAYS:
            if abs(return_pct) < HOLD_MAX_CHANGE_PCT:
                return ("win", return_pct, days_elapsed)
            else:
                return ("loss", return_pct, days_elapsed)

    # Timeout: force-resolve after TIMEOUT_DAYS trading days as neutral
    if days_elapsed >= TIMEOUT_DAYS:
        return ("neutral", return_pct, days_elapsed)

    return None


def resolve_predictions(api=None, db_path=None):
    """Check all pending predictions and resolve those that meet criteria.

    Parameters
    ----------
    api : alpaca REST client, optional
        Pre-built API client.  Falls back to get_api() when not provided.
    db_path : str, optional
        Override database path.

    Returns the number of predictions resolved.
    """
    init_tracker_db(db_path)
    api = api or get_api()
    conn = _get_conn(db_path)

    pending = conn.execute(
        "SELECT * FROM ai_predictions WHERE status = 'pending'"
    ).fetchall()

    if not pending:
        conn.close()
        logger.info("No pending AI predictions to resolve.")
        return 0

    resolved_count = 0
    now_iso = datetime.utcnow().isoformat()

    # Collect unique symbols to minimize API calls
    symbols = list({row["symbol"] for row in pending})
    price_cache = {}
    for sym in symbols:
        price = _get_current_price(sym, api=api)
        if price is not None:
            price_cache[sym] = price

    for row in pending:
        sym = row["symbol"]
        if sym not in price_cache:
            continue

        current_price = price_cache[sym]
        result = _resolve_one(dict(row), current_price)
        if result is None:
            continue

        outcome, return_pct, days_held = result
        conn.execute(
            """UPDATE ai_predictions
               SET status = 'resolved',
                   actual_outcome = ?,
                   actual_return_pct = ?,
                   resolved_at = ?,
                   resolution_price = ?,
                   days_held = ?
               WHERE id = ?""",
            (outcome, round(return_pct, 4), now_iso, current_price,
             days_held, row["id"]),
        )
        resolved_count += 1
        # Wave 3 / Fix #9 — backfill specialist outcomes for this
        # prediction so the calibrators can learn from each
        # specialist's empirical accuracy. Treat 'win' as correct,
        # 'loss' as incorrect, 'neutral' as no-signal (not labeled —
        # we skip the calibration update for neutrals).
        if outcome in ("win", "loss"):
            try:
                from specialist_calibration import update_outcomes_on_resolve
                update_outcomes_on_resolve(
                    db_path, row["id"], was_correct=(outcome == "win"),
                )
            except Exception as _exc:
                logger.debug(
                    "Specialist calibration update failed for "
                    "prediction %d: %s", row["id"], _exc,
                )
        logger.info(
            "Resolved prediction #%d (%s %s): %s (%.2f%%, %d days)",
            row["id"], row["predicted_signal"], sym, outcome, return_pct,
            days_held,
        )

    conn.commit()
    conn.close()
    logger.info("Resolved %d / %d pending predictions.", resolved_count, len(pending))
    return resolved_count


# ---------------------------------------------------------------------------
# 3. Performance report
# ---------------------------------------------------------------------------

def get_ai_performance(db_path=None):
    """Build and return a performance report dict for AI predictions.

    Keys:
        total_predictions, resolved, pending,
        win_rate,
        avg_confidence_on_wins, avg_confidence_on_losses,
        avg_return_on_buys, avg_return_on_sells,
        accuracy_by_confidence,
        best_prediction, worst_prediction,
        profit_factor
    """
    init_tracker_db(db_path)
    conn = _get_conn(db_path)

    total = conn.execute("SELECT COUNT(*) FROM ai_predictions").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status = 'resolved'"
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status = 'pending'"
    ).fetchone()[0]

    if resolved == 0:
        conn.close()
        return {
            "total_predictions": total,
            "resolved": 0,
            "pending": pending,
            "win_rate": 0.0,
            "avg_confidence_on_wins": 0.0,
            "avg_confidence_on_losses": 0.0,
            "avg_return_on_buys": 0.0,
            "avg_return_on_sells": 0.0,
            "accuracy_by_confidence": {
                "0-25": 0.0, "25-50": 0.0, "50-75": 0.0, "75-100": 0.0,
            },
            "best_prediction": None,
            "worst_prediction": None,
            "profit_factor": 0.0,
        }

    # Win rate
    wins = conn.execute(
        "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved' AND actual_outcome='win'"
    ).fetchone()[0]
    win_rate = (wins / resolved) * 100.0 if resolved else 0.0

    # Average confidence on wins vs losses
    avg_conf_wins = conn.execute(
        "SELECT AVG(confidence) FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome='win'"
    ).fetchone()[0] or 0.0

    avg_conf_losses = conn.execute(
        "SELECT AVG(confidence) FROM ai_predictions "
        "WHERE status='resolved' AND actual_outcome='loss'"
    ).fetchone()[0] or 0.0

    # Average return by signal type
    avg_ret_buys = conn.execute(
        "SELECT AVG(actual_return_pct) FROM ai_predictions "
        "WHERE status='resolved' AND predicted_signal='BUY'"
    ).fetchone()[0] or 0.0

    avg_ret_sells = conn.execute(
        "SELECT AVG(actual_return_pct) FROM ai_predictions "
        "WHERE status='resolved' AND predicted_signal='SELL'"
    ).fetchone()[0] or 0.0

    # Accuracy by confidence band
    bands = {"0-25": (0, 25), "25-50": (25, 50), "50-75": (50, 75), "75-100": (75, 100)}
    accuracy_by_confidence = {}
    for label, (lo, hi) in bands.items():
        # Use <= 100 for the top band so confidence=100 is included
        hi_op = "<=" if hi == 100 else "<"
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
        accuracy_by_confidence[label] = (
            round((band_wins / band_total) * 100.0, 1) if band_total > 0 else 0.0
        )

    # Best and worst predictions
    best_row = conn.execute(
        "SELECT symbol, confidence, actual_return_pct "
        "FROM ai_predictions WHERE status='resolved' "
        "ORDER BY actual_return_pct DESC LIMIT 1"
    ).fetchone()
    worst_row = conn.execute(
        "SELECT symbol, confidence, actual_return_pct "
        "FROM ai_predictions WHERE status='resolved' "
        "ORDER BY actual_return_pct ASC LIMIT 1"
    ).fetchone()

    best_prediction = {
        "symbol": best_row["symbol"],
        "return_pct": best_row["actual_return_pct"],
        "confidence": best_row["confidence"],
    } if best_row else None

    worst_prediction = {
        "symbol": worst_row["symbol"],
        "return_pct": worst_row["actual_return_pct"],
        "confidence": worst_row["confidence"],
    } if worst_row else None

    # Profit factor: total_gains / abs(total_losses)
    total_gains = conn.execute(
        "SELECT COALESCE(SUM(actual_return_pct), 0) FROM ai_predictions "
        "WHERE status='resolved' AND actual_return_pct > 0"
    ).fetchone()[0]
    total_losses = abs(conn.execute(
        "SELECT COALESCE(SUM(actual_return_pct), 0) FROM ai_predictions "
        "WHERE status='resolved' AND actual_return_pct < 0"
    ).fetchone()[0])
    profit_factor = round(total_gains / total_losses, 2) if total_losses > 0 else float("inf")

    conn.close()

    return {
        "total_predictions": total,
        "resolved": resolved,
        "pending": pending,
        "win_rate": round(win_rate, 1),
        "avg_confidence_on_wins": round(avg_conf_wins, 1),
        "avg_confidence_on_losses": round(avg_conf_losses, 1),
        "avg_return_on_buys": round(avg_ret_buys, 2),
        "avg_return_on_sells": round(avg_ret_sells, 2),
        "accuracy_by_confidence": accuracy_by_confidence,
        "best_prediction": best_prediction,
        "worst_prediction": worst_prediction,
        "profit_factor": profit_factor,
    }


# ---------------------------------------------------------------------------
# 4. Rolling win-rate timeseries (for charting)
# ---------------------------------------------------------------------------

def compute_rolling_win_rate(db_paths, window_days=7, lookback_days=60):
    """Build a daily rolling win-rate series from resolved predictions.

    For each calendar day in the lookback window, compute the win rate
    over the trailing `window_days`. Days with no resolved predictions
    in their window are skipped (returned as None for win_rate so the
    caller can choose to break the line or interpolate).

    Parameters
    ----------
    db_paths : iterable of str
        Per-profile sqlite paths. Aggregated across all of them.
    window_days : int
        Trailing window size in calendar days for each rolling point.
    lookback_days : int
        How many days back from today to compute points for.

    Returns
    -------
    list of dict
        [{date: "YYYY-MM-DD", win_rate: float|None, n: int}, ...]
        sorted oldest -> newest. `n` is the number of resolved
        predictions inside that day's window.
    """
    from datetime import date, datetime, timedelta
    import sqlite3 as _sqlite3

    today = date.today()
    earliest = today - timedelta(days=lookback_days + window_days)

    # Pull all (resolved_at, outcome) tuples from each DB inside the
    # earliest-needed range.
    resolutions = []
    for db_path in db_paths:
        try:
            conn = _sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT resolved_at, actual_outcome FROM ai_predictions "
                "WHERE status = 'resolved' AND resolved_at IS NOT NULL "
                "  AND actual_outcome IN ('win', 'loss') "
                "  AND date(resolved_at) >= ?",
                (earliest.isoformat(),),
            ).fetchall()
            conn.close()
            for r in rows:
                # Parse the date portion only — outcome is bucketed daily.
                try:
                    d = datetime.fromisoformat(r[0].replace("Z", "")).date()
                except Exception:
                    d = datetime.strptime(r[0][:10], "%Y-%m-%d").date()
                resolutions.append((d, r[1]))
        except Exception:
            continue

    # Bucket by day for fast windowed sums.
    by_day_wins = {}
    by_day_losses = {}
    for d, outcome in resolutions:
        if outcome == "win":
            by_day_wins[d] = by_day_wins.get(d, 0) + 1
        elif outcome == "loss":
            by_day_losses[d] = by_day_losses.get(d, 0) + 1

    series = []
    for offset in range(lookback_days, -1, -1):
        end = today - timedelta(days=offset)
        start = end - timedelta(days=window_days - 1)
        wins = sum(by_day_wins.get(start + timedelta(days=i), 0)
                   for i in range(window_days))
        losses = sum(by_day_losses.get(start + timedelta(days=i), 0)
                     for i in range(window_days))
        n = wins + losses
        win_rate = (wins / n * 100.0) if n > 0 else None
        series.append({
            "date": end.isoformat(),
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "n": n,
        })
    return series


# ---------------------------------------------------------------------------
# 5. Pretty-print report
# ---------------------------------------------------------------------------

def print_ai_report():
    """Print a formatted AI prediction performance report to stdout."""
    perf = get_ai_performance()

    print("\n" + "=" * 60)
    print("  AI PREDICTION PERFORMANCE REPORT")
    print("=" * 60)

    print(f"\n  Total predictions:  {perf['total_predictions']}")
    print(f"  Resolved:           {perf['resolved']}")
    print(f"  Pending:            {perf['pending']}")

    if perf["resolved"] == 0:
        print("\n  No resolved predictions yet. Check back later.")
        print("=" * 60 + "\n")
        return

    print(f"\n  Win rate:           {perf['win_rate']:.1f}%")
    print(f"  Profit factor:      {perf['profit_factor']}")

    print("\n  --- Confidence calibration ---")
    print(f"  Avg confidence on wins:    {perf['avg_confidence_on_wins']:.1f}")
    print(f"  Avg confidence on losses:  {perf['avg_confidence_on_losses']:.1f}")

    print("\n  --- Returns by signal ---")
    print(f"  Avg return on BUY calls:   {perf['avg_return_on_buys']:+.2f}%")
    print(f"  Avg return on SELL calls:  {perf['avg_return_on_sells']:+.2f}%")

    print("\n  --- Accuracy by confidence band ---")
    for band, acc in perf["accuracy_by_confidence"].items():
        bar = "#" * int(acc / 5) if acc > 0 else "-"
        print(f"  {band:>6}:  {acc:5.1f}%  {bar}")

    if perf["best_prediction"]:
        bp = perf["best_prediction"]
        print(f"\n  Best prediction:  {bp['symbol']} "
              f"({bp['confidence']}% conf) -> {bp['return_pct']:+.2f}%")

    if perf["worst_prediction"]:
        wp = perf["worst_prediction"]
        print(f"  Worst prediction: {wp['symbol']} "
              f"({wp['confidence']}% conf) -> {wp['return_pct']:+.2f}%")

    print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "resolve":
        n = resolve_predictions()
        print(f"Resolved {n} prediction(s).")
    else:
        print_ai_report()
