"""Suite-wide test configuration (covers `tests/` AND the `altdata/*/tests`
trees, which cannot see fixtures defined under `tests/`).

NO TEST TOUCHES THE REAL NETWORK.

2026-09-20 — a record-only audit of the full suite attributed 684 real
outbound calls to 132 tests in 44 files: Yahoo 381, data.alpaca.markets
204, FRED 49, and 23 to the broker's paper API with the live keys from
the local `.env` (every one traced and found to be a GET — no test
placed an order). The suite's result therefore depended on those
services being up and fast: the 2026-09-19 droplet timeouts and the
2026-09-20 `test_crypto_skipped` timeout were this class, fixed one
test at a time until now.

Every test runs with outbound network REFUSED, instantly: a test that
reaches for the network exercises the code's outage path —
deterministic and fast — instead of depending on a third party. A test
that genuinely must reach the network opts out, visibly, with
`@pytest.mark.allow_network` (none does today).
"""
import socket

import pytest

_LOCAL_HOSTS = (None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0",
                b"localhost", b"127.0.0.1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: this test may make real outbound network calls "
        "(the suite refuses them by default — see conftest.py)")


@pytest.fixture
def no_network(monkeypatch):
    """Make every outbound network attempt fail INSTANTLY (DNS refused
    for anything but localhost).

    2026-09-19 — the API-walker tests and the alt-data aggregator test
    were quietly making REAL calls from the droplet (FRED, Alpaca data
    + paper-api with live keys, Google Trends, Wikipedia, EIA, SEC,
    USDA): /api/macro-data alone took 92.8s on cold caches, so the
    tests passed or hit the 30s timeout depending on whether some
    earlier run had warmed a persisted cache. With the network refused
    the routes run their OUTAGE paths — deterministic, fast, and the
    contract those tests assert (valid JSON, numeric fields numeric)
    must hold during an outage anyway.

    Applied to every test by `_network_refused_by_default` below; still
    requestable by name."""
    _real = socket.getaddrinfo

    def _refuse(host, *args, **kwargs):
        if host in _LOCAL_HOSTS:
            return _real(host, *args, **kwargs)
        raise socket.gaierror(
            socket.EAI_NONAME, f"network disabled in tests ({host!r})")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)

    # yfinance goes out through curl_cffi (libcurl does its own DNS and
    # never touches socket.getaddrinfo) — refuse that door too.
    try:
        import curl_cffi.requests as _curl_requests

        def _refuse_curl(self, method, url, *args, **kwargs):
            raise OSError(f"network disabled in tests ({url!r})")

        monkeypatch.setattr(_curl_requests.Session, "request", _refuse_curl)
    except (ImportError, AttributeError):
        # curl_cffi not installed here: that door does not exist.
        pass


@pytest.fixture(autouse=True)
def _network_refused_by_default(request):
    if request.node.get_closest_marker("allow_network") is None:
        request.getfixturevalue("no_network")
    yield
