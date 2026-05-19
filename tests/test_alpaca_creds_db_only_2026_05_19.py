"""Alpaca-credential resolution is DB-only — no env-key path.

2026-05-19. Removed the env-level "master key" path from
`market_data._resolve_alpaca_credentials`. The resolver now sources
Alpaca credentials exclusively from the `alpaca_accounts` table.

History:
- 2026-05-15 added env-first resolution + DB fallback for self-healing
- 2026-05-19 the env "master key" path produced THREE outages in 24h
  (silent Anthropic fallback gate; account-4 key confusion; options
  oracle 401). Operator feedback: kill the env-key path entirely —
  per-account keys in the DB are kept fresh by the trading workflow
  and are the canonical source.

Tests pin:
  - With env vars set to ANY value, the resolver still returns the
    DB row's key (env path is dead)
  - When DB has no rows, returns empty strings + base_url
  - 4 previously-bypass call sites (client.get_api, fetch_and_cache_names,
    _fetch_crypto_bars_alpaca, get_intraday_patterns) all reach the
    resolver — none reads config.ALPACA_API_KEY directly anymore
"""
from __future__ import annotations

import os
import sys
import sqlite3
import inspect
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def isolated_master_db(tmp_path, monkeypatch):
    """Create a tmp master DB with one alpaca_accounts row."""
    master = tmp_path / "quantopsai.db"
    conn = sqlite3.connect(str(master))
    conn.executescript("""
        CREATE TABLE alpaca_accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            alpaca_api_key_enc TEXT NOT NULL,
            alpaca_secret_key_enc TEXT NOT NULL,
            user_id INTEGER NOT NULL DEFAULT 1
        );
    """)
    # Insert encrypted row using the production crypto module
    from crypto import encrypt
    conn.execute(
        "INSERT INTO alpaca_accounts "
        "(id, name, alpaca_api_key_enc, alpaca_secret_key_enc) "
        "VALUES (1, 'acct1', ?, ?)",
        (encrypt("DB-API-KEY"), encrypt("DB-SECRET-KEY")),
    )
    conn.commit()
    conn.close()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestResolverIsDbOnly:
    """The resolver must NEVER use env vars. Any value in
    ALPACA_API_KEY/ALPACA_SECRET_KEY must be ignored."""

    def test_env_set_to_garbage_resolver_still_returns_db_key(
        self, isolated_master_db, monkeypatch,
    ):
        monkeypatch.setenv("ALPACA_API_KEY", "should-not-be-returned")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "should-not-be-returned")
        # Force re-import in case earlier modules cached
        if "market_data" in sys.modules:
            del sys.modules["market_data"]
        from market_data import _resolve_alpaca_credentials
        key, secret, _ = _resolve_alpaca_credentials()
        assert key == "DB-API-KEY", (
            f"Resolver must source from DB, not env; got {key!r}"
        )
        assert secret == "DB-SECRET-KEY"

    def test_env_empty_resolver_still_returns_db_key(
        self, isolated_master_db, monkeypatch,
    ):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        if "market_data" in sys.modules:
            del sys.modules["market_data"]
        from market_data import _resolve_alpaca_credentials
        key, secret, _ = _resolve_alpaca_credentials()
        assert key == "DB-API-KEY"
        assert secret == "DB-SECRET-KEY"

    def test_no_db_row_returns_empty_strings(self, tmp_path, monkeypatch):
        """When alpaca_accounts is empty, the resolver returns
        ("","",base_url) — callers must handle this. Env vars must
        NOT save the day (they did pre-2026-05-19)."""
        master = tmp_path / "quantopsai.db"
        conn = sqlite3.connect(str(master))
        conn.execute(
            "CREATE TABLE alpaca_accounts (id INTEGER, name TEXT, "
            "alpaca_api_key_enc TEXT, alpaca_secret_key_enc TEXT)"
        )
        conn.commit()
        conn.close()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ALPACA_API_KEY", "env-would-have-saved-us")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "env-would-have-saved-us")
        if "market_data" in sys.modules:
            del sys.modules["market_data"]
        from market_data import _resolve_alpaca_credentials
        key, secret, _ = _resolve_alpaca_credentials()
        # Empty DB → empty keys returned. NOT the env values.
        assert key == ""
        assert secret == ""


class TestNoDirectEnvReadsInCallSites:
    """Structural test — the four sites that previously bypassed the
    resolver (client.get_api / fetch_and_cache_names /
    _fetch_crypto_bars_alpaca / get_intraday_patterns) must not
    reference `config.ALPACA_API_KEY` anymore. Catches a refactor
    that re-introduces the bypass."""

    @staticmethod
    def _has_executable_config_read(src: str) -> bool:
        """Scan source lines; ignore lines whose first non-ws char is `#`.
        Returns True if any executable line contains the offending
        config.ALPACA_API_KEY reference."""
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "config.ALPACA_API_KEY" in line:
                return True
        return False

    def test_client_get_api_does_not_read_config_alpaca_key(self):
        import client
        src = inspect.getsource(client.get_api)
        assert not self._has_executable_config_read(src), (
            "client.get_api has an executable config.ALPACA_API_KEY "
            "read; must use _resolve_alpaca_credentials"
        )

    def test_fetch_and_cache_names_does_not_read_config_alpaca_key(self):
        import models
        src = inspect.getsource(models.fetch_and_cache_names)
        assert not self._has_executable_config_read(src), (
            "fetch_and_cache_names re-introduced env-key read"
        )

    def test_fetch_crypto_bars_alpaca_does_not_read_config_alpaca_key(self):
        import screener
        src = inspect.getsource(screener._fetch_crypto_bars_alpaca)
        assert not self._has_executable_config_read(src)

    def test_get_intraday_patterns_does_not_read_config_alpaca_key(self):
        import alternative_data
        src = inspect.getsource(alternative_data.get_intraday_patterns)
        assert not self._has_executable_config_read(src)


class TestResolverDocstringMentionsDbOnly:
    """The resolver's docstring must surface the DB-only contract.
    Catches a future PR that re-introduces env reads without
    updating the contract."""

    def test_docstring_says_db_only(self):
        from market_data import _resolve_alpaca_credentials
        doc = (_resolve_alpaca_credentials.__doc__ or "").lower()
        # Must mention alpaca_accounts
        assert "alpaca_accounts" in doc
        # Must explicitly state env-key path is removed / not used
        assert ("removed" in doc or "no env" in doc or
                "not from env" in doc or "env-key path" in doc), (
            f"Resolver docstring should clearly state env-key path "
            f"is removed. Got: {doc[:300]}"
        )
