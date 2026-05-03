"""Item 3a — Google Trends + Wikipedia attention signals.

These talk to external services (Google Trends via pytrends, Wikipedia
via Wikimedia REST). All HTTP is mocked so the test is offline,
deterministic, and fast.
"""
from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _isolate_alt_cache(tmp_path, monkeypatch):
    """Point the alt-data cache at a tmp DB so tests don't pollute
    each other's cache hits."""
    import alternative_data
    monkeypatch.setattr(alternative_data, "_DB_PATH", str(tmp_path / "alt.db"))
    monkeypatch.setattr(alternative_data, "_table_ensured", False)
    yield


# ---------------------------------------------------------------------------
# Google Trends
# ---------------------------------------------------------------------------

class TestGoogleTrendsSignal:
    def _mock_pytrends(self, weekly_index_series):
        """Build a fake pytrends object whose interest_over_time
        returns the given pandas Series indexed by week."""
        import pandas as pd
        idx = pd.date_range("2026-02-01", periods=len(weekly_index_series),
                              freq="W")
        df = pd.DataFrame(
            {'"AAPL"': weekly_index_series, "isPartial": [False] * len(weekly_index_series)},
            index=idx,
        )
        fake = MagicMock()
        fake.build_payload = MagicMock()
        fake.interest_over_time = MagicMock(return_value=df)
        return fake

    def test_returns_has_data_false_when_pytrends_fails(self):
        from alternative_data import get_google_trends_signal
        with patch("pytrends.request.TrendReq",
                    side_effect=Exception("rate limited")):
            r = get_google_trends_signal("AAPL")
        assert r["has_data"] is False
        assert r["trend_z_score"] is None

    def test_rising_trend_detected(self):
        from alternative_data import get_google_trends_signal
        # Direction detector looks at the last 8 weeks: first half
        # avg vs second half avg. Make the latest 4 weeks notably
        # higher than the previous 4 weeks.
        series = [50] * 44 + [40, 40, 40, 40, 80, 80, 80, 80]
        with patch("pytrends.request.TrendReq",
                    return_value=self._mock_pytrends(series)):
            r = get_google_trends_signal("AAPL")
        assert r["has_data"] is True
        assert r["trend_z_score"] > 0
        assert r["trend_direction"] == "rising"
        assert r["current_index"] == 80

    def test_falling_trend_detected(self):
        from alternative_data import get_google_trends_signal
        series = [50] * 44 + [80, 80, 80, 80, 40, 40, 40, 40]
        with patch("pytrends.request.TrendReq",
                    return_value=self._mock_pytrends(series)):
            r = get_google_trends_signal("AAPL")
        assert r["has_data"] is True
        assert r["trend_z_score"] < 0
        assert r["trend_direction"] == "falling"

    def test_flat_trend_detected(self):
        from alternative_data import get_google_trends_signal
        series = [50] * 52
        with patch("pytrends.request.TrendReq",
                    return_value=self._mock_pytrends(series)):
            r = get_google_trends_signal("AAPL")
        assert r["trend_direction"] == "flat"

    def test_crypto_skipped(self):
        from alternative_data import get_google_trends_signal
        r = get_google_trends_signal("BTC/USD")
        assert r["has_data"] is False
        assert r["is_crypto"] is True

    def test_caches_result(self):
        from alternative_data import get_google_trends_signal
        # First call populates cache; second call returns from cache
        # without hitting pytrends again.
        series = [50] * 52
        mock = self._mock_pytrends(series)
        with patch("pytrends.request.TrendReq", return_value=mock):
            get_google_trends_signal("MSFT")
            n_calls_first = mock.interest_over_time.call_count
            get_google_trends_signal("MSFT")   # should hit cache
            assert mock.interest_over_time.call_count == n_calls_first


# ---------------------------------------------------------------------------
# Wikipedia page-views
# ---------------------------------------------------------------------------

class TestWikipediaSignal:
    def _mock_pageview_response(self, daily_views):
        """Build a fake urlopen() context manager response."""
        items = [
            {"timestamp": f"{d:08d}", "views": v}
            for d, v in enumerate(daily_views, start=20260201)
        ]
        body = json.dumps({"items": items}).encode("utf-8")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(
            read=MagicMock(return_value=body)
        ))
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    def test_uses_override_for_known_ticker(self):
        from alternative_data import _resolve_wikipedia_article
        assert _resolve_wikipedia_article("AAPL") == "Apple_Inc."
        assert _resolve_wikipedia_article("MSFT") == "Microsoft"

    def test_falls_back_to_search_for_unknown(self):
        from alternative_data import _resolve_wikipedia_article
        # Mock the OpenSearch response
        body = json.dumps([
            "TICKER stock", ["Some_Company"], [""], [""],
        ]).encode("utf-8")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(
            read=MagicMock(return_value=body)
        ))
        cm.__exit__ = MagicMock(return_value=None)
        with patch("urllib.request.urlopen", return_value=cm):
            result = _resolve_wikipedia_article("ZZZUNKNOWN")
        assert result == "Some_Company"

    def test_returns_has_data_false_on_short_history(self):
        from alternative_data import get_wikipedia_pageviews_signal
        # Only 5 days of data — below the 14-day minimum
        cm = self._mock_pageview_response([100] * 5)
        with patch("urllib.request.urlopen", return_value=cm):
            r = get_wikipedia_pageviews_signal("AAPL")
        assert r["has_data"] is False

    def test_z_score_computed_on_normal_history(self):
        from alternative_data import get_wikipedia_pageviews_signal
        # 90 days: 80 days at ~1000 views, last 7 days at ~3000 → spike
        views = [1000] * 83 + [3000] * 7
        cm = self._mock_pageview_response(views)
        with patch("urllib.request.urlopen", return_value=cm):
            r = get_wikipedia_pageviews_signal("AAPL")
        assert r["has_data"] is True
        assert r["pageview_z_score"] > 1.5     # significant spike
        assert r["pageview_spike_flag"] is True
        assert r["current_7d_avg"] == 3000
        assert r["article"] == "Apple_Inc."

    def test_no_spike_when_views_steady(self):
        from alternative_data import get_wikipedia_pageviews_signal
        views = [1000] * 90
        cm = self._mock_pageview_response(views)
        with patch("urllib.request.urlopen", return_value=cm):
            r = get_wikipedia_pageviews_signal("MSFT")
        assert r["has_data"] is True
        assert r["pageview_spike_flag"] is False
        # Steady → z near 0; std is zero so z = 0 by safe-divide
        assert abs(r["pageview_z_score"]) < 0.5

    def test_crypto_skipped(self):
        from alternative_data import get_wikipedia_pageviews_signal
        r = get_wikipedia_pageviews_signal("ETH/USD")
        assert r["has_data"] is False
        assert r["is_crypto"] is True

    def test_returns_has_data_false_when_http_fails(self):
        from alternative_data import get_wikipedia_pageviews_signal
        with patch("urllib.request.urlopen",
                    side_effect=Exception("404")):
            r = get_wikipedia_pageviews_signal("AAPL")
        assert r["has_data"] is False
        assert r["pageview_z_score"] is None
