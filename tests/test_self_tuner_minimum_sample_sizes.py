"""Structural guardrail: every tightening decision in self_tuning.py
must require at least 30 resolved predictions of evidence.

The bug class (2026-05-14 incident).
The self-tuner had been ratcheting entry criteria tighter for 14
straight days based on tiny sample sizes (e.g., 5 samples, 10
samples). Each individual change passed its sanity check; the sum
of 30+ daily restrictions over 14 days drove stock new entries
from 24/day to 0/day system-wide.

Acceptable patterns:
  1. `HAVING COUNT(*) >= N` where N >= 30 in a tightening-side rule.
  2. `if total >= N` where N >= 30 in a tightening-side rule.
  3. `# LOOSEN_OK: ...` comment annotating a sample-size gate that
     belongs to a LOOSENING (action-creating) rule. Loosening can
     and should fire on smaller samples — the system's default bias
     is toward action, not stasis.
  4. `# DISPLAY_ONLY: ...` comment for a sample-size gate that's
     used for analysis/display, not a tuning decision.

Strict mode — no grandfathered baseline. Every tightening sample-
size threshold below 30 fails. New tightening rules must include
the minimum-30-sample requirement.

Why next time will be caught.
This test scans self_tuning.py with a regex for `>= N` patterns near
tuning words (HAVING / total / cnt / band) and fails on any N < 30
unless explicitly annotated as loosening or display-only. A future
PR that introduces another `if band80_total > 5: tighten...` line
will fail CI before the scheduler ever sees it.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Minimum sample size for any tightening decision.
MIN_SAMPLE_SIZE = 30

# Pattern: `>= N` or `> N` where N is a small integer, near a
# count-flavored variable name. Tightening rules typically read
# like `HAVING COUNT(*) >= N`, `if total >= N`, `if cnt > N`,
# `if band70_total > N`.
SAMPLE_GATE_RE = re.compile(
    r"\b(?:HAVING\s+COUNT\(\*\)|"
    r"\w*(?:total|cnt|count|samples|n_train|n_resolved|"
    r"resolved|predictions))\s*"
    r"(?:>=|>)\s*(\d+)\b",
    re.IGNORECASE,
)

# Annotations that explicitly mark a gate as loosening or display-only.
LOOSEN_MARK = re.compile(r"#\s*LOOSEN_OK:")
DISPLAY_MARK = re.compile(r"#\s*DISPLAY_ONLY:")


class TestSelfTunerMinimumSampleSizes:
    def test_no_tightening_rule_below_minimum_sample_size(self):
        path = os.path.join(REPO_ROOT, "self_tuning.py")
        with open(path) as f:
            lines = f.readlines()

        violations = []
        for idx, line in enumerate(lines, start=1):
            m = SAMPLE_GATE_RE.search(line)
            if not m:
                continue
            n = int(m.group(1))
            if n >= MIN_SAMPLE_SIZE:
                continue
            # Check 15 lines above and the same line for an explicit
            # annotation. Multi-line SQL strings can span 10+ lines and
            # the annotating comment sits above the conn.execute opener.
            # 15 lines is the smallest window that reliably catches the
            # annotation without being so lenient that drift hides.
            ctx = "".join(lines[max(0, idx - 15):idx + 1])
            if LOOSEN_MARK.search(ctx) or DISPLAY_MARK.search(ctx):
                continue
            violations.append((idx, n, line.rstrip()))

        if violations:
            details = "\n".join(
                f"  self_tuning.py:{ln} sample-size {n} (need ≥{MIN_SAMPLE_SIZE}): {src}"
                for ln, n, src in violations
            )
            pytest.fail(
                f"{len(violations)} tightening decision(s) in "
                f"self_tuning.py use a sample size below "
                f"{MIN_SAMPLE_SIZE}.\n\n{details}\n\nFix one of:\n"
                f"  1. Bump the threshold to ≥{MIN_SAMPLE_SIZE}.\n"
                f"  2. Add `# LOOSEN_OK: <rationale>` if this gate "
                f"belongs to a loosening rule (loosening can fire "
                f"on smaller samples — the system's default bias is "
                f"toward action).\n"
                f"  3. Add `# DISPLAY_ONLY: <rationale>` if this gate "
                f"is for analysis/display, not a tuning decision."
            )

    def test_volume_floor_signal_present_in_apply_auto_adjustments(self):
        """The volume-floor signal must exist in apply_auto_adjustments.
        It is NOT a hard block — it raises the bar for tightening
        (e.g., 30→60 sample minimum) and reorders the optimizer
        registry to try LOOSENING first. Architectural guarantee that
        the system uses its tools intelligently to drift toward
        confident trading rather than blanket-disabling either
        direction."""
        path = os.path.join(REPO_ROOT, "self_tuning.py")
        with open(path) as f:
            src = f.read()
        assert "_runtime_under_volume_floor" in src, (
            "self_tuning.py is missing the volume-floor signal marker "
            "`_runtime_under_volume_floor`. Without this, the tuner "
            "cannot intelligently shift its bias toward action when a "
            "profile is producing too few stock entries — the failure "
            "mode of the 2026-05-14 over-restriction collapse."
        )
        assert "VOLUME-FLOOR signal" in src, (
            "Expected the volume-floor block to emit a "
            "'VOLUME-FLOOR signal' message so operators can see when "
            "the tuner is biasing toward looser adjustments."
        )

    def test_alpha_decay_has_ttl_restoration(self):
        """Every deprecation must have an automatic TTL-based
        restoration path. Sharpe-based restoration alone fails because
        deprecated strategies emit no signals to recover with —
        deprecations become permanent without TTL."""
        path = os.path.join(REPO_ROOT, "alpha_decay.py")
        with open(path) as f:
            src = f.read()
        assert "restore_expired_deprecations" in src, (
            "alpha_decay.py is missing restore_expired_deprecations(). "
            "Without TTL-based auto-restoration, deprecated strategies "
            "stay deprecated forever (they can't recover their Sharpe "
            "if they're never given the chance to emit signals)."
        )
