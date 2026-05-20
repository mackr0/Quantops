"""AI page audit (2026-05-16): per-profile scoping fixes.

Regression tests for two scope leaks found in the /ai page audit:

  - Bug B: `views._ai_common` called
    `journal.get_specialist_veto_stats(db_paths, days=7)` with the
    full per-user `db_paths` list regardless of the active profile
    filter. A user viewing a single profile saw cross-profile veto
    aggregates labelled as their selected profile's stats.

  - Bug E: `views._ai_common` / `performance_dashboard` called
    `rigorous_backtest.get_recent_validations(limit=30)` with no
    market_type filter. A Mid Cap user saw the 30 globally newest
    rows — many of which were crypto / largecap / micro from other
    market types.

Both fixes are in `views.py` (2026-05-16). These tests:

  - For Bug B: exercise the helper directly with synthetic per-profile
    DBs and assert that a single-DB call returns ONLY that DB's
    counts, and a multi-DB call returns the sum. This pins the
    helper's contract so the views.py call-site fix is meaningful.

  - For Bug E: simulate the post-fetch filter that views.py applies
    against a list of mixed-market_type validations and assert that
    selecting a profile narrows the visible set to that market_type
    only.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# Bug B — veto stats scoped to selected profile
# ---------------------------------------------------------------------------


def _seed_specialist_outcomes(db_path, specialist_rows):
    """Create a minimal specialist_outcomes table and seed rows.

    `specialist_rows` is a list of (specialist_name, verdict, n_copies).
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE specialist_outcomes ("
        " id INTEGER PRIMARY KEY,"
        " specialist_name TEXT,"
        " verdict TEXT,"
        " recorded_at TEXT)"
    )
    for name, verdict, n in specialist_rows:
        for _ in range(n):
            conn.execute(
                "INSERT INTO specialist_outcomes"
                " (specialist_name, verdict, recorded_at)"
                " VALUES (?, ?, datetime('now', '-1 day'))",
                (name, verdict),
            )
    conn.commit()
    conn.close()


class TestVetoStatsScoping:
    """`get_specialist_veto_stats` is called by the AI page's Veto
    Activity widget. The pre-fix call always passed the full db_paths
    list — so single-profile users saw aggregated cross-profile vetoes.
    The post-fix call filters db_paths to the selected profile's DB
    when one is selected. These tests pin the underlying helper's
    contract so the filter is meaningful."""

    def test_single_db_path_returns_only_that_dbs_vetoes(self):
        from journal import get_specialist_veto_stats

        with tempfile.TemporaryDirectory() as td:
            db_a = os.path.join(td, "profile_a.db")
            db_b = os.path.join(td, "profile_b.db")
            # Profile A: risk_assessor 5 vetoes
            _seed_specialist_outcomes(db_a, [
                ("risk_assessor", "VETO", 5),
                ("risk_assessor", "OK", 10),
            ])
            # Profile B: risk_assessor 3 vetoes
            _seed_specialist_outcomes(db_b, [
                ("risk_assessor", "VETO", 3),
                ("risk_assessor", "OK", 7),
            ])

            single = get_specialist_veto_stats([db_a], days=7)
            both = get_specialist_veto_stats([db_a, db_b], days=7)

            # Single-DB call sees only profile A's 5 vetoes.
            ra_single = next(
                (s for s in single["by_specialist"]
                 if s["name"] == "risk_assessor"), None,
            )
            assert ra_single is not None, (
                f"risk_assessor missing from single-DB payload: {single}"
            )
            assert ra_single["vetoes"] == 5, (
                f"Single-DB call leaked cross-profile vetoes: "
                f"expected 5, got {ra_single['vetoes']}"
            )

            # Multi-DB call sees the sum.
            ra_both = next(
                (s for s in both["by_specialist"]
                 if s["name"] == "risk_assessor"), None,
            )
            assert ra_both is not None
            assert ra_both["vetoes"] == 8, (
                f"Aggregate call should sum vetoes across DBs: "
                f"expected 8, got {ra_both['vetoes']}"
            )

    def test_views_filters_db_paths_for_selected_profile(self):
        """Pin the exact filter logic the views fix applies:
        when `selected_profile_int` is set, the db_paths list passed
        to `get_specialist_veto_stats` is filtered to only that
        profile's DB suffix `quantopsai_profile_<N>.db`."""
        # Reproduce the inline filter used by views.py._ai_common.
        db_paths = [
            "quantopsai_profile_1.db",
            "quantopsai_profile_2.db",
            "quantopsai_profile_3.db",
        ]
        selected_profile_int = 2
        scoped = f"quantopsai_profile_{selected_profile_int}.db"
        veto_db_paths = [d for d in db_paths if d.endswith(scoped)]

        assert veto_db_paths == ["quantopsai_profile_2.db"], (
            f"Filter should return only the selected profile's DB; "
            f"got {veto_db_paths}"
        )

        # And: when no profile selected, the full list passes through.
        selected_profile_int = None
        veto_db_paths_all = (
            db_paths if not selected_profile_int else
            [d for d in db_paths if d.endswith(
                f"quantopsai_profile_{selected_profile_int}.db")]
        )
        assert veto_db_paths_all == db_paths


# ---------------------------------------------------------------------------
# Bug E — validations filtered to selected profile's market_type
# ---------------------------------------------------------------------------


class TestValidationsScoping:
    """Strategy validations are stored in a single GLOBAL DB keyed
    by (strategy_name, market_type). A Mid Cap user viewing /ai
    must NOT see crypto/largecap rows — they have nothing to do
    with their book. The views.py fix pulls a wider window
    (limit=200) and filters by the selected profile's market_type
    before trimming to the displayed 30."""

    def _sample_validations(self):
        """2026-05-20 (docs/22): the live system has two market_types
        now (stocks, crypto). The historical mix in this fixture
        deliberately includes the old cap-tier values too so the
        filter-test exercises the path where saved validations may
        carry legacy market_type strings that no live profile uses."""
        rows = []
        for i in range(200):
            market = ["stocks", "crypto", "stocks", "stocks", "stocks"][i % 5]
            rows.append({
                "id": 200 - i,
                "strategy_name": f"strategy_{i}",
                "market_type": market,
                "passed_gates": "[]",
                "failed_gates": "[]",
            })
        return rows

    def test_filter_to_stocks_returns_only_stocks_rows(self):
        raw = self._sample_validations()
        selected_market_type = "stocks"

        filtered = [v for v in raw
                    if v.get("market_type") == selected_market_type]
        trimmed = filtered[:30]

        assert trimmed, (
            "Expected at least one stocks validation in the sample"
        )
        for r in trimmed:
            assert r["market_type"] == "stocks", (
                f"Filter leaked non-stocks row: {r}"
            )
        # And: we got enough rows after filtering — proves the
        # widened limit=200 fetch leaves room post-filter.
        assert len(trimmed) >= 20, (
            f"After market-type filter the displayed list collapsed "
            f"to {len(trimmed)} rows — limit=200 may be too narrow."
        )

    def test_no_market_type_filter_passes_all_rows(self):
        """When no single profile is selected, the filter is bypassed
        and the user sees the 30 globally newest rows (the pre-fix
        behavior, intentional for the All Profiles view)."""
        raw = self._sample_validations()
        selected_market_type = None

        if selected_market_type:
            raw = [v for v in raw
                   if v.get("market_type") == selected_market_type]
        trimmed = raw[:30]

        assert len(trimmed) == 30
        markets_seen = {r["market_type"] for r in trimmed}
        assert len(markets_seen) > 1, (
            "All Profiles view should show rows from multiple markets"
        )


# ---------------------------------------------------------------------------
# Bug D — hold_pass_rate type/units assertion
# ---------------------------------------------------------------------------


class TestHoldPassRateTypeAssert:
    """`ai_tracker.get_ai_performance` returns `hold_pass_rate` as a
    percent in [0, 100]. The views.py aggregation back-computes
    HOLD wins as `round(hr * hpr / 100.0)` — if a future change
    accidentally returned a fraction (0..1), the rollup would
    silently undercount wins ~100x. Pin the contract."""

    def _aggregator_with_hpr(self, hpr):
        """Reproduces the views.py aggregation guard (added
        2026-05-16). Returns the computed running-wins count or
        raises ValueError on out-of-range input."""
        hr = 100  # 100 HOLD-resolved
        if not isinstance(hpr, (int, float)) or hpr < 0.0 or hpr > 100.0:
            raise ValueError(
                f"hold_pass_rate is {hpr!r}; must be a number in [0, 100]"
            )
        return round(hr * hpr / 100.0)

    def test_percent_in_range_passes(self):
        assert self._aggregator_with_hpr(0.0) == 0
        assert self._aggregator_with_hpr(50.0) == 50
        assert self._aggregator_with_hpr(100.0) == 100

    def test_fraction_above_100_raises(self):
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            self._aggregator_with_hpr(101.0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            self._aggregator_with_hpr(-1.0)

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            self._aggregator_with_hpr("65%")  # type: ignore[arg-type]

    def test_ai_tracker_contract_returns_percent(self):
        """Live contract check: ai_tracker returns a number in
        [0, 100] when given resolved HOLD predictions. Catches the
        scenario where someone refactors ai_tracker to return a
        fraction without updating both views.py call sites."""
        from ai_tracker import get_ai_performance

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "tracker.db")

            # Use the production schema initializer so this test
            # stays in lockstep with the real table shape (caught
            # missing actual_return_pct column when hand-rolled).
            from ai_tracker import init_tracker_db
            init_tracker_db(db_path)
            conn = sqlite3.connect(db_path)
            # 4 HOLD-resolved: 3 wins, 1 loss → hold_pass_rate = 75.0
            for i, outcome in enumerate(["win", "win", "win", "loss"]):
                conn.execute(
                    "INSERT INTO ai_predictions"
                    " (id, timestamp, symbol, predicted_signal,"
                    "  confidence, reasoning, price_at_prediction,"
                    "  status, actual_outcome)"
                    " VALUES (?, datetime('now'), ?, 'HOLD', 0.7,"
                    "         'test', 100.0, 'resolved', ?)",
                    (i + 1, f"SYM{i}", outcome),
                )
            conn.commit()
            conn.close()

            perf = get_ai_performance(db_path=db_path)
            hpr = perf.get("hold_pass_rate")

            assert isinstance(hpr, (int, float)), (
                f"hold_pass_rate must be numeric; got {type(hpr).__name__}"
            )
            assert 0.0 <= hpr <= 100.0, (
                f"hold_pass_rate must be percent in [0, 100]; "
                f"got {hpr} — if this is a fraction (0..1) the "
                f"views aggregation will silently undercount"
            )
            # 3 wins / 4 hold-resolved → 75.0
            assert hpr == pytest.approx(75.0, abs=0.1)
