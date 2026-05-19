"""Guardrails around the 2026-05-19 alpaca_accounts backfill outage.

Two layers under test:

1. `alpaca_credentials_invariant.check_alpaca_credentials` — the
   scheduler boot guard. Must FAIL when alpaca_accounts is empty and
   any profile has per-profile keys (Branch A) OR when any enabled
   profile has no resolvable credentials at all (Branch B). Must PASS
   on a correctly-linked state.

2. `full_reset_2026_05_18.step2_install_keys` + `step2b_link_profiles`
   + `step2c_verify_linkage` — the reset script's idempotent rebuild
   of the shared-account + linkage structure. After they run against
   any starting state (empty table, partial state, fully-populated),
   the post-condition gate must report OK.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from alpaca_credentials_invariant import check_alpaca_credentials  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures — build a master DB matching the prod schema
# ---------------------------------------------------------------------------

def _make_master_db(tmp_path, profiles, accounts=None):
    """Build a minimal master DB with users / trading_profiles /
    alpaca_accounts. `profiles` is a list of dicts with id, name,
    enabled, alpaca_account_id, alpaca_api_key_enc. `accounts` is a
    list of (name, key_enc, secret_enc); empty list means table
    exists but is empty."""
    db_path = tmp_path / "master.db"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (id, name) VALUES (1, 'op')")
        conn.execute("""
            CREATE TABLE alpaca_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'Default',
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT 'https://paper-api.alpaca.markets',
                created_at TEXT NOT NULL DEFAULT (datetime('now')))
        """)
        conn.execute("""
            CREATE TABLE trading_profiles (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                alpaca_account_id INTEGER,
                alpaca_api_key_enc TEXT NOT NULL DEFAULT '',
                alpaca_secret_key_enc TEXT NOT NULL DEFAULT '',
                market_type TEXT NOT NULL DEFAULT 'stocks')
        """)
        for a in (accounts or []):
            conn.execute(
                "INSERT INTO alpaca_accounts (user_id, name, "
                "alpaca_api_key_enc, alpaca_secret_key_enc) "
                "VALUES (1, ?, ?, ?)",
                a,
            )
        for p in profiles:
            conn.execute(
                "INSERT INTO trading_profiles (id, name, enabled, "
                "alpaca_account_id, alpaca_api_key_enc) "
                "VALUES (?, ?, ?, ?, ?)",
                (p["id"], p["name"], int(p.get("enabled", 1)),
                 p.get("alpaca_account_id"),
                 p.get("alpaca_api_key_enc", "")),
            )
        conn.commit()
    return str(db_path)


# ---------------------------------------------------------------------------
# Invariant: Branch A — per-profile keys present, shared empty
# ---------------------------------------------------------------------------

def test_branch_a_per_profile_keys_but_alpaca_accounts_empty(tmp_path):
    """Reproduces the 2026-05-19 broken state EXACTLY."""
    from alpaca_credentials_invariant import check_alpaca_credentials
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_api_key_enc": "ENC-A1-blob"},
            {"id": 16, "name": "EXP-A2-NoAltData",
             "alpaca_api_key_enc": "ENC-A2-blob"},
            {"id": 21, "name": "EXP-A3-Candidate",
             "alpaca_api_key_enc": "ENC-A3-blob"},
        ],
        accounts=[],  # the bug: empty
    )
    ok, problems = check_alpaca_credentials(db)
    assert not ok
    assert any("alpaca_accounts is EMPTY" in p for p in problems)
    assert any("full_reset_2026_05_18.py" in p for p in problems)


# ---------------------------------------------------------------------------
# Invariant: Branch B — enabled profile with no resolvable credentials
# ---------------------------------------------------------------------------

def test_branch_b_enabled_profile_with_no_credentials(tmp_path):
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY", "enabled": 1,
             "alpaca_account_id": None, "alpaca_api_key_enc": ""},
        ],
        accounts=[("A1", "ENC-A1-blob", "ENC-A1-sec")],
    )
    ok, problems = check_alpaca_credentials(db)
    assert not ok
    assert any("no resolvable Alpaca credentials" in p for p in problems)


def test_branch_b_ignores_disabled_profile_with_no_credentials(tmp_path):
    """Disabled profiles aren't expected to trade and shouldn't trip
    the invariant — only enabled ones get checked."""
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 99, "name": "EXP-A1-Disabled", "enabled": 0,
             "alpaca_account_id": None, "alpaca_api_key_enc": ""},
            {"id": 12, "name": "EXP-A1-BuyHoldSPY", "enabled": 1,
             "alpaca_account_id": 1, "alpaca_api_key_enc": ""},
        ],
        accounts=[("A1", "ENC", "SEC")],
    )
    ok, problems = check_alpaca_credentials(db)
    assert ok, problems


# ---------------------------------------------------------------------------
# Invariant: happy paths
# ---------------------------------------------------------------------------

def test_happy_path_linked_via_alpaca_account_id(tmp_path):
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_account_id": 1, "alpaca_api_key_enc": ""},
            {"id": 13, "name": "EXP-A1-RandomA",
             "alpaca_account_id": 1, "alpaca_api_key_enc": ""},
        ],
        accounts=[("A1", "ENC", "SEC")],
    )
    ok, problems = check_alpaca_credentials(db)
    assert ok, problems


def test_happy_path_per_profile_keys_with_at_least_one_shared_row(tmp_path):
    """Defense-in-depth state: profile uses per-profile keys, but at
    least one alpaca_accounts row exists for the probes to use. The
    invariant should pass — both data paths can resolve credentials."""
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_account_id": None,
             "alpaca_api_key_enc": "ENC-A1-blob"},
        ],
        accounts=[("A1", "ENC-shared", "SEC-shared")],
    )
    ok, problems = check_alpaca_credentials(db)
    assert ok, problems


def test_fresh_bootstrap_no_profiles_no_accounts(tmp_path):
    """A fresh DB with no trading_profiles at all isn't yet
    misconfigured; the invariant should pass."""
    db = _make_master_db(tmp_path, profiles=[], accounts=[])
    ok, problems = check_alpaca_credentials(db)
    assert ok, problems


def test_invariant_returns_ok_when_tables_missing(tmp_path):
    """Even-fresher bootstrap: trading_profiles table doesn't exist
    yet (pre-migration). Invariant must not blow up."""
    db_path = tmp_path / "empty.db"
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        conn.commit()
    from alpaca_credentials_invariant import check_alpaca_credentials
    ok, problems = check_alpaca_credentials(str(db_path))
    assert ok
    assert problems == []


# ---------------------------------------------------------------------------
# Reset script: step2 + step2b + step2c idempotency
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_crypto(monkeypatch):
    """The reset script imports crypto.encrypt for key blob writes;
    keep tests independent of ENCRYPTION_KEY by stubbing it."""
    import types
    fake = types.ModuleType("crypto")
    fake.encrypt = lambda s: f"ENC({s})"
    fake.decrypt = lambda s: s[4:-1] if s.startswith("ENC(") else s
    monkeypatch.setitem(sys.modules, "crypto", fake)
    return fake


def _reset_script(monkeypatch, db_path):
    """Import full_reset_2026_05_18 and rewire its hardcoded
    /opt/quantopsai/quantopsai.db path at the connection site."""
    import importlib
    import full_reset_2026_05_18 as mod
    importlib.reload(mod)
    orig_connect = sqlite3.connect

    def _intercept(p, *a, **k):
        if p == "/opt/quantopsai/quantopsai.db":
            return orig_connect(db_path, *a, **k)
        return orig_connect(p, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", _intercept)
    return mod


def test_reset_idempotent_from_empty_table(tmp_path, monkeypatch, stub_crypto):
    """The exact 2026-05-19 broken state: per-profile keys set,
    alpaca_accounts empty. After step2 + step2b, the post-condition
    must verify clean."""
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_api_key_enc": "ENC(perA1)"},
            {"id": 13, "name": "EXP-A1-RandomA",
             "alpaca_api_key_enc": "ENC(perA1)"},
            {"id": 16, "name": "EXP-A2-NoAltData",
             "alpaca_api_key_enc": "ENC(perA2)"},
            {"id": 21, "name": "EXP-A3-Candidate",
             "alpaca_api_key_enc": "ENC(perA3)"},
        ],
        accounts=[],
    )
    mod = _reset_script(monkeypatch, db)
    mod.step2_install_keys(apply=True)
    mod.step2b_link_profiles(apply=True)
    assert mod.step2c_verify_linkage() is True

    # And the actual state must match: 3 named accounts + every
    # profile has alpaca_account_id pointing at its match.
    with closing(sqlite3.connect(db)) as conn:
        accts = dict(conn.execute(
            "SELECT name, id FROM alpaca_accounts"
        ).fetchall())
        assert set(accts) == {"A1", "A2", "A3"}
        for pid, expected_group in [
            (12, "A1"), (13, "A1"), (16, "A2"), (21, "A3"),
        ]:
            aid = conn.execute(
                "SELECT alpaca_account_id FROM trading_profiles WHERE id=?",
                (pid,),
            ).fetchone()[0]
            assert aid == accts[expected_group], (
                f"profile {pid} -> aid={aid}, expected {expected_group}"
            )


def test_reset_idempotent_when_run_twice(tmp_path, monkeypatch, stub_crypto):
    """Running step2 + step2b a second time must be a no-op — no
    duplicate rows in alpaca_accounts, linkage stays correct."""
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_api_key_enc": "ENC(perA1)"},
            {"id": 16, "name": "EXP-A2-NoAltData",
             "alpaca_api_key_enc": "ENC(perA2)"},
            {"id": 21, "name": "EXP-A3-Candidate",
             "alpaca_api_key_enc": "ENC(perA3)"},
        ],
        accounts=[],
    )
    mod = _reset_script(monkeypatch, db)
    mod.step2_install_keys(apply=True)
    mod.step2b_link_profiles(apply=True)
    # Re-run
    mod.step2_install_keys(apply=True)
    mod.step2b_link_profiles(apply=True)
    with closing(sqlite3.connect(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM alpaca_accounts").fetchone()[0]
        assert n == 3, f"duplicate alpaca_accounts rows: {n}"
    assert mod.step2c_verify_linkage() is True


def test_reset_verify_step_fails_loudly_on_unlinked_profiles(
    tmp_path, monkeypatch, stub_crypto,
):
    """If step2b is skipped, step2c MUST return False — the whole
    point of the post-condition gate."""
    db = _make_master_db(
        tmp_path,
        profiles=[
            {"id": 12, "name": "EXP-A1-BuyHoldSPY",
             "alpaca_api_key_enc": "ENC(perA1)"},
        ],
        accounts=[],
    )
    mod = _reset_script(monkeypatch, db)
    mod.step2_install_keys(apply=True)
    # Deliberately skip step2b — linkage NOT set
    assert mod.step2c_verify_linkage() is False
