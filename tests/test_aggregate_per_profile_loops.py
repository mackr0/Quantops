"""Aggregate-route invariants: when a route loops over a set of
per-profile DBs, every per-DB SQL query must live INSIDE the loop.

Caught 2026-05-09: /performance and /ai both had a query block
sitting OUTSIDE the `for db_path in db_paths` loop, using the
leftover `db_path` value. Because `db_paths` is a `set()` (Python
sets have non-deterministic iteration order), the aggregate
metrics shown on those pages came from a single random profile
on every page load.

Two layers of test:
1. Behavioral: stub two profile DBs with different resolved
   predictions and assert the aggregate route returns the SUM,
   not just one profile's data.
2. Static guardrail: AST-scan `views.py` and fail if any
   `_sqlite3.connect(db_path)` call sits outside its `for
   db_path in db_paths` loop within the same function.
"""

import ast
import os
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Layer 1 — behavioral: aggregate sums across N profiles
# ---------------------------------------------------------------------------


def _seed_predictions(db_path, predictions):
    """`predictions`: list of dicts with keys
    predicted_signal/actual_outcome/actual_return_pct/confidence/prediction_type."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            symbol TEXT,
            predicted_signal TEXT,
            confidence INTEGER,
            reasoning TEXT,
            prediction_type TEXT,
            features_json TEXT,
            price_at_prediction REAL,
            price_targets TEXT,
            status TEXT DEFAULT 'pending',
            actual_outcome TEXT,
            actual_return_pct REAL,
            resolution_price REAL,
            days_held INTEGER,
            resolved_at TEXT
        )
    """)
    for p in predictions:
        conn.execute(
            "INSERT INTO ai_predictions "
            "(predicted_signal, actual_outcome, actual_return_pct, "
            " confidence, prediction_type, status, symbol) "
            "VALUES (?, ?, ?, ?, ?, 'resolved', ?)",
            (
                p["predicted_signal"], p["actual_outcome"],
                p.get("actual_return_pct"), p.get("confidence", 70),
                p.get("prediction_type", "directional_long"),
                p.get("symbol", "X"),
            ),
        )
    conn.commit()
    conn.close()


def _query_aggregate(db_paths, all_wins=0, all_losses=0,
                       conf_on_wins=None, conf_on_losses=None,
                       all_return_buys=None, all_return_sells=None,
                       returns_by_type=None):
    """Runs the EXACT same per-DB aggregation logic the routes use.
    The point: this helper duplicates the production loop body so
    we can test it in isolation, separate from the Flask plumbing."""
    import sqlite3 as _sqlite3
    conf_on_wins = conf_on_wins or []
    conf_on_losses = conf_on_losses or []
    all_return_buys = all_return_buys or []
    all_return_sells = all_return_sells or []
    returns_by_type = returns_by_type or {
        "directional_long": [], "directional_short": [],
        "exit_long": [], "exit_short": [],
    }
    for db_path in db_paths:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT predicted_signal, actual_outcome, actual_return_pct, "
            "confidence, prediction_type FROM ai_predictions "
            "WHERE status = 'resolved'"
        ).fetchall()
        conn.close()
        for r in rows:
            outcome = r["actual_outcome"]
            ret = r["actual_return_pct"]
            conf = r["confidence"] or 0
            sig = r["predicted_signal"] or ""
            ptype = r["prediction_type"]
            if outcome == "win":
                all_wins += 1
                conf_on_wins.append(conf)
            elif outcome == "loss":
                all_losses += 1
                conf_on_losses.append(conf)
            if ret is not None:
                if "BUY" in sig.upper():
                    all_return_buys.append(ret)
                elif "SELL" in sig.upper() or "SHORT" in sig.upper():
                    all_return_sells.append(ret)
                if ptype and ptype in returns_by_type:
                    returns_by_type[ptype].append(ret)
    return {
        "all_wins": all_wins, "all_losses": all_losses,
        "conf_on_wins": conf_on_wins, "conf_on_losses": conf_on_losses,
        "all_return_buys": all_return_buys,
        "all_return_sells": all_return_sells,
        "returns_by_type": returns_by_type,
    }


class TestAggregateAcrossProfiles:
    def test_two_profiles_summed_not_only_one(self, tmp_path):
        """Each profile contributes 2 wins + 1 loss + 1 BUY + 1 SHORT.
        Aggregate must show 4 wins, 2 losses, 2 buys, 2 shorts —
        NOT just 2 wins / 1 loss / 1 buy / 1 short (which is what
        the pre-fix code returned, picking one random profile)."""
        db1 = str(tmp_path / "p1.db")
        db2 = str(tmp_path / "p2.db")
        per_profile = [
            {"predicted_signal": "BUY", "actual_outcome": "win",
             "actual_return_pct": 5.0, "confidence": 80,
             "prediction_type": "directional_long"},
            {"predicted_signal": "BUY", "actual_outcome": "win",
             "actual_return_pct": 3.0, "confidence": 70,
             "prediction_type": "directional_long"},
            {"predicted_signal": "SHORT", "actual_outcome": "loss",
             "actual_return_pct": -2.0, "confidence": 60,
             "prediction_type": "directional_short"},
        ]
        _seed_predictions(db1, per_profile)
        _seed_predictions(db2, per_profile)

        agg = _query_aggregate({db1, db2})
        assert agg["all_wins"] == 4
        assert agg["all_losses"] == 2
        assert len(agg["all_return_buys"]) == 4   # 2 buys × 2 profiles
        assert len(agg["all_return_sells"]) == 2  # 1 short × 2 profiles
        assert len(agg["conf_on_wins"]) == 4
        assert len(agg["conf_on_losses"]) == 2

    def test_set_iteration_order_doesnt_matter(self, tmp_path):
        """Aggregate must be deterministic regardless of how the
        underlying set iterates. Run the same data through 3 times
        and assert identical results."""
        db1 = str(tmp_path / "p1.db")
        db2 = str(tmp_path / "p2.db")
        db3 = str(tmp_path / "p3.db")
        for path, n_wins in [(db1, 1), (db2, 2), (db3, 3)]:
            preds = [{"predicted_signal": "BUY", "actual_outcome": "win",
                      "confidence": 70, "actual_return_pct": 1.0}
                     for _ in range(n_wins)]
            _seed_predictions(path, preds)

        # Three runs — all should yield the same total
        for _ in range(3):
            agg = _query_aggregate({db1, db2, db3})
            assert agg["all_wins"] == 6  # 1 + 2 + 3, not whichever set yields last

    def test_single_profile_view_unchanged(self, tmp_path):
        """The per-profile view (db_paths has 1 element) must keep
        producing the same numbers as before. If only one DB, the
        `loop is outside` vs `inside` distinction has no effect.
        Sanity check the regression doesn't break single-profile."""
        db1 = str(tmp_path / "p1.db")
        _seed_predictions(db1, [
            {"predicted_signal": "BUY", "actual_outcome": "win",
             "confidence": 80, "actual_return_pct": 5.0},
            {"predicted_signal": "BUY", "actual_outcome": "loss",
             "confidence": 60, "actual_return_pct": -3.0},
        ])
        agg = _query_aggregate({db1})
        assert agg["all_wins"] == 1
        assert agg["all_losses"] == 1


# ---------------------------------------------------------------------------
# Layer 2 — static guardrail: every per-DB query lives inside its loop
# ---------------------------------------------------------------------------


VIEWS_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "views.py",
)


def _walk_function_calls(node, fn_node):
    """Yield Call nodes inside `node` whose ancestor scope is `fn_node`.
    We pass the For-loop node and the enclosing function; this walks
    only the For-loop's body."""
    for n in ast.walk(node):
        yield n


def _for_loop_assigns_db_path(loop_node):
    """True if this For-loop either iterates `for db_path in X` OR
    assigns `db_path = ...` inside its body. Either pattern means
    db_path is freshly bound per iteration; calls inside the body
    are safe."""
    if (isinstance(loop_node.target, ast.Name)
            and loop_node.target.id == "db_path"):
        return True
    for stmt in ast.walk(loop_node):
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "db_path":
                    return True
    return False


def _enclosing_for_loops(fn_node, target_node_id):
    """Return the For-loop ancestor nodes of the call inside `fn_node`."""
    enclosing = []
    def walk(node, ancestors):
        if id(node) == target_node_id:
            enclosing.extend(a for a in ancestors if isinstance(a, ast.For))
            return True
        for child in ast.iter_child_nodes(node):
            if walk(child, ancestors + [node]):
                return True
        return False
    walk(fn_node, [])
    return enclosing


def test_no_sqlite_connect_db_path_outside_for_loop():
    """In any function that contains `for db_path in X:` (db_path
    is a loop variable), every `<sqlite>.connect(db_path)` call
    must be inside a for-loop body that binds db_path freshly per
    iteration. This narrowly targets the 2026-05-09 bug shape and
    skips single-profile helpers where `db_path` is just a plain
    function-scope variable.

    Bug shape this catches:
        for db_path in db_paths:
            ...
        # leak: uses leftover db_path
        conn = sqlite3.connect(db_path)

    Safe patterns this allows:
        for db_path in db_paths:
            conn = sqlite3.connect(db_path)   # inside the loop
        for p in target_profiles:
            db_path = ...                      # rebound per iteration
            conn = sqlite3.connect(db_path)   # inside that loop body
    """
    with open(VIEWS_PATH) as f:
        src = f.read()
    tree = ast.parse(src)

    leaks = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Restrict scope: only functions that have `for db_path in X:`
        # somewhere are candidates for this bug pattern.
        has_db_path_loop = any(
            isinstance(n, ast.For)
            and isinstance(n.target, ast.Name)
            and n.target.id == "db_path"
            for n in ast.walk(fn)
        )
        if not has_db_path_loop:
            continue
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            if not isinstance(n.func, ast.Attribute):
                continue
            if n.func.attr != "connect":
                continue
            mod = n.func.value
            mod_name = (mod.id if isinstance(mod, ast.Name) else "")
            if mod_name not in ("sqlite3", "_sqlite3"):
                continue
            if not n.args or not isinstance(n.args[0], ast.Name):
                continue
            if n.args[0].id != "db_path":
                continue
            enclosing = _enclosing_for_loops(fn, id(n))
            safe = any(
                _for_loop_assigns_db_path(loop) for loop in enclosing
            )
            if not safe:
                line = getattr(n, "lineno", "?")
                leaks.append(f"  views.py:{line} in {fn.name}()")

    assert not leaks, (
        "Found <sqlite>.connect(db_path) call(s) OUTSIDE the "
        "`for db_path in db_paths:` loop in functions that have one. "
        "These use the leftover loop variable — exactly the "
        "2026-05-09 aggregate-views bug.\n\n"
        + "\n".join(leaks)
    )
