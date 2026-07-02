"""Track AI prediction accuracy over time.

Records AI-generated BUY/SELL/HOLD predictions, resolves them against actual
price movements, and produces performance reports so we can measure whether the
AI is actually helping make money.
"""

import sqlite3
import logging
from contextlib import closing
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
# An exit (SELL on a held long, or cover on a held short) is a "good
# call" if the price didn't move materially against the exit direction
# afterward. EXIT_BUFFER_PCT is how much the price can move "against"
# us before we judge the exit as a missed-opportunity.
EXIT_BUFFER_PCT = 2.0

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
    # See models._get_conn — busy_timeout eliminates transient-lock
    # OperationalError on concurrent reader/writer races.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def backfill_prediction_type(db_path=None):
    """Set prediction_type on existing rows that don't have it yet.

    Rules:
      BUY / HOLD                 -> directional_long
      SHORT                      -> directional_short
      SELL with reasoning text containing 'exit'/'close existing'/'sell existing'
                                 -> exit_long  (AI was suggesting to exit a held position)
      SELL otherwise             -> directional_short  (legacy semantic;
                                    matches what the old resolver assumed)

    Idempotent — only updates rows where prediction_type IS NULL.
    Returns counts dict for reporting.
    """
    init_tracker_db(db_path)
    conn = _get_conn(db_path)
    counts = {"directional_long": 0, "directional_short": 0,
              "exit_long": 0, "option_open": 0, "skipped": 0}
    try:
        # BUY and HOLD → directional_long
        c = conn.execute(
            "UPDATE ai_predictions SET prediction_type='directional_long' "
            "WHERE prediction_type IS NULL "
            "AND predicted_signal IN ('BUY', 'HOLD', 'STRONG_BUY')"
        )
        counts["directional_long"] += c.rowcount
        # Option opens → option_open (P0 2026-07-01) — a distinct expression,
        # never conflated with stock longs (see classify_prediction_type).
        c = conn.execute(
            "UPDATE ai_predictions SET prediction_type='option_open' "
            "WHERE prediction_type IS NULL "
            "AND predicted_signal IN ('MULTILEG_OPEN', 'OPTIONS', "
            "                         'OPTION_EXERCISE')"
        )
        counts["option_open"] += c.rowcount
        # SHORT → directional_short
        c = conn.execute(
            "UPDATE ai_predictions SET prediction_type='directional_short' "
            "WHERE prediction_type IS NULL "
            "AND predicted_signal IN ('SHORT', 'STRONG_SHORT')"
        )
        counts["directional_short"] += c.rowcount
        # SELL with exit-y reasoning → exit_long
        c = conn.execute(
            "UPDATE ai_predictions SET prediction_type='exit_long' "
            "WHERE prediction_type IS NULL "
            "AND predicted_signal IN ('SELL', 'STRONG_SELL') "
            "AND ("
            "  LOWER(reasoning) LIKE '%exit%' OR "
            "  LOWER(reasoning) LIKE '%close existing%' OR "
            "  LOWER(reasoning) LIKE '%sell existing%' OR "
            "  LOWER(reasoning) LIKE '%lock in%' OR "
            "  LOWER(reasoning) LIKE '%take profit%'"
            ")"
        )
        counts["exit_long"] += c.rowcount
        # Remaining SELL → directional_short (legacy semantic)
        c = conn.execute(
            "UPDATE ai_predictions SET prediction_type='directional_short' "
            "WHERE prediction_type IS NULL "
            "AND predicted_signal IN ('SELL', 'STRONG_SELL')"
        )
        counts["directional_short"] += c.rowcount
        conn.commit()
    finally:
        conn.close()
    return counts


def init_tracker_db(db_path=None):
    """Create the ai_predictions table if it doesn't exist."""
    with closing(_get_conn(db_path)) as conn:
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
                days_held INTEGER,
                prediction_type TEXT
            );
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Record predictions
# ---------------------------------------------------------------------------

def record_prediction(symbol, predicted_signal, confidence, reasoning,
                      price_at_prediction, price_targets=None, db_path=None,
                      regime=None, strategy_type=None, features=None,
                      prediction_type=None,
                      # 2026-05-19 Phase B1 — fine-tune-quality fields
                      cycle_id=None, prompt_text=None,
                      raw_response=None, meta_model_score=None,
                      online_meta_score=None,
                      # 2026-05-20 #185 — deterministic-panel snapshot
                      rule_votes=None):
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
    cycle_id : str, optional
        UUID of the parent ai_cycles row this prediction was made in. Lets
        training data reconstruct the cross-candidate context (what other
        candidates were in the same prompt, their relative ranks).
    prompt_text : str, optional
        The exact prompt the AI saw. Critical for fine-tuning — without
        this the training input must be reconstructed from features and
        loses whatever the prompt-builder added (RAG injections, panel
        renders, market-context blocks).
    raw_response : dict, optional
        The AI's full response dict (not just parsed action+reasoning).
        Serialized to JSON for storage.
    meta_model_score : float, optional
        Pre-gate P(correct) at decision time from the GBM meta-model.
    online_meta_score : float, optional
        Online SGD meta-model score at decision time (catches regime drift).
    rule_votes : list[dict], optional
        Snapshot of the deterministic-panel verdicts that fired for this
        candidate at prediction time. Each entry: {name, severity,
        direction}. Serialized to JSON and stored on ai_predictions
        for #185 — lets the fine-tune dataset builder join firing rules
        to multi-horizon outcomes (rule X, fired in direction Y, was
        followed by what return at 1d/5d/20d).

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
    raw_response_json = (
        _json.dumps(raw_response) if raw_response is not None else None
    )
    # Normalize rule_votes to a JSON string. Accept either the raw
    # output of `deterministic_specialists.run_panel` (list of dicts
    # with name/severity/reasoning) or the trimmed-for-storage shape
    # (name/severity/direction). We keep only name + severity +
    # direction at write time — reasoning text is reconstructable
    # from rerunning the rule against features and bloats the row.
    rule_votes_json = None
    if rule_votes:
        try:
            trimmed = [
                {
                    "name": rv.get("name"),
                    "severity": rv.get("severity"),
                    "direction": rv.get("direction"),
                }
                for rv in rule_votes
                if isinstance(rv, dict) and rv.get("name")
            ]
            if trimmed:
                rule_votes_json = _json.dumps(trimmed)
        except (TypeError, ValueError) as _rv_exc:
            logger.debug(
                "record_prediction: rule_votes serialization failed (%s: %s); "
                "storing NULL",
                type(_rv_exc).__name__, _rv_exc,
            )

    with closing(_get_conn(db_path)) as conn:
        cursor = conn.execute(
            """INSERT INTO ai_predictions
               (timestamp, symbol, predicted_signal, confidence, reasoning,
                price_at_prediction, target_entry, target_stop_loss,
                target_take_profit, status, regime_at_prediction, strategy_type,
                features_json, prediction_type,
                cycle_id, prompt_text, raw_response_json,
                meta_model_score, online_meta_score,
                rule_votes_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
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
                prediction_type,
                cycle_id,
                prompt_text,
                raw_response_json,
                meta_model_score,
                online_meta_score,
                rule_votes_json,
            ),
        )
        conn.commit()
        prediction_id = cursor.lastrowid
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
        except (AttributeError, ValueError, TypeError, OSError,
                ConnectionError, TimeoutError, ImportError) as _alt_exc:
            # Alpaca latest-trade fallback; falls through to
            # market_data path below. Surface for follow-up.
            logger.debug(
                "ai_tracker Alpaca latest-trade fallback: %s: %s",
                type(_alt_exc).__name__, _alt_exc,
            )

    # Fallback: market_data pipeline (shared data client → yfinance)
    try:
        df = get_bars(symbol, limit=5)
        if df is not None and not df.empty:
            return float(df.iloc[-1]["close"])
    except Exception as exc:
        logger.warning("Could not fetch price for %s: %s", symbol, exc)

    return None


def _get_current_prices_bulk(symbols, api=None):
    """Bulk-fetch latest prices for many symbols in as few API calls as
    possible. Returns dict[symbol] = float (only present when fetched).

    Critical for resolve_predictions on a long-weekend backlog: scanning
    800+ pending predictions one symbol at a time hits Alpaca with
    ~200-300 sequential REST calls, taking 15-30s per profile. The
    snapshot endpoint returns ALL prices for a symbol list in ONE call.

    Fallback to per-symbol fetch if the bulk endpoint isn't available
    or errors out.
    """
    if not symbols:
        return {}
    out = {}
    if api is not None:
        try:
            # Alpaca's snapshot endpoint accepts a comma-separated
            # symbol list; the SDK maps that to get_snapshots(symbols=).
            # Returns dict[symbol] -> Snapshot with .latest_trade etc.
            snapshots = api.get_snapshots(symbols)
            if snapshots:
                for sym, snap in snapshots.items():
                    if snap is None:
                        continue
                    trade = getattr(snap, "latest_trade", None)
                    if trade is not None and getattr(trade, "price", None):
                        out[sym] = float(trade.price)
                if out:
                    return out
        except Exception as exc:
            logger.debug("Bulk snapshot failed (%s); falling back to per-symbol", exc)

    # Fallback path — slow but correct when bulk path fails
    for sym in symbols:
        if sym in out:
            continue
        p = _get_current_price(sym, api=api)
        if p is not None:
            out[sym] = p
    return out


def _trading_days_since(timestamp_str):
    """Rough estimate of trading days elapsed since *timestamp_str*."""
    pred_dt = datetime.fromisoformat(timestamp_str)
    now = datetime.utcnow()
    calendar_days = (now - pred_dt).days
    # Approximate: 5 trading days per 7 calendar days
    return int(calendar_days * 5 / 7)


_OPTION_SIGNALS = frozenset({"MULTILEG_OPEN", "OPTIONS",
                              "OPTION_EXERCISE"})


def classify_prediction_type(signal, held_qty=0.0):
    """Classify a prediction for STATS + meta-model attribution.

    P0 of the selection-engine design (2026-07-01): option opens are a DISTINCT
    expression ("option_open") and must never be conflated with stock longs —
    per-expression scoring/feedback depends on the split. Resolution itself is
    P&L-based via the option_resolver (keyed on the SIGNAL, not this label), so
    this only drives stats. SELL is exit-vs-directional by whether we hold.
    See docs/SELECTION_ENGINE_DESIGN.md.
    """
    s = (signal or "").upper()
    if s == "BUY":
        return "directional_long"
    if s == "SHORT":
        return "directional_short"
    if s in _OPTION_SIGNALS:
        return "option_open"
    if s == "SELL":
        if held_qty > 0:
            return "exit_long"
        if held_qty < 0:
            return "exit_short"
        return "directional_short"   # SELL on unheld = directional bearish
    return "directional_long"        # HOLD / unknown — neutral


def _estimate_round_trip_cost_pct(prediction, db_path):
    """Estimate the % cost (slippage) of a round-trip trade matching
    this prediction. Used to compute actual_return_pct_net (#186
    Phase A, 2026-05-20).

    Look-up strategy:
      1. Match the prediction to an entry trade row by (symbol +
         predicted side + timestamp within +/- 10 min). Take its
         slippage_pct.
      2. Round-trip estimate = 2 × entry_slippage_pct (assumes
         symmetric exit slippage — coarse but a defensible first
         cut; refine when we instrument exit-side fill timing).

    Returns the % cost (always non-negative for sane data). Returns
    0.0 when no matching trade is found (so net == gross — better
    than NULL-ing the column for legacy / unmatched rows).

    Honest caveat: this is an APPROXIMATION. For predictions that
    never traded (AI said BUY but pre-filter / blacklist / cash
    blocked the entry), cost is genuinely 0 — the prediction is
    purely a directional bet on paper. For trades that did execute,
    the 2× entry-slippage assumption may over- or under-estimate
    depending on the actual exit market state. Better than nothing;
    iterates later as data on exit slippage accumulates.
    """
    if not db_path:
        return 0.0
    signal = (prediction.get("predicted_signal") or "").upper()
    if signal in _OPTION_SIGNALS:
        # Option resolver already operates on premium prices directly
        # (not underlying); slippage is implicit in the premium fill.
        # First cut: zero out, refine later with option-specific
        # commission ($0.65/contract × contracts) and bid-ask spread.
        return 0.0
    side = "buy" if signal in ("BUY", "STRONG_BUY") else "sell"
    # Narrow exception scope: only sqlite3.Error caught here. A bad
    # `prediction` dict (missing keys) is a caller bug and should
    # raise loudly; only DB-level failures fall through to the
    # gross=net fallback. Logged at WARNING so the operator sees it
    # in journal-tails — silently returning 0.0 on a real DB problem
    # would hide cost-tracking breakage from the self-tuner.
    with closing(_get_conn(db_path)) as conn:
        try:
            row = conn.execute(
                "SELECT slippage_pct FROM trades "
                "WHERE symbol = ? AND side = ? "
                "  AND slippage_pct IS NOT NULL "
                "  AND ABS(julianday(timestamp) - julianday(?)) <= (10.0 / (24*60)) "
                "ORDER BY ABS(julianday(timestamp) - julianday(?)) ASC "
                "LIMIT 1",
                (prediction["symbol"], side, prediction["timestamp"],
                 prediction["timestamp"]),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "round-trip-cost lookup DB failure for %s/%s: %s: %s — "
                "falling back to gross=net for this prediction",
                prediction["symbol"], signal, type(exc).__name__, exc,
            )
            return 0.0
    if not row or row[0] is None:
        return 0.0
    entry_slip_pct = abs(float(row[0]))
    # 2x to model symmetric round-trip cost. Exit slippage is not yet
    # captured separately at resolve time for stocks.
    return entry_slip_pct * 2.0


# ---------------------------------------------------------------------------
# 2026-05-20 #185 — Multi-horizon outcomes for the fine-tune dataset
# ---------------------------------------------------------------------------

# Five horizons capture different timescales. 1d = intraday-momentum
# (closest proxy for "did the trade make money" since most positions
# close within 1-2 days). 3d / 5d catch weekly mean-reversion patterns.
# 10d / 20d catch monthly trend. Adding a new horizon is a one-line
# change here plus a single backfill — no schema migration required
# (the outcomes table is keyed by (prediction_id, horizon_days), so a
# new horizon is just a new set of rows).
HORIZON_DAYS = (1, 3, 5, 10, 20)


def _classify_outcome(return_pct):
    """Categorical outcome label for the fine-tune dataset.

    Five-class scheme lets the trainer use cross-entropy loss
    directly without re-deriving thresholds in training code.
    Thresholds chosen for daily stock returns and are asymmetric
    around zero (the gain/loss boundaries are symmetric at ±1%
    and ±5% — keeping the trainer's label distribution balanced
    is more important than skewing thresholds to favor loss
    aversion, which is a portfolio-level concern not a per-trade
    label concern).
    """
    if return_pct is None:
        return None
    if return_pct >= 5.0:
        return "big_win"
    if return_pct >= 1.0:
        return "win"
    # Strict boundary on the loss side: -1.0 maps to "loss" (not
    # "flat") so a 1% loss doesn't get hidden in the neutral bucket.
    # Likewise -5.0 maps to "big_loss". Asymmetric but intentional:
    # the trainer benefits from labels that don't blur small losses
    # into "no signal."
    if return_pct > -1.0:
        return "flat"
    if return_pct > -5.0:
        return "loss"
    return "big_loss"


def _measure_one_prediction(conn, pred, bars, db_path, now_iso):
    """Fill in any missing horizon outcome rows for one prediction.

    `bars` is a DataFrame returned by market_data.get_bars_daterange
    for this prediction's symbol covering [pred_date, today]. Indexed
    by timestamp in US/Eastern.

    Returns the count of rows written.
    """
    pred_id = pred["id"]
    entry_price = float(pred["price_at_prediction"])
    if entry_price <= 0:
        return 0
    try:
        pred_dt = datetime.fromisoformat(pred["timestamp"])
    except ValueError:
        return 0
    signal = (pred["predicted_signal"] or "").upper()
    is_short = signal in ("SELL", "SHORT")

    existing = {
        row[0] for row in conn.execute(
            "SELECT horizon_days FROM ai_prediction_outcomes "
            "WHERE prediction_id = ?",
            (pred_id,),
        ).fetchall()
    }
    if len(existing) >= len(HORIZON_DAYS):
        return 0

    if bars is None or bars.empty:
        return 0
    bars = bars.sort_index()
    # Locate the entry-day bar: first bar whose date >= prediction date.
    # Compare via .date() to sidestep timezone-vs-naive datetime
    # comparison surprises (bars are tz-aware US/Eastern; pred_dt is
    # naive UTC).
    entry_date = pred_dt.date()
    entry_idx = None
    for i, ts in enumerate(bars.index):
        try:
            bar_date = ts.date()
        except AttributeError:
            continue
        if bar_date >= entry_date:
            entry_idx = i
            break
    if entry_idx is None:
        return 0

    cost_pct = _estimate_round_trip_cost_pct(
        {
            "symbol": pred["symbol"],
            "predicted_signal": signal,
            "timestamp": pred["timestamp"],
        },
        db_path,
    )

    written = 0
    for horizon in HORIZON_DAYS:
        if horizon in existing:
            continue
        target_idx = entry_idx + horizon
        if target_idx >= len(bars):
            # Horizon hasn't elapsed (not enough trading days of bar
            # history yet). Skip — next cycle will try again.
            continue

        horizon_bar = bars.iloc[target_idx]
        try:
            exit_price = float(horizon_bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        return_pct = ((exit_price - entry_price) / entry_price) * 100.0
        if is_short:
            return_pct = -return_pct

        # MFE / MAE over the window (entry+1 → horizon, inclusive).
        # Signed by DIRECTION so positive MFE always means "the
        # prediction was right at some point" regardless of long/short.
        mfe_pct = mae_pct = None
        window = bars.iloc[entry_idx + 1 : target_idx + 1]
        if not window.empty and "high" in window.columns and "low" in window.columns:
            try:
                highs = window["high"].astype(float)
                lows = window["low"].astype(float)
                if is_short:
                    mfe_pct = ((entry_price - lows.min())
                                / entry_price) * 100.0
                    mae_pct = -((highs.max() - entry_price)
                                 / entry_price) * 100.0
                else:
                    mfe_pct = ((highs.max() - entry_price)
                                / entry_price) * 100.0
                    mae_pct = -((entry_price - lows.min())
                                 / entry_price) * 100.0
            except (ValueError, TypeError) as _exc:
                logger.debug(
                    "MFE/MAE compute failed for pred %s horizon %sd: %s",
                    pred_id, horizon, _exc,
                )

        net_pct = return_pct - cost_pct
        outcome_class = _classify_outcome(return_pct)

        cursor = conn.execute(
            """INSERT OR IGNORE INTO ai_prediction_outcomes
               (prediction_id, horizon_days, price_at_horizon,
                return_pct, return_pct_net, mfe_pct, mae_pct,
                outcome_class, measured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pred_id, horizon, exit_price,
                round(return_pct, 4), round(net_pct, 4),
                round(mfe_pct, 4) if mfe_pct is not None else None,
                round(mae_pct, 4) if mae_pct is not None else None,
                outcome_class, now_iso,
            ),
        )
        if cursor.rowcount > 0:
            written += 1

    return written


def measure_horizon_outcomes(api=None, db_path=None,
                              lookback_calendar_days=35):
    """Walk recent predictions and fill in any multi-horizon outcome
    rows whose horizon has elapsed.

    Designed to be called every cycle alongside resolve_predictions.
    Each prediction is touched at most 5 times total over its 20d life
    (once per horizon); 90% of calls per cycle are no-ops because the
    next horizon hasn't elapsed yet. Idempotent via the UNIQUE
    (prediction_id, horizon_days) constraint.

    Stock signals only. Option signals are deferred — premium % moves
    don't fit the 1d/3d/5d/10d/20d horizon model used here (a multileg
    spread's premium can swing 30% on day 1 from bid/ask alone). The
    existing option_resolver in pipelines/outcomes handles those.

    Parameters
    ----------
    api : alpaca REST client, optional
        Unused here (bars are fetched via market_data.get_bars_daterange
        which uses its own client) — kept for signature parity with
        resolve_predictions and future use.
    db_path : str, optional
        Override database path.
    lookback_calendar_days : int
        How far back to scan. Default 35 = ~25 trading days, so the
        20d horizon for the oldest prediction in scope can be filled.

    Returns the number of new outcome rows written.
    """
    from market_data import get_bars_daterange

    init_tracker_db(db_path)
    conn = _get_conn(db_path)
    try:
        cutoff = (datetime.utcnow()
                  - timedelta(days=lookback_calendar_days)).isoformat()
        rows = conn.execute(
            """SELECT p.id, p.symbol, p.timestamp, p.price_at_prediction,
                      p.predicted_signal
               FROM ai_predictions p
               WHERE p.timestamp >= ?
                 AND p.price_at_prediction > 0
                 AND COALESCE(p.predicted_signal, '') NOT IN
                     ('MULTILEG_OPEN','OPTIONS','OPTION_EXERCISE')
               ORDER BY p.timestamp ASC""",
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0

        # Group by symbol so each symbol's bars are fetched once and
        # shared across all its predictions in this window.
        by_symbol = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)

        written = 0
        now = datetime.utcnow()
        now_iso = now.isoformat()
        today_iso = now.date().isoformat()

        for symbol, preds in by_symbol.items():
            # Skip the symbol entirely if every prediction already
            # has all 5 horizons — saves the bar fetch.
            pred_ids = [p["id"] for p in preds]
            placeholders = ",".join("?" * len(pred_ids))
            done_count = conn.execute(
                f"SELECT prediction_id, COUNT(*) AS n "
                f"FROM ai_prediction_outcomes "
                f"WHERE prediction_id IN ({placeholders}) "
                f"GROUP BY prediction_id "
                f"HAVING n >= ?",
                (*pred_ids, len(HORIZON_DAYS)),
            ).fetchall()
            done_pred_ids = {row[0] for row in done_count}
            pending = [p for p in preds if p["id"] not in done_pred_ids]
            if not pending:
                continue

            oldest_ts = min(p["timestamp"] for p in pending)
            try:
                start_dt = datetime.fromisoformat(oldest_ts)
            except ValueError as _exc:
                # A malformed timestamp on a prediction row is a real
                # data-integrity problem (the writer is supposed to
                # always write ISO format). Skip this symbol but log
                # LOUDLY so the operator sees it — silently dropping
                # would hide the corruption.
                logger.warning(
                    "measure_horizon_outcomes: bad timestamp %r on a "
                    "prediction for %s (%s) — skipping symbol; fix the "
                    "writer that produced this row.",
                    oldest_ts, symbol, _exc,
                )
                continue
            start = start_dt.date().isoformat()
            # Narrow exception scope. get_bars_daterange is documented
            # to return an empty DataFrame on missing-data (caught by
            # _measure_one_prediction); the exceptions that actually
            # escape are network/IO faults from the underlying Alpaca
            # / yfinance clients. Log at WARNING so flaky data sources
            # are visible — the alternative ("silently miss this
            # cycle's horizon row") is the silent-failure pattern.
            try:
                bars = get_bars_daterange(symbol, start, today_iso)
            except (ConnectionError, TimeoutError, OSError,
                    ValueError, KeyError, AttributeError) as _exc:
                logger.warning(
                    "measure_horizon_outcomes: bar fetch for %s failed "
                    "(%s: %s) — horizon rows for this symbol will be "
                    "retried next cycle.",
                    symbol, type(_exc).__name__, _exc,
                )
                continue

            for p in pending:
                # No try/except around _measure_one_prediction: it
                # already catches its OWN narrow exceptions (bad bar
                # data, missing columns) internally and surfaces only
                # via the logged sub-paths. If something escapes from
                # here, it's a programming bug — let it raise so the
                # per-symbol commit below doesn't silently bury it
                # under "everything seemed fine." The outer task
                # handler (_task_resolve_predictions) will log and
                # continue the scheduler loop.
                written += _measure_one_prediction(
                    conn, p, bars, db_path, now_iso,
                )

            # Commit per-symbol so a downstream failure doesn't lose
            # all the progress in the cycle (mirrors resolve_predictions
            # commit cadence).
            conn.commit()

        return written
    finally:
        conn.close()


def build_training_dataset(db_path=None, min_horizons_required=1,
                            include_unresolved=False,
                            include_tainted=False,
                            include_veto_counterfactuals=True):
    """Return a list of per-prediction training rows ready for the
    fine-tune pipeline. This is the payoff of the multi-horizon
    outcomes schema — one call gives you a clean, trainable dataset
    without having to know how the underlying tables join.

    Each row is a dict containing:
      - All structured fields from ai_predictions (id, symbol,
        predicted_signal, confidence, regime, strategy_type, etc.)
      - `features`: the parsed features_json dict (or None)
      - `rule_votes`: the parsed rule_votes_json list (or [])
      - `outcomes`: dict {horizon_days: {return_pct, return_pct_net,
        mfe_pct, mae_pct, outcome_class, price_at_horizon}} — one
        entry per measured horizon
      - `prompt_text`, `raw_response_json`: full decision context for
        prompt-conditioned fine-tuning

    Parameters
    ----------
    db_path : str, optional
        Override database path.
    min_horizons_required : int
        Skip predictions that don't have at least this many horizon
        rows yet. Default 1 — usable for the first day's labels;
        raise to 5 to require the full horizon set (better for
        sequence-modeling approaches that train on the full label
        vector).
    include_unresolved : bool
        If True, also yield predictions with zero outcome rows. Useful
        for prompt-only fine-tuning that doesn't need labels (e.g.
        instruction tuning on the AI's own reasoning).
    include_tainted : bool
        If True, also include predictions whose `data_quality` column
        is set to a non-NULL marker (e.g. `'tainted_equity_2026_05_21'`
        for the 17 pid16 prompts that captured phantom equity from
        the cover-classification bug). Default False — the standard
        path for the fine-tune dataset builder excludes tainted rows
        via the same `data_quality_clause` pattern that
        `journal.data_quality_clause` applies to analytics queries.
        Set True only when explicitly auditing the corruption or
        rebuilding affected prompts.

    Returns
    -------
    list[dict]
        One dict per prediction matching the criteria. Returns an
        empty list when no predictions match.
    """
    import json as _json

    init_tracker_db(db_path)
    conn = _get_conn(db_path)
    try:
        # Tainted-row exclusion uses the same defense-in-depth pattern
        # that `journal.data_quality_clause()` applies to analytics SQL
        # on trades + ai_predictions: rows tagged with any data_quality
        # marker get filtered out so corruption can't pollute the
        # downstream consumer. The training-dataset builder is exactly
        # such a consumer — fine-tune training material must be clean.
        # `include_tainted=True` bypasses for forensic / repair work.
        if include_tainted:
            where_clause = ""
        else:
            where_clause = " WHERE p.data_quality IS NULL"
        pred_rows = conn.execute(
            f"""SELECT p.* FROM ai_predictions p{where_clause}
                ORDER BY p.timestamp ASC"""
        ).fetchall()
        # NOTE: no early return on empty pred_rows — a profile may have veto
        # counterfactuals (appended after this block) even with zero resolved
        # real predictions, and those must still reach the corpus.

        # Bulk-load all outcome rows in one query and bucket by
        # prediction_id. Avoids the N+1 query problem on a dataset
        # with thousands of predictions.
        outcome_map = {}
        for row in conn.execute(
            "SELECT prediction_id, horizon_days, return_pct, "
            "return_pct_net, mfe_pct, mae_pct, outcome_class, "
            "price_at_horizon "
            "FROM ai_prediction_outcomes"
        ).fetchall():
            outcome_map.setdefault(row[0], {})[row[1]] = {
                "return_pct": row[2],
                "return_pct_net": row[3],
                "mfe_pct": row[4],
                "mae_pct": row[5],
                "outcome_class": row[6],
                "price_at_horizon": row[7],
            }

        out = []
        for r in pred_rows:
            outcomes = outcome_map.get(r["id"], {})
            if len(outcomes) < min_horizons_required and not include_unresolved:
                continue

            features = None
            if r["features_json"]:
                try:
                    features = _json.loads(r["features_json"])
                except (ValueError, TypeError):
                    features = None
            rule_votes = []
            try:
                rv_json = r["rule_votes_json"]
            except (IndexError, KeyError):
                rv_json = None
            if rv_json:
                try:
                    rule_votes = _json.loads(rv_json) or []
                except (ValueError, TypeError):
                    rule_votes = []

            out.append({
                # Provenance: a REAL executed/observed prediction (as opposed to
                # a veto counterfactual appended below). The fine-tune pipeline
                # keys on is_real to weight/segregate modeled counterfactuals.
                "source": "real",
                "is_real": True,
                "id": r["id"],
                "timestamp": r["timestamp"],
                "symbol": r["symbol"],
                "predicted_signal": r["predicted_signal"],
                "confidence": r["confidence"],
                "reasoning": r["reasoning"],
                "price_at_prediction": r["price_at_prediction"],
                "regime_at_prediction": r["regime_at_prediction"],
                "strategy_type": r["strategy_type"],
                "prediction_type": r["prediction_type"],
                "features": features,
                "rule_votes": rule_votes,
                "outcomes": outcomes,
                "prompt_text": r["prompt_text"]
                    if "prompt_text" in r.keys() else None,
                "raw_response_json": r["raw_response_json"]
                    if "raw_response_json" in r.keys() else None,
                "meta_model_score": r["meta_model_score"]
                    if "meta_model_score" in r.keys() else None,
                "online_meta_score": r["online_meta_score"]
                    if "online_meta_score" in r.keys() else None,
            })
    finally:
        conn.close()

    # Veto counterfactuals (selection-engine): the AI proposed these option
    # spreads, its own specialists vetoed them, and they resolved to a TRUE
    # would-be P&L. Rich decision-quality signal (was the rejected idea good?),
    # appended as EXPLICITLY-LABELED counterfactuals — source="veto_counter
    # factual"/is_real=False — from a table physically separate from
    # ai_predictions, so they never contaminate the real-trade rows above while
    # still reaching the fine-tune corpus. Own-book; fail-open.
    if include_veto_counterfactuals:
        try:
            from journal import resolved_veto_counterfactuals
            for r in resolved_veto_counterfactuals(db_path):
                out.append({
                    "source": "veto_counterfactual",
                    "is_real": False,
                    "timestamp": r.get("timestamp"),
                    "symbol": r.get("symbol"),
                    "predicted_signal": "MULTILEG_OPEN",
                    "prediction_type": "option_open",
                    "strategy_type": r.get("strategy"),
                    "confidence": r.get("confidence"),
                    "veto_reason": r.get("veto_reason"),
                    "features": {
                        "strategy": r.get("strategy"),
                        "sector": r.get("sector"),
                        "max_loss_per_contract": r.get("max_loss_per_contract"),
                        "max_gain_per_contract": r.get("max_gain_per_contract"),
                        "breakeven": r.get("breakeven"),
                        "entry_net_premium": r.get("entry_net_premium"),
                        "lo_strike": r.get("lo_strike"),
                        "hi_strike": r.get("hi_strike"),
                        "expiry": r.get("expiry"),
                    },
                    # would-be outcome (modeled intrinsic-at-expiry P&L)
                    "outcomes": {"wouldbe": {
                        "outcome_class": r.get("wouldbe_outcome"),
                        "wouldbe_pnl": r.get("wouldbe_pnl"),
                    }},
                    "resolved_at": r.get("resolved_at"),
                    "prompt_text": None,
                    "raw_response_json": None,
                })
        except Exception as _vc_exc:
            logger.debug("veto counterfactuals unavailable for training "
                         "dataset (fail-open): %s", _vc_exc)
    return out


def _resolve_one(prediction, current_price):
    """Determine outcome for a single prediction.

    Returns (outcome, return_pct, days_held) or None if not yet resolvable.

    Uses prediction_type when present (post 2026-04-28) to apply the
    right win/loss criteria per type. Legacy rows without prediction_type
    fall back to inferring from predicted_signal — the inferred behavior
    matches the old logic for those rows.

    Types and their win conditions:
      directional_long  — price up >= 2% wins, down >= 2% loses
      directional_short — price down >= 2% wins, up >= 2% loses
      exit_long         — price stays flat or drops (we got out OK).
                          Loses if price runs up >= EXIT_BUFFER_PCT
                          (we left money on the table).
      exit_short        — price stays flat or rises (we covered OK).
                          Loses if price keeps dropping >= EXIT_BUFFER_PCT
                          (we covered too early).

    Phase 5b safety floor (2026-05-11): when the prediction is an
    option signal (MULTILEG_OPEN / OPTIONS / OPTION_EXERCISE), this
    function returns None to DEFER resolution. The pre-refactor
    behavior was structurally wrong: it computed return % from
    underlying-stock price moves, which is meaningless for option
    premiums (a 2% underlying move can produce a 100% premium swing
    or a 0% swing depending on Greeks). Deferring → option rows
    stay 'pending' until Phase 5c lands the option-aware resolver
    that uses `_fetch_option_premium` (single-leg) or net spread
    P&L from the trades table (multileg). NO option rows get
    incorrect win/loss values written from this point forward.
    """
    pred_price = prediction["price_at_prediction"]
    signal = (prediction.get("predicted_signal") or "").upper()
    pred_type = prediction.get("prediction_type")
    days_elapsed = _trading_days_since(prediction["timestamp"])

    if pred_price is None or pred_price == 0:
        return None

    # Phase 5c (2026-05-11): option-aware resolution. For option
    # signals, route through the option-economics resolver
    # (premium delta for single-leg; net spread P&L for multileg).
    # Returns None when the row lacks the metadata needed to
    # compute correctly (occ_symbol or option_order_id missing) —
    # falls back to Phase 5b's safety floor (stays pending) rather
    # than the broken pre-Phase-5b behavior (writes wrong values).
    if signal in _OPTION_SIGNALS:
        from pipelines.outcomes import option_resolver
        # Forward-horizon gate: even option rows benefit from a
        # min-hold buffer to avoid resolving on intraday premium
        # noise (especially MULTILEG_OPEN credit spreads where the
        # first day's premium can swing 30% on bid/ask alone).
        if days_elapsed < MIN_HOLD_DAYS_BEFORE_RESOLVE:
            return None
        ret = option_resolver.compute_option_return_pct(prediction)
        if ret is None:
            return None
        outcome, ret_pct = option_resolver.classify_option_outcome(
            ret, signal,
        )
        if outcome == "neutral":
            # Neutral within a normal hold — keep waiting unless
            # we're past the timeout (resolves as neutral with the
            # computed value).
            if days_elapsed >= TIMEOUT_DAYS:
                return ("neutral", ret_pct, days_elapsed)
            return None
        return (outcome, ret_pct, days_elapsed)

    return_pct = ((current_price - pred_price) / pred_price) * 100.0

    # Forward-horizon gate. Without this, BUY/SELL predictions can
    # resolve to win/loss within hours of being made, testing noise
    # not signal. HOLD already had its own days-elapsed gate; we
    # extend the same discipline to BUY/SELL/SHORT.
    if signal in ("BUY", "SELL", "SHORT") and days_elapsed < MIN_HOLD_DAYS_BEFORE_RESOLVE:
        return None

    # Derive prediction_type when missing (legacy rows): infer from
    # signal using pre-2026-04-28 logic. SELL was always "predict drop"
    # in the old resolver, so legacy SELL → directional_short.
    if not pred_type:
        if signal == "BUY":
            pred_type = "directional_long"
        elif signal == "SHORT":
            pred_type = "directional_short"
        elif signal == "SELL":
            pred_type = "directional_short"  # legacy semantic
        elif signal == "HOLD":
            pred_type = "directional_long"  # neutral / not-trading bucket

    if pred_type == "directional_long":
        if signal == "HOLD":
            # Special case: HOLD predictions resolve faster (3 days)
            # and "correctness" means price stayed range-bound.
            if days_elapsed >= HOLD_RESOLVE_DAYS:
                if abs(return_pct) < HOLD_MAX_CHANGE_PCT:
                    return ("win", return_pct, days_elapsed)
                else:
                    return ("loss", return_pct, days_elapsed)
        else:
            if return_pct >= BUY_WIN_PCT:
                return ("win", return_pct, days_elapsed)
            if return_pct <= -BUY_LOSS_PCT:
                return ("loss", return_pct, days_elapsed)
    elif pred_type == "directional_short":
        if return_pct <= -SELL_WIN_PCT:
            return ("win", return_pct, days_elapsed)
        if return_pct >= SELL_LOSS_PCT:
            return ("loss", return_pct, days_elapsed)
    elif pred_type == "exit_long":
        # We sold a long position. "Good exit" = price didn't run up
        # materially after we got out. "Bad exit" = price kept rising
        # significantly (we left money on the table).
        if return_pct > EXIT_BUFFER_PCT:
            return ("loss", return_pct, days_elapsed)
        # Price flat or down → exit was correct judgement
        if days_elapsed >= MIN_HOLD_DAYS_BEFORE_RESOLVE:
            return ("win", return_pct, days_elapsed)
    elif pred_type == "exit_short":
        # We covered a short. "Good cover" = price didn't keep dropping
        # materially. "Bad cover" = price continued lower (covered too early).
        if return_pct < -EXIT_BUFFER_PCT:
            return ("loss", return_pct, days_elapsed)
        if days_elapsed >= MIN_HOLD_DAYS_BEFORE_RESOLVE:
            return ("win", return_pct, days_elapsed)

    # Timeout: force-resolve after TIMEOUT_DAYS trading days as neutral
    if days_elapsed >= TIMEOUT_DAYS:
        return ("neutral", return_pct, days_elapsed)

    return None


def resolve_predictions(api=None, db_path=None, profile_id=None):
    """Check all pending predictions and resolve those that meet criteria.

    Parameters
    ----------
    api : alpaca REST client, optional
        Pre-built API client.  Falls back to get_api() when not provided.
    db_path : str, optional
        Override database path.
    profile_id : int, optional
        Profile id used to locate the per-profile online meta-model
        (Item 5a). If None, the online-model update is skipped.

    Returns the number of predictions resolved.
    """
    init_tracker_db(db_path)
    api = api or get_api()
    conn = _get_conn(db_path)
    try:
        pending = conn.execute(
            "SELECT * FROM ai_predictions WHERE status = 'pending'"
        ).fetchall()

        if not pending:
            logger.info("No pending AI predictions to resolve.")
            return 0

        resolved_count = 0
        deferred_option_count = 0
        now_iso = datetime.utcnow().isoformat()

        # Bulk-fetch all unique symbols in one (or few) Alpaca calls. With
        # 800+ pending predictions over a long weekend this is the difference
        # between ~30 seconds and 15-30 minutes per profile.
        symbols = list({row["symbol"] for row in pending})
        price_cache = _get_current_prices_bulk(symbols, api=api)

        for row in pending:
            sym = row["symbol"]
            prediction_dict = dict(row)
            # Phase 5c: option rows route through option_resolver which
            # fetches premiums directly via _fetch_option_premium —
            # doesn't need the bulk stock price. Inject db_path so the
            # multileg resolver can look up legs from trades table.
            prediction_dict["db_path"] = db_path
            sig = (prediction_dict.get("predicted_signal") or "").upper()
            is_option = sig in _OPTION_SIGNALS

            if not is_option and sym not in price_cache:
                # Stock rows still need the bulk-fetched price.
                continue

            current_price = price_cache.get(sym, 0.0)
            result = _resolve_one(prediction_dict, current_price)
            if result is None:
                # Option rows that didn't resolve this cycle. Phase 5c
                # makes most option rows resolvable now (fetches premium
                # via _fetch_option_premium, computes net spread P&L for
                # multileg from trades-table legs). Rows still defer when
                # they lack the metadata (no occ_symbol / no
                # option_order_id — typically pre-Phase-5c rows or
                # newly-inserted rows whose linkage hasn't run yet) or
                # when the AI's directional thesis is still developing
                # (within MIN_HOLD_DAYS_BEFORE_RESOLVE).
                if is_option:
                    deferred_option_count += 1
                continue

            outcome, return_pct, days_held = result
            # #186 Phase A (2026-05-20): compute cost-adjusted return.
            # Subtract estimated round-trip slippage so downstream
            # analytics (self-tuner, calibration, AI track_record) work
            # against numbers that predict actual trading P&L, not just
            # price prediction. For directional-LONG winners, costs
            # eat into the gain; for losers, costs deepen the loss
            # (cost is added to magnitude regardless of direction).
            cost_pct = _estimate_round_trip_cost_pct(prediction_dict, db_path)
            if return_pct >= 0:
                net_pct = return_pct - cost_pct
            else:
                # Losses become MORE negative once costs are subtracted:
                # we lose on the price move AND pay the round-trip cost.
                net_pct = return_pct - cost_pct
            conn.execute(
                """UPDATE ai_predictions
                   SET status = 'resolved',
                       actual_outcome = ?,
                       actual_return_pct = ?,
                       actual_return_pct_net = ?,
                       resolved_at = ?,
                       resolution_price = ?,
                       days_held = ?
                   WHERE id = ?""",
                (outcome, round(return_pct, 4), round(net_pct, 4),
                 now_iso, current_price, days_held, row["id"]),
            )
            # Commit *each row* immediately. Specialist-calibration and
            # online-meta-model updates below open their own connections
            # and need to write to the same DB. With a long-running outer
            # transaction we hit "database is locked" on every iteration
            # (250+ warnings / 10 min observed in prod 2026-05-04).
            # Per-row commit gives those inner writes a window to land
            # between iterations.
            conn.commit()
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
                # Item 5a — incremental update to the online (SGD) meta-model
                # so it adapts in real time to each new resolution.
                if profile_id is not None:
                    try:
                        import json as _json
                        from online_meta_model import update_online_model
                        feats = _json.loads(row["features_json"]) if row["features_json"] else None
                        if feats:
                            update_online_model(
                                profile_id, feats,
                                outcome_label=(1 if outcome == "win" else 0),
                            )
                    except Exception as _exc:
                        logger.debug(
                            "Online model update failed for prediction "
                            "%d: %s", row["id"], _exc,
                        )
            logger.info(
                "Resolved prediction #%d (%s %s): %s (%.2f%%, %d days)",
                row["id"], row["predicted_signal"], sym, outcome, return_pct,
                days_held,
            )
    finally:
        conn.close()
    logger.info("Resolved %d / %d pending predictions.", resolved_count, len(pending))
    if deferred_option_count > 0:
        # Phase 5b safety floor: option rows accumulate as 'pending'
        # until Phase 5c lands the option-aware resolver. A growing
        # number here just means option rows aren't yet evaluable.
        logger.info(
            "Deferred %d option predictions (Phase 5c will land "
            "option-aware resolver).", deferred_option_count,
        )
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
    try:

        total = conn.execute("SELECT COUNT(*) FROM ai_predictions").fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status = 'resolved'"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status = 'pending'"
        ).fetchone()[0]

        if resolved == 0:
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

        # Win rate (everything blended — kept for backwards compat)
        wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0]
        win_rate = (wins / resolved) * 100.0 if resolved else 0.0

        # Directional-only win rate. The blended win rate above conflates
        # actual trade decisions (BUY / SHORT / STRONG_SELL / SELL /
        # MULTILEG) with HOLDs (which the resolver labels "win" when the
        # underlying didn't move much, "loss" otherwise — that's mostly a
        # measure of how volatile the universe is, not how good the AI is).
        # The directional rate is the actually meaningful number for "is
        # the AI's trading decision better than a coin flip?".
        DIRECTIONAL_SQL = (
            "UPPER(predicted_signal) IN "
            "('BUY','SHORT','STRONG_SELL','SELL','MULTILEG_OPEN')"
        )
        dir_resolved = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' AND {DIRECTIONAL_SQL} "
            f"AND actual_outcome IN ('win','loss')"
        ).fetchone()[0]
        dir_wins = conn.execute(
            f"SELECT COUNT(*) FROM ai_predictions "
            f"WHERE status='resolved' AND {DIRECTIONAL_SQL} "
            f"AND actual_outcome='win'"
        ).fetchone()[0]
        directional_win_rate = (
            (dir_wins / dir_resolved) * 100.0 if dir_resolved else 0.0
        )

        # HOLD pass rate. Different semantics: a HOLD "wins" when the AI
        # passed AND the underlying stayed flat enough that no opportunity
        # was missed. Surfaced separately so users don't read it as "AI
        # accuracy on trades".
        hold_resolved = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' "
            "AND UPPER(predicted_signal) = 'HOLD' "
            "AND actual_outcome IN ('win','loss')"
        ).fetchone()[0]
        hold_wins = conn.execute(
            "SELECT COUNT(*) FROM ai_predictions "
            "WHERE status='resolved' "
            "AND UPPER(predicted_signal) = 'HOLD' "
            "AND actual_outcome='win'"
        ).fetchone()[0]
        hold_pass_rate = (
            (hold_wins / hold_resolved) * 100.0 if hold_resolved else 0.0
        )

        # Average confidence on wins vs losses
        avg_conf_wins = conn.execute(
            "SELECT AVG(confidence) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='win'"
        ).fetchone()[0] or 0.0

        avg_conf_losses = conn.execute(
            "SELECT AVG(confidence) FROM ai_predictions "
            "WHERE status='resolved' AND actual_outcome='loss'"
        ).fetchone()[0] or 0.0

        # Average return by signal type, plus the sample size behind each
        # (so the dashboard can N/A small-sample readings instead of
        # rendering "+1.63% on SELLs" computed from 3 predictions).
        # 2026-05-12 fix: previously filtered to ONLY 'BUY' / 'SELL',
        # missing STRONG_BUY/WEAK_BUY/STRONG_SELL/WEAK_SELL. That left
        # 30-50% of entry signals out of the displayed averages. Same
        # bug class as the HOLD-attribution gap. Now uses every long-
        # entry / sell-entry signal type — keep in sync with
        # pipelines.outcomes.kind_from_signal.
        buys_row = conn.execute(
            "SELECT COUNT(*), AVG(actual_return_pct) FROM ai_predictions "
            "WHERE status='resolved' "
            "AND predicted_signal IN ('BUY','STRONG_BUY','WEAK_BUY') "
            "AND actual_return_pct IS NOT NULL"
        ).fetchone()
        n_buys = buys_row[0] or 0
        avg_ret_buys = buys_row[1] or 0.0

        sells_row = conn.execute(
            "SELECT COUNT(*), AVG(actual_return_pct) FROM ai_predictions "
            "WHERE status='resolved' "
            "AND predicted_signal IN ('SELL','STRONG_SELL','WEAK_SELL') "
            "AND actual_return_pct IS NOT NULL"
        ).fetchone()
        n_sells = sells_row[0] or 0
        avg_ret_sells = sells_row[1] or 0.0

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

        # ----- Best/worst predictions split by prediction nature -----
        # The previous single best/worst pair conflated two very different
        # things on the dashboard:
        #   - A directional trade with a real entry (BUY / STRONG_SELL /
        #     SHORT / SELL) — its `actual_return_pct` IS the trade outcome
        #     (with sign flipped for shorts since return_pct is the
        #     underlying's price move, not the position's P&L).
        #   - A HOLD prediction — the AI explicitly chose NOT to trade.
        #     `actual_return_pct` is what the underlying did afterwards.
        #     Positive = a gain we passed on (missed opportunity), negative
        #     = a loss we avoided (correct call).
        # Surface both pairs so the dashboard isn't conflating "biggest
        # avoided loss" with "worst prediction".

        # `trade_pnl_pct` flips sign for shorts so the ordering is
        # apples-to-apples in actual-P&L terms across BUY and SHORT.
        # Legacy SELL was always "directional_short" semantically (see
        # _resolve_one), so we treat SELL the same as SHORT here.
        DIRECTIONAL = ("BUY", "STRONG_SELL", "SHORT", "SELL")
        pnl_expr = (
            "CASE WHEN UPPER(predicted_signal) IN ('BUY') "
            "THEN actual_return_pct ELSE -actual_return_pct END"
        )

        best_trade_row = conn.execute(
            f"SELECT symbol, confidence, actual_return_pct, "
            f"predicted_signal, ({pnl_expr}) AS trade_pnl_pct "
            f"FROM ai_predictions "
            f"WHERE status='resolved' "
            f"AND UPPER(predicted_signal) IN ('BUY','STRONG_SELL','SHORT','SELL') "
            f"AND actual_return_pct IS NOT NULL "
            f"ORDER BY trade_pnl_pct DESC LIMIT 1"
        ).fetchone()
        worst_trade_row = conn.execute(
            f"SELECT symbol, confidence, actual_return_pct, "
            f"predicted_signal, ({pnl_expr}) AS trade_pnl_pct "
            f"FROM ai_predictions "
            f"WHERE status='resolved' "
            f"AND UPPER(predicted_signal) IN ('BUY','STRONG_SELL','SHORT','SELL') "
            f"AND actual_return_pct IS NOT NULL "
            f"ORDER BY trade_pnl_pct ASC LIMIT 1"
        ).fetchone()
        missed_gain_row = conn.execute(
            "SELECT symbol, confidence, actual_return_pct "
            "FROM ai_predictions WHERE status='resolved' "
            "AND UPPER(predicted_signal) = 'HOLD' "
            "AND actual_return_pct IS NOT NULL "
            "ORDER BY actual_return_pct DESC LIMIT 1"
        ).fetchone()
        avoided_loss_row = conn.execute(
            "SELECT symbol, confidence, actual_return_pct "
            "FROM ai_predictions WHERE status='resolved' "
            "AND UPPER(predicted_signal) = 'HOLD' "
            "AND actual_return_pct IS NOT NULL "
            "ORDER BY actual_return_pct ASC LIMIT 1"
        ).fetchone()

        def _trade(row):
            if row is None:
                return None
            return {
                "symbol": row["symbol"],
                "return_pct": row["actual_return_pct"],
                "trade_pnl_pct": row["trade_pnl_pct"],
                "confidence": row["confidence"],
                "signal": row["predicted_signal"],
            }

        def _hold(row):
            if row is None:
                return None
            return {
                "symbol": row["symbol"],
                "return_pct": row["actual_return_pct"],
            }

        best_trade = _trade(best_trade_row)
        worst_trade = _trade(worst_trade_row)
        biggest_missed_gain = _hold(missed_gain_row)
        biggest_avoided_loss = _hold(avoided_loss_row)

        # Backwards-compat: keep best_prediction/worst_prediction populated
        # so any consumers that still query them don't break. Prefer the
        # new split fields above for new UI surfaces.
        best_prediction = (
            {"symbol": best_trade["symbol"],
              "return_pct": best_trade["trade_pnl_pct"],
              "confidence": best_trade["confidence"]}
            if best_trade else None
        )
        worst_prediction = (
            {"symbol": worst_trade["symbol"],
              "return_pct": worst_trade["trade_pnl_pct"],
              "confidence": worst_trade["confidence"]}
            if worst_trade else None
        )

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


        return {
            "total_predictions": total,
            "resolved": resolved,
            "pending": pending,
            "win_rate": round(win_rate, 1),
            "directional_win_rate": round(directional_win_rate, 1),
            "directional_resolved": dir_resolved,
            "directional_wins": dir_wins,
            "hold_pass_rate": round(hold_pass_rate, 1),
            "hold_resolved": hold_resolved,
            "avg_confidence_on_wins": round(avg_conf_wins, 1),
            "avg_confidence_on_losses": round(avg_conf_losses, 1),
            "avg_return_on_buys": round(avg_ret_buys, 2),
            "avg_return_on_sells": round(avg_ret_sells, 2),
            "n_buys": n_buys,
            "n_sells": n_sells,
            "accuracy_by_confidence": accuracy_by_confidence,
            "best_prediction": best_prediction,
            "worst_prediction": worst_prediction,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "biggest_missed_gain": biggest_missed_gain,
            "biggest_avoided_loss": biggest_avoided_loss,
            "profit_factor": profit_factor,
        }


    finally:
        conn.close()
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
    from zoneinfo import ZoneInfo
    import sqlite3 as _sqlite3

    # Use ET-localized date so the chart's x-axis matches the user's
    # perception. date.today() returns the SERVER's local date (UTC on
    # the prod droplet), which causes the right edge to label as "tomorrow"
    # any time after ~8pm ET (= midnight UTC).
    today = datetime.now(ZoneInfo("America/New_York")).date()
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
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _res_exc:
            # Per-DB resolutions aggregation loop; one bad DB
            # shouldn't kill cross-profile rollup. Surface for follow-up.
            logger.debug(
                "ai_tracker resolutions aggregation failed: %s: %s",
                type(_res_exc).__name__, _res_exc,
            )
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
