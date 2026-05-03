"""Item 3a — App Store ranking signal tests."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _isolate_alt_cache(tmp_path, monkeypatch):
    import alternative_data
    monkeypatch.setattr(alternative_data, "_DB_PATH", str(tmp_path / "alt.db"))
    monkeypatch.setattr(alternative_data, "_table_ensured", False)
    # Reset the in-memory chart cache between tests
    monkeypatch.setattr(alternative_data, "_RANKING_CHART_CACHE", {})
    yield


def _mock_apple_chart(rankings):
    """Build a mock urlopen response. `rankings` is list of dicts with
    {rank, name, app_id} (or just {name, app_id} — rank inferred)."""
    entries = []
    for i, r in enumerate(rankings, start=1):
        entries.append({
            "im:name": {"label": r["name"]},
            "id": {"attributes": {"im:id": str(r["app_id"])}},
        })
    body = json.dumps({"feed": {"entry": entries}}).encode("utf-8")
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(
        read=MagicMock(return_value=body),
    ))
    cm.__exit__ = MagicMock(return_value=None)
    return cm


class TestAppStoreRanking:
    def test_unknown_ticker_returns_no_data(self):
        from alternative_data import get_app_store_ranking
        r = get_app_store_ranking("ZZZUNKNOWN")
        assert r["has_data"] is False
        assert r["no_known_app"] is True

    def test_crypto_skipped(self):
        from alternative_data import get_app_store_ranking
        r = get_app_store_ranking("BTC/USD")
        assert r["has_data"] is False
        assert r["is_crypto"] is True

    def test_uber_in_top_grossing(self):
        from alternative_data import get_app_store_ranking, APP_STORE_TICKER_OVERRIDES
        uber_app_id = APP_STORE_TICKER_OVERRIDES["UBER"][0][1]
        # Uber app at rank 5 in grossing chart
        grossing_chart = [
            {"name": "App1", "app_id": 100},
            {"name": "App2", "app_id": 200},
            {"name": "App3", "app_id": 300},
            {"name": "App4", "app_id": 400},
            {"name": "Uber",  "app_id": uber_app_id},
        ]
        free_chart = [
            {"name": "Other", "app_id": 999},
        ]
        # Mock both chart fetches via the urlopen call
        call_count = [0]
        def _mock_urlopen(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_apple_chart(grossing_chart)
            return _mock_apple_chart(free_chart)
        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            r = get_app_store_ranking("UBER")
        assert r["has_data"] is True
        assert r["best_grossing_rank"] == 5
        assert r["best_free_rank"] is None  # Uber not in free chart mock

    def test_takes_lowest_rank_across_multiple_apps(self):
        """META has Instagram + Facebook + Threads. Best rank wins."""
        from alternative_data import get_app_store_ranking, APP_STORE_TICKER_OVERRIDES
        ig_id = APP_STORE_TICKER_OVERRIDES["META"][0][1]
        fb_id = APP_STORE_TICKER_OVERRIDES["META"][1][1]
        chart = [
            {"name": "Instagram", "app_id": ig_id},   # rank 1
            {"name": "Other",     "app_id": 999},     # rank 2
            {"name": "Facebook",  "app_id": fb_id},   # rank 3
        ]
        # Both grossing and free return the same chart for simplicity
        with patch("urllib.request.urlopen",
                    return_value=_mock_apple_chart(chart)):
            r = get_app_store_ranking("META")
        assert r["has_data"] is True
        # Best is Instagram at rank 1 (Facebook at 3, Threads not in chart)
        assert r["best_grossing_rank"] == 1
        assert len(r["apps"]) == 3
        # Each app entry has its own grossing_rank field
        ig_entry = next(a for a in r["apps"] if a["name"] == "Instagram")
        assert ig_entry["grossing_rank"] == 1

    def test_app_not_in_top_200_returns_none(self):
        from alternative_data import get_app_store_ranking
        # Empty chart → app not present
        with patch("urllib.request.urlopen",
                    return_value=_mock_apple_chart([])):
            r = get_app_store_ranking("LYFT")
        assert r["has_data"] is False
        assert r["best_grossing_rank"] is None

    def test_http_failure_returns_no_data_gracefully(self):
        from alternative_data import get_app_store_ranking
        with patch("urllib.request.urlopen",
                    side_effect=Exception("403")):
            r = get_app_store_ranking("UBER")
        assert r["has_data"] is False
