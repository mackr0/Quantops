#!/usr/bin/env python3
"""Offline book-integrity audit of per-profile DB snapshots.

The 2026-06-18 phantom-equity incident slipped past every unit test because
those tests run on fixtures I author by hand — they encode my (sometimes
wrong) mental model, so they pass even when the model is wrong. The bug
only showed up against REAL data (the operator eyeballing p154) and
adversarial review. This harness closes that gap: point it at real
per-profile DB snapshots (e.g. a prod backup) and it runs ground-truth
invariants the journaling code must satisfy.

WHAT THIS COVERS (offline — no live broker):

  1. ORDER-ID TRUTH (per profile, STOCK) — for every stock symbol, the net
     get_virtual_positions() reports must equal the signed sum of that
     profile's own filled orders (Σ buy+cover − Σ sell+short over confirmed
     fills). A mismatch is a position the journal's own fills don't support
     — a dropped oversell short (the UWMC phantom) or a phantom long.

  2. ORDER-ID OWNERSHIP (cross profile) — the load-bearing isolation
     invariant: every fill-bearing row carries an alpaca order_id, and NO
     order_id (entry OR protective_*) appears in more than one profile's
     book. This is what makes profile-bleed and orphans decidable from the
     order_id alone — "you never touch what isn't yours." Stocks AND
     options. Empirically held across the whole 06-17/06-18 corrupt cohort
     (234 backups, 1,200 order_ids, zero bleed), so the incident was a
     single-profile reconstruction bug, never bleed — but this pins it so a
     regression fails loudly instead of by eye.

  3. FILLED-BUT-UNPRICED (per profile) — a STOCK row marked 'closed'
     (terminal-filled) with no recorded price is a real position move the
     position view silently drops → phantom-class. Escalated, not a soft
     note. A non-'closed' unpriced row (e.g. a resting/abandoned sell) is a
     softer data-quality finding (it likely never filled; both raw and
     get_virtual correctly skip it — the real p152 BBD 'open' sell).

  4. DECOMPOSITION (per profile, $) — (equity − initial_capital) must equal
     realized + unrealized P&L. Valued at cost, this catches equity not
     backed by booked P&L + held positions (phantom equity). Needs the
     profile's initial_capital; if a profile has trades but no capital is
     available it is reported as SKIPPED, never silently half-audited.

WHAT THIS DOES NOT COVER (by construction — needs a live broker):

  • JOURNAL vs BROKER. Every check here is journal-internal +
    cross-profile-consistent. A journal that is internally consistent but
    disagrees with the actual broker position (a fill that bypassed
    journaling entirely, a manual broker action) is invisible HERE. That
    reconciliation is the LIVE job of certify_books.check_broker_drift (the
    per-cycle integrity gate) and aggregate_audit (manual-order /
    per-account Σ==broker). This offline harness is the regression net for
    the journal's own math + ownership; it is not a broker reconciler.
  • OPTION position-truth as a directory invariant. An option net is not a
    pure signed sum (a 'closed' sell-to-open leg is a resolved round-trip
    recorded as ONE row → nets 0), so a generic option Σ check would just
    re-encode get_virtual's own rules (circular). Option order-id OWNERSHIP
    is covered (check 2); option position shapes are pinned by fixture
    regression tests (tests/test_snapshot_audit_*.py).

Usage:
    python snapshot_audit.py <snapshot_dir> [--master <master.db>]
    # audits every quantopsai_profile_<id>.db in <snapshot_dir>; reads
    # initial_capital from <master.db> (defaults to <dir>/quantopsai.db
    # then ./quantopsai.db).

Exit code 0 = all clean; 1 = findings (so it can gate CI / a cron).
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from typing import Dict, List, Optional, Sequence, Tuple

# Never-filled statuses: these rows did not move the broker position, so
# they're excluded from the order-id-truth sum (mirrors get_virtual_positions
# and certify_books).
_NEVER_FILLED = (
    "canceled", "expired", "rejected", "done_for_day",
    "pending_protective", "auto_reconciled_phantom_close",
)
# The only TERMINAL-FILLED status in this schema (grounded in the real
# cohort: statuses are open/canceled/pending_protective/closed/expired). A
# 'closed' row completed its lifecycle, so it filled — you don't 'close' an
# order that never filled (those go canceled/expired). So 'closed' + no
# price = a filled row whose price never backfilled = a real position move.
_TERMINAL_FILLED = ("closed",)
# Tolerance for the dollar decomposition check (fee/slippage/rounding noise).
_DECOMP_TOL = 50.0

# Synthetic "profile" key under which cross-profile (directory-level)
# findings are reported by audit_snapshot_dir.
_CROSS_PROFILE_KEY = "<cross-profile>"


def _confirmed_fill_clause() -> str:
    """SQL fragment: a row that actually FILLED (a real position move) —
    not a never-filled status, and with a positive price. Shared by the
    raw signed-net sum and the untracked-fill check so they agree."""
    ph = ",".join("?" * len(_NEVER_FILLED))
    return ("COALESCE(status,'open') NOT IN (%s) "
            "AND COALESCE(NULLIF(fill_price, 0), price) > 0" % ph)


def _raw_signed_net_by_symbol(db_path: str) -> Dict[str, float]:
    """Order-id truth per STOCK symbol: Σ(buy+cover) − Σ(sell+short) over
    confirmed fills. This is what the broker net MUST be for this profile's
    own orders.

    A row counts only if it actually FILLED — a positive price
    (COALESCE(NULLIF(fill_price,0), price) > 0), matching exactly what
    get_virtual_positions counts. A row with no recorded price is not a
    confirmed fill and is surfaced separately (the unpriced checks below),
    not counted here. (Caught on the real p152 BBD snapshot: a no-price
    'open' sell made raw disagree with get_virtual by 5 shares.)"""
    out: Dict[str, float] = {}
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT symbol, COALESCE(SUM(CASE "
            "  WHEN side IN ('buy','cover') THEN qty "
            "  WHEN side IN ('sell','short') THEN -qty ELSE 0 END), 0) "
            "FROM trades "
            "WHERE (occ_symbol IS NULL OR occ_symbol = '') "
            "  AND " + _confirmed_fill_clause() + " "
            "GROUP BY symbol",
            _NEVER_FILLED,
        ).fetchall()
    for sym, net in rows:
        if sym:
            out[sym] = float(net or 0)
    return out


def _unpriced_buckets(db_path: str) -> Tuple[int, int]:
    """Split rows that should carry a price but don't into two classes:

      (position_truth, soft)

    position_truth — a STOCK row that reached the broker (it carries an
      order_id) and is not never-filled, but has no positive price. The
      position view drops it for lack of a price → a real move gone
      missing (phantom-class). This deliberately keys on
      "reached-the-broker" (order_id present), NOT a literal status
      allowlist: 'closed', 'pending_fill', and 'open'-with-order_id are all
      real fills the view would drop. (The earlier cut keyed only on
      status='closed' and missed pending_fill — the residual phantom the
      adversarial re-review caught.)
    soft — every other non-never-filled unpriced row: an OPTION leg (which
      legitimately writes price=NULL transiently while a just-entered combo
      backfills — can't tell fresh from stuck offline without the snapshot
      time, so surfaced softly not escalated), or a row with NO order_id
      (never submitted to the broker → can't be a fill). Reported as a
      softer DATA-QUALITY finding so it's never silent.
    """
    never_ph = ",".join("?" * len(_NEVER_FILLED))
    unpriced = ("qty > 0 "
                "AND COALESCE(status,'open') NOT IN (%s) "
                "AND COALESCE(NULLIF(fill_price, 0), price, 0) <= 0" % never_ph)
    stock_with_oid = ("(occ_symbol IS NULL OR occ_symbol = '') "
                      "AND order_id IS NOT NULL AND TRIM(order_id) != ''")
    with closing(sqlite3.connect(db_path)) as conn:
        position_truth = int(conn.execute(
            "SELECT COUNT(*) FROM trades WHERE " + unpriced +
            " AND " + stock_with_oid, _NEVER_FILLED).fetchone()[0] or 0)
        total = int(conn.execute(
            "SELECT COUNT(*) FROM trades WHERE " + unpriced,
            _NEVER_FILLED).fetchone()[0] or 0)
    return position_truth, total - position_truth


def _untracked_fill_count(db_path: str) -> int:
    """Count rows that FILLED (a real position move) but carry no alpaca
    order_id — the orphan-prone shape (a fill the order_id can't attribute).
    Stocks AND options. Empirically 0 across the corrupt cohort; pinned so
    a regression that journals a fill without its order_id fails loudly."""
    with closing(sqlite3.connect(db_path)) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE (order_id IS NULL OR TRIM(order_id) = '') "
            "  AND " + _confirmed_fill_clause(),
            _NEVER_FILLED,
        ).fetchone()[0] or 0)


def _virtual_net_by_symbol(db_path: str) -> Dict[str, float]:
    """Net STOCK position per symbol as get_virtual_positions reconstructs
    it (FIFO + closed-origin handling). Valued at cost (no price_fetcher)."""
    from journal import get_virtual_positions
    out: Dict[str, float] = {}
    for p in (get_virtual_positions(db_path, price_fetcher=None) or []):
        if p.get("occ_symbol"):
            continue  # stocks only; options use a separate convention
        sym = p.get("symbol")
        if sym:
            out[sym] = out.get(sym, 0.0) + float(p.get("qty") or 0)
    return out


def audit_profile_snapshot(db_path: str,
                           initial_capital: Optional[float] = None,
                           tol: float = 0.5) -> List[str]:
    """Run the per-profile book-integrity invariants on one snapshot DB.
    Returns a list of human-readable finding strings (empty = clean).

    Note: cross-profile order_id OWNERSHIP (no order_id in two books) is a
    directory-level check — see audit_order_id_bleed / audit_snapshot_dir.
    Passing initial_capital=None here means the caller opted out of the
    dollar decomposition check; audit_snapshot_dir reports a SKIPPED finding
    when a profile HAS trades but no capital is available."""
    findings: List[str] = []
    try:
        raw = _raw_signed_net_by_symbol(db_path)
        gv = _virtual_net_by_symbol(db_path)
    except Exception as exc:  # a snapshot we can't read is itself a finding
        return ["UNREADABLE %s: %s: %s" % (
            os.path.basename(db_path), type(exc).__name__, exc)]

    # INVARIANT 1 — order-id truth (exact share counts).
    for sym in sorted(set(raw) | set(gv)):
        r = raw.get(sym, 0.0)
        g = gv.get(sym, 0.0)
        if abs(r - g) > tol:
            findings.append(
                "ORDER-ID-TRUTH DRIFT %s: get_virtual_positions=%.0f vs "
                "signed-fill truth=%.0f (delta %.0f) — a position the "
                "journal's own fills don't support (dropped oversell short "
                "/ phantom long)" % (sym, g, r, g - r))

    # UNTRACKED FILL — a confirmed fill with no order_id (orphan-prone).
    try:
        n_untracked = _untracked_fill_count(db_path)
        if n_untracked:
            findings.append(
                "ORDER-ID OWNERSHIP: %d confirmed-fill row(s) with NO "
                "order_id — a real position move the order_id can't "
                "attribute (orphan-prone; breaks 'every trade has an "
                "alpaca order number')" % n_untracked)
    except Exception as exc:
        findings.append("UNTRACKED-FILL CHECK FAILED %s: %s: %s" % (
            os.path.basename(db_path), type(exc).__name__, exc))

    # FILLED-BUT-UNPRICED (escalated) + other unpriced (data-quality).
    try:
        n_position_truth, n_soft = _unpriced_buckets(db_path)
        if n_position_truth:
            findings.append(
                "POSITION-TRUTH GAP: %d stock row(s) that reached the broker "
                "(order_id present) but have no recorded price — a real "
                "position move the view silently drops (phantom-class: cash "
                "moved, position invisible). Covers closed / pending_fill / "
                "open-with-order_id alike. Backfill the fill price or repair."
                % n_position_truth)
        if n_soft:
            findings.append(
                "DATA-QUALITY: %d other unpriced row(s) (qty>0, price<=0) — "
                "an option leg awaiting its backfill, or a row with no "
                "order_id that never reached the broker. Surfaced for "
                "verify/backfill, not escalated as a phantom" % n_soft)
    except Exception as exc:
        findings.append("UNPRICED-ROW CHECK FAILED %s: %s: %s" % (
            os.path.basename(db_path), type(exc).__name__, exc))

    # INVARIANT 2 — decomposition (dollar, needs initial_capital).
    if initial_capital is not None:
        try:
            from journal import get_virtual_account_info, get_virtual_positions
            never_ph = ",".join("?" * len(_NEVER_FILLED))
            with closing(sqlite3.connect(db_path)) as conn:
                # Booked P&L only: a never-filled row (canceled/expired/…)
                # may carry a stray pnl from a prior life; it is not a
                # realized close. Match the 'booked P&L only' contract.
                realized = float(conn.execute(
                    "SELECT COALESCE(SUM(pnl),0) FROM trades "
                    "WHERE pnl IS NOT NULL AND COALESCE(status,'open') "
                    "NOT IN (%s)" % never_ph, _NEVER_FILLED).fetchone()[0] or 0)
            info = get_virtual_account_info(
                db_path, initial_capital=initial_capital, price_fetcher=None)
            equity = float(info.get("equity") or 0)
            unreal = sum(float(p.get("unrealized_pl") or 0)
                         for p in (get_virtual_positions(
                             db_path, price_fetcher=None) or []))
            gap = (equity - initial_capital) - (realized + unreal)
            if abs(gap) > _DECOMP_TOL:
                findings.append(
                    "DECOMPOSITION DRIFT: (equity-capital)=%.2f but "
                    "realized+unrealized=%.2f (gap %.2f) — equity not "
                    "backed by booked P&L + held positions (phantom "
                    "equity)" % (equity - initial_capital,
                                 realized + unreal, gap))
        except Exception as exc:
            findings.append("DECOMPOSITION CHECK FAILED %s: %s: %s" % (
                os.path.basename(db_path), type(exc).__name__, exc))
    return findings


def _profile_id_columns(db_path: str) -> Dict[str, set]:
    """Map each broker order_id this profile references -> the set of
    column-kinds it appears in ('entry' for order_id, 'protective' for any
    protective_*_order_id). One UNION map across all id-bearing columns, so
    a cross-namespace collision (an id that is an entry in one profile and a
    protective in another) is caught — they share the same id space."""
    out: Dict[str, set] = {}
    with closing(sqlite3.connect(db_path)) as conn:
        for (oid,) in conn.execute(
                "SELECT DISTINCT order_id FROM trades "
                "WHERE order_id IS NOT NULL AND TRIM(order_id) != ''"):
            out.setdefault(oid, set()).add("entry")
        for col in ("protective_stop_order_id", "protective_tp_order_id",
                    "protective_trailing_order_id"):
            try:
                for (oid,) in conn.execute(
                        "SELECT DISTINCT %s FROM trades "
                        "WHERE %s IS NOT NULL AND TRIM(%s) != ''"
                        % (col, col, col)):
                    out.setdefault(oid, set()).add("protective")
            except sqlite3.OperationalError:
                pass  # column absent in an older schema — not a finding
    return out


def _has_trades(db_path: str) -> bool:
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM trades LIMIT 1").fetchone())
    except Exception:
        return False


def _bleed_finding(kind: str, oid: str, pids: Sequence,
                   account_map: Optional[Dict[int, object]]) -> str:
    """One bleed finding, account-aware. An order_id lives on exactly ONE
    Alpaca account, so the SAME order_id under profiles that route to
    DIFFERENT alpaca_account_ids is the worst case — a fill recorded into
    the wrong account's profile. Without a profile→account map we can only
    say it's in two books (still wrong). Sort keys are coerced to str so a
    mix of int and non-numeric profile ids never raises."""
    pids = sorted(pids, key=str)
    accts = None
    if account_map:
        accts = sorted({account_map[p] for p in pids
                        if account_map.get(p) is not None}, key=str)
    if accts and len(accts) > 1:
        return ("CROSS-ACCOUNT BLEED: %s order_id %s appears under profiles "
                "%s spanning Alpaca accounts %s — an order exists on ONE "
                "account; recording it under another account's profile is "
                "cross-account contamination" % (kind, oid, pids, accts))
    acct_note = (" (account %s)" % accts[0]) if accts else ""
    return ("ORDER-ID BLEED: %s order_id %s appears in %d profiles %s%s — "
            "one profile is touching another's order (profile-bleed)"
            % (kind, oid, len(pids), pids, acct_note))


def audit_order_id_bleed(db_paths: Sequence[str],
                         account_map: Optional[Dict[int, object]] = None
                         ) -> List[str]:
    """Cross-profile ORDER-ID OWNERSHIP: an order_id (entry or protective)
    must live in exactly one profile's book — and, when a profile→account
    map is given, never under profiles routing to two different Alpaca
    accounts. Anything shared is bleed. Returns findings."""
    # order_id -> {pid: {kinds}} over the UNION of entry + protective id
    # columns. One id space: a collision is flagged no matter which column
    # it appeared in (entry-in-A vs protective-in-B is still bleed).
    owners: Dict[str, Dict[object, set]] = {}
    for db in db_paths:
        m = re.search(r"quantopsai_profile_(\d+)", os.path.basename(db))
        pid = int(m.group(1)) if m else os.path.basename(db)
        try:
            idmap = _profile_id_columns(db)
        except Exception as exc:
            return ["ORDER-ID OWNERSHIP CHECK FAILED on %s: %s: %s" % (
                os.path.basename(db), type(exc).__name__, exc)]
        for oid, kinds in idmap.items():
            owners.setdefault(oid, {})[pid] = kinds
    findings: List[str] = []
    for oid, by_pid in sorted(owners.items(), key=lambda kv: str(kv[0])):
        if len(by_pid) > 1:
            kinds = sorted({k for s in by_pid.values() for k in s})
            findings.append(_bleed_finding(
                "/".join(kinds) or "entry", oid, list(by_pid.keys()),
                account_map))
    return findings


def _account_map(master_db: Optional[str]) -> Dict[int, object]:
    """profile id -> alpaca_account_id from the master DB, for account-aware
    ownership. Empty (account-agnostic bleed only) if unavailable."""
    if not master_db or not os.path.exists(master_db):
        return {}
    try:
        with closing(sqlite3.connect(master_db)) as conn:
            return {int(pid): acct for pid, acct in conn.execute(
                "SELECT id, alpaca_account_id FROM trading_profiles")
                if acct is not None}
    except Exception:
        return {}


def _initial_capital_map(master_db: Optional[str]) -> Dict[int, float]:
    """Map profile id -> initial_capital from the master DB. A NULL capital
    is left OUT of the map (so the caller reports SKIPPED rather than
    silently auditing against a coerced 0.0). Logs why it came back empty."""
    if not master_db:
        logging.warning("snapshot_audit: no master DB given — decomposition "
                        "will be SKIPPED for every profile")
        return {}
    if not os.path.exists(master_db):
        logging.warning("snapshot_audit: master DB %s not found — "
                        "decomposition SKIPPED", master_db)
        return {}
    try:
        with closing(sqlite3.connect(master_db)) as conn:
            out: Dict[int, float] = {}
            for pid, cap in conn.execute(
                    "SELECT id, initial_capital FROM trading_profiles"):
                if cap is None:
                    continue  # surfaced as SKIPPED, not coerced to 0
                out[int(pid)] = float(cap)
            return out
    except Exception as exc:
        logging.warning("snapshot_audit: could not read initial_capital "
                        "from %s (%s: %s) — decomposition SKIPPED",
                        master_db, type(exc).__name__, exc)
        return {}


def audit_snapshot_dir(snapshot_dir: str,
                       master_db: Optional[str] = None) -> Dict[str, List[str]]:
    """Audit every quantopsai_profile_<id>.db in a snapshot directory.
    Returns {db_basename: [findings]} (only entries WITH findings). The
    cross-profile order-id ownership check is reported under the synthetic
    key '<cross-profile>'."""
    if master_db is None:
        for cand in (os.path.join(snapshot_dir, "quantopsai.db"),
                     "quantopsai.db"):
            if os.path.exists(cand):
                master_db = cand
                break
    caps = _initial_capital_map(master_db)
    dbs = sorted(glob.glob(os.path.join(
        snapshot_dir, "quantopsai_profile_*.db")))
    out: Dict[str, List[str]] = {}
    for db in dbs:
        m = re.search(r"quantopsai_profile_(\d+)\.db$", db)
        pid = int(m.group(1)) if m else None
        cap = caps.get(pid) if pid is not None else None
        findings = audit_profile_snapshot(db, initial_capital=cap)
        # Don't silently half-audit: if a profile has trades but we have no
        # capital for it, say so — decomposition did NOT run.
        if cap is None and _has_trades(db):
            findings.append(
                "DECOMPOSITION SKIPPED: no initial_capital for profile %s "
                "(master DB missing/unreadable or NULL capital) — the "
                "dollar phantom-equity check did NOT run for this profile"
                % (pid if pid is not None else os.path.basename(db)))
        if findings:
            out[os.path.basename(db)] = findings
    # Cross-profile ownership (bleed) — the load-bearing isolation invariant,
    # account-aware: a fill belongs to one profile AND that profile's one
    # Alpaca account; an order_id under two accounts' profiles is the worst
    # case (cross-account contamination).
    bleed = audit_order_id_bleed(dbs, account_map=_account_map(master_db))
    if bleed:
        out[_CROSS_PROFILE_KEY] = bleed
    return out


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot_dir", help="dir of quantopsai_profile_*.db")
    ap.add_argument("--master", default=None,
                    help="master DB for initial_capital (default: autodetect)")
    args = ap.parse_args()
    results = audit_snapshot_dir(args.snapshot_dir, master_db=args.master)
    if not results:
        print("SNAPSHOT CLEAN — all profile books satisfy the invariants "
              "(order-id truth, ownership, decomposition).")
        return 0
    print("SNAPSHOT FINDINGS:")
    for db, findings in results.items():
        print("  %s:" % db)
        for f in findings:
            print("    - %s" % f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
