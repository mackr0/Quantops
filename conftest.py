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

NO TEST TOUCHES PRODUCTION DATA.

2026-09-20 — a droplet run of the suite left `["FRESH1", "FRESH2"]` — a
test's fake asset list — in the PRODUCTION master database's
`alpaca_active_symbols_cache`, through a hardcoded
`/opt/quantopsai/quantopsai.db` path; the altdata cron jobs read that
table, so until it was repaired they would have treated every real
symbol as inactive. An audit then counted 2,217 database opens per run,
from 285 tests in 83 files, aimed at production-named databases in the
repo root — and on the droplet the repo root IS `/opt/quantopsai`, so a
relative `quantopsai.db` is the live master database and
`quantopsai_profile_<id>.db` a live journal. Two guards, both applied to
every test:

  1. WORKING DIRECTORY — every test runs in its own temp directory, so
     a relative path (`quantopsai.db`, `cycle_data_7.json`, a marker
     file) lands in temp, never in the install directory. Tests that
     read repo files use absolute paths.
  2. DATABASE SANDBOX — a `sqlite3.connect` aimed at an ABSOLUTE path
     inside the production install directory or the repo is redirected
     to a per-test sandbox file of the same name. The code under test
     behaves exactly as on a clean machine (the database starts empty)
     and cannot reach the real file. The session ends with a count of
     redirected opens.
"""
import hashlib
import os
import socket
import sqlite3

import pytest

_LOCAL_HOSTS = (None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0",
                b"localhost", b"127.0.0.1")

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROTECTED_ROOTS = tuple(sorted({"/opt/quantopsai", _REPO_ROOT}))
_real_sqlite_connect = sqlite3.connect
_sandbox = {"dir": None, "redirected": 0}


def _protected(path: str) -> bool:
    return any(path == r or path.startswith(r + os.sep)
               for r in _PROTECTED_ROOTS)


def _sandboxed_connect(database, *args, **kwargs):
    """sqlite3.connect, except that an absolute path inside a protected
    root opens a same-named file in the current test's sandbox."""
    try:
        raw = (database.decode() if isinstance(database, bytes)
               else os.fspath(database))
    except TypeError:
        return _real_sqlite_connect(database, *args, **kwargs)
    is_uri = raw.startswith("file:")
    path, _, query = (raw[5:].partition("?") if is_uri else (raw, "", ""))
    if (path and not path.startswith(":memory:") and os.path.isabs(path)
            and _protected(os.path.normpath(path))
            and _sandbox["dir"]):
        norm = os.path.normpath(path)
        tag = hashlib.sha1(norm.encode()).hexdigest()[:8]
        target = os.path.join(_sandbox["dir"],
                              f"{tag}_{os.path.basename(norm)}")
        _sandbox["redirected"] += 1
        database = (f"file:{target}" + (f"?{query}" if query else "")
                    if is_uri else target)
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _sandboxed_connect


@pytest.fixture(autouse=True)
def _no_production_data(tmp_path_factory, monkeypatch):
    """Guards 1 and 2 above, plus the one non-database production file
    a rendered page writes (the dropdown-medals cache)."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    previous = _sandbox["dir"]
    _sandbox["dir"] = str(tmp_path_factory.mktemp("prod_sandbox"))
    # views._medals_path() reads this at call time, so it holds no
    # matter when (or whether) a test imports the module.
    monkeypatch.setenv("QUANTOPSAI_MEDALS_FILE",
                       os.path.join(_sandbox["dir"], "medals_cache.json"))
    yield
    _sandbox["dir"] = previous


@pytest.fixture
def prod_sandbox_dir():
    """Where this test's redirected production-database opens land."""
    return _sandbox["dir"]


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(
        f"production-data guard: {_sandbox['redirected']} database open(s) "
        "aimed at the install directory were redirected to a sandbox")


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
