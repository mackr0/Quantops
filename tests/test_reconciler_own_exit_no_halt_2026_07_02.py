"""Reconciler safety net: own-exit-in-flight closes must NOT halt the profile
(2026-07-02) — symbol-level verified-fill arithmetic, own-book only.

Measured live flap (21 halt events on 2026-07-02 vs 1 the day before, under
the 5-min cadence): an exit fills at the broker, the exit row IS in this
profile's own journal, but the ENTRY row's terminal flip lags by up to a
sweep interval — the orphan_close check saw "journal long, broker flat,
unexplained" and halted the whole profile for 25s-4min per event (p202
MSFT: own STRONG_SELL journaled 14:22:35, halt fired 14:24:31; VZ same
class via a trailing-stop fill).

`_own_exit_fills_explain` suppresses the halt ONLY when our own book is
self-consistently flat on verified evidence:

    adjusted_own_net == 0   (get_virtual_positions truth; verified
                             protective fills adjust toward flat)
    AND every gvp-credited exit row is broker-verified FILLED on OUR
        OWN order_id, aggregated PER BROKER ORDER (duplicate journal
        rows sharing one order_id are one fill, not two)

Two adversarial-review rounds shaped this; each pin below was a proven
hole in an earlier form: one exit row explaining two lots (per-row round
1; duplicate-order_id round 2); an OWN option leg explaining a stock
phantom; journal qty trusted over broker filled_qty; phantom-'closed'
trust (+ its `auto_reconciled_phantom_close` sibling — the candidate
predicate is the COMPLEMENT of gvp's dead set, so it cannot drift from
what j_net credits); no replace-chain walk; scaled-out false-halts;
SHORT protectives journaled side='buy' (the production shape — the
round-1 test used 'cover', which no writer produces); and comparison
against the shared conduit's AGGREGATE qty (own-book net==0 only —
a sibling's holding must never mask our missing shares). No fuzzy
matching, no sibling attribution (A3 intact). Fail-CLOSED on any error.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from reconcile_journal_to_broker import _own_exit_fills_explain


class FakeOrder:
    def __init__(self, status, filled_qty=0, replaced_by=None, side=None):
        self.status = status
        self.filled_qty = filled_qty
        self.replaced_by = replaced_by
        if side is not None:
            self.side = side


class FakeApi:
    """Broker stub keyed by order_id. Records every lookup so tests can
    pin that only OWN order_ids are queried, each broker order at most
    once per evaluation, and that option-row / settled-history evidence
    is never even fetched."""

    def __init__(self, orders=None):
        self.orders = orders or {}
        self.calls = []

    def get_order(self, oid):
        self.calls.append(oid)
        if oid not in self.orders:
            raise RuntimeError(f"order {oid} not found")
        return self.orders[oid]


@pytest.fixture
def db(tmp_path):
    """Real DB file — the helper reads it twice: via the passed conn AND
    via journal.get_virtual_positions(db_path=...)."""
    path = str(tmp_path / "profile_test.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, "
        "symbol TEXT, side TEXT, qty REAL, price REAL, order_id TEXT, "
        "status TEXT, occ_symbol TEXT, data_quality TEXT, "
        "stop_loss REAL, take_profit REAL)")
    conn.commit()
    yield conn, path
    conn.close()


def _add(conn, ts, symbol, side, qty, order_id, status, occ=None):
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, price, "
        "order_id, status, occ_symbol) VALUES (?, ?, ?, ?, 100.0, ?, ?, ?)",
        (ts, symbol, side, qty, order_id, status, occ))
    conn.commit()


T0 = "2026-07-02T14:06:57"   # entry
T1 = "2026-07-02T14:10:00"
T2 = "2026-07-02T14:22:35"   # exit


def _explain(api, db, symbol, entry_side="buy", memo=None):
    conn, path = db
    return _own_exit_fills_explain(api, conn, path, symbol, entry_side,
                                   memo if memo is not None else {})


# ---------------------------------------------------------------- explained

def test_msft_flap_pending_fill_exit_verified(db):
    # p202 MSFT: entry open, own STRONG_SELL pending_fill, broker filled,
    # entry flip lagging; canceled protectives present → explained.
    conn, _ = db
    _add(conn, T0, "MSFT", "buy", 13, "entry001", "open")
    _add(conn, T2, "MSFT", "sell", 13, "9fbec087", "pending_fill")
    _add(conn, T1, "MSFT", "sell", 13, "dd6bac91", "canceled")
    api = FakeApi({"9fbec087": FakeOrder("filled", 13, side="sell")})
    assert _explain(api, db, "MSFT") is True
    # Only OUR OWN order ids are ever queried (A3) — the entry id is
    # never fetched. 2026-08-03: the recent canceled protective IS now
    # queried (canceled ≠ unfilled — its fills would be evidence); it
    # is unknown at the broker here, so it contributes nothing and the
    # explanation still holds on the live exit alone.
    assert "9fbec087" in api.calls
    assert "entry001" not in api.calls
    assert set(api.calls) <= {"9fbec087", "dd6bac91"}


def test_vz_trailing_stop_pending_protective_filled(db):
    conn, _ = db
    _add(conn, T0, "VZ", "buy", 213, "entry002", "open")
    _add(conn, T1, "VZ", "sell", 213, "8e93ae8d", "pending_protective")
    api = FakeApi({"8e93ae8d": FakeOrder("filled", 213, side="sell")})
    assert _explain(api, db, "VZ") is True


def test_replaced_trailing_stop_chain_walked(db):
    # Alpaca silently replaced the trailing stop; the journal id is
    # mid-chain. The fill lives on the terminal id — walked forward on
    # OUR OWN ids. Without the walk this flap class still false-halts.
    conn, _ = db
    _add(conn, T0, "KO", "buy", 50, "entry003", "open")
    _add(conn, T1, "KO", "sell", 50, "mid00001", "pending_protective")
    api = FakeApi({
        "mid00001": FakeOrder("replaced", 0, replaced_by="term0001"),
        "term0001": FakeOrder("filled", 50, side="sell"),
    })
    assert _explain(api, db, "KO") is True
    assert api.calls == ["mid00001", "term0001"]


def test_scaled_out_position_fully_explained(db):
    # Entry 20; 10 already sold + confirmed (closed); remaining 10 exiting
    # in-flight (filled, flip lagging). Own net 0 on verified fills.
    conn, _ = db
    _add(conn, T0, "AMD", "buy", 20, "entry004", "open")
    _add(conn, T1, "AMD", "sell", 10, "sold0001", "closed")
    _add(conn, T2, "AMD", "sell", 10, "sell0002", "pending_fill")
    api = FakeApi({
        "sold0001": FakeOrder("filled", 10, side="sell"),
        "sell0002": FakeOrder("filled", 10, side="sell"),
    })
    assert _explain(api, db, "AMD") is True


def test_short_protective_buy_shape_filled(db):
    # THE PRODUCTION SHORT SHAPE (round-2 HIGH): protectives for shorts
    # are journaled side='buy' (bracket_orders close_side), NOT 'cover'.
    # The round-1 test pinned a 'cover' protective no writer produces —
    # mock-parity failure class — leaving every short's protective close
    # still false-halting.
    conn, _ = db
    _add(conn, T0, "TSLA", "short", 5, "entry005", "open")
    _add(conn, T1, "TSLA", "buy", 5, "prot0001", "pending_protective")
    api = FakeApi({"prot0001": FakeOrder("filled", 5, side="buy")})
    assert _explain(api, db, "TSLA", entry_side="short") is True


def test_short_reconciler_flipped_closed_buy_protective(db):
    # After the pending-UPDATE flips the protective row to 'closed' it
    # keeps side='buy'; gvp drops unconsumed closed buys (never nets
    # them against short lots), so the verified fill must adjust the
    # net during the entry-flip lag window.
    conn, _ = db
    _add(conn, T0, "RIVN", "short", 30, "entry006", "open")
    _add(conn, T2, "RIVN", "buy", 30, "prot0002", "closed")
    api = FakeApi({"prot0002": FakeOrder("filled", 30, side="buy")})
    assert _explain(api, db, "RIVN", entry_side="short") is True


def test_short_cover_exit_pending_fill(db):
    # Explicit cover exits (trader cover path) are gvp-credited.
    conn, _ = db
    _add(conn, T0, "PLTR", "short", 12, "entry007", "open")
    _add(conn, T2, "PLTR", "cover", 12, "cover001", "pending_fill")
    api = FakeApi({"cover001": FakeOrder("filled", 12, side="buy")})
    assert _explain(api, db, "PLTR", entry_side="short") is True


# -------------------------------------------------- NOT explained → halt

def test_resting_unfilled_protective_still_halts(db):
    # Externally closed while our stop rests UNFILLED: the row exists but
    # the broker says not filled → genuine divergence → halt.
    conn, _ = db
    _add(conn, T0, "NVDA", "buy", 8, "entry008", "open")
    _add(conn, T1, "NVDA", "sell", 8, "resting1", "pending_protective")
    api = FakeApi({"resting1": FakeOrder("new", 0, side="sell")})
    assert _explain(api, db, "NVDA") is False


def test_one_exit_row_cannot_explain_two_lots(db):
    # Round-1 CRITICAL: two open 10-share lots, ONE verified 10-share own
    # exit, broker flat — 10 shares genuinely unexplained. Arithmetic
    # halts (own net 10 != 0).
    conn, _ = db
    _add(conn, T0, "ORCL", "buy", 10, "entry009", "open")
    _add(conn, T1, "ORCL", "buy", 10, "entry010", "open")
    _add(conn, T2, "ORCL", "sell", 10, "sell0004", "pending_fill")
    api = FakeApi({"sell0004": FakeOrder("filled", 10, side="sell")})
    memo = {}
    assert _explain(api, db, "ORCL", memo=memo) is False
    # memoized: a second phantom lot of the same symbol re-uses the
    # verdict without re-querying the broker
    n_calls = len(api.calls)
    assert _explain(api, db, "ORCL", memo=memo) is False
    assert len(api.calls) == n_calls


def test_duplicate_rows_sharing_one_order_id_are_one_fill(db):
    # Round-2 HIGH: duplicate exit rows carrying the SAME order_id (a
    # documented live race: bracket-child exemption + A0 minimal-INSERT
    # are both second writers) must be explained by ONE broker fill.
    # Two 10-share rows on one 10-share fill leave 10 shares
    # unexplained → halt; and the broker order is fetched ONCE.
    conn, _ = db
    _add(conn, T0, "SOFI", "buy", 10, "entry011", "open")
    _add(conn, T0, "SOFI", "buy", 10, "entry012", "open")
    _add(conn, T2, "SOFI", "sell", 10, "dupX0001", "pending_fill")
    _add(conn, T2, "SOFI", "sell", 10, "dupX0001", "pending_fill")
    api = FakeApi({"dupX0001": FakeOrder("filled", 10, side="sell")})
    assert _explain(api, db, "SOFI") is False
    assert api.calls == ["dupX0001"]


def test_option_row_never_explains_stock_phantom(db):
    # Round-1 HIGH: an OWN option leg (side='sell', symbol=underlying,
    # qty in CONTRACTS) must not count as stock-exit evidence — repeat
    # of the 2026-06-05 FIFO-mixing class.
    conn, _ = db
    _add(conn, T0, "NFLX", "buy", 5, "entry013", "open")
    _add(conn, T2, "NFLX", "sell", 5, "optleg01", "pending_fill",
         occ="NFLX260117C01000000")
    api = FakeApi({"optleg01": FakeOrder("filled", 5, side="sell")})
    assert _explain(api, db, "NFLX") is False
    assert "optleg01" not in api.calls  # never even fetched


def test_phantom_closed_sell_does_not_suppress(db):
    # Round-1 MEDIUM: legacy paths wrote SELL status='closed' at submit
    # and Alpaca async-canceled. A recent 'closed' sell is verified at
    # the broker before it may support suppression.
    conn, _ = db
    _add(conn, T0, "NKE", "buy", 10, "entry014", "open")
    _add(conn, T2, "NKE", "sell", 10, "phantom1", "closed")
    api = FakeApi({"phantom1": FakeOrder("canceled", 0, side="sell")})
    assert _explain(api, db, "NKE") is False


def test_auto_reconciled_phantom_close_sell_is_dead_everywhere(db):
    # 2026-07-14 (engine unification): 'auto_reconciled_phantom_close'
    # means "no money moved" and is now in EVERY engine's dead set —
    # gvp's exit side stopped crediting it (the p214 CVX identity
    # drift), so the helper's complement-predicate excludes it too.
    # The arpc sell contributes nothing: the entry lot stays intact,
    # own net != 0, halt — and the dead row is never even fetched.
    conn, _ = db
    _add(conn, T0, "GT", "buy", 10, "entry015", "open")
    _add(conn, T1, "GT", "sell", 10, "arpc0001",
         "auto_reconciled_phantom_close")
    api = FakeApi({"arpc0001": FakeOrder("canceled", 0, side="sell")})
    assert _explain(api, db, "GT") is False
    assert api.calls == []  # dead rows are not candidates at all


def test_partial_broker_fill_not_trusted_for_full_credit(db):
    # Round-1 HIGH: journal row qty is never trusted over broker
    # filled_qty (replaced-down protective class).
    conn, _ = db
    _add(conn, T0, "AAPL", "buy", 10, "entry016", "open")
    _add(conn, T2, "AAPL", "sell", 10, "partf001", "pending_fill")
    api = FakeApi({"partf001": FakeOrder("partially_filled", 4,
                                         side="sell")})
    assert _explain(api, db, "AAPL") is False


def test_own_book_not_flat_never_compares_aggregate(db):
    # Round-2 MEDIUM: the criterion is OWN net == 0, never a match
    # against the shared conduit's aggregate. Entry 20 with only 12
    # verified sold leaves OUR book claiming 8 — even if a sibling's 8
    # shares make the aggregate "look right", we halt.
    conn, _ = db
    _add(conn, T0, "WMT", "buy", 20, "entry017", "open")
    _add(conn, T2, "WMT", "sell", 12, "sell0005", "pending_fill")
    api = FakeApi({"sell0005": FakeOrder("filled", 12, side="sell")})
    assert _explain(api, db, "WMT") is False


def test_broker_order_side_mismatch_rejected(db):
    # A journal exit row pointing at a NON-exit broker order (e.g. a
    # synthesized row carrying an entry id) is never fill-evidence.
    conn, _ = db
    _add(conn, T0, "DKNG", "buy", 15, "entry018", "open")
    _add(conn, T2, "DKNG", "sell", 15, "entry018", "pending_fill")
    api = FakeApi({"entry018": FakeOrder("filled", 15, side="buy")})
    assert _explain(api, db, "DKNG") is False


def test_open_long_lot_on_short_symbol_distrusted(db):
    # side='buy' status='open' coexisting with our short is a live LONG
    # lot, not exit evidence — arithmetic can't be trusted → halt.
    conn, _ = db
    _add(conn, T0, "LCID", "short", 25, "entry019", "open")
    _add(conn, T1, "LCID", "buy", 25, "longlot1", "open")
    api = FakeApi({"longlot1": FakeOrder("filled", 25, side="buy")})
    assert _explain(api, db, "LCID", entry_side="short") is False


def test_spent_evidence_closed_sell_consumed_by_closed_entry(db):
    # Closed sell already consumed by a PREVIOUS (closed) entry cannot
    # explain a later lot's external close: FIFO nets to 10 ≠ 0.
    conn, _ = db
    _add(conn, "2026-07-01T10:00:00", "T", "buy", 10, "oldentry", "closed")
    _add(conn, "2026-07-01T15:00:00", "T", "sell", 10, "oldsell1", "closed")
    _add(conn, T0, "T", "buy", 10, "entry020", "open")
    api = FakeApi({"oldsell1": FakeOrder("filled", 10, side="sell")})
    assert _explain(api, db, "T") is False
    # settled history (older than the oldest open entry) is not fetched
    assert api.calls == []


def test_no_exit_rows_at_all_halts(db):
    conn, _ = db
    _add(conn, T0, "GM", "buy", 30, "entry021", "open")
    api = FakeApi()
    assert _explain(api, db, "GM") is False
    assert api.calls == []


def test_offsetting_oversell_remnant_is_not_vacuous_truth(db):
    # Round-3 MEDIUM: a pre-entry unmatched closed sell (oversell
    # remnant) books a gvp short lot that offsets the open buy —
    # j_net == 0 with ZERO exit evidence. Vacuously-true verification
    # must not suppress: no verified own-exit fill → halt for review.
    conn, _ = db
    _add(conn, "2026-07-01T09:00:00", "SNAP", "sell", 10, "remnant1",
         "closed")
    _add(conn, T0, "SNAP", "buy", 10, "entry024", "open")
    api = FakeApi()
    assert _explain(api, db, "SNAP") is False
    assert api.calls == []  # pre-window remnant never fetched


def test_zombie_buy_plus_short_pair_halts(db):
    # Round-3 MEDIUM sibling: corrupt simultaneous open buy + open
    # short nets j_net to 0 with no exit rows at all — the old code
    # halted these for operator review; the fill floor keeps that.
    conn, _ = db
    _add(conn, T0, "BB", "buy", 15, "entry025", "open")
    _add(conn, T0, "BB", "short", 15, "entry026", "open")
    api = FakeApi()
    assert _explain(api, db, "BB") is False


def test_sideless_broker_order_is_unverifiable(db):
    # Fail-CLOSED: a broker order object with no side attribute is
    # unverifiable evidence, not a pass.
    conn, _ = db
    _add(conn, T0, "UAL", "buy", 9, "entry027", "open")
    _add(conn, T2, "UAL", "sell", 9, "noside01", "pending_fill")
    api = FakeApi({"noside01": FakeOrder("filled", 9)})  # side absent
    assert _explain(api, db, "UAL") is False


def test_credited_row_with_null_order_id_halts(db):
    # A gvp-credited exit row with NO order_id is unverifiable evidence
    # — it must force a halt, never be skipped as if absent.
    conn, _ = db
    _add(conn, T0, "F", "buy", 40, "entry022", "open")
    _add(conn, T2, "F", "sell", 40, None, "pending_fill")
    api = FakeApi()
    assert _explain(api, db, "F") is False
    assert api.calls == []


# ------------------------------------------------------------- fail-closed

def test_unverifiable_order_fails_closed(db, monkeypatch):
    import reconcile_journal_to_broker as rjb
    monkeypatch.setattr(rjb, "_API_MAX_RETRIES", 1)
    conn, _ = db
    _add(conn, T0, "GE", "buy", 40, "entry023", "open")
    _add(conn, T2, "GE", "sell", 40, "borked01", "pending_fill")
    api = FakeApi()  # get_order raises for unknown ids
    assert _explain(api, db, "GE") is False


def test_db_error_fails_closed(db):
    class _Boom:
        def execute(self, *a):
            raise RuntimeError("db exploded")
    _, path = db
    assert _own_exit_fills_explain(FakeApi(), _Boom(), path, "GE", "buy",
                                   {}) is False


# ------------------------------------------------------------ structural

def test_orphan_close_branch_consults_the_check():
    # Structural pin: the safety-net orphan_close branch must call the
    # own-exit-fill check BEFORE appending (before it can count toward
    # the halt). If a refactor drops it, the 25s-4min full-profile halt
    # flaps on every routine exit-fill race return.
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "reconcile_journal_to_broker.py")).read()
    before_append = src.split('actions["orphan_close"].append')[0]
    # one hit is the def itself; a second hit before the append is the
    # actual guard call in the branch
    assert before_append.count("_own_exit_fills_explain(") >= 2, (
        "orphan_close must consult _own_exit_fills_explain before halting")


def test_candidate_predicate_matches_gvp_dead_set():
    # The helper's dead set must stay the complement of gvp's exit-side
    # exclusions MINUS the evidence trio (pending_protective, canceled,
    # expired), which the helper handles as the v_pp bucket — selected
    # for their broker-verified fills but never credited in j_net.
    # 2026-08-03 (canceled ≠ unfilled): 'canceled'/'expired' moved from
    # hard-dead to the evidence trio; a canceled protective's partial
    # fills are own-exit evidence. If gvp's exit-side dead set gains a
    # status without the helper following, credited-but-unverified
    # evidence returns (the round-2 auto_reconciled_phantom_close hole).
    helper_src = open(os.path.join(
        os.path.dirname(__file__), os.pardir,
        "reconcile_journal_to_broker.py")).read()
    helper_body = helper_src.split("def _own_exit_fills_explain")[1]
    helper_body = helper_body.split("\ndef ")[0]
    flat = (helper_body.replace("\"", "").replace("\n", "")
            .replace(" ", ""))
    assert ("NOTIN('rejected','done_for_day',"
            "'auto_reconciled_phantom_close',"
            "'auto_closed_external')" in flat), (
        "helper hard-dead set changed — re-derive from gvp's "
        "exit-side list minus the evidence trio")
    assert "ifstatus_rin(pending_protective,canceled,expired):" in flat, (
        "the evidence trio (pending_protective/canceled/expired → vpp "
        "bucket, never credited) is no longer intact")
    gvp_src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                                "journal.py")).read()
    # BOTH query variants (primary + legacy-schema fallback) must
    # stay unified — a de-unified fallback re-creates the drift on
    # legacy DBs (round-2 review L2)
    parts = gvp_src.split("(side IN ('sell', 'cover') AND ")
    assert len(parts) >= 3, "expected two gvp exit-side branches"
    for exit_block in (parts[1][:900], parts[2][:900]):
      for status in ("canceled", "expired", "rejected", "done_for_day",
                     "pending_protective", "auto_reconciled_phantom_close",
                     "auto_closed_external"):
        assert status in exit_block, (
            f"gvp exit-side exclusion lost '{status}' — helper must be "
            "re-aligned (its dead set is gvp's minus pending_protective)")


def test_fill_confirm_flips_stock_open_exits_but_not_option_legs():
    # multi_scheduler's confirm branch now includes STOCK 'open' exits
    # (the trader's pnl-unavailable shape) so a filled exit can't leave
    # its entry lot open forever — but it must stay scoped occ_symbol
    # IS NULL, or an option sell-to-open leg would flip to 'closed' on
    # fill (the 2026-06-05 broker_orphan drift class). Status is
    # NULL-coalesced to match the feeding SELECT's semantics.
    src = open(os.path.join(os.path.dirname(__file__), os.pardir,
                            "multi_scheduler.py")).read()
    assert "_stock_open_exit" in src
    guard = src.split("_stock_open_exit = (")[1][:200]
    assert '(trade["status"] or "open") == "open"' in guard
    assert 'not trade["occ_symbol"]' in guard
    cond = src.split("or _stock_open_exit)")[0]
    assert '"pending_fill"' in cond.rsplit("if ((", 1)[-1]
