"""#185 — multi-horizon outcomes + deterministic-panel snapshot
(2026-05-20).

Optimizes the schema for future AI fine-tuning. The dataset builder
(`ai_tracker.build_training_dataset`) is the payoff — one call returns
a clean per-prediction frame with horizon labels (1d/3d/5d/10d/20d)
and the parsed rule-vote snapshot, ready for the training pipeline.

Surface tested:
  1. Migration creates ai_prediction_outcomes table + adds
     rule_votes_json to ai_predictions; idempotent re-run.
  2. record_prediction serializes rule_votes to JSON (trimming
     reasoning text; preserving name/severity/direction).
  3. record_prediction with rule_votes=None writes NULL.
  4. _classify_outcome boundary cases (the trainer's label distribution
     hinges on these thresholds being stable).
  5. _measure_one_prediction writes the right return + MFE + MAE +
     outcome_class for a long prediction.
  6. _measure_one_prediction inverts return/MFE/MAE sign convention
     for a short prediction (positive MFE = "right at some point").
  7. _measure_one_prediction is idempotent — re-running does NOT
     write duplicates (UNIQUE constraint).
  8. _measure_one_prediction skips horizons that haven't elapsed
     (target_idx beyond bars).
  9. measure_horizon_outcomes skips option signals entirely.
 10. measure_horizon_outcomes skips symbols whose every prediction
     already has all 5 horizons (saves the bar fetch).
 11. build_training_dataset returns per-prediction dicts with
     {features, rule_votes, outcomes} parsed.
 12. build_training_dataset honors min_horizons_required threshold.
 13. build_training_dataset with include_unresolved=True yields rows
     with empty outcomes dicts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Per-test temp DB initialized to the full current schema."""
    db_path = str(tmp_path / "test_185.db")
    monkeypatch.setattr("config.DB_PATH", db_path, raising=False)
    from journal import init_db
    init_db(db_path)
    from ai_tracker import init_tracker_db
    init_tracker_db(db_path)
    return db_path


def _record_pred(db_path, symbol="AAPL", signal="BUY", price=100.0,
                  ts=None, rule_votes=None):
    from ai_tracker import record_prediction
    pid = record_prediction(
        symbol=symbol, predicted_signal=signal, confidence=80,
        reasoning="t", price_at_prediction=price,
        db_path=db_path, rule_votes=rule_votes,
    )
    if ts:
        # Backdate so horizon math works without time-travelling
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (ts, pid),
            )
            conn.commit()
    return pid


def _bars(prices_by_date):
    """Build a daily OHLCV DataFrame from a {date_iso: (o,h,l,c)} map.
    Indexed by tz-aware US/Eastern timestamps to match what
    market_data.get_bars_daterange returns."""
    rows = []
    for ds, (o, h, l, c) in prices_by_date.items():
        rows.append({
            "open": o, "high": h, "low": l, "close": c, "volume": 1_000_000,
        })
    idx = pd.to_datetime(list(prices_by_date.keys())).tz_localize("US/Eastern")
    return pd.DataFrame(rows, index=idx)


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_outcomes_table_exists_after_init(self, db):
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='ai_prediction_outcomes'"
            ).fetchone()
        assert row is not None, (
            "ai_prediction_outcomes table missing after init. The "
            "CREATE TABLE statement in journal.py must run during "
            "init_db so fresh DBs get the new schema."
        )

    def test_outcomes_table_unique_constraint(self, db):
        """The UNIQUE (prediction_id, horizon_days) is what makes the
        measurement function safely idempotent. Without it, re-runs
        would duplicate horizon rows."""
        pid = _record_pred(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO ai_prediction_outcomes "
                "(prediction_id, horizon_days, return_pct, measured_at) "
                "VALUES (?, 5, 1.0, '2026-05-20T00:00:00')",
                (pid,),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ai_prediction_outcomes "
                    "(prediction_id, horizon_days, return_pct, measured_at) "
                    "VALUES (?, 5, 2.0, '2026-05-20T00:00:00')",
                    (pid,),
                )

    def test_rule_votes_json_column_exists(self, db):
        with closing(sqlite3.connect(db)) as conn:
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(ai_predictions)").fetchall()}
        assert "rule_votes_json" in cols

    def test_migration_idempotent(self, db):
        """Re-running init must not error on the second call (legacy
        DBs may get init_db called repeatedly across deploys)."""
        from journal import init_db
        init_db(db)  # second call — must not raise


# ---------------------------------------------------------------------------
# 2-3. record_prediction with rule_votes
# ---------------------------------------------------------------------------

class TestRecordPredictionRuleVotes:
    def test_serializes_to_json_trimming_reasoning(self, db):
        """Each verdict should serialize to {name, severity, direction}
        only. Reasoning text is dropped — it's reconstructable by
        rerunning the rule and bloats the row."""
        votes = [
            {"name": "rule_a", "severity": "VETO",
             "reasoning": "long text we drop", "direction": "long"},
            {"name": "rule_b", "severity": "CONFIRM",
             "reasoning": "more text", "direction": "long"},
        ]
        pid = _record_pred(db, rule_votes=votes)
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT rule_votes_json FROM ai_predictions WHERE id = ?",
                (pid,),
            ).fetchone()
        stored = json.loads(row[0])
        assert stored == [
            {"name": "rule_a", "severity": "VETO", "direction": "long"},
            {"name": "rule_b", "severity": "CONFIRM", "direction": "long"},
        ]
        assert "reasoning" not in json.dumps(stored)

    def test_none_writes_null(self, db):
        pid = _record_pred(db, rule_votes=None)
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT rule_votes_json FROM ai_predictions WHERE id = ?",
                (pid,),
            ).fetchone()
        assert row[0] is None

    def test_empty_list_writes_null(self, db):
        """Empty list (no rules fired) should also be NULL — saves
        storage on the modal case (most candidates fire zero rules)."""
        pid = _record_pred(db, rule_votes=[])
        with closing(sqlite3.connect(db)) as conn:
            row = conn.execute(
                "SELECT rule_votes_json FROM ai_predictions WHERE id = ?",
                (pid,),
            ).fetchone()
        assert row[0] is None


# ---------------------------------------------------------------------------
# 4. _classify_outcome
# ---------------------------------------------------------------------------

class TestClassifyOutcome:
    """The trainer's cross-entropy label distribution hinges on these
    thresholds. Lock them so a refactor can't quietly shift the
    label space."""

    def test_big_win(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(5.0) == "big_win"
        assert _classify_outcome(10.0) == "big_win"

    def test_win(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(1.0) == "win"
        assert _classify_outcome(4.99) == "win"

    def test_flat(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(0.0) == "flat"
        assert _classify_outcome(0.99) == "flat"
        assert _classify_outcome(-0.99) == "flat"

    def test_loss(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(-1.0) == "loss"
        assert _classify_outcome(-4.99) == "loss"

    def test_big_loss(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(-5.0) == "big_loss"
        assert _classify_outcome(-100.0) == "big_loss"

    def test_none_returns_none(self):
        from ai_tracker import _classify_outcome
        assert _classify_outcome(None) is None


# ---------------------------------------------------------------------------
# 5-8. _measure_one_prediction
# ---------------------------------------------------------------------------

class TestMeasureOneLong:
    def _pred_row(self, db, pid, symbol="AAPL", signal="BUY", price=100.0,
                   ts_offset_days=21):
        ts = (datetime.utcnow() - timedelta(days=ts_offset_days)).isoformat()
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (ts, pid),
            )
            conn.commit()
        return {
            "id": pid, "symbol": symbol, "predicted_signal": signal,
            "price_at_prediction": price, "timestamp": ts,
        }

    def _build_bars(self):
        """22 trading days of bars starting at $100, ending at $108.
        Day 1 = +1.5%, day 5 = +3%, day 20 = +8%. Includes intraday
        swing so MFE/MAE math is non-trivial."""
        prices = {}
        base = datetime(2026, 4, 1)
        # 25 calendar days = ~18 weekdays. Index by weekdays only.
        d = base
        day_idx = 0
        targets = {
            0: 100.0,   # entry-day close
            1: 101.5,
            3: 102.0,
            5: 103.0,
            10: 105.0,
            20: 108.0,
        }
        intraday_swings = {
            1: (102.5, 99.5),   # high/low day 1: MFE candidate
            5: (104.0, 102.0),
            20: (109.0, 107.5),
        }
        while day_idx <= 22:
            if d.weekday() < 5:  # skip weekends
                close = targets.get(day_idx, 100.0 + day_idx * 0.4)
                hi, lo = intraday_swings.get(day_idx, (close + 0.5, close - 0.5))
                prices[d.strftime("%Y-%m-%d")] = (close, hi, lo, close)
                day_idx += 1
            d = d + timedelta(days=1)
        return _bars(prices)

    def test_long_writes_horizon_rows_with_correct_returns(self, db):
        from ai_tracker import _measure_one_prediction
        pid = _record_pred(db, signal="BUY", price=100.0)
        # Backdate the prediction so the bars' first row IS the entry day
        pred_dict = self._pred_row(db, pid, ts_offset_days=25)
        bars = self._build_bars()
        # Re-stamp ts to align with bars start
        first_bar_date = bars.index[0].date().isoformat() + "T09:30:00"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (first_bar_date, pid),
            )
            conn.commit()
            pred_dict["timestamp"] = first_bar_date
            conn.row_factory = sqlite3.Row
            written = _measure_one_prediction(
                conn, pred_dict, bars, db, "2026-05-20T12:00:00",
            )
            conn.commit()
            rows = conn.execute(
                "SELECT horizon_days, return_pct, mfe_pct, mae_pct, "
                "outcome_class "
                "FROM ai_prediction_outcomes WHERE prediction_id = ? "
                "ORDER BY horizon_days",
                (pid,),
            ).fetchall()

        assert written == 5, f"Expected 5 horizon rows, got {written}"
        horizons = {r[0]: r for r in rows}
        assert set(horizons.keys()) == {1, 3, 5, 10, 20}
        # 1d horizon: bar close = 101.5 → +1.5% return → "win"
        assert abs(horizons[1][1] - 1.5) < 0.01
        assert horizons[1][4] == "win"
        # 20d horizon: bar close = 108.0 → +8% return → "big_win"
        assert abs(horizons[20][1] - 8.0) < 0.01
        assert horizons[20][4] == "big_win"
        # MFE for the 20d horizon should be at least +9% (from the
        # day-20 intraday high of 109.0)
        assert horizons[20][2] >= 9.0 - 0.01
        # MAE for the 20d horizon: lowest intraday low is 99.5 on day 1
        # → MAE = -(100 - 99.5)/100 = -0.5%
        assert horizons[20][3] <= -0.5 + 0.01

    def test_short_inverts_return_and_mfe_sign(self, db):
        """For a SHORT, positive MFE must still mean 'the prediction
        was right at some point' — i.e., price went DOWN. That requires
        MFE = (entry - lowest_low)/entry, not (highest - entry)/entry."""
        from ai_tracker import _measure_one_prediction
        pid = _record_pred(db, signal="SHORT", price=100.0)
        # Synthesize bars: price drops to 95 by day 5, recovers to 102 by day 20
        prices = {}
        base = datetime(2026, 4, 1)
        d = base
        day_idx = 0
        targets = {0: 100.0, 1: 99.0, 3: 96.0, 5: 95.0, 10: 98.0, 20: 102.0}
        swings = {
            1: (100.5, 98.0),
            5: (96.0, 94.5),    # biggest favorable drop
            20: (103.0, 101.0), # adverse: rally above entry
        }
        while day_idx <= 22:
            if d.weekday() < 5:
                close = targets.get(day_idx, 100.0 - day_idx * 0.1)
                hi, lo = swings.get(day_idx, (close + 0.5, close - 0.5))
                prices[d.strftime("%Y-%m-%d")] = (close, hi, lo, close)
                day_idx += 1
            d = d + timedelta(days=1)
        bars = _bars(prices)
        first_bar_date = bars.index[0].date().isoformat() + "T09:30:00"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (first_bar_date, pid),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            pred_dict = {
                "id": pid, "symbol": "AAPL", "predicted_signal": "SHORT",
                "price_at_prediction": 100.0, "timestamp": first_bar_date,
            }
            _measure_one_prediction(
                conn, pred_dict, bars, db, "2026-05-20T12:00:00",
            )
            conn.commit()
            r20 = conn.execute(
                "SELECT return_pct, mfe_pct, mae_pct, outcome_class "
                "FROM ai_prediction_outcomes "
                "WHERE prediction_id = ? AND horizon_days = 20",
                (pid,),
            ).fetchone()
            r5 = conn.execute(
                "SELECT return_pct, mfe_pct "
                "FROM ai_prediction_outcomes "
                "WHERE prediction_id = ? AND horizon_days = 5",
                (pid,),
            ).fetchone()

        # 20d: price ended at 102 (up 2% from 100); SHORT loses 2%
        # → return_pct = -2.0, outcome_class = "loss"
        assert abs(r20[0] - (-2.0)) < 0.01
        assert r20[3] == "loss"
        # MFE for the 20d window: price hit 94.5 on day 5 (favorable
        # for a short). MFE = (100 - 94.5)/100 = +5.5%
        assert r20[1] >= 5.5 - 0.01, (
            f"MFE for short should be positive when price dropped; "
            f"got {r20[1]}. If this fails, the sign convention is "
            "inverted — fix _measure_one_prediction so MFE is "
            "DIRECTIONAL not just (max-entry)."
        )
        # 5d window: short gained 5% (price closed at 95)
        assert abs(r5[0] - 5.0) < 0.01

    def test_idempotent_no_duplicate_rows(self, db):
        from ai_tracker import _measure_one_prediction
        pid = _record_pred(db, signal="BUY", price=100.0)
        bars = self._build_bars()
        first_bar_date = bars.index[0].date().isoformat() + "T09:30:00"
        pred_dict = {
            "id": pid, "symbol": "AAPL", "predicted_signal": "BUY",
            "price_at_prediction": 100.0, "timestamp": first_bar_date,
        }
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (first_bar_date, pid),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            first = _measure_one_prediction(
                conn, pred_dict, bars, db, "2026-05-20T12:00:00")
            conn.commit()
            second = _measure_one_prediction(
                conn, pred_dict, bars, db, "2026-05-20T13:00:00")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM ai_prediction_outcomes "
                "WHERE prediction_id = ?", (pid,),
            ).fetchone()[0]
        assert first == 5
        assert second == 0, "Re-run wrote new rows; UNIQUE constraint not enforced"
        assert count == 5

    def test_skips_horizons_beyond_available_bars(self, db):
        """If only 6 bars are available, only horizons 1, 3, 5 get
        written; 10 and 20 are skipped for now (will be filled on a
        later cycle when more bars accumulate)."""
        from ai_tracker import _measure_one_prediction
        pid = _record_pred(db, signal="BUY", price=100.0)
        prices = {}
        base = datetime(2026, 4, 1)
        d = base; day_idx = 0
        while day_idx < 6:
            if d.weekday() < 5:
                close = 100.0 + day_idx * 0.5
                prices[d.strftime("%Y-%m-%d")] = (
                    close, close + 0.3, close - 0.3, close,
                )
                day_idx += 1
            d = d + timedelta(days=1)
        bars = _bars(prices)
        first_bar_date = bars.index[0].date().isoformat() + "T09:30:00"
        pred_dict = {
            "id": pid, "symbol": "AAPL", "predicted_signal": "BUY",
            "price_at_prediction": 100.0, "timestamp": first_bar_date,
        }
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET timestamp = ? WHERE id = ?",
                (first_bar_date, pid),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            _measure_one_prediction(
                conn, pred_dict, bars, db, "2026-05-20T12:00:00")
            conn.commit()
            horizons_written = {
                row[0] for row in conn.execute(
                    "SELECT horizon_days FROM ai_prediction_outcomes "
                    "WHERE prediction_id = ?", (pid,),
                ).fetchall()
            }
        # entry_idx=0; only horizons whose target_idx < len(bars)=6:
        # 1d → idx 1 ✓, 3d → idx 3 ✓, 5d → idx 5 ✓, 10d → idx 10 ✗, 20d → idx 20 ✗
        assert horizons_written == {1, 3, 5}


# ---------------------------------------------------------------------------
# 9-10. measure_horizon_outcomes top-level filtering
# ---------------------------------------------------------------------------

class TestMeasureHorizonOutcomesFilters:
    def test_skips_option_signals(self, db, monkeypatch):
        """Option signals have their own resolver (premium math); the
        multi-horizon stock measurer must not write rows for them."""
        from ai_tracker import measure_horizon_outcomes
        # Backdated stock + option preds
        for sig in ("BUY", "MULTILEG_OPEN", "OPTIONS", "OPTION_EXERCISE"):
            _record_pred(
                db, signal=sig, price=100.0,
                ts=(datetime.utcnow() - timedelta(days=25)).isoformat(),
            )
        # Stub bars fetch so we don't hit the network
        called_symbols = []
        def fake_bars(symbol, start, end):
            called_symbols.append(symbol)
            return pd.DataFrame()
        monkeypatch.setattr(
            "market_data.get_bars_daterange", fake_bars,
        )
        measure_horizon_outcomes(db_path=db)
        # The stock BUY for AAPL should have driven a fetch attempt;
        # the option rows must not have.
        assert called_symbols == ["AAPL"]

    def test_skips_predictions_with_full_horizon_set(self, db, monkeypatch):
        """If a prediction already has 5 outcome rows, the symbol fetch
        should be skipped — saves the bar fetch in steady state."""
        from ai_tracker import measure_horizon_outcomes, HORIZON_DAYS
        pid = _record_pred(
            db, price=100.0,
            ts=(datetime.utcnow() - timedelta(days=25)).isoformat(),
        )
        with closing(sqlite3.connect(db)) as conn:
            for h in HORIZON_DAYS:
                conn.execute(
                    "INSERT INTO ai_prediction_outcomes "
                    "(prediction_id, horizon_days, return_pct, measured_at) "
                    "VALUES (?, ?, 1.0, '2026-05-20T00:00:00')",
                    (pid, h),
                )
            conn.commit()
        called_symbols = []
        def fake_bars(symbol, start, end):
            called_symbols.append(symbol)
            return pd.DataFrame()
        monkeypatch.setattr(
            "market_data.get_bars_daterange", fake_bars,
        )
        measure_horizon_outcomes(db_path=db)
        assert called_symbols == [], (
            "Predictions with full horizon set should skip the bar "
            "fetch entirely. Got fetches for: " + str(called_symbols)
        )


# ---------------------------------------------------------------------------
# 11-13. build_training_dataset
# ---------------------------------------------------------------------------

class TestBuildTrainingDataset:
    def test_returns_per_prediction_rows_with_parsed_fields(self, db):
        from ai_tracker import build_training_dataset
        votes = [
            {"name": "rule_x", "severity": "CONFIRM", "direction": "long"},
        ]
        pid = _record_pred(db, rule_votes=votes)
        # Add an outcome row
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "INSERT INTO ai_prediction_outcomes "
                "(prediction_id, horizon_days, price_at_horizon, "
                " return_pct, return_pct_net, mfe_pct, mae_pct, "
                " outcome_class, measured_at) "
                "VALUES (?, 5, 105.0, 5.0, 4.8, 6.0, -1.0, 'big_win', "
                "        '2026-05-20T12:00:00')",
                (pid,),
            )
            conn.commit()
        ds = build_training_dataset(db_path=db)
        assert len(ds) == 1
        row = ds[0]
        assert row["id"] == pid
        assert row["rule_votes"] == votes
        assert 5 in row["outcomes"]
        assert row["outcomes"][5]["outcome_class"] == "big_win"
        assert row["outcomes"][5]["return_pct"] == 5.0

    def test_min_horizons_required_filters(self, db):
        """A prediction with 2 outcome rows should be excluded when
        min_horizons_required=5 (the trainer wants full label vectors
        for sequence-modeling)."""
        from ai_tracker import build_training_dataset
        pid = _record_pred(db)
        with closing(sqlite3.connect(db)) as conn:
            for h in (1, 5):
                conn.execute(
                    "INSERT INTO ai_prediction_outcomes "
                    "(prediction_id, horizon_days, return_pct, measured_at) "
                    "VALUES (?, ?, 1.0, '2026-05-20T00:00:00')",
                    (pid, h),
                )
            conn.commit()
        full = build_training_dataset(db_path=db, min_horizons_required=5)
        loose = build_training_dataset(db_path=db, min_horizons_required=1)
        assert len(full) == 0, (
            "min_horizons_required=5 should drop predictions with "
            "only 2 horizon rows"
        )
        assert len(loose) == 1

    def test_include_unresolved_yields_empty_outcomes(self, db):
        """For prompt-only / instruction-tuning use cases, the trainer
        can ask for ALL predictions including those with zero outcome
        rows. The outcomes dict should be empty {}, not omitted."""
        from ai_tracker import build_training_dataset
        _record_pred(db)
        ds = build_training_dataset(db_path=db, include_unresolved=True)
        assert len(ds) == 1
        assert ds[0]["outcomes"] == {}

    def test_tainted_rows_excluded_by_default(self, db):
        """The 2026-05-21 cover-classification bug persisted phantom
        equity in 17 pid16 prompts. Those rows were tagged with
        `data_quality='tainted_equity_2026_05_21'`. The fine-tune
        dataset builder must filter them out by default so training
        material stays clean."""
        from ai_tracker import build_training_dataset
        good_id = _record_pred(db, symbol="AAPL")
        bad_id = _record_pred(db, symbol="MSFT")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET data_quality = ? WHERE id = ?",
                ("tainted_equity_2026_05_21", bad_id),
            )
            conn.commit()
        ds = build_training_dataset(db_path=db, include_unresolved=True)
        ids = {row["id"] for row in ds}
        assert good_id in ids
        assert bad_id not in ids, (
            "Tainted row leaked into fine-tune dataset. The default "
            "build_training_dataset path must apply the data_quality "
            "filter — otherwise corrupt prompts pollute training."
        )

    def test_include_tainted_returns_everything(self, db):
        """The `include_tainted=True` escape hatch is for forensic
        review / repair workflows. It must return ALL rows including
        the tagged ones."""
        from ai_tracker import build_training_dataset
        good_id = _record_pred(db, symbol="AAPL")
        bad_id = _record_pred(db, symbol="MSFT")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET data_quality = ? WHERE id = ?",
                ("tainted_equity_2026_05_21", bad_id),
            )
            conn.commit()
        ds = build_training_dataset(
            db_path=db, include_unresolved=True, include_tainted=True,
        )
        ids = {row["id"] for row in ds}
        assert good_id in ids
        assert bad_id in ids

    def test_tainted_marker_value_not_hardcoded(self, db):
        """The filter is `data_quality IS NULL` — any non-NULL marker
        excludes the row, not just the specific 2026-05-21 string. This
        means future bug-tag values automatically get the same
        defense-in-depth without code changes."""
        from ai_tracker import build_training_dataset
        good_id = _record_pred(db, symbol="AAPL")
        future_tag_id = _record_pred(db, symbol="MSFT")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute(
                "UPDATE ai_predictions SET data_quality = ? WHERE id = ?",
                ("hypothetical_future_bug_2027_01_01", future_tag_id),
            )
            conn.commit()
        ds = build_training_dataset(db_path=db, include_unresolved=True)
        ids = {row["id"] for row in ds}
        assert good_id in ids
        assert future_tag_id not in ids
