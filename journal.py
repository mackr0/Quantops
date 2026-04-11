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
            date TEXT NOT NULL,
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
            resolution_price REAL
        );
    """)

    # Auto-migration: add slippage columns to existing databases
    _migrate_slippage_columns(conn)

    conn.commit()
    conn.close()


def _migrate_slippage_columns(conn):
    """Add decision_price, fill_price, slippage_pct columns if missing."""
    try:
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        for col, col_type in [
            ("decision_price", "REAL"),
            ("fill_price", "REAL"),
            ("slippage_pct", "REAL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
    except Exception:
        pass  # Table may not exist yet (first run)


def log_trade(symbol, side, qty, price=None, order_id=None, signal_type=None,
              strategy=None, reason=None, ai_reasoning=None, ai_confidence=None,
              stop_loss=None, take_profit=None, status="open", pnl=None,
              decision_price=None, fill_price=None, slippage_pct=None,
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

    Returns the row id of the inserted trade.
    """
    conn = _get_conn(db_path)
    cursor = conn.execute(
        """INSERT INTO trades
           (timestamp, symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit, status, pnl,
            decision_price, fill_price, slippage_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit,
            status, pnl, decision_price, fill_price, slippage_pct,
        ),
    )
    conn.commit()
    trade_id = cursor.lastrowid
    conn.close()
    return trade_id


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

    Returns the row id.
    """
    conn = _get_conn(db_path)
    cursor = conn.execute(
        """INSERT INTO daily_snapshots
           (date, equity, cash, portfolio_value, num_positions, daily_pnl)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            date.today().isoformat(),
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


def get_slippage_stats(db_path=None):
    """Return slippage statistics from trades that have fill_price data.

    Returns a dict with avg_slippage_pct, total_slippage_cost, worst_slippage,
    trades_with_fills, or None if no fill data is available.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT
                COUNT(*) AS trades_with_fills,
                AVG(slippage_pct) AS avg_slippage_pct,
                MAX(ABS(slippage_pct)) AS worst_slippage_pct,
                SUM(ABS(fill_price - decision_price) * qty) AS total_slippage_cost
            FROM trades
            WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
              AND decision_price > 0
        """).fetchone()

        if not row or row["trades_with_fills"] == 0:
            conn.close()
            return None

        # Get the worst slippage trade details
        worst = conn.execute("""
            SELECT symbol, side, qty, decision_price, fill_price, slippage_pct, timestamp
            FROM trades
            WHERE fill_price IS NOT NULL AND decision_price IS NOT NULL
              AND decision_price > 0
            ORDER BY ABS(slippage_pct) DESC
            LIMIT 1
        """).fetchone()

        conn.close()
        return {
            "trades_with_fills": row["trades_with_fills"],
            "avg_slippage_pct": round(row["avg_slippage_pct"] or 0, 4),
            "worst_slippage_pct": round(row["worst_slippage_pct"] or 0, 4),
            "total_slippage_cost": round(row["total_slippage_cost"] or 0, 2),
            "worst_trade": dict(worst) if worst else None,
        }
    except Exception:
        conn.close()
        return None
