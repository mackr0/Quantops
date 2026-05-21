"""Highest-stakes invariant: the fine-tune dataset builder must never
introduce look-ahead bias (docs/20 §11, §13).

A label derived from data known at or before the decision time would
make the model look good in eval and be useless — or actively
harmful — in live trading. docs/20 rates this the one Critical-impact
risk in the whole phase, so it gets BOTH a behavioral test (in
test_finetune_dataset_builder.py::TestLookAheadGuard) AND this
structural scan.

This file pins, at the source level:
  1. `build_example` calls `assert_no_lookahead` before emitting —
     so the guard can't be bypassed by a future refactor that
     forgets to call it.
  2. The builder never reads a price/bar/quote at or before the
     prediction timestamp to construct a label (it only consumes
     already-resolved outcome columns, which the resolver computed
     forward-in-time).
"""
from __future__ import annotations

import ast
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_BUILDER = os.path.join(REPO, "finetune", "dataset_builder.py")


def _read():
    with open(_BUILDER) as fh:
        return fh.read()


def test_build_example_calls_the_guard():
    """build_example must invoke assert_no_lookahead. Pinned via AST so
    a refactor that drops the call fails here, not silently in prod."""
    tree = ast.parse(_read())
    build_example_fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "build_example"),
        None,
    )
    assert build_example_fn is not None, "build_example function missing"
    calls = {
        n.func.id
        for n in ast.walk(build_example_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "assert_no_lookahead" in calls, (
        "build_example must call assert_no_lookahead before emitting a "
        "training example. Without it, a row whose outcome resolved "
        "at/before the decision time could leak into the corpus — the "
        "one Critical-impact risk in docs/20."
    )


def test_guard_requires_strict_after():
    """The guard's comparison must be STRICT (resolved > pred), not
    >=. An outcome timestamped at the same instant as the decision is
    not provably forward-in-time."""
    src = _read()
    assert "resolved_ts > pred_ts" in src, (
        "assert_no_lookahead must require resolved_at STRICTLY after "
        "the prediction timestamp (resolved_ts > pred_ts). A >= "
        "comparison would admit same-instant rows that aren't "
        "provably post-decision."
    )


def test_builder_does_not_fetch_market_data():
    """The dataset builder must derive labels ONLY from already-resolved
    outcome columns (computed forward-in-time by the resolver), never
    by fetching prices/bars itself — a fetch could pull data from the
    wrong point in time and silently leak. Pin that the builder imports
    no market-data access."""
    src = _read()
    forbidden = ("market_data", "get_bars", "get_snapshot",
                 "alpaca", "yfinance", "list_positions", "get_latest")
    offenders = [tok for tok in forbidden if tok in src]
    assert not offenders, (
        "finetune/dataset_builder.py references market-data access "
        f"{offenders} — labels must come ONLY from the already-resolved "
        "outcome columns (actual_return_pct/_net, actual_outcome) that "
        "the resolver computed forward-in-time. Fetching prices in the "
        "builder risks pulling data from the wrong timestamp = "
        "look-ahead leak."
    )
