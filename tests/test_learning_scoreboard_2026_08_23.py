"""Learning Scoreboard engine (docs/25 step 2).

Pins the register definitions in calculation_verification/learning.md:
ISO-week bucketing by decision time, the directional hit rule,
calibration (high/low band + Brier), HOLD quality, weekly equity
returns with replicate spread, SPY excess, the benchmark band, and
the never-0% rule.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import learning_scoreboard as ls  # noqa: E402


def _profile_db(tmp_path, pid, preds=(), snaps=()):
    path = str(tmp_path / f"quantopsai_profile_{pid}.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE ai_predictions (
        id INTEGER PRIMARY KEY, timestamp TEXT, predicted_signal TEXT,
        confidence REAL, actual_return_pct REAL, status TEXT,
        data_quality TEXT)""")
    conn.execute("CREATE TABLE daily_snapshots (date TEXT, equity REAL)")
    conn.executemany(
        "INSERT INTO ai_predictions (timestamp, predicted_signal, confidence,"
        " actual_return_pct, status, data_quality) VALUES (?,?,?,?,?,?)", preds)
    conn.executemany("INSERT INTO daily_snapshots VALUES (?,?)", snaps)
    conn.commit()
    conn.close()
    return path


def _p(ts, sig, conf, ret, status="resolved", dq=None):
    return (ts, sig, conf, ret, status, dq)


class TestWeekly:
    def test_iso_week(self):
        assert ls.iso_week("2026-08-24 14:00:00") == "2026-W35"
        assert ls.iso_week("2026-08-23") == "2026-W34"   # Sunday belongs to W34

    def test_hit_rule_and_calibration(self, tmp_path):
        db = _profile_db(tmp_path, 1, preds=[
            _p("2026-08-24 10:00", "BUY", 80, 2.0),        # hit, high conf
            _p("2026-08-24 10:00", "STRONG_BUY", 90, -1.0),  # miss, high conf
            _p("2026-08-25 10:00", "SHORT", 40, -3.0),     # hit, low conf
            _p("2026-08-25 10:00", "SELL", 40, 1.0),       # miss, low conf
            _p("2026-08-25 10:00", "HOLD", 50, 0.5),       # hold ok
            _p("2026-08-25 10:00", "HOLD", 50, 4.0),       # hold missed a move
            _p("2026-08-25 10:00", "BUY", 99, 5.0, status="pending"),
            _p("2026-08-25 10:00", "BUY", 99, -5.0, dq="bad"),
        ])
        w = ls.profile_weekly_predictions(db)["2026-W35"]
        assert (w["n"], w["hits"]) == (4, 2)
        assert (w["hi_n"], w["hi_hits"], w["lo_n"], w["lo_hits"]) == (2, 1, 2, 1)
        assert (w["hold_n"], w["hold_ok"]) == (2, 1)
        fin = ls._finalize_week(w)
        assert fin["hit_rate"] == 50.0 and fin["hold_quality"] == 50.0
        assert fin["thin"] is True

    def test_brier_extremes(self, tmp_path):
        perfect = _profile_db(tmp_path, 2, preds=[
            _p("2026-08-24 10:00", "BUY", 100, 1.0)] * 3)
        coin = _profile_db(tmp_path, 3, preds=[
            _p("2026-08-24 10:00", "BUY", 50, 1.0)] * 3)
        assert ls._finalize_week(ls.profile_weekly_predictions(perfect)["2026-W35"])["brier"] == 0.0
        assert ls._finalize_week(ls.profile_weekly_predictions(coin)["2026-W35"])["brier"] == 0.25

    def test_weekly_equity_return_uses_last_snapshot_of_week(self, tmp_path):
        db = _profile_db(tmp_path, 4, snaps=[
            ("2026-08-17", 100.0), ("2026-08-21", 110.0),   # W34 ends 110
            ("2026-08-24", 105.0), ("2026-08-28", 121.0),   # W35 ends 121
        ])
        r = ls.profile_weekly_equity_returns(db)
        assert "2026-W34" not in r            # no prior week
        assert abs(r["2026-W35"] - 0.10) < 1e-9

    def test_spy_weekly_compounds_daily_and_is_empty_when_unavailable(self):
        fetch = lambda t, s, e: {"2026-08-24": 0.01, "2026-08-25": 0.01}
        assert abs(ls.spy_weekly_returns("a", "b", fetch=fetch)["2026-W35"]
                   - (1.01 * 1.01 - 1)) < 1e-12
        assert ls.spy_weekly_returns("a", "b", fetch=lambda *a: {}) == {}


class TestCollect:
    def _profiles(self):
        return [
            {"id": 1, "name": "EXP-A1-LUNA-1", "strategy_type": "ai",
             "ai_provider": "openai", "ai_model": "gpt-5.6-luna", "enabled": 1},
            {"id": 2, "name": "EXP-A1-LUNA-2", "strategy_type": "ai",
             "ai_provider": "openai", "ai_model": "gpt-5.6-luna", "enabled": 1},
            {"id": 9, "name": "old-baseline", "strategy_type": "buy_hold",
             "ai_provider": "google", "ai_model": "x", "enabled": 1},
        ]

    def test_arms_aggregate_counters_and_show_replicate_spread(self, tmp_path):
        _profile_db(tmp_path, 1,
                    preds=[_p("2026-08-24 10:00", "BUY", 80, 1.0)] * 10,
                    snaps=[("2026-08-21", 100.0), ("2026-08-28", 110.0)])
        _profile_db(tmp_path, 2,
                    preds=[_p("2026-08-24 10:00", "BUY", 80, -1.0)] * 10,
                    snaps=[("2026-08-21", 100.0), ("2026-08-28", 90.0)])
        board = ls.collect_scoreboard(
            self._profiles(),
            lambda pid: str(tmp_path / f"quantopsai_profile_{pid}.db"),
            spy_fetch=lambda t, s, e: {"2026-08-24": 0.02},
        )
        assert list(board["arms"]) == ["openai:gpt-5.6-luna"]   # baseline excluded
        arm = board["arms"]["openai:gpt-5.6-luna"]
        w = arm["weeks"]["2026-W35"]
        assert w["n"] == 20 and w["hit_rate"] == 50.0 and w["thin"] is False
        assert w["equity_ret"] == 0.0 and (w["equity_ret_min"], w["equity_ret_max"]) == (-10.0, 10.0)
        assert w["excess"] == -2.0                       # 0% − SPY 2%
        reps = {r["name"]: r["weeks"]["2026-W35"]["hit_rate"] for r in arm["replicates"]}
        assert reps == {"EXP-A1-LUNA-1": 100.0, "EXP-A1-LUNA-2": 0.0}

    def test_never_zero_for_unmeasured(self, tmp_path):
        _profile_db(tmp_path, 1, preds=[_p("2026-08-24 10:00", "HOLD", 50, 0.1)])
        _profile_db(tmp_path, 2)
        board = ls.collect_scoreboard(
            self._profiles()[:2],
            lambda pid: str(tmp_path / f"quantopsai_profile_{pid}.db"),
            spy_fetch=lambda *a: {})
        w = board["arms"]["openai:gpt-5.6-luna"]["weeks"]["2026-W35"]
        assert w["hit_rate"] is None and w["brier"] is None
        assert w["hold_quality"] == 100.0 and w["hold_n"] == 1
        assert w["equity_ret"] is None and w["excess"] is None

    def test_benchmark_band_from_series(self, tmp_path):
        _profile_db(tmp_path, 1, snaps=[("2026-08-21", 100.0), ("2026-08-28", 101.0)])
        bench = [
            {"profile_name": "BENCH-BuyHoldSPY", "strategy_type": "buy_hold",
             "points": [{"date": "2026-08-21", "return_pct": 0.0},
                        {"date": "2026-08-28", "return_pct": 2.0}]},
            {"profile_name": "BENCH-Random-01", "strategy_type": "random",
             "points": [{"date": "2026-08-21", "return_pct": 0.0},
                        {"date": "2026-08-28", "return_pct": -3.0}]},
            {"profile_name": "BENCH-Random-02", "strategy_type": "random",
             "points": [{"date": "2026-08-21", "return_pct": 0.0},
                        {"date": "2026-08-28", "return_pct": 5.0}]},
        ]
        board = ls.collect_scoreboard(
            self._profiles()[:1],
            lambda pid: str(tmp_path / f"quantopsai_profile_{pid}.db"),
            benchmark_series=bench, spy_fetch=lambda *a: {})
        row = board["benchmarks"]["2026-W35"]
        assert row["buy_hold"] == 2.0
        assert (row["random_min"], row["random_median"], row["random_max"], row["random_n"]) == (-3.0, 1.0, 5.0, 2)

    def test_tuner_scorecard_counts(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tuning_history (profile_id INTEGER, outcome_after TEXT)")
        conn.executemany("INSERT INTO tuning_history VALUES (?,?)", [
            (1, "improved"), (1, "improved"), (2, "worsened"), (2, "unchanged"),
            (2, None), (3, "improved")])
        sc = ls.tuner_scorecard(conn, [1, 2])
        assert sc == {"total": 5, "improved": 2, "worsened": 1, "unchanged": 1,
                      "pending": 1, "other": 0, "improved_share": 66.7, "judged": 3}


class TestPageSmoke:
    """The page must render through the real app for a logged-in user
    (UI surfaces need a happy-path smoke test, not just compiling)."""

    def test_learning_page_renders_empty_state(self, tmp_main_db, tmp_path,
                                               monkeypatch):
        monkeypatch.chdir(tmp_path)
        import config
        config.DB_PATH = str(tmp_main_db)
        from models import create_user
        from app import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        create_user("t@t.com", "password123", "T", is_admin=True)
        client = app.test_client()
        client.post("/login", data={"email": "t@t.com",
                                    "password": "password123"},
                    follow_redirects=True)
        r = client.get("/learning")
        assert r.status_code == 200
        html = r.data.decode()
        assert "Learning Scoreboard" in html
        assert "No arms yet" in html
        assert 'href="/learning"' in html


class TestRouteWiring:
    def test_route_and_nav_exist(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        views = open(os.path.join(root, "views.py")).read()
        assert '@views_bp.route("/learning")' in views
        assert "collect_scoreboard" in views[views.index("def learning_page"):][:2500]
        base = open(os.path.join(root, "templates", "base.html")).read()
        assert 'href="/learning"' in base

    def test_template_has_explicit_empty_states(self):
        root = os.path.join(os.path.dirname(__file__), os.pardir)
        tpl = open(os.path.join(root, "templates", "learning.html")).read()
        assert "No arms yet" in tpl
        assert "No virtual benchmark snapshots yet" in tpl
        assert "Scoreboard unavailable" in tpl
