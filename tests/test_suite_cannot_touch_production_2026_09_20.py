"""2026-09-20 — no test touches production data.

A droplet run of the suite left `["FRESH1", "FRESH2"]` — a test's fake
asset list — in the PRODUCTION master database's
`alpaca_active_symbols_cache`, through `screener._PERSISTED_CACHE_PATH`
(a hardcoded `/opt/quantopsai/quantopsai.db`). An audit then counted
2,217 database opens per run, from 285 tests in 83 files, aimed at
production-named databases in the repo root — which on the droplet IS
the install directory. The repo-root conftest.py now runs every test in
a temp working directory and redirects any database open aimed at the
install directory into a per-test sandbox. These tests pin that guard.
"""
from __future__ import annotations

import glob
import os
import sqlite3

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _opened_file(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


class TestWorkingDirectory:
    def test_every_test_runs_outside_the_repo(self):
        cwd = os.path.realpath(os.getcwd())
        assert not cwd.startswith(os.path.realpath(REPO))
        assert not cwd.startswith("/opt/quantopsai")

    def test_a_relative_database_lands_in_temp(self):
        conn = sqlite3.connect("quantopsai.db")
        try:
            opened = os.path.realpath(_opened_file(conn))
        finally:
            conn.close()
        assert opened.startswith(os.path.realpath(os.getcwd()))
        assert not opened.startswith(os.path.realpath(REPO))


class TestDatabaseSandbox:
    @pytest.mark.parametrize("target", [
        "/opt/quantopsai/quantopsai.db",
        "/opt/quantopsai/quantopsai_profile_229.db",
        "/opt/quantopsai/altdata/edgar13f/data/edgar13f.db",
        os.path.join(REPO, "quantopsai.db"),
        os.path.join(REPO, "quantopsai_profile_1.db"),
    ])
    def test_an_install_directory_database_opens_in_the_sandbox(
            self, target, prod_sandbox_dir):
        conn = sqlite3.connect(target)
        try:
            conn.execute("CREATE TABLE marker (x)")
            conn.commit()
            opened = _opened_file(conn)
        finally:
            conn.close()
        assert os.path.dirname(opened) == prod_sandbox_dir
        assert opened.endswith("_" + os.path.basename(target))

    def test_the_same_path_is_the_same_sandbox_file_within_a_test(self):
        a = sqlite3.connect("/opt/quantopsai/quantopsai.db")
        a.execute("CREATE TABLE t (x)")
        a.execute("INSERT INTO t VALUES (7)")
        a.commit()
        a.close()
        b = sqlite3.connect("/opt/quantopsai/quantopsai.db")
        try:
            assert b.execute("SELECT x FROM t").fetchone() == (7,)
        finally:
            b.close()

    def test_a_different_test_starts_with_an_empty_sandbox(self):
        conn = sqlite3.connect("/opt/quantopsai/quantopsai.db")
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master")]
        finally:
            conn.close()
        assert names == []

    def test_uri_form_is_redirected_and_keeps_its_mode(self,
                                                       prod_sandbox_dir):
        with pytest.raises(sqlite3.OperationalError):
            # read-only open of a database that does not exist in the
            # sandbox: what a clean machine would do
            sqlite3.connect("file:/opt/quantopsai/quantopsai.db?mode=ro",
                            uri=True).execute("SELECT 1")
        conn = sqlite3.connect(
            "file:/opt/quantopsai/quantopsai.db?mode=rwc", uri=True)
        try:
            assert os.path.dirname(_opened_file(conn)) == prod_sandbox_dir
        finally:
            conn.close()

    def test_temp_and_memory_databases_are_left_alone(self, tmp_path):
        p = str(tmp_path / "mine.db")
        conn = sqlite3.connect(p)
        try:
            assert os.path.realpath(_opened_file(conn)) == os.path.realpath(p)
        finally:
            conn.close()
        sqlite3.connect(":memory:").close()


class TestTheIncidentPath:
    def test_the_active_symbols_cache_write_cannot_reach_production(
            self, prod_sandbox_dir):
        """The exact write that put a test's fake assets into the
        production master database."""
        import screener
        assert screener._PERSISTED_CACHE_PATH == (
            "/opt/quantopsai/quantopsai.db")
        screener._write_persisted_active_symbols({"FRESH1", "FRESH2"})
        assert set(screener._read_persisted_active_symbols() or []) == {
            "FRESH1", "FRESH2"}
        landed = glob.glob(os.path.join(prod_sandbox_dir, "*_quantopsai.db"))
        assert len(landed) == 1

    def test_the_medals_cache_file_is_pointed_at_the_sandbox(
            self, prod_sandbox_dir):
        import views
        assert os.path.dirname(views._medals_path()) == prod_sandbox_dir


class TestTheFloorIsTheSessionNotTheTest:
    """A background thread can outlive the test that started it. On
    2026-09-20 the dashboard's medal-warm thread did: its test's
    overrides were torn down, the thread fell through to the real path,
    and a droplet run wrote an empty medals cache into the install
    directory. After EVERY per-test patch is undone, the guards must
    still hold."""

    def test_after_teardown_the_medals_path_is_still_temp(self,
                                                          monkeypatch):
        import views
        monkeypatch.undo()
        path = os.path.realpath(views._medals_path())
        assert not path.startswith("/opt/quantopsai")
        assert not path.startswith(os.path.realpath(REPO))

    def test_after_teardown_the_working_directory_is_still_temp(
            self, monkeypatch):
        monkeypatch.undo()
        cwd = os.path.realpath(os.getcwd())
        assert not cwd.startswith("/opt/quantopsai")
        assert not cwd.startswith(os.path.realpath(REPO))

    def test_a_thread_outliving_its_patches_still_hits_the_sandbox(
            self, monkeypatch):
        import threading
        seen = {}
        go = threading.Event()

        def straggler():
            go.wait(5)
            conn = sqlite3.connect("/opt/quantopsai/quantopsai.db")
            try:
                seen["file"] = _opened_file(conn)
            finally:
                conn.close()
        t = threading.Thread(target=straggler)
        t.start()
        monkeypatch.undo()              # the test's own overrides are gone
        go.set()
        t.join(10)
        assert "file" in seen
        opened = os.path.realpath(seen["file"])
        assert not opened.startswith("/opt/quantopsai")
        assert opened.endswith("_quantopsai.db")

    def test_the_session_floor_is_set_at_import(self):
        src = open(os.path.join(REPO, "conftest.py")).read()
        assert "_SESSION_DIR = tempfile.mkdtemp(" in src
        assert 'os.environ["QUANTOPSAI_MEDALS_FILE"] = ' in src
        assert '@pytest.fixture(scope="session", autouse=True)' in src
        assert "def _session_working_directory():" in src


class TestMechanismIsWired:
    def test_root_conftest_carries_both_guards(self):
        src = open(os.path.join(REPO, "conftest.py")).read()
        assert "sqlite3.connect = _sandboxed_connect" in src
        assert "def _no_production_data(" in src
        assert "monkeypatch.chdir(" in src
        assert '"/opt/quantopsai"' in src
        assert "QUANTOPSAI_MEDALS_FILE" in src
