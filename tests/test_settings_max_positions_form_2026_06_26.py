"""Settings form must accommodate the seeded max_total_positions (2026-06-26).

Bug it pins: the non-control experiment profiles are seeded with
`max_total_positions = 999` ("effectively uncapped", create_experiment_profiles.py),
but the Settings form's number input carried `max="50"`. HTML5 client-side
validation then rejected the field on EVERY save ("the number has to be 50 or
less"), so the operator could not change ANY setting on those profiles — the
whole form refused to submit because a legitimately-configured value was
outside the input's own bound.

Class: a Settings form numeric input whose [min, max] is TIGHTER than a value
the system legitimately seeds will silently block the form. This test pins that
the max_total_positions input spans every value create_experiment_profiles.py
seeds, so a seeded profile is always editable.
"""
from __future__ import annotations

import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _input_bounds(html: str, field: str):
    """Return (min, max) ints for the <input name="field"> in `html`."""
    m = re.search(
        r'<input\b[^>]*\bname="%s"[^>]*>' % re.escape(field), html, re.DOTALL)
    assert m, "no <input name=%r> found in settings.html" % field
    tag = m.group(0)
    mn = re.search(r'\bmin="(-?\d+)"', tag)
    mx = re.search(r'\bmax="(-?\d+)"', tag)
    assert mn and mx, "max_total_positions input must declare min and max: %r" % tag
    return int(mn.group(1)), int(mx.group(1))


def _seeded_values(py: str, field: str):
    return [int(v) for v in re.findall(r'"%s":\s*(\d+)' % re.escape(field), py)]


def test_form_accommodates_every_seeded_max_total_positions():
    html = open(os.path.join(REPO, "templates", "settings.html")).read()
    seed = open(os.path.join(REPO, "create_experiment_profiles.py")).read()

    lo, hi = _input_bounds(html, "max_total_positions")
    seeded = _seeded_values(seed, "max_total_positions")
    assert seeded, "expected create_experiment_profiles.py to seed the field"

    for v in seeded:
        assert lo <= v <= hi, (
            "create_experiment_profiles.py seeds max_total_positions=%d, but "
            "the Settings input only allows [%d, %d] — the form will reject the "
            "save for every profile holding that value. Widen the input bound."
            % (v, lo, hi)
        )
    # The uncapped sentinel must be reachable.
    assert hi >= 999, (
        "max_total_positions input max=%d < 999 (the 'effectively uncapped' "
        "sentinel the non-control profiles use)." % hi
    )
