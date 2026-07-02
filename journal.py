"""SQLite trade journal for logging trades, signals, and portfolio snapshots."""

import logging
import sqlite3
import json
from contextlib import closing
from datetime import datetime, date, timedelta

import config

logger = logging.getLogger(__name__)


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
    try:
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

            -- #185 (2026-05-20): multi-horizon outcomes table. Stores
            -- the realized return at each of several horizons (1d, 3d,
            -- 5d, 10d, 20d) per prediction, plus max-favorable /
            -- max-adverse excursion within each window. Built as a
            -- sibling table (not wide columns on ai_predictions) so:
            --   - new horizons cost a row insert, not a migration
            --   - per-prediction partial-fill states are clean row
            --     existence rather than NULLs scattered across columns
            --   - the future fine-tune dataset builder gets one
            --     SELECT ... json_group_array() over this table to
            --     produce a clean multi-label vector per prediction
            --
            -- MFE/MAE columns aren't in the literal ticket text but
            -- are essential for fine-tuning: they separate directional
            -- skill from timing skill (a trade that hit +5% then
            -- closed at +1% is "right call, weak hold" — fine-tune
            -- needs that distinction to avoid wrong-lesson learning).
            -- Sign convention: MFE/MAE are signed by prediction
            -- DIRECTION (long: high - entry vs low - entry; short:
            -- entry - low vs entry - high) so positive MFE always
            -- means "the prediction was right at some point" no
            -- matter the side.
            CREATE TABLE IF NOT EXISTS ai_prediction_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id INTEGER NOT NULL REFERENCES ai_predictions(id),
                horizon_days INTEGER NOT NULL,
                price_at_horizon REAL,
                return_pct REAL NOT NULL,
                return_pct_net REAL,
                mfe_pct REAL,
                mae_pct REAL,
                outcome_class TEXT,
                measured_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (prediction_id, horizon_days)
            );
            CREATE INDEX IF NOT EXISTS idx_outcomes_pred
                ON ai_prediction_outcomes(prediction_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_horizon
                ON ai_prediction_outcomes(horizon_days);
            CREATE INDEX IF NOT EXISTS idx_outcomes_class
                ON ai_prediction_outcomes(outcome_class);

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

            -- 2026-07-01 (selection-engine P3/P4) — per-proposal option
            -- outcomes. ONE row per option proposal the AI sent to the option
            -- pipeline: vetoed (1) or accepted (0), keyed by the SPREAD
            -- strategy + sector, so the opportunity ledger can discount an
            -- option's RAR by THIS profile's own per-(strategy x sector) veto
            -- rate BEFORE selection (stops the AI wasting picks on spreads its
            -- own specialists will only veto). For vetoed rows we also capture
            -- enough to resolve the spread's TRUE would-be P&L from its own
            -- legs (P4). PHYSICALLY SEPARATE from ai_predictions on purpose:
            -- a would-be/shadow outcome can NEVER contaminate real-trade
            -- reputation / meta-model / win-rate stats. Own-book only (this
            -- profile's DB file is the isolation boundary).
            CREATE TABLE IF NOT EXISTS option_proposal_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                sector TEXT,
                vetoed INTEGER NOT NULL,
                veto_reason TEXT,
                confidence REAL,
                -- would-be resolution fields (P4; NULL for accepted rows).
                -- lo_strike/hi_strike + strategy direction + max_loss/gain let
                -- the resolver compute the spread's INTRINSIC expiry P&L from
                -- the underlying's close alone (no illiquid near-expiry option
                -- marks) — the accurate path.
                entry_net_premium REAL,
                max_loss_per_contract REAL,
                max_gain_per_contract REAL,
                breakeven REAL,
                lo_strike REAL,
                hi_strike REAL,
                expiry TEXT,
                legs_json TEXT,
                status TEXT DEFAULT 'pending',
                wouldbe_outcome TEXT,
                wouldbe_pnl REAL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_opo_strategy_sector
                ON option_proposal_outcomes(strategy, sector);
            CREATE INDEX IF NOT EXISTS idx_opo_status
                ON option_proposal_outcomes(status);

            -- 2026-06-09 — PRE-broker drop log. broker_rejections (above)
            -- records what Alpaca rejected. trade_drops records what
            -- trade_pipeline.execute_trade dropped BEFORE submission —
            -- the silent-skip paths that were invisible until this
            -- table (instrumentation revealed they exist; the AI Brain
            -- panel's "BLOCKED" badge previously showed nothing more
            -- specific because there was no source-of-truth for why).
            -- Every early-return in execute_trade now writes a row.
            CREATE TABLE IF NOT EXISTS trade_drops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                symbol TEXT NOT NULL,
                side TEXT,
                drop_code TEXT NOT NULL,
                drop_reason TEXT NOT NULL,
                cycle_id TEXT,
                ai_confidence INTEGER,
                ai_reasoning TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trade_drops_timestamp
                ON trade_drops(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_trade_drops_symbol_ts
                ON trade_drops(symbol, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_trade_drops_cycle
                ON trade_drops(cycle_id);

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
            --
            -- `call_id` is added via _migrate_all_columns for existing
            -- DBs, so the corresponding index is created post-migration
            -- (idx_ai_cost_call_id is built in _post_migration_indexes).
            CREATE TABLE IF NOT EXISTS ai_cost_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                purpose TEXT,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                call_id TEXT,
                -- 2026-07-02 — prompt tokens served from the provider's
                -- implicit cache (subset of input_tokens; billed ~10% of the
                -- input rate). Captured so cache hits price honestly and any
                -- caching claim is measurable, never assumed.
                cached_tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ai_cost_ts
                ON ai_cost_ledger(timestamp DESC);

            -- Shadow model evaluation: parallel candidate-model calls
            -- captured for offline comparison. The primary Haiku call
            -- writes one row to ai_cost_ledger; each shadow call writes
            -- one row here, joinable on call_id.
            CREATE TABLE IF NOT EXISTS ai_shadow_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                purpose TEXT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_hash TEXT,
                prompt_text TEXT,
                raw_response TEXT,
                parsed_signal TEXT,
                latency_ms INTEGER,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                error TEXT,
                agreement INTEGER,
                primary_provider TEXT,
                primary_model TEXT,
                primary_response TEXT,
                primary_parsed TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ai_shadow_ts
                ON ai_shadow_calls(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_shadow_call_id
                ON ai_shadow_calls(call_id);
            CREATE INDEX IF NOT EXISTS idx_ai_shadow_purpose
                ON ai_shadow_calls(purpose);

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

            -- 2026-06-05 — regime snapshot per Scan & Trade cycle. Captures
            -- the market state in which the AI made its picks, so post-hoc
            -- analysis can correlate AI confidence with realized outcomes
            -- conditional on regime (SPY move, VIX, halted sectors,
            -- intraday alerts). Replaces in-pipeline blocking: instead of
            -- gating trades, we record the conditions and let analysis
            -- ask "did the AI's picks at this confidence level work out
            -- under regime X?"
            CREATE TABLE IF NOT EXISTS cycle_regime (
                cycle_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                spy_move_pct REAL,
                vix_level REAL,
                halted_sectors_json TEXT,
                intraday_alerts_json TEXT,
                sector_moves_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cycle_regime_time
                ON cycle_regime(created_at DESC);

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

            -- Phase 5d of pipeline refactor (2026-05-11): one-time
            -- migration markers. The Phase 5d historical option-row
            -- backfill checks `WHERE key='phase_5d_option_backfill'`
            -- to know whether it has already run on this DB. Generic
            -- design — future one-shot migrations register here.
            CREATE TABLE IF NOT EXISTS migration_markers (
                key TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT (datetime('now')),
                details TEXT
            );

            -- 2026-05-19 — Scope C of the per-pipeline refactor:
            -- shadow-eval the new Pipeline.run_cycle dispatch path
            -- against the legacy trade_pipeline.run_trade_cycle.
            -- Every cycle (when ctx.enable_pipeline_shadow_eval=1)
            -- writes one row here capturing how the two paths'
            -- specialist-routing decisions compared. Read-only —
            -- never affects broker calls or journal trades.
            -- Queryable later: "over the last N cycles, how often
            -- did the two paths agree on which trades to approve?"
            CREATE TABLE IF NOT EXISTS pipeline_shadow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                cycle_id TEXT,
                legacy_proposal_count INTEGER NOT NULL DEFAULT 0,
                pipeline_proposal_count INTEGER NOT NULL DEFAULT 0,
                legacy_approved_count INTEGER NOT NULL DEFAULT 0,
                pipeline_approved_count INTEGER NOT NULL DEFAULT 0,
                legacy_vetoed_count INTEGER NOT NULL DEFAULT 0,
                pipeline_vetoed_count INTEGER NOT NULL DEFAULT 0,
                legacy_symbols TEXT,
                pipeline_symbols TEXT,
                symbols_diff TEXT,
                verdict_diff TEXT,
                duration_ms REAL,
                success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pipeline_shadow_runs_ts
                ON pipeline_shadow_runs(timestamp DESC);

            -- 2026-05-19 (Phase B1 data-collection upgrade) —
            -- append-only history of the per-cycle decision context.
            -- Today's `cycle_data_{profile_id}.json` holds the same
            -- shape but gets OVERWRITTEN every 15 minutes. Without
            -- history, fine-tuning loses cross-candidate context
            -- (what other candidates the AI saw in the same prompt,
            -- their relative ranks, what got picked vs HOLD'd).
            --
            -- ai_predictions.cycle_id is a FK pointing back here so
            -- training data can reconstruct the full cycle a single
            -- prediction came from.
            CREATE TABLE IF NOT EXISTS ai_cycles (
                cycle_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                profile_id INTEGER,
                regime TEXT,
                vix REAL,
                ai_reasoning TEXT,
                shortlist_json TEXT,
                market_context_json TEXT,
                sector_rotation_json TEXT,
                learned_patterns_json TEXT,
                meta_model_stats_json TEXT,
                ensemble_summary_json TEXT,
                n_trades_selected INTEGER NOT NULL DEFAULT 0,
                n_candidates_in_shortlist INTEGER NOT NULL DEFAULT 0,
                -- 2026-06-15 — full per-trade decision list so the
                -- dashboard's AI-Brain history can replay each past
                -- cycle's TRADES SELECTED panel verbatim (badges,
                -- confidence, per-trade reasoning). Pre-existing rows
                -- have NULL here; the history view falls back to the
                -- count + cycle reasoning + trade_drops for those.
                trades_selected_json TEXT,
                -- 2026-07-02 (storage dedupe) — the cycle's batch prompt +
                -- raw AI response stored ONCE here instead of duplicated on
                -- every ai_predictions row of the cycle (measured 6.15x /
                -- ~31 MB/day of byte-identical duplication). The fine-tune
                -- dataset builder joins predictions -> ai_cycles on cycle_id;
                -- pre-dedupe prediction rows keep their own copies.
                prompt_text TEXT,
                raw_response_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ai_cycles_ts
                ON ai_cycles(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_cycles_profile
                ON ai_cycles(profile_id, timestamp DESC);

            -- Freshness ledger for the broker/journal divergence invariant
            -- (2026-06-23). Records, per symbol, the cycle epoch at which
            -- THIS profile's journal for that symbol was last reconciled to
            -- broker truth. The oversell door refuses to act on any symbol
            -- whose epoch is older than the live cycle epoch (cycle_epoch).
            CREATE TABLE IF NOT EXISTS reconcile_state (
                symbol                TEXT PRIMARY KEY,
                last_reconciled_epoch INTEGER NOT NULL DEFAULT 0,
                updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Durable-journaling recovery ledger (2026-06-23). The oversell
            -- door records every order the broker accepts here the moment it
            -- returns — BEFORE the full log_trade row is written. If that
            -- journal write is then lost (DB lock / disk), the order_id
            -- survives so the reconciler can reconstruct the trades row from
            -- the broker fill instead of orphan-halting an unowned position.
            CREATE TABLE IF NOT EXISTS submitted_orders (
                order_id     TEXT PRIMARY KEY,
                symbol       TEXT,
                side         TEXT,        -- the BROKER side: 'buy' / 'sell'
                qty          REAL,
                occ_symbol   TEXT,
                intent       TEXT,        -- 'open_short' for a deliberate short
                                          -- entry; NULL otherwise. Lets the
                                          -- reconstruct map broker side ->
                                          -- journal side without ambiguity.
                submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # Universal schema migration: ensures every column defined in the
        # CREATE TABLE statements above actually exists in the DB. Catches
        # any column added to the schema that wasn't present when the DB
        # was first created. Replaces the old per-column migration functions.
        _migrate_all_columns(conn)

        # Post-migration indexes. Any index referencing a column that
        # only exists after migration must be built here, AFTER
        # _migrate_all_columns has done its ALTER TABLE pass. Building
        # them inside the executescript above would fail with "no such
        # column" on pre-existing DBs that haven't picked up the
        # column yet.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_ai_cost_call_id "
            "ON ai_cost_ledger(call_id)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as _idx_exc:
                logger.debug(
                    "post-migration index skipped: %s: %s",
                    type(_idx_exc).__name__, _idx_exc,
                )

        # daily_snapshots: dedupe + add UNIQUE(date) constraint.
        # Existing DBs created before 2026-04-28 had no UNIQUE constraint
        # and accumulated duplicate rows when the scheduler restarted
        # before the marker-file fix landed. Combine the dedupe and the
        # constraint addition in one table-rebuild migration.
        _migrate_daily_snapshots_unique(conn)

        conn.commit()
    finally:
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
        with closing(_get_conn(db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO recently_exited_symbols "
                "(symbol, exited_at, trigger, exit_price) "
                "VALUES (?, datetime('now'), 'wash_cooldown', NULL)",
                (symbol,),
            )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _wc_exc:
        # Wash-cooldown marker write; cooldown is best-effort and
        # self-heals on next exit. Surface for follow-up.
        logger.debug(
            "wash_cooldown marker write failed for %s: %s: %s",
            symbol, type(_wc_exc).__name__, _wc_exc,
        )


def get_wash_cooldown_symbols(db_path: str, days: int = 30) -> set:
    """Return symbols currently in wash-trade cooldown (30-day window)."""
    if not db_path:
        return set()
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT symbol FROM recently_exited_symbols "
                "WHERE trigger = 'wash_cooldown' "
                "AND exited_at >= datetime('now', ?)",
                (f"-{int(days)} days",),
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def record_htb_cooldown(db_path: str, symbol: str) -> None:
    """Mark a symbol as learned hard-to-borrow from a broker order rejection.

    Alpaca's asset-level ``easy_to_borrow`` flag is not always truthful:
    SPCX, for one, reports ``easy_to_borrow=True`` yet the order engine
    rejects every GTC protective stop with *"only day orders are allowed
    for hard-to-borrow asset"*. The asset flag lied; the order engine is
    the source of truth. When we observe that authoritative rejection
    (``bracket_orders`` protective placement), we record the symbol here
    so the entry gate stops opening NEW positions in a name we cannot
    protect with a standing stop — the asset-flag gate could never catch
    it because the flag itself is wrong.

    Reuses ``recently_exited_symbols`` with ``trigger='htb_cooldown'`` so
    the pre-filter pipeline lumps it in with the other cooldowns. The
    INSERT OR REPLACE refreshes ``exited_at`` on every fresh rejection, so
    a name stays excluded as long as it keeps rejecting; if it genuinely
    becomes easy-to-borrow again, the cooldown lapses after the window.
    """
    if not db_path or not symbol:
        return
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO recently_exited_symbols "
                "(symbol, exited_at, trigger, exit_price) "
                "VALUES (?, datetime('now'), 'htb_cooldown', NULL)",
                (symbol.upper(),),
            )
            conn.commit()
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _htb_exc:
        # HTB-cooldown marker write; best-effort and re-recorded on the
        # next rejection. Surface for follow-up.
        logger.debug(
            "htb_cooldown marker write failed for %s: %s: %s",
            symbol, type(_htb_exc).__name__, _htb_exc,
        )


def get_htb_cooldown_symbols(db_path: str, days: int = 30) -> set:
    """Return symbols learned hard-to-borrow from broker order rejections.

    Default 30-day window. The marker refreshes on every fresh HTB
    rejection (see :func:`record_htb_cooldown`), so a name that is
    actively hard-to-borrow stays excluded indefinitely while it keeps
    rejecting; one that recovers ages out after the window."""
    if not db_path:
        return set()
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT symbol FROM recently_exited_symbols "
                "WHERE trigger = 'htb_cooldown' "
                "AND exited_at >= datetime('now', ?)",
                (f"-{int(days)} days",),
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def get_recently_exited(db_path: str, cooldown_minutes: int = 60) -> set:
    """Return the set of symbols currently in the post-exit cooldown window."""
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT symbol FROM recently_exited_symbols "
                "WHERE exited_at >= datetime('now', ?)",
                (f"-{int(cooldown_minutes)} minutes",),
            ).fetchall()
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
            # Phase 5e (2026-05-12) — generic data-quality marker.
            # NULL on normal trades. Set to a tag string for rows
            # excluded from analytics aggregates due to a known
            # data-quality issue (e.g., 'phantom_stop_2026_05_11'
            # for the 31 KO / mis-classified-stock-stop rows
            # written during yesterday's phantom-stops incident).
            # Excluded by get_slippage_stats and surfaced as a
            # separate count so operators see when corrupt data
            # is present without it polluting the headline metrics.
            ("data_quality", "TEXT"),
            # 2026-07-01 — per-row share of the option spread's max-loss
            # (dollars). Stamped at multileg entry as
            # (max_loss_per_contract * qty) / n_legs on EACH leg, so
            # SUM(spread_max_loss) over a profile's OPEN option rows =
            # the book's aggregate options capital-at-risk. Feeds the
            # fund-grade max_options_risk_pct budget gate. NULL/0 on stock
            # rows and on legs opened before this column existed.
            ("spread_max_loss", "REAL"),
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
            # 2026-05-13 — generic data-quality marker, mirroring
            # the trades.data_quality column. Defense-in-depth
            # against the pollution chain:
            #   corrupt trades row → resolver computes wrong
            #   actual_return_pct → polluted ai_predictions row
            #   → analytics on ai_predictions pool the pollution
            # Today's resolver gates (multileg leg-lookup excludes
            # data_quality-tagged trades) prevent the chain at
            # source, but having the column on ai_predictions
            # lets analytics queries use the same data_quality_clause
            # filter pattern uniformly across both tables, killing
            # the bug class structurally.
            ("data_quality", "TEXT"),
            # 2026-05-19 (Phase B1 data-collection upgrade) —
            # columns that turn ai_predictions into a fine-tune-
            # quality dataset. Each row now carries the full
            # decision context, not just the parsed action.
            #
            # cycle_id links the prediction back to its parent
            # cycle in the new ai_cycles table — restores cross-
            # candidate context (what OTHER candidates were in the
            # same prompt, their relative ranks, what the AI did
            # with each) that today's overwriting cycle_data_*.json
            # was throwing away.
            ("cycle_id", "TEXT"),
            # The actual prompt text the AI saw. Previously only
            # the shadow_eval framework persisted this; main path
            # threw it away after submission. Fine-tune needs the
            # exact input the model was trained against.
            ("prompt_text", "TEXT"),
            # Full AI response dict (JSON). The parsed signal +
            # reasoning summary throw away structured fields some
            # training approaches use. Stored verbatim.
            ("raw_response_json", "TEXT"),
            # Pre-gate meta-model score at decision time (GBM batch
            # model in meta_model.py). The candidate may or may not
            # have been suppressed based on this; either way it's
            # useful supervision input for fine-tune.
            ("meta_model_score", "REAL"),
            # Online meta-model score at decision time (SGD in
            # online_meta_model.py). Catches regime drift faster
            # than the nightly GBM.
            ("online_meta_score", "REAL"),
            # 2026-05-20 (#186 Phase A — docs/23-adjacent): cost-
            # adjusted return. `actual_return_pct` is the gross price
            # move; this column subtracts an estimate of round-trip
            # transaction costs (slippage) so downstream analytics
            # (self-tuner, calibration, win-rate, AI track_record)
            # work against numbers that actually predict trading P&L,
            # not just price prediction. Populated by the resolver
            # alongside actual_return_pct; NULL on legacy rows
            # resolved before the column existed.
            ("actual_return_pct_net", "REAL"),
            # 2026-05-20 (#185 — multi-horizon outcomes): snapshot of
            # which deterministic-panel rules fired at prediction time.
            # JSON list of {"name": str, "severity": "VETO"|"CAUTION"|
            # "CONFIRM", "direction": "long"|"short"|"neutral"}. Lets
            # the fine-tune dataset builder join rule-firings to
            # multi-horizon outcomes (which rule, fired in which
            # direction, was followed by what outcome at 1d/5d/20d)
            # without re-running the panel from saved features.
            # NULL on rows written before the column existed; NULL
            # also when the panel didn't fire any rules.
            ("rule_votes_json", "TEXT"),
        ],
        # `call_id` joins primary cost-ledger rows to the
        # ai_shadow_calls rows produced by the shadow dispatcher for
        # the same primary invocation. NULL on rows logged before the
        # shadow eval feature shipped.
        "ai_cost_ledger": [
            ("call_id", "TEXT"),
            # 2026-07-02 — cached-token telemetry. Without this ALTER on
            # EXISTING DBs the ledger INSERT silently falls back to the
            # legacy shape forever and cache hits are never measurable.
            ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ],
        # 2026-06-15 — full per-cycle decision list for the AI-Brain
        # history view. CREATE TABLE only adds it to fresh DBs; this
        # entry ALTER-adds it to every EXISTING profile DB so the
        # /api/cycle-history endpoint can SELECT it (without this the
        # column lands only on post-change DBs and the endpoint
        # 'no such column'-errors on all current profiles).
        "ai_cycles": [
            ("trades_selected_json", "TEXT"),
            # 2026-07-02 — once-per-cycle prompt/raw-response dedupe. Without
            # these ALTERs on EXISTING DBs the cycle-mint insert fails every
            # cycle and — with predictions no longer storing their own copy —
            # the prompt would be PERMANENTLY LOST for every new prediction
            # (perfect-data violation caught by adversarial review).
            ("prompt_text", "TEXT"),
            ("raw_response_json", "TEXT"),
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
        except sqlite3.OperationalError as _alt_exc:
            # Per-table column ALTER loop; column may already exist
            # on a later schema. Surface for follow-up.
            logger.debug(
                "schema migration ALTER on %s: %s: %s",
                table, type(_alt_exc).__name__, _alt_exc,
            )

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
            # and pipelines.outcomes.kind_from_signal.
            # HOLD is included: a HOLD prediction is "AI saw this
            # stock candidate, decided not to trade it" — that's a
            # stock-pipeline decision and should be tagged 'stock'
            # so it counts in stock-only calibration / tuning rather
            # than staying NULL (which left ~90% of resolved rows
            # untagged on prod through 2026-05-11).
            stock_signals = (
                "BUY", "STRONG_BUY", "WEAK_BUY",
                "SELL", "STRONG_SELL", "WEAK_SELL",
                "SHORT", "COVER", "HOLD",
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
    except sqlite3.OperationalError as _bf_exc:
        # pipeline_kind backfill UPDATE; idempotent — next call
        # retries. Surface for follow-up.
        logger.debug(
            "pipeline_kind backfill failed: %s: %s",
            type(_bf_exc).__name__, _bf_exc,
        )

    # Phase 5e tcols lookup — used by both phantom_stop tagging
    # blocks below. Done once to avoid duplicate PRAGMA calls.
    try:
        tcols = {row[1] for row in conn.execute(
            "PRAGMA table_info(trades)"
        ).fetchall()}
    except Exception:
        tcols = set()

    # Phase 5e wave 3 (2026-05-12) — tag reconcile_backfill rows
    # whose pnl was computed against a phantom entry price.
    #
    # Pattern: the dashboard's template renders
    #   pnl_pct = pnl / (price*qty - pnl) * 100
    # which goes to thousands of percent when price*qty - pnl
    # approaches zero. That happens specifically when the
    # reconciler computed pnl = (sell - buy) * qty with `buy` =
    # corrupt option premium (~$0.45) and `sell` = real stock
    # price (~$22). Then pnl ≈ qty * sell ≈ qty * price, so
    # cost_basis_implied = qty*price - pnl ≈ 0.
    #
    # Detector: cost_basis_implied = (price * qty) - pnl < $1.
    # That's the structural fingerprint of the bug. Real
    # reconcile_backfill rows have cost_basis ≈ price*qty
    # (because pnl is a small fraction of proceeds).
    #
    # Idempotent on data_quality IS NULL.
    try:
        if "data_quality" in tcols:
            conn.execute(
                "UPDATE trades "
                "SET data_quality = 'phantom_stop_reconcile_2026_05_12' "
                "WHERE data_quality IS NULL "
                "AND strategy LIKE 'reconcile_backfill%' "
                "AND pnl IS NOT NULL AND price > 0 AND qty > 0 "
                "AND ABS((price * qty) - pnl) < 1.0"
            )
    except sqlite3.OperationalError as _ph_exc:
        # phantom-stop reconcile data_quality tag; idempotent
        # UPDATE retries next call. Surface for follow-up.
        logger.debug(
            "phantom-stop reconcile tag UPDATE failed: %s: %s",
            type(_ph_exc).__name__, _ph_exc,
        )

    # Phase 5e (2026-05-12) — tag the phantom_stop_2026_05_11 incident
    # rows so analytics aggregates exclude them. Pattern: STOCK-tagged
    # trades (occ_symbol IS NULL) where the decision_price is the
    # option premium ($0.16, $1.10, $1.48 — not the actual stock
    # price) but the SELL was submitted to the underlying ticker
    # and filled at the real stock price ($78, $292, $66...).
    # Result: slippage_pct of thousands-of-percent.
    #
    # Initial criterion `decision_price < 1.0` was too tight — it
    # missed AAPL ($1.07-$1.13), TECK ($1.44-$1.48), U ($1.12)
    # rows. Real stock slippage NEVER exceeds 50% on a normal
    # fill (the price would have to halve or double in the
    # decision-to-fill window). Combined with the timestamp window
    # (<2026-05-12), `ABS(slippage_pct) > 50` is sufficient and
    # captures all 31 incident rows.
    #
    # Future high-slippage rows from after 2026-05-12 must be
    # investigated individually — not auto-tagged.
    #
    # Idempotent: gated on `data_quality IS NULL`.
    try:
        # tcols already populated above
        if "data_quality" in tcols and "slippage_pct" in tcols:
            conn.execute(
                "UPDATE trades "
                "SET data_quality = 'phantom_stop_2026_05_11' "
                "WHERE data_quality IS NULL "
                "AND occ_symbol IS NULL "
                "AND ABS(slippage_pct) > 50 "
                "AND timestamp < '2026-05-12T00:00:00'"
            )
    except sqlite3.OperationalError as _ph_exc:
        # phantom-stop 2026-05-11 data_quality tag; idempotent
        # UPDATE retries next call. Surface for follow-up.
        logger.debug(
            "phantom-stop 2026-05-11 tag UPDATE failed: %s: %s",
            type(_ph_exc).__name__, _ph_exc,
        )


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
              spread_max_loss=None,
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

    PERFECT-MATCHING INVARIANT (2026-05-17): every row written here
    represents a trade that the trading system (broker) executed or
    is in the process of executing. Therefore `order_id` should
    ALWAYS equal the trading system's order ID — that's what makes
    journal ↔ broker reconciliation trivial. Exceptions logged
    LOUDLY so we can find any code path that's writing without one:
      - AUTO_RECONCILE / AUTO_RECONCILE_PHANTOM_CLOSE: sentinel
        order_ids OK (explicit backfill/rebuild)
      - pending_fill rows: order_id must be set (submitted, not yet
        confirmed); if absent → bug, log
      - any other status: order_id required; missing → bug, log
    """
    if not order_id and signal_type not in (
        "AUTO_RECONCILE", "AUTO_RECONCILE_PHANTOM_CLOSE",
    ):
        import logging as _log
        _log.warning(
            "log_trade: BROKER ORDER ID MISSING for %s %s qty=%s "
            "signal=%s status=%s — every trade row should carry the "
            "trading system's order_id (broker-agnostic identifier "
            "of the actual order submitted). Without it, journal ↔ "
            "broker reconciliation can't match this row to its fill, "
            "leaving an unattributable position that will appear as "
            "drift. Investigate the caller.",
            side, symbol, qty, signal_type, status,
        )
    # Retry transient DB failures (locked / busy / disk). The order this row
    # represents may ALREADY be live at the broker — losing the row would
    # orphan it (a submit with no journal entry → next reconcile sees an
    # unowned broker position → drift halt). A momentary SQLite lock must not
    # be able to create an unjournaled submit, so retry before giving up.
    # (2026-06-23 durable-journaling hardening.) On final failure we re-raise:
    # the door's submitted_orders recovery ledger has already recorded the
    # order_id so the reconciler can reconstruct the row from the broker fill.
    import time as _time
    _cols = ["timestamp", "symbol", "side", "qty", "price", "order_id",
             "signal_type", "strategy", "reason", "ai_reasoning",
             "ai_confidence", "stop_loss", "take_profit", "status", "pnl",
             "decision_price", "fill_price", "slippage_pct", "occ_symbol",
             "option_strategy", "expiry", "strike", "predicted_slippage_bps",
             "adv_at_decision"]
    _vals = [datetime.utcnow().isoformat(), symbol, side, qty, price, order_id,
             signal_type, strategy, reason, ai_reasoning, ai_confidence,
             stop_loss, take_profit, status, pnl, decision_price, fill_price,
             slippage_pct, occ_symbol, option_strategy, expiry, strike,
             predicted_slippage_bps, adv_at_decision]
    _last_exc = None
    for _attempt in range(3):
        try:
            with closing(_get_conn(db_path)) as conn:
                # spread_max_loss is a migrated column — include it only when
                # the table actually has it, so log_trade never hard-fails on a
                # schema that predates it (e.g. hand-crafted test tables). Prod
                # DBs are migrated by init_db, so the column is present there.
                cols, vals = list(_cols), list(_vals)
                _have = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
                if "spread_max_loss" in _have:
                    cols.append("spread_max_loss")
                    vals.append(spread_max_loss)
                _ph = ", ".join(["?"] * len(vals))
                cursor = conn.execute(
                    f"INSERT INTO trades ({', '.join(cols)}) VALUES ({_ph})",
                    tuple(vals),
                )
                conn.commit()
                trade_id = cursor.lastrowid
            return trade_id
        except sqlite3.OperationalError as _exc:
            _last_exc = _exc
            _time.sleep(0.2 * (_attempt + 1))
    raise _last_exc


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
    # Phase 4b of pipeline refactor (2026-05-11): SYSTEM-side veto by
    # an option specialist (option_spread_risk + future option-only
    # specialists). Distinct from broker-side rejections — we said
    # no, never sent to broker. Operators see a different badge code
    # so they understand the trade was blocked structurally rather
    # than refused by the exchange.
    ("specialist veto",
     "specialist_veto"),
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
        with closing(_get_conn(db_path)) as conn:
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
        return rid
    except Exception as exc:
        _logging.warning(
            "record_broker_rejection(symbol=%s, code=%s) failed: %s",
            symbol, rejection_code, exc,
        )
        return None


def record_option_proposal_outcome(db_path, *, symbol, strategy, sector,
                                   vetoed, veto_reason=None, confidence=None,
                                   entry_net_premium=None,
                                   max_loss_per_contract=None,
                                   max_gain_per_contract=None,
                                   breakeven=None, lo_strike=None,
                                   hi_strike=None, expiry=None,
                                   legs_json=None):
    """Record ONE option proposal outcome — vetoed (1) or accepted (0) — keyed
    by the SPREAD strategy + sector, for the selection engine's per-(strategy x
    sector) veto-rate discount (P3) and the would-be-P&L shadow resolver (P4).

    Writes to THIS profile's DB only (`db_path` is the isolation boundary) and
    to a table PHYSICALLY SEPARATE from ai_predictions, so a would-be outcome
    can never contaminate real-trade reputation / meta-model / win-rate stats.
    Vetoed rows land status='pending' (the shadow resolver fills wouldbe_*);
    accepted rows land status='accepted' (no resolution needed — they became a
    real trade tracked in ai_predictions/trades). Fail-open: on any error, log
    and return None — a feedback-signal write must never break execution.
    """
    if not db_path or not strategy:
        return None
    import logging as _logging
    try:
        with closing(_get_conn(db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO option_proposal_outcomes "
                "(symbol, strategy, sector, vetoed, veto_reason, confidence, "
                " entry_net_premium, max_loss_per_contract, "
                " max_gain_per_contract, breakeven, lo_strike, hi_strike, "
                " expiry, legs_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((symbol or "").upper(), str(strategy), sector,
                 1 if vetoed else 0, veto_reason, confidence,
                 entry_net_premium, max_loss_per_contract,
                 max_gain_per_contract, breakeven, lo_strike, hi_strike,
                 expiry, legs_json,
                 "pending" if vetoed else "accepted"),
            )
            conn.commit()
            return cursor.lastrowid
    except Exception as exc:
        _logging.warning(
            "record_option_proposal_outcome(symbol=%s, strategy=%s) failed: "
            "%s: %s — veto/accept feedback not recorded this proposal",
            symbol, strategy, type(exc).__name__, exc,
        )
        return None


def option_veto_counts(db_path):
    """Per-(strategy, sector) option-proposal counts for THIS profile's own
    book: returns a list of (strategy, sector, vetoed_count, total_count).
    Feeds veto_feedback.load_veto_discounts. Own-book only; returns [] on any
    error or when the table is absent (fail-open — a missing feedback signal
    must never block the ledger)."""
    if not db_path:
        return []
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT strategy, "
                "       COALESCE(sector, '') AS sector, "
                "       SUM(CASE WHEN vetoed = 1 THEN 1 ELSE 0 END) AS vetoed, "
                "       COUNT(*) AS total "
                "FROM option_proposal_outcomes "
                "GROUP BY strategy, COALESCE(sector, '')"
            ).fetchall()
        return [(r["strategy"], r["sector"], int(r["vetoed"] or 0),
                 int(r["total"] or 0)) for r in rows]
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "option_veto_counts unavailable (fail-open): %s", exc)
        return []


def pending_veto_outcomes(db_path, strictly_before):
    """Vetoed option-proposal rows awaiting would-be-P&L resolution: priced
    verticals (max_loss/gain + both strikes present) whose expiry is STRICTLY
    BEFORE `strictly_before` (YYYY-MM-DD). Strict `<` (not `<=`) so we never
    resolve a row on its own expiry day, when the daily bar is still the
    in-progress session's PARTIAL print rather than the settlement close —
    locking in a wrong outcome permanently. Own-book; [] on any error
    (fail-open). Returns a list of dicts."""
    if not db_path:
        return []
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, symbol, strategy, lo_strike, hi_strike, "
                "       max_loss_per_contract, max_gain_per_contract, expiry "
                "FROM option_proposal_outcomes "
                "WHERE vetoed = 1 AND status = 'pending' "
                "  AND max_loss_per_contract IS NOT NULL "
                "  AND max_gain_per_contract IS NOT NULL "
                "  AND lo_strike IS NOT NULL AND hi_strike IS NOT NULL "
                "  AND expiry IS NOT NULL AND expiry < ?",
                (str(strictly_before),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "pending_veto_outcomes unavailable (fail-open): %s", exc)
        return []


def mark_veto_outcome_resolved(db_path, row_id, *, outcome, pnl):
    """Mark ONE vetoed option-proposal row resolved with its would-be outcome
    (win/loss/neutral) + would-be P&L per contract. Idempotent (only updates a
    still-'pending' row). Fail-open: log and return False on error."""
    if not db_path or row_id is None:
        return False
    try:
        with closing(_get_conn(db_path)) as conn:
            cursor = conn.execute(
                "UPDATE option_proposal_outcomes "
                "SET status = 'resolved', wouldbe_outcome = ?, "
                "    wouldbe_pnl = ?, resolved_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (outcome, pnl, row_id),
            )
            conn.commit()
            return cursor.rowcount > 0   # False if the row wasn't still pending
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "mark_veto_outcome_resolved(id=%s) failed: %s", row_id, exc)
        return False


def mark_veto_outcome_unresolvable(db_path, row_id):
    """Mark a stale pending vetoed row 'unresolvable' (e.g. delisted underlying
    with no expiry-day bar, or a non-trading-day expiry that will never print a
    matching close). Excluded from both the pending set AND the resolved
    veto-quality counts, so it neither re-queries forever nor pollutes the
    signal. Fail-open (log, return False)."""
    if not db_path or row_id is None:
        return False
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute(
                "UPDATE option_proposal_outcomes "
                "SET status = 'unresolvable', resolved_at = datetime('now') "
                "WHERE id = ? AND status = 'pending'",
                (row_id,),
            )
            conn.commit()
        return True
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "mark_veto_outcome_unresolvable(id=%s) failed: %s", row_id, exc)
        return False


def option_veto_quality_counts(db_path):
    """Per-(strategy, sector) RESOLVED-veto counts for THIS profile: a list of
    (strategy, sector, resolved_count, wouldbe_loss_count). A high loss fraction
    means the profile's vetoes on that pair actually AVOIDED losses (smart
    vetoes); a low fraction means they blocked would-be winners. Feeds the
    veto-QUALITY refinement of the discount. Own-book; [] on any error."""
    if not db_path:
        return []
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT strategy, COALESCE(sector, '') AS sector, "
                "       COUNT(*) AS resolved, "
                "       SUM(CASE WHEN wouldbe_outcome = 'loss' THEN 1 ELSE 0 END)"
                "         AS losses "
                "FROM option_proposal_outcomes "
                "WHERE vetoed = 1 AND status = 'resolved' "
                "GROUP BY strategy, COALESCE(sector, '')"
            ).fetchall()
        return [(r["strategy"], r["sector"], int(r["resolved"] or 0),
                 int(r["losses"] or 0)) for r in rows]
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "option_veto_quality_counts unavailable (fail-open): %s", exc)
        return []


def resolved_veto_counterfactuals(db_path):
    """RESOLVED vetoed option proposals as would-be training examples: the AI
    proposed the spread, the specialists vetoed it, and it resolved to a TRUE
    would-be P&L. Returns a list of dicts for the fine-tune corpus builder.
    Own-book; [] on any error (fail-open). These are COUNTERFACTUALS (modeled
    intrinsic-at-expiry P&L, NOT real fills) — the caller MUST tag them so the
    training pipeline never mistakes them for real executed trades."""
    if not db_path:
        return []
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT timestamp, symbol, strategy, sector, confidence, "
                "       veto_reason, entry_net_premium, max_loss_per_contract, "
                "       max_gain_per_contract, breakeven, lo_strike, hi_strike, "
                "       expiry, wouldbe_outcome, wouldbe_pnl, resolved_at "
                "FROM option_proposal_outcomes "
                "WHERE vetoed = 1 AND status = 'resolved' "
                "  AND wouldbe_outcome IS NOT NULL "
                "ORDER BY timestamp ASC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "resolved_veto_counterfactuals unavailable (fail-open): %s", exc)
        return []


def open_options_capital_at_risk(db_path=None) -> float:
    """Aggregate options CAPITAL-AT-RISK for this profile's OWN book, in $.

    Sums `spread_max_loss` over the profile's OPEN option rows (rows with an
    occ_symbol and a live status). Each multileg entry stamps its total
    max-loss spread evenly across its legs, so this sum is the book's total
    defined-risk max-loss. Feeds the fund-grade `max_options_risk_pct`
    budget gate. Own-book only (single db_path) — isolation-safe. Returns
    0.0 on any error or when the column is absent (fail-open: a missing
    figure must never block trading; the gate treats 0 as "no prior risk").
    """
    try:
        with closing(_get_conn(db_path)) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)").fetchall()}
            if "spread_max_loss" not in cols:
                return 0.0
            row = conn.execute(
                "SELECT COALESCE(SUM(spread_max_loss), 0) FROM trades "
                "WHERE occ_symbol IS NOT NULL "
                "AND spread_max_loss IS NOT NULL "
                "AND status IN ('open', 'pending', 'pending_fill', "
                "               'pending_protective', 'partially_filled')",
            ).fetchone()
            return float(row[0] or 0.0) if row else 0.0
    except Exception as exc:
        _logging.warning(
            "open_options_capital_at_risk failed (fail-open, returning 0): %s",
            exc,
        )
        return 0.0


def record_trade_drop(db_path, symbol, side, drop_code, drop_reason,
                       cycle_id=None, ai_confidence=None,
                       ai_reasoning=None):
    """Persist a pre-broker trade drop. Called from every early-return
    path in trade_pipeline.execute_trade so the AI Brain panel can
    show the actual reason a proposed trade didn't make it to the
    broker — not just the generic "BLOCKED" badge.

    drop_code: short identifier the UI humanizes (e.g.
        'already_held_broker_side', 'entry_blacklist',
        'qty_too_small', 'portfolio_constraint',
        'schedule_window', 'qty_runaway', 'price_invalid').
    drop_reason: human-readable, operator-visible string.

    Best-effort: failures are logged but never raise — the live trade
    pipeline must never be blocked by audit-table writes."""
    import logging as _logging
    if not db_path or not symbol or not drop_code:
        return
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute(
                "INSERT INTO trade_drops "
                "(symbol, side, drop_code, drop_reason, cycle_id, "
                " ai_confidence, ai_reasoning) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, side, drop_code, drop_reason, cycle_id,
                 int(ai_confidence) if ai_confidence is not None else None,
                 ai_reasoning),
            )
            conn.commit()
    except Exception as exc:
        _logging.warning(
            "record_trade_drop(%s/%s) failed: %s: %s",
            symbol, drop_code, type(exc).__name__, exc,
        )


def get_recent_trade_drops(db_path, hours=24, limit=200):
    """Reader for the AI Brain panel — return pre-broker drops in the
    last `hours`. Same shape as get_recent_broker_rejections."""
    import logging as _logging
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, timestamp, symbol, side, drop_code, "
                "       drop_reason, cycle_id, ai_confidence, ai_reasoning "
                "FROM trade_drops "
                "WHERE timestamp >= datetime('now', ?) "
                "ORDER BY timestamp DESC LIMIT ?",
                (f"-{int(hours)} hours", int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        _logging.warning(
            "get_recent_trade_drops(%s) failed: %s",
            db_path, exc,
        )
        return []


def get_recent_broker_rejections(db_path, hours=24, limit=200):
    """Return broker-rejection rows from the last `hours` for one
    profile DB. Used by the AI Brain panel to surface "REJECTED"
    badges on recently-proposed trades."""
    import logging as _logging
    try:
        with closing(_get_conn(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, timestamp, symbol, action, signal_type, "
                "       ai_confidence, ai_reasoning, rejection_code, "
                "       broker_message, prediction_id "
                "FROM broker_rejections "
                "WHERE timestamp >= datetime('now', ?) "
                "ORDER BY timestamp DESC LIMIT ?",
                (f"-{int(hours)} hours", int(limit)),
            ).fetchall()
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
        with closing(_get_conn(db_path)) as conn:
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


def get_symbol_epoch(db_path, symbol) -> int:
    """Return the cycle epoch at which THIS profile's journal for `symbol`
    was last reconciled to broker truth.

    Returns 0 — meaning "never / unknown" — when there is no db_path, no
    symbol, the ledger row is missing, or the read errors. 0 is older than
    any live epoch (which starts at 1), so the oversell door treats it as
    STALE and forces a reconcile before any sell. Unknown is fail-safe."""
    if not db_path or not symbol:
        return 0
    import sqlite3
    from contextlib import closing
    try:
        with closing(_get_conn(db_path)) as conn:
            row = conn.execute(
                "SELECT last_reconciled_epoch FROM reconcile_state "
                "WHERE symbol = ?", ((symbol or "").upper(),)).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        # Missing table / locked DB → treat as stale (fail-safe): the door
        # will force a just-in-time reconcile rather than trust a maybe-
        # stale journal.
        return 0


def stamp_symbols_fresh(db_path, symbols, epoch) -> None:
    """Record that THIS profile's journal for each of `symbols` has been
    reconciled to broker truth at cycle `epoch`. Idempotent upsert.

    Called by the reconcile-and-stamp wrapper after a successful
    reconcile_with_ctx (full reconcile → stamp every symbol it confirmed).
    Creates the ledger table on demand so it works on DBs that predate the
    migration. Errors are NOT swallowed silently: a stamp failure leaves the
    symbol stale, which is safe (the door re-reconciles) but the caller
    should know, so we let real DB errors propagate."""
    if not db_path or not symbols:
        return
    from contextlib import closing
    syms = sorted({(s or "").upper() for s in symbols if s})
    if not syms:
        return
    with closing(_get_conn(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reconcile_state ("
            "symbol TEXT PRIMARY KEY, "
            "last_reconciled_epoch INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.executemany(
            "INSERT OR REPLACE INTO reconcile_state "
            "(symbol, last_reconciled_epoch, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            [(s, int(epoch)) for s in syms])
        conn.commit()


def record_submitted_order(db_path, order_id, symbol, side, qty,
                           occ_symbol=None, intent=None) -> None:
    """Durably record an order the moment the broker accepts it — BEFORE the
    full log_trade row is written. If log_trade then fails (DB lock/disk), the
    order_id survives here so the reconciler can reconstruct the journal row
    from the broker fill instead of orphan-halting (durable-journaling
    recovery, 2026-06-23).

    `side` is the BROKER side ('buy'/'sell'); `intent` is the caller's journal
    intent ('open_short' for a deliberate short entry, else None) so the
    reconstruct can map broker side -> journal side without ambiguity (a
    broker 'sell' is a long close OR a short open; a broker 'buy' is a long
    open OR a short cover).

    BEST-EFFORT: never raises. The order is already live at the broker;
    failing here must not break the caller (which would falsely look like a
    submit failure). Creates the table on demand for pre-migration DBs."""
    if not db_path or not order_id:
        return
    from contextlib import closing
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS submitted_orders ("
                "order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, qty REAL, "
                "occ_symbol TEXT, intent TEXT, submitted_at TEXT NOT NULL "
                "DEFAULT (datetime('now')))")
            conn.execute(
                "INSERT OR IGNORE INTO submitted_orders "
                "(order_id, symbol, side, qty, occ_symbol, intent) "
                "VALUES (?,?,?,?,?,?)",
                (str(order_id), symbol, str(side or ""),
                 float(qty or 0), occ_symbol,
                 str(intent) if intent else None))
            conn.commit()
    except sqlite3.Error as exc:
        import logging as _log
        _log.getLogger(__name__).warning(
            "record_submitted_order(%s) failed (best-effort): %s",
            order_id, exc)


def unjournaled_submitted_orders(db_path) -> list:
    """Return recovery-ledger rows whose order_id has NO matching trades row —
    an order the broker accepted but whose journal write was lost. The
    reconciler reconstructs these from broker fills before halting."""
    if not db_path:
        return []
    from contextlib import closing
    try:
        with closing(_get_conn(db_path)) as conn:
            tabs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "submitted_orders" not in tabs:
                return []
            has_intent = any(
                r[1] == "intent" for r in
                conn.execute("PRAGMA table_info(submitted_orders)"))
            intent_col = "s.intent" if has_intent else "NULL"
            rows = conn.execute(
                f"SELECT s.order_id, s.symbol, s.side, s.qty, s.occ_symbol, "
                f"{intent_col} FROM submitted_orders s "
                "WHERE NOT EXISTS (SELECT 1 FROM trades t "
                "                  WHERE t.order_id = s.order_id)"
            ).fetchall()
            return [{"order_id": r[0], "symbol": r[1], "side": r[2],
                     "qty": r[3], "occ_symbol": r[4], "intent": r[5]}
                    for r in rows]
    except sqlite3.Error:
        return []


def drop_submitted_order(db_path, order_id) -> None:
    """Remove a recovery-ledger row (the order was reconstructed, or it never
    filled). Best-effort; keeps the ledger bounded."""
    if not db_path or not order_id:
        return
    from contextlib import closing
    try:
        with closing(_get_conn(db_path)) as conn:
            conn.execute("DELETE FROM submitted_orders WHERE order_id = ?",
                         (str(order_id),))
            conn.commit()
    except sqlite3.Error as exc:
        import logging as _log
        _log.getLogger(__name__).debug(
            "drop_submitted_order(%s) failed (best-effort): %s",
            order_id, exc)


def prune_journaled_submitted_orders(db_path) -> None:
    """Keep the recovery ledger bounded. Delete rows that (a) now HAVE a trades
    row — journaled normally, no recovery needed — OR (b) are older than 2 days.
    Recovery happens on the very next reconcile (within a cycle), so a row that
    is still unrecovered after 2 days never will be (e.g. a lost SELL/COVER,
    which the reconciler's backfill handles via the existing ENTRY row, not via
    this ledger); pruning it stops an unbounded re-fetch-every-cycle leak.
    Best-effort."""
    if not db_path:
        return
    from contextlib import closing
    try:
        with closing(_get_conn(db_path)) as conn:
            tabs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "submitted_orders" not in tabs:
                return
            conn.execute(
                "DELETE FROM submitted_orders WHERE EXISTS "
                "(SELECT 1 FROM trades t "
                " WHERE t.order_id = submitted_orders.order_id) "
                "OR submitted_at < datetime('now', '-2 days')")
            conn.commit()
    except sqlite3.Error as exc:
        import logging as _log
        _log.getLogger(__name__).debug(
            "prune_journaled_submitted_orders failed (best-effort): %s", exc)


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
            # 2026-06-11 — lots are valued at the broker's actual
            # fill price when reported (decision price is the
            # fallback for rows without fills). Same hyper-accuracy
            # rule as the cash math: avg_entry_price and FIFO
            # consumption must reflect what actually executed, not
            # what was intended. Column-probed so minimal-schema
            # fixtures (no fill_price) keep working without losing
            # the occ_symbol option/stock separation.
            try:
                _vp_cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(trades)").fetchall()}
            except sqlite3.Error:
                _vp_cols = set()
            _vp_price = (
                "COALESCE(NULLIF(fill_price, 0), price)"
                if "fill_price" in _vp_cols else "price"
            )
            # Exclude rows tagged with a known data-corruption marker
            # (e.g. 'phantom_stop_2026_05_11'). The reconciler and the
            # decomposition check already filter these; position truth —
            # which feeds the oversell door's own_virtual_qty — must filter
            # them too, or a poisoned 'open' row inflates/poisons the qty the
            # door trusts. (2026-06-23 divergence-class hardening.)
            _dq_clause = (
                " AND data_quality IS NULL"
                if "data_quality" in _vp_cols else ""
            )
            try:
                rows = conn.execute(
                    f"SELECT symbol, side, qty, {_vp_price} AS price, "
                    "timestamp, occ_symbol, status "
                    "FROM trades "
                    # Status-aware filter. Pre-2026-05-16 only
                    # 'canceled' was excluded, which left two leaks:
                    # (a) expired/rejected multileg legs (price=0,
                    #     never opened a position) leaked in and
                    #     tripped the "qty>0 but price<=0" warning
                    #     ~170x/day.
                    # (b) status='closed' BUY rows whose CLOSE was
                    #     recorded as a status flip (lifecycle sweep,
                    #     reconciliation) without a matching SELL
                    #     row stayed in FIFO accounting as phantom
                    #     lots — causing the price_fetcher to poll
                    #     5 already-closed OCCs ~1000x/day.
                    # ENTRY rows (buy/short) with status='closed'
                    # are excluded because their lot is gone; EXIT
                    # rows (sell/cover) with status='closed' are KEPT
                    # because that's the real close (partial-close
                    # accounting depends on the closed SELL still
                    # consuming the open BUY lot).
                    "WHERE ("
                    # 2026-06-11 — 'pending_protective' added to
                    # the ENTRY-side exclusion too. Protectives
                    # for SHORT positions are BUY orders; their
                    # pending placeholder rows counted as real
                    # long entry lots (the exit-side branch
                    # below always excluded them; entry-side
                    # was written when all protectives were
                    # sell-side). Caught on p93: NU short's
                    # stop+TP placeholders became 2130 phantom
                    # long shares and inflated dashboard P&L
                    # by ~$25K.
                    "    (side IN ('buy', 'short') AND "
                    "     COALESCE(status, 'open') NOT IN "
                    "     ('canceled', 'expired', 'rejected', "
                    "      'done_for_day', 'pending_protective', "
                    "      'auto_reconciled_phantom_close')"
                    # 2026-06-18 — INCLUDE closed STOCK buys (occ NULL) in
                    # the FIFO timeline so a sell that closed them is
                    # matched in order, and a real oversell (a sell beyond
                    # every buy ever placed) surfaces as a short instead of
                    # vanishing — the dropping is what hid the UWMC shorts
                    # and left ~$187K of phantom equity. Unconsumed
                    # closed-buy lots are removed right after this FIFO
                    # pass, so a status-flip close with no sell can't show
                    # as a phantom long. Closed shorts and closed OPTION
                    # buys stay excluded (resolved positions).
                    "     AND (COALESCE(status, 'open') != 'closed' "
                    "          OR (side = 'buy' AND occ_symbol IS NULL)))"
                    "    OR "
                    "    (side IN ('sell', 'cover') AND "
                    "     COALESCE(status, 'open') NOT IN "
                    "     ('canceled', 'expired', 'rejected', "
                    "      'done_for_day', 'pending_protective'))"
                    ")"
                    f"{_dq_clause} "
                    "ORDER BY timestamp ASC, id ASC"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    "SELECT symbol, side, qty, price, timestamp "
                    "FROM trades "
                    # Status-aware filter. Pre-2026-05-16 only
                    # 'canceled' was excluded, which left two leaks:
                    # (a) expired/rejected multileg legs (price=0,
                    #     never opened a position) leaked in and
                    #     tripped the "qty>0 but price<=0" warning
                    #     ~170x/day.
                    # (b) status='closed' BUY rows whose CLOSE was
                    #     recorded as a status flip (lifecycle sweep,
                    #     reconciliation) without a matching SELL
                    #     row stayed in FIFO accounting as phantom
                    #     lots — causing the price_fetcher to poll
                    #     5 already-closed OCCs ~1000x/day.
                    # ENTRY rows (buy/short) with status='closed'
                    # are excluded because their lot is gone; EXIT
                    # rows (sell/cover) with status='closed' are KEPT
                    # because that's the real close (partial-close
                    # accounting depends on the closed SELL still
                    # consuming the open BUY lot).
                    "WHERE ("
                    # 2026-06-11 — 'pending_protective' added to
                    # the ENTRY-side exclusion too. Protectives
                    # for SHORT positions are BUY orders; their
                    # pending placeholder rows counted as real
                    # long entry lots (the exit-side branch
                    # below always excluded them; entry-side
                    # was written when all protectives were
                    # sell-side). Caught on p93: NU short's
                    # stop+TP placeholders became 2130 phantom
                    # long shares and inflated dashboard P&L
                    # by ~$25K.
                    "    (side IN ('buy', 'short') AND "
                    "     COALESCE(status, 'open') NOT IN "
                    "     ('canceled', 'expired', 'rejected', "
                    "      'done_for_day', 'closed', 'pending_protective', "
                    "      'auto_reconciled_phantom_close'))"
                    "    OR "
                    "    (side IN ('sell', 'cover') AND "
                    "     COALESCE(status, 'open') NOT IN "
                    "     ('canceled', 'expired', 'rejected', "
                    "      'done_for_day', 'pending_protective'))"
                    ")"
                    f"{_dq_clause} "
                    "ORDER BY timestamp ASC, id ASC"
                ).fetchall()
            # 2026-05-12 — per-trade TP/SL price lookup. UNH bug: the AI
            # set a $379 target on a $356 entry (6.5%), but the polling
            # take-profit check used the profile-level take_profit_pct
            # (15% on Large Cap profiles), so the position never fired
            # the TP at $379 and rode all the way to $396+ with no
            # capture. Pull the most-recent open BUY's stop_loss /
            # take_profit PRICES per symbol so they can be propagated
            # into the position output for `check_stop_loss_take_profit`.
            try:
                tp_sl_rows = conn.execute(
                    "SELECT symbol, occ_symbol, stop_loss, take_profit "
                    "FROM trades "
                    "WHERE side IN ('buy', 'short') "
                    "  AND status = 'open' "
                    "  AND (stop_loss IS NOT NULL OR take_profit IS NOT NULL) "
                    "ORDER BY timestamp DESC, id DESC"
                ).fetchall()
                per_trade_targets: Dict[str, Dict[str, float]] = {}
                for sym, occ, sl, tp in tp_sl_rows:
                    key = occ if occ else sym
                    # First (most recent) wins per key
                    if key not in per_trade_targets:
                        per_trade_targets[key] = {
                            "stop_loss_price": sl, "take_profit_price": tp,
                        }
            except sqlite3.OperationalError:
                per_trade_targets = {}
        except Exception:
            return []
    finally:
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
    # A freshly-opened multileg leg writes price=NULL BY DESIGN
    # (options_multileg._log_strategy_legs refuses a non-positive price
    # and leaves the column NULL) until _task_update_fills backfills the
    # per-leg fill on a later cycle — paper fills lag and the combo's
    # per-leg prices aren't known at submit. That transient price<=0 is
    # EXPECTED and self-heals, so warning on it cries wolf on every
    # just-entered spread leg. Only surface rows that have STAYED
    # price<=0 PAST the backfill window — genuinely stuck data (e.g. a
    # real combo-net write that never backfilled).
    _stuck_cutoff = datetime.utcnow() - timedelta(minutes=20)
    for row in rows:
        symbol = row[0]
        side = row[1]
        qty = float(row[2] or 0)
        price = float(row[3] or 0)
        row_ts = row[4] if len(row) > 4 else None
        occ_symbol = row[5] if len(row) > 5 else None
        row_status = (row[6] if len(row) > 6 else None) or "open"
        if qty <= 0 or price <= 0:
            # A NEGATIVE price is unambiguously bad data (the combo-net-
            # as-per-leg-price artifact) — never a pending leg, which
            # writes NULL (->0). Surface it immediately, any age. A price
            # of exactly 0/NULL is the shape of a just-entered leg
            # awaiting its fill; only surface THAT if it stayed so past
            # the backfill window (genuinely stuck, not self-healing).
            if qty > 0 and price < 0:
                skipped_bad_price += 1
            elif qty > 0 and price <= 0:
                _stuck = True
                if row_ts:
                    try:
                        _t = datetime.fromisoformat(
                            str(row_ts).replace("Z", "").split("+")[0].strip())
                        _stuck = _t < _stuck_cutoff
                    except Exception:
                        _stuck = True  # unparseable ts → surface it
                if _stuck:
                    skipped_bad_price += 1
            continue
        # Position key: OCC for options, underlying symbol for stock.
        key = occ_symbol if occ_symbol else symbol
        if key not in pos_meta:
            pos_meta[key] = {"symbol": symbol, "occ_symbol": occ_symbol}

        if side == "buy":
            # 3rd element tags a CLOSED stock buy (now included in the
            # FIFO timeline, 2026-06-18). It still consumes sells in order
            # (so a round-trip nets flat and a real oversell surfaces as a
            # short), but any UNCONSUMED remainder is dropped before the
            # positions are built — a closed buy is gone, never a held
            # long. Open buys carry False and stay as normal lots.
            long_lots.setdefault(key, []).append(
                [qty, price, row_status == "closed"])
        elif side == "short":
            short_lots.setdefault(key, []).append([qty, price])
        elif side == "sell":
            # Closes a long. FIFO-consume from OPEN long lots first.
            remaining = qty
            ll = long_lots.setdefault(key, [])
            while remaining > 0 and ll:
                consumed = min(ll[0][0], remaining)
                ll[0][0] -= consumed
                remaining -= consumed
                if ll[0][0] <= 0.001:
                    ll.pop(0)
            # Whatever remains after consuming the FIFO long lots — which
            # now include CLOSED stock buys in timestamp order, so a
            # round-trip's sell matches its (closed) buy and nets flat — is
            # a real short lot:
            #   • STOCK OVERSELL — sold more shares than were EVER bought
            #     → a genuine broker short. MUST be booked: order-id truth
            #     says a filled sell always moves the net. Dropping it is
            #     the phantom-equity bug (the cash credit survives, the
            #     short liability vanishes). p128 SOUN (3772 sell over a
            #     3672 long) and the 2026-06-18 UWMC oversells land here.
            #   • OPTION SELL-TO-OPEN — a multileg short leg (side='sell').
            #     An OPEN leg creates the short lot; a 'closed' /
            #     phantom-close leg is a RESOLVED short and must not spawn
            #     a phantom (the expired-multileg-short orphan, 2026-06-17).
            if (remaining > 0
                    and not (occ_symbol and row_status in (
                        "closed", "auto_reconciled_phantom_close"))):
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

    # 2026-06-18 — drop any UNCONSUMED closed-origin long lots. A CLOSED
    # stock buy was added to the FIFO timeline so a same-symbol sell
    # matches it in order (round-trip nets flat; a real oversell beyond
    # every buy surfaces as a short above). But a closed buy is gone —
    # whatever a sell didn't consume must NOT linger as a held long, or a
    # status-flip close with no sell would read as a phantom long. Open
    # buys (3rd element False) are untouched.
    #
    # KNOWN LIMITATION (anomalous data only): if a symbol has an ORPHAN
    # closed buy (status='closed' with no matching sell) that is OLDER
    # than a genuinely-held open buy, a later partial sell consumes the
    # orphan (FIFO head) instead of the open lot, over-stating the long.
    # The live close machinery never produces this state — _task_update_
    # fills FIFO-closes a buy only alongside its triggering sell, and the
    # reconciler only flips a buy 'closed' when broker qty<=0 (i.e. NOT
    # while a newer lot is held). It only arises from manual/historical
    # data repair, and the per-cycle integrity gate catches the symptom
    # (broker-drift: virtual qty != broker qty) and HALTS. A fresh-start
    # reset clears any such state. A perfect per-row resolution isn't
    # possible without recorded sell↔buy pairing, so the gate is the
    # backstop by design.
    for _k in long_lots:
        lots = long_lots[_k]
        if any(len(lot) > 2 and lot[2] for lot in lots):
            long_lots[_k] = [lot for lot in lots
                             if not (len(lot) > 2 and lot[2])]

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
            except (ValueError, AttributeError, OSError, RuntimeError,
                    ConnectionError, TimeoutError) as _pf_exc:
                # Side-aware price-fetcher fallback; current_price
                # falls through to entry-price assumption below.
                # Pluggable fetcher contract — broker errors surface
                # as arbitrary exceptions; broaden to keep callers
                # whole.
                logger.debug(
                    "price_fetcher fallback for %s: %s: %s",
                    key, type(_pf_exc).__name__, _pf_exc,
                )
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
        # Per-trade TP/SL prices from the most-recent open entry row.
        # `check_stop_loss_take_profit` (portfolio_manager.py) reads
        # these as `take_profit_price` / `stop_loss_price` and
        # compares directly to current_price — bypassing the
        # profile-level percent threshold, which is often miles wider
        # than the AI's per-trade target.
        targets = per_trade_targets.get(key, {})
        row = {
            "symbol": symbol,
            "occ_symbol": occ_symbol,
            "qty": round(signed_qty, 4),
            "avg_entry_price": round(avg_entry, 4),
            "current_price": round(current_price, 4),
            "market_value": round(market_value, 2),
            "unrealized_pl": round(unrealized_pl, 2),
            "unrealized_plpc": round(unrealized_plpc, 6),
            "take_profit_price": targets.get("take_profit_price"),
            "stop_loss_price": targets.get("stop_loss_price"),
        }
        positions.append(Position.from_virtual_row(row))

    if skipped_bad_price > 0:
        import logging as _logging
        _logging.warning(
            "get_virtual_positions(%s): skipped %d row(s) with qty>0 / "
            "price<=0 that are NOT just-entered legs awaiting a fill — "
            "either a negative per-leg price (combo-net artifact) or a "
            "row stuck price<=0 past the >20min backfill window. "
            "Invisible to the AI's portfolio view; run the backfill to "
            "repair.",
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
        # Probe for the occ_symbol column — tests use a minimal
        # schema without it. Production DBs all have it (added via
        # _migrate_all_columns). Read it when present so option
        # trades get the contract multiplier.
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)"
            ).fetchall()}
            has_occ = "occ_symbol" in cols
            has_fill = "fill_price" in cols
            has_dq = "data_quality" in cols
        except Exception:
            has_occ = False
            has_fill = False
            has_dq = False
        try:
            # 2026-05-21 — exclude 'pending_protective' placeholder
            # rows from the cash math (placement-time trigger prices
            # are not cash flows).
            #
            # 2026-06-11 — TWO accuracy fixes:
            # (a) Exclude EVERY never-filled terminal status, not
            #     just 'pending_protective'. A protective row that
            #     transitions pending_protective → canceled KEEPS its
            #     trigger price; the old filter counted it as a real
            #     cash flow (caught on p94: ONE canceled BBAI
            #     take-profit row injected $9,970.79 phantom cash —
            #     the entire +3.76% the dashboard showed).
            #     auto_reconciled_phantom_close marks entries the
            #     broker never filled — same rule: no money moved.
            # (b) Prefer fill_price over the decision price when the
            #     broker reported a real fill. Decision-vs-fill drift
            #     is real money at size (WCT: 9,029 shares × $0.055
            #     slippage = $497 cash error on a single trade).
            _cash_excluded = (
                "'pending_protective', 'canceled', 'expired', "
                "'rejected', 'done_for_day', "
                "'auto_reconciled_phantom_close'"
            )
            _price_expr = (
                "COALESCE(NULLIF(fill_price, 0), price)"
                if has_fill else "price"
            )
            # Exclude data_quality-tagged rows from the cash math IDENTICALLY
            # to get_virtual_positions (which now filters them). Otherwise a
            # tagged exit row keeps its cash effect while its lot is dropped
            # from positions → equity overstated (broker/journal divergence).
            # (2026-06-23 — the consistency half of the data_quality hardening.)
            _dq = " AND data_quality IS NULL" if has_dq else ""
            if has_occ:
                rows = conn.execute(
                    f"SELECT side, qty, {_price_expr}, occ_symbol "
                    "FROM trades "
                    "WHERE COALESCE(status, 'open') NOT IN "
                    f"({_cash_excluded}){_dq}"
                ).fetchall()
            else:
                rows = [
                    (r[0], r[1], r[2], None)
                    for r in conn.execute(
                        f"SELECT side, qty, {_price_expr} FROM trades "
                        "WHERE COALESCE(status, 'open') NOT IN "
                        f"({_cash_excluded}){_dq}"
                    ).fetchall()
                ]
        except Exception:
            return {
                "equity": initial_capital,
                "buying_power": initial_capital,
                "cash": initial_capital,
                "portfolio_value": 0.0,
                "status": "ACTIVE",
            }
    finally:
        conn.close()

    # Cash flows. Two bugs fixed 2026-05-17:
    # (1) 'short' (sell-to-open a stock short) wasn't crediting cash.
    #     Stocks shorted via side='short' had proceeds invisible to
    #     virtual equity — equity understated by short premium.
    # (2) Options had no contract multiplier. 1 contract = 100 shares,
    #     so the cash effect of an option trade is qty * price * 100,
    #     not qty * price. Every option trade was off by 100x.
    # Caught when AUTO_RECONCILE backfill of 33 stock-shorts dropped
    # the dashboard total by $216K — the broker had credited cash for
    # those shorts months ago, but the virtual ledger never did.
    total_buys = 0.0
    total_sells = 0.0
    for row in rows:
        side = row[0]
        qty = float(row[1] or 0)
        price = float(row[2] or 0)
        occ = row[3]
        if qty <= 0 or price <= 0:
            continue
        multiplier = 100.0 if occ else 1.0
        notional = qty * price * multiplier
        # 'buy'   = cash OUT (long open or short close via 'buy' label)
        # 'cover' = cash OUT (stock short close — buying shares back
        #           to return them to the lender)
        # 'sell'  = cash IN  (long close OR option sell-to-open premium)
        # 'short' = cash IN  (stock short open — proceeds received)
        # 'dividend' = cash IN (non-trade credit; #168)
        #
        # 2026-05-21 — 'cover' was previously in the cash-IN bucket
        # alongside 'sell'/'short' with a misleading "mostly dormant"
        # comment claiming the codebase always used 'buy' to close
        # shorts. That was wrong: stock-side short closes via the
        # `cover` label DO get written here (caught when pid16 showed
        # +$40K phantom equity from 2 NVTS covers totaling ~$20.6K
        # notional that were inflating cash by ~$41.2K — once as
        # spurious cash-IN, once as never-subtracted cash-OUT). The
        # fix moves 'cover' into the cash-OUT bucket. Audit + snapshot
        # rewrite of every profile with cover rows happens alongside
        # this commit; see CHANGELOG 2026-05-21.
        if side in ("buy", "cover"):
            total_buys += notional
        elif side in ("sell", "short", "dividend"):
            # 'dividend' added 2026-05-17 (#168): non-trade cash credits
            # captured via activities_capture.py. Stored as a trades row
            # with side='dividend', qty=1, price=dividend_amount so the
            # row participates in cash math identically to a sell.
            total_sells += notional

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


def realized_option_close_pnl(entry_px, close_px, qty, entry_side):
    """Signed realized P&L for closing ONE option leg — the single
    source of the sign convention, kept next to the FIFO in
    recompute_realized_pnl that mirrors it.

    entry_side is the side of the OPENING leg: 'buy' for a long
    (sell-to-close), 'sell' for a short (buy-to-close). Premium is
    per-contract; the 100× multiplier is applied here.

    Returns None when any input is missing / non-positive / non-sane
    (legacy or not-yet-filled rows) so callers leave pnl NULL and let
    the fill-true recompute pass book it later — never fabricate P&L
    from a price we don't have.
    """
    try:
        qty = int(qty)
        entry_px = float(entry_px) if entry_px is not None else None
        close_px = float(close_px) if close_px is not None else None
    except (TypeError, ValueError):
        return None
    if (entry_px is None or entry_px <= 0
            or close_px is None or close_px <= 0
            or qty <= 0):
        return None
    side = (entry_side or "").lower()
    if side == "buy":          # long leg: profit when it rises
        return round((close_px - entry_px) * qty * 100.0, 2)
    if side == "sell":         # short leg: profit when it falls
        return round((entry_px - close_px) * qty * 100.0, 2)
    return None


def recompute_realized_pnl(db_path=None):
    """Re-derive realized P&L on every exit row from fill-based FIFO
    lot matching. Idempotent truing pass — overwrites estimates.

    2026-06-11 (hyper-accuracy audit) — two gaps this closes:
      * Exit-row pnl was stamped at SUBMIT time as a prorated copy
        of the position's unrealized_pl (decision prices). The
        broker's actual fill lands minutes later and nothing
        corrected the estimate (PLUG: estimate +$77.83, fill-true
        $0.00).
      * Synthesized exits (reconciler bracket-child backfills, e.g.
        the WCT stop fills) carried NULL pnl forever, silently
        understating realized losses on /trades and in every
        consumer of trades.pnl (self-tuning, kelly sizing,
        post-mortems).

    FIFO semantics mirror get_virtual_positions exactly (same key
    scoping: OCC for options / symbol for stock; option multiplier
    100×; sell-with-no-long-lot on an OCC = sell-to-open), PLUS
    short-side attribution: 'buy' rows consume open short lots
    first (protective covers are journaled side='buy') and realize
    (short_entry − buy_px); leftovers open long lots.

    Includes CLOSED entry rows (their consumed lots are the cost
    basis) and excludes never-filled statuses + placeholder rows.
    Writes pnl only when it differs > $0.005 from the stored value.

    Returns the number of rows updated.
    """
    updated = 0
    with closing(_get_conn(db_path)) as conn:
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(trades)").fetchall()}
        except sqlite3.Error:
            return 0
        px_expr = ("COALESCE(NULLIF(fill_price, 0), price)"
                   if "fill_price" in cols else "price")
        occ_expr = "occ_symbol" if "occ_symbol" in cols else "NULL"
        # Exclude data_quality-tagged rows so the realized-P&L FIFO consumes
        # the IDENTICAL row-set as get_virtual_positions (the docstring's
        # "mirror exactly" promise). Without this, a tagged-but-not-canceled
        # row consumes a lot here that positions no longer does → a NON-tagged
        # trade's pnl is computed against the wrong basis and poisons
        # self-tuning / kelly / post-mortems. (2026-06-23 hardening.)
        #
        # NOTE (2026-06-24): we deliberately do NOT filter 'pending_fill' rows
        # here. get_virtual_positions AND the cash math both KEEP pending_fill
        # rows during the fill-confirmation window (using COALESCE(fill_price,
        # price)), so realized must too — else positions/cash/realized disagree
        # on a pending_fill close and the decomposition transiently breaks. An
        # earlier pending_fill guard caused exactly that and was reverted.
        dq_guard = " AND data_quality IS NULL" if "data_quality" in cols else ""
        try:
            rows = conn.execute(
                f"SELECT id, symbol, side, qty, {px_expr}, "
                f"{occ_expr}, pnl FROM trades "
                "WHERE COALESCE(status, 'open') NOT IN "
                "('pending_protective', 'canceled', 'expired', "
                " 'rejected', 'done_for_day', "
                " 'auto_reconciled_phantom_close') "
                f"{dq_guard} "
                "ORDER BY timestamp ASC, id ASC"
            ).fetchall()
        except sqlite3.Error:
            return 0
        long_lots: Dict[str, list] = {}
        short_lots: Dict[str, list] = {}
        for tid, symbol, side, qty, pxv, occ, old_pnl in rows:
            qty = float(qty or 0)
            pxv = float(pxv or 0)
            if qty <= 0 or pxv <= 0:
                continue
            key = occ if occ else symbol
            mult = 100.0 if occ else 1.0
            realized = None
            if side == "buy":
                # Cover-first: protective closes for SHORTS are
                # journaled side='buy'. Consume open short lots and
                # realize; any leftover opens a long lot.
                sl = short_lots.setdefault(key, [])
                remaining = qty
                amt = 0.0
                consumed_any = False
                while remaining > 0 and sl:
                    consumed = min(sl[0][0], remaining)
                    amt += (sl[0][1] - pxv) * consumed * mult
                    sl[0][0] -= consumed
                    remaining -= consumed
                    consumed_any = True
                    if sl[0][0] <= 0.001:
                        sl.pop(0)
                if remaining > 0:
                    long_lots.setdefault(key, []).append(
                        [remaining, pxv])
                if consumed_any:
                    realized = amt
            elif side == "short":
                short_lots.setdefault(key, []).append([qty, pxv])
            elif side == "sell":
                ll = long_lots.setdefault(key, [])
                remaining = qty
                amt = 0.0
                consumed_any = False
                while remaining > 0 and ll:
                    consumed = min(ll[0][0], remaining)
                    amt += (pxv - ll[0][1]) * consumed * mult
                    ll[0][0] -= consumed
                    remaining -= consumed
                    consumed_any = True
                    if ll[0][0] <= 0.001:
                        ll.pop(0)
                if remaining > 0 and occ:
                    # Option sell-to-open (multileg short leg) — a
                    # new short lot, not a phantom close.
                    short_lots.setdefault(key, []).append(
                        [remaining, pxv])
                if consumed_any:
                    realized = amt
            elif side == "cover":
                sl = short_lots.setdefault(key, [])
                remaining = qty
                amt = 0.0
                consumed_any = False
                while remaining > 0 and sl:
                    consumed = min(sl[0][0], remaining)
                    amt += (sl[0][1] - pxv) * consumed * mult
                    sl[0][0] -= consumed
                    remaining -= consumed
                    consumed_any = True
                    if sl[0][0] <= 0.001:
                        sl.pop(0)
                if consumed_any:
                    realized = amt
            # 'dividend' and anything else: no lot interaction.
            if realized is not None:
                old = (float(old_pnl)
                       if old_pnl is not None else None)
                if old is None or abs(old - realized) > 0.005:
                    conn.execute(
                        "UPDATE trades SET pnl = ? WHERE id = ?",
                        (round(realized, 2), tid),
                    )
                    updated += 1
        conn.commit()
    return updated


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
    with closing(_get_conn(db_path)) as conn:
        # 1. SELL rows with pnl but open status
        cur = conn.execute(
            "UPDATE trades SET status='closed' "
            "WHERE side='sell' AND pnl IS NOT NULL AND status='open'"
        )
        sells_fixed = cur.rowcount

        # 2. Step 2 was REMOVED 2026-05-18 (15:55 ET) after a second
        # outage caused by the same code path. Earlier in the day the
        # empty-open_symbols variant was fixed (see ca2cdac), but the
        # NON-empty variant has a race condition that's just as
        # destructive: in the few seconds between when a BUY order is
        # submitted and when the broker registers the fill in
        # list_positions, the reconciler sees an INCOMPLETE
        # broker_open_symbols set and the `symbol NOT IN
        # (open_symbols)` SQL flips fresh open BUYs to closed. With
        # status='closed' BUYs excluded from get_virtual_positions,
        # the dashboard collapses to cash-only equity. Caught
        # 2026-05-18 15:38–15:51 when P12/P13/P14 each placed their
        # day-1 buys and every BUY was marked closed within minutes
        # by a reconcile cycle that fired while the broker was still
        # processing the orders.
        #
        # The blanket SQL is redundant: reconcile_journal_to_broker
        # has _classify_long_phantom (called from
        # _task_reconcile_trade_statuses) which does per-trade
        # reasoning by checking each BUY's order_id status at the
        # broker — only acts when the order is truly terminal
        # (filled+sold or canceled). The FIFO matching in step 3
        # below closes BUYs that have a real matching SELL with
        # realized pnl. Those two paths cover every legitimate
        # close-detection case without the race-condition false
        # positives.
        buys_fixed = 0

        conn.commit()
    # 3. Fill-true realized-P&L pass on exit rows. 2026-06-11 — the
    # previous in-line FIFO here was DEAD CODE: it computed per-lot
    # realized amounts into a local list and never wrote them
    # anywhere (a leftover from the "pnl belongs on the SELL row"
    # migration). Replaced by recompute_realized_pnl, which actually
    # stamps exit rows, prefers broker fill prices, handles shorts,
    # covers, and the option multiplier, and is idempotent.
    pnl_computed = recompute_realized_pnl(db_path)
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
    indicators_json = json.dumps(indicators) if indicators else None
    with closing(_get_conn(db_path)) as conn:
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
    with closing(_get_conn(db_path)) as conn:
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
    return snapshot_id


def get_trade_history(symbol=None, limit=50, db_path=None):
    """Return recent trades, optionally filtered by symbol.

    Returns a list of dicts.
    """
    with closing(_get_conn(db_path)) as conn:
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
    return [dict(r) for r in rows]


def get_performance_summary(db_path=None):
    """Return aggregate performance metrics from the trade journal.

    Returns a dict with total_trades, winning_trades, losing_trades, win_rate,
    total_pnl, avg_pnl, best_trade, worst_trade.
    """
    with closing(_get_conn(db_path)) as conn:
        # Phase 5e — exclude data_quality-tagged rows.
        _dq = data_quality_clause(conn)

        total = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE pnl IS NOT NULL{_dq}"
        ).fetchone()[0]
        if total == 0:
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

        row = conn.execute(f"""
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losing_trades,
                SUM(pnl) AS total_pnl,
                AVG(pnl) AS avg_pnl,
                MAX(pnl) AS best_trade,
                MIN(pnl) AS worst_trade
            FROM trades
            WHERE pnl IS NOT NULL{_dq}
        """).fetchone()

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
    with closing(_get_conn(db_path)) as conn:
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
    with closing(_get_conn(db_path)) as conn:
        rows = conn.execute(
            """SELECT date, equity, cash, portfolio_value, num_positions, daily_pnl
               FROM daily_snapshots
               ORDER BY date DESC
               LIMIT ?""",
            (days,),
        ).fetchall()
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
    with closing(_get_conn(db_path)) as conn:
        where_kind = ""
        if kind == "stocks":
            where_kind = " AND occ_symbol IS NULL"
        elif kind == "options":
            where_kind = " AND occ_symbol IS NOT NULL"
        # Phase 5e (2026-05-12): exclude data-quality-tagged rows from
        # the aggregate. data_quality IS NULL for normal trades; a
        # tag string indicates a known data corruption (e.g.,
        # 'phantom_stop_2026_05_11'). The excluded count is returned
        # in `excluded_data_quality` so operators see when corrupt
        # data is present — masked from the metric, surfaced as a
        # separate signal.
        #
        # Back-compat: minimal test fixtures + legacy DBs may not
        # have the data_quality column yet. Detect its presence
        # before adding the filter.
        try:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(trades)"
            ).fetchall()}
            has_dq = "data_quality" in cols
        except Exception:
            has_dq = False
        excluded_data_quality_clause = (
            " AND data_quality IS NULL" if has_dq else ""
        )
        try:
            # Count of rows excluded by the data_quality filter.
            # Returns 0 when the column doesn't exist.
            if has_dq:
                excluded_data_quality = conn.execute(f"""
                    SELECT COUNT(*) FROM trades
                    WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
                      AND decision_price > 0{where_kind}
                      AND data_quality IS NOT NULL
                """).fetchone()[0] or 0
            else:
                excluded_data_quality = 0

            row = conn.execute(f"""
                SELECT
                    COUNT(*) AS trades_with_fills,
                    AVG(slippage_pct) AS avg_slippage_pct,
                    MAX(ABS(slippage_pct)) AS worst_slippage_pct,
                    SUM(ABS(fill_price - decision_price) * qty)
                        AS total_slippage_magnitude,
                    SUM(
                        CASE
                            -- Cash-OUT sides (you pay; bad slippage when
                            -- fill > decision): buy / sell_short / cover.
                            -- ('cover' moved here 2026-05-21 alongside the
                            -- equity-cash fix in get_virtual_account_info;
                            -- same misclassification class.)
                            WHEN side IN ('buy', 'sell_short', 'cover')
                                THEN (fill_price - decision_price) * qty
                            -- Cash-IN sides (you receive; bad slippage when
                            -- fill < decision): sell / short.
                            WHEN side IN ('sell', 'short')
                                THEN (decision_price - fill_price) * qty
                            ELSE 0
                        END
                    ) AS total_slippage_cost
                FROM trades
                WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
                  AND decision_price > 0{where_kind}{excluded_data_quality_clause}
            """).fetchone()

            if not row or row["trades_with_fills"] == 0:
                return None

            # Get the worst slippage trade details — same kind filter
            # AND same data_quality filter so the row shown matches the
            # aggregate (operators clicking "worst" should see a trade
            # that's actually IN the average, not the phantom-stop
            # row that got excluded).
            worst = conn.execute(f"""
                SELECT symbol, side, qty, decision_price, fill_price, slippage_pct, timestamp
                FROM trades
                WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
                  AND decision_price > 0{where_kind}{excluded_data_quality_clause}
                ORDER BY ABS(slippage_pct) DESC
                LIMIT 1
            """).fetchone()

            return {
                "trades_with_fills": row["trades_with_fills"],
                "avg_slippage_pct": round(row["avg_slippage_pct"] or 0, 4),
                "worst_slippage_pct": round(row["worst_slippage_pct"] or 0, 4),
                "total_slippage_cost": round(row["total_slippage_cost"] or 0, 2),
                "total_slippage_magnitude": round(row["total_slippage_magnitude"] or 0, 2),
                "worst_trade": dict(worst) if worst else None,
                # Phase 5e: count of rows excluded by data_quality
                # tag (e.g., phantom-stop incident artifacts). Zero
                # when the table has no tagged rows in scope.
                "excluded_data_quality": excluded_data_quality,
            }
        except Exception:
            return None


def is_migration_done(db_path, key):
    """Phase 5d (2026-05-11): check whether a one-time migration
    has already run on this DB. Used by backfill scripts to gate
    themselves so the same migration doesn't re-fire on every
    scheduler restart."""
    if not db_path or not key:
        return False
    try:
        conn = _get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM migration_markers WHERE key = ?",
                (key,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def mark_migration_done(db_path, key, details=None):
    """Phase 5d (2026-05-11): record that a one-time migration has
    completed. Idempotent (INSERT OR REPLACE). Caller should mark
    AFTER the migration's writes have committed."""
    if not db_path or not key:
        return False
    try:
        conn = _get_conn(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO migration_markers "
                "(key, details) VALUES (?, ?)",
                (key, str(details) if details is not None else None),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


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
        # 2026-05-12 — exclude data_quality-tagged rows. Without this,
        # a phantom-stop-style incident that pollutes a MULTILEG leg
        # row would drive the option resolver to compute a wrong
        # spread P&L → wrong actual_return_pct on the linked
        # ai_predictions row → wrong alpha_decay/strategy_lifecycle
        # signal. Defense-in-depth: today's MULTILEG rows are clean,
        # but the filter ensures future incidents can't propagate
        # without going through the data_quality-tagging audit trail.
        _dq = data_quality_clause(conn)
        try:
            rows = conn.execute(
                f"""SELECT occ_symbol, qty, price, side, fill_price
                   FROM trades
                   WHERE signal_type = 'MULTILEG'
                   AND (order_id = ? OR reason LIKE ?){_dq}""",
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


def data_quality_clause(conn, table: str = "trades") -> str:
    """Phase 5e (2026-05-12, generalized 2026-05-13) — return
    ' AND data_quality IS NULL' if the named table has the
    data_quality column, else ''.

    Use in any analytics SQL on `trades` or `ai_predictions` to
    exclude rows tagged with a known data-corruption marker (e.g.,
    'phantom_stop_2026_05_11').

    The 2026-05-13 generalization added the `table` parameter
    after the structural audit found 11 analytics queries on
    `ai_predictions` that couldn't filter data_quality (the
    column didn't exist on that table). Same column was added in
    the same commit; this helper now serves both tables uniformly.

    Wrapped in a helper so analytics call sites don't have to
    duplicate the column-presence check (which is needed for
    back-compat with legacy DBs / minimal test fixtures that
    pre-date the migration).

    Usage:
        # trades (default — back-compat with old callers):
        cls = data_quality_clause(conn)
        rows = conn.execute(
            f"SELECT ... FROM trades WHERE pnl IS NOT NULL{cls}"
        ).fetchall()

        # ai_predictions:
        cls = data_quality_clause(conn, table='ai_predictions')
        rows = conn.execute(
            f"SELECT AVG(actual_return_pct) FROM ai_predictions "
            f"WHERE status='resolved'{cls}"
        ).fetchall()
    """
    try:
        cols = {row[1] for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()}
        return " AND data_quality IS NULL" if "data_quality" in cols else ""
    except Exception:
        return ""


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
    # 2026-07-02 (review M2): use the DERIVED authority set — the static
    # constant alone would misreport every specialist that DECLARES authority
    # (e.g. the structural option gate) as has_authority=False with 0
    # effective vetoes, the exact silent-no-op misreport this stats function
    # exists to expose.
    try:
        from ensemble import veto_authorized_names
        from specialists import discover_specialists
        VETO_AUTHORIZED = veto_authorized_names(discover_specialists())
    except Exception:
        try:
            from ensemble import VETO_AUTHORIZED
        except Exception:
            VETO_AUTHORIZED = {"risk_assessor", "adversarial_reviewer"}

    counts = {}  # name -> {"total": int, "vetoes": int}
    for db_path in db_paths:
        try:
            with closing(_get_conn(db_path)) as conn:
                rows = conn.execute(
                    """SELECT specialist_name, verdict, COUNT(*) as n
                       FROM specialist_outcomes
                       WHERE recorded_at > datetime('now', '-' || ? || ' days')
                       GROUP BY specialist_name, verdict""",
                    (days,),
                ).fetchall()
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as _so_exc:
            # Per-DB specialist outcomes aggregation loop; one bad
            # DB shouldn't kill cross-profile reporting. Surface
            # for follow-up.
            logger.debug(
                "specialist_outcomes aggregation failed for %s: %s: %s",
                db_path, type(_so_exc).__name__, _so_exc,
            )
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
