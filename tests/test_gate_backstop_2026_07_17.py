"""Confidence gate is a BACKSTOP, and it stays falsifiable (2026-07-17).

Operator-caught: two days after the gate went live, books sat at
70-92% cash with entries choked — the bars (60/65/55) were seeded
at/above the AI's honest confidence median (62), and the meta-model
blend discounts stated confidence BEFORE the gate reads it, so the
"rarely-firing backstop" was allocating capital (132 blocks vs 39
executed buys in half a day). Operator design rule: mechanisms INFORM
the AI; hard gates fire rarely ("the whole system is designed to
trade, not avoid trading").

Pinned here:
  1. every manifest seed for ai_confidence_threshold is backstop
     level (45) — an arm bar creeping back toward the median fails
     loudly;
  2. every CONFIDENCE_GATE drop hard-links to its pre-gate prediction
     row (trade_drops.pred_id), which the horizon resolver scores
     regardless of execution — the counterfactual evidence that keeps
     the floor falsifiable;
  3. gate_drop_calibration turns those links into per-band would-be
     outcomes;
  4. the backstop-health alarm fires a rate-limited ERROR when the
     gate blocks >20% of a day's entry flow — "fires rarely" is a
     CHECKED invariant now, not a design intention;
  5. the reseed migration is idempotent and its ledger rows are
     terminally stamped (the auto-expiry-replay class from
     2026-07-15 review finding #1).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_db(tmp_path):
    """A real journal-schema profile DB (same init path as prod)."""
    db = str(tmp_path / "quantopsai_profile_901.db")
    import journal
    journal.init_db(db)
    from ai_tracker import init_tracker_db
    init_tracker_db(db)
    return db


def _seed_prediction(db, symbol, signal="BUY", confidence=62,
                     cycle_id="cyc-1", outcome=None, return_pct=None):
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO ai_predictions "
            "(symbol, predicted_signal, confidence, reasoning, "
            " price_at_prediction, status, actual_outcome, "
            " actual_return_pct, cycle_id) "
            "VALUES (?, ?, ?, 'test', 100.0, ?, ?, ?, ?)",
            (symbol, signal, confidence,
             "resolved" if outcome else "pending",
             outcome, return_pct, cycle_id))
        return cur.lastrowid


# ---------------------------------------------------------------------------
# 1. seeds are backstop level
# ---------------------------------------------------------------------------

class TestBackstopSeeds:
    def test_every_manifest_seed_is_45(self):
        from create_experiment_profiles import PROFILES
        seeded = [(p["name"], p["ai_confidence_threshold"])
                  for p in PROFILES if "ai_confidence_threshold" in p]
        assert seeded, "manifest lost its confidence seeds entirely"
        wrong = [(n, v) for n, v in seeded if v != 45]
        assert not wrong, (
            f"confidence floor seeds crept off backstop level: {wrong}. "
            "The floor is a rarely-firing backstop — the QUALITY bar "
            "lives in the prompt, and per-profile moves belong to the "
            "tuner with counterfactual evidence (see CHANGELOG "
            "2026-07-17). If this change is deliberate, bring the "
            "gate-drop calibration data.")

    def test_bounds_allow_tuning_around_the_backstop(self):
        from param_bounds import PARAM_BOUNDS
        lo, hi = PARAM_BOUNDS["ai_confidence_threshold"]
        assert lo <= 40 and hi >= 50, (
            "the tuner must be able to move the floor around 45 in "
            "both directions")


# ---------------------------------------------------------------------------
# 2. drop → prediction hard link
# ---------------------------------------------------------------------------

class TestDropPredictionLink:
    def test_record_trade_drop_persists_pred_id(self, profile_db):
        import journal
        journal.record_trade_drop(
            db_path=profile_db, symbol="FCX", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="test",
            cycle_id="cyc-1", ai_confidence=55, pred_id=777)
        with sqlite3.connect(profile_db) as conn:
            row = conn.execute(
                "SELECT pred_id FROM trade_drops WHERE symbol='FCX'"
            ).fetchone()
        assert row[0] == 777

    def test_gate_threads_pred_id_into_the_drop(self, profile_db):
        """The REAL gate, not a replica: a blocked trade carrying the
        _pred_id stamped at recording time must land in the drop row."""
        from trade_pipeline import _apply_confidence_gate
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        captured = {}

        def _capture(**kw):
            captured.update(kw)

        # No resolve patch: the override resolve either succeeds or
        # falls back to ctx.ai_confidence_threshold — both are 45 here,
        # so the test exercises the gate's REAL resolution path.
        with patch("journal.record_trade_drop", side_effect=_capture):
            kept = _apply_confidence_gate(
                [{"symbol": "FCX", "action": "BUY", "confidence": 41,
                  "_pred_id": 314, "reasoning": "r"}],
                ctx, "cyc-1", [])
        assert kept == []
        assert captured.get("pred_id") == 314, (
            "the drop lost its prediction link — the gate's "
            "counterfactual scoring is dead without it")

    def test_pipeline_stamps_pred_id_on_selected_trades(self):
        """Structural: the recording loop must attach _pred_id to the
        selected trade dict, and the gate must pass it through."""
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline)
        assert 'selected["_pred_id"] = int(pred_id)' in src
        gate_src = inspect.getsource(
            trade_pipeline._apply_confidence_gate)
        assert 'pred_id=t.get("_pred_id")' in gate_src


# ---------------------------------------------------------------------------
# 3. calibration bands
# ---------------------------------------------------------------------------

class TestGateDropCalibration:
    def test_bands_wins_losses_and_pending(self, profile_db):
        import journal
        from ai_tracker import gate_drop_calibration
        # blocked idea that would have WON (+3.2%)
        p1 = _seed_prediction(profile_db, "AAA", confidence=62,
                              outcome="win", return_pct=3.2)
        journal.record_trade_drop(
            db_path=profile_db, symbol="AAA", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            ai_confidence=52, pred_id=p1)
        # blocked idea that would have LOST (-2.0%), same band
        p2 = _seed_prediction(profile_db, "BBB", confidence=60,
                              outcome="loss", return_pct=-2.0)
        journal.record_trade_drop(
            db_path=profile_db, symbol="BBB", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            ai_confidence=51, pred_id=p2)
        # blocked idea not yet resolved, different band
        p3 = _seed_prediction(profile_db, "CCC", confidence=48)
        journal.record_trade_drop(
            db_path=profile_db, symbol="CCC", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            ai_confidence=43, pred_id=p3)
        # non-gate drop must NOT count
        journal.record_trade_drop(
            db_path=profile_db, symbol="DDD", side="buy",
            drop_code="entry_blacklist", drop_reason="t",
            ai_confidence=70)

        cal = gate_drop_calibration(profile_db, days=7)
        assert cal["total"] == 3
        assert cal["resolved"] == 2
        by_band = {b["band"]: b for b in cal["bands"]}
        assert by_band["50-54"]["n"] == 2
        assert by_band["50-54"]["wins"] == 1
        assert by_band["50-54"]["losses"] == 1
        assert by_band["50-54"]["avg_return_pct"] == pytest.approx(0.6)
        assert by_band["40-44"]["n"] == 1
        assert by_band["40-44"]["resolved"] == 0
        assert by_band["40-44"]["avg_return_pct"] is None

    def test_multi_merge_weights_by_resolved(self, profile_db, tmp_path):
        import journal
        from ai_tracker import init_tracker_db, gate_drop_calibration_multi
        db2 = str(tmp_path / "quantopsai_profile_902.db")
        journal.init_db(db2)
        init_tracker_db(db2)
        p1 = _seed_prediction(profile_db, "AAA", outcome="win",
                              return_pct=4.0)
        journal.record_trade_drop(
            db_path=profile_db, symbol="AAA", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            ai_confidence=50, pred_id=p1)
        p2 = _seed_prediction(db2, "BBB", outcome="loss",
                              return_pct=-1.0)
        journal.record_trade_drop(
            db_path=db2, symbol="BBB", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            ai_confidence=53, pred_id=p2)
        merged = gate_drop_calibration_multi([profile_db, db2], days=7)
        assert merged["total"] == 2 and merged["resolved"] == 2
        band = merged["bands"][0]
        assert band["band"] == "50-54"
        assert band["n"] == 2 and band["wins"] == 1 and band["losses"] == 1
        assert band["avg_return_pct"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# 4. backstop-health alarm
# ---------------------------------------------------------------------------

class TestBackstopHealthAlarm:
    def _seed_flow(self, db, blocked, entered):
        import journal
        for i in range(blocked):
            journal.record_trade_drop(
                db_path=db, symbol=f"B{i}", side="buy",
                drop_code="CONFIDENCE_GATE", drop_reason="t",
                ai_confidence=40)
        with sqlite3.connect(db) as conn:
            for i in range(entered):
                conn.execute(
                    "INSERT INTO trades (symbol, side, qty, price, "
                    "status) VALUES (?, 'buy', 1, 10.0, 'open')",
                    (f"E{i}",))

    def test_fires_above_share_with_enough_flow(self, profile_db,
                                                caplog, monkeypatch):
        import trade_pipeline as tp
        monkeypatch.setattr(tp, "_GATE_BACKSTOP_ALARM_MEMO", {})
        self._seed_flow(profile_db, blocked=8, entered=2)
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        with caplog.at_level(logging.ERROR):
            tp._check_gate_backstop_health(ctx)
        assert any("NOT A BACKSTOP" in r.message for r in caplog.records)

    def test_rate_limited_after_firing(self, profile_db, caplog,
                                       monkeypatch):
        import trade_pipeline as tp
        monkeypatch.setattr(tp, "_GATE_BACKSTOP_ALARM_MEMO", {})
        self._seed_flow(profile_db, blocked=8, entered=2)
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        with caplog.at_level(logging.ERROR):
            tp._check_gate_backstop_health(ctx)
            caplog.clear()
            tp._check_gate_backstop_health(ctx)
        assert not any("NOT A BACKSTOP" in r.message
                       for r in caplog.records)

    def test_silent_below_min_flow(self, profile_db, caplog,
                                   monkeypatch):
        import trade_pipeline as tp
        monkeypatch.setattr(tp, "_GATE_BACKSTOP_ALARM_MEMO", {})
        self._seed_flow(profile_db, blocked=5, entered=1)  # flow 6 < 10
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        with caplog.at_level(logging.ERROR):
            tp._check_gate_backstop_health(ctx)
        assert not any("NOT A BACKSTOP" in r.message
                       for r in caplog.records)

    def test_silent_at_backstop_share(self, profile_db, caplog,
                                      monkeypatch):
        import trade_pipeline as tp
        monkeypatch.setattr(tp, "_GATE_BACKSTOP_ALARM_MEMO", {})
        self._seed_flow(profile_db, blocked=2, entered=18)  # 10%
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        with caplog.at_level(logging.ERROR):
            tp._check_gate_backstop_health(ctx)
        assert not any("NOT A BACKSTOP" in r.message
                       for r in caplog.records)

    def test_wired_into_the_gate(self):
        import inspect
        import trade_pipeline
        src = inspect.getsource(trade_pipeline._apply_confidence_gate)
        assert "_check_gate_backstop_health(ctx)" in src, (
            "the alarm exists but the gate never calls it — a checked "
            "invariant that nothing checks")


# ---------------------------------------------------------------------------
# 4b. the tuner can never RAISE the floor (review 2026-07-17 H1)
# ---------------------------------------------------------------------------

class TestFloorRaisesAreOperatorOnly:
    def test_apply_param_change_refuses_raises(self):
        """The one-way valve at the single write choke: raising the
        floor is refused before any DB touch; the refusal suffix names
        the rule."""
        from self_tuning import _apply_param_change
        applied, clamped, suffix = _apply_param_change(
            901, 1, "any_type", "ai_confidence_threshold", 45, 60,
            "test raise")
        assert applied == 45 and "operator-only" in suffix

    def test_band_jump_rules_are_gone(self):
        """The <35%-win-rate band rules jumped the floor to an
        absolute 70/60 — with a 45 backstop they could legally walk it
        to 67 in ~6 days (delta cap, then the re-anchored ±50% window).
        Structural: no tuner code writes 70/60 into the floor."""
        import inspect
        import self_tuning
        src = inspect.getsource(self_tuning)
        assert "_optimize_confidence_threshold_upward," not in src, (
            "the upward optimizer is back in the dispatch list")
        assert "def _optimize_confidence_threshold_upward" not in src
        # the main-pass band rules' signature constants
        assert "old_val, 70, reason" not in src
        assert "old_val, 60, reason" not in src

    def test_insight_propagation_cannot_replay_raises(self):
        import insight_propagation
        det = insight_propagation._detector_for("confidence_threshold")
        det2 = insight_propagation._detector_for(
            "confidence_threshold_optimization")
        assert det is None and det2 is None

    def test_lowering_paths_stay_live(self):
        """Drift-toward-trading is preserved: the false-negative
        lowerer still exists and still targets the floor downward."""
        import inspect
        import self_tuning
        src = inspect.getsource(self_tuning._optimize_false_negatives)
        assert "ai_confidence_threshold" in src
        assert "threshold - 5" in src


# ---------------------------------------------------------------------------
# 5. reseed migration: idempotent, terminal, exact backfill
# ---------------------------------------------------------------------------

class TestReseedMigration:
    def _master_db(self, tmp_path):
        db = str(tmp_path / "master.db")
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                CREATE TABLE trading_profiles (
                    id INTEGER PRIMARY KEY, name TEXT, user_id INTEGER,
                    ai_confidence_threshold INTEGER,
                    regime_overrides TEXT, symbol_overrides TEXT,
                    tod_overrides TEXT);
                CREATE TABLE tuning_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER, user_id INTEGER, timestamp TEXT,
                    adjustment_type TEXT, parameter_name TEXT,
                    old_value TEXT, new_value TEXT, reason TEXT,
                    outcome_after TEXT, expired_at TEXT, reviewed_at TEXT);
                CREATE TABLE param_references (
                    profile_id INTEGER, parameter_name TEXT,
                    reference_value REAL);
            """)
            from create_experiment_profiles import PROFILES
            name_60era = next(p["name"] for p in PROFILES
                              if "ai_confidence_threshold" in p)
            conn.execute(
                "INSERT INTO trading_profiles (id, name, user_id, "
                "ai_confidence_threshold) VALUES (210, ?, 1, 60)",
                (name_60era,))
            conn.execute(
                "INSERT INTO trading_profiles (id, name, user_id, "
                "ai_confidence_threshold) VALUES (207, "
                "'EXP-A1-BuyHoldSPY-NOTINMANIFEST', 1, 25)")
            conn.execute(
                "INSERT INTO param_references VALUES "
                "(210, 'ai_confidence_threshold', 60.0)")
        return db

    def test_reseed_applies_terminally_and_reruns_clean(
            self, tmp_path, monkeypatch):
        import config
        import migrate_confidence_backstop_2026_07_17 as mig
        monkeypatch.setattr(config, "DB_PATH", self._master_db(tmp_path))
        findings = mig.reseed_master(apply=True)
        assert any("-> 45" in f for f in findings)
        with sqlite3.connect(config.DB_PATH) as conn:
            thr = conn.execute(
                "SELECT ai_confidence_threshold FROM trading_profiles "
                "WHERE id=210").fetchone()[0]
            assert thr == 45
            control = conn.execute(
                "SELECT ai_confidence_threshold FROM trading_profiles "
                "WHERE id=207").fetchone()[0]
            assert control == 25, "controls must never be redesigned"
            led = conn.execute(
                "SELECT outcome_after, expired_at, reviewed_at FROM "
                "tuning_history WHERE "
                "adjustment_type='operator_backstop_reseed'").fetchone()
            assert led[0] == "n/a" and led[1] and led[2], (
                "reseed ledger row must be terminal on every axis — "
                "the auto-expiry replay class")
            n_refs = conn.execute(
                "SELECT COUNT(*) FROM param_references WHERE "
                "parameter_name='ai_confidence_threshold'").fetchone()[0]
            assert n_refs == 0
        # idempotent: second apply changes nothing
        findings2 = mig.reseed_master(apply=True)
        assert not any("-> 45" in f for f in findings2)

    def test_backfill_links_exact_cycle_and_is_rerun_safe(
            self, profile_db, tmp_path, monkeypatch):
        import journal
        import migrate_confidence_backstop_2026_07_17 as mig
        # the glob is cwd-relative — point it at the fixture dir
        monkeypatch.chdir(tmp_path)
        pred = _seed_prediction(profile_db, "FCX", cycle_id="cyc-9")
        decoy = _seed_prediction(profile_db, "FCX", signal="HOLD",
                                 cycle_id="cyc-9")
        journal.record_trade_drop(
            db_path=profile_db, symbol="FCX", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            cycle_id="cyc-9", ai_confidence=55)
        journal.record_trade_drop(
            db_path=profile_db, symbol="FCX", side="buy",
            drop_code="entry_blacklist", drop_reason="t",
            cycle_id="cyc-9")
        mig.link_profile_dbs(apply=True)
        with sqlite3.connect(profile_db) as conn:
            linked = conn.execute(
                "SELECT pred_id FROM trade_drops WHERE "
                "drop_code='CONFIDENCE_GATE'").fetchone()[0]
            assert linked == pred, (
                f"must link the non-HOLD prediction ({pred}), never "
                f"the HOLD decoy ({decoy})")
            other = conn.execute(
                "SELECT pred_id FROM trade_drops WHERE "
                "drop_code='entry_blacklist'").fetchone()[0]
            assert other is None, "non-gate drops must stay unlinked"
        findings2 = mig.link_profile_dbs(apply=True)
        assert not any("linked" in f for f in findings2)

    def test_backfill_skips_same_cycle_executed_symbol(
            self, profile_db, tmp_path, monkeypatch):
        """Review 2026-07-17 M3: a gate-blocked ALTERNATE carries no
        prediction row; if its symbol ALSO executed as a primary in
        that cycle, the (cycle_id, symbol) join would link the drop to
        the EXECUTED primary's prediction — a real trade's outcome
        counted as refused. The guard: any entry-side trades row for
        the symbol within ±10 min of the drop blocks the link."""
        import sqlite3 as _sq
        import journal
        import migrate_confidence_backstop_2026_07_17 as mig
        monkeypatch.chdir(tmp_path)
        _seed_prediction(profile_db, "NVDA", cycle_id="cyc-x")
        journal.record_trade_drop(
            db_path=profile_db, symbol="NVDA", side="buy",
            drop_code="CONFIDENCE_GATE", drop_reason="t",
            cycle_id="cyc-x", ai_confidence=52)
        with _sq.connect(profile_db) as conn:
            conn.execute(
                "INSERT INTO trades (symbol, side, qty, price, status) "
                "VALUES ('NVDA', 'buy', 10, 100.0, 'open')")
        mig.link_profile_dbs(apply=True)
        with _sq.connect(profile_db) as conn:
            linked = conn.execute(
                "SELECT pred_id FROM trade_drops WHERE symbol='NVDA'"
            ).fetchone()[0]
        assert linked is None, (
            "an executed trade's prediction must never be counted as "
            "a refused counterfactual")

    def test_verify_flags_manifest_names_missing_from_prod(
            self, tmp_path, monkeypatch, capsys):
        """Review 2026-07-17 M4: a renamed prod profile silently kept
        its allocator-era bar while the script printed CLEAN. verify()
        now requires every manifest seed to match a prod row by
        name."""
        import config
        import migrate_confidence_backstop_2026_07_17 as mig
        monkeypatch.setattr(config, "DB_PATH", self._master_db(tmp_path))
        monkeypatch.chdir(tmp_path)  # no profile DBs to scan
        mig.reseed_master(apply=True)
        bad = mig.verify()
        out = capsys.readouterr().out
        assert bad >= 1
        assert "matched no prod profile by name" in out


# ---------------------------------------------------------------------------
# 6. alarm denominator excludes protective covers (review 2026-07-17 L1)
# ---------------------------------------------------------------------------

class TestAlarmExcludesCovers:
    def test_cover_fill_does_not_count_as_entry(self, profile_db,
                                                caplog, monkeypatch):
        """Discriminating construction: 3 gate blocks + 11 real entries
        + 30 filled cover-buys TODAY, each cover referenced by a
        YESTERDAY short's protective pointer. Covers wrongly counted:
        3/44 = 7% -> silent (the L1 defect). Covers excluded:
        3/14 = 21% -> fires. The alarm MUST fire."""
        import sqlite3 as _sq
        import journal
        import trade_pipeline as tp
        monkeypatch.setattr(tp, "_GATE_BACKSTOP_ALARM_MEMO", {})
        for i in range(3):
            journal.record_trade_drop(
                db_path=profile_db, symbol=f"B{i}", side="buy",
                drop_code="CONFIDENCE_GATE", drop_reason="t",
                ai_confidence=40)
        with _sq.connect(profile_db) as conn:
            for i in range(11):
                conn.execute(
                    "INSERT INTO trades (symbol, side, qty, price, "
                    "status, order_id) "
                    "VALUES (?, 'buy', 5, 10.0, 'open', ?)",
                    (f"R{i}", f"real-{i}"))
            for i in range(30):
                # today's cover fill…
                conn.execute(
                    "INSERT INTO trades (symbol, side, qty, price, "
                    "status, order_id) "
                    "VALUES (?, 'buy', 5, 10.0, 'open', ?)",
                    (f"C{i}", f"cover-{i}"))
                # …referenced by yesterday's short entry (old-dated so
                # the referencing row itself is outside today's count)
                conn.execute(
                    "INSERT INTO trades (symbol, side, qty, price, "
                    "status, order_id, protective_stop_order_id, "
                    "timestamp) "
                    "VALUES (?, 'short', 5, 10.0, 'closed', ?, ?, "
                    "datetime('now', '-1 day'))",
                    (f"C{i}", f"e-{i}", f"cover-{i}"))
        ctx = SimpleNamespace(db_path=profile_db,
                              ai_confidence_threshold=45)
        with caplog.at_level(logging.ERROR):
            tp._check_gate_backstop_health(ctx)
        assert any("NOT A BACKSTOP" in r.message
                   for r in caplog.records), (
            "cover-buys inflated the entry count and muted the alarm")
