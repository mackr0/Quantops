"""2026-06-19 — property tests for the position-truth harness.

Two complementary properties, both driven through the REAL journal code:

  SOUNDNESS (clean data) — for any lifecycle-consistent fill stream,
    get_virtual_positions' net must equal the broker net the generator
    tracked independently as it emitted each fill, AND snapshot_audit must
    report NO serious finding. (clean books → clean harness)

  COMPLETENESS (corrupt data) — for a stream with INJECTED corruption the
    live code's known failure modes can produce (an orphan 'closed' buy
    older than a held lot; a 'closed' fill with no price), snapshot_audit
    MUST report a finding. It must never stay SILENT while the books are
    wrong. (corrupt books → harness fires)

The 2026-06-19 adversarial review showed the FIRST cut of this test was a
false-green: its generator could only emit consistent streams, and it
computed `expected` with the SAME signed-sum formula the audit uses — so it
could only ever re-confirm an identity, never fail on the one shape the
harness was built to surface. The completeness half below fixes that: the
generator now emits the orphan/unpriced shapes, and we assert the harness
catches them (not that get_virtual is magically always right).

Deterministic (fixed seeds), so any failure reproduces.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from collections import deque
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Findings that mean "the books are actually wrong" (vs the soft
# data-quality note for a resting/unfilled row).
_SERIOUS = ("ORDER-ID-TRUTH DRIFT", "POSITION-TRUTH GAP", "ORDER-ID OWNERSHIP")


def _build_db(db, events):
    """events: list of (side, qty, status, price). price None => unpriced."""
    from journal import init_db
    init_db(db)
    c = sqlite3.connect(db)
    for i, (side, qty, status, price) in enumerate(events):
        c.execute(
            "INSERT INTO trades (timestamp,symbol,side,qty,price,fill_price,"
            "status,order_id) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-06-19T13:%02d:%02d" % (i // 60, i % 60), "SYM", side,
             qty, price, price, status, "oid-%d" % i))
    c.commit()
    c.close()


def _gen_lifecycle(rng):
    """Generate a random, lifecycle-consistent stream for one stock symbol,
    with statuses set the way the live close machinery would (a buy fully
    consumed by sells flips 'closed', its consuming sell 'closed'; a
    partially-consumed buy stays 'open'; an oversell is a 'closed' row with
    no lot left). `broker_net` is tracked INDEPENDENTLY — incremented as
    each fill is emitted (the broker's own view), not recomputed from the
    rows — and returned as the oracle. Returns (events, broker_net)."""
    events = []
    lots = deque()  # open buy lots: [qty, event_index]
    broker_net = 0
    n_ops = rng.randint(1, 12)
    for _ in range(n_ops):
        op = rng.choice(["buy", "buy", "trim", "close_all", "oversell"])
        if op == "buy":
            q = rng.randint(1, 500)
            events.append(["buy", q, "open", 10.0])
            lots.append([q, len(events) - 1])
            broker_net += q
        elif op == "trim" and lots:
            held = sum(l[0] for l in lots)
            q = rng.randint(1, max(1, held))
            events.append(["sell", q, "closed", 10.0])
            broker_net -= q
            rem = q
            while rem > 0 and lots:
                take = min(lots[0][0], rem)
                lots[0][0] -= take
                rem -= take
                if lots[0][0] <= 0:
                    events[lots[0][1]][2] = "closed"  # buy fully consumed
                    lots.popleft()
        elif op == "close_all" and lots:
            held = sum(l[0] for l in lots)
            events.append(["sell", held, "closed", 10.0])
            broker_net -= held
            for l in lots:
                events[l[1]][2] = "closed"
            lots.clear()
        elif op == "oversell":
            held = sum(l[0] for l in lots)
            q = held + rng.randint(1, 300)
            events.append(["sell", q, "closed", 10.0])
            broker_net -= q
            for l in lots:
                events[l[1]][2] = "closed"
            lots.clear()
        # else: no-op (e.g. trim/close with nothing held)
    return events, broker_net


def _gen_corrupt(rng):
    """Generate a stream with INJECTED corruption the live failure modes can
    produce. Returns (events, kind). The harness MUST report a finding for
    each — that's the anti-false-green property."""
    kind = rng.choice(
        ["orphan_closed_buy", "closed_unpriced", "pending_fill_unpriced"])
    if kind == "orphan_closed_buy":
        # An ORPHAN 'closed' buy (no matching sell) OLDER than a genuinely-
        # held open buy. A later partial sell mis-FIFOs onto the orphan, so
        # get_virtual's net diverges from the signed-fill truth (the
        # documented KNOWN LIMITATION). a-b > 0 by construction → drift.
        a = rng.randint(50, 300)
        b = rng.randint(10, 40)
        return ([["buy", a, "closed", 10.0],    # orphan, no consuming sell
                 ["buy", a, "open", 10.0],       # genuinely held lot
                 ["sell", b, "closed", 10.0]],   # partial sell
                kind)
    # *_unpriced: a sell that reached the broker (it gets an order_id in
    # _build_db) but carries no price. The view drops it → a phantom long.
    # Two fill-bearing statuses: 'closed' and 'pending_fill' (the latter is
    # the residual phantom the first cut missed by keying only on 'closed').
    a = rng.randint(10, 200)
    status = "closed" if kind == "closed_unpriced" else "pending_fill"
    return ([["buy", a, "open", 10.0],
             ["sell", a, status, None]], kind)


# ── SOUNDNESS: clean data → get_virtual == broker net, harness clean ───

@pytest.mark.parametrize("seed", list(range(60)))
def test_clean_lifecycles_match_broker_net_and_audit_clean(seed, tmp_path):
    import snapshot_audit
    from journal import get_virtual_positions
    rng = random.Random(seed)
    events, broker_net = _gen_lifecycle(rng)
    db = str(tmp_path / "quantopsai_profile_1.db")
    _build_db(db, events)
    net = sum(float(p.get("qty") or 0) for p in get_virtual_positions(db)
              if p.get("symbol") == "SYM" and not p.get("occ_symbol"))
    assert net == broker_net, (
        "get_virtual net != independently-tracked broker net\n"
        "events=%s\nbroker_net=%s got=%s" % (events, broker_net, net))
    serious = [f for f in snapshot_audit.audit_profile_snapshot(db)
               if any(s in f for s in _SERIOUS)]
    assert serious == [], "clean stream flagged:\n%s" % "\n".join(serious)


# ── COMPLETENESS: corrupt data → harness MUST fire (never silent) ──────

@pytest.mark.parametrize("seed", list(range(40)))
def test_corrupt_streams_are_always_flagged(seed, tmp_path):
    """The property the first cut missed: for an injected corruption the
    harness must NOT be silent — it must report a serious finding. For the
    unpriced kinds we ALSO bind the assertion to get_virtual's real output
    (the phantom long must be present), so the finding tracks production
    reconstruction behavior rather than a static SQL count."""
    import snapshot_audit
    from journal import get_virtual_positions
    rng = random.Random(5000 + seed)
    events, kind = _gen_corrupt(rng)
    db = str(tmp_path / "quantopsai_profile_1.db")
    _build_db(db, events)
    findings = snapshot_audit.audit_profile_snapshot(db)
    serious = [f for f in findings if any(s in f for s in _SERIOUS)]
    assert serious, (
        "harness stayed SILENT on corrupt data (kind=%s)\nevents=%s\n"
        "all findings=%s" % (kind, events, findings))
    if kind in ("closed_unpriced", "pending_fill_unpriced"):
        # the unpriced sell reached the broker (true net 0) but get_virtual
        # drops it → a phantom long. If get_virtual were changed to net it,
        # this notices — the POSITION-TRUTH GAP is not a static-only signal.
        net = sum(float(p.get("qty") or 0) for p in get_virtual_positions(db)
                  if p.get("symbol") == "SYM" and not p.get("occ_symbol"))
        assert net != 0, (
            "expected get_virtual to show the phantom long for %s" % kind)
        assert any("POSITION-TRUTH GAP" in f for f in findings), findings
