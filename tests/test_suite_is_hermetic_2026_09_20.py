"""2026-09-20 — no test touches the real network.

A record-only audit of the full suite attributed 684 real outbound
calls to 132 tests in 44 files (Yahoo, Alpaca data, FRED, and 23 GETs
to the broker's paper API with live keys). The suite's result depended
on those services being up and fast. The repo-root conftest.py now
refuses outbound network for EVERY test; these tests pin that
mechanism, because a suite-wide safety net that silently stops working
is worse than none.
"""
from __future__ import annotations

import os
import re
import socket

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestRefusedByDefault:
    def test_dns_for_a_real_host_is_refused_instantly(self):
        with pytest.raises(socket.gaierror, match="network disabled"):
            socket.getaddrinfo("api.stlouisfed.org", 443)

    def test_a_plain_http_call_fails_fast_not_slow(self):
        import time
        import urllib.request
        t0 = time.time()
        with pytest.raises(OSError):
            urllib.request.urlopen("https://query2.finance.yahoo.com/",
                                   timeout=20)
        assert time.time() - t0 < 2.0

    def test_requests_library_is_refused_too(self):
        import requests
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get("https://data.alpaca.markets/v2/stocks/bars",
                         timeout=20)

    def test_the_curl_door_yfinance_uses_is_refused(self):
        import curl_cffi.requests as creq
        with pytest.raises(OSError, match="network disabled"):
            creq.Session().request("GET", "https://finance.yahoo.com/")

    def test_localhost_still_resolves(self):
        assert socket.getaddrinfo("localhost", 80)
        assert socket.getaddrinfo("127.0.0.1", 80)


class TestOptOutIsExplicitAndVisible:
    @pytest.mark.allow_network
    def test_the_marker_leaves_the_real_resolver_in_place(self):
        # Not resolving anything real — only checking that the refusing
        # wrapper was NOT installed for a test that opted out.
        assert "network disabled" not in (
            socket.getaddrinfo.__doc__ or "")
        assert getattr(socket.getaddrinfo, "__name__", "") != "_refuse"

    def test_only_this_file_opts_out(self):
        """An opt-out must be a visible, reviewed decision. Today no
        real test needs the network; if one ever does, it is added to
        this list on purpose."""
        allowed = {"tests/test_suite_is_hermetic_2026_09_20.py"}
        users = set()
        for root in ("tests", "altdata"):
            for dirpath, _dirs, files in os.walk(os.path.join(REPO, root)):
                for name in files:
                    if name.endswith(".py"):
                        path = os.path.join(dirpath, name)
                        # a real decorator or pytestmark — not a mention
                        # in a comment or docstring
                        if re.search(
                                r"^\s*(@pytest\.mark\.allow_network"
                                r"|pytestmark\s*=.*allow_network)",
                                open(path).read(), re.M):
                            users.add(os.path.relpath(path, REPO))
        assert users == allowed, users


class TestMechanismIsWired:
    def test_root_conftest_applies_the_refusal_to_every_test(self):
        src = open(os.path.join(REPO, "conftest.py")).read()
        # installed for the whole SESSION at import, not per test
        assert "\nsocket.getaddrinfo = _refuse\n" in src
        assert "_curl_requests.Session.request = _refuse_curl" in src
        assert "def _network_refused_by_default(request, monkeypatch):" in src
        assert 'get_closest_marker("allow_network")' in src
        assert "def no_network():" in src

    def test_the_refusal_outlives_a_tests_own_teardown(self, monkeypatch):
        """A background thread can outlive the test that started it (the
        dashboard's medal warm did, on 2026-09-20, and wrote into the
        install directory). Undoing every per-test patch must leave the
        network STILL refused — the floor is the session, not the test."""
        monkeypatch.undo()
        with pytest.raises(socket.gaierror, match="network disabled"):
            socket.getaddrinfo("paper-api.alpaca.markets", 443)

    def test_there_is_one_definition_of_the_fixture(self):
        """A second `no_network` under tests/ would shadow the root one
        for that tree and could drift from it."""
        hits = []
        for dirpath, _dirs, files in os.walk(REPO):
            if any(part in dirpath for part in
                   ("/venv", "/.claude", "/node_modules", "/.git")):
                continue
            for name in files:
                if name == "conftest.py":
                    path = os.path.join(dirpath, name)
                    if "def no_network(" in open(path).read():
                        hits.append(os.path.relpath(path, REPO))
        assert hits == ["conftest.py"], hits

    def test_every_test_tree_in_pytest_ini_sits_under_the_root_conftest(
            self):
        ini = open(os.path.join(REPO, "pytest.ini")).read()
        paths = re.findall(r"^\s+(\S+)\s*$",
                           ini.split("testpaths =")[1].split(
                               "python_files")[0], re.M)
        assert "tests" in paths and len(paths) >= 5
        for p in paths:
            assert os.path.isdir(os.path.join(REPO, p)), p
