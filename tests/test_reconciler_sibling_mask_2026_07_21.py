"""2026-07-21 — the p214 GOOG false-halt: a genuinely-owned long,
masked by a sibling profile's larger short on the shared account
(p214 long 10 + p212 short 11 → broker nets -1), was classified
orphan_close because the per-profile scan compared own qty against the
ACCOUNT-aggregate broker position. The reconciler's own contract says
aggregate parity is the decomposition gate's job — so when the
account's symbol-level decomposition HOLDS (Σ virtual == broker), no
profile's position is phantom and the halt is a false positive.

Pins:
  1. decomposition holds (−11 + 10 == broker −1) → suppress (True)
  2. decomposition broken (Σ virtual ≠ broker) → NO suppression
  3. any error in the cross-book sum → fail-CLOSED (False)
  4. the orphan_close path consults the check after the own-exit
     escape and before bucketing (structural)
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _mk_profile_db(rows):
    """rows: list of (symbol, side, qty, price)."""
    from journal import init_db, log_trade
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    for i, (sym, side, qty, price) in enumerate(rows):
        log_trade(symbol=sym, side=side, qty=qty, price=price,
                  order_id=f"o{i}", signal_type="STRONG_SELL"
                  if side == "short" else "BUY",
                  status="open", fill_price=price, db_path=path)
    return path


def _broker(positions):
    api = MagicMock()
    objs = []
    for sym, qty in positions.items():
        p = MagicMock()
        p.symbol = sym
        p.qty = qty
        objs.append(p)
    api.list_positions.return_value = objs
    return api


def _ctx(aid="acct56"):
    ctx = MagicMock()
    ctx.alpaca_account_id = aid
    return ctx


def _patched(profiles):
    """profiles: {pid: (account_id, db_path)}"""
    def fake_active():
        return [{"id": pid, "alpaca_account_id": aid}
                for pid, (aid, _) in profiles.items()]

    def fake_build(pid):
        c = MagicMock()
        c.db_path = profiles[pid][1]
        return c

    return (patch("models.get_active_profiles", side_effect=fake_active),
            patch("models.build_user_context_from_profile",
                  side_effect=fake_build))


class TestSymbolDecomposition:
    def test_sibling_masked_long_suppresses(self):
        """p214-shape: long 10 + sibling short 11 vs broker -1."""
        from reconcile_journal_to_broker import _symbol_decomposition_holds
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        p1, p2 = _patched({214: ("acct56", db_long),
                           212: ("acct56", db_short)})
        with p1, p2:
            assert _symbol_decomposition_holds(
                _broker({"GOOG": -1}), _ctx(), "GOOG", {}) is True

    def test_broken_decomposition_does_not_suppress(self):
        """Journal long with broker flat and NO sibling short — a real
        externally-closed phantom must still halt."""
        from reconcile_journal_to_broker import _symbol_decomposition_holds
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        p1, p2 = _patched({214: ("acct56", db_long)})
        with p1, p2:
            assert _symbol_decomposition_holds(
                _broker({}), _ctx(), "GOOG", {}) is False

    def test_error_fails_closed(self):
        from reconcile_journal_to_broker import _symbol_decomposition_holds
        api = MagicMock()
        api.list_positions.side_effect = RuntimeError("alpaca down")
        assert _symbol_decomposition_holds(api, _ctx(), "GOOG", {}) is False

    def test_no_account_id_fails_closed(self):
        from reconcile_journal_to_broker import _symbol_decomposition_holds
        ctx = MagicMock()
        ctx.alpaca_account_id = None
        assert _symbol_decomposition_holds(
            _broker({"GOOG": -1}), ctx, "GOOG", {}) is False


class TestOrphanPathConsultsDecomposition:
    def test_check_sits_between_own_exit_escape_and_bucketing(self):
        src = open(os.path.join(
            REPO, "reconcile_journal_to_broker.py")).read()
        own_exit = src.index("_own_exit_fills_explain(api, conn, db_path, sym")
        decomp = src.index("_symbol_decomposition_holds(api, ctx, sym")
        bucket = src.index('actions["orphan_close"].append')
        assert own_exit < decomp < bucket, (
            "the decomposition suppressor must run after the own-exit "
            "escape and before orphan_close bucketing")
