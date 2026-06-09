"""2026-06-09 (rework) — bracket-order entries replace the
separate-protective-stops pattern.

Pre-rework: execute_trade submitted a plain BUY/SHORT; then a
later `ensure_protective_stops` sweep placed a stop + (after the
broker-side TP commit) a TP as SEPARATE reduce-only orders. On
shared Alpaca accounts where multiple profiles hold the same
symbol, this broke: Alpaca's reduce-only-qty accounting only
allows total-reduce-only <= position-qty. Once one profile placed
its trailing reserving its qty, sibling profiles' attempts to
place their own protective hit "insufficient qty available."
Result: pid 56 + pid 63's PAVS positions sat unprotected at the
broker while one sibling profile's trailing covered the aggregate.

Post-rework: each BUY/SHORT submits an Alpaca BRACKET order. The
stop + TP are OCO sub-orders of the parent entry — they reserve
the entry qty ONCE between them, not twice. Each profile's
bracket is independent at the broker. For shared accounts the
math holds: sum(per-profile bracket qty) == aggregate position.

Tests pin:
  1. BUY submit uses order_class="bracket" with stop_loss + take_profit.
  2. SHORT submit uses order_class="bracket" with stop ABOVE entry,
     TP BELOW (the short direction).
  3. Stop and TP prices match the entry's clamped sl/tp pcts.
  4. Bracket child IDs (when surfaced by the SDK) get stamped onto
     the entry row's protective_*_order_id columns so the protective
     sweep sees live coverage and skips re-placement.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — source-code pins on order_class="bracket" + child stamps
# ---------------------------------------------------------------------------


def test_buy_submit_uses_bracket_order_class():
    """Source-code pin: execute_trade's BUY submit must include
    order_class='bracket' with stop_loss and take_profit sub-orders.
    A refactor that reverts to plain market+protective-sweep
    re-introduces the shared-account reduce-only contention bug."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Find the BUY-bracket anchor comment that lives right before the
    # bracket kwargs (more precise than the broad BUY branch start).
    anchor = src.find("BRACKET ENTRY. Replaces the prior")
    assert anchor > 0, "BUY bracket anchor comment missing"
    window = src[anchor:anchor + 3000]
    assert '"order_class": "bracket"' in window, (
        "BUY submit must use order_class='bracket'. Without it, "
        "stop and TP are placed as separate reduce-only orders by "
        "ensure_protective_stops — which hits 'insufficient qty' "
        "on shared Alpaca accounts (the PAVS damage pattern)."
    )
    assert '"stop_loss": {"stop_price"' in window, (
        "BUY bracket must include stop_loss sub-order with stop_price."
    )
    assert '"take_profit": {"limit_price"' in window, (
        "BUY bracket must include take_profit sub-order with limit_price."
    )


def test_short_submit_uses_bracket_order_class():
    """Same for the SHORT path. Stop is ABOVE entry, TP is BELOW —
    the short direction's profit/loss geometry."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # Anchor on the short-open submit comment block
    anchor = src.find("BRACKET ENTRY for shorts")
    assert anchor > 0, (
        "SHORT bracket anchor comment missing — refactor must "
        "preserve it or update this pin."
    )
    window = src[anchor:anchor + 2500]
    assert '"order_class": "bracket"' in window
    assert '"stop_loss": {"stop_price"' in window
    assert '"take_profit": {"limit_price"' in window
    # The short-side direction: stop above entry, TP below
    assert "price * (1 + short_sl)" in window, (
        "SHORT stop must be ABOVE entry: price * (1 + short_sl). "
        "If a refactor flips the sign the stop fires immediately."
    )
    assert "price * (1 - short_tp)" in window, (
        "SHORT TP must be BELOW entry: price * (1 - short_tp)."
    )


def test_bracket_children_stamped_onto_entry_row():
    """Source pin: after the bracket parent returns, the BUY path
    must extract the stop + TP child order IDs and stamp them into
    protective_stop_order_id / protective_tp_order_id on the entry
    row. Without this `ensure_protective_stops` will try to place
    DUPLICATE protective orders on the next sweep, hitting the
    same 'insufficient qty' error the bracket was meant to solve."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    # The BUY branch's UPDATE that stamps the IDs
    anchor = src.find("Stamp the bracket child IDs onto the just-written")
    assert anchor > 0, (
        "Bracket child-id stamp comment anchor missing"
    )
    window = src[anchor:anchor + 1500]
    assert "protective_stop_order_id = COALESCE" in window, (
        "Entry-row UPDATE must populate protective_stop_order_id "
        "from the bracket's stop child. Without this the protective "
        "sweep sees no coverage and tries to place duplicate stops."
    )
    assert "protective_tp_order_id" in window, (
        "Entry-row UPDATE must populate protective_tp_order_id from "
        "the bracket's TP child."
    )


# ---------------------------------------------------------------------------
# Layer 2 — assert the stop/TP prices come from the clamped pcts
# ---------------------------------------------------------------------------


def test_bracket_stop_price_derived_from_clamped_sl_pct():
    """The bracket's stop_price must be derived from actual_sl_pct
    (the clamped post-risk_clamps SL fraction). The bracket cannot
    skip the clamp — otherwise the protective bound regresses to the
    raw ATR formula and we lose the [3%, 7%] / [4%, 12%] envelope."""
    src = (REPO_ROOT / "trade_pipeline.py").read_text()
    anchor = src.find("BRACKET ENTRY. Replaces the prior")
    assert anchor > 0
    window = src[anchor:anchor + 2500]
    # The bracket's stop_price line is computed just before the kwargs
    assert "stop_price = round(price * (1 - actual_sl_pct)" in window, (
        "BUY bracket stop_price must be computed from actual_sl_pct "
        "(post-clamp). Without this, the bracket bypasses risk_clamps "
        "and the 80%+ TP / 50%+ SL regression returns."
    )
    assert "target_price = round(price * (1 + actual_tp_pct)" in window, (
        "BUY bracket take_profit must be computed from actual_tp_pct."
    )
