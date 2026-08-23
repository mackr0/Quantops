"""In-context learning: the profile's own track record in every prompt.

docs/25 step 4.2. Pins the definitions (resolved win/loss, data-quality
rows excluded, scratch outside the denominator), the sample-size rule
(n<10 buckets never show a percentage), the thin-record statement
(never a fabricated 0%), and the wiring into the batch prompt.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import calibration_block as cb  # noqa: E402


def _db(tmp_path, rows):
    path = str(tmp_path / "quantopsai_profile_900.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE ai_predictions (
        id INTEGER PRIMARY KEY, status TEXT, actual_outcome TEXT,
        actual_return_pct REAL, confidence REAL, predicted_signal TEXT,
        strategy_type TEXT, regime_at_prediction TEXT, resolved_at TEXT,
        data_quality TEXT)""")
    conn.executemany(
        "INSERT INTO ai_predictions (status, actual_outcome, actual_return_pct,"
        " confidence, predicted_signal, strategy_type, regime_at_prediction,"
        " resolved_at, data_quality) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def _row(outcome, ret, conf, sig="BUY", strat="batch_ai", regime="bull",
         resolved_at="2026-08-20 15:00:00", dq=None, status="resolved"):
    return (status, outcome, ret, conf, sig, strat, regime, resolved_at, dq)


class TestDefinitions:
    def test_thin_record_is_stated_not_zeroed(self, tmp_path):
        db = _db(tmp_path, [_row("win", 2.0, 60)] * 5)
        text = cb.render_track_record(db)
        assert "only 5 resolved directional predictions" in text
        assert "%" not in text

    def test_holds_are_excluded_from_headline_and_bands(self, tmp_path):
        """HOLD rows carry confidence 0 and would flood the 0-25 band
        and the all-time line (live 2026-08-23: 2,579 of 3,700). They
        are scored only on their own 'By call' line."""
        rows = ([_row("win", 1.0, 80)] * 20
                + [_row("loss", 0.1, 0, sig="HOLD")] * 30)
        db = _db(tmp_path, rows)
        rec = cb.compute_track_record(db)
        assert rec["total"] == 20
        assert rec["overall"][:2] == (20, 20)
        bands = {lab: n for lab, n, _w, _m in rec["by_band"]}
        assert bands["0-25"] == 0 and bands["75-100"] == 20
        by_call = {lab: (n, w) for lab, n, w, _ in rec["by_signal"]}
        assert by_call["HOLD"] == (30, 0)
        text = cb.render_track_record(db)
        assert "HOLD: 0% win rate on 30" in text
        assert "All-time: 100% win rate on 20" in text

    def test_win_rate_excludes_scratch_and_data_quality_rows(self, tmp_path):
        rows = [_row("win", 3.0, 80)] * 15 + [_row("loss", -2.0, 80)] * 5
        rows += [_row("scratch", 0.1, 80)] * 10          # not in denominator
        rows += [_row("loss", -9.0, 80, dq="corrupt")] * 10  # excluded
        rows += [_row("win", 9.0, 80, status="pending")] * 10  # unresolved
        rec = cb.compute_track_record(_db(tmp_path, rows))
        assert rec["total"] == 20
        assert rec["overall"][:2] == (20, 15)
        assert abs(rec["overall"][2] - (15 * 3.0 - 5 * 2.0) / 20) < 1e-9

    def test_small_buckets_never_show_a_percentage(self, tmp_path):
        rows = [_row("win", 1.0, 80)] * 25 + [_row("loss", -1.0, 30)] * 3
        text = cb.render_track_record(_db(tmp_path, rows))
        assert "75-100→ 100% win rate on 25" in text
        assert "25-50→ n<10" in text
        assert "0-25→ n<10" in text

    def test_signal_families_collapse(self, tmp_path):
        rows = ([_row("win", 1.0, 70, sig="BUY")] * 8
                + [_row("win", 1.0, 70, sig="STRONG_BUY")] * 4
                + [_row("loss", -1.0, 70, sig="HOLD")] * 12)
        rec = cb.compute_track_record(_db(tmp_path, rows))
        by = {lab: (n, w) for lab, n, w, _ in rec["by_signal"]}
        assert by == {"BUY": (12, 12), "HOLD": (12, 0)}

    def test_recent_window_uses_resolved_at(self, tmp_path):
        import datetime as dt
        recent = (dt.datetime.utcnow() - dt.timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S")
        rows = ([_row("win", 1.0, 70, resolved_at=recent)] * 12
                + [_row("loss", -1.0, 70, resolved_at="2026-01-05 15:00:00")] * 12)
        rec = cb.compute_track_record(_db(tmp_path, rows))
        assert rec["overall"][:2] == (24, 12)
        assert rec["recent"][:2] == (12, 12)

    def test_unreadable_db_returns_empty_and_logs(self, tmp_path, caplog):
        assert cb.render_track_record(str(tmp_path / "missing.db")) == ""
        assert "could not read" in caplog.text


class TestWiring:
    def test_batch_context_carries_the_block(self):
        """trade_pipeline must put the block in the batch context and
        ai_analyst must render it — structural, so the wiring can't be
        dropped silently."""
        import inspect
        import ai_analyst
        import trade_pipeline
        tp = inspect.getsource(trade_pipeline)
        assert "calibration_block" in tp and "render_track_record" in tp
        aa = inspect.getsource(ai_analyst._build_batch_prompt)
        assert 'market_context.get("calibration_block"' in aa
