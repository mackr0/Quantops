"""OCC symbol format consistency (2026-06-15).

Root cause of a three-symptom bug found live on BMNR:

  format_occ_symbol emits the OSI space-padded root
  ("BMNR  260724C00018000"); Alpaca's API uses the COMPACT form
  ("BMNR260724C00018000") and 422-rejects the padded one. The
  multileg snap replaces padded with Alpaca's compact symbol before
  submit (so multileg works and STORES compact); single-leg rebuilt
  the padded symbol at submit and never snapped it (so single-leg
  was 100% rejected). And parse_occ_symbol used fixed offsets that
  assumed a padded 6-char root, so it raised on every compact
  symbol — which callers catch into a dropped result, silently
  blinding the Greeks gate and options-exit timing to the
  (compact-stored) multileg legs.

Pins:
  1. parse_occ_symbol decodes BOTH padded and compact, round-trips,
     and still rejects garbage.
  2. _parse_option_position (Greeks) COUNTS a compact leg instead of
     dropping it.
  3. _days_to_expiry (exits) decodes a compact symbol.
  4. The single-leg executor snaps the OCC to the listed contract
     before submit (source pin).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# (1) Parser handles both formats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("occ,root,exp,strike,right", [
    ("BMNR260724C00018000", "BMNR", date(2026, 7, 24), 18.0, "C"),
    ("BMNR  260724C00018000", "BMNR", date(2026, 7, 24), 18.0, "C"),
    ("GOOGL260724P00200000", "GOOGL", date(2026, 7, 24), 200.0, "P"),
    ("F     260724C00012000", "F", date(2026, 7, 24), 12.0, "C"),
    ("SPY   251231P00450500", "SPY", date(2025, 12, 31), 450.5, "P"),
])
def test_parse_both_formats(occ, root, exp, strike, right):
    from options_trader import parse_occ_symbol
    p = parse_occ_symbol(occ)
    assert p["underlying"] == root
    assert p["expiry"] == exp
    assert abs(p["strike"] - strike) < 1e-9
    assert p["right"] == right


def test_format_then_parse_round_trips():
    from options_trader import format_occ_symbol, parse_occ_symbol
    occ = format_occ_symbol("BMNR", date(2026, 7, 24), 18.0, "C")
    p = parse_occ_symbol(occ)
    assert p["underlying"] == "BMNR"
    assert p["expiry"] == date(2026, 7, 24)
    assert p["strike"] == 18.0
    assert p["right"] == "C"


@pytest.mark.parametrize("bad", [
    "", "XYZ", "BMNR260724X00018000", "BMNR2607XXC00018000",
])
def test_parse_rejects_garbage(bad):
    from options_trader import parse_occ_symbol
    with pytest.raises(ValueError):
        parse_occ_symbol(bad)


# ---------------------------------------------------------------------------
# (2) Greeks gate counts a compact leg (was silently dropped)
# ---------------------------------------------------------------------------

def test_greeks_counts_compact_leg():
    from options_greeks_aggregator import _parse_option_position
    r = _parse_option_position(
        {"occ_symbol": "BMNR260724C00018000", "qty": 2})
    assert r is not None, (
        "Greeks aggregator dropped a compact-symbol leg — the risk "
        "gate under-counts option exposure (the live BMNR bug)."
    )
    assert r["strike"] == 18.0 and r["right"] == "C"


# ---------------------------------------------------------------------------
# (3) Exit timing decodes a compact symbol
# ---------------------------------------------------------------------------

def test_exit_days_to_expiry_reads_compact():
    from options_exits import _days_to_expiry
    d = _days_to_expiry("BMNR260724C00018000", today=date(2026, 7, 1))
    assert d == 23, (
        "Options-exit timing can't read compact symbols — near-expiry "
        "exits go blind on multileg legs."
    )


# ---------------------------------------------------------------------------
# (4) Single-leg executor snaps before submit (source pin)
# ---------------------------------------------------------------------------

def test_single_leg_executor_snaps_occ():
    src = (REPO / "options_trader.py").read_text()
    start = src.index("# Build OCC + submit")
    end = src.index("# Duplicate-position guard", start)
    block = src[start:end]
    assert "snap_to_listed_contract" in block, (
        "Single-leg executor no longer snaps the OCC to the listed "
        "contract — the padded symbol returns to the broker and gets "
        "422-rejected (BMNR single-leg dead path)."
    )
    assert '_snapped["symbol"]' in block or "_snapped.get(\"symbol\")" in block
