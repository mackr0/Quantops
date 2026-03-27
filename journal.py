"""SQLite trade journal for logging trades, signals, and portfolio snapshots."""

import sqlite3
import json
from datetime import datetime, date

import config


def _get_conn():
    """Get a connection to the journal database."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create journal tables if they don't exist."""
    conn = _get_conn()
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
            pnl REAL
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
    """)
    conn.commit()
    conn.close()


def log_trade(symbol, side, qty, price=None, order_id=None, signal_type=None,
              strategy=None, reason=None, ai_reasoning=None, ai_confidence=None,
              stop_loss=None, take_profit=None, status="open", pnl=None):
    """Log a trade execution to the journal.

    Returns the row id of the inserted trade.
    """
    conn = _get_conn()
    cursor = conn.execute(
        """INSERT INTO trades
           (timestamp, symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit, status, pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.utcnow().isoformat(),
            symbol, side, qty, price, order_id, signal_type, strategy,
            reason, ai_reasoning, ai_confidence, stop_loss, take_profit,
            status, pnl,
        ),
    )
    conn.commit()
    trade_id = cursor.lastrowid
    conn.close()
    return trade_id


def log_signal(symbol, signal, strategy=None, reason=None, price=None,
               indicators=None, acted_on=False):
    """Log a strategy signal to the journal.

    Args:
        indicators: dict of indicator values; stored as JSON.
    Returns the row id.
    """
    conn = _get_conn()
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


def log_daily_snapshot(equity, cash, portfolio_value, num_positions, daily_pnl=None):
    """Log an end-of-day portfolio snapshot.

    Returns the row id.
    """
    conn = _get_conn()
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


def get_trade_history(symbol=None, limit=50):
    """Return recent trades, optionally filtered by symbol.

    Returns a list of dicts.
    """
    conn = _get_conn()
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


def get_performance_summary():
    """Return aggregate performance metrics from the trade journal.

    Returns a dict with total_trades, winning_trades, losing_trades, win_rate,
    total_pnl, avg_pnl, best_trade, worst_trade.
    """
    conn = _get_conn()

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


def get_signal_history(symbol=None, limit=100):
    """Return recent signals, optionally filtered by symbol.

    Returns a list of dicts with indicators parsed from JSON.
    """
    conn = _get_conn()
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


def get_equity_curve(days=30):
    """Return daily equity snapshots for charting.

    Returns a list of dicts with date, equity, portfolio_value, daily_pnl.
    """
    conn = _get_conn()
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
