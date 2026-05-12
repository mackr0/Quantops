"""Pipeline-kind classification completeness (2026-05-11).

CLASS INVARIANT: every signal type that has ever appeared in
production AI predictions must classify into either 'stock' or
'option' via `pipelines.outcomes.kind_from_signal`. NULLs after
backfill mean ~5% of stock data was being silently dropped from
calibration / tuning — exactly the cherry-picking pattern the
pipeline_kind tag was supposed to eliminate.

This catches the 2026-05-11 regression where HOLD predictions
(17,111 of 18,318 = 93% of all production rows) were
accidentally excluded from STOCK_SIGNAL_TYPES, leaving them
NULL after backfill and starving stock specialist calibration of
its dominant data class.

Pin both:
1. The HARD-CODED list of every signal type that has appeared in
   production at any point — sourced from the audit query
   `SELECT DISTINCT predicted_signal FROM ai_predictions` run
   2026-05-11. Adding new signal types REQUIRES updating this
   list AND assigning them to a pipeline.
2. CONSISTENCY between the four definition sites:
   - `journal.py` backfill stock_signals + option_signals
   - `pipelines.outcomes.kind_from_signal`
   - `tuning.stock.STOCK_SIGNAL_TYPES`
   - `tuning.option.OPTION_SIGNAL_TYPES`
   All four must agree on which signals belong to which pipeline.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from pipelines.outcomes import kind_from_signal


# ---------------------------------------------------------------------------
# Production-observed signal types (2026-05-11 audit)
# ---------------------------------------------------------------------------

# Sourced from prod query:
#   SELECT DISTINCT predicted_signal FROM ai_predictions
# across all 11 profile DBs on 2026-05-11.
# When a new signal type lands, add it here AND classify it into
# kind_from_signal — this test fails until both happen.
PRODUCTION_OBSERVED_SIGNALS = (
    "HOLD",
    "BUY",
    "MULTILEG_OPEN",
    "SHORT",
    "SELL",
)

# Plus signals defined in the codebase but not yet seen in prod
# (still must classify cleanly when they do appear).
DEFINED_BUT_RARE_SIGNALS = (
    "STRONG_BUY", "WEAK_BUY",
    "STRONG_SELL", "WEAK_SELL",
    "COVER",
    "OPTIONS", "OPTION_EXERCISE",
)


# ---------------------------------------------------------------------------
# CLASS INVARIANT — every observed signal classifies
# ---------------------------------------------------------------------------

class TestEveryProductionSignalClassifies:
    """Pin the structural guarantee: no production signal type
    silently falls through to None. Catches the cherry-pick bug
    by making it impossible to land a new signal that doesn't
    have an explicit pipeline assignment."""

    @pytest.mark.parametrize("signal", PRODUCTION_OBSERVED_SIGNALS)
    def test_observed_signal_classifies_into_a_pipeline(self, signal):
        kind = kind_from_signal(signal)
        assert kind in ("stock", "option"), (
            f"Production signal {signal!r} classifies as {kind!r}. "
            f"This means rows of this signal type sit unattributed "
            f"in pipeline_kind=NULL after backfill, getting "
            f"silently excluded from per-pipeline calibration / "
            f"tuning. Add {signal!r} to the appropriate signal set "
            f"in pipelines/outcomes/__init__.py:kind_from_signal "
            f"AND journal.py backfill AND tuning/{{stock,option}}.py."
        )

    @pytest.mark.parametrize("signal", DEFINED_BUT_RARE_SIGNALS)
    def test_defined_signal_classifies_into_a_pipeline(self, signal):
        kind = kind_from_signal(signal)
        assert kind in ("stock", "option")


# ---------------------------------------------------------------------------
# CONSISTENCY — four definition sites must agree
# ---------------------------------------------------------------------------

class TestSignalListConsistency:
    """The same signal set is defined in four places (for SQL
    performance reasons — can't import a Python tuple into a SQL
    backfill running inside journal.init_db). Pin that they agree.
    A new signal added to one place must be added to all four."""

    def _journal_backfill_lists(self):
        """Extract the stock_signals and option_signals tuples
        from journal.py:_migrate_all_columns by import-and-eval —
        the lists live as inline tuples in the function so we have
        to invoke a tiny helper."""
        # Read journal.py source and evaluate the literal tuples.
        # Brittle to refactoring but cheap to keep in sync.
        import re
        path = os.path.join(
            os.path.dirname(__file__), os.pardir, "journal.py",
        )
        with open(path) as fh:
            src = fh.read()
        # Find both tuple literals in the backfill block.
        m_stock = re.search(
            r"stock_signals\s*=\s*\(([^)]+)\)", src,
        )
        m_option = re.search(
            r"option_signals\s*=\s*\(([^)]+)\)", src,
        )
        assert m_stock, "Couldn't locate stock_signals in journal.py"
        assert m_option, "Couldn't locate option_signals in journal.py"
        stock = tuple(
            s.strip().strip("'\"")
            for s in m_stock.group(1).split(",")
            if s.strip()
        )
        option = tuple(
            s.strip().strip("'\"")
            for s in m_option.group(1).split(",")
            if s.strip()
        )
        return stock, option

    def test_journal_backfill_matches_kind_from_signal(self):
        stock, option = self._journal_backfill_lists()
        for sig in stock:
            assert kind_from_signal(sig) == "stock", (
                f"journal.py backfill tags {sig!r} as 'stock' but "
                f"kind_from_signal says {kind_from_signal(sig)!r}"
            )
        for sig in option:
            assert kind_from_signal(sig) == "option", (
                f"journal.py backfill tags {sig!r} as 'option' but "
                f"kind_from_signal says {kind_from_signal(sig)!r}"
            )

    def test_tuning_stock_matches_kind_from_signal(self):
        from tuning.stock import STOCK_SIGNAL_TYPES
        for sig in STOCK_SIGNAL_TYPES:
            assert kind_from_signal(sig) == "stock", (
                f"tuning/stock.py STOCK_SIGNAL_TYPES includes {sig!r} "
                f"but kind_from_signal says {kind_from_signal(sig)!r}"
            )

    def test_tuning_option_matches_kind_from_signal(self):
        from tuning.option import OPTION_SIGNAL_TYPES
        for sig in OPTION_SIGNAL_TYPES:
            assert kind_from_signal(sig) == "option", (
                f"tuning/option.py OPTION_SIGNAL_TYPES includes "
                f"{sig!r} but kind_from_signal says "
                f"{kind_from_signal(sig)!r}"
            )

    def test_calibrator_pk_clause_matches_stock_signals(self):
        """specialist_calibration.fit_calibrator builds an inline
        SQL list for the legacy-NULL-fallback case. Verify it
        matches kind_from_signal's stock set."""
        import re
        path = os.path.join(
            os.path.dirname(__file__), os.pardir,
            "specialist_calibration.py",
        )
        with open(path) as fh:
            src = fh.read()
        # The stock pk_clause is a multi-line Python-concatenated
        # SQL string. Pull out every single-quoted SQL identifier
        # within the stock branch — that's the signal-fallback list.
        # Find the chunk between `pipeline_kind == "stock":` and
        # the next `elif`/`else`/end of function.
        m = re.search(
            r"pipeline_kind\s*==\s*[\"']stock[\"'].*?"
            r"(?=elif\s+pipeline_kind|else:|\Z)",
            src, re.DOTALL,
        )
        assert m, "Couldn't locate stock pk_clause block"
        block = m.group(0)
        # Every single-quoted SQL token in the block is a signal.
        sigs = tuple(set(re.findall(r"'([A-Z_]+)'", block)))
        assert sigs, "No signals found in stock pk_clause block"
        for sig in sigs:
            assert kind_from_signal(sig) == "stock", (
                f"specialist_calibration.py stock pk_clause has "
                f"{sig!r} but kind_from_signal says "
                f"{kind_from_signal(sig)!r}"
            )


# ---------------------------------------------------------------------------
# DOMINANT-SIGNAL GUARDRAIL — HOLD specifically
# ---------------------------------------------------------------------------

class TestHOLDIsClassified:
    """Pin HOLD specifically. It's the dominant signal in
    production (93% of all predictions). Excluding it left stock
    calibration with ~5% of available training data — the
    2026-05-11 regression that prompted this whole guardrail file."""

    def test_hold_is_stock(self):
        assert kind_from_signal("HOLD") == "stock"

    def test_hold_in_journal_backfill(self):
        path = os.path.join(
            os.path.dirname(__file__), os.pardir, "journal.py",
        )
        with open(path) as fh:
            src = fh.read()
        # The backfill stock_signals tuple must include "HOLD"
        import re
        m = re.search(r"stock_signals\s*=\s*\(([^)]+)\)", src)
        assert m
        assert '"HOLD"' in m.group(1) or "'HOLD'" in m.group(1), (
            "journal.py backfill stock_signals MUST include HOLD — "
            "without it, ~93% of stock-pipeline predictions stay "
            "NULL after backfill (the 2026-05-11 cherry-pick bug)."
        )

    def test_hold_in_tuning_stock(self):
        from tuning.stock import STOCK_SIGNAL_TYPES
        assert "HOLD" in STOCK_SIGNAL_TYPES
