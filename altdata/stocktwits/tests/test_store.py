"""Storage tests — schema, message dedup, daily aggregation, trending."""

import sqlite3

import pytest

from stocktwits.store import (
    _apply_migrations,
    connect,
    init_db,
    insert_message,
    insert_raw_response,
    insert_trending_snapshot,
    latest_trending,
    query_daily_sentiment,
    query_messages,
    upsert_daily_sentiment,
)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "stocktwits.db"
    monkeypatch.setattr("stocktwits.store.DEFAULT_DB_PATH", str(db))
    init_db(str(db))
    return str(db)


class TestSchema:
    def test_tables_exist(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {"messages", "ticker_sentiment_daily", "trending_snapshots",
                "raw_responses", "scrape_runs"}.issubset(names)


class TestMigrations:
    def test_idempotent(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        _apply_migrations(conn)
        _apply_migrations(conn)
        conn.close()


class TestMessages:
    def test_insert_returns_true(self, tmp_db):
        with connect(tmp_db) as conn:
            assert insert_message(
                conn, msg_id=1, ticker="NVDA", body="bullish on this",
                created_at="2026-04-25T10:00:00Z", sentiment="bullish",
            ) is True

    def test_duplicate_msg_id_returns_false(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="NVDA", body="x",
                           created_at="2026-04-25T10:00:00Z")
            assert insert_message(
                conn, msg_id=1, ticker="NVDA", body="x",
                created_at="2026-04-25T10:00:00Z",
            ) is False

    def test_query_by_ticker(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="NVDA", body="A",
                           created_at="2026-04-25T10:00:00Z")
            insert_message(conn, msg_id=2, ticker="MSFT", body="B",
                           created_at="2026-04-25T11:00:00Z")
            rows = query_messages(conn, ticker="NVDA")
            assert len(rows) == 1
            assert rows[0]["ticker"] == "NVDA"

    def test_query_by_sentiment(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="NVDA", body="up",
                           sentiment="bullish",
                           created_at="2026-04-25T10:00:00Z")
            insert_message(conn, msg_id=2, ticker="NVDA", body="down",
                           sentiment="bearish",
                           created_at="2026-04-25T11:00:00Z")
            assert len(query_messages(conn, sentiment="bullish")) == 1


class TestDailyAggregation:
    def test_aggregate_recomputes_correctly(self, tmp_db):
        """upsert_daily_sentiment recomputes from messages, not from prior
        aggregate value — so adding more messages updates the row."""
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="NVDA", body="up",
                           sentiment="bullish",
                           created_at="2026-04-25T10:00:00Z")
            insert_message(conn, msg_id=2, ticker="NVDA", body="up",
                           sentiment="bullish",
                           created_at="2026-04-25T11:00:00Z")
            insert_message(conn, msg_id=3, ticker="NVDA", body="down",
                           sentiment="bearish",
                           created_at="2026-04-25T12:00:00Z")
            agg = upsert_daily_sentiment(conn, "NVDA", "2026-04-25")
        assert agg["n_messages"] == 3
        assert agg["n_bullish"] == 2
        assert agg["n_bearish"] == 1
        assert abs(agg["net_sentiment"] - (1/3)) < 1e-9

    def test_neutral_messages_count_separately(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="X", body="a",
                           sentiment=None, created_at="2026-04-25T10:00:00Z")
            insert_message(conn, msg_id=2, ticker="X", body="b",
                           sentiment="bullish",
                           created_at="2026-04-25T11:00:00Z")
            agg = upsert_daily_sentiment(conn, "X", "2026-04-25")
        assert agg["n_neutral"] == 1
        assert agg["n_bullish"] == 1
        assert agg["n_messages"] == 2

    def test_aggregate_re_run_overwrites(self, tmp_db):
        """Calling upsert twice on same (ticker, date) doesn't double-count."""
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="X", body="a",
                           sentiment="bullish",
                           created_at="2026-04-25T10:00:00Z")
            upsert_daily_sentiment(conn, "X", "2026-04-25")
            agg2 = upsert_daily_sentiment(conn, "X", "2026-04-25")
            assert agg2["n_messages"] == 1   # not 2

    def test_query_daily_sentiment(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_message(conn, msg_id=1, ticker="X", body="a",
                           sentiment="bullish",
                           created_at="2026-04-25T10:00:00Z")
            upsert_daily_sentiment(conn, "X", "2026-04-25")
            rows = query_daily_sentiment(conn, ticker="X")
            assert len(rows) == 1
            assert rows[0]["n_bullish"] == 1


class TestTrendingSnapshots:
    def test_insert_preserves_rank_order(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_trending_snapshot(
                conn, "2026-04-25 10:00:00",
                ["NVDA", "AAPL", "TSLA", "MSFT"],
            )
            rows = latest_trending(conn)
            assert [r["ticker"] for r in rows] == ["NVDA", "AAPL", "TSLA", "MSFT"]
            assert [r["rank"] for r in rows] == [1, 2, 3, 4]

    def test_latest_returns_most_recent_only(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_trending_snapshot(
                conn, "2026-04-25 09:00:00", ["OLD1", "OLD2"],
            )
            insert_trending_snapshot(
                conn, "2026-04-25 10:00:00", ["NEW1", "NEW2", "NEW3"],
            )
            rows = latest_trending(conn)
            tickers = {r["ticker"] for r in rows}
            assert tickers == {"NEW1", "NEW2", "NEW3"}


class TestRawResponses:
    def test_insert(self, tmp_db):
        with connect(tmp_db) as conn:
            insert_raw_response(conn, "streams/symbol",
                                 payload='{"messages":[]}',
                                 request_params={"symbol": "NVDA"})
            row = conn.execute(
                "SELECT endpoint, payload_text FROM raw_responses"
            ).fetchone()
            assert row["endpoint"] == "streams/symbol"
            assert "messages" in row["payload_text"]
