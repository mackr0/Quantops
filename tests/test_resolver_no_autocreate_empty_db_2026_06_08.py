"""2026-06-08 — `_resolve_alpaca_credentials` must NEVER auto-create
a 0-byte sqlite file at a relative path.

Root cause traced this date: an altdata cron subprocess running from
/opt/quantopsai/altdata/congresstrades/ called
`screener.is_alpaca_active(...)` → `screener.get_active_alpaca_symbols`
→ `client.get_api(None)` → `market_data._resolve_alpaca_credentials()`.
The resolver did `sqlite3.connect("quantopsai.db")` against a relative
path that didn't exist in the cron's CWD. sqlite3.connect()'s default
mode CREATES a 0-byte file if the path doesn't exist. That file then
persisted; every subsequent altdata cron run found it via the
relative-path-exists check in the resolver, ran the SELECT against
an empty SQLite file, and threw 'no such table: alpaca_accounts'
WARN events on /issues, daily, since 2026-05-20.

Two contracts pinned:

  1. Connect uses `file:<path>?mode=ro` URI syntax (or an explicit
     existence guard) so no auto-create can happen, period.

  2. Path resolution prefers a candidate where the alpaca_accounts
     table actually exists, not just where a file with any contents
     does. `_path_has_alpaca_table` is the helper.

These are structural pins (regex / behavior tests on the resolver
code path itself) plus a behavioral test that confirms calling the
resolver from a CWD with no DB does NOT leave a file behind.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Behavioral pin — the resolver must not create files
# ---------------------------------------------------------------------------

def test_resolver_does_not_autocreate_file_in_cwd(
        tmp_path, monkeypatch,
):
    """The smoking gun: calling the resolver from a CWD with no
    quantopsai.db must NOT leave a file behind.

    Pre-2026-06-08 sqlite3.connect("quantopsai.db") auto-created
    a 0-byte file on first call. That stale empty file then
    poisoned every subsequent run."""
    # Run from an empty directory
    monkeypatch.chdir(tmp_path)
    # Pin DB_PATH to the relative form so we exercise the cron-context
    # path; also clear any absolute config to force the relative branch
    import config
    monkeypatch.setattr(config, "DB_PATH", "quantopsai.db")
    monkeypatch.setenv("DB_PATH", "quantopsai.db")
    # Reset cached resolver module state if any
    if "market_data" in sys.modules:
        del sys.modules["market_data"]
    from market_data import _resolve_alpaca_credentials
    _resolve_alpaca_credentials()
    files_left = [p for p in tmp_path.iterdir()
                   if p.name == "quantopsai.db"]
    assert files_left == [], (
        f"Resolver left a file behind in CWD: {files_left!r}. "
        f"This is the 2026-06-08 root cause — sqlite3.connect() "
        f"on a missing relative path auto-creates a 0-byte file "
        f"unless mode=ro URI is used."
    )


def test_path_has_alpaca_table_distinguishes_empty_from_populated(
        tmp_path,
):
    """The helper that prevents the empty-file trap. An empty
    sqlite file (no tables) must return False; a file with the
    alpaca_accounts table must return True."""
    from market_data import _path_has_alpaca_table
    # Path doesn't exist
    assert _path_has_alpaca_table(str(tmp_path / "nope.db")) is False
    # Empty sqlite file (no tables at all)
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert _path_has_alpaca_table(str(empty)) is False, (
        "Empty sqlite file must NOT be treated as a usable master "
        "DB — that's the bug that made the cron run pick up a "
        "0-byte stale file instead of falling through to "
        "/opt/quantopsai/quantopsai.db"
    )
    # File with the table
    populated = tmp_path / "real.db"
    conn = sqlite3.connect(str(populated))
    conn.execute(
        "CREATE TABLE alpaca_accounts ("
        "id INTEGER PRIMARY KEY, "
        "alpaca_api_key_enc TEXT, "
        "alpaca_secret_key_enc TEXT)"
    )
    conn.commit()
    conn.close()
    assert _path_has_alpaca_table(str(populated)) is True


# ---------------------------------------------------------------------------
# Structural pins — the fix can't be silently reverted
# ---------------------------------------------------------------------------

def test_resolver_uses_mode_ro_uri():
    """The resolver MUST connect via `file:<path>?mode=ro` so
    sqlite never auto-creates. Pin this against a refactor that
    reverts to plain `sqlite3.connect(_DB_PATH)`."""
    src = (REPO_ROOT / "market_data.py").read_text()
    # Find the _resolve_alpaca_credentials function body
    m = re.search(
        r"def _resolve_alpaca_credentials\(\):(.*?)(?=^def |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    body = m.group(1) if m else ""
    assert "mode=ro" in body, (
        "_resolve_alpaca_credentials must connect with "
        "`file:<path>?mode=ro` URI syntax. Without this, calling "
        "the resolver from a CWD without quantopsai.db will "
        "auto-create a 0-byte file at that path — the exact "
        "2026-06-08 root cause."
    )
    # And the connect call uses uri=True
    assert re.search(r"_sq3\.connect\([^)]*uri=True", body), (
        "The URI form requires uri=True kwarg on sqlite3.connect"
    )


def test_resolver_path_check_uses_table_helper():
    """The resolver's absolute-ify branch MUST validate that
    candidates have the alpaca_accounts table, not just that a
    file exists at the path."""
    src = (REPO_ROOT / "market_data.py").read_text()
    m = re.search(
        r"def _resolve_alpaca_credentials\(\):(.*?)(?=^def |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    body = m.group(1) if m else ""
    assert "_path_has_alpaca_table" in body, (
        "_resolve_alpaca_credentials must use _path_has_alpaca_table "
        "to validate candidates. The pre-2026-06-08 os.path.exists() "
        "check let an empty 0-byte sqlite file masquerade as a "
        "usable master DB."
    )
