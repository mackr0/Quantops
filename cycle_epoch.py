"""Process-wide monotonic cycle epoch — the clock for the freshness invariant.

The recurring broker/journal divergence class (phantom positions, oversells,
decomposition gaps) is, in every instance, the same shape: an actor (a
protective re-arm, an exit, a multileg rollback, an option close, a short
entry) reads a journal that has NOT been reconciled to broker truth this
cycle and submits an order on it.

The fix makes staleness un-actable at the one door every order passes
through. This module is the clock that makes "this cycle" precise:

  - The scheduler calls ``bump()`` once at the top of each cycle's mutating
    phase.
  - Every (profile, symbol) reconciled to broker truth is stamped (in the
    profile's ``reconcile_state`` table) with the epoch current at reconcile
    time — see ``journal.stamp_symbols_fresh``.
  - The oversell door (``order_guard.assert_sell_within_own_book``) refuses
    any sell whose symbol's stamped epoch is older than ``current()``,
    forcing a just-in-time reconcile first.

A never-stamped symbol reads epoch 0, which is always older than the live
epoch (which starts at 1) — so an unknown symbol is treated as STALE and
must be reconciled before it can be sold. That is the fail-safe default.

Built 2026-06-23 as the foundation of the divergence-class elimination.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
# Seed from the wall clock (whole seconds) so the epoch is MONOTONIC ACROSS
# PROCESS RESTARTS. A plain counter starting at 1 would reset on every
# scheduler restart, and then a stamp written by the PREVIOUS run (e.g.
# 1_700_000_113) would read "fresh" against the reset epoch — silently failing
# the door's just-in-time gate OPEN exactly when state is most likely to have
# drifted (the bug a restart-aware review caught). Because the seed is the
# current unix time and bump() never goes backwards, every stamp from a prior
# run is < the new live epoch → STALE → forced reconcile. A ledger stamp of 0
# (never reconciled) is older than any seed, so unknown is still fail-safe.
_epoch = int(time.time())


def current() -> int:
    """The live cycle epoch. Symbols stamped with this value are fresh."""
    with _lock:
        return _epoch


def bump() -> int:
    """Advance to a new cycle. Every symbol's freshness stamp is now stale
    until that symbol is reconciled-to-broker again this cycle. Monotonic and
    restart-safe: advances to at least now() (whole seconds), and always by at
    least 1 so rapid same-second bumps still strictly increase. Returns the
    new epoch."""
    global _epoch
    with _lock:
        _epoch = max(_epoch + 1, int(time.time()))
        return _epoch
