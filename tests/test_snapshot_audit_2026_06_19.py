"""2026-06-19 — the snapshot-replay invariant harness (snapshot_audit.py),
the regression net the phantom-equity incident proved we needed. Unit tests
on hand-authored fixtures passed while the bug shipped, because they encoded
the same wrong model as the code. This harness runs ground-truth invariants
against the REAL corrupt snapshot (p154, frozen in a prod backup) so a
regression of the fix fails loudly here.

The fixture tests/fixtures/p154_corrupt_trades_20260619.sql is the actual
`.dump trades` from quantopsai_profile_154.db captured at the pre-reset
backup — it contains the real UWMC sequence: buy 20634 (entry), the bracket
stop sell 20634 @2.29 (closed the long), and the RE-ARMED trailing-stop sell
20634 @2.18 that filled as an oversell short.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FIXTURE = REPO / "tests" / "fixtures" / "p154_corrupt_trades_20260619.sql"


def _load_fixture(tmp_path):
    """Materialise the real p154 trades dump into a per-profile DB."""
    db = str(tmp_path / "quantopsai_profile_154.db")
    sql = FIXTURE.read_text()
    conn = sqlite3.connect(db)
    conn.executescript(sql)
    conn.commit()
    conn.close()
    return db


def _seed(db, rows):
    """rows: (symbol, side, qty, price, status). occ NULL (stock)."""
    from journal import init_db
    init_db(db)
    c = sqlite3.connect(db)
    for i, (sym, side, qty, px, status) in enumerate(rows):
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "status,order_id) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-06-19T13:00:0%d" % (i % 10), sym, side, qty, px, px,
             status, "oid-%d" % i))
    c.commit()
    c.close()


# ── the real corrupt snapshot pins the fix ─────────────────────────────

def test_real_p154_surfaces_uwmc_oversell_short(tmp_path):
    """Against the ACTUAL corrupt data, get_virtual_positions must surface
    the UWMC oversell as a real short (-20634), not drop it. Revert the fix
    and get_virtual returns 0 for UWMC → this fails."""
    from journal import get_virtual_positions
    db = _load_fixture(tmp_path)
    uwmc = sum(float(p.get("qty") or 0) for p in get_virtual_positions(db)
               if p.get("symbol") == "UWMC" and not p.get("occ_symbol"))
    assert uwmc == -20634.0, (
        "UWMC oversell short must surface (was the dropped-short phantom)")


def test_real_p154_satisfies_order_id_truth(tmp_path):
    """With the fix, every stock symbol in the real snapshot satisfies the
    order-id-truth invariant (get_virtual net == signed-fill net). A
    regression that drops/over-books any position fails here."""
    import snapshot_audit
    db = _load_fixture(tmp_path)
    findings = [f for f in snapshot_audit.audit_profile_snapshot(db)
                if "ORDER-ID-TRUTH DRIFT" in f]
    assert findings == [], "\n".join(findings)


def test_real_p154_decomposition_reconciles_with_fix(tmp_path):
    """The phantom equity (~$45K on p154) is gone: with the short surfaced,
    (equity-capital) reconciles with realized+unrealized. Revert the fix and
    this drifts by ~$45K → fails."""
    import snapshot_audit
    db = _load_fixture(tmp_path)
    findings = [f for f in snapshot_audit.audit_profile_snapshot(
        db, initial_capital=700000.0) if "DECOMPOSITION DRIFT" in f]
    assert findings == [], "\n".join(findings)


# ── the harness actually catches divergence (not a no-op) ──────────────

def test_harness_flags_phantom_divergence(tmp_path):
    """Prove the invariant is live: a state where get_virtual_positions
    disagrees with signed-fill truth (an orphan closed-buy older than a
    held lot — the documented FIFO edge) MUST be flagged."""
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_777.db")
    _seed(db, [
        ("ZZZ", "buy", 100, 50.0, "closed"),   # orphan closed buy (no sell)
        ("ZZZ", "buy", 100, 10.0, "open"),       # genuinely held
        ("ZZZ", "sell", 60, 20.0, "closed"),     # partial trim
    ])
    findings = snapshot_audit.audit_profile_snapshot(db)
    assert any("ORDER-ID-TRUTH DRIFT" in f and "ZZZ" in f for f in findings), (
        "harness must flag a get_virtual vs signed-fill divergence; "
        "got: %s" % findings)


def test_unpriced_sell_with_order_id_is_position_truth_gap(tmp_path):
    """The real p152 BBD case, re-classified after the adversarial
    re-review: a sell with an order_id (it REACHED the broker) but no
    recorded price is a real move the position view drops — phantom-class,
    regardless of status ('open'/'pending_fill'/'closed'). It must NOT
    register as order-id-truth drift (both raw and get_virtual skip
    price<=0), but it IS escalated to a POSITION-TRUTH GAP, not the soft
    data-quality bucket (the residual phantom the first cut missed)."""
    import snapshot_audit
    from journal import init_db
    db = str(tmp_path / "quantopsai_profile_222.db")
    init_db(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO trades (timestamp,symbol,side,qty,price,"
              "fill_price,status,order_id) VALUES (?,?,?,?,?,?,?,?)",
              ("2026-06-19T13:00:00", "QQ", "buy", 100, 10.0, 10.0,
               "open", "b1"))
    c.execute("INSERT INTO trades (timestamp,symbol,side,qty,price,"
              "fill_price,status,order_id) VALUES (?,?,?,?,?,?,?,?)",
              ("2026-06-19T13:01:00", "QQ", "sell", 5, None, None,
               "pending_fill", "s1"))  # reached broker (order_id), no price
    c.commit()
    c.close()
    findings = snapshot_audit.audit_profile_snapshot(db)
    assert not any("ORDER-ID-TRUTH DRIFT" in f for f in findings), findings
    assert any("POSITION-TRUTH GAP" in f for f in findings), findings


def test_clean_book_has_no_findings(tmp_path):
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_888.db")
    _seed(db, [
        ("AAA", "buy", 100, 10.0, "open"),       # held long
        ("BBB", "buy", 50, 4.0, "closed"),       # round trip
        ("BBB", "sell", 50, 4.2, "closed"),
    ])
    assert snapshot_audit.audit_profile_snapshot(db, initial_capital=250000.0) == []


def test_oversell_round_trip_surfaces_short_not_drift(tmp_path):
    """The canonical UWMC shape (buy N, stop sell N, re-armed sell N) nets
    to a -N short AND satisfies order-id truth (get_virtual == signed)."""
    import snapshot_audit
    from journal import get_virtual_positions
    db = str(tmp_path / "quantopsai_profile_999.db")
    _seed(db, [
        ("UWX", "buy", 20634, 2.47, "closed"),
        ("UWX", "sell", 20634, 2.29, "closed"),
        ("UWX", "sell", 20634, 2.18, "closed"),
    ])
    net = sum(float(p.get("qty") or 0) for p in get_virtual_positions(db)
              if p.get("symbol") == "UWX")
    assert net == -20634.0
    assert [f for f in snapshot_audit.audit_profile_snapshot(db)
            if "ORDER-ID-TRUTH" in f] == []


# ── ORDER-ID OWNERSHIP: the invariant the operator named ───────────────
#   "every trade has an alpaca order number ... you don't sell what
#    doesn't belong to that profile." Profile-bleed = one order_id in two
#    books. Orphan-prone = a confirmed fill with no order_id. Both decidable
#    from the order_id alone — no broker, no FIFO.

def _seed_rows(db, rows):
    """rows: list of dicts (symbol, side, qty, price, status, order_id, and
    optional occ_symbol / protective_stop_order_id). price None => unpriced."""
    from journal import init_db
    init_db(db)
    c = sqlite3.connect(db)
    for i, r in enumerate(rows):
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "status,order_id,occ_symbol,protective_stop_order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-06-19T13:%02d:%02d" % (i // 60, i % 60), r["symbol"],
             r["side"], r["qty"], r.get("price"), r.get("price"),
             r.get("status", "open"), r.get("order_id"),
             r.get("occ_symbol"), r.get("protective_stop_order_id")))
    c.commit()
    c.close()


def test_cross_profile_entry_order_id_bleed_is_flagged(tmp_path):
    """The SAME alpaca order_id in two profiles' books = one profile touched
    another's order. MUST be flagged (this is profile-bleed)."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_301.db")
    b = str(tmp_path / "quantopsai_profile_302.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 10, "price": 5.0,
                    "status": "open", "order_id": "SHARED-OID"}])
    _seed_rows(b, [{"symbol": "AAA", "side": "buy", "qty": 10, "price": 5.0,
                    "status": "open", "order_id": "SHARED-OID"}])
    findings = snapshot_audit.audit_order_id_bleed([a, b])
    assert any("ORDER-ID BLEED" in f and "SHARED-OID" in f
               and "301" in f and "302" in f for f in findings), findings


def test_cross_profile_protective_order_id_bleed_is_flagged(tmp_path):
    """A protective order_id owned by two profiles is bleed too (a profile
    armed/cancelled a protective order belonging to another)."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_311.db")
    b = str(tmp_path / "quantopsai_profile_312.db")
    _seed_rows(a, [{"symbol": "BBB", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "e-a",
                    "protective_stop_order_id": "PROT-SHARED"}])
    _seed_rows(b, [{"symbol": "BBB", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "e-b",
                    "protective_stop_order_id": "PROT-SHARED"}])
    findings = snapshot_audit.audit_order_id_bleed([a, b])
    assert any("ORDER-ID BLEED" in f and "PROT-SHARED" in f
               and "protective" in f for f in findings), findings


def test_disjoint_order_ids_no_bleed(tmp_path):
    """Control: every order_id unique to its profile → no bleed (the real
    cohort's actual state — 234 backups, 1,200 order_ids, zero bleed)."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_321.db")
    b = str(tmp_path / "quantopsai_profile_322.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "a-1"}])
    _seed_rows(b, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "b-1"}])
    assert snapshot_audit.audit_order_id_bleed([a, b]) == []


def test_cross_account_bleed_is_worst_case(tmp_path):
    """An order lives on ONE Alpaca account. The same order_id under two
    profiles that route to DIFFERENT accounts means a fill was recorded
    into the wrong account's profile — flagged as CROSS-ACCOUNT BLEED, the
    worst case ('you don't put one account's order into another')."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_401.db")
    b = str(tmp_path / "quantopsai_profile_402.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "X-OID"}])
    _seed_rows(b, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "X-OID"}])
    acct_map = {401: 7, 402: 9}  # profiles route to DIFFERENT accounts
    findings = snapshot_audit.audit_order_id_bleed([a, b],
                                                   account_map=acct_map)
    assert any("CROSS-ACCOUNT BLEED" in f and "X-OID" in f
               and "7" in f and "9" in f for f in findings), findings


def test_same_account_two_profiles_sharing_order_id_still_bleed(tmp_path):
    """Two profiles on the SAME account sharing an order_id is still bleed
    (a fill belongs to exactly one profile), just not cross-account."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_411.db")
    b = str(tmp_path / "quantopsai_profile_412.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "Y-OID"}])
    _seed_rows(b, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "Y-OID"}])
    acct_map = {411: 7, 412: 7}  # SAME account
    findings = snapshot_audit.audit_order_id_bleed([a, b],
                                                   account_map=acct_map)
    assert any("ORDER-ID BLEED" in f and "Y-OID" in f for f in findings)
    assert not any("CROSS-ACCOUNT" in f for f in findings), findings


def test_confirmed_fill_without_order_id_is_flagged(tmp_path):
    """A filled position move with NO alpaca order_id is orphan-prone — the
    order_id can't attribute it. MUST surface (breaks 'every trade has an
    alpaca order number')."""
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_331.db")
    _seed_rows(db, [
        {"symbol": "AAA", "side": "buy", "qty": 10, "price": 5.0,
         "status": "closed", "order_id": None},  # filled, no order_id
        {"symbol": "AAA", "side": "sell", "qty": 10, "price": 5.5,
         "status": "closed", "order_id": "ok-1"},
    ])
    findings = snapshot_audit.audit_profile_snapshot(db)
    assert any("ORDER-ID OWNERSHIP" in f and "order_id" in f
               for f in findings), findings


def test_cross_namespace_bleed_is_flagged(tmp_path):
    """One id space: an id that is an ENTRY order_id in profile A and a
    PROTECTIVE order_id in profile B is still bleed — the bleed check must
    union all id columns, not check entry vs protective separately."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_371.db")
    b = str(tmp_path / "quantopsai_profile_372.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "X-CROSS"}])
    _seed_rows(b, [{"symbol": "BBB", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "e-b",
                    "protective_stop_order_id": "X-CROSS"}])
    findings = snapshot_audit.audit_order_id_bleed([a, b])
    assert any("BLEED" in f and "X-CROSS" in f for f in findings), findings


def test_bleed_tolerates_non_numeric_profile_filename(tmp_path):
    """A snapshot dir may hold a non-numeric profile file (e.g. a backup).
    A shared id across an int-named and a non-numeric-named profile must
    return a finding, never raise on the int/str sort."""
    import snapshot_audit
    a = str(tmp_path / "quantopsai_profile_5.db")
    b = str(tmp_path / "quantopsai_profile_backup.db")
    _seed_rows(a, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "DUP"}])
    _seed_rows(b, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                    "status": "open", "order_id": "DUP"}])
    findings = snapshot_audit.audit_order_id_bleed([a, b])  # must not raise
    assert any("BLEED" in f and "DUP" in f for f in findings), findings


# ── FILLED-BUT-UNPRICED: the false-green the review caught ──────────────

def test_closed_unpriced_fill_is_position_truth_gap_not_soft_note(tmp_path):
    """A 'closed' (terminal-filled) stock row with NO price is a REAL move
    the position view drops — phantom-class. It must escalate to a
    POSITION-TRUTH GAP, NOT the soft data-quality bucket. (Finding 1: a
    filled-but-unpriced row was masked because both raw and get_virtual
    skip price<=0.)"""
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_341.db")
    _seed_rows(db, [
        {"symbol": "AAA", "side": "buy", "qty": 100, "price": 10.0,
         "status": "open", "order_id": "b1"},
        {"symbol": "AAA", "side": "sell", "qty": 100, "price": None,
         "status": "closed", "order_id": "s1"},  # filled, price never set
    ])
    findings = snapshot_audit.audit_profile_snapshot(db)
    assert any("POSITION-TRUTH GAP" in f for f in findings), findings
    # and it is NOT misfiled as the benign data-quality class
    assert not any("DATA-QUALITY" in f for f in findings), findings


def test_unpriced_row_without_order_id_is_dataquality(tmp_path):
    """The discriminator is "did it reach the broker": a row with NO
    order_id was never submitted, so an unpriced one can't be a dropped
    fill — it's a soft data-quality note, NOT a phantom. (An unpriced row
    WITH an order_id is the opposite — see the position-truth test above.)"""
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_342.db")
    _seed_rows(db, [
        {"symbol": "QQ", "side": "buy", "qty": 100, "price": 10.0,
         "status": "open", "order_id": "b1"},
        {"symbol": "QQ", "side": "sell", "qty": 5, "price": None,
         "status": "open", "order_id": None},  # never submitted (no oid)
    ])
    findings = snapshot_audit.audit_profile_snapshot(db)
    assert any("DATA-QUALITY" in f for f in findings), findings
    assert not any("POSITION-TRUTH GAP" in f for f in findings), findings
    assert not any("ORDER-ID-TRUTH DRIFT" in f for f in findings), findings


# ── DECOMPOSITION never silently half-audits (Finding 4) ───────────────

def test_decomposition_skipped_is_surfaced_when_no_capital(tmp_path):
    """If a profile has trades but no initial_capital is available, the
    dollar phantom-equity check did NOT run — that must be SAID, not read
    as clean."""
    import snapshot_audit
    db = str(tmp_path / "quantopsai_profile_351.db")
    _seed_rows(db, [{"symbol": "AAA", "side": "buy", "qty": 1, "price": 5.0,
                     "status": "open", "order_id": "o1"}])
    # explicit missing master → no capital for anyone (hermetic)
    results = snapshot_audit.audit_snapshot_dir(
        str(tmp_path), master_db=str(tmp_path / "no_such_master.db"))
    findings = results.get("quantopsai_profile_351.db", [])
    assert any("DECOMPOSITION SKIPPED" in f for f in findings), results


# ── OPTIONS: order-id-truth shape regressions (Finding 8) ──────────────
#   Option net is not a pure signed sum, so these pin the documented
#   shapes directly through get_virtual_positions.

_OCC = "SPY260116C00500000"


def _occ_net(db, occ):
    from journal import get_virtual_positions
    return sum(float(p.get("qty") or 0) for p in get_virtual_positions(db)
               if p.get("occ_symbol") == occ)


def test_open_option_sell_to_open_surfaces_as_short(tmp_path):
    """An OPEN sell-to-open option leg is a real short — it must surface as
    a -N position (not be dropped like a stock sell once was)."""
    db = str(tmp_path / "quantopsai_profile_361.db")
    _seed_rows(db, [{"symbol": "SPY", "side": "sell", "qty": 2, "price": 1.5,
                     "status": "open", "order_id": "o1", "occ_symbol": _OCC}])
    assert _occ_net(db, _OCC) == -2.0


def test_closed_option_short_leg_nets_zero(tmp_path):
    """The 2026-06-17 orphan class: a RESOLVED ('closed') short option leg
    must NOT spawn a phantom short — it nets to 0."""
    db = str(tmp_path / "quantopsai_profile_362.db")
    _seed_rows(db, [{"symbol": "SPY", "side": "sell", "qty": 2, "price": 1.5,
                     "status": "closed", "order_id": "o1",
                     "occ_symbol": _OCC}])
    assert _occ_net(db, _OCC) == 0.0


def test_phantom_close_option_short_leg_nets_zero(tmp_path):
    """An auto_reconciled_phantom_close short leg is also resolved — nets 0
    (must not resurrect as a phantom short)."""
    db = str(tmp_path / "quantopsai_profile_363.db")
    _seed_rows(db, [{"symbol": "SPY", "side": "sell", "qty": 2, "price": 1.5,
                     "status": "auto_reconciled_phantom_close",
                     "order_id": "o1", "occ_symbol": _OCC}])
    assert _occ_net(db, _OCC) == 0.0


def test_option_round_trip_nets_zero(tmp_path):
    """Buy-to-open then sell-to-close the same option leg nets flat."""
    db = str(tmp_path / "quantopsai_profile_364.db")
    _seed_rows(db, [
        {"symbol": "SPY", "side": "buy", "qty": 2, "price": 1.5,
         "status": "closed", "order_id": "o1", "occ_symbol": _OCC},
        {"symbol": "SPY", "side": "sell", "qty": 2, "price": 1.8,
         "status": "closed", "order_id": "o2", "occ_symbol": _OCC},
    ])
    assert _occ_net(db, _OCC) == 0.0
