"""Pin profit_factor's inclusion of every traded signal type.

Caught 2026-05-09: profit_factor in /performance and /ai filtered
`predicted_signal IN ('BUY', 'SELL')` — silently excluding SHORT,
MULTILEG_OPEN, and any future trade-emitting signal type. Across 11
prod profiles, 138 of 926 actually-traded predictions (~15%) were
dropped from the headline metric. On options profiles the displayed
profit_factor reflected ~10% of trades.

This test pins:
1. The HOLD-exclusion query includes BUY, SELL, SHORT, MULTILEG_OPEN
   (every prod-real type) AND any future signal type EXCEPT HOLD.
2. HOLDs and NULL signals are still excluded (no trade ⇒ no money
   moved ⇒ no contribution to profit_factor).
3. Hand-computed profit_factor matches the query-driven one.

Plus a cross-cutting AST guardrail: no SQL string in views.py may
filter `predicted_signal IN (` — the closed-set whitelist is the
2026-05-09 bug shape and re-creates whitelist-rot.
"""

import ast
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


# ---------------------------------------------------------------------------
# Layer 1 — behavioral
# ---------------------------------------------------------------------------


# The exact SQL the production routes use post-fix. Test runs the SAME
# string; if a future refactor diverges, this test will need to be
# updated alongside views.py — that's the point.
PROFIT_FACTOR_SQL = (
    "SELECT actual_return_pct FROM ai_predictions "
    "WHERE status='resolved' AND actual_return_pct IS NOT NULL "
    "AND predicted_signal IS NOT NULL "
    "AND UPPER(predicted_signal) != 'HOLD'"
)


def _seed(db_path, rows):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT,
            predicted_signal TEXT,
            confidence INTEGER,
            actual_return_pct REAL,
            status TEXT DEFAULT 'resolved'
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(predicted_signal, actual_return_pct, status, symbol) "
            "VALUES (?, ?, 'resolved', 'X')",
            (r["sig"], r["ret"]),
        )
    conn.commit()
    conn.close()


def _profit_factor(db_path):
    """Replays the production query and computes profit_factor exactly
    as the routes do (so we test the actual SQL)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(PROFIT_FACTOR_SQL).fetchall()
    conn.close()
    returns = [r[0] for r in rows if r[0] is not None]
    gains = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if gains > 0 and losses > 0:
        return round(gains / losses, 2)
    return None


class TestProfitFactorSignalInclusion:
    def test_includes_every_real_prod_signal_type(self, tmp_path):
        """BUY, SELL, SHORT, MULTILEG_OPEN — every signal type
        actually present in production must be included."""
        db = str(tmp_path / "p.db")
        _seed(db, [
            {"sig": "BUY",            "ret":  10.0},  # +10
            {"sig": "SELL",           "ret":   5.0},  # +5
            {"sig": "SHORT",          "ret":   8.0},  # +8 (was excluded)
            {"sig": "MULTILEG_OPEN",  "ret":  12.0},  # +12 (was excluded)
            {"sig": "BUY",            "ret":  -4.0},  # -4
            {"sig": "SHORT",          "ret":  -2.0},  # -2 (was excluded)
            {"sig": "HOLD",           "ret":  20.0},  # excluded (no trade)
            {"sig": "HOLD",           "ret": -50.0},  # excluded (no trade)
        ])
        # gains = 10+5+8+12 = 35; losses = 4+2 = 6; pf = 35/6 ≈ 5.83
        assert _profit_factor(db) == pytest.approx(5.83, abs=0.01)

    def test_excludes_hold_case_insensitive(self, tmp_path):
        """The query uses UPPER(predicted_signal) so 'hold', 'Hold',
        'HOLD' all exclude. Confirms case-insensitivity in case the
        recorder ever writes mixed case."""
        db = str(tmp_path / "p.db")
        _seed(db, [
            {"sig": "BUY",  "ret": 10.0},
            {"sig": "BUY",  "ret": -5.0},
            {"sig": "HOLD", "ret": 999.0},
            {"sig": "hold", "ret": 999.0},
            {"sig": "Hold", "ret": 999.0},
        ])
        # gains 10, losses 5, pf = 2.0
        assert _profit_factor(db) == pytest.approx(2.0)

    def test_excludes_null_signal(self, tmp_path):
        """A NULL signal can't have been a trade; it must be
        excluded so a recorder bug never inflates profit_factor."""
        db = str(tmp_path / "p.db")
        _seed(db, [
            {"sig": "BUY",  "ret": 10.0},
            {"sig": "BUY",  "ret": -5.0},
            {"sig": None,   "ret": 999.0},  # excluded
            {"sig": None,   "ret": -999.0}, # excluded
        ])
        assert _profit_factor(db) == pytest.approx(2.0)

    def test_future_signal_type_included_automatically(self, tmp_path):
        """Pin the open-world property: ANY signal type other than
        HOLD/NULL counts toward profit_factor. This protects against
        regression to a closed-set whitelist if a future contributor
        re-introduces `IN ('BUY', ...)` thinking they're being safe."""
        db = str(tmp_path / "p.db")
        _seed(db, [
            # Hypothetical future signals — must each contribute.
            {"sig": "STRONG_BUY",   "ret":  6.0},
            {"sig": "STRONG_SHORT", "ret":  4.0},
            {"sig": "PAIR_TRADE",   "ret": -3.0},
            {"sig": "EXIT",         "ret":  2.0},
            {"sig": "COVER",        "ret": -1.0},
            {"sig": "HOLD",         "ret": 999.0},  # still excluded
        ])
        # gains 6+4+2 = 12; losses 3+1 = 4; pf = 3.0
        assert _profit_factor(db) == pytest.approx(3.0)

    def test_no_trades_returns_none(self, tmp_path):
        """All HOLDs ⇒ profit_factor not computed (no division by zero,
        no misleading 0.0 displayed). The route's `if total_gains > 0
        and total_losses_abs > 0` guard handles this; this test pins
        that the SQL doesn't return rows that would cause issues."""
        db = str(tmp_path / "p.db")
        _seed(db, [
            {"sig": "HOLD", "ret": 1.0},
            {"sig": "HOLD", "ret": -1.0},
        ])
        assert _profit_factor(db) is None


# ---------------------------------------------------------------------------
# Layer 2 — static guardrail: no IN(...)-whitelist on predicted_signal
# ---------------------------------------------------------------------------


def test_no_predicted_signal_in_whitelist_in_views_py():
    """`predicted_signal IN ('BUY', 'SELL')` style whitelisting is the
    2026-05-09 bug shape — closed-set filters silently drop new signal
    types as they're added (SHORT and MULTILEG_OPEN got dropped this
    way for months). Use HOLD-exclusion instead.

    If a future query LEGITIMATELY needs to target a closed set (e.g.,
    'show me only the BUY trades'), allowlist its surrounding function
    name below — DON'T silently delete this guardrail.
    """
    with open(VIEWS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    ALLOWED_FN_NAMES: set = set()  # currently none

    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in ALLOWED_FN_NAMES:
            continue
        # Concatenate every string literal in the function so we catch
        # SQL split across multiple adjacent string literals.
        sql_blob_parts = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                sql_blob_parts.append(n.value)
        blob = " ".join(sql_blob_parts).upper()
        # Normalize whitespace so "predicted_signal IN(" and
        # "predicted_signal  IN  (" both match.
        blob_compact = " ".join(blob.split())
        if "PREDICTED_SIGNAL IN (" in blob_compact:
            # Get the line number of any matching string literal for
            # the error message.
            line = "?"
            for n in ast.walk(fn):
                if (isinstance(n, ast.Constant)
                        and isinstance(n.value, str)
                        and "predicted_signal" in n.value.lower()
                        and " in " in n.value.lower()
                        and "(" in n.value):
                    line = getattr(n, "lineno", "?")
                    break
            leaks.append(
                f"  views.py:{line} in {fn.name}() — "
                "`predicted_signal IN (...)` whitelist is the "
                "2026-05-09 profit_factor bug shape. Use "
                "`UPPER(predicted_signal) != 'HOLD'` instead."
            )
    assert not leaks, (
        "Found `predicted_signal IN (...)` whitelist(s) in views.py. "
        "These silently drop new signal types as they're added.\n\n"
        + "\n".join(leaks)
    )
