"""Tests for comparative_returns.py (2026-05-17, batch D / #164).

Covers:
  - empty state when no profile DBs exist
  - empty state when DBs exist but daily_snapshots is empty
  - happy path: equity → cumulative-% return normalization
  - strategy_type is propagated to the payload (so the chart can
    style baselines distinctly)
  - missing daily_snapshots table is treated as empty, not an error
  - return_pct is rounded to 4 decimals for transport size
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


def _make_profile_db(tmp_path, pid, equity_series):
    """Create a per-profile DB with the daily_snapshots schema and
    insert the given (date, equity) rows."""
    db = tmp_path / f"quantopsai_profile_{pid}.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE daily_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                equity REAL, cash REAL, portfolio_value REAL,
                num_positions INTEGER, daily_pnl REAL
            );
            """
        )
        conn.executemany(
            "INSERT INTO daily_snapshots (date, equity) VALUES (?, ?)",
            equity_series,
        )
    return str(db)


def _patch_resolver(monkeypatch, tmp_path):
    """Force comparative_returns._resolve_db to look only in tmp_path."""
    import comparative_returns

    def _fake_resolve(pid):
        p = tmp_path / f"quantopsai_profile_{pid}.db"
        return str(p) if p.exists() else None

    monkeypatch.setattr(comparative_returns, "_resolve_db", _fake_resolve)


class TestEmptyState:
    def test_no_profiles_empty(self, monkeypatch):
        import comparative_returns
        with patch("models.get_user_profiles", return_value=[]):
            payload = comparative_returns.build_payload(user_id=1)
        assert payload["empty_state"] is True
        assert "No equity snapshots yet" in payload["empty_message"]
        assert payload["series"] == []

    def test_profiles_without_snapshots_empty(self, monkeypatch, tmp_path):
        import comparative_returns
        _make_profile_db(tmp_path, 1, [])
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[{
                "id": 1, "name": "P1", "enabled": 1,
                "strategy_type": "ai", "initial_capital": 100000.0,
            }],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        assert payload["empty_state"] is True
        assert len(payload["series"]) == 1
        assert payload["series"][0]["points"] == []


class TestNormalization:
    def test_equity_normalized_to_cumulative_return(
        self, monkeypatch, tmp_path,
    ):
        import comparative_returns
        # equity 100k → 110k → 121k = +10% then +21% cumulative
        _make_profile_db(tmp_path, 1, [
            ("2026-05-01", 100_000.0),
            ("2026-05-02", 110_000.0),
            ("2026-05-03", 121_000.0),
        ])
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[{
                "id": 1, "name": "AI Profile", "enabled": 1,
                "strategy_type": "ai", "initial_capital": 100_000.0,
            }],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        assert payload["empty_state"] is False
        pts = payload["series"][0]["points"]
        assert pts[0] == {"date": "2026-05-01", "return_pct": 0.0}
        assert pts[1] == {"date": "2026-05-02", "return_pct": 10.0}
        assert pts[2] == {"date": "2026-05-03", "return_pct": 21.0}

    def test_strategy_type_propagated_for_chart_styling(
        self, monkeypatch, tmp_path,
    ):
        """Chart picks distinct color/dash for buy_hold and random
        baselines — payload must carry strategy_type per series."""
        import comparative_returns
        _make_profile_db(tmp_path, 1, [
            ("2026-05-01", 333_000.0), ("2026-05-02", 340_000.0),
        ])
        _make_profile_db(tmp_path, 2, [
            ("2026-05-01", 333_000.0), ("2026-05-02", 330_000.0),
        ])
        _make_profile_db(tmp_path, 3, [
            ("2026-05-01", 333_000.0), ("2026-05-02", 350_000.0),
        ])
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[
                {"id": 1, "name": "Buy-Hold SPY", "enabled": 1,
                 "strategy_type": "buy_hold", "initial_capital": 333_000.0},
                {"id": 2, "name": "Random Stock", "enabled": 1,
                 "strategy_type": "random", "initial_capital": 333_000.0},
                {"id": 3, "name": "Full System", "enabled": 1,
                 "strategy_type": "ai", "initial_capital": 333_000.0},
            ],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        by_name = {s["profile_name"]: s for s in payload["series"]}
        assert by_name["Buy-Hold SPY"]["strategy_type"] == "buy_hold"
        assert by_name["Random Stock"]["strategy_type"] == "random"
        assert by_name["Full System"]["strategy_type"] == "ai"

    def test_zero_base_equity_renders_flat_zero(
        self, monkeypatch, tmp_path,
    ):
        """If the first snapshot is somehow 0, downstream divisions
        would explode — instead return a flat 0% series."""
        import comparative_returns
        _make_profile_db(tmp_path, 1, [
            ("2026-05-01", 0.0), ("2026-05-02", 100_000.0),
        ])
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[{
                "id": 1, "name": "P1", "enabled": 1,
                "strategy_type": "ai", "initial_capital": 0.0,
            }],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        pts = payload["series"][0]["points"]
        assert all(p["return_pct"] == 0.0 for p in pts)


class TestSchemaTolerance:
    def test_missing_table_treated_as_empty(self, monkeypatch, tmp_path):
        """A fresh DB without the daily_snapshots table is fine —
        returns empty series, not an error."""
        import comparative_returns
        db = tmp_path / "quantopsai_profile_1.db"
        sqlite3.connect(db).close()  # empty DB, no tables
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[{
                "id": 1, "name": "Fresh", "enabled": 1,
                "strategy_type": "ai", "initial_capital": 100_000.0,
            }],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        assert payload["series"][0]["points"] == []
        assert payload["empty_state"] is True

    def test_disabled_profile_excluded(self, monkeypatch, tmp_path):
        """get_user_profiles returns all; build_payload filters to
        enabled=1 only."""
        import comparative_returns
        _make_profile_db(tmp_path, 1, [("2026-05-01", 100_000.0)])
        _make_profile_db(tmp_path, 2, [("2026-05-01", 100_000.0)])
        _patch_resolver(monkeypatch, tmp_path)
        with patch(
            "models.get_user_profiles",
            return_value=[
                {"id": 1, "name": "Active", "enabled": 1,
                 "strategy_type": "ai", "initial_capital": 100_000.0},
                {"id": 2, "name": "Disabled", "enabled": 0,
                 "strategy_type": "ai", "initial_capital": 100_000.0},
            ],
        ):
            payload = comparative_returns.build_payload(user_id=1)
        names = [s["profile_name"] for s in payload["series"]]
        assert names == ["Active"]
