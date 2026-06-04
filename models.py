"""Database models and helpers for multi-user platform.

Uses sqlite3 directly (matching journal.py patterns). All user data,
segment configurations, decision logs, and API usage tracking live in a
single database file.
"""

import sqlite3
import json
import logging
from contextlib import closing
from datetime import datetime, date
from typing import Optional, Dict, List, Any

import bcrypt

import config
from crypto import encrypt, decrypt
from segments import get_segment, list_segments
from user_context import UserContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _get_conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Get a connection to the user database.

    PRAGMAs:
      - journal_mode=WAL: readers don't block writers, writers don't block readers.
      - foreign_keys=ON: enforce FK constraints (sqlite default is OFF).
      - busy_timeout=5000: when a write lock IS contested (rare under WAL),
        wait up to 5 seconds for it to clear instead of immediately raising
        OperationalError. Eliminates the entire class of "transient lock"
        failures the silent-pass blocks in views.py were protecting against.
    """
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def open_profile_db(db_path: str) -> sqlite3.Connection:
    """The single authorized way for views.py to open a per-profile DB.

    Ensures the connection is ready to read every column the dashboard
    code references. Three migrations run idempotently:

      - ai_tracker.init_tracker_db: CREATE TABLE IF NOT EXISTS
        ai_predictions with the legacy column set.
      - journal.init_db: CREATE TABLE IF NOT EXISTS trades + run
        journal._migrate_columns which ALTER-ADD-COLUMNs every
        column added since the original schema (regime_at_prediction,
        strategy_type, features_json, days_held, prediction_type on
        ai_predictions; the slippage / option / protective-stop set
        on trades). Without this, a never-written-to or pre-migration
        profile DB throws "no such column" on dashboard reads — the
        exact failure mode the silent-pass swallows in views.py used
        to hide.

    PRAGMAs (WAL + busy_timeout=5000) eliminate the transient-lock
    failure mode on the read connection.

    Use this everywhere views.py would otherwise call sqlite3.connect()
    directly. It is the foundation for eliminating the silent-pass
    swallows: with this helper the failure modes the swallows were
    catching can no longer occur on a healthy DB."""
    from ai_tracker import init_tracker_db
    from journal import init_db as init_journal_db
    init_tracker_db(db_path)        # CREATE TABLE ai_predictions
    init_journal_db(db_path)        # CREATE TABLE trades + ALTER ADD COLUMN migrations
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_user_db(db_path: Optional[str] = None) -> None:
    """Create all multi-user tables if they do not exist."""
    conn = _get_conn(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                is_active INTEGER NOT NULL DEFAULT 1,
                is_admin INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'admin',
                linked_to_user_id INTEGER,
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                anthropic_api_key_enc TEXT NOT NULL DEFAULT '',
                notification_email TEXT NOT NULL DEFAULT '',
                resend_api_key_enc TEXT NOT NULL DEFAULT '',
                last_login_at TEXT,
                excluded_symbols TEXT NOT NULL DEFAULT '[]',
                scanning_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS user_segment_configs (
                user_id INTEGER NOT NULL,
                segment TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                stop_loss_pct REAL NOT NULL DEFAULT 0.03,
                take_profit_pct REAL NOT NULL DEFAULT 0.10,
                max_position_pct REAL NOT NULL DEFAULT 0.10,
                max_total_positions INTEGER NOT NULL DEFAULT 10,
                ai_confidence_threshold INTEGER NOT NULL DEFAULT 25,
                min_price REAL NOT NULL DEFAULT 1.0,
                max_price REAL NOT NULL DEFAULT 20.0,
                min_volume INTEGER NOT NULL DEFAULT 500000,
                volume_surge_multiplier REAL NOT NULL DEFAULT 2.0,
                rsi_overbought REAL NOT NULL DEFAULT 85.0,
                rsi_oversold REAL NOT NULL DEFAULT 25.0,
                momentum_5d_gain REAL NOT NULL DEFAULT 3.0,
                momentum_20d_gain REAL NOT NULL DEFAULT 5.0,
                breakout_volume_threshold REAL NOT NULL DEFAULT 1.0,
                gap_pct_threshold REAL NOT NULL DEFAULT 3.0,
                strategy_momentum_breakout INTEGER NOT NULL DEFAULT 1,
                strategy_volume_spike INTEGER NOT NULL DEFAULT 1,
                strategy_mean_reversion INTEGER NOT NULL DEFAULT 1,
                strategy_gap_and_go INTEGER NOT NULL DEFAULT 1,
                custom_watchlist TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (user_id, segment),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS user_api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                anthropic_calls INTEGER NOT NULL DEFAULT 0,
                UNIQUE (user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS alpaca_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'Default',
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT 'https://paper-api.alpaca.markets',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS trading_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                market_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                stop_loss_pct REAL NOT NULL DEFAULT 0.03,
                take_profit_pct REAL NOT NULL DEFAULT 0.10,
                max_position_pct REAL NOT NULL DEFAULT 0.10,
                max_total_positions INTEGER NOT NULL DEFAULT 10,
                ai_confidence_threshold INTEGER NOT NULL DEFAULT 25,
                min_price REAL NOT NULL DEFAULT 1.0,
                max_price REAL NOT NULL DEFAULT 20.0,
                min_volume INTEGER NOT NULL DEFAULT 500000,
                volume_surge_multiplier REAL NOT NULL DEFAULT 2.0,
                rsi_overbought REAL NOT NULL DEFAULT 85.0,
                rsi_oversold REAL NOT NULL DEFAULT 25.0,
                momentum_5d_gain REAL NOT NULL DEFAULT 3.0,
                momentum_20d_gain REAL NOT NULL DEFAULT 5.0,
                breakout_volume_threshold REAL NOT NULL DEFAULT 1.0,
                gap_pct_threshold REAL NOT NULL DEFAULT 3.0,
                strategy_momentum_breakout INTEGER NOT NULL DEFAULT 1,
                strategy_volume_spike INTEGER NOT NULL DEFAULT 1,
                strategy_mean_reversion INTEGER NOT NULL DEFAULT 1,
                strategy_gap_and_go INTEGER NOT NULL DEFAULT 1,
                custom_watchlist TEXT NOT NULL DEFAULT '[]',
                maga_mode INTEGER NOT NULL DEFAULT 0,
                enable_short_selling INTEGER NOT NULL DEFAULT 0,
                short_stop_loss_pct REAL NOT NULL DEFAULT 0.08,
                short_take_profit_pct REAL NOT NULL DEFAULT 0.08,
                short_max_position_pct REAL DEFAULT NULL,
                short_max_hold_days INTEGER NOT NULL DEFAULT 10,
                target_short_pct REAL NOT NULL DEFAULT 0.0,
                target_book_beta REAL DEFAULT NULL,
                enable_self_tuning INTEGER NOT NULL DEFAULT 1,
                ai_provider TEXT NOT NULL DEFAULT 'anthropic',
                ai_model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001',
                ai_api_key_enc TEXT NOT NULL DEFAULT '',
                schedule_type TEXT NOT NULL DEFAULT 'market_hours',
                custom_start TEXT NOT NULL DEFAULT '09:30',
                custom_end TEXT NOT NULL DEFAULT '16:00',
                custom_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
                drawdown_pause_pct REAL NOT NULL DEFAULT 0.20,
                drawdown_reduce_pct REAL NOT NULL DEFAULT 0.10,
                avoid_earnings_days INTEGER NOT NULL DEFAULT 2,
                skip_first_minutes INTEGER NOT NULL DEFAULT 0,
                enable_consensus INTEGER NOT NULL DEFAULT 0,
                consensus_model TEXT NOT NULL DEFAULT '',
                consensus_api_key_enc TEXT NOT NULL DEFAULT '',
                enable_shadow_eval INTEGER NOT NULL DEFAULT 0,
                shadow_models TEXT NOT NULL DEFAULT '[]',
                shadow_api_keys_enc TEXT NOT NULL DEFAULT '{}',
                use_atr_stops INTEGER NOT NULL DEFAULT 1,
                atr_multiplier_sl REAL NOT NULL DEFAULT 2.0,
                atr_multiplier_tp REAL NOT NULL DEFAULT 3.0,
                use_trailing_stops INTEGER NOT NULL DEFAULT 1,
                trailing_atr_multiplier REAL NOT NULL DEFAULT 1.5,
                use_limit_orders INTEGER NOT NULL DEFAULT 0,
                max_correlation REAL NOT NULL DEFAULT 0.7,
                max_sector_positions INTEGER NOT NULL DEFAULT 5,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            -- NOTE: For existing databases, run these migrations manually:
            -- ALTER TABLE trading_profiles ADD COLUMN maga_mode INTEGER NOT NULL DEFAULT 0;
            -- ALTER TABLE trading_profiles ADD COLUMN ai_provider TEXT NOT NULL DEFAULT 'anthropic';
            -- ALTER TABLE trading_profiles ADD COLUMN ai_model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001';
            -- ALTER TABLE trading_profiles ADD COLUMN ai_api_key_enc TEXT NOT NULL DEFAULT '';
            -- ALTER TABLE trading_profiles ADD COLUMN enable_short_selling INTEGER NOT NULL DEFAULT 0;
            -- ALTER TABLE trading_profiles ADD COLUMN enable_self_tuning INTEGER NOT NULL DEFAULT 1;
            -- ALTER TABLE trading_profiles ADD COLUMN schedule_type TEXT NOT NULL DEFAULT 'market_hours';
            -- ALTER TABLE trading_profiles ADD COLUMN custom_start TEXT NOT NULL DEFAULT '09:30';
            -- ALTER TABLE trading_profiles ADD COLUMN custom_end TEXT NOT NULL DEFAULT '16:00';
            -- ALTER TABLE trading_profiles ADD COLUMN custom_days TEXT NOT NULL DEFAULT '0,1,2,3,4';
            -- ALTER TABLE trading_profiles ADD COLUMN drawdown_pause_pct REAL NOT NULL DEFAULT 0.20;
            -- ALTER TABLE trading_profiles ADD COLUMN drawdown_reduce_pct REAL NOT NULL DEFAULT 0.10;
            -- ALTER TABLE trading_profiles ADD COLUMN avoid_earnings_days INTEGER NOT NULL DEFAULT 2;
            -- ALTER TABLE trading_profiles ADD COLUMN skip_first_minutes INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                activity_type TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                symbol TEXT,
                FOREIGN KEY (profile_id) REFERENCES trading_profiles(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS symbol_names (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tuning_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                adjustment_type TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                reason TEXT NOT NULL,
                win_rate_at_change REAL,
                predictions_resolved INTEGER,
                outcome_after TEXT DEFAULT 'pending',
                win_rate_after REAL,
                reviewed_at TEXT,
                -- Item 4 of docs/17 — set when the auto-expire optimizer
                -- has processed this row. NULL = not yet expired.
                expired_at TEXT DEFAULT NULL,
                FOREIGN KEY (profile_id) REFERENCES trading_profiles(id)
            );

            CREATE TABLE IF NOT EXISTS param_references (
                profile_id INTEGER NOT NULL,
                parameter_name TEXT NOT NULL,
                reference_value TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (profile_id, parameter_name)
                -- No FK to trading_profiles intentionally: orphan rows
                -- after a profile delete are harmless (keyed by id;
                -- the reset script wipes them via clear_param_references).
                -- Letting the FK in caused test fixtures that exercise
                -- the helpers directly to need profile-row setup,
                -- which is needless coupling.
            );
        """)
        conn.commit()

        # Auto-migrate: add columns that may not exist in older databases.
        # EVERY column that was ever added after initial table creation MUST be here.
        # This is the ONLY safe way to evolve the schema — CREATE TABLE IF NOT EXISTS
        # will NOT add new columns to an existing table.
        _migrations = [
            # --- users table ---
            ("users", "excluded_symbols", "TEXT NOT NULL DEFAULT '[]'"),
            ("users", "scanning_active", "INTEGER NOT NULL DEFAULT 1"),
            ("users", "role", "TEXT NOT NULL DEFAULT 'admin'"),
            ("users", "linked_to_user_id", "INTEGER"),
            # 2026-05-19 — provider-agnostic "fallback LLM key" column.
            # The legacy `anthropic_api_key_enc` column stays (it now
            # stores the key for whatever provider `llm_provider`
            # selects — the column name is historical). This pair is
            # used by CLI tools / helpers that don't have a profile
            # context: main.py ai-analyze, news_sentiment, etc.
            # Default 'anthropic' preserves behavior for existing users
            # whose stored key is an Anthropic key.
            ("users", "llm_provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
            # 2026-05-21 — user-configurable fallback MODEL. Pairs with
            # llm_provider above. Lets the operator set a specific
            # model (e.g. gemini-2.5-flash) as the same-provider
            # fallback when the profile-level primary model
            # (e.g. gemini-2.5-flash-lite) trips its circuit. Was
            # added after Gemini's flash-lite tier started 503ing
            # ~40-50% of the time, and the chain had no way to
            # promote to the more-reliable flash tier without
            # losing the cost savings on the modal call path. NULL
            # (or empty) means "use the provider's default model"
            # for backward compat with users who haven't picked.
            ("users", "llm_model", "TEXT"),
            # --- user_segment_configs table ---
            ("user_segment_configs", "alpaca_api_key_enc", "TEXT NOT NULL DEFAULT ''"),
            ("user_segment_configs", "alpaca_secret_key_enc", "TEXT NOT NULL DEFAULT ''"),
            # --- trading_profiles table ---
            ("trading_profiles", "maga_mode", "INTEGER NOT NULL DEFAULT 0"),
            # 2026-05-12 — default flipped ON (was 0). See conviction-TP
            # commit for the same reasoning: feature designed correctly
            # but sitting opt-in / unused for months. Audit showed shorts
            # had +4.04% avg return on 40 resolved predictions. Crypto
            # profiles still skip via the migration's name filter.
            ("trading_profiles", "enable_short_selling", "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "short_stop_loss_pct", "REAL NOT NULL DEFAULT 0.08"),
            ("trading_profiles", "short_take_profit_pct", "REAL NOT NULL DEFAULT 0.08"),
            # P1.9b of LONG_SHORT_PLAN.md
            ("trading_profiles", "short_max_position_pct", "REAL DEFAULT NULL"),
            ("trading_profiles", "short_max_hold_days", "INTEGER NOT NULL DEFAULT 10"),
            # P2.2 of LONG_SHORT_PLAN.md
            ("trading_profiles", "target_short_pct", "REAL NOT NULL DEFAULT 0.0"),
            # P4.1 of LONG_SHORT_PLAN.md — beta-targeted construction.
            ("trading_profiles", "target_book_beta", "REAL DEFAULT NULL"),
            ("trading_profiles", "enable_self_tuning", "INTEGER NOT NULL DEFAULT 1"),
            # 2026-05-19 — explicit per-profile asset-class
            # enablement flags. `enable_options` already existed; the
            # other two complete the trio so the Settings UI shows
            # operator-controllable Stocks / Options / Crypto toggles.
            # Defaults preserve current behavior: every existing
            # profile already trades stocks (default 1); no profile
            # trades crypto (default 0).
            ("trading_profiles", "enable_stocks", "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "enable_crypto", "INTEGER NOT NULL DEFAULT 0"),
            # 2026-05-19 — Scope C: per-profile opt-in to shadow eval
            # of the new Pipeline.run_cycle dispatch path. When set,
            # `trade_pipeline.run_trade_cycle` calls
            # `pipelines.shadow.shadow_compare` at end-of-cycle to
            # log how the new pipeline path would have classified
            # the same proposals — read-only, no broker impact.
            # Default OFF; operator turns it on per profile for soak.
            ("trading_profiles", "enable_pipeline_shadow_eval", "INTEGER NOT NULL DEFAULT 0"),
            # 2026-05-19 Scope C cutover gate. When 1, the scheduler
            # dispatches this profile's cycles through
            # `pipelines.dispatch.run_via_pipelines` (which calls
            # Pipeline.run_cycle per enabled pipeline) instead of the
            # legacy `trade_pipeline.run_trade_cycle`. Flip per profile
            # AFTER shadow soak shows verdict agreement ≥ 95% for
            # 1–2 trading days. Default OFF (legacy dispatch).
            ("trading_profiles", "use_pipeline_dispatch", "INTEGER NOT NULL DEFAULT 0"),
            # 2026-05-19 reconciler safety net. When 1, the scheduler
            # SKIPS this profile's trade-pipeline dispatch (no new
            # entries). Set automatically by the reconciler when it
            # detects a journal-vs-broker drift that would require
            # synthesizing journal rows (a `backfill_sell` /
            # `backfill_cover` / `broker_orphan` / `journal_phantom`).
            # Auto-clears when the next reconcile pass shows no
            # synthesis needed. Operator can also clear manually
            # from Settings after fixing the root-cause submit_order
            # leak. Existing exit / monitoring tasks continue to run
            # while halted — only new entries are blocked.
            ("trading_profiles", "trading_halted", "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "halt_reason", "TEXT"),
            ("trading_profiles", "halted_at", "TEXT"),
            ("trading_profiles", "ai_provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
            ("trading_profiles", "ai_model", "TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001'"),
            ("trading_profiles", "ai_api_key_enc", "TEXT NOT NULL DEFAULT ''"),
            ("trading_profiles", "schedule_type", "TEXT NOT NULL DEFAULT 'market_hours'"),
            ("trading_profiles", "custom_start", "TEXT NOT NULL DEFAULT '09:30'"),
            ("trading_profiles", "custom_end", "TEXT NOT NULL DEFAULT '16:00'"),
            ("trading_profiles", "custom_days", "TEXT NOT NULL DEFAULT '0,1,2,3,4'"),
            ("trading_profiles", "drawdown_pause_pct", "REAL NOT NULL DEFAULT 0.20"),
            ("trading_profiles", "drawdown_reduce_pct", "REAL NOT NULL DEFAULT 0.10"),
            ("trading_profiles", "avoid_earnings_days", "INTEGER NOT NULL DEFAULT 2"),
            ("trading_profiles", "skip_first_minutes", "INTEGER NOT NULL DEFAULT 5"),
            ("trading_profiles", "enable_consensus", "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "consensus_model", "TEXT NOT NULL DEFAULT ''"),
            ("trading_profiles", "consensus_api_key_enc", "TEXT NOT NULL DEFAULT ''"),
            ("trading_profiles", "use_atr_stops", "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "atr_multiplier_sl", "REAL NOT NULL DEFAULT 2.0"),
            ("trading_profiles", "atr_multiplier_tp", "REAL NOT NULL DEFAULT 3.0"),
            ("trading_profiles", "use_trailing_stops", "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "trailing_atr_multiplier", "REAL NOT NULL DEFAULT 1.5"),
            ("trading_profiles", "use_limit_orders", "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "max_correlation", "REAL NOT NULL DEFAULT 0.7"),
            ("trading_profiles", "max_sector_positions", "INTEGER NOT NULL DEFAULT 5"),
            # 2026-05-12 — flipped default ON. Originally shipped off
            # (2026-04-15) as opt-in. The data shows fixed TPs are
            # capping runaway winners — UNH-style trades where the AI
            # set $379 target but the position kept running to $396+.
            # ON default + AI-tunable means: every new profile starts
            # with "let winners run" enabled; the self-tuner will flip
            # it back to OFF for profiles where MFE capture is already
            # strong and stop-to-TP is balanced (no asymmetry to fix).
            ("trading_profiles", "use_conviction_tp_override", "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "conviction_tp_min_confidence", "REAL NOT NULL DEFAULT 70.0"),
            ("trading_profiles", "conviction_tp_min_adx", "REAL NOT NULL DEFAULT 25.0"),
            # Virtual account layer
            ("trading_profiles", "is_virtual", "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "initial_capital", "REAL NOT NULL DEFAULT 100000.0"),
            ("trading_profiles", "alpaca_account_id", "INTEGER"),
            # Layer 2 — weighted signal intensity
            ("trading_profiles", "signal_weights", "TEXT NOT NULL DEFAULT '{}'"),
            # Layer 3 — per-regime parameter overrides
            ("trading_profiles", "regime_overrides", "TEXT NOT NULL DEFAULT '{}'"),
            # Layer 4 — per-time-of-day parameter overrides
            ("trading_profiles", "tod_overrides", "TEXT NOT NULL DEFAULT '{}'"),
            # Layer 7 — per-symbol parameter overrides (most-specific tier)
            ("trading_profiles", "symbol_overrides", "TEXT NOT NULL DEFAULT '{}'"),
            # Layer 6 — adaptive AI prompt structure (per-section verbosity)
            ("trading_profiles", "prompt_layout", "TEXT NOT NULL DEFAULT '{}'"),
            # Layer 9 — auto capital allocation (per-user opt-in toggle)
            ("users", "auto_capital_allocation", "INTEGER NOT NULL DEFAULT 0"),
            # Cost guard — user-configurable daily ceiling (NULL = auto-compute
            # via trailing-7-day-avg × 1.5, floored at $5/day).
            ("users", "daily_cost_ceiling_usd", "REAL"),
            # Shadow eval — separate daily cap so shadow traffic can never
            # blow out the operational AI budget. NULL = use the
            # SHADOW_DAILY_COST_CAP_USD env var default ($1/day).
            ("users", "shadow_daily_cost_cap_usd", "REAL"),
            # 2026-06-04 — operator-tunable scan cadence (minutes).
            # multi_scheduler reads this each loop iteration so the
            # change takes effect on the next cycle (no restart needed).
            # Valid range enforced server-side at 1..60; UI restricts
            # to the safe options 15/10/5/3/2 (1-min is excluded because
            # the slowest scan can exceed 60s, causing cycle overlap).
            # Default 15 preserves pre-2026-06-04 behavior.
            ("users", "scan_interval_minutes",
             "INTEGER NOT NULL DEFAULT 15"),
            # Per-profile opt-in: lets the tuner A/B test ai_provider/ai_model
            # within the cost guard. Default OFF so cost-conscious users
            # aren't surprised by Sonnet/Opus calls.
            ("trading_profiles", "ai_model_auto_tune", "INTEGER NOT NULL DEFAULT 0"),
            # Layer 9 — recommended capital scale per profile (1.0 = baseline,
            # 0.5 = halved, 2.0 = doubled). The auto-allocator updates this;
            # the trading pipeline reads it before computing position sizes.
            ("trading_profiles", "capital_scale", "REAL NOT NULL DEFAULT 1.0"),
            # Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md — per-profile
            # disable list for ensemble specialists. JSON array of names
            # like ["pattern_recognizer"] when calibration data shows
            # that specialist is anti-correlated. Maintained by the
            # daily _task_specialist_health_check (auto-disable when
            # calibrator slope is inverse for ≥30 days; auto-re-enable
            # when slope recovers to positive). Hard floor: never more
            # than 2 of 4 specialists disabled.
            ("trading_profiles", "disabled_specialists", "TEXT NOT NULL DEFAULT '[]'"),
            # Lever 2 of COST_AND_QUALITY_LEVERS_PLAN.md — meta-model
            # pre-gate threshold. Candidates with meta_prob < this value
            # are dropped BEFORE the ensemble runs. 0.0 = disabled (gate
            # falls open). 0.5 default = drop candidates the meta-model
            # is more confident the AI is wrong about than right.
            # 2026-05-13 — default lowered 0.5 → 0.35. Audit (139
            # cycles, 1985 candidates, 68% drop rate, median 73% per
            # cycle) showed the 0.5 threshold was structurally
            # over-filtering. AI-tunable via
            # `_optimize_meta_pregate_threshold`.
            ("trading_profiles", "meta_pregate_threshold", "REAL NOT NULL DEFAULT 0.35"),

            # Item 2b of COMPETITIVE_GAP_PLAN.md — intraday risk monitor
            # auto-halt. When alerts fire (drawdown acceleration, vol
            # spike, sector swing, halted positions), the trade pipeline
            # blocks new entries until the halt auto-clears. Default ON
            # for capital preservation; user can disable per profile if
            # they want to override the safety layer.
            ("trading_profiles", "enable_intraday_risk_halt",
                "INTEGER NOT NULL DEFAULT 1"),
            # Item 1b — stat-arb cointegrated pair book. Off by default
            # because pair trades require both legs (one long + one short)
            # so a long-only profile can't act on the surfaced pairs.
            # Long/short profiles can opt in.
            ("trading_profiles", "enable_stat_arb_pairs",
                "INTEGER NOT NULL DEFAULT 0"),
            # Item 2a — Barra-style portfolio risk daily snapshot. On by
            # default; the snapshot is informational (surfaces VaR + ES +
            # stress scenarios in the AI prompt). Disabling stops the
            # snapshot task and removes the prompt section.
            ("trading_profiles", "enable_portfolio_risk_snapshot",
                "INTEGER NOT NULL DEFAULT 1"),
            # Item 1c — long-vol portfolio tail-risk hedge. OFF by
            # default: opt-in because it costs real money in put premium
            # and only makes sense when the user has decided they want
            # active tail protection (vs the existing passive layers like
            # crisis_state). Auto-opens SPY puts when drawdown / crisis /
            # VaR triggers fire; auto-closes when all clear.
            ("trading_profiles", "enable_long_vol_hedge",
                "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "long_vol_hedge_drawdown_pct",
                "REAL NOT NULL DEFAULT 0.05"),
            ("trading_profiles", "long_vol_hedge_var_pct",
                "REAL NOT NULL DEFAULT 0.03"),
            ("trading_profiles", "long_vol_hedge_premium_pct",
                "REAL NOT NULL DEFAULT 0.01"),
            # Item 1a / Phase C3 — wheel automation. JSON list of
            # symbols this profile is opted into for the wheel cycle
            # (cash → CSP → assigned → shares → CC → called away → cash).
            # Empty list = wheel inactive for this profile (default).
            ("trading_profiles", "wheel_symbols",
                "TEXT NOT NULL DEFAULT '[]'"),
            # 2026-05-17 — ablation flags for the post-audit fresh-
            # start experiment (docs/15_EXPERIMENT_DESIGN). Each
            # disables one major system component so we can attribute
            # alpha to specific subsystems:
            # - enable_alt_data: insider / congress / Form 4 / 13F /
            #   sentiment feeds. Off → AI prompt sees only price +
            #   technicals + macro.
            # - enable_meta_model: GBM + SGD confidence-adjustment
            #   layer. Off → raw AI confidence used directly (no
            #   meta-learning calibration).
            # - enable_options: single-leg + multi-leg options
            #   proposals. Off → AI is restricted to stock trades.
            # All default ON to preserve current behavior; ablation
            # profiles flip them off individually.
            ("trading_profiles", "enable_alt_data",
                "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "enable_meta_model",
                "INTEGER NOT NULL DEFAULT 1"),
            ("trading_profiles", "enable_options",
                "INTEGER NOT NULL DEFAULT 1"),
            # 2026-05-17 — strategy_type: dispatches to a non-AI
            # baseline pipeline when set. 'ai' (default) runs the
            # normal scan-and-trade flow. 'buy_hold_spy' rebalances
            # to 100% SPY weekly. 'random_stock' picks N random
            # symbols from universe each day. Both bypass AI for
            # the experiment-design controls.
            ("trading_profiles", "strategy_type",
                "TEXT NOT NULL DEFAULT 'ai'"),
            # OPEN_ITEMS #10 — options roll-window thresholds, was module
            # constants in options_roll_manager.py. Per-profile lets users
            # who want tighter management (close at 60% max profit) or
            # looser (90%) tune without code changes.
            ("trading_profiles", "options_roll_window_days",
                "INTEGER NOT NULL DEFAULT 7"),
            ("trading_profiles", "options_auto_close_profit_pct",
                "REAL NOT NULL DEFAULT 0.80"),
            ("trading_profiles", "options_roll_recommend_profit_pct",
                "REAL NOT NULL DEFAULT 0.50"),
            # 2026-05-12 — Phase 2b option-tuner WRITE targets. The
            # OptionPipeline.tune() method adjusts these three Greek-
            # budget caps based on option win rate (loosens at >=60%,
            # tightens at <=40%). Defaults match the UserContext
            # dataclass defaults (user_context.py:118-127). Without
            # these as actual columns, the tuner had nothing to write
            # to and per-profile customization was impossible.
            ("trading_profiles", "max_net_options_delta_pct",
                "REAL NOT NULL DEFAULT 0.05"),
            ("trading_profiles", "max_theta_burn_dollars_per_day",
                "REAL NOT NULL DEFAULT 50.0"),
            ("trading_profiles", "max_short_vega_dollars",
                "REAL NOT NULL DEFAULT 500.0"),
            # 2026-05-12 — AI-tunable option exit + veto thresholds.
            # Until this commit these lived as module constants in
            # options_exits.py / option_spread_risk.py. The whole system
            # is built so the AI figures out the right parameters from
            # outcome data; hardcoded constants violated that premise.
            # OptionPipeline.tune() adjusts each based on option win
            # rate with per-param direction (some loosen=raise,
            # some loosen=lower).
            # LONG single-leg exits — trigger when pct_change crosses:
            ("trading_profiles", "option_premium_stop_loss_pct",
                "REAL NOT NULL DEFAULT -0.50"),
            ("trading_profiles", "option_premium_take_profit_pct",
                "REAL NOT NULL DEFAULT 1.00"),
            ("trading_profiles", "option_dte_exit_threshold_days",
                "INTEGER NOT NULL DEFAULT 7"),
            # SHORT single-leg exits (asymmetric — premium-collected):
            ("trading_profiles", "option_short_premium_take_profit_pct",
                "REAL NOT NULL DEFAULT -0.50"),
            ("trading_profiles", "option_short_premium_stop_loss_pct",
                "REAL NOT NULL DEFAULT 1.00"),
            # option_spread_risk specialist VETO thresholds (surfaced
            # in the prompt text, controlling when the LLM is told to
            # veto for IV crush / gamma blowup / credit insufficiency):
            ("trading_profiles", "option_spread_iv_rank_veto_threshold",
                "REAL NOT NULL DEFAULT 80.0"),
            ("trading_profiles", "option_spread_gamma_dte_veto_threshold",
                "INTEGER NOT NULL DEFAULT 7"),
            ("trading_profiles", "option_spread_credit_ratio_veto_threshold",
                "REAL NOT NULL DEFAULT 0.20"),
            # 2026-05-12 — AI-tunable option candidate-gen IV thresholds.
            # See user_context.py for semantics. Defaults close the
            # 10-point dead zone that suppressed option proposals on
            # IV-rank 50-60 candidates.
            ("trading_profiles", "option_iv_rich_threshold",
                "REAL NOT NULL DEFAULT 55.0"),
            ("trading_profiles", "option_iv_cheap_threshold",
                "REAL NOT NULL DEFAULT 55.0"),
            # 2026-05-12 — per-symbol entry blacklist (Wave 8c).
            # See entry_blacklist.py for semantics.
            ("trading_profiles", "entry_blacklist",
                "TEXT NOT NULL DEFAULT '{}'"),
            # Shadow model evaluation — fire N candidate models in
            # parallel with the primary on every AI call. Operational
            # behavior unchanged; results land in ai_shadow_calls and
            # are summarized in a daily email. See ai_providers.py
            # shadow dispatcher + notifications.notify_shadow_eval_daily.
            ("trading_profiles", "enable_shadow_eval",
                "INTEGER NOT NULL DEFAULT 0"),
            ("trading_profiles", "shadow_models",
                "TEXT NOT NULL DEFAULT '[]'"),
            ("trading_profiles", "shadow_api_keys_enc",
                "TEXT NOT NULL DEFAULT '{}'"),
            # --- tuning_history table ---
            # 2026-05-18 — Item 4 of docs/17 Phase 1. Auto-expiry on
            # restrictions. Set when the auto-expire optimizer fires for
            # this tuning event so the rule doesn't re-process it.
            # Default NULL means "not yet expired". Indexed implicitly
            # via the ORDER BY timestamp query.
            ("tuning_history", "expired_at", "TEXT DEFAULT NULL"),
        ]
        for table, col, col_def in _migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                conn.commit()
                logger.info("Migrated: added %s.%s", table, col)
            except sqlite3.OperationalError:
                pass  # Column already exists

        # 2026-05-12 — one-shot data migration: flip
        # use_conviction_tp_override from 0 → 1 on existing profiles.
        # The DEFAULT change above only affects NEW profiles; existing
        # rows keep their old 0. Idempotent via the migration_markers
        # table so it runs exactly once per DB. Profiles that the
        # operator manually flipped back to 0 BEFORE this migration
        # WILL be flipped back to 1 here — that's intentional, the
        # change is a system-wide default reset. Operators can flip
        # back to 0 after this if they want.
        try:
            # The migration_markers table lives in the user DB (same as
            # trading_profiles). Create it inline because this code
            # path is _init_user_db, not the per-profile journal init.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS migration_markers ("
                "key TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "details TEXT)"
            )
            marker = "conviction_tp_default_on_2026_05_12"
            already = conn.execute(
                "SELECT 1 FROM migration_markers WHERE key = ?", (marker,),
            ).fetchone()
            if not already:
                cur = conn.execute(
                    "UPDATE trading_profiles SET use_conviction_tp_override = 1 "
                    "WHERE COALESCE(use_conviction_tp_override, 0) = 0"
                )
                flipped = cur.rowcount
                conn.execute(
                    "INSERT OR REPLACE INTO migration_markers (key, details) "
                    "VALUES (?, ?)",
                    (marker, f"flipped {flipped} profile(s) 0→1"),
                )
                conn.commit()
                logger.info(
                    "Migrated: use_conviction_tp_override flipped 0→1 on %d profile(s)",
                    flipped,
                )
            # 2026-05-12 — flip enable_short_selling 0→1 on stock-trading
            # profiles. Crypto profiles excluded by name (Alpaca crypto
            # doesn't short). Same idempotent-marker pattern as above.
            marker_short = "short_selling_default_on_2026_05_12"
            already_short = conn.execute(
                "SELECT 1 FROM migration_markers WHERE key = ?",
                (marker_short,),
            ).fetchone()
            if not already_short:
                cur = conn.execute(
                    "UPDATE trading_profiles SET enable_short_selling = 1 "
                    "WHERE COALESCE(enable_short_selling, 0) = 0 "
                    "  AND COALESCE(name, '') NOT LIKE '%Crypto%' "
                    "  AND COALESCE(market_type, '') NOT LIKE '%crypto%'"
                )
                flipped_short = cur.rowcount
                conn.execute(
                    "INSERT OR REPLACE INTO migration_markers (key, details) "
                    "VALUES (?, ?)",
                    (marker_short,
                     f"flipped {flipped_short} non-crypto profile(s) 0→1"),
                )
                conn.commit()
                logger.info(
                    "Migrated: enable_short_selling flipped 0→1 on %d profile(s)",
                    flipped_short,
                )
            # 2026-05-12 — bump skip_first_minutes from 0→5 on profiles
            # that left it at the launch default. First 5 minutes after
            # the open has wider spreads + lower-quality fills. Profiles
            # that already set this value (10, 20, 25) are preserved.
            marker_skip = "skip_first_minutes_default_5_2026_05_12"
            already_skip = conn.execute(
                "SELECT 1 FROM migration_markers WHERE key = ?", (marker_skip,),
            ).fetchone()
            if not already_skip:
                cur = conn.execute(
                    "UPDATE trading_profiles SET skip_first_minutes = 5 "
                    "WHERE COALESCE(skip_first_minutes, 0) = 0"
                )
                flipped_skip = cur.rowcount
                conn.execute(
                    "INSERT OR REPLACE INTO migration_markers (key, details) "
                    "VALUES (?, ?)",
                    (marker_skip, f"bumped {flipped_skip} profile(s) 0→5"),
                )
                conn.commit()
                logger.info(
                    "Migrated: skip_first_minutes bumped 0→5 on %d profile(s)",
                    flipped_skip,
                )
            # 2026-05-13 — lower meta_pregate_threshold default from
            # 0.5 → 0.35. Audit found 68% of all candidates filtered
            # before AI evaluation across 139 cycles, suppressing
            # system activity. Only flips profiles still at the
            # historical default 0.5 — profiles the operator already
            # tuned are preserved.
            marker_pregate = "meta_pregate_default_0_35_2026_05_13"
            already_pregate = conn.execute(
                "SELECT 1 FROM migration_markers WHERE key = ?",
                (marker_pregate,),
            ).fetchone()
            if not already_pregate:
                cur = conn.execute(
                    "UPDATE trading_profiles SET meta_pregate_threshold = 0.35 "
                    "WHERE COALESCE(meta_pregate_threshold, 0.5) = 0.5"
                )
                flipped_pregate = cur.rowcount
                conn.execute(
                    "INSERT OR REPLACE INTO migration_markers (key, details) "
                    "VALUES (?, ?)",
                    (marker_pregate,
                     f"lowered {flipped_pregate} profile(s) 0.5→0.35"),
                )
                conn.commit()
                logger.info(
                    "Migrated: meta_pregate_threshold lowered 0.5→0.35 on %d profile(s)",
                    flipped_pregate,
                )
        except Exception as exc:
            logger.warning(
                "Conviction-TP default flip migration failed (non-fatal): %s", exc,
            )

    finally:
        conn.close()
    logger.info("User database initialised.")


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, display_name: str = "",
                is_admin: bool = False, role: str = "admin",
                linked_to_user_id: int = None) -> int:
    """Insert a new user with a bcrypt-hashed password. Returns user_id.

    Roles: 'admin' (full access), 'viewer' (read-only — can see everything
    but cannot change settings, create/delete profiles, or modify keys).

    linked_to_user_id: for viewer accounts, the admin user whose data
    they can see. Viewers with no link see nothing.
    """
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with closing(_get_conn()) as conn:
        cursor = conn.execute(
            """INSERT INTO users (email, password_hash, display_name, is_admin, role, linked_to_user_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (email.lower().strip(), password_hash, display_name, int(is_admin), role, linked_to_user_id),
        )
        conn.commit()
        user_id = cursor.lastrowid
    logger.info("Created user #%d (%s, role=%s, linked_to=%s)", user_id, email, role, linked_to_user_id)
    return user_id


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Return user dict or None."""
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Return user dict or None."""
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


_VALID_SCAN_INTERVAL_MINUTES = (15, 10, 5, 3, 2)
_DEFAULT_SCAN_INTERVAL_MINUTES = 15


def get_scan_interval_minutes(user_id: int = 1) -> int:
    """Return the operator-configured scan cadence in minutes.

    multi_scheduler reads this on every loop iteration so a change in
    the Settings UI takes effect on the next cycle (no restart). The
    column was added 2026-06-04 with default=15 (preserves prior
    behavior); the Settings dropdown restricts to {15, 10, 5, 3, 2}
    (1-min excluded — slowest single-profile scan can exceed 60s,
    causing cycle overlap).

    Defaults to 15 on any read error so a flaky DB doesn't accidentally
    change cadence to something unexpected.
    """
    try:
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT scan_interval_minutes FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row and row[0] and 1 <= int(row[0]) <= 60:
            return int(row[0])
    except Exception:
        # Defensive: never let a flaky DB read change cadence silently.
        pass
    return _DEFAULT_SCAN_INTERVAL_MINUTES


def set_scan_interval_minutes(user_id: int, minutes: int) -> None:
    """Update the operator-configured scan cadence. Enforces the same
    safe range the Settings UI exposes (15/10/5/3/2). Raises
    ValueError on out-of-range input."""
    if int(minutes) not in _VALID_SCAN_INTERVAL_MINUTES:
        raise ValueError(
            f"scan_interval_minutes must be one of "
            f"{_VALID_SCAN_INTERVAL_MINUTES}, got {minutes!r}"
        )
    with closing(_get_conn()) as conn:
        conn.execute(
            "UPDATE users SET scan_interval_minutes = ? WHERE id = ?",
            (int(minutes), user_id),
        )
        conn.commit()


def verify_password(user: Dict[str, Any], password: str) -> bool:
    """Check bcrypt hash against plaintext password."""
    if not user or not user.get("password_hash"):
        return False
    return bcrypt.checkpw(password.encode(), user["password_hash"].encode())


def update_user_credentials(user_id: int, alpaca_key: str = "",
                            alpaca_secret: str = "",
                            llm_key: str = "",
                            llm_provider: Optional[str] = None,
                            llm_model: Optional[str] = None,
                            notification_email: str = "",
                            resend_key: str = "",
                            # 2026-05-19 — `anthropic_key` retained as
                            # alias for `llm_key` so existing callers
                            # don't break. The DB column itself is
                            # still `anthropic_api_key_enc` (the rename
                            # is a future refactor); semantically it
                            # now holds any provider's key per
                            # `llm_provider`.
                            anthropic_key: Optional[str] = None) -> None:
    """Encrypt and store API credentials for a user.

    `llm_key` + `llm_provider` set the user-level "fallback LLM" used
    by CLI tools and helpers that don't have a per-profile context
    (e.g., `main.py ai-analyze`, `news_sentiment.analyze_sentiment`).
    Trading-profile cycles use the per-profile key in
    `trading_profiles.ai_api_key_enc`, not this one.
    """
    # Back-compat alias: callers that still pass `anthropic_key=`
    # see the value applied as the new `llm_key`. If both are passed,
    # `llm_key` wins.
    if not llm_key and anthropic_key is not None:
        llm_key = anthropic_key
    fields = [
        "alpaca_api_key_enc = ?",
        "alpaca_secret_key_enc = ?",
        "anthropic_api_key_enc = ?",
        "notification_email = ?",
        "resend_api_key_enc = ?",
    ]
    params = [
        encrypt(alpaca_key),
        encrypt(alpaca_secret),
        encrypt(llm_key),
        notification_email,
        encrypt(resend_key),
    ]
    if llm_provider is not None:
        # Only touch the provider column when an explicit value is
        # given — preserves the existing value when callers update
        # only the key.
        fields.append("llm_provider = ?")
        params.append(llm_provider)
    if llm_model is not None:
        # 2026-05-21 — same partial-update pattern as llm_provider.
        # An empty string is a legitimate value (meaning "use the
        # provider's default model" — clears the explicit override).
        fields.append("llm_model = ?")
        params.append(llm_model or None)
    params.append(user_id)
    sql = f"UPDATE users SET {', '.join(fields)} WHERE id = ?"
    with closing(_get_conn()) as conn:
        conn.execute(sql, tuple(params))
        conn.commit()
    logger.info("Updated credentials for user #%d", user_id)


def get_user_llm_settings(user_id: int) -> Dict[str, str]:
    """Return the user's fallback LLM provider + (decrypted) key.

    Used by CLI / helper code paths that don't have a per-profile
    context. Returns {"provider": <str>, "api_key": <str>}; both
    fields may be empty when the user hasn't configured one. The
    canonical helper, replacing direct reads of
    `users.anthropic_api_key_enc` (2026-05-19 column-name preserved
    but semantics generalised).
    """
    with closing(_get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT llm_provider, llm_model, anthropic_api_key_enc "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {"provider": "anthropic", "model": None, "api_key": ""}
    return {
        "provider": row["llm_provider"] or "anthropic",
        # 2026-05-21 — same-provider fallback model. None / empty
        # means "use provider's default model" for back-compat.
        "model": row["llm_model"] if "llm_model" in row.keys() else None,
        "api_key": decrypt(row["anthropic_api_key_enc"] or ""),
    }


def is_scanning_active(user_id: int) -> bool:
    """Check if a user's scanning is currently active."""
    with closing(_get_conn()) as conn:
        row = conn.execute("SELECT scanning_active FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row["scanning_active"]) if row else False


def set_scanning_active(user_id: int, active: bool) -> None:
    """Turn scanning on or off for a user."""
    with closing(_get_conn()) as conn:
        conn.execute("UPDATE users SET scanning_active = ? WHERE id = ?", (int(active), user_id))
        conn.commit()
    logger.info("User #%d scanning set to %s", user_id, active)


def get_excluded_symbols(user_id: int) -> List[str]:
    """Return the list of symbols this user is not allowed to trade."""
    with closing(_get_conn()) as conn:
        row = conn.execute("SELECT excluded_symbols FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["excluded_symbols"])
    except (json.JSONDecodeError, TypeError):
        return []


def update_excluded_symbols(user_id: int, symbols: List[str]) -> None:
    """Update the exclusion list for a user."""
    cleaned = sorted(set(s.strip().upper() for s in symbols if s.strip()))
    with closing(_get_conn()) as conn:
        conn.execute(
            "UPDATE users SET excluded_symbols = ? WHERE id = ?",
            (json.dumps(cleaned), user_id),
        )
        conn.commit()
    logger.info("Updated excluded symbols for user #%d: %s", user_id, cleaned)


def is_symbol_excluded(user_id: int, symbol: str) -> bool:
    """Check if a symbol is on the user's exclusion list."""
    excluded = get_excluded_symbols(user_id)
    return symbol.upper() in excluded


def get_active_users() -> List[Dict[str, Any]]:
    """Return list of active user dicts that have Alpaca keys configured."""
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            """SELECT * FROM users
               WHERE is_active = 1
                 AND alpaca_api_key_enc != ''
                 AND alpaca_secret_key_enc != ''"""
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Segment configuration
# ---------------------------------------------------------------------------

def create_default_segment_configs(user_id: int) -> None:
    """Insert default config rows for all market segments.

    Default values are pulled from the segment definitions in segments.py.
    """
    with closing(_get_conn()) as conn:
        for seg_name in ("stocks", "crypto"):
            seg = get_segment(seg_name)
            conn.execute(
                """INSERT OR IGNORE INTO user_segment_configs
                   (user_id, segment, enabled,
                    stop_loss_pct, take_profit_pct, max_position_pct,
                    min_price, max_price, min_volume)
                   VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    seg_name,
                    seg.get("stop_loss_pct", 0.03),
                    seg.get("take_profit_pct", 0.10),
                    seg.get("max_position_pct", 0.10),
                    seg.get("min_price", 1.0),
                    seg.get("max_price", 20.0),
                    seg.get("min_volume", 500_000),
                ),
            )
        conn.commit()
    logger.info("Created default segment configs for user #%d", user_id)


def get_user_segment_config(user_id: int, segment: str) -> Optional[Dict[str, Any]]:
    """Return config dict for a user + segment, or None."""
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM user_segment_configs WHERE user_id = ? AND segment = ?",
            (user_id, segment),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    # Parse the JSON watchlist
    try:
        d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["custom_watchlist"] = []
    return d


def update_user_segment_config(user_id: int, segment: str, **kwargs) -> None:
    """Update specific fields on a user's segment config.

    Only keys that match column names will be applied; unknown keys are
    silently ignored.
    """
    allowed_cols = {
        "enabled", "alpaca_api_key_enc", "alpaca_secret_key_enc",
        "stop_loss_pct", "take_profit_pct", "max_position_pct",
        "max_total_positions", "ai_confidence_threshold",
        "min_price", "max_price", "min_volume", "volume_surge_multiplier",
        "rsi_overbought", "rsi_oversold",
        "momentum_5d_gain", "momentum_20d_gain",
        "breakout_volume_threshold", "gap_pct_threshold",
        "strategy_momentum_breakout", "strategy_volume_spike",
        "strategy_mean_reversion", "strategy_gap_and_go",
        "custom_watchlist",
    }
    updates = {}
    for key, value in kwargs.items():
        if key in allowed_cols:
            # Serialise list values to JSON
            if key == "custom_watchlist" and isinstance(value, list):
                value = json.dumps(value)
            # Store booleans as integers
            if isinstance(value, bool):
                value = int(value)
            updates[key] = value

    if not updates:
        return

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [user_id, segment]

    with closing(_get_conn()) as conn:
        conn.execute(
            f"UPDATE user_segment_configs SET {set_clause} "
            f"WHERE user_id = ? AND segment = ?",
            values,
        )
        conn.commit()
    logger.info("Updated segment config (%s) for user #%d: %s",
                segment, user_id, list(updates.keys()))


# ---------------------------------------------------------------------------
# Trading Profiles
# ---------------------------------------------------------------------------

def asset_classes_label(profile: Dict[str, Any]) -> str:
    """Render the asset classes this profile is configured to trade.

    2026-05-19 — replaces the old `market_type_name` label ("Large
    Cap" / "Mid Cap" / etc.) on dashboard / settings / popup
    surfaces. The within-stock filtering is gone, so the only
    meaningful distinction is which asset classes a profile trades:
    Stocks, Options, Crypto, or a combination. The market_type
    column is preserved in the schema but is no longer surfaced
    in the UI.
    """
    parts = []
    if profile.get("enable_stocks", 1):
        parts.append("Stocks")
    if profile.get("enable_options", 1):
        parts.append("Options")
    if profile.get("enable_crypto", 0):
        parts.append("Crypto")
    return " + ".join(parts) if parts else "(no asset class enabled)"


MARKET_TYPE_NAMES = {
    "stocks": "Stocks",
    "crypto": "Crypto",
}


def create_alpaca_account(user_id: int, name: str,
                          api_key_enc: str, secret_key_enc: str,
                          base_url: str = "https://paper-api.alpaca.markets") -> int:
    """Create a named Alpaca account reference. Returns account id."""
    with closing(_get_conn()) as conn:
        cursor = conn.execute(
            "INSERT INTO alpaca_accounts (user_id, name, alpaca_api_key_enc, "
            "alpaca_secret_key_enc, base_url) VALUES (?,?,?,?,?)",
            (user_id, name, api_key_enc, secret_key_enc, base_url),
        )
        conn.commit()
        aid = cursor.lastrowid
    return aid


def get_alpaca_accounts(user_id: int) -> List[Dict[str, Any]]:
    """Return all Alpaca accounts for a user."""
    with closing(_get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM alpaca_accounts WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_alpaca_account(account_id: int) -> Optional[Dict[str, Any]]:
    """Return a single Alpaca account by id."""
    with closing(_get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM alpaca_accounts WHERE id=?", (account_id,),
        ).fetchone()
    return dict(row) if row else None


def create_trading_profile(user_id: int, name: str, market_type: str) -> int:
    """Create a new trading profile with defaults from segments.py.  Returns profile_id.

    Eagerly initialises the profile's journal DB (writes the schema)
    immediately after the master INSERT commits. Reason: a bare
    `sqlite3.connect(path)` creates a 0-byte file as a side-effect and
    only writes the SQLite header on the first transaction. If the
    process is SIGKILLed during that window, a 0-byte phantom remains
    on disk — exactly the failure that caused the 2026-05-19 outage
    (phantom `quantopsai_profile_25.db` halted the integrity gate and
    locked the scheduler into a 19-restart loop). Calling
    `open_profile_db()` here forces an immediate schema write, so
    by the time this function returns, the journal file either
    exists with a valid SQLite header or doesn't exist at all —
    never the 0-byte limbo.
    """
    seg = get_segment(market_type)
    # Default schedule: crypto gets 24/7, everything else gets market_hours
    default_schedule = "24_7" if market_type == "crypto" else "market_hours"
    with closing(_get_conn()) as conn:
        cursor = conn.execute(
            """INSERT INTO trading_profiles
               (user_id, name, market_type, enabled,
                stop_loss_pct, take_profit_pct, max_position_pct,
                min_price, max_price, min_volume, schedule_type)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                name,
                market_type,
                seg.get("stop_loss_pct", 0.03),
                seg.get("take_profit_pct", 0.10),
                seg.get("max_position_pct", 0.10),
                seg.get("min_price", 1.0),
                seg.get("max_price", 20.0),
                seg.get("min_volume", 500_000),
                default_schedule,
            ),
        )
        conn.commit()
        profile_id = cursor.lastrowid

    # Eager journal initialisation — writes the SQLite header + base
    # schema synchronously. After this returns, the journal file is
    # NEVER 0 bytes; an integrity scan can't mistake it for a phantom.
    # Best-effort: if the write fails, log and continue — the master
    # row is the source of truth, and a subsequent trading cycle will
    # retry the open. The PHANTOM scenario this prevents is the one
    # where the master row is committed but the file never gets a
    # header before some unrelated process is killed.
    try:
        journal_path = f"quantopsai_profile_{profile_id}.db"
        conn = open_profile_db(journal_path)
        try:
            pass  # open is enough to materialize the schema header
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "create_trading_profile: eager journal init failed "
            "for profile %d (%s); the next trading cycle will "
            "retry the journal open",
            profile_id, exc,
        )

    logger.info("Created trading profile #%d (%s/%s) for user #%d",
                profile_id, name, market_type, user_id)
    return profile_id


def get_trading_profile(profile_id: int) -> Optional[Dict[str, Any]]:
    """Return profile dict or None."""
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM trading_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["custom_watchlist"] = []
    # Human-readable label: post-2026-05-19 the displayed concept is
    # asset classes (Stocks / Options / Crypto), not market_type.
    d["market_type_name"] = asset_classes_label(d)
    return d


def get_user_profiles(user_id: int) -> List[Dict[str, Any]]:
    """Return list of all profiles for a user."""
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM trading_profiles WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["custom_watchlist"] = []
        d["market_type_name"] = asset_classes_label(d)
        results.append(d)
    return results


def get_active_profiles(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return all enabled profiles, optionally filtered by user.

    If user_id is None, returns all active profiles across all users (for the
    scheduler).
    """
    with closing(_get_conn()) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM trading_profiles WHERE user_id = ? AND enabled = 1 ORDER BY created_at",
                (user_id,),
            ).fetchall()
        else:
            # Only return profiles for users who have scanning_active = 1
            rows = conn.execute(
                """SELECT tp.* FROM trading_profiles tp
                   JOIN users u ON tp.user_id = u.id
                   WHERE tp.enabled = 1 AND u.scanning_active = 1
                   ORDER BY tp.user_id, tp.created_at"""
            ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["custom_watchlist"] = []
        d["market_type_name"] = asset_classes_label(d)
        results.append(d)
    return results


def get_active_profile_ids(user_id: Optional[int] = None) -> List[int]:
    """Return enabled profile IDs, optionally filtered by user.

    Helper for code that needs to iterate every active profile's
    journal DB — replaces hardcoded `range(1, 12)` (the old 11-profile
    range) so future profile additions are picked up automatically.
    Without this, reconcile/audit cross-profile dedup silently
    excluded all profiles outside the hardcoded range (caught
    2026-05-18: manual cleanup SELLs on profiles 12-14 got duplicated
    by the reconciler because their order_ids weren't in the
    cross-used set).
    """
    return [int(p["id"]) for p in get_active_profiles(user_id=user_id)]


def update_trading_profile(profile_id: int, **kwargs) -> None:
    """Update specific fields on a trading profile.

    Only keys that match column names will be applied; unknown keys are
    silently ignored.
    """
    allowed_cols = {
        "name", "market_type", "enabled",
        "alpaca_api_key_enc", "alpaca_secret_key_enc",
        "stop_loss_pct", "take_profit_pct", "max_position_pct",
        "max_total_positions", "ai_confidence_threshold",
        "min_price", "max_price", "min_volume", "volume_surge_multiplier",
        "rsi_overbought", "rsi_oversold",
        "momentum_5d_gain", "momentum_20d_gain",
        "breakout_volume_threshold", "gap_pct_threshold",
        "strategy_momentum_breakout", "strategy_volume_spike",
        "strategy_mean_reversion", "strategy_gap_and_go",
        "custom_watchlist", "maga_mode", "enable_short_selling",
        "short_stop_loss_pct", "short_take_profit_pct",
        # P1.9b of LONG_SHORT_PLAN.md — tunable short-side sizing
        # and time stop. Without these on the allowlist, the tuner's
        # update_trading_profile calls are silently filtered out and
        # adjustments don't persist (same class of bug as the
        # disabled_specialists/meta_pregate_threshold gap from
        # 2026-04-28).
        "short_max_position_pct", "short_max_hold_days",
        # P2.2 of LONG_SHORT_PLAN.md — long/short balance target.
        "target_short_pct",
        # P4.1 of LONG_SHORT_PLAN.md — book beta target.
        "target_book_beta",
        "enable_self_tuning",
        "ai_provider", "ai_model", "ai_api_key_enc",
        "schedule_type", "custom_start", "custom_end", "custom_days",
        "drawdown_pause_pct", "drawdown_reduce_pct",
        "avoid_earnings_days", "skip_first_minutes",
        "enable_consensus", "consensus_model", "consensus_api_key_enc",
        "use_atr_stops", "atr_multiplier_sl", "atr_multiplier_tp",
        "use_trailing_stops", "trailing_atr_multiplier",
        "use_limit_orders",
        "max_correlation", "max_sector_positions",
        "use_conviction_tp_override", "conviction_tp_min_confidence",
        "conviction_tp_min_adx",
        "is_virtual", "initial_capital", "alpaca_account_id",
        # Lever 2 + Lever 3 of COST_AND_QUALITY_LEVERS_PLAN.md
        # (added 2026-04-28). Without these, the daily
        # _task_specialist_health_check's update_trading_profile
        # call was silently filtered out — health check logged
        # "DISABLE pattern_recognizer" but the column stayed [].
        "disabled_specialists", "meta_pregate_threshold",
        # COMPETITIVE_GAP_PLAN feature toggles. Without these on
        # the allowlist the settings POST silently drops them.
        "enable_intraday_risk_halt",
        "enable_stat_arb_pairs",
        "enable_portfolio_risk_snapshot",
        # 2026-05-17 ablation flags + strategy_type for fresh-start.
        "enable_alt_data",
        "enable_meta_model",
        "enable_options",
        "strategy_type",
        # Item 1c — long-vol hedge toggle + thresholds.
        "enable_long_vol_hedge",
        "long_vol_hedge_drawdown_pct",
        "long_vol_hedge_var_pct",
        "long_vol_hedge_premium_pct",
        # OPEN_ITEMS #4 — wheel automation symbol opt-in list.
        "wheel_symbols",
        # OPEN_ITEMS #10 — options roll-window knobs.
        "options_roll_window_days",
        "options_auto_close_profit_pct",
        "options_roll_recommend_profit_pct",
        # 2026-05-12 — Phase 2b option-tuner WRITE targets.
        "max_net_options_delta_pct",
        "max_theta_burn_dollars_per_day",
        "max_short_vega_dollars",
        # 2026-05-12 — AI-tunable option exit + veto thresholds.
        "option_premium_stop_loss_pct",
        "option_premium_take_profit_pct",
        "option_dte_exit_threshold_days",
        "option_short_premium_take_profit_pct",
        "option_short_premium_stop_loss_pct",
        "option_spread_iv_rank_veto_threshold",
        "option_spread_gamma_dte_veto_threshold",
        "option_spread_credit_ratio_veto_threshold",
        # 2026-05-12 — option candidate-gen IV thresholds.
        "option_iv_rich_threshold",
        "option_iv_cheap_threshold",
        # 2026-05-12 — per-symbol entry blacklist (Wave 8c).
        "entry_blacklist",
        # 2026-05-16 — Layer-2 signal-weight JSON, written by
        # auto_expiry revert path when removing a weight override.
        # Was missing from the allowlist so the update was silently
        # filtered out (same bug class as the disabled_specialists/
        # meta_pregate_threshold gap from 2026-04-28).
        "signal_weights",
        # Shadow model evaluation — toggle + selected candidates +
        # encrypted per-provider API keys. Edited from the settings
        # page and consumed by ai_providers.call_ai shadow dispatch.
        "enable_shadow_eval", "shadow_models", "shadow_api_keys_enc",
        # 2026-05-19 — per-asset-class enablement flags. The settings
        # POST sends these; without them on the allowlist they get
        # silently dropped (same bug class as the 2026-04-28
        # disabled_specialists incident). enable_options already
        # above; adding the other two.
        "enable_stocks", "enable_crypto",
        # 2026-05-19 Scope C of the per-pipeline refactor — per-
        # profile opt-in to read-only A/B of the new
        # Pipeline.run_cycle dispatch path. See
        # pipelines/shadow.py.
        "enable_pipeline_shadow_eval",
        # 2026-05-19 Scope C cutover. When 1, the scheduler uses
        # Pipeline.run_cycle dispatch in place of the legacy
        # run_trade_cycle. Default OFF; flip after shadow soak.
        "use_pipeline_dispatch",
        # 2026-05-19 reconciler safety net — operator-clearable
        # halt flag + structured reason / timestamp. Set by the
        # reconciler on synthesis-action detection, cleared
        # automatically when drift resolves OR via Settings UI.
        "trading_halted", "halt_reason", "halted_at",
    }
    updates = {}
    rejected = []
    for key, value in kwargs.items():
        if key in allowed_cols:
            if key == "custom_watchlist" and isinstance(value, list):
                value = json.dumps(value)
            if isinstance(value, bool):
                value = int(value)
            updates[key] = value
        else:
            rejected.append(key)

    if rejected:
        # Loud log instead of silent swallow. The 2026-04-28
        # disabled_specialists incident hid for hours because the
        # rejected kwargs were dropped quietly — caller couldn't
        # tell its UPDATE didn't apply.
        logger.warning(
            "update_trading_profile(%s) rejected unknown columns: %s. "
            "If these are valid columns, add them to allowed_cols in "
            "models.py:update_trading_profile.",
            profile_id, rejected,
        )

    if not updates:
        return

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [profile_id]

    with closing(_get_conn()) as conn:
        conn.execute(
            f"UPDATE trading_profiles SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
    logger.info("Updated trading profile #%d: %s", profile_id, list(updates.keys()))


def delete_trading_profile(profile_id: int) -> None:
    """Delete a trading profile AND rename its journal file aside.

    Two-step:
      1. DELETE FROM trading_profiles WHERE id=?
      2. Rename `quantopsai_profile_<N>.db` to
         `quantopsai_profile_<N>.db.deleted-<ts>` so the orphan
         doesn't keep matching the `quantopsai_profile_*.db` glob
         used by the integrity gate and dashboard enumerators.

    Rename, not delete: a profile's journal contains trade history
    + AI predictions + cost ledger — too valuable to throw away on
    a UI button click. The renamed file remains on disk for
    forensic review and can be reinstated by renaming it back.

    Renamed files end with `.deleted-<utc-iso>` which is
    grep-friendly (`ls *.deleted-*` lists every parked journal).
    Best-effort: rename failures are logged but do not block the
    master DELETE (the master row is the source of truth)."""
    import os
    from datetime import datetime, timezone
    with closing(_get_conn()) as conn:
        conn.execute("DELETE FROM trading_profiles WHERE id = ?", (profile_id,))
        conn.commit()
    journal_path = f"quantopsai_profile_{profile_id}.db"
    if os.path.exists(journal_path):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        parked = f"{journal_path}.deleted-{ts}"
        try:
            os.rename(journal_path, parked)
            logger.info(
                "Renamed journal %s to %s on profile delete; "
                "preserved for forensic review",
                journal_path, parked,
            )
        except OSError as exc:
            logger.warning(
                "delete_trading_profile: failed to rename %s "
                "(%s); journal file remains in place and may be "
                "picked up by orphan-DB scanners",
                journal_path, exc,
            )
    logger.info("Deleted trading profile #%d", profile_id)


def _parse_wheel_symbols(raw):
    """JSON list or empty when missing/invalid."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(s).upper() for s in raw if s]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(s).upper() for s in parsed if s]
    except (json.JSONDecodeError, TypeError, ValueError) as _js_exc:
        # JSON list-of-symbols parse fallback; returns [] on
        # malformed input. Surface for follow-up.
        logger.debug(
            "models JSON list-of-symbols parse failed: %s: %s",
            type(_js_exc).__name__, _js_exc,
        )
    return []


def build_user_context_from_profile(profile_id: int) -> UserContext:
    """Load profile + user from DB, decrypt credentials, return UserContext.

    Sets ctx.segment to the profile's market_type and ctx.display_name to
    the profile name.  Uses a per-profile db_path for isolated data.
    """
    profile = get_trading_profile(profile_id)
    if profile is None:
        raise ValueError(f"Trading profile #{profile_id} not found")

    user = get_user_by_id(profile["user_id"])
    if user is None:
        raise ValueError(f"User #{profile['user_id']} not found")

    # Resolve Alpaca credentials — priority order:
    # 1. Shared alpaca_account (if alpaca_account_id is set)
    # 2. Per-profile encrypted keys
    # 3. User-level encrypted keys (fallback)
    alpaca_account_id = profile.get("alpaca_account_id")
    if alpaca_account_id:
        acct = get_alpaca_account(alpaca_account_id)
        if acct:
            alpaca_key = decrypt(acct.get("alpaca_api_key_enc", ""))
            alpaca_secret = decrypt(acct.get("alpaca_secret_key_enc", ""))
        else:
            alpaca_key = ""
            alpaca_secret = ""
    else:
        prof_alpaca_key = profile.get("alpaca_api_key_enc", "")
        prof_alpaca_secret = profile.get("alpaca_secret_key_enc", "")
        if prof_alpaca_key:
            alpaca_key = decrypt(prof_alpaca_key)
            alpaca_secret = decrypt(prof_alpaca_secret)
        else:
            alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
            alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))

    # Per-profile isolated DB path
    db_path = f"quantopsai_profile_{profile_id}.db"

    return UserContext(
        user_id=profile["user_id"],
        profile_id=profile_id,
        segment=profile["market_type"],
        display_name=profile["name"],
        alpaca_api_key=alpaca_key,
        alpaca_secret_key=alpaca_secret,
        alpaca_base_url=config.ALPACA_BASE_URL,
        # AI configuration: per-profile provider/model, with key fallback
        ai_provider=profile.get("ai_provider", "anthropic"),
        ai_model=profile.get("ai_model", "claude-haiku-4-5-20251001"),
        ai_api_key=(
            decrypt(profile.get("ai_api_key_enc", ""))
            or decrypt(user.get("anthropic_api_key_enc", ""))
        ),
        db_path=db_path,
        notification_email=user.get("notification_email", ""),
        resend_api_key=decrypt(user.get("resend_api_key_enc", "")),
        # Risk parameters
        stop_loss_pct=profile["stop_loss_pct"],
        take_profit_pct=profile["take_profit_pct"],
        max_position_pct=profile["max_position_pct"],
        max_total_positions=profile["max_total_positions"],
        ai_confidence_threshold=profile["ai_confidence_threshold"],
        # Screener parameters
        min_price=profile["min_price"],
        max_price=profile["max_price"],
        min_volume=profile["min_volume"],
        volume_surge_multiplier=profile["volume_surge_multiplier"],
        # RSI thresholds
        rsi_overbought=profile["rsi_overbought"],
        rsi_oversold=profile["rsi_oversold"],
        # Momentum thresholds
        momentum_5d_gain=profile["momentum_5d_gain"],
        momentum_20d_gain=profile["momentum_20d_gain"],
        # Breakout / gap thresholds
        breakout_volume_threshold=profile["breakout_volume_threshold"],
        gap_pct_threshold=profile["gap_pct_threshold"],
        # Strategy toggles
        strategy_momentum_breakout=bool(profile["strategy_momentum_breakout"]),
        strategy_volume_spike=bool(profile["strategy_volume_spike"]),
        strategy_mean_reversion=bool(profile["strategy_mean_reversion"]),
        strategy_gap_and_go=bool(profile["strategy_gap_and_go"]),
        # Custom watchlist
        custom_watchlist=profile.get("custom_watchlist", []),
        # MAGA Mode
        maga_mode=bool(profile.get("maga_mode", 0)),
        # Short selling
        enable_short_selling=bool(profile.get("enable_short_selling", 0)),
        short_stop_loss_pct=profile.get("short_stop_loss_pct", 0.08),
        short_take_profit_pct=profile.get("short_take_profit_pct", 0.08),
        # P1.9b of LONG_SHORT_PLAN.md — tunable short-side sizing/time stop.
        # short_max_position_pct=None means "derive as max_position_pct / 2"
        # at use-time; explicit float overrides.
        short_max_position_pct=profile.get("short_max_position_pct"),
        short_max_hold_days=int(profile.get("short_max_hold_days", 10) or 10),
        # P2.2 of LONG_SHORT_PLAN.md — long/short balance target.
        # 0.0 = long-only (default), 0.5 = balanced, 1.0 = short-dominant.
        target_short_pct=float(profile.get("target_short_pct", 0.0) or 0.0),
        # P4.1 of LONG_SHORT_PLAN.md — book beta target.
        # None = no target (existing behavior); float = book beta to aim for.
        target_book_beta=(profile.get("target_book_beta")
                          if profile.get("target_book_beta") is not None
                          else None),
        # Self-tuning
        enable_self_tuning=bool(profile.get("enable_self_tuning", 1)),
        # Trading schedule
        schedule_type=profile.get("schedule_type", "market_hours"),
        custom_start=profile.get("custom_start", "09:30"),
        custom_end=profile.get("custom_end", "16:00"),
        custom_days=profile.get("custom_days", "0,1,2,3,4"),
        # Drawdown protection
        drawdown_pause_pct=profile.get("drawdown_pause_pct", 0.20),
        drawdown_reduce_pct=profile.get("drawdown_reduce_pct", 0.10),
        # Earnings calendar
        avoid_earnings_days=profile.get("avoid_earnings_days", 2),
        # Time-of-day patterns
        skip_first_minutes=profile.get("skip_first_minutes", 0),
        # Multi-model consensus
        enable_consensus=bool(profile.get("enable_consensus", 0)),
        consensus_model=profile.get("consensus_model", ""),
        consensus_api_key=decrypt(profile.get("consensus_api_key_enc", "")),
        # Shadow model evaluation toggle (observational only)
        enable_shadow_eval=bool(profile.get("enable_shadow_eval", 0)),
        # ATR-based stops
        use_atr_stops=bool(profile.get("use_atr_stops", 1)),
        atr_multiplier_sl=profile.get("atr_multiplier_sl", 2.0),
        atr_multiplier_tp=profile.get("atr_multiplier_tp", 3.0),
        # Trailing stops
        use_trailing_stops=bool(profile.get("use_trailing_stops", 1)),
        trailing_atr_multiplier=profile.get("trailing_atr_multiplier", 1.5),
        # Limit orders
        use_limit_orders=bool(profile.get("use_limit_orders", 0)),
        # Correlation management
        max_correlation=profile.get("max_correlation", 0.7),
        max_sector_positions=profile.get("max_sector_positions", 5),
        # Conviction-based take-profit override
        use_conviction_tp_override=bool(profile.get("use_conviction_tp_override", 0)),
        conviction_tp_min_confidence=profile.get("conviction_tp_min_confidence", 70.0),
        conviction_tp_min_adx=profile.get("conviction_tp_min_adx", 25.0),
        # Virtual account layer
        is_virtual=bool(profile.get("is_virtual", 0)),
        initial_capital=profile.get("initial_capital", 100000.0),
        # Lever 2 + Lever 3 (COST_AND_QUALITY_LEVERS_PLAN.md).
        # Without these on ctx, ensemble.run_ensemble can't see the
        # disable list / pregate threshold the DB has stored — auto-
        # disable would write to DB but the running scheduler would
        # ignore it. Found via verification 2026-04-28.
        disabled_specialists=profile.get("disabled_specialists", "[]") or "[]",
        meta_pregate_threshold=profile.get("meta_pregate_threshold", 0.5)
        if profile.get("meta_pregate_threshold") is not None else 0.5,
        # Layer storage columns. Same silent-disconnect class as the
        # 2026-04-28 disabled_specialists incident — code reads
        # via getattr(ctx, X, default), so without explicit
        # population each ctx.X returns the dataclass default and
        # the per-profile DB value is ignored.
        signal_weights=profile.get("signal_weights", "{}") or "{}",
        regime_overrides=profile.get("regime_overrides", "{}") or "{}",
        tod_overrides=profile.get("tod_overrides", "{}") or "{}",
        symbol_overrides=profile.get("symbol_overrides", "{}") or "{}",
        prompt_layout=profile.get("prompt_layout", "{}") or "{}",
        # Layer 9 — auto-allocator recommendation
        capital_scale=profile.get("capital_scale", 1.0)
        if profile.get("capital_scale") is not None else 1.0,
        # Multi-account linkage
        alpaca_account_id=profile.get("alpaca_account_id"),
        # AI-model auto-tune toggle
        ai_model_auto_tune=bool(profile.get("ai_model_auto_tune", 0)),
        # Item 2b — intraday risk monitor auto-halt (default ON)
        enable_intraday_risk_halt=bool(
            profile.get("enable_intraday_risk_halt", 1)),
        # Item 1b — stat-arb pair book opt-in (default OFF)
        enable_stat_arb_pairs=bool(
            profile.get("enable_stat_arb_pairs", 0)),
        # Item 2a — Barra portfolio risk daily snapshot (default ON)
        enable_portfolio_risk_snapshot=bool(
            profile.get("enable_portfolio_risk_snapshot", 1)),
        # Item 5c — slippage model uses market_type to scope the K
        # calibration cache.
        market_type=profile.get("market_type"),
        # 2026-05-17 ablation flags (all default ON to preserve
        # current behavior) + strategy_type (default 'ai').
        enable_alt_data=bool(profile.get("enable_alt_data", 1)),
        enable_meta_model=bool(profile.get("enable_meta_model", 1)),
        enable_options=bool(profile.get("enable_options", 1)),
        # 2026-05-19 — per-asset-class flags. Defaults match the
        # new column defaults (stocks=on, crypto=off).
        enable_stocks=bool(profile.get("enable_stocks", 1)),
        enable_crypto=bool(profile.get("enable_crypto", 0)),
        enable_pipeline_shadow_eval=bool(
            profile.get("enable_pipeline_shadow_eval", 0)),
        use_pipeline_dispatch=bool(
            profile.get("use_pipeline_dispatch", 0)),
        strategy_type=profile.get("strategy_type") or "ai",
        # Item 1c — long-vol portfolio tail-risk hedge (default OFF)
        enable_long_vol_hedge=bool(
            profile.get("enable_long_vol_hedge", 0)),
        long_vol_hedge_drawdown_pct=(
            profile.get("long_vol_hedge_drawdown_pct", 0.05)
            if profile.get("long_vol_hedge_drawdown_pct") is not None
            else 0.05),
        long_vol_hedge_var_pct=(
            profile.get("long_vol_hedge_var_pct", 0.03)
            if profile.get("long_vol_hedge_var_pct") is not None
            else 0.03),
        long_vol_hedge_premium_pct=(
            profile.get("long_vol_hedge_premium_pct", 0.01)
            if profile.get("long_vol_hedge_premium_pct") is not None
            else 0.01),
        # OPEN_ITEMS #4 — wheel symbols (JSON list)
        wheel_symbols=_parse_wheel_symbols(profile.get("wheel_symbols")),
        # OPEN_ITEMS #10 — options roll-window knobs
        options_roll_window_days=int(
            profile.get("options_roll_window_days", 7) or 7),
        options_auto_close_profit_pct=float(
            profile.get("options_auto_close_profit_pct", 0.80) or 0.80),
        options_roll_recommend_profit_pct=float(
            profile.get("options_roll_recommend_profit_pct", 0.50) or 0.50),
        # 2026-05-12 — Phase 2b option-Greeks budget caps. Per-profile
        # values; OptionPipeline.tune() can adjust them based on
        # option win rate. Defaults match user_context.py:118-127.
        max_net_options_delta_pct=float(
            profile.get("max_net_options_delta_pct", 0.05)
            if profile.get("max_net_options_delta_pct") is not None
            else 0.05),
        max_theta_burn_dollars_per_day=float(
            profile.get("max_theta_burn_dollars_per_day", 50.0)
            if profile.get("max_theta_burn_dollars_per_day") is not None
            else 50.0),
        max_short_vega_dollars=float(
            profile.get("max_short_vega_dollars", 500.0)
            if profile.get("max_short_vega_dollars") is not None
            else 500.0),
        # 2026-05-12 — AI-tunable option exit + veto thresholds.
        option_premium_stop_loss_pct=float(
            profile.get("option_premium_stop_loss_pct", -0.50)
            if profile.get("option_premium_stop_loss_pct") is not None
            else -0.50),
        option_premium_take_profit_pct=float(
            profile.get("option_premium_take_profit_pct", 1.00)
            if profile.get("option_premium_take_profit_pct") is not None
            else 1.00),
        option_dte_exit_threshold_days=int(
            profile.get("option_dte_exit_threshold_days", 7)
            if profile.get("option_dte_exit_threshold_days") is not None
            else 7),
        option_short_premium_take_profit_pct=float(
            profile.get("option_short_premium_take_profit_pct", -0.50)
            if profile.get("option_short_premium_take_profit_pct") is not None
            else -0.50),
        option_short_premium_stop_loss_pct=float(
            profile.get("option_short_premium_stop_loss_pct", 1.00)
            if profile.get("option_short_premium_stop_loss_pct") is not None
            else 1.00),
        option_spread_iv_rank_veto_threshold=float(
            profile.get("option_spread_iv_rank_veto_threshold", 80.0)
            if profile.get("option_spread_iv_rank_veto_threshold") is not None
            else 80.0),
        option_spread_gamma_dte_veto_threshold=int(
            profile.get("option_spread_gamma_dte_veto_threshold", 7)
            if profile.get("option_spread_gamma_dte_veto_threshold") is not None
            else 7),
        option_spread_credit_ratio_veto_threshold=float(
            profile.get("option_spread_credit_ratio_veto_threshold", 0.20)
            if profile.get("option_spread_credit_ratio_veto_threshold") is not None
            else 0.20),
        # 2026-05-12 — option candidate-gen IV thresholds.
        option_iv_rich_threshold=float(
            profile.get("option_iv_rich_threshold", 55.0)
            if profile.get("option_iv_rich_threshold") is not None
            else 55.0),
        option_iv_cheap_threshold=float(
            profile.get("option_iv_cheap_threshold", 55.0)
            if profile.get("option_iv_cheap_threshold") is not None
            else 55.0),
        entry_blacklist=str(
            profile.get("entry_blacklist", "{}") or "{}"),
    )


# ---------------------------------------------------------------------------
# Build UserContext from DB (legacy segment-based)
# ---------------------------------------------------------------------------

def build_user_context(user_id: int, segment: str) -> UserContext:
    """Load user + segment config from the DB, decrypt credentials,
    and return a fully populated UserContext.
    """
    user = get_user_by_id(user_id)
    if user is None:
        raise ValueError(f"User #{user_id} not found")

    seg_config = get_user_segment_config(user_id, segment)
    if seg_config is None:
        raise ValueError(f"No segment config for user #{user_id}, segment={segment!r}")

    # Use per-segment Alpaca keys if set, otherwise fall back to user-level keys
    seg_alpaca_key = seg_config.get("alpaca_api_key_enc", "")
    seg_alpaca_secret = seg_config.get("alpaca_secret_key_enc", "")
    if seg_alpaca_key:
        alpaca_key = decrypt(seg_alpaca_key)
        alpaca_secret = decrypt(seg_alpaca_secret)
    else:
        alpaca_key = decrypt(user.get("alpaca_api_key_enc", ""))
        alpaca_secret = decrypt(user.get("alpaca_secret_key_enc", ""))

    return UserContext(
        user_id=user_id,
        segment=segment,
        display_name=user.get("display_name", ""),
        alpaca_api_key=alpaca_key,
        alpaca_secret_key=alpaca_secret,
        alpaca_base_url=config.ALPACA_BASE_URL,
        # AI config: legacy path uses Anthropic defaults
        ai_provider="anthropic",
        ai_model=config.CLAUDE_MODEL,
        ai_api_key=decrypt(user.get("anthropic_api_key_enc", "")),
        db_path=config.DB_PATH,
        notification_email=user.get("notification_email", ""),
        resend_api_key=decrypt(user.get("resend_api_key_enc", "")),
        # Risk parameters from segment config
        stop_loss_pct=seg_config["stop_loss_pct"],
        take_profit_pct=seg_config["take_profit_pct"],
        max_position_pct=seg_config["max_position_pct"],
        max_total_positions=seg_config["max_total_positions"],
        ai_confidence_threshold=seg_config["ai_confidence_threshold"],
        # Screener parameters
        min_price=seg_config["min_price"],
        max_price=seg_config["max_price"],
        min_volume=seg_config["min_volume"],
        volume_surge_multiplier=seg_config["volume_surge_multiplier"],
        # RSI thresholds
        rsi_overbought=seg_config["rsi_overbought"],
        rsi_oversold=seg_config["rsi_oversold"],
        # Momentum thresholds
        momentum_5d_gain=seg_config["momentum_5d_gain"],
        momentum_20d_gain=seg_config["momentum_20d_gain"],
        # Breakout / gap thresholds
        breakout_volume_threshold=seg_config["breakout_volume_threshold"],
        gap_pct_threshold=seg_config["gap_pct_threshold"],
        # Strategy toggles
        strategy_momentum_breakout=bool(seg_config["strategy_momentum_breakout"]),
        strategy_volume_spike=bool(seg_config["strategy_volume_spike"]),
        strategy_mean_reversion=bool(seg_config["strategy_mean_reversion"]),
        strategy_gap_and_go=bool(seg_config["strategy_gap_and_go"]),
        # Custom watchlist (already parsed from JSON by get_user_segment_config)
        custom_watchlist=seg_config.get("custom_watchlist", []),
    )


# ---------------------------------------------------------------------------
# API usage tracking
# ---------------------------------------------------------------------------

def increment_api_usage(user_id: int) -> None:
    """Bump the anthropic_calls counter for today (ET)."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    with closing(_get_conn()) as conn:
        conn.execute(
            """INSERT INTO user_api_usage (user_id, date, anthropic_calls)
               VALUES (?, ?, 1)
               ON CONFLICT (user_id, date)
               DO UPDATE SET anthropic_calls = anthropic_calls + 1""",
            (user_id, today),
        )
        conn.commit()


def get_api_usage(user_id: int, date_str: Optional[str] = None) -> int:
    """Get today's Anthropic API call count for a user."""
    if date_str is None:
        from zoneinfo import ZoneInfo
        date_str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT anthropic_calls FROM user_api_usage WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        ).fetchone()
    return row["anthropic_calls"] if row else 0


# ---------------------------------------------------------------------------
# Activity Log
# ---------------------------------------------------------------------------

def log_activity(profile_id: int, user_id: int, activity_type: str,
                 title: str, detail: str, symbol: Optional[str] = None) -> int:
    """Insert an activity log entry. Returns the row id."""
    with closing(_get_conn()) as conn:
        cursor = conn.execute(
            """INSERT INTO activity_log
               (profile_id, user_id, timestamp, activity_type, title, detail, symbol)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                user_id,
                datetime.utcnow().isoformat(),
                activity_type,
                title,
                detail,
                symbol,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
    return row_id


def get_activity_feed(user_id: int, profile_id: Optional[int] = None,
                      limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get activity log entries, newest first.

    If profile_id is None, returns all entries for the user.
    """
    with closing(_get_conn()) as conn:
        if profile_id is not None:
            rows = conn.execute(
                """SELECT a.*, p.name AS profile_name
                   FROM activity_log a
                   LEFT JOIN trading_profiles p ON a.profile_id = p.id
                   WHERE a.user_id = ? AND a.profile_id = ?
                   ORDER BY a.timestamp DESC LIMIT ? OFFSET ?""",
                (user_id, profile_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT a.*, p.name AS profile_name
                   FROM activity_log a
                   LEFT JOIN trading_profiles p ON a.profile_id = p.id
                   WHERE a.user_id = ?
                   ORDER BY a.timestamp DESC LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_activity_count(user_id: int, profile_id: Optional[int] = None) -> int:
    """Total activity log count for pagination."""
    with closing(_get_conn()) as conn:
        if profile_id is not None:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_log WHERE user_id = ? AND profile_id = ?",
                (user_id, profile_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_log WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Tuning History
# ---------------------------------------------------------------------------

def log_tuning_change(profile_id: int, user_id: int, adjustment_type: str,
                      parameter_name: str, old_value: str, new_value: str,
                      reason: str, win_rate_at_change: Optional[float] = None,
                      predictions_resolved: Optional[int] = None) -> int:
    """Insert a tuning history record. Returns the row id."""
    with closing(_get_conn()) as conn:
        cursor = conn.execute(
            """INSERT INTO tuning_history
               (profile_id, user_id, timestamp, adjustment_type, parameter_name,
                old_value, new_value, reason, win_rate_at_change, predictions_resolved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                user_id,
                datetime.utcnow().isoformat(),
                adjustment_type,
                parameter_name,
                str(old_value),
                str(new_value),
                reason,
                win_rate_at_change,
                predictions_resolved,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
    return row_id


def get_tuning_history(profile_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Get recent tuning history for a profile, newest first."""
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            """SELECT * FROM tuning_history
               WHERE profile_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (profile_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Day-1 parameter references — Item 3 of docs/17 Phase 1 (2026-05-18).
# Underpins the reference-window invariant. A reference is the value
# the parameter held the first time the tuner observed it; subsequent
# autonomous changes must stay within ±50% of that value. Prevents the
# slow-cascade scenario where 14 cycles of within-cap tightening
# compounds past safety.
# ---------------------------------------------------------------------------

def get_param_reference(profile_id: int, parameter_name: str) -> Optional[float]:
    """Return the recorded day-1 reference value for (profile, param), or
    None if no reference has been recorded yet OR the stored value
    can't be parsed as a float (corrupt row — fail safe).
    """
    try:
        with closing(_get_conn()) as conn:
            row = conn.execute(
                "SELECT reference_value FROM param_references "
                "WHERE profile_id = ? AND parameter_name = ?",
                (profile_id, parameter_name),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        return float(row["reference_value"])
    except (TypeError, ValueError):
        return None


def record_param_reference_if_absent(profile_id: int, parameter_name: str,
                                      value) -> bool:
    """Record `value` as the reference for (profile, param) ONLY if no
    reference has been recorded yet. Returns True when a new row was
    inserted, False when one already existed.

    Idempotent — safe to call on every `_apply_param_change`. The
    INSERT OR IGNORE matches the (profile_id, parameter_name) PRIMARY
    KEY so re-recording is a no-op rather than an error.
    """
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO param_references "
                "(profile_id, parameter_name, reference_value, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (profile_id, parameter_name, str(value),
                 datetime.utcnow().isoformat()),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False


def clear_param_references(profile_id: int) -> int:
    """Wipe all reference rows for a profile. Used by the reset script
    so a wiped profile starts fresh — first post-reset tuning event
    re-establishes the new reference. Returns count deleted.
    """
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "DELETE FROM param_references WHERE profile_id = ?",
                (profile_id,),
            )
            conn.commit()
            return cur.rowcount
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# Tightening auto-expiry helpers — Item 4 of docs/17 Phase 1 (2026-05-18).
# Used by `_optimize_auto_expire_old_tightenings` in self_tuning.py.
# Excluded outcomes:
#   'improved' — the tightening helped; keep it
#   'worsened' — auto-reversal handles it via the existing reverse path
# Eligible: 'pending', 'unchanged', and anything not in the above.
# ---------------------------------------------------------------------------

def get_expirable_tightenings(profile_id: int, ttl_days: int = 14,
                               limit: int = 1) -> List[Dict[str, Any]]:
    """Return up to `limit` unexpired tuning_history rows older than
    `ttl_days` whose outcome is NOT 'improved'. Oldest first so the
    auto-expire optimizer unwinds the stack from the bottom.

    Each row is returned as a dict so the optimizer doesn't depend on
    sqlite3.Row's column-access semantics.
    """
    try:
        with closing(_get_conn()) as conn:
            rows = conn.execute(
                """SELECT * FROM tuning_history
                   WHERE profile_id = ?
                     AND expired_at IS NULL
                     AND COALESCE(outcome_after, 'pending') != 'improved'
                     AND datetime(timestamp) <= datetime('now', '-' || ? || ' days')
                   ORDER BY timestamp ASC
                   LIMIT ?""",
                (profile_id, ttl_days, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def mark_tuning_event_expired(event_id: int) -> bool:
    """Stamp `expired_at = now` on the given tuning_history row so
    the auto-expire optimizer skips it on future runs. Returns True
    if a row was updated.
    """
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "UPDATE tuning_history SET expired_at = ? "
                "WHERE id = ? AND expired_at IS NULL",
                (datetime.utcnow().isoformat(), event_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False


def review_past_adjustments(profile_id: int,
                            db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Review adjustments made 3+ days ago with at least 10 new predictions since.

    Compares win rate at change time vs current win rate, updates outcome_after.
    Returns list of reviewed adjustment dicts.
    """
    conn = _get_conn()
    reviewed = []

    try:
        # Get pending adjustments made 3+ days ago
        pending = conn.execute(
            """SELECT * FROM tuning_history
               WHERE profile_id = ? AND outcome_after = 'pending'
               AND datetime(timestamp) <= datetime('now', '-3 days')
               ORDER BY timestamp ASC""",
            (profile_id,),
        ).fetchall()

        if not pending:
            conn.close()
            return []

        # Get current win rate from profile's prediction DB
        try:
            with closing(sqlite3.connect(db_path or config.DB_PATH)) as pred_conn:
                pred_conn.row_factory = sqlite3.Row

                # Check ai_predictions table exists
                table_check = pred_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
                ).fetchone()
                if not table_check:
                    conn.close()
                    return []

                # 2026-05-12 fix: previously `total` counted ALL resolved
                # rows (wins + losses + NEUTRALS). Neutrals are timeouts
                # where the AI's directional thesis didn't play out — they
                # diluted the denominator and UNDERSTATED win rate, which
                # made parameter changes look worse than they were and
                # could trigger SPURIOUS rollbacks. The correct win rate
                # is wins / (wins + losses) — neutrals belong in their own
                # tracking but never in the denominator of "did the AI's
                # directional call work out?". Same bug class as the
                # HOLD-attribution gap (2026-05-11).
                decisive = pred_conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' "
                    "AND actual_outcome IN ('win', 'loss')"
                ).fetchone()[0]
                wins = pred_conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE status='resolved' AND actual_outcome='win'"
                ).fetchone()[0]
                current_wr = (wins / decisive * 100) if decisive > 0 else 0.0
                # `total` retained for the "10 new resolved" gate further
                # down — it intentionally counts everything resolved
                # (including neutrals) since that's the cadence signal,
                # not a quality signal.
                total = pred_conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
                ).fetchone()[0]
        except Exception:
            conn.close()
            return []

        now_iso = datetime.utcnow().isoformat()

        for row in pending:
            row = dict(row)
            old_wr = row.get("win_rate_at_change") or 0.0
            old_resolved = row.get("predictions_resolved") or 0

            # Require at least 10 new resolved predictions since the change
            if total - old_resolved < 10:
                continue

            # Determine outcome
            delta = current_wr - old_wr
            if delta > 3.0:
                outcome = "improved"
            elif delta < -3.0:
                outcome = "worsened"
            else:
                outcome = "unchanged"

            conn.execute(
                """UPDATE tuning_history
                   SET outcome_after = ?, win_rate_after = ?, reviewed_at = ?
                   WHERE id = ?""",
                (outcome, current_wr, now_iso, row["id"]),
            )
            row["outcome_after"] = outcome
            row["win_rate_after"] = current_wr
            row["reviewed_at"] = now_iso
            reviewed.append(row)

        conn.commit()
    except Exception as exc:
        logger.warning("Failed to review past adjustments: %s", exc)
    finally:
        conn.close()

    return reviewed


# ---------------------------------------------------------------------------
# Symbol Name Cache
# ---------------------------------------------------------------------------

def get_cached_names(symbols: List[str]) -> Dict[str, str]:
    """Return a dict of {symbol: name} from the cache. Missing symbols omitted."""
    if not symbols:
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with closing(_get_conn()) as conn:
        rows = conn.execute(
            f"SELECT symbol, name FROM symbol_names WHERE symbol IN ({placeholders})",
            symbols,
        ).fetchall()
    return {r["symbol"]: r["name"] for r in rows}


def cache_symbol_names(names: Dict[str, str]) -> None:
    """Upsert symbol names into the cache."""
    if not names:
        return
    with closing(_get_conn()) as conn:
        for sym, name in names.items():
            conn.execute(
                "INSERT OR REPLACE INTO symbol_names (symbol, name, updated_at) VALUES (?, ?, datetime('now'))",
                (sym, name),
            )
        conn.commit()


def fetch_and_cache_names(symbols: List[str]) -> Dict[str, str]:
    """Fetch company names for symbols not already cached, cache them, return all.

    Migrated 2026-05-01 from yfinance.Tickers.info.shortName to
    Alpaca's `/v2/assets/<symbol>` endpoint which exposes a `name`
    field (e.g., "Apple Inc. Common Stock"). Real-time, free with
    our paper-account keys.
    """
    cached = get_cached_names(symbols)
    missing = [s for s in symbols if s not in cached]

    if not missing:
        return cached

    import requests
    # 2026-05-19 — use the alpaca_accounts-backed resolver instead of
    # config.ALPACA_API_KEY (the env-level "master key" path was
    # removed). The resolver returns per-account keys from the DB.
    from market_data import _resolve_alpaca_credentials
    api_key, secret_key, base_url = _resolve_alpaca_credentials()
    if not api_key:
        logger.warning(
            "fetch_and_cache_names: no Alpaca credentials available; "
            "returning cached names only"
        )
        return cached
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    new_names: Dict[str, str] = {}
    base_url = base_url.rstrip("/")
    for sym in missing:
        try:
            r = requests.get(f"{base_url}/v2/assets/{sym}",
                             headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                name = data.get("name") or sym
                new_names[sym] = name
            else:
                new_names[sym] = sym
        except Exception:
            new_names[sym] = sym

    if new_names:
        cache_symbol_names(new_names)

    cached.update(new_names)
    return cached
