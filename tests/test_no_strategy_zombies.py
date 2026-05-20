"""Structural guardrail: no registered strategy should be a zombie.

A "zombie" is a strategy that's in the `strategies/` registry but
has never produced a single prediction across any profile after a
grace period (default 14 days).

The bug class (2026-05-15 audit).
The first audit found 12 of 26 strategies producing zero lifetime
predictions across all profiles ever. Root causes ranged from API
contract drift (`earnings_drift` reading a field that doesn't exist
in the data source) to genuine bugs (`high_iv_rank_fade` comparing a
dict to an int) to silent data-layer failures (Alpaca data API
returning 401 → bars falling back to yfinance, options endpoints
returning None). All of these would have been caught WEEKS earlier
by a simple "did this strategy ever fire?" check.

This test pins the contract:
  - Every strategy registered in `strategies/` and >14 days old
    MUST have at least one lifetime prediction in at least one
    profile DB. If not → zombie → fail CI.
  - Strategies registered <14 days ago are exempt (cold-start
    grace).
  - Wrappers (e.g. `market_engine`) are exempt — they emit
    predictions tagged with sub-strategy names.
  - Test only runs against profile DBs that exist locally.
    On CI without prod DBs the test is a no-op (it only catches
    regressions when run on a system that HAS production data).

Class-level enforcement: the test reads the strategy registry
dynamically. New strategies added under `strategies/` are
automatically subject to the check after 14 days. No allowlist
needed.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from glob import glob
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Strategies that are wrappers/routers, not real strategies.
# Their signals get tagged under sub-strategy names so they don't
# need their OWN lifetime entries.
_WRAPPERS = {"market_engine"}

# Grace period: a strategy file added <N days ago is exempt because
# it hasn't had time to encounter its trigger conditions yet.
_GRACE_DAYS = 14


def _profile_dbs():
    """Return all profile DBs that exist in the project root.
    Empty on CI / fresh checkouts → test becomes a no-op."""
    root = Path(__file__).resolve().parent.parent
    return [str(p) for p in root.glob("quantopsai_profile_*.db")]


def _strategy_age_days(strategy_module):
    """File mtime → days since last modification. Approximates
    'how long has this strategy been deployed.' A strategy that was
    just edited this session counts as fresh; one that hasn't been
    touched in 30 days has had plenty of trigger windows."""
    try:
        path = strategy_module.__file__
        if not path:
            return 0
        from datetime import datetime, timezone
        mtime = os.path.getmtime(path)
        age_seconds = datetime.now(timezone.utc).timestamp() - mtime
        return int(age_seconds / 86400)
    except Exception:
        return 0


def _lifetime_n_anywhere(strategy_name, db_paths):
    """Total lifetime predictions for `strategy_name` summed across
    every profile DB. Returns 0 if no DB has a row."""
    total = 0
    for db in db_paths:
        try:
            with closing(sqlite3.connect(db)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM ai_predictions "
                    "WHERE strategy_type = ?",
                    (strategy_name,),
                ).fetchone()
                if row:
                    total += int(row[0])
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            continue
    return total


class TestNoStrategyZombies:
    def test_every_aged_strategy_has_at_least_one_lifetime_prediction(self):
        """Headline contract. If a strategy file has existed for >14
        days and has zero lifetime predictions in EVERY profile DB,
        it's a zombie — flag it.

        On a CI machine with no profile DBs, this becomes a no-op
        (skips). The test only catches regressions in environments
        with production data."""
        dbs = _profile_dbs()
        if not dbs:
            pytest.skip(
                "No profile DBs found — test requires production-like "
                "data; skipped on CI / fresh checkout.",
            )

        from strategies import discover_strategies
        # discover_strategies takes a market_type but to enumerate ALL
        # registered strategies we union across the markets we trade.
        all_modules = {}
        for mt in ("stocks", "crypto"):
            for mod in discover_strategies(mt):
                name = getattr(mod, "NAME", None)
                if name and name not in _WRAPPERS:
                    all_modules[name] = mod

        zombies = []
        for name, mod in all_modules.items():
            age = _strategy_age_days(mod)
            if age < _GRACE_DAYS:
                continue
            total = _lifetime_n_anywhere(name, dbs)
            if total == 0:
                zombies.append((name, age))

        assert not zombies, (
            f"Found {len(zombies)} strategy zombie(s) — registered "
            f"but produced ZERO lifetime predictions across all "
            f"profiles after >{_GRACE_DAYS} days:\n"
            + "\n".join(f"  - {n} (age {a}d)" for n, a in zombies)
            + "\n\nA zombie usually means: API contract drift on the "
            "data source, unreachable threshold conditions, or the "
            "strategy is registered but its dependency is broken. "
            "See `STRATEGY_AUDIT_PLAN.md` for the previous incident "
            "and remediation playbook."
        )

    def test_grace_period_protects_freshly_added_strategies(self, tmp_path):
        """Sanity check on the exemption logic itself: a strategy
        edited today must NOT be flagged. Uses a synthetic module
        whose mtime is now."""
        # Create a fake strategy file with mtime=now.
        fake = tmp_path / "fake_strategy.py"
        fake.write_text("NAME = 'fake_strategy'\n")
        from types import ModuleType
        mod = ModuleType("fake_strategy")
        mod.__file__ = str(fake)
        mod.NAME = "fake_strategy"
        age = _strategy_age_days(mod)
        assert age <= 1, (
            f"Just-created strategy has age {age}d; expected <=1. "
            f"The grace-period mechanism is broken."
        )

    def test_wrapper_strategies_are_exempt(self):
        """`market_engine` is a wrapper — its predictions are tagged
        with sub-strategy names. Must not be in the zombie set."""
        assert "market_engine" in _WRAPPERS, (
            "market_engine must be in _WRAPPERS — without this "
            "exemption the zombie test will permanently flag it as "
            "broken even though it's working as designed."
        )
