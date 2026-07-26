"""Slippage calibrator predicate reconnect (P1.3, 2026-07-26).

The calibrator queried `status = 'filled'`, a status the journal never
writes (real lifecycle: pending_fill/pending_protective → open →
closed / canceled / expired). Result: zero rows matched on every
profile, `calibrate_from_history` always returned
`insufficient_history`, and K stayed frozen at the 12.0 default while
realized slippage ran ±850 bps. Three views.py analytics queries
(two round-trip Monte-Carlo joins and the predicted-vs-realized
series) were dead for the same reason.

Pinned here: the calibrator fits from real lifecycle statuses, the
'cover' side is signed as a buy, and no `status='filled'` predicate
survives anywhere in the codebase.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _seed_db(rows):
    """Temp profile DB with the given (side, status, dp, fp) trades."""
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    conn = sqlite3.connect(path)
    for side, status, dp, fp in rows:
        conn.execute(
            "INSERT INTO trades (symbol, qty, side, status, price, "
            "decision_price, fill_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("SYM", 100, side, status, dp, dp, fp),
        )
    conn.commit()
    conn.close()
    return path


class TestPredicate:
    def test_fits_from_real_lifecycle_statuses(self):
        """30 closed + 5 open rows must all reach the fitter; the old
        `status='filled'` predicate saw zero of them."""
        from slippage_model import calibrate_from_history
        rows = [("buy", "closed", 50.0, 50.05)] * 30
        rows += [("buy", "open", 50.0, 50.05)] * 5
        # Never-filled lifecycle states must stay excluded even if a
        # future writer stamps prices on them.
        rows += [("sell", "canceled", 50.0, 50.05)] * 5
        rows += [("buy", "pending_fill", 50.0, 50.05)] * 5
        rows += [("sell", "expired", 50.0, 50.05)] * 5
        path = _seed_db(rows)
        try:
            fit = calibrate_from_history(path, market_type="p13test")
            assert fit["source"] == "fit", (
                "calibrator found no rows in a DB full of real filled "
                "trades — the dead status predicate is back."
            )
            assert fit["n_samples"] == 35
        finally:
            os.unlink(path)
            cache = os.path.join(
                ".cache/slippage_calibration", "k_p13test.json")
            if os.path.exists(cache):
                os.unlink(cache)

    def test_cover_side_signed_as_buy(self):
        """'cover' buys back a short: adverse = paying MORE than the
        decision price. Under the old sell-branch signing, this fill
        (fp > dp) would read as negative slippage and the single-sample
        K would clamp to the 1.0 floor."""
        from slippage_model import calibrate_from_history
        path = _seed_db([("cover", "closed", 50.0, 50.50)])  # +100 bps
        try:
            fit = calibrate_from_history(
                path, market_type="p13cover", min_trades=1)
            assert fit["source"] == "fit"
            assert fit["K_bps"] > 1.0, (
                "a cover fill above decision price fitted as favorable "
                "slippage — 'cover' fell into the sell branch again."
            )
        finally:
            os.unlink(path)
            cache = os.path.join(
                ".cache/slippage_calibration", "k_p13cover.json")
            if os.path.exists(cache):
                os.unlink(cache)


class TestNoDeadFilledPredicates:
    # 'filled' may appear in comments/docs; what must never appear is a
    # SQL predicate comparing status to it.
    _PREDICATE = re.compile(r"status\s*=\s*'filled'")

    @pytest.mark.parametrize("fname", sorted(
        f for f in os.listdir(REPO)
        if f.endswith(".py") and os.path.isfile(os.path.join(REPO, f))
    ))
    def test_no_status_equals_filled(self, fname):
        src = open(os.path.join(REPO, fname), encoding="utf-8",
                   errors="replace").read()
        hits = [
            i + 1 for i, line in enumerate(src.splitlines())
            if self._PREDICATE.search(line)
        ]
        assert not hits, (
            f"{fname}:{hits} filters on status='filled', which the "
            "journal never writes — that query matches zero rows "
            "forever (the P1.3 frozen-K bug class)."
        )


class TestViewsQueriesUseRealStatuses:
    def test_round_trip_joins_require_closed_legs(self):
        src = open(os.path.join(REPO, "views.py")).read()
        assert src.count(
            "t1.status='closed' AND t2.status='closed'") == 2, (
            "the two Monte-Carlo round-trip joins must pair CLOSED "
            "entry/exit legs — any other status predicate either "
            "matches nothing or pairs still-open lots."
        )

    def test_predicted_vs_realized_series_matches_fills(self):
        src = open(os.path.join(REPO, "views.py")).read()
        assert "AND status IN ('open', 'closed')" in src, (
            "the predicted-vs-realized slippage series lost its "
            "real-fill status whitelist."
        )
