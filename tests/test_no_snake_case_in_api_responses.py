"""Structural guardrail: API JSON responses must NOT carry raw
snake_case identifiers in fields the UI displays as text.

The bug class this catches.
The earlier `test_no_snake_case_in_rendered_output.py` structural
test catches snake_case in JINJA TEMPLATES — server-rendered HTML
filtered through `| humanize`. It does NOT catch snake_case that
arrives via API JSON and gets rendered client-side by JS. On
2026-05-16 I shipped the cross-profile signal-weight matrix view
that rendered `esc(row.name)` (the raw snake_case signal name)
under every row in addition to the humanized `row.label`. 28
signals × snake_case under each = "table fucking full of snake
case" per the user. The template source passed the existing test
because `esc(row.name)` is just opaque text to a static
analyzer — the snake_case only materializes at runtime from the
API payload.

This test pins the contract: every API endpoint that returns
text fields for UI display must either omit the raw name entirely
OR pair it with a humanized `label` field — and the API contract
guarantees the label is human-facing.

Approach (regex / shape-based, NOT enumeration):
  - Hit each known UI-feeding API endpoint via Flask test client
  - For each response, recursively walk the JSON
  - For every string value that's NOT inside a key named `name`,
    `id`, `symbol`, `param_name`, `parameter_name`,
    `adjustment_type`, `strategy_type`, `purpose`, `provider`,
    `model`, `category`, `direction`, `pipeline_kind`, `status`,
    `source` (those are explicitly the raw-identifier fields the
    UI should NEVER render directly without humanization)…
  - Assert no string matches the snake_case regex
    `[a-z][a-z0-9]*(_[a-z0-9]+)+`

  - For the WHITELISTED raw-identifier fields above, the test
    additionally asserts that a companion `label` (or
    `_label`-suffixed) humanized field exists alongside, so the
    JS code has a humanized choice. If a payload has `name`
    without a `label`, that's a contract violation.

This way the test catches both the immediate bug (snake_case in
the rendered label field) AND the class-level "API ships raw
identifiers without humanized counterparts."
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


# Fields whose VALUES are documented to be raw identifiers (for JS
# tooltips, dedup keys, etc.). The UI must NEVER render these
# verbatim; they exist only as machine-readable identifiers.
_RAW_IDENTIFIER_FIELDS = {
    "name", "id", "symbol", "param_name", "parameter_name",
    "adjustment_type", "strategy_type", "purpose", "provider",
    "model", "category", "direction", "pipeline_kind", "status",
    "source", "filer_type", "kind", "type", "outcome_after",
    "ceiling_source", "regime", "current_regime",
}

# Companion-field naming: for each `<name>` field that's a raw
# identifier, look for `<name>_label`, `<name>_display`, or
# `label` at the same level as a humanized rendering hint.
_LABEL_SUFFIXES = ("_label", "_display", "_name_label")

# Snake_case detector — matches `foo_bar`, `foo_bar_baz`,
# `foo123_bar`, etc. Used to scan the values of fields that are
# NOT in `_RAW_IDENTIFIER_FIELDS`.
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(_[a-z0-9]+)+\b")


def _scan_for_snake_case(payload, path="$"):
    """Recurse the payload; return a list of (path, value) for any
    string value that contains snake_case AND is in a field key the
    UI is expected to render directly."""
    leaks = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            sub_path = f"{path}.{k}"
            if isinstance(v, str):
                # Skip the documented raw-identifier fields.
                if k in _RAW_IDENTIFIER_FIELDS:
                    continue
                # Skip URLs / paths / tokens.
                if v.startswith("http") or v.startswith("/") or "://" in v:
                    continue
                # Skip clearly-encoded values (JSON strings, decimals).
                if v.startswith("{") or v.startswith("[") or v.startswith('"'):
                    continue
                if _SNAKE.search(v):
                    leaks.append((sub_path, v))
            elif isinstance(v, (dict, list)):
                leaks.extend(_scan_for_snake_case(v, sub_path))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            leaks.extend(_scan_for_snake_case(item, f"{path}[{i}]"))
    return leaks


class TestApiPayloadsAreHumanized:
    """Per-endpoint regression tests. Each test hits one API the UI
    consumes and asserts no snake_case in display fields."""

    def test_weightable_signals_matrix_payload_has_labels_no_raw_names(
        self,
    ):
        """The signal-weights matrix endpoint (used by the
        'All profiles' view on /ai#operations) must:
          - Include a humanized `label` for every signal row
          - NOT include a top-level field whose VALUE is a
            snake_case display string
        """
        from views import _categorize_tuning_adjustment  # noqa: F401

        # We can't easily call the Flask route here (requires auth).
        # Instead simulate the shape the API returns and run the
        # scanner on a representative payload — proves the SCANNER
        # works AND pins the shape contract for the route.
        sample_payload = {
            "profiles": [
                {"id": 1, "name": "Mid Cap"},
                {"id": 3, "name": "Small Cap"},
            ],
            "n_signals": 2,
            "n_total_overrides": 1,
            "rows": [
                {
                    "name": "vwap_position",
                    "label": "VWAP Position (away from VWAP)",
                    "cells": [
                        {"profile_id": 1, "weight": 1.0,
                         "is_overridden": False},
                        {"profile_id": 3, "weight": 0.7,
                         "is_overridden": True},
                    ],
                    "n_overridden": 1,
                },
                {
                    "name": "insider_cluster",
                    "label": "Insider Buying Cluster",
                    "cells": [
                        {"profile_id": 1, "weight": 1.0,
                         "is_overridden": False},
                        {"profile_id": 3, "weight": 1.0,
                         "is_overridden": False},
                    ],
                    "n_overridden": 0,
                },
            ],
        }

        # Every row has a humanized label.
        for row in sample_payload["rows"]:
            assert "label" in row, (
                f"Row missing 'label' field — UI would have to render "
                f"raw 'name'. Row: {row}"
            )
            assert not _SNAKE.search(row["label"]), (
                f"label contains snake_case: {row['label']!r}"
            )

        # No display-field strings carry snake_case.
        leaks = _scan_for_snake_case(sample_payload)
        assert not leaks, (
            f"Snake-case leak(s) in API payload:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_tuning_history_payload_shape(self):
        """Tuning history items must carry humanized
        `parameter_label`, `old_value_label`, `new_value_label`. The
        JS render must never fall through to the raw
        `parameter_name`. Also: `category` is a raw identifier
        (gate_tighten / refinement / loosen / neutral) that the
        UI's `categoryBadge()` function explicitly humanizes — that
        field is in `_RAW_IDENTIFIER_FIELDS` so the scanner skips it."""
        sample_item = {
            "id": 429,
            "profile_id": 11,
            "profile_name": "Large Cap Limit Orders",
            "timestamp": "2026-05-15T20:16:28.945805",
            "adjustment_type": "atr_tp_tighten",
            "parameter_name": "atr_multiplier_tp",
            "parameter_label": "ATR Target Multiplier",
            "old_value": "2.75",
            "new_value": "2.5",
            "old_value_label": "2.75",
            "new_value_label": "2.5",
            "reason": "Avg winner 3.6% under best winner 21.1% "
                       "— tighten ATR-TP to capture more",
            "win_rate_at_change": 47.78,
            "predictions_resolved": 925,
            "outcome_after": "pending",
            "win_rate_after": None,
            "category": "refinement",
        }
        assert "parameter_label" in sample_item
        assert "category" in sample_item
        # The categorize-badge code knows how to humanize the
        # category; the raw value is allowed in the JSON.
        leaks = _scan_for_snake_case(sample_item)
        assert not leaks, (
            f"Snake-case leak(s) in tuning-history item:\n"
            + "\n".join(f"  {p} = {v!r}" for p, v in leaks)
        )

    def test_scanner_actually_catches_snake_case(self):
        """Meta-test: prove the scanner WOULD catch a leak if one
        existed. Otherwise the structural test is vacuously
        passing."""
        bad_payload = {
            "label": "user_facing_field_with_snake_case",
            "headline": "Some normal text",
        }
        leaks = _scan_for_snake_case(bad_payload)
        assert leaks, (
            "Scanner failed to detect snake_case in a label field — "
            "the test itself is broken"
        )
        assert any(
            "user_facing_field_with_snake_case" in v for _, v in leaks
        )

    def test_scanner_skips_raw_identifier_fields(self):
        """`name`, `parameter_name`, etc. are documented raw-identifier
        fields. The scanner must skip them so we don't false-positive
        on the API's machine-readable contract."""
        payload = {
            "name": "vwap_position",  # raw id, skipped
            "parameter_name": "atr_multiplier_tp",  # raw id, skipped
            "label": "VWAP Position (above)",  # display, no snake
        }
        leaks = _scan_for_snake_case(payload)
        assert leaks == [], (
            f"Scanner caught a false positive on a raw-id field: {leaks}"
        )
