"""Parse-layer multileg strike validation (2026-06-10).

Bug class: AI-proposed multileg strikes that don't resolve to
DISTINCT listed contracts were only caught at EXECUTION (the 06-09
strike-snap collision refusal in options_multileg) and surfaced as
GATED · ERROR badges. Concrete repro from the first post-PM-reset
cycle: bear_call_spread on AAL with strikes 14/14.5 on a $1-spaced
chain — both legs snapped to AAL260717C00014000 (zero-width spread)
on 3 profiles simultaneously (pids 95/96/98, same shared proposal).

The fix mirrors the morning's single-leg RC11 pattern one layer up:
`validate_and_snap_multileg_strikes` (options_multileg) is called
from the MULTILEG_OPEN parse branch (ai_analyst) and either bakes
repaired/snapped strikes into the proposal or rejects it before it
enters the validated list. The execution-layer collision refusal
stays as the safety belt.

Pins:
  1. snap_strike_group repairs collisions + preserves order, and
     refuses unplaceable groups (unit tests on fake chains).
  2. validate_and_snap_multileg_strikes handles every promptable
     strategy shape; AAL regression case repairs to 14/15.
  3. ai_analyst's MULTILEG_OPEN parse branch calls the validator
     before appending to the validated list (source pin).
  4. The multileg prompt note requires DISTINCT listed strikes
     (source pin).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

EXPIRY = "2026-07-17"


def _chain(strikes, expiry=EXPIRY, rights=("call", "put"), symbol="AAL"):
    """Fake Alpaca contracts list shaped like list_available_contracts."""
    out = []
    for s in strikes:
        for r in rights:
            occ = (
                f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
                f"{'C' if r == 'call' else 'P'}{int(float(s) * 1000):08d}"
            )
            out.append({
                "symbol": occ,
                "expiration_date": expiry,
                "type": r,
                "strike": float(s),
            })
    return out


# ---------------------------------------------------------------------------
# (1) snap_strike_group unit behavior
# ---------------------------------------------------------------------------

def test_group_snap_repairs_collision_upward():
    from options_chain_alpaca import snap_strike_group
    got = snap_strike_group(
        "AAL", EXPIRY, [14.0, 14.5], "C",
        contracts=_chain([13, 14, 15, 16]),
    )
    assert got is not None
    assert got["expiration_date"] == EXPIRY
    assert got["strikes"] == [14.0, 15.0]


def test_group_snap_falls_back_to_downward_repair():
    from options_chain_alpaca import snap_strike_group
    # Nearest snap for both targets is the TOP grid strike — no room
    # above, so the lower leg must walk down instead.
    got = snap_strike_group(
        "AAL", EXPIRY, [13.9, 14.1], "C",
        contracts=_chain([13, 14]),
    )
    assert got is not None
    assert got["strikes"] == [13.0, 14.0]


def test_group_snap_refuses_out_of_tolerance_targets():
    from options_chain_alpaca import snap_strike_group
    assert snap_strike_group(
        "AAL", EXPIRY, [5.0, 5.2], "C",
        contracts=_chain([13, 14]),
    ) is None


def test_group_snap_refuses_when_grid_too_small():
    from options_chain_alpaca import snap_strike_group
    assert snap_strike_group(
        "AAL", EXPIRY, [13.9, 14.1], "C",
        contracts=_chain([14]),
    ) is None


def test_group_snap_refuses_non_ascending_input():
    from options_chain_alpaca import snap_strike_group
    chain = _chain([13, 14, 15])
    assert snap_strike_group(
        "AAL", EXPIRY, [14.0, 14.0], "C", contracts=chain) is None
    assert snap_strike_group(
        "AAL", EXPIRY, [15.0, 14.0], "C", contracts=chain) is None


# ---------------------------------------------------------------------------
# (2) validate_and_snap_multileg_strikes per strategy shape
# ---------------------------------------------------------------------------

def test_aal_bear_call_spread_regression_repaired():
    """The exact 3-profile GATED · ERROR case: 14/14.5 on a $1 grid."""
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "bear_call_spread",
        {"short": 14, "long": 14.5}, EXPIRY,
        contracts=_chain([12, 13, 14, 15, 16]),
    )
    assert got is not None
    strikes, expiry = got
    assert expiry == EXPIRY
    assert strikes == {"short": 14.0, "long": 15.0}


def test_vertical_zero_width_input_rejected():
    from options_multileg import validate_and_snap_multileg_strikes
    assert validate_and_snap_multileg_strikes(
        "AAL", "bull_put_spread",
        {"short": 14, "long": 14}, EXPIRY,
        contracts=_chain([13, 14, 15]),
    ) is None


def test_vertical_label_inversion_preserved():
    """AI sometimes inverts short/long labels; orientation must be
    preserved (the build dispatcher sorts anyway)."""
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "bear_call_spread",
        {"short": 14.5, "long": 14}, EXPIRY,
        contracts=_chain([12, 13, 14, 15, 16]),
    )
    assert got is not None
    strikes, _ = got
    assert strikes == {"short": 15.0, "long": 14.0}


def test_iron_condor_overlapping_wings_rejected():
    """Put pair and call pair each snap fine, but the snapped
    put_short collides with the snapped call_short — builder would
    raise its ordering ValueError; must reject at parse instead."""
    from options_multileg import validate_and_snap_multileg_strikes
    assert validate_and_snap_multileg_strikes(
        "AAL", "iron_condor",
        {"put_long": 12, "put_short": 13,
         "call_short": 13.4, "call_long": 14.4},
        EXPIRY,
        contracts=_chain([10, 11, 12, 13, 14, 15, 16]),
    ) is None


def test_iron_condor_valid_passes():
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "iron_condor",
        {"put_long": 11, "put_short": 12,
         "call_short": 15, "call_long": 16},
        EXPIRY,
        contracts=_chain([10, 11, 12, 13, 14, 15, 16]),
    )
    assert got is not None
    strikes, _ = got
    assert strikes == {"put_long": 11.0, "put_short": 12.0,
                       "call_short": 15.0, "call_long": 16.0}


def test_straddle_snaps_body_to_listed_strike():
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "long_straddle",
        {"strike": 13.9}, EXPIRY,
        contracts=_chain([13, 14, 15]),
    )
    assert got is not None
    strikes, _ = got
    assert strikes == {"strike": 14.0}


def test_straddle_missing_put_side_rejected():
    from options_multileg import validate_and_snap_multileg_strikes
    assert validate_and_snap_multileg_strikes(
        "AAL", "short_straddle",
        {"strike": 14}, EXPIRY,
        contracts=_chain([13, 14, 15], rights=("call",)),
    ) is None


def test_strangle_collapse_repaired_to_distinct_strikes():
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "long_strangle",
        {"put": 13.9, "call": 14.1}, EXPIRY,
        contracts=_chain([13, 14, 15, 16]),
    )
    assert got is not None
    strikes, _ = got
    assert strikes["put"] < strikes["call"]
    assert strikes == {"put": 14.0, "call": 15.0}


def test_iron_butterfly_subgrid_width_rejected():
    """wing_width below the grid spacing collapses wings onto the
    body at execution — must reject at parse."""
    from options_multileg import validate_and_snap_multileg_strikes
    assert validate_and_snap_multileg_strikes(
        "AAL", "iron_butterfly",
        {"body": 14, "wing_width": 0.25}, EXPIRY,
        contracts=_chain([12, 13, 14, 15, 16]),
    ) is None


def test_iron_butterfly_valid_width_snapped():
    from options_multileg import validate_and_snap_multileg_strikes
    got = validate_and_snap_multileg_strikes(
        "AAL", "iron_butterfly",
        {"body": 13.9, "wing_width": 1.1}, EXPIRY,
        contracts=_chain([12, 13, 14, 15, 16]),
    )
    assert got is not None
    strikes, _ = got
    assert strikes == {"body": 14.0, "wing_width": 1.0}


def test_unknown_shape_and_empty_chain_pass_through():
    from options_multileg import validate_and_snap_multileg_strikes
    raw = {"near": 14, "far": 15}
    assert validate_and_snap_multileg_strikes(
        "AAL", "calendar_spread", raw, EXPIRY,
        contracts=_chain([13, 14, 15]),
    ) == (raw, EXPIRY)
    # Chain outage (empty contracts) degrades gracefully — the
    # execution-layer snap is the safety belt, same as single-leg.
    assert validate_and_snap_multileg_strikes(
        "AAL", "bear_call_spread",
        {"short": 14, "long": 14.5}, EXPIRY,
        contracts=[],
    ) == ({"short": 14, "long": 14.5}, EXPIRY)


def test_malformed_strikes_dict_rejected():
    from options_multileg import validate_and_snap_multileg_strikes
    assert validate_and_snap_multileg_strikes(
        "AAL", "bear_call_spread",
        {"wrong_key": 14}, EXPIRY,
        contracts=_chain([13, 14, 15]),
    ) is None


# ---------------------------------------------------------------------------
# (3) source pin: parse branch calls the validator
# ---------------------------------------------------------------------------

def test_parse_branch_calls_multileg_snap_validator():
    src = (REPO / "ai_analyst.py").read_text()
    start = src.index('if action == "MULTILEG_OPEN":')
    end = src.index('if action == "PAIR_TRADE":', start)
    branch = src[start:end]
    assert "validate_and_snap_multileg_strikes" in branch, (
        "MULTILEG_OPEN parse branch no longer validates strikes "
        "against the listed chain — the strike-snap-collision "
        "GATED · ERROR class (AAL 14/14.5, 2026-06-10) will return."
    )
    # Rejection must drop the proposal (continue), not just log.
    reject_idx = branch.index("validate_and_snap_multileg_strikes")
    assert "continue" in branch[reject_idx:], (
        "Validator result is not enforced — a None return must "
        "`continue` past validated.append."
    )


# ---------------------------------------------------------------------------
# (4) source pin: prompt requires distinct listed strikes
# ---------------------------------------------------------------------------

def test_multileg_prompt_note_requires_distinct_listed_strikes():
    src = (REPO / "ai_analyst.py").read_text()
    m = re.search(r"multileg_note = \((?:.|\n)*?\)\n", src)
    assert m, "multileg_note block not found in ai_analyst.py"
    note = m.group(0)
    assert "DISTINCT" in note and "LISTED" in note, (
        "Multileg prompt note no longer tells the AI that spread "
        "legs must be distinct listed strikes — parser rejections "
        "will rise."
    )
