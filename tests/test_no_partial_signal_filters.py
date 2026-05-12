"""Class-invariant guardrails against the cherry-pick bug class
(2026-05-12).

Three patterns repeatedly produce silent data drops:

1. SINGLE-SIGNAL FILTER — `predicted_signal = 'BUY'` or
   `predicted_signal IN ('BUY', 'SELL')`. Misses STRONG_BUY,
   WEAK_BUY, STRONG_SELL, WEAK_SELL, SHORT, COVER variants.
   The 2026-05-12 audit found this in kelly_sizing,
   self_tuning, ai_tracker (avg_return_on_buys), and the
   legacy CASE in specialist_calibration.

2. NEUTRAL-INCLUDED WIN-RATE DENOMINATOR —
   `wins / total_resolved` where total includes neutrals.
   Neutrals are timeouts; including them dilutes the win
   rate and can trigger spurious self-tuning rollbacks.
   The 2026-05-12 audit found this in models.py rollback
   trigger.

3. PARTIAL PIPELINE_KIND BACKFILL — covered by
   test_pipeline_kind_completeness.py.

This test scans production source for these patterns and fails
if a NEW occurrence lands without explicit allowlisting. The
allowlist requires a written rationale (e.g., "intentionally
counts only the BUY signal because <reason>"); empty allowlist
entries fail the consistency check.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

REPO_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


# ---------------------------------------------------------------------------
# Pattern 1 — single-signal filter
# ---------------------------------------------------------------------------

# Allowlist: (file:line_substring) → rationale.
# Add entries when an intentionally-narrow filter is correct
# (e.g., resolver branch logic that only handles one signal type).
PARTIAL_SIGNAL_ALLOWLIST = {
    # ai_tracker._migrate_prediction_type — one-shot migration to
    # set prediction_type for legacy rows. Each branch handles ONE
    # signal type, so single-signal filters are correct.
    "predicted_signal IN ('BUY', 'HOLD', 'STRONG_BUY')":
        "ai_tracker._migrate_prediction_type — legacy long-direction inference",
    "predicted_signal IN ('SHORT', 'STRONG_SHORT')":
        "ai_tracker._migrate_prediction_type — legacy short-direction inference",
    "predicted_signal IN ('SELL', 'STRONG_SELL')":
        "ai_tracker._migrate_prediction_type — legacy SELL-as-short or "
        "exit-quality classification",
    # HOLD has no STRONG_HOLD or WEAK_HOLD variants in the system —
    # the signal class is intrinsically single-element. Single-
    # signal filters on HOLD are correct by definition.
    "predicted_signal='HOLD'":
        "HOLD is intrinsically single-element (no STRONG_HOLD/WEAK_HOLD)",
    # Resolver branch logic: signal is the dispatch key, not a
    # filter. Each branch genuinely handles one signal class.
}


def _scan_source(file_relpath: str, pattern: re.Pattern) -> list:
    """Return list of matched lines as (line_number, text) tuples."""
    full = os.path.join(REPO_ROOT, file_relpath)
    if not os.path.exists(full):
        return []
    matches = []
    with open(full) as fh:
        for n, line in enumerate(fh, 1):
            if pattern.search(line):
                matches.append((n, line.rstrip()))
    return matches


# Files that get scanned (production-side analytics / training).
# Add new files as the system grows.
SCANNED_FILES = [
    "ai_tracker.py", "self_tuning.py", "kelly_sizing.py",
    "models.py", "meta_model.py", "online_meta_model.py",
    "post_mortem.py", "alpha_decay.py", "insight_propagation.py",
    "specialist_calibration.py",
]


class TestNoPartialSignalFilters:
    """Pin: every occurrence of `predicted_signal IN (...)` or
    `predicted_signal = '<one>'` either lists ALL relevant signal
    variants OR appears in PARTIAL_SIGNAL_ALLOWLIST with a
    rationale."""

    # Pattern: predicted_signal IN ('a', 'b', ...) with fewer than
    # 3 entries OR predicted_signal = 'X' for a single signal.
    SINGLE_SIGNAL_PATTERN = re.compile(
        r"predicted_signal\s*=\s*['\"]([A-Z_]+)['\"]"
    )

    # Pattern: IN list — capture the full list to count entries
    IN_LIST_PATTERN = re.compile(
        r"predicted_signal\s+IN\s*\(([^)]+)\)"
    )

    # Signal variants we expect to see TOGETHER when handling
    # entry signals broadly (lists shorter than this likely miss
    # variants).
    LONG_ENTRY_FAMILY = {"BUY", "STRONG_BUY", "WEAK_BUY"}
    SHORT_ENTRY_FAMILY = {"SELL", "STRONG_SELL", "WEAK_SELL",
                            "SHORT", "COVER"}

    def test_no_unexpected_single_signal_filters(self):
        offenses = []
        for relpath in SCANNED_FILES:
            for ln, text in _scan_source(
                relpath, self.SINGLE_SIGNAL_PATTERN,
            ):
                # Allowlisted strings appear verbatim in the line.
                if any(allow in text for allow in PARTIAL_SIGNAL_ALLOWLIST):
                    continue
                # Single-signal filters in the source code that
                # are NOT in the allowlist are suspicious.
                offenses.append(f"{relpath}:{ln}  {text.strip()}")

        if offenses:
            pytest.fail(
                "Single-signal filter detected without allowlist "
                "entry. Either (a) expand the IN list to include "
                "all variants of the signal family (BUY → "
                "BUY/STRONG_BUY/WEAK_BUY etc.), or (b) add an "
                "entry to PARTIAL_SIGNAL_ALLOWLIST with a written "
                "rationale.\n\n"
                + "\n".join(f"  {o}" for o in offenses)
            )

    def test_no_partial_in_list_for_entry_signals(self):
        """When the IN list includes BUY but misses STRONG_BUY/
        WEAK_BUY, that's the cherry-pick pattern. Same for SELL."""
        offenses = []
        for relpath in SCANNED_FILES:
            for ln, text in _scan_source(
                relpath, self.IN_LIST_PATTERN,
            ):
                if any(allow in text for allow in PARTIAL_SIGNAL_ALLOWLIST):
                    continue
                m = self.IN_LIST_PATTERN.search(text)
                if not m:
                    continue
                # Extract individual signals from the IN list
                tokens = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
                # If list mentions BUY but is missing variants, flag it.
                if "BUY" in tokens and not (
                    self.LONG_ENTRY_FAMILY <= tokens
                    or "HOLD" in tokens   # broader-set inclusion OK
                ):
                    offenses.append(
                        f"{relpath}:{ln}  has BUY but missing "
                        f"{sorted(self.LONG_ENTRY_FAMILY - tokens)}: "
                        f"{text.strip()}"
                    )
                if "SELL" in tokens and not (
                    self.SHORT_ENTRY_FAMILY <= tokens | {"SELL"}
                    or "HOLD" in tokens
                ):
                    # SELL without all sell variants is suspicious
                    if not any(t in tokens for t in
                                ("STRONG_SELL", "WEAK_SELL")):
                        offenses.append(
                            f"{relpath}:{ln}  has SELL but missing "
                            f"STRONG_SELL/WEAK_SELL: {text.strip()}"
                        )

        if offenses:
            pytest.fail(
                "Partial signal-family IN list detected. Either "
                "expand the list to include all variants, or add "
                "an entry to PARTIAL_SIGNAL_ALLOWLIST.\n\n"
                + "\n".join(f"  {o}" for o in offenses)
            )


# ---------------------------------------------------------------------------
# Pattern 2 — neutral-included win-rate denominator
# ---------------------------------------------------------------------------

# Allowlist: (substring of a context-distinguishing line near the
# offender) → rationale.
NEUTRAL_DENOM_ALLOWLIST = {
    # The "10 new resolved" cadence gate in models.py — counts ALL
    # resolutions including neutrals because it's measuring CADENCE
    # (how long since the param change), not quality.
    "kept for backwards compat":
        "ai_tracker.get_ai_performance — blended-rate intentional, "
        "directional_win_rate is the quality metric",
}


class TestNoNeutralDilutedWinRate:
    """Pin: every win-rate calculation uses
    `wins / (wins+losses)` not `wins / total_resolved` —
    neutrals are timeouts and dilute the metric.
    """

    def test_no_undocumented_neutral_dilution(self):
        # Pattern: SELECT COUNT FROM ... status='resolved' followed
        # within ~4 lines by COUNT actual_outcome='win', without
        # actual_outcome IN ('win','loss') filter.
        # Hard to fully detect via regex alone, so we check the
        # known-fixed sites for the correct pattern.
        sites = {
            "models.py": "actual_outcome IN ('win', 'loss')",
        }
        for relpath, expected_filter in sites.items():
            full = os.path.join(REPO_ROOT, relpath)
            if not os.path.exists(full):
                continue
            text = open(full).read()
            assert expected_filter in text, (
                f"{relpath} should contain "
                f"{expected_filter!r} (the win-rate denominator "
                f"must EXCLUDE neutrals to avoid spurious "
                f"rollbacks). 2026-05-12 fix."
            )


# ---------------------------------------------------------------------------
# Allowlist hygiene — no stale entries
# ---------------------------------------------------------------------------

class TestAllowlistHygiene:
    """Each allowlist entry must still match an actual pattern in
    the codebase. Stale entries indicate the offender was removed
    but the allowlist wasn't cleaned up — bloat that masks future
    additions."""

    def test_partial_signal_allowlist_entries_still_match(self):
        for snippet, rationale in PARTIAL_SIGNAL_ALLOWLIST.items():
            assert rationale, (
                f"Empty rationale for allowlist entry: {snippet}"
            )
            found = False
            for relpath in SCANNED_FILES:
                full = os.path.join(REPO_ROOT, relpath)
                if not os.path.exists(full):
                    continue
                if snippet in open(full).read():
                    found = True
                    break
            assert found, (
                f"Stale PARTIAL_SIGNAL_ALLOWLIST entry: {snippet!r} "
                f"no longer appears anywhere in scanned source. "
                f"Remove the allowlist entry."
            )
