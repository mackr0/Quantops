"""SQLite trade journal for logging trades, signals, and portfolio snapshots."""

import sqlite3
import json
from datetime import datetime, date

import config


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
    # busy_timeout: wait up to 5s for write locks to clear instead of
    # immediately raising OperationalError. Eliminates transient-lock
    # failures during scheduler-writes-while-dashboard-reads races.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path=None):
    """Create journal tables if they don't exist."""
    conn = _get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL,
            order_id TEXT,
            signal_type TEXT,
            strategy TEXT,
            reason TEXT,
            ai_reasoning TEXT,
            ai_confidence REAL,
            stop_loss REAL,
            take_profit REAL,
            status TEXT DEFAULT 'open',
            pnl REAL,
            decision_price REAL,
            fill_price REAL,
            slippage_pct REAL
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            strategy TEXT,
            signal TEXT NOT NULL,
            reason TEXT,
            price REAL,
            indicators TEXT,
            acted_on INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            equity REAL,
            cash REAL,
            portfolio_value REAL,
            num_positions INTEGER,
            daily_pnl REAL
        );

        CREATE TABLE IF NOT EXISTS ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            predicted_signal TEXT NOT NULL,
            confidence REAL,
            reasoning TEXT,
            price_at_prediction REAL NOT NULL,
            target_entry REAL,
            target_stop_loss REAL,
            target_take_profit REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            actual_outcome TEXT,
            actual_return_pct REAL,
            resolved_at TEXT,
            resolution_price REAL,
            prediction_type TEXT
        );

        -- Broker-rejected order attempts. The AI proposed the trade,
        -- the system tried to submit it, but the broker (Alpaca)
        -- refused with a recoverable reason (cross-direction conflict
        -- with a sibling profile on a shared account, wash-trade
        -- guard, insufficient buying power, etc.). Captured so the
        -- UI can show "REJECTED" inline on the AI Brain panel
        -- instead of the trade silently disappearing, and so
        -- post-trade analytics can exclude rejected predictions
        -- from win-rate computations (they didn't actually trade).
        -- Caught 2026-05-11: CWAN BUY on Mid Cap was rejected by
        -- Alpaca's cross-direction guard and operator went looking
        -- for the trade because nothing on screen surfaced it.
        CREATE TABLE IF NOT EXISTS broker_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            signal_type TEXT,
            ai_confidence REAL,
            ai_reasoning TEXT,
            rejection_code TEXT NOT NULL,
            broker_message TEXT,
            prediction_id INTEGER REFERENCES ai_predictions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_broker_rejections_timestamp
            ON broker_rejections(timestamp);
        CREATE INDEX IF NOT EXISTS idx_broker_rejections_symbol
            ON broker_rejections(symbol);
        CREATE INDEX IF NOT EXISTS idx_broker_rejections_prediction_id
            ON broker_rejections(prediction_id);

        -- Phase 3: rolling performance snapshots per strategy/signal type.
        -- Written daily by alpha_decay monitoring task. Each row is one
        -- day's 30-day rolling view of a specific signal's performance.
        CREATE TABLE IF NOT EXISTS signal_performance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            n_predictions INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            win_rate REAL NOT NULL,
            avg_return_pct REAL NOT NULL,
            sharpe_ratio REAL NOT NULL,
            profit_factor REAL,
            UNIQUE (snapshot_date, strategy_type, window_days)
        );

        -- Phase 3: strategies that have been auto-deprecated due to alpha
        -- decay. The trade pipeline checks this table and skips deprecated
        -- strategies' signals. Restoration sets deprecated_at=NULL.
        CREATE TABLE IF NOT EXISTS deprecated_strategies (
            strategy_type TEXT PRIMARY KEY,
            deprecated_at TEXT NOT NULL DEFAULT (datetime('now')),
            reason TEXT NOT NULL,
            rolling_sharpe_at_deprecation REAL,
            lifetime_sharpe REAL,
            consecutive_bad_days INTEGER,
            restored_at TEXT
        );

        -- Phase 4: SEC filing history and AI-analyzed semantic alerts.
        -- One row per (symbol, accession_number). Consecutive filings of
        -- the same type are diffed by the AI to detect material language
        -- changes (going concern, material weakness, risk factor shifts).
        CREATE TABLE IF NOT EXISTS sec_filings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            accession_number TEXT NOT NULL,
            form_type TEXT NOT NULL,
            filed_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            filing_url TEXT,
            risk_factors_text TEXT,
            mdna_text TEXT,
            going_concern_flag INTEGER DEFAULT 0,
            material_weakness_flag INTEGER DEFAULT 0,
            analyzed_at TEXT,
            alert_severity TEXT,
            alert_signal TEXT,
            alert_summary TEXT,
            alert_changes_json TEXT,
            UNIQUE (symbol, accession_number)
        );

        -- Task-run ledger: one row per scheduled task invocation. Used
        -- by the run-completion watchdog to detect stalled runs — any
        -- row with started_at older than N minutes but completed_at NULL
        -- is a stuck task that needs investigation.
        CREATE TABLE IF NOT EXISTS task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at TEXT,
            duration_seconds REAL,
            status TEXT NOT NULL DEFAULT 'running',
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_task_runs_active
            ON task_runs(completed_at) WHERE completed_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_task_runs_started
            ON task_runs(started_at DESC);

        -- Symbols that were recently exited (stop-loss, trailing-stop,
        -- take-profit, or manual close). The trade pipeline skips BUY
        -- signals on symbols with a row here within the cooldown window.
        -- Prevents the sell→immediate-rebuy-higher churn.
        CREATE TABLE IF NOT EXISTS recently_exited_symbols (
            symbol TEXT PRIMARY KEY,
            exited_at TEXT NOT NULL DEFAULT (datetime('now')),
            trigger TEXT,
            exit_price REAL
        );

        -- AI cost ledger: one row per call_ai invocation. Token counts
        -- are stored separately from USD so re-pricing history is a
        -- single-file change in ai_pricing.py.
        CREATE TABLE IF NOT EXISTS ai_cost_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            purpose TEXT,
            estimated_cost_usd REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ai_cost_ts
            ON ai_cost_ledger(timestamp DESC);

        -- Phase 10: cross-asset crisis state transitions. One row per
        -- transition (normal → elevated → crisis → severe). The trade
        -- pipeline reads the latest active row to gate position sizing
        -- and new longs.
        CREATE TABLE IF NOT EXISTS crisis_state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transitioned_at TEXT NOT NULL DEFAULT (datetime('now')),
            from_level TEXT,
            to_level TEXT NOT NULL,
            signals_json TEXT,
            readings_json TEXT,
            size_multiplier REAL NOT NULL DEFAULT 1.0
        );
        CREATE INDEX IF NOT EXISTS idx_crisis_state_time
            ON crisis_state_history(transitioned_at DESC);

        -- Phase 9: event stream. Events are detected by pollers and
        -- dispatched to subscribed handlers. Idempotency is enforced on
        -- (type, symbol, DATE(detected_at)) so the same detector run
        -- twice in a day doesn't duplicate an event.
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            symbol TEXT,
            severity TEXT NOT NULL DEFAULT 'info',
            payload_json TEXT,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            handled_at TEXT,
            handler_results_json TEXT,
            dedup_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_events_handled
            ON events(handled_at) WHERE handled_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_events_type_time
            ON events(type, detected_at DESC);

        -- Phase 7: auto-generated strategies proposed by the AI. Each row is
        -- a strategy variant with its full JSON spec, lifecycle status, and
        -- lineage. Status progression:
        --   proposed  → AI wrote the spec; awaiting backtest validation
        --   validated → passed Phase 2 rigorous_backtest
        --   shadow    → runs live; predictions tracked but no real capital
        --   active    → promoted after shadow period with sufficient edge
        --   retired   → failed validation, decayed, or lost shadow race
        -- parent_id links genealogies (Phase 7 evolves variants of winners).
        CREATE TABLE IF NOT EXISTS auto_generated_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            generation INTEGER NOT NULL DEFAULT 1,
            parent_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            validated_at TEXT,
            shadow_started_at TEXT,
            promoted_at TEXT,
            retired_at TEXT,
            retirement_reason TEXT,
            validation_report_json TEXT,
            FOREIGN KEY (parent_id) REFERENCES auto_generated_strategies(id)
        );
        CREATE INDEX IF NOT EXISTS idx_auto_strategies_status
            ON auto_generated_strategies(status);
        -- Item 1b — stat-arb pair book. Each row is one cointegrated
        -- pair the daily scanner has flagged as tradeable. The (a,b)
        -- pair is canonical: symbol_a < symbol_b alphabetically so
        -- there's exactly one row per unordered pair. last_*_at fields
        -- track lifecycle: created_at when first detected, retested_at
        -- when the daily rebalance last verified cointegration,
        -- retired_at when the pair broke (p > 0.10) or was manually
        -- closed.
        CREATE TABLE IF NOT EXISTS stat_arb_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_a TEXT NOT NULL,
            symbol_b TEXT NOT NULL,
            hedge_ratio REAL NOT NULL,
            p_value REAL NOT NULL,
            half_life_days REAL NOT NULL,
            correlation REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            retested_at TEXT,
            retired_at TEXT,
            retirement_reason TEXT,
            UNIQUE(symbol_a, symbol_b)
        );
        CREATE INDEX IF NOT EXISTS idx_stat_arb_pairs_status
            ON stat_arb_pairs(status);
    """)

    # Universal schema migration: ensures every column defined in the
    # CREATE TABLE statements above actually exists in the DB. Catches
    # any column added to the schema that wasn't present when the DB
    # was first created. Replaces the old per-column migration functions.
    _migrate_all_columns(conn)

    # daily_snapshots: dedupe + add UNIQUE(date) constraint.
    # Existing DBs created before 2026-04-28 had no UNIQUE constraint
    # and accumulated duplicate rows when the scheduler restarted
    # before the marker-file fix landed. Combine the dedupe and the
    # constraint addition in one table-rebuild migration.
    _migrate_daily_snapshots_unique(conn)

    conn.commit()
    conn.close()


def record_exit(db_path: str, symbol: str, trigger: str,
                exit_price: float = 0) -> None:
    """Mark a symbol as recently exited so the pipeline skips re-entry."""
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO recently_exited_symbols "
            "(symbol, exited_at, trigger, exit_price) "
            "VALUES (?, datetime('now'), ?, ?)",
            (symbol, trigger, exit_price),
        )
        conn.commit()
    finally:
        conn.close()


def record_wash_cooldown(db_path: str, symbol: str) -> None:
    """Mark a symbol as in wash-trade cooldown.

    Reuses the recently_exited_symbols table with trigger='wash_cooldown'
    so the pre-filter pipeline can lump it in with normal cooldowns.
    A 30-day cooldown is the standard wash-sale window — Alpaca's
    detection is on a shorter horizon but 30 days covers it cleanly.
    """
    if not db_path or not symbol:
        return
    try:
        conn = _get_conn(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO recently_exited_symbols "
            "(symbol, exited_at, trigger, exit_price) "
            "VALUES (?, datetime('now'), 'wash_cooldown', NULL)",
            (symbol,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_wash_cooldown_symbols(db_path: str, days: int = 30) -> set:
    """Return symbols currently in wash-trade cooldown (30-day window)."""
    if not db_path:
        return set()
    try:
        conn = _get_conn(db_path)
        rows = conn.execute(
            "SELECT symbol FROM recently_exited_symbols "
            "WHERE trigger = 'wash_cooldown' "
            "AND exited_at >= datetime('now', ?)",
            (f"-{int(days)} days",),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def get_recently_exited(db_path: str, cooldown_minutes: int = 60) -> set:
    """Return the set of symbols currently in the post-exit cooldown window."""
    try:
        conn = _get_conn(db_path)
        rows = conn.execute(
            "SELECT symbol FROM recently_exited_symbols "
            "WHERE exited_at >= datetime('now', ?)",
            (f"-{int(cooldown_minutes)} minutes",),
        ).fetchall()
        conn.close()
        return {r["symbol"] for r in rows}
    except Exception:
        return set()


def _migrate_all_columns(conn):
    """Universal schema migration — ensures every column defined in the
    CREATE TABLE statements exists in the actual DB.

    This replaces the old per-column migration functions
    (_migrate_slippage_columns, _migrate_prediction_columns) that
    missed columns and caused 'no such column' runtime errors
    (e.g. days_held on ai_predictions, 2026-04-22).

    For each table, we define the full expected column set. Any column
    present in the definition but missing from the actual table gets
    added via ALTER TABLE. Safe to run repeatedly — already-existing
    columns are skipped.
    """
    _EXPECTED_COLUMNS = {
        "trades": [
            ("decision_price", "REAL"),
            ("fill_price", "REAL"),
            ("slippage_pct", "REAL"),
            # Max favorable excursion — highest price the position
            # touched between entry and exit. Sampled by the
            # exit-cycle's MFE updater. Used by the trailing-stop
            # tuner to compute "give-back" (MFE - exit_price) per
            # bucket of trailing_atr_multiplier.
            ("max_favorable_excursion", "REAL"),
            # INTRADAY_STOPS_PLAN Stage 1 — Alpaca order id for the
            # broker-managed stop-loss attached to this entry. Stored
            # so we can cancel it when the AI does an early exit
            # (otherwise the broker would double-fire after our market
            # sell). NULL when no protective stop has been placed yet
            # or the order has already filled.
            ("protective_stop_order_id", "TEXT"),
            # INTRADAY_STOPS_PLAN Stage 2 — Alpaca order id for the
            # broker-managed take-profit limit. Locks in wins at the
            # take_profit_pct threshold instead of waiting for trail
            # stops to fire after a reversal (which often gives back
            # most of the gain). Skipped on positions covered by the
            # conviction-tp override.
            ("protective_tp_order_id", "TEXT"),
            # INTRADAY_STOPS_PLAN Stage 3 — Alpaca order id for the
            # broker-managed trailing stop. Replaces the polling-based
            # trailing logic that fired on daily close after intraday
            # reversal (e.g. IBM rallied to $258 then collapsed to $231
            # in one day; polling caught the EOD close at $231 = $2 win
            # on what was an $1500 unrealized winner). Broker trailing
            # tracks high water continuously and fires at trail_percent
            # below it the moment the level is broken.
            ("protective_trailing_order_id", "TEXT"),
            # Item 1a of COMPETITIVE_GAP_PLAN.md — options trading.
            # When the row represents an option position, occ_symbol
            # holds the 21-char OCC contract symbol (e.g. AAPL  250516C00150000),
            # symbol holds the underlying ticker, option_strategy
            # tags the strategy (covered_call / protective_put / etc),
            # and expiry / strike are denormalized from the OCC symbol
            # for cheap querying.
            ("occ_symbol", "TEXT"),
            ("option_strategy", "TEXT"),
            ("expiry", "TEXT"),
            ("strike", "REAL"),
            # Item 5c — slippage model predicted vs realized.
            # Captured at order-submit time from
            # slippage_model.estimate_slippage. Realized slippage is
            # already in slippage_pct; comparing the two over time
            # tells us if the model's K coefficient is calibrated
            # well or drifting.
            ("predicted_slippage_bps", "REAL"),
            # Item 5c continuation — 20-day average daily volume at
            # decision time. Captured at order submit. Lets the
            # slippage calibrator use REAL participation rate
            # (qty * price / adv_dollars) instead of the coarse
            # `assumed_adv_dollars=$50M` default.
            ("adv_at_decision", "REAL"),
        ],
        "ai_predictions": [
            ("regime_at_prediction", "TEXT"),
            ("strategy_type", "TEXT"),
            ("features_json", "TEXT"),
            ("days_held", "INTEGER"),
            # Classification of what the prediction means:
            #   'directional_long'  — BUY: predict price goes up
            #   'directional_short' — SHORT or SELL on unheld: predict price goes down
            #   'exit_long'         — SELL on a long position we hold: lock in / exit
            #   'exit_short'        — close a short position we hold
            # The resolver applies different win/loss criteria per type so
            # exit-quality doesn't get conflated with directional-bearish accuracy.
            ("prediction_type", "TEXT"),
            # Phase 5 of the instrument-class pipeline refactor (2026-05-11):
            # which pipeline owns this prediction. 'stock' or 'option'
            # (future: 'crypto', 'fx'). Lets per-pipeline tuning queries
            # filter by structural tag rather than enumerating signal
            # types — closes audit finding #2 by construction (option
            # outcomes can no longer pool with stock outcomes).
            #
            # NULL on rows written before the migration backfill ran.
            # tuning/{stock,option}.py reads via a UNION pattern: rows
            # tagged 'stock' OR (NULL AND signal_type IN STOCK_SIGNAL_TYPES).
            # See pipelines/outcomes/__init__.py for the writer side.
            ("pipeline_kind", "TEXT"),
            # Phase 5b of pipeline refactor (2026-05-11): the OCC option
            # symbol the prediction refers to (single-leg option rows
            # only; multileg rows reference the combo via
            # `option_order_id` instead). NULL for stock predictions
            # and legacy option rows pre-Phase-5b. The Phase 5c
            # resolver uses this to fetch the contract's current
            # premium via client._fetch_option_premium → compute return
            # from premium delta, replacing today's broken behavior
            # (resolver computes return % from underlying price moves
            # which are structurally meaningless for option premiums).
            ("occ_symbol", "TEXT"),
            # Phase 5b: order_id used to look up multileg trade legs
            # from the `trades` table at resolution time. Phase 5c
            # uses this to compute net spread P&L vs entry credit/
            # debit — the only correct return metric for multileg.
            ("option_order_id", "TEXT"),
        ],
    }

    for table, columns in _EXPECTED_COLUMNS.items():
        try:
            existing = {
                row[1] for row in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for col, col_type in columns:
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    )
        except Exception:
            pass

    # Phase 5 of pipeline refactor: backfill pipeline_kind for any
    # ai_predictions row that's still NULL after the column was
    # added. Idempotent — only touches NULL rows. Skipped silently if
    # the column doesn't exist (e.g., the ALTER above failed for some
    # reason); subsequent calls retry.
    try:
        # Test for the column existing before attempting backfill.
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(ai_predictions)"
        ).fetchall()}
        if "pipeline_kind" in cols and "predicted_signal" in cols:
            # Stock signal types — keep in sync with tuning/stock.py
            stock_signals = (
                "BUY", "STRONG_BUY", "WEAK_BUY",
                "SELL", "STRONG_SELL", "WEAK_SELL",
                "SHORT", "COVER",
            )
            # Option signal types — keep in sync with tuning/option.py
            option_signals = ("MULTILEG_OPEN", "OPTIONS", "OPTION_EXERCISE")
            sp = ",".join("?" * len(stock_signals))
            op = ",".join("?" * len(option_signals))
            conn.execute(
                f"UPDATE ai_predictions SET pipeline_kind = 'stock' "
                f"WHERE pipeline_kind IS NULL "
                f"AND predicted_signal IN ({sp})",
                stock_signals,
            )
            conn.execute(
                f"UPDATE ai_predictions SET pipeline_kind = 'option' "
                f"WHERE pipeline_kind IS NULL "
                f"AND predicted_signal IN ({op})",
                option_signals,
            )
    except Exception:
        pass


def _migrate_daily_snapshots_unique(conn):
    """Ensure daily_snapshots has UNIQUE(date) and one row per date.

    SQLite can't ALTER TABLE ADD CONSTRAINT, so we rebuild the table.
    The rebuild also dedupes — for any date with multiple rows, only
    the row with MAX(id) survives (latest write wins, which matches
    what the readers were already picking by accident).

    Idempotent: if the constraint is already present we skip.
    """
    try:
        # Detect whether `date` already has a UNIQUE index. PRAGMA
        # index_list returns one row per index; PRAGMA index_info
        # tells us which column the index covers. The implicit index
        # SQLite creates for UNIQUE columns has unique=1.
        idx_rows = conn.execute(
            "PRAGMA index_list(daily_snapshots)"
        ).fetchall()
        for idx in idx_rows:
            # idx columns: seq, name, unique, origin, partial
            if int(idx[2]) != 1:
                continue
            cols = conn.execute(
                f"PRAGMA index_info({idx[1]!r})"
            ).fetchall()
            if len(cols) == 1 and cols[0][2] == "date":
                return  # already migrated
    except Exception:
        return  # table missing entirely — init_db just created it fresh

    try:
        conn.execute("BEGIN")
        conn.execute(
            "CREATE TABLE daily_snapshots_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "date TEXT NOT NULL UNIQUE, "
            "equity REAL, cash REAL, portfolio_value REAL, "
            "num_positions INTEGER, daily_pnl REAL"
            ")"
        )
        conn.execute(
            "INSERT INTO daily_snapshots_new "
            "(id, date, equity, cash, portfolio_value, num_positions, daily_pnl) "
            "SELECT id, date, equity, cash, portfolio_value, num_positions, daily_pnl "
            "FROM daily_snapshots WHERE id IN ("
            "SELECT MAX(id) FROM daily_snapshots GROUP BY date"
            ")"
        )
        conn.execute("DROP TABLE daily_snapshots")
        conn.execute("ALTER TABLE daily_snapshots_new RENAME TO daily_snapshots")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def log_trade(symbol, side, qty, price=None, order_id=None, signal_type=None,
              strategy=None, reason=None, ai_reasoning=None, ai_confidence=None,
              stop_loss=None, take_profit=None, status="open", pnl=None,
              decision_price=None, fill_price=None, slippage_pct=None,
              occ_symbol=None, option_strategy=None, expiry=None, strike=None,
              predicted_slippage_bps=None, adv_at_decision=None,
              db_path=None):
    """Log a trade execution to the journal.

    Parameters
    ----------
    decision_price : float, optional
        The price the strategy/AI saw when making the decision.
    fill_price : float, optional
        The actual fill price from Alpaca (updated later by fill updater).
    slippage_pct : float, optional
        (fill_price - decision_price) / decision_price * 100.
    occ_symbol : str, optional
        OCC option contract symbol when this row represents an option
        position. None for stock trades.
    option_strategy : str, optional
        'covered_call' / 'protective_put' / 'long_call' / 'long_put' /
        'cash_secured_put'. None for stock trades.
    expiry : str, optional
        ISO date string of the option expiry. Denormalized from OCC.
    strike : float, optional
        Option strike price. Denormalized from OCC.

    Returns the row id of the inserted trade.
    """
    conn = _get_conn(db_path)
    cursor = conn.execute(
        """INSERT INTO trades
           (timestamp, symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit, status, pnl,
            decision_price, fill_price, slippage_pct,
            occ_symbol, option_strategy, expiry, strike,
            predicted_slippage_bps, adv_at_decision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit,
            status, pnl, decision_price, fill_price, slippage_pct,
            occ_symbol, option_strategy, expiry, strike,
            predicted_slippage_bps, adv_at_decision,
        ),
    )
    conn.commit()
    trade_id = cursor.lastrowid
    conn.close()
    return trade_id


# Broker-rejection classification + persistence ----------------------------

# Map Alpaca's rejection-message patterns to a stable code so the
# UI + analytics can group rejections by cause without parsing free
# text. Order matters — first match wins. Patterns are case-folded
# at lookup time so callers don't have to.
_REJECTION_PATTERNS = (
    ("wash trade",
     "wash_trade"),
    ("cannot open a long buy while a short sell order",
     "cross_direction_long_blocked"),
    ("cannot open a short sell while a long buy order",
     "cross_direction_short_blocked"),
    ("insufficient buying power",
     "insufficient_buying_power"),
    ("insufficient qty",
     "insufficient_qty"),
    ("no available quote",
     "no_quote_available"),
)


def classify_broker_rejection_message(msg):
    """Map a broker rejection message to a stable rejection_code.

    Returns 'other' if no known pattern matches — the row still gets
    written so we can audit + improve the classifier later.
    """
    if not msg:
        return "other"
    lo = str(msg).lower()
    for pat, code in _REJECTION_PATTERNS:
        if pat in lo:
            return code
    return "other"


def record_broker_rejection(db_path, *, symbol, action, signal_type,
                            ai_confidence, ai_reasoning,
                            broker_message, prediction_id=None,
                            rejection_code=None):
    """Persist one broker-rejected order attempt to the
    `broker_rejections` table. The AI proposed it, the system tried,
    the broker refused — the row exists so the UI can surface it
    and analytics can exclude these from win-rate stats (they didn't
    actually trade).

    `rejection_code` is auto-derived from `broker_message` via
    `classify_broker_rejection_message` when not supplied.

    Returns the row id of the inserted rejection, or None on failure
    (logged warning, never silent — same shape Issue 9 enforces
    everywhere else).
    """
    import logging as _logging
    if rejection_code is None:
        rejection_code = classify_broker_rejection_message(broker_message)
    try:
        conn = _get_conn(db_path)
        cursor = conn.execute(
            "INSERT INTO broker_rejections "
            "(symbol, action, signal_type, ai_confidence, ai_reasoning, "
            " rejection_code, broker_message, prediction_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, action, signal_type, ai_confidence, ai_reasoning,
             rejection_code,
             str(broker_message)[:1000] if broker_message else None,
             prediction_id),
        )
        conn.commit()
        rid = cursor.lastrowid
        conn.close()
        return rid
    except Exception as exc:
        _logging.warning(
            "record_broker_rejection(symbol=%s, code=%s) failed: %s",
            symbol, rejection_code, exc,
        )
        return None


def get_recent_broker_rejections(db_path, hours=24, limit=200):
    """Return broker-rejection rows from the last `hours` for one
    profile DB. Used by the AI Brain panel to surface "REJECTED"
    badges on recently-proposed trades."""
    import logging as _logging
    try:
        conn = _get_conn(db_path)
        rows = conn.execute(
            "SELECT id, timestamp, symbol, action, signal_type, "
            "       ai_confidence, ai_reasoning, rejection_code, "
            "       broker_message, prediction_id "
            "FROM broker_rejections "
            "WHERE timestamp >= datetime('now', ?) "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"-{int(hours)} hours", int(limit)),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        _logging.warning(
            "get_recent_broker_rejections(%s) failed: %s",
            db_path, exc,
        )
        return []


def get_open_entry_metadata(db_path, symbol, occ_symbol=None):
    """Return ai_confidence + ai_reasoning from the most-recent open
    entry (BUY or SHORT) trade row matching `symbol` (or `occ_symbol`
    for option legs). Used by auto-exit close paths so the close row
    inherits the AI's original conviction.

    Without this, stop-loss / take-profit / pair-exit close rows
    show "Auto-exit" on /trades instead of the entry's confidence,
    breaking the trade narrative — the operator can't tell at a
    glance what the AI thought when it took the position that just
    closed.

    Returns a dict with `ai_confidence` and `ai_reasoning`. Both
    are None if no matching open entry is found OR if the read
    fails (logged warning, never silently swallowed — the close
    row still gets logged either way; it just won't carry the
    propagated metadata).
    """
    import logging as _logging
    try:
        conn = _get_conn(db_path)
        if occ_symbol:
            row = conn.execute(
                "SELECT ai_confidence, ai_reasoning FROM trades "
                "WHERE occ_symbol=? "
                "  AND COALESCE(status,'open')='open' "
                "  AND side IN ('buy','short') "
                "ORDER BY timestamp DESC LIMIT 1",
                (occ_symbol,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT ai_confidence, ai_reasoning FROM trades "
                "WHERE symbol=? "
                "  AND occ_symbol IS NULL "
                "  AND COALESCE(status,'open')='open' "
                "  AND side IN ('buy','short') "
                "ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        conn.close()
        if row:
            return {
                "ai_confidence": row["ai_confidence"],
                "ai_reasoning": row["ai_reasoning"],
            }
        return {"ai_confidence": None, "ai_reasoning": None}
    except Exception as exc:
        _logging.warning(
            "get_open_entry_metadata(symbol=%s, occ=%s) failed: %s",
            symbol, occ_symbol, exc,
        )
        return {"ai_confidence": None, "ai_reasoning": None}


def get_virtual_positions(db_path=None, price_fetcher=None):
    """Compute net open positions from the trades table using FIFO lots.

    This is the core of the virtual-account layer: instead of asking
    Alpaca "what do I hold?", we derive it from our own trade records.
    The output shape matches `client.get_positions()` exactly so every
    downstream consumer (trader.py, trade_pipeline.py, views.py) works
    unchanged.

    Args:
        db_path: profile journal DB path.
        price_fetcher: optional `callable(symbol) -> float` that returns
            the current market price. Used for unrealized P&L. When
            None, uses the last trade price as a fallback.

    Returns:
        List of position dicts:
        [{symbol, qty, avg_entry_price, market_value, unrealized_pl,
          unrealized_plpc, current_price}]
    """
    conn = _get_conn(db_path)
    try:
        # Exclude status='canceled' rows — entry orders that never
        # filled at the broker. Without this the phantom stays
        # "open" forever in the FIFO.
        # Pull occ_symbol so option legs are tracked as separate
        # positions from any underlying-stock holding (without this,
        # FIFO mixes a $3.10 option premium with a $416 stock price
        # under the same "MSFT" key and produces nonsense valuations).
        # Fall back to the legacy stock-only query when occ_symbol
        # doesn't exist (older test fixtures with minimal schemas).
        try:
            rows = conn.execute(
                "SELECT symbol, side, qty, price, timestamp, occ_symbol "
                "FROM trades "
                "WHERE COALESCE(status, 'open') != 'canceled' "
                "ORDER BY timestamp ASC, id ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT symbol, side, qty, price, timestamp "
                "FROM trades "
                "WHERE COALESCE(status, 'open') != 'canceled' "
                "ORDER BY timestamp ASC, id ASC"
            ).fetchall()
    except Exception:
        conn.close()
        return []
    conn.close()

    # FIFO lot tracking. Position key is the OCC symbol when present
    # (so each option contract is its own position) or the stock
    # symbol otherwise. Stock holdings and option legs on the same
    # underlying never share a FIFO bucket. The position output
    # carries both `symbol` (underlying for grouping/display) and
    # `occ_symbol` (contract identifier when applicable).
    long_lots: Dict[str, list] = {}
    short_lots: Dict[str, list] = {}
    pos_meta: Dict[str, Dict[str, Any]] = {}  # key -> {symbol, occ_symbol}
    skipped_bad_price = 0
    for row in rows:
        symbol = row[0]
        side = row[1]
        qty = float(row[2] or 0)
        price = float(row[3] or 0)
        occ_symbol = row[5] if len(row) > 5 else None
        if qty <= 0 or price <= 0:
            # Defense-in-depth: a row that gets here with qty<=0 or
            # price<=0 has bad data upstream (e.g. multileg combo
            # writing the signed net premium as the per-leg price —
            # caught 2026-05-11). The row's position is silently
            # invisible to the AI; surface it so the bug class is
            # observable.
            if qty > 0 and price <= 0:
                skipped_bad_price += 1
            continue
        # Position key: OCC for options, underlying symbol for stock.
        key = occ_symbol if occ_symbol else symbol
        if key not in pos_meta:
            pos_meta[key] = {"symbol": symbol, "occ_symbol": occ_symbol}

        if side == "buy":
            long_lots.setdefault(key, []).append([qty, price])
        elif side == "short":
            short_lots.setdefault(key, []).append([qty, price])
        elif side == "sell":
            # Closes a long. FIFO-consume from long_lots first.
            remaining = qty
            ll = long_lots.setdefault(key, [])
            while remaining > 0 and ll:
                consumed = min(ll[0][0], remaining)
                ll[0][0] -= consumed
                remaining -= consumed
                if ll[0][0] <= 0.001:
                    ll.pop(0)
            # OPTION SELL-TO-OPEN: a multileg short leg writes
            # side='sell' (not 'short') because OptionLeg.side is
            # 'buy'/'sell' — the same overloaded vocabulary stocks
            # use for close-a-long. For options specifically, a
            # `side='sell'` row with no long lot to consume is a
            # sell-to-open (short option position), not a phantom
            # close. Without this branch, every multileg short leg
            # silently produces zero position state and the AI
            # thinks the spread is just the long leg.
            # Caught 2026-05-11 (same incident that surfaced the
            # combo-net price bug).
            if remaining > 0 and occ_symbol:
                short_lots.setdefault(key, []).append(
                    [remaining, price]
                )
        elif side == "cover":
            # Closes a short. FIFO-consume from short_lots.
            remaining = qty
            sl = short_lots.setdefault(key, [])
            while remaining > 0 and sl:
                consumed = min(sl[0][0], remaining)
                sl[0][0] -= consumed
                remaining -= consumed
                if sl[0][0] <= 0.001:
                    sl.pop(0)

    # Build position dicts. A position-key can have BOTH a long and
    # a short open (rare for stock; common for option spreads where
    # the same OCC could have offsetting legs); we net them and
    # report a single position with the net signed qty.
    positions = []
    all_keys = set(long_lots.keys()) | set(short_lots.keys())
    for key in all_keys:
        long_remaining = sum(lot[0] for lot in long_lots.get(key, []))
        short_remaining = sum(lot[0] for lot in short_lots.get(key, []))
        net_qty = long_remaining - short_remaining
        if abs(net_qty) < 0.001:
            continue

        if net_qty > 0:
            lots = long_lots[key]
        else:
            lots = short_lots[key]
        total_qty = abs(net_qty)
        dominant_remaining = long_remaining if net_qty > 0 else short_remaining

        total_cost = sum(lot[0] * lot[1] for lot in lots if lot[0] > 0)
        avg_entry = (
            total_cost / dominant_remaining if dominant_remaining > 0 else 0
        )

        meta = pos_meta.get(key, {"symbol": key, "occ_symbol": None})
        symbol = meta["symbol"]
        occ_symbol = meta["occ_symbol"]
        is_option = bool(occ_symbol)

        # Current price: ask the price_fetcher for the position key
        # (OCC for options, underlying symbol for stock). The fetcher
        # routes OCC symbols to the option-quote path. We pass the
        # position direction (long/short via `side`) so option
        # fetchers can use the conservative side of a one-sided
        # market — a LONG holder values at the bid (their exit
        # price), a SHORT at the ask. Using the offer side on a
        # long inflates the mark when the bid is 0.
        is_short_dir = (net_qty < 0)
        side_hint = "sell" if is_short_dir else "buy"
        current_price = 0.0
        if price_fetcher:
            try:
                # Older fetchers don't accept `side` — fall back to
                # the single-arg form when TypeError surfaces.
                try:
                    current_price = float(
                        price_fetcher(key, side=side_hint) or 0
                    )
                except TypeError:
                    current_price = float(price_fetcher(key) or 0)
            except Exception:
                pass
        if current_price <= 0:
            current_price = avg_entry  # fallback: no price change assumed

        # Sign convention matches Alpaca: long qty>0, short qty<0.
        # Options have a 100x contract multiplier on dollar values.
        contract_mult = 100 if is_option else 1
        is_short = is_short_dir  # already computed above for side_hint
        signed_qty = -total_qty if is_short else total_qty
        if is_short:
            unrealized_pl = (avg_entry - current_price) * total_qty * contract_mult
            unrealized_plpc = (
                (avg_entry - current_price) / avg_entry if avg_entry > 0 else 0
            )
            market_value = -current_price * total_qty * contract_mult
        else:
            unrealized_pl = (current_price - avg_entry) * total_qty * contract_mult
            unrealized_plpc = (
                (current_price - avg_entry) / avg_entry if avg_entry > 0 else 0
            )
            market_value = current_price * total_qty * contract_mult

        # Phase 1 Position-class refactor: build the canonical row
        # then wrap as a Position object. The shim lets every existing
        # consumer (pos["symbol"], pos.get("qty"), etc.) keep working
        # unchanged. New code uses pos.broker_symbol / pos.is_option /
        # pos.is_short directly.
        from position import Position
        row = {
            "symbol": symbol,
            "occ_symbol": occ_symbol,
            "qty": round(signed_qty, 4),
            "avg_entry_price": round(avg_entry, 4),
            "current_price": round(current_price, 4),
            "market_value": round(market_value, 2),
            "unrealized_pl": round(unrealized_pl, 2),
            "unrealized_plpc": round(unrealized_plpc, 6),
        }
        positions.append(Position.from_virtual_row(row))

    if skipped_bad_price > 0:
        import logging as _logging
        _logging.warning(
            "get_virtual_positions(%s): skipped %d row(s) with qty>0 "
            "but price<=0 — these positions are invisible to the AI's "
            "portfolio view. Likely the multileg combo writing the "
            "signed net premium as the per-leg price (caught "
            "2026-05-11). Run the backfill to fix existing rows.",
            db_path, skipped_bad_price,
        )

    return positions


def get_virtual_account_info(db_path=None, initial_capital=100000.0,
                             price_fetcher=None):
    """Compute virtual account info from the trades table.

    Returns a dict matching `client.get_account_info()` shape:
    {equity, buying_power, cash, portfolio_value, status}

    Cash is computed from money flows:
        cash = initial_capital - sum(BUY costs) + sum(SELL proceeds)

    Portfolio value = sum(current_price × qty) for open positions.
    Equity = cash + portfolio_value.
    Buying power = cash (no margin on paper).
    """
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT side, qty, price FROM trades"
        ).fetchall()
    except Exception:
        conn.close()
        return {
            "equity": initial_capital,
            "buying_power": initial_capital,
            "cash": initial_capital,
            "portfolio_value": 0.0,
            "status": "ACTIVE",
        }
    conn.close()

    total_buys = 0.0
    total_sells = 0.0
    for row in rows:
        side = row[0]
        qty = float(row[1] or 0)
        price = float(row[2] or 0)
        if qty <= 0 or price <= 0:
            continue
        if side == "buy":
            total_buys += qty * price
        elif side in ("sell", "cover"):
            total_sells += qty * price

    cash = initial_capital - total_buys + total_sells

    positions = get_virtual_positions(db_path=db_path, price_fetcher=price_fetcher)
    portfolio_value = sum(p["market_value"] for p in positions)
    equity = cash + portfolio_value

    return {
        "equity": round(equity, 2),
        "buying_power": round(max(cash, 0), 2),
        "cash": round(cash, 2),
        "portfolio_value": round(portfolio_value, 2),
        "status": "ACTIVE",
    }


def reconcile_trade_statuses(db_path=None, open_symbols=None):
    """Fix up `trades.status` AND compute realized P&L on BUY rows from
    their matching SELL exits, so the trades page shows a dollar value
    on every closed trade instead of a useless "closed" label.

    Three problems this corrects:
      1. SELL rows that were logged without status="closed" even though
         they have realized pnl.
      2. BUY rows for symbols no longer held — marked closed.
      3. Closed BUY rows with NULL pnl — populated via FIFO matching
         to the symbol's SELL rows (proceeds − cost per BUY lot).

    FIFO matching: for each symbol, walk trades in timestamp order.
    Every BUY opens a lot (qty remaining, entry price). Every SELL
    consumes qty from the oldest open lots first and attributes the
    realized P&L back to each consumed lot's BUY row.

    Args:
        db_path: profile journal DB path.
        open_symbols: optional set of symbols currently held in Alpaca.
            When provided, used as ground truth for status updates.

    Returns dict with counts: {"sells_fixed", "buys_fixed", "pnl_computed"}.
    """
    conn = _get_conn(db_path)

    # 1. SELL rows with pnl but open status
    cur = conn.execute(
        "UPDATE trades SET status='closed' "
        "WHERE side='sell' AND pnl IS NOT NULL AND status='open'"
    )
    sells_fixed = cur.rowcount

    # 2. BUY rows for closed positions
    if open_symbols is not None:
        placeholders = ",".join("?" * len(open_symbols)) if open_symbols else "''"
        if open_symbols:
            cur = conn.execute(
                f"UPDATE trades SET status='closed' "
                f"WHERE side='buy' AND status='open' "
                f"AND symbol NOT IN ({placeholders})",
                list(open_symbols),
            )
        else:
            cur = conn.execute(
                "UPDATE trades SET status='closed' "
                "WHERE side='buy' AND status='open'"
            )
        buys_fixed = cur.rowcount
    else:
        cur = conn.execute(
            "UPDATE trades SET status='closed' "
            "WHERE side='buy' AND status='open' AND symbol IN ("
            "  SELECT DISTINCT symbol FROM trades "
            "  WHERE side='sell' AND pnl IS NOT NULL"
            ")"
        )
        buys_fixed = cur.rowcount

    # 3. FIFO-match BUY lots to SELLs to compute realized pnl on BUY rows
    pnl_computed = 0
    symbols = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM trades "
            "WHERE side='buy' AND status='closed' AND pnl IS NULL"
        ).fetchall()
    ]
    for symbol in symbols:
        rows = conn.execute(
            "SELECT id, side, qty, price, pnl, timestamp "
            "FROM trades WHERE symbol=? "
            "ORDER BY timestamp ASC, id ASC",
            (symbol,),
        ).fetchall()
        # Lots: list of [buy_id, qty_remaining, entry_price, realized_pnl]
        lots = []
        for r in rows:
            tid, side, qty, price, row_pnl, ts = r
            qty = float(qty or 0)
            price = float(price or 0)
            if side == "buy":
                lots.append([tid, qty, price, 0.0])
            elif side == "sell" and qty > 0 and price > 0:
                remaining = qty
                for lot in lots:
                    if remaining <= 0:
                        break
                    if lot[1] <= 0:
                        continue
                    consumed = min(lot[1], remaining)
                    # Realized for this slice: (exit - entry) × qty
                    lot[3] += (price - lot[2]) * consumed
                    lot[1] -= consumed
                    remaining -= consumed
        # BUY rows no longer get pnl backfilled — realized P&L belongs
        # on the SELL row only. The UI now has separate Unrealized and
        # Realized columns so there's no need to duplicate the number.

    conn.commit()
    conn.close()
    return {
        "sells_fixed": sells_fixed,
        "buys_fixed": buys_fixed,
        "pnl_computed": pnl_computed,
    }


def log_signal(symbol, signal, strategy=None, reason=None, price=None,
               indicators=None, acted_on=False, db_path=None):
    """Log a strategy signal to the journal.

    Args:
        indicators: dict of indicator values; stored as JSON.
    Returns the row id.
    """
    conn = _get_conn(db_path)
    indicators_json = json.dumps(indicators) if indicators else None
    cursor = conn.execute(
        """INSERT INTO signals
           (timestamp, symbol, strategy, signal, reason, price, indicators, acted_on)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            symbol, strategy, signal, reason, price,
            indicators_json, int(acted_on),
        ),
    )
    conn.commit()
    signal_id = cursor.lastrowid
    conn.close()
    return signal_id


def log_daily_snapshot(equity, cash, portfolio_value, num_positions, daily_pnl=None,
                       db_path=None):
    """Log an end-of-day portfolio snapshot.

    Uses INSERT OR REPLACE against the UNIQUE(date) constraint so that
    if the writer fires more than once on the same calendar day (deploy
    restart, manual re-run, etc.) the latest snapshot overwrites the
    earlier one instead of accumulating duplicate rows.

    Returns the row id.
    """
    # ET-localized date so the snapshot's "day" matches what a US-market
    # user expects. The droplet runs in UTC; date.today() would roll into
    # the next calendar day at midnight UTC (~8pm ET), causing late-day
    # snapshots to land under tomorrow's date from the user's perspective.
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    conn = _get_conn(db_path)
    cursor = conn.execute(
        """INSERT OR REPLACE INTO daily_snapshots
           (date, equity, cash, portfolio_value, num_positions, daily_pnl)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            today_et.isoformat(),
            equity, cash, portfolio_value, num_positions, daily_pnl,
        ),
    )
    conn.commit()
    snapshot_id = cursor.lastrowid
    conn.close()
    return snapshot_id


def get_trade_history(symbol=None, limit=50, db_path=None):
    """Return recent trades, optionally filtered by symbol.

    Returns a list of dicts.
    """
    conn = _get_conn(db_path)
    if symbol:
        rows = conn.execute(
            "SELECT * FROM trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_performance_summary(db_path=None):
    """Return aggregate performance metrics from the trade journal.

    Returns a dict with total_trades, winning_trades, losing_trades, win_rate,
    total_pnl, avg_pnl, best_trade, worst_trade.
    """
    conn = _get_conn(db_path)

    total = conn.execute("SELECT COUNT(*) FROM trades WHERE pnl IS NOT NULL").fetchone()[0]
    if total == 0:
        conn.close()
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
        }

    row = conn.execute("""
        SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
            SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losing_trades,
            SUM(pnl) AS total_pnl,
            AVG(pnl) AS avg_pnl,
            MAX(pnl) AS best_trade,
            MIN(pnl) AS worst_trade
        FROM trades
        WHERE pnl IS NOT NULL
    """).fetchone()

    conn.close()

    total_trades = row["total_trades"]
    winning = row["winning_trades"]

    return {
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": row["losing_trades"],
        "win_rate": (winning / total_trades * 100) if total_trades > 0 else 0.0,
        "total_pnl": row["total_pnl"] or 0.0,
        "avg_pnl": row["avg_pnl"] or 0.0,
        "best_trade": row["best_trade"] or 0.0,
        "worst_trade": row["worst_trade"] or 0.0,
    }


def get_signal_history(symbol=None, limit=100, db_path=None):
    """Return recent signals, optionally filtered by symbol.

    Returns a list of dicts with indicators parsed from JSON.
    """
    conn = _get_conn(db_path)
    if symbol:
        rows = conn.execute(
            "SELECT * FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        if d.get("indicators"):
            try:
                d["indicators"] = json.loads(d["indicators"])
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(d)
    return results


def get_equity_curve(days=30, db_path=None):
    """Return daily equity snapshots for charting.

    Returns a list of dicts with date, equity, portfolio_value, daily_pnl.
    """
    conn = _get_conn(db_path)
    rows = conn.execute(
        """SELECT date, equity, cash, portfolio_value, num_positions, daily_pnl
           FROM daily_snapshots
           ORDER BY date DESC
           LIMIT ?""",
        (days,),
    ).fetchall()
    conn.close()
    # Return in chronological order
    return [dict(r) for r in reversed(rows)]


def get_slippage_stats(db_path=None, kind=None):
    """Return slippage statistics from trades that have fill_price data.

    Args:
        kind: 'stocks' (occ_symbol IS NULL), 'options' (occ_symbol
            IS NOT NULL), or None (all). Wired 2026-05-11 (Phase 1
            of instrument-class pipeline refactor).

            Mixing stock and option rows in one slippage aggregate
            inflates `avg_slippage_pct` to impossible values
            (1130% observed in prod 2026-05-11) because option
            premiums move 10-100% per cycle on small underlying
            moves and pollute the average. Per-pipeline metrics
            (metrics.stock / metrics.option) call this with kind=
            set so each instrument class gets its own clean
            aggregate.

    The total_slippage_cost is the SIGNED net economic cost — favorable
    slippage (buying cheaper than decision price, selling higher than
    decision) reduces it; adverse slippage adds to it. The previous
    formulation summed ABS(fill - decision) * qty which double-counted
    favorable executions as cost and inflated the dashboard numbers
    by ~4× vs the real impact on P&L.

    For BUY / sell_short (entries that consume capital): cost is positive
    when fill_price > decision_price (we paid more / received less).

    For SELL / cover (exits that return capital): cost is positive when
    fill_price < decision_price (we received less than expected).

    `total_slippage_magnitude` (also returned) is the absolute version —
    sum of |fill - decision| * qty — useful as a measure of execution
    variance independent of direction.
    """
    conn = _get_conn(db_path)
    where_kind = ""
    if kind == "stocks":
        where_kind = " AND occ_symbol IS NULL"
    elif kind == "options":
        where_kind = " AND occ_symbol IS NOT NULL"
    try:
        row = conn.execute(f"""
            SELECT
                COUNT(*) AS trades_with_fills,
                AVG(slippage_pct) AS avg_slippage_pct,
                MAX(ABS(slippage_pct)) AS worst_slippage_pct,
                SUM(ABS(fill_price - decision_price) * qty)
                    AS total_slippage_magnitude,
                SUM(
                    CASE
                        WHEN side IN ('buy', 'sell_short')
                            THEN (fill_price - decision_price) * qty
                        WHEN side IN ('sell', 'cover', 'short')
                            THEN (decision_price - fill_price) * qty
                        ELSE 0
                    END
                ) AS total_slippage_cost
            FROM trades
            WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
              AND decision_price > 0{where_kind}
        """).fetchone()

        if not row or row["trades_with_fills"] == 0:
            conn.close()
            return None

        # Get the worst slippage trade details — same kind filter
        # so the row shown matches the aggregate's instrument class.
        worst = conn.execute(f"""
            SELECT symbol, side, qty, decision_price, fill_price, slippage_pct, timestamp
            FROM trades
            WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
              AND decision_price > 0{where_kind}
            ORDER BY ABS(slippage_pct) DESC
            LIMIT 1
        """).fetchone()

        conn.close()
        return {
            "trades_with_fills": row["trades_with_fills"],
            "avg_slippage_pct": round(row["avg_slippage_pct"] or 0, 4),
            "worst_slippage_pct": round(row["worst_slippage_pct"] or 0, 4),
            "total_slippage_cost": round(row["total_slippage_cost"] or 0, 2),
            "total_slippage_magnitude": round(row["total_slippage_magnitude"] or 0, 2),
            "worst_trade": dict(worst) if worst else None,
        }
    except Exception:
        conn.close()
        return None


def link_option_prediction_to_trade(db_path, symbol, signal,
                                      option_order_id=None,
                                      occ_symbol=None,
                                      max_age_minutes=10):
    """Phase 5c of pipeline refactor (2026-05-11): link an option
    prediction row in `ai_predictions` to its broker order so the
    Phase 5c resolver can fetch the right premium / spread legs at
    resolution time.

    Called from `trade_pipeline.py` immediately after a successful
    option trade execution. Finds the most recent pending
    ai_predictions row matching `(symbol, predicted_signal,
    status='pending')` within `max_age_minutes` and UPDATEs its
    `option_order_id` and/or `occ_symbol` columns. Idempotent;
    safely no-ops when no matching row exists (the prediction may
    not have been recorded if `record_prediction` failed for any
    reason — don't crash the trade-execution flow).

    Returns True if a row was updated, False otherwise.
    """
    if not db_path or not symbol or not signal:
        return False
    if not option_order_id and not occ_symbol:
        return False
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow()
                  - timedelta(minutes=max_age_minutes)).isoformat()
        conn = _get_conn(db_path)
        try:
            # Find the latest pending row for this symbol+signal.
            row = conn.execute(
                """SELECT id FROM ai_predictions
                   WHERE symbol = ? AND predicted_signal = ?
                   AND status = 'pending'
                   AND timestamp >= ?
                   ORDER BY id DESC LIMIT 1""",
                (symbol.upper(), signal.upper(), cutoff),
            ).fetchone()
            if row is None:
                return False
            pred_id = row[0]
            # Build dynamic UPDATE — only set the columns we have.
            sets, vals = [], []
            if option_order_id:
                sets.append("option_order_id = ?")
                vals.append(str(option_order_id))
            if occ_symbol:
                sets.append("occ_symbol = ?")
                vals.append(str(occ_symbol))
            vals.append(pred_id)
            conn.execute(
                f"UPDATE ai_predictions SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        # Non-fatal — the trade still went through. Resolver will
        # just defer this row (Phase 5b safety floor still applies).
        return False


def get_multileg_legs_by_combo_order(db_path, combo_order_id):
    """Phase 5c of pipeline refactor (2026-05-11): return the leg
    rows for a multileg combo, used by the option-aware resolver
    to compute net spread P&L.

    A combo is identified by either:
      - `order_id == combo_order_id` (combo path: every leg shares
        the parent's order_id)
      - `reason LIKE '%(combo=<combo_order_id>)%'` (sequential path:
        each leg has its own order_id; the combo id is in the
        reason string)

    Returns a list of dicts {occ_symbol, qty, price, side} suitable
    for net-P&L computation. Empty list when no legs found.
    """
    if not db_path or not combo_order_id:
        return []
    try:
        conn = _get_conn(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT occ_symbol, qty, price, side, fill_price
                   FROM trades
                   WHERE signal_type = 'MULTILEG'
                   AND (order_id = ? OR reason LIKE ?)""",
                (str(combo_order_id),
                 f"%(combo={combo_order_id})%"),
            ).fetchall()
            return [
                {
                    "occ_symbol": r["occ_symbol"],
                    "qty": float(r["qty"] or 0),
                    "price": float(r["fill_price"] or r["price"] or 0),
                    "side": r["side"],
                }
                for r in rows
                if r["occ_symbol"]
            ]
        finally:
            conn.close()
    except Exception:
        return []


def get_specialist_veto_stats(db_paths, days=7):
    """Aggregate per-specialist verdict counts across profiles.

    Distinguishes "claimed" vetoes (any specialist that wrote VETO into
    specialist_outcomes) from "effective" vetoes (those that actually
    blocked a trade — only specialists in ensemble.VETO_AUTHORIZED have
    that authority). Surfaces a real and easy-to-miss bug: an unauthorized
    specialist emitting VETO is silently a no-op, which looks like
    healthy disagreement on the dashboard but contributes nothing.

    Args:
        db_paths: list of profile journal DB paths.
        days: lookback window in days.

    Returns dict shaped:
      {
        "window_days": int,
        "by_specialist": [
          {"name", "total", "vetoes", "veto_rate_pct", "has_authority",
           "effective_vetoes"},
          ...
        ],
        "total_vetoes_claimed": int,
        "total_vetoes_effective": int,
      }
    Sorted by veto count descending.
    """
    try:
        from ensemble import VETO_AUTHORIZED
    except Exception:
        VETO_AUTHORIZED = {"risk_assessor", "adversarial_reviewer"}

    counts = {}  # name -> {"total": int, "vetoes": int}
    for db_path in db_paths:
        try:
            conn = _get_conn(db_path)
            rows = conn.execute(
                """SELECT specialist_name, verdict, COUNT(*) as n
                   FROM specialist_outcomes
                   WHERE recorded_at > datetime('now', '-' || ? || ' days')
                   GROUP BY specialist_name, verdict""",
                (days,),
            ).fetchall()
            conn.close()
        except Exception:
            continue
        for row in rows:
            name = row[0]
            verdict = row[1]
            n = int(row[2] or 0)
            entry = counts.setdefault(name, {"total": 0, "vetoes": 0})
            entry["total"] += n
            if verdict == "VETO":
                entry["vetoes"] += n

    by_specialist = []
    for name, c in counts.items():
        has_authority = name in VETO_AUTHORIZED
        rate = (c["vetoes"] / c["total"] * 100) if c["total"] else 0.0
        by_specialist.append({
            "name": name,
            "total": c["total"],
            "vetoes": c["vetoes"],
            "veto_rate_pct": round(rate, 1),
            "has_authority": has_authority,
            "effective_vetoes": c["vetoes"] if has_authority else 0,
        })
    by_specialist.sort(key=lambda x: (-x["vetoes"], x["name"]))

    return {
        "window_days": days,
        "by_specialist": by_specialist,
        "total_vetoes_claimed": sum(s["vetoes"] for s in by_specialist),
        "total_vetoes_effective": sum(
            s["effective_vetoes"] for s in by_specialist),
    }
