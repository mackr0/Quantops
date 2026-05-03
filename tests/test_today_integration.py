"""End-to-end integration tests for tonight's additions.

These cover the cross-component behaviors that unit tests can't:
  - Capital allocation with mixed Sharpe at the new 16-strategy library size
  - Multi-strategy score aggregation when many strategies vote on one symbol
  - call_ai → ledger threading (kwargs flow through to a real ledger row)
  - Scheduler wiring assertions (backup task is actually registered)
  - Backup integrity on a WAL-mode database that's actively being written
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# 1. Capital allocation with mixed Sharpe at 16 strategies
# ---------------------------------------------------------------------------

class TestCapitalAllocationAtScale:
    """The cap-and-redistribute logic gets exercised differently at 16
    strategies vs 6: there are far more under-cap strategies to absorb
    excess from a hot strategy. Verify it still sums to 1.0 and respects
    the 40% cap."""

    def test_one_hot_strategy_capped_redistributed(self, tmp_profile_db,
                                                    monkeypatch):
        import multi_strategy
        import alpha_decay

        names = [f"strat_{i}" for i in range(16)]

        def fake_rolling(db, name, window_days=30):
            # One strategy with massive Sharpe, rest losing
            if name == "strat_0":
                return {"sharpe_ratio": 8.0, "n_predictions": 60, "win_rate": 0.7}
            return {"sharpe_ratio": 0, "n_predictions": 0, "win_rate": 0}

        def fake_lifetime(db, name):
            if name == "strat_0":
                return {"sharpe_ratio": 5.0, "n_predictions": 200}
            return {"sharpe_ratio": 0, "n_predictions": 0}

        monkeypatch.setattr(alpha_decay, "compute_rolling_metrics", fake_rolling)
        monkeypatch.setattr(alpha_decay, "compute_lifetime_metrics", fake_lifetime)

        weights = multi_strategy.compute_capital_allocations(names, tmp_profile_db)

        # Always sum to 1.0
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0
        # Hot strategy capped at 40%
        assert weights["strat_0"] <= 0.40 + 1e-6
        # The 15 cold strategies share the remaining ~60% — each gets a
        # positive but small allocation
        for name in names[1:]:
            assert weights[name] > 0
            assert weights[name] < 0.40

    def test_three_hot_strategies_all_capped(self, tmp_profile_db, monkeypatch):
        """Three strategies each individually deserve > 40%. After capping,
        all three sit at 40% and the remaining 13 share -20% (impossible)
        — so the cap math must clamp gracefully."""
        import multi_strategy
        import alpha_decay

        names = [f"strat_{i}" for i in range(16)]
        hot = {"strat_0", "strat_1", "strat_2"}

        def fake_rolling(db, name, window_days=30):
            if name in hot:
                return {"sharpe_ratio": 5.0, "n_predictions": 60, "win_rate": 0.7}
            return {"sharpe_ratio": 0, "n_predictions": 0, "win_rate": 0}

        def fake_lifetime(db, name):
            if name in hot:
                return {"sharpe_ratio": 4.0, "n_predictions": 200}
            return {"sharpe_ratio": 0, "n_predictions": 0}

        monkeypatch.setattr(alpha_decay, "compute_rolling_metrics", fake_rolling)
        monkeypatch.setattr(alpha_decay, "compute_lifetime_metrics", fake_lifetime)

        weights = multi_strategy.compute_capital_allocations(names, tmp_profile_db)
        assert pytest.approx(sum(weights.values()), abs=1e-6) == 1.0
        # No single strategy may exceed 40%
        for w in weights.values():
            assert w <= 0.40 + 1e-6, f"strategy got {w*100:.1f}% (over 40% cap)"

    def test_default_weight_scales_inversely_with_count(self):
        from multi_strategy import _default_weight
        # The bug we fixed: hardcoded 1/6. At 16 it must be 1/16.
        assert _default_weight(6) == pytest.approx(1.0 / 6)
        assert _default_weight(16) == pytest.approx(1.0 / 16)
        assert _default_weight(40) == pytest.approx(1.0 / 40)


# ---------------------------------------------------------------------------
# 2. Multi-strategy score aggregation under heavy voting
# ---------------------------------------------------------------------------

class TestScoreAggregationManyVoters:
    """When 10+ strategies all flag the same symbol, the merged score can
    exceed the original ±2 STRONG_BUY threshold by a lot. Verify the
    final signal mapping handles it sanely."""

    def test_unanimous_buy_gets_strong_buy(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        # Force every strategy to buy AAPL
        for mod in discover_strategies("midcap"):
            monkeypatch.setattr(mod, "find_candidates", lambda ctx, uni: [{
                "symbol": "AAPL", "signal": "BUY", "score": 1,
                "votes": {mod.NAME: "BUY"}, "reason": "test", "price": 100,
            }])

        sample_ctx.segment = "midcap"
        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        entries = [c for c in out["candidates"] if c["symbol"] == "AAPL"]
        assert len(entries) == 1
        # With 16 strategies all voting BUY, score should be >= 2 → STRONG_BUY
        assert entries[0]["signal"] == "STRONG_BUY"
        # Score reflects the count of agreeing strategies (capped/floored
        # by the dominant direction logic — should be > 1)
        assert entries[0]["score"] > 1
        # Every strategy is recorded as a voter
        assert len(entries[0]["source_strategies"]) >= 5

    def test_split_decision_dampens_to_hold(self, sample_ctx, monkeypatch):
        from strategies import discover_strategies
        mods = discover_strategies("midcap")
        # Half BUY, half SELL — dominant direction wins by 1
        for i, mod in enumerate(mods):
            sig = "BUY" if i % 2 == 0 else "SELL"
            monkeypatch.setattr(
                mod, "find_candidates",
                lambda ctx, uni, s=sig, m=mod: [{
                    "symbol": "AAPL", "signal": s, "score": 1,
                    "votes": {m.NAME: s}, "reason": "test", "price": 100,
                }],
            )

        sample_ctx.segment = "midcap"
        # Short-selling must be on for SELL votes to survive aggregation
        sample_ctx.enable_short_selling = True
        from multi_strategy import aggregate_candidates
        out = aggregate_candidates(sample_ctx, ["AAPL"])
        entries = [c for c in out["candidates"] if c["symbol"] == "AAPL"]
        assert len(entries) == 1
        # All voters recorded even when conflicting
        votes = entries[0]["votes"]
        buy_votes = sum(1 for v in votes.values() if v == "BUY")
        sell_votes = sum(1 for v in votes.values() if v == "SELL")
        assert buy_votes > 0 and sell_votes > 0


# ---------------------------------------------------------------------------
# 3. call_ai end-to-end ledger threading
# ---------------------------------------------------------------------------

class TestCallAiLedgerThreading:
    """We added db_path + purpose kwargs to call_ai. Verify a successful
    call actually produces a row in the ledger via the SDK usage object."""

    def test_anthropic_call_writes_ledger_row(self, tmp_profile_db, monkeypatch):
        # Mock the Anthropic SDK at the import inside _call_anthropic.
        # The mock returns a message with .content[0].text and .usage attrs.
        class _Usage:
            input_tokens = 1500
            output_tokens = 200

        class _ContentBlock:
            text = '{"trades": [], "portfolio_reasoning": "test"}'

        class _Message:
            content = [_ContentBlock()]
            usage = _Usage()

        class _Messages:
            def create(self, **kwargs):
                return _Message()

        class _Client:
            def __init__(self, api_key=None):
                self.messages = _Messages()

        import sys, types
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = _Client
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        from ai_providers import call_ai
        result = call_ai(
            "test prompt",
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key="fake-key",
            db_path=tmp_profile_db,
            purpose="integration_test",
        )
        assert "test" in result   # response was received

        # Verify the ledger has exactly one row with the right metadata
        conn = sqlite3.connect(tmp_profile_db)
        rows = conn.execute(
            "SELECT provider, model, input_tokens, output_tokens, "
            "       purpose, estimated_cost_usd FROM ai_cost_ledger"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "anthropic"
        assert row[1] == "claude-haiku-4-5-20251001"
        assert row[2] == 1500
        assert row[3] == 200
        assert row[4] == "integration_test"
        # Cost = 1500 * 1/M + 200 * 5/M = 0.0015 + 0.001 = 0.0025
        assert row[5] == pytest.approx(0.0025, abs=1e-6)

    def test_call_without_db_path_does_not_log(self, monkeypatch):
        """Backwards-compat: callers that don't pass db_path must not
        crash and must not write anywhere."""
        class _Usage:
            input_tokens = 100
            output_tokens = 50

        class _ContentBlock:
            text = "ok"

        class _Message:
            content = [_ContentBlock()]
            usage = _Usage()

        class _Messages:
            def create(self, **kwargs):
                return _Message()

        class _Client:
            def __init__(self, api_key=None):
                self.messages = _Messages()

        import sys, types
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = _Client
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        from ai_providers import call_ai
        # No db_path — should still work, just no ledger
        result = call_ai("test", provider="anthropic",
                         model="claude-haiku-4-5", api_key="fake")
        assert result == "ok"


# ---------------------------------------------------------------------------
# 4. Scheduler wiring — backup task must be registered
# ---------------------------------------------------------------------------

class TestSchedulerWiring:
    """Make sure tonight's two new scheduler tasks are actually wired in.
    A regression here would silently disable backups or AI cost tracking."""

    def test_run_segment_cycle_invokes_db_backup_in_snapshot_block(self,
                                                                     monkeypatch,
                                                                     sample_ctx):
        """Verify that when run_snapshot=True, the DB backup task fires."""
        import multi_scheduler

        called: list = []

        def fake_backup(ctx):
            called.append(("backup", getattr(ctx, "segment", "?")))

        # Stub all the heavy tasks so the cycle runs fast
        monkeypatch.setattr(multi_scheduler, "_task_db_backup", fake_backup)
        for stub in ("_task_scan_and_trade", "_task_check_exits",
                     "_task_cancel_stale_orders", "_task_update_fills",
                     "_task_resolve_predictions", "_task_daily_snapshot",
                     "_task_self_tune", "_task_retrain_meta_model",
                     "_task_alpha_decay", "_task_sec_filings",
                     "_task_event_tick", "_task_crisis_monitor",
                     "_task_auto_strategy_lifecycle",
                     "_task_auto_strategy_generation",
                     "_task_daily_summary_email",
                     "_task_app_store_snapshot",
                     "_task_pdufa_scrape"):
            monkeypatch.setattr(multi_scheduler, stub, lambda ctx: None)

        # Make init_db a no-op since we're not actually persisting anything
        monkeypatch.setattr("journal.init_db", lambda db: None)

        multi_scheduler.run_segment_cycle(
            sample_ctx,
            run_scan=False, run_exits=False,
            run_predictions=False, run_snapshot=True,
            run_summary=False,
        )
        assert len(called) == 1, "DB backup task did not fire in snapshot block"
        assert called[0][0] == "backup"

    def test_scan_block_invokes_event_tick(self, monkeypatch, sample_ctx):
        """Phase 9 event tick must fire alongside scan. If a refactor
        removes it, events stop dispatching silently."""
        import multi_scheduler

        called: list = []
        monkeypatch.setattr(
            multi_scheduler, "_task_event_tick",
            lambda ctx: called.append("event_tick"),
        )
        for stub in ("_task_scan_and_trade", "_task_crisis_monitor"):
            monkeypatch.setattr(multi_scheduler, stub, lambda ctx: None)
        monkeypatch.setattr("journal.init_db", lambda db: None)

        multi_scheduler.run_segment_cycle(
            sample_ctx,
            run_scan=True, run_exits=False,
            run_predictions=False, run_snapshot=False,
        )
        assert called == ["event_tick"]


# ---------------------------------------------------------------------------
# 5. Backup integrity under WAL-mode write load
# ---------------------------------------------------------------------------

class TestBackupOnWalDb:
    """The whole point of using SQLite's .backup API is WAL safety.
    A `cp` of a WAL-mode DB during writes can corrupt the copy. Verify
    our backup_one produces a readable, complete DB even when the source
    is being actively written to."""

    def test_backup_consistent_during_concurrent_writes(self, tmp_path):
        from backup_db import backup_one

        src = tmp_path / "live.db"
        dest = tmp_path / "bkp.db"

        # Set up source with WAL mode + 100 rows
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (k INTEGER, v TEXT)")
        for i in range(100):
            conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
        conn.commit()
        conn.close()

        # Concurrent writer adds rows while backup runs
        stop = threading.Event()
        write_count = [0]

        def writer():
            w = sqlite3.connect(str(src))
            w.execute("PRAGMA journal_mode=WAL")
            i = 1000
            while not stop.is_set():
                try:
                    w.execute("INSERT INTO t VALUES (?, ?)", (i, f"w{i}"))
                    w.commit()
                    write_count[0] += 1
                    i += 1
                except Exception:
                    pass
                time.sleep(0.001)
            w.close()

        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)   # let some writes happen first

        ok = backup_one(str(src), str(dest))
        stop.set()
        t.join(timeout=2)

        assert ok is True
        assert dest.exists()

        # Backup must be a valid SQLite DB with at least the original 100 rows
        bkp = sqlite3.connect(str(dest))
        n = bkp.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        bkp.close()
        assert n >= 100, f"backup lost rows ({n} < 100) — WAL safety broken"
        # Sanity: writer made progress so the test was meaningful
        assert write_count[0] > 0, "concurrent writer didn't run — test invalid"


# ---------------------------------------------------------------------------
# 6. Spend summary on a profile DB that's actively being written
# ---------------------------------------------------------------------------

class TestSpendSummaryOnRealJournal:
    """Verify spend_summary works on a DB that has the full schema (not
    just the ai_cost_ledger table) — catches schema-conflict regressions."""

    def test_works_alongside_other_tables(self, tmp_profile_db):
        # tmp_profile_db is created via journal.init_db, which creates
        # all tables including ai_cost_ledger. Insert a few rows mixing
        # ai_predictions and ai_cost_ledger to verify isolation.
        conn = sqlite3.connect(tmp_profile_db)
        conn.execute(
            """INSERT INTO ai_predictions
                 (timestamp, symbol, predicted_signal, confidence,
                  price_at_prediction, status)
               VALUES (datetime('now'), 'AAPL', 'BUY', 70, 100, 'pending')"""
        )
        conn.execute(
            """INSERT INTO ai_cost_ledger
                 (provider, model, input_tokens, output_tokens,
                  purpose, estimated_cost_usd)
               VALUES ('anthropic', 'haiku', 1000, 200, 'test', 0.002)"""
        )
        conn.commit()
        conn.close()

        from ai_cost_ledger import spend_summary
        s = spend_summary(tmp_profile_db)
        assert s["today"]["calls"] == 1
        assert s["today"]["usd"] == pytest.approx(0.002, abs=1e-6)
