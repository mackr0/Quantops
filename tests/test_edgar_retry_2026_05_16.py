"""EDGAR transient-error retry tests for pdufa_scraper._fetch_edgar_search.

Pre-2026-05-16 a single 500 response from EDGAR's full-text search
caused the entire PDUFA scan to silently drop that cycle's 8-K
filings (3+/day on prod). Fix: retry transient HTTP codes (5xx) and
network errors with exponential backoff; surface non-transient
codes (404, 403) loudly without retry.
"""
from __future__ import annotations

import os
import sys
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Skip the real sleeps so the test suite stays fast."""
    monkeypatch.setattr(
        "pdufa_scraper._EDGAR_RETRY_DELAYS", (0.0, 0.0, 0.0),
    )


def _mk_urlopen_returning(payloads):
    """Build a mock urlopen() that yields each payload in turn.

    A payload can be:
      - bytes → wrapped in a context-manager mock that returns that bytes
      - Exception → raised when urlopen is called
    """
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(payloads):
            raise AssertionError(
                f"urlopen called {calls['n']}× but only "
                f"{len(payloads)} payloads queued"
            )
        p = payloads[i]
        if isinstance(p, Exception):
            raise p
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=MagicMock(
            read=MagicMock(return_value=p),
        ))
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    return fake_urlopen, calls


class TestEdgarRetry:

    def test_success_first_try_no_retry(self):
        from pdufa_scraper import _fetch_edgar_search
        fake, calls = _mk_urlopen_returning([b'{"hits":{"hits":[]}}'])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {"hits": {"hits": []}}
        assert calls["n"] == 1, "no retry should fire on first-try success"

    def test_500_retries_until_success(self):
        from pdufa_scraper import _fetch_edgar_search
        # First two attempts get 500, third succeeds
        http500 = urllib.error.HTTPError(
            "https://edgar/", 500, "Internal Server Error", {}, None,
        )
        fake, calls = _mk_urlopen_returning([
            http500, http500, b'{"hits":{"hits":[1,2,3]}}',
        ])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {"hits": {"hits": [1, 2, 3]}}
        assert calls["n"] == 3, "must retry past 2× 500 to reach success"

    def test_503_is_retried(self):
        from pdufa_scraper import _fetch_edgar_search
        http503 = urllib.error.HTTPError(
            "https://edgar/", 503, "Service Unavailable", {}, None,
        )
        fake, calls = _mk_urlopen_returning([
            http503, b'{"ok": true}',
        ])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {"ok": True}
        assert calls["n"] == 2

    def test_404_NOT_retried(self):
        """404 = caller bug. Retrying just amplifies it; should give
        up immediately with a loud warning."""
        from pdufa_scraper import _fetch_edgar_search
        http404 = urllib.error.HTTPError(
            "https://edgar/", 404, "Not Found", {}, None,
        )
        fake, calls = _mk_urlopen_returning([http404])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {}
        assert calls["n"] == 1, "404 must NOT trigger retries"

    def test_403_NOT_retried(self):
        from pdufa_scraper import _fetch_edgar_search
        http403 = urllib.error.HTTPError(
            "https://edgar/", 403, "Forbidden", {}, None,
        )
        fake, calls = _mk_urlopen_returning([http403])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {}
        assert calls["n"] == 1, "403 must NOT trigger retries"

    def test_all_retries_exhausted_returns_empty(self):
        """4 consecutive 500s exhaust the retry budget; final result
        is {} (caller treats as "no events this cycle")."""
        from pdufa_scraper import _fetch_edgar_search
        http500 = urllib.error.HTTPError(
            "https://edgar/", 500, "Internal Server Error", {}, None,
        )
        fake, calls = _mk_urlopen_returning(
            [http500] * 4,  # initial + 3 retries
        )
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {}
        assert calls["n"] == 4, (
            "must attempt 4 times (initial + 3 retries)"
        )

    def test_url_error_is_retried(self):
        """Network-level URLError (DNS, connection refused) is
        transient — retry."""
        from pdufa_scraper import _fetch_edgar_search
        net_err = urllib.error.URLError("temporary connection refused")
        fake, calls = _mk_urlopen_returning([
            net_err, b'{"recovered": true}',
        ])
        with patch("urllib.request.urlopen", side_effect=fake):
            out = _fetch_edgar_search("PDUFA")
        assert out == {"recovered": True}
        assert calls["n"] == 2
