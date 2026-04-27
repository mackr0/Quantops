"""Guardrail: the meta-model must use a time-ordered train/test
split, NEVER a random one.

History: on 2026-04-27 we discovered that meta_model.train_meta_model
was using sklearn's `train_test_split(..., random_state=42)`. That
produces a RANDOM 80/20 split, where test predictions are interleaved
in time with training predictions. Because financial features are
heavily autocorrelated day-to-day (RSI today ≈ RSI tomorrow, regime
today ≈ regime tomorrow), the model effectively memorized "this
regime → this outcome" instead of learning a predictive pattern.

Result: every profile reported AUC 0.83-0.96, when realistic
out-of-sample financial AUCs are ~0.55. The numbers were a leakage
artifact, not real edge.

The fix: build_training_set returns rows in ascending time order
(`ORDER BY id ASC`); train_meta_model takes the LAST 20% as test.
This guarantees the model is evaluated only on predictions made
AFTER its training horizon, which is the only honest measurement
of "is the AI's confidence calibrated to truth?"

These tests prevent any future regression that re-introduces a
random split.
"""

from __future__ import annotations

import inspect
import re

import meta_model


# ---------------------------------------------------------------------------
# Source-level guardrails — kill the regression at compile time
# ---------------------------------------------------------------------------

def test_train_meta_model_does_not_import_train_test_split():
    """sklearn.train_test_split shuffles by default and is always a
    risk for time-series data. The meta-model must not import it."""
    src = inspect.getsource(meta_model.train_meta_model)
    assert "train_test_split" not in src, (
        "REGRESSION: meta_model.train_meta_model now references "
        "sklearn.model_selection.train_test_split. That function "
        "produces a RANDOM split unless explicitly time-aware, and "
        "for financial autocorrelated features it leaks future data "
        "into the training set, inflating AUC. Use a deterministic "
        "time-ordered split: X_train = X[:n_train], X_test = X[n_train:]."
    )


def test_build_training_set_orders_by_id_asc():
    """The split is only time-ordered if the rows arrive in time order.
    `ORDER BY id ASC` (or timestamp ASC) is the contract."""
    src = inspect.getsource(meta_model.build_training_set)
    # Match either ORDER BY id ASC or ORDER BY timestamp ASC, case-insensitive
    pattern = re.compile(r"ORDER\s+BY\s+(id|timestamp)\s+ASC", re.IGNORECASE)
    assert pattern.search(src), (
        "REGRESSION: build_training_set must include `ORDER BY id ASC` "
        "(or `ORDER BY timestamp ASC`) so resolved predictions come "
        "back in ascending time order. Without this guarantee, the "
        "downstream time-ordered split is meaningless — SQLite's row "
        "order is implementation-defined."
    )


def test_train_meta_model_uses_deterministic_tail_split():
    """The split must be `X[n_train:]` (last 20% as test), not any
    randomized variant."""
    src = inspect.getsource(meta_model.train_meta_model)
    # Look for the slice-style split — at least one of the canonical
    # patterns we use.
    has_slice_split = (
        "X[:n_train]" in src or "X[n_train:]" in src
        or "X_train, X_test = X[:" in src
    )
    assert has_slice_split, (
        "train_meta_model must split with a deterministic slice "
        "(X[:n_train] / X[n_train:]) so the test set is always the "
        "most-recent fraction of the data. This is the only honest "
        "out-of-sample measurement for time-series predictions."
    )


# ---------------------------------------------------------------------------
# Behavioral guardrail — feed time-ordered data, verify the split honors it
# ---------------------------------------------------------------------------

def test_split_takes_most_recent_data_as_test_set():
    """End-to-end: if we feed train_meta_model 100 samples in time
    order where the LAST 20 are deliberately mislabeled, the AUC on
    the held-out test set should be poor (because test data
    contradicts what the model learned). A random split would
    interleave the mislabels and look much better — that's the
    leakage signature we're guarding against."""
    # Build a feature pattern: feature[0] strongly predicts class on
    # samples 0-79, but the relationship is INVERTED on samples 80-99.
    # A time-ordered split holds out the inverted samples and should
    # see degraded AUC (~0.0-0.5). A random split would interleave
    # them and AUC would still look good.
    X = []
    y = []
    feature_names = ["x1", "x2"]
    # Train half: x1 high → win, x1 low → loss
    for i in range(80):
        if i % 2 == 0:
            X.append([1.0, 0.0])
            y.append(1)
        else:
            X.append([0.0, 0.0])
            y.append(0)
    # Test half (most recent): inverted relationship
    for i in range(20):
        if i % 2 == 0:
            X.append([1.0, 0.0])
            y.append(0)  # x1 high → loss now
        else:
            X.append([0.0, 0.0])
            y.append(1)  # x1 low → win now

    bundle = meta_model.train_meta_model(X, y, feature_names)
    auc = bundle["metrics"]["auc"]
    n_train = bundle["metrics"]["n_train"]
    n_test = bundle["metrics"]["n_test"]

    # Time-ordered split must have given us 80 train / 20 test
    assert n_train == 80, f"Expected n_train=80 (time-ordered), got {n_train}"
    assert n_test == 20, f"Expected n_test=20 (time-ordered), got {n_test}"

    # And because the test half inverts the training pattern, AUC
    # should be poor (≤ 0.5 — random or worse). If it's high, the
    # split was random and leaked the inverted samples into training.
    assert auc <= 0.5, (
        f"AUC={auc} on inverted-test data is too high — implies the "
        f"split is interleaving test samples into training (leakage). "
        f"A correct time-ordered split holds out the inverted half "
        f"entirely and should produce AUC ≤ 0.5."
    )
