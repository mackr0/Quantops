"""2026-07-21 (evening) — the p212 GOOG stranded short: the cover
guard's drift check compares this profile's own-book short against the
ACCOUNT-AGGREGATE broker position, so a sibling's long can mask a
genuinely-owned short (p212 short 11 + p214 long 10 → broker nets −1
→ "broker short 1, journal claims short 11" → every time-stop cover
refused, position stuck paying borrow and retrying every cycle).

Same sibling-masking class the reconciler's orphan halt had that
afternoon, living in a second place. Same fix: when the account's
symbol-level decomposition HOLDS (Σ every same-account profile's
virtual qty == broker qty — `_symbol_decomposition_holds`), the books
collectively explain the broker, the own-book claim is genuine, and
the exit must be allowed up to the own-book qty.

Pins:
  1. p212-shape cover (short 11, sibling long 10, broker −1) with ctx
     → ALLOWED in full.
  2. Broken decomposition (Σ virtual ≠ broker) → refusal stands.
  3. Legacy call (no ctx) → refusal stands (escape needs a ctx).
  4. Cross-book sum errors → fail-CLOSED (refusal stands).
  5. Mirror on the sell side: own long masked by a sibling's larger
     short → ALLOWED when the decomposition holds.
  6. Call sites pass ctx: trader COVER + SELL branches,
     trade_pipeline SELL branch (structural).
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

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


class TestCoverSiblingMask:

    def test_masked_short_cover_allowed(self):
        """p212-shape: own short 11, sibling long 10, broker −1.
        Decomposition holds (−11 + 10 == −1) → cover allowed in full."""
        from order_guard import allowable_cover_qty
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        p1, p2 = _patched({212: ("acct56", db_short),
                           214: ("acct56", db_long)})
        api = _broker({"GOOG": -1})
        with p1, p2:
            qty, reason = allowable_cover_qty(
                api, "GOOG", 11, db_path=db_short, ctx=_ctx())
        assert qty == 11
        assert "sibling-masked" in reason

    def test_partial_masked_cover_allowed(self):
        """The time-stop also tried partial lots (3, 5) — those must
        flow too."""
        from order_guard import allowable_cover_qty
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        p1, p2 = _patched({212: ("acct56", db_short),
                           214: ("acct56", db_long)})
        with p1, p2:
            qty, reason = allowable_cover_qty(
                _broker({"GOOG": -1}), "GOOG", 3,
                db_path=db_short, ctx=_ctx())
        assert qty == 3
        assert "sibling-masked" in reason

    def test_broken_decomposition_still_refuses(self):
        """Journal short 11 with broker −1 but NO sibling book that
        explains the gap — real drift must still refuse."""
        from order_guard import allowable_cover_qty
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        p1, p2 = _patched({212: ("acct56", db_short)})
        with p1, p2:
            qty, reason = allowable_cover_qty(
                _broker({"GOOG": -1}), "GOOG", 11,
                db_path=db_short, ctx=_ctx())
        assert qty == 0
        assert "drift" in reason.lower()

    def test_no_ctx_refuses_as_before(self):
        """Legacy callers pass no ctx — the escape needs the account
        id, so the pre-fix refusal stands unchanged."""
        from order_guard import allowable_cover_qty
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        qty, reason = allowable_cover_qty(
            _broker({"GOOG": -1}), "GOOG", 11, db_path=db_short)
        assert qty == 0
        assert "drift" in reason.lower()

    def test_cross_book_error_fails_closed(self):
        """Any error computing the cross-book sum → refusal stands."""
        from order_guard import allowable_cover_qty
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        with patch("models.get_active_profiles",
                   side_effect=RuntimeError("db locked")):
            qty, reason = allowable_cover_qty(
                _broker({"GOOG": -1}), "GOOG", 11,
                db_path=db_short, ctx=_ctx())
        assert qty == 0
        assert "drift" in reason.lower()


class TestSellSiblingMaskMirror:

    def test_masked_long_sell_allowed(self):
        """Mirror case: own long 10, sibling short 11, broker −1.
        Decomposition holds → the own-book sell must flow."""
        from order_guard import allowable_sell_qty
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        db_short = _mk_profile_db([("GOOG", "short", 11, 355.0)])
        p1, p2 = _patched({214: ("acct56", db_long),
                           212: ("acct56", db_short)})
        with p1, p2:
            qty, reason = allowable_sell_qty(
                _broker({"GOOG": -1}), "GOOG", 10,
                db_path=db_long, ctx=_ctx())
        assert qty == 10
        assert "sibling-masked" in reason

    def test_masked_long_sell_without_ctx_refuses(self):
        from order_guard import allowable_sell_qty
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        qty, reason = allowable_sell_qty(
            _broker({"GOOG": -1}), "GOOG", 10, db_path=db_long)
        assert qty == 0
        assert "drift" in reason.lower()

    def test_sell_broken_decomposition_still_refuses(self):
        from order_guard import allowable_sell_qty
        db_long = _mk_profile_db([("GOOG", "buy", 10, 357.0)])
        p1, p2 = _patched({214: ("acct56", db_long)})
        with p1, p2:
            qty, reason = allowable_sell_qty(
                _broker({"GOOG": -1}), "GOOG", 10,
                db_path=db_long, ctx=_ctx())
        assert qty == 0
        assert "drift" in reason.lower()


class TestCallSitesPassCtx:

    def test_trader_cover_branch_passes_ctx(self):
        src = open(os.path.join(REPO, "trader.py")).read()
        idx = src.find("if is_short:")
        assert idx > 0
        window = src[idx:idx + 2500]
        call_idx = window.find("allowable_cover_qty(\n")
        assert call_idx > 0, "cover-guard call site missing"
        call = window[call_idx:call_idx + 200]
        assert "ctx=ctx" in call, (
            "trader.py's COVER branch must pass ctx so a sibling-masked "
            "short isn't refused as drift (the p212 GOOG stranded short)")

    def test_trader_sell_branch_passes_ctx(self):
        src = open(os.path.join(REPO, "trader.py")).read()
        idx = src.find("allowable_sell_qty(\n")
        assert idx > 0
        assert "ctx=ctx" in src[idx:idx + 200]

    def test_pipeline_sell_branch_passes_ctx(self):
        src = open(os.path.join(REPO, "trade_pipeline.py")).read()
        idx = src.find("allowable_sell_qty(\n")
        assert idx > 0
        assert "ctx=ctx" in src[idx:idx + 200]
