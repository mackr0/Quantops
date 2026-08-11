"""2026-08-03 — canceled ≠ unfilled (the p212 GOOGL 5-of-14 incident).

Alpaca's order status describes the ORDER, not the fills: an order
canceled mid-execution keeps its partial fills (status='canceled',
filled_qty>0). Three writer sites trusted status alone and one
detection layer required status=='filled', so a trailing stop that
covered 5 of a 14-share short in the second before its cancel landed
left the 5 shares in no journal: broker −9 vs virtual −14, kill
switch tripped, and the rump position sat naked because every
downstream guard correctly refused the wrong books.

Contract pinned here (the CLASS, not the instance):
  no code path may mutate the journal on the strength of a broker
  cancellation without reading the terminal order's filled_qty;
  filled_qty > 0 is exit evidence that must be BOOKED, never
  discarded.

Pins:
  A. sync_pending_protective_order_ids defers canceled-with-fill rows
     to the reconciler (and still cancels truly-unfilled ones).
  B. cancel_for_symbol: partially_filled → abort (never cancel into an
     executing stop); terminal-with-fill → abort, pointers kept;
     post-cancel read-back discovers a raced fill → abort, pointers
     kept; clean confirmed cancel → pointers cleared as before.
  C. trader exit path defers when its own-order cancel reveals fills.
  D. reconciler detection: terminal-with-fill orders are protective
     fill evidence (not just status=='filled'), including via the
     layer-3 own-row scan when pointer columns were NULLed; the apply
     path heals a mislabeled 'canceled' row into a closed partial
     cover with side='cover' and evidence-based pnl (the exact p212
     repair shape).
  E. structural: every SQL write of status='canceled' lives in an
     audited function (new writers must join the audit or fail).
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FakeOrder:
    def __init__(self, oid, status, side="buy", filled_qty=0,
                 filled_avg_price=0, qty=None, order_type="trailing_stop",
                 filled_at="2026-08-03T13:34:37+00:00",
                 replaced_by=None):
        self.id = oid
        self.status = status
        self.side = side
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.qty = qty if qty is not None else filled_qty
        self.order_type = order_type
        self.type = order_type
        self.filled_at = filled_at
        self.replaced_by = replaced_by
        self.replaces = None
        self.legs = None


class FakeAPI:
    """Deliberate fake: unknown order ids raise (like a 404)."""

    def __init__(self, orders=None, positions=None):
        self.orders = dict(orders or {})
        self.positions = positions or []
        self.canceled = []

    def get_order(self, oid):
        if oid not in self.orders:
            raise RuntimeError(f"order not found: {oid}")
        return self.orders[oid]

    def cancel_order(self, oid):
        self.canceled.append(oid)
        o = self.orders.get(oid)
        if o is not None and o.status in ("new", "accepted", "held"):
            o.status = "canceled"

    def list_positions(self):
        out = []
        for sym, qty in self.positions:
            p = MagicMock()
            p.symbol = sym
            p.qty = qty
            p.unrealized_pl = 0
            out.append(p)
        return out

    def list_orders(self, *a, **k):
        return []

    def get_activities(self, *a, **k):
        return []


def _mk_db():
    from journal import init_db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    return path


def _insert(path, **kw):
    defaults = dict(timestamp="2026-07-30T16:26:29", symbol="GOOGL",
                    side="short", qty=14.0, price=334.59,
                    fill_price=334.59, order_id="entry-A",
                    signal_type="STRONG_SELL", status="open")
    defaults.update(kw)
    cols = ", ".join(defaults)
    ph = ", ".join("?" for _ in defaults)
    with sqlite3.connect(path) as c:
        cur = c.execute(
            f"INSERT INTO trades ({cols}) VALUES ({ph})",
            list(defaults.values()))
        return cur.lastrowid


def _row(path, rid):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute(
            "SELECT * FROM trades WHERE id=?", (rid,)).fetchone())


# ---------------------------------------------------------------- A —
class TestSyncDefersPartialFill:
    def _run(self, filled_qty):
        from bracket_orders import sync_pending_protective_order_ids
        db = _mk_db()
        rid = _insert(db, side="buy", qty=14.0, price=None,
                      fill_price=None, order_id="prot-B",
                      signal_type="PROTECTIVE_TRAILING",
                      status="pending_protective")
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "canceled", side="buy", filled_qty=filled_qty,
            filled_avg_price=368.55 if filled_qty else 0, qty=14)})
        stats = sync_pending_protective_order_ids(api, db)
        return db, rid, stats

    def test_canceled_with_fill_left_for_reconciler(self):
        db, rid, stats = self._run(filled_qty=5)
        row = _row(db, rid)
        assert row["status"] == "pending_protective", (
            "canceled-with-fill row must NOT be stamped canceled")
        assert stats.get("deferred_partial_fill") == 1
        assert not stats.get("marked_canceled")

    def test_canceled_unfilled_still_marked(self):
        db, rid, stats = self._run(filled_qty=0)
        row = _row(db, rid)
        assert row["status"] == "canceled"
        assert stats.get("marked_canceled") == 1


# ---------------------------------------------------------------- B —
class TestCancelForSymbolFillEvidence:
    def _db_with_pointer(self):
        db = _mk_db()
        rid = _insert(db, protective_trailing_order_id="prot-B")
        return db, rid

    def test_partially_filled_aborts_without_cancel(self):
        from bracket_orders import cancel_for_symbol
        db, rid = self._db_with_pointer()
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "partially_filled", filled_qty=5, qty=14)})
        assert cancel_for_symbol(api, db, "GOOGL") is True
        assert api.canceled == [], (
            "must never cancel into an executing stop")
        assert _row(db, rid)["protective_trailing_order_id"] == "prot-B"

    def test_terminal_with_fill_aborts_and_keeps_pointer(self):
        from bracket_orders import cancel_for_symbol
        db, rid = self._db_with_pointer()
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "canceled", filled_qty=5, qty=14)})
        assert cancel_for_symbol(api, db, "GOOGL") is True
        assert api.canceled == []
        assert _row(db, rid)["protective_trailing_order_id"] == "prot-B"

    def test_cancel_race_discovered_by_readback(self):
        from bracket_orders import cancel_for_symbol
        db, rid = self._db_with_pointer()

        class RacingAPI(FakeAPI):
            def cancel_order(self, oid):
                super().cancel_order(oid)
                # the trigger beat the cancel: fills materialize
                self.orders[oid].filled_qty = 5

        api = RacingAPI(orders={"prot-B": FakeOrder(
            "prot-B", "new", filled_qty=0, qty=14)})
        assert cancel_for_symbol(api, db, "GOOGL") is True
        assert _row(db, rid)["protective_trailing_order_id"] == "prot-B"

    def test_clean_confirmed_cancel_clears_pointers(self):
        from bracket_orders import cancel_for_symbol
        db, rid = self._db_with_pointer()
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "new", filled_qty=0, qty=14)})
        assert cancel_for_symbol(api, db, "GOOGL") is False
        assert api.canceled == ["prot-B"]
        assert _row(db, rid)["protective_trailing_order_id"] is None

    def test_unconfirmed_cancel_keeps_pointers(self):
        from bracket_orders import cancel_for_symbol
        db, rid = self._db_with_pointer()

        class NoReadbackAPI(FakeAPI):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._cancelled_once = False

            def cancel_order(self, oid):
                self.canceled.append(oid)
                self._cancelled_once = True

            def get_order(self, oid):
                if self._cancelled_once:
                    raise RuntimeError("alpaca hiccup")
                return super().get_order(oid)

        api = NoReadbackAPI(orders={"prot-B": FakeOrder(
            "prot-B", "new", filled_qty=0, qty=14)})
        cancel_for_symbol(api, db, "GOOGL")
        assert _row(db, rid)["protective_trailing_order_id"] == "prot-B", (
            "unverified cancel must not clear pointers")


# ---------------------------------------------------------------- C —
class TestTraderExitDefersOnRacedCancel:
    def test_source_defers_on_partial_and_readback(self):
        """Structural: the own-conflicting-cancel loop carries both
        gates — a partially_filled pre-check and a post-cancel
        filled_qty read-back — each returning (deferring the exit)."""
        src = open(os.path.join(REPO, "trader.py")).read()
        i = src.index("Cancelled own conflicting order")
        block = src[i - 4000:i + 3000]
        assert "partially_filled" in block
        assert "filled_qty" in block
        pre = block.index("partially_filled")
        assert "return" in block[pre:pre + 700], (
            "partially_filled own order must defer the exit")


# ---------------------------------------------------------------- D —
def _reconcile_ctx(db, api):
    ctx = MagicMock()
    ctx.display_name = "test-p212"
    ctx.profile_id = 212
    ctx.db_path = db
    ctx.alpaca_account_id = "56"
    ctx.get_alpaca_api = lambda: api
    return ctx


class TestReconcilerHealsCanceledWithFill:
    def _fixture(self, protective_status):
        """Short 14 open (entry filled); protective buy row for 14 in
        `protective_status`; broker order for it canceled with 5
        filled @ 368.55; broker holds -9. Pointer columns NULL (the
        cancel path cleared them) — layer 3 must find the row."""
        db = _mk_db()
        entry = _insert(db)  # short 14, order entry-A, open
        prot = _insert(db, timestamp="2026-07-30T16:39:02", side="buy",
                       qty=14.0, price=None, fill_price=None,
                       order_id="prot-B",
                       signal_type="PROTECTIVE_TRAILING",
                       status=protective_status)
        api = FakeAPI(
            orders={
                "entry-A": FakeOrder(
                    "entry-A", "filled", side="sell", filled_qty=14,
                    filled_avg_price=334.59, qty=14,
                    order_type="market",
                    filled_at="2026-07-30T16:26:29+00:00"),
                "prot-B": FakeOrder(
                    "prot-B", "canceled", side="buy", filled_qty=5,
                    filled_avg_price=368.55, qty=14),
            },
            positions=[("GOOGL", "-9")],
        )
        return db, entry, prot, api

    @pytest.mark.parametrize("status", ["canceled", "pending_protective"])
    def test_partial_cover_healed(self, status):
        from reconcile_journal_to_broker import reconcile_with_ctx
        db, entry, prot, api = self._fixture(status)
        res = reconcile_with_ctx(
            _reconcile_ctx(db, api), apply_changes=True,
            cross_profile_used_ids=set())
        prow = _row(db, prot)
        assert prow["status"] == "closed", res
        assert prow["side"] == "cover", (
            "healed partial cover must net in gvp's FIFO — side='buy' "
            "closed rows are dropped as long lots")
        assert prow["qty"] == 5.0
        assert prow["fill_price"] == pytest.approx(368.55)
        assert prow["pnl"] == pytest.approx(
            round((334.59 - 368.55) * 5, 2))
        # entry stays open; FIFO nets -14 + 5 = -9 == broker
        assert _row(db, entry)["status"] == "open"
        from journal import get_virtual_positions
        vp = {p["symbol"]: p["qty"]
              for p in get_virtual_positions(db_path=db)}
        assert vp.get("GOOGL") == pytest.approx(-9.0)

    def test_heal_is_idempotent(self):
        from reconcile_journal_to_broker import reconcile_with_ctx
        db, entry, prot, api = self._fixture("canceled")
        for _ in range(2):
            reconcile_with_ctx(
                _reconcile_ctx(db, api), apply_changes=True,
                cross_profile_used_ids=set())
        prow = _row(db, prot)
        assert prow["qty"] == 5.0 and prow["status"] == "closed"
        with sqlite3.connect(db) as c:
            n = c.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE order_id='prot-B'").fetchone()[0]
        assert n == 1, "heal must never duplicate the cover row"

    def test_truly_unfilled_canceled_not_healed(self):
        from reconcile_journal_to_broker import reconcile_with_ctx
        db, entry, prot, api = self._fixture("canceled")
        api.orders["prot-B"].filled_qty = 0
        api.orders["prot-B"].filled_avg_price = 0
        # broker still holds the full short in this shape
        api.positions = [("GOOGL", "-14")]
        reconcile_with_ctx(
            _reconcile_ctx(db, api), apply_changes=True,
            cross_profile_used_ids=set())
        assert _row(db, prot)["status"] == "canceled", (
            "zero-fill canceled rows carry no evidence — no heal")
        assert _row(db, entry)["status"] == "open"


class TestDetectionAcceptsTerminalWithFill:
    def test_pointer_walk_accepts_canceled_with_fill(self):
        """Layer 1 (pointer columns) must treat a canceled-with-fill
        terminal order as evidence too."""
        from reconcile_journal_to_broker import _detect_protective_fill
        db = _mk_db()
        entry = _insert(db, protective_trailing_order_id="prot-B")
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "canceled", side="buy", filled_qty=5,
            filled_avg_price=368.55, qty=14)})
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM trades WHERE id=?", (entry,)).fetchone()
            kind, detail = _detect_protective_fill(api, row, set())
        assert kind == "backfill_partial"
        assert detail["filled_qty"] == 5

    def test_active_partially_filled_is_not_evidence(self):
        """A WORKING order's fills may still grow — booking them now
        would strand later increments. Only terminal orders count."""
        from reconcile_journal_to_broker import _detect_protective_fill
        db = _mk_db()
        entry = _insert(db, protective_trailing_order_id="prot-B")
        api = FakeAPI(orders={"prot-B": FakeOrder(
            "prot-B", "partially_filled", side="buy", filled_qty=5,
            filled_avg_price=368.55, qty=14)})
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM trades WHERE id=?", (entry,)).fetchone()
            kind, detail = _detect_protective_fill(api, row, set())
        assert kind is None


class TestOwnExitEvidenceIncludesCanceledFills:
    """_own_exit_fills_explain: a canceled row's broker-verified fills
    are own-exit evidence (vpp bucket); an UNVERIFIABLE canceled row
    contributes nothing rather than poisoning the whole check."""

    def _base(self):
        db = _mk_db()
        _insert(db)  # short 14 open, order entry-A
        _insert(db, timestamp="2026-08-03T13:40:00", side="cover",
                qty=9.0, price=368.0, fill_price=368.0,
                order_id="cover-C", signal_type="SELL",
                status="closed")
        _insert(db, timestamp="2026-07-30T16:39:02", side="buy",
                qty=14.0, price=None, fill_price=None,
                order_id="prot-B",
                signal_type="PROTECTIVE_TRAILING", status="canceled")
        return db

    def _explain(self, db, api):
        from reconcile_journal_to_broker import _own_exit_fills_explain
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            return _own_exit_fills_explain(
                api, conn, db, "GOOGL", "short", {})
        finally:
            conn.close()

    def test_canceled_fill_completes_the_explanation(self):
        db = self._base()
        api = FakeAPI(orders={
            "cover-C": FakeOrder("cover-C", "filled", side="buy",
                                 filled_qty=9, filled_avg_price=368.0,
                                 qty=9, order_type="market"),
            "prot-B": FakeOrder("prot-B", "canceled", side="buy",
                                filled_qty=5, filled_avg_price=368.55,
                                qty=14),
        })
        assert self._explain(db, api) is True, (
            "-14 +9 (verified cover) +5 (canceled-with-fill) == 0 — "
            "fully self-explained, must suppress the false halt")

    def test_unverifiable_canceled_row_contributes_nothing(self):
        db = self._base()
        api = FakeAPI(orders={
            "cover-C": FakeOrder("cover-C", "filled", side="buy",
                                 filled_qty=9, filled_avg_price=368.0,
                                 qty=9, order_type="market"),
            # prot-B unknown at broker → 404 on walk
        })
        assert self._explain(db, api) is False, (
            "evidence-only row unverifiable → no fill counted → net "
            "-5 ≠ 0 → no suppression (but also no hard poison)")


# ---------------------------------------------------------------- E —
# Structural pin: every SQL write of status='canceled' must live in an
# audited function. Each audited site either (a) verified filled_qty==0
# on the terminal broker order, (b) hands filled>0 off to the
# reconciler first, or (c) is a submit-time rollback where no fill can
# exist / the profile halts for review. A new writer failing this test
# must implement the fill-evidence contract, then join the list.
_AUDITED_CANCELED_WRITERS = {
    ("bracket_orders.py", "sync_pending_protective_order_ids"),
    ("bracket_orders.py", "cancel_orphaned_protective_orders"),
    ("bracket_orders.py", "revoke_unbacked_protective_orders"),
    ("reconcile_journal_to_broker.py", "reconcile_with_ctx"),
    ("trader.py", "_handle_phantom_option_close"),
    ("multi_scheduler.py", "_task_update_fills"),
    ("phantom_sweep.py", "sweep_profile"),
    ("options_multileg.py", "_mark_legs_canceled"),
}

# Dated one-off incident scripts (already run, kept for the record) —
# not live code paths; excluded from the live-writer audit.
_ONE_OFF_SCRIPT_PREFIXES = ("repair_", "recover_", "cleanup_",
                            "full_fresh_", "reset_")


class TestCanceledWritersAreAudited:
    def _find_writers(self):
        import ast
        pat = re.compile(r"status\s*=\s*'canceled'", re.I)
        found = set()
        for fname in sorted(os.listdir(REPO)):
            if not fname.endswith(".py"):
                continue
            if fname.startswith(_ONE_OFF_SCRIPT_PREFIXES):
                continue
            path = os.path.join(REPO, fname)
            src = open(path, encoding="utf-8").read()
            hits = [m for m in pat.finditer(src)
                    if "SET" in src[max(0, m.start() - 120):m.start()]
                    .upper()]
            if not hits:
                continue
            tree = ast.parse(src)
            spans = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    spans.append((node.lineno, node.end_lineno,
                                  node.name))
            for m in hits:
                line = src[:m.start()].count("\n") + 1
                fn = None
                for lo, hi, name in spans:
                    if lo <= line <= hi and (
                            fn is None or lo > fn[0]):
                        fn = (lo, name)
                found.add((fname, fn[1] if fn else "<module>"))
        return found

    def test_all_canceled_writers_audited(self):
        found = self._find_writers()
        unaudited = found - _AUDITED_CANCELED_WRITERS
        assert not unaudited, (
            "UNAUDITED status='canceled' writer(s) — a cancel-driven "
            "journal write must verify the broker order's filled_qty "
            "(canceled ≠ unfilled) and book any fills before stamping "
            "canceled; implement the contract, then add the function "
            f"to _AUDITED_CANCELED_WRITERS: {sorted(unaudited)}")
        stale = _AUDITED_CANCELED_WRITERS - found
        assert not stale, (
            "stale _AUDITED_CANCELED_WRITERS entries (writer no "
            "longer exists / renamed) — remove them so they can't "
            f"mask a future writer of the same name: {sorted(stale)}")


class TestOwnRoundtripNeverExternal:
    """2026-08-10 — external means NOT OURS. An option OCC that went
    account-flat because this profile's OWN journaled orders opened
    and closed it is an ordinary close whose state-machine flip
    lagged; stamping it 'auto_closed_external' excludes both legs
    from the cash algebra and vanishes the pair's net premium (the
    p211 AMGN/PM/KO $142 cash residual on account 56)."""

    def _fixture(self, tmp_path, close_status="open"):
        db = _mk_db()
        short = _insert(db, timestamp="2026-08-06T15:31:00",
                        symbol="AMGN", side="sell", qty=2.0,
                        price=4.75, fill_price=4.75,
                        order_id="occ-open", status="open",
                        occ_symbol="AMGN260821P00390000")
        cover = _insert(db, timestamp="2026-08-10T14:43:00",
                        symbol="AMGN", side="buy", qty=2.0,
                        price=2.16, fill_price=2.16,
                        order_id="occ-close", status=close_status,
                        occ_symbol="AMGN260821P00390000")
        api = FakeAPI(orders={
            "occ-open": FakeOrder("occ-open", "filled", side="sell",
                                  filled_qty=2, filled_avg_price=4.75,
                                  qty=2),
            "occ-close": FakeOrder("occ-close", "filled", side="buy",
                                   filled_qty=2, filled_avg_price=2.16,
                                   qty=2),
        })
        return db, short, cover, api

    def _run(self, db, api, monkeypatch):
        import sqlite3 as _sq
        import order_status_cache
        monkeypatch.setattr(order_status_cache, "get_order_cached",
                            lambda a, oid, **kw: a.get_order(oid))
        from reconcile_journal_to_broker import reconcile_option_orphans
        from datetime import date
        conn = _sq.connect(db)
        conn.row_factory = _sq.Row
        try:
            out = reconcile_option_orphans(
                api, conn, [], date(2026, 8, 10), True)
        finally:
            conn.close()
        return out

    def test_flat_own_pair_books_as_ordinary_close(self, tmp_path,
                                                   monkeypatch):
        db, short, cover, api = self._fixture(tmp_path)
        out = self._run(db, api, monkeypatch)
        assert any(d.get("kind") == "own_close" for d in out)
        assert _row(db, short)["status"] == "closed"
        assert _row(db, cover)["status"] == "closed"

    def test_unverified_close_falls_back_to_external_path(
            self, tmp_path, monkeypatch):
        """Close order unknown at broker → NOT provably ours → the
        external-evidence path decides (here: no evidence → leg stays
        open)."""
        db, short, cover, api = self._fixture(tmp_path)
        del api.orders["occ-close"]
        out = self._run(db, api, monkeypatch)
        assert not any(d.get("kind") == "own_close" for d in out)
        assert _row(db, short)["status"] == "open"

    def test_one_sided_flat_still_external_path(self, tmp_path,
                                                monkeypatch):
        """Only the entry exists (true external close shape): the
        own-roundtrip branch must not claim it."""
        db = _mk_db()
        _insert(db, timestamp="2026-08-06T15:31:00",
                symbol="AMGN", side="sell", qty=2.0,
                price=4.75, fill_price=4.75,
                order_id="occ-open", status="open",
                occ_symbol="AMGN260821P00390000")
        api = FakeAPI(orders={
            "occ-open": FakeOrder("occ-open", "filled", side="sell",
                                  filled_qty=2, filled_avg_price=4.75,
                                  qty=2)})
        out = self._run(db, api, monkeypatch)
        assert not any(d.get("kind") == "own_close" for d in out)
