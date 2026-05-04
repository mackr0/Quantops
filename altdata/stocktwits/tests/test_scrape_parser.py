"""Parser tests for StockTwits API responses."""

from stocktwits.scrape import parse_messages_response, parse_trending_response


def _msg(id_, body, sent=None, user_id=42, user_name="trader"):
    out = {
        "id": id_,
        "body": body,
        "created_at": "2026-04-25T10:00:00Z",
        "user": {"id": user_id, "username": user_name},
        "likes": {"total": 5},
    }
    if sent:
        out["entities"] = {"sentiment": {"basic": sent}}
    return out


class TestParseMessages:
    def test_basic_message(self):
        data = {"messages": [_msg(1, "going to the moon")]}
        out = parse_messages_response(data)
        assert len(out) == 1
        m = out[0]
        assert m["msg_id"] == 1
        assert m["body"] == "going to the moon"
        assert m["user_id"] == 42
        assert m["user_name"] == "trader"

    def test_bullish_sentiment_extracted(self):
        data = {"messages": [_msg(1, "x", sent="Bullish")]}
        out = parse_messages_response(data)
        assert out[0]["sentiment"] == "bullish"

    def test_bearish_sentiment_extracted(self):
        data = {"messages": [_msg(1, "x", sent="Bearish")]}
        out = parse_messages_response(data)
        assert out[0]["sentiment"] == "bearish"

    def test_no_sentiment_is_none(self):
        data = {"messages": [_msg(1, "neutral take")]}
        out = parse_messages_response(data)
        assert out[0]["sentiment"] is None

    def test_unknown_sentiment_is_none(self):
        """Defensive: if StockTwits ever adds a third sentiment, default
        to None rather than emit garbage."""
        data = {"messages": [_msg(1, "x", sent="Unknown")]}
        out = parse_messages_response(data)
        assert out[0]["sentiment"] is None

    def test_likes_default_zero(self):
        m = _msg(1, "x")
        del m["likes"]
        data = {"messages": [m]}
        out = parse_messages_response(data)
        assert out[0]["like_count"] == 0

    def test_empty_response(self):
        assert parse_messages_response({}) == []
        assert parse_messages_response({"messages": []}) == []
        assert parse_messages_response(None) == []

    def test_missing_user_safe(self):
        m = _msg(1, "x")
        del m["user"]
        data = {"messages": [m]}
        out = parse_messages_response(data)
        assert out[0]["user_id"] is None
        assert out[0]["user_name"] is None


class TestParseTrending:
    def test_extracts_ticker_list(self):
        data = {
            "symbols": [
                {"symbol": "NVDA", "title": "Nvidia"},
                {"symbol": "tsla"},   # lowercase normalized
                {"symbol": "AAPL"},
            ],
        }
        out = parse_trending_response(data)
        assert out == ["NVDA", "TSLA", "AAPL"]

    def test_skips_missing_symbol(self):
        data = {"symbols": [{"title": "no ticker"}, {"symbol": "X"}]}
        out = parse_trending_response(data)
        assert out == ["X"]

    def test_empty(self):
        assert parse_trending_response({}) == []
        assert parse_trending_response({"symbols": []}) == []
        assert parse_trending_response(None) == []
