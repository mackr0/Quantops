"""2026-08-31 — the 32-hour master-lock class, pinned.

The App Store snapshot task opened a master write transaction and
called a per-ticker NETWORK fetch inside it; one wedged fetch
(urlopen's timeout does not cover DNS) held the fleet-wide master
write lock for 32 hours. Every master write failed silently; the
operator discovered it as page timeouts.

Pins: (1) the snapshot function is fetch-then-write — no sqlite
connection may be open while the network fetcher runs; (2) the
issues collector's master-writability canary raises a loud ERROR
finding when the master is unwritable and stays silent when healthy.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


class TestFetchThenWrite:
    def test_no_connection_open_during_fetches(self, tmp_path,
                                               monkeypatch):
        """Behavioral pin: the network fetcher must never run while a
        DB connection is held by the snapshot function."""
        import alternative_data as ad
        db = str(tmp_path / "master.db")
        monkeypatch.setattr(ad, "_DB_PATH", db)
        open_conns = []
        real_connect = sqlite3.connect

        def tracking_connect(*a, **k):
            conn = real_connect(*a, **k)
            open_conns.append(conn)
            return conn

        fetch_seen_open = []

        def fake_ranking(ticker):
            # A connection is "held" if any tracked conn is still open.
            held = 0
            for c in open_conns:
                try:
                    c.execute("SELECT 1")
                    held += 1
                except sqlite3.ProgrammingError:
                    pass  # closed
            fetch_seen_open.append(held)
            return {"has_data": True, "best_grossing_rank": 5,
                    "best_free_rank": 9,
                    "apps": [{"name": "TestApp"}]}

        with patch.object(ad.sqlite3, "connect", tracking_connect), \
             patch.object(ad, "get_app_store_ranking", fake_ranking), \
             patch.object(ad, "APP_STORE_TICKER_OVERRIDES",
                          {"AAPL": ["x"], "RBLX": ["y"]}):
            written = ad.snapshot_app_store_rankings_for_all_tickers()
        assert written == 2
        assert fetch_seen_open and all(n == 0 for n in fetch_seen_open), (
            "a DB connection was open during a network fetch — the "
            "32-hour master-lock shape is back")

    def test_one_failing_fetch_does_not_kill_the_snapshot(
            self, tmp_path, monkeypatch):
        import alternative_data as ad
        db = str(tmp_path / "master.db")
        monkeypatch.setattr(ad, "_DB_PATH", db)

        def flaky(ticker):
            if ticker == "AAPL":
                raise OSError("wedged DNS")
            return {"has_data": True, "best_grossing_rank": 1,
                    "best_free_rank": 2, "apps": [{"name": "A"}]}

        with patch.object(ad, "get_app_store_ranking", flaky), \
             patch.object(ad, "APP_STORE_TICKER_OVERRIDES",
                          {"AAPL": ["x"], "RBLX": ["y"]}):
            written = ad.snapshot_app_store_rankings_for_all_tickers()
        assert written == 1


class TestMasterWriteCanary:
    def test_silent_when_master_writable(self, tmp_path, monkeypatch):
        import config
        from issues_collector import _collect_master_write_canary
        monkeypatch.setattr(config, "DB_PATH",
                            str(tmp_path / "master.db"))
        assert _collect_master_write_canary() == []

    def test_screams_when_master_locked(self, tmp_path, monkeypatch):
        import config
        from issues_collector import _collect_master_write_canary
        db = str(tmp_path / "master.db")
        monkeypatch.setattr(config, "DB_PATH", db)
        holder = sqlite3.connect(db)
        holder.execute("CREATE TABLE t (x)")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            rows = _collect_master_write_canary()
        finally:
            holder.rollback()
            holder.close()
        assert len(rows) == 1
        assert rows[0]["level"] == "ERROR"
        assert rows[0]["source"] == "master_writability"
        assert "UNWRITABLE" in rows[0]["message"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
