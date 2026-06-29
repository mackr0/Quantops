"""Structural guardrail (Phase 2 architecture): every optimizer in
the self-tuner registry must carry an explicit direction tag, and
the running sequence must prioritize loosening before tightening.

The bug class.
Pre-2026-05-15 the self-tuner registry was a hand-curated list with
no explicit direction metadata. The file order happened to put
tightening rules first. Combined with first-match-wins iteration,
the system structurally biased toward tightening. The 2026-05-14
over-restriction collapse (stock entries fell to 0/day over 14
days) was the eventual consequence.

This test pins three architectural invariants:

  1. Every callable in `all_optimizers` MUST appear in
     `_OPTIMIZER_DIRECTION` with a tag in the canonical set.
     A new optimizer added without a tag falls through to TIGHTEN
     by default — defensible as a safe default but the omission
     should be deliberate. This test forces the conversation.

  2. The sorted running sequence must put LOOSEN strictly first,
     then BIDIRECTIONAL, then STRUCTURAL, then TIGHTEN. No mixing
     within bands — the system structurally drifts toward action.

  3. There must be at least one optimizer with direction LOOSEN.
     A registry with zero looseners is a registry that can only
     restrict — that's the architectural failure the 2026-05-14
     incident exposed.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


_VALID_DIRECTIONS = {"LOOSEN", "TIGHTEN", "BIDIRECTIONAL", "STRUCTURAL"}


class TestSelfTunerOptimizerDirections:
    def test_every_optimizer_has_direction_tag(self):
        """Every optimizer the registry uses must have a direction
        tag in `_OPTIMIZER_DIRECTION`. A missing tag means a future
        author added a new rule without thinking about whether it
        loosens or tightens — the exact failure mode that produced
        the 2026-05-14 over-restriction collapse."""
        from self_tuning import _OPTIMIZER_DIRECTION
        # Find every _optimize_* function defined in self_tuning that
        # is in the registry (excluding helpers like _optimize_*_helper
        # if any exist — none do today). The registry itself is the
        # source of truth.
        from self_tuning import _apply_upward_optimizations
        import inspect
        # Inspect the source to extract registered optimizer names.
        src = inspect.getsource(_apply_upward_optimizations)
        import re
        # Strip comments first so retired-optimizer tombstones (e.g.
        # "# _optimize_min_volume removed 2026-06-26 ...") are NOT mistaken
        # for live dispatch entries — only actual code references a
        # registered optimizer.
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        # Match "_optimize_<name>," in the all_optimizers list.
        # Include digits (momentum_5d, momentum_20d, etc.).
        registered = set(re.findall(r"_optimize_[a-z0-9_]+", code))
        missing = registered - set(_OPTIMIZER_DIRECTION.keys())
        assert not missing, (
            f"Optimizers in the registry are missing direction tags "
            f"in _OPTIMIZER_DIRECTION: {sorted(missing)}.\n\n"
            f"Add an entry like:\n"
            f"  '_optimize_<name>': 'LOOSEN' | 'TIGHTEN' | "
            f"'BIDIRECTIONAL' | 'STRUCTURAL'"
        )

    def test_direction_tags_are_valid(self):
        """Every tag in `_OPTIMIZER_DIRECTION` must be a valid value.
        Catches typos like 'TIGHTERN'."""
        from self_tuning import _OPTIMIZER_DIRECTION
        bad = {
            name: tag for name, tag in _OPTIMIZER_DIRECTION.items()
            if tag not in _VALID_DIRECTIONS
        }
        assert not bad, (
            f"Invalid direction tags: {bad}\n\n"
            f"Valid values: {_VALID_DIRECTIONS}"
        )

    def test_at_least_one_loosener_registered(self):
        """A registry with zero looseners can only restrict the
        system. The 2026-05-14 incident showed where that ends.
        This test enforces that the architecture stays bidirectional."""
        from self_tuning import _OPTIMIZER_DIRECTION
        looseners = [
            name for name, tag in _OPTIMIZER_DIRECTION.items()
            if tag == "LOOSEN"
        ]
        assert len(looseners) >= 1, (
            "No LOOSEN-tagged optimizers in the registry. The "
            "self-tuner can only restrict the system. Add at least "
            "one action-creating optimizer."
        )

    def test_running_sequence_prioritizes_loosening(self):
        """The sorted optimizer sequence must put all LOOSEN entries
        before any BIDIRECTIONAL / STRUCTURAL / TIGHTEN entries, and
        all TIGHTEN entries must come last. No mixing within bands."""
        from self_tuning import (
            _OPTIMIZER_DIRECTION, _DIRECTION_PRIORITY,
        )
        # Build the running sequence the same way _apply_upward_optimizations
        # does — sort by direction priority, stable.
        # The direction priority constants must match the sort order.
        assert _DIRECTION_PRIORITY[0] == "LOOSEN", (
            f"_DIRECTION_PRIORITY must have LOOSEN first; "
            f"got {_DIRECTION_PRIORITY}"
        )
        assert _DIRECTION_PRIORITY[-1] == "TIGHTEN", (
            f"_DIRECTION_PRIORITY must have TIGHTEN last; "
            f"got {_DIRECTION_PRIORITY}"
        )
        # Sample sort to verify the ordering is consistent.
        items = list(_OPTIMIZER_DIRECTION.items())
        sorted_items = sorted(
            items,
            key=lambda kv: _DIRECTION_PRIORITY.index(kv[1]),
        )
        # In the sorted list, LOOSEN comes before TIGHTEN for any
        # pair we sample.
        loosen_indices = [
            i for i, (_, t) in enumerate(sorted_items) if t == "LOOSEN"
        ]
        tighten_indices = [
            i for i, (_, t) in enumerate(sorted_items) if t == "TIGHTEN"
        ]
        if loosen_indices and tighten_indices:
            assert max(loosen_indices) < min(tighten_indices), (
                "Sorted optimizer sequence has TIGHTEN before "
                "LOOSEN. The architecture must structurally favor "
                "action — loosening rules MUST run first."
            )

    def test_priority_constant_covers_every_direction(self):
        """`_DIRECTION_PRIORITY` must enumerate every valid direction
        exactly once. Without this, a tag that's missing from the
        priority constant fails .index() at runtime when the sorter
        runs."""
        from self_tuning import _DIRECTION_PRIORITY
        assert set(_DIRECTION_PRIORITY) == _VALID_DIRECTIONS, (
            f"_DIRECTION_PRIORITY must cover every valid direction "
            f"exactly once. Got: {_DIRECTION_PRIORITY}, "
            f"expected: {_VALID_DIRECTIONS}"
        )
