"""Database models and helpers for multi-user platform.

Uses sqlite3 directly (matching journal.py patterns). All user data,
segment configurations, decision logs, and API usage tracking live in a
single database file.
"""

import sqlite3
import json
import logging
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
    """Get a connection to the user database."""
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_user_db(db_path: Optional[str] = None) -> None:
    """Create all multi-user tables if they do not exist."""
    conn = _get_conn(db_path)
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

        CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            segment TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            technical_score REAL,
            strategy_votes TEXT,
            strategy_reasons TEXT,
            ai_signal TEXT,
            ai_confidence REAL,
            ai_reasoning TEXT,
            ai_risk_factors TEXT,
            ai_price_targets TEXT,
            veto_rule TEXT,
            action_taken TEXT,
            qty REAL,
            price REAL,
            order_id TEXT,
            exit_trigger TEXT,
            pnl REAL,
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
            FOREIGN KEY (profile_id) REFERENCES trading_profiles(id)
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
        # --- user_segment_configs table ---
        ("user_segment_configs", "alpaca_api_key_enc", "TEXT NOT NULL DEFAULT ''"),
        ("user_segment_configs", "alpaca_secret_key_enc", "TEXT NOT NULL DEFAULT ''"),
        # --- trading_profiles table ---
        ("trading_profiles", "maga_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("trading_profiles", "enable_short_selling", "INTEGER NOT NULL DEFAULT 0"),
        ("trading_profiles", "short_stop_loss_pct", "REAL NOT NULL DEFAULT 0.08"),
        ("trading_profiles", "short_take_profit_pct", "REAL NOT NULL DEFAULT 0.08"),
        ("trading_profiles", "enable_self_tuning", "INTEGER NOT NULL DEFAULT 1"),
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
        ("trading_profiles", "skip_first_minutes", "INTEGER NOT NULL DEFAULT 0"),
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
        ("trading_profiles", "use_conviction_tp_override", "INTEGER NOT NULL DEFAULT 0"),
        ("trading_profiles", "conviction_tp_min_confidence", "REAL NOT NULL DEFAULT 70.0"),
        ("trading_profiles", "conviction_tp_min_adx", "REAL NOT NULL DEFAULT 25.0"),
        # Virtual account layer
        ("trading_profiles", "is_virtual", "INTEGER NOT NULL DEFAULT 0"),
        ("trading_profiles", "initial_capital", "REAL NOT NULL DEFAULT 100000.0"),
        ("trading_profiles", "alpaca_account_id", "INTEGER"),
    ]
    for table, col, col_def in _migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            conn.commit()
            logger.info("Migrated: added %s.%s", table, col)
        except sqlite3.OperationalError:
            pass  # Column already exists

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
    conn = _get_conn()
    cursor = conn.execute(
        """INSERT INTO users (email, password_hash, display_name, is_admin, role, linked_to_user_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (email.lower().strip(), password_hash, display_name, int(is_admin), role, linked_to_user_id),
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    logger.info("Created user #%d (%s, role=%s, linked_to=%s)", user_id, email, role, linked_to_user_id)
    return user_id


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Return user dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Return user dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def verify_password(user: Dict[str, Any], password: str) -> bool:
    """Check bcrypt hash against plaintext password."""
    if not user or not user.get("password_hash"):
        return False
    return bcrypt.checkpw(password.encode(), user["password_hash"].encode())


def update_user_credentials(user_id: int, alpaca_key: str = "",
                            alpaca_secret: str = "", anthropic_key: str = "",
                            notification_email: str = "",
                            resend_key: str = "") -> None:
    """Encrypt and store API credentials for a user."""
    conn = _get_conn()
    conn.execute(
        """UPDATE users
           SET alpaca_api_key_enc = ?,
               alpaca_secret_key_enc = ?,
               anthropic_api_key_enc = ?,
               notification_email = ?,
               resend_api_key_enc = ?
           WHERE id = ?""",
        (
            encrypt(alpaca_key),
            encrypt(alpaca_secret),
            encrypt(anthropic_key),
            notification_email,
            encrypt(resend_key),
            user_id,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Updated credentials for user #%d", user_id)


def is_scanning_active(user_id: int) -> bool:
    """Check if a user's scanning is currently active."""
    conn = _get_conn()
    row = conn.execute("SELECT scanning_active FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(row["scanning_active"]) if row else False


def set_scanning_active(user_id: int, active: bool) -> None:
    """Turn scanning on or off for a user."""
    conn = _get_conn()
    conn.execute("UPDATE users SET scanning_active = ? WHERE id = ?", (int(active), user_id))
    conn.commit()
    conn.close()
    logger.info("User #%d scanning set to %s", user_id, active)


def get_excluded_symbols(user_id: int) -> List[str]:
    """Return the list of symbols this user is not allowed to trade."""
    conn = _get_conn()
    row = conn.execute("SELECT excluded_symbols FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return []
    try:
        return json.loads(row["excluded_symbols"])
    except (json.JSONDecodeError, TypeError):
        return []


def update_excluded_symbols(user_id: int, symbols: List[str]) -> None:
    """Update the exclusion list for a user."""
    cleaned = sorted(set(s.strip().upper() for s in symbols if s.strip()))
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET excluded_symbols = ? WHERE id = ?",
        (json.dumps(cleaned), user_id),
    )
    conn.commit()
    conn.close()
    logger.info("Updated excluded symbols for user #%d: %s", user_id, cleaned)


def is_symbol_excluded(user_id: int, symbol: str) -> bool:
    """Check if a symbol is on the user's exclusion list."""
    excluded = get_excluded_symbols(user_id)
    return symbol.upper() in excluded


def get_active_users() -> List[Dict[str, Any]]:
    """Return list of active user dicts that have Alpaca keys configured."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM users
           WHERE is_active = 1
             AND alpaca_api_key_enc != ''
             AND alpaca_secret_key_enc != ''"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Segment configuration
# ---------------------------------------------------------------------------

def create_default_segment_configs(user_id: int) -> None:
    """Insert default config rows for all market segments.

    Default values are pulled from the segment definitions in segments.py.
    """
    conn = _get_conn()
    for seg_name in ("micro", "small", "midcap", "largecap", "crypto"):
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
    conn.close()
    logger.info("Created default segment configs for user #%d", user_id)


def get_user_segment_config(user_id: int, segment: str) -> Optional[Dict[str, Any]]:
    """Return config dict for a user + segment, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM user_segment_configs WHERE user_id = ? AND segment = ?",
        (user_id, segment),
    ).fetchone()
    conn.close()
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

    conn = _get_conn()
    conn.execute(
        f"UPDATE user_segment_configs SET {set_clause} "
        f"WHERE user_id = ? AND segment = ?",
        values,
    )
    conn.commit()
    conn.close()
    logger.info("Updated segment config (%s) for user #%d: %s",
                segment, user_id, list(updates.keys()))


# ---------------------------------------------------------------------------
# Trading Profiles
# ---------------------------------------------------------------------------

MARKET_TYPE_NAMES = {
    "micro": "Micro Cap",
    "small": "Small Cap",
    "midcap": "Mid Cap",
    "largecap": "Large Cap",
    "crypto": "Crypto",
}


def create_alpaca_account(user_id: int, name: str,
                          api_key_enc: str, secret_key_enc: str,
                          base_url: str = "https://paper-api.alpaca.markets") -> int:
    """Create a named Alpaca account reference. Returns account id."""
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO alpaca_accounts (user_id, name, alpaca_api_key_enc, "
        "alpaca_secret_key_enc, base_url) VALUES (?,?,?,?,?)",
        (user_id, name, api_key_enc, secret_key_enc, base_url),
    )
    conn.commit()
    aid = cursor.lastrowid
    conn.close()
    return aid


def get_alpaca_accounts(user_id: int) -> List[Dict[str, Any]]:
    """Return all Alpaca accounts for a user."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM alpaca_accounts WHERE user_id=? ORDER BY id",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_alpaca_account(account_id: int) -> Optional[Dict[str, Any]]:
    """Return a single Alpaca account by id."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM alpaca_accounts WHERE id=?", (account_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_trading_profile(user_id: int, name: str, market_type: str) -> int:
    """Create a new trading profile with defaults from segments.py.  Returns profile_id."""
    seg = get_segment(market_type)
    # Default schedule: crypto gets 24/7, everything else gets market_hours
    default_schedule = "24_7" if market_type == "crypto" else "market_hours"
    conn = _get_conn()
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
    conn.close()
    logger.info("Created trading profile #%d (%s/%s) for user #%d",
                profile_id, name, market_type, user_id)
    return profile_id


def get_trading_profile(profile_id: int) -> Optional[Dict[str, Any]]:
    """Return profile dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM trading_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    d = dict(row)
    try:
        d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["custom_watchlist"] = []
    # Add human-readable market type name
    d["market_type_name"] = MARKET_TYPE_NAMES.get(d["market_type"], d["market_type"])
    return d


def get_user_profiles(user_id: int) -> List[Dict[str, Any]]:
    """Return list of all profiles for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trading_profiles WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["custom_watchlist"] = []
        d["market_type_name"] = MARKET_TYPE_NAMES.get(d["market_type"], d["market_type"])
        results.append(d)
    return results


def get_active_profiles(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return all enabled profiles, optionally filtered by user.

    If user_id is None, returns all active profiles across all users (for the
    scheduler).
    """
    conn = _get_conn()
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
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["custom_watchlist"] = json.loads(d.get("custom_watchlist", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["custom_watchlist"] = []
        d["market_type_name"] = MARKET_TYPE_NAMES.get(d["market_type"], d["market_type"])
        results.append(d)
    return results


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
    }
    updates = {}
    for key, value in kwargs.items():
        if key in allowed_cols:
            if key == "custom_watchlist" and isinstance(value, list):
                value = json.dumps(value)
            if isinstance(value, bool):
                value = int(value)
            updates[key] = value

    if not updates:
        return

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [profile_id]

    conn = _get_conn()
    conn.execute(
        f"UPDATE trading_profiles SET {set_clause} WHERE id = ?",
        values,
    )
    conn.commit()
    conn.close()
    logger.info("Updated trading profile #%d: %s", profile_id, list(updates.keys()))


def delete_trading_profile(profile_id: int) -> None:
    """Delete a trading profile."""
    conn = _get_conn()
    conn.execute("DELETE FROM trading_profiles WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()
    logger.info("Deleted trading profile #%d", profile_id)


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
    )


def migrate_segments_to_profiles(user_id: int) -> List[int]:
    """Read old user_segment_configs rows and create corresponding trading_profiles.

    Returns list of created profile_ids.  Skips segments that already have a
    matching profile (same user + market_type + name).
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM user_segment_configs WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()

    created_ids = []
    existing_profiles = get_user_profiles(user_id)
    existing_names = {(p["market_type"], p["name"]) for p in existing_profiles}

    for row in rows:
        seg = dict(row)
        market_type = seg["segment"]
        profile_name = MARKET_TYPE_NAMES.get(market_type, market_type)

        if (market_type, profile_name) in existing_names:
            logger.info("Skipping migration for user #%d segment %s — profile already exists",
                        user_id, market_type)
            continue

        # Create profile with all saved settings
        conn2 = _get_conn()
        cursor = conn2.execute(
            """INSERT INTO trading_profiles
               (user_id, name, market_type, enabled,
                alpaca_api_key_enc, alpaca_secret_key_enc,
                stop_loss_pct, take_profit_pct, max_position_pct,
                max_total_positions, ai_confidence_threshold,
                min_price, max_price, min_volume, volume_surge_multiplier,
                rsi_overbought, rsi_oversold,
                momentum_5d_gain, momentum_20d_gain,
                breakout_volume_threshold, gap_pct_threshold,
                strategy_momentum_breakout, strategy_volume_spike,
                strategy_mean_reversion, strategy_gap_and_go,
                custom_watchlist)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                profile_name,
                market_type,
                seg.get("enabled", 0),
                seg.get("alpaca_api_key_enc", ""),
                seg.get("alpaca_secret_key_enc", ""),
                seg.get("stop_loss_pct", 0.03),
                seg.get("take_profit_pct", 0.10),
                seg.get("max_position_pct", 0.10),
                seg.get("max_total_positions", 10),
                seg.get("ai_confidence_threshold", 25),
                seg.get("min_price", 1.0),
                seg.get("max_price", 20.0),
                seg.get("min_volume", 500000),
                seg.get("volume_surge_multiplier", 2.0),
                seg.get("rsi_overbought", 85.0),
                seg.get("rsi_oversold", 25.0),
                seg.get("momentum_5d_gain", 3.0),
                seg.get("momentum_20d_gain", 5.0),
                seg.get("breakout_volume_threshold", 1.0),
                seg.get("gap_pct_threshold", 3.0),
                seg.get("strategy_momentum_breakout", 1),
                seg.get("strategy_volume_spike", 1),
                seg.get("strategy_mean_reversion", 1),
                seg.get("strategy_gap_and_go", 1),
                seg.get("custom_watchlist", "[]"),
            ),
        )
        conn2.commit()
        pid = cursor.lastrowid
        conn2.close()
        created_ids.append(pid)
        logger.info("Migrated segment %s to trading profile #%d for user #%d",
                     market_type, pid, user_id)

    return created_ids


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
# Decision log
# ---------------------------------------------------------------------------

def log_decision(user_id: int, segment: str, symbol: str, decision_type: str,
                 technical_score: Optional[float] = None,
                 strategy_votes: Optional[dict] = None,
                 strategy_reasons: Optional[dict] = None,
                 ai_signal: Optional[str] = None,
                 ai_confidence: Optional[float] = None,
                 ai_reasoning: Optional[str] = None,
                 ai_risk_factors: Optional[list] = None,
                 ai_price_targets: Optional[dict] = None,
                 veto_rule: Optional[str] = None,
                 action_taken: Optional[str] = None,
                 qty: Optional[float] = None,
                 price: Optional[float] = None,
                 order_id: Optional[str] = None,
                 exit_trigger: Optional[str] = None,
                 pnl: Optional[float] = None) -> int:
    """Insert a row into the decision_log table. Returns the row id."""
    conn = _get_conn()
    cursor = conn.execute(
        """INSERT INTO decision_log
           (user_id, segment, timestamp, symbol, decision_type,
            technical_score, strategy_votes, strategy_reasons,
            ai_signal, ai_confidence, ai_reasoning,
            ai_risk_factors, ai_price_targets,
            veto_rule, action_taken, qty, price, order_id,
            exit_trigger, pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            segment,
            datetime.utcnow().isoformat(),
            symbol,
            decision_type,
            technical_score,
            json.dumps(strategy_votes) if strategy_votes is not None else None,
            json.dumps(strategy_reasons) if strategy_reasons is not None else None,
            ai_signal,
            ai_confidence,
            ai_reasoning,
            json.dumps(ai_risk_factors) if ai_risk_factors is not None else None,
            json.dumps(ai_price_targets) if ai_price_targets is not None else None,
            veto_rule,
            action_taken,
            qty,
            price,
            order_id,
            exit_trigger,
            pnl,
        ),
    )
    conn.commit()
    decision_id = cursor.lastrowid
    conn.close()
    return decision_id


def get_decisions(user_id: int, segment: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
    """Query decision_log for a user, optionally filtered by segment."""
    conn = _get_conn()
    if segment:
        rows = conn.execute(
            """SELECT * FROM decision_log
               WHERE user_id = ? AND segment = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (user_id, segment, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM decision_log
               WHERE user_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON columns
        for col in ("strategy_votes", "strategy_reasons",
                     "ai_risk_factors", "ai_price_targets"):
            if d.get(col):
                try:
                    d[col] = json.loads(d[col])
                except (json.JSONDecodeError, TypeError):
                    pass
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# API usage tracking
# ---------------------------------------------------------------------------

def increment_api_usage(user_id: int) -> None:
    """Bump the anthropic_calls counter for today."""
    today = date.today().isoformat()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO user_api_usage (user_id, date, anthropic_calls)
           VALUES (?, ?, 1)
           ON CONFLICT (user_id, date)
           DO UPDATE SET anthropic_calls = anthropic_calls + 1""",
        (user_id, today),
    )
    conn.commit()
    conn.close()


def get_api_usage(user_id: int, date_str: Optional[str] = None) -> int:
    """Get today's Anthropic API call count for a user."""
    if date_str is None:
        date_str = date.today().isoformat()
    conn = _get_conn()
    row = conn.execute(
        "SELECT anthropic_calls FROM user_api_usage WHERE user_id = ? AND date = ?",
        (user_id, date_str),
    ).fetchone()
    conn.close()
    return row["anthropic_calls"] if row else 0


# ---------------------------------------------------------------------------
# Activity Log
# ---------------------------------------------------------------------------

def log_activity(profile_id: int, user_id: int, activity_type: str,
                 title: str, detail: str, symbol: Optional[str] = None) -> int:
    """Insert an activity log entry. Returns the row id."""
    conn = _get_conn()
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
    conn.close()
    return row_id


def get_activity_feed(user_id: int, profile_id: Optional[int] = None,
                      limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Get activity log entries, newest first.

    If profile_id is None, returns all entries for the user.
    """
    conn = _get_conn()
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
    conn.close()
    return [dict(r) for r in rows]


def get_activity_count(user_id: int, profile_id: Optional[int] = None) -> int:
    """Total activity log count for pagination."""
    conn = _get_conn()
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
    conn.close()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Tuning History
# ---------------------------------------------------------------------------

def log_tuning_change(profile_id: int, user_id: int, adjustment_type: str,
                      parameter_name: str, old_value: str, new_value: str,
                      reason: str, win_rate_at_change: Optional[float] = None,
                      predictions_resolved: Optional[int] = None) -> int:
    """Insert a tuning history record. Returns the row id."""
    conn = _get_conn()
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
    conn.close()
    return row_id


def get_tuning_history(profile_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Get recent tuning history for a profile, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM tuning_history
           WHERE profile_id = ?
           ORDER BY timestamp DESC LIMIT ?""",
        (profile_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        pred_conn = None
        try:
            pred_conn = sqlite3.connect(db_path or config.DB_PATH)
            pred_conn.row_factory = sqlite3.Row

            # Check ai_predictions table exists
            table_check = pred_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_predictions'"
            ).fetchone()
            if not table_check:
                if pred_conn:
                    pred_conn.close()
                conn.close()
                return []

            total = pred_conn.execute(
                "SELECT COUNT(*) FROM ai_predictions WHERE status='resolved'"
            ).fetchone()[0]
            wins = pred_conn.execute(
                "SELECT COUNT(*) FROM ai_predictions "
                "WHERE status='resolved' AND actual_outcome='win'"
            ).fetchone()[0]
            current_wr = (wins / total * 100) if total > 0 else 0.0
            pred_conn.close()
            pred_conn = None
        except Exception:
            if pred_conn:
                pred_conn.close()
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
    conn = _get_conn()
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"SELECT symbol, name FROM symbol_names WHERE symbol IN ({placeholders})",
        symbols,
    ).fetchall()
    conn.close()
    return {r["symbol"]: r["name"] for r in rows}


def cache_symbol_names(names: Dict[str, str]) -> None:
    """Upsert symbol names into the cache."""
    if not names:
        return
    conn = _get_conn()
    for sym, name in names.items():
        conn.execute(
            "INSERT OR REPLACE INTO symbol_names (symbol, name, updated_at) VALUES (?, ?, datetime('now'))",
            (sym, name),
        )
    conn.commit()
    conn.close()


def fetch_and_cache_names(symbols: List[str]) -> Dict[str, str]:
    """Fetch names from yfinance for symbols not already cached, cache them, return all."""
    cached = get_cached_names(symbols)
    missing = [s for s in symbols if s not in cached]

    if not missing:
        return cached

    # Fetch from yfinance in batches of 20
    import yfinance as yf
    new_names = {}
    for i in range(0, len(missing), 20):
        batch = missing[i:i + 20]
        yf_syms = [s.replace("/", "-") for s in batch]
        try:
            tickers = yf.Tickers(" ".join(yf_syms))
            for orig, yf_sym in zip(batch, yf_syms):
                try:
                    info = tickers.tickers[yf_sym].info
                    name = info.get("shortName") or info.get("longName") or orig
                    new_names[orig] = name
                except Exception:
                    new_names[orig] = orig
        except Exception:
            for s in batch:
                new_names[s] = s

    if new_names:
        cache_symbol_names(new_names)

    cached.update(new_names)
    return cached
